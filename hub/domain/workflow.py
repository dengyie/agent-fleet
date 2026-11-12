"""Workflow state vocabulary and internal, fact-driven transition guards."""
from __future__ import annotations

from .workflow_errors import WorkflowValidationError

STEP_STATES = frozenset({
    "pending", "ready", "dispatching", "running", "waiting_node",
    "waiting_external", "waiting_approval", "paused", "cancelling",
    "succeeded", "failed", "unknown", "cancelled", "skipped",
})
WORKFLOW_EXECUTION_STATES = frozenset({
    "queued", "running", "waiting_node", "waiting_external", "waiting_approval",
    "paused", "cancelling", "succeeded", "failed", "unknown", "cancelled",
})
_ALLOWED: dict[str, frozenset[str]] = {
    "pending": frozenset({"ready", "failed", "cancelled"}),
    "ready": frozenset({
        "dispatching", "waiting_node", "waiting_approval", "paused", "failed", "cancelled",
    }),
    "dispatching": frozenset({
        "running", "waiting_external", "waiting_approval", "succeeded", "failed", "unknown", "cancelling",
    }),
    "running": frozenset({
        "waiting_node", "waiting_external", "waiting_approval", "paused", "succeeded", "failed",
        "unknown", "cancelling",
    }),
    "waiting_node": frozenset({
        "ready", "running", "paused", "succeeded", "failed", "unknown", "cancelling", "cancelled",
    }),
    "waiting_external": frozenset({
        "running", "waiting_approval", "succeeded", "failed", "unknown", "cancelling",
    }),
    "waiting_approval": frozenset({
        "ready", "running", "paused", "succeeded", "failed", "unknown", "cancelling", "cancelled",
    }),
    "paused": frozenset({
        "ready", "running", "succeeded", "failed", "unknown", "cancelling", "cancelled",
    }),
    "cancelling": frozenset({"cancelled", "unknown", "succeeded", "failed"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "unknown": frozenset(),
    "cancelled": frozenset(),
    "skipped": frozenset(),
}
_RECONCILED_TERMINALS = frozenset({"succeeded", "failed", "cancelled"})


def transition_step(current: str, target: str, *, reconciled: bool = False) -> str:
    """Validate the graph; the caller verifies binding, stop evidence and authority."""
    if type(current) is not str or current not in STEP_STATES:
        raise WorkflowValidationError("invalid_current_state")
    if type(target) is not str or target not in STEP_STATES:
        raise WorkflowValidationError("invalid_target_state")
    if type(reconciled) is not bool:
        raise WorkflowValidationError("invalid_reconciliation_flag")
    if reconciled:
        if current != "unknown" or target not in _RECONCILED_TERMINALS:
            raise WorkflowValidationError("invalid_reconciliation_transition")
        return target
    if current == target:
        return current
    if target not in _ALLOWED[current]:
        raise WorkflowValidationError("invalid_step_transition")
    return target
