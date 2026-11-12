"""Deterministic operator control contracts and generation guards."""
from __future__ import annotations

from dataclasses import dataclass

from .workflow_errors import WorkflowValidationError
from .workflow_identity import validate_reference
from .workflow_contract import reference_values
from .workflow_contract_types import NamedReference

CONTROL_ACTIONS = frozenset({"pause", "resume", "cancel", "retry", "invalidate", "reconcile"})
MAX_COUNTER = 9007199254740991


def exact_counter(value: int, *, minimum: int = 1) -> int:
    if type(value) is not int or not minimum <= value <= MAX_COUNTER:
        raise WorkflowValidationError("invalid_integer")
    return value


@dataclass(frozen=True, slots=True)
class WorkflowControlRequest:
    owner_id: str
    workflow_id: str
    action: str
    expected_revision: int
    client_token: str
    step_key: str | None = None
    expected_generation: int | None = None
    reason: str | None = None
    input_refs: tuple[NamedReference, ...] | None = None

    def __post_init__(self) -> None:
        for value in (self.owner_id, self.workflow_id, self.client_token):
            validate_reference(value)
        exact_counter(self.expected_revision)
        if type(self.action) is not str or self.action not in CONTROL_ACTIONS:
            raise WorkflowValidationError("invalid_control_action")
        if self.action in ("retry", "invalidate", "reconcile"):
            validate_reference(self.step_key)
            if self.action != "reconcile" or self.expected_generation is not None:
                exact_counter(self.expected_generation)
        elif self.step_key is not None or self.expected_generation is not None:
            raise WorkflowValidationError("unexpected_step_control")
        if self.action == "invalidate":
            validate_reference(self.reason)
            if self.input_refs is not None:
                reference_values(self.input_refs)
        elif self.reason is not None:
            raise WorkflowValidationError("unexpected_control_reason")
        if self.action != "invalidate" and self.input_refs is not None:
            raise WorkflowValidationError("unexpected_input_change")


@dataclass(frozen=True, slots=True)
class AuthorizedInputRevision:
    owner_id: str
    workspace_id: str
    input_refs: tuple[NamedReference, ...]

    def __post_init__(self) -> None:
        validate_reference(self.owner_id)
        validate_reference(self.workspace_id)
        reference_values(self.input_refs)


@dataclass(frozen=True, slots=True)
class WorkflowControlReceipt:
    action: str
    revision: int
    execution_state: str
    generations: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class WorkflowStopRequest:
    step_key: str
    generation: int
    reason_code: str
    requested_at: float
    deadline: float


@dataclass(frozen=True, slots=True)
class WorkflowReconciliationRequest:
    step_key: str
    generation: int
    mode: str
    deadline: float | None


def generation_plan(
    steps: tuple[tuple[str, int, str, bool, int], ...], step_key: str, *, invalidate: bool,
) -> tuple[tuple[str, int], ...]:
    """Linear O(n), n<=8; no active/unknown generation may be superseded."""
    if type(steps) is not tuple or not 1 <= len(steps) <= 8 or type(invalidate) is not bool:
        raise WorkflowValidationError("invalid_generation_plan")
    index = {step[0]: position for position, step in enumerate(steps)}
    if len(index) != len(steps) or step_key not in index:
        raise WorkflowValidationError("unknown_step")
    affected = steps[index[step_key]:]
    if not invalidate and affected[0][2] != "failed":
        raise WorkflowValidationError("retry_requires_failure")
    for _, generation, state, held, limit in affected:
        exact_counter(generation)
        exact_counter(limit)
        if state == "unknown" or held or state in ("dispatching", "running", "waiting_external", "cancelling"):
            raise WorkflowValidationError("unresolved_execution")
        if state == "cancelled":
            raise WorkflowValidationError("cancelled_generation")
        if generation >= limit:
            raise WorkflowValidationError("generation_exhausted")
    return tuple((key, generation + 1) for key, generation, *_ in affected)
