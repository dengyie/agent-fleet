"""Durable explicit summary versions and bounded exact-version authorization."""
from __future__ import annotations

import sqlite3
from typing import Any

from platform_schema import validate_owner_id
from hub.domain.workflow_contract_types import ContextReference, ResolvedContract
from hub.domain.workflow_errors import WorkflowValidationError
from hub.domain.workflow_identity import canonical_digest, validate_reference
from hub.domain.workflow_json import decode_snapshot
from hub.domain.workflow_summary import MAX_REVISION, SummaryInputs, WorkflowSummaryVersion, parse_summary, summary_selection
from .workflow_repository import WorkflowRepository, WorkflowRepositoryError, _time
from .workflow_schema import inspect_schema
from .workflow_summary_schema import STATEMENTS, TABLE_COLUMNS, inspect_summary_schema

_VERSION_COLUMNS = "owner_id,workspace_id,summary_id,revision,CASE WHEN typeof(snapshot)='blob' AND length(snapshot)<=8192 THEN snapshot END,sha256,client_token,request_digest,expected_epoch"


def _scope(owner: str, workspace: str, summary_id: str | None = None) -> None:
    if type(owner) is not str or validate_owner_id(owner) != owner:
        raise WorkflowValidationError("invalid_summary_selection")
    validate_reference(workspace)
    if summary_id is not None:
        validate_reference(summary_id)


def _epoch(value: int) -> int:
    if type(value) is not int or not 0 <= value < MAX_REVISION:
        raise WorkflowValidationError("invalid_summary_epoch")
    return value


def _decode(row: Any, connection: sqlite3.Connection | None = None) -> WorkflowSummaryVersion:
    try:
        if row is None or type(row[4]) is not bytes:
            raise WorkflowValidationError("invalid_summary_value")
        item = parse_summary(decode_snapshot(row[4], max_bytes=8192))
        if (item.owner_id, item.workspace_id, item.summary_id, item.revision, item.snapshot, item.sha256) != tuple(row[:6]):
            raise WorkflowValidationError("summary_snapshot_mismatch")
        validate_reference(row[6])
        _epoch(row[8])
        request = {"workspace_id": item.workspace_id, "summary_id": item.summary_id,
            "title": item.title, "content": item.content, "expected_epoch": row[8]}
        if item.generation is not None:
            from .workflow_summary_generation_repository import WorkflowSummaryGenerationRepository

            if connection is None:
                raise WorkflowRepositoryError("summary_snapshot_invalid")
            WorkflowSummaryGenerationRepository.verify_version(connection, item)
            request["generation"] = item.generation.value
        expected_digest = canonical_digest(request)
        if row[7] != expected_digest or row[8] < item.revision - 1:
            raise WorkflowValidationError("summary_snapshot_mismatch")
        return item
    except (ValueError, TypeError, KeyError) as exc:
        raise WorkflowRepositoryError("summary_snapshot_invalid") from exc


class WorkflowSummaryRepository:
    def __init__(self, workflows: WorkflowRepository) -> None:
        self.workflows = workflows

    @staticmethod
    def _authority(connection: sqlite3.Connection, owner: str, workspace: str, *, write: bool) -> None:
        metadata = inspect_schema(connection)
        if metadata is None:
            raise WorkflowRepositoryError("workflow_schema_unavailable")
        if write and metadata["admission_mode"] != "open":
            raise WorkflowRepositoryError("admission_frozen" if metadata["admission_mode"] == "frozen" else "admission_closed")
        row = connection.execute("SELECT enabled FROM workspaces WHERE owner_id=? AND workspace_id=?", (owner, workspace)).fetchone()
        if row is None or (write and tuple(row) != (1,)):
            raise WorkflowRepositoryError("reference_forbidden")

    @staticmethod
    def _head(connection: sqlite3.Connection, owner: str, workspace: str, summary_id: str) -> dict[str, Any] | None:
        row = connection.execute("SELECT latest_revision,epoch,enabled FROM workflow_summary_heads WHERE owner_id=? AND workspace_id=? AND summary_id=?",
            (owner, workspace, summary_id)).fetchone()
        if row is None:
            return None
        if (any(type(value) is not int for value in row) or not 1 <= row[0] <= row[1] <= MAX_REVISION or row[2] not in (0, 1)):
            raise WorkflowRepositoryError("summary_snapshot_invalid")
        return {"summary_id": summary_id, "workspace_id": workspace, "latest_revision": row[0], "epoch": row[1], "enabled": bool(row[2])}

    def publish(self, owner: str, workspace: str, summary_id: str, *, title: str, content: str,
                client_token: str, expected_epoch: int, now: float) -> WorkflowSummaryVersion:
        _scope(owner, workspace, summary_id)
        _epoch(expected_epoch)
        validate_reference(client_token)
        created_ms = int(_time(now) * 1000)
        WorkflowSummaryVersion(owner, workspace, summary_id, 1, title, content, owner, created_ms)
        request_digest = canonical_digest({"workspace_id": workspace, "summary_id": summary_id, "title": title,
            "content": content, "expected_epoch": expected_epoch})
        with self.workflows.transaction() as connection:
            metadata = inspect_schema(connection)
            if metadata is None:
                raise WorkflowRepositoryError("workflow_schema_unavailable")
            present = inspect_summary_schema(connection)
            if present:
                row = connection.execute(f"SELECT {_VERSION_COLUMNS} FROM workflow_summary_versions WHERE owner_id=? AND client_token=?", (owner, client_token)).fetchone()
                if row is not None:
                    item = _decode(row, connection)
                    if row[7] != request_digest:
                        raise WorkflowRepositoryError("idempotency_conflict")
                    return item
            self._authority(connection, owner, workspace, write=True)
            if not present:
                for statement in STATEMENTS:
                    connection.execute(statement)
                connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=max(minimum_reader_version,11) WHERE singleton=1")
            head = self._head(connection, owner, workspace, summary_id)
            if (head["epoch"] if head else 0) != expected_epoch:
                raise WorkflowRepositoryError("revision_conflict")
            if head and not head["enabled"]:
                raise WorkflowRepositoryError("summary_disabled")
            revision = head["latest_revision"] + 1 if head else 1
            item = WorkflowSummaryVersion(owner, workspace, summary_id, revision, title, content, owner, created_ms)
            if head:
                connection.execute("UPDATE workflow_summary_heads SET latest_revision=?,epoch=epoch+1 WHERE owner_id=? AND workspace_id=? AND summary_id=?",
                    (revision, owner, workspace, summary_id))
            else:
                connection.execute("INSERT INTO workflow_summary_heads VALUES(?,?,?,?,?,1)", (owner, workspace, summary_id, revision, 1))
            connection.execute("INSERT INTO workflow_summary_versions VALUES(?,?,?,?,?,?,?,?,?)",
                (owner, workspace, summary_id, revision, item.snapshot, item.sha256, client_token, request_digest, expected_epoch))
            return item

    def publish_generated(self, owner: str, run_id: str, *, now: float) -> WorkflowSummaryVersion:
        from .workflow_summary_generation_repository import WorkflowSummaryGenerationRepository

        created_ms = int(_time(now) * 1000)
        with self.workflows.transaction() as connection:
            row, (request, run, provenance) = WorkflowSummaryGenerationRepository.binding(connection, owner, run_id)
            token = "summary-generated-" + canonical_digest({"run_id": run_id})
            prior = connection.execute(f"SELECT {_VERSION_COLUMNS} FROM workflow_summary_versions WHERE owner_id=? AND client_token=?", (owner, token)).fetchone()
            if prior is not None:
                item = _decode(prior, connection)
                if item.generation != provenance:
                    raise WorkflowRepositoryError("idempotency_conflict")
                return item
            self._authority(connection, owner, row["workspace_id"], write=True)
            if run["state"] != "succeeded" or run["cancel_requested"]:
                raise WorkflowRepositoryError("summary_generation_not_succeeded")
            if run["result_text"] is None:
                raise WorkflowValidationError("summary_output_too_large")
            if not run["result_text"].strip():
                raise WorkflowRepositoryError("summary_output_empty")
            head = self._head(connection, owner, row["workspace_id"], request["summary_id"])
            if (head["epoch"] if head else 0) != request["expected_epoch"]:
                raise WorkflowRepositoryError("revision_conflict")
            if head and not head["enabled"]:
                raise WorkflowRepositoryError("summary_disabled")
            revision = head["latest_revision"] + 1 if head else 1
            try:
                item = WorkflowSummaryVersion(owner, row["workspace_id"], request["summary_id"], revision,
                    request["title"], run["result_text"], owner, created_ms, source_kind="model", generation=provenance)
            except WorkflowValidationError as exc:
                if exc.code == "context_too_large":
                    raise WorkflowValidationError("summary_output_too_large") from exc
                raise
            digest = canonical_digest({"workspace_id": item.workspace_id, "summary_id": item.summary_id,
                "title": item.title, "content": item.content, "expected_epoch": request["expected_epoch"], "generation": provenance.value})
            if head:
                connection.execute("UPDATE workflow_summary_heads SET latest_revision=?,epoch=epoch+1 WHERE owner_id=? AND workspace_id=? AND summary_id=?",
                    (revision, owner, item.workspace_id, item.summary_id))
            else:
                connection.execute("INSERT INTO workflow_summary_heads VALUES(?,?,?,?,?,1)", (owner, item.workspace_id, item.summary_id, revision, 1))
            connection.execute("INSERT INTO workflow_summary_versions VALUES(?,?,?,?,?,?,?,?,?)",
                (owner, item.workspace_id, item.summary_id, revision, item.snapshot, item.sha256, token, digest, request["expected_epoch"]))
            return item

    def head(self, owner: str, workspace: str, summary_id: str) -> dict[str, Any]:
        _scope(owner, workspace, summary_id)
        with self.workflows.transaction(write=False) as connection:
            self._authority(connection, owner, workspace, write=False)
            head = self._head(connection, owner, workspace, summary_id) if inspect_summary_schema(connection) else None
            if head is None:
                raise WorkflowRepositoryError("summary_not_found")
            return head

    def get(self, owner: str, workspace: str, summary_id: str, *, revision: int) -> WorkflowSummaryVersion:
        _scope(owner, workspace, summary_id)
        if type(revision) is not int or not 1 <= revision <= MAX_REVISION:
            raise WorkflowValidationError("invalid_summary_revision")
        with self.workflows.transaction(write=False) as connection:
            self._authority(connection, owner, workspace, write=False)
            row = connection.execute(f"SELECT {_VERSION_COLUMNS} FROM workflow_summary_versions WHERE owner_id=? AND workspace_id=? AND summary_id=? AND revision=?",
                (owner, workspace, summary_id, revision)).fetchone() if inspect_summary_schema(connection) else None
            if row is None:
                raise WorkflowRepositoryError("summary_not_found")
            return _decode(row, connection)

    def list(self, owner: str, workspace: str, *, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        _scope(owner, workspace)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise WorkflowValidationError("invalid_summary_limit")
        if cursor is not None:
            validate_reference(cursor)
        with self.workflows.transaction(write=False) as connection:
            self._authority(connection, owner, workspace, write=False)
            if not inspect_summary_schema(connection):
                return {"summaries": [], "next_cursor": None}
            rows = connection.execute("SELECT summary_id FROM workflow_summary_heads WHERE owner_id=? AND workspace_id=? "
                + ("AND summary_id>? " if cursor is not None else "") + "ORDER BY summary_id LIMIT ?",
                (owner, workspace, cursor, limit+1) if cursor is not None else (owner, workspace, limit+1)).fetchall()
            return {"summaries": [self._head(connection, owner, workspace, row[0]) for row in rows[:limit]],
                "next_cursor": rows[limit-1][0] if len(rows) > limit else None}

    def set_enabled(self, owner: str, workspace: str, summary_id: str, *, enabled: bool, expected_epoch: int) -> dict[str, Any]:
        _scope(owner, workspace, summary_id)
        _epoch(expected_epoch)
        if type(enabled) is not bool:
            raise WorkflowValidationError("invalid_summary_enabled")
        with self.workflows.transaction() as connection:
            self._authority(connection, owner, workspace, write=True)
            head = self._head(connection, owner, workspace, summary_id) if inspect_summary_schema(connection) else None
            if head is None:
                raise WorkflowRepositoryError("summary_not_found")
            if head["epoch"] != expected_epoch:
                raise WorkflowRepositoryError("revision_conflict")
            connection.execute("UPDATE workflow_summary_heads SET enabled=?,epoch=epoch+1 WHERE owner_id=? AND workspace_id=? AND summary_id=?",
                (int(enabled), owner, workspace, summary_id))
            return self._head(connection, owner, workspace, summary_id)

    def read_selected(self, owner: str, workspace: str, references: tuple[ContextReference, ...], *,
                      connection: sqlite3.Connection | None = None, require_enabled: bool = True) -> tuple[WorkflowSummaryVersion, ...]:
        summary_selection(references)
        _scope(owner, workspace)
        if not references:
            return ()
        if connection is None:
            with self.workflows.transaction(write=False) as owned:
                return self.read_selected(owner, workspace, references, connection=owned, require_enabled=require_enabled)
        return self.read_connection(connection, owner, workspace, references, require_enabled=require_enabled)

    @staticmethod
    def read_connection(connection: sqlite3.Connection, owner: str, workspace: str,
                        references: tuple[ContextReference, ...], *, require_enabled: bool = True) -> tuple[WorkflowSummaryVersion, ...]:
        summary_selection(references)
        _scope(owner, workspace)
        if not references:
            return ()
        inspect_schema(connection)
        if not inspect_summary_schema(connection):
            raise WorkflowValidationError("summary_selection_stale")
        selected = tuple(dict.fromkeys((ref.ref_id, ref.revision) for ref in references))
        rows = connection.execute("WITH selected(summary_id,revision) AS (VALUES "
            + ",".join("(?,?)" for _ in selected) + ") "
            "SELECT v.owner_id,v.workspace_id,v.summary_id,v.revision,CASE WHEN typeof(v.snapshot)='blob' AND length(v.snapshot)<=8192 THEN v.snapshot END,"
            "v.sha256,v.client_token,v.request_digest,v.expected_epoch,h.enabled,w.enabled "
            "FROM selected s CROSS JOIN workflow_summary_versions v ON (v.summary_id,v.revision)=(s.summary_id,s.revision) "
            "JOIN workflow_summary_heads h ON (h.owner_id,h.workspace_id,h.summary_id)=(v.owner_id,v.workspace_id,v.summary_id) "
            "JOIN workspaces w ON (w.owner_id,w.workspace_id)=(v.owner_id,v.workspace_id) "
            "WHERE v.owner_id=? AND v.workspace_id=? LIMIT 17", (*(value for pair in selected for value in pair), owner, workspace)).fetchall()
        by_key = {(row[2], row[3]): row for row in rows}
        result: list[WorkflowSummaryVersion] = []
        for ref in references:
            row = by_key.get((ref.ref_id, ref.revision))
            if row is None or row[5] != ref.sha256 or (require_enabled and tuple(row[9:]) != (1, 1)):
                raise WorkflowValidationError("summary_selection_stale")
            result.append(_decode(row, connection))
        return tuple(result)

    @staticmethod
    def validate_snapshot(connection: sqlite3.Connection) -> None:
        if not inspect_summary_schema(connection):
            return
        from .workflow_summary_generation_repository import WorkflowSummaryGenerationRepository

        WorkflowSummaryGenerationRepository.validate_snapshot(connection)
        for table in TABLE_COLUMNS:
            if connection.execute(f"PRAGMA foreign_key_check({table})").fetchone() is not None:
                raise WorkflowRepositoryError("summary_snapshot_invalid")
        cursor = None
        while True:
            rows = connection.execute("SELECT rowid AS summary_rowid," + _VERSION_COLUMNS + " FROM workflow_summary_versions "
                + ("WHERE rowid>? " if cursor is not None else "") + "ORDER BY rowid LIMIT 128", () if cursor is None else (cursor,)).fetchall()
            for row in rows:
                _decode(tuple(row)[1:], connection)
            if len(rows) < 128:
                break
            cursor = rows[-1][0]

        cursor = None
        while True:
            rows = connection.execute("SELECT rowid,owner_id,workspace_id,summary_id,latest_revision,epoch,enabled FROM workflow_summary_heads "
                + ("WHERE rowid>? " if cursor is not None else "") + "ORDER BY rowid LIMIT 128", () if cursor is None else (cursor,)).fetchall()
            for row in rows:
                _scope(*row[1:4])
                if (any(type(value) is not int for value in row[4:]) or not 1 <= row[4] <= row[5] <= MAX_REVISION or row[6] not in (0, 1)):
                    raise WorkflowRepositoryError("summary_snapshot_invalid")
                count, low, high = connection.execute("SELECT count(*),min(revision),max(revision) FROM workflow_summary_versions WHERE owner_id=? AND workspace_id=? AND summary_id=?", tuple(row[1:4])).fetchone()
                if (count, low, high) != (row[4], 1, row[4]):
                    raise WorkflowRepositoryError("summary_snapshot_invalid")
            if len(rows) < 128:
                return
            cursor = rows[-1][0]


def validate_summary_input_closure(connection: sqlite3.Connection, records: list[tuple[ResolvedContract, SummaryInputs]]) -> None:
    """Bound original request versions in batches, independently of enable state."""
    expected: dict[tuple[str, str, str, int], WorkflowSummaryVersion] = {}
    for resolved, inputs in records:
        for _key, item in inputs:
            identity = (resolved.owner_id, resolved.workspace_id, item.summary_id, item.revision)
            if expected.setdefault(identity, item) != item:
                raise WorkflowRepositoryError("summary_snapshot_invalid")
    if not expected:
        return
    inspect_schema(connection)
    if not inspect_summary_schema(connection):
        raise WorkflowRepositoryError("summary_snapshot_invalid")
    identities = tuple(expected)
    for offset in range(0, len(identities), 128):
        batch = identities[offset:offset+128]
        # SQLite 3.40 uses only the first three PK columns for tuple IN and
        # scans all revisions. The small VALUES driver forces exact PK seeks.
        query = ("WITH selected(owner_id,workspace_id,summary_id,revision) AS (VALUES "
            + ",".join("(?,?,?,?)" for _ in batch) + ") SELECT " + _VERSION_COLUMNS
            + " FROM selected CROSS JOIN workflow_summary_versions USING(owner_id,workspace_id,summary_id,revision) LIMIT ?")
        rows = connection.execute(query, (*(value for identity in batch for value in identity), len(batch)+1)).fetchall()
        actual = {tuple(row[:4]): _decode(row, connection) for row in rows}
        if actual != {identity: expected[identity] for identity in batch}:
            raise WorkflowRepositoryError("summary_snapshot_invalid")
