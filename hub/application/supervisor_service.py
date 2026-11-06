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

The queue/audit ALWAYS live in a ``SupervisorCommandRepository`` (single code
path).  Bootstrap wires the durable ``var/supervisor/commands.db`` whenever
the supervisor is enabled; a service built without one (tests, or a disabled
hub that never issues) lazily gets a private throwaway SQLite file instead of a
second in-memory implementation.
"""
from __future__ import annotations

import logging
import secrets
import sqlite3
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from hub.infrastructure.supervisor_command_repository import (
    SupervisorCommandRepository,
    SupervisorCommandRepositoryError,
)

from fleet_core.control import (
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


class SupervisorService:
    """Hub-side control queue authority (repository-backed queue + audit)."""

    def __init__(self, *, signing_key: bytes | None,
                 now_fn: Callable[[], float] | None = None,
                 ttl_s: float = 3600.0, max_clock_skew_s: float = 300.0,
                 append_user_turn_enabled: bool = False,
                 apply_local_profile_enabled: bool = False,
                 command_repository: SupervisorCommandRepository | None = None,
                 delivery_lease_s: float = 60.0):
        self._signing_key = signing_key
        self._now_fn = now_fn or _now_s
        self._ttl_s = float(ttl_s)
        self._max_clock_skew_s = float(max_clock_skew_s)
        self._append_user_turn_enabled = bool(append_user_turn_enabled)
        self._apply_local_profile_enabled = bool(apply_local_profile_enabled)
        self._repository = command_repository
        self._delivery_lease_s = max(1.0, float(delivery_lease_s))
        if self._repository is not None:
            self._repository.init()

    @property
    def _command_repository(self) -> SupervisorCommandRepository:
        """The queue repository; a private throwaway one when none was wired."""
        if self._repository is None:
            # 未接入持久库（测试 / 未启用 supervisor 的 hub）时用私有临时 SQLite，
            # 与生产走同一条代码路径；首次使用才创建，禁用态不产生文件。
            path = Path(tempfile.mkdtemp(prefix="fleet-supervisor-")) / "commands.db"
            repository = SupervisorCommandRepository(path)
            repository.init()
            self._repository = repository
        return self._repository

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
                payload: Mapping | None = None,
                idempotency_key: str | None = None) -> dict:
        """Create a signed control command for one machine/session/attempt.

        Returns the full signed envelope (with ``signature``).  Raises
        ``unsupported_action`` for an action outside the fixed set,
        ``duplicate_nonce`` for a reused nonce, and ``resume_superseded`` when
        a higher precedence action already owns the target.

        ``candidate`` is the optional bounded adoption identity (pid /
        started_at / exe_path / agent_family / native_file_path) that rides
        INSIDE the signed envelope only for ``adopt``.  It is embedded BEFORE
        signing so the Ed25519 signature covers it, and it never reaches the
        bounded audit (audit rows carry ids only).  An invalid candidate
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
        try:
            if self._command_repository.nonce_exists(action_nonce):
                raise SupervisorServiceError("duplicate_nonce", "")
        except SupervisorCommandRepositoryError as exc:
            raise SupervisorServiceError(exc.code, "", 503) from exc

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

        command_id = body["command_id"]
        # conflict-precedence gate BEFORE the row owns the target: a stale
        # higher-precedence command (terminate/quarantine) for the same target
        # rejects a later resume / follow-up outright.
        try:
            existing = self._command_repository.target_owner(session_id, attempt_id)
        except SupervisorCommandRepositoryError as exc:
            raise SupervisorServiceError(exc.code, "", 503) from exc
        existing_action = existing.get("action") if existing else None
        existing_status = existing.get("status") if existing else None
        if existing and existing_status in ("queued", "delivered"):
            if (existing_action in _HIGH_PRECEDENCE
                    and action in _LOW_PRECEDENCE):
                raise SupervisorServiceError(
                    "resume_superseded",
                    "目标已有终止/隔离命令，resume 被更高优先级命令取代", 409)

        # 仓储 insert 原子地完成：幂等键回放、目标占用切换、以及
        # terminate/quarantine 取代同目标仍排队的低优先级命令。
        try:
            stored = self._command_repository.insert(
                command_id=command_id, machine=machine, session_id=session_id,
                attempt_id=attempt_id, action=action, nonce=action_nonce,
                idempotency_key=idempotency_key, wire=body,
                issued_s=now, expires_s=expires_at_s,
                audit_row=_bound_row({
                    "event": "enqueue", "ts": _iso(now),
                    "command_id": command_id, "machine": machine,
                    "session_id": session_id, "action": action,
                }))
        except SupervisorCommandRepositoryError as exc:
            status = 409 if exc.code in {"resume_superseded", "command_conflict"} else 503
            raise SupervisorServiceError(exc.code, "", status) from exc
        if stored["command_id"] != command_id:
            return dict(stored["wire"])
        return dict(body)

    # ------------------------------------------------------------------
    # poll
    # ------------------------------------------------------------------

    def poll(self, machine: str, *, max_commands: int = 20) -> list[dict]:
        """Hand out queued signed envelopes for ``machine`` (each once)."""
        self.assert_keyed()
        self._expire_stale()
        try:
            now = self._now_fn()
            rows = self._command_repository.claim_available(
                machine, now, max(1, min(int(max_commands), 100)),
                now + self._delivery_lease_s,
                audit_row={"event": "deliver", "ts": _iso(now),
                           "machine": machine})
        except SupervisorCommandRepositoryError as exc:
            raise SupervisorServiceError(exc.code, "", 503) from exc
        return [dict(item["wire"]) for item in rows]

    def _expire_stale(self) -> None:
        now = self._now_fn()
        try:
            self._command_repository.expire(now, audit_ts=_iso(now))
        except SupervisorCommandRepositoryError as exc:
            raise SupervisorServiceError(exc.code, "", 503) from exc

    # ------------------------------------------------------------------
    # receipts
    # ------------------------------------------------------------------

    def receipt(self, command_id: str, status: str, reason: str = "",
                *, machine: str | None = None) -> dict:
        """Record an Agent receipt; duplicate receipts replay the original."""
        self.assert_keyed()
        try:
            persisted = self._command_repository.find_command(command_id)
        except (SupervisorCommandRepositoryError, sqlite3.Error, OSError) as exc:
            code = exc.code if isinstance(exc, SupervisorCommandRepositoryError) else "command_unavailable"
            raise SupervisorServiceError(code, "", 503) from exc
        if persisted is None:
            raise SupervisorServiceError("command_not_found", "")
        if machine and persisted["machine"] != machine:
            raise SupervisorServiceError("machine_mismatch", "", 403)
        if status not in RECEIPT_STATUSES:
            raise SupervisorServiceError("invalid_receipt_status", "", 400)

        receipt_now = self._now_fn()
        if persisted["status"] in _TERMINAL:
            # Already terminal: deterministically replay the ORIGINAL persisted
            # sender receipt (status/reason), never accept a second receipt that
            # would overwrite it.  The source of truth is receipt_status /
            # receipt_reason — once recorded it is immutable.
            return {"command_id": command_id,
                    "status": persisted["receipt_status"] or persisted["status"],
                    "reason": persisted["receipt_reason"] or "", "first": False}
        try:
            updated, first = self._command_repository.mark_status(
                command_id, status, status, str(reason)[:120],
                now_s=receipt_now, audit_ts=_iso(receipt_now),
                audit_row=_bound_row({
                    "event": f"receipt:{status}", "ts": _iso(receipt_now),
                    "command_id": command_id, "machine": persisted["machine"],
                    "status": status,
                }))
        except (SupervisorCommandRepositoryError, sqlite3.Error, OSError) as exc:
            code = exc.code if isinstance(exc, SupervisorCommandRepositoryError) else "command_unavailable"
            raise SupervisorServiceError(code, "", 503) from exc
        if updated is None:
            raise SupervisorServiceError("command_not_found", "", 404)
        return {"command_id": command_id, "status": updated["receipt_status"] or status,
                "reason": updated["receipt_reason"] or "", "first": first}

    def pending_session_projections(self, machine: str) -> list[dict]:
        try:
            return self._command_repository.pending_session_projections(
                machine, self._now_fn())
        except SupervisorCommandRepositoryError as exc:
            raise SupervisorServiceError(exc.code, "", 503) from exc

    def defer_session_projection(self, command_id: str, delay_s: float = 30.0) -> None:
        try:
            self._command_repository.defer_session_projection(
                command_id, self._now_fn() + delay_s)
        except SupervisorCommandRepositoryError as exc:
            raise SupervisorServiceError(exc.code, "", 503) from exc

    def complete_session_projection(self, command_id: str) -> None:
        try:
            self._command_repository.complete_session_projection(command_id)
        except SupervisorCommandRepositoryError as exc:
            raise SupervisorServiceError(exc.code, "", 503) from exc

    def command_status(self, command_id: str) -> str | None:
        try:
            row = self._command_repository.find_command(command_id)
        except SupervisorCommandRepositoryError as exc:
            raise SupervisorServiceError(exc.code, "", 503) from exc
        return row["status"] if row else None

    def find_idempotency(self, key: str) -> dict | None:
        try:
            row = self._command_repository.find_idempotency(key)
        except SupervisorCommandRepositoryError as exc:
            raise SupervisorServiceError(exc.code, "", 503) from exc
        return dict(row["wire"]) if row else None

    def receipt_status(self, command_id: str) -> dict:
        """Return a bounded operator view of one command's current receipt."""
        self._expire_stale()
        try:
            row = self._command_repository.find_command(str(command_id or ""))
        except SupervisorCommandRepositoryError as exc:
            raise SupervisorServiceError(exc.code, "", 503) from exc
        if row is None:
            raise SupervisorServiceError("command_not_found", "", 404)
        return {"command_id": row["command_id"], "action": row["action"],
                "status": row["receipt_status"] or row["status"],
                "reason": row["receipt_reason"] or "", "issued_at": _iso(row["issued_s"]),
                "expires_at": _iso(row["expires_s"]), "attempt_id": row["attempt_id"],
                "machine_id": row["machine"], "session_id": row["session_id"]}

    # ------------------------------------------------------------------
    # audit helpers
    # ------------------------------------------------------------------

    def audit_recent(self, limit: int = 200) -> list[dict]:
        try:
            return self._command_repository.audit_recent(limit=limit)
        except SupervisorCommandRepositoryError as exc:
            raise SupervisorServiceError(exc.code, "", 503) from exc


def canonical_command_bytes(body: dict) -> bytes:
    """Canonical signable bytes (signature field excluded)."""
    return canonical_json_bytes(
        {k: v for k, v in body.items() if k != "signature"})


def is_control_action(action: str) -> bool:
    return action in CONTROL_ACTIONS


__all__ = [
    "AuditStore",
    "SupervisorService",
    "SupervisorServiceError",
    "canonical_command_bytes",
    "is_control_action",
]
