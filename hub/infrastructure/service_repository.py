"""SQLite repository for service definitions and health evidence."""
from __future__ import annotations

import json
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping

from hub.domain.service import (
    HEALTH_DIMENSIONS,
    ServiceValidationError,
    validate_health_evidence,
    validate_service_definition,
)
from platform_schema import validate_id, validate_owner_id

MAX_DETAIL_BYTES = 64 * 1024


def _reject_json_constant(token: str) -> None:
    raise ValueError(f"non-finite JSON constant: {token}")


def _safe_rollback(conn) -> None:
    if conn is None:
        return
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


class ServiceRepositoryError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


class ServiceRepository:
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
            CREATE TABLE IF NOT EXISTS service_definitions (
              owner_id TEXT NOT NULL, service_id TEXT NOT NULL,
              node_id TEXT NOT NULL, name TEXT NOT NULL, adapter TEXT NOT NULL,
              target_alias TEXT NOT NULL, checks TEXT NOT NULL DEFAULT '{}',
              control_authority TEXT, allowed_actions TEXT NOT NULL DEFAULT '[]',
              version INTEGER NOT NULL DEFAULT 1,
              enabled INTEGER NOT NULL DEFAULT 1, updated_at REAL NOT NULL,
              PRIMARY KEY(owner_id, service_id)
            );
            CREATE TABLE IF NOT EXISTS service_health_evidence (
              evidence_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
              service_id TEXT NOT NULL, dimension TEXT NOT NULL,
              source TEXT NOT NULL, state TEXT NOT NULL,
              observed_at REAL NOT NULL, received_at REAL NOT NULL,
              ttl_s REAL NOT NULL, detail TEXT NOT NULL DEFAULT '{}',
              UNIQUE(owner_id, service_id, dimension, source, observed_at)
            );
            CREATE INDEX IF NOT EXISTS idx_service_owner ON service_definitions(owner_id, service_id);
            CREATE INDEX IF NOT EXISTS idx_service_evidence_latest ON service_health_evidence(owner_id, service_id, dimension, observed_at DESC);
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(service_definitions)")}
            if "allowed_actions" not in columns:
                conn.execute(
                    "ALTER TABLE service_definitions ADD COLUMN allowed_actions TEXT NOT NULL DEFAULT '[]'"
                )
        except (sqlite3.Error, OSError):
            raise ServiceRepositoryError("service_store") from None
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _json(value: Any) -> str:
        try:
            encoded = json.dumps(
                {} if value is None else value, ensure_ascii=True, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            )
        except (TypeError, ValueError):
            raise ServiceRepositoryError("invalid_evidence_detail") from None
        if len(encoded.encode("utf-8")) > MAX_DETAIL_BYTES:
            raise ServiceRepositoryError("evidence_detail_too_large")
        return encoded

    @staticmethod
    def _decode(value: str | None) -> dict[str, Any]:
        try:
            decoded = json.loads(value or "{}", parse_constant=_reject_json_constant)
            if not isinstance(decoded, Mapping):
                raise ValueError("expected object")
            return dict(decoded)
        except (TypeError, ValueError) as exc:
            raise ServiceRepositoryError("service_store") from exc

    @staticmethod
    def _decode_list(value: str | None) -> list[str]:
        try:
            decoded = json.loads(value or "[]", parse_constant=_reject_json_constant)
            if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
                raise ValueError("expected string array")
            return decoded
        except (TypeError, ValueError) as exc:
            raise ServiceRepositoryError("service_store") from exc

    @classmethod
    def _service_row(cls, row) -> dict[str, Any]:
        return {
            "owner_id": row["owner_id"], "service_id": row["service_id"],
            "node_id": row["node_id"], "name": row["name"],
            "adapter": row["adapter"], "target_alias": row["target_alias"],
            "checks": cls._decode(row["checks"]),
            "control_authority": row["control_authority"],
            "allowed_actions": cls._decode_list(row["allowed_actions"]),
            "version": int(row["version"]), "enabled": bool(row["enabled"]),
            "updated_at": row["updated_at"],
        }

    @classmethod
    def _evidence_row(cls, row) -> dict[str, Any]:
        return {
            "evidence_id": row["evidence_id"], "owner_id": row["owner_id"],
            "service_id": row["service_id"], "dimension": row["dimension"],
            "source": row["source"], "state": row["state"],
            "observed_at": row["observed_at"], "received_at": row["received_at"],
            "ttl_s": row["ttl_s"], "detail": cls._decode(row["detail"]),
        }

    def upsert_service(self, owner_id: str, data: Mapping[str, Any]) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        try:
            normalized = validate_service_definition(data)
        except ServiceValidationError:
            raise
        conn = None
        try:
            conn = self._connect()
            node = conn.execute(
                "SELECT enabled FROM nodes WHERE owner_id=? AND node_id=?",
                (owner_id, normalized["node_id"]),
            ).fetchone()
            if not node or not bool(node["enabled"]):
                raise ServiceRepositoryError("reference_forbidden")
            existing = conn.execute(
                "SELECT version FROM service_definitions WHERE owner_id=? AND service_id=?",
                (owner_id, normalized["service_id"]),
            ).fetchone()
            version = int(normalized["version"])
            if existing:
                version = max(version, int(existing["version"]) + 1)
            now = float(self.clock())
            conn.execute(
                "INSERT INTO service_definitions(owner_id,service_id,node_id,name,adapter,target_alias,checks,control_authority,allowed_actions,version,enabled,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(owner_id,service_id) DO UPDATE SET node_id=excluded.node_id,name=excluded.name,adapter=excluded.adapter,target_alias=excluded.target_alias,checks=excluded.checks,control_authority=excluded.control_authority,allowed_actions=excluded.allowed_actions,version=excluded.version,enabled=excluded.enabled,updated_at=excluded.updated_at",
                (owner_id, normalized["service_id"], normalized["node_id"], normalized["name"], normalized["adapter"], normalized["target_alias"], self._json(normalized["checks"]), normalized["control_authority"], self._json(normalized["allowed_actions"]), version, int(normalized["enabled"]), now),
            )
            row = conn.execute("SELECT * FROM service_definitions WHERE owner_id=? AND service_id=?", (owner_id, normalized["service_id"])).fetchone()
            return self._service_row(row)
        except (ServiceRepositoryError, ServiceValidationError):
            raise
        except (sqlite3.Error, OSError):
            raise ServiceRepositoryError("service_store") from None
        finally:
            if conn is not None:
                conn.close()

    def get_service(self, owner_id: str, service_id: str) -> dict[str, Any] | None:
        owner_id = validate_owner_id(owner_id)
        service_id = validate_id(service_id, "service_id")
        conn = None
        try:
            conn = self._connect()
            row = conn.execute("SELECT * FROM service_definitions WHERE owner_id=? AND service_id=?", (owner_id, service_id)).fetchone()
            return self._service_row(row) if row else None
        except (sqlite3.Error, KeyError):
            raise ServiceRepositoryError("service_store") from None
        finally:
            if conn is not None:
                conn.close()

    def list_services(self, owner_id: str) -> list[dict[str, Any]]:
        owner_id = validate_owner_id(owner_id)
        conn = None
        try:
            conn = self._connect()
            rows = conn.execute("SELECT * FROM service_definitions WHERE owner_id=? ORDER BY service_id", (owner_id,)).fetchall()
            return [self._service_row(row) for row in rows]
        except sqlite3.Error:
            raise ServiceRepositoryError("service_store") from None
        finally:
            if conn is not None:
                conn.close()

    def list_monitoring_owners(self) -> list[str]:
        """Return owners with registered enabled services in stable order."""
        conn = None
        try:
            conn = self._connect()
            rows = conn.execute(
                "SELECT DISTINCT owner_id FROM service_definitions WHERE enabled=1 ORDER BY owner_id"
            ).fetchall()
            return [str(row["owner_id"]) for row in rows]
        except sqlite3.Error:
            raise ServiceRepositoryError("service_store") from None
        finally:
            if conn is not None:
                conn.close()

    def get_node_status(self, owner_id: str, node_id: str) -> dict[str, Any] | None:
        owner_id = validate_owner_id(owner_id)
        node_id = validate_id(node_id, "node_id")
        conn = None
        try:
            conn = self._connect()
            row = conn.execute("SELECT node_id,status,last_seen_at,enabled FROM nodes WHERE owner_id=? AND node_id=?", (owner_id, node_id)).fetchone()
            return dict(row) if row else None
        except sqlite3.Error:
            raise ServiceRepositoryError("service_store") from None
        finally:
            if conn is not None:
                conn.close()

    def record_evidence(
        self,
        owner_id: str,
        data: Mapping[str, Any],
        *,
        expected_service_version: int | None = None,
    ) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        if expected_service_version is not None and (
            type(expected_service_version) is not int
            or not 1 <= expected_service_version <= 1_000_000
        ):
            raise ServiceRepositoryError("invalid_service_version")
        now = float(self.clock())
        try:
            normalized = validate_health_evidence(data, now=now)
        except ServiceValidationError:
            raise
        evidence_id = normalized["evidence_id"] or "ev_" + secrets.token_urlsafe(18)
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            service = conn.execute(
                "SELECT enabled,version FROM service_definitions WHERE owner_id=? AND service_id=?",
                (owner_id, normalized["service_id"]),
            ).fetchone()
            if not service or not bool(service["enabled"]):
                raise ServiceRepositoryError("service_not_found")
            if (
                expected_service_version is not None
                and int(service["version"]) != expected_service_version
            ):
                raise ServiceRepositoryError("service_version_conflict")
            existing = conn.execute("SELECT * FROM service_health_evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
            if existing:
                if existing["owner_id"] != owner_id or existing["service_id"] != normalized["service_id"]:
                    raise ServiceRepositoryError("evidence_conflict")
                conn.execute("COMMIT")
                return self._evidence_row(existing)
            conn.execute(
                "INSERT INTO service_health_evidence(evidence_id,owner_id,service_id,dimension,source,state,observed_at,received_at,ttl_s,detail) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (evidence_id, owner_id, normalized["service_id"], normalized["dimension"], normalized["source"], normalized["state"], normalized["observed_at"], now, normalized["ttl_s"], self._json(normalized["detail"])),
            )
            row = conn.execute("SELECT * FROM service_health_evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
            conn.execute("COMMIT")
            return self._evidence_row(row)
        except (ServiceRepositoryError, ServiceValidationError):
            _safe_rollback(conn)
            raise
        except sqlite3.IntegrityError:
            _safe_rollback(conn)
            raise ServiceRepositoryError("evidence_conflict") from None
        except (sqlite3.Error, OSError):
            _safe_rollback(conn)
            raise ServiceRepositoryError("service_store") from None
        finally:
            if conn is not None:
                conn.close()

    def list_evidence(self, owner_id: str, service_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        owner_id = validate_owner_id(owner_id)
        service_id = validate_id(service_id, "service_id")
        try:
            limit = max(1, min(int(limit), 500))
        except (TypeError, ValueError):
            raise ServiceRepositoryError("invalid_limit") from None
        conn = None
        try:
            conn = self._connect()
            rows = conn.execute("SELECT * FROM service_health_evidence WHERE owner_id=? AND service_id=? ORDER BY observed_at DESC,evidence_id DESC LIMIT ?", (owner_id, service_id, limit)).fetchall()
            return [self._evidence_row(row) for row in rows]
        except sqlite3.Error:
            raise ServiceRepositoryError("service_store") from None
        finally:
            if conn is not None:
                conn.close()

    def discard_versioned_evidence(
        self, owner_id: str, service_id: str, evidence_id: str, *,
        expected_service_version: int,
    ) -> bool:
        """Delete an in-flight probe sample only after its version is stale."""
        owner_id = validate_owner_id(owner_id)
        service_id = validate_id(service_id, "service_id")
        evidence_id = validate_id(evidence_id, "evidence_id")
        if type(expected_service_version) is not int or not 1 <= expected_service_version <= 1_000_000:
            raise ServiceRepositoryError("invalid_service_version")
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            service = conn.execute(
                "SELECT version FROM service_definitions WHERE owner_id=? AND service_id=?",
                (owner_id, service_id),
            ).fetchone()
            if service is not None and int(service["version"]) == expected_service_version:
                conn.execute("COMMIT")
                return False
            deleted = conn.execute(
                "DELETE FROM service_health_evidence WHERE evidence_id=? AND owner_id=? AND service_id=?",
                (evidence_id, owner_id, service_id),
            ).rowcount
            conn.execute("COMMIT")
            return bool(deleted)
        except ServiceRepositoryError:
            _safe_rollback(conn)
            raise
        except (sqlite3.Error, OSError):
            _safe_rollback(conn)
            raise ServiceRepositoryError("service_store") from None
        finally:
            if conn is not None:
                conn.close()

__all__ = ["MAX_DETAIL_BYTES", "ServiceRepository", "ServiceRepositoryError"]
