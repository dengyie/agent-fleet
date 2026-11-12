"""Canonical private requests with complete bounded text and Memory evidence."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .workflow_contract import parse_resolved
from .workflow_contract_types import (
    ArtifactReference,
    ContextReference,
    ResolvedContract,
)
from .workflow_errors import WorkflowValidationError
from .workflow_identity import canonical_json
from .workflow_json import decode_snapshot
from .workflow_memory import WorkflowMemoryItem, memory_value, parse_memory_item
from .workflow_summary import (
    SummaryInputs,
    parse_summary_inputs,
    summary_input_values,
    validate_context_selection,
)
from .workflow_text_inputs import (
    MAX_TEXT_INPUT_BYTES,
    WorkflowTextInput,
    artifact_input_references,
    text_input_value,
)

MAX_INPUT_BYTES = MAX_TEXT_INPUT_BYTES
MAX_PROMPT_CHARS = 8192
MAX_REQUEST_BYTES = 65536
ExternalTextInput = WorkflowTextInput


@dataclass(frozen=True, slots=True)
class ExternalMemoryInput:
    ref_key: str
    item: WorkflowMemoryItem = field(repr=False)


@dataclass(frozen=True, slots=True)
class ExternalAgentRequest:
    prompt: str = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.prompt) is not str or not self.prompt or len(self.prompt) > MAX_PROMPT_CHARS:
            raise WorkflowValidationError("context_too_large")
        try:
            encoded = self.snapshot
        except UnicodeError as exc:
            raise WorkflowValidationError("invalid_external_request") from exc
        if len(encoded) > MAX_REQUEST_BYTES:
            raise WorkflowValidationError("context_too_large")

    @property
    def snapshot(self) -> bytes:
        return canonical_json({"schema_version": 1, "prompt": self.prompt})


def external_input_references(resolved: ResolvedContract) -> tuple[tuple[str, ArtifactReference], ...]:
    if type(resolved) is not ResolvedContract or parse_resolved(resolved.snapshot) != resolved:
        raise WorkflowValidationError("invalid_resolved_snapshot")
    contract = resolved.contract
    if any(type(item.reference) is not ContextReference for item in contract.context_refs):
        raise WorkflowValidationError("external_contract_unsupported")
    validate_context_selection(tuple(item.reference for item in contract.context_refs))
    try:
        references = artifact_input_references(resolved)
    except WorkflowValidationError as exc:
        code = "external_contract_unsupported" if exc.code == "artifact_input_unsupported" else exc.code.replace("artifact_input_", "external_input_", 1)
        raise WorkflowValidationError(code) from exc
    if len(resolved.snapshot.decode("utf-8")) > MAX_PROMPT_CHARS:
        raise WorkflowValidationError("context_too_large")
    return references


def _text_value(expected: tuple[str, ArtifactReference], actual: ExternalTextInput) -> dict[str, Any]:
    try:
        return text_input_value(expected, actual)
    except WorkflowValidationError as exc:
        raise WorkflowValidationError(exc.code.replace("artifact_input_", "external_input_", 1)) from exc


def _memory_values(resolved: ResolvedContract, inputs: tuple[ExternalMemoryInput, ...]) -> tuple[list[dict[str, Any]], int]:
    references = tuple(named for named in resolved.contract.context_refs if named.reference.kind == "memory")
    if type(inputs) is not tuple or len(inputs) != len(references):
        raise WorkflowValidationError("external_input_mismatch")
    values: list[dict[str, Any]] = []
    seen: dict[str, WorkflowMemoryItem] = {}
    total = 0
    for named, actual in zip(references, inputs):
        if type(actual) is not ExternalMemoryInput or type(actual.item) is not WorkflowMemoryItem:
            raise WorkflowValidationError("external_input_mismatch")
        item, reference = actual.item, named.reference
        if (actual.ref_key, item.memory_id, item.revision) != (named.ref_key, reference.ref_id, reference.revision):
            raise WorkflowValidationError("external_input_mismatch")
        if seen.setdefault(item.memory_id, item) != item:
            raise WorkflowValidationError("external_input_mismatch")
        total += len(item.snapshot)
        if total > MAX_INPUT_BYTES:
            raise WorkflowValidationError("context_too_large")
        values.append({"ref_key": actual.ref_key, "memory_id": item.memory_id, "revision": item.revision,
                       "sha256": item.sha256, "item": memory_value(item)})
    return values, total


def build_external_request(resolved: ResolvedContract, inputs: tuple[ExternalTextInput, ...] = (), *,
                           memories: tuple[ExternalMemoryInput, ...] = (), summaries: SummaryInputs = ()) -> ExternalAgentRequest:
    references = external_input_references(resolved)
    if type(inputs) is not tuple or len(inputs) != len(references):
        raise WorkflowValidationError("external_input_mismatch")
    memory_values, memory_bytes = _memory_values(resolved, memories)
    summary_values, summary_bytes = summary_input_values(resolved, summaries)
    if sum(reference.size for _key, reference in references) + memory_bytes + summary_bytes > MAX_INPUT_BYTES:
        raise WorkflowValidationError("context_too_large")
    if not references and not memory_values and not summary_values:
        return ExternalAgentRequest(resolved.snapshot.decode("utf-8"))
    values: list[dict[str, Any]] = []
    content_types: dict[str, str] = {}
    for expected, actual in zip(references, inputs):
        value = _text_value(expected, actual)
        if content_types.setdefault(actual.reference.artifact_id, actual.content_type) != actual.content_type:
            raise WorkflowValidationError("external_input_mismatch")
        values.append(value)
    payload = {"schema_version": 3 if summary_values else (2 if memory_values else 1), "resolved_contract": decode_snapshot(resolved.snapshot), "text_inputs": values}
    if memory_values:
        payload["memory_inputs"] = memory_values
    if summary_values:
        payload["summary_inputs"] = summary_values
    return ExternalAgentRequest(canonical_json(payload).decode("utf-8"))


def _parse_text_inputs(values: object) -> tuple[ExternalTextInput, ...]:
    if type(values) is not list or len(values) > 16:
        raise WorkflowValidationError("external_input_mismatch")
    inputs: list[ExternalTextInput] = []
    for item in values:
        if (type(item) is not dict or set(item) != {"ref_key", "artifact_id", "sha256", "size", "content_type", "text"}
                or type(item["size"]) is not int):
            raise WorkflowValidationError("invalid_external_request")
        inputs.append(ExternalTextInput(item["ref_key"], ArtifactReference(item["artifact_id"], item["sha256"], item["size"]), item["content_type"], item["text"]))
    return tuple(inputs)


def _parse_memory_inputs(values: object) -> tuple[ExternalMemoryInput, ...]:
    if type(values) is not list or len(values) > 16:
        raise WorkflowValidationError("external_input_mismatch")
    inputs: list[ExternalMemoryInput] = []
    for value in values:
        if (type(value) is not dict or set(value) != {"ref_key", "memory_id", "revision", "sha256", "item"}
                or type(value["revision"]) is not int):
            raise WorkflowValidationError("invalid_external_request")
        item = parse_memory_item(value["item"])
        if (value["memory_id"], value["revision"], value["sha256"]) != (item.memory_id, item.revision, item.sha256):
            raise WorkflowValidationError("external_input_mismatch")
        inputs.append(ExternalMemoryInput(value["ref_key"], item))
    return tuple(inputs)


def parse_external_request(raw: bytes, resolved: ResolvedContract) -> ExternalAgentRequest:
    value = decode_snapshot(raw, max_bytes=MAX_REQUEST_BYTES)
    if set(value) != {"schema_version", "prompt"} or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise WorkflowValidationError("invalid_external_request")
    request = ExternalAgentRequest(value["prompt"])
    references = external_input_references(resolved)
    has_memories = any(named.reference.kind == "memory" for named in resolved.contract.context_refs)
    has_summaries = any(named.reference.kind == "summary" for named in resolved.contract.context_refs)
    if not references and not has_memories and not has_summaries:
        expected = build_external_request(resolved)
    else:
        prompt = decode_snapshot(request.prompt.encode("utf-8"), max_bytes=MAX_REQUEST_BYTES)
        fields = {"schema_version", "resolved_contract", "text_inputs"} | ({"memory_inputs"} if has_memories else set())
        if has_summaries:
            fields.add("summary_inputs")
        if (set(prompt) != fields or type(prompt["schema_version"]) is not int
                or prompt["schema_version"] != (3 if has_summaries else (2 if has_memories else 1))
                or prompt["resolved_contract"] != decode_snapshot(resolved.snapshot)):
            raise WorkflowValidationError("invalid_external_request")
        memories = _parse_memory_inputs(prompt["memory_inputs"]) if has_memories else ()
        summaries = parse_summary_inputs(prompt["summary_inputs"]) if has_summaries else ()
        expected = build_external_request(resolved, _parse_text_inputs(prompt["text_inputs"]), memories=memories, summaries=summaries)
    if request != expected or request.snapshot != raw:
        raise WorkflowValidationError("noncanonical_external_request")
    return request
