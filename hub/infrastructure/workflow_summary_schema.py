"""Optional append-only summary authority, fenced independently of gates."""
from __future__ import annotations

import sqlite3

TABLE_COLUMNS = {
    "workflow_summary_schema_meta": {"singleton", "schema_version", "minimum_reader_version"},
    "workflow_summary_heads": {"owner_id", "workspace_id", "summary_id", "latest_revision", "epoch", "enabled"},
    "workflow_summary_versions": {"owner_id", "workspace_id", "summary_id", "revision", "snapshot", "sha256", "client_token", "request_digest", "expected_epoch"},
}
STATEMENTS = (
    "CREATE TABLE workflow_summary_schema_meta(singleton INTEGER PRIMARY KEY CHECK(singleton=1),schema_version INTEGER NOT NULL,minimum_reader_version INTEGER NOT NULL)",
    "INSERT INTO workflow_summary_schema_meta VALUES(1,1,1)",
    """CREATE TABLE workflow_summary_heads(
        owner_id TEXT NOT NULL,workspace_id TEXT NOT NULL,summary_id TEXT NOT NULL,
        latest_revision INTEGER NOT NULL CHECK(latest_revision>0),epoch INTEGER NOT NULL CHECK(epoch>0),
        enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),PRIMARY KEY(owner_id,workspace_id,summary_id),
        FOREIGN KEY(owner_id,workspace_id) REFERENCES workspaces(owner_id,workspace_id))""",
    """CREATE TABLE workflow_summary_versions(
        owner_id TEXT NOT NULL,workspace_id TEXT NOT NULL,summary_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision>0),snapshot BLOB NOT NULL CHECK(length(snapshot)<=8192),
        sha256 TEXT NOT NULL CHECK(length(sha256)=64),client_token TEXT NOT NULL,request_digest TEXT NOT NULL,
        expected_epoch INTEGER NOT NULL CHECK(expected_epoch>=0),
        PRIMARY KEY(owner_id,workspace_id,summary_id,revision),UNIQUE(owner_id,client_token),
        FOREIGN KEY(owner_id,workspace_id,summary_id) REFERENCES workflow_summary_heads(owner_id,workspace_id,summary_id))""",
)


def inspect_summary_schema(connection: sqlite3.Connection) -> bool:
    from .workflow_schema import WorkflowSchemaError

    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'workflow_summary_*'")}
    if not tables:
        return False
    if tables != set(TABLE_COLUMNS):
        raise WorkflowSchemaError("incomplete_summary_schema" if not set(TABLE_COLUMNS).issubset(tables) else "unsupported_summary_schema")
    for table, columns in TABLE_COLUMNS.items():
        if {row[1] for row in connection.execute(f"PRAGMA table_info({table})")} != columns:
            raise WorkflowSchemaError("invalid_summary_schema")
    rows = connection.execute("SELECT singleton,schema_version,minimum_reader_version FROM workflow_summary_schema_meta LIMIT 2").fetchall()
    if len(rows) != 1 or any(type(value) is not int for value in rows[0]) or tuple(rows[0]) != (1, 1, 1):
        raise WorkflowSchemaError("unsupported_summary_schema")
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_schema_meta'").fetchone() is None:
        raise WorkflowSchemaError("incomplete_workflow_schema")
    row = connection.execute("SELECT minimum_reader_version FROM workflow_schema_meta WHERE singleton=1").fetchone()
    if row is None or type(row[0]) is not int or row[0] < 11:
        raise WorkflowSchemaError("invalid_summary_reader")
    return True
