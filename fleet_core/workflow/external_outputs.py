"""Exact immutable external exports and independently verified source references."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .external_agent import ExternalTaskRef
from .contract import parse_input_references, reference_values
from .contract_types import ArtifactReference, CodeRevisionReference, NamedReference, ResolvedContract
from .errors import WorkflowValidationError
from .identity import canonical_digest, canonical_json, dispatch_token, validate_reference
from .json import decode_snapshot

MAX_OUTPUT_BYTES = 256 * 1024 * 1024
MAX_RESULT_BYTES = 65536


@dataclass(frozen=True, slots=True)
class ExternalArtifactOutput:
    output_key: str
    artifact: ArtifactReference

    def __post_init__(self) -> None:
        validate_reference(self.output_key)
        if type(self.artifact) is not ArtifactReference:
            raise WorkflowValidationError("invalid_external_output")
        reference_values((NamedReference(self.output_key, self.artifact),))
        if self.artifact.size > MAX_OUTPUT_BYTES:
            raise WorkflowValidationError("external_output_too_large")


@dataclass(frozen=True, slots=True)
class ExternalOutputSet:
    workflow_id: str
    step_key: str
    generation: int
    task: ExternalTaskRef
    outputs: tuple[ExternalArtifactOutput, ...]

    def __post_init__(self) -> None:
        validate_reference(self.workflow_id)
        validate_reference(self.step_key)
        if type(self.generation) is not int or not 1 <= self.generation <= 9007199254740991:
            raise WorkflowValidationError("invalid_generation")
        if type(self.task) is not ExternalTaskRef or not (self.task.external_id or self.task.session_id):
            raise WorkflowValidationError("external_task_mismatch")
        if type(self.outputs) is not tuple or len(self.outputs) > 16:
            raise WorkflowValidationError("invalid_external_outputs")
        keys: set[str] = set()
        identities: dict[str, ArtifactReference] = {}
        for output in self.outputs:
            if type(output) is not ExternalArtifactOutput or output.output_key in keys:
                raise WorkflowValidationError("duplicate_output_key")
            keys.add(output.output_key)
            artifact = output.artifact
            if identities.setdefault(artifact.artifact_id, artifact) != artifact:
                raise WorkflowValidationError("external_output_conflict")
        if sum(item.size for item in identities.values()) > MAX_OUTPUT_BYTES:
            raise WorkflowValidationError("external_output_too_large")
        object.__setattr__(self, "outputs", tuple(sorted(self.outputs, key=lambda item: item.output_key)))

    def payload(self) -> dict[str, Any]:
        return {"schema_version": 1, "workflow_id": self.workflow_id, "step_key": self.step_key,
                "generation": self.generation, "task": asdict(self.task),
                "outputs": [asdict(output) for output in self.outputs]}

    @property
    def snapshot(self) -> bytes:
        raw = canonical_json(self.payload())
        if len(raw) > 16384:
            raise WorkflowValidationError("external_output_too_large")
        return raw


def validate_external_outputs(result: ExternalOutputSet, resolved: ResolvedContract, task: ExternalTaskRef) -> None:
    if (type(result) is not ExternalOutputSet or type(resolved) is not ResolvedContract
            or result.task != task or result.step_key != resolved.step_key):
        raise WorkflowValidationError("external_task_mismatch")
    if task.dispatch_token != dispatch_token(resolved.owner_id, result.workflow_id, result.step_key, result.generation):
        raise WorkflowValidationError("external_task_mismatch")
    specs = {spec.output_key: spec for spec in resolved.contract.output_specs}
    present = {output.output_key for output in result.outputs}
    if any(spec.required and spec.output_key not in present for spec in specs.values()):
        raise WorkflowValidationError("external_output_missing")
    for output in result.outputs:
        spec = specs.get(output.output_key)
        if spec is None:
            raise WorkflowValidationError("external_output_not_declared")
        limit = spec.max_bytes if spec.kind == "artifact" else MAX_RESULT_BYTES
        if output.artifact.size > limit:
            raise WorkflowValidationError("external_output_too_large")
    result.snapshot


def parse_external_outputs(raw: bytes, resolved: ResolvedContract, task: ExternalTaskRef) -> ExternalOutputSet:
    value = decode_snapshot(raw, max_bytes=16384)
    if (set(value) != {"schema_version", "workflow_id", "step_key", "generation", "task", "outputs"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or type(value["task"]) is not dict or set(value["task"]) != set(ExternalTaskRef.__dataclass_fields__)
            or type(value["outputs"]) is not list or len(value["outputs"]) > 16):
        raise WorkflowValidationError("invalid_external_outputs")
    outputs: list[ExternalArtifactOutput] = []
    for item in value["outputs"]:
        if (type(item) is not dict or set(item) != {"output_key", "artifact"}
                or type(item["artifact"]) is not dict or set(item["artifact"]) != {"artifact_id", "sha256", "size"}):
            raise WorkflowValidationError("invalid_external_output")
        outputs.append(ExternalArtifactOutput(item["output_key"], ArtifactReference(**item["artifact"])))
    if value["task"] != asdict(task):
        raise WorkflowValidationError("external_task_mismatch")
    result = ExternalOutputSet(value["workflow_id"], value["step_key"], value["generation"], task, tuple(outputs))
    validate_external_outputs(result, resolved, task)
    if result.snapshot != raw:
        raise WorkflowValidationError("noncanonical_external_outputs")
    return result


@dataclass(frozen=True, slots=True)
class VerifiedExternalOutputs:
    export: ExternalOutputSet
    source_revisions: tuple[NamedReference, ...] = ()

    def __post_init__(self) -> None:
        if type(self.export) is not ExternalOutputSet or type(self.source_revisions) is not tuple:
            raise WorkflowValidationError("invalid_external_output_proof")
        reference_values(self.source_revisions)
        if any(type(item.reference) is not CodeRevisionReference for item in self.source_revisions):
            raise WorkflowValidationError("invalid_external_output_proof")
        keys = {item.ref_key for item in self.source_revisions}
        if len(keys) != len(self.source_revisions) or not keys <= {item.output_key for item in self.export.outputs}:
            raise WorkflowValidationError("external_output_conflict")
        object.__setattr__(self, "source_revisions", tuple(sorted(self.source_revisions, key=lambda item: item.ref_key)))
        artifacts = {item.artifact.artifact_id: item.artifact for item in self.export.outputs}
        for source in self.source_revisions:
            bundle = source.reference.source_bundle
            if not 1 <= bundle.size <= 10 * 1024 * 1024 or artifacts.setdefault(bundle.artifact_id, bundle) != bundle:
                raise WorkflowValidationError("external_output_conflict")
        if sum(item.size for item in artifacts.values()) > MAX_OUTPUT_BYTES:
            raise WorkflowValidationError("external_output_too_large")

    def payload(self) -> dict[str, Any]:
        return {"export": self.export.payload(), "source_revisions": reference_values(self.source_revisions)}

    @property
    def snapshot(self) -> bytes:
        raw = canonical_json(self.payload())
        if len(raw) > 16384:
            raise WorkflowValidationError("external_output_too_large")
        return raw

    def subject_digest(self, resolved_digest: str) -> str:
        return canonical_digest({"schema_version": 1, "resolved_contract_digest": resolved_digest,
            "outputs": [asdict(output) for output in self.export.outputs],
            "source_revisions": reference_values(self.source_revisions)})

    def artifact_refs(self) -> tuple[tuple[str, ArtifactReference], ...]:
        return tuple(("output:" + item.output_key, item.artifact) for item in self.export.outputs) + tuple(
            ("external-source:" + item.ref_key, item.reference.source_bundle) for item in self.source_revisions)


def parse_external_output_proof(raw: bytes, resolved: ResolvedContract, task: ExternalTaskRef) -> VerifiedExternalOutputs:
    value = decode_snapshot(raw, max_bytes=16384)
    if set(value) != {"export", "source_revisions"}:
        raise WorkflowValidationError("invalid_external_output_proof")
    exported = parse_external_outputs(canonical_json(value["export"]), resolved, task)
    sources = parse_input_references(value["source_revisions"])
    proof = VerifiedExternalOutputs(exported, sources)
    specs = {spec.output_key: spec for spec in resolved.contract.output_specs}
    required = {output.output_key for output in exported.outputs if specs[output.output_key].kind == "code_revision"}
    if {source.ref_key for source in sources} != required or proof.snapshot != raw:
        raise WorkflowValidationError("invalid_external_output_proof")
    return proof
