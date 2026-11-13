"""Durable platform command/outbox repository with lease recovery."""
from __future__ import annotations

import json
import math
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable
from contextlib import closing, contextmanager

from fleet_core.platform_command import RECEIPT_STATES, PlatformCommand, TERMINAL_COMMAND_STATES, args_hash
from fleet_core.command_timing import CommandTiming, MAX_COMMAND_LIFETIME_S
from hub.domain.capability import CapabilityStoreError
from hub.domain.capability_execution import CAPABILITY_ARGUMENT
from hub.infrastructure.sqlite import connect as sqlite_connect
from hub.infrastructure.platform_db_node_auth import node_credential_is_current


_TERMINAL_POSTCHECK_STATES = frozenset({'matched', 'mismatch', 'remains_unknown', 'failed'})
_POSTCHECK_COLUMNS = 'owner_id,command_id,kind,check_command_id,state,result,evidence_id,created_at,updated_at'
_POSTCHECK_STORAGE = {
    'workspace_digest': ('platform_command_postchecks', _POSTCHECK_COLUMNS + ',path,expected_sha256', 'workspace_check'),
    'service_inspect': ('platform_service_postchecks', _POSTCHECK_COLUMNS + ',service_id,service_version', 'service_check'),
    'service_logs': ('platform_service_postchecks', _POSTCHECK_COLUMNS + ',service_id,service_version', 'service_logs_check'),
}


class CommandRepositoryError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

    def __str__(self):
        return self.code


class CommandRepository:
    def __init__(self, db_path: Path, *, clock=time.time):
        self.db_path = Path(db_path)
        self.clock = clock
        self.artifact_receive_guard = None
        self.artifact_export_guard = None
        self.capability_guard: Callable[[sqlite3.Connection, dict[str, Any], float], None] | None = None

    def _connect(self):
        return sqlite_connect(self.db_path, timeout=10)

    def init(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = None
        try:
            conn = self._connect()
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS platform_commands (
              command_id TEXT PRIMARY KEY, owner_id TEXT, target_node TEXT NOT NULL, action TEXT NOT NULL,
              resource_id TEXT NOT NULL, arguments TEXT NOT NULL, retry_class TEXT NOT NULL,
              expires_at REAL NOT NULL, args_hash TEXT NOT NULL, run_id TEXT, grant_id TEXT,
              signature TEXT, status TEXT NOT NULL, lease_owner TEXT, lease_until REAL,
              attempt INTEGER NOT NULL DEFAULT 0, result TEXT, created_at REAL NOT NULL,
              updated_at REAL NOT NULL, idempotency_key TEXT UNIQUE
            );
            CREATE TABLE IF NOT EXISTS platform_command_outbox (
              outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
              command_id TEXT NOT NULL,
              event_type TEXT NOT NULL,
              payload TEXT NOT NULL,
              state TEXT NOT NULL DEFAULT 'pending',
              lease_owner TEXT,
              lease_until REAL,
              attempts INTEGER NOT NULL DEFAULT 0,
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL,
              UNIQUE(command_id, event_type)
            );
            CREATE TABLE IF NOT EXISTS platform_command_timing (
              command_id TEXT PRIMARY KEY REFERENCES platform_commands(command_id) ON DELETE CASCADE,
              queue_deadline REAL NOT NULL, execution_timeout_s REAL NOT NULL,
              receipt_timeout_s REAL NOT NULL, claimed_at REAL,
              execution_deadline REAL, receipt_deadline REAL
            );
            CREATE INDEX IF NOT EXISTS idx_platform_commands_poll ON platform_commands(target_node,status,expires_at);
            CREATE INDEX IF NOT EXISTS idx_platform_outbox_claim ON platform_command_outbox(state,lease_until,created_at);
            CREATE TABLE IF NOT EXISTS platform_command_reconciliations (
              reconciliation_id TEXT PRIMARY KEY, command_id TEXT NOT NULL,
              owner_id TEXT NOT NULL, actor TEXT NOT NULL, outcome TEXT NOT NULL,
              evidence_source TEXT NOT NULL, evidence_reference TEXT NOT NULL,
              created_at REAL NOT NULL,
              FOREIGN KEY(command_id) REFERENCES platform_commands(command_id)
            );
            CREATE INDEX IF NOT EXISTS idx_platform_reconcile_owner
              ON platform_command_reconciliations(owner_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS platform_command_postchecks (
              owner_id TEXT NOT NULL, command_id TEXT NOT NULL, kind TEXT NOT NULL,
              check_command_id TEXT NOT NULL UNIQUE, path TEXT NOT NULL,
              expected_sha256 TEXT NOT NULL, state TEXT NOT NULL, result TEXT,
              evidence_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
              PRIMARY KEY(owner_id, command_id, kind),
              FOREIGN KEY(command_id) REFERENCES platform_commands(command_id)
            );
            CREATE INDEX IF NOT EXISTS idx_platform_postcheck_owner
              ON platform_command_postchecks(owner_id, updated_at DESC);
            CREATE TABLE IF NOT EXISTS platform_service_postchecks (
              owner_id TEXT NOT NULL, command_id TEXT NOT NULL, kind TEXT NOT NULL,
              check_command_id TEXT NOT NULL UNIQUE, service_id TEXT NOT NULL,
              service_version INTEGER NOT NULL, state TEXT NOT NULL, result TEXT,
              evidence_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
              PRIMARY KEY(owner_id, command_id, kind),
              FOREIGN KEY(command_id) REFERENCES platform_commands(command_id)
            );
            CREATE INDEX IF NOT EXISTS idx_platform_service_postcheck_owner
              ON platform_service_postchecks(owner_id, updated_at DESC);
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(platform_commands)")}
            if "owner_id" not in columns:
                conn.execute("ALTER TABLE platform_commands ADD COLUMN owner_id TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_platform_commands_owner_run "
                "ON platform_commands(owner_id,run_id)"
            )
            # Retention deletes child rows by command_id (and FK checks on a
            # parent delete probe them); the PKs lead with owner_id/other keys.
            for table, index in (
                ("platform_command_reconciliations", "idx_platform_reconcile_command"),
                ("platform_command_postchecks", "idx_platform_postcheck_command"),
                ("platform_service_postchecks", "idx_platform_service_postcheck_command"),
            ):
                conn.execute(f"CREATE INDEX IF NOT EXISTS {index} ON {table}(command_id)")
            # Early M1 databases may contain commands written before the
            # transactional outbox existed. Backfill only the bounded routing
            # event; the command row remains the source of truth.
            conn.execute(
                "INSERT OR IGNORE INTO platform_command_outbox("
                "command_id,event_type,payload,state,created_at,updated_at) "
                "SELECT c.command_id, 'command_queued', ?, CASE WHEN c.status IN ('succeeded','failed','unknown','expired') THEN 'sent' ELSE 'pending' END, c.created_at, c.updated_at "
                "FROM platform_commands c "
                "WHERE NOT EXISTS (SELECT 1 FROM platform_command_outbox o "
                "WHERE o.command_id=c.command_id AND o.event_type='command_queued')",
                (json.dumps({"command_id": "__COMMAND_ID__", "target_node": "__TARGET_NODE__", "owner_id": "__OWNER_ID__"}, sort_keys=True, separators=(",", ":")),),
            )
            # Replace placeholders per row without exposing command arguments
            # or secrets in the outbox payload.
            for row in conn.execute("SELECT outbox_id, command_id FROM platform_command_outbox WHERE payload LIKE '%__COMMAND_ID__%'").fetchall():
                command = conn.execute("SELECT command_id,target_node,owner_id FROM platform_commands WHERE command_id=?", (row[1],)).fetchone()
                if command:
                    conn.execute("UPDATE platform_command_outbox SET payload=? WHERE outbox_id=?", (json.dumps(dict(command), sort_keys=True, separators=(",", ":")), row[0]))
        except (sqlite3.Error, OSError):
            raise CommandRepositoryError("command_store") from None
        finally:
            if conn is not None: conn.close()

    @staticmethod
    def _decode(row) -> dict[str, Any]:
        payload = json.loads(row["arguments"])
        result = dict(row)
        result["arguments"] = payload if isinstance(payload, dict) else {}
        result["result"] = json.loads(row["result"]) if row["result"] else None
        return result

    @contextmanager
    def _enqueue_connection(self, connection: sqlite3.Connection | None):
        if connection is None:
            with closing(self._connect()) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
            return
        database = connection.execute("PRAGMA database_list").fetchone()[2]
        if not connection.in_transaction or Path(database).resolve() != self.db_path.resolve():
            raise CommandRepositoryError("command_transaction_required")
        connection.execute("SAVEPOINT artifact_input_command")
        try:
            yield connection
            connection.execute("RELEASE artifact_input_command")
        except BaseException:
            connection.execute("ROLLBACK TO artifact_input_command")
            connection.execute("RELEASE artifact_input_command")
            raise

    def enqueue(self, command: PlatformCommand, *, idempotency_key: str | None = None,
                require_signature: bool = False,
                prepare: Callable[[sqlite3.Connection, PlatformCommand], PlatformCommand] | None = None,
                timing: CommandTiming | None = None,
                connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        """Persist command/outbox and any admission preparation in one transaction."""
        now = float(self.clock())
        if CAPABILITY_ARGUMENT in command.arguments:
            raise CommandRepositoryError("invalid_capability_command")
        if require_signature and prepare is None and not command.signature:
            raise CommandRepositoryError("command_signature_required")
        try:
            with self._enqueue_connection(connection) as conn:
                prior = conn.execute("SELECT * FROM platform_commands WHERE idempotency_key=? LIMIT 1", (idempotency_key,)).fetchone() if idempotency_key else None
                if prior is not None:
                    existing = self._decode(prior)
                    expected_digest = existing["args_hash"]
                    if CAPABILITY_ARGUMENT in existing["arguments"]:
                        expected_arguments = dict(existing["arguments"])
                        expected_arguments.pop(CAPABILITY_ARGUMENT)
                        if command.action == "tool.browser.submit":
                            expected_arguments.pop("approval_id", None)
                        expected_digest = args_hash(expected_arguments)
                    elif prepare is not None and command.action == "tool.browser.submit":
                        expected_arguments = dict(existing["arguments"])
                        expected_arguments.pop("approval_id", None)
                        expected_digest = args_hash(expected_arguments)
                    if (existing["owner_id"] != command.owner_id
                            or expected_digest != command.args_digest
                            or existing["target_node"] != command.target_node
                            or existing["action"] != command.action
                            or existing["resource_id"] != command.resource_id
                            or existing["retry_class"] != command.retry_class
                            or existing["run_id"] != command.run_id):
                        raise CommandRepositoryError("idempotency_conflict")
                    return existing
                if timing is not None and (type(timing) is not CommandTiming
                        or not math.isclose(timing.hard_deadline, command.expires_at, rel_tol=0, abs_tol=1e-6)
                        or timing.queue_deadline <= now
                        or command.expires_at - now > MAX_COMMAND_LIFETIME_S):
                    raise CommandRepositoryError("invalid_command_timing")
                from .capability_dispatch import reject_disabled_command
                if self.capability_guard is None:
                    reject_disabled_command(conn, command.as_dict())
                original_expiry = command.expires_at
                if prepare is not None:
                    command = prepare(conn, command)
                if timing is not None and command.expires_at < original_expiry:
                    timing = timing.bounded_deadline(now=now, deadline=command.expires_at)
                if self.capability_guard is not None:
                    self.capability_guard(conn, command.as_dict(), now)
                if timing is not None and (type(timing) is not CommandTiming
                        or not math.isclose(timing.hard_deadline, command.expires_at, rel_tol=0, abs_tol=1e-6)
                        or timing.queue_deadline <= now
                        or command.expires_at - now > MAX_COMMAND_LIFETIME_S):
                    raise CommandRepositoryError("invalid_command_timing")
                if require_signature and not command.signature:
                    raise CommandRepositoryError("command_signature_required")
                conn.execute("INSERT INTO platform_commands(command_id,owner_id,target_node,action,resource_id,arguments,retry_class,expires_at,args_hash,run_id,grant_id,signature,status,created_at,updated_at,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (command.command_id, command.owner_id, command.target_node, command.action, command.resource_id, json.dumps(command.arguments, sort_keys=True, separators=(",", ":")), command.retry_class, command.expires_at, command.args_digest, command.run_id, command.grant_id, command.signature, "queued", now, now, idempotency_key))
                if timing is not None:
                    conn.execute("INSERT INTO platform_command_timing(command_id,queue_deadline,execution_timeout_s,receipt_timeout_s) VALUES(?,?,?,?)",
                        (command.command_id, timing.queue_deadline, timing.execution_timeout_s, timing.receipt_timeout_s))
                payload = {"command_id": command.command_id, "target_node": command.target_node, "owner_id": command.owner_id}
                if command.action == "tool.browser.submit":
                    payload["approval_id"] = command.arguments["approval_id"]
                conn.execute(
                    "INSERT INTO platform_command_outbox(command_id,event_type,payload,state,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (command.command_id, "command_queued", json.dumps(payload, sort_keys=True, separators=(",", ":")), "pending", now, now),
                )
                return self._decode(conn.execute("SELECT * FROM platform_commands WHERE command_id=? LIMIT 1", (command.command_id,)).fetchone())
        except sqlite3.IntegrityError as exc:
            raise CommandRepositoryError("command_conflict") from exc
        except CapabilityStoreError as exc:
            raise CommandRepositoryError(exc.code) from exc
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise CommandRepositoryError("command_store") from exc

    def claim_for_node(self, node_id: str, worker_id: str, *, owner_id: str | None = None, limit: int = 20, lease_s: float = 60.0,
                       credential_fingerprint: str | None = None) -> list[dict[str, Any]]:
        now = float(self.clock())
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            if credential_fingerprint is not None and (
                    owner_id is None or not node_credential_is_current(
                        conn, owner=owner_id, node=node_id, fingerprint=credential_fingerprint)):
                raise CommandRepositoryError("command_credential_revoked")
            # Settle known non-dispatch separately from uncertain execution.
            # Intermediate receipts do not remove the original lease fence.
            self._expire(conn, now, node_id=node_id, owner_id=owner_id)
            if owner_id is None:
                rows = conn.execute("SELECT * FROM platform_commands WHERE target_node=? AND owner_id IS NULL AND status='queued' AND expires_at>? ORDER BY created_at, command_id LIMIT ?", (node_id, now, max(1, min(int(limit), 100)))).fetchall()
            else:
                rows = conn.execute("SELECT * FROM platform_commands WHERE target_node=? AND owner_id=? AND status='queued' AND expires_at>? ORDER BY created_at, command_id LIMIT ?", (node_id, owner_id, now, max(1, min(int(limit), 100)))).fetchall()
            requested_lease = float(lease_s)
            if not math.isfinite(requested_lease):
                raise ValueError("invalid lease")
            until = now + max(1.0, min(requested_lease, MAX_COMMAND_LIFETIME_S))
            out = []
            for row in rows:
                if not self._capability_lease_allowed(conn, self._decode(row), now):
                    continue
                timing = conn.execute("SELECT * FROM platform_command_timing WHERE command_id=?", (row["command_id"],)).fetchone()
                lease_until = until
                if timing is not None:
                    # A timed command's original hard limit bounds the lease;
                    # the Node never pre-leases work it cannot start now.
                    lease_until = float(row["expires_at"])
                    execution_deadline = min(lease_until, now + timing["execution_timeout_s"])
                    receipt_deadline = min(lease_until, execution_deadline + timing["receipt_timeout_s"])
                    conn.execute("UPDATE platform_command_timing SET claimed_at=?,execution_deadline=?,receipt_deadline=? WHERE command_id=?",
                        (now, execution_deadline, receipt_deadline, row["command_id"]))
                conn.execute("UPDATE platform_commands SET status='leased',lease_owner=?,lease_until=?,attempt=attempt+1,updated_at=? WHERE command_id=? AND status='queued'", (worker_id, lease_until, now, row["command_id"]))
                out.append(self._decode(conn.execute("SELECT * FROM platform_commands WHERE command_id=?", (row["command_id"],)).fetchone()))
            conn.execute("COMMIT")
            return out
        except CommandRepositoryError:
            raise
        except (sqlite3.Error, ValueError, TypeError) as exc:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise CommandRepositoryError("command_store") from exc
        finally:
            if conn is not None: conn.close()

    def renew_command(self, command_id: str, worker_id: str, *, owner_id: str,
                      node_id: str, lease_s: float = 15.0,
                      credential_fingerprint: str | None = None) -> dict[str, Any]:
        """Observe stop/deadline state and renew only an existing active owner.

        Terminal facts stay immutable. In particular, a Node that locally
        stops after cancellation cannot turn the earlier uncertain command
        into a retryable failure through this control channel.
        """
        if (not isinstance(worker_id, str) or not worker_id or len(worker_id) > 128
                or type(lease_s) not in (int, float) or not math.isfinite(lease_s)
                or not 1 <= lease_s <= MAX_COMMAND_LIFETIME_S):
            raise CommandRepositoryError('invalid_command_timing')
        conn = None
        try:
            conn = self._connect()
            conn.execute('BEGIN IMMEDIATE')
            if credential_fingerprint is not None and not node_credential_is_current(
                    conn, owner=owner_id, node=node_id, fingerprint=credential_fingerprint):
                raise CommandRepositoryError('command_credential_revoked')
            row = conn.execute('SELECT * FROM platform_commands WHERE command_id=?', (command_id,)).fetchone()
            if row is None:
                raise CommandRepositoryError('command_not_found')
            if row['owner_id'] != owner_id:
                raise CommandRepositoryError('owner_mismatch')
            if row['target_node'] != node_id:
                raise CommandRepositoryError('node_mismatch')
            now = float(self.clock())
            if row['status'] not in TERMINAL_COMMAND_STATES:
                if row['lease_owner'] != worker_id or row['status'] == 'queued':
                    raise CommandRepositoryError('lease_mismatch')
                self._expire(conn, now, command_id=command_id)
                row = conn.execute('SELECT * FROM platform_commands WHERE command_id=?', (command_id,)).fetchone()
                if (row['status'] not in TERMINAL_COMMAND_STATES and row['run_id']
                        and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone()
                        and conn.execute('SELECT 1 FROM runs WHERE owner_id=? AND run_id=? AND cancel_requested=1',
                                         (owner_id, row['run_id'])).fetchone()):
                    row = self.fence_pending_in_transaction(conn, command_id, now=now,
                        reason='execution_cancelled', expire_queued=True)
            timing = conn.execute('SELECT * FROM platform_command_timing WHERE command_id=?', (command_id,)).fetchone()
            execution_deadline = float(timing['execution_deadline']) if timing and timing['execution_deadline'] is not None else float(row['expires_at'])
            active = row['status'] not in TERMINAL_COMMAND_STATES and now < execution_deadline
            valid_for = 0.0
            if active:
                ceiling = float(row['expires_at'])
                if timing and timing['receipt_deadline'] is not None:
                    ceiling = min(ceiling, float(timing['receipt_deadline']))
                # A control observation must not shorten an already valid
                # lease or extend the original execution/receipt budgets.
                until = min(ceiling, max(float(row['lease_until']), now + lease_s))
                if until > float(row['lease_until']):
                    conn.execute('UPDATE platform_commands SET lease_until=?,updated_at=? WHERE command_id=?',
                                 (until, now, command_id))
                valid_for = max(0.0, until - now)
                active = valid_for > 0
            conn.execute('COMMIT')
            return {'command_id': command_id, 'status': row['status'], 'continue': active,
                    'execution_deadline': execution_deadline, 'valid_for_s': valid_for}
        except CommandRepositoryError:
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise CommandRepositoryError('command_store') from exc
        finally:
            if conn is not None:
                conn.close()

    def _capability_lease_allowed(self, connection: sqlite3.Connection, command: dict[str, Any], now: float) -> bool:
        from .capability_dispatch import reject_disabled_command
        try:
            if self.capability_guard is None:
                reject_disabled_command(connection, command)
            else:
                self.capability_guard(connection, command, now)
            return True
        except CapabilityStoreError as exc:
            if exc.code == "capability_dispatch_blocked" or (exc.code == "capability_unavailable" and self.capability_guard is None):
                return False
            result = json.dumps({"state": "failed", "result": {}, "error_code": "capability_changed"}, separators=(",", ":"))
            connection.execute("UPDATE platform_commands SET status='failed',result=?,updated_at=? WHERE command_id=? AND status='queued'",
                               (result, now, command["command_id"]))
            return False

    def record_receipt(self, command_id: str, status: str, *, result: dict[str, Any] | None = None, worker_id: str | None = None, owner_id: str | None = None, node_id: str | None = None, credential_fingerprint: str | None = None) -> dict[str, Any]:
        if status not in RECEIPT_STATES:
            raise CommandRepositoryError("invalid_command_state")
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM platform_commands WHERE command_id=?", (command_id,)).fetchone()
            if not row:
                conn.execute("ROLLBACK")
                raise CommandRepositoryError("command_not_found")
            if owner_id is not None and row["owner_id"] != owner_id:
                conn.execute("ROLLBACK")
                raise CommandRepositoryError("owner_mismatch")
            if node_id is not None and row["target_node"] != node_id:
                conn.execute("ROLLBACK")
                raise CommandRepositoryError("node_mismatch")
            if row["action"] == "artifact.receive":
                if self.artifact_receive_guard is None:
                    raise CommandRepositoryError("artifact_receive_disabled")
                from hub.domain.artifact_receive import ArtifactReceiveError
                try:
                    self.artifact_receive_guard(conn, self._decode(row), status, result or {}, worker_id,
                                                float(self.clock()), credential_fingerprint)
                except ArtifactReceiveError as exc:
                    raise CommandRepositoryError(exc.code) from exc
            if row["action"] == "artifact.export":
                if self.artifact_export_guard is None:
                    raise CommandRepositoryError("artifact_export_disabled")
                from hub.domain.artifact_transfer import ArtifactTransferError
                try:
                    self.artifact_export_guard(conn, self._decode(row), status, result or {}, worker_id,
                                               float(self.clock()), credential_fingerprint)
                except ArtifactTransferError as exc:
                    raise CommandRepositoryError(exc.code) from exc
            current = row["status"]
            # A non-terminal receipt is only valid for the worker that holds
            # the current lease. Accepting a missing worker or an unleased
            # command would let any authenticated node mutate command state.
            if current not in TERMINAL_COMMAND_STATES and (
                not worker_id or row["lease_owner"] != worker_id
            ):
                conn.execute("ROLLBACK")
                raise CommandRepositoryError("lease_mismatch")
            if worker_id and row["lease_owner"] not in (None, worker_id):
                conn.execute("ROLLBACK")
                raise CommandRepositoryError("lease_mismatch")
            if current in TERMINAL_COMMAND_STATES:
                conn.execute("COMMIT")
                return self._decode(row)
            now = float(self.clock())
            # Deadline settlement and receipt acceptance share the write
            # transaction, so a late receipt cannot race the expiry sweep.
            self._expire(conn, now, command_id=command_id)
            settled = conn.execute("SELECT * FROM platform_commands WHERE command_id=?", (command_id,)).fetchone()
            if settled["status"] in TERMINAL_COMMAND_STATES:
                conn.execute("COMMIT")
                if row["lease_until"] is None or float(row["lease_until"]) <= now:
                    raise CommandRepositoryError("lease_expired")
                return self._decode(settled)
            if row["lease_until"] is None or float(row["lease_until"]) <= now:
                conn.execute("UPDATE platform_commands SET status='unknown',result=?,lease_owner=NULL,lease_until=NULL,updated_at=? WHERE command_id=?", (json.dumps({"reason": "lease_expired"}, sort_keys=True, separators=(",", ":")), now, command_id))
                conn.execute("COMMIT")
                raise CommandRepositoryError("lease_expired")
            encoded = json.dumps(result or {}, sort_keys=True, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > 64 * 1024:
                conn.execute("ROLLBACK")
                raise CommandRepositoryError("result_too_large")
            if status in TERMINAL_COMMAND_STATES:
                conn.execute("UPDATE platform_commands SET status=?,result=?,lease_owner=NULL,lease_until=NULL,updated_at=? WHERE command_id=?", (status, encoded, now, command_id))
            else:
                # Intermediate receipts keep the lease so the same worker can
                # later send a terminal receipt.
                conn.execute("UPDATE platform_commands SET status=?,result=?,updated_at=? WHERE command_id=?", (status, encoded, now, command_id))
            saved = conn.execute("SELECT * FROM platform_commands WHERE command_id=?", (command_id,)).fetchone()
            conn.execute("COMMIT")
            return self._decode(saved)
        except CommandRepositoryError:
            raise
        except (sqlite3.Error, TypeError, ValueError):
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise CommandRepositoryError("command_store") from None
        finally:
            if conn is not None: conn.close()

    @staticmethod
    def _expire(conn, now: float, *, command_id=None, node_id=None, owner_id=None) -> int:
        scope, values = "", []
        if command_id is not None:
            scope += " AND command_id=?"
            values.append(command_id)
        if node_id is not None:
            scope += " AND target_node=?"
            values.append(node_id)
            scope += " AND owner_id IS NULL" if owner_id is None else " AND owner_id=?"
            if owner_id is not None:
                values.append(owner_id)
        queued = conn.execute("UPDATE platform_commands SET status='expired',result=?,lease_owner=NULL,lease_until=NULL,updated_at=? "
            "WHERE status='queued' AND (expires_at<=? OR EXISTS "
            "(SELECT 1 FROM platform_command_timing t WHERE t.command_id=platform_commands.command_id AND t.queue_deadline<=?))" + scope,
            (json.dumps({"reason": "queue_expired"}), now, now, now, *values))
        active = conn.execute("UPDATE platform_commands SET status='unknown',result=?,lease_owner=NULL,lease_until=NULL,updated_at=? "
            "WHERE status IN ('leased','accepted','running') AND (expires_at<=? OR lease_until<=? OR EXISTS "
            "(SELECT 1 FROM platform_command_timing t WHERE t.command_id=platform_commands.command_id AND t.receipt_deadline<=?))" + scope,
            (json.dumps({"reason": "execution_deadline_expired"}), now, now, now, now, *values))
        return int(queued.rowcount or 0) + int(active.rowcount or 0)

    def get_timing(self, command_id: str) -> dict[str, Any] | None:
        try:
            with closing(self._connect()) as conn:
                row = conn.execute("SELECT * FROM platform_command_timing WHERE command_id=?", (command_id,)).fetchone()
                return dict(row) if row else None
        except sqlite3.Error:
            raise CommandRepositoryError("command_store") from None

    def expire_command(self, command_id: str) -> dict[str, Any] | None:
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                self._expire(conn, float(self.clock()), command_id=command_id)
                row = conn.execute("SELECT * FROM platform_commands WHERE command_id=?", (command_id,)).fetchone()
                return self._decode(row) if row else None
        except sqlite3.Error:
            raise CommandRepositoryError("command_store") from None

    def expire_stale(self) -> int:
        """Queued expiry proves non-dispatch; active expiry remains uncertain."""
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            now = float(self.clock())
            count = self._expire(conn, now)
            conn.execute("COMMIT")
            return count
        except sqlite3.Error:
            raise CommandRepositoryError("command_store") from None
        finally:
            if conn is not None: conn.close()

    def get(self, command_id: str) -> dict[str, Any] | None:
        conn = None
        try:
            conn = self._connect()
            row = conn.execute("SELECT * FROM platform_commands WHERE command_id=?", (command_id,)).fetchone()
            return self._decode(row) if row else None
        except sqlite3.Error:
            raise CommandRepositoryError("command_store") from None
        finally:
            if conn is not None: conn.close()

    def get_for_owner(self, owner_id: str, command_id: str) -> dict[str, Any] | None:
        """Return a command only when it belongs to ``owner_id``.

        This method is deliberately separate from ``get`` so operator HTTP
        adapters cannot accidentally turn an unscoped repository lookup into
        an authorization check.
        """
        conn = None
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM platform_commands WHERE owner_id=? AND command_id=?",
                (owner_id, command_id),
            ).fetchone()
            return self._decode(row) if row else None
        except sqlite3.Error:
            raise CommandRepositoryError("command_store") from None
        finally:
            if conn is not None: conn.close()

    def list_unknown_for_owner(self, owner_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        conn = None
        try:
            conn = self._connect()
            rows = conn.execute(
                "SELECT * FROM platform_commands WHERE owner_id=? AND status='unknown' "
                "ORDER BY updated_at DESC, command_id DESC LIMIT ?",
                (owner_id, max(1, min(int(limit), 100))),
            ).fetchall()
            return [self._decode(row) for row in rows]
        except (sqlite3.Error, TypeError, ValueError):
            raise CommandRepositoryError("command_store") from None
        finally:
            if conn is not None: conn.close()

    def record_reconciliation(self, owner_id: str, command_id: str, *, actor: str,
                              outcome: str, evidence_source: str,
                              evidence_reference: str, reconciliation_id: str,
                              now: float | None = None) -> dict[str, Any]:
        """Append idempotent audit evidence without changing the unknown command."""
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute('BEGIN IMMEDIATE')
                self._require_unknown_command(connection, owner_id, command_id)
                values = {'reconciliation_id': reconciliation_id, 'command_id': command_id, 'owner_id': owner_id,
                    'actor': actor, 'outcome': outcome, 'evidence_source': evidence_source,
                    'evidence_reference': evidence_reference}
                return self._save_reconciliation(connection, values, self._evidence_time(self.clock() if now is None else now))
        except CommandRepositoryError:
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise CommandRepositoryError('command_store') from exc

    @staticmethod
    def _evidence_time(value: float) -> float:
        result = float(value)
        if not math.isfinite(result):
            raise ValueError('invalid_evidence_time')
        return result

    @staticmethod
    def _require_unknown_command(connection: sqlite3.Connection, owner_id: str, command_id: str) -> None:
        row = connection.execute('SELECT status FROM platform_commands WHERE owner_id=? AND command_id=?',
            (owner_id, command_id)).fetchone()
        if row is None:
            raise CommandRepositoryError('command_not_found')
        if row['status'] != 'unknown':
            raise CommandRepositoryError('reconcile_requires_unknown')

    @staticmethod
    def _matching_reconciliation(connection: sqlite3.Connection, values: dict[str, str]) -> dict[str, Any] | None:
        row = connection.execute(
            'SELECT reconciliation_id,command_id,owner_id,actor,outcome,evidence_source,evidence_reference,created_at '
            'FROM platform_command_reconciliations WHERE reconciliation_id=?', (values['reconciliation_id'],),
        ).fetchone()
        if row is None:
            return None
        if any(row[key] != value for key, value in values.items()):
            raise CommandRepositoryError('reconciliation_conflict')
        return dict(row)

    @staticmethod
    def _save_reconciliation(connection: sqlite3.Connection, values: dict[str, str], created: float) -> dict[str, Any]:
        prior = CommandRepository._matching_reconciliation(connection, values)
        if prior is not None:
            return prior
        connection.execute(
            'INSERT INTO platform_command_reconciliations(reconciliation_id,command_id,owner_id,actor,outcome,'
            'evidence_source,evidence_reference,created_at) VALUES(?,?,?,?,?,?,?,?)',
            tuple(values[key] for key in ('reconciliation_id', 'command_id', 'owner_id', 'actor', 'outcome',
                'evidence_source', 'evidence_reference')) + (created,),
        )
        return values | {'created_at': created}

    def create_postcheck(self, owner_id: str, command_id: str, *, kind: str,
                         check_command_id: str, path: str, expected_sha256: str,
                         now: float | None = None) -> dict[str, Any]:
        """Create or return one deterministic post-check descriptor.

        The descriptor is created separately from command enqueue so the
        application can use the existing delivery facade and signature policy.
        Its unique key makes retries return the same check command id.
        """
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            original = conn.execute(
                "SELECT command_id,owner_id,status FROM platform_commands "
                "WHERE owner_id=? AND command_id=?", (owner_id, command_id),
            ).fetchone()
            if not original:
                conn.execute("ROLLBACK")
                raise CommandRepositoryError("command_not_found")
            if original["status"] != "unknown":
                conn.execute("ROLLBACK")
                raise CommandRepositoryError("reconcile_requires_unknown")
            prior = conn.execute(
                "SELECT * FROM platform_command_postchecks WHERE owner_id=? AND command_id=? AND kind=?",
                (owner_id, command_id, kind),
            ).fetchone()
            if prior:
                conn.execute("COMMIT")
                return self._decode_postcheck(prior)
            created = float(self.clock() if now is None else now)
            conn.execute(
                "INSERT INTO platform_command_postchecks("
                "owner_id,command_id,kind,check_command_id,path,expected_sha256,state,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?, 'pending', ?,?)",
                (owner_id, command_id, kind, check_command_id, path, expected_sha256, created, created),
            )
            row = conn.execute(
                "SELECT * FROM platform_command_postchecks WHERE owner_id=? AND command_id=? AND kind=?",
                (owner_id, command_id, kind),
            ).fetchone()
            conn.execute("COMMIT")
            return self._decode_postcheck(row)
        except CommandRepositoryError:
            raise
        except sqlite3.IntegrityError:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise CommandRepositoryError("postcheck_conflict") from None
        except (sqlite3.Error, TypeError, ValueError):
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise CommandRepositoryError("command_store") from None
        finally:
            if conn is not None: conn.close()

    @staticmethod
    def _decode_postcheck(row) -> dict[str, Any]:
        result = dict(row)
        result["result"] = json.loads(row["result"]) if row["result"] else None
        return result

    def get_postcheck(self, owner_id: str, command_id: str, *, kind: str = "workspace_digest") -> dict[str, Any] | None:
        conn = None
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM platform_command_postchecks WHERE owner_id=? AND command_id=? AND kind=?",
                (owner_id, command_id, kind),
            ).fetchone()
            return self._decode_postcheck(row) if row else None
        except sqlite3.Error:
            raise CommandRepositoryError("command_store") from None
        finally:
            if conn is not None: conn.close()

    def get_postcheck_by_check_command(self, owner_id: str, check_command_id: str) -> dict[str, Any] | None:
        conn = None
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM platform_command_postchecks WHERE owner_id=? AND check_command_id=?",
                (owner_id, check_command_id),
            ).fetchone()
            return self._decode_postcheck(row) if row else None
        except sqlite3.Error:
            raise CommandRepositoryError("command_store") from None
        finally:
            if conn is not None: conn.close()

    def finish_postcheck(self, owner_id: str, command_id: str, *, kind: str,
                         state: str, result: dict[str, Any] | None = None,
                         evidence_id: str | None = None, now: float | None = None) -> dict[str, Any]:
        if kind != 'workspace_digest':
            raise CommandRepositoryError('postcheck_conflict')
        return self._finish_postcheck(owner_id, command_id, kind=kind, state=state, result=result,
            evidence_id=evidence_id, now=now)

    @staticmethod
    def _postcheck_audit(row: sqlite3.Row, source: str) -> dict[str, str]:
        evidence_id = 'postcheck-' + row['check_command_id']
        if row['state'] not in _TERMINAL_POSTCHECK_STATES or row['evidence_id'] != evidence_id:
            raise CommandRepositoryError('postcheck_conflict')
        return {'reconciliation_id': evidence_id, 'command_id': row['command_id'], 'owner_id': row['owner_id'],
            'actor': 'postcheck', 'outcome': 'confirmed_succeeded' if row['state'] == 'matched' else 'remains_unknown',
            'evidence_source': source, 'evidence_reference': evidence_id}

    def _complete_postcheck(self, connection: sqlite3.Connection, owner_id: str, command_id: str, *, kind: str,
                            state: str, result: dict[str, Any] | None, evidence_id: str | None,
                            now: float | None) -> dict[str, Any]:
        table, columns, source = _POSTCHECK_STORAGE[kind]
        select = f'SELECT {columns} FROM {table} WHERE owner_id=? AND command_id=? AND kind=?'
        identity = (owner_id, command_id, kind)
        row = connection.execute(select, identity).fetchone()
        if row is None:
            raise CommandRepositoryError('postcheck_not_found')
        self._require_unknown_command(connection, owner_id, command_id)
        if row['state'] == 'pending':
            if evidence_id != 'postcheck-' + row['check_command_id']:
                raise CommandRepositoryError('postcheck_conflict')
            if result is not None and type(result) is not dict:
                raise CommandRepositoryError('postcheck_conflict')
            encoded = json.dumps(result or {}, sort_keys=True, separators=(',', ':'), allow_nan=False)
            if len(encoded.encode('utf-8')) > 16 * 1024:
                raise CommandRepositoryError('result_too_large')
            created = self._evidence_time(self.clock() if now is None else now)
            connection.execute(
                f'UPDATE {table} SET state=?,result=?,evidence_id=?,updated_at=? '
                "WHERE owner_id=? AND command_id=? AND kind=? AND state='pending'",
                (state, encoded, evidence_id, created) + identity,
            )
            row = connection.execute(select, identity).fetchone()
        self._save_reconciliation(connection, self._postcheck_audit(row, source), self._evidence_time(row['updated_at']))
        return self._decode_postcheck(row)

    def _finish_postcheck(self, owner_id: str, command_id: str, *, kind: str, state: str,
                          result: dict[str, Any] | None, evidence_id: str | None,
                          now: float | None) -> dict[str, Any]:
        if type(state) is not str or state not in _TERMINAL_POSTCHECK_STATES:
            raise CommandRepositoryError('invalid_postcheck_state')
        table, columns, source = _POSTCHECK_STORAGE[kind]
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(f'SELECT {columns} FROM {table} WHERE owner_id=? AND command_id=? AND kind=?',
                    (owner_id, command_id, kind)).fetchone()
                if row is None:
                    raise CommandRepositoryError('postcheck_not_found')
                if row['state'] in _TERMINAL_POSTCHECK_STATES and self._matching_reconciliation(
                        connection, self._postcheck_audit(row, source)) is not None:
                    self._require_unknown_command(connection, owner_id, command_id)
                    return self._decode_postcheck(row)
                with connection:
                    connection.execute('BEGIN IMMEDIATE')
                    return self._complete_postcheck(connection, owner_id, command_id, kind=kind,
                        state=state, result=result, evidence_id=evidence_id, now=now)
        except CommandRepositoryError:
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise CommandRepositoryError('command_store') from exc

    def create_service_postcheck(self, owner_id: str, command_id: str, *, kind: str,
                                 check_command_id: str, service_id: str,
                                 service_version: int, now: float | None = None) -> dict[str, Any]:
        """Create or return one deterministic service-inspect post-check."""
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            original = conn.execute(
                "SELECT command_id,owner_id,status FROM platform_commands "
                "WHERE owner_id=? AND command_id=?", (owner_id, command_id),
            ).fetchone()
            if not original:
                conn.execute("ROLLBACK")
                raise CommandRepositoryError("command_not_found")
            if original["status"] != "unknown":
                conn.execute("ROLLBACK")
                raise CommandRepositoryError("reconcile_requires_unknown")
            prior = conn.execute(
                "SELECT * FROM platform_service_postchecks WHERE owner_id=? AND command_id=? AND kind=?",
                (owner_id, command_id, kind),
            ).fetchone()
            if prior:
                if (prior["service_id"] != service_id
                        or int(prior["service_version"]) != int(service_version)):
                    conn.execute("ROLLBACK")
                    raise CommandRepositoryError("service_postcheck_conflict")
                conn.execute("COMMIT")
                return self._decode_service_postcheck(prior)
            created = float(self.clock() if now is None else now)
            conn.execute(
                "INSERT INTO platform_service_postchecks("
                "owner_id,command_id,kind,check_command_id,service_id,service_version,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?, 'pending', ?,?)",
                (owner_id, command_id, kind, check_command_id, service_id,
                 int(service_version), created, created),
            )
            row = conn.execute(
                "SELECT * FROM platform_service_postchecks WHERE owner_id=? AND command_id=? AND kind=?",
                (owner_id, command_id, kind),
            ).fetchone()
            conn.execute("COMMIT")
            return self._decode_service_postcheck(row)
        except CommandRepositoryError:
            raise
        except sqlite3.IntegrityError:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise CommandRepositoryError("service_postcheck_conflict") from None
        except (sqlite3.Error, TypeError, ValueError):
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise CommandRepositoryError("command_store") from None
        finally:
            if conn is not None: conn.close()

    @staticmethod
    def _decode_service_postcheck(row) -> dict[str, Any]:
        result = dict(row)
        result["result"] = json.loads(row["result"]) if row["result"] else None
        return result

    def get_service_postcheck(self, owner_id: str, command_id: str, *,
                              kind: str = "service_inspect") -> dict[str, Any] | None:
        conn = None
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM platform_service_postchecks WHERE owner_id=? AND command_id=? AND kind=?",
                (owner_id, command_id, kind),
            ).fetchone()
            return self._decode_service_postcheck(row) if row else None
        except sqlite3.Error:
            raise CommandRepositoryError("command_store") from None
        finally:
            if conn is not None: conn.close()

    def finish_service_postcheck(self, owner_id: str, command_id: str, *, kind: str,
                                 state: str, result: dict[str, Any] | None = None,
                                 evidence_id: str | None = None,
                                 now: float | None = None) -> dict[str, Any]:
        if type(kind) is not str or kind not in {'service_inspect', 'service_logs'}:
            raise CommandRepositoryError('postcheck_conflict')
        return self._finish_postcheck(owner_id, command_id, kind=kind, state=state, result=result,
            evidence_id=evidence_id, now=now)

    def mark_unknown(self, command_id: str, *, reason: str) -> dict[str, Any]:
        """Fence a non-terminal command as unknown without re-queueing it.

        A timeout or lost receipt cannot prove whether the Node performed the
        side effect.  The durable command therefore becomes terminal
        ``unknown`` and must be reconciled explicitly by a later operation.
        """
        return self._fence_pending_command(command_id, reason=reason, expire_queued=False)

    def cancel_wait(self, command_id: str) -> dict[str, Any]:
        """Prevent unclaimed work; dispatched work has no stop confirmation.

        Read the current state under the same writer lock as Node claim, rather
        than trusting the waiter's earlier read. Existing terminal facts win.
        """
        return self._fence_pending_command(command_id, reason="execution_cancelled", expire_queued=True)

    def _fence_pending_command(self, command_id: str, *, reason: str,
                               expire_queued: bool) -> dict[str, Any]:
        if not isinstance(reason, str) or not reason:
            raise CommandRepositoryError("invalid_unknown_reason")
        bounded_reason = reason[:120]
        conn = None
        try:
            conn = self._connect()
            now = float(self.clock())
            conn.execute("BEGIN IMMEDIATE")
            saved = self.fence_pending_in_transaction(conn, command_id, now=now,
                reason=bounded_reason, expire_queued=expire_queued)
            conn.execute("COMMIT")
            return self._decode(saved)
        except CommandRepositoryError:
            raise
        except (sqlite3.Error, TypeError, ValueError):
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise CommandRepositoryError("command_store") from None
        finally:
            if conn is not None: conn.close()

    @staticmethod
    def fence_pending_in_transaction(conn, command_id, *, now, reason, expire_queued):
        """Share the caller's writer lock with Run cancel or Node claim."""
        row = conn.execute('SELECT * FROM platform_commands WHERE command_id=?', (command_id,)).fetchone()
        if row is None:
            raise CommandRepositoryError('command_not_found')
        if row['status'] not in TERMINAL_COMMAND_STATES:
            status = 'expired' if expire_queued and row['status'] == 'queued' else 'unknown'
            conn.execute('UPDATE platform_commands SET status=?,result=?,lease_owner=NULL,lease_until=NULL,updated_at=? WHERE command_id=?',
                (status, json.dumps({'reason': reason}, sort_keys=True, separators=(',', ':')), now, command_id))
            row = conn.execute('SELECT * FROM platform_commands WHERE command_id=?', (command_id,)).fetchone()
        return row

    def claim_outbox(self, worker_id: str, *, limit: int = 20,
                     lease_s: float = 60.0) -> list[dict[str, Any]]:
        """Claim pending command-created events for a dispatcher.

        Claiming is transactional and lease based. Expired claims return to
        pending; the payload is bounded metadata and never contains secrets.
        """
        if not isinstance(worker_id, str) or not worker_id or len(worker_id) > 128:
            raise CommandRepositoryError("invalid_outbox_worker")
        conn = None
        try:
            now = float(self.clock())
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE platform_command_outbox SET state='pending',lease_owner=NULL,lease_until=NULL,updated_at=? "
                "WHERE state='leased' AND lease_until IS NOT NULL AND lease_until<?",
                (now, now),
            )
            rows = conn.execute(
                "SELECT * FROM platform_command_outbox WHERE state='pending' "
                "ORDER BY created_at,outbox_id LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
            until = now + max(1.0, float(lease_s))
            out = []
            for row in rows:
                conn.execute(
                    "UPDATE platform_command_outbox SET state='leased',lease_owner=?,lease_until=?,attempts=attempts+1,updated_at=? WHERE outbox_id=? AND state='pending'",
                    (worker_id, until, now, row["outbox_id"]),
                )
                saved = conn.execute(
                    "SELECT * FROM platform_command_outbox WHERE outbox_id=?",
                    (row["outbox_id"],),
                ).fetchone()
                item = dict(saved)
                item["payload"] = json.loads(item["payload"])
                out.append(item)
            conn.execute("COMMIT")
            return out
        except CommandRepositoryError:
            raise
        except (sqlite3.Error, ValueError, TypeError):
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise CommandRepositoryError("command_store") from None
        finally:
            if conn is not None: conn.close()

    def ack_outbox(self, outbox_id: int, worker_id: str) -> bool:
        conn = None
        try:
            conn = self._connect()
            now = float(self.clock())
            cur = conn.execute(
                "UPDATE platform_command_outbox SET state='sent',lease_owner=NULL,lease_until=NULL,updated_at=? "
                "WHERE outbox_id=? AND state='leased' AND lease_owner=? AND lease_until>?",
                (now, int(outbox_id), worker_id, now),
            )
            return cur.rowcount == 1
        except (sqlite3.Error, ValueError, TypeError):
            raise CommandRepositoryError("command_store") from None
        finally:
            if conn is not None: conn.close()

    def get_outbox(self, outbox_id: int) -> dict[str, Any] | None:
        conn = None
        try:
            conn = self._connect()
            row = conn.execute("SELECT * FROM platform_command_outbox WHERE outbox_id=?", (int(outbox_id),)).fetchone()
            if not row:
                return None
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            return item
        except (sqlite3.Error, ValueError, TypeError):
            raise CommandRepositoryError("command_store") from None
        finally:
            if conn is not None: conn.close()


__all__ = ["CommandRepository", "CommandRepositoryError"]
