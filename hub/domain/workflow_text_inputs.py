"""Complete bounded text values shared by explicit Workflow consumers."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from typing import Any

from .workflow_contract import parse_resolved
from .workflow_contract_types import ArtifactReference, ResolvedContract
from .workflow_errors import WorkflowValidationError


MAX_TEXT_INPUT_BYTES = 8192
TEXT_CONTENT_TYPES = frozenset({"text/plain", "text/markdown", "application/json"})


@dataclass(frozen=True, slots=True)
class WorkflowTextInput:
    ref_key: str
    reference: ArtifactReference
    content_type: str
    text: str = field(repr=False)


def artifact_input_references(resolved: ResolvedContract) -> tuple[tuple[str, ArtifactReference], ...]:
    if type(resolved) is not ResolvedContract or parse_resolved(resolved.snapshot) != resolved:
        raise WorkflowValidationError("invalid_resolved_snapshot")
    return text_artifact_references(resolved.contract.input_refs)


def text_artifact_references(named_references: tuple[Any, ...]) -> tuple[tuple[str, ArtifactReference], ...]:
    seen: dict[str, ArtifactReference] = {}
    references: list[tuple[str, ArtifactReference]] = []
    total = 0
    if type(named_references) is not tuple or len(named_references) > 16:
        raise WorkflowValidationError("invalid_input_references")
    for item in named_references:
        reference = item.reference
        if type(reference) is not ArtifactReference:
            raise WorkflowValidationError("artifact_input_unsupported")
        if seen.setdefault(reference.artifact_id, reference) != reference:
            raise WorkflowValidationError("artifact_input_mismatch")
        total += reference.size
        if total > MAX_TEXT_INPUT_BYTES:
            raise WorkflowValidationError("context_too_large")
        references.append((item.ref_key, reference))
    return tuple(references)


def text_input_value(expected: tuple[str, ArtifactReference], actual: WorkflowTextInput) -> dict[str, Any]:
    if type(actual) is not WorkflowTextInput or (actual.ref_key, actual.reference) != expected:
        raise WorkflowValidationError("artifact_input_mismatch")
    reference = actual.reference
    if type(reference) is not ArtifactReference or type(reference.size) is not int:
        raise WorkflowValidationError("artifact_input_mismatch")
    if type(actual.content_type) is not str or actual.content_type not in TEXT_CONTENT_TYPES or type(actual.text) is not str:
        raise WorkflowValidationError("artifact_input_unsupported")
    if len(actual.text) > MAX_TEXT_INPUT_BYTES:
        raise WorkflowValidationError("context_too_large")
    try:
        raw = actual.text.encode("utf-8")
    except UnicodeError as exc:
        raise WorkflowValidationError("artifact_input_invalid_utf8") from exc
    if len(raw) != reference.size or hashlib.sha256(raw).hexdigest() != reference.sha256:
        raise WorkflowValidationError("artifact_input_mismatch")
    return {"ref_key": actual.ref_key, "artifact_id": reference.artifact_id, "sha256": reference.sha256,
            "size": reference.size, "content_type": actual.content_type, "text": actual.text}


def parse_text_inputs(value: object) -> tuple[WorkflowTextInput, ...]:
    if type(value) is not list or len(value) > 16:
        raise WorkflowValidationError("invalid_text_inputs")
    inputs: list[WorkflowTextInput] = []
    for item in value:
        if (type(item) is not dict or set(item) != {"ref_key", "artifact_id", "sha256", "size", "content_type", "text"}
                or type(item["size"]) is not int):
            raise WorkflowValidationError("invalid_text_inputs")
        inputs.append(WorkflowTextInput(item["ref_key"], ArtifactReference(item["artifact_id"], item["sha256"], item["size"]),
                                        item["content_type"], item["text"]))
    return tuple(inputs)
