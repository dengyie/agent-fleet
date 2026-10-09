"""Durable, owner-scoped Incident state derived from health evidence."""
from __future__ import annotations

import json
import math
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping

from hub.domain.incident import IncidentValidationError, validate_incident_input
from platform_schema import validate_id, validate_owner_id

MAX_EVIDENCE_IDS = 100
MAX_DETAIL_BYTES = 64 * 1024
_INCIDENT_STATES = frozenset({"unhealthy", "degraded", "unknown", "stale", "unsupported"})


class IncidentRepositoryError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _reject_json_constant(token: str) -> None:
    raise ValueError(f"non-finite JSON constant: {token}")


def _json_ids(value: list[str]) -> str:
    return json.dumps(value[-MAX_EVIDENCE_IDS:], ensure_ascii=True, separators=(",", ":"))


def _decode_ids(value: str | None) -> list[str]:
    try:
        decoded = json.loads(value or "[]", parse_constant=_reject_json_constant)
        if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
            raise ValueError("expected string array")
        return decoded[-MAX_EVIDENCE_IDS:]
    except (TypeError, ValueError) as exc:
        raise IncidentRepositoryError("incident_store") from exc


def _decode_detail(value: str | None) -> dict[str, Any]:
    try:
        decoded = json.loads(value or "{}", parse_constant=_reject_json_constant)
        if not isinstance(decoded, Mapping):
            raise ValueError("expected object")
        return dict(decoded)
    except (TypeError, ValueError) as exc:
        raise IncidentRepositoryError("incident_store") from exc


def _bounded_observed_at(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise IncidentRepositoryError("invalid_observed_at") from None
    if not math.isfinite(parsed) or parsed < 0:
        raise IncidentRepositoryError("invalid_observed_at")
    return parsed


def _bounded_source(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 120:
        raise IncidentRepositoryError("invalid_source")
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in value):
        raise IncidentRepositoryError("invalid_source")
    return value.strip()


def _bounded_evidence_id(value: Any) -> str | None:
    if value is None or value == "":
        return None
    try:
        return validate_id(value, "evidence_id")
    except ValueError:
        raise IncidentRepositoryError("invalid_evidence_id") from None


def _detail_json(detail: Mapping[str, Any] | None) -> str:
    if detail is not None and not isinstance(detail, Mapping):
        raise IncidentRepositoryError("invalid_detail")
    try:
        encoded = json.dumps(
            dict(detail or {}), ensure_ascii=True, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        )
    except (TypeError, ValueError):
        raise IncidentRepositoryError("invalid_detail") from None
    if len(encoded.encode("utf-8")) > MAX_DETAIL_BYTES:
        raise IncidentRepositoryError("detail_too_large")
    return encoded


def _safe_rollback(conn) -> None:
    if conn is None:
        return
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _validate_service_version(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= 1_000_000:
        raise IncidentRepositoryError("invalid_service_version")
    return value


class IncidentRepository:
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
            CREATE TABLE IF NOT EXISTS incidents (
              incident_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, service_id TEXT NOT NULL,
              rule_id TEXT NOT NULL, fingerprint TEXT NOT NULL, failure_class TEXT NOT NULL,
              state TEXT NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
              last_evidence_at REAL NOT NULL, closed_at REAL, recovery_streak INTEGER NOT NULL DEFAULT 0,
              recovery_required INTEGER NOT NULL DEFAULT 2, evidence_ids TEXT NOT NULL DEFAULT '[]',
              latest_state TEXT NOT NULL, latest_source TEXT NOT NULL, latest_detail TEXT NOT NULL DEFAULT '{}',
              created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_incidents_open_fingerprint
              ON incidents(owner_id, fingerprint) WHERE state='open';
            CREATE INDEX IF NOT EXISTS idx_incidents_owner_state
              ON incidents(owner_id, state, last_seen DESC);
            CREATE INDEX IF NOT EXISTS idx_incidents_service
              ON incidents(owner_id, service_id, last_seen DESC);
            """)
        except (sqlite3.Error, OSError):
            raise IncidentRepositoryError("incident_store") from None
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _row(row) -> dict[str, Any]:
        if row is None:
            return None
        detail = _decode_detail(row["latest_detail"])
        return {
            "incident_id": row["incident_id"], "owner_id": row["owner_id"],
            "service_id": row["service_id"], "rule_id": row["rule_id"],
            "fingerprint": row["fingerprint"], "failure_class": row["failure_class"],
            "state": row["state"], "first_seen": row["first_seen"],
            "last_seen": row["last_seen"], "last_evidence_at": row["last_evidence_at"],
            "closed_at": row["closed_at"], "recovery_streak": int(row["recovery_streak"]),
            "recovery_required": int(row["recovery_required"]),
            "evidence_ids": _decode_ids(row["evidence_ids"]),
            "latest_state": row["latest_state"], "latest_source": row["latest_source"],
            "latest_detail": dict(detail), "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _find_open(self, conn, owner_id: str, fingerprint: str):
        return conn.execute(
            "SELECT * FROM incidents WHERE owner_id=? AND fingerprint=? AND state='open'",
            (owner_id, fingerprint),
        ).fetchone()

    def open_or_update(
        self, owner_id: str, data: Mapping[str, Any], *, observed_at: float,
        source: str, state: str, detail: Mapping[str, Any] | None = None,
        rule_id: str = "health", recovery_required: int = 2,
        expected_service_version: int | None = None,
    ) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        try:
            normalized = validate_incident_input(data)
        except IncidentValidationError:
            raise
        source = _bounded_source(source)
        if not isinstance(state, str) or state not in _INCIDENT_STATES:
            raise IncidentRepositoryError("invalid_state")
        observed_at = _bounded_observed_at(observed_at)
        try:
            required = max(1, min(10, int(recovery_required)))
        except (TypeError, ValueError):
            raise IncidentRepositoryError("invalid_recovery_required") from None
        now = float(self.clock())
        detail_json = _detail_json(detail)
        expected_service_version = _validate_service_version(expected_service_version)
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            if expected_service_version is not None:
                service = conn.execute(
                    "SELECT enabled,version FROM service_definitions WHERE owner_id=? AND service_id=?",
                    (owner_id, normalized["service_id"]),
                ).fetchone()
                if (
                    not service or not bool(service["enabled"])
                    or int(service["version"]) != expected_service_version
                ):
                    raise IncidentRepositoryError("service_version_conflict")
            row = self._find_open(conn, owner_id, normalized["fingerprint"])
            if row is None:
                # ``validate_id`` rejects security-sensitive marker words such
                # as ``key`` and ``token``.  URL-safe random text can contain
                # those substrings by chance, which would make a persisted
                # incident impossible to read back.  Hex is opaque, bounded,
                # and cannot contain any of the rejected alphabetic markers.
                incident_id = "inc_" + secrets.token_hex(18)
                evidence_ids = [normalized["evidence_id"]] if normalized["evidence_id"] else []
                conn.execute(
                    "INSERT INTO incidents(incident_id,owner_id,service_id,rule_id,fingerprint,failure_class,state,first_seen,last_seen,last_evidence_at,closed_at,recovery_streak,recovery_required,evidence_ids,latest_state,latest_source,latest_detail,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (incident_id, owner_id, normalized["service_id"], rule_id, normalized["fingerprint"], normalized["failure_class"], "open", observed_at, observed_at, observed_at, None, 0, required, _json_ids(evidence_ids), state, source, detail_json, now, now),
                )
            else:
                incident_id = row["incident_id"]
                evidence_ids = _decode_ids(row["evidence_ids"])
                if normalized["evidence_id"] and normalized["evidence_id"] not in evidence_ids:
                    evidence_ids.append(normalized["evidence_id"]); evidence_ids = evidence_ids[-MAX_EVIDENCE_IDS:]
                # An older external sample may be stored for audit, but it may
                # never roll incident timestamps or latest state backwards.
                if float(observed_at) >= float(row["last_evidence_at"]):
                    conn.execute(
                        "UPDATE incidents SET last_seen=?,last_evidence_at=?,failure_class=?,recovery_streak=0,recovery_required=?,evidence_ids=?,latest_state=?,latest_source=?,latest_detail=?,updated_at=? WHERE incident_id=?",
                        (max(float(row["last_seen"]), float(observed_at)), float(observed_at), normalized["failure_class"], required, _json_ids(evidence_ids), state, source, detail_json, now, incident_id),
                    )
                else:
                    conn.execute("UPDATE incidents SET evidence_ids=?,updated_at=? WHERE incident_id=?", (_json_ids(evidence_ids), now, incident_id))
            conn.execute("COMMIT")
            row = conn.execute("SELECT * FROM incidents WHERE incident_id=?", (incident_id,)).fetchone()
            return self._row(row)
        except (IncidentRepositoryError, IncidentValidationError):
            _safe_rollback(conn)
            raise
        except (sqlite3.Error, OSError):
            _safe_rollback(conn)
            raise IncidentRepositoryError("incident_store") from None
        finally:
            if conn is not None:
                conn.close()

    def recover(
        self, owner_id: str, *, fingerprint: str, observed_at: float,
        evidence_id: str | None, source: str, state: str, detail: Mapping[str, Any] | None = None,
        recovery_required: int = 2,
        service_id: str | None = None,
        expected_service_version: int | None = None,
    ) -> dict[str, Any] | None:
        owner_id = validate_owner_id(owner_id)
        if not isinstance(fingerprint, str) or not fingerprint or len(fingerprint) > 256:
            raise IncidentRepositoryError("invalid_fingerprint")
        evidence_id = _bounded_evidence_id(evidence_id)
        source = _bounded_source(source)
        if state not in {"healthy", *_INCIDENT_STATES}:
            raise IncidentRepositoryError("invalid_state")
        observed_at = _bounded_observed_at(observed_at)
        detail_json = _detail_json(detail)
        try:
            required = max(1, min(10, int(recovery_required)))
        except (TypeError, ValueError):
            raise IncidentRepositoryError("invalid_recovery_required") from None
        now = float(self.clock())
        if service_id is not None:
            try:
                service_id = validate_id(service_id, "service_id")
            except ValueError:
                raise IncidentRepositoryError("invalid_service_id") from None
        if expected_service_version is not None and service_id is None:
            raise IncidentRepositoryError("invalid_service_id")
        expected_service_version = _validate_service_version(expected_service_version)
        conn = None
        try:
            conn = self._connect(); conn.execute("BEGIN IMMEDIATE")
            if expected_service_version is not None:
                service = conn.execute(
                    "SELECT enabled,version FROM service_definitions WHERE owner_id=? AND service_id=?",
                    (owner_id, service_id),
                ).fetchone()
                if (
                    not service or not bool(service["enabled"])
                    or int(service["version"]) != expected_service_version
                ):
                    raise IncidentRepositoryError("service_version_conflict")
            row = self._find_open(conn, owner_id, fingerprint)
            if row is None:
                conn.execute("COMMIT"); return None
            evidence_ids = _decode_ids(row["evidence_ids"])
            if evidence_id and evidence_id not in evidence_ids:
                evidence_ids.append(evidence_id)
            # Reordered healthy samples cannot close an incident whose latest
            # known failure is newer.
            if float(observed_at) < float(row["last_evidence_at"]):
                conn.execute("UPDATE incidents SET evidence_ids=?,updated_at=? WHERE incident_id=?", (_json_ids(evidence_ids), now, row["incident_id"]))
            else:
                streak = int(row["recovery_streak"]) + 1 if state == "healthy" else 0
                closed_at = now if state == "healthy" and streak >= required else None
                next_state = "closed" if closed_at is not None else "open"
                conn.execute(
                    "UPDATE incidents SET last_seen=?,last_evidence_at=?,closed_at=?,state=?,recovery_streak=?,recovery_required=?,evidence_ids=?,latest_state=?,latest_source=?,latest_detail=?,updated_at=? WHERE incident_id=?",
                    (max(float(row["last_seen"]), float(observed_at)), float(observed_at), closed_at, next_state, streak, required, _json_ids(evidence_ids), state, source, detail_json, now, row["incident_id"]),
                )
            conn.execute("COMMIT")
            return self._row(conn.execute("SELECT * FROM incidents WHERE incident_id=?", (row["incident_id"],)).fetchone())
        except IncidentRepositoryError:
            _safe_rollback(conn)
            raise
        except ValueError:
            _safe_rollback(conn)
            raise IncidentRepositoryError("incident_store") from None
        except (sqlite3.Error, OSError):
            _safe_rollback(conn)
            raise IncidentRepositoryError("incident_store") from None
        finally:
            if conn is not None:
                conn.close()

    def get(self, owner_id: str, incident_id: str) -> dict[str, Any] | None:
        owner_id = validate_owner_id(owner_id); incident_id = validate_id(incident_id, "incident_id")
        conn = None
        try:
            conn = self._connect()
            return self._row(conn.execute("SELECT * FROM incidents WHERE owner_id=? AND incident_id=?", (owner_id, incident_id)).fetchone())
        except (sqlite3.Error, OSError):
            raise IncidentRepositoryError("incident_store") from None
        finally:
            if conn is not None: conn.close()

    def list(self, owner_id: str, *, state: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        owner_id = validate_owner_id(owner_id)
        if state is not None and state not in {"open", "closed"}:
            raise IncidentRepositoryError("invalid_state")
        try: limit = max(1, min(500, int(limit)))
        except (TypeError, ValueError): raise IncidentRepositoryError("invalid_limit") from None
        conn = None
        try:
            conn = self._connect()
            if state is None:
                rows = conn.execute("SELECT * FROM incidents WHERE owner_id=? ORDER BY (state='open') DESC,last_seen DESC LIMIT ?", (owner_id, limit)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM incidents WHERE owner_id=? AND state=? ORDER BY last_seen DESC LIMIT ?", (owner_id, state, limit)).fetchall()
            return [self._row(row) for row in rows]
        except sqlite3.Error:
            raise IncidentRepositoryError("incident_store") from None
        finally:
            if conn is not None: conn.close()


__all__ = ["IncidentRepository", "IncidentRepositoryError", "MAX_EVIDENCE_IDS"]
