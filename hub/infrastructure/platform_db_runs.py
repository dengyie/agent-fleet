"""Runs, run leases and run events for ``PlatformRepository``."""
from __future__ import annotations

import secrets
import sqlite3
import time
from collections.abc import Mapping
from typing import Any

from hub.domain.request_metadata import (
    REQUEST_EVENT_KINDS,
    TERMINAL_STATES,
    project_requests,
    thaw_requests,
)
from hub.infrastructure.platform_db_base import (
    PlatformRepositoryError,
    _decode,
    _json,
    freeze_run_request_summary,
)
from hub.infrastructure.run_capacity import workspace_capacity_filter
from hub.infrastructure.runtime_checkpoint import resumable_checkpoint, decode_checkpoint
from hub.domain.run_progress import PROGRESS_KINDS, project_progress
from platform_schema import PlatformValidationError, validate_id, validate_owner_id


class PlatformRunMixin:
    @staticmethod
    def _row_run(row) -> dict[str, Any]:
        return {"run_id": row["run_id"], "conversation_id": row["conversation_id"],
                "owner_id": row["owner_id"], "trigger_message_id": row["trigger_message_id"],
                "state": row["state"], "config_snapshot": _decode(row["config_snapshot"]),
                "cancel_requested": bool(row["cancel_requested"]),
                "attempt": int(row["attempt"] or 0),
                "lease_expires_at": row["lease_expires_at"],
                "result_text": row["result_text"] or "",
                "progress": _decode(row["progress_summary"]),
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

    @staticmethod
    def _attach_requests(conn, owner_id, runs, *, conversation_id=None, run_id=None, run_ids=None):
        # A history page loads only its own Runs and request summaries.
        if run_ids is not None:
            run_ids = tuple(run_ids)
            if not run_ids:
                return
            placeholders = ",".join("?" for _ in run_ids)
            event_scope = f"r.run_id IN ({placeholders})"
            frozen_scope = f"run_id IN ({placeholders})"
            parameters = (owner_id, *run_ids)
        else:
            event_scope = "r.conversation_id=?" if conversation_id else "r.run_id=?"
            frozen_scope = "conversation_id=?" if conversation_id else "run_id=?"
            parameters = (owner_id, conversation_id or run_id)
        rows = conn.execute(
            "SELECT e.run_id,e.kind,e.payload FROM run_events e JOIN runs r ON r.run_id=e.run_id "
            "WHERE r.owner_id=? AND " + event_scope +
            " AND e.kind IN ('provider_request_started','provider_request_finished') ORDER BY e.run_id,e.sequence",
            parameters).fetchall()
        grouped = {}
        for row in rows:
            grouped.setdefault(row['run_id'], []).append({'kind': row['kind'], 'payload': _decode(row['payload'])})
        frozen = {}
        if any(run['run_id'] not in grouped for run in runs):
            # Live events win; once retention pruned them, use the frozen copy.
            frozen = {row['run_id']: row['request_summary'] for row in conn.execute(
                "SELECT run_id,request_summary FROM runs WHERE owner_id=? AND " +
                frozen_scope + " AND request_summary IS NOT NULL",
                parameters).fetchall()}
        for run in runs:
            if run['run_id'] in grouped or run['run_id'] not in frozen:
                run['requests'] = project_requests(grouped.get(run['run_id'], []), run['state'])
            else:
                run['requests'] = thaw_requests(frozen[run['run_id']])
            for request in run['requests']:
                if request.get('status') == 'running' and 'started_at' in request:
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

    def claim_run(self, owner_id: str, *, worker_id: str, now: float, lease_s: float = 60.0,
                  max_workspace_concurrency: int | None = None) -> dict[str, Any] | None:
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
            capacity_sql, capacity_params = workspace_capacity_filter(max_workspace_concurrency, now)
            row = conn.execute(
                "SELECT r.* FROM runs r JOIN messages trigger ON trigger.message_id=r.trigger_message_id "
                # The redundant state IN (...) lets the planner seek
                # idx_runs_owner_state_created instead of scanning the owner's runs.
                "WHERE r.owner_id=? AND r.state IN ('queued','cancelling','running','waiting_node')"
                " AND ((r.state='waiting_node' AND r.runtime_resume_at<=?) OR (r.state='queued' AND"
                " (r.lease_id IS NULL OR r.lease_expires_at IS NULL OR r.lease_expires_at<=?)) OR"
                " (r.state='cancelling' AND (r.lease_expires_at IS NULL OR r.lease_expires_at<=?)) OR"
                " (r.state='running' AND r.lease_expires_at IS NOT NULL AND r.lease_expires_at<=?))"
                " AND NOT EXISTS (SELECT 1 FROM runs prior JOIN messages pm ON pm.message_id=prior.trigger_message_id "
                " WHERE prior.owner_id=r.owner_id AND prior.conversation_id=r.conversation_id "
                " AND pm.turn_sequence<trigger.turn_sequence "
                " AND prior.state NOT IN ('succeeded','failed','unknown','cancelled'))"
                + capacity_sql +
                " ORDER BY r.created_at, trigger.turn_sequence, r.run_id LIMIT 1",
                (owner_id, now, now, now, now, *capacity_params)).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return None
            # Only committed safe native checkpoints may resume. Older Runs
            # and in-flight provider/tool boundaries stay unknown; a lease
            # expiry is not proof that an external side effect stopped.
            started = conn.execute(
                "SELECT 1 FROM run_events WHERE run_id=? "
                "AND kind IN ('run_started','tool_call') LIMIT 1",
                (row["run_id"],),
            ).fetchone()
            if started and not resumable_checkpoint(row["runtime_checkpoint"]):
                conn.execute(
                    "UPDATE runs SET state='unknown',runtime_wait_command_id=NULL,lease_id=NULL,lease_owner=NULL,"
                    "lease_expires_at=NULL,updated_at=? WHERE owner_id=? AND run_id=?",
                    (now, owner_id, row["run_id"]),
                )
                freeze_run_request_summary(conn, row["run_id"], "unknown")
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
            if bool(row["cancel_requested"]) and not resumable_checkpoint(row["runtime_checkpoint"]):
                conn.execute(
                    "UPDATE runs SET state='cancelled',lease_id=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=?"
                    " WHERE owner_id=? AND run_id=?",
                    (now, owner_id, row["run_id"]),
                )
                freeze_run_request_summary(conn, row["run_id"], "cancelled")
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
                if row['runtime_checkpoint']:
                    from hub.infrastructure.command_repository import CommandRepository
                    try:
                        checkpoint = decode_checkpoint(row['runtime_checkpoint'])
                    except PlatformRepositoryError:
                        checkpoint = None
                    if (checkpoint and checkpoint['phase'] == 'remote_wait'
                            and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='platform_commands'").fetchone()):
                        command_id = f"{run_id}:step:{checkpoint['step']}"
                        if conn.execute("SELECT 1 FROM platform_commands WHERE command_id=? AND owner_id=? AND run_id=?",
                                        (command_id, owner_id, run_id)).fetchone():
                            CommandRepository.fence_pending_in_transaction(conn, command_id,
                                now=float(now), reason='execution_cancelled', expire_queued=True)
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
            run = conn.execute("SELECT run_id,state,lease_id,lease_owner,lease_expires_at,progress_summary FROM runs WHERE owner_id=? AND run_id=?", (owner_id, run_id)).fetchone()
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
            if kind in PROGRESS_KINDS:
                progress = project_progress(_decode(run['progress_summary']), kind, payload or {}, sequence)
                conn.execute('UPDATE runs SET progress_summary=? WHERE run_id=?', (_json(progress), run_id))
            if kind in REQUEST_EVENT_KINDS and run["state"] in TERMINAL_STATES:
                # A late request event must not leave the frozen copy stale.
                freeze_run_request_summary(conn, run_id, run["state"])
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

    @staticmethod
    def _workflow_test_completed(connection: sqlite3.Connection, row: sqlite3.Row) -> bool:
        """Only a persisted, scoped test receipt wins a late cancel race."""
        config = _decode(row["config_snapshot"])
        recipe = config.get("workflow_test")
        marker = config.get("workflow_dispatch")
        if type(recipe) is not dict or type(recipe.get("revision")) is not int or recipe != {"recipe_id": "coding-delivery-tests", "revision": 1}:
            return False
        remote = "workflow_remote_test" in config
        if remote:
            from hub.domain.workflow_errors import WorkflowPersistenceError, WorkflowValidationError
            from hub.domain.remote_workflow_test import RemoteTestError, validate_remote_test_config
            from .capability_dispatch import CapabilityDispatchRepository, pinned_dispatch

            try:
                validate_remote_test_config(config)
                pinned = pinned_dispatch(connection, marker["token"])
                if pinned is None or pinned.binding.node_id != config["execution_node_id"]:
                    return False
                import hashlib

                validate_remote_test_config(config, binding_digest=hashlib.sha256(pinned.binding.snapshot).hexdigest())
                CapabilityDispatchRepository._run_configuration(connection, row, config, pinned)
            except (RemoteTestError, WorkflowPersistenceError, WorkflowValidationError, ValueError):
                return False
        if (not remote and set(config) != {"owner_id", "workspace_id", "workflow_dispatch", "workflow_test"}) or config["owner_id"] != row["owner_id"]:
            return False
        if type(marker) is not dict or set(marker) != {"token", "payload_hash", "config_digest"} or any(
                type(value) is not str or len(value) != 64 for value in marker.values()):
            return False
        from hub.domain.workflow_identity import canonical_digest

        token = "delivery:" + canonical_digest({"token": marker["token"], "operation": "test"})
        return connection.execute("SELECT 1 FROM workflow_dispatches d JOIN workflow_operations o "
            "USING(owner_id,workspace_id,workflow_id) WHERE d.owner_id=? AND d.workspace_id=? "
            "AND d.binding_kind='run' AND d.binding_id=? AND d.binding_run_id=? AND d.step_key='test' "
            "AND d.stage='bound' AND d.token=? AND d.payload_hash=? AND o.client_token=? LIMIT 1",
            (row["owner_id"], config["workspace_id"], row["run_id"], row["run_id"], marker["token"], marker["payload_hash"], token)).fetchone() is not None

    @staticmethod
    def _workflow_review_completed(connection: sqlite3.Connection, row: sqlite3.Row) -> bool:
        from hub.domain.workflow_errors import WorkflowValidationError
        from hub.domain.workflow_identity import canonical_digest
        from hub.domain.workflow_review import ReviewAdapterSpec

        config = _decode(row["config_snapshot"])
        if set(config) != {"owner_id", "workspace_id", "workflow_dispatch", "workflow_review"} or config["owner_id"] != row["owner_id"]:
            return False
        try:
            ReviewAdapterSpec.parse(config["workflow_review"])
        except WorkflowValidationError:
            return False
        marker = config["workflow_dispatch"]
        if type(marker) is not dict or set(marker) != {"token", "payload_hash", "config_digest"} or any(
                type(value) is not str or len(value) != 64 for value in marker.values()):
            return False
        token = "delivery:" + canonical_digest({"token": marker["token"], "operation": "review"})
        return connection.execute("SELECT 1 FROM workflow_dispatches d JOIN workflow_operations o "
            "USING(owner_id,workspace_id,workflow_id) WHERE d.owner_id=? AND d.workspace_id=? "
            "AND d.binding_kind='run' AND d.binding_id=? AND d.binding_run_id=? AND d.step_key='review' "
            "AND d.stage='bound' AND d.token=? AND d.payload_hash=? AND o.client_token=? LIMIT 1",
            (row["owner_id"], config["workspace_id"], row["run_id"], row["run_id"], marker["token"], marker["payload_hash"], token)).fetchone() is not None

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
            try:
                checkpoint = decode_checkpoint(row["runtime_checkpoint"])
            except PlatformRepositoryError:
                checkpoint = None
            native_completed = bool(
                checkpoint and checkpoint['phase'] == 'complete'
                and checkpoint['result'].get('state') == state
                and checkpoint['result'].get('text') == str(result_text or '')[:32768])
            if bool(row["cancel_requested"]) and state != "unknown" and not (
                    native_completed or (state == "succeeded" and (
                        self._workflow_test_completed(conn, row) or self._workflow_review_completed(conn, row)))):
                state = "cancelled"
            if usage is None:
                usage_values = tuple(int(row[f"usage_{key}"] or 0) for key in (
                    "input_tokens", "output_tokens", "total_tokens", "provider_requests"))
            else:
                usage_values = tuple(max(0, int(usage.get(key, 0) or 0)) for key in (
                    "input_tokens", "output_tokens", "total_tokens", "provider_requests"))
            conn.execute(
                "UPDATE runs SET runtime_wait_command_id=NULL,state=?,result_text=?,usage_input_tokens=?,usage_output_tokens=?,usage_total_tokens=?,usage_provider_requests=?,lease_id=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=?"
                " WHERE owner_id=? AND run_id=? AND lease_id=? AND lease_owner=?",
                (state, str(result_text or "")[:32768], *usage_values, float(now), owner_id, run_id, lease_id, worker_id),
            )
            if native_completed and conn.execute(
                    "SELECT 1 FROM run_events WHERE run_id=? AND kind='run_finished' LIMIT 1", (run_id,)).fetchone() is None:
                sequence = conn.execute(
                    'SELECT COALESCE(MAX(sequence),0)+1 FROM run_events WHERE run_id=?', (run_id,)).fetchone()[0]
                conn.execute('INSERT INTO run_events(run_id,sequence,kind,payload,created_at) VALUES(?,?,?,?,?)',
                    (run_id, sequence, 'run_finished', _json({'run_id': run_id, 'state': state,
                     'steps': checkpoint['result']['steps'], 'usage': checkpoint['result']['usage']}), float(now)))
            freeze_run_request_summary(conn, run_id, state)
            row = conn.execute("SELECT * FROM runs WHERE owner_id=? AND run_id=?", (owner_id, run_id)).fetchone()
            conn.execute("COMMIT")
            result = self._row_run(row)
            if result_text:
                result["result_text"] = str(result_text)[:32768]
            return result
        except PlatformRepositoryError:
            raise
        except sqlite3.Error as exc:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise PlatformRepositoryError("platform_store") from exc
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
            if state in TERMINAL_STATES:
                conn.execute('UPDATE runs SET runtime_wait_command_id=NULL,runtime_resume_at=NULL WHERE run_id=?', (run_id,))
                freeze_run_request_summary(conn, run_id, state)
            else:
                conn.execute("UPDATE runs SET request_summary=NULL WHERE run_id=?", (run_id,))
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
