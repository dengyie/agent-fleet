"""Durable owner-scoped browser sessions and one-time artifact tickets."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from platform_schema import validate_id, validate_owner_id

from tools.platform.browser_policy import validate_selector


class BrowserRepositoryError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


_SESSION_STATES = frozenset({"open", "closing", "closed", "unknown"})
_TICKET_STATES = frozenset({"issued", "uploading", "consumed"})
_APPROVAL_STATES = frozenset({"active", "consumed", "expired", "revoked"})
_APPROVAL_TTL_S = 300.0
_MAX_ACTIVE_APPROVALS_PER_SESSION = 4
_MAX_ACTIVE_APPROVALS_PER_RUN = 16
_MAX_APPROVALS_PER_WORKSPACE_HOUR = 64


def _bounded(value: Any, field: str, limit: int = 128) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise BrowserRepositoryError("invalid_" + field)
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in value):
        raise BrowserRepositoryError("invalid_" + field)
    return value


class BrowserRepository:
    """Separate browser metadata tables in the platform database."""

    def __init__(self, db_path: Path, *, clock=time.time):
        self.db_path = Path(db_path)
        self.clock = clock

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
                CREATE INDEX IF NOT EXISTS idx_browser_submit_approvals_rate
                    ON browser_submit_approvals(workspace_id, granted_at);
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

    @staticmethod
    def _digest(token: str) -> bytes:
        return hashlib.sha256(token.encode("utf-8")).digest()

    def issue_artifact_ticket(self, owner_id: str, *, workspace_id: str, run_id: str,
                              node_id: str, command_id: str, expires_at: float,
                              content_type: str = "image/png", max_bytes: int = 256 * 1024,
                              idempotency_key: str | None = None,
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
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                prior = conn.execute(
                    "SELECT * FROM browser_artifact_tickets WHERE owner_id=? AND command_id=? AND idempotency_key=?",
                    (owner_id, command_id, idempotency_key),
                ).fetchone()
                if prior:
                    if (prior["workspace_id"] != workspace_id or prior["run_id"] != run_id
                            or prior["node_id"] != node_id):
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
                "INSERT INTO browser_artifact_tickets(ticket_id,token_digest,owner_id,workspace_id,run_id,node_id,command_id,content_type,max_bytes,expires_at,state,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ticket_id, self._digest(upload_token), owner_id, workspace_id, run_id,
                 node_id, command_id, content_type, max_bytes, expires_at, "issued", idempotency_key, timestamp),
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
                                       command_id: str, idempotency_key: str) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        ticket_id = _bounded(ticket_id, "ticket_id")
        conn = None
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT ticket_id,owner_id,workspace_id,run_id,node_id,command_id,idempotency_key,state,expires_at "
                "FROM browser_artifact_tickets WHERE ticket_id=?", (ticket_id,),
            ).fetchone()
            if row is None:
                raise BrowserRepositoryError("artifact_ticket_invalid")
            expected = (owner_id, workspace_id, run_id, node_id, command_id, idempotency_key)
            actual = tuple(row[key] for key in (
                "owner_id", "workspace_id", "run_id", "node_id", "command_id",
                "idempotency_key",
            ))
            if actual != expected:
                raise BrowserRepositoryError("artifact_ticket_scope")
            return {
                "state": row["state"], "expires_at": float(row["expires_at"]),
                "ticket_id": row["ticket_id"],
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
                                 artifact_id: str, now: float | None = None) -> dict[str, Any]:
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
    def _approval_row(row) -> dict[str, Any]:
        return {key: row[key] for key in (
            "approval_id", "owner_id", "workspace_id", "run_id", "node_id",
            "session_id", "selector", "state", "granted_at", "expires_at",
            "consumed_at", "consumed_command_id",
        )}

    def grant_submit_approval(self, owner_id: str, *, workspace_id: str, run_id: str,
                              node_id: str, session_id: str, selector: str,
                              idempotency_key: str | None = None,
                              now: float | None = None) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        for value, field in ((workspace_id, "workspace_id"), (run_id, "run_id"),
                             (node_id, "node_id"), (session_id, "session_id")):
            validate_id(value, field)
        try:
            selector = validate_selector(selector)
        except ValueError:
            raise BrowserRepositoryError("invalid_selector") from None
        idempotency_key = (
            _bounded(idempotency_key, "idempotency_key", 256) if idempotency_key else None
        )
        timestamp = float(self.clock() if now is None else now)
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            # Lazy TTL sweep inside the same transaction: expired-but-still-
            # active rows must not consume the per-session/per-run budgets.
            conn.execute(
                "UPDATE browser_submit_approvals SET state='expired' "
                "WHERE state='active' AND expires_at<=?",
                (timestamp,),
            )
            session = conn.execute(
                "SELECT state FROM browser_sessions WHERE owner_id=? AND session_id=?",
                (owner_id, session_id),
            ).fetchone()
            if session is None or session["state"] not in {"open", "closing"}:
                conn.execute("ROLLBACK")
                raise BrowserRepositoryError("session_not_found")
            if idempotency_key:
                prior = conn.execute(
                    "SELECT * FROM browser_submit_approvals WHERE owner_id=? AND run_id=? AND idempotency_key=?",
                    (owner_id, run_id, idempotency_key),
                ).fetchone()
                if prior:
                    if (prior["workspace_id"] != workspace_id or prior["session_id"] != session_id
                            or prior["selector"] != selector):
                        conn.execute("ROLLBACK")
                        raise BrowserRepositoryError("approval_idempotency_conflict")
                    conn.execute("COMMIT")
                    return self._approval_row(prior)
            hour_ago = timestamp - 3600.0
            grants = conn.execute(
                "SELECT COUNT(*) AS n FROM browser_submit_approvals WHERE workspace_id=? AND granted_at>?",
                (workspace_id, hour_ago),
            ).fetchone()["n"]
            if grants >= _MAX_APPROVALS_PER_WORKSPACE_HOUR:
                conn.execute("ROLLBACK")
                raise BrowserRepositoryError("approval_limit")
            active_run = conn.execute(
                "SELECT COUNT(*) AS n FROM browser_submit_approvals WHERE owner_id=? AND run_id=? AND state='active'",
                (owner_id, run_id),
            ).fetchone()["n"]
            if active_run >= _MAX_ACTIVE_APPROVALS_PER_RUN:
                conn.execute("ROLLBACK")
                raise BrowserRepositoryError("approval_limit")
            active_session = conn.execute(
                "SELECT COUNT(*) AS n FROM browser_submit_approvals WHERE owner_id=? AND session_id=? AND state='active'",
                (owner_id, session_id),
            ).fetchone()["n"]
            if active_session >= _MAX_ACTIVE_APPROVALS_PER_SESSION:
                conn.execute("ROLLBACK")
                raise BrowserRepositoryError("approval_limit")
            approval_id = secrets.token_urlsafe(18)
            conn.execute(
                "INSERT INTO browser_submit_approvals(approval_id,owner_id,workspace_id,run_id,node_id,session_id,selector,state,granted_at,expires_at,idempotency_key) "
                "VALUES(?,?,?,?,?,?,?,'active',?,?,?)",
                (approval_id, owner_id, workspace_id, run_id, node_id, session_id,
                 selector, timestamp, timestamp + _APPROVAL_TTL_S, idempotency_key),
            )
            row = conn.execute(
                "SELECT * FROM browser_submit_approvals WHERE approval_id=?", (approval_id,)
            ).fetchone()
            conn.execute("COMMIT")
            return self._approval_row(row)
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

    def consume_submit_approval(self, owner_id: str, run_id: str, *, session_id: str,
                                selector: str, command_id: str,
                                now: float | None = None) -> str:
        """Atomically consume one active approval; return its opaque id."""
        owner_id = validate_owner_id(owner_id)
        run_id = validate_id(run_id, "run_id")
        session_id = _bounded(session_id, "session_id")
        command_id = _bounded(command_id, "command_id")
        try:
            selector = validate_selector(selector)
        except ValueError:
            raise BrowserRepositoryError("invalid_selector") from None
        timestamp = float(self.clock() if now is None else now)
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            # Prefer an unexpired active row over a stale one: consuming the
            # oldest active row when it has already lapsed would surface
            # approval_expired even though a later grant is still valid.
            row = conn.execute(
                "SELECT approval_id,expires_at,state FROM browser_submit_approvals "
                "WHERE owner_id=? AND run_id=? AND session_id=? AND selector=? AND state='active' "
                "ORDER BY (expires_at > ?) DESC, granted_at LIMIT 1",
                (owner_id, run_id, session_id, selector, timestamp),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise BrowserRepositoryError("approval_required")
            if timestamp >= float(row["expires_at"]):
                conn.execute(
                    "UPDATE browser_submit_approvals SET state='expired' WHERE approval_id=? AND state='active'",
                    (row["approval_id"],),
                )
                conn.execute("COMMIT")
                raise BrowserRepositoryError("approval_expired")
            updated = conn.execute(
                "UPDATE browser_submit_approvals SET state='consumed',consumed_at=?,consumed_command_id=? "
                "WHERE approval_id=? AND state='active'",
                (timestamp, command_id, row["approval_id"]),
            ).rowcount
            if updated != 1:
                conn.execute("ROLLBACK")
                raise BrowserRepositoryError("approval_consumed")
            conn.execute("COMMIT")
            return row["approval_id"]
        except BrowserRepositoryError:
            raise
        except sqlite3.Error:
            raise BrowserRepositoryError("browser_store") from None
        finally:
            if conn is not None:
                conn.close()

    def revoke_submit_approval(self, owner_id: str, approval_id: str,
                               *, run_id: str | None = None) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        approval_id = _bounded(approval_id, "approval_id")
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            query = "SELECT * FROM browser_submit_approvals WHERE owner_id=? AND approval_id=?"
            params = [owner_id, approval_id]
            if run_id is not None:
                query += " AND run_id=?"
                params.append(validate_id(run_id, "run_id"))
            row = conn.execute(query, params).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise BrowserRepositoryError("approval_not_found")
            if row["state"] == "active":
                conn.execute(
                    "UPDATE browser_submit_approvals SET state='revoked' WHERE approval_id=?",
                    (approval_id,),
                )
                row = conn.execute(
                    "SELECT * FROM browser_submit_approvals WHERE approval_id=?", (approval_id,)
                ).fetchone()
            conn.execute("COMMIT")
            return self._approval_row(row)
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

    def get_submit_approval(self, owner_id: str, approval_id: str) -> dict[str, Any] | None:
        owner_id = validate_owner_id(owner_id)
        approval_id = _bounded(approval_id, "approval_id")
        conn = None
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM browser_submit_approvals WHERE owner_id=? AND approval_id=?",
                (owner_id, approval_id),
            ).fetchone()
            return self._approval_row(row) if row else None
        except BrowserRepositoryError:
            raise
        except sqlite3.Error:
            raise BrowserRepositoryError("browser_store") from None
        finally:
            if conn is not None:
                conn.close()

    def expire_submit_approvals(self, *, now: float | None = None) -> int:
        """Terminalize active approvals whose TTL has passed."""
        timestamp = float(self.clock() if now is None else now)
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE browser_submit_approvals SET state='expired' "
                "WHERE state='active' AND expires_at<=?",
                (timestamp,),
            )
            conn.execute("COMMIT")
            return cur.rowcount
        except sqlite3.Error:
            raise BrowserRepositoryError("browser_store") from None
        finally:
            if conn is not None:
                conn.close()

    def expire_run_submit_approvals(self, owner_id: str, run_id: str,
                                    *, now: float | None = None) -> int:
        """Terminalize active approvals when their run ends or is cancelled."""
        owner_id = validate_owner_id(owner_id)
        run_id = validate_id(run_id, "run_id")
        timestamp = float(self.clock() if now is None else now)
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE browser_submit_approvals SET state='expired' "
                "WHERE owner_id=? AND run_id=? AND state='active'",
                (owner_id, run_id),
            )
            conn.execute("COMMIT")
            return cur.rowcount
        except sqlite3.Error:
            raise BrowserRepositoryError("browser_store") from None
        finally:
            if conn is not None:
                conn.close()


__all__ = ["BrowserRepository", "BrowserRepositoryError"]
