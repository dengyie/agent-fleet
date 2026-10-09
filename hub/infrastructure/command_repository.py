"""Durable platform command/outbox repository with lease recovery."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable
from contextlib import closing

from hub.domain.platform_command import RECEIPT_STATES, PlatformCommand, TERMINAL_COMMAND_STATES, args_hash


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

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

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

    def enqueue(self, command: PlatformCommand, *, idempotency_key: str | None = None,
                require_signature: bool = False,
                prepare: Callable[[sqlite3.Connection, PlatformCommand], PlatformCommand] | None = None) -> dict[str, Any]:
        """Persist command/outbox and any admission preparation in one transaction."""
        now = float(self.clock())
        if require_signature and prepare is None and not command.signature:
            raise CommandRepositoryError("command_signature_required")
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                prior = conn.execute("SELECT * FROM platform_commands WHERE idempotency_key=? LIMIT 1", (idempotency_key,)).fetchone() if idempotency_key else None
                if prior is not None:
                    existing = self._decode(prior)
                    expected_digest = existing["args_hash"]
                    if prepare is not None and command.action == "tool.browser.submit":
                        expected_arguments = dict(existing["arguments"])
                        expected_arguments.pop("approval_id", None)
                        expected_digest = args_hash(expected_arguments)
                    if (existing["owner_id"] != command.owner_id
                            or expected_digest != command.args_digest
                            or existing["target_node"] != command.target_node
                            or existing["action"] != command.action
                            or existing["resource_id"] != command.resource_id
                            or existing["run_id"] != command.run_id):
                        raise CommandRepositoryError("idempotency_conflict")
                    return existing
                if prepare is not None:
                    command = prepare(conn, command)
                if require_signature and not command.signature:
                    raise CommandRepositoryError("command_signature_required")
                conn.execute("INSERT INTO platform_commands(command_id,owner_id,target_node,action,resource_id,arguments,retry_class,expires_at,args_hash,run_id,grant_id,signature,status,created_at,updated_at,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (command.command_id, command.owner_id, command.target_node, command.action, command.resource_id, json.dumps(command.arguments, sort_keys=True, separators=(",", ":")), command.retry_class, command.expires_at, command.args_digest, command.run_id, command.grant_id, command.signature, "queued", now, now, idempotency_key))
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
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise CommandRepositoryError("command_store") from exc

    def claim_for_node(self, node_id: str, worker_id: str, *, owner_id: str | None = None, limit: int = 20, lease_s: float = 60.0) -> list[dict[str, Any]]:
        now = float(self.clock())
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            # A lost lease does not prove that the side effect did not happen.
            # Preserve that uncertainty instead of blindly redelivering the
            # same command.  An explicit reconciliation policy may later
            # create a new command with a fresh idempotency key.
            if owner_id is None:
                conn.execute("UPDATE platform_commands SET status='unknown', lease_owner=NULL, lease_until=NULL, updated_at=? WHERE target_node=? AND status='leased' AND lease_until IS NOT NULL AND lease_until<?", (now, node_id, now))
            else:
                conn.execute("UPDATE platform_commands SET status='unknown', lease_owner=NULL, lease_until=NULL, updated_at=? WHERE owner_id=? AND target_node=? AND status='leased' AND lease_until IS NOT NULL AND lease_until<?", (now, owner_id, node_id, now))
            if owner_id is None:
                rows = conn.execute("SELECT * FROM platform_commands WHERE target_node=? AND owner_id IS NULL AND status='queued' AND expires_at>? ORDER BY created_at, command_id LIMIT ?", (node_id, now, max(1, min(int(limit), 100)))).fetchall()
            else:
                rows = conn.execute("SELECT * FROM platform_commands WHERE target_node=? AND owner_id=? AND status='queued' AND expires_at>? ORDER BY created_at, command_id LIMIT ?", (node_id, owner_id, now, max(1, min(int(limit), 100)))).fetchall()
            until = now + max(1.0, float(lease_s))
            out = []
            for row in rows:
                conn.execute("UPDATE platform_commands SET status='leased',lease_owner=?,lease_until=?,attempt=attempt+1,updated_at=? WHERE command_id=? AND status='queued'", (worker_id, until, now, row["command_id"]))
                out.append(self._decode(conn.execute("SELECT * FROM platform_commands WHERE command_id=?", (row["command_id"],)).fetchone()))
            conn.execute("COMMIT")
            return out
        except (sqlite3.Error, ValueError, TypeError):
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise CommandRepositoryError("command_store") from None
        finally:
            if conn is not None: conn.close()

    def record_receipt(self, command_id: str, status: str, *, result: dict[str, Any] | None = None, worker_id: str | None = None, owner_id: str | None = None, node_id: str | None = None) -> dict[str, Any]:
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

    def expire_stale(self) -> int:
        """Mark commands past their expiry as terminal ``expired``."""
        conn = None
        try:
            conn = self._connect()
            now = float(self.clock())
            cur = conn.execute("UPDATE platform_commands SET status='expired',lease_owner=NULL,lease_until=NULL,updated_at=? WHERE status NOT IN ('succeeded','failed','unknown','expired') AND expires_at<=?", (now, now))
            return int(cur.rowcount or 0)
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
        """Append bounded operator evidence for an unknown command.

        This is audit-only by design: it never changes the command status and
        never creates a replacement command. A later deterministic post-check
        may consume this record under a separate policy.
        """
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            command = conn.execute(
                "SELECT command_id, owner_id, status FROM platform_commands "
                "WHERE owner_id=? AND command_id=?", (owner_id, command_id),
            ).fetchone()
            if not command:
                conn.execute("ROLLBACK")
                raise CommandRepositoryError("command_not_found")
            if command["status"] != "unknown":
                conn.execute("ROLLBACK")
                raise CommandRepositoryError("reconcile_requires_unknown")
            created = float(self.clock() if now is None else now)
            conn.execute(
                "INSERT INTO platform_command_reconciliations("
                "reconciliation_id,command_id,owner_id,actor,outcome,"
                "evidence_source,evidence_reference,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (reconciliation_id, command_id, owner_id, actor, outcome,
                 evidence_source, evidence_reference, created),
            )
            row = conn.execute(
                "SELECT * FROM platform_command_reconciliations WHERE reconciliation_id=?",
                (reconciliation_id,),
            ).fetchone()
            conn.execute("COMMIT")
            return dict(row)
        except CommandRepositoryError:
            raise
        except sqlite3.IntegrityError:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            # Re-reading a completed deterministic post-check must be safe.
            # A stable reconciliation id is therefore treated as an idempotency
            # key when its bounded audit payload is identical.
            try:
                conn = self._connect()
                prior = conn.execute(
                    "SELECT * FROM platform_command_reconciliations WHERE reconciliation_id=?",
                    (reconciliation_id,),
                ).fetchone()
                if prior and all(prior[key] == value for key, value in {
                    "command_id": command_id, "owner_id": owner_id,
                    "actor": actor, "outcome": outcome,
                    "evidence_source": evidence_source,
                    "evidence_reference": evidence_reference,
                }.items()):
                    return dict(prior)
            except sqlite3.Error:
                pass
            finally:
                if conn is not None:
                    conn.close()
            raise CommandRepositoryError("reconciliation_conflict") from None
        except (sqlite3.Error, TypeError, ValueError):
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise CommandRepositoryError("command_store") from None
        finally:
            if conn is not None: conn.close()

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
        if state not in {"matched", "mismatch", "remains_unknown", "failed"}:
            raise CommandRepositoryError("invalid_postcheck_state")
        conn = None
        try:
            conn = self._connect()
            timestamp = float(self.clock() if now is None else now)
            encoded = json.dumps(result or {}, sort_keys=True, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > 16 * 1024:
                raise CommandRepositoryError("result_too_large")
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM platform_command_postchecks WHERE owner_id=? AND command_id=? AND kind=?",
                (owner_id, command_id, kind),
            ).fetchone()
            if not row:
                conn.execute("ROLLBACK")
                raise CommandRepositoryError("postcheck_not_found")
            if row["state"] in {"matched", "mismatch", "remains_unknown", "failed"}:
                conn.execute("COMMIT")
                return self._decode_postcheck(row)
            conn.execute(
                "UPDATE platform_command_postchecks SET state=?,result=?,evidence_id=?,updated_at=? "
                "WHERE owner_id=? AND command_id=? AND kind=? AND state='pending'",
                (state, encoded, evidence_id, timestamp, owner_id, command_id, kind),
            )
            saved = conn.execute(
                "SELECT * FROM platform_command_postchecks WHERE owner_id=? AND command_id=? AND kind=?",
                (owner_id, command_id, kind),
            ).fetchone()
            conn.execute("COMMIT")
            return self._decode_postcheck(saved)
        except CommandRepositoryError:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise
        except (sqlite3.Error, TypeError, ValueError):
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise CommandRepositoryError("command_store") from None
        finally:
            if conn is not None: conn.close()

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
        if state not in {"matched", "mismatch", "remains_unknown", "failed"}:
            raise CommandRepositoryError("invalid_postcheck_state")
        conn = None
        try:
            conn = self._connect()
            encoded = json.dumps(result or {}, sort_keys=True, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > 16 * 1024:
                raise CommandRepositoryError("result_too_large")
            timestamp = float(self.clock() if now is None else now)
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM platform_service_postchecks WHERE owner_id=? AND command_id=? AND kind=?",
                (owner_id, command_id, kind),
            ).fetchone()
            if not row:
                conn.execute("ROLLBACK")
                raise CommandRepositoryError("postcheck_not_found")
            if row["state"] in {"matched", "mismatch", "remains_unknown", "failed"}:
                conn.execute("COMMIT")
                return self._decode_service_postcheck(row)
            conn.execute(
                "UPDATE platform_service_postchecks SET state=?,result=?,evidence_id=?,updated_at=? "
                "WHERE owner_id=? AND command_id=? AND kind=? AND state='pending'",
                (state, encoded, evidence_id, timestamp, owner_id, command_id, kind),
            )
            saved = conn.execute(
                "SELECT * FROM platform_service_postchecks WHERE owner_id=? AND command_id=? AND kind=?",
                (owner_id, command_id, kind),
            ).fetchone()
            conn.execute("COMMIT")
            return self._decode_service_postcheck(saved)
        except CommandRepositoryError:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise
        except (sqlite3.Error, TypeError, ValueError):
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise CommandRepositoryError("command_store") from None
        finally:
            if conn is not None: conn.close()

    def mark_unknown(self, command_id: str, *, reason: str) -> dict[str, Any]:
        """Fence a non-terminal command as unknown without re-queueing it.

        A timeout or lost receipt cannot prove whether the Node performed the
        side effect.  The durable command therefore becomes terminal
        ``unknown`` and must be reconciled explicitly by a later operation.
        """
        if not isinstance(reason, str) or not reason:
            raise CommandRepositoryError("invalid_unknown_reason")
        bounded_reason = reason[:120]
        conn = None
        try:
            conn = self._connect()
            now = float(self.clock())
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM platform_commands WHERE command_id=?", (command_id,)).fetchone()
            if not row:
                conn.execute("ROLLBACK")
                raise CommandRepositoryError("command_not_found")
            if row["status"] in TERMINAL_COMMAND_STATES:
                conn.execute("COMMIT")
                return self._decode(row)
            conn.execute(
                "UPDATE platform_commands SET status='unknown', result=?, lease_owner=NULL, lease_until=NULL, updated_at=? WHERE command_id=?",
                (json.dumps({"reason": bounded_reason}, sort_keys=True, separators=(",", ":")), now, command_id),
            )
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
