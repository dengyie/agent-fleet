"""Legacy task bridge (``legacy_task_links``) for ``PlatformRepository``."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from hub.domain.request_metadata import TERMINAL_STATES
from hub.infrastructure.platform_db_base import (
    PlatformRepositoryError, _json, freeze_run_request_summary,
)
from platform_schema import PlatformValidationError, validate_id, validate_owner_id


class PlatformLegacyTaskMixin:
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
                if run_state in TERMINAL_STATES:
                    freeze_run_request_summary(conn, run_id, run_state)
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
