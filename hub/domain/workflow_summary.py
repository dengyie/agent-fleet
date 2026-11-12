"""Explicit immutable summary versions; no generation or execution authority."""
from __future__ import annotations

from dataclasses import dataclass, field, fields
import hashlib
import re
from typing import Any, Protocol

from platform_schema import validate_owner_id
from .workflow_contract_types import ContextReference, ResolvedContract
from .workflow_errors import WorkflowValidationError
from .workflow_identity import canonical_json, validate_reference
from .workflow_json import decode_snapshot

MAX_SUMMARY_BYTES = 8192
MAX_REVISION = 9007199254740991


def _text(value: object, limit: int, *, strip: bool = False) -> None:
    if type(value) is not str or not value.strip() or len(value) > limit or "\x00" in value:
        raise WorkflowValidationError("invalid_summary_value")
    if strip and value != value.strip():
        raise WorkflowValidationError("invalid_summary_value")
    try:
        if len(value.encode("utf-8")) > limit:
            raise WorkflowValidationError("invalid_summary_value")
    except UnicodeError as exc:
        raise WorkflowValidationError("invalid_summary_value") from exc


@dataclass(frozen=True, slots=True)
class WorkflowSummaryVersion:
    owner_id: str
    workspace_id: str
    summary_id: str
    revision: int
    title: str = field(repr=False)
    content: str = field(repr=False)
    created_by: str
    created_at_ms: int
    source_kind: str = "operator"

    def __post_init__(self) -> None:
        try:
            if type(self.owner_id) is not str or validate_owner_id(self.owner_id) != self.owner_id:
                raise WorkflowValidationError("invalid_summary_value")
            validate_reference(self.workspace_id)
            validate_reference(self.summary_id)
            if (type(self.revision) is not int or not 1 <= self.revision <= MAX_REVISION
                    or type(self.created_at_ms) is not int or not 0 <= self.created_at_ms <= MAX_REVISION
                    or self.created_by != self.owner_id or self.source_kind != "operator"):
                raise WorkflowValidationError("invalid_summary_value")
            _text(self.title, 160, strip=True)
            _text(self.content, MAX_SUMMARY_BYTES)
            if len(self.snapshot) > MAX_SUMMARY_BYTES:
                raise WorkflowValidationError("context_too_large")
        except (TypeError, UnicodeError, ValueError) as exc:
            if isinstance(exc, WorkflowValidationError):
                raise
            raise WorkflowValidationError("invalid_summary_value") from exc

    @property
    def snapshot(self) -> bytes:
        return canonical_json({"schema_version": 1, **{item.name: getattr(self, item.name) for item in fields(self)}})

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.snapshot).hexdigest()


def summary_value(item: WorkflowSummaryVersion) -> dict[str, Any]:
    if type(item) is not WorkflowSummaryVersion:
        raise WorkflowValidationError("invalid_summary_value")
    return decode_snapshot(item.snapshot)


def parse_summary(value: object) -> WorkflowSummaryVersion:
    names = {item.name for item in fields(WorkflowSummaryVersion)}
    if (type(value) is not dict or set(value) != names | {"schema_version"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1):
        raise WorkflowValidationError("invalid_summary_value")
    return WorkflowSummaryVersion(**{name: value[name] for name in names})


def summary_selection(references: tuple[ContextReference, ...]) -> None:
    if type(references) is not tuple or len(references) > 16:
        raise WorkflowValidationError("invalid_summary_selection")
    seen: dict[tuple[str, int], str] = {}
    for ref in references:
        if (type(ref) is not ContextReference or ref.kind != "summary" or type(ref.revision) is not int
                or not 1 <= ref.revision <= MAX_REVISION or type(ref.sha256) is not str
                or re.fullmatch("[a-f0-9]{64}", ref.sha256) is None):
            raise WorkflowValidationError("invalid_summary_selection")
        validate_reference(ref.ref_id)
        if seen.setdefault((ref.ref_id, ref.revision), ref.sha256) != ref.sha256:
            raise WorkflowValidationError("summary_revision_conflict")


def validate_context_selection(references: tuple[ContextReference, ...]) -> None:
    from .workflow_memory import memory_selection

    if (type(references) is not tuple or len(references) > 16
            or any(type(ref) is not ContextReference or ref.kind not in ("memory", "summary") for ref in references)):
        raise WorkflowValidationError("invalid_context_selection")
    memory_selection(tuple(ref for ref in references if ref.kind == "memory"))
    summary_selection(tuple(ref for ref in references if ref.kind == "summary"))


SummaryInputs = tuple[tuple[str, WorkflowSummaryVersion], ...]


def summary_input_values(resolved: ResolvedContract, inputs: SummaryInputs) -> tuple[list[dict[str, Any]], int]:
    references = tuple(named for named in resolved.contract.context_refs if named.reference.kind == "summary")
    if type(inputs) is not tuple or len(inputs) != len(references):
        raise WorkflowValidationError("summary_input_mismatch")
    values, total = [], 0
    seen: dict[tuple[str, int], WorkflowSummaryVersion] = {}
    for named, entry in zip(references, inputs):
        if type(entry) is not tuple or len(entry) != 2 or type(entry[1]) is not WorkflowSummaryVersion:
            raise WorkflowValidationError("summary_input_mismatch")
        key, item = entry
        ref = named.reference
        if ((key, item.owner_id, item.workspace_id, item.summary_id, item.revision, item.sha256)
                != (named.ref_key, resolved.owner_id, resolved.workspace_id, ref.ref_id, ref.revision, ref.sha256)
                or seen.setdefault((item.summary_id, item.revision), item) != item):
            raise WorkflowValidationError("summary_input_mismatch")
        total += len(item.snapshot)
        values.append({"ref_key": key, "summary_id": item.summary_id, "revision": item.revision,
            "sha256": item.sha256, "item": summary_value(item)})
    return values, total


def parse_summary_inputs(values: object) -> SummaryInputs:
    if type(values) is not list or len(values) > 16:
        raise WorkflowValidationError("invalid_summary_inputs")
    result = []
    for value in values:
        if (type(value) is not dict or set(value) != {"ref_key", "summary_id", "revision", "sha256", "item"}
                or type(value["revision"]) is not int):
            raise WorkflowValidationError("invalid_summary_inputs")
        item = parse_summary(value["item"])
        if (value["summary_id"], value["revision"], value["sha256"]) != (item.summary_id, item.revision, item.sha256):
            raise WorkflowValidationError("summary_input_mismatch")
        result.append((validate_reference(value["ref_key"]), item))
    return tuple(result)


class WorkflowSummaryStore(Protocol):
    def read_selected(self, owner: str, workspace: str, references: tuple[ContextReference, ...]) -> tuple[WorkflowSummaryVersion, ...]: ...
