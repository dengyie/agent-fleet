"""Bounded retention and crash recovery for published browser PNG captures."""
from __future__ import annotations

import json
import hashlib
import math
import sqlite3
import stat
import time
from pathlib import Path
from typing import Callable

from hub.platform.artifact_lock import artifact_snapshot_barrier
from hub.infrastructure.sqlite import connect as sqlite_connect

CAPTURE_RETENTION_SECONDS = 30 * 24 * 60 * 60
GC_BATCH_LIMIT = 128
# JSON columns that can embed {"artifact_id": ...} references. Rows are only
# parsed when they contain the literal key, which every writer emits verbatim.
JSON_REFERENCE_SURFACES = {
    "execution_window_events": "payload",
    "run_events": "payload",
    "platform_commands": "result",
    "workflow_runs": "definition_snapshot",
    "workflow_steps": "resolved_snapshot",
}
_GC_OWN_TABLES = frozenset({"browser_artifact_tickets", "browser_capture_gc", "browser_capture_gc_done"})
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class BrowserCaptureGcError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


class BrowserCaptureGcRepository:
    """Keep SQLite reference checks and filesystem quarantine under one barrier."""

    def __init__(self, db_path: Path, artifact_root: Path, *, clock: Callable[[], float] = time.time):
        self.db_path = Path(db_path)
        self.artifact_root = Path(artifact_root).expanduser().resolve()
        self.clock = clock

    def _connect(self) -> sqlite3.Connection:
        # 该库历史上未切换 WAL；保持原日志模式。
        return sqlite_connect(self.db_path, timeout=10, wal=False)

    def init(self) -> None:
        conn = None
        try:
            conn = self._connect()
            conn.execute("CREATE TABLE IF NOT EXISTS browser_capture_gc (artifact_id TEXT PRIMARY KEY, marked_at REAL NOT NULL)")
            # Additive tombstones for captures whose files are already gone, so
            # finished removals never re-enter (and starve) the candidate batch.
            conn.execute("CREATE TABLE IF NOT EXISTS browser_capture_gc_done (artifact_id TEXT PRIMARY KEY, removed_at REAL NOT NULL)")
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='browser_artifact_tickets'"
            ).fetchone() is not None:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_browser_capture_gc_ticket_consumed "
                    "ON browser_artifact_tickets(state, content_type, consumed_at)"
                )
        except sqlite3.Error as exc:
            raise BrowserCaptureGcError("capture_gc_store") from exc
        finally:
            if conn is not None:
                conn.close()

    def cleanup_expired(self, *, now: float | None = None,
                        retention_s: int = CAPTURE_RETENTION_SECONDS,
                        limit: int = GC_BATCH_LIMIT) -> int:
        if type(retention_s) is not int or not 1 <= retention_s <= 365 * 24 * 60 * 60:
            raise BrowserCaptureGcError("capture_gc_retention_invalid")
        if type(limit) is not int or not 1 <= limit <= GC_BATCH_LIMIT:
            raise BrowserCaptureGcError("capture_gc_limit_invalid")
        try:
            timestamp = float(self.clock() if now is None else now)
        except (TypeError, ValueError, OverflowError) as exc:
            raise BrowserCaptureGcError("capture_gc_time_invalid") from exc
        if not math.isfinite(timestamp):
            raise BrowserCaptureGcError("capture_gc_time_invalid")
        if not self.artifact_root.exists():
            return 0
        cutoff = timestamp - retention_s
        try:
            with artifact_snapshot_barrier(self.artifact_root):
                self._mark_candidates(cutoff, timestamp, limit)
                return self._finish_marked(cutoff, limit, timestamp)
        except BrowserCaptureGcError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise BrowserCaptureGcError("capture_gc_failed") from exc

    def _mark_candidates(self, cutoff: float, now: float, limit: int) -> None:
        conn = self._connect()
        try:
            surfaces = self._reference_surfaces(conn)
            conn.execute(
                "CREATE TEMP TABLE gc_candidates (artifact_id TEXT PRIMARY KEY) WITHOUT ROWID"
            )
            # Reference checks run against a read snapshot outside the write
            # lock and only for eligible candidates; the authoritative recheck
            # of the bounded marked set happens under BEGIN IMMEDIATE below.
            conn.execute("BEGIN")
            conn.execute(
                "INSERT OR IGNORE INTO temp.gc_candidates(artifact_id) "
                "SELECT t.artifact_id FROM browser_artifact_tickets t "
                "WHERE t.state='consumed' AND t.content_type='image/png' "
                "AND t.artifact_id IS NOT NULL AND t.consumed_at<=? "
                "AND NOT EXISTS (SELECT 1 FROM browser_capture_gc g WHERE g.artifact_id=t.artifact_id) "
                "AND NOT EXISTS (SELECT 1 FROM browser_capture_gc_done d WHERE d.artifact_id=t.artifact_id)",
                (cutoff,),
            )
            referenced = self._referenced(conn, surfaces, "gc_candidates")
            rows = conn.execute(
                "SELECT t.artifact_id,t.owner_id,t.workspace_id FROM browser_artifact_tickets t "
                "JOIN temp.gc_candidates c ON c.artifact_id=t.artifact_id "
                "WHERE t.state='consumed' AND t.content_type='image/png' AND t.consumed_at<=? "
                "ORDER BY t.consumed_at,t.artifact_id",
                (cutoff,),
            )
            selected: list[sqlite3.Row] = []
            seen: set[str] = set()
            for row in rows:
                artifact_id = row["artifact_id"]
                if artifact_id in referenced or artifact_id in seen:
                    continue
                seen.add(artifact_id)
                selected.append(row)
                if len(selected) >= limit:
                    break
            conn.execute("COMMIT")
            for row in selected:
                artifact_id = row["artifact_id"]
                if not isinstance(artifact_id, str) or not artifact_id or len(artifact_id) > 128:
                    raise BrowserCaptureGcError("capture_gc_candidate_invalid")
                self._validate_capture(
                    artifact_id, cutoff, owner_id=row["owner_id"],
                    workspace_id=row["workspace_id"],
                )
            if not selected:
                return
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany(
                "INSERT OR IGNORE INTO browser_capture_gc(artifact_id,marked_at) VALUES(?,?)",
                ((row["artifact_id"], now) for row in selected),
            )
            conn.execute("COMMIT")
        except BrowserCaptureGcError:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        except (sqlite3.Error, OSError) as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise BrowserCaptureGcError(
                "capture_gc_store" if isinstance(exc, sqlite3.Error)
                else "capture_gc_failed"
            ) from exc
        finally:
            conn.close()

    def _finish_marked(self, cutoff: float, limit: int, now: float) -> int:
        conn = self._connect()
        deleted = 0
        try:
            conn.execute(
                "CREATE TEMP TABLE gc_marked (artifact_id TEXT PRIMARY KEY) WITHOUT ROWID"
            )
            conn.execute("BEGIN IMMEDIATE")
            surfaces = self._reference_surfaces(conn)
            conn.execute(
                "INSERT INTO temp.gc_marked(artifact_id) SELECT artifact_id FROM browser_capture_gc "
                "ORDER BY marked_at,artifact_id LIMIT ?",
                (limit,),
            )
            rows = conn.execute(
                "SELECT g.artifact_id FROM browser_capture_gc g "
                "JOIN temp.gc_marked m ON m.artifact_id=g.artifact_id "
                "ORDER BY g.marked_at,g.artifact_id"
            ).fetchall()
            references = self._referenced(conn, surfaces, "gc_marked") if rows else set()
            for row in rows:
                artifact_id = row["artifact_id"]
                artifact_path = self.artifact_root / artifact_id
                quarantine_path = self.artifact_root / f".gc-{artifact_id}"
                if (not artifact_path.exists() and not artifact_path.is_symlink()
                        and not quarantine_path.exists() and not quarantine_path.is_symlink()):
                    conn.execute("DELETE FROM browser_capture_gc WHERE artifact_id=?", (artifact_id,))
                    self._record_done(conn, artifact_id, now)
                    continue
                ticket = conn.execute(
                    "SELECT consumed_at,owner_id,workspace_id FROM browser_artifact_tickets WHERE artifact_id=? AND state='consumed' AND content_type='image/png' LIMIT 1",
                    (artifact_id,),
                ).fetchone()
                if ticket is None or float(ticket["consumed_at"] or 0) > cutoff:
                    conn.execute("DELETE FROM browser_capture_gc WHERE artifact_id=?", (artifact_id,))
                    continue
                if artifact_id in references:
                    conn.execute("DELETE FROM browser_capture_gc WHERE artifact_id=?", (artifact_id,))
                    continue
                self._validate_capture(
                    artifact_id, cutoff, owner_id=ticket["owner_id"],
                    workspace_id=ticket["workspace_id"],
                )
                self._quarantine_and_remove(artifact_id)
                conn.execute("DELETE FROM browser_capture_gc WHERE artifact_id=?", (artifact_id,))
                self._record_done(conn, artifact_id, now)
                deleted += 1
            conn.execute("COMMIT")
            return deleted
        except BrowserCaptureGcError:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        except (sqlite3.Error, OSError) as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise BrowserCaptureGcError(
                "capture_gc_store" if isinstance(exc, sqlite3.Error)
                else "capture_gc_failed"
            ) from exc
        finally:
            conn.close()

    @staticmethod
    def _record_done(conn: sqlite3.Connection, artifact_id: str, now: float) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO browser_capture_gc_done(artifact_id,removed_at) VALUES(?,?)",
            (artifact_id, now),
        )

    def _reference_surfaces(self, conn: sqlite3.Connection) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        """Return (direct artifact_id tables, JSON surfaces) after fail-closed checks."""
        tables = [row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()]
        if not {"run_events", "platform_commands"} <= set(tables):
            raise BrowserCaptureGcError("capture_gc_reference_surface_unknown")
        from hub.infrastructure.workflow_schema import WorkflowSchemaError, inspect_schema
        from hub.infrastructure.artifact_transfer_repository import (
            ArtifactTransferRepositoryError, inspect_transfer_schema,
        )
        try:
            inspect_schema(conn)
            inspect_transfer_schema(conn)
        except (WorkflowSchemaError, ArtifactTransferRepositoryError, sqlite3.Error) as exc:
            raise BrowserCaptureGcError("capture_gc_reference_surface_unknown") from exc
        direct: list[tuple[str, str]] = []
        json_surfaces: list[tuple[str, str]] = []
        for table in tables:
            quoted_table = _quote(table)
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({quoted_table})")}
            direct_column = "report_artifact_id" if table == "workflow_evidence" else "artifact_id"
            if direct_column in columns and table not in _GC_OWN_TABLES:
                direct.append((table, direct_column))
            column = JSON_REFERENCE_SURFACES.get(table)
            if column is None:
                continue
            if column not in columns:
                raise BrowserCaptureGcError("capture_gc_reference_surface_unknown")
            json_surfaces.append((table, column))
        return direct, json_surfaces

    @staticmethod
    def _referenced(conn: sqlite3.Connection,
                    surfaces: tuple[list[tuple[str, str]], list[tuple[str, str]]],
                    id_table: str) -> set[str]:
        """Return which ids in ``temp.<id_table>`` are referenced anywhere.

        Work is driven by the id set: direct columns use ``artifact_id IN``
        lookups, and JSON surfaces are streamed in SQLite so that only rows
        containing an ``"artifact_id"`` key are parsed and only matches for the
        given ids are materialized. No history-sized result reaches Python.
        """
        direct, json_surfaces = surfaces
        quoted_ids = f"temp.{_quote(id_table)}"
        referenced: set[str] = set()
        for table, column in direct:
            quoted_column = _quote(column)
            for row in conn.execute(
                f"SELECT DISTINCT {quoted_column} AS artifact_id FROM {_quote(table)} "
                f"WHERE {quoted_column} IN (SELECT artifact_id FROM {quoted_ids})"
            ):
                referenced.add(row["artifact_id"])
        for table, column in json_surfaces:
            # Workflow snapshots are canonical UTF-8 bytes (BLOB storage).
            # Cast explicitly for SQLite releases that reject JSON BLOB input.
            quoted_column = f"CAST({_quote(column)} AS TEXT)"
            try:
                for row in conn.execute(
                    f"SELECT j.type AS type, j.value AS value FROM {_quote(table)} e, "
                    f"json_tree({quoted_column}) j "
                    f"WHERE {quoted_column} IS NOT NULL "
                    f"AND instr({quoted_column}, '\"artifact_id\"')>0 "
                    "AND j.key='artifact_id' "
                    f"AND (j.type!='text' OR j.value IN (SELECT artifact_id FROM {quoted_ids}))"
                ):
                    value = row["value"]
                    if row["type"] != "text" or not value or len(value) > 128:
                        raise BrowserCaptureGcError("capture_gc_reference_invalid")
                    referenced.add(value)
            except sqlite3.OperationalError as exc:
                if "malformed JSON" in str(exc):
                    raise BrowserCaptureGcError("capture_gc_reference_malformed") from exc
                raise
        return referenced

    def _validate_capture(self, artifact_id: str, cutoff: float, *,
                          owner_id: str, workspace_id: str) -> None:
        if not artifact_id or any(char in artifact_id for char in "/\\"):
            raise BrowserCaptureGcError("capture_gc_artifact_invalid")
        directory = self.artifact_root / artifact_id
        quarantine = self.artifact_root / f".gc-{artifact_id}"
        try:
            if not directory.exists():
                if quarantine.is_symlink() or (quarantine.exists() and not quarantine.is_dir()):
                    raise BrowserCaptureGcError("capture_gc_artifact_invalid")
                if quarantine.is_dir():
                    names = {entry.name for entry in quarantine.iterdir()}
                    if not names <= {"content", "manifest.json"}:
                        raise BrowserCaptureGcError("capture_gc_artifact_invalid")
                    for entry in quarantine.iterdir():
                        if entry.is_symlink() or not entry.is_file():
                            raise BrowserCaptureGcError("capture_gc_artifact_invalid")
                return
            manifest_path = directory / "manifest.json"
            directory_stat = directory.lstat()
            manifest_stat = manifest_path.lstat()
            if (not stat.S_ISDIR(directory_stat.st_mode)
                    or stat.S_ISLNK(directory_stat.st_mode)
                    or stat.S_ISLNK(manifest_stat.st_mode)):
                raise BrowserCaptureGcError("capture_gc_artifact_invalid")
            if {entry.name for entry in directory.iterdir()} != {"manifest.json", "content"}:
                raise BrowserCaptureGcError("capture_gc_artifact_invalid")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (not isinstance(manifest, dict) or manifest.get("artifact_id") != artifact_id
                    or manifest.get("owner_id") != owner_id
                    or manifest.get("workspace_id") != workspace_id
                    or manifest.get("content_type") != "image/png"
                    or manifest.get("name") != "screenshot.png"
                    or type(manifest.get("created_at")) not in (int, float)
                    or float(manifest["created_at"]) > cutoff
                    or type(manifest.get("size")) is not int
                    or not 0 <= manifest["size"] <= 256 * 1024
                    or not isinstance(manifest.get("sha256"), str)
                    or len(manifest["sha256"]) != 64
                    or any(char not in "0123456789abcdef" for char in manifest["sha256"])):
                raise BrowserCaptureGcError("capture_gc_artifact_invalid")
            content = directory / "content"
            content_stat = content.lstat()
            if (stat.S_ISLNK(content_stat.st_mode) or not stat.S_ISREG(content_stat.st_mode)
                    or content_stat.st_size != manifest["size"]):
                raise BrowserCaptureGcError("capture_gc_artifact_invalid")
            digest = hashlib.sha256()
            with content.open("rb") as source:
                signature = source.read(len(PNG_SIGNATURE))
                if signature != PNG_SIGNATURE:
                    raise BrowserCaptureGcError("capture_gc_artifact_invalid")
                digest.update(signature)
                while chunk := source.read(64 * 1024):
                    digest.update(chunk)
            if digest.hexdigest() != manifest["sha256"]:
                raise BrowserCaptureGcError("capture_gc_artifact_invalid")
        except BrowserCaptureGcError:
            raise
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise BrowserCaptureGcError("capture_gc_artifact_invalid") from exc

    def _quarantine_and_remove(self, artifact_id: str) -> None:
        directory = self.artifact_root / artifact_id
        quarantine = self.artifact_root / f".gc-{artifact_id}"
        try:
            directory_is_link = directory.is_symlink()
            quarantine_is_link = quarantine.is_symlink()
            if directory_is_link or quarantine_is_link:
                raise BrowserCaptureGcError("capture_gc_quarantine_invalid")
            if directory.exists() and quarantine.exists():
                raise BrowserCaptureGcError("capture_gc_quarantine_conflict")
            if directory.exists():
                if not directory.is_dir():
                    raise BrowserCaptureGcError("capture_gc_quarantine_invalid")
                directory.rename(quarantine)
            if quarantine.exists():
                if not quarantine.is_dir():
                    raise BrowserCaptureGcError("capture_gc_quarantine_invalid")
                if {entry.name for entry in quarantine.iterdir()} - {"content", "manifest.json"}:
                    raise BrowserCaptureGcError("capture_gc_quarantine_invalid")
                for name in ("content", "manifest.json"):
                    path = quarantine / name
                    if path.exists() or path.is_symlink():
                        if path.is_symlink() or not path.is_file():
                            raise BrowserCaptureGcError("capture_gc_quarantine_invalid")
                        path.unlink()
                quarantine.rmdir()
        except BrowserCaptureGcError:
            raise
        except OSError as exc:
            raise BrowserCaptureGcError("capture_gc_quarantine_failed") from exc


__all__ = ["BrowserCaptureGcError", "BrowserCaptureGcRepository"]
