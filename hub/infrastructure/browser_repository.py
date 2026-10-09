"""Durable owner-scoped browser sessions and one-time artifact tickets."""
from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import sqlite3
import time
from collections.abc import Callable, Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

from platform_schema import validate_id, validate_owner_id

from hub.domain.browser_submit import APPROVAL_TTL_S, submit_selector, utc_timestamp, opaque_browser_id


class BrowserRepositoryError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


_SESSION_STATES = frozenset({"open", "closing", "closed", "unknown"})
_TICKET_STATES = frozenset({"issued", "uploading", "consumed"})
_APPROVAL_TTL_S = APPROVAL_TTL_S
_MAX_ACTIVE_APPROVALS_PER_SESSION = 4
_MAX_ACTIVE_APPROVALS_PER_RUN = 16
_MAX_APPROVALS_PER_WORKSPACE_HOUR = 64
_STALE_NODE_AFTER_S = 300
_STALE_SESSION_BATCH = 128


def _bounded(value: Any, field: str, limit: int = 128) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise BrowserRepositoryError("invalid_" + field)
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in value):
        raise BrowserRepositoryError("invalid_" + field)
    return value


class BrowserRepository:
    """Separate browser metadata tables in the platform database."""

    def __init__(self, db_path: Path, *, clock: Callable[[], float] = time.time,
                 approval_id_factory: Callable[[], str] = lambda: secrets.token_urlsafe(18)) -> None:
        self.db_path = Path(db_path)
        self.clock = clock
        self.approval_id_factory = approval_id_factory

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = None
        try:
            conn = self._connect()
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS browser_sessions (
                    session_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    backend TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    closed_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_browser_sessions_owner
                    ON browser_sessions(owner_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_browser_sessions_run
                    ON browser_sessions(owner_id, run_id, state);
                CREATE INDEX IF NOT EXISTS idx_browser_sessions_node_state
                    ON browser_sessions(owner_id, node_id, state, updated_at);
                CREATE TABLE IF NOT EXISTS browser_artifact_tickets (
                    ticket_id TEXT PRIMARY KEY,
                    token_digest BLOB NOT NULL,
                    owner_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    command_id TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    max_bytes INTEGER NOT NULL,
                    expires_at REAL NOT NULL,
                    state TEXT NOT NULL,
                    idempotency_key TEXT,
                    session_id TEXT,
                    window_id TEXT,
                    artifact_id TEXT,
                    sha256 TEXT,
                    size INTEGER,
                    created_at REAL NOT NULL,
                    upload_started_at REAL,
                    consumed_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_browser_artifact_ticket_expiry
                    ON browser_artifact_tickets(expires_at, state);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_browser_artifact_ticket_idempotency
                    ON browser_artifact_tickets(owner_id, command_id, idempotency_key);
                CREATE TABLE IF NOT EXISTS browser_submit_approvals (
                    approval_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    selector TEXT NOT NULL,
                    state TEXT NOT NULL,
                    granted_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    consumed_at REAL,
                    consumed_command_id TEXT,
                    idempotency_key TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_browser_submit_approvals_scope
                    ON browser_submit_approvals(owner_id, run_id, session_id, selector, state);
                CREATE INDEX IF NOT EXISTS idx_browser_submit_owner_rate
                    ON browser_submit_approvals(owner_id, workspace_id, granted_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_browser_submit_idempotency
                    ON browser_submit_approvals(owner_id, run_id, idempotency_key);
                CREATE INDEX IF NOT EXISTS idx_browser_submit_active_session
                    ON browser_submit_approvals(owner_id, session_id, state, expires_at);
                CREATE INDEX IF NOT EXISTS idx_browser_submit_active_run
                    ON browser_submit_approvals(owner_id, run_id, state, expires_at);
                CREATE INDEX IF NOT EXISTS idx_browser_submit_history
                    ON browser_submit_approvals(owner_id, run_id, session_id, selector, granted_at DESC);
                CREATE INDEX IF NOT EXISTS idx_browser_submit_expiry
                    ON browser_submit_approvals(state, expires_at);
                CREATE TRIGGER IF NOT EXISTS browser_session_approval_expiry
                AFTER UPDATE OF state ON browser_sessions
                WHEN NEW.state IN ('closed','unknown')
                BEGIN
                    UPDATE browser_submit_approvals SET state='expired'
                    WHERE owner_id=NEW.owner_id AND session_id=NEW.session_id AND state='active';
                END;
            """)
            ticket_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(browser_artifact_tickets)")
            }
            if "session_id" not in ticket_columns:
                conn.execute("ALTER TABLE browser_artifact_tickets ADD COLUMN session_id TEXT")
            if "window_id" not in ticket_columns:
                conn.execute("ALTER TABLE browser_artifact_tickets ADD COLUMN window_id TEXT")
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone():
                conn.executescript("""
                    CREATE TRIGGER IF NOT EXISTS browser_run_approval_expiry
                    AFTER UPDATE OF state,cancel_requested ON runs
                    WHEN NEW.state IN ('succeeded','failed','unknown','cancelled','cancelling') OR NEW.cancel_requested=1
                    BEGIN
                        UPDATE browser_submit_approvals SET state='expired'
                        WHERE owner_id=NEW.owner_id AND run_id=NEW.run_id AND state='active';
                    END;
                """)
        except sqlite3.Error:
            raise BrowserRepositoryError("browser_store") from None
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _session_row(row) -> dict[str, Any]:
        return {key: row[key] for key in (
            "session_id", "owner_id", "workspace_id", "run_id", "node_id",
            "profile_id", "backend", "state", "created_at", "updated_at",
            "closed_at",
        )}

    @staticmethod
    def _ticket_row(row, *, upload_token: str | None = None) -> dict[str, Any]:
        result = {key: row[key] for key in (
            "ticket_id", "owner_id", "workspace_id", "run_id", "node_id",
            "command_id", "content_type", "max_bytes", "expires_at", "state",
            "idempotency_key", "artifact_id", "sha256", "size", "created_at",
            "session_id", "window_id",
            "upload_started_at", "consumed_at",
        )}
        if upload_token is not None:
            result["upload_token"] = upload_token
        return result

    def create_session(self, owner_id: str, *, workspace_id: str, run_id: str,
                       node_id: str, profile_id: str, backend: str = "cdp_local",
                       session_id: str | None = None,
                       now: float | None = None) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        for value, field in ((workspace_id, "workspace_id"), (run_id, "run_id"),
                             (node_id, "node_id"), (profile_id, "profile_id")):
            validate_id(value, field)
        if backend not in {"cdp_local", "browserbase"}:
            raise BrowserRepositoryError("invalid_backend")
        timestamp = float(self.clock() if now is None else now)
        session_id = _bounded(session_id, "session_id") if session_id is not None else secrets.token_urlsafe(24)
        conn = None
        try:
            conn = self._connect()
            conn.execute(
                "INSERT INTO browser_sessions(session_id,owner_id,workspace_id,run_id,node_id,profile_id,backend,state,created_at,updated_at,closed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
                (session_id, owner_id, workspace_id, run_id, node_id, profile_id,
                 backend, "open", timestamp, timestamp),
            )
            row = conn.execute("SELECT * FROM browser_sessions WHERE session_id=?", (session_id,)).fetchone()
            return self._session_row(row)
        except BrowserRepositoryError:
            raise
        except sqlite3.Error:
            raise BrowserRepositoryError("browser_store") from None
        finally:
            if conn is not None:
                conn.close()

    def get_session(self, owner_id: str, session_id: str) -> dict[str, Any] | None:
        owner_id = validate_owner_id(owner_id)
        session_id = _bounded(session_id, "session_id")
        conn = None
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM browser_sessions WHERE owner_id=? AND session_id=?",
                (owner_id, session_id),
            ).fetchone()
            return self._session_row(row) if row else None
        except BrowserRepositoryError:
            raise
        except sqlite3.Error:
            raise BrowserRepositoryError("browser_store") from None
        finally:
            if conn is not None:
                conn.close()

    def close_session(self, owner_id: str, session_id: str, *, state: str = "closed",
                      now: float | None = None) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        session_id = _bounded(session_id, "session_id")
        if state not in {"closed", "unknown"}:
            raise BrowserRepositoryError("invalid_state")
        timestamp = float(self.clock() if now is None else now)
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE browser_sessions SET state=?,updated_at=?,closed_at=? WHERE owner_id=? AND session_id=? AND state NOT IN ('closed','unknown')",
                (state, timestamp, timestamp, owner_id, session_id),
            )
            if cur.rowcount == 1:
                # Close-time sweep: active approvals for this session cannot
                # be consumed afterwards, so terminalize them in the same
                # transaction as the session state change.
                conn.execute(
                    "UPDATE browser_submit_approvals SET state='expired' "
                    "WHERE session_id=? AND owner_id=? AND state='active'",
                    (session_id, owner_id),
                )
            row = conn.execute(
                "SELECT * FROM browser_sessions WHERE owner_id=? AND session_id=?",
                (owner_id, session_id),
            ).fetchone()
            if row is None:
                raise BrowserRepositoryError("session_not_found")
            conn.execute("COMMIT")
            return self._session_row(row)
        except BrowserRepositoryError:
            if conn is not None:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        except sqlite3.Error:
            if conn is not None:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise BrowserRepositoryError("browser_store") from None
        finally:
            if conn is not None:
                conn.close()

    def reconcile_stale_sessions(self, *, now: float | None = None,
                                 limit: int = _STALE_SESSION_BATCH) -> int:
        """Fence sessions whose enabled Node has missed its heartbeat.

        This records an indeterminate remote state only. It neither claims the
        browser process exited nor queues/replays any command.
        """
        if type(limit) is not int or not 1 <= limit <= _STALE_SESSION_BATCH:
            raise BrowserRepositoryError("invalid_reconciliation_limit")
        try:
            timestamp = float(self.clock() if now is None else now)
        except (TypeError, ValueError, OverflowError) as exc:
            raise BrowserRepositoryError("invalid_reconciliation_time") from exc
        if not math.isfinite(timestamp):
            raise BrowserRepositoryError("invalid_reconciliation_time")
        cutoff = timestamp - _STALE_NODE_AFTER_S
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            node_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(nodes)")
            }
            if not {"owner_id", "node_id", "enabled", "last_seen_at"} <= node_columns:
                conn.execute("COMMIT")
                return 0
            candidates = conn.execute(
                "SELECT s.session_id FROM browser_sessions AS s "
                "JOIN nodes AS n ON n.owner_id=s.owner_id AND n.node_id=s.node_id "
                "WHERE n.enabled=1 AND n.last_seen_at IS NOT NULL AND n.last_seen_at<? "
                "AND s.state IN ('open','closing') "
                "ORDER BY n.last_seen_at ASC,s.updated_at ASC,s.session_id ASC LIMIT ?",
                (cutoff, limit),
            ).fetchall()
            if not candidates:
                conn.execute("COMMIT")
                return 0
            session_ids = [row["session_id"] for row in candidates]
            placeholders = ",".join("?" for _ in session_ids)
            cursor = conn.execute(
                f"UPDATE browser_sessions SET state='unknown',updated_at=? "
                f"WHERE state IN ('open','closing') AND session_id IN ({placeholders})",
                (timestamp, *session_ids),
            )
            conn.execute("COMMIT")
            return cursor.rowcount
        except sqlite3.Error as exc:
            if conn is not None:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise BrowserRepositoryError("browser_store") from exc
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _digest(token: str) -> bytes:
        return hashlib.sha256(token.encode("utf-8")).digest()

    @staticmethod
    def _attached_window(connection: sqlite3.Connection, owner_id: str,
                         workspace_id: str, run_id: str, node_id: str,
                         session_id: str | None, now: float) -> str | None:
        has_windows = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_windows'"
        ).fetchone() is not None
        if not has_windows or session_id is None:
            return None
        session = connection.execute(
            "SELECT workspace_id,run_id,node_id,state FROM browser_sessions "
            "WHERE owner_id=? AND session_id=?",
            (owner_id, session_id),
        ).fetchone()
        if (session is None or session["state"] != "open"
                or session["workspace_id"] != workspace_id
                or session["run_id"] != run_id or session["node_id"] != node_id):
            raise BrowserRepositoryError("session_scope")
        window = connection.execute(
            "SELECT window_id FROM execution_windows WHERE owner_id=? AND run_id=? "
            "AND state='attached' AND expires_at>? "
            "ORDER BY attached_at DESC,updated_at DESC,window_id DESC LIMIT 1",
            (owner_id, run_id, now),
        ).fetchone()
        return window["window_id"] if window is not None else None

    def issue_artifact_ticket(self, owner_id: str, *, workspace_id: str, run_id: str,
                              node_id: str, command_id: str, expires_at: float,
                              content_type: str = "image/png", max_bytes: int = 256 * 1024,
                              idempotency_key: str | None = None,
                              session_id: str | None = None,
                              capture_frame: bool = True,
                              now: float | None = None) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        for value, field in ((workspace_id, "workspace_id"), (run_id, "run_id"),
                             (node_id, "node_id"), (command_id, "command_id")):
            validate_id(value, field)
        if content_type != "image/png":
            raise BrowserRepositoryError("invalid_content_type")
        try:
            max_bytes = int(max_bytes)
            expires_at = float(expires_at)
        except (TypeError, ValueError):
            raise BrowserRepositoryError("invalid_ticket") from None
        timestamp = float(self.clock() if now is None else now)
        if not 1 <= max_bytes <= 256 * 1024 or expires_at <= timestamp:
            raise BrowserRepositoryError("invalid_ticket")
        idempotency_key = _bounded(idempotency_key, "idempotency_key", 256) if idempotency_key else None
        session_id = _bounded(session_id, "session_id") if session_id is not None else None
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            window_id = self._attached_window(
                conn, owner_id, workspace_id, run_id, node_id, session_id, timestamp,
            ) if capture_frame else None
            if idempotency_key:
                prior = conn.execute(
                    "SELECT * FROM browser_artifact_tickets WHERE owner_id=? AND command_id=? AND idempotency_key=?",
                    (owner_id, command_id, idempotency_key),
                ).fetchone()
                if prior:
                    if (prior["workspace_id"] != workspace_id or prior["run_id"] != run_id
                            or prior["node_id"] != node_id
                            or prior["session_id"] != session_id):
                        raise BrowserRepositoryError("artifact_idempotency_conflict")
                    if prior["state"] == "consumed":
                        conn.execute("COMMIT")
                        return self._ticket_row(prior)
                    if prior["state"] == "uploading":
                        conn.execute("ROLLBACK")
                        raise BrowserRepositoryError("artifact_upload_in_progress")
                    ticket_id = prior["ticket_id"]
                    upload_token = secrets.token_urlsafe(32)
                    conn.execute(
                        "UPDATE browser_artifact_tickets SET token_digest=?,expires_at=?,created_at=? WHERE ticket_id=? AND state='issued'",
                        (self._digest(upload_token), expires_at, timestamp, ticket_id),
                    )
                    row = conn.execute(
                        "SELECT * FROM browser_artifact_tickets WHERE ticket_id=?", (ticket_id,)
                    ).fetchone()
                    conn.execute("COMMIT")
                    return self._ticket_row(row, upload_token=upload_token)
            ticket_id = secrets.token_urlsafe(18)
            upload_token = secrets.token_urlsafe(32)
            conn.execute(
                "INSERT INTO browser_artifact_tickets(ticket_id,token_digest,owner_id,workspace_id,run_id,node_id,command_id,content_type,max_bytes,expires_at,state,idempotency_key,created_at,session_id,window_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ticket_id, self._digest(upload_token), owner_id, workspace_id, run_id,
                 node_id, command_id, content_type, max_bytes, expires_at, "issued", idempotency_key, timestamp,
                 session_id, window_id),
            )
            row = conn.execute("SELECT * FROM browser_artifact_tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
            conn.execute("COMMIT")
            return self._ticket_row(row, upload_token=upload_token)
        except BrowserRepositoryError:
            if conn is not None:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        except sqlite3.Error:
            if conn is not None:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise BrowserRepositoryError("browser_store") from None
        finally:
            if conn is not None:
                conn.close()

    def validate_artifact_upload_scope(self, ticket_id: str, owner_id: str, *,
                                       workspace_id: str, run_id: str, node_id: str,
                                       command_id: str, idempotency_key: str,
                                       session_id: str | None = None) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        ticket_id = _bounded(ticket_id, "ticket_id")
        conn = None
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT ticket_id,owner_id,workspace_id,run_id,node_id,command_id,idempotency_key,state,expires_at,session_id,window_id "
                "FROM browser_artifact_tickets WHERE ticket_id=?", (ticket_id,),
            ).fetchone()
            if row is None:
                raise BrowserRepositoryError("artifact_ticket_invalid")
            expected = (owner_id, workspace_id, run_id, node_id, command_id, idempotency_key)
            actual = tuple(row[key] for key in (
                "owner_id", "workspace_id", "run_id", "node_id", "command_id",
                "idempotency_key", "session_id",
            ))
            if actual != (*expected, session_id):
                raise BrowserRepositoryError("artifact_ticket_scope")
            return {
                "state": row["state"], "expires_at": float(row["expires_at"]),
                "ticket_id": row["ticket_id"], "window_id": row["window_id"],
                "session_id": row["session_id"],
            }
        except BrowserRepositoryError:
            raise
        except sqlite3.Error:
            raise BrowserRepositoryError("browser_store") from None
        finally:
            if conn is not None:
                conn.close()

    def begin_artifact_upload(self, ticket_id: str, upload_token: str, *, node_id: str,
                              command_id: str, idempotency_key: str, now: float | None = None) -> dict[str, Any]:
        ticket_id = _bounded(ticket_id, "ticket_id")
        upload_token = _bounded(upload_token, "upload_token", 256)
        node_id = validate_id(node_id, "node_id")
        command_id = validate_id(command_id, "command_id")
        idempotency_key = _bounded(idempotency_key, "idempotency_key", 256)
        timestamp = float(self.clock() if now is None else now)
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM browser_artifact_tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
            if row is None or not hmac.compare_digest(bytes(row["token_digest"]), self._digest(upload_token)):
                conn.execute("ROLLBACK")
                raise BrowserRepositoryError("artifact_ticket_invalid")
            if row["node_id"] != node_id or row["command_id"] != command_id:
                conn.execute("ROLLBACK")
                raise BrowserRepositoryError("artifact_ticket_scope")
            if timestamp > float(row["expires_at"]):
                conn.execute("ROLLBACK")
                raise BrowserRepositoryError("artifact_ticket_expired")
            if row["state"] == "consumed":
                if row["idempotency_key"] != idempotency_key:
                    conn.execute("ROLLBACK")
                    raise BrowserRepositoryError("artifact_idempotency_conflict")
                conn.execute("COMMIT")
                return self._ticket_row(row)
            if row["state"] == "uploading":
                conn.execute("ROLLBACK")
                raise BrowserRepositoryError("artifact_upload_in_progress")
            conn.execute(
                "UPDATE browser_artifact_tickets SET state='uploading',idempotency_key=?,upload_started_at=? WHERE ticket_id=?",
                (idempotency_key, timestamp, ticket_id),
            )
            saved = conn.execute("SELECT * FROM browser_artifact_tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
            conn.execute("COMMIT")
            return self._ticket_row(saved)
        except BrowserRepositoryError:
            raise
        except sqlite3.Error:
            if conn is not None:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise BrowserRepositoryError("browser_store") from None
        finally:
            if conn is not None:
                conn.close()

    def complete_artifact_upload(self, ticket_id: str, *, sha256: str, size: int,
                                 artifact_id: str, frame_publisher=None,
                                 frame_dimensions: tuple[int, int] | None = None,
                                 now: float | None = None) -> dict[str, Any]:
        ticket_id = _bounded(ticket_id, "ticket_id")
        if not isinstance(sha256, str) or len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise BrowserRepositoryError("invalid_hash")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise BrowserRepositoryError("invalid_size")
        artifact_id = _bounded(artifact_id, "artifact_id")
        timestamp = float(self.clock() if now is None else now)
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM browser_artifact_tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise BrowserRepositoryError("artifact_ticket_invalid")
            if row["state"] == "consumed":
                if row["sha256"] != sha256 or int(row["size"] or -1) != size:
                    conn.execute("ROLLBACK")
                    raise BrowserRepositoryError("artifact_idempotency_conflict")
                conn.execute("COMMIT")
                return self._ticket_row(row)
            if row["state"] != "uploading":
                conn.execute("ROLLBACK")
                raise BrowserRepositoryError("artifact_ticket_state")
            if timestamp > float(row["expires_at"]):
                conn.execute("ROLLBACK")
                raise BrowserRepositoryError("artifact_ticket_expired")
            if size > int(row["max_bytes"]):
                conn.execute("ROLLBACK")
                raise BrowserRepositoryError("artifact_too_large")
            conn.execute(
                "UPDATE browser_artifact_tickets SET state='consumed',artifact_id=?,sha256=?,size=?,consumed_at=? WHERE ticket_id=?",
                (artifact_id, sha256, size, timestamp, ticket_id),
            )
            if row["window_id"] and frame_publisher is not None and frame_dimensions is not None:
                frame_publisher(conn, self._ticket_row(conn.execute(
                    "SELECT * FROM browser_artifact_tickets WHERE ticket_id=?", (ticket_id,)
                ).fetchone()), frame_dimensions)
            saved = conn.execute("SELECT * FROM browser_artifact_tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
            conn.execute("COMMIT")
            return self._ticket_row(saved)
        except BrowserRepositoryError:
            raise
        except sqlite3.Error:
            if conn is not None:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise BrowserRepositoryError("browser_store") from None
        finally:
            if conn is not None:
                conn.close()

    def reset_artifact_upload(self, ticket_id: str) -> None:
        ticket_id = _bounded(ticket_id, "ticket_id")
        conn = None
        try:
            conn = self._connect()
            conn.execute(
                "UPDATE browser_artifact_tickets SET state='issued',upload_started_at=NULL WHERE ticket_id=? AND state='uploading'",
                (ticket_id,),
            )
        except sqlite3.Error:
            raise BrowserRepositoryError("browser_store") from None
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _approval_row(row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in (
            "approval_id", "owner_id", "workspace_id", "run_id", "node_id",
            "session_id", "selector", "state", "granted_at", "expires_at",
            "consumed_at", "consumed_command_id",
        )}

    @staticmethod
    def _approval_selector(selector: object) -> str:
        try:
            return submit_selector(selector)
        except ValueError as exc:
            raise BrowserRepositoryError(getattr(exc, "code", "invalid_selector")) from exc

    def _approval_time(self, now: float | None) -> float:
        try:
            return utc_timestamp(self.clock() if now is None else now)
        except ValueError as exc:
            raise BrowserRepositoryError("invalid_timestamp") from exc

    @staticmethod
    def _approval_session(conn: sqlite3.Connection, owner: str, run: str,
                          session: str, workspace: str | None, node: str | None) -> sqlite3.Row:
        row = conn.execute(
            "SELECT s.workspace_id,s.node_id,s.state,r.state AS run_state,r.cancel_requested "
            "FROM browser_sessions s JOIN runs r ON r.run_id=s.run_id AND r.owner_id=s.owner_id "
            "WHERE s.owner_id=? AND s.run_id=? AND s.session_id=? LIMIT 1", (owner, run, session),
        ).fetchone()
        if (row is None or row["state"] not in {"open", "closing"}
                or row["run_state"] in {"succeeded", "failed", "unknown", "cancelled", "cancelling"}
                or row["cancel_requested"]
                or (workspace is not None and row["workspace_id"] != workspace)
                or (node is not None and row["node_id"] != node)):
            raise BrowserRepositoryError("session_not_found")
        return row

    def grant_submit_approval(self, owner_id: str, *, workspace_id: str, run_id: str,
                              node_id: str, session_id: str, selector: str,
                              idempotency_key: str | None = None,
                              now: float | None = None) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        for value, field in ((workspace_id, "workspace_id"), (run_id, "run_id"),
                             (node_id, "node_id")):
            validate_id(value, field)
        session_id = opaque_browser_id(session_id, "session")
        selector = self._approval_selector(selector)
        key = _bounded(idempotency_key, "idempotency_key", 256) if idempotency_key is not None else None
        timestamp = self._approval_time(now)
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                self._approval_session(conn, owner_id, run_id, session_id, workspace_id, node_id)
                conn.execute("UPDATE browser_submit_approvals SET state='expired' WHERE owner_id=? AND run_id=? AND state='active' AND expires_at<=?",
                             (owner_id, run_id, timestamp))
                prior = conn.execute(
                    "SELECT * FROM browser_submit_approvals WHERE owner_id=? AND run_id=? AND idempotency_key=? LIMIT 1",
                    (owner_id, run_id, key),
                ).fetchone() if key is not None else None
                if prior is not None and (prior["workspace_id"], prior["node_id"], prior["session_id"], prior["selector"]) != (workspace_id, node_id, session_id, selector):
                    raise BrowserRepositoryError("approval_idempotency_conflict")
                if prior is not None:
                    return self._approval_row(prior)
                quotas = (
                    ("owner_id=? AND workspace_id=? AND granted_at>?", (owner_id, workspace_id, timestamp - 3600), _MAX_APPROVALS_PER_WORKSPACE_HOUR),
                    ("owner_id=? AND run_id=? AND state='active' AND expires_at>?", (owner_id, run_id, timestamp), _MAX_ACTIVE_APPROVALS_PER_RUN),
                    ("owner_id=? AND session_id=? AND state='active' AND expires_at>?", (owner_id, session_id, timestamp), _MAX_ACTIVE_APPROVALS_PER_SESSION),
                )
                if any(conn.execute("SELECT COUNT(*) FROM (SELECT 1 FROM browser_submit_approvals WHERE " + where + " LIMIT ?)",
                                    (*args, limit)).fetchone()[0] >= limit for where, args, limit in quotas):
                    raise BrowserRepositoryError("approval_limit")
                approval_id = opaque_browser_id(self.approval_id_factory(), "approval")
                conn.execute(
                    "INSERT INTO browser_submit_approvals(approval_id,owner_id,workspace_id,run_id,node_id,session_id,selector,state,granted_at,expires_at,idempotency_key) VALUES(?,?,?,?,?,?,?,'active',?,?,?)",
                    (approval_id, owner_id, workspace_id, run_id, node_id, session_id, selector, timestamp, timestamp + _APPROVAL_TTL_S, key),
                )
                return self._approval_row(conn.execute("SELECT * FROM browser_submit_approvals WHERE approval_id=? LIMIT 1", (approval_id,)).fetchone())
        except sqlite3.Error as exc:
            raise BrowserRepositoryError("browser_store") from exc

    def consume_submit_approval(self, owner_id: str, run_id: str, *, session_id: str,
                                selector: str, command_id: str, now: float | None = None,
                                workspace_id: str | None = None, node_id: str | None = None,
                                connection: sqlite3.Connection | None = None) -> str:
        """Consume within the caller's command transaction, or an isolated store transaction."""
        owner_id = validate_owner_id(owner_id)
        run_id = validate_id(run_id, "run_id")
        session_id = opaque_browser_id(session_id, "session")
        command_id = validate_id(command_id, "command_id")
        selector = self._approval_selector(selector)
        timestamp = self._approval_time(now)
        if connection is not None:
            if not connection.in_transaction:
                raise BrowserRepositoryError("approval_transaction_required")
            return self._consume_approval(connection, owner_id, run_id, session_id, selector,
                                          command_id, timestamp, workspace_id, node_id)
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                return self._consume_approval(conn, owner_id, run_id, session_id, selector,
                                              command_id, timestamp, workspace_id, node_id)
        except sqlite3.Error as exc:
            raise BrowserRepositoryError("browser_store") from exc

    def _consume_approval(self, conn: sqlite3.Connection, owner: str, run: str,
                          session: str, selector: str, command: str, now: float,
                          workspace: str | None, node: str | None) -> str:
        self._approval_session(conn, owner, run, session, workspace, node)
        scope = (owner, run, session, selector)
        row = conn.execute(
            "SELECT approval_id FROM browser_submit_approvals WHERE owner_id=? AND run_id=? AND session_id=? AND selector=? AND state='active' AND expires_at>? ORDER BY expires_at,approval_id LIMIT 1",
            (*scope, now),
        ).fetchone()
        if row is None:
            prior = conn.execute(
                "SELECT state,expires_at FROM browser_submit_approvals WHERE owner_id=? AND run_id=? AND session_id=? AND selector=? ORDER BY granted_at DESC,rowid DESC LIMIT 1", scope,
            ).fetchone()
            state = "required" if prior is None else prior["state"]
            if prior is not None and state == "active":
                state = "expired"
            raise BrowserRepositoryError("approval_" + state)
        changed = conn.execute(
            "UPDATE browser_submit_approvals SET state='consumed',consumed_at=?,consumed_command_id=? WHERE approval_id=? AND state='active' AND expires_at>?",
            (now, command, row["approval_id"], now),
        ).rowcount
        if changed != 1:
            raise BrowserRepositoryError("approval_consumed")
        return row["approval_id"]

    def revoke_submit_approval(self, owner_id: str, approval_id: str,
                               *, run_id: str | None = None) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        approval_id = opaque_browser_id(approval_id, "approval")
        timestamp = self._approval_time(None)
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM browser_submit_approvals WHERE owner_id=? AND approval_id=? LIMIT 1", (owner_id, approval_id)).fetchone()
                if row is None or (run_id is not None and row["run_id"] != run_id):
                    raise BrowserRepositoryError("approval_not_found")
                state = "expired" if row["expires_at"] <= timestamp else "revoked"
                conn.execute("UPDATE browser_submit_approvals SET state=? WHERE approval_id=? AND state='active'", (state, approval_id))
                return self._approval_row(conn.execute("SELECT * FROM browser_submit_approvals WHERE approval_id=? LIMIT 1", (approval_id,)).fetchone())
        except sqlite3.Error as exc:
            raise BrowserRepositoryError("browser_store") from exc

    def get_submit_approval(self, owner_id: str, approval_id: str) -> dict[str, Any] | None:
        owner_id = validate_owner_id(owner_id)
        approval_id = opaque_browser_id(approval_id, "approval")
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("UPDATE browser_submit_approvals SET state='expired' WHERE owner_id=? AND approval_id=? AND state='active' AND expires_at<=?", (owner_id, approval_id, self._approval_time(None)))
                row = conn.execute("SELECT * FROM browser_submit_approvals WHERE owner_id=? AND approval_id=? LIMIT 1", (owner_id, approval_id)).fetchone()
                return self._approval_row(row) if row is not None else None
        except sqlite3.Error as exc:
            raise BrowserRepositoryError("browser_store") from exc

    def expire_submit_approvals(self, *, owner_id: str | None = None, run_id: str | None = None,
                                session_id: str | None = None, now: float | None = None) -> int:
        """Expire an owner-scoped lifecycle target, or one bounded TTL sweep batch."""
        timestamp = self._approval_time(now)
        scoped = run_id is not None or session_id is not None
        if scoped and owner_id is None:
            raise BrowserRepositoryError("invalid_owner")
        clauses = ["state='active'"]
        params: list[Any] = []
        if owner_id is not None:
            clauses.append("owner_id=?")
            params.append(validate_owner_id(owner_id))
        if run_id is not None:
            clauses.append("run_id=?")
            params.append(validate_id(run_id, "run_id"))
        if session_id is not None:
            clauses.append("session_id=?")
            params.append(opaque_browser_id(session_id, "session"))
        if not scoped:
            clauses.append("expires_at<=?")
            params.append(timestamp)
        try:
            with closing(self._connect()) as conn, conn:
                return conn.execute(
                    "UPDATE browser_submit_approvals SET state='expired' WHERE rowid IN (SELECT rowid FROM browser_submit_approvals WHERE " + " AND ".join(clauses) + " LIMIT 256)", params,
                ).rowcount
        except sqlite3.Error as exc:
            raise BrowserRepositoryError("browser_store") from exc


__all__ = ["BrowserRepository", "BrowserRepositoryError"]
