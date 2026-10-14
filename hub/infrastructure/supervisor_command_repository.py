"""Durable supervisor command outbox.

The supervisor queue is a delivery boundary, so command envelopes must survive
Hub restarts. This repository stores bounded command metadata and the signed
wire envelope; progress receipts move monotonically and the first terminal
receipt wins. Delivery uses a short lease so an interrupted poll can retry.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping


class SupervisorCommandRepositoryError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class SupervisorCommandRepository:
    """SQLite-backed command outbox with atomic claim and receipt updates."""

    _AUDIT_CAP = 5000

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=10, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            return conn
        except (sqlite3.Error, OSError) as exc:
            if conn is not None:
                conn.close()
            raise SupervisorCommandRepositoryError("command_unavailable") from exc

    def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS supervisor_commands (
                    command_id TEXT PRIMARY KEY,
                    machine_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    attempt_id TEXT,
                    action TEXT NOT NULL,
                    nonce TEXT NOT NULL UNIQUE,
                    idempotency_key TEXT UNIQUE,
                    wire_json TEXT NOT NULL,
                    issued_s REAL NOT NULL,
                    expires_s REAL NOT NULL,
                    status TEXT NOT NULL,
                    lease_until_s REAL NOT NULL DEFAULT 0,
                    receipt_status TEXT NOT NULL DEFAULT '',
                    receipt_reason TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_supervisor_poll
                    ON supervisor_commands(machine_id, status, lease_until_s, issued_s, command_id);
                CREATE INDEX IF NOT EXISTS idx_supervisor_target
                    ON supervisor_commands(session_id, attempt_id, status);
                CREATE INDEX IF NOT EXISTS idx_supervisor_expiry
                    ON supervisor_commands(expires_s, command_id)
                    WHERE status NOT IN ('succeeded','already_finished','failed','rejected','expired');
                CREATE TABLE IF NOT EXISTS supervisor_audit_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_json TEXT NOT NULL
                );
                """
            )
        except (sqlite3.Error, OSError) as exc:
            raise SupervisorCommandRepositoryError("command_unavailable") from exc
        finally:
            conn.close()

    @classmethod
    def _append_audit_rows(cls, conn: sqlite3.Connection,
                           rows: list[Mapping[str, Any]]) -> None:
        if not rows:
            return
        conn.executemany(
            "INSERT INTO supervisor_audit_events(event_json) VALUES (?)",
            [(json.dumps(dict(row), ensure_ascii=True, separators=(",", ":")),)
             for row in rows],
        )
        conn.execute("DELETE FROM supervisor_audit_events WHERE event_id<=?",
                     (conn.execute(
                         "SELECT COALESCE(MAX(event_id), 0)-? FROM supervisor_audit_events",
                         (cls._AUDIT_CAP,)).fetchone()[0],))

    def audit_recent(self, limit: int = 200) -> list[dict[str, Any]]:
        bounded_limit = max(0, min(int(limit), self._AUDIT_CAP))
        if bounded_limit == 0:
            return []
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT event_json FROM supervisor_audit_events "
                "ORDER BY event_id DESC LIMIT ?", (bounded_limit,)
            ).fetchall()
            decoded = []
            for row in reversed(rows):
                try:
                    event = json.loads(row["event_json"])
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise SupervisorCommandRepositoryError("audit_corrupt") from exc
                if not isinstance(event, dict):
                    raise SupervisorCommandRepositoryError("audit_corrupt")
                decoded.append(event)
            return decoded
        except (sqlite3.Error, OSError) as exc:
            raise SupervisorCommandRepositoryError("command_unavailable") from exc
        finally:
            conn.close()

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        try:
            wire = json.loads(row["wire_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SupervisorCommandRepositoryError("command_corrupt") from exc
        if not isinstance(wire, dict):
            raise SupervisorCommandRepositoryError("command_corrupt")
        return {
            "command_id": row["command_id"], "machine": row["machine_id"],
            "session_id": row["session_id"], "attempt_id": row["attempt_id"],
            "action": row["action"], "nonce": row["nonce"], "wire": wire,
            "issued_s": float(row["issued_s"]), "expires_s": float(row["expires_s"]),
            "status": row["status"], "lease_until_s": float(row["lease_until_s"]),
            "receipt_status": row["receipt_status"], "receipt_reason": row["receipt_reason"],
            "idempotency_key": row["idempotency_key"],
        }

    def find_idempotency(self, key: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            try:
                return self._decode(conn.execute(
                    "SELECT * FROM supervisor_commands WHERE idempotency_key=?", (key,)
                ).fetchone())
            except (sqlite3.Error, OSError) as exc:
                raise SupervisorCommandRepositoryError("command_unavailable") from exc
        finally:
            conn.close()

    def find_command(self, command_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            try:
                return self._decode(conn.execute(
                    "SELECT * FROM supervisor_commands WHERE command_id=?", (command_id,)
                ).fetchone())
            except (sqlite3.Error, OSError) as exc:
                raise SupervisorCommandRepositoryError("command_unavailable") from exc
        finally:
            conn.close()

    def nonce_exists(self, nonce: str) -> bool:
        conn = self._connect()
        try:
            try:
                return conn.execute(
                    "SELECT 1 FROM supervisor_commands WHERE nonce=?", (nonce,)
                ).fetchone() is not None
            except (sqlite3.Error, OSError) as exc:
                raise SupervisorCommandRepositoryError("command_unavailable") from exc
        finally:
            conn.close()

    def target_owner(self, session_id: str, attempt_id: str | None) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            try:
                row = conn.execute(
                    "SELECT * FROM supervisor_commands WHERE session_id=? AND "
                    "((attempt_id IS NULL AND ? IS NULL) OR attempt_id=?) AND "
                    "status NOT IN ('succeeded','already_finished','failed','rejected','expired') "
                    "ORDER BY issued_s DESC LIMIT 1", (session_id, attempt_id, attempt_id)
                ).fetchone()
                return self._decode(row)
            except (sqlite3.Error, OSError) as exc:
                raise SupervisorCommandRepositoryError("command_unavailable") from exc
        finally:
            conn.close()

    def insert(self, *, command_id: str, machine: str, session_id: str,
               attempt_id: str | None, action: str, nonce: str,
               idempotency_key: str | None, wire: Mapping[str, Any],
               issued_s: float, expires_s: float,
               audit_row: Mapping[str, Any] | None = None) -> dict[str, Any]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                existing = conn.execute(
                    "SELECT * FROM supervisor_commands WHERE idempotency_key=?",
                    (idempotency_key,)).fetchone()
                if existing is not None:
                    conn.commit()
                    return self._decode(existing)  # type: ignore[return-value]
            target = conn.execute(
                "SELECT action FROM supervisor_commands WHERE session_id=? AND "
                "((attempt_id IS NULL AND ? IS NULL) OR attempt_id=?) AND "
                "status IN ('queued','delivered') ORDER BY issued_s DESC LIMIT 1",
                (session_id, attempt_id, attempt_id),
            ).fetchone()
            if (target and target["action"] in ("terminate_session", "quarantine_session")
                    and action in ("resume_session", "append_user_turn")):
                conn.rollback()
                raise SupervisorCommandRepositoryError("resume_superseded")
            superseded_audit_rows = []
            if action in ("terminate_session", "quarantine_session"):
                if audit_row is not None:
                    superseded_audit_rows = conn.execute(
                        "SELECT command_id, action FROM supervisor_commands WHERE session_id=? AND "
                        "((attempt_id IS NULL AND ? IS NULL) OR attempt_id=?) AND "
                        "action IN ('resume_session','append_user_turn') "
                        "AND status IN ('queued','delivered') "
                        "ORDER BY issued_s DESC, command_id DESC LIMIT ?",
                        (session_id, attempt_id, attempt_id, self._AUDIT_CAP),
                    ).fetchall()
                    superseded_audit_rows.reverse()
                conn.execute(
                    "UPDATE supervisor_commands SET status='expired', "
                    "receipt_status='expired', receipt_reason='superseded', lease_until_s=0 "
                    "WHERE session_id=? AND "
                    "((attempt_id IS NULL AND ? IS NULL) OR attempt_id=?) AND "
                    "action IN ('resume_session','append_user_turn') "
                    "AND status IN ('queued','delivered')",
                    (session_id, attempt_id, attempt_id),
                )
            conn.execute(
                "INSERT INTO supervisor_commands(command_id,machine_id,session_id,attempt_id,"
                "action,nonce,idempotency_key,wire_json,issued_s,expires_s,status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?, 'queued')",
                (command_id, machine, session_id, attempt_id, action, nonce,
                 idempotency_key, json.dumps(dict(wire), ensure_ascii=True, separators=(",", ":")),
                 float(issued_s), float(expires_s)),
            )
            row = conn.execute("SELECT * FROM supervisor_commands WHERE command_id=?",
                               (command_id,)).fetchone()
            if audit_row is not None:
                audit_rows = [
                    {**dict(audit_row), "event": "supersede",
                     "command_id": old["command_id"], "action": old["action"],
                     "by": action}
                    for old in superseded_audit_rows
                ]
                audit_rows.append(dict(audit_row))
                self._append_audit_rows(conn, audit_rows)
            conn.commit()
            return self._decode(row)  # type: ignore[return-value]
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            if idempotency_key:
                existing = conn.execute(
                    "SELECT * FROM supervisor_commands WHERE idempotency_key=?",
                    (idempotency_key,)).fetchone()
                if existing is not None:
                    return self._decode(existing)  # type: ignore[return-value]
            raise SupervisorCommandRepositoryError("command_conflict") from exc
        except (sqlite3.Error, OSError) as exc:
            conn.rollback()
            raise SupervisorCommandRepositoryError("command_unavailable") from exc
        finally:
            conn.close()

    def claim_available(self, machine: str, now_s: float, limit: int,
                        lease_until_s: float,
                        audit_row: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        """Atomically lease a poll batch so concurrent workers cannot double-send."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT command_id FROM supervisor_commands WHERE machine_id=? AND "
                "status IN ('queued','delivered') AND lease_until_s<=? AND expires_s>=? "
                "ORDER BY issued_s ASC, command_id ASC LIMIT ?",
                (machine, float(now_s), float(now_s), int(limit)),
            ).fetchall()
            ids = [row["command_id"] for row in rows]
            for command_id in ids:
                conn.execute(
                    "UPDATE supervisor_commands SET status='delivered',lease_until_s=? "
                    "WHERE command_id=? AND status IN ('queued','delivered') AND lease_until_s<=?",
                    (float(lease_until_s), command_id, float(now_s)))
            claimed = [self._decode(conn.execute(
                "SELECT * FROM supervisor_commands WHERE command_id=?", (command_id,)
            ).fetchone()) for command_id in ids]
            if audit_row is not None:
                self._append_audit_rows(conn, [
                    {**dict(audit_row), "command_id": row["command_id"],
                     "action": row["action"]}
                    for row in claimed if row is not None
                ])
            conn.commit()
            return [row for row in claimed if row is not None]
        except (sqlite3.Error, OSError) as exc:
            conn.rollback()
            raise SupervisorCommandRepositoryError("command_unavailable") from exc
        finally:
            conn.close()

    def mark_status(self, command_id: str, status: str, receipt_status: str,
                    receipt_reason: str,
                    audit_row: Mapping[str, Any] | None = None, *,
                    now_s: float | None = None,
                    audit_ts: str | None = None
                    ) -> tuple[dict[str, Any] | None, bool]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM supervisor_commands WHERE command_id=?",
                               (command_id,)).fetchone()
            if row is None:
                conn.rollback()
                return None, False
            current = row["status"]
            current_progress = {"accepted": 0, "executing": 1}.get(current, -1)
            incoming_progress = {"accepted": 0, "executing": 1}.get(status, -1)
            terminal = current in ('succeeded', 'already_finished', 'failed', 'rejected', 'expired')
            if (not terminal and now_s is not None
                    and float(row["expires_s"]) < float(now_s)):
                conn.execute(
                    "UPDATE supervisor_commands SET status='expired', "
                    "receipt_status='expired', receipt_reason='expired', lease_until_s=0 "
                    "WHERE command_id=?", (command_id,))
                row = conn.execute("SELECT * FROM supervisor_commands WHERE command_id=?",
                                   (command_id,)).fetchone()
                if audit_ts is not None:
                    self._append_audit_rows(conn, [{
                        "event": "expire", "ts": audit_ts,
                        "command_id": command_id,
                    }])
                conn.commit()
                return self._decode(row), False  # type: ignore[return-value]
            advances = not terminal and (incoming_progress < 0 or incoming_progress > current_progress)
            first = not bool(row["receipt_status"]) and advances
            if advances:
                conn.execute("UPDATE supervisor_commands SET status=?, receipt_status=?, "
                             "receipt_reason=?, lease_until_s=0 WHERE command_id=?",
                             (status, receipt_status, receipt_reason[:120], command_id))
                row = conn.execute("SELECT * FROM supervisor_commands WHERE command_id=?",
                                   (command_id,)).fetchone()
                if audit_row is not None:
                    self._append_audit_rows(conn, [dict(audit_row)])
            conn.commit()
            return self._decode(row), first  # type: ignore[return-value]
        except (sqlite3.Error, OSError) as exc:
            conn.rollback()
            raise SupervisorCommandRepositoryError("command_unavailable") from exc
        finally:
            conn.close()

    def expire(self, now_s: float, *, audit_ts: str | None = None) -> int:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            audit_ids = []
            if audit_ts is not None:
                audit_ids = conn.execute(
                    "SELECT command_id FROM supervisor_commands WHERE expires_s<? AND "
                    "status NOT IN ('succeeded','already_finished','failed','rejected','expired') "
                    "ORDER BY expires_s DESC, command_id DESC LIMIT ?",
                    (float(now_s), self._AUDIT_CAP),
                ).fetchall()
                audit_ids.reverse()
            cur = conn.execute(
                "UPDATE supervisor_commands SET status='expired', "
                "receipt_status='expired', receipt_reason='expired', lease_until_s=0 "
                "WHERE expires_s<? AND "
                "status NOT IN ('succeeded','already_finished','failed','rejected','expired')",
                (float(now_s),),
            )
            if audit_ts is not None and audit_ids:
                self._append_audit_rows(conn, [
                    {"event": "expire", "ts": audit_ts,
                     "command_id": row["command_id"]}
                    for row in audit_ids
                ])
            conn.commit()
            return int(cur.rowcount)
        except (sqlite3.Error, OSError) as exc:
            conn.rollback()
            raise SupervisorCommandRepositoryError("command_unavailable") from exc
        finally:
            conn.close()

__all__ = ["SupervisorCommandRepository", "SupervisorCommandRepositoryError"]
