"""Complete immutable Memory values; no clock, storage or framework authority."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from .memory import MEMORY_KINDS, MAX_CONTENT_BYTES, MAX_SOURCE, MAX_TAG_BYTES, MAX_TAGS, MAX_TITLE
from .workflow_contract_types import ContextReference
from .workflow_errors import WorkflowValidationError
from .workflow_identity import canonical_json
from .workflow_json import decode_snapshot

MAX_MEMORY_INPUT_BYTES = 8192
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FORBIDDEN_ID = ("/", "\\", "..", "token", "secret", "key", "password")


def _text(value: str, limit: int, *, required: bool = True) -> None:
    if type(value) is not str or len(value) > limit or value != value.strip() or (required and not value):
        raise WorkflowValidationError("invalid_memory_value")
    try:
        if len(value.encode("utf-8")) > limit:
            raise WorkflowValidationError("invalid_memory_value")
    except UnicodeError as exc:
        raise WorkflowValidationError("invalid_memory_value") from exc


@dataclass(frozen=True, slots=True)
class WorkflowMemoryItem:
    memory_id: str
    revision: int
    kind: str
    title: str = field(repr=False)
    content: str = field(repr=False)
    tags: tuple[str, ...] = field(repr=False)
    source: str = field(repr=False)

    def __post_init__(self) -> None:
        if (type(self.memory_id) is not str or _ID.fullmatch(self.memory_id) is None
                or any(marker in self.memory_id.lower() for marker in _FORBIDDEN_ID)
                or type(self.revision) is not int or not 0 <= self.revision <= 9007199254740991
                or type(self.kind) is not str or self.kind not in MEMORY_KINDS):
            raise WorkflowValidationError("invalid_memory_value")
        _text(self.title, MAX_TITLE)
        _text(self.content, MAX_CONTENT_BYTES)
        _text(self.source, MAX_SOURCE)
        if type(self.tags) is not tuple or len(self.tags) > MAX_TAGS:
            raise WorkflowValidationError("invalid_memory_value")
        for tag in self.tags:
            _text(tag, MAX_TAG_BYTES)
        if len(set(self.tags)) != len(self.tags):
            raise WorkflowValidationError("invalid_memory_value")

    @property
    def snapshot(self) -> bytes:
        return canonical_json({"memory_id": self.memory_id, "revision": self.revision, "kind": self.kind,
            "title": self.title, "content": self.content, "tags": list(self.tags), "source": self.source})

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.snapshot).hexdigest()


def parse_memory_item(value: object) -> WorkflowMemoryItem:
    if (type(value) is not dict or set(value) != {"memory_id", "revision", "kind", "title", "content", "tags", "source"}
            or type(value["tags"]) is not list):
        raise WorkflowValidationError("invalid_memory_value")
    return WorkflowMemoryItem(value["memory_id"], value["revision"], value["kind"], value["title"],
                              value["content"], tuple(value["tags"]), value["source"])


def memory_value(item: WorkflowMemoryItem) -> dict[str, Any]:
    if type(item) is not WorkflowMemoryItem:
        raise WorkflowValidationError("invalid_memory_value")
    return decode_snapshot(item.snapshot)


def memory_selection(references: tuple[ContextReference, ...]) -> dict[str, ContextReference]:
    if type(references) is not tuple or len(references) > 16:
        raise WorkflowValidationError("invalid_memory_selection")
    selected: dict[str, ContextReference] = {}
    for reference in references:
        if (type(reference) is not ContextReference or reference.kind != "memory" or reference.sha256 is not None
                or type(reference.ref_id) is not str or _ID.fullmatch(reference.ref_id) is None
                or any(marker in reference.ref_id.lower() for marker in _FORBIDDEN_ID)
                or type(reference.revision) is not int or not 0 <= reference.revision <= 9007199254740991):
            raise WorkflowValidationError("invalid_memory_selection")
        if selected.setdefault(reference.ref_id, reference) != reference:
            raise WorkflowValidationError("memory_revision_conflict")
    return selected


class WorkflowMemoryStore(Protocol):
    def read_selected(self, owner_id: str, references: tuple[ContextReference, ...]) -> tuple[WorkflowMemoryItem, ...]: ...
