"""Durable, owner-scoped approval grants for production service actions."""
from __future__ import annotations

import json
import secrets
import sqlite3
import time
from collections.abc import Mapping
import math
from pathlib import Path
from typing import Any

from platform_schema import validate_id, validate_owner_id

MAX_JSON_BYTES = 16 * 1024
GRANT_STATES = frozenset({"pending", "approved", "rejected", "consuming", "consumed", "expired"})


class ApprovalRepositoryError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _json(value: Mapping[str, Any] | None) -> str:
    try:
        encoded = json.dumps(dict(value or {}), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        raise ApprovalRepositoryError("invalid_arguments") from None
    if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
        raise ApprovalRepositoryError("arguments_too_large")
    return encoded


class ApprovalRepository:
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
            CREATE TABLE IF NOT EXISTS platform_approval_grants (
              grant_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, actor TEXT NOT NULL,
              service_id TEXT NOT NULL, service_version INTEGER NOT NULL,
              node_id TEXT NOT NULL, adapter TEXT NOT NULL, target_alias TEXT NOT NULL,
              action TEXT NOT NULL, arguments TEXT NOT NULL DEFAULT '{}',
              args_hash TEXT NOT NULL, policy_version TEXT NOT NULL,
              state TEXT NOT NULL, expires_at REAL NOT NULL, remaining_uses INTEGER NOT NULL DEFAULT 1,
              revoked_at REAL, command_id TEXT, idempotency_key TEXT,
              created_at REAL NOT NULL, updated_at REAL NOT NULL,
              UNIQUE(owner_id, idempotency_key)
            );
            CREATE INDEX IF NOT EXISTS idx_platform_grants_owner_state
              ON platform_approval_grants(owner_id, state, updated_at DESC);
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(platform_approval_grants)")}
            if "idempotency_key" not in columns:
                conn.execute("ALTER TABLE platform_approval_grants ADD COLUMN idempotency_key TEXT")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_platform_grants_owner_idempotency ON platform_approval_grants(owner_id, idempotency_key) WHERE idempotency_key IS NOT NULL")
        except (sqlite3.Error, OSError):
            raise ApprovalRepositoryError("approval_store") from None
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _row(row) -> dict[str, Any] | None:
        if row is None:
            return None
        try:
            args = json.loads(row["arguments"] or "{}")
        except (TypeError, ValueError):
            args = {}
        return {
            "grant_id": row["grant_id"], "owner_id": row["owner_id"],
            "actor": row["actor"], "service_id": row["service_id"],
            "service_version": int(row["service_version"]), "node_id": row["node_id"],
            "adapter": row["adapter"], "target_alias": row["target_alias"],
            "action": row["action"], "arguments": dict(args) if isinstance(args, Mapping) else {},
            "args_hash": row["args_hash"], "policy_version": row["policy_version"],
            "state": row["state"], "expires_at": row["expires_at"],
            "remaining_uses": int(row["remaining_uses"]), "revoked_at": row["revoked_at"],
            "command_id": row["command_id"], "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "idempotency_key": row["idempotency_key"] if "idempotency_key" in row.keys() else None,
        }

    @staticmethod
    def _public(row: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in row.items() if key not in {"owner_id", "actor", "node_id", "adapter", "target_alias", "arguments", "args_hash", "idempotency_key"}} | {
            "arguments_summary": {"keys": sorted(row.get("arguments", {}).keys())}
        }

    def create(self, owner_id: str, *, actor: str, service: Mapping[str, Any], action: str, arguments: Mapping[str, Any], args_hash: str, policy_version: str, expires_at: float, idempotency_key: str | None = None) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        now = float(self.clock())
        if not isinstance(actor, str) or not actor or len(actor) > 200:
            raise ApprovalRepositoryError("invalid_actor")
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 200):
            raise ApprovalRepositoryError("invalid_idempotency_key")
        if action not in {"inspect", "restart"}:
            raise ApprovalRepositoryError("unsupported_action")
        try:
            expires = float(expires_at)
            version = int(service["version"])
        except (TypeError, ValueError, KeyError):
            raise ApprovalRepositoryError("invalid_grant") from None
        if not math.isfinite(expires) or expires <= now or not isinstance(policy_version, str) or not policy_version or len(policy_version) > 80:
            raise ApprovalRepositoryError("invalid_expiry")
        for key in ("service_id", "node_id", "adapter", "target_alias"):
            value = service.get(key)
            if not isinstance(value, str) or not value or len(value) > 512:
                raise ApprovalRepositoryError("invalid_grant")
        if not isinstance(args_hash, str) or len(args_hash) != 64 or any(char not in "0123456789abcdef" for char in args_hash):
            raise ApprovalRepositoryError("invalid_grant")
        if not isinstance(arguments, Mapping):
            raise ApprovalRepositoryError("invalid_arguments")
        encoded = _json(arguments)
        grant_id = "grant_" + secrets.token_urlsafe(18)
        conn = None
        try:
            conn = self._connect()
            if idempotency_key:
                prior = conn.execute("SELECT * FROM platform_approval_grants WHERE owner_id=? AND idempotency_key=?", (owner_id, idempotency_key)).fetchone()
                if prior:
                    if prior["service_id"] != service["service_id"] or prior["action"] != action or prior["args_hash"] != args_hash:
                        raise ApprovalRepositoryError("idempotency_conflict")
                    return self._row(prior)
            conn.execute("INSERT INTO platform_approval_grants(grant_id,owner_id,actor,service_id,service_version,node_id,adapter,target_alias,action,arguments,args_hash,policy_version,state,expires_at,remaining_uses,created_at,updated_at,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (grant_id, owner_id, actor, service["service_id"], version, service["node_id"], service["adapter"], service["target_alias"], action, encoded, args_hash, policy_version, "pending", expires, 1, now, now, idempotency_key))
            row = conn.execute("SELECT * FROM platform_approval_grants WHERE grant_id=?", (grant_id,)).fetchone()
            return self._row(row)
        except ApprovalRepositoryError:
            raise
        except (sqlite3.Error, OSError):
            raise ApprovalRepositoryError("approval_store") from None
        finally:
            if conn is not None:
                conn.close()

    def get(self, owner_id: str, grant_id: str) -> dict[str, Any] | None:
        owner_id = validate_owner_id(owner_id)
        grant_id = validate_id(grant_id, "grant_id")
        conn = None
        try:
            conn = self._connect()
            row = conn.execute("SELECT * FROM platform_approval_grants WHERE owner_id=? AND grant_id=?", (owner_id, grant_id)).fetchone()
            return self._row(row)
        except (sqlite3.Error, OSError):
            raise ApprovalRepositoryError("approval_store") from None
        finally:
            if conn is not None:
                conn.close()

    def decide(self, owner_id: str, grant_id: str, *, approve: bool) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        grant_id = validate_id(grant_id, "grant_id")
        now = float(self.clock())
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM platform_approval_grants WHERE owner_id=? AND grant_id=?", (owner_id, grant_id)).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise ApprovalRepositoryError("grant_not_found")
            if float(row["expires_at"]) <= now and row["state"] in {"pending", "approved"}:
                conn.execute("UPDATE platform_approval_grants SET state='expired',updated_at=? WHERE grant_id=?", (now, grant_id))
                conn.execute("COMMIT")
                raise ApprovalRepositoryError("approval_expired")
            if row["state"] != "pending":
                conn.execute("ROLLBACK")
                raise ApprovalRepositoryError("grant_not_pending")
            state = "approved" if approve else "rejected"
            conn.execute("UPDATE platform_approval_grants SET state=?,updated_at=? WHERE grant_id=? AND state='pending'", (state, now, grant_id))
            saved = conn.execute("SELECT * FROM platform_approval_grants WHERE grant_id=?", (grant_id,)).fetchone()
            conn.execute("COMMIT")
            return self._row(saved)
        except ApprovalRepositoryError:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise
        except (sqlite3.Error, OSError):
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise ApprovalRepositoryError("approval_store") from None
        finally:
            if conn is not None:
                conn.close()

    def begin_consume(self, owner_id: str, grant_id: str, *, command_id: str) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        grant_id = validate_id(grant_id, "grant_id")
        now = float(self.clock())
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM platform_approval_grants WHERE owner_id=? AND grant_id=?", (owner_id, grant_id)).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise ApprovalRepositoryError("grant_not_found")
            if row["state"] in {"consuming", "consumed"} and row["command_id"] == command_id:
                conn.execute("COMMIT")
                return self._row(row)
            if row["state"] != "approved" or int(row["remaining_uses"]) < 1:
                conn.execute("ROLLBACK")
                raise ApprovalRepositoryError("grant_not_usable")
            if float(row["expires_at"]) <= now:
                conn.execute("UPDATE platform_approval_grants SET state='expired',updated_at=? WHERE grant_id=?", (now, grant_id))
                conn.execute("COMMIT")
                raise ApprovalRepositoryError("approval_expired")
            conn.execute("UPDATE platform_approval_grants SET state='consuming',command_id=?,updated_at=? WHERE grant_id=? AND state='approved'", (command_id, now, grant_id))
            saved = conn.execute("SELECT * FROM platform_approval_grants WHERE grant_id=?", (grant_id,)).fetchone()
            conn.execute("COMMIT")
            return self._row(saved)
        except ApprovalRepositoryError:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise
        except (sqlite3.Error, OSError):
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise ApprovalRepositoryError("approval_store") from None
        finally:
            if conn is not None:
                conn.close()

    def finish_consume(self, owner_id: str, grant_id: str, *, command_id: str, success: bool) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        grant_id = validate_id(grant_id, "grant_id")
        now = float(self.clock())
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            if not isinstance(command_id, str) or not command_id:
                conn.execute("ROLLBACK")
                raise ApprovalRepositoryError("invalid_command")
            existing = conn.execute("SELECT * FROM platform_approval_grants WHERE owner_id=? AND grant_id=?", (owner_id, grant_id)).fetchone()
            if existing is None:
                conn.execute("ROLLBACK")
                raise ApprovalRepositoryError("grant_not_found")
            if existing["state"] == "consumed" and existing["command_id"] == command_id and success:
                conn.execute("COMMIT")
                return self._row(existing)
            state = "consumed" if success else "approved"
            uses = 0 if success else 1
            cur = conn.execute("UPDATE platform_approval_grants SET state=?,remaining_uses=?,updated_at=? WHERE owner_id=? AND grant_id=? AND state='consuming' AND command_id=?", (state, uses, now, owner_id, grant_id, command_id))
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                raise ApprovalRepositoryError("grant_state_conflict")
            row = conn.execute("SELECT * FROM platform_approval_grants WHERE grant_id=?", (grant_id,)).fetchone()
            conn.execute("COMMIT")
            return self._row(row)
        except ApprovalRepositoryError:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise
        except (sqlite3.Error, OSError):
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise ApprovalRepositoryError("approval_store") from None
        finally:
            if conn is not None:
                conn.close()


__all__ = ["ApprovalRepository", "ApprovalRepositoryError", "GRANT_STATES"]
