"""Task 12 — migration, rollback and chaos acceptance gates.

Each of the eleven fault-drill scenarios asserts BOTH the success path and the
terminal/failure path — a silent gap is not a pass.  Failure assertions use
only the bounded codes each surface is contractually allowed to emit:

- ``validate_command`` codes (``allowed`` / ``expired`` / ``clock_skew`` /
  ``invalid_signature`` etc);
- spool ``AppendResult`` fields (``capture_blocked`` / ``gap_sequence``);
- supervisor terminate outcomes (``terminated`` / ``terminated_forced`` /
  ``escape_unverified`` / ``already_finished`` / ``no_live_process``);
- control poll surface (``error == "poll_transport_error"``);
- sqlite ``OperationalError`` inside a read-only repository;
- release/routing file invariants.

No token/secret/key VALUE, signature, nonce value, credential, real
filesystem path, or exception text ever appears in an assertion, an audit
dump, or a printed line. Test keys are placeholder byte strings only.

Scenarios (one class each):

 1. HubUnreachableTests   — Agent control pull degrades to a bounded
                            ``poll_transport_error`` surface (never raises);
                            Hub-side enqueue deliv receipt stays healthy.
 2. ReadonlyRepositoryTests — a read-only sqlite store fails closed on write
                            while reads stay healthy; a writable store
                            round-trips a session row.
 3. SpoolFullTests        — exact/structured overflow is ``capture_blocked``
                            (never a silent drop); a fitting event is durable;
                            best-effort overflow signals a ``gap_sequence``.
 4. CrashBeforeAckTests   — a torn trailing frame is repaired so the durable
                            max is the last COMPLETE frame, the stream resumes
                            with no hole, and the uploader replays only the
                            unacked tail from the persisted ack cursor.
 5. SupervisorCrashTests  — an unknown-liveness / graceful-denying group is
                            still terminated (``terminated_forced``); a healthy
                            group terminates ``terminated``.
 6. CommandLifetimeTests  — an expired command is rejected ``expired``; a live
                            one is ``allowed``.
 7. ClockSkewTests        — a far-future command is rejected ``clock_skew``;
                            an on-time one passes ``allowed``.
 8. SigningKeyRotationTests — an old-key-signed command fails the new public
                            key (``invalid_signature``); a new-key command is
                            ``allowed``.
 9. SourceDisconnectTests — a truncated native JSONL is flagged
                            ``disconnected`` and the managed bridge persists a
                            ``capture_quality_changed`` + ``capture_gap`` pair
                            (schema-validated); a complete tail has no false
                            gap.
10. EscapedChildTests     — an escaped child degrades terminate to
                            ``escape_unverified``; a contained group ends
                            ``terminated``.
11. RollbackIndependenceTests — the backend assembles with no frontend tree
                            and keeps the session/stream routes; the release
                            routing example keeps SSE long-read and stays
                            credential-free; the standalone frontend release
                            is a required module set free of credential tokens.
"""
from __future__ import annotations

import json
import shutil
import signal
import sqlite3
import struct
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from session_schema import validate_event

from hub.application.supervisor_service import SupervisorService
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.domain import control as ctrl
from hub.infrastructure.session_repository import SessionRepository
from tools.session.bridge import SessionBridge
from tools.session.capture.native_jsonl import tailer
from tools.session.spool import LocalSpool
from tools.session.uploader import SessionUploader
from tools.supervisor.supervisor import GroupOps as _GroupOpsBase
from tools.supervisor.supervisor import Supervisor as _SupervisorBase

REPO_ROOT = Path(__file__).resolve().parents[1]
MACHINE = "mac-chaos"
SESS = "sess_chaos"
TEST_KEY = bytes(range(32))

# On-disk magic + reserved u64 header is 16 bytes.
_SEGMENT_HEADER = 16
_FRAME_HEADER = 12


# --------------------------------------------------------------------------- #
# shared fixtures
# --------------------------------------------------------------------------- #

def _keypair():
    return ctrl.generate_ed25519_keypair()


def _now_s() -> float:
    return datetime.now(timezone.utc).timestamp()


def _rfc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace(
        "+00:00", "Z")


def _command_payload(*, machine=MACHINE, session=SESS, attempt=None,
                     action="pause_session", nonce="n", command_id="cmd",
                     issued_s=None, expires_delta=600, reason_code="op"):
    now = issued_s if issued_s is not None else _now_s()
    return {
        "command_id": command_id,
        "action": action,
        "target": {
            "machine_id": machine,
            "session_id": session,
            "attempt_id": attempt,
        },
        "issued_at": _rfc(now),
        "expires_at": _rfc(now + expires_delta),
        "nonce": nonce,
        "reason_code": reason_code,
    }


def _sign(body: dict, priv: bytes) -> dict:
    body = dict(body)
    body["signature"] = ctrl.sign_payload(
        priv, ctrl.canonical_json_bytes(body))
    return body


def _validate(body, *, pub):
    return ctrl.validate_command(
        body, public_key=pub, expected_machine=MACHINE,
        now=_now_s(), max_clock_skew_s=300.0, nonce_used=lambda nonce: False)


def _evt(seq, kind="assistant_message", quality="structured",
         text="hello"):
    event = {
        "schema_version": 1,
        "event_id": f"evt_{seq:08x}",
        "stream_id": "stream_alpha",
        "machine_id": MACHINE,
        "session_id": SESS,
        "sequence": seq,
        "kind": kind,
        "capture_quality": quality,
        "source": "source",
        "emitted_at": _rfc(_now_s()),
    }
    if kind in ("user_message", "assistant_message"):
        event["payload"] = {"text": text, "is_complete": True}
    elif kind == "capture_gap":
        event["payload"] = {
            "start_sequence": seq - 1,
            "end_sequence": seq - 1,
            "quality": "best_effort",
            "reason": "spool_quota",
        }
    else:
        event["payload"] = {"step_id": "s1", "status": "ok"}
    return validate_event(event)


def _json_size(event) -> int:
    return len(json.dumps(event, ensure_ascii=True,
                          separators=(",", ":")).encode("utf-8"))


def _frame_cost(event) -> int:
    """On-disk footprint of one event frame (matches spool internals)."""
    return _FRAME_HEADER + 12 + _json_size(event) + 16


def _make_spool(root, session_id=SESS, **kwargs) -> LocalSpool:
    kwargs.setdefault("key", TEST_KEY)
    return LocalSpool(root, MACHINE, session_id, **kwargs)


class _FaultProc:
    """Deterministic fake process-group member (no real process)."""

    def __init__(self, backend, *, alive=True, alive_unknown=False,
                 escaped=False):
        self.backend = backend
        self.pid = backend._next_pid()
        self.alive = alive
        self.alive_unknown = alive_unknown
        self.escaped = escaped
        self.signals: list[int] = []
        self.exit_code = None

    def poll(self):
        return None if self.alive else (self.exit_code or 0)

    def receive(self, sig):
        self.signals.append(sig)
        if sig == signal.SIGKILL:
            self.alive = False
            self.alive_unknown = False
            self.exit_code = -(sig)
        elif sig in (signal.SIGTERM, signal.SIGINT):
            if not self.backend.deny_graceful:
                self.alive = False
                self.exit_code = -(sig)


class _FaultOps(_GroupOpsBase):
    """GroupOps fake whose failure knobs drive the terminate escalation."""

    def __init__(self, *, alive=True, alive_unknown=False,
                 deny_graceful=False, escaped=False):
        super().__init__(platform="test")
        self.alive = alive
        self.alive_unknown = alive_unknown
        self.deny_graceful = deny_graceful
        self.escaped_flag = escaped
        self._pid_counter = 1000
        self.children: list[_FaultProc] = []

    def _next_pid(self):
        self._pid_counter += 1
        return self._pid_counter

    def capability(self):
        return ("process_group", "1")

    def create(self, argv, cwd, env):
        proc = _FaultProc(self, alive=self.alive,
                          alive_unknown=self.alive_unknown,
                          escaped=self.escaped_flag)
        self.children.append(proc)
        return proc

    def proc_poll(self, proc):
        return proc.poll()

    def proc_wait(self, proc, timeout=None):
        proc.alive = False
        return proc.exit_code if proc.exit_code is not None else 0

    def group_id(self, proc):
        return f"grp_{proc.pid}"

    def group_alive(self, proc):
        if getattr(proc, "alive_unknown", False):
            raise RuntimeError("liveness unknown")
        return proc.alive

    def group_terminate(self, proc, sig):
        proc.receive(sig)
        return True

    def group_kill(self, proc, sig):
        return self.group_terminate(proc, sig)

    def group_reap(self, proc, timeout=None):
        return not proc.alive

    def escaped(self, proc):
        return proc.escaped

    def close(self, proc):
        pass

    def pump(self, proc, timeout, on_line=None, should_abort=None):
        return "eof"


def _launch(tmp: Path, ops, session_id):
    sv = _SupervisorBase(manifest_dir=str(tmp), machine_id=MACHINE, ops=ops)
    sv.launch(agent="codex", command=["/bin/true"], cwd="/tmp",
              env_allowlist={}, session_id=session_id)
    return sv


# --------------------------------------------------------------------------- #
# 1. hub unreachable
# --------------------------------------------------------------------------- #

class HubUnreachableTests(unittest.TestCase):

    def test_poll_transport_failure_degrades_bounded(self):
        from tools.supervisor.control_client import ControlClient, NonceStore

        def raise_transport(path, body, headers=None):
            raise OSError("route unreachable")

        with tempfile.TemporaryDirectory() as td:
            client = ControlClient(
                hub_url="https://weigh.invalid",
                machine_id=MACHINE,
                credential="operator-secret",
                hub_public_key=b"\x00" * 32,
                public_supervisor=None,
                transport=raise_transport,
                nonce_store=NonceStore(path=Path(td) / "used"),
            )
            result = client.poll_once()
        self.assertFalse(result["ok"])
        self.assertEqual(result["commands"], 0)
        self.assertEqual(result["error"], "poll_transport_error")
        # the degraded surface is exactly these keys — nothing else leaks.
        self.assertEqual(set(result), {"ok", "commands", "error"})

    def test_http_transport_retries_transient_timeout_then_succeeds(self):
        from tools.supervisor import control_client as cc
        from tools.supervisor.control_client import ControlClient, NonceStore

        class _Resp:
            status = 200

            def read(self):
                return b'{"ok": true, "commands": []}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        calls = {"n": 0}

        def fake_open(req, timeout=None):
            calls["n"] += 1
            if calls["n"] < 2:
                raise TimeoutError("handshake")
            return _Resp()

        with tempfile.TemporaryDirectory() as td:
            client = ControlClient(
                hub_url="https://weigh.invalid",
                machine_id=MACHINE,
                credential="operator-secret",
                hub_public_key=b"\x00" * 32,
                public_supervisor=None,
                nonce_store=NonceStore(path=Path(td) / "used"),
            )
            fake_opener = mock.Mock()
            fake_opener.open.side_effect = fake_open
            with mock.patch.object(cc.urllib.request, "build_opener",
                                   return_value=fake_opener):
                with mock.patch.object(cc, "_HTTP_RETRY_SLEEP_S", 0):
                    status, body = client._http_transport(
                        "/api/supervisor/poll", None, client._headers())
        self.assertEqual(calls["n"], 2)
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))

    def test_http_transport_does_not_retry_http_error(self):
        from tools.supervisor import control_client as cc
        from tools.supervisor.control_client import ControlClient, NonceStore

        class _Fp:
            def read(self):
                return b'{"ok": false, "error": "forbidden"}'

            def close(self):
                return None

        calls = {"n": 0}

        def fake_open(req, timeout=None):
            calls["n"] += 1
            raise urllib.error.HTTPError(
                "https://weigh.invalid/api/supervisor/poll",
                403, "Forbidden", hdrs=None, fp=_Fp())

        with tempfile.TemporaryDirectory() as td:
            client = ControlClient(
                hub_url="https://weigh.invalid",
                machine_id=MACHINE,
                credential="operator-secret",
                hub_public_key=b"\x00" * 32,
                public_supervisor=None,
                nonce_store=NonceStore(path=Path(td) / "used"),
            )
            fake_opener = mock.Mock()
            fake_opener.open.side_effect = fake_open
            with mock.patch.object(cc.urllib.request, "build_opener",
                                   return_value=fake_opener):
                status, body = client._http_transport(
                    "/api/supervisor/poll", None, client._headers())
        self.assertEqual(calls["n"], 1)
        self.assertEqual(status, 403)
        self.assertEqual(body.get("error"), "forbidden")

    def test_hub_serves_terminate_and_durable_receipt(self):
        svc = SupervisorService(signing_key=_keypair()[0])
        cmd = svc.enqueue(MACHINE, SESS, None, "terminate_session", "op")
        cid = cmd["command_id"]
        delivered = svc.poll(MACHINE)
        self.assertEqual([c["command_id"] for c in delivered], [cid])
        self.assertEqual(svc.command_status(cid), "delivered")
        receipt = svc.receipt(cid, "succeeded", "terminated")
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["reason"], "terminated")
        self.assertTrue(receipt["first"])
        self.assertEqual(svc.command_status(cid), "succeeded")
        text = json.dumps(svc.audit_recent(50))
        self.assertNotIn("signature", text)
        self.assertNotIn("secret", text.lower())
        self.assertNotIn("Traceback", text)
        self.assertNotIn(".json", text)


# --------------------------------------------------------------------------- #
# 2. repository read-only
# --------------------------------------------------------------------------- #

class ReadonlyRepositoryTests(unittest.TestCase):

    def _readonly_repo(self):
        d = Path(tempfile.mkdtemp(prefix="fleet-ro-"))
        repo = SessionRepository(d / "meta.db")
        repo.init()
        (d / "meta.db").chmod(0o444)
        d.chmod(0o555)

        def tidy():
            try:
                d.chmod(0o755)
            except OSError:
                pass
            try:
                (d / "meta.db").chmod(0o644)
            except OSError:
                pass
            shutil.rmtree(d, ignore_errors=True)

        self.addCleanup(tidy)
        return repo

    def test_readonly_write_fails_closed(self):
        repo = self._readonly_repo()
        # A write against a read-only store must fail closed with a bounded
        # sqlite error — never a silent partial write.
        with self.assertRaises(sqlite3.OperationalError):
            repo.upsert_session({
                "session_id": SESS,
                "machine_id": MACHINE,
                "managed": False,
                "capture_quality": "structured",
            })

    def test_writable_upsert_and_read(self):
        d = Path(tempfile.mkdtemp(prefix="fleet-rw-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        repo = SessionRepository(d / "meta.db")
        repo.init()
        row = repo.upsert_session({
            "session_id": SESS,
            "machine_id": MACHINE,
            "managed": False,
            "capture_quality": "structured",
        })
        # the unmanaged capture_quality is preserved by the repo (the DTO layer
        # clamps it to best_effort on the public surface).
        self.assertEqual(row["session_id"], SESS)
        self.assertEqual(row["capture_quality"], "structured")
        rows = repo.list_sessions(machine_id=MACHINE)
        self.assertEqual([r["session_id"] for r in rows], [SESS])


# --------------------------------------------------------------------------- #
# 3. spool full
# --------------------------------------------------------------------------- #

class SpoolFullTests(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="fleet-spool-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_exact_overflow_is_capture_blocked_not_silent_drop(self):
        sp = _make_spool(self.root, max_session_bytes=64)
        self.addCleanup(sp.close)
        result = sp.append(_evt(1))
        self.assertTrue(result.capture_blocked)
        self.assertIsNone(result.sequence)
        self.assertFalse(result.accepted)
        self.assertEqual(sp.status()["last_sequence"], 0)

    def test_accept_then_second_overflows_bounded(self):
        quota = _SEGMENT_HEADER + _frame_cost(_evt(1))
        sp = _make_spool(self.root, max_session_bytes=quota)
        self.addCleanup(sp.close)
        first = sp.append(_evt(1))
        self.assertEqual(first.sequence, 1)
        self.assertEqual(sp.status()["last_sequence"], 1)
        second = sp.append(_evt(2, quality="structured"))
        self.assertTrue(second.capture_blocked)
        self.assertIsNone(second.sequence)
        self.assertEqual([e["sequence"] for e in sp.read_after(0, 10, 1 << 16)],
                         [1])

    def test_best_effort_overflow_signals_gap(self):
        one = _evt(1)
        quota = _SEGMENT_HEADER + _frame_cost(one)
        sp = _make_spool(self.root, max_session_bytes=quota)
        self.addCleanup(sp.close)
        sp.append(one)
        gap = sp.append(_evt(2, quality="best_effort"))
        self.assertFalse(gap.capture_blocked)
        self.assertIsNone(gap.sequence)
        self.assertIsNotNone(gap.gap_sequence)
        self.assertTrue(sp.status()["gap_signaled"])


# --------------------------------------------------------------------------- #
# 4. agent crash before ack — torn frame repair + durable replay cursor
# --------------------------------------------------------------------------- #

class CrashBeforeAckTests(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="fleet-crash-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _tear_last_frame(self, segment: Path) -> None:
        """Simulate a crash mid-write: leave a partial FINAL frame on disk.

        Walks the frame chain recording the byte offset where each new frame
        begins; then truncates the file so the last frame's 8-byte sequence
        header is present but its body is short on disk (a torn tail).
        """
        data = segment.read_bytes()
        pos = _SEGMENT_HEADER
        last_frame_start = _SEGMENT_HEADER
        while True:
            if pos + 12 > len(data):
                break
            enc_len = struct.unpack(">I", bytes(data[pos + 8:pos + 12]))[0]
            if enc_len < 13 or pos + 12 + enc_len > len(data):
                break
            last_frame_start = pos
            pos += 12 + enc_len
        if last_frame_start <= _SEGMENT_HEADER:
            return  # nothing to tear
        # keep every complete frame plus a 3-byte fragment of the final one.
        segment.write_bytes(bytes(data[:last_frame_start + 3]))

    def test_torn_frame_repair_resumes_without_hole(self):
        sp = _make_spool(self.root)
        self.addCleanup(sp.close)
        for n in range(1, 6):
            sp.append(_evt(n))
        sp.close()
        seg = sorted((self.root / "segments").glob("*.sealed"))[-1]
        self._tear_last_frame(seg)
        fresh = _make_spool(self.root)
        self.addCleanup(fresh.close)
        # the torn frame's sequence is never durable
        self.assertEqual(fresh.status()["last_sequence"], 4)
        fresh.append(_evt(99))
        self.assertEqual(fresh.status()["last_sequence"], 5)
        seqs = [e["sequence"] for e in fresh.read_after(0, 64, 1 << 16)]
        self.assertEqual(seqs, [1, 2, 3, 4, 5])

    def test_uploader_replay_resumes_from_ack_cursor(self):
        sp = _make_spool(self.root)
        for n in range(1, 7):
            sp.append(_evt(n))
        sp.ack(4)
        sp.close()

        fresh = _make_spool(self.root)
        sent: list = []

        def fake_post(payload):
            sent.append(payload["events"])
            return {"ok": True, "accepted_through": 6,
                    "next_cursor": "c", "rejected": [], "request_id": "r"}

        uploader = SessionUploader(fake_post,
                                   spool=fresh,
                                   batch_events=100, batch_bytes=262144)
        self.addCleanup(fresh.close)
        acked = uploader.flush_once()
        self.assertEqual(acked, 2)      # only the unacked tail
        self.assertEqual([e["sequence"] for e in sent[0]], [5, 6])
        self.assertEqual(fresh.status()["ack_sequence"], 6)


# --------------------------------------------------------------------------- #
# 5. supervisor crash / unresponsive during terminate
# --------------------------------------------------------------------------- #

class SupervisorCrashTests(unittest.TestCase):

    def _make(self, **knobs):
        with tempfile.TemporaryDirectory() as td:
            chaos = _FaultOps(**knobs)
            sv = _launch(Path(td), chaos, SESS)
            outcome = sv.terminate_session(SESS, grace_s=0.02)
            sstate = sv.get(SESS).state
            return outcome, sstate, chaos

    def test_unknown_liveness_still_terminates_forced(self):
        outcome, sstate, chaos = self._make(alive=True, alive_unknown=True,
                                            deny_graceful=True)
        self.assertEqual(outcome, "terminated_forced")
        self.assertEqual(sstate, "terminated")
        handle = chaos.children[0] if getattr(chaos, "children", None) else None
        # SIGTERM first, then the forced SIGKILL escalation — never silent.
        self.assertIn(signal.SIGTERM, handle.signals)
        self.assertIn(signal.SIGKILL, handle.signals)

    def test_graceful_terminate_succeeds(self):
        outcome, sstate, chaos = self._make(alive=True, alive_unknown=False,
                                            deny_graceful=False)
        self.assertEqual(outcome, "terminated")
        self.assertEqual(sstate, "terminated")


# --------------------------------------------------------------------------- #
# 6. stale / expired command
# --------------------------------------------------------------------------- #

class CommandLifetimeTests(unittest.TestCase):

    def setUp(self):
        self.priv, self.pub = _keypair()

    def test_expired_rejected(self):
        body = _sign(_command_payload(expires_delta=-3600.0), self.priv)
        ok, code = _validate(body, pub=self.pub)
        self.assertFalse(ok)
        self.assertEqual(code, "expired")

    def test_live_allowed(self):
        body = _sign(_command_payload(), self.priv)
        ok, code = _validate(body, pub=self.pub)
        self.assertTrue(ok)
        self.assertEqual(code, "allowed")


# --------------------------------------------------------------------------- #
# 7. clock skew
# --------------------------------------------------------------------------- #

class ClockSkewTests(unittest.TestCase):

    def setUp(self):
        self.priv, self.pub = _keypair()

    def test_far_future_rejected_clock_skew(self):
        future = _now_s() + 1000.0
        body = _sign(_command_payload(issued_s=future, expires_delta=1200),
                     self.priv)
        ok, code = _validate(body, pub=self.pub)
        self.assertFalse(ok)
        self.assertEqual(code, "clock_skew")

    def test_within_skew_allowed(self):
        body = _sign(_command_payload(issued_s=_now_s()), self.priv)
        ok, code = _validate(body, pub=self.pub)
        self.assertTrue(ok)
        self.assertEqual(code, "allowed")


# --------------------------------------------------------------------------- #
# 8. signing key rotation
# --------------------------------------------------------------------------- #

class SigningKeyRotationTests(unittest.TestCase):

    def test_old_key_rejected_under_new_key(self):
        old_priv, _ = _keypair()
        _new, new_pub = _keypair()
        body = _sign(_command_payload(nonce="rot-1"), old_priv)
        ok, code = _validate(body, pub=new_pub)
        self.assertFalse(ok)
        self.assertEqual(code, "invalid_signature")

    def test_new_key_command_accepted(self):
        priv, pub = _keypair()
        body = _sign(_command_payload(nonce="rot-2"), priv)
        ok, code = _validate(body, pub=pub)
        self.assertTrue(ok)
        self.assertEqual(code, "allowed")


# --------------------------------------------------------------------------- #
# 9. source disconnect
# --------------------------------------------------------------------------- #

class SourceDisconnectTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fleet-disc-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _bridge(self) -> SessionBridge:
        manifest = {
            "agent": "claude", "version": "1.0.0", "installed": True,
            "spawn": False, "resume": False, "native_transcript": True,
            "structured_stream": False, "hooks": False, "pty": False,
            "supported_event_kinds": [],
        }
        spool = _make_spool(self.tmp / "spool", max_session_bytes=1 << 16)
        self.addCleanup(spool.close)
        return SessionBridge.open(manifest, {
            "session_id": SESS,
            "machine_id": MACHINE,
            "stream_id": "stream_alpha",
            "process_group_id": "grp_1",
            "managed": True,
            "spool": spool,
        })

    def test_truncated_native_tail_persists_gap_pair(self):
        path = self.tmp / "native.jsonl"
        path.write_text(
            '{"kind":"user_message","text":"one"}\n'
            '{"kind":"user_message","text":"tw', encoding="utf-8")
        bridge = self._bridge()
        count = bridge.ingest_native(str(path))
        self.assertGreaterEqual(count, 1)          # complete line fed
        replay = bridge.spool.read_after(0, 100, 1 << 16)
        kinds = [e["kind"] for e in replay]
        self.assertIn("user_message", kinds)
        self.assertIn("capture_quality_changed", kinds)
        self.assertIn("capture_gap", kinds)
        for event in replay:
            validate_event(event)
        flat = json.dumps(replay)
        self.assertNotIn("Traceback", flat)
        # the raw transcript path never reaches durable/validated events.
        self.assertNotIn(self.tmp.name, flat)

    def test_plain_tailer_disconnected_on_truncated_tail(self):
        path = self.tmp / "plain.jsonl"
        path.write_text('{"a":1}\n{"b":', encoding="utf-8")
        t = tailer(str(path))
        t.read()
        self.assertTrue(t.disconnected)

    def test_complete_tail_no_false_gap(self):
        path = self.tmp / "clean.jsonl"
        path.write_text('{"kind":"user_message","text":"one"}\n',
                        encoding="utf-8")
        bridge = self._bridge()
        bridge.ingest_native(str(path))
        replay = bridge.spool.read_after(0, 100, 1 << 16)
        self.assertEqual([e["kind"] for e in replay], ["user_message"])


# --------------------------------------------------------------------------- #
# 10. escaped child
# --------------------------------------------------------------------------- #

class EscapedChildTests(unittest.TestCase):

    def _make(self, escaped=False):
        with tempfile.TemporaryDirectory() as td:
            chaos = _FaultOps(escaped=escaped)
            sv = _launch(Path(td), chaos, SESS)
            outcome = sv.terminate_session(SESS, grace_s=0.05)
            state = sv.get(SESS).state
        return outcome, state

    def test_escaped_child_terminate_degrades_bounded(self):
        outcome, state = self._make(escaped=True)
        self.assertEqual(outcome, "escape_unverified")
        self.assertEqual(state, "terminated")

    def test_no_escape_terminate_succeeds(self):
        outcome, state = self._make(escaped=False)
        self.assertEqual(outcome, "terminated")
        self.assertEqual(state, "terminated")


# --------------------------------------------------------------------------- #
# 11. frontend/backend rollback independence
# --------------------------------------------------------------------------- #

class RollbackIndependenceTests(unittest.TestCase):

    def test_backend_app_assembles_with_no_frontend_tree(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            root.mkdir(parents=True, exist_ok=True)
            cfg = FleetConfig(
                root=root,
                state_dir=root / "state",
                hosts_file=root / "hosts.yaml",
                event_log=root / "state" / "events.jsonl",
                task_db=root / "state" / "fleet_tasks.db",
                ingest_token="",
                tasks_enabled=False,
                frontend_cutover=False,
                frontend_dir=root / "no-such-frontend",
                session_repositories_enabled=True,
                session_db=root / "var" / "sessions" / "meta.db",
                session_transcript_root=root / "var" / "sessions" / "transcripts",
                session_encryption_raw=TEST_KEY,
            )
            app = create_app(cfg)
            with app.test_client() as c:
                self.assertEqual(c.get("/api/status").status_code, 200)
            rules = {r.rule for r in app.url_map.iter_rules()}
            self.assertIn("/api/session-events", rules)
            self.assertIn("/api/stream", rules)

    def test_release_routing_config_contracts(self):
        src = (REPO_ROOT / "deploy" / "nginx-frontend-backend.example.conf"
               ).read_text()
        self.assertIn("location /api/stream", src)
        start = src.index("location /api/stream")
        block = src[start:src.index("location /api/", start + 20)]
        self.assertIn("proxy_buffering off", block)
        self.assertIn("proxy_read_timeout", block)
        self.assertTrue(
            any(tok in block for tok in
                ("proxy_read_timeout 3600s", "proxy_read_timeout 1800s",
                 "proxy_read_timeout 600s")),
            "SSE read timeout must be long")
        self.assertIn("proxy_http_version 1.1", block)
        self.assertIn("proxy_set_header Connection", block)
        for token in ("BEGIN PRIVATE KEY", "ingest-token", "runner-credential",
                      "secret="):
            self.assertNotIn(token, src)

    def test_frontend_release_module_set_registered(self):
        frontend = REPO_ROOT / "frontend"
        required = ("index.html", "config.js", "api/client.js",
                    "api/contracts.js", "realtime/sse.js", "state/store.js",
                    "views/session.js")
        for rel in required:
            self.assertTrue((frontend / rel).is_file(), rel)

    def test_frontend_release_is_credential_free(self):
        frontend = REPO_ROOT / "frontend"
        if not frontend.is_dir():
            self.skipTest("frontend tree not present")
        files = [p for p in frontend.rglob("*") if p.is_file()]
        self.assertTrue(files)
        for relative in files:
            source = relative.read_text()
            for token in ("X-Agent-Fleet-Token", "X-Runner-Credential",
                          "BEGIN PRIVATE KEY"):
                self.assertNotIn(token, source, f"{relative.name} leaked token")


if __name__ == "__main__":
    unittest.main()