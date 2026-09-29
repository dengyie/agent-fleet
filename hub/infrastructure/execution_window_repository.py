"""Durable execution-window tickets and exclusive writer leases.

This module deliberately implements only the control plane. It never starts a
PTY/browser process and never accepts a host command or executable.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping

from hub.domain.execution_window import (
    TERMINAL_WINDOW_STATES, WINDOW_EVENT_FIELDS, WINDOW_EVENT_KINDS,
    WINDOW_STATES, public_window,
)
from platform_schema import validate_id, validate_owner_id

MAX_METADATA_BYTES = 16 * 1024
MAX_EVENT_BYTES = 32 * 1024
MAX_EVENT_TEXT = 8 * 1024
MAX_EVENT_LIMIT = 200
MAX_WINDOW_LIST_LIMIT = 50
_FORBIDDEN_EVENT_MARKERS = (
    "command", "argv", "executable", "shell", "path", "pid", "pty",
    "browser", "process", "secret", "token", "password", "credential",
    "lease",
)


class ExecutionWindowRepositoryError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _json(value: Mapping[str, Any] | None) -> str:
    try:
        encoded = json.dumps(dict(value or {}), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        raise ExecutionWindowRepositoryError("invalid_metadata") from None
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ExecutionWindowRepositoryError("metadata_too_large")
    return encoded


def _decode(value: str | None) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return dict(data) if isinstance(data, Mapping) else {}


def _event_json(value: Mapping[str, Any] | None, kind: str) -> str:
    if not isinstance(value, Mapping):
        raise ExecutionWindowRepositoryError("invalid_event_payload")

    def inspect(item: Any, depth: int = 0) -> None:
        if depth > 4:
            raise ExecutionWindowRepositoryError("invalid_event_payload")
        if isinstance(item, Mapping):
            for key, child in item.items():
                if not isinstance(key, str) or not key or len(key) > 64:
                    raise ExecutionWindowRepositoryError("invalid_event_payload")
                lowered = key.lower()
                if any(marker in lowered for marker in _FORBIDDEN_EVENT_MARKERS):
                    raise ExecutionWindowRepositoryError("forbidden_event_field")
                if depth == 0 and key not in WINDOW_EVENT_FIELDS[kind]:
                    raise ExecutionWindowRepositoryError("invalid_event_payload")
                inspect(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            if len(item) > 100:
                raise ExecutionWindowRepositoryError("invalid_event_payload")
            for child in item:
                inspect(child, depth + 1)
        elif isinstance(item, str):
            if len(item.encode("utf-8")) > MAX_EVENT_TEXT:
                raise ExecutionWindowRepositoryError("event_payload_too_large")
        elif isinstance(item, float) and (item != item or item in (float("inf"), float("-inf"))):
            raise ExecutionWindowRepositoryError("invalid_event_payload")
        elif item is not None and not isinstance(item, (bool, int, float)):
            raise ExecutionWindowRepositoryError("invalid_event_payload")

    inspect(value)
    try:
        encoded = json.dumps(dict(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        raise ExecutionWindowRepositoryError("invalid_event_payload") from None
    if len(encoded.encode("utf-8")) > MAX_EVENT_BYTES:
        raise ExecutionWindowRepositoryError("event_payload_too_large")
    return encoded


class ExecutionWindowRepository:
    def __init__(self, db_path: Path, *, clock=time.time):
        self.db_path = Path(db_path)
        self.clock = clock

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = None
        try:
            conn = self._connect()
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS execution_windows (
              window_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, run_id TEXT NOT NULL,
              state TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}',
              created_at REAL NOT NULL, expires_at REAL NOT NULL,
              attached_at REAL, closed_at REAL, updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_execution_windows_owner
              ON execution_windows(owner_id, updated_at DESC);
            CREATE TABLE IF NOT EXISTS execution_window_tickets (
              ticket_id TEXT PRIMARY KEY, window_id TEXT NOT NULL, owner_id TEXT NOT NULL,
              token_hash TEXT NOT NULL UNIQUE, expires_at REAL NOT NULL,
              consumed_at REAL, revoked_at REAL, created_at REAL NOT NULL,
              FOREIGN KEY(window_id) REFERENCES execution_windows(window_id)
            );
            CREATE INDEX IF NOT EXISTS idx_execution_window_tickets_window
              ON execution_window_tickets(owner_id, window_id, expires_at);
            CREATE TABLE IF NOT EXISTS execution_window_leases (
              lease_id TEXT PRIMARY KEY, window_id TEXT NOT NULL, owner_id TEXT NOT NULL,
              holder_id TEXT NOT NULL, lease_token_hash TEXT NOT NULL UNIQUE,
              expires_at REAL NOT NULL, created_at REAL NOT NULL,
              released_at REAL, revoked_at REAL,
              FOREIGN KEY(window_id) REFERENCES execution_windows(window_id)
            );
            CREATE INDEX IF NOT EXISTS idx_execution_window_leases_active
              ON execution_window_leases(owner_id, window_id, expires_at);
            CREATE TABLE IF NOT EXISTS execution_window_events (
              window_id TEXT NOT NULL, owner_id TEXT NOT NULL, sequence INTEGER NOT NULL,
              client_event_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
              created_at REAL NOT NULL,
              PRIMARY KEY(window_id, sequence),
              UNIQUE(owner_id, window_id, client_event_id),
              FOREIGN KEY(window_id) REFERENCES execution_windows(window_id)
            );
            CREATE INDEX IF NOT EXISTS idx_execution_window_events_cursor
              ON execution_window_events(owner_id, window_id, sequence);
            """)
        except (sqlite3.Error, OSError):
            raise ExecutionWindowRepositoryError("window_store") from None
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _window(row) -> dict[str, Any]:
        if row is None:
            return {}
        data = dict(row)
        data["metadata"] = _decode(data.get("metadata"))
        return data

    @staticmethod
    def _lease(row) -> dict[str, Any]:
        return dict(row) if row is not None else {}

    @staticmethod
    def _owner_window(owner_id: str, window_id: str) -> tuple[str, str]:
        try:
            return validate_owner_id(owner_id), validate_id(window_id, "window_id")
        except ValueError:
            raise ExecutionWindowRepositoryError("invalid_window") from None

    @staticmethod
    def _ttl(value: Any, *, minimum: float, maximum: float, code: str) -> float:
        try:
            ttl = float(value)
        except (TypeError, ValueError):
            raise ExecutionWindowRepositoryError(code) from None
        if ttl != ttl or ttl < minimum or ttl > maximum:
            raise ExecutionWindowRepositoryError(code)
        return ttl

    def _expire_if_needed(self, conn, row, now: float):
        if row and row["state"] not in TERMINAL_WINDOW_STATES and float(row["expires_at"]) <= now:
            conn.execute("UPDATE execution_windows SET state='expired',updated_at=? WHERE window_id=? AND state NOT IN ('closed','expired')", (now, row["window_id"]))
            row = conn.execute("SELECT * FROM execution_windows WHERE window_id=?", (row["window_id"],)).fetchone()
        return row

    def create_window(self, owner_id: str, run_id: str, *, ttl_s: float = 900.0, metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
        try:
            owner_id = validate_owner_id(owner_id)
            run_id = validate_id(run_id, "run_id")
        except ValueError:
            raise ExecutionWindowRepositoryError("invalid_window") from None
        ttl = self._ttl(ttl_s, minimum=30.0, maximum=24 * 3600.0, code="invalid_window_ttl")
        encoded = _json(metadata)
        now = float(self.clock())
        # ``validate_id`` rejects security-marker substrings such as ``key``
        # and ``token``. Hex output cannot contain alphabetic marker text, so
        # generated IDs remain readable even under adversarial/random output.
        window_id = "win_" + secrets.token_hex(18)
        ticket_id = "ticket_" + secrets.token_urlsafe(18)
        ticket = secrets.token_urlsafe(32)
        conn = None
        try:
            conn = self._connect(); conn.execute("BEGIN IMMEDIATE")
            run = conn.execute("SELECT run_id FROM runs WHERE owner_id=? AND run_id=?", (owner_id, run_id)).fetchone()
            if run is None:
                conn.execute("ROLLBACK")
                raise ExecutionWindowRepositoryError("run_not_found")
            expires = now + ttl
            conn.execute("INSERT INTO execution_windows(window_id,owner_id,run_id,state,metadata,created_at,expires_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (window_id, owner_id, run_id, "pending", encoded, now, expires, now))
            conn.execute("INSERT INTO execution_window_tickets(ticket_id,window_id,owner_id,token_hash,expires_at,created_at) VALUES(?,?,?,?,?,?)", (ticket_id, window_id, owner_id, _token_hash(ticket), expires, now))
            row = conn.execute("SELECT * FROM execution_windows WHERE window_id=?", (window_id,)).fetchone()
            conn.execute("COMMIT")
            return {"window": public_window(self._window(row)), "attach_ticket": ticket, "ticket_expires_at": expires}
        except ExecutionWindowRepositoryError:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise
        except (sqlite3.Error, OSError):
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise ExecutionWindowRepositoryError("window_store") from None
        finally:
            if conn is not None: conn.close()

    def get_window(self, owner_id: str, window_id: str, *, now: float | None = None) -> dict[str, Any] | None:
        owner_id, window_id = self._owner_window(owner_id, window_id)
        now = float(self.clock() if now is None else now)
        conn = None
        try:
            conn = self._connect(); conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM execution_windows WHERE owner_id=? AND window_id=?", (owner_id, window_id)).fetchone()
            row = self._expire_if_needed(conn, row, now)
            conn.execute("COMMIT")
            return public_window(self._window(row)) if row else None
        except ExecutionWindowRepositoryError:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise
        except sqlite3.Error:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise ExecutionWindowRepositoryError("window_store") from None
        finally:
            if conn is not None: conn.close()

    def list_windows(
        self, owner_id: str, *, run_id: str | None = None,
        limit: int = 20, now: float | None = None,
    ) -> dict[str, Any]:
        """List recent public windows for one owner, optionally for one Run."""
        try:
            owner_id = validate_owner_id(owner_id)
            if run_id is not None:
                run_id = validate_id(run_id, "run_id")
        except ValueError:
            raise ExecutionWindowRepositoryError("invalid_window") from None
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise ExecutionWindowRepositoryError("invalid_cursor") from None
        if limit < 1 or limit > MAX_WINDOW_LIST_LIMIT:
            raise ExecutionWindowRepositoryError("invalid_cursor")
        now = float(self.clock() if now is None else now)
        conn = None
        try:
            conn = self._connect()
            where = ["owner_id=?"]
            params: list[Any] = [owner_id]
            if run_id is not None:
                where.append("run_id=?")
                params.append(run_id)
            query = "SELECT * FROM execution_windows WHERE " + " AND ".join(where) + " ORDER BY updated_at DESC, window_id DESC LIMIT ?"
            rows = conn.execute(query, (*params, limit)).fetchall()
            expired_ids = [
                row["window_id"] for row in rows
                if row["state"] not in TERMINAL_WINDOW_STATES
                and float(row["expires_at"]) <= now
            ]
            if expired_ids:
                conn.execute("BEGIN IMMEDIATE")
                conn.executemany(
                    "UPDATE execution_windows SET state='expired',updated_at=? WHERE owner_id=? AND window_id=? AND state NOT IN ('closed','expired')",
                    [(now, owner_id, window_id) for window_id in expired_ids],
                )
                conn.execute("COMMIT")
                rows = conn.execute(query, (*params, limit)).fetchall()
            windows = [public_window(self._window(row)) for row in rows]
            return {"ok": True, "windows": windows, "next_cursor": windows[-1]["window_id"] if windows else None}
        except ExecutionWindowRepositoryError:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise
        except sqlite3.Error:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise ExecutionWindowRepositoryError("window_store") from None
        finally:
            if conn is not None: conn.close()

    def _load_for_update(self, conn, owner_id: str, window_id: str, now: float):
        row = conn.execute("SELECT * FROM execution_windows WHERE owner_id=? AND window_id=?", (owner_id, window_id)).fetchone()
        return self._expire_if_needed(conn, row, now)

    def redeem_ticket(self, owner_id: str, window_id: str, ticket: str, *, now: float | None = None) -> dict[str, Any]:
        owner_id, window_id = self._owner_window(owner_id, window_id)
        if not isinstance(ticket, str) or not (20 <= len(ticket) <= 512):
            raise ExecutionWindowRepositoryError("invalid_ticket")
        now = float(self.clock() if now is None else now); conn = None
        try:
            conn = self._connect(); conn.execute("BEGIN IMMEDIATE")
            row = self._load_for_update(conn, owner_id, window_id, now)
            if row is None: raise ExecutionWindowRepositoryError("window_not_found")
            if row["state"] == "expired": raise ExecutionWindowRepositoryError("window_expired")
            if row["state"] in {"closed", "closing"}: raise ExecutionWindowRepositoryError("window_closed")
            ticket_row = conn.execute("SELECT * FROM execution_window_tickets WHERE owner_id=? AND window_id=? AND token_hash=?", (owner_id, window_id, _token_hash(ticket))).fetchone()
            if ticket_row is None: raise ExecutionWindowRepositoryError("invalid_ticket")
            if ticket_row["consumed_at"] is not None or ticket_row["revoked_at"] is not None: raise ExecutionWindowRepositoryError("ticket_used")
            if float(ticket_row["expires_at"]) <= now:
                raise ExecutionWindowRepositoryError("ticket_expired")
            conn.execute("UPDATE execution_window_tickets SET consumed_at=? WHERE ticket_id=? AND consumed_at IS NULL AND revoked_at IS NULL", (now, ticket_row["ticket_id"]))
            conn.execute("UPDATE execution_windows SET state='attached',attached_at=?,updated_at=? WHERE window_id=?", (now, now, window_id))
            saved = conn.execute("SELECT * FROM execution_windows WHERE window_id=?", (window_id,)).fetchone()
            conn.execute("COMMIT")
            return {"window": public_window(self._window(saved)), "attached": True}
        except ExecutionWindowRepositoryError:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise
        except sqlite3.Error:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise ExecutionWindowRepositoryError("window_store") from None
        finally:
            if conn is not None: conn.close()

    def reconnect(self, owner_id: str, window_id: str, *, ttl_s: float = 300.0, now: float | None = None) -> dict[str, Any]:
        owner_id, window_id = self._owner_window(owner_id, window_id)
        ttl = self._ttl(ttl_s, minimum=30.0, maximum=3600.0, code="invalid_ticket_ttl")
        now = float(self.clock() if now is None else now); conn = None
        try:
            conn = self._connect(); conn.execute("BEGIN IMMEDIATE")
            row = self._load_for_update(conn, owner_id, window_id, now)
            if row is None: raise ExecutionWindowRepositoryError("window_not_found")
            if row["state"] in TERMINAL_WINDOW_STATES or row["state"] == "closing": raise ExecutionWindowRepositoryError("window_closed")
            token = secrets.token_urlsafe(32); ticket_id = "ticket_" + secrets.token_urlsafe(18); expires = min(float(row["expires_at"]), now + ttl)
            conn.execute("INSERT INTO execution_window_tickets(ticket_id,window_id,owner_id,token_hash,expires_at,created_at) VALUES(?,?,?,?,?,?)", (ticket_id, window_id, owner_id, _token_hash(token), expires, now))
            if row["state"] == "pending":
                conn.execute("UPDATE execution_windows SET state='ready',updated_at=? WHERE window_id=?", (now, window_id))
            saved = conn.execute("SELECT * FROM execution_windows WHERE window_id=?", (window_id,)).fetchone()
            conn.execute("COMMIT")
            return {"window": public_window(self._window(saved)), "attach_ticket": token, "ticket_expires_at": expires}
        except ExecutionWindowRepositoryError:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise
        except sqlite3.Error:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise ExecutionWindowRepositoryError("window_store") from None
        finally:
            if conn is not None: conn.close()

    def acquire_writer(self, owner_id: str, window_id: str, holder_id: str, *, ttl_s: float = 60.0, now: float | None = None) -> dict[str, Any]:
        owner_id, window_id = self._owner_window(owner_id, window_id)
        if not isinstance(holder_id, str) or not holder_id.strip() or len(holder_id) > 128 or any(ord(c) < 0x20 for c in holder_id):
            raise ExecutionWindowRepositoryError("invalid_holder")
        holder_id = holder_id.strip(); ttl = self._ttl(ttl_s, minimum=5.0, maximum=600.0, code="invalid_lease_ttl")
        now = float(self.clock() if now is None else now); conn = None
        try:
            conn = self._connect(); conn.execute("BEGIN IMMEDIATE")
            row = self._load_for_update(conn, owner_id, window_id, now)
            if row is None: raise ExecutionWindowRepositoryError("window_not_found")
            if row["state"] != "attached":
                raise ExecutionWindowRepositoryError("window_not_attached")
            active = conn.execute("SELECT * FROM execution_window_leases WHERE owner_id=? AND window_id=? AND released_at IS NULL AND revoked_at IS NULL AND expires_at>? ORDER BY created_at DESC LIMIT 1", (owner_id, window_id, now)).fetchone()
            if active is not None: raise ExecutionWindowRepositoryError("lease_conflict")
            conn.execute("UPDATE execution_window_leases SET revoked_at=? WHERE owner_id=? AND window_id=? AND released_at IS NULL AND revoked_at IS NULL", (now, owner_id, window_id))
            token = secrets.token_urlsafe(32); lease_id = "lease_" + secrets.token_urlsafe(18); expires = min(float(row["expires_at"]), now + ttl)
            conn.execute("INSERT INTO execution_window_leases(lease_id,window_id,owner_id,holder_id,lease_token_hash,expires_at,created_at) VALUES(?,?,?,?,?,?,?)", (lease_id, window_id, owner_id, holder_id, _token_hash(token), expires, now))
            conn.execute("COMMIT")
            return {"lease": {"lease_id": lease_id, "window_id": window_id, "holder_id": holder_id, "expires_at": expires}, "lease_token": token}
        except ExecutionWindowRepositoryError:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise
        except sqlite3.Error:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise ExecutionWindowRepositoryError("window_store") from None
        finally:
            if conn is not None: conn.close()

    def _lease_action(self, owner_id: str, window_id: str, holder_id: str, lease_token: str, action: str, *, ttl_s: float | None = None, now: float | None = None) -> dict[str, Any]:
        owner_id, window_id = self._owner_window(owner_id, window_id)
        if not isinstance(holder_id, str) or not holder_id or len(holder_id) > 128 or not isinstance(lease_token, str) or not (20 <= len(lease_token) <= 512):
            raise ExecutionWindowRepositoryError("invalid_lease")
        now = float(self.clock() if now is None else now); conn = None
        try:
            conn = self._connect(); conn.execute("BEGIN IMMEDIATE")
            row = self._load_for_update(conn, owner_id, window_id, now)
            if row is None: raise ExecutionWindowRepositoryError("window_not_found")
            if row["state"] in TERMINAL_WINDOW_STATES or row["state"] == "closing":
                raise ExecutionWindowRepositoryError("window_closed")
            lease = conn.execute("SELECT * FROM execution_window_leases WHERE owner_id=? AND window_id=? AND holder_id=? AND lease_token_hash=? AND released_at IS NULL AND revoked_at IS NULL", (owner_id, window_id, holder_id, _token_hash(lease_token))).fetchone()
            if lease is None: raise ExecutionWindowRepositoryError("lease_not_found")
            if float(lease["expires_at"]) <= now: raise ExecutionWindowRepositoryError("lease_expired")
            if action == "renew":
                ttl = self._ttl(ttl_s, minimum=5.0, maximum=600.0, code="invalid_lease_ttl")
                expires = min(float(row["expires_at"]), now + ttl)
                conn.execute("UPDATE execution_window_leases SET expires_at=? WHERE lease_id=? AND released_at IS NULL AND revoked_at IS NULL", (expires, lease["lease_id"]))
                result = {"lease_id": lease["lease_id"], "window_id": window_id, "holder_id": holder_id, "expires_at": expires}
            else:
                conn.execute("UPDATE execution_window_leases SET released_at=? WHERE lease_id=? AND released_at IS NULL AND revoked_at IS NULL", (now, lease["lease_id"]))
                result = {"lease_id": lease["lease_id"], "window_id": window_id, "holder_id": holder_id, "released_at": now}
            conn.execute("COMMIT")
            return {"lease": result}
        except ExecutionWindowRepositoryError:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise
        except sqlite3.Error:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise ExecutionWindowRepositoryError("window_store") from None
        finally:
            if conn is not None: conn.close()

    def renew_writer(self, owner_id, window_id, holder_id, lease_token, *, ttl_s=60.0, now=None):
        return self._lease_action(owner_id, window_id, holder_id, lease_token, "renew", ttl_s=ttl_s, now=now)

    def release_writer(self, owner_id, window_id, holder_id, lease_token, *, now=None):
        return self._lease_action(owner_id, window_id, holder_id, lease_token, "release", now=now)

    def close_window(self, owner_id: str, window_id: str, *, now: float | None = None) -> dict[str, Any]:
        owner_id, window_id = self._owner_window(owner_id, window_id)
        now = float(self.clock() if now is None else now); conn = None
        try:
            conn = self._connect(); conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM execution_windows WHERE owner_id=? AND window_id=?", (owner_id, window_id)).fetchone()
            if row is None: raise ExecutionWindowRepositoryError("window_not_found")
            if row["state"] == "closed":
                conn.execute("COMMIT"); return {"window": public_window(self._window(row))}
            if row["state"] == "expired": raise ExecutionWindowRepositoryError("window_expired")
            conn.execute("UPDATE execution_windows SET state='closing',updated_at=? WHERE window_id=?", (now, window_id))
            conn.execute("UPDATE execution_window_tickets SET revoked_at=? WHERE window_id=? AND revoked_at IS NULL", (now, window_id))
            conn.execute("UPDATE execution_window_leases SET revoked_at=? WHERE window_id=? AND released_at IS NULL AND revoked_at IS NULL", (now, window_id))
            conn.execute("UPDATE execution_windows SET state='closed',closed_at=?,updated_at=? WHERE window_id=?", (now, now, window_id))
            saved = conn.execute("SELECT * FROM execution_windows WHERE window_id=?", (window_id,)).fetchone()
            conn.execute("COMMIT")
            return {"window": public_window(self._window(saved))}
        except ExecutionWindowRepositoryError:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise
        except sqlite3.Error:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise ExecutionWindowRepositoryError("window_store") from None
        finally:
            if conn is not None: conn.close()

    @staticmethod
    def _event_id(value: Any) -> str:
        try:
            return validate_id(value, "client_event_id")
        except ValueError:
            raise ExecutionWindowRepositoryError("invalid_event_id") from None

    @staticmethod
    def _event_kind(value: Any) -> str:
        if not isinstance(value, str) or value not in WINDOW_EVENT_KINDS:
            raise ExecutionWindowRepositoryError("invalid_event_kind")
        return value

    def append_event(
        self, owner_id: str, window_id: str, holder_id: str, lease_token: str,
        client_event_id: str, kind: str, payload: Mapping[str, Any] | None = None,
        *, now: float | None = None,
    ) -> dict[str, Any]:
        owner_id, window_id = self._owner_window(owner_id, window_id)
        if not isinstance(holder_id, str) or not holder_id.strip() or len(holder_id) > 128:
            raise ExecutionWindowRepositoryError("invalid_holder")
        if not isinstance(lease_token, str) or not (20 <= len(lease_token) <= 512):
            raise ExecutionWindowRepositoryError("invalid_lease")
        client_event_id = self._event_id(client_event_id)
        kind = self._event_kind(kind)
        encoded = _event_json(payload or {}, kind)
        now = float(self.clock() if now is None else now)
        conn = None
        try:
            conn = self._connect(); conn.execute("BEGIN IMMEDIATE")
            row = self._load_for_update(conn, owner_id, window_id, now)
            if row is None:
                raise ExecutionWindowRepositoryError("window_not_found")
            if row["state"] in TERMINAL_WINDOW_STATES or row["state"] == "closing":
                raise ExecutionWindowRepositoryError("window_closed")
            if row["state"] != "attached":
                raise ExecutionWindowRepositoryError("window_not_attached")
            lease = conn.execute(
                "SELECT lease_id,expires_at FROM execution_window_leases "
                "WHERE owner_id=? AND window_id=? AND holder_id=? AND lease_token_hash=? "
                "AND released_at IS NULL AND revoked_at IS NULL",
                (owner_id, window_id, holder_id.strip(), _token_hash(lease_token)),
            ).fetchone()
            if lease is None:
                raise ExecutionWindowRepositoryError("lease_not_found")
            if float(lease["expires_at"]) <= now:
                raise ExecutionWindowRepositoryError("lease_expired")
            existing = conn.execute(
                "SELECT window_id,sequence,client_event_id,kind,payload,created_at "
                "FROM execution_window_events WHERE owner_id=? AND window_id=? AND client_event_id=?",
                (owner_id, window_id, client_event_id),
            ).fetchone()
            if existing is not None:
                if existing["kind"] != kind or existing["payload"] != encoded:
                    raise ExecutionWindowRepositoryError("event_conflict")
                conn.execute("COMMIT")
                return self._event(existing)
            next_row = conn.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 AS next_sequence "
                "FROM execution_window_events WHERE window_id=?", (window_id,),
            ).fetchone()
            sequence = int(next_row["next_sequence"])
            conn.execute(
                "INSERT INTO execution_window_events(window_id,owner_id,sequence,client_event_id,kind,payload,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (window_id, owner_id, sequence, client_event_id, kind, encoded, now),
            )
            saved = conn.execute(
                "SELECT window_id,sequence,client_event_id,kind,payload,created_at "
                "FROM execution_window_events WHERE window_id=? AND sequence=?",
                (window_id, sequence),
            ).fetchone()
            conn.execute("COMMIT")
            return self._event(saved)
        except ExecutionWindowRepositoryError:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise
        except sqlite3.IntegrityError:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise ExecutionWindowRepositoryError("event_conflict") from None
        except sqlite3.Error:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise ExecutionWindowRepositoryError("window_store") from None
        finally:
            if conn is not None: conn.close()

    @staticmethod
    def _event(row) -> dict[str, Any]:
        return {
            "window_id": row["window_id"], "sequence": int(row["sequence"]),
            "client_event_id": row["client_event_id"], "kind": row["kind"],
            "payload": _decode(row["payload"]), "created_at": float(row["created_at"]),
        }

    def list_events(self, owner_id: str, window_id: str, *, after: int = 0, limit: int = 100) -> dict[str, Any]:
        owner_id, window_id = self._owner_window(owner_id, window_id)
        try:
            after = int(after); limit = int(limit)
        except (TypeError, ValueError):
            raise ExecutionWindowRepositoryError("invalid_cursor") from None
        if after < 0 or limit < 1 or limit > MAX_EVENT_LIMIT:
            raise ExecutionWindowRepositoryError("invalid_cursor")
        conn = None
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT window_id FROM execution_windows WHERE owner_id=? AND window_id=?",
                (owner_id, window_id),
            ).fetchone()
            if row is None:
                raise ExecutionWindowRepositoryError("window_not_found")
            rows = conn.execute(
                "SELECT window_id,sequence,client_event_id,kind,payload,created_at "
                "FROM execution_window_events WHERE owner_id=? AND window_id=? AND sequence>? "
                "ORDER BY sequence LIMIT ?",
                (owner_id, window_id, after, limit),
            ).fetchall()
            events = [self._event(item) for item in rows]
            return {"ok": True, "events": events, "next_cursor": events[-1]["sequence"] if events else after}
        except ExecutionWindowRepositoryError:
            raise
        except sqlite3.Error:
            raise ExecutionWindowRepositoryError("window_store") from None
        finally:
            if conn is not None: conn.close()


__all__ = ["ExecutionWindowRepository", "ExecutionWindowRepositoryError"]
