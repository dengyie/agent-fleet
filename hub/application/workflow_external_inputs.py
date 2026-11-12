"""Scoped artifact and Memory I/O for complete bounded private requests."""
from __future__ import annotations

from hub.domain.external_request import (
    ExternalAgentRequest, ExternalMemoryInput, build_external_request,
    external_input_references, parse_external_request,
)
from hub.domain.workflow_contract_types import ResolvedContract
from hub.domain.workflow_errors import WorkflowValidationError
from hub.domain.workflow_json import decode_snapshot
from hub.domain.workflow_memory import WorkflowMemoryStore, parse_memory_item
from hub.domain.workflow_summary import SummaryInputs, WorkflowSummaryStore, parse_summary_inputs
from hub.domain.workflow_text_inputs import parse_text_inputs
from .workflow_dispatch import WorkflowDispatchError
from .workflow_text_inputs import WorkflowInputArtifacts, WorkflowTextInputs

ExternalInputArtifacts = WorkflowInputArtifacts


class WorkflowExternalInputs:
    def __init__(self, artifacts: ExternalInputArtifacts, *, memories: WorkflowMemoryStore | None = None,
                 summaries: WorkflowSummaryStore | None = None) -> None:
        self.texts = WorkflowTextInputs(artifacts)
        self.memories = memories
        self.summaries = summaries

    def _memories(self, resolved: ResolvedContract) -> tuple[ExternalMemoryInput, ...]:
        references = tuple(named for named in resolved.contract.context_refs if named.reference.kind == "memory")
        if not references:
            return ()
        if self.memories is None:
            raise WorkflowValidationError("external_input_unavailable")
        items = self.memories.read_selected(resolved.owner_id, tuple(named.reference for named in references))
        if len(items) != len(references):
            raise WorkflowValidationError("external_input_mismatch")
        return tuple(ExternalMemoryInput(named.ref_key, item) for named, item in zip(references, items))

    def _summaries(self, resolved: ResolvedContract) -> SummaryInputs:
        references = tuple(named for named in resolved.contract.context_refs if named.reference.kind == "summary")
        if not references:
            return ()
        if self.summaries is None:
            raise WorkflowValidationError("external_input_unavailable")
        items = self.summaries.read_selected(resolved.owner_id, resolved.workspace_id, tuple(named.reference for named in references))
        if len(items) != len(references):
            raise WorkflowValidationError("external_input_mismatch")
        return tuple((named.ref_key, item) for named, item in zip(references, items))

    @staticmethod
    def _code(exc: Exception) -> str:
        if not isinstance(exc, WorkflowValidationError) or exc.code == "artifact_input_invalid_utf8":
            return "external_input_unavailable"
        return exc.code.replace("artifact_input_", "external_input_", 1)

    @staticmethod
    def _cause(exc: Exception) -> BaseException:
        if (isinstance(exc, WorkflowValidationError)
                and exc.code in {"artifact_input_unavailable", "artifact_input_invalid_utf8"}
                and exc.__cause__ is not None):
            return exc.__cause__
        return exc

    def collect(self, resolved: ResolvedContract) -> ExternalAgentRequest:
        try:
            external_input_references(resolved)
            return build_external_request(resolved, self.texts.collect(resolved), memories=self._memories(resolved), summaries=self._summaries(resolved))
        except Exception as exc:
            raise WorkflowDispatchError(self._code(exc), uncertain=False) from self._cause(exc)

    def authorize(self, resolved: ResolvedContract, request: ExternalAgentRequest) -> None:
        try:
            original = parse_external_request(request.snapshot, resolved)
            if not resolved.contract.input_refs and not resolved.contract.context_refs:
                return
            # Saved content is authoritative; current authority only permits sends.
            prompt = decode_snapshot(original.prompt.encode("utf-8"))
            self.texts.authorize(resolved, parse_text_inputs(prompt["text_inputs"]))
            if resolved.contract.context_refs:
                current = self._memories(resolved)
                saved = tuple(ExternalMemoryInput(value["ref_key"], parse_memory_item(value["item"])) for value in prompt.get("memory_inputs", []))
                if current != saved:
                    raise WorkflowValidationError("memory_selection_stale")
                if self._summaries(resolved) != parse_summary_inputs(prompt.get("summary_inputs", [])):
                    raise WorkflowValidationError("summary_selection_stale")
        except Exception as exc:
            raise WorkflowDispatchError(self._code(exc), uncertain=False) from self._cause(exc)
