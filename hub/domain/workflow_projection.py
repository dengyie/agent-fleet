"""Deterministic projections of current execution, outputs and fixed evidence."""
from __future__ import annotations

from dataclasses import dataclass

from .workflow import STEP_STATES
from .workflow_contract_types import WorkflowDefinition
from .workflow_errors import WorkflowValidationError
from .workflow_evidence import can_release_successor, validation_state
from .workflow_records import WorkflowSnapshot

TERMINAL_STATES = frozenset({"succeeded", "failed", "unknown", "cancelled"})
_LIVE_STATES = frozenset({
    "dispatching", "running", "waiting_node", "waiting_external", "waiting_approval", "paused", "cancelling",
})


@dataclass(frozen=True, slots=True)
class WorkflowProjection:
    execution_state: str
    validation_state: str
    reason_code: str | None
    eligible_step: str | None
    required_outputs_ready: bool
    step_validations: tuple[str, ...]


def project_workflow(definition: WorkflowDefinition, snapshot: WorkflowSnapshot) -> WorkflowProjection:
    """O(steps + bounded evidence); no clock, random source or cached verdicts."""
    if type(definition) is not WorkflowDefinition or type(snapshot) is not WorkflowSnapshot:
        raise WorkflowValidationError("invalid_workflow_snapshot")
    if definition.snapshot != snapshot.workflow.definition_snapshot or len(snapshot.steps) != len(definition.steps):
        raise WorkflowValidationError("invalid_workflow_snapshot")
    validations: list[str] = []
    output_ready: list[bool] = []
    states: list[str] = []
    for expected, step in zip(definition.steps, snapshot.steps):
        if (expected.step_key, expected.kind) != (step.step_key, step.kind) or step.state not in STEP_STATES or step.state == "skipped":
            raise WorkflowValidationError("invalid_workflow_snapshot")
        required = frozenset(f"{item.id}@{item.revision}" for item in expected.contract.validator_refs)
        verdict = "pending" if required else "not_requested"
        if step.subject_digest is not None:
            verdict = validation_state(step.subject_digest, required, step.evidence)
        validations.append(verdict)
        present = frozenset(step.output_keys)
        output_ready.append(all(item.output_key in present for item in expected.contract.output_specs if item.required))
        states.append(step.state)
    required_results = tuple(value for value in validations if value != "not_requested")
    aggregate = next((value for value in ("failed", "pending", "inconclusive") if value in required_results),
                     "passed" if required_results else "not_requested")
    execution, reason, eligible = _execution(snapshot, states, validations, output_ready)
    return WorkflowProjection(execution, aggregate, reason, eligible, all(output_ready), tuple(validations))


def _execution(
    snapshot: WorkflowSnapshot, states: list[str], validations: list[str], output_ready: list[bool],
) -> tuple[str, str | None, str | None]:
    if snapshot.workflow.cancel_requested:
        if "unknown" in states:
            return "unknown", "stop_unconfirmed", None
        if any(step.dispatch_stage is not None and step.state not in TERMINAL_STATES for step in snapshot.steps):
            return "cancelling", "cancel_requested", None
        if all(state == "succeeded" for state in states):
            return "succeeded", None if all(output_ready) else "outputs_unavailable", None
        return "cancelled", "cancel_requested", None
    for terminal in ("unknown", "failed", "cancelled"):
        if terminal in states:
            step = snapshot.steps[states.index(terminal)]
            return terminal, step.reason_code, None
    if all(state == "succeeded" for state in states):
        return "succeeded", None if all(output_ready) else "outputs_unavailable", None
    active = next((step for step in snapshot.steps if step.dispatch_stage is not None and step.state in _LIVE_STATES), None)
    if active is not None:
        state = "running" if active.state == "dispatching" else active.state
        return state, active.reason_code, None
    if snapshot.workflow.pause_requested:
        return "paused", "pause_requested", None
    for index, step in enumerate(snapshot.steps):
        if step.state != "succeeded":
            return ("waiting_node" if step.state == "waiting_node" else "running"), step.reason_code, step.step_key
        if not can_release_successor(step.state, validations[index], validation_required=validations[index] != "not_requested"):
            reason = "blocked_by_validation" if validations[index] == "failed" else "awaiting_validation"
            return "waiting_approval", reason, None
        if not output_ready[index]:
            return "waiting_approval", "outputs_unavailable", None
    raise WorkflowValidationError("invalid_workflow_snapshot")
