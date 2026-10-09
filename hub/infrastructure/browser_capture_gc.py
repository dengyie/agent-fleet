"""Bounded retention and crash recovery for published browser PNG captures."""
from __future__ import annotations

import json
import hashlib
import math
import sqlite3
import stat
import time
from pathlib import Path
from typing import Any, Callable

from tools.platform.artifact_lock import artifact_snapshot_barrier

CAPTURE_RETENTION_SECONDS = 30 * 24 * 60 * 60
GC_BATCH_LIMIT = 128
REFERENCE_LIMIT = 4096
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class BrowserCaptureGcError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _artifact_ids(value: Any, out: set[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "artifact_id":
                if not isinstance(child, str) or not child or len(child) > 128:
                    raise BrowserCaptureGcError("capture_gc_reference_invalid")
                out.add(child)
            _artifact_ids(child, out)
    elif isinstance(value, list):
        for child in value:
            _artifact_ids(child, out)


class BrowserCaptureGcRepository:
    """Keep SQLite reference checks and filesystem quarantine under one barrier."""

    def __init__(self, db_path: Path, artifact_root: Path, *, clock: Callable[[], float] = time.time):
        self.db_path = Path(db_path)
        self.artifact_root = Path(artifact_root).expanduser().resolve()
        self.clock = clock

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init(self) -> None:
        conn = None
        try:
            conn = self._connect()
            conn.execute("CREATE TABLE IF NOT EXISTS browser_capture_gc (artifact_id TEXT PRIMARY KEY, marked_at REAL NOT NULL)")
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
                return self._finish_marked(cutoff, limit)
        except BrowserCaptureGcError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise BrowserCaptureGcError("capture_gc_failed") from exc

    def _mark_candidates(self, cutoff: float, now: float, limit: int) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            references = self._collect_references(conn)
            conn.execute(
                "CREATE TEMP TABLE gc_references (artifact_id TEXT PRIMARY KEY) WITHOUT ROWID"
            )
            conn.executemany(
                "INSERT INTO gc_references(artifact_id) VALUES(?)",
                ((artifact_id,) for artifact_id in references),
            )
            rows = conn.execute(
                "SELECT t.artifact_id,t.owner_id,t.workspace_id FROM browser_artifact_tickets t "
                "LEFT JOIN browser_capture_gc g ON g.artifact_id=t.artifact_id "
                "LEFT JOIN gc_references r ON r.artifact_id=t.artifact_id "
                "WHERE t.state='consumed' AND t.content_type='image/png' "
                "AND t.artifact_id IS NOT NULL AND t.consumed_at<=? "
                "AND g.artifact_id IS NULL AND r.artifact_id IS NULL"
                " ORDER BY t.consumed_at,t.artifact_id LIMIT ?",
                (cutoff, limit),
            ).fetchall()
            for row in rows:
                artifact_id = row["artifact_id"]
                if not isinstance(artifact_id, str) or not artifact_id or len(artifact_id) > 128:
                    raise BrowserCaptureGcError("capture_gc_candidate_invalid")
                self._validate_capture(
                    artifact_id, cutoff, owner_id=row["owner_id"],
                    workspace_id=row["workspace_id"],
                )
                conn.execute(
                    "INSERT OR IGNORE INTO browser_capture_gc(artifact_id,marked_at) VALUES(?,?)",
                    (artifact_id, now),
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

    def _finish_marked(self, cutoff: float, limit: int) -> int:
        conn = self._connect()
        deleted = 0
        try:
            conn.execute("BEGIN IMMEDIATE")
            references = self._collect_references(conn)
            rows = conn.execute(
                "SELECT artifact_id FROM browser_capture_gc ORDER BY marked_at,artifact_id LIMIT ?",
                (limit,),
            ).fetchall()
            for row in rows:
                artifact_id = row["artifact_id"]
                artifact_path = self.artifact_root / artifact_id
                quarantine_path = self.artifact_root / f".gc-{artifact_id}"
                if (not artifact_path.exists() and not artifact_path.is_symlink()
                        and not quarantine_path.exists() and not quarantine_path.is_symlink()):
                    conn.execute("DELETE FROM browser_capture_gc WHERE artifact_id=?", (artifact_id,))
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

    def _collect_references(self, conn: sqlite3.Connection) -> set[str]:
        references: set[str] = set()
        scanned_rows = 0
        tables = [row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()]
        if len(tables) > REFERENCE_LIMIT:
            raise BrowserCaptureGcError("capture_gc_reference_surface_unknown")
        if not {"run_events", "platform_commands"} <= set(tables):
            raise BrowserCaptureGcError("capture_gc_reference_surface_unknown")
        json_surfaces = {
            "execution_window_events": "payload",
            "run_events": "payload",
            "platform_commands": "result",
        }
        for table in tables:
            quoted_table = '"' + table.replace('"', '""') + '"'
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({quoted_table})")}
            if "artifact_id" not in columns or table in {"browser_artifact_tickets", "browser_capture_gc"}:
                direct_rows = []
            else:
                direct_rows = conn.execute(
                    f"SELECT artifact_id FROM {quoted_table} WHERE artifact_id IS NOT NULL LIMIT ?",
                    (REFERENCE_LIMIT - scanned_rows + 1,),
                ).fetchall()
                scanned_rows += len(direct_rows)
                if scanned_rows > REFERENCE_LIMIT:
                    raise BrowserCaptureGcError("capture_gc_reference_limit")
                for row in direct_rows:
                    value = row["artifact_id"]
                    if not isinstance(value, str) or not value or len(value) > 128:
                        raise BrowserCaptureGcError("capture_gc_reference_malformed")
                    references.add(value)
                    if len(references) > REFERENCE_LIMIT:
                        raise BrowserCaptureGcError("capture_gc_reference_limit")

            column = json_surfaces.get(table)
            if column is None:
                continue
            if column not in columns:
                raise BrowserCaptureGcError("capture_gc_reference_surface_unknown")
            rows = conn.execute(
                f"SELECT {column} FROM {quoted_table} WHERE {column} IS NOT NULL LIMIT ?",
                (REFERENCE_LIMIT - scanned_rows + 1,),
            ).fetchall()
            scanned_rows += len(rows)
            if scanned_rows > REFERENCE_LIMIT:
                raise BrowserCaptureGcError("capture_gc_reference_limit")
            for row in rows:
                try:
                    payload = json.loads(row[column])
                except (TypeError, ValueError) as exc:
                    raise BrowserCaptureGcError("capture_gc_reference_malformed") from exc
                if not isinstance(payload, dict):
                    raise BrowserCaptureGcError("capture_gc_reference_malformed")
                _artifact_ids(payload, references)
                if len(references) > REFERENCE_LIMIT:
                    raise BrowserCaptureGcError("capture_gc_reference_limit")
        return references

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
