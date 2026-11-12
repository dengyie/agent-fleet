"""Worker owner fairness leases and concurrency slots for ``PlatformRepository``."""
from __future__ import annotations

import secrets
import sqlite3
from typing import Any

from hub.infrastructure.platform_db_base import PlatformRepositoryError
from hub.infrastructure.run_capacity import workspace_capacity_filter
from platform_schema import validate_id, validate_owner_id


class PlatformWorkerMixin:
    def claim_worker_owner(self, *, worker_id: str, now: float,
                           lease_s: float = 60.0,
                           max_workspace_concurrency: int | None = None) -> dict[str, Any] | None:
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
            capacity_sql, capacity_params = workspace_capacity_filter(max_workspace_concurrency, now)
            row = conn.execute(
                "SELECT o.owner_id FROM platform_worker_owners o "
                "WHERE o.enabled=1 AND (o.lease_expires_at IS NULL OR o.lease_expires_at<=?) "
                "AND EXISTS (SELECT 1 FROM runs r WHERE r.owner_id=o.owner_id AND ("
                "(r.state='waiting_node' AND r.runtime_resume_at<=?) OR (r.state='queued' AND (r.lease_id IS NULL OR r.lease_expires_at IS NULL OR r.lease_expires_at<=?)) OR "
                "(r.state='cancelling' AND (r.lease_expires_at IS NULL OR r.lease_expires_at<=?)) OR "
                "(r.state='running' AND r.lease_expires_at IS NOT NULL AND r.lease_expires_at<=?) OR "
                "EXISTS (SELECT 1 FROM legacy_task_links l WHERE l.owner_id=o.owner_id AND l.state IN ('pending','linked')"
                " OR (l.state='leased' AND (l.lease_expires_at IS NULL OR l.lease_expires_at<=?)))"
                ")" + capacity_sql +
                ") ORDER BY (o.last_claim_at IS NOT NULL), o.last_claim_at, o.owner_id LIMIT 1",
                (now, now, now, now, now, now, *capacity_params),
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
                waiting_workspace = int(conn.execute(
                    "SELECT COUNT(*) FROM runs w WHERE w.run_id!=? AND w.runtime_wait_command_id IS NOT NULL "
                    "AND NOT EXISTS (SELECT 1 FROM platform_worker_slots ws WHERE ws.run_id=w.run_id AND ws.expires_at>?) "
                    "AND w.owner_id || ':' || "
                    "COALESCE(NULLIF(json_extract(w.config_snapshot,'$.workspace_id'),''),"
                    "(SELECT c.workspace_id FROM conversations c WHERE c.conversation_id=w.conversation_id))=?",
                    (run_id, float(now), workspace_key)).fetchone()[0])
                if active_workspace + waiting_workspace >= max_workspace_concurrency:
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
