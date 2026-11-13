"""Additive external dispatch facts, recognized without runtime gates."""
from __future__ import annotations

import sqlite3

TABLE_COLUMNS = {
    "workflow_external_schema_meta": {"singleton", "schema_version", "minimum_reader_version"},
    "workflow_external_bindings": {"token", "owner_id", "workspace_id", "workflow_id", "step_key", "generation",
        "target_snapshot", "target_digest", "send_started_at", "task_snapshot", "created_at", "updated_at", "request_snapshot"},
}
REQUEST_COLUMN = "request_snapshot BLOB CHECK(length(request_snapshot)<=65536)"
STATEMENTS = (
    """CREATE TABLE workflow_external_schema_meta (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema_version INTEGER NOT NULL,
        minimum_reader_version INTEGER NOT NULL)""",
    "INSERT INTO workflow_external_schema_meta VALUES(1,3,3)",
    """CREATE TABLE workflow_external_bindings (
        token TEXT PRIMARY KEY, owner_id TEXT NOT NULL, workspace_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
        step_key TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation>0),
        target_snapshot BLOB NOT NULL CHECK(length(target_snapshot)<=8192),
        target_digest TEXT NOT NULL CHECK(length(target_digest)=64), send_started_at REAL,
        task_snapshot BLOB CHECK(length(task_snapshot)<=2048), created_at REAL NOT NULL, updated_at REAL NOT NULL,
        request_snapshot BLOB CHECK(length(request_snapshot)<=65536),
        UNIQUE(owner_id,workflow_id,step_key,generation),
        FOREIGN KEY(token) REFERENCES workflow_dispatches(token),
        FOREIGN KEY(owner_id,workspace_id,workflow_id,step_key,generation)
            REFERENCES workflow_steps(owner_id,workspace_id,workflow_id,step_key,generation))""",
)


def inspect_external_schema(connection: sqlite3.Connection) -> int | None:
    from .workflow_schema import WorkflowSchemaError

    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'workflow_external_%'")}
    if not tables:
        return None
    if not set(TABLE_COLUMNS).issubset(tables):
        raise WorkflowSchemaError("incomplete_external_schema")
    if tables != set(TABLE_COLUMNS):
        raise WorkflowSchemaError("unsupported_external_schema")
    rows = connection.execute("SELECT singleton,schema_version,minimum_reader_version FROM workflow_external_schema_meta LIMIT 2").fetchall()
    if len(rows) != 1 or any(type(item) is not int for item in rows[0]) or rows[0][0] != 1:
        raise WorkflowSchemaError("invalid_external_schema")
    if tuple(rows[0]) not in {(1, 1, 1), (1, 2, 2), (1, 3, 3), (1, 4, 4)}:
        raise WorkflowSchemaError("unsupported_external_schema")
    version = rows[0][1]
    for name, columns in TABLE_COLUMNS.items():
        expected = columns - {"request_snapshot"} if version == 1 else columns
        if {row[1] for row in connection.execute(f"PRAGMA table_info({name})")} != expected:
            raise WorkflowSchemaError("invalid_external_schema")
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name='workflow_schema_meta' AND type='table'").fetchone() is None:
        raise WorkflowSchemaError("incomplete_workflow_schema")
    workflow = connection.execute("SELECT minimum_reader_version FROM workflow_schema_meta WHERE singleton=1").fetchone()
    if workflow is None or type(workflow[0]) is not int or workflow[0] < {1: 3, 2: 5, 3: 6, 4: 11}[version]:
        raise WorkflowSchemaError("invalid_external_reader")
    return version
