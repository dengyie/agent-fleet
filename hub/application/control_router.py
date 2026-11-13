"""hub/application/control_router.py — task cancel → cancel_attempt bridge (Task 10).

``ControlRouter`` connects the operator task-cancel transition to the Task 9
Supervisor control queue for MANAGED ACTIVE attempts only:

* ``enqueue_cancel_attempt(task_id, attempt_id, operator, reason_code)`` is
  called from ``TaskService.cancel`` strictly AFTER the existing task
  transition (queued/leased/running -> cancelled) has been accepted by the
  repository.  When the attempt is a managed active attempt (a ``managed``
  session row bound to that attempt_id exists in the session repository) it
  enqueues exactly ONE signed fixed-action ``cancel_attempt`` command through
  ``SupervisorService`` and returns its ``command_id``; otherwise it returns
  ``None`` and the unmanaged/queued/terminal cancel remains byte-identical to
  the pre-Task-10 behaviour.
* A command-queue failure NEVER rolls back or further mutates the task state
  (the cancel transition already committed) - it is recorded as a bounded
  ``control_pending``/``control_failed`` audit row instead.
* ``on_supervisor_receipt`` finalizes a tracked attempt with one of the
  bounded outcomes ``terminated`` / ``already_finished`` / ``failed`` /
  ``expired`` / ``rejected``, idempotently - a replayed terminal receipt
  returns the ORIGINAL finalized outcome and never overwrites it.
* Attempt fencing lives in the existing task/lease state machine: a cancelled
  task is no longer ``leased``/``running`` so its heartbeats/results keep
  being REJECTED (409 ``lease_mismatch`` / ``lease_expired``).

No credential, token, key VALUE, path or raw exception is ever logged,
printed or stored here; only bounded identifiers and stable codes.
"""
from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from hub.application.supervisor_service import (
    AuditStore,
    SupervisorService,
    SupervisorServiceError,
)
from hub.infrastructure.adoption_repository import AdoptionRepositoryError

_ACTION = "cancel_attempt"
_DEFAULT_REASON = "operator_requested"

#: Receipt terminal status -> bounded attempt outcome (verbatim from the brief).
_RECEIPT_OUTCOMES: dict[str, str] = {
    "succeeded": "terminated",
    "already_finished": "already_finished",
    "failed": "failed",
    "expired": "expired",
    "rejected": "rejected",
}

#: Bounded outcomes the router can emit for an attempt (exported for tests).
CANCEL_OUTCOMES = frozenset(_RECEIPT_OUTCOMES.values())

_MAX_OPAQUE = 128
_MAX_REASON = 32
_MAX_AUDIT_STR = 200
_SESSION_SCAN = 1000
#: 进程内跟踪表上限：条目数与存活时间双重约束，长跑 hub 不会无限增长。
_TRACK_MAX = 4096
_TRACK_TTL_S = 24 * 3600.0
#: Supervisor 无法确认进程组生死（如跨实例无法核验身份）：绝不能当作已停止。
_UNVERIFIED_REASONS = frozenset({"escape_unverified", "liveness_unknown"})
#: 会话仓库查询失败的哨兵（区别于「确实没有托管会话」）
_LOOKUP_FAILED = object()


def _bound(value: Any, limit: int = _MAX_OPAQUE, default: str = "") -> str:
    """Coerce any value to a bounded string (never leaks internals)."""
    if value is None:
        return default
    text = str(value)
    return text[:limit] if text else default


def _bound_row(row: dict) -> dict:
    """Drop secret/path/signature fields and bound every string in an audit row."""
    out: dict[str, Any] = {}
    for key, value in row.items():
        if key in ("signature", "secret", "token", "credential", "raw",
                   "payload", "nonce"):
            continue
        if isinstance(value, str):
            value = value[:_MAX_AUDIT_STR]
        out[key] = value
    return out


class ControlRouterError(RuntimeError):
    """Bounded control-router error: only a stable short code is exposed."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = str(detail)[:_MAX_AUDIT_STR]
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


class ControlRouter:
    """Cancel -> cancel_attempt bridge + receipt finalization (Task 10)."""

    def __init__(self, *, supervisor: SupervisorService, task_repository: Any,
                 session_repo: Any | None = None,
                 audit_store: Any | None = None) -> None:
        self._supervisor = supervisor
        self._task_repo = task_repository
        self._session_repo = session_repo
        self._audit = audit_store if audit_store is not None else AuditStore()
        # command_id -> bounded context
        self._tracked: dict[str, dict[str, str]] = {}
        # command_id -> bounded finalized outcome (immutable once set)
        self._finalized: dict[str, str] = {}
        # command_id -> 登记时刻（按插入顺序，供 TTL/容量淘汰）
        self._tracked_at: dict[str, float] = {}
        self._clock = time.monotonic

    # ------------------------------------------------------------------ #
    # write use case
    # ------------------------------------------------------------------ #

    def enqueue_cancel_attempt(self, task_id: str, attempt_id: str | None,
                               operator: str,
                               reason_code: str = "") -> str | None:
        """Enqueue ONE ``cancel_attempt`` command for a managed active attempt.

        Returns the signed command ``command_id`` when a managed active attempt
        exists, otherwise ``None``.  Contracts:

        * only a *managed* attempt (a managed session bound to the attempt in
          the session repository) triggers a command;
        * only the *active* attempt (an attempt a Supervisor has actually
          launched for the task) is eligible - a queued task has never started
          a managed session, so it falls into the unmanaged path;
        * a command-queue failure records bounded ``control_failed`` and
          returns ``None``; the task state is never touched again;
        * a successful enqueue records bounded ``control_pending`` and returns
          the ``command_id``;
        * no secret/nonce/token ever appears in an audit row.
        """
        task_id = _bound(task_id, 128)
        attempt = _bound(attempt_id, 128)
        if not attempt:
            return None
        task = self._get_task(task_id)
        if task is None:
            self._note("control_failed", task_id=task_id, attempt_id=attempt,
                       operator=_bound(operator), code="task_not_found")
            return None
        machine = _bound(task.get("machine"))
        session = self._managed_session(machine, attempt)
        if session is _LOOKUP_FAILED:
            # 查询失败 ≠ 非托管：记 control_failed，避免把托管尝试静默当作普通取消
            self._note("control_failed", task_id=task_id, attempt_id=attempt,
                       machine=machine, operator=_bound(operator),
                       code="session_lookup_failed")
            return None
        if session is None:
            # Unmanaged (or never-started) attempt: byte-identical cancel, no cmd.
            return None
        session_id = _bound(session.get("session_id"))
        reason = _bound(reason_code or _DEFAULT_REASON, _MAX_REASON)
        try:
            envelope = self._supervisor.enqueue(
                machine, session_id, attempt, _ACTION, reason)
        except SupervisorServiceError as exc:
            self._note("control_failed", task_id=task_id, attempt_id=attempt,
                       operator=_bound(operator), code=_bound(exc.code))
            return None
        except Exception:
            self._note("control_failed", task_id=task_id, attempt_id=attempt,
                       machine=machine, code="control_enqueue_failed")
            return None
        command_id = _bound(envelope.get("command_id"), 128)
        if not command_id:
            self._note("control_failed", task_id=task_id, attempt_id=attempt,
                       machine=machine, code="malformed_command")
            return None
        self._prune_tracking()
        self._tracked_at.pop(command_id, None)
        self._tracked_at[command_id] = self._clock()
        self._tracked[command_id] = {
            "command_id": command_id,
            "task_id": task_id,
            "attempt_id": attempt,
            "machine": machine,
            "session_id": session_id,
            "operator": _bound(operator, 128),
            "action": _ACTION,
        }
        self._note("control_pending", command_id=command_id, task_id=task_id,
                   attempt_id=attempt, machine=machine,
                   operator=_bound(operator), action=_ACTION)
        return command_id

    # ------------------------------------------------------------------ #
    # receipt finalization
    # ------------------------------------------------------------------ #

    def on_supervisor_receipt(self, command_id: str, *, status: str | None = None,
                              reason: str = "") -> dict | None:
        """Finalize a tracked cancel_attempt from a Supervisor receipt.

        Terminal statuses map to the bounded outcomes ``terminated`` /
        ``already_finished`` / ``failed`` / ``expired`` / ``rejected`` and are
        recorded ONCE.  A replayed terminal receipt (or a duplicate/late
        command) returns the ORIGINAL finalized outcome - a late success can
        never overwrite a terminated/cancelled attempt.  Non-terminal statuses
        leave the attempt unfinalized and return ``None``.
        """
        if command_id not in self._tracked:
            return None
        status = _bound(status if status is not None
                        else self._safe_status(command_id), 32)
        receipt_reason = _bound(reason, 120)
        outcome = _RECEIPT_OUTCOMES.get(status)
        # Older or incompatible clients could mark an unverified kill as
        # succeeded. Treat that bounded reason as a failed control outcome so
        # the session summary remains active until exit is actually observed.
        if receipt_reason in _UNVERIFIED_REASONS and outcome in (
                "terminated", "already_finished"):
            outcome = "failed"
        if outcome is None:
            # queued / delivered / accepted / executing are progress only.
            return None
        finalized = self._finalized.get(command_id)
        if finalized is not None:
            # Deterministic replay: return the FIRST recorded outcome.
            return {"command_id": command_id, "outcome": finalized,
                    "first": False}
        tracked = self._tracked[command_id]
        if receipt_reason == "liveness_unknown":
            # 进程是否仍在运行未知：会话保持原状态，只记控制失败供运营排查
            self._note("control_failed", command_id=command_id,
                       task_id=tracked["task_id"], attempt_id=tracked["attempt_id"],
                       machine=tracked.get("machine"), code="liveness_unknown")
        if outcome in ("terminated", "already_finished"):
            if not self._project_terminated_session(tracked):
                return None
        self._finalized[command_id] = outcome
        self._note("control_final", command_id=command_id,
                   task_id=tracked["task_id"], attempt_id=tracked["attempt_id"],
                   machine=tracked.get("machine"), outcome=outcome,
                   reason=receipt_reason)
        return {"command_id": command_id, "outcome": outcome, "first": True}

    def _project_terminated_session(self, tracked: Mapping[str, str]) -> bool:
        """Close the managed-session summary after a terminal receipt."""
        session_id = tracked.get("session_id")
        close_session = getattr(self._session_repo, "close_session", None)
        if not session_id or not callable(close_session):
            return True
        finished_at = datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z")
        try:
            finish_managed = getattr(
                self._session_repo, "finish_managed_session", None)
            if callable(finish_managed):
                return finish_managed(session_id, finished_at) is not False
            session = self._session_repo.get_session(session_id)
            closed = close_session(session_id, "closed", finished_at)
            if session is None or closed is None:
                return True
            if (session.get("status") not in ("closed", "failed")
                    and session.get("current_state") not in (
                        "completed", "failed", "stopped")):
                sequence = max(1, int(session.get("state_sequence") or 0) + 1)
                self._session_repo.update_agent_state(
                    session_id, "stopped", "supervisor", "high", sequence)
            return True
        except Exception:
            self._note("control_projection_failed",
                       command_id=_bound(tracked.get("command_id")),
                       task_id=_bound(tracked.get("task_id")),
                       attempt_id=_bound(tracked.get("attempt_id")),
                       code="session_unavailable")
            return False

    def _prune_tracking(self) -> None:
        """按登记顺序淘汰超龄或超额的跟踪条目（含其终态记录）。"""
        now = self._clock()
        while self._tracked_at:
            oldest, at = next(iter(self._tracked_at.items()))
            if len(self._tracked_at) < _TRACK_MAX and now - at <= _TRACK_TTL_S:
                break
            self._tracked_at.pop(oldest, None)
            self._tracked.pop(oldest, None)
            self._finalized.pop(oldest, None)

    def outcome_of(self, command_id: str) -> str | None:
        """Bounded finalized outcome for ``command_id`` or ``None``."""
        return self._finalized.get(command_id)

    def tracked(self, command_id: str) -> dict | None:
        """Bounded view of one tracked command (never a nonce/signature)."""
        row = self._tracked.get(command_id)
        if row is None:
            return None
        return {k: _bound(v, _MAX_AUDIT_STR) for k, v in row.items()}

    def audit_recent(self, limit: int = 200) -> list[dict]:
        try:
            return self._audit.read(limit=limit)
        except Exception:
            return []

    def pending_count(self) -> int:
        return len(self._tracked)

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    def _get_task(self, task_id: str) -> dict | None:
        try:
            row = self._task_repo.get_task(task_id)
        except Exception:
            return None
        return dict(row) if isinstance(row, Mapping) else None

    def _managed_session(self, machine: str, attempt_id: str) -> Any:
        """Return the managed session bound to ``(machine, attempt_id)``.

        Bounded scan: at most ``_SESSION_SCAN`` recent rows of the machine;
        requires ``managed`` (an unmanaged session never gets control) and an
        exact opaque attempt match.
        """
        if self._session_repo is None:
            return None
        try:
            rows = self._session_repo.list_sessions(machine_id=machine,
                                                    limit=_SESSION_SCAN)
        except Exception:
            return _LOOKUP_FAILED
        if not isinstance(rows, list):
            return _LOOKUP_FAILED
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            if bool(row.get("managed")) and (row.get("attempt_id") or None) == attempt_id:
                return dict(row)
        return None

    def _safe_status(self, command_id: str) -> str:
        try:
            status = self._supervisor.command_status(command_id)
        except Exception:
            return ""
        return str(status or "")

    def _note(self, event: str, **fields: Any) -> None:
        bounded = _bound_row({"event": event, **fields})
        try:
            self._audit.emit(bounded)
        except Exception:
            pass


class SupervisorReceiptHook:
    """Fan canonical receipts out and recover durable session projections.

    Confirmed termination closes the managed summary before consumers settle.
    The existing machine poll retries pending projections after a store failure
    or Hub restart, without returning those terminal commands to the client.
    """

    def __init__(self, inner: SupervisorService,
                 router: ControlRouter | None,
                 adoption=None, *, session_repo=None) -> None:
        self._inner = inner
        self._router = router
        self._adoptions = adoption
        self._session_repo = session_repo

    def set_adoption(self, adoption) -> None:
        """Attach the (optional) adoption-service receipt consumer."""
        self._adoptions = adoption

    def poll(self, machine: str, *, max_commands: int = 20) -> list[dict]:
        if self._session_repo is not None:
            for command in self._inner.pending_session_projections(machine):
                self._project_session(command)
        return self._inner.poll(machine, max_commands=max_commands)

    def _project_session(self, command: Mapping[str, Any]) -> None:
        confirmed = (
            command["status"] == "succeeded"
            and command["reason"] in ("terminated", "terminated_forced")
        ) or (
            command["status"] == "already_finished"
            and command["reason"] in ("already_finished", "no_live_process")
        )
        if (command["action"] not in ("terminate_session", "cancel_attempt")
                or not confirmed):
            return
        finished_at = datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z")
        try:
            session = self._session_repo.finish_managed_session(
                command["session_id"], finished_at)
        except Exception as exc:
            raise SupervisorServiceError(
                "session_projection_unavailable", "", 503) from exc
        if session is None:
            # The bridge may upload its session_start after the receipt. Keep
            # the pending marker and let later batches progress in the meantime.
            self._inner.defer_session_projection(command["command_id"])
            return
        self._inner.complete_session_projection(command["command_id"])

    def receipt(self, command_id: str, status: str, reason: str = "",
                *, machine: str | None = None) -> dict:
        result = self._inner.receipt(command_id, status, reason, machine=machine)
        # Read identity and outcome from the durable command, never the
        # incoming replay or the adoption service's volatile tracking map.
        # A committed receipt may be replayed after a projection failure or
        # Hub restart; the atomic session finalizer is safe to repeat.
        if self._session_repo is not None:
            command = self._inner.receipt_status(command_id)
            self._project_session(command)
        canonical_status = str(result.get("status") or "")
        canonical_reason = str(result.get("reason") or "")
        if self._router is not None:
            try:
                self._router.on_supervisor_receipt(
                    command_id,
                    status=canonical_status, reason=canonical_reason)
            except Exception:
                pass
        if self._adoptions is not None:
            try:
                # Same bounds the router sees: use the ENQUEUED reason when the
                # receipt replays the original (idempotency is replayed here).
                self._adoptions.on_supervisor_receipt(
                    command_id,
                    status=canonical_status, reason=canonical_reason)
            except AdoptionRepositoryError:
                # A genuine store failure is NOT a stray receipt: it must
                # reach the hub's 500-protect handler (bounded) instead of
                # being swallowed here, so the operator/controller learns the
                # adoption work did not land and the tracking entry stays for
                # a retry.
                raise
            except Exception:
                # every OTHER stray/advisory receipt stays inert - the
                # adoption service never raises for receipt-visible failure.
                pass
        return result

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


__all__ = [
    "CANCEL_OUTCOMES",
    "ControlRouter",
    "ControlRouterError",
    "SupervisorReceiptHook",
]
