"""Bounded background monitoring and read-only Komari synchronization."""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections.abc import Callable
from typing import Any

from hub.infrastructure.platform_scheduler_repository import PlatformSchedulerRepository

logger = logging.getLogger(__name__)
_ERROR_CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")


def _safe_error_code(value: Any, fallback: str) -> str:
    try:
        candidate = str(value or fallback)[:120]
    except Exception:
        return fallback
    return candidate if _ERROR_CODE_RE.fullmatch(candidate) else fallback


def _bounded_interval(value: Any, *, default: float = 60.0) -> float:
    try:
        parsed = float(default if value is None else value)
    except (TypeError, ValueError):
        parsed = default
    if not math.isfinite(parsed):
        parsed = default
    return max(5.0, min(3600.0, parsed))


class PlatformMonitoringService:
    """Runs one owner-scoped Komari snapshot cycle at a time.

    The service has no Flask dependency and never starts a thread during
    construction. ``sync_once`` is deterministic for tests and the process
    entrypoint uses ``start`` to schedule it. A failed source still delegates
    to ``IncidentService.sync_from_client`` so every registered service gets
    an explicit unknown/source-unavailable evidence record.
    """

    JOB_ID = "komari_sync"

    def __init__(
        self,
        service_repository,
        incident_service,
        komari_client,
        scheduler_repository: PlatformSchedulerRepository,
        *,
        interval_s: float = 60.0,
        lease_s: float = 45.0,
        backoff_cap_s: float = 900.0,
        clock: Callable[[], float] = time.time,
    ):
        self.service_repository = service_repository
        self.incident_service = incident_service
        self.komari_client = komari_client
        self.scheduler = scheduler_repository
        self.interval_s = _bounded_interval(interval_s)
        self.lease_s = max(5.0, min(3600.0, float(lease_s)))
        self.backoff_cap_s = max(self.interval_s, min(86400.0, float(backoff_cap_s)))
        self.clock = clock

    def _backoff(self, failures: int) -> float:
        return min(self.backoff_cap_s, self.interval_s * (2 ** min(max(0, int(failures)), 6)))

    def sync_once(self, *, now: float | None = None, force: bool = False) -> dict[str, Any]:
        now = float(self.clock() if now is None else now)
        claim = self.scheduler.claim(self.JOB_ID, now=now, lease_s=self.lease_s, force=force)
        if claim is None:
            status = self.scheduler.get(self.JOB_ID)
            return {"ok": True, "skipped": True, "status": status}
        lease_id = claim["lease_id"]
        failures = int(claim.get("consecutive_failures", 0))
        results: list[dict[str, Any]] = []
        owners: list[str] = []
        source_error = None
        try:
            owners = self.service_repository.list_monitoring_owners()
            snapshots = self.komari_client.fetch_nodes(observed_at=now)
        except Exception as exc:
            source_error = _safe_error_code(
                getattr(exc, "code", None), "source_unavailable")
            snapshots = []
        try:
            for owner_id in owners[:100]:
                services = self.service_repository.list_services(owner_id)
                if not services:
                    continue
                synced = self.incident_service.sync(
                    owner_id, services, snapshots, source_error=source_error,
                )
                results.append({"owner_id": owner_id, "result": synced})
            success = source_error is None
            next_run = now + (self.interval_s if success else self._backoff(failures + 1))
            status = self.scheduler.finish(
                self.JOB_ID, lease_id, now=now, success=success,
                next_run_at=next_run, error_code=source_error,
                result={"owners": len(results), "source_error": source_error},
            )
            return {"ok": success, "source_error": source_error, "owners": results, "status": status}
        except Exception as exc:
            error_code = _safe_error_code(getattr(exc, "code", None), "sync_failed")
            try:
                status = self.scheduler.finish(
                    self.JOB_ID, lease_id, now=now, success=False,
                    next_run_at=now + self._backoff(failures + 1),
                    error_code=error_code, result={"owners": len(results)},
                )
            except Exception:
                status = self.scheduler.get(self.JOB_ID)
            logger.exception("platform monitoring cycle failed")
            return {"ok": False, "error": error_code, "owners": results, "status": status}

    def status(self) -> dict[str, Any]:
        return self.scheduler.get(self.JOB_ID) or {"job_id": self.JOB_ID, "state": "idle", "next_run_at": 0.0}

    def start(self, *, stop_event: threading.Event | None = None, initial_run: bool = True):
        stop = stop_event or threading.Event()

        def loop():
            if initial_run:
                try:
                    self.sync_once()
                except Exception:
                    logger.exception("initial platform monitoring cycle failed")
            while not stop.wait(self.interval_s):
                try:
                    self.sync_once()
                except Exception:
                    logger.exception("platform monitoring cycle failed")

        thread = threading.Thread(target=loop, name="platform-monitoring", daemon=True)
        thread.start()
        return stop


__all__ = ["PlatformMonitoringService"]
