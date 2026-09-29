"""Node-local durable command journal."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class NodeJournal:
    """Persists command execution before/after the side effect.

    ``command_id`` is unique.  A completed row is returned on redelivery so a
    node never executes a known-complete side effect twice.  A row left in
    ``running`` after a crash is explicitly reported as ``unknown`` until an
    executor reconciles it.
    """
    def __init__(self, path: Path):
        self.path = Path(path)

    def _connect(self):
        conn = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def init(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS command_journal (command_id TEXT PRIMARY KEY, state TEXT NOT NULL, result TEXT, updated_at REAL NOT NULL)")

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        return {"command_id": row["command_id"], "state": row["state"],
                "result": json.loads(row["result"]) if row["result"] else None,
                "updated_at": row["updated_at"]}

    def get(self, command_id: str):
        with self._connect() as conn:
            return self._decode(conn.execute("SELECT * FROM command_journal WHERE command_id=?", (command_id,)).fetchone())

    def begin(self, command_id: str, *, now: float) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM command_journal WHERE command_id=?", (command_id,)).fetchone()
            if row is not None:
                return self._decode(row)
            conn.execute("INSERT INTO command_journal(command_id,state,updated_at) VALUES(?,?,?)", (command_id, "running", float(now)))
            return {"command_id": command_id, "state": "running", "result": None, "updated_at": float(now)}

    def finish(self, command_id: str, state: str, result: dict[str, Any] | None, *, now: float):
        if state not in {"succeeded", "failed", "unknown"}:
            raise ValueError("invalid journal state")
        with self._connect() as conn:
            conn.execute("UPDATE command_journal SET state=?,result=?,updated_at=? WHERE command_id=?", (state, json.dumps(result or {}, sort_keys=True, separators=(",", ":")), float(now), command_id))
            return self.get(command_id)

    def recover_unknown(self, *, now: float):
        with self._connect() as conn:
            conn.execute("UPDATE command_journal SET state='unknown',updated_at=? WHERE state='running'", (float(now),))


__all__ = ["NodeJournal"]
