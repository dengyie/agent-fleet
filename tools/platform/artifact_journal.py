"""Independent durable Node input ledger; no command replay is needed to recover."""
from __future__ import annotations

from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from fleet_core.artifact_receive import ArtifactReceiveError, ReceiveRequest, ReceiveReceipt

_COLUMNS = "command_id,grant_id,command_digest,command,state,root_device,root_inode,stage_device,stage_inode,receipt,acknowledged,attempts,updated_at"


class ArtifactReceiveJournal:
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
            raise ArtifactReceiveError("receive_journal_store", uncertain=True) from exc

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS artifact_receive_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1),schema_version INTEGER NOT NULL)")
            connection.execute("INSERT OR IGNORE INTO artifact_receive_meta VALUES(1,1)")
            if tuple(connection.execute("SELECT schema_version FROM artifact_receive_meta LIMIT 2").fetchone()) != (1,):
                raise ArtifactReceiveError("receive_journal_schema")
            connection.execute("""CREATE TABLE IF NOT EXISTS artifact_receives (
                command_id TEXT PRIMARY KEY,grant_id TEXT NOT NULL UNIQUE,command_digest TEXT NOT NULL,
                command TEXT NOT NULL CHECK(length(command)<=65536),attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3),
                state TEXT NOT NULL CHECK(state IN ('receiving','publishing','ready','reconciliation')),failure_code TEXT,
                root_device TEXT NOT NULL,root_inode TEXT NOT NULL,stage_device TEXT,stage_inode TEXT,
                receipt TEXT NOT NULL,acknowledged INTEGER NOT NULL DEFAULT 0 CHECK(acknowledged IN (0,1)),updated_at REAL NOT NULL)""")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_receive_pending ON artifact_receives(acknowledged,state,command_id)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_receive_recovery ON artifact_receives(state,attempts,command_id)")

    def get(self, command_id: str) -> dict[str, Any]:
        with self.transaction() as connection:
            row = connection.execute(f"SELECT {_COLUMNS} FROM artifact_receives WHERE command_id=? LIMIT 1", (command_id,)).fetchone()
            if row is None:
                raise ArtifactReceiveError("receive_journal_corrupt")
            return dict(row)

    def reconcile(self, command_id: str, code: str, *, now: float) -> None:
        with self.transaction() as connection:
            connection.execute("UPDATE artifact_receives SET state='reconciliation',failure_code=?,updated_at=? WHERE command_id=? AND acknowledged=0", (code[:80], now, command_id))

    def begin(self, request: ReceiveRequest, *, command: dict[str, Any], device: int, inode: int, now: float) -> dict[str, Any]:
        receipt = json.dumps(request.receipt(), sort_keys=True, separators=(",", ":"))
        with self.transaction() as connection:
            row = connection.execute(f"SELECT {_COLUMNS} FROM artifact_receives WHERE command_id=? LIMIT 1", (request.command_id,)).fetchone()
            if row is not None:
                if (row["command_digest"], row["grant_id"], row["root_device"], row["root_inode"], row["receipt"]) != (
                        request.command_digest, request.grant_id, str(device), str(inode), receipt):
                    raise ArtifactReceiveError("receive_conflict")
                return dict(row)
            connection.execute("INSERT INTO artifact_receives(command_id,grant_id,command_digest,command,state,root_device,root_inode,receipt,updated_at) VALUES(?,?,?,?,'receiving',?,?,?,?)",
                (request.command_id, request.grant_id, request.command_digest,
                 json.dumps(command, sort_keys=True, separators=(",", ":"), allow_nan=False), str(device), str(inode), receipt, now))
            return dict(connection.execute(f"SELECT {_COLUMNS} FROM artifact_receives WHERE command_id=? LIMIT 1", (request.command_id,)).fetchone())

    def attach_stage(self, command_id: str, *, device: int, inode: int, now: float) -> None:
        with self.transaction() as connection:
            cursor = connection.execute("UPDATE artifact_receives SET stage_device=?,stage_inode=?,updated_at=? WHERE command_id=? AND state='receiving' AND stage_inode IS NULL",
                (str(device), str(inode), now, command_id))
            if cursor.rowcount != 1:
                raise ArtifactReceiveError("receive_conflict")

    def attempt(self, command_id: str, *, now: float) -> None:
        with self.transaction() as connection:
            cursor = connection.execute("UPDATE artifact_receives SET attempts=attempts+1,updated_at=? WHERE command_id=? AND state='receiving' AND attempts<3", (now, command_id))
            if cursor.rowcount != 1:
                raise ArtifactReceiveError("receive_attempt_limit")

    def incomplete(self, *, limit: int = 20) -> tuple[dict[str, Any], ...]:
        with self.transaction() as connection:
            rows = connection.execute("SELECT command FROM artifact_receives WHERE state='publishing' OR (state='receiving' AND attempts<3) ORDER BY command_id LIMIT ?", (max(1, min(limit, 100)),)).fetchall()
            try:
                commands = tuple(json.loads(row[0]) for row in rows)
            except (TypeError, ValueError) as exc:
                raise ArtifactReceiveError("receive_journal_corrupt") from exc
            if any(type(command) is not dict for command in commands):
                raise ArtifactReceiveError("receive_journal_corrupt")
            return commands

    def mark(self, command_id: str, state: str, *, now: float) -> None:
        if state not in {"publishing", "ready"}:
            raise ArtifactReceiveError("receive_conflict")
        with self.transaction() as connection:
            cursor = connection.execute("UPDATE artifact_receives SET state=?,updated_at=? WHERE command_id=? AND (state=? OR state=?)",
                (state, now, command_id, state, "receiving" if state == "publishing" else "publishing"))
            if cursor.rowcount != 1:
                raise ArtifactReceiveError("receive_conflict")

    def pending(self, *, limit: int = 20) -> tuple[dict[str, Any], ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ArtifactReceiveError("invalid_receive_limit")
        with self.transaction() as connection:
            rows = connection.execute(f"SELECT {_COLUMNS} FROM artifact_receives WHERE state='ready' AND acknowledged=0 ORDER BY command_id LIMIT ?", (limit,)).fetchall()
            return tuple(dict(row) for row in rows)

    def acknowledge(self, command_id: str, receipt: ReceiveReceipt, *, now: float) -> None:
        encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
        with self.transaction() as connection:
            cursor = connection.execute("UPDATE artifact_receives SET acknowledged=1,updated_at=? WHERE command_id=? AND state='ready' AND receipt=?", (now, command_id, encoded))
            if cursor.rowcount != 1:
                raise ArtifactReceiveError("invalid_receive_receipt")
