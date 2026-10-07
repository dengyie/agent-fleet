"""Durable leases and bounded status for platform background jobs."""
from __future__ import annotations

import json
import math
import secrets
import sqlite3
import time
from collections.abc import Mapping
from pathlib import Path
import re
from typing import Any


MAX_RESULT_BYTES = 16 * 1024
_ERROR_CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")


class PlatformSchedulerRepositoryError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _reject_json_constant(token: str) -> None:
    raise ValueError(f"non-finite JSON constant: {token}")


def _number(value: Any, code: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise PlatformSchedulerRepositoryError(code) from None
    if not math.isfinite(parsed):
        raise PlatformSchedulerRepositoryError(code)
    return parsed


def _result_json(value: Mapping[str, Any] | None) -> str:
    try:
        encoded = json.dumps(
            dict(value or {}), ensure_ascii=True, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        )
    except (TypeError, ValueError):
        raise PlatformSchedulerRepositoryError("invalid_result") from None
    if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
        raise PlatformSchedulerRepositoryError("result_too_large")
    return encoded


def _error_code(value: Any) -> str:
    if value is None:
        return "sync_failed"
    candidate = str(value)[:120]
    return candidate if _ERROR_CODE_RE.fullmatch(candidate) else "sync_failed"


class PlatformSchedulerRepository:
    """A tiny job table with transactionally claimed leases.

    The repository deliberately stores only bounded operational metadata. A
    worker may crash after claiming a lease; once its expiry passes, another
    process can reclaim the job. Completion is accepted only for the current
    lease token, so an old worker cannot overwrite a newer result.
    """

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
            CREATE TABLE IF NOT EXISTS platform_jobs (
              job_id TEXT PRIMARY KEY,
              state TEXT NOT NULL DEFAULT 'idle',
              next_run_at REAL NOT NULL DEFAULT 0,
              lease_id TEXT,
              lease_expires_at REAL,
              attempt INTEGER NOT NULL DEFAULT 0,
              consecutive_failures INTEGER NOT NULL DEFAULT 0,
              last_started_at REAL,
              last_finished_at REAL,
              last_success_at REAL,
              last_error_code TEXT,
              last_result TEXT NOT NULL DEFAULT '{}',
              updated_at REAL NOT NULL
            );
            """)
        except (sqlite3.Error, OSError):
            raise PlatformSchedulerRepositoryError("scheduler_store") from None
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _public(row) -> dict[str, Any]:
        if row is None:
            return None
        try:
            result = json.loads(row["last_result"] or "{}", parse_constant=_reject_json_constant)
            if not isinstance(result, Mapping):
                raise ValueError("expected object")
        except (TypeError, ValueError) as exc:
            raise PlatformSchedulerRepositoryError("scheduler_store") from exc
        return {
            "job_id": row["job_id"],
            "state": row["state"],
            "next_run_at": float(row["next_run_at"]),
            "attempt": int(row["attempt"]),
            "consecutive_failures": int(row["consecutive_failures"]),
            "last_started_at": row["last_started_at"],
            "last_finished_at": row["last_finished_at"],
            "last_success_at": row["last_success_at"],
            "last_error_code": row["last_error_code"],
            "last_result": dict(result),
            "updated_at": row["updated_at"],
            # Lease material is intentionally omitted from the public status.
        }

    def get(self, job_id: str) -> dict[str, Any] | None:
        if not isinstance(job_id, str) or not job_id or len(job_id) > 128:
            raise PlatformSchedulerRepositoryError("invalid_job_id")
        conn = None
        try:
            conn = self._connect()
            return self._public(conn.execute("SELECT * FROM platform_jobs WHERE job_id=?", (job_id,)).fetchone())
        except sqlite3.Error:
            raise PlatformSchedulerRepositoryError("scheduler_store") from None
        finally:
            if conn is not None:
                conn.close()

    def claim(self, job_id: str, *, now: float | None = None, lease_s: float = 60.0, force: bool = False) -> dict[str, Any] | None:
        if not isinstance(job_id, str) or not job_id or len(job_id) > 128:
            raise PlatformSchedulerRepositoryError("invalid_job_id")
        now = _number(self.clock() if now is None else now, "invalid_time")
        lease_s = _number(lease_s, "invalid_lease")
        if lease_s <= 0 or lease_s > 3600:
            raise PlatformSchedulerRepositoryError("invalid_lease")
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM platform_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO platform_jobs(job_id,state,next_run_at,updated_at) VALUES(?,?,?,?)",
                    (job_id, "idle", 0.0, now),
                )
                row = conn.execute("SELECT * FROM platform_jobs WHERE job_id=?", (job_id,)).fetchone()
            active_until = row["lease_expires_at"]
            if active_until is not None and float(active_until) > now:
                conn.execute("ROLLBACK")
                return None
            if not force and float(row["next_run_at"]) > now:
                conn.execute("ROLLBACK")
                return None
            lease_id = "lease_" + secrets.token_urlsafe(18)
            conn.execute(
                "UPDATE platform_jobs SET state='running',lease_id=?,lease_expires_at=?,attempt=attempt+1,last_started_at=?,updated_at=? WHERE job_id=?",
                (lease_id, now + lease_s, now, now, job_id),
            )
            conn.execute("COMMIT")
            updated = conn.execute("SELECT * FROM platform_jobs WHERE job_id=?", (job_id,)).fetchone()
            claimed = self._public(updated)
            claimed["lease_id"] = lease_id
            claimed["lease_expires_at"] = now + lease_s
            return claimed
        except PlatformSchedulerRepositoryError:
            if conn is not None:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        except (sqlite3.Error, OSError):
            if conn is not None:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise PlatformSchedulerRepositoryError("scheduler_store") from None
        finally:
            if conn is not None:
                conn.close()

    def finish(
        self,
        job_id: str,
        lease_id: str,
        *,
        now: float | None = None,
        success: bool,
        next_run_at: float,
        error_code: str | None = None,
        result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(lease_id, str) or not lease_id or len(lease_id) > 256:
            raise PlatformSchedulerRepositoryError("invalid_lease")
        now = _number(self.clock() if now is None else now, "invalid_time")
        next_run_at = _number(next_run_at, "invalid_time")
        encoded = _result_json(result)
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM platform_jobs WHERE job_id=? AND lease_id=? "
                "AND state='running' AND lease_expires_at>?",
                (job_id, lease_id, now),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise PlatformSchedulerRepositoryError("lease_mismatch")
            failures = 0 if success else int(row["consecutive_failures"]) + 1
            conn.execute(
                "UPDATE platform_jobs SET state='idle',next_run_at=?,lease_id=NULL,lease_expires_at=NULL,last_finished_at=?,last_success_at=?,last_error_code=?,last_result=?,consecutive_failures=?,updated_at=? WHERE job_id=? AND lease_id=?",
                (next_run_at, now, now if success else row["last_success_at"], None if success else _error_code(error_code), encoded, failures, now, job_id, lease_id),
            )
            conn.execute("COMMIT")
            return self._public(conn.execute("SELECT * FROM platform_jobs WHERE job_id=?", (job_id,)).fetchone())
        except PlatformSchedulerRepositoryError:
            if conn is not None:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        except (sqlite3.Error, OSError):
            if conn is not None:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise PlatformSchedulerRepositoryError("scheduler_store") from None
        finally:
            if conn is not None:
                conn.close()


__all__ = ["MAX_RESULT_BYTES", "PlatformSchedulerRepository", "PlatformSchedulerRepositoryError"]
