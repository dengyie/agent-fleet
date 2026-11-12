"""Resolve scoped current outputs in a bounded read before fenced input freeze."""
from __future__ import annotations

from typing import Protocol
from hub.platform.async_io import run_owned_io

from hub.domain.workflow_contract import parse_contract, parse_definition, parse_input_references, resolve_contract
from hub.domain.workflow_contract_types import (
    ArtifactReference, AuthorizedInputs, CatalogRef, CodeRevisionReference, InputBinding, PriorOutputReference, ResolvedContract,
)
from hub.domain.workflow_errors import WorkflowValidationError
from hub.domain.workflow_json import decode_snapshot
from hub.domain.workflow_records import WorkflowSnapshot
from hub.domain.workflow_memory import WorkflowMemoryStore
from hub.domain.workflow_summary import WorkflowSummaryStore, validate_context_selection
from hub.platform.artifacts import ArtifactError
from .workflow_inputs import ArtifactMetadataStore
from .workflow_repository import WorkflowRepository, WorkflowRepositoryError


class InputArtifacts(ArtifactMetadataStore, Protocol):
    def read(self, owner_id: str, workspace_id: str, artifact_id: str) -> bytes: ...


class WorkflowArtifactInputResolver:
    def __init__(self, repository: WorkflowRepository, artifacts: InputArtifacts, *, memories: WorkflowMemoryStore | None = None,
                 summaries: WorkflowSummaryStore | None = None) -> None:
        self.repository = repository
        self.artifacts = artifacts
        self.memories = memories
        self.summaries = summaries

    async def resolve(self, snapshot: WorkflowSnapshot, step_key: str, adapter_ref: CatalogRef) -> ResolvedContract:
        try:
            return await run_owned_io(self._resolve, snapshot, step_key, adapter_ref)
        except (ArtifactError, WorkflowValidationError) as exc:
            raise WorkflowRepositoryError("reference_forbidden") from exc

    def _authorize(self, owner: str, workspace: str, reference: ArtifactReference) -> None:
        manifest = self.artifacts.get(owner, workspace, reference.artifact_id)
        if manifest is None or tuple(manifest.get(key) for key in ("owner_id", "workspace_id", "artifact_id", "sha256", "size")) != (
                owner, workspace, reference.artifact_id, reference.sha256, reference.size):
            raise WorkflowRepositoryError("reference_forbidden")

    def _revision(self, owner: str, workspace: str, artifact: ArtifactReference) -> CodeRevisionReference:
        self._authorize(owner, workspace, artifact)
        if artifact.size > 65536:
            raise WorkflowRepositoryError("reference_forbidden")
        payload = decode_snapshot(self.artifacts.read(owner, workspace, artifact.artifact_id))
        if set(payload) != {"schema_version", "source_revision", "executor_id"} or type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
            raise WorkflowRepositoryError("reference_forbidden")
        references = parse_input_references([payload["source_revision"]])
        reference = references[0].reference
        if type(reference) is not CodeRevisionReference:
            raise WorkflowRepositoryError("reference_forbidden")
        self._authorize(owner, workspace, reference.source_bundle)
        return reference

    def _resolve(self, snapshot: WorkflowSnapshot, step_key: str, adapter_ref: CatalogRef) -> ResolvedContract:
        owner, workspace, workflow = snapshot.workflow.owner_id, snapshot.workflow.workspace_id, snapshot.workflow.workflow_id
        definition = parse_definition(snapshot.workflow.definition_snapshot)
        steps = {step.step_key: step for step in snapshot.steps}
        if step_key not in steps:
            raise WorkflowRepositoryError("unknown_step")
        step = steps[step_key]
        if step.contract_snapshot is None:
            raise WorkflowRepositoryError("invalid_workflow_snapshot")
        contract = parse_contract(step.contract_snapshot)
        if contract.context_refs:
            if step.kind not in ("external_agent", "native_run"):
                raise WorkflowRepositoryError("context_resolution_required")
            validate_context_selection(tuple(item.reference for item in contract.context_refs))
            memory_refs = tuple(named.reference for named in contract.context_refs if named.reference.kind == "memory")
            summary_refs = tuple(named.reference for named in contract.context_refs if named.reference.kind == "summary")
            if memory_refs:
                if self.memories is None:
                    raise WorkflowRepositoryError("context_resolution_required")
                self.memories.read_selected(owner, memory_refs)
            if summary_refs:
                if self.summaries is None:
                    raise WorkflowRepositoryError("context_resolution_required")
                self.summaries.read_selected(owner, workspace, summary_refs)
        positions = {item.step_key: index for index, item in enumerate(definition.steps)}
        with self.repository.transaction(write=False) as connection:
            current = connection.execute("SELECT r.revision,m.minimum_reader_version FROM workflow_runs r CROSS JOIN workflow_schema_meta m "
                "WHERE owner_id=? AND workspace_id=? AND workflow_id=? AND m.singleton=1", (owner, workspace, workflow)).fetchone()
            if current is None or current[0] != snapshot.workflow.revision:
                raise WorkflowRepositoryError("revision_conflict")
            if contract.schema_version == 2 and current[1] < 6:
                raise WorkflowRepositoryError("invalid_contract_reader")
            rows = connection.execute(
                "SELECT a.step_key,a.output_key,a.artifact_id,a.sha256,a.size,s.state FROM workflow_artifact_refs a "
                "JOIN workflow_steps s USING(owner_id,workspace_id,workflow_id,step_key,generation) "
                "WHERE a.owner_id=? AND a.workspace_id=? AND a.workflow_id=? AND s.is_current=1 AND s.position<? "
                "AND a.output_key LIKE 'output:%' LIMIT 129", (owner, workspace, workflow, positions[step_key])).fetchall()
        if len(rows) > 128:
            raise WorkflowRepositoryError("output_limit")
        outputs = {(row["step_key"], row["output_key"][7:]): row for row in rows}
        kinds = {(item.step_key, spec.output_key): spec.kind for item in definition.steps for spec in item.contract.output_specs}
        bindings: list[InputBinding] = []
        for named in contract.input_refs + contract.context_refs:
            reference, source = named.reference, None
            if type(reference) is PriorOutputReference:
                source = reference
                row = outputs.get((reference.step_key, reference.output_key))
                if row is None or row["state"] != "succeeded":
                    raise WorkflowRepositoryError("output_unavailable")
                artifact = ArtifactReference(row["artifact_id"], row["sha256"], row["size"])
                reference = self._revision(owner, workspace, artifact) if kinds[(reference.step_key, reference.output_key)] == "code_revision" else artifact
            if type(reference) is ArtifactReference:
                self._authorize(owner, workspace, reference)
            elif type(reference) is CodeRevisionReference:
                self._authorize(owner, workspace, reference.source_bundle)
            bindings.append(InputBinding(named.ref_key, owner, workspace, reference, source))
        return resolve_contract(definition, AuthorizedInputs(owner, workspace, step_key, adapter_ref, tuple(bindings)), contract_override=contract)
