"""Immutable, command-bound continuation snapshots in the session database.

The migration has its own metadata key and runs in one IMMEDIATE transaction;
existing session schema/version ownership is left to SessionRepository.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from fleet_core.continuation_result import metadata, validate_result
from hub.domain.continuation_results import ContinuationResultStoreError
from hub.infrastructure.sqlite import connect

_VERSION_KEY = "continuation_results_schema"
_COLUMNS = {"result_cursor", "command_id", "session_id", "machine_id", "digest", "result_json"}


def _decode(row):
    value = validate_result(json.loads(row["result_json"]))
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if (value["command_id"] != row["command_id"] or value["session_id"] != row["session_id"]
            or hashlib.sha256(text.encode("utf-8")).hexdigest() != row["digest"]):
        raise ValueError("invalid_result_store")
    return value


class ContinuationResultRepository:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    def _connect(self):
        return connect(self.db_path)

    def init(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            if {row["name"] for row in conn.execute("PRAGMA table_info(meta)")} != {"key", "value"}:
                raise sqlite3.DatabaseError("invalid metadata")
            stored = conn.execute("SELECT value FROM meta WHERE key=?", (_VERSION_KEY,)).fetchone()
            if stored is not None and stored["value"] != "1":
                raise sqlite3.DatabaseError("unsupported result schema")
            conn.execute("CREATE TABLE IF NOT EXISTS continuation_results ("
                         "result_cursor INTEGER PRIMARY KEY AUTOINCREMENT,"
                         "command_id TEXT NOT NULL UNIQUE,session_id TEXT NOT NULL,"
                         "machine_id TEXT NOT NULL,digest TEXT NOT NULL,result_json TEXT NOT NULL)")
            info = {row["name"]: row for row in conn.execute("PRAGMA table_info(continuation_results)")}
            if (set(info) != _COLUMNS or info["result_cursor"]["pk"] != 1
                    or info["result_cursor"]["type"].upper() != "INTEGER"
                    or any(info[key]["type"].upper() != "TEXT" or info[key]["notnull"] != 1
                           for key in _COLUMNS - {"result_cursor"})):
                raise sqlite3.DatabaseError("invalid result schema")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_continuation_command ON continuation_results(command_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_continuation_page ON continuation_results(session_id,result_cursor)")
            indexes = {row['name']: row for row in conn.execute('PRAGMA index_list(continuation_results)')}
            for name, expected_columns, unique in (
                    ('idx_continuation_command', ['command_id'], 1),
                    ('idx_continuation_page', ['session_id', 'result_cursor'], 0)):
                index = indexes.get(name)
                actual = [row['name'] for row in conn.execute(f"PRAGMA index_info('{name}')")]
                if (index is None or index['unique'] != unique or index['partial'] != 0
                        or actual != expected_columns):
                    raise sqlite3.DatabaseError('invalid result index')
            conn.execute("INSERT INTO meta(key,value) VALUES (?, '1') ON CONFLICT(key) DO UPDATE SET value='1'", (_VERSION_KEY,))
            conn.commit()
        except (sqlite3.Error, OSError, ValueError) as exc:
            if conn is not None and conn.in_transaction:
                conn.rollback()
            raise ContinuationResultStoreError() from exc
        finally:
            if conn is not None:
                conn.close()

    def put(self, machine: str, value: dict) -> tuple[dict, bool]:
        conn = None
        try:
            value = validate_result(value)
            text = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute("SELECT * FROM continuation_results WHERE command_id=?", (value["command_id"],)).fetchone()
            if prior is not None:
                if prior["session_id"] != value["session_id"] or prior["machine_id"] != machine or prior["digest"] != digest:
                    raise ContinuationResultStoreError("continuation_result_conflict")
                conn.commit()
                return {**_decode(prior), "cursor": str(prior["result_cursor"])}, False
            result = conn.execute("INSERT INTO continuation_results(command_id,session_id,machine_id,digest,result_json) VALUES (?,?,?,?,?)",
                                 (value["command_id"], value["session_id"], machine, digest, text))
            conn.commit()
            return {**json.loads(text), "cursor": str(result.lastrowid)}, True
        except ContinuationResultStoreError:
            raise
        except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
            raise ContinuationResultStoreError() from exc
        finally:
            if conn is not None:
                if conn.in_transaction:
                    conn.rollback()
                conn.close()

    def page(self, session_id: str, *, limit: int, before: int | None) -> dict:
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN")
            high = conn.execute("SELECT COALESCE(MAX(result_cursor),0) FROM continuation_results").fetchone()[0]
            if before is not None and before > high:
                raise ContinuationResultStoreError("invalid_cursor")
            rows = conn.execute("SELECT * FROM continuation_results WHERE session_id=? "
                                + ("AND result_cursor<? " if before is not None else "")
                                + "ORDER BY result_cursor DESC LIMIT ?",
                                (session_id, before, limit + 1) if before is not None else (session_id, limit + 1)).fetchall()
            conn.commit()
            visible = rows[:limit]
            out = [{**metadata(_decode(row)), "cursor": str(row["result_cursor"])} for row in visible]
            return {"results": out, "has_more": len(rows) > limit,
                    "before_cursor": str(visible[-1]["result_cursor"]) if visible else str(before or 0)}
        except ContinuationResultStoreError:
            raise
        except (sqlite3.Error, OSError, ValueError, TypeError, RecursionError) as exc:
            raise ContinuationResultStoreError() from exc
        finally:
            if conn is not None:
                if conn.in_transaction:
                    conn.rollback()
                conn.close()

    def get(self, session_id: str, command_id: str) -> dict | None:
        conn = None
        try:
            conn = self._connect()
            row = conn.execute("SELECT * FROM continuation_results WHERE session_id=? AND command_id=?", (session_id, command_id)).fetchone()
            return {**_decode(row), "cursor": str(row["result_cursor"])} if row else None
        except (sqlite3.Error, OSError, ValueError, TypeError, RecursionError) as exc:
            raise ContinuationResultStoreError() from exc
        finally:
            if conn is not None:
                conn.close()
