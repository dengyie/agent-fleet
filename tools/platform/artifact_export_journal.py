"""Independent export intent/outbox; fixed remote IDs survive lost responses."""
from __future__ import annotations

from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from fleet_core.artifact_export import ArtifactExportError, ExportReceipt, ExportRequest

_COLUMNS = ("command_id,command_digest,command,context_digest,source_identity,name,size,sha256,state,"
    "transfer_id,artifact_id,expires_at,started_at,attempts,receipt,acknowledged,updated_at")
_TABLES = {"artifact_export_meta", "artifact_exports"}


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class ArtifactExportJournal:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            with closing(sqlite3.connect(self.path, timeout=10)) as connection, connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                yield connection
        except sqlite3.Error as exc:
            raise ArtifactExportError("export_journal_store", uncertain=True) from exc

    @staticmethod
    def _schema(connection: sqlite3.Connection) -> None:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 4")}
        if tables != _TABLES:
            raise ArtifactExportError("export_journal_schema")
        versions = connection.execute("SELECT schema_version FROM artifact_export_meta LIMIT 2").fetchall()
        columns = {row[1] for row in connection.execute("PRAGMA table_info(artifact_exports)")}
        if len(versions) != 1 or tuple(versions[0]) != (1,) or columns != set(_COLUMNS.split(",")) | {"failure_code"}:
            raise ArtifactExportError("export_journal_schema")

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as connection:
            existing = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone()
            if existing:
                self._schema(connection)
                return
            connection.execute("CREATE TABLE artifact_export_meta(singleton INTEGER PRIMARY KEY CHECK(singleton=1),schema_version INTEGER NOT NULL)")
            connection.execute("INSERT INTO artifact_export_meta VALUES(1,1)")
            connection.execute("""CREATE TABLE artifact_exports(
                command_id TEXT PRIMARY KEY,command_digest TEXT NOT NULL,command TEXT NOT NULL CHECK(length(command)<=65536),
                context_digest TEXT NOT NULL,source_identity TEXT NOT NULL,name TEXT NOT NULL,
                size INTEGER NOT NULL CHECK(size BETWEEN 0 AND 268435456),sha256 TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('prepared','reserved','committing','ready','reconciliation')),
                transfer_id TEXT UNIQUE,artifact_id TEXT UNIQUE,expires_at REAL,started_at REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3),receipt TEXT,
                acknowledged INTEGER NOT NULL DEFAULT 0 CHECK(acknowledged IN (0,1)),
                failure_code TEXT,updated_at REAL NOT NULL)""")
            connection.execute("CREATE INDEX artifact_export_recovery ON artifact_exports(state,attempts,command_id)")
            connection.execute("CREATE INDEX artifact_export_outbox ON artifact_exports(acknowledged,state,command_id)")

    def get(self, command_id: str) -> dict[str, Any] | None:
        with self.transaction() as connection:
            self._schema(connection)
            row = connection.execute(f"SELECT {_COLUMNS} FROM artifact_exports WHERE command_id=? LIMIT 1", (command_id,)).fetchone()
            return dict(row) if row is not None else None

    def begin(self, request: ExportRequest, *, command: dict[str, Any], context_digest: str,
              source_identity: dict[str, str], name: str, size: int, sha256: str, now: float) -> dict[str, Any]:
        with self.transaction() as connection:
            self._schema(connection)
            row = connection.execute(f"SELECT {_COLUMNS} FROM artifact_exports WHERE command_id=? LIMIT 1", (request.command_id,)).fetchone()
            if row is not None:
                if (row["command_digest"], row["context_digest"], row["source_identity"], row["name"], row["size"], row["sha256"]) != (
                        request.command_digest, context_digest, _json(source_identity), name, size, sha256):
                    raise ArtifactExportError("export_source_changed", uncertain=True)
                return dict(row)
            if connection.execute("SELECT count(*) FROM artifact_exports").fetchone()[0] >= 10000:
                raise ArtifactExportError("export_journal_limit")
            connection.execute("INSERT INTO artifact_exports(command_id,command_digest,command,context_digest,source_identity,name,size,sha256,state,started_at,updated_at) VALUES(?,?,?,?,?,?,?,?,'prepared',?,?)",
                (request.command_id, request.command_digest, _json(command), context_digest, _json(source_identity), name, size, sha256, now, now))
            return dict(connection.execute(f"SELECT {_COLUMNS} FROM artifact_exports WHERE command_id=? LIMIT 1", (request.command_id,)).fetchone())

    def attempt(self, command_id: str, *, now: float) -> None:
        with self.transaction() as connection:
            cursor = connection.execute("UPDATE artifact_exports SET attempts=attempts+1,updated_at=? WHERE command_id=? AND state IN ('prepared','reserved') AND attempts<3", (now, command_id))
            if cursor.rowcount != 1:
                raise ArtifactExportError("export_attempt_limit", uncertain=True)

    def reserve(self, command_id: str, *, transfer_id: str, artifact_id: str, expires_at: float, now: float) -> None:
        with self.transaction() as connection:
            cursor = connection.execute("UPDATE artifact_exports SET state='reserved',transfer_id=?,artifact_id=?,expires_at=?,updated_at=? WHERE command_id=? AND state='prepared' AND transfer_id IS NULL",
                (transfer_id, artifact_id, expires_at, now, command_id))
            if cursor.rowcount != 1:
                raise ArtifactExportError("export_journal_conflict", uncertain=True)

    def committing(self, command_id: str, *, now: float) -> None:
        with self.transaction() as connection:
            cursor = connection.execute("UPDATE artifact_exports SET state='committing',updated_at=? WHERE command_id=? AND state IN ('reserved','committing')", (now, command_id))
            if cursor.rowcount != 1:
                raise ArtifactExportError("export_journal_conflict", uncertain=True)

    def ready(self, command_id: str, receipt: ExportReceipt, *, now: float) -> None:
        with self.transaction() as connection:
            cursor = connection.execute("UPDATE artifact_exports SET state='ready',receipt=?,updated_at=? WHERE command_id=? AND state='committing' AND artifact_id=? AND size=? AND sha256=?",
                (_json(receipt), now, command_id, receipt["artifact"]["artifact_id"], receipt["artifact"]["size"], receipt["artifact"]["sha256"]))
            if cursor.rowcount != 1:
                raise ArtifactExportError("export_journal_conflict", uncertain=True)

    def reconcile(self, command_id: str, code: str, *, now: float) -> None:
        with self.transaction() as connection:
            connection.execute("UPDATE artifact_exports SET state='reconciliation',failure_code=?,updated_at=? WHERE command_id=? AND acknowledged=0", (code[:80], now, command_id))

    def records(self, *, ready: bool, limit: int) -> tuple[dict[str, Any], ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ArtifactExportError("invalid_export_limit")
        predicate = ("state='ready' AND acknowledged=0" if ready else
            "state='committing' OR (state IN ('prepared','reserved') AND attempts<3)")
        with self.transaction() as connection:
            self._schema(connection)
            rows = connection.execute(f"SELECT {_COLUMNS} FROM artifact_exports WHERE {predicate} ORDER BY command_id LIMIT ?", (limit,)).fetchall()
            return tuple(dict(row) for row in rows)

    def acknowledge(self, command_id: str, receipt: ExportReceipt, *, now: float) -> None:
        with self.transaction() as connection:
            cursor = connection.execute("UPDATE artifact_exports SET acknowledged=1,updated_at=? WHERE command_id=? AND state='ready' AND receipt=?", (now, command_id, _json(receipt)))
            if cursor.rowcount != 1:
                raise ArtifactExportError("invalid_export_receipt")
