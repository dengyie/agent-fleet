"""Current-version evidence and deterministic delivery projection; no I/O."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .workflow import STEP_STATES, WORKFLOW_EXECUTION_STATES
from .workflow_errors import WorkflowValidationError
from .workflow_identity import validate_reference

VALIDATION_STATES = frozenset({"not_requested", "pending", "passed", "failed", "inconclusive"})
ACCEPTANCE_STATES = frozenset({"not_requested", "pending", "accepted", "rejected"})
_RESULTS = frozenset({"passed", "failed", "inconclusive"})
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _digest(value: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise WorkflowValidationError("invalid_subject_digest")


def _choice(value: str, allowed: frozenset[str]) -> None:
    if type(value) is not str or value not in allowed:
        raise WorkflowValidationError("invalid_state")


def _flag(value: bool) -> None:
    if type(value) is not bool:
        raise WorkflowValidationError("invalid_boolean")


@dataclass(frozen=True, slots=True)
class ValidationEvidence:
    evidence_key: str
    validator_ref: str
    subject_digest: str
    result: str

    def __post_init__(self) -> None:
        validate_reference(self.evidence_key)
        validate_reference(self.validator_ref)
        _digest(self.subject_digest)
        _choice(self.result, _RESULTS)


def validation_state(
    subject_digest: str,
    required_validators: frozenset[str],
    evidence: tuple[ValidationEvidence, ...],
) -> str:
    """Only matching subject and recipe revisions count; contradictory keys fail."""
    _digest(subject_digest)
    if type(required_validators) is not frozenset or len(required_validators) > 16:
        raise WorkflowValidationError("invalid_validator_set")
    for validator in required_validators:
        validate_reference(validator)
    if type(evidence) is not tuple or len(evidence) > 32:
        raise WorkflowValidationError("invalid_evidence_set")
    seen: dict[str, ValidationEvidence] = {}
    results: dict[str, set[str]] = {validator: set() for validator in required_validators}
    for item in evidence:
        if type(item) is not ValidationEvidence:
            raise WorkflowValidationError("invalid_evidence")
        previous = seen.get(item.evidence_key)
        if previous is not None and previous != item:
            raise WorkflowValidationError("evidence_conflict")
        seen[item.evidence_key] = item
        if item.subject_digest == subject_digest and item.validator_ref in results:
            results[item.validator_ref].add(item.result)
    if not required_validators:
        return "not_requested"
    if any("failed" in values for values in results.values()):
        return "failed"
    if any(not values for values in results.values()):
        return "pending"
    if any("inconclusive" in values for values in results.values()):
        return "inconclusive"
    return "passed"


def can_release_successor(execution: str, validation: str, *, validation_required: bool) -> bool:
    _choice(execution, STEP_STATES)
    _choice(validation, VALIDATION_STATES)
    _flag(validation_required)
    return execution == "succeeded" and (not validation_required or validation == "passed")


def delivery_state(
    execution: str, validation: str, acceptance: str,
    *, validation_required: bool, acceptance_required: bool, required_outputs_ready: bool = True,
) -> str:
    _choice(execution, WORKFLOW_EXECUTION_STATES)
    _choice(validation, VALIDATION_STATES)
    _choice(acceptance, ACCEPTANCE_STATES)
    _flag(validation_required)
    _flag(acceptance_required)
    _flag(required_outputs_ready)
    if execution == "unknown":
        return "needs_reconciliation"
    if execution == "failed":
        return "execution_failed"
    if execution == "cancelled":
        return "cancelled"
    if execution != "succeeded":
        return "in_progress"
    if validation_required and validation == "failed":
        return "validation_failed"
    if not required_outputs_ready:
        return "awaiting_validation"
    if validation_required and validation != "passed":
        return "awaiting_validation"
    if acceptance == "rejected":
        return "rejected"
    if acceptance_required and acceptance != "accepted":
        return "awaiting_acceptance"
    return "delivered"
