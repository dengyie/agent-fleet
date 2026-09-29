"""Owner-scoped durable schedule definitions and trigger leases."""
from __future__ import annotations

import json
import math
import secrets
import sqlite3
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from platform_schema import validate_id, validate_owner_id

MIN_INTERVAL_S = 5.0
MAX_INTERVAL_S = 86400.0
MAX_TARGET_BYTES = 16 * 1024
MAX_RESULT_BYTES = 8 * 1024
SUPPORTED_ACTIONS = frozenset({"service_health", "http_probe"})
MISSED_POLICIES = frozenset({"skip", "catch_up"})
OVERLAP_POLICIES = frozenset({"skip", "coalesce"})
TERMINAL_TRIGGER_STATES = frozenset({"succeeded", "failed", "skipped", "coalesced"})


class PlatformScheduleRepositoryError(RuntimeError):
    def __init__(self, code: str):
        self.code = str(code)[:120]
        super().__init__(self.code)

    def __str__(self) -> str:
        return self.code


def _finite(value: Any, code: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise PlatformScheduleRepositoryError(code) from None
    if not math.isfinite(parsed) or parsed < 0:
        raise PlatformScheduleRepositoryError(code)
    return parsed


def _json(value: Any, *, limit: int, code: str) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        raise PlatformScheduleRepositoryError(code) from None
    if len(encoded.encode("utf-8")) > limit:
        raise PlatformScheduleRepositoryError("value_too_large")
    return encoded


def _decode(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return fallback


def _safe_rollback(conn) -> None:
    if conn is None:
        return
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _validate_timezone(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise PlatformScheduleRepositoryError("invalid_timezone")
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise PlatformScheduleRepositoryError("invalid_timezone") from None
    return value


def _validate_target(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PlatformScheduleRepositoryError("invalid_target")
    keys = set(value)
    if keys != {"service_id"}:
        raise PlatformScheduleRepositoryError("invalid_target")
    try:
        service_id = validate_id(value.get("service_id"), "service_id")
    except ValueError:
        raise PlatformScheduleRepositoryError("invalid_target") from None
    return {"service_id": service_id}


def _validate_definition(data: Mapping[str, Any], *, now: float | None = None) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise PlatformScheduleRepositoryError("invalid_schedule")
    try:
        schedule_id = validate_id(data.get("schedule_id"), "schedule_id")
    except ValueError:
        raise PlatformScheduleRepositoryError("invalid_id") from None
    name = data.get("name") or schedule_id
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        raise PlatformScheduleRepositoryError("invalid_name")
    action = data.get("action")
    if not isinstance(action, str) or action not in SUPPORTED_ACTIONS:
        raise PlatformScheduleRepositoryError("invalid_action")
    interval_s = _finite(data.get("interval_s"), "invalid_interval")
    if interval_s < MIN_INTERVAL_S or interval_s > MAX_INTERVAL_S:
        raise PlatformScheduleRepositoryError("invalid_interval")
    timezone = _validate_timezone(data.get("timezone") or "UTC")
    missed_policy = data.get("missed_policy") or "skip"
    if missed_policy not in MISSED_POLICIES:
        raise PlatformScheduleRepositoryError("invalid_missed_policy")
    overlap_policy = data.get("overlap_policy") or "skip"
    if overlap_policy not in OVERLAP_POLICIES:
        raise PlatformScheduleRepositoryError("invalid_overlap_policy")
    target = _validate_target(data.get("target") or {})
    next_run_at = data.get("next_run_at")
    if next_run_at is None:
        if now is None:
            raise PlatformScheduleRepositoryError("invalid_next_run")
        next_run_at = float(now) + interval_s
    next_run_at = _finite(next_run_at, "invalid_next_run")
    return {
        "schedule_id": schedule_id,
        "name": name.strip(),
        "action": action,
        "target": target,
        "interval_s": interval_s,
        "timezone": timezone,
        "missed_policy": missed_policy,
        "overlap_policy": overlap_policy,
        "next_run_at": next_run_at,
        "enabled": bool(data.get("enabled", True)),
    }


class PlatformScheduleRepository:
    """SQLite persistence for schedule definitions and unique trigger keys."""

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
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS platform_schedules (
                  owner_id TEXT NOT NULL,
                  schedule_id TEXT NOT NULL,
                  name TEXT NOT NULL,
                  action TEXT NOT NULL,
                  target TEXT NOT NULL,
                  interval_s REAL NOT NULL,
                  timezone TEXT NOT NULL,
                  missed_policy TEXT NOT NULL,
                  overlap_policy TEXT NOT NULL,
                  next_run_at REAL NOT NULL,
                  enabled INTEGER NOT NULL DEFAULT 1,
                  revision INTEGER NOT NULL DEFAULT 0,
                  consecutive_failures INTEGER NOT NULL DEFAULT 0,
                  last_run_at REAL,
                  last_success_at REAL,
                  last_error_code TEXT,
                  last_result TEXT NOT NULL DEFAULT '{}',
                  updated_at REAL NOT NULL,
                  PRIMARY KEY(owner_id, schedule_id)
                );
                CREATE TABLE IF NOT EXISTS platform_schedule_triggers (
                  owner_id TEXT NOT NULL,
                  schedule_id TEXT NOT NULL,
                  scheduled_at REAL NOT NULL,
                  state TEXT NOT NULL,
                  attempt INTEGER NOT NULL DEFAULT 0,
                  lease_id TEXT,
                  lease_owner TEXT,
                  lease_expires_at REAL,
                  started_at REAL,
                  finished_at REAL,
                  error_code TEXT,
                  last_result TEXT NOT NULL DEFAULT '{}',
                  updated_at REAL NOT NULL,
                  PRIMARY KEY(owner_id, schedule_id, scheduled_at),
                  FOREIGN KEY(owner_id, schedule_id)
                    REFERENCES platform_schedules(owner_id, schedule_id)
                    ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_platform_schedule_due
                  ON platform_schedules(enabled, next_run_at, owner_id);
                CREATE INDEX IF NOT EXISTS idx_platform_schedule_triggers_recent
                  ON platform_schedule_triggers(owner_id, schedule_id, scheduled_at DESC);
                """
            )
        except (sqlite3.Error, OSError):
            raise PlatformScheduleRepositoryError("schedule_store") from None
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _public_schedule(row) -> dict[str, Any]:
        return {
            "schedule_id": row["schedule_id"],
            "name": row["name"],
            "action": row["action"],
            "target": _decode(row["target"], {}),
            "interval_s": float(row["interval_s"]),
            "timezone": row["timezone"],
            "missed_policy": row["missed_policy"],
            "overlap_policy": row["overlap_policy"],
            "next_run_at": float(row["next_run_at"]),
            "enabled": bool(row["enabled"]),
            "revision": int(row["revision"]),
            "consecutive_failures": int(row["consecutive_failures"]),
            "last_run_at": row["last_run_at"],
            "last_success_at": row["last_success_at"],
            "last_error_code": row["last_error_code"],
            "last_result": _decode(row["last_result"], {}),
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _public_trigger(row) -> dict[str, Any]:
        return {
            "schedule_id": row["schedule_id"],
            "scheduled_at": float(row["scheduled_at"]),
            "state": row["state"],
            "attempt": int(row["attempt"]),
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "error_code": row["error_code"],
            "last_result": _decode(row["last_result"], {}),
            "updated_at": row["updated_at"],
        }

    def _owner_schedule(self, owner_id: str, schedule_id: str):
        try:
            return validate_owner_id(owner_id), validate_id(schedule_id, "schedule_id")
        except ValueError as exc:
            raise PlatformScheduleRepositoryError(getattr(exc, "code", "invalid_id")) from None

    def create(self, owner_id: str, data: Mapping[str, Any], *, now: float | None = None) -> dict[str, Any]:
        try:
            owner_id = validate_owner_id(owner_id)
        except ValueError:
            raise PlatformScheduleRepositoryError("invalid_owner") from None
        normalized = _validate_definition(data, now=self.clock() if now is None else now)
        timestamp = _finite(self.clock() if now is None else now, "invalid_time")
        encoded_target = _json(normalized["target"], limit=MAX_TARGET_BYTES, code="invalid_target")
        conn = None
        try:
            conn = self._connect()
            conn.execute(
                "INSERT INTO platform_schedules(owner_id,schedule_id,name,action,target,interval_s,timezone,missed_policy,overlap_policy,next_run_at,enabled,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (owner_id, normalized["schedule_id"], normalized["name"], normalized["action"],
                 encoded_target, normalized["interval_s"], normalized["timezone"],
                 normalized["missed_policy"], normalized["overlap_policy"],
                 normalized["next_run_at"], int(normalized["enabled"]), timestamp),
            )
            return self._public_schedule(conn.execute(
                "SELECT * FROM platform_schedules WHERE owner_id=? AND schedule_id=?",
                (owner_id, normalized["schedule_id"]),
            ).fetchone())
        except sqlite3.IntegrityError:
            raise PlatformScheduleRepositoryError("schedule_conflict") from None
        except (sqlite3.Error, OSError):
            raise PlatformScheduleRepositoryError("schedule_store") from None
        finally:
            if conn is not None:
                conn.close()

    def get(self, owner_id: str, schedule_id: str) -> dict[str, Any] | None:
        owner_id, schedule_id = self._owner_schedule(owner_id, schedule_id)
        conn = None
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM platform_schedules WHERE owner_id=? AND schedule_id=?",
                (owner_id, schedule_id),
            ).fetchone()
            return self._public_schedule(row) if row else None
        except sqlite3.Error:
            raise PlatformScheduleRepositoryError("schedule_store") from None
        finally:
            if conn is not None:
                conn.close()

    def list(self, owner_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        try:
            owner_id = validate_owner_id(owner_id)
            limit = max(1, min(int(limit), 100))
        except (ValueError, TypeError):
            raise PlatformScheduleRepositoryError("invalid_limit") from None
        conn = None
        try:
            conn = self._connect()
            rows = conn.execute(
                "SELECT * FROM platform_schedules WHERE owner_id=? ORDER BY schedule_id LIMIT ?",
                (owner_id, limit),
            ).fetchall()
            return [self._public_schedule(row) for row in rows]
        except sqlite3.Error:
            raise PlatformScheduleRepositoryError("schedule_store") from None
        finally:
            if conn is not None:
                conn.close()

    def list_owners(self, *, limit: int = 100) -> list[str]:
        try:
            limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            raise PlatformScheduleRepositoryError("invalid_limit") from None
        conn = None
        try:
            conn = self._connect()
            rows = conn.execute(
                "SELECT DISTINCT owner_id FROM platform_schedules "
                "WHERE enabled=1 ORDER BY owner_id LIMIT ?",
                (limit,),
            ).fetchall()
            return [str(row["owner_id"]) for row in rows]
        except sqlite3.Error:
            raise PlatformScheduleRepositoryError("schedule_store") from None
        finally:
            if conn is not None:
                conn.close()

    def update(
        self, owner_id: str, schedule_id: str, data: Mapping[str, Any], *,
        expected_revision: int | None = None, now: float | None = None,
    ) -> dict[str, Any]:
        owner_id, schedule_id = self._owner_schedule(owner_id, schedule_id)
        existing = self.get(owner_id, schedule_id)
        if existing is None:
            raise PlatformScheduleRepositoryError("schedule_not_found")
        merged = dict(existing)
        merged.update(dict(data or {}))
        merged["schedule_id"] = schedule_id
        normalized = _validate_definition(merged, now=now)
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision < 0
        ):
            raise PlatformScheduleRepositoryError("invalid_revision")
        timestamp = _finite(self.clock() if now is None else now, "invalid_time")
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT revision FROM platform_schedules WHERE owner_id=? AND schedule_id=?",
                (owner_id, schedule_id),
            ).fetchone()
            if row is None:
                raise PlatformScheduleRepositoryError("schedule_not_found")
            if expected_revision is not None and int(row["revision"]) != expected_revision:
                raise PlatformScheduleRepositoryError("revision_conflict")
            conn.execute(
                "UPDATE platform_schedules SET name=?,action=?,target=?,interval_s=?,timezone=?,missed_policy=?,overlap_policy=?,next_run_at=?,enabled=?,revision=revision+1,updated_at=? "
                "WHERE owner_id=? AND schedule_id=?",
                (normalized["name"], normalized["action"],
                 _json(normalized["target"], limit=MAX_TARGET_BYTES, code="invalid_target"),
                 normalized["interval_s"], normalized["timezone"], normalized["missed_policy"],
                 normalized["overlap_policy"], normalized["next_run_at"], int(normalized["enabled"]),
                 timestamp, owner_id, schedule_id),
            )
            saved = conn.execute(
                "SELECT * FROM platform_schedules WHERE owner_id=? AND schedule_id=?",
                (owner_id, schedule_id),
            ).fetchone()
            conn.execute("COMMIT")
            return self._public_schedule(saved)
        except PlatformScheduleRepositoryError:
            _safe_rollback(conn)
            raise
        except sqlite3.Error:
            _safe_rollback(conn)
            raise PlatformScheduleRepositoryError("schedule_store") from None
        finally:
            if conn is not None:
                conn.close()

    def delete(self, owner_id: str, schedule_id: str) -> bool:
        owner_id, schedule_id = self._owner_schedule(owner_id, schedule_id)
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            deleted = conn.execute(
                "DELETE FROM platform_schedules WHERE owner_id=? AND schedule_id=?",
                (owner_id, schedule_id),
            ).rowcount
            conn.execute("COMMIT")
            return bool(deleted)
        except sqlite3.Error:
            _safe_rollback(conn)
            raise PlatformScheduleRepositoryError("schedule_store") from None
        finally:
            if conn is not None:
                conn.close()

    def get_trigger(self, owner_id: str, schedule_id: str, scheduled_at: float) -> dict[str, Any] | None:
        owner_id, schedule_id = self._owner_schedule(owner_id, schedule_id)
        scheduled_at = _finite(scheduled_at, "invalid_scheduled_at")
        conn = None
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM platform_schedule_triggers WHERE owner_id=? AND schedule_id=? AND scheduled_at=?",
                (owner_id, schedule_id, scheduled_at),
            ).fetchone()
            return self._public_trigger(row) if row else None
        except sqlite3.Error:
            raise PlatformScheduleRepositoryError("schedule_store") from None
        finally:
            if conn is not None:
                conn.close()

    def get_active_trigger(
        self, owner_id: str, schedule_id: str, *, now: float | None = None,
    ) -> dict[str, Any] | None:
        owner_id, schedule_id = self._owner_schedule(owner_id, schedule_id)
        now = _finite(self.clock() if now is None else now, "invalid_time")
        conn = None
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM platform_schedule_triggers "
                "WHERE owner_id=? AND schedule_id=? AND state='running' "
                "AND lease_expires_at>? ORDER BY scheduled_at LIMIT 1",
                (owner_id, schedule_id, now),
            ).fetchone()
            return self._public_trigger(row) if row else None
        except sqlite3.Error:
            raise PlatformScheduleRepositoryError("schedule_store") from None
        finally:
            if conn is not None:
                conn.close()

    def claim_trigger(
        self, owner_id: str, schedule_id: str, *, scheduled_at: float,
        worker_id: str, now: float | None = None, lease_s: float = 60.0,
        advance_to: float | None = None,
    ) -> dict[str, Any] | None:
        owner_id, schedule_id = self._owner_schedule(owner_id, schedule_id)
        scheduled_at = _finite(scheduled_at, "invalid_scheduled_at")
        now = _finite(self.clock() if now is None else now, "invalid_time")
        lease_s = _finite(lease_s, "invalid_lease")
        if lease_s <= 0 or lease_s > 3600 or not isinstance(worker_id, str) or not worker_id or len(worker_id) > 128:
            raise PlatformScheduleRepositoryError("invalid_lease")
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            schedule = conn.execute(
                "SELECT * FROM platform_schedules WHERE owner_id=? AND schedule_id=?",
                (owner_id, schedule_id),
            ).fetchone()
            if schedule is None:
                raise PlatformScheduleRepositoryError("schedule_not_found")
            row = conn.execute(
                "SELECT * FROM platform_schedule_triggers WHERE owner_id=? AND schedule_id=? AND scheduled_at=?",
                (owner_id, schedule_id, scheduled_at),
            ).fetchone()
            if row is not None:
                if row["state"] in TERMINAL_TRIGGER_STATES:
                    conn.execute("ROLLBACK")
                    return None
                if row["lease_expires_at"] is not None and float(row["lease_expires_at"]) > now:
                    conn.execute("ROLLBACK")
                    return None
                attempt = int(row["attempt"]) + 1
                conn.execute(
                    "UPDATE platform_schedule_triggers SET state='running',attempt=?,lease_id=?,lease_owner=?,lease_expires_at=?,started_at=?,finished_at=NULL,error_code=NULL,updated_at=? "
                    "WHERE owner_id=? AND schedule_id=? AND scheduled_at=?",
                    (attempt, "lease_" + secrets.token_urlsafe(18), worker_id, now + lease_s,
                     now, now, owner_id, schedule_id, scheduled_at),
                )
            else:
                lease_id = "lease_" + secrets.token_urlsafe(18)
                conn.execute(
                    "INSERT INTO platform_schedule_triggers(owner_id,schedule_id,scheduled_at,state,attempt,lease_id,lease_owner,lease_expires_at,started_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (owner_id, schedule_id, scheduled_at, "running", 1, lease_id, worker_id,
                     now + lease_s, now, now),
                )
            saved = conn.execute(
                "SELECT * FROM platform_schedule_triggers WHERE owner_id=? AND schedule_id=? AND scheduled_at=?",
                (owner_id, schedule_id, scheduled_at),
            ).fetchone()
            conn.execute("COMMIT")
            public = self._public_trigger(saved)
            public["lease_id"] = saved["lease_id"]
            public["lease_expires_at"] = saved["lease_expires_at"]
            public["owner_id"] = owner_id
            return public
        except PlatformScheduleRepositoryError:
            _safe_rollback(conn)
            raise
        except sqlite3.IntegrityError:
            _safe_rollback(conn)
            return None
        except (sqlite3.Error, OSError):
            _safe_rollback(conn)
            raise PlatformScheduleRepositoryError("schedule_store") from None
        finally:
            if conn is not None:
                conn.close()

    def finish_trigger(
        self, owner_id: str, schedule_id: str, scheduled_at: float, lease_id: str, *,
        state: str, next_run_at: float, now: float | None = None,
        error_code: str | None = None, result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        owner_id, schedule_id = self._owner_schedule(owner_id, schedule_id)
        if state not in TERMINAL_TRIGGER_STATES:
            raise PlatformScheduleRepositoryError("invalid_trigger_state")
        if not isinstance(lease_id, str) or not lease_id or len(lease_id) > 256:
            raise PlatformScheduleRepositoryError("invalid_lease")
        scheduled_at = _finite(scheduled_at, "invalid_scheduled_at")
        next_run_at = _finite(next_run_at, "invalid_next_run")
        now = _finite(self.clock() if now is None else now, "invalid_time")
        encoded = _json(dict(result or {}), limit=MAX_RESULT_BYTES, code="invalid_result")
        safe_error = None if error_code is None else str(error_code)[:120]
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM platform_schedule_triggers WHERE owner_id=? AND schedule_id=? AND scheduled_at=? "
                "AND state='running' AND lease_id=? AND lease_expires_at>?",
                (owner_id, schedule_id, scheduled_at, lease_id, now),
            ).fetchone()
            if row is None:
                raise PlatformScheduleRepositoryError("lease_mismatch")
            success = state == "succeeded"
            previous_failures = int(conn.execute(
                "SELECT consecutive_failures FROM platform_schedules "
                "WHERE owner_id=? AND schedule_id=?",
                (owner_id, schedule_id),
            ).fetchone()["consecutive_failures"])
            failures = (
                0 if state == "succeeded"
                else previous_failures + 1 if state == "failed"
                else previous_failures
            )
            conn.execute(
                "UPDATE platform_schedule_triggers SET state=?,lease_id=NULL,lease_owner=NULL,lease_expires_at=NULL,finished_at=?,error_code=?,last_result=?,updated_at=? "
                "WHERE owner_id=? AND schedule_id=? AND scheduled_at=?",
                (state, now, safe_error, encoded, now, owner_id, schedule_id, scheduled_at),
            )
            conn.execute(
                "UPDATE platform_schedules SET next_run_at=CASE WHEN next_run_at>? THEN next_run_at ELSE ? END,"
                "consecutive_failures=?,last_run_at=?,last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END,"
                "last_error_code=?,last_result=?,updated_at=? "
                "WHERE owner_id=? AND schedule_id=?",
                (next_run_at, next_run_at, failures, now, int(state == "succeeded"),
                 now if state == "succeeded" else None,
                 safe_error if state == "failed" else None, encoded, now,
                 owner_id, schedule_id),
            )
            if success:
                conn.execute(
                    "UPDATE platform_schedules SET consecutive_failures=0,last_success_at=?,last_error_code=NULL "
                    "WHERE owner_id=? AND schedule_id=?",
                    (now, owner_id, schedule_id),
                )
            saved = conn.execute(
                "SELECT * FROM platform_schedule_triggers WHERE owner_id=? AND schedule_id=? AND scheduled_at=?",
                (owner_id, schedule_id, scheduled_at),
            ).fetchone()
            conn.execute("COMMIT")
            return self._public_trigger(saved)
        except PlatformScheduleRepositoryError:
            _safe_rollback(conn)
            raise
        except (sqlite3.Error, OSError):
            _safe_rollback(conn)
            raise PlatformScheduleRepositoryError("schedule_store") from None
        finally:
            if conn is not None:
                conn.close()

    def advance_schedule(
        self, owner_id: str, schedule_id: str, *, next_run_at: float,
        now: float | None = None, result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        owner_id, schedule_id = self._owner_schedule(owner_id, schedule_id)
        next_run_at = _finite(next_run_at, "invalid_next_run")
        timestamp = _finite(self.clock() if now is None else now, "invalid_time")
        encoded = _json(dict(result or {}), limit=MAX_RESULT_BYTES, code="invalid_result")
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "UPDATE platform_schedules SET next_run_at=?,last_run_at=?,last_result=?,updated_at=? "
                "WHERE owner_id=? AND schedule_id=?",
                (next_run_at, timestamp, encoded, timestamp, owner_id, schedule_id),
            ).rowcount
            if not changed:
                raise PlatformScheduleRepositoryError("schedule_not_found")
            row = conn.execute(
                "SELECT * FROM platform_schedules WHERE owner_id=? AND schedule_id=?",
                (owner_id, schedule_id),
            ).fetchone()
            conn.execute("COMMIT")
            return self._public_schedule(row)
        except PlatformScheduleRepositoryError:
            _safe_rollback(conn)
            raise
        except (sqlite3.Error, OSError):
            _safe_rollback(conn)
            raise PlatformScheduleRepositoryError("schedule_store") from None
        finally:
            if conn is not None:
                conn.close()

    def status(self, owner_id: str, schedule_id: str, *, limit: int = 100) -> dict[str, Any] | None:
        owner_id, schedule_id = self._owner_schedule(owner_id, schedule_id)
        schedule = self.get(owner_id, schedule_id)
        if schedule is None:
            return None
        conn = None
        try:
            conn = self._connect()
            rows = conn.execute(
                "SELECT * FROM platform_schedule_triggers WHERE owner_id=? AND schedule_id=? ORDER BY scheduled_at DESC LIMIT ?",
                (owner_id, schedule_id, max(1, min(int(limit), 100))),
            ).fetchall()
            schedule["triggers"] = [self._public_trigger(row) for row in rows]
            return schedule
        except (sqlite3.Error, ValueError):
            raise PlatformScheduleRepositoryError("schedule_store") from None
        finally:
            if conn is not None:
                conn.close()


__all__ = [
    "MAX_INTERVAL_S",
    "MIN_INTERVAL_S",
    "OVERLAP_POLICIES",
    "MISSED_POLICIES",
    "SUPPORTED_ACTIONS",
    "PlatformScheduleRepository",
    "PlatformScheduleRepositoryError",
]
