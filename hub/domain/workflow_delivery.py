"""Immutable delivery writes bound to one execution and one source generation."""
from __future__ import annotations

from dataclasses import dataclass

from .workflow_contract import reference_values
from .workflow_contract_types import ArtifactReference, CodeRevisionReference, NamedReference, ResolvedContract, WorkContract
from .workflow_control import exact_counter
from .workflow_errors import WorkflowValidationError
from .workflow_execution import ExecutionBinding
from .workflow_identity import canonical_digest, digest_value, validate_reference


@dataclass(frozen=True, slots=True)
class DeliveryRunLease:
    owner_id: str
    run_id: str
    lease_id: str
    worker_id: str
    attempt: int

    def __post_init__(self) -> None:
        for value in (self.owner_id, self.run_id, self.lease_id, self.worker_id):
            validate_reference(value)
        exact_counter(self.attempt)


@dataclass(frozen=True, slots=True)
class DeliveryTaskLease:
    task_id: str
    attempt_id: str
    nonce: str
    machine: str
    owner_id: str
    run_id: str

    def __post_init__(self) -> None:
        for value in (self.task_id, self.attempt_id, self.nonce, self.machine, self.owner_id, self.run_id):
            validate_reference(value)


@dataclass(frozen=True, slots=True)
class DeliveryTarget:
    workflow_id: str
    step_key: str
    generation: int
    expected_revision: int
    binding: ExecutionBinding
    run_lease: DeliveryRunLease | None = None
    task_lease: DeliveryTaskLease | None = None

    def __post_init__(self) -> None:
        validate_reference(self.workflow_id)
        validate_reference(self.step_key)
        exact_counter(self.generation)
        exact_counter(self.expected_revision)
        if type(self.binding) is not ExecutionBinding:
            raise WorkflowValidationError("invalid_delivery_binding")
        if self.run_lease is not None and (type(self.run_lease) is not DeliveryRunLease or
                self.binding.kind != "run" or (self.run_lease.owner_id, self.run_lease.run_id) != (
                    self.binding.owner_id, self.binding.run_id)):
            raise WorkflowValidationError("invalid_delivery_lease")
        if self.task_lease is not None and (type(self.task_lease) is not DeliveryTaskLease or self.run_lease is not None or
                self.binding.kind != "run" or (self.task_lease.owner_id, self.task_lease.run_id) != (
                    self.binding.owner_id, self.binding.run_id)):
            raise WorkflowValidationError("invalid_delivery_lease")


@dataclass(frozen=True, slots=True)
class DeliveryOutput:
    output_key: str
    kind: str
    artifact: ArtifactReference

    def __post_init__(self) -> None:
        validate_reference(self.output_key)
        if self.kind not in ("artifact", "code_revision"):
            raise WorkflowValidationError("invalid_output_kind")
        reference_values((NamedReference(self.output_key, self.artifact),))


@dataclass(frozen=True, slots=True)
class DeliveryEvidence:
    validator_ref: str
    result: str
    source: str
    report: ArtifactReference
    content_digest: str

    def __post_init__(self) -> None:
        validate_reference(self.validator_ref)
        validate_reference(self.source)
        digest_value(self.content_digest)
        reference_values((NamedReference("report", self.report),))
        if self.result not in ("passed", "failed", "inconclusive"):
            raise WorkflowValidationError("invalid_evidence_result")


@dataclass(frozen=True, slots=True)
class DeliveryContext:
    contract: WorkContract
    resolved: ResolvedContract
    source_generation: int | None
    source_subject: str | None
    source_outputs: tuple[DeliveryOutput, ...]
    semantic_inputs: bytes
    active_deadline: float | None = None
    prerequisite_reports: tuple[ArtifactReference, ...] = ()


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    subject_digest: str
    outputs: tuple[DeliveryOutput, ...]
    evidence: DeliveryEvidence | None = None
    source_generation: int | None = None

    def __post_init__(self) -> None:
        digest_value(self.subject_digest)
        if type(self.outputs) is not tuple or not 1 <= len(self.outputs) <= 16 or any(type(item) is not DeliveryOutput for item in self.outputs):
            raise WorkflowValidationError("invalid_delivery_outputs")
        if len({item.output_key for item in self.outputs}) != len(self.outputs):
            raise WorkflowValidationError("duplicate_output_key")
        if self.evidence is not None and type(self.evidence) is not DeliveryEvidence:
            raise WorkflowValidationError("invalid_delivery_evidence")
        if self.source_generation is not None:
            exact_counter(self.source_generation)


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    revision: int
    subject_digest: str
    outputs: tuple[DeliveryOutput, ...]
    evidence_key: str | None


def source_subject(reference: CodeRevisionReference, outputs: tuple[DeliveryOutput, ...], semantic_inputs: object) -> str:
    reference_values((NamedReference("source", reference),))
    if len({item.output_key for item in outputs}) != len(outputs):
        raise WorkflowValidationError("duplicate_output_key")
    return canonical_digest({"schema_version": 1, "source_revision": {
        "repository_id": reference.repository_id, "base_commit": reference.base_commit,
        "head_commit": reference.head_commit, "tree_id": reference.tree_id},
        "outputs": [{"output_key": item.output_key, "artifact_id": item.artifact.artifact_id,
                     "sha256": item.artifact.sha256, "size": item.artifact.size} for item in sorted(outputs, key=lambda item: item.output_key)],
        "semantic_inputs": semantic_inputs})


def delivery_operation_token(target: DeliveryTarget, operation: str) -> str:
    validate_reference(operation)
    return "delivery:" + canonical_digest({"token": target.binding.token, "operation": operation})
