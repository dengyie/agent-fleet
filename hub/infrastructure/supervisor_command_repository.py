"""Durable supervisor command outbox.

The supervisor queue is a delivery boundary, so command envelopes must survive
Hub restarts. This repository stores bounded command metadata and the signed
wire envelope; progress receipts move monotonically and the first terminal
receipt wins. Delivery uses a short lease so an interrupted poll can retry.
"""
from __future__ import annotations

import json
import math
import os
import secrets
import sqlite3
from pathlib import Path
from typing import Any, Mapping
from hub.infrastructure.sqlite import connect as sqlite_connect

_SESSION_MESSAGE_SCHEMA_VERSION = 2


class SupervisorCommandRepositoryError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class SupervisorCommandRepository:
    """SQLite-backed command outbox with atomic claim and receipt updates."""

    _AUDIT_CAP = 5000

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    @staticmethod
    def _private_directory(path: Path) -> None:
        try:
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise SupervisorCommandRepositoryError("command_private_store")
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            info = path.stat()
            if path.is_symlink() or info.st_uid != os.getuid():
                raise SupervisorCommandRepositoryError("command_private_store")
            os.chmod(path, 0o700)
            if path.stat().st_mode & 0o077:
                raise SupervisorCommandRepositoryError("command_private_store")
        except SupervisorCommandRepositoryError:
            raise
        except (OSError, ValueError) as exc:
            raise SupervisorCommandRepositoryError("command_private_store") from exc

    def _private_database(self) -> None:
        self._private_directory(self.db_path.parent)
        try:
            if self.db_path.is_symlink():
                raise SupervisorCommandRepositoryError("command_private_store")
            if not self.db_path.exists():
                fd = os.open(self.db_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
            info = self.db_path.stat()
            if not self.db_path.is_file() or info.st_uid != os.getuid():
                raise SupervisorCommandRepositoryError("command_private_store")
            os.chmod(self.db_path, 0o600)
            if self.db_path.stat().st_mode & 0o077:
                raise SupervisorCommandRepositoryError("command_private_store")
        except SupervisorCommandRepositoryError:
            raise
        except (OSError, ValueError) as exc:
            raise SupervisorCommandRepositoryError("command_private_store") from exc

    def _check_journal_permissions(self) -> None:
        for suffix in ("-wal", "-shm"):
            path = Path(str(self.db_path) + suffix)
            if not path.exists():
                continue
            try:
                info = path.stat()
                if path.is_symlink() or not path.is_file() or info.st_uid != os.getuid():
                    raise SupervisorCommandRepositoryError("command_private_store")
                os.chmod(path, 0o600)
                if path.stat().st_mode & 0o077:
                    raise SupervisorCommandRepositoryError("command_private_store")
            except SupervisorCommandRepositoryError:
                raise
            except (OSError, ValueError) as exc:
                raise SupervisorCommandRepositoryError("command_private_store") from exc

    def _connect(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = None
        try:
            self._private_database()
            conn = sqlite_connect(self.db_path, timeout=10)
            self._check_journal_permissions()
            return conn
        except SupervisorCommandRepositoryError:
            if conn is not None:
                conn.close()
            raise
        except (sqlite3.Error, OSError, ValueError) as exc:
            if conn is not None:
                conn.close()
            raise SupervisorCommandRepositoryError("command_unavailable") from exc

    def init(self) -> None:
        self._private_database()
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
                CREATE INDEX IF NOT EXISTS idx_supervisor_session_history
                    ON supervisor_commands(session_id);
                CREATE INDEX IF NOT EXISTS idx_supervisor_expiry
                    ON supervisor_commands(expires_s, command_id)
                    WHERE status NOT IN ('succeeded','already_finished','failed','rejected','expired');
                CREATE TABLE IF NOT EXISTS supervisor_audit_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_json TEXT NOT NULL
                );
                """
            )
            conn.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in conn.execute(
                "PRAGMA table_info(supervisor_commands)")}
            if "session_projection_pending" not in columns:
                conn.execute("ALTER TABLE supervisor_commands ADD COLUMN"
                             " session_projection_pending INTEGER NOT NULL DEFAULT 0")
                # Backfill confirmed exits once, including sessions left active
                # by older releases. No command is delivered or executed again.
                conn.execute("UPDATE supervisor_commands SET session_projection_pending=1"
                             " WHERE action IN ('terminate_session','cancel_attempt') AND ("
                             " (receipt_status='succeeded' AND receipt_reason IN"
                             " ('terminated','terminated_forced')) OR"
                             " (receipt_status='already_finished' AND receipt_reason IN"
                             " ('already_finished','no_live_process')))")
            if "session_projection_retry_s" not in columns:
                conn.execute("ALTER TABLE supervisor_commands ADD COLUMN"
                             " session_projection_retry_s REAL NOT NULL DEFAULT 0")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_supervisor_session_projection"
                         " ON supervisor_commands(machine_id,session_projection_retry_s,"
                         "issued_s,command_id)"
                         " WHERE session_projection_pending=1")
            self._migrate_session_message_operations(conn)
            self._migrate_dispatch_evidence(conn)
            conn.commit()
        except (sqlite3.Error, OSError) as exc:
            if conn.in_transaction:
                conn.rollback()
            raise SupervisorCommandRepositoryError("command_unavailable") from exc
        finally:
            conn.close()

    @staticmethod
    def _migrate_dispatch_evidence(conn: sqlite3.Connection) -> None:
        """Retain first poll dispatch independently of mutable leases/receipts."""
        stored = conn.execute("SELECT value FROM supervisor_meta WHERE key='dispatch_schema'").fetchone()
        if stored is not None and stored['value'] != '1':
            raise sqlite3.DatabaseError('unsupported dispatch schema')
        columns = {row['name']: row for row in conn.execute('PRAGMA table_info(supervisor_commands)')}
        if stored is not None and 'dispatched_s' not in columns:
            raise sqlite3.DatabaseError('missing dispatch schema')
        if 'dispatched_s' not in columns:
            conn.execute('ALTER TABLE supervisor_commands ADD COLUMN dispatched_s REAL NOT NULL DEFAULT 0')
        else:
            info = columns['dispatched_s']
            default = str(info['dflt_value']).strip().strip('()')
            if (info['type'].upper() != 'REAL' or info['notnull'] != 1
                    or info['pk'] != 0 or default not in {'0', '0.0'}):
                raise sqlite3.DatabaseError('invalid dispatch schema')
        if stored is None:
            # A currently delivered command or confirmed node receipt is
            # evidence. An old expired queued command has no such proof.
            conn.execute("UPDATE supervisor_commands SET dispatched_s=MAX(issued_s,0.000001)"
                         " WHERE dispatched_s=0 AND (status='delivered' OR receipt_status IN"
                         " ('accepted','executing','succeeded','failed','already_finished'))")
            conn.execute("INSERT INTO supervisor_meta(key,value) VALUES('dispatch_schema','1')")
        if conn.execute("SELECT 1 FROM supervisor_commands WHERE "
                        "typeof(dispatched_s) NOT IN ('integer','real') "
                        "OR dispatched_s<0 OR dispatched_s>9007199254740992 LIMIT 1").fetchone():
            raise sqlite3.DatabaseError('invalid dispatch value')

    @staticmethod
    def _create_session_message_operations(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE session_message_operations ("
                     "token_digest TEXT NOT NULL PRIMARY KEY CHECK(length(token_digest)=64),"
                     "operator_id TEXT NOT NULL,session_id TEXT NOT NULL,"
                     "operation TEXT NOT NULL CHECK(operation='append_user_turn'),"
                     "content_digest TEXT NOT NULL CHECK(length(content_digest)=64),"
                     "command_id TEXT NOT NULL UNIQUE REFERENCES supervisor_commands(command_id))")

    @staticmethod
    def _validate_session_message_operations(
            conn: sqlite3.Connection, *, token_digest_not_null: bool) -> None:
        """Validate declarations, indexes, foreign keys, and SQL CHECK behavior."""
        columns = {row["name"]: row for row in conn.execute(
            "PRAGMA table_info(session_message_operations)")}
        expected = {"token_digest", "operator_id", "session_id", "operation",
                    "content_digest", "command_id"}
        if set(columns) != expected:
            raise sqlite3.DatabaseError("invalid session message operation columns")
        for name, column in columns.items():
            expected_not_null = int(name != "token_digest" or token_digest_not_null)
            expected_pk = int(name == "token_digest")
            if (column["type"].upper() != "TEXT"
                    or column["notnull"] != expected_not_null
                    or column["pk"] != expected_pk
                    or column["dflt_value"] is not None):
                raise sqlite3.DatabaseError("invalid session message operation column")

        unique_command_id = False
        for index in conn.execute("PRAGMA index_list(session_message_operations)"):
            if not index["unique"] or index["partial"]:
                continue
            indexed_columns = [row["name"] for row in conn.execute(
                f"PRAGMA index_info('{index['name']}')")]
            if indexed_columns == ["command_id"]:
                unique_command_id = True
                break
        if not unique_command_id:
            raise sqlite3.DatabaseError("invalid session message operation indexes")

        foreign_keys = [dict(row) for row in conn.execute(
            "PRAGMA foreign_key_list(session_message_operations)")]
        if (len(foreign_keys) != 1
                or foreign_keys[0]["table"] != "supervisor_commands"
                or foreign_keys[0]["from"] != "command_id"
                or foreign_keys[0]["to"] != "command_id"
                or foreign_keys[0]["on_update"].upper() != "NO ACTION"
                or foreign_keys[0]["on_delete"].upper() != "NO ACTION"
                or foreign_keys[0]["match"].upper() != "NONE"):
            raise sqlite3.DatabaseError("invalid session message operation foreign key")

        # SQLite CHECK expressions accept NULL, so a schema-text comparison
        # would miss the nullable TEXT PRIMARY KEY defect. Exercise each
        # invariant inside savepoints and roll every probe back before startup.
        savepoint = "session_message_schema_probe"
        probe_command = "schema-probe-" + secrets.token_hex(12)
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            conn.execute(
                "INSERT INTO supervisor_commands(command_id,machine_id,session_id,attempt_id,"
                "action,nonce,idempotency_key,wire_json,issued_s,expires_s,status) "
                "VALUES(?, 'schema-probe', 'schema-probe', NULL, 'pause_session', ?, NULL,"
                "'{}', 0, 1, 'queued')",
                (probe_command, secrets.token_hex(16)))

            def rejected(values: tuple[object, ...]) -> bool:
                nested = "session_message_constraint_probe"
                conn.execute(f"SAVEPOINT {nested}")
                try:
                    conn.execute(
                        "INSERT INTO session_message_operations(token_digest,operator_id,"
                        "session_id,operation,content_digest,command_id) VALUES(?,?,?,?,?,?)",
                        values)
                except sqlite3.IntegrityError:
                    return True
                finally:
                    conn.execute(f"ROLLBACK TO {nested}")
                    conn.execute(f"RELEASE {nested}")
                return False

            valid_row = (secrets.token_hex(32), "schema-probe", "schema-probe",
                         "append_user_turn", secrets.token_hex(32), probe_command)
            nested = "session_message_valid_constraint_probe"
            conn.execute(f"SAVEPOINT {nested}")
            try:
                conn.execute(
                    "INSERT INTO session_message_operations(token_digest,operator_id,"
                    "session_id,operation,content_digest,command_id) VALUES(?,?,?,?,?,?)",
                    valid_row)
            except sqlite3.IntegrityError as exc:
                raise sqlite3.DatabaseError(
                    "session message operation schema rejects valid row") from exc
            finally:
                conn.execute(f"ROLLBACK TO {nested}")
                conn.execute(f"RELEASE {nested}")

            invalid_rows = (
                ("short", "schema-probe", "schema-probe", "append_user_turn",
                 secrets.token_hex(32), probe_command),
                (secrets.token_hex(32), "schema-probe", "schema-probe", "invalid_operation",
                 secrets.token_hex(32), probe_command),
                (secrets.token_hex(32), "schema-probe", "schema-probe", "append_user_turn",
                 "short", probe_command),
            )
            if not all(rejected(row) for row in invalid_rows):
                raise sqlite3.DatabaseError("invalid session message operation checks")
            null_key_rejected = rejected((None, "schema-probe", "schema-probe",
                                          "append_user_turn", secrets.token_hex(32),
                                          probe_command))
            if null_key_rejected != token_digest_not_null:
                raise sqlite3.DatabaseError("invalid session message token nullability")
        finally:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")

    @classmethod
    def _migrate_session_message_operations(cls, conn: sqlite3.Connection) -> None:
        """Atomically upgrade ordinary-message operation bindings to v2."""
        tables = {row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        version = 0
        if "supervisor_meta" in tables:
            meta_columns = {row["name"] for row in conn.execute(
                "PRAGMA table_info(supervisor_meta)")}
            if meta_columns != {"key", "value"}:
                raise sqlite3.DatabaseError("invalid supervisor metadata schema")
            row = conn.execute(
                "SELECT value FROM supervisor_meta WHERE key='schema_version'").fetchone()
            if row is not None:
                raw_version = row["value"]
                if (not isinstance(raw_version, str) or len(raw_version) > 10
                        or not raw_version.isascii()
                        or not raw_version.isdecimal()):
                    raise sqlite3.DatabaseError("invalid supervisor schema version")
                version = int(raw_version)
                if version > _SESSION_MESSAGE_SCHEMA_VERSION:
                    raise sqlite3.DatabaseError("unsupported supervisor schema version")

        operation_table_exists = "session_message_operations" in tables
        if version == _SESSION_MESSAGE_SCHEMA_VERSION and not operation_table_exists:
            raise sqlite3.DatabaseError("missing session message operation table")
        if version == 1 and not operation_table_exists:
            raise sqlite3.DatabaseError("missing legacy session message operation table")

        conn.execute("CREATE TABLE IF NOT EXISTS supervisor_meta ("
                     "key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        if not operation_table_exists:
            cls._create_session_message_operations(conn)

        columns = {row["name"]: row for row in conn.execute(
            "PRAGMA table_info(session_message_operations)")}
        migrate_v1 = version == 1
        if version == 0 and operation_table_exists:
            # A pre-versioned database from the first implementation is v1;
            # a stricter table is already compatible with v2.
            token_column = columns.get("token_digest")
            if token_column is None:
                raise sqlite3.DatabaseError("missing session message token column")
            migrate_v1 = token_column["notnull"] == 0

        if migrate_v1:
            # Version 1 used a nullable TEXT PRIMARY KEY. Its other declarations
            # and constraints must match before any rows are copied.
            cls._validate_session_message_operations(conn, token_digest_not_null=False)
            conn.execute("ALTER TABLE session_message_operations "
                         "RENAME TO session_message_operations_v1")
            cls._create_session_message_operations(conn)
            conn.execute(
                "INSERT INTO session_message_operations(token_digest,operator_id,session_id,"
                "operation,content_digest,command_id) SELECT token_digest,operator_id,session_id,"
                "operation,content_digest,command_id FROM session_message_operations_v1")
            conn.execute("DROP TABLE session_message_operations_v1")

        cls._validate_session_message_operations(conn, token_digest_not_null=True)
        conn.execute(
            "INSERT INTO supervisor_meta(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(_SESSION_MESSAGE_SCHEMA_VERSION),))

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

    def session_logs(self, session_id: str, *, limit: int = 20, before: int = 0) -> dict:
        """Page durable commands without exposing signed envelopes or payloads.

        rowid is an ingestion cursor, independent of clocks and command IDs.
        Audit storage already retains at most 5000 records across the fleet.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                'SELECT rowid AS cursor, * FROM supervisor_commands WHERE session_id=? '
                'AND (?=0 OR rowid<?) ORDER BY rowid DESC LIMIT ?',
                (session_id, before, before, limit + 1)).fetchall()
            selected = rows[:limit]
            ids = [row['command_id'] for row in selected]
            audit = []
            if ids:
                placeholders = ','.join('?' for _ in ids)
                audit_rows = conn.execute(
                    'SELECT event_json FROM supervisor_audit_events '
                    f"WHERE json_extract(event_json, '$.command_id') IN ({placeholders}) "
                    'ORDER BY event_id DESC LIMIT 201', ids).fetchall()
                audit = [json.loads(row['event_json']) for row in audit_rows]
            return {'commands': [self._decode(row) for row in selected],
                    'has_more': len(rows) > limit,
                    'next_cursor': str(selected[-1]['cursor']) if len(rows) > limit else None,
                    'audit': audit[:200], 'audit_truncated': len(audit) > 200}
        except (sqlite3.Error, OSError, ValueError) as exc:
            raise SupervisorCommandRepositoryError('command_unavailable') from exc
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
        dispatched = row['dispatched_s']
        if (type(dispatched) not in (int, float) or not 0 <= dispatched <= 2**53
                or not math.isfinite(dispatched)):
            raise SupervisorCommandRepositoryError('command_corrupt')
        return {
            "command_id": row["command_id"], "machine": row["machine_id"],
            "session_id": row["session_id"], "attempt_id": row["attempt_id"],
            "action": row["action"], "nonce": row["nonce"], "wire": wire,
            "issued_s": float(row["issued_s"]), "expires_s": float(row["expires_s"]),
            "status": row["status"], "lease_until_s": float(row["lease_until_s"]),
            "dispatched_s": float(dispatched),
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

    def find_session_message(self, token_digest: str) -> dict[str, Any] | None:
        """Read a message binding and its current command state, never its body."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT o.token_digest,o.operator_id,o.session_id,o.operation,"
                "o.content_digest,o.command_id,c.action AS command_action,"
                "c.session_id AS command_session_id,c.status,c.receipt_status "
                "FROM session_message_operations o LEFT JOIN supervisor_commands c "
                "ON c.command_id=o.command_id WHERE o.token_digest=?",
                (token_digest,),
            ).fetchone()
            if row is None:
                return None
            if row["status"] is None:
                raise SupervisorCommandRepositoryError("command_corrupt")
            if (row["command_action"] != row["operation"]
                    or row["command_session_id"] != row["session_id"]):
                raise SupervisorCommandRepositoryError("command_corrupt")
            return {key: row[key] for key in row.keys()}
        except SupervisorCommandRepositoryError:
            raise
        except (sqlite3.Error, OSError) as exc:
            raise SupervisorCommandRepositoryError("command_unavailable") from exc
        finally:
            conn.close()

    def nonce_exists(self, nonce: str, *, connection=None) -> bool:
        conn = connection if connection is not None else self._connect()
        try:
            try:
                return conn.execute(
                    "SELECT 1 FROM supervisor_commands WHERE nonce=?", (nonce,)
                ).fetchone() is not None
            except (sqlite3.Error, OSError) as exc:
                raise SupervisorCommandRepositoryError("command_unavailable") from exc
        finally:
            if connection is None:
                conn.close()

    def target_owner(self, session_id: str, attempt_id: str | None, *, connection=None) -> dict[str, Any] | None:
        conn = connection if connection is not None else self._connect()
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
            if connection is None:
                conn.close()

    def insert(self, *, command_id: str, machine: str, session_id: str,
               attempt_id: str | None, action: str, nonce: str,
               idempotency_key: str | None, wire: Mapping[str, Any],
               issued_s: float, expires_s: float,
               audit_row: Mapping[str, Any] | None = None,
               message_binding: Mapping[str, str] | None = None,
               connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        # Provider management shares its operation and command commit. A foreign
        # or autocommit connection can never smuggle a partially persisted job.
        if connection is not None:
            database = connection.execute('PRAGMA database_list').fetchone()[2]
            if not connection.in_transaction or Path(database).resolve() != self.db_path.resolve():
                raise SupervisorCommandRepositoryError('command_transaction_required')
        conn = connection if connection is not None else self._connect()
        def commit():
            if connection is None:
                conn.commit()
            else:
                conn.execute('RELEASE provider_account_command')
        def rollback():
            if connection is None:
                conn.rollback()
            else:
                conn.execute('ROLLBACK TO provider_account_command')
                conn.execute('RELEASE provider_account_command')
        binding = None
        try:
            conn.execute("BEGIN IMMEDIATE" if connection is None else "SAVEPOINT provider_account_command")
            if message_binding is not None:
                binding = self._validate_message_binding(message_binding, action, idempotency_key)
                existing_operation = conn.execute(
                    "SELECT o.operator_id,o.session_id,o.operation,o.content_digest,c.* "
                    "FROM session_message_operations o JOIN supervisor_commands c "
                    "ON c.command_id=o.command_id WHERE o.token_digest=?",
                    (binding["token_digest"],)).fetchone()
                if existing_operation is not None:
                    if self._same_message_binding(existing_operation, binding):
                        commit()
                        return self._decode(existing_operation)  # type: ignore[return-value]
                    rollback()
                    raise SupervisorCommandRepositoryError("idempotency_conflict")
            if idempotency_key:
                existing = conn.execute(
                    "SELECT * FROM supervisor_commands WHERE idempotency_key=?",
                    (idempotency_key,)).fetchone()
                if existing is not None:
                    if message_binding is not None:
                        rollback()
                        raise SupervisorCommandRepositoryError("idempotency_conflict")
                    commit()
                    return self._decode(existing)  # type: ignore[return-value]
            target = conn.execute(
                "SELECT action FROM supervisor_commands WHERE session_id=? AND "
                "((attempt_id IS NULL AND ? IS NULL) OR attempt_id=?) AND "
                "status IN ('queued','delivered') ORDER BY issued_s DESC LIMIT 1",
                (session_id, attempt_id, attempt_id),
            ).fetchone()
            if (target and target["action"] in ("terminate_session", "quarantine_session")
                    and action in ("resume_session", "append_user_turn", "configure_model", "answer_native_request")):
                rollback()
                raise SupervisorCommandRepositoryError("resume_superseded")
            superseded_audit_rows = []
            if action in ("terminate_session", "quarantine_session"):
                if audit_row is not None:
                    superseded_audit_rows = conn.execute(
                        "SELECT command_id, action FROM supervisor_commands WHERE session_id=? AND "
                        "((attempt_id IS NULL AND ? IS NULL) OR attempt_id=?) AND "
                        "action IN ('resume_session','append_user_turn','configure_model','answer_native_request') "
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
                    "action IN ('resume_session','append_user_turn','configure_model','answer_native_request') "
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
            if message_binding is not None:
                conn.execute(
                    "INSERT INTO session_message_operations(token_digest,operator_id,session_id,"
                    "operation,content_digest,command_id) VALUES(?,?,?,?,?,?)",
                    (binding["token_digest"], binding["operator_id"], binding["session_id"],
                     binding["operation"], binding["content_digest"], command_id))
            if audit_row is not None:
                audit_rows = [
                    {**dict(audit_row), "event": "supersede",
                     "command_id": old["command_id"], "action": old["action"],
                     "by": action}
                    for old in superseded_audit_rows
                ]
                audit_rows.append(dict(audit_row))
                self._append_audit_rows(conn, audit_rows)
            commit()
            return self._decode(row)  # type: ignore[return-value]
        except sqlite3.IntegrityError as exc:
            rollback()
            if message_binding is not None:
                existing_operation = conn.execute(
                    "SELECT o.operator_id,o.session_id,o.operation,o.content_digest,c.* "
                    "FROM session_message_operations o JOIN supervisor_commands c "
                    "ON c.command_id=o.command_id WHERE o.token_digest=?",
                    (binding["token_digest"],)).fetchone()
                if existing_operation is not None:
                    if self._same_message_binding(existing_operation, binding):
                        return self._decode(existing_operation)  # type: ignore[return-value]
                    raise SupervisorCommandRepositoryError("idempotency_conflict") from exc
                existing_command = conn.execute(
                    "SELECT 1 FROM supervisor_commands WHERE idempotency_key=?",
                    (idempotency_key,)).fetchone()
                if existing_command is not None:
                    raise SupervisorCommandRepositoryError("idempotency_conflict") from exc
                raise SupervisorCommandRepositoryError("command_unavailable") from exc
            if idempotency_key:
                existing = conn.execute(
                    "SELECT * FROM supervisor_commands WHERE idempotency_key=?",
                    (idempotency_key,)).fetchone()
                if existing is not None:
                    return self._decode(existing)  # type: ignore[return-value]
            raise SupervisorCommandRepositoryError("command_conflict") from exc
        except (sqlite3.Error, OSError) as exc:
            rollback()
            raise SupervisorCommandRepositoryError("command_unavailable") from exc
        finally:
            if connection is None:
                conn.close()

    @staticmethod
    def _validate_message_binding(binding, action, idempotency_key):
        required = {"token_digest", "operator_id", "session_id", "operation", "content_digest"}
        if (not isinstance(binding, Mapping) or set(binding) != required
                or action != "append_user_turn" or not idempotency_key):
            raise SupervisorCommandRepositoryError("command_invalid")
        token_digest = binding.get("token_digest")
        content_digest = binding.get("content_digest")
        if (not isinstance(token_digest, str) or len(token_digest) != 64
                or any(char not in "0123456789abcdef" for char in token_digest)
                or not isinstance(content_digest, str) or len(content_digest) != 64
                or any(char not in "0123456789abcdef" for char in content_digest)):
            raise SupervisorCommandRepositoryError("command_invalid")
        for key, cap in (("operator_id", 128), ("session_id", 256)):
            value = binding.get(key)
            if not isinstance(value, str) or not value or len(value) > cap:
                raise SupervisorCommandRepositoryError("command_invalid")
        if binding.get("operation") != "append_user_turn":
            raise SupervisorCommandRepositoryError("command_invalid")
        return {key: str(binding[key]) for key in required}

    @staticmethod
    def _same_message_binding(row, binding):
        return (row["operator_id"], row["session_id"], row["operation"], row["content_digest"]) == (
            binding["operator_id"], binding["session_id"], binding["operation"], binding["content_digest"])

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
                    "UPDATE supervisor_commands SET status='delivered',lease_until_s=?,"
                    "dispatched_s=CASE WHEN dispatched_s=0 THEN MAX(?,0.000001) ELSE dispatched_s END "
                    "WHERE command_id=? AND status IN ('queued','delivered') AND lease_until_s<=?",
                    (float(lease_until_s), float(now_s), command_id, float(now_s)))
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
                             "receipt_reason=?, lease_until_s=0,"
                             "session_projection_pending=CASE WHEN"
                             " action IN ('terminate_session','cancel_attempt') AND ("
                             " (?='succeeded' AND ? IN ('terminated','terminated_forced')) OR"
                             " (?='already_finished' AND ? IN ('already_finished','no_live_process')))"
                             " THEN 1 ELSE 0 END,session_projection_retry_s=0"
                             " WHERE command_id=?",
                             (status, receipt_status, receipt_reason[:120],
                              receipt_status, receipt_reason, receipt_status, receipt_reason,
                              command_id))
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

    def pending_session_projections(self, machine: str,
                                    now_s: float) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT command_id,session_id,machine_id,action,receipt_status,receipt_reason"
                " FROM supervisor_commands WHERE machine_id=? AND session_projection_pending=1"
                " AND session_projection_retry_s<=?"
                " ORDER BY session_projection_retry_s,issued_s,command_id LIMIT 100",
                (machine, now_s)).fetchall()
            return [{"command_id": row["command_id"], "session_id": row["session_id"],
                     "machine_id": row["machine_id"], "action": row["action"],
                     "status": row["receipt_status"], "reason": row["receipt_reason"]}
                    for row in rows]
        except (sqlite3.Error, OSError) as exc:
            raise SupervisorCommandRepositoryError("command_unavailable") from exc
        finally:
            conn.close()

    def defer_session_projection(self, command_id: str,
                                 retry_at_s: float) -> None:
        conn = self._connect()
        try:
            conn.execute("UPDATE supervisor_commands SET session_projection_retry_s=?"
                         " WHERE command_id=? AND session_projection_pending=1",
                         (retry_at_s, command_id))
        except (sqlite3.Error, OSError) as exc:
            raise SupervisorCommandRepositoryError("command_unavailable") from exc
        finally:
            conn.close()

    def complete_session_projection(self, command_id: str) -> None:
        conn = self._connect()
        try:
            conn.execute("UPDATE supervisor_commands SET session_projection_pending=0,"
                         "session_projection_retry_s=0"
                         " WHERE command_id=?", (command_id,))
        except (sqlite3.Error, OSError) as exc:
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
