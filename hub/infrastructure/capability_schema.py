"""Additive capability storage, recognized independently of runtime gates."""
from __future__ import annotations

import sqlite3

from hub.domain.capability import CapabilityStoreError

TABLE_COLUMNS = {
    "capability_schema_meta": {"singleton", "schema_version", "minimum_reader_version", "report_sequence"},
    "capability_definitions": {"capability_id", "revision", "snapshot", "digest", "created_at"},
    "capability_node_policies": {"owner_id", "node_id", "snapshot", "digest", "updated_at"},
    "capability_node_offers": {"owner_id", "node_id", "offer_revision", "snapshot", "config_digest", "received_at", "report_sequence"},
    "capability_step_bindings": {"owner_id", "workspace_id", "workflow_id", "step_key", "generation", "node_id",
        "snapshot", "digest", "requirements_snapshot", "memory_bytes", "disk_bytes", "created_at", "admission_revision"},
}
STATEMENTS = (
    """CREATE TABLE capability_schema_meta (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema_version INTEGER NOT NULL,
        minimum_reader_version INTEGER NOT NULL, report_sequence INTEGER NOT NULL DEFAULT 0 CHECK(report_sequence>=0))""",
    "INSERT INTO capability_schema_meta VALUES(1,1,1,0)",
    """CREATE TABLE capability_definitions (
        capability_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
        snapshot BLOB NOT NULL CHECK(length(snapshot)<=65536), digest TEXT NOT NULL CHECK(length(digest)=64),
        created_at REAL NOT NULL, PRIMARY KEY(capability_id,revision))""",
    """CREATE TABLE capability_node_policies (
        owner_id TEXT NOT NULL, node_id TEXT NOT NULL, snapshot BLOB NOT NULL CHECK(length(snapshot)<=65536),
        digest TEXT NOT NULL CHECK(length(digest)=64), updated_at REAL NOT NULL,
        PRIMARY KEY(owner_id,node_id), FOREIGN KEY(owner_id,node_id) REFERENCES nodes(owner_id,node_id))""",
    """CREATE TABLE capability_node_offers (
        owner_id TEXT NOT NULL, node_id TEXT NOT NULL, offer_revision INTEGER NOT NULL CHECK(offer_revision>0),
        snapshot BLOB NOT NULL CHECK(length(snapshot)<=65536), config_digest TEXT NOT NULL CHECK(length(config_digest)=64),
        received_at REAL NOT NULL, report_sequence INTEGER NOT NULL CHECK(report_sequence>0),
        PRIMARY KEY(owner_id,node_id), FOREIGN KEY(owner_id,node_id) REFERENCES capability_node_policies(owner_id,node_id))""",
    """CREATE TABLE capability_step_bindings (
        owner_id TEXT NOT NULL, workspace_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
        step_key TEXT NOT NULL, generation INTEGER NOT NULL, node_id TEXT NOT NULL,
        snapshot BLOB NOT NULL CHECK(length(snapshot)<=65536), digest TEXT NOT NULL CHECK(length(digest)=64),
        requirements_snapshot BLOB NOT NULL CHECK(length(requirements_snapshot)<=65536),
        memory_bytes INTEGER NOT NULL CHECK(memory_bytes>=0), disk_bytes INTEGER NOT NULL CHECK(disk_bytes>=0), created_at REAL NOT NULL,
        admission_revision INTEGER NOT NULL CHECK(admission_revision>0),
        PRIMARY KEY(owner_id,workflow_id,step_key,generation),
        FOREIGN KEY(owner_id,workflow_id,step_key,generation) REFERENCES capability_reservations(owner_id,workflow_id,step_key,generation),
        FOREIGN KEY(owner_id,workspace_id,workflow_id,step_key,generation) REFERENCES workflow_steps(owner_id,workspace_id,workflow_id,step_key,generation),
        FOREIGN KEY(owner_id,node_id) REFERENCES capability_node_policies(owner_id,node_id))""",
    "CREATE INDEX capability_binding_node ON capability_step_bindings(node_id)",
)


def inspect_capability_schema(connection: sqlite3.Connection) -> bool:
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'capability_%'")}
    tables.discard("capability_reservations")
    if not tables:
        return False
    if tables != set(TABLE_COLUMNS):
        raise CapabilityStoreError("incomplete_capability_schema")
    for table, columns in TABLE_COLUMNS.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if not columns.issubset(actual):
            raise CapabilityStoreError("invalid_capability_schema")
    rows = connection.execute("SELECT singleton,schema_version,minimum_reader_version,report_sequence FROM capability_schema_meta LIMIT 2").fetchall()
    if len(rows) != 1 or any(type(value) is not int for value in rows[0]) or rows[0][0] != 1 or rows[0][3] < 0:
        raise CapabilityStoreError("invalid_capability_schema")
    if rows[0][1] != 1 or rows[0][2] != 1:
        raise CapabilityStoreError("unsupported_capability_schema")
    workflow = connection.execute("SELECT minimum_reader_version FROM workflow_schema_meta WHERE singleton=1").fetchone()
    if workflow is None or workflow[0] < 2:
        raise CapabilityStoreError("invalid_capability_reader")
    return True
