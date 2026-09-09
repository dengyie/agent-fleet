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

from collections.abc import Mapping
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
                 session_repo: Any, audit_store: Any | None = None) -> None:
        self._supervisor = supervisor
        self._task_repo = task_repository
        self._session_repo = session_repo
        self._audit = audit_store if audit_store is not None else AuditStore()
        # command_id -> bounded context
        self._tracked: dict[str, dict[str, str]] = {}
        # command_id -> bounded finalized outcome (immutable once set)
        self._finalized: dict[str, str] = {}

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
        self._tracked[command_id] = {
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
        outcome = _RECEIPT_OUTCOMES.get(status)
        if outcome is None:
            # queued / delivered / accepted / executing are progress only.
            return None
        finalized = self._finalized.get(command_id)
        if finalized is not None:
            # Deterministic replay: return the FIRST recorded outcome.
            return {"command_id": command_id, "outcome": finalized,
                    "first": False}
        self._finalized[command_id] = outcome
        tracked = self._tracked[command_id]
        self._note("control_final", command_id=command_id,
                   task_id=tracked["task_id"], attempt_id=tracked["attempt_id"],
                   machine=tracked.get("machine"), outcome=outcome,
                   reason=_bound(reason, 120))
        return {"command_id": command_id, "outcome": outcome, "first": True}

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

    def _managed_session(self, machine: str, attempt_id: str) -> dict | None:
        """Return the managed session bound to ``(machine, attempt_id)``.

        Bounded scan: at most ``_SESSION_SCAN`` recent rows of the machine;
        requires ``managed`` (an unmanaged session never gets control) and an
        exact opaque attempt match.
        """
        try:
            rows = self._session_repo.list_sessions(machine_id=machine,
                                                    limit=_SESSION_SCAN)
        except Exception:
            return None
        if not isinstance(rows, list):
            return None
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
    """Thin additive wrapper notifying the router AND adoption service.

    Only ``receipt`` is special-cased; every other attribute is forwarded to
    the inner ``SupervisorService``, so the Task 9 supervisor surface and its
    poll/enqueue/audit contracts stay fully intact for the rest of the system.
    Task 12 fans EVERY recorded receipt to the adoption service too (its
    ``on_supervisor_receipt`` closure stays inert for commands it never
    issued).  The adoption consumer is optional: when absent the receipt flow
    is byte-identical to Task 10.
    """

    def __init__(self, inner: SupervisorService,
                 router: ControlRouter | None,
                 adoption=None) -> None:
        self._inner = inner
        self._router = router
        self._adoptions = adoption

    def set_adoption(self, adoption) -> None:
        """Attach the (optional) adoption-service receipt consumer."""
        self._adoptions = adoption

    def receipt(self, command_id: str, status: str, reason: str = "",
                *, machine: str | None = None) -> dict:
        result = self._inner.receipt(command_id, status, reason, machine=machine)
        if self._router is not None:
            try:
                self._router.on_supervisor_receipt(
                    command_id,
                    status=str(result.get("status") or status),
                    reason=reason or str(result.get("reason") or ""))
            except Exception:
                pass
        if self._adoptions is not None:
            try:
                # Same bounds the router sees: use the ENQUEUED reason when the
                # receipt replays the original (idempotency is replayed here).
                self._adoptions.on_supervisor_receipt(
                    command_id,
                    status=str(result.get("status") or status),
                    reason=reason or str(result.get("reason") or ""))
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