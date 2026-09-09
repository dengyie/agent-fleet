"""Task 9 tests — Supervisor credential, signed command queue and receipts.

Covers:
- fixed CONTROL_ACTIONS / CONTROL_STATES enums (and forbidden set);
- canonical JSON serialization + Ed25519 sign/verify (incl. tamper + key-order);
- command TTL / clock skew / nonce replay / scope checks in the domain layer;
- supervisor poll/receipt service: enqueue, delivery, expiry, idempotent
  receipts, conflict precedence (terminate/quarantine > resume), audit;
- HTTP supervisors: require_supervisor auth isolation, missing signing key
  disables issuance (fail closed), no /api/v1/supervisor surface;
- Agent control client: local signature/scope/TTL/nonce validation, persisted
  used nonce, fixed-actions-only execution through Supervisor, bounded receipts.

No key/secret VALUE ever appears in test output or assertions (only byte
lengths and structural checks).
"""
from __future__ import annotations

import base64
import json
import secrets
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization as _ser
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hub import events
from hub import state as store
from hub import task_store
from hub.application.supervisor_service import (
    SupervisorService,
    SupervisorServiceError,
)
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.domain import control as ctrl
from hub.domain import supervisor as sup_dom

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


def _command_payload(*, machine="mac-local", session="sess", attempt=None,
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


def _sign(body: dict, priv: bytes, *, insert_order=None) -> dict:
    """Return a signed copy of ``body`` (keys sorted during signing)."""
    body = dict(body)
    payload = ctrl.canonical_json_bytes(body)
    body["signature"] = ctrl.sign_payload(priv, payload)
    if insert_order:
        outer = {k: body[k] for k in insert_order if k in body}
        for k, v in body.items():
            outer.setdefault(k, v)
        return outer
    return body


class ControlDomainTests(unittest.TestCase):
    def test_control_actions_are_exactly_the_fixed_set(self):
        self.assertEqual(
            set(ctrl.CONTROL_ACTIONS),
            {"pause_session", "resume_session", "terminate_session",
             "quarantine_session", "cancel_attempt", "adopt", "detach",
             "append_user_turn", "apply_local_profile"},
        )
        self.assertEqual(
            set(ctrl.PAYLOAD_ACTIONS),
            {"append_user_turn", "apply_local_profile"},
        )

    def test_forbidden_actions_never_in_fixed_set(self):
        forbidden = {"raw_cmd", "exec_shell", "write_file", "inject_stdin",
                     "tmux_command", "arbitrary_signal", "change_credential",
                     "change_policy"}
        self.assertTrue(forbidden.isdisjoint(ctrl.CONTROL_ACTIONS))
        self.assertEqual(len(ctrl.CONTROL_ACTIONS), 9)

    def test_control_states_cover_queue_machine(self):
        self.assertEqual(
            set(ctrl.CONTROL_STATES),
            {"queued", "delivered", "accepted", "rejected", "executing",
             "succeeded", "already_finished", "failed", "expired"},
        )

    def test_canonical_json_is_key_sorted_deterministic(self):
        a = {"z": 1, "a": [2, {"y": 3, "x": 4}], "m": "sn"}
        b = {"a": [2, {"x": 4, "y": 3}], "m": "sn", "z": 1}
        self.assertEqual(ctrl.canonical_json_bytes(a),
                         ctrl.canonical_json_bytes(b))
        self.assertEqual(ctrl.canonical_json_bytes(a),
                         b'{"a":[2,{"x":4,"y":3}],"m":"sn","z":1}')

    def test_keypair_widths(self):
        priv, pub = ctrl.generate_ed25519_keypair()
        self.assertEqual(len(priv), 32)
        self.assertEqual(len(pub), 32)

    def test_sign_verify_roundtrip(self):
        priv, pub = _keypair()
        payload = ctrl.canonical_json_bytes({"command_id": "c1"})
        sig = ctrl.sign_payload(priv, payload)
        self.assertTrue(ctrl.verify_payload(pub, payload, sig))
        self.assertFalse(ctrl.verify_payload(pub, b"other", sig))

    def test_verify_rejects_tamper(self):
        priv, pub = _keypair()
        sig = ctrl.sign_payload(
            priv, ctrl.canonical_json_bytes({"action": "pause_session"}))
        tampered = ctrl.canonical_json_bytes({"action": "terminate_session"})
        self.assertFalse(ctrl.verify_payload(pub, tampered, sig))

    def test_payload_bytes_exclude_signature_and_are_deterministic(self):
        priv, _pub = _keypair()
        body = _sign(_command_payload(action="pause_session", nonce="aa"),
                     priv)
        payload = ctrl.payload_bytes(body)
        self.assertNotIn(b"signature", payload)
        reordered = {}
        for k in ("signature", "action", "target", "issued_at", "expires_at",
                  "nonce", "reason_code", "command_id"):
            if k in body:
                reordered[k] = body[k]
        self.assertEqual(ctrl.payload_bytes(reordered), payload)

    def test_command_dict_roundtrip(self):
        priv, _pub = _keypair()
        body = _sign(_command_payload(action="cancel_attempt", nonce="bb",
                                       attempt="att_1"), priv)
        cmd = ctrl.command_from_dict(body)
        self.assertEqual(cmd.target.session_id, body["target"]["session_id"])
        self.assertEqual(cmd.target.attempt_id, "att_1")
        self.assertEqual(cmd.action, "cancel_attempt")
        back = ctrl.command_to_dict(cmd)
        self.assertEqual(back["target"]["machine_id"],
                         body["target"]["machine_id"])
        self.assertEqual(back["reason_code"], body["reason_code"])

    def test_command_dict_roundtrip_keeps_bounded_payload(self):
        priv, _pub = _keypair()
        body = _command_payload(action="append_user_turn", nonce="pay1")
        body["payload"] = {"text": "hello"}
        signed = _sign(body, priv)
        cmd = ctrl.command_from_dict(signed)
        self.assertEqual(cmd.action, "append_user_turn")
        self.assertEqual(cmd.payload, {"text": "hello"})
        back = ctrl.command_to_dict(cmd)
        self.assertEqual(back["payload"], {"text": "hello"})


# --------------------------------------------------------------------------- #
# domain: validate_command — signature / action-set / TTL / skew / nonce
# --------------------------------------------------------------------------- #

class ControlValidationTests(unittest.TestCase):
    def setUp(self):
        self.priv, self.pub = _keypair()

    def _check(self, body, *, machine="mac-01", used=None):
        return ctrl.validate_command(
            body,
            public_key=self.pub,
            expected_machine=machine,
            now=_now_s(),
            max_clock_skew_s=300,
            nonce_used=lambda n: n in (used or ()),
        )

    def test_valid_command_passes(self):
        body = _sign(_command_payload(action="pause_session", nonce="pass1",
                                       machine="mac-01"), self.priv)
        ok, err = self._check(body)
        self.assertTrue(ok, err)
        self.assertEqual(err, "allowed")

    def test_key_order_does_not_affect_verification(self):
        body = _sign(_command_payload(action="pause_session", nonce="ok2",
                                       machine="mac-01"),
                     self.priv,
                     insert_order=("expires_at", "nonce", "reason_code",
                                   "target", "command_id", "action",
                                   "issued_at"))
        ok, err = self._check(body)
        self.assertTrue(ok, err)

    def test_action_not_in_fixed_set_rejected(self):
        body = _sign(_command_payload(action="exec_shell", nonce="x3"),
                     self.priv)
        ok, err = self._check(body)
        self.assertFalse(ok)
        self.assertEqual(err, "unsupported_action")

    def test_forbidden_action_rejected(self):
        body = _sign(_command_payload(action="change_policy", nonce="x4"),
                     self.priv)
        ok, err = self._check(body)
        self.assertFalse(ok)
        self.assertEqual(err, "unsupported_action")

    def test_machine_mismatch_rejected(self):
        body = _sign(_command_payload(action="pause_session", nonce="x5",
                                       machine="other-x"), self.priv)
        ok, err = self._check(body)
        self.assertFalse(ok)
        self.assertEqual(err, "scope_mismatch")

    def test_expired_command_rejected(self):
        body = _sign(_command_payload(action="pause_session", nonce="x6",
                                       machine="mac-01", expires_delta=-30),
                     self.priv)
        ok, err = self._check(body)
        self.assertFalse(ok)
        self.assertEqual(err, "expired")

    def test_future_issued_beyond_skew_rejected(self):
        body = _sign(_command_payload(action="pause_session", nonce="x7",
                                       machine="mac-01",
                                       issued_s=_now_s() + 1000,
                                       expires_delta=1200), self.priv)
        ok, err = self._check(body)
        self.assertFalse(ok)
        self.assertEqual(err, "clock_skew")

    def test_nonce_replay_rejected(self):
        body = _sign(_command_payload(action="pause_session",
                                       machine="mac-01",
                                       nonce="already-used"), self.priv)
        ok, err = self._check(body, used={"already-used"})
        self.assertFalse(ok)
        self.assertEqual(err, "nonce_replay")

    def test_bad_signature_rejected(self):
        body = _sign(_command_payload(action="pause_session", nonce="x8",
                                       machine="mac-01"), self.priv)
        body["signature"] = base64.b64encode(b"\x00" * 64).decode()
        ok, err = self._check(body)
        self.assertFalse(ok)
        self.assertEqual(err, "invalid_signature")

    def test_missing_signature_rejected(self):
        body = _command_payload(action="pause_session", nonce="x9",
                                machine="mac-01")
        ok, err = self._check(body)
        self.assertFalse(ok)
        self.assertEqual(err, "malformed_signature")

    def test_wrong_key_never_unsigned_accept(self):
        body = _sign(_command_payload(action="pause_session", nonce="x10",
                                       machine="mac-01"),
                     b"\x01" * 32)
        ok, err = self._check(body)
        self.assertFalse(ok)
        self.assertEqual(err, "invalid_signature")


# --------------------------------------------------------------------------- #
# supervisor application service
# --------------------------------------------------------------------------- #

class _Clock:
    def __init__(self, now=None):
        self.t = now if now is not None else _now_s()

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class SupervisorServiceTests(unittest.TestCase):
    def setUp(self):
        self.priv, self.pub = _keypair()

    def _svc(self, *, signing=None, ttl_s=3600, clock=None):
        return SupervisorService(
            signing_key=signing,
            now_fn=clock if clock is not None else _Clock(),
            ttl_s=ttl_s,
            max_clock_skew_s=300,
        )

    def test_enqueue_requires_signing_key(self):
        sv = self._svc(signing=None)
        with self.assertRaises(SupervisorServiceError) as cm:
            sv.enqueue("mac-01", "sess_1", None, "pause_session", "x")
        self.assertEqual(cm.exception.code, "supervisor_disabled")
        self.assertEqual(cm.exception.status, 503)

    def test_enqueue_creates_signed_command_envelope(self):
        sv = self._svc(signing=self.priv)
        cmd = sv.enqueue("mac-01", "sess_1", None, "pause_session", "x")
        self.assertEqual(cmd["action"], "pause_session")
        self.assertEqual(cmd["target"]["session_id"], "sess_1")
        self.assertEqual(cmd["reason_code"], "x")
        for field in ("command_id", "target", "issued_at", "expires_at",
                      "nonce", "signature"):
            self.assertIn(field, cmd)
        body = dict(cmd)
        sig = body.pop("signature")
        self.assertTrue(ctrl.verify_payload(
            sv.public_key(), ctrl.canonical_json_bytes(body), sig))
        self.assertNotIn("status", cmd)

    def test_enqueue_rejects_unsupported_action(self):
        sv = self._svc(signing=self.priv)
        with self.assertRaises(SupervisorServiceError) as cm:
            sv.enqueue("mac-01", "sess_1", None, "write_file", "x")
        self.assertEqual(cm.exception.code, "unsupported_action")

    def test_phase45_actions_default_off(self):
        sv = self._svc(signing=self.priv)
        with self.assertRaises(SupervisorServiceError) as cm:
            sv.enqueue("mac-01", "sess_1", None, "append_user_turn", "x",
                       payload={"text": "hello"})
        self.assertEqual(cm.exception.code, "feature_disabled")
        self.assertEqual(cm.exception.status, 403)
        with self.assertRaises(SupervisorServiceError) as cm:
            sv.enqueue("mac-01", "sess_1", None, "apply_local_profile", "x",
                       payload={"profile_id": "p1"})
        self.assertEqual(cm.exception.code, "feature_disabled")

    def test_phase45_enqueue_signs_bounded_payload_when_enabled(self):
        sv = SupervisorService(
            signing_key=self.priv, now_fn=_Clock(), ttl_s=3600,
            append_user_turn_enabled=True, apply_local_profile_enabled=True)
        cmd = sv.enqueue("mac-01", "sess_1", None, "append_user_turn", "x",
                         payload={"text": "hello"})
        self.assertEqual(cmd["payload"], {"text": "hello"})
        body = dict(cmd)
        sig = body.pop("signature")
        self.assertTrue(ctrl.verify_payload(
            sv.public_key(), ctrl.canonical_json_bytes(body), sig))
        profile = sv.enqueue(
            "mac-01", "sess_1", None, "apply_local_profile", "x",
            payload={"profile_id": "local-1"})
        self.assertEqual(profile["payload"], {"profile_id": "local-1"})

    def test_phase45_payload_rejects_secrets_and_shell(self):
        sv = SupervisorService(
            signing_key=self.priv, now_fn=_Clock(), ttl_s=3600,
            append_user_turn_enabled=True, apply_local_profile_enabled=True)
        with self.assertRaises(SupervisorServiceError) as cm:
            sv.enqueue("mac-01", "sess_1", None, "append_user_turn", "x",
                       payload={"text": "hi", "api_key": "secret"})
        self.assertEqual(cm.exception.code, "invalid_payload")
        with self.assertRaises(SupervisorServiceError) as cm:
            sv.enqueue("mac-01", "sess_1", None, "apply_local_profile", "x",
                       payload={"profile_id": "p1", "command": "echo"})
        self.assertEqual(cm.exception.code, "invalid_payload")

    def test_phase45_payload_exact_allowlist_rejects_extra_keys(self):
        sv = SupervisorService(
            signing_key=self.priv, now_fn=_Clock(), ttl_s=3600,
            append_user_turn_enabled=True, apply_local_profile_enabled=True)
        with self.assertRaises(SupervisorServiceError) as cm:
            sv.enqueue("mac-01", "sess_1", None, "append_user_turn", "x",
                       payload={"text": "hello", "cwd": "/tmp"})
        self.assertEqual(cm.exception.code, "invalid_payload")
        with self.assertRaises(SupervisorServiceError) as cm:
            sv.enqueue("mac-01", "sess_1", None, "apply_local_profile", "x",
                       payload={"profile_id": "local-1", "family": "codex"})
        self.assertEqual(cm.exception.code, "invalid_payload")
        with self.assertRaises(ValueError):
            ctrl.validated_control_payload(
                "append_user_turn", {"text": "hello", "cwd": "/tmp"})

    def test_poll_only_returns_own_machine(self):
        sv = self._svc(signing=self.priv)
        sv.enqueue("mac-a", "s1", None, "pause_session", "x")
        sv.enqueue("mac-b", "s2", None, "pause_session", "x")
        got_a = sv.poll("mac-a")
        self.assertEqual([c["target"]["session_id"] for c in got_a], ["s1"])
        got_b = sv.poll("mac-b")
        self.assertEqual([c["target"]["session_id"] for c in got_b], ["s2"])

    def test_poll_delivers_then_does_not_redeliver(self):
        sv = self._svc(signing=self.priv)
        sv.enqueue("mac-a", "s1", None, "pause_session", "x")
        first = sv.poll("mac-a")
        self.assertTrue(first)
        self.assertEqual(sv.poll("mac-a"), [])

    def test_poll_expires_and_marks_stale(self):
        clock = _Clock()
        sv = self._svc(signing=self.priv, ttl_s=60, clock=clock)
        cmd = sv.enqueue("mac-a", "s1", None, "pause_session", "x")
        clock.advance(120)
        self.assertEqual(sv.poll("mac-a"), [])
        self.assertEqual(sv.command_status(cmd["command_id"]), "expired")

    def test_receipt_lifecycle_transitions(self):
        sv = self._svc(signing=self.priv)
        cmd = sv.enqueue("mac-a", "s1", None, "pause_session", "x")
        cid = cmd["command_id"]
        self.assertEqual(sv.command_status(cid), "queued")
        r = sv.receipt(cid, "succeeded", "paused")
        self.assertEqual(r["status"], "succeeded")
        self.assertEqual(sv.command_status(cid), "succeeded")

    def test_receipt_unknown_command_rejected(self):
        sv = self._svc(signing=self.priv)
        with self.assertRaises(SupervisorServiceError) as cm:
            sv.receipt("cmd-ghost", "succeeded", "ok")
        self.assertEqual(cm.exception.code, "command_not_found")

    def test_receipt_duplicate_is_idempotent(self):
        sv = self._svc(signing=self.priv)
        cmd = sv.enqueue("mac-a", "s1", None, "terminate_session", "t")
        cid = cmd["command_id"]
        sv.receipt(cid, "succeeded", "terminated")
        again = sv.receipt(cid, "failed", "later-junk")
        self.assertEqual(again["status"], "succeeded")
        self.assertEqual(again["reason"], "terminated")
        self.assertEqual(sv.command_status(cid), "succeeded")

    def test_terminal_receipt_replays_original_deterministically(self):
        # A duplicate receipt on an already-terminal command must REPLAY the
        # original receipt (status + reason), never be overwritten by the
        # later junk — the invariant is deterministic, not a first-write-wins
        # race.
        sv = self._svc(signing=self.priv)
        cmd = sv.enqueue("mac-a", "s1", None, "pause_session", "x")
        cid = cmd["command_id"]
        first = sv.receipt(cid, "succeeded", "paused")
        self.assertTrue(first["first"])
        for _attempt in range(3):
            replay = sv.receipt(cid, "failed", "later junk")
            self.assertFalse(replay["first"])
            self.assertEqual(replay["status"], "succeeded")
            self.assertEqual(replay["reason"], "paused")
        self.assertEqual(sv.command_status(cid), "succeeded")

    def test_receipt_after_expiry_keeps_expired_state(self):
        # Once a command is terminal (expired), a late receipt cannot resurrect
        # it into "succeeded" — the original terminal receipt is replayed.
        clock = _Clock()
        sv = self._svc(signing=self.priv, ttl_s=60, clock=clock)
        cmd = sv.enqueue("mac-a", "s1", None, "pause_session", "x")
        cid = cmd["command_id"]
        clock.advance(120)
        sv.poll("mac-a")  # runs _expire_stale
        self.assertEqual(sv.command_status(cid), "expired")
        stale = sv.receipt(cid, "succeeded", "late")
        self.assertEqual(stale["status"], "expired")
        self.assertEqual(stale["reason"], "expired")
        self.assertEqual(sv.command_status(cid), "expired")

    def test_duplicate_nonce_rejected_on_enqueue(self):
        sv = self._svc(signing=self.priv)
        cmd = sv.enqueue("mac-a", "s1", None, "pause_session", "x")
        with self.assertRaises(SupervisorServiceError) as cm:
            sv.enqueue("mac-a", "s1", None, "pause_session", "x",
                       nonce=cmd["nonce"])
        self.assertEqual(cm.exception.code, "duplicate_nonce")

    def test_terminate_blocks_later_resume(self):
        sv = self._svc(signing=self.priv)
        sv.enqueue("mac-a", "s1", None, "terminate_session", "p")
        with self.assertRaises(SupervisorServiceError) as cm:
            sv.enqueue("mac-a", "s1", None, "resume_session", "o")
        self.assertEqual(cm.exception.code, "resume_superseded")

    def test_quarantine_blocks_later_resume(self):
        sv = self._svc(signing=self.priv)
        sv.enqueue("mac-a", "s1", None, "quarantine_session", "p")
        with self.assertRaises(SupervisorServiceError) as cm:
            sv.enqueue("mac-a", "s1", None, "resume_session", "o")
        self.assertEqual(cm.exception.code, "resume_superseded")

    def test_terminate_supersedes_queued_resume(self):
        sv = self._svc(signing=self.priv)
        sv.enqueue("mac-a", "s1", None, "resume_session", "o")
        sv.enqueue("mac-a", "s1", None, "terminate_session", "p")
        delivered = sv.poll("mac-a")
        actions = [c["action"] for c in delivered]
        self.assertEqual(actions, ["terminate_session"])
        self.assertNotIn("resume_session", actions)

    def test_terminate_supersedes_queued_append_user_turn(self):
        sv = SupervisorService(
            signing_key=self.priv, now_fn=_Clock(), ttl_s=3600,
            append_user_turn_enabled=True)
        sv.enqueue("mac-a", "s1", None, "append_user_turn", "o",
                   payload={"text": "hello"})
        sv.enqueue("mac-a", "s1", None, "terminate_session", "p")
        delivered = sv.poll("mac-a")
        actions = [c["action"] for c in delivered]
        self.assertEqual(actions, ["terminate_session"])
        self.assertNotIn("append_user_turn", actions)

    def test_quarantine_blocks_later_append_user_turn(self):
        sv = SupervisorService(
            signing_key=self.priv, now_fn=_Clock(), ttl_s=3600,
            append_user_turn_enabled=True)
        sv.enqueue("mac-a", "s1", None, "quarantine_session", "p")
        with self.assertRaises(SupervisorServiceError) as cm:
            sv.enqueue("mac-a", "s1", None, "append_user_turn", "o",
                       payload={"text": "hello"})
        self.assertEqual(cm.exception.code, "resume_superseded")

    def test_poll_and_receipt_missing_key_fail_closed(self):
        sv = self._svc(signing=None)
        with self.assertRaises(SupervisorServiceError) as cm:
            sv.poll("mac-a")
        self.assertEqual(cm.exception.code, "supervisor_disabled")
        with self.assertRaises(SupervisorServiceError):
            sv.receipt("x", "succeeded", "ok")

    def test_audit_rows_are_bounded_and_leak_free(self):
        sv = self._svc(signing=self.priv)
        cmd = sv.enqueue("mac-a", "s1", None, "pause_session", "x")
        sv.poll("mac-a")
        sv.receipt(cmd["command_id"], "succeeded", "paused")
        rows = sv.audit_recent(50)
        self.assertGreaterEqual(len(rows), 2)
        text = json.dumps(rows)
        self.assertNotIn("signature", text)
        self.assertNotIn("secret", text.lower())
        for row in rows:
            self.assertIn("event", row)
            self.assertIn("ts", row)
            for value in row.values():
                if isinstance(value, str):
                    self.assertLessEqual(len(value), 320)

    def test_public_key_width(self):
        sv = self._svc(signing=self.priv)
        self.assertEqual(len(sv.public_key()), 32)


# --------------------------------------------------------------------------- #
# HTTP routes + auth isolation
# --------------------------------------------------------------------------- #

def _make_client(signing=None, *, supervisor_enabled=True):
    tmp = Path(tempfile.mkdtemp(prefix="fleet-http-"))
    old = (store.STATE_DIR, events.EVENT_LOG, task_store.DB_PATH)
    store.STATE_DIR = tmp
    events.EVENT_LOG = tmp / "events.jsonl"
    task_store.DB_PATH = tmp / "fleet.db"
    task_store.init_db()
    store.save_snapshot("mac-local", {
        "machine": "mac-local", "source": "ingest", "reachable": True,
        "agents": {}, "system": {},
    })
    cfg = FleetConfig.from_root(
        tmp,
        ingest_token="test-only",
        dev_operator="op@example.com",
        runner_credentials={"mac-local": "runner-secret"},
        project_whitelist={"mac-local": ["agent-fleet"]},
        supervisor_enabled=supervisor_enabled,
        supervisor_credentials={"mac-local": "sup-secret"},
        supervisor_signing_raw=signing,
    )
    app = create_app(cfg)
    app._fleet_test_cleanup = old
    return app.test_client(), app


def _sup_headers(machine="mac-local", secret="sup-secret"):
    return {"X-Supervisor-Credential": f"{machine}:{secret}"}


class SupervisorRoutesTests(unittest.TestCase):
    def setUp(self):
        self._apps = []
        self.priv, self.pub = _keypair()

    def tearDown(self):
        for app in self._apps:
            store.STATE_DIR, events.EVENT_LOG, task_store.DB_PATH = \
                app._fleet_test_cleanup

    def _client(self, **kwargs):
        client, app = _make_client(self.priv, **kwargs)
        self._apps.append(app)
        return client

    def _service(self):
        return self._apps[-1].extensions["fleet"]["services"]["supervisor"]

    def test_missing_supervisor_header_forbidden(self):
        client = self._client()
        resp = client.post("/api/supervisor/poll", json={})
        self.assertEqual(resp.status_code, 403)
        body = resp.get_json()
        self.assertFalse(body["ok"])
        self.assertIn("request_id", body)

    def test_invalid_supervisor_credential_forbidden(self):
        client = self._client()
        resp = client.post("/api/supervisor/poll", json={},
                           headers={"X-Supervisor-Credential": "mac-local:bad"})
        self.assertEqual(resp.status_code, 403)

    def test_poll_valid_returns_signed_command(self):
        client = self._client()
        sv = self._service()
        cmd = sv.enqueue("mac-local", "sess_p", None, "pause_session", "op")
        resp = client.post("/api/supervisor/poll", json={},
                           headers=_sup_headers())
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual([c["command_id"] for c in body["commands"]],
                         [cmd["command_id"]])

    def test_foreign_runner_credential_cannot_poll_supervisor(self):
        client = self._client()
        resp = client.post("/api/supervisor/poll", json={},
                           headers={"X-Runner-Credential":
                                    "mac-local:runner-secret"})
        self.assertEqual(resp.status_code, 403)

    def test_ingest_token_cannot_poll_supervisor(self):
        client = self._client()
        resp = client.post("/api/supervisor/poll", json={},
                           headers={"X-Agent-Fleet-Token": "test-only"})
        self.assertEqual(resp.status_code, 403)

    def test_operator_header_cannot_poll_supervisor(self):
        client = self._client()
        resp = client.post("/api/supervisor/poll", json={},
                           headers={"Cf-Access-Authenticated-User-Email":
                                    "o@example.com"})
        self.assertEqual(resp.status_code, 403)

    def test_supervisor_credential_cannot_poll_commands(self):
        client = self._client()
        resp = client.post("/api/commands/poll", json={},
                           headers=_sup_headers())
        self.assertEqual(resp.status_code, 403)

    def test_supervisor_credential_cannot_access_tasks(self):
        client = self._client()
        resp = client.post("/api/tasks", json={},
                           headers=_sup_headers())
        self.assertEqual(resp.status_code, 401)
        # no DEV_OPERATOR fallback either when only a supervisor header is carried
        self.assertIsNone(client.application.extensions["fleet"].get("op"))

    def test_receipts_unknown_command_bounded(self):
        client = self._client()
        resp = client.post("/api/supervisor/receipts", json={
            "command_id": "cmd_nope", "status": "succeeded", "reason": "ok",
        }, headers=_sup_headers())
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "command_not_found")
        self.assertIn("request_id", body)
        text = json.dumps(body)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("secret", text.lower())

    def test_receipts_valid(self):
        client = self._client()
        sv = self._service()
        cmd = sv.enqueue("mac-local", "sess_rcpt", "att_rcpt",
                         "pause_session", "x")
        resp = client.post("/api/supervisor/receipts", json={
            "command_id": cmd["command_id"], "status": "succeeded",
            "reason": "paused",
        }, headers=_sup_headers())
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["receipt"]["status"], "succeeded")
        self.assertEqual(sv.command_status(cmd["command_id"]), "succeeded")

    def test_missing_signing_key_disables_issuance(self):
        client, app = _make_client(signing=None)
        self._apps.append(app)
        resp = client.post("/api/supervisor/poll", json={},
                           headers=_sup_headers())
        self.assertEqual(resp.status_code, 503)
        body = resp.get_json()
        self.assertEqual(body["error"], "supervisor_disabled")
        text = json.dumps(body)
        self.assertNotIn("Traceback", text)

    def test_no_v1_supervisor_surface(self):
        client = self._client()
        rules = {r.rule for r in client.application.url_map.iter_rules()}
        self.assertNotIn("/api/v1/supervisor/poll", rules)
        self.assertNotIn("/api/v1/supervisor/receipts", rules)

    def test_not_enabled_has_no_supervisor_route(self):
        client, app = _make_client(self.priv, supervisor_enabled=False)
        self._apps.append(app)
        rules = {r.rule for r in client.application.url_map.iter_rules()}
        self.assertNotIn("/api/supervisor/poll", rules)
        self.assertNotIn("/api/supervisor/receipts", rules)
        resp = client.post("/api/supervisor/poll", json={},
                           headers=_sup_headers())
        # A disabled supervisor exposes no endpoint: the request hits the
        # catch-all page rule, so the bounded API error is either 404 or 405.
        self.assertIn(resp.status_code, (404, 405))
        body = resp.get_json()
        self.assertFalse(body["ok"])


# --------------------------------------------------------------------------- #
# Agent control client — local validation + execution + bounded receipts
# --------------------------------------------------------------------------- #

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


class _FakeOps:
    def __init__(self):
        self.alive = True
        self._platform = "test"

    def capability(self):
        return ("process_group_only", "1")

    def attach(self, pid, started_at, exe_path):
        return _FakeAttachedHandle(pid, started_at, exe_path)

    def create(self, argv, cwd, env):
        return self

    def proc_poll(self, handle):
        return None

    def proc_wait(self, handle, timeout=None):
        return 0

    def group_id(self, handle):
        return "grp_1"

    def group_alive(self, handle):
        return self.alive

    def group_terminate(self, handle, sig):
        return True

    def group_kill(self, handle, sig):
        return True

    def group_reap(self, handle, timeout=None):
        return True

    def escaped(self, handle):
        return False

    def close(self, handle):
        pass

    def pump(self, handle, timeout, on_line=None, should_abort=None):
        return "eof"


class AgentControlClientTestsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fleet-agent-"))
        from tools.supervisor.supervisor import Supervisor
        self.sup = Supervisor(
            manifest_dir=str(self.tmp / "sup"), machine_id="agent-a",
            ops=_FakeOps())
        self.session_id = _opaque("sess")
        self.sup.launch(agent="codex", command=["/bin/true"], cwd="/tmp",
                        env_allowlist={}, session_id=self.session_id,
                        attempt_id=None)
        self.priv, self.pub = _keypair()

    def _client(self, transport=None, nonce_dir=None, bridge_factory=None):
        from tools.supervisor.control_client import ControlClient, NonceStore
        return ControlClient(
            hub_url="https://agent.test",
            machine_id="agent-a",
            credential="secret-secret",
            hub_public_key=self.pub,
            public_supervisor=self.sup,
            transport=transport or _Pipe(),
            nonce_store=NonceStore(path=nonce_dir or self.tmp / "used"),
            bridge_factory=bridge_factory,
        )

    def _cmd(self, *, action="pause_session", nonce="u", session=None,
             machine="agent-a", now=None, expires_delta=600.0,
             attempt=None):
        body = _command_payload(
            machine=machine,
            session=session if session is not None else self.session_id,
            attempt=attempt, action=action, nonce=nonce,
            command_id=_opaque("cmd"), issued_s=now, expires_delta=expires_delta)
        return _sign(body, self.priv)

    def test_poll_empty(self):
        client = self._client()
        result = client.poll_once()
        self.assertTrue(result["ok"])
        self.assertEqual(result["commands"], 0)

    def test_pause_executes_through_supervisor(self):
        cmd = self._cmd(action="pause_session", nonce="u1")
        pipe = _Pipe([cmd])
        client = self._client(pipe)
        result = client.poll_once()
        self.assertTrue(result["ok"])
        self.assertEqual(pipe.uploads[0]["status"], "succeeded")
        self.assertEqual(self.sup.get(self.session_id).state, "paused")

    def test_terminate_executes_through_supervisor(self):
        cmd = self._cmd(action="terminate_session", nonce="u2")
        pipe = _Pipe([cmd])
        self._client(pipe).poll_once()
        self.assertEqual(pipe.uploads[0]["status"], "succeeded")
        self.assertEqual(self.sup.get(self.session_id).state, "terminated")

    def test_cancel_attempt_executes_through_supervisor(self):
        cmd = self._cmd(action="cancel_attempt", nonce="u3")
        pipe = _Pipe([cmd])
        self._client(pipe).poll_once()
        self.assertEqual(pipe.uploads[0]["status"], "succeeded")
        self.assertEqual(self.sup.get(self.session_id).state, "terminated")

    def test_unsupported_action_rejects_bounded(self):
        cmd = self._cmd(action="exec_shell", nonce="u4")
        pipe = _Pipe([cmd])
        self._client(pipe).poll_once()
        self.assertEqual(pipe.uploads[0]["status"], "rejected")
        self.assertEqual(pipe.uploads[0]["reason"], "unsupported_action")
        self.assertNotIn("exec_shell", json.dumps(pipe.uploads[0]))

    def test_forged_signature_rejects(self):
        # sign a command then corrupt the signature (wrong key bytes)
        cmd = self._cmd(action="pause_session", nonce="u5")
        wrong_priv, _ = _keypair()
        body = dict(cmd)
        sig = body.pop("signature")
        _ = sig  # original hash discarded; replaced below
        body["signature"] = ctrl.sign_payload(
            wrong_priv, ctrl.canonical_json_bytes(body))
        pipe = _Pipe([body])
        self._client(pipe).poll_once()
        self.assertEqual(pipe.uploads[0]["status"], "rejected")
        self.assertEqual(pipe.uploads[0]["reason"], "invalid_signature")

    def test_machine_mismatch_rejects(self):
        cmd = self._cmd(machine="other-host", action="pause_session",
                        nonce="u6")
        pipe = _Pipe([cmd])
        self._client(pipe).poll_once()
        self.assertEqual(pipe.uploads[0]["status"], "rejected")
        self.assertEqual(pipe.uploads[0]["reason"], "scope_mismatch")

    def test_expired_command_rejects(self):
        # issued within the skew window (not clock_skew) but already expired:
        cmd = self._cmd(nonce="u8", now=_now_s() - 100, expires_delta=-300)
        pipe = _Pipe([cmd])
        self._client(pipe).poll_once()
        self.assertEqual(pipe.uploads[0]["status"], "rejected")
        self.assertEqual(pipe.uploads[0]["reason"], "expired")

    def test_nonce_replay_rejected(self):
        cmd = self._cmd(nonce="u9")
        pipe1 = _Pipe([cmd])
        self._client(pipe1).poll_once()
        pipe2 = _Pipe([cmd])
        self._client(pipe2).poll_once()
        self.assertEqual(pipe2.uploads[0]["status"], "rejected")
        self.assertEqual(pipe2.uploads[0]["reason"], "nonce_replay")

    def test_nonce_persists_across_restart(self):
        nonce_dir = self.tmp / "used" / "restart"
        cmd = self._cmd(nonce="ur1")
        pipe1 = _Pipe([cmd])
        self._client(pipe1, nonce_dir=nonce_dir).poll_once()
        pipe2 = _Pipe([cmd])
        self._client(pipe2, nonce_dir=nonce_dir).poll_once()
        self.assertEqual(pipe2.uploads[0]["status"], "rejected")

    def test_unknown_session_rejected(self):
        cmd = self._cmd(session="sess_dead", action="pause",
                        nonce="u10")
        pipe = _Pipe([cmd])
        self._client(pipe).poll_once()
        self.assertEqual(pipe.uploads[0]["status"], "rejected")
        self.assertEqual(pipe.uploads[0]["reason"], "unknown_session")

    def test_adopt_with_malformed_candidate_is_rejected_bounded(self):
        # ``adopt`` is session-pending: the adopt_<session> does not exist yet
        # in the local supervisor manifest (that is the whole point), so the
        # session-existence pre-check is BYPASSED for ``adopt`` alone.  But a
        # signed adopt envelope whose ``candidate`` is missing/malformed is
        # rejected BEFORE any attach or bridge with the bounded
        # ``invalid_candidate``: no file read, no supervisor mutation,
        # no signal.
        cmd = self._cmd(action="adopt", session="adopt_deadbeef", nonce="uda1")
        pipe = _Pipe([cmd])
        self._client(pipe).poll_once()
        self.assertEqual(pipe.uploads[0]["status"], "rejected")
        self.assertEqual(pipe.uploads[0]["reason"], "invalid_candidate")
        self.assertNotIn("Traceback", json.dumps(pipe.uploads[0]))

    def test_adopt_missing_session_receipt_is_bounded(self):
        cmd = self._cmd(action="adopt", session="adopt_deadbeef", nonce="uda2")
        pipe = _Pipe([cmd])
        self._client(pipe).poll_once()
        receipt = pipe.uploads[0]
        self.assertEqual(receipt["status"], "rejected")
        self.assertEqual(receipt["reason"], "invalid_candidate")
        self.assertEqual(
            set(receipt),
            {"command_id", "machine_id", "status", "reason", "received_at"},
        )
        self.assertNotIn("signature", json.dumps(receipt))

    def test_adopt_success_starts_bridge_and_receipts_accepted(self):
        # A fully signed adopt envelope carrying a valid candidate binds the
        # real Supervisor (injected identity probe re-read matches) and starts
        # the managed bridge through the injected recording factory.  The
        # receipt is the bounded ``accepted``/``adopted`` pair and the factory
        # saw ``managed=True``.
        attached_sid = _opaque("adopt")
        pid = 9876
        started = "2026-08-30T12:00:00Z"
        exe = "/opt/agent/bin/run"
        self.sup._default_attach_probe = (
            lambda p: (started, exe) if int(p) == pid else None)

        class _RecBridge:
            def __init__(self, cfg):
                self.cfg = cfg
                self.started = False
                self.closed = False

            def start(self):
                self.started = True

            def ingest_native(self, path, checkpoint_path=None):
                pass

            def flush(self):
                pass

            def close(self):
                self.closed = True

        class _RecFactory:
            def __init__(self):
                self.last = None

            def __call__(self, cfg):
                bridge = _RecBridge(cfg)
                self.last = bridge
                return bridge

        factory = _RecFactory()
        body = _command_payload(
            action="adopt", session=attached_sid, nonce="ufa1",
            machine="agent-a")
        body["candidate"] = {
            "pid": pid, "started_at": started, "exe_path": exe,
            "agent_family": "codex", "native_file_path": None,
        }
        cmd = _sign(body, self.priv)
        pipe = _Pipe([cmd])
        client = self._client(pipe, bridge_factory=factory)
        result = client.poll_once()
        self.assertTrue(result["ok"])
        receipt = pipe.uploads[0]
        self.assertEqual(
            set(receipt),
            {"command_id", "machine_id", "status", "reason", "received_at"},
        )
        self.assertEqual(receipt["status"], "accepted")
        self.assertEqual(receipt["reason"], "adopted")
        self.assertIsNotNone(factory.last)
        self.assertTrue(factory.last.started)
        self.assertIs(factory.last.cfg["managed"], True)
        self.assertEqual(factory.last.cfg["session_id"], attached_sid)
        # no raw pid leaks into the receipt
        self.assertNotIn(str(pid), json.dumps(receipt))
        # the entry remains bound in the supervisor
        self.assertIn(attached_sid, self.sup.all_status())

    def test_detach_missing_session_still_unknown_session(self):
        # the bypass must NOT widen: detach (like every other action) still
        # passes the session-existence pre-check, so a missing session yields
        # ``unknown_session`` — never a silent file read or a signal.
        cmd = self._cmd(action="detach", session="adopt_deadbeef", nonce="udt1")
        pipe = _Pipe([cmd])
        self._client(pipe).poll_once()
        self.assertEqual(pipe.uploads[0]["status"], "rejected")
        self.assertEqual(pipe.uploads[0]["reason"], "unknown_session")

    def test_detach_signed_command_succeeds_and_receipts_detached(self):
        # A real attached (纳管) session answering a signed detach: the entry is
        # dropped, the receipt is ``succeeded``/``detached`` and NO signal is
        # ever sent to the attached handle.
        attached_sid = _opaque("adopt")
        probe = lambda pid: ("2026-08-30T10:00:00Z", "/opt/agent/bin/run")
        rc = self.sup.attach_to_existing(
            4321, "2026-08-30T10:00:00Z", "/opt/agent/bin/run",
            None, "codex", session_id=attached_sid, probe=probe)
        self.assertEqual(rc, "adopted")
        handle = self.sup._handle_of(attached_sid)
        cmd = self._cmd(action="detach", session=attached_sid, nonce="udw2")
        pipe = _Pipe([cmd])
        result = self._client(pipe).poll_once()
        self.assertTrue(result["ok"])
        receipt = pipe.uploads[0]
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["reason"], "detached")
        # entry no longer bound
        self.assertNotIn(attached_sid, self.sup.all_status())
        # nothing touched the attached handle's signal paths
        self.assertEqual(handle.signals, [])
        self.assertEqual(handle.group_signals, [])

    def test_normal_action_missing_session_still_unknown_session(self):
        cmd = self._cmd(action="pause_session", session="adopt_deadbeef",
                        nonce="u15")
        pipe = _Pipe([cmd])
        self._client(pipe).poll_once()
        self.assertEqual(pipe.uploads[0]["status"], "rejected")
        self.assertEqual(pipe.uploads[0]["reason"], "unknown_session")

    def test_forged_attempt_id_rejected_no_execution(self):
        # Session is bound to attempt_id=None (setUp launch with attempt_id=None).
        # A crafted terminate carrying attempt_id="att_WRONG" must be REJECTED
        # as attempt_mismatch — the session must NOT be terminated.
        cmd = self._cmd(action="terminate_session", nonce="att1",
                        attempt="att_WRONG")
        pipe = _Pipe([cmd])
        self._client(pipe).poll_once()
        self.assertEqual(pipe.uploads[0]["status"], "rejected")
        self.assertEqual(pipe.uploads[0]["reason"], "attempt_mismatch")
        self.assertEqual(self.sup.get(self.session_id).state, "running")

    def test_forged_attempt_cancel_rejected_not_terminated(self):
        cmd = self._cmd(action="cancel_attempt", nonce="att2",
                         attempt="att_WRONG")
        pipe = _Pipe([cmd])
        self._client(pipe).poll_once()
        self.assertEqual(pipe.uploads[0]["status"], "rejected")
        self.assertEqual(pipe.uploads[0]["reason"], "attempt_mismatch")
        self.assertEqual(self.sup.get(self.session_id).state, "running")

    def test_attempt_none_command_matches_none_bound_session(self):
        # A command targeting attempt=None matches a session bound to None.
        cmd = self._cmd(action="pause_session", nonce="att3", attempt=None)
        pipe = _Pipe([cmd])
        self._client(pipe).poll_once()
        self.assertEqual(pipe.uploads[0]["status"], "succeeded")
        self.assertEqual(self.sup.get(self.session_id).state, "paused")

    def test_attempt_binding_mismatch_rejected(self):
        # Launch a new session bound to a real attempt_id, then target a DIFFERENT
        # attempt_id → must reject attempt_mismatch without touching the session.
        att = _opaque("att")
        sess2 = _opaque("sess")
        self.sup.launch(agent="codex", command=["/bin/true"], cwd="/tmp",
                        env_allowlist={}, session_id=sess2, attempt_id=att)
        cmd = self._cmd(action="pause_session", nonce="att4", session=sess2,
                         attempt="att_other")
        pipe = _Pipe([cmd])
        self._client(pipe).poll_once()
        self.assertEqual(pipe.uploads[0]["status"], "rejected")
        self.assertEqual(pipe.uploads[0]["reason"], "attempt_mismatch")
        self.assertEqual(self.sup.get(sess2).state, "running")

    def test_attempt_matching_binding_succeeds(self):
        att = _opaque("att")
        sess3 = _opaque("sess")
        self.sup.launch(agent="agent-b", command=["/bin/true"], cwd="/tmp",
                        env_allowlist={}, session_id=sess3, attempt_id=att)
        cmd = self._cmd(action="pause_session", nonce="att5", session=sess3,
                         attempt=att)
        pipe = _Pipe([cmd])
        self._client(pipe).poll_once()
        self.assertEqual(pipe.uploads[0]["status"], "succeeded")
        self.assertEqual(self.sup.get(sess3).state, "paused")

    def test_stale_resume_rejected_after_quarantine(self):
        self.sup.quarantine_session(self.session_id)
        cmd = self._cmd(action="resume_session", nonce="u11")
        pipe = _Pipe([cmd])
        self._client(pipe).poll_once()
        self.assertEqual(pipe.uploads[0]["status"], "rejected")
        self.assertEqual(pipe.uploads[0]["reason"], "scope_mismatch")
        self.assertEqual(self.sup.get(self.session_id).state, "quarantined")

    def test_stale_resume_rejected_after_terminate(self):
        self.sup.terminate_session(self.session_id, grace_s=0.0)
        cmd = self._cmd(action="resume_session", nonce="u13")
        pipe = _Pipe([cmd])
        self._client(pipe).poll_once()
        self.assertEqual(pipe.uploads[0]["status"], "rejected")
        self.assertEqual(pipe.uploads[0]["reason"], "scope_mismatch")
        self.assertEqual(self.sup.get(self.session_id).state, "terminated")

    def test_bounded_receipt_fields_and_no_raw(self):
        cmd = self._cmd(action="pause_session", nonce="u14")
        pipe = _Pipe([cmd])
        self._client(pipe).poll_once()
        receipt = pipe.uploads[0]
        self.assertEqual(
            set(receipt),
            {"command_id", "machine_id", "status", "reason", "received_at"},
        )
        text = json.dumps(receipt)
        self.assertNotIn("signature", text)
        self.assertNotIn("Traceback", text)

    def test_poll_network_failure_not_raised(self):
        pipe = _Pipe()
        pipe.network_error = True
        client = self._client(pipe)
        result = client.poll_once()
        self.assertFalse(result["ok"])


class SupervisorIdentityDomainTests(unittest.TestCase):
    def test_parse_credential(self):
        self.assertEqual(sup_dom.parse_credential("m1:secret1"),
                         ("m1", "secret1"))

    def test_malformed_credential_rejected(self):
        for bad in ("no-separator", "a:b:c", "", ":only-secret",
                    "bad machine:secret"):
            self.assertIsNone(sup_dom.parse_credential(bad))

    def test_constants(self):
        self.assertEqual(sup_dom.SUPERVISOR_CREDENTIAL_HEADER,
                         "X-Supervisor-Credential")
        self.assertEqual(sup_dom.SUPERVISOR_CREDENTIAL_ENV,
                         "AGENT_FLEET_SUPERVISOR_CREDENTIALS")
        self.assertEqual(sup_dom.SUPERVISOR_SIGNING_KEY_ENV,
                         "AGENT_FLEET_SUPERVISOR_SIGNING_KEY")


if __name__ == "__main__":
    unittest.main()