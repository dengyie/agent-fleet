"""Application service for bounded, read-only durable schedules."""
from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from hub.infrastructure.platform_schedule_repository import (
    MIN_INTERVAL_S,
    PlatformScheduleRepository,
    PlatformScheduleRepositoryError,
)
from platform_schema import validate_owner_id
from hub.diagnostics import log_failure

logger = logging.getLogger(__name__)

PlatformScheduleError = PlatformScheduleRepositoryError


def _safe_error_code(value: Any, fallback: str = "executor_failed") -> str:
    text = str(value or fallback)[:120]
    return text if all(char.isalnum() or char in "_.:-" for char in text) and text else fallback


def _future_run(scheduled_at: float, interval_s: float, now: float) -> float:
    candidate = float(scheduled_at) + float(interval_s)
    if candidate > now:
        return candidate
    steps = max(1, math.floor((now - scheduled_at) / interval_s) + 1)
    return float(scheduled_at) + steps * float(interval_s)


def _bounded_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"state": "completed"}
    allowed: dict[str, Any] = {}
    for key in ("state", "service_id", "dimension", "freshness", "observed_at", "detail"):
        if key not in value:
            continue
        item = value[key]
        if key == "detail" and isinstance(item, Mapping):
            allowed[key] = {
                str(k): str(v)[:200] for k, v in list(item.items())[:16]
                if isinstance(k, str)
            }
        elif isinstance(item, (str, int, float, bool)) or item is None:
            allowed[key] = item
    return allowed or {"state": "completed"}


class DurableReadOnlyScheduleService:
    """Create and execute fixed, owner-scoped read-only schedule definitions."""

    def __init__(
        self,
        repository: PlatformScheduleRepository,
        *,
        executor: Callable[[str, str, Mapping[str, Any]], Mapping[str, Any]] | None = None,
        worker_id: str = "schedule-worker",
        lease_s: float = 60.0,
        failure_backoff_cap_s: float = 3600.0,
        poll_interval_s: float = 5.0,
        clock: Callable[[], float] = time.time,
    ):
        self.repository = repository
        self.executor = executor or self._unavailable_executor
        self.worker_id = str(worker_id)[:128] or "schedule-worker"
        self.lease_s = max(5.0, min(3600.0, float(lease_s)))
        self.failure_backoff_cap_s = max(MIN_INTERVAL_S, min(86400.0, float(failure_backoff_cap_s)))
        self.poll_interval_s = max(1.0, min(60.0, float(poll_interval_s)))
        self.clock = clock

    @staticmethod
    def _unavailable_executor(_owner_id: str, _action: str, _target: Mapping[str, Any]):
        raise PlatformScheduleError("executor_unavailable")

    @staticmethod
    def _owner(owner_id: str) -> str:
        try:
            return validate_owner_id(owner_id)
        except ValueError:
            raise PlatformScheduleError("invalid_owner") from None

    def create(self, owner_id: str, data: Mapping[str, Any]) -> dict[str, Any]:
        return self.repository.create(self._owner(owner_id), data, now=float(self.clock()))

    def get(self, owner_id: str, schedule_id: str) -> dict[str, Any]:
        schedule = self.repository.get(self._owner(owner_id), schedule_id)
        if schedule is None:
            raise PlatformScheduleError("schedule_not_found")
        return schedule

    def list(self, owner_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.repository.list(self._owner(owner_id), limit=limit)

    def update(self, owner_id: str, schedule_id: str, data: Mapping[str, Any], *, expected_revision: int | None = None) -> dict[str, Any]:
        return self.repository.update(
            self._owner(owner_id), schedule_id, data,
            expected_revision=expected_revision, now=float(self.clock()),
        )

    def delete(self, owner_id: str, schedule_id: str) -> dict[str, Any]:
        deleted = self.repository.delete(self._owner(owner_id), schedule_id)
        if not deleted:
            raise PlatformScheduleError("schedule_not_found")
        return {"ok": True, "deleted": True, "schedule_id": schedule_id}

    def _next_after_failure(self, now: float, interval_s: float, failures: int) -> float:
        delay = min(self.failure_backoff_cap_s, float(interval_s) * (2 ** min(max(1, int(failures)), 8)))
        return float(now) + delay

    def _run_one(self, owner_id: str, schedule: Mapping[str, Any], *, now: float) -> dict[str, Any]:
        schedule_id = schedule["schedule_id"]
        scheduled_at = float(schedule["next_run_at"])
        interval_s = float(schedule["interval_s"])
        existing = self.repository.get_trigger(owner_id, schedule_id, scheduled_at)
        active = self.repository.get_active_trigger(owner_id, schedule_id, now=now)
        if active and float(active["scheduled_at"]) != scheduled_at:
            next_run = _future_run(scheduled_at, interval_s, now)
            policy_state = (
                "coalesced" if schedule["overlap_policy"] == "coalesce"
                else "skipped"
            )
            claim = self.repository.claim_trigger(
                owner_id, schedule_id, scheduled_at=scheduled_at,
                worker_id=self.worker_id, now=now, lease_s=self.lease_s,
            )
            if claim is not None:
                self.repository.finish_trigger(
                    owner_id, schedule_id, scheduled_at, claim["lease_id"],
                    state=policy_state, next_run_at=next_run, now=now,
                    result={"state": policy_state, "policy": schedule["overlap_policy"]},
                )
            else:
                self.repository.advance_schedule(
                    owner_id, schedule_id, next_run_at=next_run, now=now,
                    result={"state": policy_state, "policy": schedule["overlap_policy"]},
                )
            return {
                "schedule_id": schedule_id, "scheduled_at": scheduled_at,
                "skipped": 1 if schedule["overlap_policy"] == "skip" else 0,
                "coalesced": 1 if schedule["overlap_policy"] == "coalesce" else 0,
                "executed": 0,
            }
        if active:
            # Keep this occurrence discoverable if its worker crashes. Only a
            # terminal finish advances the schedule; an expired lease below
            # is reclaimed through the repository's atomic admission.
            return {
                "schedule_id": schedule_id, "scheduled_at": scheduled_at,
                "skipped": 1 if schedule["overlap_policy"] == "skip" else 0,
                "coalesced": 1 if schedule["overlap_policy"] == "coalesce" else 0,
                "executed": 0,
            }
        if existing and existing["state"] in {"succeeded", "failed", "skipped", "coalesced"}:
            self.repository.advance_schedule(
                owner_id, schedule_id,
                next_run_at=_future_run(scheduled_at, interval_s, now), now=now,
                result={"state": "duplicate"},
            )
            return {"schedule_id": schedule_id, "scheduled_at": scheduled_at, "executed": 0, "skipped": 1}

        if existing is None and schedule["missed_policy"] == "skip" and scheduled_at < now:
            claim = self.repository.claim_trigger(
                owner_id, schedule_id, scheduled_at=scheduled_at,
                worker_id=self.worker_id, now=now, lease_s=self.lease_s,
            )
            if claim is None:
                return {"schedule_id": schedule_id, "scheduled_at": scheduled_at, "executed": 0, "skipped": 1}
            self.repository.finish_trigger(
                owner_id, schedule_id, scheduled_at, claim["lease_id"],
                state="skipped",
                next_run_at=_future_run(scheduled_at, interval_s, now),
                now=now, result={"state": "missed", "policy": "skip"},
            )
            return {"schedule_id": schedule_id, "scheduled_at": scheduled_at, "executed": 0, "skipped": 1}

        claim = self.repository.claim_trigger(
            owner_id, schedule_id, scheduled_at=scheduled_at,
            worker_id=self.worker_id, now=now, lease_s=self.lease_s,
        )
        if claim is None:
            return {"schedule_id": schedule_id, "scheduled_at": scheduled_at, "executed": 0, "skipped": 1}
        try:
            result = self.executor(owner_id, schedule["action"], schedule["target"])
            bounded = _bounded_result(result)
            finished_at = max(now, float(self.clock()))
            self.repository.finish_trigger(
                owner_id, schedule_id, scheduled_at, claim["lease_id"],
                state="succeeded",
                next_run_at=_future_run(scheduled_at, interval_s, finished_at),
                now=finished_at, result=bounded,
            )
            return {"schedule_id": schedule_id, "scheduled_at": scheduled_at, "executed": 1, "result": bounded}
        except Exception as exc:
            code = _safe_error_code(getattr(exc, "code", None))
            if code == "lease_mismatch":
                return {"schedule_id": schedule_id, "scheduled_at": scheduled_at,
                        "executed": 0, "skipped": 1, "error_code": code}
            failures = int(schedule.get("consecutive_failures", 0)) + 1
            finished_at = max(now, float(self.clock()))
            try:
                self.repository.finish_trigger(
                    owner_id, schedule_id, scheduled_at, claim["lease_id"],
                    state="failed",
                    next_run_at=self._next_after_failure(finished_at, interval_s, failures),
                    now=finished_at, error_code=code, result={"state": "failed", "error_code": code},
                )
            except PlatformScheduleError as finish_error:
                if finish_error.code != "lease_mismatch":
                    raise
                return {"schedule_id": schedule_id, "scheduled_at": scheduled_at,
                        "executed": 0, "skipped": 1, "error_code": "lease_mismatch"}
            return {"schedule_id": schedule_id, "scheduled_at": scheduled_at, "executed": 0, "failed": 1, "error_code": code}

    def tick_once(self, *, owner_id: str, schedule_id: str | None = None, now: float | None = None, force: bool = False) -> dict[str, Any]:
        owner_id = self._owner(owner_id)
        now = float(self.clock() if now is None else now)
        if schedule_id is None:
            schedules = self.repository.list_due(owner_id, now=now)
        else:
            schedule = self.get(owner_id, schedule_id)
            expired = self.repository.get_expired_trigger(owner_id, schedule_id, now=now)
            if expired is not None:
                schedule = {**schedule, "next_run_at": expired["scheduled_at"]}
            schedules = [schedule]
        result = {"ok": True, "executed": 0, "failed": 0, "skipped": 0, "coalesced": 0, "results": []}
        for schedule in schedules:
            if not schedule["enabled"]:
                continue
            if not force and float(schedule["next_run_at"]) > now:
                continue
            item = self._run_one(owner_id, schedule, now=now)
            for key in ("executed", "failed", "skipped", "coalesced"):
                result[key] += int(item.get(key, 0))
            result["results"].append(item)
        result["ok"] = result["failed"] == 0
        return result

    def run_once(self, owner_id: str, schedule_id: str) -> dict[str, Any]:
        return self.tick_once(owner_id=owner_id, schedule_id=schedule_id, force=True)

    def start(self, *, stop_event: threading.Event | None = None):
        stop = stop_event if stop_event is not None else ScheduleStopHandle()

        def loop():
            after_owner_id = None
            while not stop.wait(self.poll_interval_s):
                try:
                    owners = self.repository.list_owners(after_owner_id=after_owner_id, limit=100)
                    if not owners and after_owner_id is not None:
                        after_owner_id = None
                        owners = self.repository.list_owners(limit=100)
                    for owner_id in owners:
                        try:
                            self.tick_once(owner_id=owner_id)
                        except Exception as exc:
                            log_failure(logger, "platform_schedule_owner_tick_failed", exc,
                                        worker_id=self.worker_id)
                    if owners:
                        after_owner_id = owners[-1]
                except Exception as exc:
                    log_failure(logger, "platform_schedule_tick_failed", exc,
                                worker_id=self.worker_id)
                    continue

        thread = threading.Thread(target=loop, name="platform-schedules", daemon=True)
        thread.start()
        if isinstance(stop, ScheduleStopHandle):
            stop.bind(thread)
        return stop


class ScheduleStopHandle(threading.Event):
    """Stop signal that can also wait for the scheduler thread to finish."""

    def __init__(self):
        super().__init__()
        self.thread: threading.Thread | None = None

    def bind(self, thread: threading.Thread) -> None:
        self.thread = thread

    def join(self, timeout: float | None = None) -> None:
        if self.thread is not None:
            self.thread.join(timeout)

    def __call__(self, timeout: float | None = None) -> None:
        self.set()
        self.join(timeout)


__all__ = ["DurableReadOnlyScheduleService", "PlatformScheduleError", "ScheduleStopHandle"]
