"""Independent catalog reader metadata, recognized even with its gate off."""
from __future__ import annotations

import sqlite3

from hub.domain.mcp_catalog import MCPCatalogError

TABLE_COLUMNS = {
    "mcp_catalog_meta": {"singleton", "schema_version", "minimum_reader_version"},
    "mcp_catalog_connectors": {"connector_id", "config_snapshot", "config_digest", "active_snapshot",
        "active_digest", "pending_snapshot", "pending_digest", "last_revision", "epoch", "created_at",
        "updated_at", "revoked_at", "restore_disabled"},
}
STATEMENTS = (
    """CREATE TABLE mcp_catalog_meta (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        schema_version INTEGER NOT NULL, minimum_reader_version INTEGER NOT NULL)""",
    "INSERT INTO mcp_catalog_meta VALUES(1,1,1)",
    """CREATE TABLE mcp_catalog_connectors (
        connector_id TEXT NOT NULL PRIMARY KEY COLLATE BINARY,
        config_snapshot BLOB NOT NULL CHECK(length(config_snapshot)<=8192),
        config_digest TEXT NOT NULL CHECK(length(config_digest)=64),
        active_snapshot BLOB CHECK(length(active_snapshot)<=524288), active_digest TEXT,
        pending_snapshot BLOB CHECK(length(pending_snapshot)<=524288), pending_digest TEXT,
        last_revision INTEGER NOT NULL CHECK(last_revision BETWEEN 0 AND 9007199254740991),
        epoch INTEGER NOT NULL CHECK(epoch BETWEEN 1 AND 9007199254740991),
        created_at REAL NOT NULL, updated_at REAL NOT NULL CHECK(updated_at>=created_at),
        revoked_at REAL CHECK(revoked_at>=created_at),
        restore_disabled INTEGER NOT NULL DEFAULT 0 CHECK(restore_disabled IN (0,1)))""",
)


def inspect_catalog_schema(connection: sqlite3.Connection) -> bool:
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'mcp_catalog_*' LIMIT 3")}
    if not tables:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone() is not None:
            version = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            marker = connection.execute("SELECT value FROM meta WHERE key='mcp_catalog_initialized'").fetchone()
            if (version is not None and version[0] == "4") or marker is not None:
                raise MCPCatalogError("incomplete_mcp_catalog_schema")
        return False
    if not set(TABLE_COLUMNS).issubset(tables):
        raise MCPCatalogError("incomplete_mcp_catalog_schema")
    if tables != set(TABLE_COLUMNS):
        raise MCPCatalogError("unsupported_mcp_catalog_schema")
    for name, columns in TABLE_COLUMNS.items():
        if {row[1] for row in connection.execute(f"PRAGMA table_info({name})")} != columns:
            raise MCPCatalogError("invalid_mcp_catalog_schema")
    metadata = connection.execute("SELECT singleton,schema_version,minimum_reader_version FROM mcp_catalog_meta LIMIT 2").fetchall()
    if len(metadata) != 1 or any(type(item) is not int for item in metadata[0]) or metadata[0][0] != 1:
        raise MCPCatalogError("invalid_mcp_catalog_schema")
    if tuple(metadata[0]) != (1, 1, 1):
        raise MCPCatalogError("unsupported_mcp_catalog_schema")
    indexes = [row for row in connection.execute("PRAGMA index_list(mcp_catalog_connectors)") if row[3] == "pk"]
    if len(indexes) != 1 or (indexes[0][2], indexes[0][4]) != (1, 0):
        raise MCPCatalogError("invalid_mcp_catalog_schema")
    columns = [(row[2], row[4]) for row in connection.execute(f"PRAGMA index_xinfo({indexes[0][1]})") if row[5]]
    if columns != [("connector_id", "BINARY")]:
        raise MCPCatalogError("invalid_mcp_catalog_schema")
    try:
        row = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.Error as exc:
        raise MCPCatalogError("incomplete_platform_schema", cause=exc) from exc
    if row is None or type(row[0]) is not str or row[0] not in ("4", "5"):
        raise MCPCatalogError("unsupported_mcp_platform_reader")
    return True
