"""Additive identity metadata recognized independently of the MCP gate."""
from __future__ import annotations

import sqlite3

TABLE_COLUMNS = {
    "workflow_integration_schema_meta": {"singleton", "schema_version", "minimum_reader_version"},
    "workflow_integration_identities": {"owner_id", "integration_id", "token_digest", "definition_snapshot",
        "definition_digest", "created_at", "revoked_at", "restore_disabled"},
}
STATEMENTS = (
    """CREATE TABLE workflow_integration_schema_meta (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema_version INTEGER NOT NULL,
        minimum_reader_version INTEGER NOT NULL)""",
    "INSERT INTO workflow_integration_schema_meta VALUES(1,1,1)",
    """CREATE TABLE workflow_integration_identities (
        owner_id TEXT NOT NULL, integration_id TEXT NOT NULL, token_digest TEXT NOT NULL CHECK(length(token_digest)=64),
        definition_snapshot BLOB NOT NULL CHECK(length(definition_snapshot)<=65536),
        definition_digest TEXT NOT NULL CHECK(length(definition_digest)=64), created_at REAL NOT NULL,
        revoked_at REAL CHECK(revoked_at>=created_at), restore_disabled INTEGER NOT NULL DEFAULT 0 CHECK(restore_disabled IN (0,1)),
        PRIMARY KEY(owner_id,integration_id))""",
    "CREATE UNIQUE INDEX workflow_integration_token ON workflow_integration_identities(token_digest)",
)


def inspect_integration_schema(connection: sqlite3.Connection) -> bool:
    from .workflow_schema import WorkflowSchemaError

    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'workflow_integration_*'",
    )}
    if not tables:
        return False
    if not set(TABLE_COLUMNS).issubset(tables):
        raise WorkflowSchemaError("incomplete_integration_schema")
    if tables != set(TABLE_COLUMNS):
        raise WorkflowSchemaError("unsupported_integration_schema")
    for name, columns in TABLE_COLUMNS.items():
        if {row[1] for row in connection.execute(f"PRAGMA table_info({name})")} != columns:
            raise WorkflowSchemaError("invalid_integration_schema")
    rows = connection.execute("SELECT singleton,schema_version,minimum_reader_version FROM workflow_integration_schema_meta LIMIT 2").fetchall()
    if len(rows) != 1 or any(type(item) is not int for item in rows[0]) or rows[0][0] != 1:
        raise WorkflowSchemaError("invalid_integration_schema")
    if tuple(rows[0]) != (1, 1, 1):
        raise WorkflowSchemaError("unsupported_integration_schema")
    indexes = {row[1]: (row[2], row[4]) for row in connection.execute("PRAGMA index_list(workflow_integration_identities)")}
    columns = tuple((row[2], row[4]) for row in connection.execute("PRAGMA index_xinfo(workflow_integration_token)") if row[5])
    if indexes.get("workflow_integration_token") != (1, 0) or columns != (("token_digest", "BINARY"),):
        raise WorkflowSchemaError("invalid_integration_schema")
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_schema_meta'").fetchone() is None:
        raise WorkflowSchemaError("incomplete_workflow_schema")
    workflow = connection.execute("SELECT minimum_reader_version FROM workflow_schema_meta WHERE singleton=1").fetchone()
    if workflow is None or type(workflow[0]) is not int or workflow[0] < 4:
        raise WorkflowSchemaError("invalid_integration_reader")
    return True
