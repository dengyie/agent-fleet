"""Node-local durable command admission and terminal outcome journal."""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
import json
import sqlite3
from pathlib import Path
from typing import Any, TypedDict


class JournalRecord(TypedDict):
    command_id: str
    state: str
    result: dict[str, Any] | None
    updated_at: float
    receipt_pending: bool
    worker_id: str | None


class JournalAdmission(JournalRecord):
    claimed: bool


class JournalError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


_COLUMNS = "command_id,state,result,updated_at,receipt_pending,worker_id"


class NodeJournal:
    """Only the transaction that inserts a command may dispatch it.

    A running row observed on redelivery is indeterminate. It becomes unknown
    without dispatch, and a late executor cannot overwrite that terminal state.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        try:
            with closing(self._connect()) as conn, conn:
                if write:
                    conn.execute("BEGIN IMMEDIATE")
                yield conn
        except sqlite3.Error as exc:
            raise JournalError("journal_store") from exc

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS command_journal (command_id TEXT PRIMARY KEY, state TEXT NOT NULL, result TEXT, updated_at REAL NOT NULL, receipt_pending INTEGER NOT NULL DEFAULT 0, worker_id TEXT)")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(command_journal)")}
            if "receipt_pending" not in columns:
                conn.execute("ALTER TABLE command_journal ADD COLUMN receipt_pending INTEGER NOT NULL DEFAULT 0")
            if "worker_id" not in columns:
                conn.execute("ALTER TABLE command_journal ADD COLUMN worker_id TEXT")

    @staticmethod
    def _decode(row: sqlite3.Row) -> JournalRecord:
        return {"command_id": row["command_id"], "state": row["state"],
                "result": json.loads(row["result"]) if row["result"] else None,
                "updated_at": row["updated_at"],
                "receipt_pending": bool(row["receipt_pending"]),
                "worker_id": row["worker_id"]}

    def get(self, command_id: str) -> JournalRecord | None:
        with self._transaction() as conn:
            row = conn.execute(f"SELECT {_COLUMNS} FROM command_journal WHERE command_id=? LIMIT 1", (command_id,)).fetchone()
            return self._decode(row) if row is not None else None

    def pending_receipts(self, *, limit: int = 20) -> list[JournalRecord]:
        with self._transaction() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM command_journal WHERE receipt_pending=1 ORDER BY updated_at,command_id LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
            return [self._decode(row) for row in rows]

    def acknowledge_receipt(self, command_id: str) -> None:
        with self._transaction(write=True) as conn:
            conn.execute("UPDATE command_journal SET receipt_pending=0 WHERE command_id=?", (command_id,))

    def begin(self, command_id: str, *, now: float,
              metadata: Mapping[str, str] | None = None,
              worker_id: str | None = None) -> JournalAdmission:
        with self._transaction(write=True) as conn:
            row = conn.execute(f"SELECT {_COLUMNS} FROM command_journal WHERE command_id=? LIMIT 1", (command_id,)).fetchone()
            if row is None:
                initial = dict(metadata) if metadata else None
                conn.execute("INSERT INTO command_journal(command_id,state,result,updated_at,worker_id) VALUES(?,?,?,?,?)",
                             (command_id, "running", json.dumps(initial) if initial else None, float(now), worker_id))
                return {"command_id": command_id, "state": "running", "result": initial,
                        "updated_at": float(now), "receipt_pending": False,
                        "worker_id": worker_id, "claimed": True}
            record = self._decode(row)
            if record["state"] == "running":
                result = {**(metadata or {}), **(record["result"] or {}), "reason": "executor_interrupted"}
                conn.execute("UPDATE command_journal SET state='unknown',result=?,updated_at=?,receipt_pending=1 WHERE command_id=? AND state='running'",
                             (json.dumps(result, separators=(",", ":")), float(now), command_id))
                record = {**record, "state": "unknown", "result": result, "updated_at": float(now), "receipt_pending": True}
            return {**record, "claimed": False}

    def finish(self, command_id: str, state: str, result: dict[str, Any] | None,
               *, now: float, worker_id: str | None = None) -> JournalRecord:
        if state not in {"succeeded", "failed", "unknown"}:
            raise ValueError("invalid journal state")
        with self._transaction(write=True) as conn:
            row = conn.execute(f"SELECT {_COLUMNS} FROM command_journal WHERE command_id=? LIMIT 1", (command_id,)).fetchone()
            if row is None:
                raise JournalError("journal_command_not_found")
            if row["state"] != "running":
                return self._decode(row)
            conn.execute("UPDATE command_journal SET state=?,result=?,updated_at=?,receipt_pending=1,worker_id=COALESCE(worker_id,?) WHERE command_id=? AND state='running'",
                         (state, json.dumps(result or {}, sort_keys=True, separators=(",", ":"), allow_nan=False), float(now), worker_id, command_id))
            row = conn.execute(f"SELECT {_COLUMNS} FROM command_journal WHERE command_id=? LIMIT 1", (command_id,)).fetchone()
            return self._decode(row)

    def recover_unknown(self, *, now: float) -> None:
        with self._transaction(write=True) as conn:
            conn.execute("UPDATE command_journal SET state='unknown',receipt_pending=1,updated_at=? WHERE state='running'", (float(now),))


__all__ = ["NodeJournal", "JournalError"]
