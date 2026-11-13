"""One bounded owner-scoped read snapshot of explicitly selected Memory rows."""
from __future__ import annotations

import json
import sqlite3

from platform_schema import validate_owner_id
from hub.domain.workflow_contract_types import ContextReference
from hub.domain.workflow_errors import WorkflowValidationError
from hub.domain.workflow_memory import MAX_MEMORY_INPUT_BYTES, WorkflowMemoryItem, memory_selection, parse_memory_item
from .workflow_repository import WorkflowRepository
from .workflow_schema import inspect_schema

_TEXT_LIMITS = (("memory_id", 128), ("kind", 16), ("title", 160), ("content", MAX_MEMORY_INPUT_BYTES),
                ("tags", MAX_MEMORY_INPUT_BYTES), ("source", 256))
_PROJECTION = ",".join(f"CASE WHEN typeof({name})='text' AND length(CAST({name} AS BLOB))<={limit} "
                       f"THEN {name} END AS {name}" for name, limit in _TEXT_LIMITS)


class WorkflowMemoryRepository:
    def __init__(self, workflows: WorkflowRepository) -> None:
        self.workflows = workflows

    def read_selected(self, owner_id: str, references: tuple[ContextReference, ...], *,
                      connection: sqlite3.Connection | None = None) -> tuple[WorkflowMemoryItem, ...]:
        if connection is not None:
            return self._read_selected(connection, owner_id, references)
        with self.workflows.transaction(write=False) as owned:
            return self._read_selected(owned, owner_id, references)

    def _read_selected(self, connection: sqlite3.Connection, owner_id: str,
                       references: tuple[ContextReference, ...]) -> tuple[WorkflowMemoryItem, ...]:
        selected = memory_selection(references)
        try:
            if type(owner_id) is not str or validate_owner_id(owner_id) != owner_id:
                raise WorkflowValidationError("invalid_memory_selection")
        except ValueError as exc:
            raise WorkflowValidationError("invalid_memory_selection") from exc
        if not selected:
            return ()
        inspect_schema(connection)
        rows = connection.execute(
            f"SELECT {_PROJECTION},CASE WHEN typeof(enabled)='integer' THEN enabled END AS enabled,"
            "CASE WHEN typeof(revision)='integer' THEN revision END AS revision FROM platform_memory_items WHERE owner_id=? "
            f"AND memory_id IN ({','.join('?' for _ in selected)}) LIMIT 17", (owner_id, *selected),
        ).fetchall()
        by_id = {row["memory_id"]: row for row in rows}
        items: dict[str, WorkflowMemoryItem] = {}
        for memory_id, reference in selected.items():
            row = by_id.get(memory_id)
            if (row is None or type(row["enabled"]) is not int or row["enabled"] != 1
                    or type(row["revision"]) is not int or row["revision"] != reference.revision):
                raise WorkflowValidationError("memory_selection_stale")
            try:
                tags = json.loads(row["tags"]) if type(row["tags"]) is str else None
                item = parse_memory_item({"memory_id": memory_id, "revision": row["revision"], "kind": row["kind"],
                    "title": row["title"], "content": row["content"], "tags": tags, "source": row["source"]})
            except (TypeError, ValueError) as exc:
                raise WorkflowValidationError("invalid_memory_value") from exc
            if len(item.snapshot) > MAX_MEMORY_INPUT_BYTES:
                raise WorkflowValidationError("context_too_large")
            items[memory_id] = item
        return tuple(items[reference.ref_id] for reference in references)
