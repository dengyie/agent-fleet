"""Task 7 tests — Supervisor.attach_to_existing / detach (纳管既有进程).

Covers:
- the exact 6-code bounded return set:
  adopted | pid_reused | exe_changed | no_permission |
  unsupported_family | native_file_unreadable
- gate ordering (fail closed at first non-success): family allowlist FIRST
  (probe must not even be called), native-file readability second, a live
  identity re-read third (pid gone / started_at mismatch / exe mismatch),
  and the already-bound idempotency check last;
- a successful attach binds ONLY the private (pid, pgid, started_at,
  exe_path) handle: the durable manifest and public status keep the bounded
  field set with an opaque ``grp_<hex>`` and never show raw values;
- detach removes the entry + on-disk manifest WITHOUT any signal (only
  ``ops.close``), is idempotent, and an unknown session is a no-op;
- an attached session is still governable: ``pause_session`` reaches the fake
  process group through the fake ops (pgid), mirroring the plan's
  ``test_attached_pause_signals_pgid``.

No real process or real signal is ever used: the `Supervisor` runs against an
in-memory ops fake and the identity reader is an injected ``probe`` (the same
duck-typed seam the production code uses with ``discovery.enumerate_*``).
"""
from __future__ import annotations

import json
import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.supervisor import supervisor as sup_mod


def _rfc3339() -> str:
    return "2026-08-30T10:00:00Z"


# --------------------------------------------------------------------------- #
# deterministic fakes (no subprocess, no real signals)
# --------------------------------------------------------------------------- #

class _FakeProcess:
    """The existing OS process whose identity the injected probe mirrors."""

    def __init__(self, pid, started_at, exe_path, pgid=None):
        self.pid = pid
        self.pgid = pgid if pgid is not None else pid
        self.started_at = started_at
        self.exe_path = exe_path


class _FakeHandle:
    """The private handle the fake ops returns for an attached pid."""

    def __init__(self, pid, started_at, exe_path, pgid=None):
        self.pid = int(pid)
        self.pgid = pgid if pgid is not None else int(pid)
        self.started_at = started_at
        self.exe_path = exe_path
        self.alive = True
        self.signals = []          # any signal routed to the process
        self.group_signals = []    # (pgid, sig) tuples routed to the group

    def poll(self):
        # the process is untouched (no reaping); we stay "running"
        return None


class _FakeOps(sup_mod.GroupOps):
    """In-memory GroupOps: records every signal/close, never touches OS."""

    def __init__(self):
        super().__init__(platform="test")
        self.attach_calls = []
        self.create_calls = []
        self.group_terminate_calls = []   # sigs attempted via the ops
        self.group_kill_calls = []
        self.closed = []

    def capability(self):
        return ("process_group_only", "1")

    def attach(self, pid, started_at, exe_path):
        handle = _FakeHandle(pid, started_at, exe_path)
        self.attach_calls.append(handle)
        return handle

    def create(self, argv, cwd, env):
        self.create_calls.append({
            "argv": list(argv),
            "cwd": cwd,
            "env": dict(env or {}),
        })
        handle = _FakeHandle(pid=9000, started_at=_rfc3339(),
                             exe_path=argv[0] if argv else "/bin/false")
        return handle

    def group_alive(self, handle):
        return handle.alive

    def group_terminate(self, handle, sig):
        self.group_terminate_calls.append(sig)
        handle.signals.append(sig)
        handle.group_signals.append((handle.pgid, sig))
        return True

    def group_kill(self, handle, sig):
        self.group_kill_calls.append(sig)
        handle.signals.append(sig)
        handle.group_signals.append((handle.pgid, sig))
        return True

    def group_reap(self, handle, timeout=None):
        return True

    def close(self, handle):
        self.closed.append(handle)


def _probe_of(proc, *, started_at=None, exe_path=None):
    """Default injected identity reader mirroring ``proc`` (or overrides)."""

    def probe(pid):
        if int(pid) != int(proc.pid):
            return None
        return (started_at if started_at is not None else proc.started_at,
                exe_path if exe_path is not None else proc.exe_path)

    return probe


# --------------------------------------------------------------------------- #
# suite
# --------------------------------------------------------------------------- #

class AttachTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-attach-")
        self.dir = Path(self.tmp)
        self.ops = _FakeOps()
        self.sup = sup_mod.Supervisor(
            manifest_dir=self.dir / "sup", machine_id="test-host", ops=self.ops)
        self.proc = _FakeProcess(
            pid=4242, started_at=_rfc3339(), exe_path="/opt/example/bin/agent")
        self.sid = "adopt_feed0123cafe"

    def _attach(self, **kw):
        defs = dict(
            pid=self.proc.pid,
            started_at=self.proc.started_at,
            exe_path=self.proc.exe_path,
            native_file_path=None,
            agent_family="codex",
            session_id=self.sid,
            probe=_probe_of(self.proc),
        )
        defs.update(kw)
        return self.sup.attach_to_existing(**defs)

    # ---- success / bounded surface ---------------------------------------- #

    def test_attach_success_returns_adopted_binds_private_and_stays_public_bounded(self):
        result = self._attach()
        self.assertEqual(result, "adopted")

        rows = self.sup.all_status()
        self.assertEqual(sorted(rows), [self.sid])
        row = rows[self.sid]
        self.assertTrue(row["process_group_id"].startswith("grp_"))
        self.assertEqual(
            set(row),
            {"session_id", "attempt_id", "process_group_id", "agent",
             "machine_id", "managed", "state", "process_group_capability",
             "platform", "reason"},
        )
        self.assertEqual(row["state"], "running")
        self.assertEqual(row["agent"], "codex")
        # raw pid must NEVER appear inside any public value (deterministic:
        # no JSON value parses to it).
        for value in row.values():
            if isinstance(value, int):
                self.assertNotEqual(value, self.proc.pid)
            if isinstance(value, str) and value.isdigit():
                self.assertNotEqual(int(value), self.proc.pid)
        text = json.dumps(row)
        for priv in ("pid", "pgid", "exe_path", "native_file_path", "started_at"):
            self.assertNotIn(priv, text)

        # the private values stay on the handle ONLY ...
        handle = self.sup._handle_of(self.sid)
        self.assertEqual(handle.pid, self.proc.pid)
        self.assertEqual(handle.pgid, self.proc.pgid)
        self.assertEqual(handle.started_at, self.proc.started_at)
        self.assertEqual(handle.exe_path, self.proc.exe_path)

        # ... and never reach the durable manifest bytes.
        raw = (self.dir / "sup" / f"{self.sid}.json").read_text()
        for priv in ("pid", "pgid", "exe_path", "native_file_path", "started_at"):
            self.assertNotIn(priv, raw)
        for private_value in (self.proc.exe_path, self.proc.started_at):
            self.assertNotIn(private_value, raw)

    # ---- gate 3: identity re-read (fail closed) --------------------------- #

    def test_reused_pid_fails_closed(self):
        # caller's stale started_at does not match the re-read reality
        result = self._attach(started_at="2026-08-29T00:00:00Z")
        self.assertEqual(result, "pid_reused")
        self.assertEqual(self.sup.all_status(), {})
        # nothing was ever bound — the handle never received a signal
        self.assertEqual(self.ops.attach_calls, [])
        self.assertEqual(self.ops.group_terminate_calls, [])
        self.assertEqual(self.ops.group_kill_calls, [])

    def test_exe_changed_rejected(self):
        result = self._attach(exe_path="/usr/bin/unexpected")
        self.assertEqual(result, "exe_changed")
        self.assertEqual(self.sup.all_status(), {})
        self.assertEqual(self.ops.attach_calls, [])

    def test_attach_gone_pid_fails_closed(self):
        result = self._attach(probe=lambda pid: None)
        self.assertEqual(result, "pid_reused")
        self.assertEqual(self.sup.all_status(), {})

    def test_reader_exception_fails_closed_no_permission(self):
        def probe(pid):
            raise OSError("identity unreadable")

        result = self._attach(probe=probe)
        self.assertEqual(result, "no_permission")
        self.assertEqual(self.sup.all_status(), {})
        self.assertEqual(self.ops.attach_calls, [])

    # ---- gate 2: native transcript gate ----------------------------------- #

    def test_native_file_unreadable_rejected(self):
        # a path that does not exist
        missing = str(self.dir / "ghost.jsonl")
        self.assertEqual(self._attach(native_file_path=missing),
                         "native_file_unreadable")
        self.assertEqual(self.sup.all_status(), {})

        # a real file with no read permission (same user, chmod 000)
        blocked = self.dir / "blocked.jsonl"
        blocked.write_text("x")
        os.chmod(blocked, 0)
        try:
            self.assertEqual(
                self._attach(native_file_path=str(blocked)),
                "native_file_unreadable")
        finally:
            os.chmod(blocked, 0o600)
        self.assertEqual(self.sup.all_status(), {})

    # ---- gate 3 ordering: unsupported family never touches the probe ------- #

    def test_unsupported_family_rejected_never_reads(self):
        calls = []

        def probe(pid):
            calls.append(pid)
            return (self.proc.started_at, self.proc.exe_path)

        result = self._attach(agent_family="bogus_thing", probe=probe)
        self.assertEqual(result, "unsupported_family")
        self.assertEqual(calls, [], "family gate must run before any probe")
        self.assertEqual(self.sup.all_status(), {})

    # ---- idempotency / already-bound ------------------------------------- #

    def test_repeat_attach_same_session_idempotent(self):
        self.assertEqual(self._attach(), "adopted")
        again = self._attach()   # same pid + same session id + same probe
        self.assertEqual(again, "adopted")
        self.assertEqual(len(self.sup.all_status()), 1)   # one entry
        self.assertEqual(len(self.ops.attach_calls), 1)   # one handle only

    def test_repeat_attach_different_pid_same_session_fails_closed(self):
        self.assertEqual(self._attach(), "adopted")
        other = _FakeProcess(pid=9999, started_at=_rfc3339(),
                             exe_path="/usr/bin/other")
        result = self._attach(pid=other.pid, exe_path=other.exe_path,
                              probe=_probe_of(other))
        self.assertEqual(result, "pid_reused")
        # the original attachment is untouched
        self.assertEqual(len(self.sup.all_status()), 1)
        self.assertEqual(len(self.ops.attach_calls), 1)

    # ---- detach (never signals) ------------------------------------------- #

    def test_detach_removes_entry_never_signals(self):
        self.assertEqual(self._attach(), "adopted")
        handle = self.sup._handle_of(self.sid)
        manifest_file = self.dir / "sup" / f"{self.sid}.json"
        self.assertTrue(manifest_file.exists())

        self.sup.detach(self.sid)

        # no signal path was ever reached ...
        self.assertEqual(self.ops.group_terminate_calls, [])
        self.assertEqual(self.ops.group_kill_calls, [])
        self.assertEqual(handle.signals, [])
        self.assertEqual(handle.group_signals, [])
        self.assertEqual(handle.poll(), None)      # process untouched
        # ... only a metadata close happened
        self.assertIn(handle, self.ops.closed)
        # entry + durable manifest both gone
        self.assertEqual(self.sup.all_status(), {})
        self.assertFalse(manifest_file.exists())

    def test_detach_idempotent(self):
        self.assertEqual(self._attach(), "adopted")
        self.sup.detach(self.sid)
        self.sup.detach(self.sid)   # second call: no-op, no exception
        self.assertEqual(self.sup.all_status(), {})
        self.assertEqual(self.ops.group_terminate_calls, [])
        self.assertEqual(self.ops.group_kill_calls, [])
        self.assertEqual(list((self.dir / "sup").glob("*.json")), [])

    def test_detach_unknown_session_noop(self):
        self.assertEqual(self._attach(), "adopted")
        self.assertIsNone(self.sup.detach("adopt_ffffffff"))
        self.assertEqual(len(self.sup.all_status()), 1)   # nothing changed

    # ---- attached sessions stay governable -------------------------------- #

    def test_attached_pause_signals_pgid(self):
        self.assertEqual(self._attach(), "adopted")
        outcome = self.sup.pause_session(self.sid)
        self.assertEqual(outcome, "paused")
        attached = self.ops.attach_calls[0]
        self.assertEqual(attached.group_signals, [(attached.pgid, signal.SIGSTOP)])
        self.assertEqual(attached.signals, [signal.SIGSTOP])

    def test_attach_binds_native_identity_privately_not_in_public_status(self):
        native = self.dir / "rollout-native.jsonl"
        native.write_text("{}")
        os.chmod(native, 0o600)
        result = self._attach(native_file_path=str(native))
        self.assertEqual(result, "adopted")
        handle = self.sup._handle_of(self.sid)
        entry = self.sup._entries[self.sid]
        self.assertEqual(getattr(handle, "native_file_path", None), str(native))
        self.assertEqual(getattr(entry, "native_file_path", None), str(native))
        public = json.dumps(self.sup.status(self.sid))
        raw = (self.dir / "sup" / f"{self.sid}.json").read_text()
        for priv in ("pid", "cwd", "argv", "env", "native_file_path",
                      str(native), self.proc.exe_path):
            self.assertNotIn(priv, public)
            self.assertNotIn(priv, raw)

    def test_attached_follow_up_without_native_identity_refuses_sibling(self):
        self.assertEqual(self._attach(), "adopted")
        manifest = self.sup.get(self.sid)
        manifest.capability_manifest = {"resume": True}
        before = list(self.ops.create_calls)
        self.assertEqual(
            self.sup.append_user_turn(self.sid, "continue"),
            "unsupported_action",
        )
        self.assertEqual(self.ops.create_calls, before)
        self.assertEqual(self.sup.get(self.sid).state, "running")

    def test_attached_native_file_without_cwd_or_token_refuses_sibling(self):
        native = self.dir / "rollout-native.jsonl"
        native.write_text("{}")
        os.chmod(native, 0o600)
        self.assertEqual(self._attach(native_file_path=str(native)), "adopted")
        manifest = self.sup.get(self.sid)
        manifest.capability_manifest = {"resume": True}
        before = list(self.ops.create_calls)
        self.assertEqual(
            self.sup.append_user_turn(self.sid, "continue"),
            "unsupported_action",
        )
        self.assertEqual(self.ops.create_calls, before)

    def test_attached_native_resume_uses_session_identity_not_tmp(self):
        native = self.dir / "rollout-native.jsonl"
        native.write_text("{}")
        os.chmod(native, 0o600)
        self.assertEqual(self._attach(native_file_path=str(native)), "adopted")
        manifest = self.sup.get(self.sid)
        manifest.capability_manifest = {"resume": True}
        handle = self.sup._handle_of(self.sid)
        entry = self.sup._entries[self.sid]
        for target in (handle, entry):
            setattr(target, "native_file_path", str(native))
            setattr(target, "cwd", "/srv/jobs")
            setattr(target, "env", {"HOME": "/srv/jobs"})
            setattr(target, "resume_token", "adoptTok")
            setattr(target, "exe_path", self.proc.exe_path)
        before = list(self.ops.create_calls)
        outcome = self.sup.append_user_turn(self.sid, "continue")
        self.assertEqual(outcome, "appended")
        self.assertEqual(len(self.ops.create_calls), len(before) + 1)
        resume = self.ops.create_calls[-1]
        self.assertEqual(resume["cwd"], "/srv/jobs")
        self.assertNotEqual(resume["cwd"], "/tmp")
        self.assertEqual(resume["env"], {"HOME": "/srv/jobs"})
        self.assertIn("--resume", resume["argv"])
        self.assertIn("adoptTok", resume["argv"])
        self.assertNotIn("--ephemeral", resume["argv"])
        self.assertEqual(handle.signals, [])

    def test_attach_stamps_live_cwd_and_bounded_env_privately(self):
        native = self.dir / "2026-09-06T19-20-24-489Z_01a0782a-2aa9-75a7-a47b-4d3e0b7225a5.jsonl"
        native.write_text("{}")
        os.chmod(native, 0o600)

        def fake_cwd(pid):
            self.assertEqual(int(pid), int(self.proc.pid))
            return "/Users/mango"

        with mock.patch.object(
                sup_mod, "_live_process_cwd", side_effect=fake_cwd):
            self.assertEqual(
                self._attach(native_file_path=str(native),
                             agent_family="pi"),
                "adopted")
        entry = self.sup._entries[self.sid]
        handle = self.sup._handle_of(self.sid)
        self.assertEqual(entry.cwd, "/Users/mango")
        self.assertEqual(getattr(handle, "cwd", None), "/Users/mango")
        self.assertIsInstance(entry.env, dict)
        self.assertTrue(entry.env)
        self.assertNotEqual(entry.cwd, "/tmp")
        public = json.dumps(self.sup.status(self.sid))
        raw = (self.dir / "sup" / f"{self.sid}.json").read_text()
        self.assertNotIn("/Users/mango", public)
        self.assertNotIn("/Users/mango", raw)

    def test_pi_attached_native_resume_appends_via_session_file(self):
        native = self.dir / "2026-09-06T19-20-24-489Z_01a0782a-2aa9-75a7-a47b-4d3e0b7225a5.jsonl"
        native.write_text("{}")
        os.chmod(native, 0o600)
        with mock.patch.object(
                sup_mod, "_live_process_cwd", return_value="/Users/mango"):
            self.assertEqual(
                self._attach(native_file_path=str(native),
                             agent_family="pi"),
                "adopted")
        before = list(self.ops.create_calls)
        outcome = self.sup.append_user_turn(self.sid, "phase4 ping")
        self.assertEqual(outcome, "appended")
        self.assertEqual(len(self.ops.create_calls), len(before) + 1)
        resume = self.ops.create_calls[-1]
        self.assertEqual(resume["cwd"], "/Users/mango")
        self.assertNotEqual(resume["cwd"], "/tmp")
        self.assertIn("--session", resume["argv"])
        self.assertIn(str(native), resume["argv"])
        self.assertIn("phase4 ping", resume["argv"])
        self.assertNotIn("--resume", resume["argv"])
        self.assertNotIn("--ephemeral", resume["argv"])


class AttachSessionIdTests(unittest.TestCase):
    def test_empty_session_id_gets_opaque_adopt_key(self):
        tmp = Path(tempfile.mkdtemp(prefix="fleet-attach-id-"))
        ops = _FakeOps()
        sup = sup_mod.Supervisor(manifest_dir=tmp / "sup", machine_id="h",
                                 ops=ops)
        proc = _FakeProcess(pid=7777, started_at=_rfc3339(),
                            exe_path="/x/y")
        result = sup.attach_to_existing(
            proc.pid, proc.started_at, proc.exe_path, None, "claude_code",
            probe=_probe_of(proc))
        self.assertEqual(result, "adopted")
        (sid,) = sup.all_status()
        self.assertTrue(sid.startswith("adopt_"))


if __name__ == "__main__":
    unittest.main()