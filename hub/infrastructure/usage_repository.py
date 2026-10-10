"""Bounded provider usage ledger and opt-in sliding-window admission.

The ledger is deliberately independent from prompts, provider payloads, and
secrets.  It can share ``platform.db`` with :class:`PlatformRepository` and
uses short SQLite transactions so multiple Hub workers cannot oversubscribe a
configured owner/model window.
"""
from __future__ import annotations

import secrets
import sqlite3
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generator, Mapping

from platform_schema import validate_id, validate_owner_id
from tools.platform.request_metadata import decimal_amount, normalized_tokens, request_cost


MAX_COUNTER = 10_000_000
MAX_RUN_COUNTER = 1_000_000_000
MAX_WINDOW_S = 7 * 24 * 60 * 60


class UsageRepositoryError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


class UsageLimitExceeded(UsageRepositoryError):
    def __init__(self, dimension: str = "usage"):
        self.dimension = dimension if dimension in {"requests", "tokens", "usage"} else "usage"
        super().__init__("usage_limit_exceeded")


@dataclass(frozen=True)
class UsageReservation:
    reservation_id: str
    owner_id: str
    model_key: str
    run_id: str
    attempt: int
    step: int
    estimated_tokens: int
    created_at: float
    expires_at: float
    already_settled: bool = False


def _counter(value: Any, *, maximum: int = MAX_COUNTER) -> int:
    if isinstance(value, bool):
        raise UsageRepositoryError("invalid_usage")
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        raise UsageRepositoryError("invalid_usage") from None
    if number < 0 or number > maximum:
        raise UsageRepositoryError("invalid_usage")
    return number


def normalize_usage(value: Mapping[str, Any] | None) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    input_tokens = _counter(value.get("input_tokens", 0))
    output_tokens = _counter(value.get("output_tokens", 0))
    supplied_total = value.get("total_tokens")
    total_tokens = _counter(supplied_total) if supplied_total is not None else input_tokens + output_tokens
    total_tokens = max(total_tokens, input_tokens + output_tokens)
    if total_tokens > MAX_COUNTER:
        raise UsageRepositoryError("invalid_usage")
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


class UsageRepository:
    """SQLite-backed usage policies, reservations, and settled events."""

    def __init__(self, db_path: Path, *, enforce_limits: bool = True):
        self.db_path = Path(db_path)
        self.path = self.db_path
        self.enforce_limits = bool(enforce_limits)
        self._summary_error: sqlite3.Error | None = None

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
                CREATE TABLE IF NOT EXISTS platform_usage_limits (
                    owner_id TEXT NOT NULL,
                    model_key TEXT NOT NULL,
                    request_limit INTEGER,
                    token_limit INTEGER,
                    window_s INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY(owner_id, model_key)
                );
                CREATE TABLE IF NOT EXISTS platform_usage_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    model_key TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    step INTEGER NOT NULL,
                    estimated_tokens INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    UNIQUE(run_id, attempt, step)
                );
                CREATE TABLE IF NOT EXISTS platform_usage_events (
                    event_id TEXT PRIMARY KEY,
                    reservation_id TEXT NOT NULL UNIQUE,
                    owner_id TEXT NOT NULL,
                    model_key TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    step INTEGER NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    outcome TEXT NOT NULL,
                    occurred_at REAL NOT NULL,
                    UNIQUE(run_id, attempt, step)
                );
                CREATE TABLE IF NOT EXISTS platform_run_usage (
                    owner_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    provider_requests INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY(owner_id, run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_platform_usage_events_window
                    ON platform_usage_events(owner_id, model_key, occurred_at);
                CREATE INDEX IF NOT EXISTS idx_platform_usage_reservations_window
                    ON platform_usage_reservations(owner_id, model_key, expires_at);
                """
            )
        except sqlite3.Error as exc:
            raise UsageRepositoryError("usage_store") from exc
        finally:
            if conn is not None:
                conn.close()
        # A failed additive read-model migration only disables this new API.
        # The existing admission/settlement ledger remains usable.
        try:
            self._migrate_summary()
            self._summary_error = None
        except sqlite3.Error as exc:
            self._summary_error = exc

    def _migrate_summary(self) -> None:
        conn = self._connect()
        try:
            # Old ledger rows cannot prove omitted tokens meant zero.
            conn.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(platform_usage_events)")}
            for name, declaration in (
                ("summary_complete", "INTEGER NOT NULL DEFAULT 0"),
                ("summary_project", "TEXT"), ("summary_machine", "TEXT"),
                ("summary_agent", "TEXT"), ("summary_model", "TEXT"),
                ("estimated_cost", "TEXT"), ("cost_source", "TEXT NOT NULL DEFAULT 'unknown'"),
            ):
                if name not in columns:
                    conn.execute(f"ALTER TABLE platform_usage_events ADD COLUMN {name} {declaration}")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_platform_usage_summary ON platform_usage_events(owner_id,occurred_at,event_id)")
            conn.execute("COMMIT")
        finally:
            conn.close()

    @staticmethod
    def _model_key(value: Any) -> str:
        if not isinstance(value, str) or not value or len(value) > 160:
            raise UsageRepositoryError("invalid_model_key")
        # Model keys are catalog identifiers or the worker's bounded fallback.
        try:
            return validate_id(value, "model_key")
        except Exception:
            if value == "default":
                return value
            raise UsageRepositoryError("invalid_model_key") from None

    @staticmethod
    def _attempt_step(value: Any) -> int:
        if isinstance(value, bool):
            raise UsageRepositoryError("invalid_usage_key")
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise UsageRepositoryError("invalid_usage_key") from None
        if number < 1 or number > 1_000_000:
            raise UsageRepositoryError("invalid_usage_key")
        return number

    @classmethod
    def _key(cls, owner_id: Any, model_key: Any, run_id: Any, attempt: Any, step: Any):
        try:
            owner_id = validate_owner_id(owner_id)
            model_key = cls._model_key(model_key)
            run_id = validate_id(run_id, "run_id")
        except Exception as exc:
            if isinstance(exc, UsageRepositoryError):
                raise
            raise UsageRepositoryError("invalid_usage_key") from None
        return owner_id, model_key, run_id, cls._attempt_step(attempt), cls._attempt_step(step)

    @staticmethod
    def _policy(row) -> dict[str, Any] | None:
        if row is None or not bool(row["enabled"]):
            return None
        return {
            "owner_id": row["owner_id"],
            "model_key": row["model_key"],
            "request_limit": row["request_limit"],
            "token_limit": row["token_limit"],
            "window_s": int(row["window_s"]),
            "enabled": bool(row["enabled"]),
            "updated_at": row["updated_at"],
        }

    def set_limit(self, owner_id: str, model_key: str, *, request_limit: int | None = None,
                  token_limit: int | None = None, window_s: int = 3600,
                  enabled: bool = True) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        model_key = self._model_key(model_key)
        if request_limit is None and token_limit is None:
            raise UsageRepositoryError("invalid_usage_limit")
        if request_limit is not None:
            request_limit = _counter(request_limit, maximum=1_000_000)
        if token_limit is not None:
            token_limit = _counter(token_limit, maximum=MAX_RUN_COUNTER)
        try:
            window_s = int(window_s)
        except (TypeError, ValueError):
            raise UsageRepositoryError("invalid_usage_limit") from None
        if window_s < 1 or window_s > MAX_WINDOW_S:
            raise UsageRepositoryError("invalid_usage_limit")
        conn = None
        try:
            conn = self._connect()
            conn.execute(
                "INSERT INTO platform_usage_limits(owner_id,model_key,request_limit,token_limit,window_s,enabled,updated_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(owner_id,model_key) DO UPDATE SET request_limit=excluded.request_limit, token_limit=excluded.token_limit, window_s=excluded.window_s, enabled=excluded.enabled, updated_at=excluded.updated_at",
                (owner_id, model_key, request_limit, token_limit, window_s, int(bool(enabled)), 0.0),
            )
            row = conn.execute(
                "SELECT * FROM platform_usage_limits WHERE owner_id=? AND model_key=?",
                (owner_id, model_key),
            ).fetchone()
            return self._policy(row) or {"owner_id": owner_id, "model_key": model_key, "enabled": False}
        except sqlite3.Error:
            raise UsageRepositoryError("usage_store") from None
        finally:
            if conn is not None:
                conn.close()

    def get_limit(self, owner_id: str, model_key: str) -> dict[str, Any] | None:
        owner_id = validate_owner_id(owner_id)
        model_key = self._model_key(model_key)
        conn = None
        try:
            conn = self._connect()
            return self._policy(conn.execute(
                "SELECT * FROM platform_usage_limits WHERE owner_id=? AND model_key=?",
                (owner_id, model_key),
            ).fetchone())
        except sqlite3.Error:
            raise UsageRepositoryError("usage_store") from None
        finally:
            if conn is not None:
                conn.close()

    def admit(self, owner_id: str, model_key: str, run_id: str, attempt: int, step: int,
              *, now: float, estimated_tokens: int = 0) -> UsageReservation:
        owner_id, model_key, run_id, attempt, step = self._key(owner_id, model_key, run_id, attempt, step)
        estimated_tokens = _counter(estimated_tokens, maximum=MAX_RUN_COUNTER)
        try:
            now = float(now)
        except (TypeError, ValueError):
            raise UsageRepositoryError("invalid_usage_clock") from None
        if now != now:
            raise UsageRepositoryError("invalid_usage_clock")
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT e.*, r.created_at, r.expires_at, r.estimated_tokens FROM platform_usage_events e "
                "LEFT JOIN platform_usage_reservations r ON r.reservation_id=e.reservation_id "
                "WHERE e.run_id=? AND e.attempt=? AND e.step=?",
                (run_id, attempt, step),
            ).fetchone()
            if existing is not None:
                if existing["owner_id"] != owner_id or existing["model_key"] != model_key:
                    conn.execute("ROLLBACK")
                    raise UsageRepositoryError("usage_owner_conflict")
                conn.execute("COMMIT")
                return UsageReservation(
                    existing["reservation_id"], owner_id, model_key, run_id, attempt, step,
                    int(existing["estimated_tokens"] or 0),
                    float(existing["created_at"] or now),
                    float(existing["expires_at"] or now), True,
                )
            pending = conn.execute(
                "SELECT * FROM platform_usage_reservations WHERE run_id=? AND attempt=? AND step=?",
                (run_id, attempt, step),
            ).fetchone()
            if pending is not None:
                if pending["owner_id"] != owner_id or pending["model_key"] != model_key:
                    conn.execute("ROLLBACK")
                    raise UsageRepositoryError("usage_owner_conflict")
                conn.execute("COMMIT")
                return UsageReservation(
                    pending["reservation_id"], pending["owner_id"], pending["model_key"],
                    pending["run_id"], int(pending["attempt"]), int(pending["step"]),
                    int(pending["estimated_tokens"]), float(pending["created_at"]),
                    float(pending["expires_at"]), False,
                )
            conn.execute("DELETE FROM platform_usage_reservations WHERE expires_at<=?", (now,))
            policy_row = conn.execute(
                "SELECT * FROM platform_usage_limits WHERE owner_id=? AND model_key=?",
                (owner_id, model_key),
            ).fetchone()
            policy = self._policy(policy_row)
            if policy is not None and self.enforce_limits:
                cutoff = now - policy["window_s"]
                counts = conn.execute(
                    "SELECT COALESCE(SUM(total_tokens),0) AS tokens, COUNT(*) AS requests "
                    "FROM platform_usage_events WHERE owner_id=? AND model_key=? AND occurred_at>?",
                    (owner_id, model_key, cutoff),
                ).fetchone()
                active = conn.execute(
                    "SELECT COALESCE(SUM(estimated_tokens),0) AS tokens, COUNT(*) AS requests "
                    "FROM platform_usage_reservations WHERE owner_id=? AND model_key=? AND expires_at>?",
                    (owner_id, model_key, now),
                ).fetchone()
                requests = int(counts["requests"] or 0) + int(active["requests"] or 0) + 1
                tokens = int(counts["tokens"] or 0) + int(active["tokens"] or 0) + estimated_tokens
                if policy["request_limit"] is not None and requests > int(policy["request_limit"]):
                    conn.execute("ROLLBACK")
                    raise UsageLimitExceeded("requests")
                if policy["token_limit"] is not None and tokens > int(policy["token_limit"]):
                    conn.execute("ROLLBACK")
                    raise UsageLimitExceeded("tokens")
            reservation_id = "usageres_" + secrets.token_hex(12)
            expires_at = now + (policy["window_s"] if policy else 3600)
            conn.execute(
                "INSERT INTO platform_usage_reservations(reservation_id,owner_id,model_key,run_id,attempt,step,estimated_tokens,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (reservation_id, owner_id, model_key, run_id, attempt, step,
                 estimated_tokens, now, expires_at),
            )
            conn.execute("COMMIT")
            return UsageReservation(reservation_id, owner_id, model_key, run_id,
                                    attempt, step, estimated_tokens, now, expires_at)
        except UsageRepositoryError:
            raise
        except sqlite3.IntegrityError:
            if conn is not None:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            # Another worker inserted the same key between our read and write;
            # retrying the read is safe and keeps the public operation idempotent.
            return self.admit(owner_id, model_key, run_id, attempt, step,
                              now=now, estimated_tokens=estimated_tokens)
        except sqlite3.Error:
            if conn is not None:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise UsageRepositoryError("usage_store") from None
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _dimension(value: Any, *, model: bool = False) -> str | None:
        if not isinstance(value, str) or not value or len(value) > (160 if model else 128):
            return None
        if model:
            return value if not any(ord(char) < 32 or ord(char) == 127 for char in value) else None
        try:
            return validate_id(value)
        except ValueError:
            return None

    def _settlement_summary(self, conn: sqlite3.Connection, reservation: UsageReservation,
                            usage: Mapping[str, Any] | None,
                            metadata: Mapping[str, Any] | None) -> tuple[Any, ...]:
        """Freeze public dimensions and cost at settlement, never at read time."""
        tables = {row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('runs','model_profiles','run_events')")}
        config: dict[str, Any] = {}
        profile: dict[str, Any] = {}
        detail = dict(metadata or {})
        if "runs" in tables:
            row = conn.execute("SELECT config_snapshot FROM runs WHERE owner_id=? AND run_id=?",
                               (reservation.owner_id, reservation.run_id)).fetchone()
            config = json.loads(row["config_snapshot"]) if row is not None else {}
        if "model_profiles" in tables:
            row = conn.execute("SELECT model,provider_config FROM model_profiles WHERE owner_id=? AND profile_id=?",
                               (reservation.owner_id, reservation.model_key)).fetchone()
            profile = dict(row) if row is not None else {}
        if metadata is None and "run_events" in tables and "runs" in tables:
            row = conn.execute(
                "SELECT e.payload FROM run_events e JOIN runs r ON r.run_id=e.run_id "
                "WHERE r.owner_id=? AND e.run_id=? AND e.kind='provider_request_finished' "
                "AND json_extract(e.payload,'$.attempt')=? AND json_extract(e.payload,'$.step')=? "
                "ORDER BY e.sequence DESC LIMIT 1",
                (reservation.owner_id, reservation.run_id, reservation.attempt, reservation.step),
            ).fetchone()
            detail = json.loads(row["payload"]) if row is not None else {}
        tokens = normalized_tokens(usage)
        detail_usage = detail.get("usage")
        if isinstance(detail_usage, Mapping):
            tokens = normalized_tokens({**detail_usage, **{field: tokens[field] for field in
                                                         ("input_tokens", "output_tokens", "total_tokens")}})
        complete = tokens["input_tokens"] is not None and tokens["output_tokens"] is not None
        cost = detail.get("cost")
        amount = decimal_amount(cost.get("amount")) if isinstance(cost, Mapping) else None
        source = cost.get("source") if isinstance(cost, Mapping) else None
        if amount is not None and source in {"provider", "configured"} and cost.get("currency") == "USD":
            if source == "configured" and not complete:
                amount, source = None, "unknown"
        else:
            provider_config = json.loads(profile.get("provider_config") or "{}")
            estimate = None if detail.get("cost_unavailable_reason") else request_cost(tokens, pricing=provider_config.get("pricing"))
            amount, source = (estimate["amount"], estimate["source"]) if estimate else (None, "unknown")
        model = detail.get("response_model") or detail.get("model") or profile.get("model") or reservation.model_key
        return (int(complete), self._dimension(config.get("workspace_id")),
                self._dimension(config.get("execution_node_id")),
                self._dimension(config.get("agent_family")), self._dimension(model, model=True), amount, source)

    def settle(self, reservation: UsageReservation, usage: Mapping[str, Any] | None,
               outcome: str, *, now: float | None = None,
               metadata: Mapping[str, Any] | None = None) -> dict[str, int]:
        if not isinstance(reservation, UsageReservation):
            raise UsageRepositoryError("invalid_reservation")
        if outcome not in {"succeeded", "failed", "unknown"}:
            raise UsageRepositoryError("invalid_usage_outcome")
        normalized = normalize_usage(usage)
        if outcome == "unknown" and usage is None:
            normalized = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        try:
            occurred_at = float(now) if now is not None else reservation.created_at
        except (ValueError, TypeError) as exc:
            raise UsageRepositoryError("invalid_usage_clock") from exc
        if not math.isfinite(occurred_at):
            raise UsageRepositoryError("invalid_usage_clock")
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT owner_id,model_key,run_id,attempt,step,input_tokens,output_tokens,total_tokens "
                "FROM platform_usage_events WHERE reservation_id=? OR (run_id=? AND attempt=? AND step=?) LIMIT 1",
                (reservation.reservation_id, reservation.run_id, reservation.attempt, reservation.step),
            ).fetchone()
            if existing is not None:
                identity = ("owner_id", "model_key", "run_id", "attempt", "step")
                if any(existing[key] != getattr(reservation, key) for key in identity):
                    raise UsageRepositoryError("usage_owner_conflict")
                conn.execute("DELETE FROM platform_usage_reservations WHERE run_id=? AND attempt=? AND step=?",
                             (reservation.run_id, reservation.attempt, reservation.step))
                conn.execute("COMMIT")
                return {"input_tokens": int(existing["input_tokens"]), "output_tokens": int(existing["output_tokens"]), "total_tokens": int(existing["total_tokens"])}
            row = conn.execute(
                "SELECT * FROM platform_usage_reservations WHERE reservation_id=?",
                (reservation.reservation_id,),
            ).fetchone()
            if row is None:
                # Expired cleanup may have removed a pending reservation, but
                # accepting the event still preserves conservative accounting.
                owner_id, model_key, run_id = reservation.owner_id, reservation.model_key, reservation.run_id
            else:
                owner_id, model_key, run_id = row["owner_id"], row["model_key"], row["run_id"]
                if any(row[key] != getattr(reservation, key) for key in ("owner_id", "model_key", "run_id", "attempt", "step")):
                    raise UsageRepositoryError("usage_owner_conflict")
            columns = "event_id,reservation_id,owner_id,model_key,run_id,attempt,step,input_tokens,output_tokens,total_tokens,outcome,occurred_at"
            values = ("usageevt_" + secrets.token_hex(12), reservation.reservation_id,
                      owner_id, model_key, run_id, reservation.attempt, reservation.step,
                      normalized["input_tokens"], normalized["output_tokens"], normalized["total_tokens"], outcome, occurred_at)
            if self._summary_error is None:
                values += self._settlement_summary(conn, reservation, usage, metadata)
                columns += ",summary_complete,summary_project,summary_machine,summary_agent,summary_model,estimated_cost,cost_source"
            conn.execute("INSERT INTO platform_usage_events(" + columns + ") VALUES(" + ",".join("?" for _ in values) + ")", values)
            conn.execute("DELETE FROM platform_usage_reservations WHERE reservation_id=?", (reservation.reservation_id,))
            conn.execute(
                "INSERT INTO platform_run_usage(owner_id,run_id,input_tokens,output_tokens,total_tokens,provider_requests,updated_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(owner_id,run_id) DO UPDATE SET input_tokens=input_tokens+excluded.input_tokens, output_tokens=output_tokens+excluded.output_tokens, total_tokens=total_tokens+excluded.total_tokens, provider_requests=provider_requests+excluded.provider_requests, updated_at=excluded.updated_at",
                (owner_id, run_id, normalized["input_tokens"], normalized["output_tokens"],
                 normalized["total_tokens"], 1, occurred_at),
            )
            conn.execute("COMMIT")
            return normalized
        except (sqlite3.Error, ValueError, TypeError) as exc:
            # Closing the connection rolls back this short transaction. An
            # unexpected integrity failure must not recurse indefinitely.
            raise UsageRepositoryError("usage_store") from exc
        finally:
            if conn is not None:
                conn.close()

    def get_run_usage(self, owner_id: str, run_id: str) -> dict[str, int]:
        owner_id = validate_owner_id(owner_id)
        run_id = validate_id(run_id, "run_id")
        conn = None
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT input_tokens,output_tokens,total_tokens,provider_requests FROM platform_run_usage WHERE owner_id=? AND run_id=?",
                (owner_id, run_id),
            ).fetchone()
            if row is None:
                return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "provider_requests": 0}
            return {key: int(row[key]) for key in ("input_tokens", "output_tokens", "total_tokens", "provider_requests")}
        except sqlite3.Error:
            raise UsageRepositoryError("usage_store") from None
        finally:
            if conn is not None:
                conn.close()

    def iter_summary_events(self, owner_id: str, *, from_time: float, to_time: float,
                            filters: Mapping[str, str]) -> Generator[dict[str, Any], None, None]:
        """Read an owner/time-indexed snapshot in bounded chunks, without N+1 reads."""
        if self._summary_error is not None:
            raise UsageRepositoryError("usage_summary_unavailable") from self._summary_error
        owner_id = validate_owner_id(owner_id)
        dimensions = {"project": "summary_project", "machine": "summary_machine",
                      "agent": "summary_agent", "model": "COALESCE(summary_model,model_key)"}
        predicates = ["owner_id=?", "occurred_at>=?", "occurred_at<?"]
        values: list[Any] = [owner_id, from_time, to_time]
        for key, value in filters.items():
            if key not in dimensions:
                raise UsageRepositoryError("invalid_usage_query")
            predicates.append(dimensions[key] + "=?")
            values.append(value)
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN")
            cursor_time, cursor_id = from_time, ""
            while True:
                rows = conn.execute(
                    "SELECT event_id,occurred_at,input_tokens,output_tokens,total_tokens,summary_complete,"
                    "summary_project AS project,summary_machine AS machine,summary_agent AS agent,"
                    "COALESCE(summary_model,model_key) AS model,estimated_cost,cost_source "
                    "FROM platform_usage_events WHERE " + " AND ".join(predicates) +
                    " AND (occurred_at,event_id)>(?,?) ORDER BY occurred_at,event_id LIMIT 512",
                    (*values, cursor_time, cursor_id),
                ).fetchall()
                if not rows:
                    return
                for row in rows:
                    yield dict(row)
                cursor_time, cursor_id = rows[-1]["occurred_at"], rows[-1]["event_id"]
        except sqlite3.Error as exc:
            raise UsageRepositoryError("usage_store") from exc
        finally:
            if conn is not None:
                conn.close()


__all__ = [
    "UsageLimitExceeded", "UsageRepository", "UsageRepositoryError",
    "UsageReservation", "normalize_usage",
]
