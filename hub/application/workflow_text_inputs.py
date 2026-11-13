"""Scoped, owned artifact I/O for complete bounded Workflow text inputs."""
from __future__ import annotations

import hashlib
from typing import Any, BinaryIO, Protocol

from hub.domain.workflow_contract_types import ArtifactReference, ResolvedContract
from hub.domain.workflow_errors import WorkflowValidationError
from hub.domain.workflow_text_inputs import (
    TEXT_CONTENT_TYPES, WorkflowTextInput, artifact_input_references, text_artifact_references, text_input_value,
)


class WorkflowInputArtifacts(Protocol):
    def get(self, owner_id: str, workspace_id: str, artifact_id: str) -> dict[str, Any] | None: ...

    def open_verified(self, owner_id: str, workspace_id: str, artifact_id: str) -> BinaryIO: ...


class WorkflowTextInputs:
    def __init__(self, artifacts: WorkflowInputArtifacts) -> None:
        self.artifacts = artifacts

    def _metadata(self, resolved: ResolvedContract, reference: ArtifactReference) -> str:
        metadata = self.artifacts.get(resolved.owner_id, resolved.workspace_id, reference.artifact_id)
        if (type(metadata) is not dict or type(metadata.get("size")) is not int
                or tuple(metadata.get(key) for key in ("owner_id", "workspace_id", "artifact_id", "sha256", "size")) != (
                    resolved.owner_id, resolved.workspace_id, reference.artifact_id, reference.sha256, reference.size)):
            raise WorkflowValidationError("artifact_input_forbidden")
        content_type = metadata.get("content_type")
        if type(content_type) is not str or content_type not in TEXT_CONTENT_TYPES:
            raise WorkflowValidationError("artifact_input_unsupported")
        return content_type

    def _read(self, resolved: ResolvedContract, reference: ArtifactReference) -> tuple[str, str]:
        content_type = self._metadata(resolved, reference)
        raw = bytearray(reference.size)
        written = 0
        digest = hashlib.sha256()
        with self.artifacts.open_verified(resolved.owner_id, resolved.workspace_id, reference.artifact_id) as handle:
            while True:
                requested = reference.size - written + 1
                block = handle.read(requested)
                if type(block) is not bytes or len(block) > requested or written + len(block) > reference.size:
                    raise WorkflowValidationError("artifact_input_mismatch")
                if not block:
                    break
                raw[written:written + len(block)] = block
                written += len(block)
                digest.update(block)
        if written != reference.size or digest.hexdigest() != reference.sha256:
            raise WorkflowValidationError("artifact_input_mismatch")
        try:
            return content_type, raw.decode("utf-8")
        except UnicodeError as exc:
            raise WorkflowValidationError("artifact_input_invalid_utf8") from exc

    def collect(self, resolved: ResolvedContract, *, references=None) -> tuple[WorkflowTextInput, ...]:
        try:
            references = artifact_input_references(resolved) if references is None else text_artifact_references(references)
            cache: dict[str, tuple[str, str]] = {}
            inputs: list[WorkflowTextInput] = []
            for key, reference in references:
                prior = cache.get(reference.artifact_id)
                if prior is None:
                    prior = self._read(resolved, reference)
                    cache[reference.artifact_id] = prior
                inputs.append(WorkflowTextInput(key, reference, prior[0], prior[1]))
            return tuple(inputs)
        except WorkflowValidationError:
            raise
        except Exception as exc:
            raise WorkflowValidationError("artifact_input_unavailable") from exc

    def authorize(self, resolved: ResolvedContract, inputs: tuple[WorkflowTextInput, ...], *, references=None) -> None:
        """Saved complete bytes stay authoritative; current metadata permits use."""
        try:
            references = artifact_input_references(resolved) if references is None else text_artifact_references(references)
            if type(inputs) is not tuple or len(inputs) != len(references):
                raise WorkflowValidationError("artifact_input_mismatch")
            checked: dict[str, tuple[ArtifactReference, str]] = {}
            for expected, actual in zip(references, inputs):
                text_input_value(expected, actual)
                reference = actual.reference
                identity = (reference, actual.content_type)
                prior = checked.get(reference.artifact_id)
                if prior is None:
                    prior = (reference, self._metadata(resolved, reference))
                    checked[reference.artifact_id] = prior
                if prior != identity:
                    raise WorkflowValidationError("artifact_input_mismatch")
        except WorkflowValidationError:
            raise
        except Exception as exc:
            raise WorkflowValidationError("artifact_input_unavailable") from exc
