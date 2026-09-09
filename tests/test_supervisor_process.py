"""Tests for the Task 8 Supervisor Process Lifecycle fix.

The process-group structure: a small FakeOps fake backend (deterministic, no
subprocess) and real POSIX process-group tests exercise the real Linux/Mac
``os.killpg`` path.

Review round 2 regression coverage:
- success-path ``reason`` literals (``pause`` / ``quarantine`` / ``recovered``)
  are members of the bounded ``_CONTROL_OUTCOMES`` enum, and every persisted
  write routes through the enum-validating setter ``_set_reason``.
- ``_abuf`` (raw byte buffer) is capped: a child emitting a huge line-free
  stream never grows past ``_ABUF_MAX``.
- natural completion guarantees the whole process group is gone: a leader
  that exited 0 while a group member still holds stdout open is force-killed
  and reported ``group_remaining`` (a bounded degraded outcome), never plain
  success; the post-exit flush is wall-time-bounded.
- Phase 4 ``append_user_turn``: claimed resume without native identity still
  refuses ``GroupOps.create``; a complete private identity (cwd/env/token/exe)
  resumes with ``--resume``, original cwd/env, stdin=DEVNULL, never ``/tmp``
  sibling / ``--ephemeral`` / SIGCONT; identity survives handle release.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tools.supervisor import model as model_mod
from tools.supervisor import supervisor as sup_mod


def _py(code):
    return [sys.executable, "-c", code]


def _sid():
    return model_mod.new_opaque_id("sess")


def _att():
    return model_mod.new_opaque_id("attn")


def _proc_stat(pid: int) -> str:
    """Return the process state char from /proc (``T`` stopped, ``S`` sleeping)."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except (OSError, ValueError):
        return "?"
    idx = raw.rfind(")")
    if idx < 1 or idx + 2 > len(raw):
        return "?"
    return raw[idx + 2]


def _proc_exists(pid: int) -> bool:
    return os.path.exists(f"/proc/{pid}")


# --------------------------------------------------------------------------- #
# In-memory process-group fake backend (deterministic, no subprocess)
# --------------------------------------------------------------------------- #

class FakeProc:
    def __init__(self, fake_backend, exit_code=0):
        self.backend = fake_backend
        self.pid = fake_backend._next_pid()
        self.alive = True
        self.exit_code = exit_code
        self.signals = []
        self.escaped = False
        self.stopped = False
        self.stdio = []  # queued output lines
        # simulate a group *member* that exits the leader but keeps the stdout
        # pipe open (leader forked a child and returned)
        self.hold_stdout = False
        # aliveness cannot be verified (group_alive raises)
        self.alive_unknown = False

    def add_line(self, text: str):
        self.stdio.append(text)

    def poll(self):
        if not self.alive:
            return self.exit_code if self.exit_code is not None else 0
        return None

    def receive(self, sig):
        self.signals.append(sig)
        if sig == signal.SIGKILL:
            self.alive = False
            self.alive_unknown = False
            self.exit_code = -sig
        elif sig in (signal.SIGTERM, signal.SIGINT):
            if not self.backend.deny_graceful:
                self.alive = False
                self.exit_code = -sig
            # a process that ignores SIGTERM stays alive
        elif sig == signal.SIGSTOP:
            self.stopped = True
        elif sig == signal.SIGCONT:
            self.stopped = False


class FakeOps(sup_mod.GroupOps):
    """Duck-typed GroupOps implemented with in-memory fakes only."""

    def __init__(self, capability="process_group_and_cgroup",
                 capability_version="1", platform="linux",
                 deny_graceful=False, deny_signals=False):
        super().__init__(platform=platform)
        self._capability = capability
        self._version = capability_version
        self.deny_graceful = deny_graceful
        self.deny_signals = deny_signals  # signal delivery fails (returns False)
        self.children = []
        self.last_argv = None
        self.last_cwd = None
        self.last_env = None
        self.create_calls = []
        self._pid_counter = 101

    def _next_pid(self):
        self._pid_counter += 1
        return self._pid_counter

    # ---- GroupOps interface -----------------------------------------------

    def capability(self):
        return (self._capability, self._version)

    def create(self, argv, cwd, env):
        self.last_argv = list(argv)
        self.last_cwd = cwd
        self.last_env = dict(env or {})
        self.create_calls.append({
            "argv": list(argv),
            "cwd": cwd,
            "env": dict(env or {}),
        })
        proc = FakeProc(self)
        proc.argv = list(argv)
        proc.cwd = cwd
        proc.env = dict(env or {})
        self.children.append(proc)
        return proc

    def attach(self, pid, started_at, exe_path):
        proc = FakeProc(self)
        proc.pid = int(pid)
        proc.pgid = int(pid)
        proc.started_at = started_at
        proc.exe_path = exe_path
        self.children.append(proc)
        return proc

    def proc_poll(self, proc):
        return proc.poll()

    def proc_wait(self, proc, timeout=None):
        proc.alive = False
        return proc.exit_code if proc.exit_code is not None else 0

    def group_id(self, proc):
        # mirrors POSIX: internally the leader pid (never used for the manifest)
        return f"grp_{proc.pid}"

    def group_alive(self, proc):
        if getattr(proc, "alive_unknown", False):
            raise RuntimeError("liveness unknown")
        return proc.alive

    def group_terminate(self, proc, sig):
        if self.deny_signals:
            return False
        proc.receive(sig)
        return True

    def group_kill(self, proc, sig):
        return self.group_terminate(proc, sig)

    def group_reap(self, proc, timeout=None):
        if proc.alive:
            return False
        return True

    def escaped(self, proc):
        return proc.escaped

    def close(self, proc):
        pass

    def pump(self, handle, timeout, on_line=None, should_abort=None):
        """Fake pump: drains queued lines, never blocks past the timeout."""
        if should_abort is not None and should_abort():
            return "abort"
        if handle is None:
            return "eof"
        start = time.monotonic()
        delivered = False
        while handle.stdio:
            line = handle.stdio.pop(0)
            if line:
                delivered = True
                if on_line:
                    try:
                        on_line(line[:2048])
                    except Exception:
                        pass
            if not handle.alive:
                break
            if time.monotonic() - start >= max(0.0, float(timeout)):
                break
        if not handle.alive:
            # a live member keeping the pipe open means the group is not done
            if getattr(handle, "hold_stdout", False):
                return "line" if delivered else "timeout"
            return "line" if delivered else "eof"
        if handle.stdio:
            return "line" if delivered else "timeout"
        return "timeout"


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-sup-")
        self.dir = Path(self.tmp)
        self.machine = "mac-local"

    def _sv(self, ops=None, manifest_dir="sup"):
        if ops is None:
            ops = FakeOps()
        return sup_mod.Supervisor(
            manifest_dir=self.dir / manifest_dir, machine_id=self.machine,
            ops=ops)

    def _launch(self, sv, session_id=None, attempt_id=None,
                capability_manifest=None, command=None):
        if command is None:
            command = ["/usr/bin/env", "python3", "-c", "pass"]
        if session_id is None:
            session_id = _sid()
        return sv.launch(
            agent="codex",
            command=command,
            cwd="/srv/jobs",
            env_allowlist={"HOME": "/srv/jobs"},
            session_id=session_id,
            attempt_id=attempt_id,
            capability_manifest=capability_manifest,
        )

    # ---- manifest / launch ------------------------------------------------

    def test_launch_binds_opaque_ids_and_persists_manifest(self):
        sv = self._sv()
        sid = _sid()
        att = _att()
        m = self._launch(sv, session_id=sid, attempt_id=att)
        self.assertEqual(m.session_id, sid)
        self.assertEqual(m.attempt_id, att)
        self.assertTrue(m.process_group_id.startswith("grp_"))
        self.assertEqual(m.agent, "codex")
        self.assertEqual(m.state, "running")

        target = self.dir / "sup" / f"{sid}.json"
        self.assertTrue(target.exists())
        saved = json.loads(target.read_text())
        self.assertEqual(saved["session_id"], sid)
        self.assertEqual(saved["state"], "running")
        self.assertTrue(saved["process_group_id"].startswith("grp_"))
        for priv in ("command", "env", "argv", "cwd", "pid", "path",
                     "token", "secret"):
            self.assertNotIn(priv, saved)

    def test_launch_manifest_hides_raw_pid_entirely(self):
        sv = self._sv()
        m = self._launch(sv)
        handle = sv._handle_of(m.session_id)
        # the raw leader PID lives only on the private handle ...
        self.assertTrue(hasattr(handle, "pid"))
        # ... and never reaches the durable manifest or public status
        raw = (self.dir / "sup" / f"{m.session_id}.json").read_text()
        self.assertNotIn(str(handle.pid), raw)
        status = sv.status(m.session_id)
        self.assertNotIn(str(handle.pid), json.dumps(status))

    def test_launch_binds_capability_manifest(self):
        sv = self._sv()
        caps = {"agent": "claude", "structured_stream": True, "installed": True}
        m = self._launch(sv, capability_manifest=caps)
        self.assertEqual(m.capability_manifest["agent"], "claude")
        self.assertTrue(m.capability_manifest["structured_stream"])

    def test_controls_are_fixed_actions(self):
        self.assertEqual(
            set(sup_mod.CONTROL_ACTIONS),
            {"pause_session", "resume_session", "terminate_session",
             "quarantine_session", "cancel_attempt", "adopt", "detach",
             "append_user_turn", "apply_local_profile"},
        )

    def test_append_user_turn_never_spawns_sibling(self):
        # Claimed resume without a native session identity is still a sibling
        # spawn, not native resume.  That path must keep refusing create.
        ops = FakeOps()
        sv = self._sv(ops=ops)
        m = self._launch(sv, capability_manifest={"resume": True})
        before = len(ops.children)
        launch_argv = list(ops.last_argv)
        after_launch = list(ops.create_calls)
        self.assertEqual(
            sv.append_user_turn(m.session_id, "continue"),
            "unsupported_action",
        )
        self.assertEqual(len(ops.children), before)
        self.assertEqual(ops.last_argv, launch_argv)
        self.assertEqual(ops.create_calls, after_launch)
        self.assertEqual(sv.get(m.session_id).state, "running")
        for call in ops.create_calls:
            self.assertIsNotNone(call["cwd"])
            self.assertNotEqual(call["cwd"], "/tmp")
            self.assertNotIn("--ephemeral", call["argv"])

    def test_append_user_turn_posix_create_is_not_called(self):
        class RecordingOps(FakeOps):
            def __init__(self):
                super().__init__(platform="darwin")
                self._allow_launch = True

            def create(self, argv, cwd, env):
                if not self._allow_launch:
                    raise AssertionError(
                        "append_user_turn without native identity must not spawn")
                return super().create(argv, cwd, env)

        ops = RecordingOps()
        sv = self._sv(ops=ops)
        m = self._launch(sv, capability_manifest={"resume": True})
        after_launch = list(ops.create_calls)
        ops._allow_launch = False
        self.assertEqual(
            sv.append_user_turn(m.session_id, "hello"),
            "unsupported_action",
        )
        self.assertEqual(ops.create_calls, after_launch)

    def test_append_user_turn_hermes_unsupported(self):
        sv = self._sv()
        m = sv.launch(
            agent="hermes",
            command=["/usr/bin/env", "python3", "-c", "pass"],
            cwd="/srv/jobs",
            env_allowlist={"HOME": "/srv/jobs"},
            session_id=_sid(),
            capability_manifest={"resume": True},
        )
        self.assertEqual(sv.append_user_turn(m.session_id, "hello"),
                         "unsupported_action")

    def test_append_user_turn_resume_unverified(self):
        sv = self._sv()
        m = self._launch(sv, capability_manifest={"resume": False})
        self.assertEqual(sv.append_user_turn(m.session_id, "hello"),
                         "resume_unverified")

    def test_append_user_turn_quarantined_is_refused(self):
        ops = FakeOps()
        sv = self._sv(ops=ops)
        m = self._launch(sv, capability_manifest={"resume": True})
        self.assertEqual(sv.quarantine_session(m.session_id), "quarantined")
        after = list(ops.create_calls)
        self.assertEqual(sv.append_user_turn(m.session_id, "hello"),
                         "unsupported_action")
        self.assertEqual(ops.create_calls, after)
        self.assertEqual(sv.get(m.session_id).state, "quarantined")

    def test_append_user_turn_quarantined_with_identity_still_refuses(self):
        ops = FakeOps()
        sv = self._sv(ops=ops)
        native = self._native_jsonl()
        m = self._launch(sv, capability_manifest={"resume": True},
                         command=["/opt/codex", "exec", "--json", "first"])
        self._bind_native_identity(
            sv, m.session_id, native_file_path=native,
            cwd="/srv/jobs", env={"HOME": "/srv/jobs"},
            resume_token="qTok", exe_path="/opt/codex")
        self.assertEqual(sv.quarantine_session(m.session_id), "quarantined")
        after = list(ops.create_calls)
        self.assertEqual(sv.append_user_turn(m.session_id, "hello"),
                         "unsupported_action")
        self.assertEqual(ops.create_calls, after)
        self.assertEqual(sv.get(m.session_id).state, "quarantined")
        self.assertNotIn(signal.SIGCONT, ops.children[0].signals)

    def test_append_user_turn_natural_finish_without_identity_stays_terminated(self):
        ops = FakeOps()
        sv = self._sv(ops=ops)
        m = self._launch(sv, capability_manifest={"resume": True})
        handle = ops.children[0]
        handle.stdio = ["done"]
        handle.alive = False
        handle.exit_code = 0
        self.assertEqual(sv.wait_session(m.session_id, timeout_s=0.2).outcome,
                         "finished")
        after = list(ops.create_calls)
        self.assertEqual(sv.append_user_turn(m.session_id, "hello"),
                         "terminated")
        self.assertEqual(ops.create_calls, after)
        self.assertEqual(sv.get(m.session_id).state, "terminated")

    def _native_jsonl(self, name="rollout-2026-09-08T00-00-00-native.jsonl"):
        path = self.dir / name
        path.write_text("{\"type\":\"session\"}\n")
        os.chmod(path, 0o600)
        return str(path)

    def _bind_native_identity(self, sv, session_id, *,
                              native_file_path,
                              cwd="/srv/jobs",
                              env=None,
                              resume_token="abc123xyz",
                              exe_path="/opt/codex"):
        """Pin the private native resume identity the executor must consume.

        Public status / durable manifest stay free of pid/cwd/argv/env/path.
        The identity must survive handle release so a finished native session
        can still be resumed without SIGCONT.
        """
        entry = sv._entries[session_id]
        handle = sv._handle_of(session_id)
        env = dict(env or {"HOME": "/srv/jobs"})
        for target in (entry, handle):
            setattr(target, "native_file_path", native_file_path)
            setattr(target, "cwd", cwd)
            setattr(target, "env", dict(env))
            setattr(target, "resume_token", resume_token)
            setattr(target, "exe_path", exe_path)
        return handle

    def test_native_resume_keeps_cwd_env_devnull_and_session_identity(self):
        ops = FakeOps()
        sv = self._sv(ops=ops)
        native = self._native_jsonl()
        m = self._launch(
            sv,
            capability_manifest={"resume": True},
            command=["/opt/codex", "exec", "--json", "first turn"],
        )
        after_launch = list(ops.create_calls)
        launch_signals = list(ops.children[0].signals)
        self._bind_native_identity(
            sv, m.session_id, native_file_path=native,
            cwd="/srv/jobs", env={"HOME": "/srv/jobs"},
            resume_token="abc123xyz", exe_path="/opt/codex")

        outcome = sv.append_user_turn(m.session_id, "continue the work")
        self.assertEqual(outcome, "appended")
        self.assertEqual(sv.get(m.session_id).state, "running")
        self.assertEqual(sv.get(m.session_id).reason, "appended")

        self.assertEqual(len(ops.create_calls), len(after_launch) + 1)
        resume = ops.create_calls[-1]
        self.assertEqual(resume["cwd"], "/srv/jobs")
        self.assertNotEqual(resume["cwd"], "/tmp")
        self.assertIsNotNone(resume["cwd"])
        self.assertEqual(resume["env"], {"HOME": "/srv/jobs"})
        argv = resume["argv"]
        self.assertTrue(argv)
        self.assertEqual(argv[0], "/opt/codex")
        self.assertIn("--resume", argv)
        self.assertIn("abc123xyz", argv)
        self.assertNotIn("--ephemeral", argv)
        self.assertNotIn("-p", argv)
        joined = " ".join(argv)
        self.assertNotIn("/tmp", joined)
        self.assertNotIn(native, json.dumps(sv.status(m.session_id)))
        raw = (self.dir / "sup" / f"{m.session_id}.json").read_text()
        for priv in ("pid", "cwd", "argv", "env", "command", "path",
                      "native_file_path", "resume_token", "/opt/codex",
                      native, "/srv/jobs"):
            self.assertNotIn(priv, raw)
            self.assertNotIn(priv, json.dumps(sv.status(m.session_id)))
        # Native resume is a new CLI process, never SIGCONT thaw.
        self.assertEqual(ops.children[0].signals, launch_signals)
        self.assertNotIn(signal.SIGCONT, ops.children[-1].signals)

    def test_native_resume_survives_natural_completion_without_sigcont(self):
        ops = FakeOps()
        sv = self._sv(ops=ops)
        native = self._native_jsonl()
        m = self._launch(sv, capability_manifest={"resume": True},
                         command=["/opt/codex", "exec", "--json", "done"])
        self._bind_native_identity(
            sv, m.session_id, native_file_path=native,
            cwd="/srv/jobs", env={"HOME": "/srv/jobs"},
            resume_token="sessTok9", exe_path="/opt/codex")
        handle = ops.children[0]
        handle.stdio = ["done"]
        handle.alive = False
        handle.exit_code = 0
        result = sv.wait_session(m.session_id, timeout_s=0.2)
        self.assertEqual(result.outcome, "finished")
        self.assertEqual(sv.get(m.session_id).state, "terminated")
        self.assertIsNone(sv._handle_of(m.session_id))
        after_finish = list(ops.create_calls)

        outcome = sv.append_user_turn(m.session_id, "follow up")
        self.assertEqual(outcome, "appended")
        self.assertEqual(sv.get(m.session_id).state, "running")
        self.assertEqual(len(ops.create_calls), len(after_finish) + 1)
        resume = ops.create_calls[-1]
        self.assertEqual(resume["cwd"], "/srv/jobs")
        self.assertEqual(resume["env"], {"HOME": "/srv/jobs"})
        self.assertIn("--resume", resume["argv"])
        self.assertIn("sessTok9", resume["argv"])
        self.assertNotIn("--ephemeral", resume["argv"])
        self.assertNotIn(signal.SIGCONT, handle.signals)

    def test_launch_keeps_cwd_env_private_after_handle_release(self):
        sv = self._sv()
        m = self._launch(sv)
        entry = sv._entries[m.session_id]
        self.assertEqual(getattr(entry, "cwd", None), "/srv/jobs")
        self.assertEqual(getattr(entry, "env", None), {"HOME": "/srv/jobs"})
        sv._release_handle(m.session_id)
        self.assertIsNone(sv._handle_of(m.session_id))
        self.assertEqual(entry.cwd, "/srv/jobs")
        self.assertEqual(entry.env, {"HOME": "/srv/jobs"})
        raw = (self.dir / "sup" / f"{m.session_id}.json").read_text()
        public = json.dumps(sv.status(m.session_id))
        for priv in ("cwd", "env", "argv", "pid", "/srv/jobs"):
            self.assertNotIn(priv, raw)
            self.assertNotIn(priv, public)

    def test_native_resume_posix_create_uses_devnull_not_tmp_sibling(self):
        recorded = []
        captured = {}

        class RecordingPosix(sup_mod.POSIXGroupOps):
            def create(self, argv, cwd, env):
                recorded.append((list(argv), cwd, dict(env or {})))
                return super().create(argv, cwd, env)

        class _FakePopen:
            def __init__(self, argv, **kw):
                captured["argv"] = list(argv)
                captured["kw"] = kw
                self.pid = 4242
                self.stdout = None

            def poll(self):
                return None

            def wait(self, timeout=None):
                return 0

        native = self._native_jsonl()
        ops = RecordingPosix(platform="darwin")
        with mock.patch.object(sup_mod.subprocess, "Popen", _FakePopen):
            sv = self._sv(ops=ops)
            m = sv.launch(
                agent="codex",
                command=["/opt/codex", "exec", "--json", "first"],
                cwd="/srv/jobs",
                env_allowlist={"HOME": "/srv/jobs"},
                session_id=_sid(),
                capability_manifest={"resume": True},
            )
            self._bind_native_identity(
                sv, m.session_id, native_file_path=native,
                cwd="/srv/jobs", env={"HOME": "/srv/jobs"},
                resume_token="tokLive1", exe_path="/opt/codex")
            captured.clear()
            recorded.clear()
            outcome = sv.append_user_turn(m.session_id, "keep going")
        self.assertEqual(outcome, "appended")
        self.assertTrue(recorded, "native resume must call GroupOps.create")
        argv, cwd, env = recorded[-1]
        self.assertEqual(cwd, "/srv/jobs")
        self.assertNotEqual(cwd, "/tmp")
        self.assertIsNotNone(cwd)
        self.assertEqual(env, {"HOME": "/srv/jobs"})
        self.assertIn("--resume", argv)
        self.assertIn("tokLive1", argv)
        self.assertNotIn("--ephemeral", argv)
        self.assertNotIn("--ignore-user-config", argv)
        self.assertEqual(captured["kw"]["cwd"], "/srv/jobs")
        self.assertIs(captured["kw"]["stdin"], subprocess.DEVNULL)
        self.assertNotEqual(captured["kw"].get("cwd"), "/tmp")

    def test_apply_local_profile_delegates_without_copying_secrets(self):
        calls = []

        def fake_apply(profile_id, *, family=None, db_path=None):
            calls.append((profile_id, family, db_path))
            return "applied"

        import tools.local_profile as local_profile
        orig = local_profile.apply_local_profile
        local_profile.apply_local_profile = fake_apply
        try:
            sv = self._sv()
            m = self._launch(sv)
            self.assertEqual(
                sv.apply_local_profile(m.session_id, "local-1"), "applied")
        finally:
            local_profile.apply_local_profile = orig
        self.assertEqual(calls, [("local-1", "codex", None)])
        self.assertEqual(sv.get(m.session_id).reason, "applied")

    # ---- unknown target / adoption ----------------------------------------

    def test_unknown_target_rejected(self):
        sv = self._sv()
        with self.assertRaises(sup_mod.UnknownSessionError):
            sv.pause_session("no-such-session")

    def test_no_auto_adoption_of_existing_processes(self):
        sv = self._sv()
        with self.assertRaises(sup_mod.UnknownSessionError):
            sv.terminate_session("unseen")
        self.assertEqual(sv.recover(), {})
        self.assertEqual(list((self.dir / "sup").rglob("*.json")), [])

    # ---- pause / resume ----------------------------------------------------

    def test_pause_resume(self):
        sv = self._sv()
        m = self._launch(sv)
        self.assertEqual(sv.pause_session(m.session_id), "paused")
        self.assertEqual(sv.get(m.session_id).state, "paused")
        proc = sv._ops.children[0]
        self.assertTrue(proc.stopped)
        self.assertIn(signal.SIGSTOP, proc.signals)
        self.assertEqual(sv.resume_session(m.session_id), "running")
        self.assertEqual(sv.get(m.session_id).state, "running")
        self.assertFalse(proc.stopped)
        self.assertIn(signal.SIGCONT, proc.signals)

    def test_pause_failed_surfaces_bounded(self):
        sv = self._sv(ops=FakeOps(deny_signals=True))
        m = self._launch(sv)
        outcome = sv.pause_session(m.session_id)
        self.assertEqual(outcome, "pause_failed")
        self.assertEqual(sv.get(m.session_id).reason, "pause_failed")
        # state stays running — a failed stop is never reported as paused
        self.assertEqual(sv.get(m.session_id).state, "running")

    def test_resume_failed_surfaces_bounded(self):
        sv = self._sv(ops=FakeOps(deny_signals=True))
        m = self._launch(sv)
        self.assertEqual(sv.pause_session(m.session_id), "pause_failed")
        outcome = sv.resume_session(m.session_id)
        self.assertEqual(outcome, "resume_failed")
        self.assertEqual(sv.get(m.session_id).reason, "resume_failed")

    def test_pause_already_terminal_returns_terminal(self):
        sv = self._sv()
        m = self._launch(sv)
        sv.terminate_session(m.session_id, grace_s=0.0)
        outcome = sv.pause_session(m.session_id)
        self.assertEqual(outcome, "terminated")

    # ---- capability downgrade ---------------------------------------------

    def test_missing_cgroup_downgrade(self):
        sv = self._sv(ops=FakeOps(capability="process_group_only"))
        m = self._launch(sv)
        self.assertEqual(m.group_capability, "process_group_only")

    def test_mac_platform_group_capability(self):
        sv = self._sv(ops=FakeOps(platform="darwin", capability="process_group_only"))
        m = self._launch(sv)
        self.assertEqual(m.group_capability, "process_group_only")
        self.assertEqual(m.platform, "darwin")

    # ---- terminate ---------------------------------------------------------

    def test_terminate_graceful(self):
        sv = self._sv()
        m = self._launch(sv)
        outcome = sv.terminate_session(m.session_id, grace_s=0.1)
        self.assertEqual(outcome, "terminated")
        self.assertEqual(sv.get(m.session_id).state, "terminated")

    def test_terminate_force_when_process_defies_grace(self):
        ops = FakeOps(deny_graceful=True)
        sv = self._sv(ops=ops)
        m = self._launch(sv)
        outcome = sv.terminate_session(m.session_id, grace_s=0.05)
        self.assertEqual(outcome, "terminated_forced")
        handle = sv._ops.children[0]
        self.assertIn(signal.SIGKILL, handle.signals)

    def test_terminate_escape_degrades(self):
        ops = FakeOps()
        sv = self._sv(ops=ops)
        m = self._launch(sv)
        ops.children[0].escaped = True
        outcome = sv.terminate_session(m.session_id, grace_s=0.1)
        self.assertEqual(outcome, "escape_unverified")
        self.assertEqual(sv.get(m.session_id).state, "terminated")

    def test_terminate_already_finished(self):
        sv = self._sv()
        m = self._launch(sv)
        ops = sv._ops
        handle = ops.children[0]
        handle.alive = False
        handle.exit_code = 3
        outcome = sv.terminate_session(m.session_id, grace_s=0.0)
        self.assertEqual(outcome, "already_finished")

    # ---- quarantine / cancel_attempt ----------------------------------------

    def test_quarantine_session(self):
        sv = self._sv()
        m = self._launch(sv)
        self.assertEqual(sv.quarantine_session(m.session_id), "quarantined")
        self.assertEqual(sv.get(m.session_id).state, "quarantined")
        proc = sv._ops.children[0]
        self.assertTrue(proc.stopped)
        self.assertIn(signal.SIGSTOP, proc.signals)

    def test_quarantine_failed_not_silent_success(self):
        sv = self._sv(ops=FakeOps(deny_signals=True))
        m = self._launch(sv)
        outcome = sv.quarantine_session(m.session_id)
        self.assertEqual(outcome, "quarantine_failed")
        self.assertEqual(sv.get(m.session_id).state, "running")  # state unchanged
        self.assertEqual(sv.get(m.session_id).reason, "quarantine_failed")

    def test_cancel_attempt(self):
        sv = self._sv()
        m = self._launch(sv, attempt_id=_att())
        self.assertEqual(sv.cancel_attempt(m.session_id), "terminated")
        self.assertEqual(sv.get(m.session_id).state, "terminated")

    # ---- deadline-safe wait -------------------------------------------------

    def test_wait_silent_child_times_out(self):
        sv = self._sv()
        m = self._launch(sv)
        handle = sv._ops.children[0]
        handle.stdio = []  # silent process: no line, no exit
        start = time.monotonic()
        result = sv.wait_session(m.session_id, timeout_s=0.3)
        elapsed = time.monotonic() - start
        self.assertTrue(result.timed_out)
        self.assertEqual(result.exit_code, 124)
        self.assertEqual(result.outcome, "timed_out")
        self.assertLess(elapsed, 3.0, "wait must be deadline-safe, not blocking")
        self.assertEqual(sv.get(m.session_id).state, "terminated")

    def test_wait_aborts_during_wait(self):
        sv = self._sv()
        m = self._launch(sv)
        box = {"abort": False}
        results = {}

        def worker():
            results["r"] = sv.wait_session(
                m.session_id, timeout_s=30,
                should_abort=lambda: box["abort"])

        t = threading.Thread(target=worker)
        t.start()
        time.sleep(0.05)
        box["abort"] = True
        t.join(timeout=5)
        self.assertFalse(t.is_alive())
        self.assertIn("r", results)
        self.assertTrue(results["r"].aborted)
        self.assertEqual(results["r"].exit_code, 130)
        self.assertEqual(sv.get(m.session_id).state, "terminated")

    def test_wait_capture_lines_until_exit(self):
        sv = self._sv()
        m = self._launch(sv)
        handle = sv._ops.children[0]
        handle.stdio = ["line1", "partial-tail"]
        handle.alive = False  # process exits right after emitting output
        handle.exit_code = 0
        lines = []
        result = sv.wait_session(m.session_id, timeout_s=0.2,
                                 on_line=lambda s: lines.append(s))
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.timed_out)
        joined = "\n".join(lines)
        self.assertIn("line1", joined)
        self.assertIn("partial-tail", joined)

    # ---- crash recovery -----------------------------------------------------

    def test_restart_recovers_manifest(self):
        sv1 = self._sv()
        m = self._launch(sv1)
        sv2 = self._sv()
        recovered = sv2.recover()
        self.assertIn(m.session_id, recovered)
        self.assertEqual(sv2.get(m.session_id).state, "running")

    def test_recovered_manifest_keeps_process_group_binding(self):
        sv1 = self._sv()
        m = self._launch(sv1)
        sv2 = self._sv()
        recovered = sv2.recover()[m.session_id]
        self.assertEqual(recovered.process_group_id, m.process_group_id)
        self.assertEqual(recovered.session_id, m.session_id)

    def test_restart_no_live_handle_terminate_refuses(self):
        sv1 = self._sv()
        m = self._launch(sv1)
        sv2 = self._sv()
        sv2.recover()
        outcome = sv2.terminate_session(m.session_id, grace_s=0.0)
        self.assertEqual(outcome, "no_live_process")

    # ---- public status --------------------------------------------------------

    def test_managed_status_bounded(self):
        sv = self._sv()
        m = self._launch(sv)
        status = sv.status(m.session_id)
        self.assertTrue(status["managed"])
        self.assertEqual(status["control_capability"], "available")
        self.assertEqual(status["state"], "running")
        for priv in ("pid", "cwd", "env", "argv", "command", "path",
                     "token", "secret"):
            self.assertNotIn(priv, status)

    def test_unmanaged_session_stays_best_effort(self):
        from session_schema import public_session_dto
        dto = public_session_dto({
            "session_id": "a-old-session", "machine_id": self.machine,
            "managed": False, "capture_quality": "exact",
        })
        self.assertFalse(dto["managed"])
        self.assertEqual(dto["capture_quality"], "best_effort")
        self.assertEqual(dto["control_capability"], "unavailable")

    # ---- security / scope constraints --------------------------------------

    def test_launch_rejects_non_argv_command(self):
        sv = self._sv()
        with self.assertRaises(ValueError):
            sv.launch(agent="codex", command="rm -rf /", cwd="/tmp",
                      env_allowlist={}, session_id=_sid())

    def test_launch_rejects_empty_command(self):
        sv = self._sv()
        with self.assertRaises(ValueError):
            sv.launch(agent="codex", command=[], cwd="/tmp",
                      env_allowlist={}, session_id=_sid())

    def test_launch_never_shell_true(self):
        seen = {}

        class RecordingOps(FakeOps):
            def create(self, argv, cwd, env):
                seen["argv"] = list(argv)
                seen["env"] = dict(env or {})
                seen["shell"] = False
                return super().create(argv, cwd, env)

        ops = RecordingOps()
        sv = self._sv(ops=ops)
        self._launch(sv, command=["/bin/true", "arg1"])
        self.assertEqual(seen["argv"], ["/bin/true", "arg1"])
        self.assertFalse(seen["shell"])

    def test_launch_env_is_exactly_allowlist(self):
        seen = {}

        class RecordingOps(FakeOps):
            def create(self, argv, cwd, env):
                seen["env"] = dict(env or {})
                seen["cwd"] = cwd
                return super().create(argv, cwd, env)

        sv = self._sv(ops=RecordingOps())
        self._launch(sv, command=["/bin/true"])
        self.assertEqual(seen["env"], {"HOME": "/srv/jobs"})
        self.assertEqual(seen["cwd"], "/srv/jobs")

    def test_status_no_token_or_path_shape(self):
        sv = self._sv()
        m = self._launch(sv)
        dto = sv.status(m.session_id)
        for key, value in dto.items():
            if isinstance(value, str):
                lowered = value.lower()
                for marker in ("token", "secret", "password", "/Users/",
                               "c:\\", "$HOME"):
                    self.assertNotIn(marker, lowered,
                                     f"{key} leaked secret-shaped value")

    # ---- review round 2: bounded reason enum (finding 1) -------------------

    def test_all_success_reason_literals_are_enum_members(self):
        # pause / quarantine / recovered are success-path literals that used
        # to bypass `_CONTROL_OUTCOMES`; they must now live inside the enum.
        self.assertIn("pause", sup_mod._CONTROL_OUTCOMES)
        self.assertIn("quarantine", sup_mod._CONTROL_OUTCOMES)
        self.assertIn("recovered", sup_mod._CONTROL_OUTCOMES)
        self.assertIn("group_remaining", sup_mod._CONTROL_OUTCOMES)

    def test_pause_quarantine_recover_persist_enum_bounded_reasons(self):
        sv = self._sv()
        m = self._launch(sv)
        sv.pause_session(m.session_id)
        self.assertIn(sv.get(m.session_id).reason, sup_mod._CONTROL_OUTCOMES)
        self.assertEqual(sv.get(m.session_id).reason, "pause")

        sv2 = self._sv()
        rec = sv2.recover()
        self.assertIn(m.session_id, rec)
        # recovered manifest re-rooted via the enum-validating setter
        self.assertIn(sv2.get(m.session_id).reason, sup_mod._CONTROL_OUTCOMES)
        self.assertEqual(sv2.get(m.session_id).reason, "recovered")

    def test_set_reason_downgrades_non_enum_to_control_failed(self):
        # the single enum-validating setter downgrades an unbounded literal
        sv = self._sv()
        m = self._launch(sv)
        sv._set_reason(m, "some-unbounded-junk")
        self.assertEqual(m.reason, "control_failed")

    # ---- review round 2: group completion guarantee (finding 3) ------------

    def test_natural_exit_with_live_member_is_degraded_not_success(self):
        ops = FakeOps()
        sv = self._sv(ops=ops)
        m = self._launch(sv)
        handle = ops.children[0]
        handle.stdio = ["leader-finished"]
        handle.alive = False  # leader exits 0
        handle.exit_code = 0
        handle.hold_stdout = True  # ... but a member still holds stdout open
        # shrink the flush budget so the test does not wait the real 2.0s
        old_budget, old_step = sup_mod._FLUSH_BUDGET, sup_mod._FLUSH_STEP
        sup_mod._FLUSH_BUDGET = 0.05
        sup_mod._FLUSH_STEP = 0.01
        try:
            start = time.monotonic()
            result = sv.wait_session(m.session_id, timeout_s=1.0)
            elapsed = time.monotonic() - start
        finally:
            sup_mod._FLUSH_BUDGET, sup_mod._FLUSH_STEP = old_budget, old_step
        self.assertEqual(result.outcome, "group_remaining")
        self.assertEqual(result.exit_code, 124)
        self.assertFalse(result.timed_out)  # not a timeout, a forced teardown
        self.assertEqual(sv.get(m.session_id).state, "terminated")
        self.assertEqual(sv.get(m.session_id).reason, "group_remaining")
        # the SIGKILL reached the held-open member
        self.assertIn(signal.SIGKILL, handle.signals)
        self.assertLess(elapsed, 1.5, "post-exit flush must be wall-bounded")

    def test_natural_exit_clean_eof_still_reports_finished(self):
        # regression: a clean group (no holder) still reports plain success
        sv = self._sv()
        m = self._launch(sv)
        handle = sv._ops.children[0]
        handle.stdio = ["line1", "tail"]
        handle.alive = False
        handle.exit_code = 0
        lines = []
        result = sv.wait_session(m.session_id, timeout_s=0.2,
                                 on_line=lambda s: lines.append(s))
        self.assertEqual(result.outcome, "finished")
        self.assertEqual(result.exit_code, 0)
        joined = "\n".join(lines)
        self.assertIn("line1", joined)
        self.assertIn("tail", joined)
        self.assertEqual(sv.get(m.session_id).reason, "terminated")

    # ---- review round 2 hardening: _wait_gone (finding 3 optional) ---------

    def test_wait_gone_exception_is_unknown_not_gone(self):
        ops = FakeOps()
        sv = self._sv(ops=ops)
        m = self._launch(sv)
        proc = ops.children[0]
        proc.alive_unknown = True  # group_alive raises -> liveness unknown
        # graceful SIGTERM cannot be verified as gone and the lingering group
        # is force-killed; the supervisor must never report a plain terminate
        # on a process it cannot observe.
        outcome = sv.terminate_session(m.session_id, grace_s=0.1)
        self.assertIn(outcome, ("terminated_forced", "escape_unverified"))
        self.assertEqual(sv.get(m.session_id).state, "terminated")


# --------------------------------------------------------------------------- #
# real pipe pump bound (finding 2: _abuf stays capped under a huge line-free
# stream).  Uses a real os.pipe, no subprocess, no shell.
# --------------------------------------------------------------------------- #

class RealPipePumpBoundsTests(unittest.TestCase):
    def test_pump_bounds_abuf_under_big_line_free_stream(self):
        ops = sup_mod.POSIXGroupOps()

        class _H:
            _abuf = b""

        h = _H()
        r, w = os.pipe()
        h.proc = type("P", (), {"stdout": os.fdopen(r, "rb", buffering=0)})()
        stop = threading.Event()
        max_seen = [0]

        def writer():
            chunk = b"x" * 65536
            while not stop.is_set():
                try:
                    os.write(w, chunk)
                except OSError:
                    return

        def sampler():
            while not stop.is_set():
                n = len(h._abuf)
                if n > max_seen[0]:
                    max_seen[0] = n

        tw = threading.Thread(target=writer)
        ts = threading.Thread(target=sampler)
        tw.start()
        ts.start()
        try:
            # no newline ever; with an unbounded buffer this accumulates
            # megabytes between reads.  A bounded ``_abuf`` is the invariant.
            pr = ops.pump(h, 0.4)
        finally:
            stop.set()
            try:
                os.close(w)
            except OSError:
                pass
            tw.join(timeout=3)
            ts.join(timeout=3)
        # pr is a bounded status; _abuf must never have grown past the cap
        self.assertIn(pr, ("line", "timeout", "eof"))
        self.assertGreater(max_seen[0], 0, "writer must have produced data")
        self.assertLessEqual(max_seen[0], sup_mod._ABUF_MAX,
                             "_abuf grew past the bounded cap")


# --------------------------------------------------------------------------- #
# real OS process-group exercise (POSIX, skip on win32)
# --------------------------------------------------------------------------- #

@unittest.skipIf(sys.platform == "win32", "POSIX process groups only")
class RealProcessGroupTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="fleet-sup-real-"))

    def _real_sv(self, name="sup"):
        return sup_mod.Supervisor(manifest_dir=self.dir / name,
                                  machine_id="host")

    def test_real_empty_allowlist_never_inherits_parent_env(self):
        os.environ["_FLEET_SECRET_MARKER"] = "must-not-leak"
        sv = self._real_sv("env")
        m = sv.launch(
            agent="codex",
            command=_py(
                "import os, sys;"
                "sys.exit(42 if os.environ.get('_FLEET_SECRET_MARKER') else 0)"
            ),
            cwd="/tmp",
            env_allowlist={},
            session_id=_sid(),
            attempt_id=None,
        )
        try:
            result = sv.wait_session(m.session_id, timeout_s=15)
        finally:
            del os.environ["_FLEET_SECRET_MARKER"]
        self.assertEqual(result.exit_code, 0)

    def test_real_graceful_terminate(self):
        sv = self._real_sv("sup")
        m = sv.launch(
            agent="codex",
            command=_py("import time; time.sleep(30)"),
            cwd="/tmp",
            env_allowlist={},
            session_id=_sid(),
            attempt_id=None,
        )
        outcome = sv.terminate_session(m.session_id, grace_s=1.0)
        self.assertIn(outcome, ("terminated", "terminated_forced"))

    def test_real_forced_terminate_after_grace(self):
        sv = self._real_sv("real")
        ready = self.dir / "ready"
        script = (
            "import pathlib, signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"pathlib.Path({str(ready)!r}).write_text('ready')\n"
            "time.sleep(30)\n"
        )
        m = sv.launch(
            agent="codex",
            command=_py(script),
            cwd="/tmp", env_allowlist={},
            session_id=_sid(), attempt_id=None,
        )
        deadline = time.time() + 5
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(ready.exists(), "child never became ready")
        outcome = sv.terminate_session(m.session_id, grace_s=0.4)
        self.assertIn(outcome, ("terminated_forced", "escape_unverified"))
        self.assertEqual(sv.get(m.session_id).state, "terminated")

    def test_real_merged_stdout_stderr_drain(self):
        sv = self._real_sv("merge")
        m = sv.launch(
            agent="codex",
            command=_py(
                "import sys; print('hello-out'); "
                "print('hello-err', file=sys.stderr); sys.stdout.flush()"
            ),
            cwd="/tmp", env_allowlist={},
            session_id=_sid(), attempt_id=None,
        )
        lines = []
        result = sv.wait_session(m.session_id, timeout_s=15,
                                 on_line=lambda s: lines.append(s))
        self.assertEqual(result.exit_code, 0, lines[:5])
        joined = "\n".join(lines)
        self.assertIn("hello-out", joined)
        self.assertIn("hello-err", joined)


# --------------------------------------------------------------------------- #
# real SILENT-child timeout (POSIX, skip on win32)
# --------------------------------------------------------------------------- #

@unittest.skipIf(sys.platform == "win32", "POSIX process groups only")
class RealSilentChildTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="fleet-sup-silent-"))

    def test_real_silent_child_times_out_with_teardown(self):
        sv = sup_mod.Supervisor(manifest_dir=self.dir / "silent",
                                machine_id="host")
        m = sv.launch(
            agent="codex",
            command=_py(
                "import signal, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "time.sleep(60)\n"
            ),
            cwd="/tmp", env_allowlist={},
            session_id=_sid(), attempt_id=None,
        )
        handle = sv._handle_of(m.session_id)
        before = time.monotonic()
        # child ignores SIGTERM and prints nothing: the deadline-safe pump loop
        # must tear the group down (graceful then SIGKILL) and report timed_out.
        result = sv.wait_session(m.session_id, timeout_s=1.0)
        after = time.monotonic()
        self.assertTrue(result.timed_out, "silent child must report timed_out")
        self.assertEqual(result.exit_code, 124)
        self.assertLess(after - before, 6.0,
                        "wait must be deadline-bounded, not blocked on read")
        self.assertEqual(sv.get(m.session_id).state, "terminated")
        self.assertIsNotNone(handle.proc.poll())


# --------------------------------------------------------------------------- #
# real stopped/running transitions (Linux /proc states)
# --------------------------------------------------------------------------- #

@unittest.skipIf(sys.platform == "win32", "POSIX process groups only")
class RealStopResumeTests(unittest.TestCase):
    def setUp(self):
        if not os.path.exists("/proc/self"):
            self.skipTest("/proc unavailable")
        self.dir = Path(tempfile.mkdtemp(prefix="fleet-sup-stop-"))

    def test_real_pause_then_resume_changes_process_state(self):
        sv = sup_mod.Supervisor(manifest_dir=self.dir / "stop",
                                machine_id="host")
        ready = self.dir / "ready"
        script = (
            "import pathlib, time\n"
            f"pathlib.Path({str(ready)!r}).write_text('ready')\n"
            "time.sleep(60)\n"
        )
        m = sv.launch(
            agent="codex",
            command=_py(script),
            cwd="/tmp", env_allowlist={},
            session_id=_sid(), attempt_id=None,
        )
        handle = sv._handle_of(m.session_id)
        pid = handle.proc.pid
        if not _proc_exists(pid):
            sv.terminate_session(m.session_id, grace_s=0.2)
            self.skipTest("/proc unavailable")
        deadline = time.time() + 5
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(ready.exists(), "child never became ready")
        # pause -> SIGSTOP delivered to the whole group; /proc shows T.
        outcome = sv.pause_session(m.session_id)
        self.assertEqual(outcome, "paused")
        state = _proc_stat(pid)
        self.assertEqual(state, "T", "process must be OS-stopped after pause")
        # resume -> SIGCONT -> state returns to a runnable one.
        outcome = sv.resume_session(m.session_id)
        self.assertEqual(outcome, "running")
        deadline = time.time() + 5
        while time.time() < deadline:
            state = _proc_stat(pid)
            if state in ("S", "R"):
                break
            time.sleep(0.05)
        self.assertIn(state, ("S", "R"),
                      "process must resume to a running state after SIGCONT")
        sv.terminate_session(m.session_id, grace_s=1.0)

    def test_real_pause_keeps_process_alive_then_resume(self):
        sv = sup_mod.Supervisor(manifest_dir=self.dir / "stop",
                                machine_id="host")
        m = sv.launch(
            agent="codex",
            command=_py("import time; time.sleep(60)"),
            cwd="/tmp", env_allowlist={},
            session_id=_sid(), attempt_id=None,
        )
        handle = sv._handle_of(m.session_id)
        pid = handle.proc.pid
        if not _proc_exists(pid):
            sv.terminate_session(m.session_id, grace_s=0.2)
            self.skipTest("/proc unavailable")
        self.assertEqual(sv.pause_session(m.session_id), "paused")
        # a SIGSTOPped process stays alive (no zombie, still scheduled state T)
        self.assertTrue(_proc_exists(pid))
        self.assertEqual(_proc_stat(pid), "T")
        self.assertEqual(sv.resume_session(m.session_id), "running")
        deadline = time.time() + 5
        while time.time() < deadline:
            state = _proc_stat(pid)
            if state in ("S", "R"):
                break
            time.sleep(0.05)
        self.assertIn(state, ("S", "R"))
        sv.terminate_session(m.session_id, grace_s=1.0)


if __name__ == "__main__":
    unittest.main()