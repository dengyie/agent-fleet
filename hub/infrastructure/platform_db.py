"""Durable SQLite store for the additive platform domain.

The platform store is intentionally separate from legacy task/session/adoption
databases.  It has one writer (the Hub), WAL enabled, bounded JSON fields, and
small repository methods used by the defaults API.
"""
from __future__ import annotations

import json
import hashlib
import hmac
import secrets
import sqlite3
import time
from hub.domain.request_metadata import project_requests

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from platform_schema import (
    PlatformValidationError, validate_model_profile, validate_node,
    validate_workspace, validate_id,
    validate_owner_id,
)

SCHEMA_VERSION = 2
MAX_JSON_BYTES = 64 * 1024


class PlatformRepositoryError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


def _json(value: Any) -> str:
    try:
        result = json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        raise PlatformRepositoryError("invalid_value") from None
    if len(result.encode("utf-8")) > MAX_JSON_BYTES:
        raise PlatformRepositoryError("value_too_large")
    return result


def _decode(value: str | None) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return dict(data) if isinstance(data, Mapping) else {}


class PlatformRepository:
    SCHEMA_VERSION = SCHEMA_VERSION

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.path = self.db_path

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
        except sqlite3.Error:
            conn.close()
            raise PlatformRepositoryError("platform_store") from None
        return conn

    def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = None
        try:
            conn = self._connect()
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone():
                version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                try:
                    stored_version = int(version["value"]) if version else 0
                except (TypeError, ValueError):
                    raise PlatformRepositoryError("invalid_schema_version") from None
                if stored_version > SCHEMA_VERSION:
                    raise PlatformRepositoryError("unsupported_schema_version")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS model_profiles (
                    owner_id TEXT NOT NULL, profile_id TEXT NOT NULL, provider TEXT NOT NULL,
                    model TEXT NOT NULL, secret_ref TEXT, capabilities TEXT NOT NULL,
                    provider_config TEXT NOT NULL DEFAULT '{}',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(owner_id, profile_id)
                );
                CREATE TABLE IF NOT EXISTS workspaces (
                    owner_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
                    name TEXT NOT NULL, backend TEXT NOT NULL, root_path TEXT NOT NULL,
                    default_node_id TEXT, enabled INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(owner_id, workspace_id)
                );
                CREATE TABLE IF NOT EXISTS nodes (
                    owner_id TEXT NOT NULL, node_id TEXT NOT NULL, label TEXT NOT NULL,
                    capabilities TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    last_seen_at REAL, status TEXT NOT NULL DEFAULT 'unknown',
                    PRIMARY KEY(owner_id, node_id)
                );
                CREATE TABLE IF NOT EXISTS node_credentials (
                    owner_id TEXT NOT NULL, node_id TEXT NOT NULL,
                    salt BLOB NOT NULL, secret_digest BLOB NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, issued_at REAL NOT NULL,
                    last_used_at REAL,
                    PRIMARY KEY(owner_id, node_id),
                    FOREIGN KEY(owner_id, node_id) REFERENCES nodes(owner_id, node_id)
                );
                CREATE TABLE IF NOT EXISTS owner_defaults (
                    owner_id TEXT PRIMARY KEY, model_profile_id TEXT, workspace_id TEXT,
                    execution_node_id TEXT, revision INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(owner_id, model_profile_id) REFERENCES model_profiles(owner_id, profile_id),
                    FOREIGN KEY(owner_id, workspace_id) REFERENCES workspaces(owner_id, workspace_id),
                    FOREIGN KEY(owner_id, execution_node_id) REFERENCES nodes(owner_id, node_id)
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    workspace_id TEXT, overrides TEXT NOT NULL DEFAULT '{}',
                    revision INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(owner_id, workspace_id) REFERENCES workspaces(owner_id, workspace_id)
                );
                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL, role TEXT NOT NULL, content_ref TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    client_token TEXT, created_at TEXT NOT NULL,
                    UNIQUE(owner_id, conversation_id, client_token),
                    FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id)
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL, trigger_message_id TEXT NOT NULL,
                    state TEXT NOT NULL, config_snapshot TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    lease_id TEXT, lease_owner TEXT, lease_expires_at REAL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    result_text TEXT NOT NULL DEFAULT '',
                    usage_input_tokens INTEGER NOT NULL DEFAULT 0,
                    usage_output_tokens INTEGER NOT NULL DEFAULT 0,
                    usage_total_tokens INTEGER NOT NULL DEFAULT 0,
                    usage_provider_requests INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL DEFAULT 0,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id),
                    FOREIGN KEY(trigger_message_id) REFERENCES messages(message_id)
                );
                CREATE TABLE IF NOT EXISTS run_events (
                    run_id TEXT NOT NULL, sequence INTEGER NOT NULL, kind TEXT NOT NULL,
                    payload TEXT NOT NULL, created_at REAL NOT NULL,
                    PRIMARY KEY(run_id, sequence),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS legacy_task_links (
                    owner_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    request TEXT NOT NULL, request_hash TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','leased','linked','succeeded','failed','cancelled')),
                    task_id TEXT, attempt_id TEXT, session_id TEXT, task_state TEXT,
                    projection TEXT NOT NULL DEFAULT '{}', error_code TEXT,
                    lease_owner TEXT, lease_expires_at REAL, bridge_attempt INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    PRIMARY KEY(owner_id, run_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_legacy_task_links_claim
                    ON legacy_task_links(owner_id, state, lease_expires_at, updated_at);
                CREATE TABLE IF NOT EXISTS platform_worker_owners (
                    owner_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
                    last_claim_at REAL, lease_id TEXT, lease_owner TEXT,
                    lease_expires_at REAL, updated_at REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS platform_worker_slots (
                    lease_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                    run_id TEXT NOT NULL UNIQUE, workspace_key TEXT,
                    worker_id TEXT NOT NULL, expires_at REAL NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS platform_schedules (
                    owner_id TEXT NOT NULL, schedule_id TEXT NOT NULL,
                    name TEXT NOT NULL, action TEXT NOT NULL,
                    target TEXT NOT NULL, interval_s REAL NOT NULL,
                    timezone TEXT NOT NULL, missed_policy TEXT NOT NULL,
                    overlap_policy TEXT NOT NULL, next_run_at REAL NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    revision INTEGER NOT NULL DEFAULT 0,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    last_run_at REAL, last_success_at REAL,
                    last_error_code TEXT, last_result TEXT NOT NULL DEFAULT '{}',
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(owner_id, schedule_id)
                );
                CREATE TABLE IF NOT EXISTS platform_schedule_triggers (
                    owner_id TEXT NOT NULL, schedule_id TEXT NOT NULL,
                    scheduled_at REAL NOT NULL, state TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0, lease_id TEXT,
                    lease_owner TEXT, lease_expires_at REAL,
                    started_at REAL, finished_at REAL, error_code TEXT,
                    last_result TEXT NOT NULL DEFAULT '{}', updated_at REAL NOT NULL,
                    PRIMARY KEY(owner_id, schedule_id, scheduled_at),
                    FOREIGN KEY(owner_id, schedule_id)
                      REFERENCES platform_schedules(owner_id, schedule_id)
                      ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_platform_models_owner ON model_profiles(owner_id);
                CREATE INDEX IF NOT EXISTS idx_platform_workspaces_owner ON workspaces(owner_id);
                CREATE INDEX IF NOT EXISTS idx_platform_nodes_owner ON nodes(owner_id);
                CREATE INDEX IF NOT EXISTS idx_platform_worker_owners_fairness
                    ON platform_worker_owners(enabled, last_claim_at, owner_id);
                CREATE INDEX IF NOT EXISTS idx_platform_worker_slots_workspace
                    ON platform_worker_slots(workspace_key, expires_at);
                CREATE INDEX IF NOT EXISTS idx_platform_schedule_due
                    ON platform_schedules(enabled, next_run_at, owner_id);
                CREATE INDEX IF NOT EXISTS idx_platform_schedule_triggers_recent
                    ON platform_schedule_triggers(owner_id, schedule_id, scheduled_at DESC);
            """)
            # Additive columns keep a platform.db created by an early M1 build
            # readable without touching any legacy database.
            for table, column, ddl in (
                ("conversations", "title", "TEXT NOT NULL DEFAULT ''"),
                ("messages", "content", "TEXT NOT NULL DEFAULT ''"),
                ("runs", "created_at", "REAL NOT NULL DEFAULT 0"),
                ("runs", "updated_at", "REAL NOT NULL DEFAULT 0"),
                ("runs", "lease_id", "TEXT"),
                ("runs", "lease_owner", "TEXT"),
                ("runs", "lease_expires_at", "REAL"),
                ("runs", "attempt", "INTEGER NOT NULL DEFAULT 0"),
                ("runs", "result_text", "TEXT NOT NULL DEFAULT ''"),
                ("runs", "usage_input_tokens", "INTEGER NOT NULL DEFAULT 0"),
                ("runs", "usage_output_tokens", "INTEGER NOT NULL DEFAULT 0"),
                ("runs", "usage_total_tokens", "INTEGER NOT NULL DEFAULT 0"),
                ("runs", "usage_provider_requests", "INTEGER NOT NULL DEFAULT 0"),
                ("model_profiles", "provider_config", "TEXT NOT NULL DEFAULT '{}'"),
                ("nodes", "last_seen_at", "REAL"),
                ("nodes", "status", "TEXT NOT NULL DEFAULT 'unknown'"),
            ):
                columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            # An explicit per-conversation sequence survives equal clocks,
            # UUID ordering and VACUUM. Migrate existing arrival order once.
            conn.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
            if "turn_sequence" not in columns:
                conn.execute("ALTER TABLE messages ADD COLUMN turn_sequence INTEGER NOT NULL DEFAULT 0")
                conn.execute("""
                    WITH ordered AS (
                        SELECT message_id, ROW_NUMBER() OVER (
                            PARTITION BY conversation_id ORDER BY rowid
                        ) AS seq FROM messages
                    )
                    UPDATE messages SET turn_sequence=(
                        SELECT seq FROM ordered WHERE ordered.message_id=messages.message_id
                    )
                """)
            # A pre-sequence binary can be restored while keeping this DB.
            # Its INSERT omits turn_sequence. Repair any default-zero row
            # left by an earlier rollback, then make SQLite own allocation
            # for both versions, under the same writer transaction.
            conn.execute("""
                WITH pending AS (
                    SELECT message_id, conversation_id, ROW_NUMBER() OVER (
                        PARTITION BY conversation_id ORDER BY rowid
                    ) AS offset FROM messages WHERE turn_sequence=0
                ), maxima AS (
                    SELECT conversation_id, MAX(turn_sequence) AS last_sequence
                    FROM messages GROUP BY conversation_id
                ), assigned AS (
                    SELECT p.message_id, m.last_sequence+p.offset AS sequence
                    FROM pending p JOIN maxima m USING(conversation_id)
                )
                UPDATE messages SET turn_sequence=(
                    SELECT sequence FROM assigned WHERE assigned.message_id=messages.message_id
                ) WHERE turn_sequence=0
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS assign_message_turn_sequence
                AFTER INSERT ON messages WHEN NEW.turn_sequence=0
                BEGIN
                    UPDATE messages SET turn_sequence=(
                        SELECT COALESCE(MAX(turn_sequence),0)+1 FROM messages
                        WHERE conversation_id=NEW.conversation_id
                    ) WHERE message_id=NEW.message_id;
                END
            """)
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_message_turn_order "
                         "ON messages(conversation_id, turn_sequence)")
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            conn.execute("COMMIT")
        except PlatformRepositoryError:
            raise
        except (sqlite3.Error, OSError):
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None:
                conn.close()

    def schema_version(self) -> int:
        conn = None
        try:
            conn = self._connect()
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            return int(row["value"]) if row else 0
        except (sqlite3.Error, ValueError, TypeError):
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _row_model(row) -> dict[str, Any]:
        return {"profile_id": row["profile_id"], "provider": row["provider"],
                "model": row["model"], "secret_ref": row["secret_ref"],
                "capabilities": _decode(row["capabilities"]),
                "provider_config": _decode(row["provider_config"]),
                "enabled": bool(row["enabled"]), "owner_id": row["owner_id"]}

    @staticmethod
    def _row_workspace(row) -> dict[str, Any]:
        return {"workspace_id": row["workspace_id"], "owner_id": row["owner_id"],
                "name": row["name"], "backend": row["backend"],
                "root_path": row["root_path"], "default_node_id": row["default_node_id"],
                "enabled": bool(row["enabled"])}

    @staticmethod
    def _row_node(row) -> dict[str, Any]:
        return {"node_id": row["node_id"], "owner_id": row["owner_id"],
                "label": row["label"], "capabilities": _decode(row["capabilities"]),
                "enabled": bool(row["enabled"]),
                "last_seen_at": row["last_seen_at"],
                "status": row["status"]}

    def list_models(self, owner_id: str) -> list[dict[str, Any]]:
        owner_id = validate_owner_id(owner_id)
        conn = None
        try:
            conn = self._connect()
            return [self._row_model(row) for row in conn.execute("SELECT * FROM model_profiles WHERE owner_id=? ORDER BY profile_id", (owner_id,))]
        except PlatformValidationError:
            raise
        except sqlite3.Error:
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def get_model(self, owner_id: str, profile_id: str) -> dict[str, Any] | None:
        owner_id = validate_owner_id(owner_id)
        profile_id = validate_id(profile_id, "profile_id")
        conn = None
        try:
            conn = self._connect()
            row = conn.execute("SELECT * FROM model_profiles WHERE owner_id=? AND profile_id=?", (owner_id, profile_id)).fetchone()
            return self._row_model(row) if row else None
        except (PlatformValidationError, sqlite3.Error):
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def list_workspaces(self, owner_id: str) -> list[dict[str, Any]]:
        owner_id = validate_owner_id(owner_id)
        conn = None
        try:
            conn = self._connect()
            return [self._row_workspace(row) for row in conn.execute("SELECT * FROM workspaces WHERE owner_id=? ORDER BY workspace_id", (owner_id,))]
        except PlatformValidationError:
            raise
        except sqlite3.Error:
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def list_nodes(self, owner_id: str) -> list[dict[str, Any]]:
        owner_id = validate_owner_id(owner_id)
        conn = None
        try:
            conn = self._connect()
            return [self._row_node(row) for row in conn.execute("SELECT * FROM nodes WHERE owner_id=? ORDER BY node_id", (owner_id,))]
        except PlatformValidationError:
            raise
        except sqlite3.Error:
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def upsert_model(self, owner_id: str, data: Mapping[str, Any]) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        normalized = validate_model_profile(data)
        conn = None
        try:
            conn = self._connect()
            conn.execute("INSERT INTO model_profiles(owner_id,profile_id,provider,model,secret_ref,capabilities,provider_config,enabled) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(owner_id,profile_id) DO UPDATE SET provider=excluded.provider,model=excluded.model,secret_ref=excluded.secret_ref,capabilities=excluded.capabilities,provider_config=excluded.provider_config,enabled=excluded.enabled,updated_at=CURRENT_TIMESTAMP", (owner_id, normalized["profile_id"], normalized["provider"], normalized["model"], normalized["secret_ref"], _json(normalized["capabilities"]), _json(normalized["provider_config"]), int(normalized["enabled"])))
            return normalized | {"owner_id": owner_id}
        except (PlatformValidationError, PlatformRepositoryError):
            raise
        except sqlite3.Error:
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def upsert_workspace(self, owner_id: str, data: Mapping[str, Any]) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        normalized = validate_workspace(data)
        conn = None
        try:
            conn = self._connect()
            conn.execute("INSERT INTO workspaces(owner_id,workspace_id,name,backend,root_path,default_node_id,enabled) VALUES(?,?,?,?,?,?,?) ON CONFLICT(owner_id,workspace_id) DO UPDATE SET name=excluded.name,backend=excluded.backend,root_path=excluded.root_path,default_node_id=excluded.default_node_id,enabled=excluded.enabled", (owner_id, normalized["workspace_id"], normalized["name"], normalized["backend"], normalized["root_path"], normalized["default_node_id"], int(normalized["enabled"])))
            return normalized | {"owner_id": owner_id}
        except (PlatformValidationError, PlatformRepositoryError):
            raise
        except sqlite3.Error:
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def upsert_node(self, owner_id: str, data: Mapping[str, Any]) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        normalized = validate_node(data)
        conn = None
        try:
            conn = self._connect()
            conn.execute("INSERT INTO nodes(owner_id,node_id,label,capabilities,enabled) VALUES(?,?,?,?,?) ON CONFLICT(owner_id,node_id) DO UPDATE SET label=excluded.label,capabilities=excluded.capabilities,enabled=excluded.enabled", (owner_id, normalized["node_id"], normalized["label"], _json(normalized["capabilities"]), int(normalized["enabled"])))
            return normalized | {"owner_id": owner_id}
        except (PlatformValidationError, PlatformRepositoryError):
            raise
        except sqlite3.Error:
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def get_node(self, owner_id: str, node_id: str) -> dict[str, Any] | None:
        owner_id = validate_owner_id(owner_id)
        node_id = validate_id(node_id, "node_id")
        conn = None
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM nodes WHERE owner_id=? AND node_id=?",
                (owner_id, node_id)).fetchone()
            return self._row_node(row) if row else None
        except (PlatformValidationError, sqlite3.Error, KeyError):
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    @staticmethod
    def _derive_node_digest(secret: str, salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, 120_000)

    def provision_node_credential(self, owner_id: str, node_id: str, *, secret: str | None = None) -> dict[str, Any]:
        """Rotate a node credential and return the plaintext exactly once."""
        owner_id = validate_owner_id(owner_id)
        node_id = validate_id(node_id, "node_id")
        if secret is None:
            secret = secrets.token_urlsafe(32)
        if not isinstance(secret, str) or not (32 <= len(secret) <= 256) or any(c in secret for c in (":", "\r", "\n")):
            raise PlatformRepositoryError("invalid_node_credential")
        conn = None
        try:
            conn = self._connect()
            row = conn.execute("SELECT enabled FROM nodes WHERE owner_id=? AND node_id=?", (owner_id, node_id)).fetchone()
            if not row or not bool(row["enabled"]):
                raise PlatformRepositoryError("reference_forbidden")
            salt = secrets.token_bytes(16)
            digest = self._derive_node_digest(secret, salt)
            now = time.time()
            conn.execute("INSERT INTO node_credentials(owner_id,node_id,salt,secret_digest,enabled,issued_at,last_used_at) VALUES(?,?,?,?,1,?,NULL) ON CONFLICT(owner_id,node_id) DO UPDATE SET salt=excluded.salt,secret_digest=excluded.secret_digest,enabled=1,issued_at=excluded.issued_at,last_used_at=NULL", (owner_id, node_id, salt, digest, now))
            return {"owner_id": owner_id, "node_id": node_id, "credential": f"{node_id}:{secret}", "issued_at": now}
        except PlatformRepositoryError:
            raise
        except (sqlite3.Error, OSError):
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def authenticate_node_credential(self, node_id: str, secret: str) -> dict[str, Any] | None:
        node_id = validate_id(node_id, "node_id")
        if not isinstance(secret, str) or not secret or len(secret) > 256:
            return None
        conn = None
        try:
            conn = self._connect()
            rows = conn.execute("SELECT c.owner_id,c.node_id,c.salt,c.secret_digest,n.enabled AS node_enabled,c.enabled AS credential_enabled FROM node_credentials c JOIN nodes n ON n.owner_id=c.owner_id AND n.node_id=c.node_id WHERE c.node_id=?", (node_id,)).fetchall()
            match = None
            for row in rows:
                if not bool(row["node_enabled"]) or not bool(row["credential_enabled"]):
                    continue
                digest = self._derive_node_digest(secret, bytes(row["salt"]))
                if hmac.compare_digest(digest, bytes(row["secret_digest"])):
                    if match is not None:
                        return None
                    match = row
            if match is None:
                return None
            now = time.time()
            conn.execute("UPDATE node_credentials SET last_used_at=? WHERE owner_id=? AND node_id=?", (now, match["owner_id"], node_id))
            conn.execute("UPDATE nodes SET last_seen_at=?,status='online' WHERE owner_id=? AND node_id=?", (now, match["owner_id"], node_id))
            return {"owner_id": match["owner_id"], "node_id": node_id, "last_seen_at": now}
        except (PlatformValidationError, sqlite3.Error):
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def record_node_heartbeat(self, owner_id: str, node_id: str, *, status: str = "online") -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        node_id = validate_id(node_id, "node_id")
        if status not in {"online", "degraded"}:
            raise PlatformRepositoryError("invalid_node_status")
        conn = None
        try:
            conn = self._connect()
            now = time.time()
            cur = conn.execute("UPDATE nodes SET last_seen_at=?,status=? WHERE owner_id=? AND node_id=? AND enabled=1", (now, status, owner_id, node_id))
            if cur.rowcount != 1:
                raise PlatformRepositoryError("node_not_found")
            row = conn.execute("SELECT * FROM nodes WHERE owner_id=? AND node_id=?", (owner_id, node_id)).fetchone()
            return self._row_node(row)
        except PlatformRepositoryError:
            raise
        except (sqlite3.Error, OSError):
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def get_defaults(self, owner_id: str) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        conn = None
        try:
            conn = self._connect()
            row = conn.execute("SELECT * FROM owner_defaults WHERE owner_id=?", (owner_id,)).fetchone()
            return {"owner_id": owner_id, "revision": int(row["revision"]) if row else 0,
                    "model_profile_id": row["model_profile_id"] if row else None,
                    "workspace_id": row["workspace_id"] if row else None,
                    "execution_node_id": row["execution_node_id"] if row else None}
        except (PlatformValidationError, sqlite3.Error):
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def update_defaults(self, owner_id: str, values: Mapping[str, Any], expected_revision: int) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise PlatformRepositoryError("invalid_revision")
        fields = {key: values.get(key) for key in ("model_profile_id", "workspace_id", "execution_node_id")}
        for key, value in fields.items():
            if value is not None:
                fields[key] = validate_id(value, key)
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT revision FROM owner_defaults WHERE owner_id=?", (owner_id,)).fetchone()
            revision = int(current["revision"]) if current else 0
            if revision != expected_revision:
                conn.execute("ROLLBACK")
                raise PlatformRepositoryError("revision_conflict")
            for key, table, column in (("model_profile_id", "model_profiles", "profile_id"), ("workspace_id", "workspaces", "workspace_id"), ("execution_node_id", "nodes", "node_id")):
                value = fields[key]
                if value is not None:
                    row = conn.execute(f"SELECT owner_id, enabled FROM {table} WHERE owner_id=? AND {column}=?", (owner_id, value)).fetchone()
                    if not row or row["owner_id"] != owner_id or not bool(row["enabled"]):
                        conn.execute("ROLLBACK")
                        raise PlatformRepositoryError("reference_forbidden")
            new_revision = revision + 1
            conn.execute("INSERT INTO owner_defaults(owner_id,model_profile_id,workspace_id,execution_node_id,revision) VALUES(?,?,?,?,?) ON CONFLICT(owner_id) DO UPDATE SET model_profile_id=excluded.model_profile_id,workspace_id=excluded.workspace_id,execution_node_id=excluded.execution_node_id,revision=excluded.revision", (owner_id, fields["model_profile_id"], fields["workspace_id"], fields["execution_node_id"], new_revision))
            conn.execute("COMMIT")
            return {"owner_id": owner_id, "revision": new_revision, **fields}
        except PlatformRepositoryError:
            raise
        except (sqlite3.Error, ValueError, TypeError):
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    @staticmethod
    def _row_conversation(row) -> dict[str, Any]:
        return {"conversation_id": row["conversation_id"], "owner_id": row["owner_id"],
                "title": row["title"], "workspace_id": row["workspace_id"],
                "overrides": _decode(row["overrides"]), "revision": int(row["revision"])}

    @staticmethod
    def _row_message(row) -> dict[str, Any]:
        return {"message_id": row["message_id"], "conversation_id": row["conversation_id"],
                "owner_id": row["owner_id"], "role": row["role"], "content": row["content"],
                "client_token": row["client_token"], "created_at": row["created_at"],
                "turn_sequence": row["turn_sequence"]}

    @staticmethod
    def _row_run(row) -> dict[str, Any]:
        return {"run_id": row["run_id"], "conversation_id": row["conversation_id"],
                "owner_id": row["owner_id"], "trigger_message_id": row["trigger_message_id"],
                "state": row["state"], "config_snapshot": _decode(row["config_snapshot"]),
                "cancel_requested": bool(row["cancel_requested"]),
                "attempt": int(row["attempt"] or 0),
                "lease_expires_at": row["lease_expires_at"],
                "result_text": row["result_text"] or "",
                "usage": {
                    "input_tokens": int(row["usage_input_tokens"] or 0),
                    "output_tokens": int(row["usage_output_tokens"] or 0),
                    "total_tokens": int(row["usage_total_tokens"] or 0),
                    "provider_requests": int(row["usage_provider_requests"] or 0),
                },
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    @staticmethod
    def _run_public_claim(row) -> dict[str, Any]:
        return {
            "run_id": row["run_id"], "owner_id": row["owner_id"],
            "conversation_id": row["conversation_id"],
            "trigger_message_id": row["trigger_message_id"],
            "state": row["state"], "config_snapshot": _decode(row["config_snapshot"]),
                "cancel_requested": bool(row["cancel_requested"]),
                "attempt": int(row["attempt"] or 0),
                "lease_id": row["lease_id"],
            "lease_owner": row["lease_owner"],
            "lease_expires_at": row["lease_expires_at"],
        }

    def get_workspace(self, owner_id: str, workspace_id: str) -> dict[str, Any] | None:
        owner_id = validate_owner_id(owner_id)
        workspace_id = validate_id(workspace_id, "workspace_id")
        conn = None
        try:
            conn = self._connect()
            row = conn.execute("SELECT * FROM workspaces WHERE owner_id=? AND workspace_id=?", (owner_id, workspace_id)).fetchone()
            return self._row_workspace(row) if row else None
        except (PlatformValidationError, sqlite3.Error):
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def create_conversation(self, owner_id: str, conversation_id: str, *, title: str, workspace_id: str | None, overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        conversation_id = validate_id(conversation_id, "conversation_id")
        if workspace_id is not None:
            workspace_id = validate_id(workspace_id, "workspace_id")
        conn = None
        try:
            conn = self._connect()
            if workspace_id:
                row = conn.execute("SELECT enabled FROM workspaces WHERE owner_id=? AND workspace_id=?", (owner_id, workspace_id)).fetchone()
                if not row or not bool(row["enabled"]):
                    raise PlatformRepositoryError("reference_forbidden")
            conn.execute("INSERT INTO conversations(conversation_id,owner_id,title,workspace_id,overrides,revision) VALUES(?,?,?,?,?,0)", (conversation_id, owner_id, str(title or "")[:120], workspace_id, _json(overrides or {})))
            row = conn.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
            return self._row_conversation(row)
        except PlatformRepositoryError:
            raise
        except sqlite3.IntegrityError:
            raise PlatformRepositoryError("conversation_conflict") from None
        except (PlatformValidationError, sqlite3.Error):
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def _conversation_messages(self, conn, owner_id, conversation_id, trigger_id=None):
        """Project user turns and durable assistant replies in turn order.

        Runs already persist their final answer atomically with completion.
        Reusing that record avoids a second, diverging copy of the reply.
        A worker's current trigger includes only its user message; later turns
        and the current Run's own reply can never enter its input.
        """
        rows = conn.execute(
            "SELECT m.*,r.run_id AS reply_run_id,r.state AS reply_state,"
            "r.result_text AS reply_text,r.updated_at AS reply_at "
            "FROM messages m LEFT JOIN runs r ON r.trigger_message_id=m.message_id "
            "AND r.owner_id=m.owner_id WHERE m.owner_id=? AND m.conversation_id=? "
            "AND (? IS NULL OR m.turn_sequence<=(SELECT turn_sequence FROM messages WHERE message_id=?)) "
            "ORDER BY m.turn_sequence",
            (owner_id, conversation_id, trigger_id, trigger_id),
        ).fetchall()
        result = []
        for row in rows:
            result.append(self._row_message(row))
            if (row["message_id"] != trigger_id and row["reply_state"] == "succeeded"
                    and row["reply_text"]):
                result.append({
                    "message_id": "reply_" + row["reply_run_id"],
                    "conversation_id": conversation_id, "owner_id": owner_id,
                    "role": "assistant", "content": row["reply_text"],
                    "client_token": None, "created_at": str(row["reply_at"]),
                    "turn_sequence": row["turn_sequence"],
                })
        return result

    def get_conversation(self, owner_id: str, conversation_id: str) -> dict[str, Any] | None:
        owner_id = validate_owner_id(owner_id)
        conversation_id = validate_id(conversation_id, "conversation_id")
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN")
            row = conn.execute("SELECT * FROM conversations WHERE owner_id=? AND conversation_id=?", (owner_id, conversation_id)).fetchone()
            if not row:
                return None
            conversation = self._row_conversation(row)
            conversation["messages"] = self._conversation_messages(conn, owner_id, conversation_id)
            conversation["runs"] = [self._row_run(item) for item in conn.execute("SELECT r.* FROM runs r JOIN messages m ON m.message_id=r.trigger_message_id WHERE r.owner_id=? AND r.conversation_id=? ORDER BY m.turn_sequence", (owner_id, conversation_id)).fetchall()]
            self._attach_requests(conn, owner_id, conversation["runs"], conversation_id=conversation_id)
            return conversation
        except (PlatformValidationError, sqlite3.Error):
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def list_conversations(self, owner_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """Return a bounded owner-scoped inbox without message credentials/tokens."""
        owner_id = validate_owner_id(owner_id)
        try:
            limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            raise PlatformRepositoryError("invalid_limit") from None
        conn = None
        try:
            conn = self._connect()
            rows = conn.execute(
                """
                SELECT c.*,
                  (SELECT m.content FROM messages m
                   WHERE m.owner_id=c.owner_id AND m.conversation_id=c.conversation_id
                   ORDER BY CAST(m.created_at AS REAL) DESC, m.message_id DESC LIMIT 1) AS last_message,
                  (SELECT r.run_id FROM runs r
                   WHERE r.owner_id=c.owner_id AND r.conversation_id=c.conversation_id
                   ORDER BY r.updated_at DESC, r.run_id DESC LIMIT 1) AS latest_run_id,
                  (SELECT r.state FROM runs r
                   WHERE r.owner_id=c.owner_id AND r.conversation_id=c.conversation_id
                   ORDER BY r.updated_at DESC, r.run_id DESC LIMIT 1) AS latest_run_state,
                  (SELECT r.result_text FROM runs r
                   WHERE r.owner_id=c.owner_id AND r.conversation_id=c.conversation_id
                   ORDER BY r.updated_at DESC, r.run_id DESC LIMIT 1) AS latest_run_result,
                  (SELECT r.updated_at FROM runs r
                   WHERE r.owner_id=c.owner_id AND r.conversation_id=c.conversation_id
                   ORDER BY r.updated_at DESC, r.run_id DESC LIMIT 1) AS latest_run_at,
                  (SELECT MAX(CAST(m.created_at AS REAL)) FROM messages m
                   WHERE m.owner_id=c.owner_id AND m.conversation_id=c.conversation_id) AS latest_message_at
                FROM conversations c
                WHERE c.owner_id=?
                ORDER BY COALESCE(latest_run_at, latest_message_at, 0) DESC, c.conversation_id DESC
                LIMIT ?
                """, (owner_id, limit)).fetchall()
            result = []
            for row in rows:
                item = self._row_conversation(row)
                item.pop("owner_id", None)
                item.pop("overrides", None)
                item["last_message_preview"] = str(row["last_message"] or "")[:240]
                if row["latest_run_id"]:
                    item["latest_run"] = {
                        "run_id": row["latest_run_id"],
                        "state": row["latest_run_state"],
                        "result_preview": str(row["latest_run_result"] or "")[:500],
                    }
                else:
                    item["latest_run"] = None
                result.append(item)
            return result
        except PlatformRepositoryError:
            raise
        except (sqlite3.Error, PlatformValidationError):
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def append_turn(self, owner_id: str, conversation_id: str, message_id: str, run_id: str, *, text: str, client_token: str, config_snapshot: Mapping[str, Any], now: float) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        conversation_id = validate_id(conversation_id, "conversation_id")
        message_id = validate_id(message_id, "message_id")
        run_id = validate_id(run_id, "run_id")
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            conversation = conn.execute("SELECT * FROM conversations WHERE owner_id=? AND conversation_id=?", (owner_id, conversation_id)).fetchone()
            if not conversation:
                conn.execute("ROLLBACK")
                raise PlatformRepositoryError("conversation_not_found")
            prior = conn.execute("SELECT * FROM messages WHERE owner_id=? AND conversation_id=? AND client_token=?", (owner_id, conversation_id, client_token)).fetchone()
            if prior:
                if prior["content"] != text:
                    conn.execute("ROLLBACK")
                    raise PlatformRepositoryError("idempotency_conflict")
                run = conn.execute("SELECT * FROM runs WHERE trigger_message_id=?", (prior["message_id"],)).fetchone()
                conn.execute("COMMIT")
                if not run:
                    raise PlatformRepositoryError("run_not_found")
                return {"created": False, "message": self._row_message(prior), "run": self._row_run(run)}
            conn.execute("INSERT INTO messages(message_id,conversation_id,owner_id,role,content_ref,content,client_token,created_at) VALUES(?,?,?,?,?,?,?,?)", (message_id, conversation_id, owner_id, "user", "inline", text, client_token, str(now)))
            snapshot = _json(config_snapshot)
            conn.execute("INSERT INTO runs(run_id,conversation_id,owner_id,trigger_message_id,state,config_snapshot,cancel_requested,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (run_id, conversation_id, owner_id, message_id, "queued", snapshot, 0, float(now), float(now)))
            message = conn.execute("SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone()
            run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            conn.execute("COMMIT")
            return {"created": True, "message": self._row_message(message), "run": self._row_run(run)}
        except PlatformRepositoryError:
            raise
        except sqlite3.IntegrityError:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise PlatformRepositoryError("idempotency_conflict") from None
        except (sqlite3.Error, TypeError, ValueError):
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    @staticmethod
    def _row_legacy_task_link(row) -> dict[str, Any]:
        if row is None:
            return {}
        def decode(value):
            try:
                parsed = json.loads(value or "{}")
            except (TypeError, ValueError):
                return {}
            return dict(parsed) if isinstance(parsed, Mapping) else {}
        return {
            "owner_id": row["owner_id"], "run_id": row["run_id"],
            "request": decode(row["request"]),
            "request_hash": row["request_hash"], "state": row["state"],
            "task_id": row["task_id"], "attempt_id": row["attempt_id"],
            "session_id": row["session_id"], "task_state": row["task_state"],
            "projection": decode(row["projection"]),
            "error_code": row["error_code"],
            "lease_owner": row["lease_owner"],
            "lease_expires_at": row["lease_expires_at"],
            "bridge_attempt": int(row["bridge_attempt"] or 0),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def enqueue_legacy_task(self, owner_id: str, run_id: str, request: Mapping[str, Any], *, now: float) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        run_id = validate_id(run_id, "run_id")
        if not isinstance(request, Mapping):
            raise PlatformRepositoryError("invalid_legacy_task")
        try:
            encoded_request = _json(dict(request))
        except PlatformRepositoryError:
            raise PlatformRepositoryError("invalid_legacy_task") from None
        request_hash = hashlib.sha256(encoded_request.encode("utf-8")).hexdigest()
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM legacy_task_links WHERE owner_id=? AND run_id=?",
                (owner_id, run_id),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    conn.execute("ROLLBACK")
                    raise PlatformRepositoryError("legacy_task_conflict")
                conn.execute("COMMIT")
                return self._row_legacy_task_link(existing)
            run = conn.execute(
                "SELECT state FROM runs WHERE owner_id=? AND run_id=?",
                (owner_id, run_id),
            ).fetchone()
            if not run:
                conn.execute("ROLLBACK")
                raise PlatformRepositoryError("run_not_found")
            if run["state"] != "queued":
                conn.execute("ROLLBACK")
                raise PlatformRepositoryError("legacy_task_conflict")
            timestamp = float(now)
            conn.execute(
                "INSERT INTO legacy_task_links(owner_id,run_id,request,request_hash,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (owner_id, run_id, encoded_request, request_hash, "pending", timestamp, timestamp),
            )
            conn.execute(
                "UPDATE runs SET state='waiting_task',updated_at=? WHERE owner_id=? AND run_id=? AND state='queued'",
                (timestamp, owner_id, run_id),
            )
            next_sequence = conn.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 AS next_sequence FROM run_events WHERE run_id=?",
                (run_id,),
            ).fetchone()["next_sequence"]
            conn.execute(
                "INSERT INTO run_events(run_id,sequence,kind,payload,created_at) VALUES(?,?,?,?,?)",
                (run_id, int(next_sequence), "legacy_task_queued",
                 _json({"bridge_state": "pending", "state": "waiting_task"}), timestamp),
            )
            row = conn.execute(
                "SELECT * FROM legacy_task_links WHERE owner_id=? AND run_id=?",
                (owner_id, run_id),
            ).fetchone()
            conn.execute("COMMIT")
            return self._row_legacy_task_link(row)
        except PlatformRepositoryError:
            raise
        except (sqlite3.Error, TypeError, ValueError):
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def get_legacy_task_link(self, owner_id: str, run_id: str) -> dict[str, Any] | None:
        owner_id = validate_owner_id(owner_id)
        run_id = validate_id(run_id, "run_id")
        conn = None
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM legacy_task_links WHERE owner_id=? AND run_id=?",
                (owner_id, run_id),
            ).fetchone()
            return self._row_legacy_task_link(row) if row else None
        except (sqlite3.Error, PlatformValidationError):
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def claim_legacy_task(self, owner_id: str, *, worker_id: str, now: float, lease_s: float = 60.0, run_id: str | None = None) -> dict[str, Any] | None:
        owner_id = validate_owner_id(owner_id)
        if run_id is not None:
            run_id = validate_id(run_id, "run_id")
        if not isinstance(worker_id, str) or not worker_id or len(worker_id) > 128:
            raise PlatformRepositoryError("invalid_worker_id")
        try:
            now = float(now); lease_s = float(lease_s)
        except (TypeError, ValueError):
            raise PlatformRepositoryError("invalid_lease") from None
        if lease_s <= 0 or lease_s > 3600 or not (now == now):
            raise PlatformRepositoryError("invalid_lease")
        conn = None
        try:
            conn = self._connect(); conn.execute("BEGIN IMMEDIATE")
            if run_id is None:
                row = conn.execute(
                    "SELECT * FROM legacy_task_links WHERE owner_id=? AND state IN ('pending','linked','leased') "
                    "AND (state != 'leased' OR lease_expires_at IS NULL OR lease_expires_at<=?) "
                    "ORDER BY updated_at, run_id LIMIT 1",
                    (owner_id, now),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM legacy_task_links WHERE owner_id=? AND run_id=? "
                    "AND state IN ('pending','linked','leased') "
                    "AND (state != 'leased' OR lease_expires_at IS NULL OR lease_expires_at<=?)",
                    (owner_id, run_id, now),
                ).fetchone()
            if row is None:
                conn.execute("ROLLBACK"); return None
            lease_id = int(row["bridge_attempt"] or 0) + 1
            conn.execute(
                "UPDATE legacy_task_links SET state='leased',lease_owner=?,lease_expires_at=?,bridge_attempt=?,updated_at=? "
                "WHERE owner_id=? AND run_id=?",
                (worker_id, now + lease_s, lease_id, now, owner_id, row["run_id"]),
            )
            claimed = conn.execute(
                "SELECT * FROM legacy_task_links WHERE owner_id=? AND run_id=?",
                (owner_id, row["run_id"]),
            ).fetchone()
            conn.execute("COMMIT")
            return self._row_legacy_task_link(claimed)
        except PlatformRepositoryError:
            raise
        except (sqlite3.Error, TypeError, ValueError):
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def release_legacy_task_claim(self, owner_id: str, run_id: str, *, worker_id: str, lease_attempt: int, now: float, error_code: str | None = None) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        run_id = validate_id(run_id, "run_id")
        if not isinstance(worker_id, str) or not worker_id or len(worker_id) > 128:
            raise PlatformRepositoryError("invalid_worker_id")
        conn = None
        try:
            conn = self._connect(); conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM legacy_task_links WHERE owner_id=? AND run_id=? AND state='leased' "
                "AND lease_owner=? AND bridge_attempt=? AND lease_expires_at>?",
                (owner_id, run_id, worker_id, int(lease_attempt), float(now)),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK"); raise PlatformRepositoryError("legacy_task_lease_mismatch")
            next_state = "linked" if row["task_id"] else "pending"
            conn.execute(
                "UPDATE legacy_task_links SET state=?,error_code=?,lease_owner=NULL,lease_expires_at=NULL,updated_at=? "
                "WHERE owner_id=? AND run_id=?",
                (next_state, str(error_code or "")[:120] or None, float(now), owner_id, run_id),
            )
            saved = conn.execute(
                "SELECT * FROM legacy_task_links WHERE owner_id=? AND run_id=?",
                (owner_id, run_id),
            ).fetchone()
            conn.execute("COMMIT")
            return self._row_legacy_task_link(saved)
        except PlatformRepositoryError:
            raise
        except (sqlite3.Error, TypeError, ValueError):
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def apply_legacy_task_projection(self, owner_id: str, run_id: str, projection: Mapping[str, Any], *, now: float, worker_id: str | None = None, lease_attempt: int | None = None) -> tuple[dict[str, Any], bool]:
        owner_id = validate_owner_id(owner_id)
        run_id = validate_id(run_id, "run_id")
        if not isinstance(projection, Mapping):
            raise PlatformRepositoryError("invalid_legacy_task")
        try:
            encoded = _json(dict(projection))
        except PlatformRepositoryError:
            raise PlatformRepositoryError("invalid_legacy_task") from None
        bridge_state = str(projection.get("bridge_state") or "linked")
        if bridge_state not in {"linked", "succeeded", "failed", "cancelled"}:
            raise PlatformRepositoryError("invalid_legacy_task")
        conn = None
        try:
            conn = self._connect(); conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM legacy_task_links WHERE owner_id=? AND run_id=?",
                (owner_id, run_id),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK"); raise PlatformRepositoryError("legacy_task_not_found")
            if row["state"] == "leased":
                if worker_id is None or lease_attempt is None or row["lease_owner"] != worker_id or int(row["bridge_attempt"]) != int(lease_attempt) or row["lease_expires_at"] is None or float(row["lease_expires_at"]) <= float(now):
                    conn.execute("ROLLBACK"); raise PlatformRepositoryError("legacy_task_lease_mismatch")
            prior_projection = row["projection"] or "{}"
            if prior_projection == encoded and row["state"] == bridge_state:
                conn.execute("COMMIT"); return self._row_legacy_task_link(row), False
            task_state = projection.get("task_state")
            next_state = bridge_state if bridge_state != "linked" else "linked"
            timestamp = float(now)
            conn.execute(
                "UPDATE legacy_task_links SET state=?,task_id=?,attempt_id=?,session_id=?,task_state=?,projection=?,error_code=?,lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE owner_id=? AND run_id=?",
                (next_state, projection.get("task_id"), projection.get("attempt_id"), projection.get("session_id"), task_state, encoded, projection.get("error_code"), timestamp, owner_id, run_id),
            )
            run = conn.execute("SELECT state FROM runs WHERE owner_id=? AND run_id=?", (owner_id, run_id)).fetchone()
            run_state = {"linked": "waiting_task", "succeeded": "succeeded", "failed": "failed", "cancelled": "cancelled"}[bridge_state]
            if run and run["state"] not in {"succeeded", "failed", "unknown", "cancelled", "cancelling"}:
                conn.execute("UPDATE runs SET state=?,result_text=?,updated_at=? WHERE owner_id=? AND run_id=?", (run_state, str(projection.get("summary") or "")[:32768], timestamp, owner_id, run_id))
            next_sequence = conn.execute("SELECT COALESCE(MAX(sequence),0)+1 AS next_sequence FROM run_events WHERE run_id=?", (run_id,)).fetchone()["next_sequence"]
            event_payload = dict(projection)
            event_payload["bridge_state"] = bridge_state
            conn.execute("INSERT INTO run_events(run_id,sequence,kind,payload,created_at) VALUES(?,?,?,?,?)", (run_id, int(next_sequence), "legacy_task_update", _json(event_payload), timestamp))
            saved = conn.execute("SELECT * FROM legacy_task_links WHERE owner_id=? AND run_id=?", (owner_id, run_id)).fetchone()
            conn.execute("COMMIT")
            return self._row_legacy_task_link(saved), True
        except PlatformRepositoryError:
            raise
        except (sqlite3.Error, TypeError, ValueError):
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    @staticmethod
    def _attach_requests(conn, owner_id, runs, *, conversation_id=None, run_id=None):
        # One indexed query for the entire conversation, never one per message.
        rows = conn.execute(
            "SELECT e.run_id,e.kind,e.payload FROM run_events e JOIN runs r ON r.run_id=e.run_id "
            "WHERE r.owner_id=? AND " + ("r.conversation_id=?" if conversation_id else "r.run_id=?") +
            " AND e.kind IN ('provider_request_started','provider_request_finished') ORDER BY e.run_id,e.sequence",
            (owner_id, conversation_id or run_id)).fetchall()
        grouped = {}
        for row in rows:
            grouped.setdefault(row['run_id'], []).append({'kind': row['kind'], 'payload': _decode(row['payload'])})
        for run in runs:
            run['requests'] = project_requests(grouped.get(run['run_id'], []), run['state'])
            for request in run['requests']:
                if request.get('status') == 'running':
                    request['elapsed_ms'] = max(0, round((time.time() - request['started_at']) * 1000))

    def get_run(self, owner_id: str, run_id: str) -> dict[str, Any] | None:
        owner_id = validate_owner_id(owner_id)
        run_id = validate_id(run_id, "run_id")
        conn = None
        try:
            conn = self._connect()
            row = conn.execute("SELECT * FROM runs WHERE owner_id=? AND run_id=?", (owner_id, run_id)).fetchone()
            if row is None:
                return None
            result = self._row_run(row)
            self._attach_requests(conn, owner_id, [result], run_id=run_id)
            return result
        except (PlatformValidationError, sqlite3.Error):
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def get_run_execution_context(self, owner_id: str, run_id: str) -> dict[str, Any] | None:
        """Return the owner-scoped immutable inputs a worker may consume.

        The message is read through the run's trigger foreign key rather than
        trusting a caller supplied conversation id.
        """
        owner_id = validate_owner_id(owner_id)
        run_id = validate_id(run_id, "run_id")
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN")
            row = conn.execute(
                "SELECT r.*, m.role AS message_role, m.content AS message_content,"
                " c.workspace_id AS conversation_workspace_id"
                " FROM runs r JOIN messages m ON m.message_id=r.trigger_message_id"
                " JOIN conversations c ON c.conversation_id=r.conversation_id"
                " WHERE r.owner_id=? AND r.run_id=?", (owner_id, run_id)).fetchone()
            if not row:
                return None
            result = self._run_public_claim(row)
            result["message"] = {
                "message_id": row["trigger_message_id"],
                "role": row["message_role"],
                "content": row["message_content"],
            }
            result["conversation_workspace_id"] = row["conversation_workspace_id"]
            result["messages"] = [
                {"message_id": item["message_id"], "role": item["role"], "content": item["content"]}
                for item in self._conversation_messages(
                    conn, owner_id, row["conversation_id"], row["trigger_message_id"])
            ]
            return result
        except (PlatformValidationError, sqlite3.Error):
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def claim_run(self, owner_id: str, *, worker_id: str, now: float, lease_s: float = 60.0) -> dict[str, Any] | None:
        """Atomically claim the oldest queued (or expired) Run."""
        owner_id = validate_owner_id(owner_id)
        if not isinstance(worker_id, str) or not worker_id or len(worker_id) > 128:
            raise PlatformRepositoryError("invalid_worker_id")
        try:
            lease_s = float(lease_s)
            now = float(now)
        except (TypeError, ValueError):
            raise PlatformRepositoryError("invalid_lease") from None
        if lease_s <= 0 or lease_s > 3600 or not (now == now):
            raise PlatformRepositoryError("invalid_lease")
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT r.* FROM runs r JOIN messages trigger ON trigger.message_id=r.trigger_message_id "
                "WHERE r.owner_id=? AND ((r.state='queued' AND"
                " (r.lease_id IS NULL OR r.lease_expires_at IS NULL OR r.lease_expires_at<=?)) OR"
                " (r.state='cancelling' AND (r.lease_expires_at IS NULL OR r.lease_expires_at<=?)) OR"
                " (r.state='running' AND r.lease_expires_at IS NOT NULL AND r.lease_expires_at<=?))"
                " AND NOT EXISTS (SELECT 1 FROM runs prior JOIN messages pm ON pm.message_id=prior.trigger_message_id "
                " WHERE prior.owner_id=r.owner_id AND prior.conversation_id=r.conversation_id "
                " AND pm.turn_sequence<trigger.turn_sequence "
                " AND prior.state NOT IN ('succeeded','failed','unknown','cancelled'))"
                " ORDER BY r.created_at, trigger.turn_sequence, r.run_id LIMIT 1", (owner_id, now, now, now)).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return None
            # run_started is committed under the worker lease before the first
            # provider/tool call. There is no durable runtime checkpoint from
            # which to resume, so crossing that boundary forbids automatic
            # replay (including when cancellation raced with the crash).
            started = conn.execute(
                "SELECT 1 FROM run_events WHERE run_id=? "
                "AND kind IN ('run_started','tool_call') LIMIT 1",
                (row["run_id"],),
            ).fetchone()
            if started:
                conn.execute(
                    "UPDATE runs SET state='unknown',lease_id=NULL,lease_owner=NULL,"
                    "lease_expires_at=NULL,updated_at=? WHERE owner_id=? AND run_id=?",
                    (now, owner_id, row["run_id"]),
                )
                sequence = conn.execute(
                    "SELECT COALESCE(MAX(sequence),0)+1 FROM run_events WHERE run_id=?",
                    (row["run_id"],),
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO run_events(run_id,sequence,kind,payload,created_at) VALUES(?,?,?,?,?)",
                    (row["run_id"], sequence, "run_unknown",
                     _json({"error_code": "execution_interrupted", "attempt": row["attempt"]}), now),
                )
                recovered = conn.execute("SELECT * FROM runs WHERE run_id=?", (row["run_id"],)).fetchone()
                conn.execute("COMMIT")
                result = self._row_run(recovered)
                result["recovered_before_claim"] = True
                return result
            if bool(row["cancel_requested"]):
                conn.execute(
                    "UPDATE runs SET state='cancelled',lease_id=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=?"
                    " WHERE owner_id=? AND run_id=?",
                    (now, owner_id, row["run_id"]),
                )
                row = conn.execute(
                    "SELECT r.*, m.role AS message_role, m.content AS message_content,"
                    " c.workspace_id AS conversation_workspace_id"
                    " FROM runs r JOIN messages m ON m.message_id=r.trigger_message_id"
                    " JOIN conversations c ON c.conversation_id=r.conversation_id"
                    " WHERE r.owner_id=? AND r.run_id=?", (owner_id, row["run_id"])).fetchone()
                conn.execute("COMMIT")
                result = self._run_public_claim(row)
                result["cancelled_before_claim"] = True
                return result
            lease_id = "runlease_" + secrets.token_urlsafe(18)
            attempt = int(row["attempt"] or 0) + 1
            conn.execute(
                "UPDATE runs SET state='running', lease_id=?, lease_owner=?, lease_expires_at=?, attempt=?, updated_at=?"
                " WHERE owner_id=? AND run_id=?",
                (lease_id, worker_id, now + lease_s, attempt, now, owner_id, row["run_id"]),
            )
            row = conn.execute(
                "SELECT r.*, m.role AS message_role, m.content AS message_content,"
                " c.workspace_id AS conversation_workspace_id"
                " FROM runs r JOIN messages m ON m.message_id=r.trigger_message_id"
                " JOIN conversations c ON c.conversation_id=r.conversation_id"
                " WHERE r.owner_id=? AND r.run_id=?", (owner_id, row["run_id"])).fetchone()
            result = self._run_public_claim(row)
            result["message"] = {
                "message_id": row["trigger_message_id"],
                "role": row["message_role"],
                "content": row["message_content"],
            }
            result["conversation_workspace_id"] = row["conversation_workspace_id"]
            result["messages"] = [
                {"message_id": item["message_id"], "role": item["role"], "content": item["content"]}
                for item in self._conversation_messages(
                    conn, owner_id, row["conversation_id"], row["trigger_message_id"])
            ]
            conn.execute("COMMIT")
            return result
        except PlatformRepositoryError:
            raise
        except sqlite3.Error:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def claim_worker_owner(self, *, worker_id: str, now: float,
                           lease_s: float = 60.0) -> dict[str, Any] | None:
        """Claim the fairest owner that has an eligible Run.

        This lease is a scheduler slot, separate from the Run lease. It keeps
        multiple Hub processes from repeatedly selecting the same owner while
        the Run itself remains fenced by ``claim_run``.
        """
        if not isinstance(worker_id, str) or not worker_id or len(worker_id) > 128:
            raise PlatformRepositoryError("invalid_worker_id")
        try:
            now = float(now); lease_s = float(lease_s)
        except (TypeError, ValueError):
            raise PlatformRepositoryError("invalid_lease") from None
        if lease_s <= 0 or lease_s > 3600 or not (now == now):
            raise PlatformRepositoryError("invalid_lease")
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO platform_worker_owners(owner_id, enabled, updated_at) "
                "SELECT DISTINCT owner_id, 1, ? FROM runs", (now,))
            row = conn.execute(
                "SELECT o.owner_id FROM platform_worker_owners o "
                "WHERE o.enabled=1 AND (o.lease_expires_at IS NULL OR o.lease_expires_at<=?) "
                "AND EXISTS (SELECT 1 FROM runs r WHERE r.owner_id=o.owner_id AND ("
                "(r.state='queued' AND (r.lease_id IS NULL OR r.lease_expires_at IS NULL OR r.lease_expires_at<=?)) OR "
                "(r.state='cancelling' AND (r.lease_expires_at IS NULL OR r.lease_expires_at<=?)) OR "
                "(r.state='running' AND r.lease_expires_at IS NOT NULL AND r.lease_expires_at<=?) OR "
                "EXISTS (SELECT 1 FROM legacy_task_links l WHERE l.owner_id=o.owner_id AND l.state IN ('pending','linked')"
                " OR (l.state='leased' AND (l.lease_expires_at IS NULL OR l.lease_expires_at<=?)))"
                ")) ORDER BY (o.last_claim_at IS NOT NULL), o.last_claim_at, o.owner_id LIMIT 1",
                (now, now, now, now, now),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return None
            lease_id = "ownerlease_" + secrets.token_urlsafe(18)
            conn.execute(
                "UPDATE platform_worker_owners SET lease_id=?, lease_owner=?, "
                "lease_expires_at=?, last_claim_at=?, updated_at=? WHERE owner_id=?",
                (lease_id, worker_id, now + lease_s, now, now, row["owner_id"]),
            )
            conn.execute("COMMIT")
            return {
                "owner_id": row["owner_id"], "lease_id": lease_id,
                "lease_owner": worker_id, "lease_expires_at": now + lease_s,
                "last_claim_at": now,
            }
        except PlatformRepositoryError:
            raise
        except sqlite3.Error:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def renew_worker_owner(self, owner_id: str, *, lease_id: str,
                           worker_id: str, now: float,
                           lease_s: float = 60.0) -> bool:
        owner_id = validate_owner_id(owner_id)
        conn = None
        try:
            conn = self._connect()
            cur = conn.execute(
                "UPDATE platform_worker_owners SET lease_expires_at=?, updated_at=? "
                "WHERE owner_id=? AND lease_id=? AND lease_owner=? AND lease_expires_at>?",
                (float(now) + float(lease_s), float(now), owner_id, lease_id,
                 worker_id, float(now)),
            )
            return cur.rowcount == 1
        except (sqlite3.Error, TypeError, ValueError):
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def release_worker_owner(self, owner_id: str, *, lease_id: str,
                             worker_id: str, now: float) -> bool:
        owner_id = validate_owner_id(owner_id)
        conn = None
        try:
            conn = self._connect()
            cur = conn.execute(
                "UPDATE platform_worker_owners SET lease_id=NULL, lease_owner=NULL, "
                "lease_expires_at=NULL, updated_at=? WHERE owner_id=? AND lease_id=? "
                "AND lease_owner=?",
                (float(now), owner_id, lease_id, worker_id),
            )
            return cur.rowcount == 1
        except (sqlite3.Error, TypeError, ValueError):
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def claim_worker_slot(self, owner_id: str, run_id: str, *, worker_id: str,
                          workspace_key: str | None, max_concurrency: int,
                          max_workspace_concurrency: int, now: float,
                          lease_s: float = 60.0) -> dict[str, Any] | None:
        owner_id = validate_owner_id(owner_id)
        run_id = validate_id(run_id, "run_id")
        max_concurrency = max(1, min(64, int(max_concurrency)))
        max_workspace_concurrency = max(1, min(64, int(max_workspace_concurrency)))
        if not isinstance(worker_id, str) or not worker_id or len(worker_id) > 128:
            raise PlatformRepositoryError("invalid_worker_id")
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM platform_worker_slots WHERE expires_at<=?", (float(now),))
            active = int(conn.execute(
                "SELECT COUNT(*) FROM platform_worker_slots WHERE expires_at>?", (float(now),)
            ).fetchone()[0])
            if active >= max_concurrency:
                conn.execute("ROLLBACK")
                return None
            if workspace_key is not None:
                active_workspace = int(conn.execute(
                    "SELECT COUNT(*) FROM platform_worker_slots WHERE workspace_key=? AND expires_at>?",
                    (workspace_key, float(now)),
                ).fetchone()[0])
                if active_workspace >= max_workspace_concurrency:
                    conn.execute("ROLLBACK")
                    return None
            lease_id = "workerslot_" + secrets.token_urlsafe(18)
            conn.execute(
                "INSERT INTO platform_worker_slots(lease_id,owner_id,run_id,workspace_key,worker_id,expires_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (lease_id, owner_id, run_id, workspace_key, worker_id,
                 float(now) + float(lease_s), float(now), float(now)),
            )
            conn.execute("COMMIT")
            return {"lease_id": lease_id, "owner_id": owner_id, "run_id": run_id,
                    "workspace_key": workspace_key, "worker_id": worker_id,
                    "expires_at": float(now) + float(lease_s)}
        except sqlite3.IntegrityError:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            return None
        except sqlite3.Error:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def renew_worker_slot(self, lease_id: str, *, worker_id: str, now: float,
                          lease_s: float = 60.0) -> bool:
        conn = None
        try:
            conn = self._connect()
            cur = conn.execute(
                "UPDATE platform_worker_slots SET expires_at=?,updated_at=? "
                "WHERE lease_id=? AND worker_id=? AND expires_at>?",
                (float(now) + float(lease_s), float(now), lease_id, worker_id, float(now)),
            )
            return cur.rowcount == 1
        except (sqlite3.Error, TypeError, ValueError):
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def release_worker_slot(self, lease_id: str, *, worker_id: str) -> bool:
        conn = None
        try:
            conn = self._connect()
            cur = conn.execute(
                "DELETE FROM platform_worker_slots WHERE lease_id=? AND worker_id=?",
                (lease_id, worker_id),
            )
            return cur.rowcount == 1
        except sqlite3.Error:
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def set_worker_owner_enabled(self, owner_id: str, enabled: bool, *, now: float) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        conn = None
        try:
            conn = self._connect()
            conn.execute(
                "INSERT INTO platform_worker_owners(owner_id, enabled, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(owner_id) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at",
                (owner_id, int(bool(enabled)), float(now)),
            )
            row = conn.execute("SELECT owner_id,enabled,last_claim_at,lease_expires_at,updated_at FROM platform_worker_owners WHERE owner_id=?", (owner_id,)).fetchone()
            return {"owner_id": row["owner_id"], "enabled": bool(row["enabled"]),
                    "last_claim_at": row["last_claim_at"],
                    "lease_expires_at": row["lease_expires_at"],
                    "updated_at": row["updated_at"]}
        except (sqlite3.Error, TypeError, ValueError):
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def renew_run_lease(self, owner_id: str, run_id: str, *, lease_id: str, worker_id: str, now: float, lease_s: float = 60.0) -> bool:
        owner_id = validate_owner_id(owner_id)
        run_id = validate_id(run_id, "run_id")
        conn = None
        try:
            conn = self._connect()
            cur = conn.execute(
                "UPDATE runs SET lease_expires_at=?, updated_at=? WHERE owner_id=? AND run_id=?"
                " AND lease_id=? AND lease_owner=? AND lease_expires_at>?",
                (float(now) + float(lease_s), float(now), owner_id, run_id, lease_id, worker_id, float(now)),
            )
            return cur.rowcount == 1
        except (sqlite3.Error, TypeError, ValueError):
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def requeue_claimed_run(self, owner_id: str, run_id: str, *, lease_id: str,
                            worker_id: str, now: float) -> bool:
        """Return a freshly claimed Run to queued without changing its attempt."""
        owner_id = validate_owner_id(owner_id)
        run_id = validate_id(run_id, "run_id")
        conn = None
        try:
            conn = self._connect()
            cur = conn.execute(
                "UPDATE runs SET state='queued', lease_id=NULL, lease_owner=NULL, "
                "lease_expires_at=NULL, attempt=CASE WHEN attempt>0 THEN attempt-1 ELSE 0 END, "
                "updated_at=? WHERE owner_id=? AND run_id=? "
                "AND state='running' AND lease_id=? AND lease_owner=? AND lease_expires_at>?",
                (float(now), owner_id, run_id, lease_id, worker_id, float(now)),
            )
            return cur.rowcount == 1
        except (sqlite3.Error, TypeError, ValueError):
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def run_cancel_requested(self, owner_id: str, run_id: str, *, lease_id: str | None = None) -> bool:
        owner_id = validate_owner_id(owner_id)
        run_id = validate_id(run_id, "run_id")
        conn = None
        try:
            conn = self._connect()
            query = "SELECT cancel_requested, lease_id FROM runs WHERE owner_id=? AND run_id=?"
            row = conn.execute(query, (owner_id, run_id)).fetchone()
            if not row:
                raise PlatformRepositoryError("run_not_found")
            if lease_id is not None and row["lease_id"] != lease_id:
                raise PlatformRepositoryError("lease_mismatch")
            return bool(row["cancel_requested"])
        except PlatformRepositoryError:
            raise
        except (sqlite3.Error, TypeError, ValueError):
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def cancel_run(self, owner_id: str, run_id: str, *, now: float) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        run_id = validate_id(run_id, "run_id")
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM runs WHERE owner_id=? AND run_id=?", (owner_id, run_id)).fetchone()
            if not row:
                conn.execute("ROLLBACK")
                raise PlatformRepositoryError("run_not_found")
            if row["state"] not in {"succeeded", "failed", "unknown", "cancelled"}:
                conn.execute("UPDATE runs SET cancel_requested=1,state='cancelling',updated_at=? WHERE owner_id=? AND run_id=?", (float(now), owner_id, run_id))
                row = conn.execute("SELECT * FROM runs WHERE owner_id=? AND run_id=?", (owner_id, run_id)).fetchone()
            conn.execute("COMMIT")
            return self._row_run(row)
        except PlatformRepositoryError:
            raise
        except sqlite3.Error:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def append_run_event(self, owner_id: str, run_id: str, kind: str, payload: Mapping[str, Any] | None, *, now: float, lease_id: str | None = None, worker_id: str | None = None) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        run_id = validate_id(run_id, "run_id")
        if not isinstance(kind, str) or not kind or len(kind) > 64:
            raise PlatformRepositoryError("invalid_event")
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute("SELECT run_id,lease_id,lease_owner,lease_expires_at FROM runs WHERE owner_id=? AND run_id=?", (owner_id, run_id)).fetchone()
            if not run:
                conn.execute("ROLLBACK")
                raise PlatformRepositoryError("run_not_found")
            if lease_id is not None:
                if (run["lease_id"] != lease_id or run["lease_owner"] != worker_id
                        or run["lease_expires_at"] is None or float(run["lease_expires_at"]) <= float(now)):
                    conn.execute("ROLLBACK")
                    raise PlatformRepositoryError("lease_mismatch")
            row = conn.execute("SELECT COALESCE(MAX(sequence),0) + 1 AS next_sequence FROM run_events WHERE run_id=?", (run_id,)).fetchone()
            sequence = int(row["next_sequence"])
            encoded = _json(payload or {})
            conn.execute("INSERT INTO run_events(run_id,sequence,kind,payload,created_at) VALUES(?,?,?,?,?)", (run_id, sequence, kind, encoded, float(now)))
            conn.execute("UPDATE runs SET updated_at=? WHERE owner_id=? AND run_id=?", (float(now), owner_id, run_id))
            conn.execute("COMMIT")
            return {"run_id": run_id, "sequence": sequence, "kind": kind, "payload": dict(payload or {}), "created_at": float(now)}
        except PlatformRepositoryError:
            raise
        except (sqlite3.Error, TypeError, ValueError):
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def finish_run(self, owner_id: str, run_id: str, *, lease_id: str, worker_id: str,
                   state: str, now: float, result_text: str = "", usage: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Fence terminal writeback to the current, unexpired worker lease."""
        owner_id = validate_owner_id(owner_id)
        run_id = validate_id(run_id, "run_id")
        if state not in {"succeeded", "failed", "unknown", "cancelled"}:
            raise PlatformRepositoryError("invalid_run_state")
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM runs WHERE owner_id=? AND run_id=? AND lease_id=?"
                " AND lease_owner=? AND lease_expires_at>?",
                (owner_id, run_id, lease_id, worker_id, float(now)),
            ).fetchone()
            if not row:
                conn.execute("ROLLBACK")
                raise PlatformRepositoryError("lease_mismatch")
            if bool(row["cancel_requested"]) and state != "unknown":
                state = "cancelled"
            if usage is None:
                usage_values = tuple(int(row[f"usage_{key}"] or 0) for key in (
                    "input_tokens", "output_tokens", "total_tokens", "provider_requests"))
            else:
                usage_values = tuple(max(0, int(usage.get(key, 0) or 0)) for key in (
                    "input_tokens", "output_tokens", "total_tokens", "provider_requests"))
            conn.execute(
                "UPDATE runs SET state=?,result_text=?,usage_input_tokens=?,usage_output_tokens=?,usage_total_tokens=?,usage_provider_requests=?,lease_id=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=?"
                " WHERE owner_id=? AND run_id=? AND lease_id=? AND lease_owner=?",
                (state, str(result_text or "")[:32768], *usage_values, float(now), owner_id, run_id, lease_id, worker_id),
            )
            row = conn.execute("SELECT * FROM runs WHERE owner_id=? AND run_id=?", (owner_id, run_id)).fetchone()
            conn.execute("COMMIT")
            result = self._row_run(row)
            if result_text:
                result["result_text"] = str(result_text)[:32768]
            return result
        except PlatformRepositoryError:
            raise
        except sqlite3.Error:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def list_run_events(self, owner_id: str, run_id: str, *, after: int = 0, limit: int = 100) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        run_id = validate_id(run_id, "run_id")
        try:
            after = max(0, int(after))
            limit = max(1, min(int(limit), 200))
        except (TypeError, ValueError):
            raise PlatformRepositoryError("invalid_cursor") from None
        conn = None
        try:
            conn = self._connect()
            run = conn.execute("SELECT run_id FROM runs WHERE owner_id=? AND run_id=?", (owner_id, run_id)).fetchone()
            if not run:
                raise PlatformRepositoryError("run_not_found")
            rows = conn.execute("SELECT run_id,sequence,kind,payload,created_at FROM run_events WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT ?", (run_id, after, limit)).fetchall()
            events = []
            for row in rows:
                events.append({"run_id": row["run_id"], "sequence": int(row["sequence"]), "kind": row["kind"], "payload": _decode(row["payload"]), "created_at": row["created_at"]})
            cursor = events[-1]["sequence"] if events else after
            return {"ok": True, "events": events, "next_cursor": cursor}
        except PlatformRepositoryError:
            raise
        except sqlite3.Error:
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def set_run_state(self, owner_id: str, run_id: str, state: str, *, now: float,
                      result_text: str = "", usage: Mapping[str, Any] | None = None) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        run_id = validate_id(run_id, "run_id")
        if state not in {"queued", "running", "waiting_node", "waiting_approval", "waiting_task", "paused", "cancelling", "succeeded", "failed", "unknown", "cancelled"}:
            raise PlatformRepositoryError("invalid_run_state")
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM runs WHERE owner_id=? AND run_id=?", (owner_id, run_id)).fetchone()
            if not row:
                conn.execute("ROLLBACK")
                raise PlatformRepositoryError("run_not_found")
            if usage is None:
                usage_values = tuple(int(row[f"usage_{key}"] or 0) for key in (
                    "input_tokens", "output_tokens", "total_tokens", "provider_requests"))
            else:
                usage_values = tuple(max(0, int(usage.get(key, 0) or 0)) for key in (
                    "input_tokens", "output_tokens", "total_tokens", "provider_requests"))
            conn.execute("UPDATE runs SET state=?,result_text=?,usage_input_tokens=?,usage_output_tokens=?,usage_total_tokens=?,usage_provider_requests=?,updated_at=? WHERE owner_id=? AND run_id=?", (state, str(result_text or "")[:32768], *usage_values, float(now), owner_id, run_id))
            conn.execute("COMMIT")
            result = self.get_run(owner_id, run_id)
            if result is not None and result_text:
                result["result_text"] = result_text[:32768]
            return result or {}
        except PlatformRepositoryError:
            raise
        except sqlite3.Error:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()


__all__ = ["PlatformRepository", "PlatformRepositoryError", "SCHEMA_VERSION"]
