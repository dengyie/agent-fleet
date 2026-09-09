"""hub/application/supervisor_service.py — supervisor poll/receipt use cases.

``SupervisorService`` is the Hub-side authority over the pull-mediated control
queue (spec §13).  It:

* enqueues signed, TTL-scoped control commands (fixed actions only) for one
  machine/session/attempt — *never* raw/arbitrary commands (issuance is
  disabled when no signing key is configured — fail closed);
* serves ``poll`` to a machine and marks commands ``queued -> delivered``;
  delivered commands are never redelivered; stale commands are expired;
* accepts ``receipt`` uploads idempotently (a duplicate command or return
  receipt replays the original result) and transitions the queue row;
* enforces conflict precedence ``terminate_session`` / ``quarantine_session``
  OVER ``resume_session`` / ``append_user_turn`` (a stale resume or follow-up
  can never undo a quarantine);
* writes bounded audit rows for enqueue / delivery / receipt / expiry /
  supersede / reject, and never records a raw signature, token, credential,
  path or full command body.

Queue state != execution: ``queued`` never implies the action ran.  Only an
Agent receipt says that.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from hub.domain.control import (
    CONTROL_ACTIONS,
    PAYLOAD_ACTIONS,
    RECEIPT_STATUSES,
    canonical_json_bytes,
    new_command_id,
    sign_payload,
    validated_control_payload,
)

logger = logging.getLogger(__name__)

#: A queue row is terminal (no further mutations) when it holds one of these.
_TERMINAL = frozenset({
    "succeeded", "already_finished", "failed", "rejected", "expired",
})

#: Actions that pre-empt a lower-precedence queued resume / follow-up.
_HIGH_PRECEDENCE = frozenset({"terminate_session", "quarantine_session"})
_LOW_PRECEDENCE = frozenset({"resume_session", "append_user_turn"})


def _now_s() -> float:
    from time import time as _t
    return _t()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat().replace(
        "+00:00", "Z")


def _bound_row(row: dict) -> dict:
    """Drop secret/path/signature fields and bound every string in an audit row."""
    out: dict[str, Any] = {}
    for key, value in row.items():
        if key in ("signature", "secret", "token", "credential", "raw",
                   "path", "payload"):
            continue
        if isinstance(value, str):
            value = value[:160]
        out[key] = value
    return out


#: Bounded length caps for a signed-envelope adoption candidate.  The source
#: Adoption record already bounds these fields; this is the defensive gate the
#: service layer applies before anything is embedded.
_CANDIDATE_SHORT_MAX = 128
_CANDIDATE_PATH_MAX = 4096


def _validated_candidate(candidate: Any) -> dict:
    """Validate an adoption ``candidate`` and return a plain bounded copy.

    Any violation raises ``SupervisorServiceError("invalid_candidate", "", 400)``
    — bounded, no raw text.  The candidate is embedded into the signed
    envelope (and covered by the signature) but never reaches the bounded
    audit rows.
    """
    if not isinstance(candidate, Mapping):
        raise SupervisorServiceError("invalid_candidate", "", 400)
    pid = candidate.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise SupervisorServiceError("invalid_candidate", "", 400)
    for key, cap in (("started_at", _CANDIDATE_SHORT_MAX),
                     ("agent_family", _CANDIDATE_SHORT_MAX),
                     ("exe_path", _CANDIDATE_PATH_MAX)):
        value = candidate.get(key)
        if (not isinstance(value, str) or not value or len(value) > cap):
            raise SupervisorServiceError("invalid_candidate", "", 400)
    native = candidate.get("native_file_path")
    if native is not None and (
            not isinstance(native, str) or len(native) > _CANDIDATE_PATH_MAX):
        raise SupervisorServiceError("invalid_candidate", "", 400)
    return {
        "pid": int(pid),
        "started_at": str(candidate["started_at"]),
        "exe_path": str(candidate["exe_path"]),
        "agent_family": str(candidate["agent_family"]),
        "native_file_path": native if isinstance(native, str) else None,
    }


class SupervisorServiceError(RuntimeError):
    """Bounded supervisor-service error (stable code/detail/status)."""

    def __init__(self, code: str, detail: str = "", status: int = 400) -> None:
        self.code = code
        self.detail = str(detail)[:200]
        self.status = status
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


class AuditStore:
    """Bounded in-memory audit store (rows capped; durable swap surface)."""

    _CAP = 5000

    def __init__(self):
        self._rows: list[dict] = []

    def emit(self, row: dict) -> None:
        bounded = _bound_row(dict(row))
        if not bounded.get("event"):
            return
        self._rows.append(bounded)
        if len(self._rows) > self._CAP:
            self._rows = self._rows[-1000:]

    def read(self, limit: int = 200) -> list[dict]:
        return list(self._rows[-limit:])


class SupervisorCommand:
    """In-memory queue row for one signed control command."""

    __slots__ = ("command_id", "machine", "session_id", "attempt_id",
                 "action", "nonce", "wire", "issued_s", "expires_s",
                 "status", "receipt_status", "receipt_reason")

    def __init__(self, *, command_id, machine, session_id, attempt_id, action,
                 nonce, wire, issued_s, expires_s):
        self.command_id = command_id
        self.machine = machine
        self.session_id = session_id
        self.attempt_id = attempt_id
        self.action = action
        self.nonce = nonce
        self.wire = wire
        self.issued_s = issued_s
        self.expires_s = expires_s
        self.status = "queued"
        self.receipt_status = ""
        self.receipt_reason = ""

    def as_envelope(self) -> dict:
        return dict(self.wire)


class SupervisorService:
    """Hub-side control queue authority (in-memory queue + bounded audit)."""

    def __init__(self, *, signing_key: bytes | None, audit_store=None,
                 now_fn: Callable[[], float] | None = None,
                 ttl_s: float = 3600.0, max_clock_skew_s: float = 300.0,
                 append_user_turn_enabled: bool = False,
                 apply_local_profile_enabled: bool = False):
        self._signing_key = signing_key
        self._audit = audit_store if audit_store is not None else AuditStore()
        self._now_fn = now_fn or _now_s
        self._ttl_s = float(ttl_s)
        self._max_clock_skew_s = float(max_clock_skew_s)
        self._append_user_turn_enabled = bool(append_user_turn_enabled)
        self._apply_local_profile_enabled = bool(apply_local_profile_enabled)
        self._commands: dict[str, SupervisorCommand] = {}
        self._nonces: dict[str, str] = {}
        self._target_cmds: dict[str, str] = {}

    # ------------------------------------------------------------------
    # key gate
    # ------------------------------------------------------------------

    def assert_keyed(self) -> None:
        """Fail-closed gate: NO signing key means NO command issuance."""
        if not self._signing_key:
            raise SupervisorServiceError(
                "supervisor_disabled", "supervisor 命令签发未启用", 503)

    def public_key(self) -> bytes | None:
        """32-byte public half of the configured signing key (or None)."""
        if not self._signing_key:
            return None
        from cryptography.hazmat.primitives import serialization as _ser
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
        try:
            priv = Ed25519PrivateKey.from_private_bytes(self._signing_key)
            return priv.public_key().public_bytes(
                _ser.Encoding.Raw, _ser.PublicFormat.Raw)
        except (ValueError, TypeError):
            return None

    # ------------------------------------------------------------------
    # enqueue
    # ------------------------------------------------------------------

    def enqueue(self, machine: str, session_id: str, attempt_id: str | None,
                action: str, reason_code: str, *, nonce: str | None = None,
                candidate: Mapping | None = None,
                payload: Mapping | None = None) -> dict:
        """Create a signed control command for one machine/session/attempt.

        Returns the full signed envelope (with ``signature``).  Raises
        ``unsupported_action`` for an action outside the fixed set,
        ``duplicate_nonce`` for a reused nonce, and ``resume_superseded`` when
        a higher precedence action already owns the target.

        ``candidate`` is the optional bounded adoption identity (pid /
        started_at / exe_path / agent_family / native_file_path) that rides
        INSIDE the signed envelope only for ``adopt``.  It is embedded BEFORE
        signing so the Ed25519 signature covers it, and it never reaches the
        bounded audit (``_note`` rows carry ids only).  An invalid candidate
        raises ``invalid_candidate`` 400.

        ``payload`` is the optional bounded Phase 4/5 body.  ``append_user_turn``
        may carry only ``text``; ``apply_local_profile`` may carry only a local
        ``profile_id``.  Extra keys (api_key / argv / pid / signal) are refused.
        Both actions are default-off: issuance without the matching gate is
        ``feature_disabled``.  They are never ``inject_stdin`` and never push
        credentials.
        """
        self.assert_keyed()
        if action not in CONTROL_ACTIONS:
            raise SupervisorServiceError("unsupported_action",
                                         "不支持的控制动作")
        if action == "append_user_turn" and not self._append_user_turn_enabled:
            raise SupervisorServiceError("feature_disabled",
                                         "append_user_turn 未启用", 403)
        if action == "apply_local_profile" and not self._apply_local_profile_enabled:
            raise SupervisorServiceError("feature_disabled",
                                         "apply_local_profile 未启用", 403)
        bounded_payload = None
        if action in PAYLOAD_ACTIONS:
            try:
                bounded_payload = validated_control_payload(action, payload)
            except ValueError:
                raise SupervisorServiceError("invalid_payload", "", 400)
        elif payload is not None:
            raise SupervisorServiceError("invalid_payload", "", 400)
        action_nonce = nonce or secrets.token_hex(16)
        if action_nonce in self._nonces:
            raise SupervisorServiceError("duplicate_nonce", "")

        now = self._now_fn()
        expires_at_s = now + self._ttl_s
        body = {
            "command_id": new_command_id(),
            "action": action,
            "target": {
                "machine_id": machine,
                "session_id": session_id,
                "attempt_id": attempt_id if attempt_id else None,
            },
            "issued_at": _iso(now),
            "expires_at": _iso(expires_at_s),
            "nonce": action_nonce,
            "reason_code": str(reason_code)[:32] or "operator_requested",
        }
        if candidate is not None:
            body["candidate"] = _validated_candidate(candidate)
        if bounded_payload is not None:
            body["payload"] = bounded_payload
        body_bytes = canonical_json_bytes(
            {k: v for k, v in body.items() if k != "signature"})
        body["signature"] = sign_payload(self._signing_key, body_bytes)

        row = SupervisorCommand(
            command_id=body["command_id"],
            machine=machine,
            session_id=session_id,
            attempt_id=attempt_id,
            action=action,
            nonce=action_nonce,
            wire=body,
            issued_s=now,
            expires_s=expires_at_s,
        )
        # conflict-precedence gate BEFORE the row owns the target: a stale
        # higher-precedence command (terminate/quarantine) for the same target
        # rejects a later resume / follow-up outright.
        target_key = _bounded_target_key(session_id, attempt_id)
        cid = self._target_cmds.get(target_key)
        existing = self._commands.get(cid) if cid else None
        if existing and existing.status == "queued":
            if (existing.action in _HIGH_PRECEDENCE
                    and action in _LOW_PRECEDENCE):
                raise SupervisorServiceError(
                    "resume_superseded",
                    "目标已有终止/隔离命令，resume 被更高优先级命令取代", 409)

        previous_owner = self._target_cmds.get(target_key)
        self._commands[row.command_id] = row
        self._nonces[action_nonce] = row.command_id
        self._target_cmds[target_key] = row.command_id

        # terminate/quarantine supersedes a queued lower-precedence resume or
        # follow-up for the same target so the stale command is never delivered.
        if action in _HIGH_PRECEDENCE:
            self._supersede_queued_resume(
                target_key, action, previous_owner=previous_owner)

        self._note({"event": "enqueue", "ts": _iso(now), "machine": machine,
                    "session_id": session_id, "action": action})
        return row.as_envelope()

    def _supersede_queued_resume(self, target_key: str, by_action: str,
                                 *, previous_owner: str | None = None) -> None:
        cid = previous_owner or self._target_cmds.get(target_key)
        if not cid:
            return
        row = self._commands.get(cid)
        if row is None or row.status != "queued":
            return
        if row.action in _LOW_PRECEDENCE:
            row.status = "expired"
            row.receipt_status = "expired"
            row.receipt_reason = "superseded"
            self._note({"event": "supersede", "ts": _iso(self._now_fn()),
                        "command_id": row.command_id,
                        "action": row.action, "by": by_action})

    # ------------------------------------------------------------------
    # poll
    # ------------------------------------------------------------------

    def poll(self, machine: str, *, max_commands: int = 20) -> list[dict]:
        """Hand out queued signed envelopes for ``machine`` (each once)."""
        self.assert_keyed()
        self._expire_stale()
        out: list[dict] = []
        for cid in sorted(self._commands):
            row = self._commands[cid]
            if row.machine != machine or row.status != "queued":
                continue
            out.append(row.as_envelope())
            row.status = "delivered"
            self._note({"event": "deliver", "ts": _iso(self._now_fn()),
                        "command_id": row.command_id, "machine": row.machine,
                        "action": row.action})
            if len(out) >= max_commands:
                break
        return out

    def _expire_stale(self) -> None:
        now = self._now_fn()
        for row in list(self._commands.values()):
            if row.status in _TERMINAL:
                continue
            if row.expires_s < now:
                row.status = "expired"
                row.receipt_status = "expired"
                row.receipt_reason = "expired"
                self._note({"event": "expire", "ts": _iso(now),
                            "command_id": row.command_id})

    # ------------------------------------------------------------------
    # receipts
    # ------------------------------------------------------------------

    def receipt(self, command_id: str, status: str, reason: str = "",
                *, machine: str | None = None) -> dict:
        """Record an Agent receipt; duplicate receipts replay the original."""
        self.assert_keyed()
        row = self._commands.get(command_id)
        if row is None:
            raise SupervisorServiceError("command_not_found", "")
        if machine and row.machine != machine:
            raise SupervisorServiceError("machine_mismatch", "", 403)
        if status not in RECEIPT_STATUSES:
            raise SupervisorServiceError("invalid_receipt_status", "", 400)

        if row.status in _TERMINAL:
            # Already terminal: deterministically replay the ORIGINAL persisted
            # sender receipt (status/reason), never accept a second receipt that
            # would overwrite it.  The source of truth is receipt_status /
            # receipt_reason — once recorded it is immutable.
            return self._receipt_view(row, first=False)
        row.status = status
        row.receipt_status = status
        row.receipt_reason = str(reason)[:120]
        self._note({"event": f"receipt:{status}", "ts": _iso(self._now_fn()),
                    "command_id": row.command_id, "machine": row.machine,
                    "status": status})
        return self._receipt_view(row, first=True)

    def _receipt_view(self, row: SupervisorCommand, *, first: bool) -> dict:
        return {
            "command_id": row.command_id,
            "status": row.receipt_status or row.status,
            "reason": row.receipt_reason or "",
            "first": bool(first),
        }

    def command_status(self, command_id: str) -> str | None:
        row = self._commands.get(command_id)
        return row.status if row else None

    # ------------------------------------------------------------------
    # audit helpers
    # ------------------------------------------------------------------

    def audit_recent(self, limit: int = 200) -> list[dict]:
        try:
            return self._audit.read(limit=limit)
        except Exception:
            return []

    def _note(self, row: dict) -> None:
        try:
            self._audit.emit(row)
        except Exception:
            pass


def _bounded_target_key(session_id: str, attempt_id: str | None) -> str:
    return f"{session_id}|{attempt_id or ''}"


def canonical_command_bytes(body: dict) -> bytes:
    """Canonical signable bytes (signature field excluded)."""
    return canonical_json_bytes(
        {k: v for k, v in body.items() if k != "signature"})


def is_control_action(action: str) -> bool:
    return action in CONTROL_ACTIONS


__all__ = [
    "AuditStore",
    "SupervisorCommand",
    "SupervisorService",
    "SupervisorServiceError",
    "canonical_command_bytes",
    "is_control_action",
]