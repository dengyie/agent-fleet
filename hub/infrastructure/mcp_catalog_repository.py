"""Bounded durable MCP registrations and epoch-fenced discovery publication."""
from __future__ import annotations

import hashlib
import math
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Iterator

from hub.domain.mcp_administration import catalog_page
from hub.domain.mcp_catalog import MAX_CATALOG_INTEGER, MAX_CONNECTORS, MCPCatalogError, MCPCatalogRecord
from hub.platform.connectors.mcp import MCPConnectorConfig, MCPDiscovery, _IDENTIFIER
from hub.platform.connectors.snapshots import decode_config, decode_discovery, discovery_digest, encode_discovery
from .mcp_catalog_schema import STATEMENTS, inspect_catalog_schema

_COLUMNS = ("connector_id,config_snapshot,config_digest,active_snapshot,active_digest,pending_snapshot,"
    "pending_digest,last_revision,epoch,created_at,updated_at,revoked_at,restore_disabled")


def _time(value: object) -> float:
    if type(value) not in (int, float) or not 0 <= value <= 253402300799 or not math.isfinite(value):
        raise MCPCatalogError("invalid_time")
    return float(value)


def _id(value: object) -> str:
    if type(value) is not str or not _IDENTIFIER.fullmatch(value):
        raise MCPCatalogError("invalid_connector_id")
    return value


def _decode(row: sqlite3.Row) -> MCPCatalogRecord:
    try:
        config = decode_config(row["config_snapshot"])
        if (config.connector_id != row["connector_id"]
                or hashlib.sha256(row["config_snapshot"]).hexdigest() != row["config_digest"]):
            raise MCPCatalogError("connector_snapshot_invalid")
        active = _discovery(row["active_snapshot"], row["active_digest"], config)
        pending = _discovery(row["pending_snapshot"], row["pending_digest"], config)
        last, epoch = row["last_revision"], row["epoch"]
        if (type(last) is not int or not 0 <= last <= MAX_CATALOG_INTEGER
                or type(epoch) is not int or not 1 <= epoch <= MAX_CATALOG_INTEGER
                or (active is None and (last != 0 or pending is not None))
                or (active is not None and (active.revision > last
                    or (pending is None and active.revision != last)))
                or (pending is not None and not active.revision < pending.revision == last)):
            raise MCPCatalogError("connector_snapshot_invalid")
        created, updated = _time(row["created_at"]), _time(row["updated_at"])
        revoked = None if row["revoked_at"] is None else _time(row["revoked_at"])
        if (updated < created or (revoked is not None and not created <= revoked <= updated)
                or type(row["restore_disabled"]) is not int or row["restore_disabled"] not in (0, 1)):
            raise MCPCatalogError("connector_snapshot_invalid")
        return MCPCatalogRecord(config.connector_id, row["config_snapshot"], row["config_digest"],
            row["active_snapshot"], row["pending_snapshot"], last, epoch, created, updated, revoked,
            bool(row["restore_disabled"]))
    except (MCPCatalogError, KeyError, ValueError, TypeError, OverflowError) as exc:
        raise MCPCatalogError("connector_snapshot_invalid", cause=exc) from exc


def _discovery(raw: bytes | None, digest: str | None, config: MCPConnectorConfig) -> MCPDiscovery | None:
    if raw is None:
        if digest is not None:
            raise MCPCatalogError("connector_snapshot_invalid")
        return None
    discovery = decode_discovery(raw, config)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise MCPCatalogError("connector_snapshot_invalid")
    return discovery


class MCPCatalogRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path).resolve()

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.db_path.as_uri() + "?mode=rw", uri=True, timeout=10, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except sqlite3.Error as exc:
            raise MCPCatalogError("mcp_catalog_store_unavailable", cause=exc) from exc
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _require_schema(connection: sqlite3.Connection) -> None:
        if not inspect_catalog_schema(connection):
            raise MCPCatalogError("mcp_catalog_schema_unavailable")

    def init(self) -> None:
        with self._transaction(write=True) as connection:
            if not inspect_catalog_schema(connection):
                version = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                if version is None or version[0] not in ("3", "4"):
                    raise MCPCatalogError("unsupported_mcp_platform_reader")
                for statement in STATEMENTS:
                    connection.execute(statement)
                connection.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
        with self._transaction() as connection:
            self.validate_snapshot(connection)

    @staticmethod
    def validate_snapshot(connection: sqlite3.Connection) -> None:
        if not inspect_catalog_schema(connection):
            return
        connection.row_factory = sqlite3.Row
        after: int | None = None
        count = 0
        names: set[str] = set()
        while True:
            clause, values = ("", ()) if after is None else ("WHERE rowid>?", (after,))
            rows = connection.execute(f"SELECT rowid AS catalog_rowid,{_COLUMNS} FROM mcp_catalog_connectors "
                f"{clause} ORDER BY rowid LIMIT 8", values).fetchall()
            if not rows:
                return
            for row in rows:
                record = _decode(row)
                count += 1
                if count > MAX_CONNECTORS:
                    raise MCPCatalogError("too_many_connectors")
                config = decode_config(record.config_snapshot)
                selected = {f"mcp.{config.connector_id}.{name}" for name in config.selected_tools}
                if names.intersection(selected):
                    raise MCPCatalogError("connector_tool_name_conflict")
                names.update(selected)
            after = rows[-1]["catalog_rowid"]

    def get_many(self, connector_ids: tuple[str, ...]) -> tuple[MCPCatalogRecord, ...]:
        if (type(connector_ids) is not tuple or len(connector_ids) > MAX_CONNECTORS
                or any(type(value) is not str for value in connector_ids) or len(set(connector_ids)) != len(connector_ids)):
            raise MCPCatalogError("invalid_connector_selection")
        for value in connector_ids:
            _id(value)
        with self._transaction() as connection:
            self._require_schema(connection)
            if not connector_ids:
                return ()
            placeholders = ",".join("?" for _ in connector_ids)
            rows = connection.execute(f"SELECT {_COLUMNS} FROM mcp_catalog_connectors WHERE connector_id IN ({placeholders}) "
                "ORDER BY connector_id LIMIT 128", connector_ids).fetchall()
            return tuple(_decode(row) for row in rows)

    def list_page(self, after: str | None = None, limit: int = 8) -> tuple[tuple[MCPCatalogRecord, ...], str | None]:
        after, limit = catalog_page(after, limit)
        clause, values = ("", (limit + 1,)) if after is None else ("WHERE connector_id>?", (after, limit + 1))
        with self._transaction() as connection:
            self._require_schema(connection)
            rows = connection.execute(f"SELECT {_COLUMNS} FROM mcp_catalog_connectors {clause} "
                "ORDER BY connector_id LIMIT ?", values).fetchall()
            records = tuple(_decode(row) for row in rows[:limit])
            cursor = records[-1].connector_id if len(rows) > limit else None
            return records, cursor

    @staticmethod
    def _get(connection: sqlite3.Connection, connector_id: str) -> MCPCatalogRecord:
        row = connection.execute(f"SELECT {_COLUMNS} FROM mcp_catalog_connectors WHERE connector_id=?", (connector_id,)).fetchone()
        if row is None:
            raise MCPCatalogError("connector_not_found")
        return _decode(row)

    def register(self, config_snapshot: bytes, *, now: float) -> MCPCatalogRecord:
        config, now = decode_config(config_snapshot), _time(now)
        digest = hashlib.sha256(config_snapshot).hexdigest()
        with self._transaction(write=True) as connection:
            self._require_schema(connection)
            prior = connection.execute(f"SELECT {_COLUMNS} FROM mcp_catalog_connectors WHERE connector_id=?", (config.connector_id,)).fetchone()
            if prior is not None:
                record = _decode(prior)
                if record.config_snapshot != config_snapshot:
                    raise MCPCatalogError("connector_registration_conflict")
                return record
            rows = connection.execute("SELECT config_snapshot FROM mcp_catalog_connectors LIMIT 129").fetchall()
            if len(rows) >= MAX_CONNECTORS:
                raise MCPCatalogError("too_many_connectors")
            selected = {f"mcp.{config.connector_id}.{name}" for name in config.selected_tools}
            for row in rows:
                previous = decode_config(row[0])
                if any(f"mcp.{previous.connector_id}.{name}" in selected for name in previous.selected_tools):
                    raise MCPCatalogError("connector_tool_name_conflict")
            connection.execute("INSERT INTO mcp_catalog_connectors(connector_id,config_snapshot,config_digest,"
                "last_revision,epoch,created_at,updated_at) VALUES(?,?,?,0,1,?,?)", (config.connector_id, config_snapshot, digest, now, now))
            return self._get(connection, config.connector_id)

    @staticmethod
    def _writable(record: MCPCatalogRecord, now: float) -> None:
        if record.disabled:
            raise MCPCatalogError("connector_revoked")
        if now < record.updated_at:
            raise MCPCatalogError("invalid_time")
        if record.epoch >= MAX_CATALOG_INTEGER:
            raise MCPCatalogError("connector_revision_exhausted")

    def publish(self, connector_id: str, *, expected_epoch: int, discovery_snapshot: bytes, now: float) -> MCPCatalogRecord:
        connector_id, now = _id(connector_id), _time(now)
        with self._transaction(write=True) as connection:
            self._require_schema(connection)
            record = self._get(connection, connector_id)
            self._writable(record, now)
            if type(expected_epoch) is not int or record.epoch != expected_epoch:
                raise MCPCatalogError("connector_revision_changed")
            config = decode_config(record.config_snapshot)
            discovery = decode_discovery(discovery_snapshot, config)
            prior = record.pending_snapshot or record.active_snapshot
            if prior is not None and discovery_digest(discovery) == discovery_digest(decode_discovery(prior, config)):
                return record
            if record.last_revision >= MAX_CATALOG_INTEGER:
                raise MCPCatalogError("connector_revision_exhausted")
            raw = encode_discovery(replace(discovery, revision=record.last_revision + 1))
            field = "active" if record.active_snapshot is None else "pending"
            connection.execute(f"UPDATE mcp_catalog_connectors SET {field}_snapshot=?,{field}_digest=?,last_revision=?,epoch=epoch+1,"
                "updated_at=? WHERE connector_id=?", (raw, hashlib.sha256(raw).hexdigest(), record.last_revision + 1, now, connector_id))
            return self._get(connection, connector_id)

    @staticmethod
    def _check_epoch(record: MCPCatalogRecord, expected_epoch: int | None) -> None:
        if expected_epoch is not None and (type(expected_epoch) is not int or record.epoch != expected_epoch):
            raise MCPCatalogError("connector_revision_changed")

    def admit(self, connector_id: str, *, expected_revision: int, now: float,
              expected_epoch: int | None = None) -> MCPCatalogRecord:
        connector_id, now = _id(connector_id), _time(now)
        with self._transaction(write=True) as connection:
            self._require_schema(connection)
            record = self._get(connection, connector_id)
            self._check_epoch(record, expected_epoch)
            self._writable(record, now)
            pending = decode_discovery(record.pending_snapshot, decode_config(record.config_snapshot)) if record.pending_snapshot else None
            if type(expected_revision) is not int or pending is None or pending.revision != expected_revision:
                raise MCPCatalogError("connector_revision_changed")
            connection.execute("UPDATE mcp_catalog_connectors SET active_snapshot=pending_snapshot,active_digest=pending_digest,"
                "pending_snapshot=NULL,pending_digest=NULL,epoch=epoch+1,updated_at=? WHERE connector_id=?", (now, connector_id))
            return self._get(connection, connector_id)

    def revoke(self, connector_id: str, *, expected_digest: str, now: float,
               expected_epoch: int | None = None) -> MCPCatalogRecord:
        connector_id, now = _id(connector_id), _time(now)
        with self._transaction(write=True) as connection:
            self._require_schema(connection)
            record = self._get(connection, connector_id)
            self._check_epoch(record, expected_epoch)
            if type(expected_digest) is not str or record.config_digest != expected_digest:
                raise MCPCatalogError("connector_registration_conflict")
            if now < record.updated_at:
                raise MCPCatalogError("invalid_time")
            if record.disabled:
                return record
            self._writable(record, now)
            connection.execute("UPDATE mcp_catalog_connectors SET revoked_at=?,updated_at=?,epoch=epoch+1 WHERE connector_id=?",
                (now, now, connector_id))
            return self._get(connection, connector_id)
