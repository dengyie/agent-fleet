"""Task 8 tests — attach-to-bridge wiring in the Agent control client.

Covers the contract that a *successful* adopt attach (`Supervisor
attach_to_existing(...) == "adopted"`) starts exactly ONE managed native tail
into the encrypted spool, and that a failed attach never starts a bridge:

- a fully signed adopt envelope carrying a bounded ``candidate``
  (pid / started_at / exe_path / agent_family / native_file_path) against a
  real ``Supervisor`` (fake ops + injected identity probe) yields the bounded
  ``{"status": "accepted", "reason": "adopted"}`` receipt and starts the
  recording bridge with ``config["managed"] is True``;
- a failed attach (``pid_reused``) NEVER invokes the bridge factory;
- a malformed / missing ``candidate`` is rejected before any attach;
- ``detach`` flushes + closes the bridge first — ``uploads_after_close`` stays
  empty and no signal is ever routed to the process group;
- a bridge construction failure rolls back the just-attached private entry
  (``all_status()`` empty) with the bounded ``capture_gap`` receipt;
- every reason surfaced is a fixed bounded token (no exception text/path/pid).

No real subprocess, signal, or network is used: the Supervisor runs against an
in-memory ops fake, the identity re-read is an injected probe, and the bridge
is a recording fake injected through ``bridge_factory``.
"""
from __future__ import annotations

import json
import secrets
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization as _ser
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hub.domain import control as ctrl
from tools.supervisor import supervisor as sup_mod
from tools.supervisor.control_client import ControlClient, NonceStore

#: The fixed bounded reason/bridge codes the adopt/detach surface may emit.
#: Every reason surfaced in a receipt must be a member of this set — no raw
#: exception text, no path, no pid ever leaks into a reason string.
_FIXED_CODES = frozenset({
    "adopted", "detached",
    "pid_reused", "exe_changed", "no_permission",
    "unsupported_family", "native_file_unreadable",
    "invalid_candidate", "bridge_unavailable", "capture_gap",
    "unknown_session", "scope_mismatch", "attempt_mismatch",
    "invalid_signature", "malformed_signature", "nonce_replay",
    "expired", "clock_skew", "malformed_timestamp", "malformed_nonce",
    "unsupported_action", "control_failed",
})


# --------------------------------------------------------------------------- #
# crypto / ids / helpers
# --------------------------------------------------------------------------- #

def _keypair():
    priv = Ed25519PrivateKey.generate()
    priv_bytes = priv.private_bytes(
        _ser.Encoding.Raw, _ser.PrivateFormat.Raw, _ser.NoEncryption())
    pub_bytes = priv.public_key().public_bytes(
        _ser.Encoding.Raw, _ser.PublicFormat.Raw)
    return priv_bytes, pub_bytes


def _opaque(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def _now_s() -> float:
    return datetime.now(timezone.utc).timestamp()


def _rfc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace(
        "+00:00", "Z")


def _command_payload(*, machine="agent-a", session="sess", attempt=None,
                     action="pause_session", nonce="n", command_id="cmd",
                     issued_s=None, expires_delta=600,
                     reason_code="operator_requested"):
    now = issued_s if issued_s is not None else _now_s()
    return {
        "command_id": command_id,
        "action": action,
        "target": {"machine_id": machine,
                   "session_id": session,
                   "attempt_id": attempt},
        "issued_at": _rfc(now),
        "expires_at": _rfc(now + expires_delta),
        "nonce": nonce,
        "reason_code": reason_code,
    }


def _sign(body: dict, priv: bytes) -> dict:
    body = dict(body)
    payload = ctrl.canonical_json_bytes(body)
    body["signature"] = ctrl.sign_payload(priv, payload)
    return body


def _adopt_command(priv, *, session, candidate, nonce=None, command_id=None,
                   machine="agent-a"):
    body = _command_payload(
        action="adopt", session=session, nonce=nonce or _opaque("n"),
        command_id=command_id or _opaque("cmd"), machine=machine)
    body["candidate"] = candidate
    return _sign(body, priv)


def _detach_command(priv, *, session, nonce=None, machine="agent-a"):
    return _sign(_command_payload(
        action="detach", session=session, nonce=nonce or _opaque("n"),
        command_id=_opaque("cmd"), machine=machine), priv)


def _new_candidate(*, pid=4321, started_at="2026-08-30T19:00:00Z",
                   exe_path="/usr/local/bin/codex", family="codex",
                   native_file_path=None) -> dict:
    return {
        "pid": pid,
        "started_at": started_at,
        "exe_path": exe_path,
        "agent_family": family,
        "native_file_path": native_file_path,
    }


# --------------------------------------------------------------------------- #
# deterministic fakes (no subprocess, no real signals)
# --------------------------------------------------------------------------- #

class _FakeAttachedHandle:
    """The private handle the fake ops returns for an attached pid."""

    def __init__(self, pid, started_at, exe_path):
        self.pid = int(pid)
        self.pgid = int(pid)
        self.started_at = started_at
        self.exe_path = exe_path
        self.signals = []
        self.group_signals = []
        self.alive = True

    def poll(self):
        return None


class _FakeOps(sup_mod.GroupOps):
    """In-memory GroupOps: records every signal/close, never touches OS."""

    def __init__(self):
        super().__init__(platform="test")
        self.alive = True
        self.attach_calls = []
        self.group_terminate_calls = []
        self.group_kill_calls = []
        self.closed = []

    def capability(self):
        return ("process_group_only", "1")

    def attach(self, pid, started_at, exe_path):
        handle = _FakeAttachedHandle(pid, started_at, exe_path)
        self.attach_calls.append(handle)
        return handle

    def create(self, argv, cwd, env):
        return self

    def proc_poll(self, handle):
        return None

    def proc_wait(self, handle, timeout=None):
        return 0

    def group_alive(self, handle):
        return self.alive

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

    def escaped(self, handle):
        return False

    def close(self, handle):
        self.closed.append(handle)


class _Pipe:
    """Deterministic transport for the Agent control client."""

    def __init__(self, commands=None):
        self.commands = list(commands or [])
        self.uploads = []
        self.poll_calls = 0
        self.network_error = False

    def __call__(self, path, body, headers=None):
        if self.network_error:
            return 0, {"error": "network unreachable"}
        if path == "/api/supervisor/poll":
            self.poll_calls += 1
            return 200, {"ok": True, "commands": self.commands,
                         "request_id": "rid_1"}
        if path == "/api/supervisor/receipts":
            self.uploads.append(dict(body))
            return 200, {"ok": True, "receipt": dict(body)}
        return 404, {"ok": False, "error": "not_found"}


# --------------------------------------------------------------------------- #
# recording bridge + factory fakes
# --------------------------------------------------------------------------- #

class RecordingBridge:
    """Records the adopt native-tail dance; never touches the spool/uploader."""

    def __init__(self, config):
        self.config = dict(config)
        self.started = False
        self.flushed = False
        self.closed = False
        self.uploads_after_close = []   # late events pushed after close()
        self.ingested = []              # [(path, checkpoint_path)]

    @property
    def session_id(self):
        return self.config.get("session_id") or ""

    def start(self):
        self.started = True
        return {}

    def ingest_native(self, path, checkpoint_path=None):
        self.ingested.append((path, checkpoint_path))
        return 0

    def flush(self):
        self.flushed = True
        if self.closed:
            self.uploads_after_close.append("late_event")
        return 0

    def close(self):
        self.closed = True


class RecordingBridgeFactory:
    """Callable ``bridge_factory(config) -> RecordingBridge``.

    ``last`` stays ``None`` until the factory was actually invoked — the
    plan's ``test_failed_attach_never_starts_bridge`` assertion surface.
    """

    def __init__(self):
        self.last = None
        self.calls = []

    def __call__(self, config):
        bridge = RecordingBridge(config)
        self.calls.append(bridge)
        self.last = bridge
        return bridge


class _BrokenFactory:
    """Factory that always raises — bridge construction can never succeed."""

    last = None

    def __call__(self, config):
        raise RuntimeError("bridge subsystem unavailable")


# --------------------------------------------------------------------------- #
# suite
# --------------------------------------------------------------------------- #

class AttachBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fleet-attach-bridge-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.priv, self.pub = _keypair()
        self.ops = _FakeOps()
        self.sup = sup_mod.Supervisor(
            manifest_dir=str(self.tmp / "sup"), machine_id="agent-a",
            ops=self.ops)

    def _client(self, transport=None, nonce_dir=None, bridge_factory=None,
                spool_root=None):
        return ControlClient(
            hub_url="https://agent.test",
            machine_id="agent-a",
            credential="credential",
            hub_public_key=self.pub,
            public_supervisor=self.sup,
            transport=transport or _Pipe(),
            nonce_store=NonceStore(path=nonce_dir or self.tmp / "used"),
            bridge_factory=bridge_factory,
            spool_root=spool_root,
        )

    def _wire_probe(self, pid, started_at, exe_path):
        """Inject the supervisor's identity re-read for one process."""
        self.sup._default_attach_probe = (
            lambda p: (started_at, exe_path) if int(p) == int(pid) else None)

    def _native_file(self):
        path = self.tmp / "native" / "snapshot.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"kind":"user_message","text":"hello"}\n',
                        encoding="utf-8")
        return str(path)

    # ---- 1. success ------------------------------------------------------- #

    def test_successful_adopt_starts_managed_bridge(self):
        pid, native = 4321, self._native_file()
        self._wire_probe(pid, "2026-08-30T19:00:00Z", "/usr/local/bin/codex")
        factory = RecordingBridgeFactory()
        sess = _opaque("adopt")
        cmd = _adopt_command(
            self.priv, session=sess,
            candidate=_new_candidate(pid=pid, native_file_path=native))
        pipe = _Pipe([cmd])
        result = self._client(
            pipe, bridge_factory=factory,
            spool_root=str(self.tmp / "spool")).poll_once()
        self.assertTrue(result["ok"])

        receipt = pipe.uploads[0]
        self.assertEqual(receipt["status"], "accepted")
        self.assertEqual(receipt["reason"], "adopted")
        # no envelope / pid leak — the raw pid never reaches the receipt
        self.assertNotIn(str(pid), json.dumps(receipt))

        # the injected factory drove exactly ONE bridge with a managed config
        self.assertIsNotNone(factory.last)
        self.assertEqual(len(factory.calls), 1)
        bridge = factory.last
        self.assertTrue(bridge.started)
        self.assertIs(bridge.config["managed"], True)
        self.assertEqual(bridge.config["session_id"], sess)
        self.assertTrue(str(bridge.config["process_group_id"]).startswith(
            "grp_"))
        # native tail requested with a checkpoint under the spool root
        self.assertEqual(len(bridge.ingested), 1)
        path, checkpoint = bridge.ingested[0]
        self.assertTrue(Path(path).exists())
        self.assertTrue(str(checkpoint).startswith(str(self.tmp / "spool")))

    def test_successful_adopt_without_native_file_still_binds_bridge(self):
        pid = 9090
        self._wire_probe(pid, "2026-08-30T20:00:00Z", "/opt/tool/bin/runner")
        factory = RecordingBridgeFactory()
        sess = _opaque("adopt")
        cmd = _adopt_command(
            self.priv, session=sess,
            candidate=_new_candidate(
                pid=pid, started_at="2026-08-30T20:00:00Z",
                exe_path="/opt/tool/bin/runner", native_file_path=None))
        pipe = _Pipe([cmd])
        self._client(pipe, bridge_factory=factory).poll_once()
        self.assertEqual(pipe.uploads[0]["reason"], "adopted")
        self.assertIsNotNone(factory.last)
        # a null native path is ignored; no ingest_native is requested
        self.assertEqual(factory.last.ingested, [])

    # ---- 2. failed attach never starts a bridge --------------------------- #

    def test_failed_attach_never_starts_bridge(self):
        # live identity differs from the wire candidate -> pid_reused
        self._wire_probe(4321, "2026-07-01T00:00:00Z", "/usr/local/bin/codex")
        factory = RecordingBridgeFactory()
        cmd = _adopt_command(
            self.priv, session=_opaque("adopt"),
            candidate=_new_candidate(pid=4321))
        pipe = _Pipe([cmd])
        self._client(pipe, bridge_factory=factory).poll_once()
        receipt = pipe.uploads[0]
        self.assertEqual(receipt["status"], "rejected")
        self.assertEqual(receipt["reason"], "pid_reused")
        self.assertIsNone(factory.last)          # factory literally never ran
        self.assertEqual(factory.calls, [])
        self.assertEqual(self.sup.all_status(), {})

    def test_exe_changed_never_starts_bridge(self):
        self._wire_probe(4321, "2026-08-30T19:00:00Z", "/usr/bin/different")
        factory = RecordingBridgeFactory()
        cmd = _adopt_command(
            self.priv, session=_opaque("adopt"),
            candidate=_new_candidate(pid=4321, exe_path="/usr/local/bin/codex"))
        pipe = _Pipe([cmd])
        self._client(pipe, bridge_factory=factory).poll_once()
        self.assertEqual(pipe.uploads[0]["reason"], "exe_changed")
        self.assertIsNone(factory.last)

    def test_unsupported_family_rejected_before_probe(self):
        factory = RecordingBridgeFactory()
        observed = []
        self.sup._default_attach_probe = lambda p: observed.append(p) or (
            "2026-08-30T19:00:00Z", "/usr/local/bin/codex")
        cmd = _adopt_command(
            self.priv, session=_opaque("adopt"),
            candidate=_new_candidate(family="not_a_family"))
        pipe = _Pipe([cmd])
        self._client(pipe, bridge_factory=factory).poll_once()
        receipt = pipe.uploads[0]
        self.assertEqual(receipt["status"], "rejected")
        self.assertEqual(receipt["reason"], "unsupported_family")
        self.assertEqual(observed, [])   # family gate runs before any read
        self.assertIsNone(factory.last)

    # ---- 3. malformed / missing candidate --------------------------------- #

    def test_malformed_candidate_rejected_bounded(self):
        factory = RecordingBridgeFactory()
        bad_candidates = (
            None,
            {"type": "junk"},                                  # no pid
            {"pid": "not-an-int", "started_at": "x", "exe_path": "x",
             "agent_family": "codex"},
            {"pid": -3, "started_at": "x", "exe_path": "x",
             "agent_family": "codex"},
            {"pid": 43, "started_at": "x"},                    # missing exe/family
        )
        for idx, bad in enumerate(bad_candidates):
            cmd = _adopt_command(
                self.priv, session=_opaque("adopt"), candidate=bad)
            pipe = _Pipe([cmd])
            self._client(pipe, bridge_factory=factory).poll_once()
            receipt = pipe.uploads[-1]
            self.assertEqual(receipt["status"], "rejected")
            self.assertEqual(receipt["reason"], "invalid_candidate")
            self.assertIn(receipt["reason"], _FIXED_CODES)
            self.assertIsNone(factory.last)
            self.assertNotIn("Traceback", json.dumps(receipt))
        # no attach ever happened, nothing bound
        self.assertEqual(self.sup.all_status(), {})
        self.assertEqual(self.ops.attach_calls, [])

    # ---- 4. detach closes the bridge and stops upload --------------------- #

    def test_detach_closes_bridge_and_stops_upload(self):
        pid = 5432
        native = self._native_file()
        self._wire_probe(pid, "2026-08-30T21:00:00Z", "/usr/local/bin/codex")
        factory = RecordingBridgeFactory()
        sess = _opaque("adopt")
        adopt = _adopt_command(
            self.priv, session=sess,
            candidate=_new_candidate(
                pid=pid, started_at="2026-08-30T21:00:00Z",
                native_file_path=native))
        detach = _detach_command(self.priv, session=sess)
        pipe = _Pipe([adopt, detach])
        self._client(pipe, bridge_factory=factory).poll_once()

        self.assertEqual(pipe.uploads[0]["status"], "accepted")
        self.assertEqual(pipe.uploads[0]["reason"], "adopted")
        detach_receipt = pipe.uploads[1]
        self.assertEqual(detach_receipt["status"], "succeeded")
        self.assertEqual(detach_receipt["reason"], "detached")

        bridge = factory.last
        self.assertIsNotNone(bridge)
        self.assertTrue(bridge.closed)
        self.assertTrue(bridge.flushed)
        self.assertEqual(bridge.uploads_after_close, [])
        # no signals were ever routed to the attached handle or its group
        self.assertEqual(self.ops.group_terminate_calls, [])
        self.assertEqual(self.ops.group_kill_calls, [])
        for handle in self.ops.attach_calls:
            self.assertEqual(handle.signals, [])
            self.assertEqual(handle.group_signals, [])
        # the private entry is gone from the supervisor after detach
        self.assertEqual(self.sup.all_status(), {})

    def test_detach_bounded_session_without_bridge_still_detaches(self):
        attached_sid = _opaque("adopt")
        rc = self.sup.attach_to_existing(
            4321, "2026-08-30T10:00:00Z", "/opt/agent/bin/run",
            None, "codex", session_id=attached_sid,
            probe=lambda pid: ("2026-08-30T10:00:00Z", "/opt/agent/bin/run"))
        self.assertEqual(rc, "adopted")
        cmd = _detach_command(self.priv, session=attached_sid)
        pipe = _Pipe([cmd])
        self._client(pipe).poll_once()   # no bridge_factory -> no bridge
        receipt = pipe.uploads[0]
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["reason"], "detached")
        self.assertEqual(self.sup.all_status(), {})
        self.assertEqual(self.ops.group_terminate_calls, [])
        self.assertEqual(self.ops.group_kill_calls, [])

    # ---- 5. bridge construction failure rolls back the private entry ------ #

    def test_bridge_start_failure_rolls_back_private_entry(self):
        pid = 6543
        self._wire_probe(pid, "2026-08-30T22:00:00Z", "/usr/local/bin/codex")
        factory = _BrokenFactory()
        sess = _opaque("adopt")
        cmd = _adopt_command(
            self.priv, session=sess,
            candidate=_new_candidate(pid=pid,
                                     started_at="2026-08-30T22:00:00Z"))
        pipe = _Pipe([cmd])
        client = self._client(pipe, bridge_factory=factory)
        result = client.poll_once()
        self.assertTrue(result["ok"])
        receipt = pipe.uploads[0]
        self.assertEqual(receipt["status"], "rejected")
        self.assertEqual(receipt["reason"], "capture_gap")
        self.assertIn(receipt["reason"], _FIXED_CODES)
        # private entry rolled back (safe_detach), no leftover bridge
        self.assertEqual(self.sup.all_status(), {})
        self.assertNotIn(sess, [b.session_id for b in client.iter_bridges()])
        # never a signal
        self.assertEqual(self.ops.group_terminate_calls, [])
        self.assertEqual(self.ops.group_kill_calls, [])

    # ---- 6. bounded-token pin --------------------------------------------- #

    def test_adopt_family_gate_native_unreadable_reason_is_bounded(self):
        # candidate holds a readable identity but an unreadable native file → the
        # attach fails closed with native_file_unreadable (fixed), bridge never
        # built. The reason string is a plain token, never the path.
        pid = 7778
        self._wire_probe(pid, "2026-08-30T23:00:00Z", "/opt/t/bin/a")
        factory = RecordingBridgeFactory()
        cmd = _adopt_command(
            self.priv, session=_opaque("adopt"),
            candidate=_new_candidate(pid=pid,
                                     native_file_path="/no/such/file.jsonl"))
        pipe = _Pipe([cmd])
        self._client(pipe, bridge_factory=factory).poll_once()
        receipt = pipe.uploads[-1]
        self.assertEqual(receipt["reason"], "native_file_unreadable")
        self.assertIn(receipt["reason"], _FIXED_CODES)
        self.assertNotIn("no/such/file", json.dumps(receipt))
        self.assertIsNone(factory.last)

    def test_reason_tokens_are_always_bounded(self):
        # structural pin for every fixed-code path: no path/pid/exception text
        pid, native = 8123, self._native_file()
        self._wire_probe(pid, "2026-08-30T23:30:00Z", "/opt/t/bin/a")
        self.assertEqual("/usr/local/bin/codex", _new_candidate()["exe_path"])
        cases = [
            (_new_candidate(pid=pid, family="unknown_shell"), None),
            (_new_candidate(pid=pid, native_file_path=native), native),
        ]
        for candidate, fname in cases:
            factory = RecordingBridgeFactory()
            cmd = _adopt_command(self.priv, session=_opaque("adopt"),
                                 candidate=candidate)
            pipe = _Pipe([cmd])
            self._client(pipe, bridge_factory=factory).poll_once()
            reason = pipe.uploads[-1]["reason"]
            self.assertIn(reason, _FIXED_CODES)
            self.assertTrue(reason.isidentifier() and reason.islower())
            if fname is not None:
                self.assertNotIn(fname, json.dumps(pipe.uploads[-1]))


if __name__ == "__main__":
    unittest.main()