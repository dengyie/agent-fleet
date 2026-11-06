"""Durable SQLite store for the additive platform domain.

The platform store is intentionally separate from legacy task/session/adoption
databases.  It has one writer (the Hub), WAL enabled, bounded JSON fields, and
small repository methods used by the defaults API.
"""
from __future__ import annotations

import sqlite3
import threading
from collections import deque
from pathlib import Path

from hub.infrastructure.platform_db_base import (  # noqa: F401 - re-exported API
    MAX_JSON_BYTES, PlatformRepositoryError, _decode, _json, freeze_run_request_summary,
)
from hub.infrastructure.platform_db_catalog import PlatformCatalogMixin
from hub.infrastructure.platform_db_conversations import PlatformConversationMixin
from hub.infrastructure.platform_db_legacy_tasks import PlatformLegacyTaskMixin
from hub.infrastructure.platform_db_node_auth import (  # noqa: F401 - re-exported API
    NODE_AUTH_FAILURE_WINDOW_S, NODE_AUTH_LIMITER_MAX_KEYS, NODE_AUTH_MAX_FAILURES,
    NODE_LAST_USED_WRITE_INTERVAL_S, PlatformNodeAuthMixin,
)
from hub.infrastructure.platform_db_runs import PlatformRunMixin
from hub.infrastructure.platform_db_workers import PlatformWorkerMixin
from hub.infrastructure.sqlite import connect as sqlite_connect
from platform_schema import PlatformValidationError  # noqa: F401 - re-exported API

SCHEMA_VERSION = 3


class PlatformRepository(
    PlatformCatalogMixin,
    PlatformNodeAuthMixin,
    PlatformConversationMixin,
    PlatformLegacyTaskMixin,
    PlatformRunMixin,
    PlatformWorkerMixin,
):
    SCHEMA_VERSION = SCHEMA_VERSION

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.path = self.db_path
        # In-memory failed-auth limiter keyed by ("node", id) / ("ip", addr).
        self._auth_failures: dict[tuple[str, str], deque] = {}
        self._auth_failures_lock = threading.Lock()

    def _connect(self):
        try:
            return sqlite_connect(self.db_path, timeout=10)
        except sqlite3.Error:
            raise PlatformRepositoryError("platform_store") from None

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
                    revision INTEGER NOT NULL DEFAULT 0, archived_at REAL,
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
                ("conversations", "archived_at", "REAL"),
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
                ("node_credentials", "fast_digest", "BLOB"),
                # Frozen request projection of terminal runs (see
                # freeze_run_request_summary); NULL until the run terminates.
                ("runs", "request_summary", "TEXT"),
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
            conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_owner_conversation "
                         "ON runs(owner_id, conversation_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_owner_archive "
                         "ON conversations(owner_id, archived_at, conversation_id)")
            # claim_run seeks (owner_id, state) per claimable state; retention
            # selects terminal runs by (state, updated_at). Additive only.
            conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_owner_state_created "
                         "ON runs(owner_id, state, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_state_updated "
                         "ON runs(state, updated_at)")
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


__all__ = ["PlatformRepository", "PlatformRepositoryError", "SCHEMA_VERSION", "freeze_run_request_summary"]
