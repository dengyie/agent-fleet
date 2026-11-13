"""Immutable execution identities and observations; no transport or clock."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .external_outputs import VerifiedExternalOutputs
from .workflow_contract_types import CatalogRef, ResolvedContract
from .workflow_errors import WorkflowValidationError
from .workflow_identity import validate_reference

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
OBSERVATION_STATES = frozenset({
    "running", "waiting_node", "waiting_external", "waiting_approval", "paused",
    "cancelling", "succeeded", "failed", "unknown", "cancelled",
})


@dataclass(frozen=True, slots=True)
class HubInputTarget:
    """Trusted local consumer identity, never an operator or contract option."""

    owner_id: str
    workspace_id: str
    target_key: str
    adapter_ref: CatalogRef
    policy_ref: CatalogRef
    kind: str

    def __post_init__(self) -> None:
        for value in (self.owner_id, self.workspace_id, self.target_key):
            validate_reference(value)
        for ref in (self.adapter_ref, self.policy_ref):
            if type(ref) is not CatalogRef or type(ref.revision) is not int or not 1 <= ref.revision <= 9007199254740991:
                raise WorkflowValidationError("invalid_hub_input_target")
            validate_reference(ref.id)
        if type(self.kind) is not str or self.kind not in {"coding_task", "validation", "native_run"}:
            raise WorkflowValidationError("invalid_hub_input_target")


@dataclass(frozen=True, slots=True)
class ExecutorCapabilities:
    idempotent: bool
    lookup: bool
    cancel: bool = False

    def __post_init__(self) -> None:
        if any(type(flag) is not bool for flag in (self.idempotent, self.lookup, self.cancel)):
            raise WorkflowValidationError("invalid_executor_capabilities")


@dataclass(frozen=True, slots=True)
class ExecutionBinding:
    owner_id: str
    workspace_id: str
    target_key: str
    token: str
    payload_hash: str
    kind: str
    execution_id: str
    run_id: str | None = None

    def __post_init__(self) -> None:
        for value in (self.owner_id, self.workspace_id, self.target_key, self.execution_id):
            validate_reference(value)
        if self.run_id is not None:
            validate_reference(self.run_id)
        if self.kind not in ("run", "task", "external"):
            raise WorkflowValidationError("invalid_binding_kind")
        if self.kind == "run" and self.run_id != self.execution_id:
            raise WorkflowValidationError("invalid_run_binding")
        if any(type(value) is not str or not _DIGEST.fullmatch(value) for value in (self.token, self.payload_hash)):
            raise WorkflowValidationError("invalid_binding_digest")


@dataclass(frozen=True, slots=True)
class ExecutionObservation:
    binding: ExecutionBinding
    state: str
    reason_code: str | None = None
    task_id: str | None = None
    attempt_id: str | None = None
    evidence_source: str | None = None
    external_outputs: VerifiedExternalOutputs | None = None

    def __post_init__(self) -> None:
        if type(self.binding) is not ExecutionBinding or type(self.state) is not str or self.state not in OBSERVATION_STATES:
            raise WorkflowValidationError("invalid_execution_observation")
        for value in (self.reason_code, self.task_id, self.attempt_id):
            if value is not None:
                validate_reference(value)
        if self.evidence_source is not None and (self.binding.kind != "external" or type(self.evidence_source) is not str or
                self.evidence_source not in {"none", "transport", "accepted", "agent_reported", "confirmed", "unknown"}):
            raise WorkflowValidationError("invalid_execution_evidence_source")
        if self.external_outputs is not None and (type(self.external_outputs) is not VerifiedExternalOutputs
                or self.binding.kind != "external" or self.state != "succeeded"
                or self.evidence_source not in {"agent_reported", "confirmed"}):
            raise WorkflowValidationError("invalid_external_output_proof")


@dataclass(frozen=True, slots=True)
class DispatchRecord:
    workflow_id: str
    step_key: str
    generation: int
    stage: str
    token: str
    payload_hash: str
    target_key: str
    resolved: ResolvedContract
    binding: ExecutionBinding | None
    revision: int
    step_state: str
    recovered: bool = False
