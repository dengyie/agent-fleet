"""Durable, opt-in per-service HTTP probe monitoring."""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from hub.integrations.http_probe import (
    HttpProbeClient,
    HttpProbeError,
    HttpProbePolicy,
    parse_http_probe_policy,
)
from hub.infrastructure.platform_scheduler_repository import PlatformSchedulerRepository

logger = logging.getLogger(__name__)


def _bounded_interval(value: Any, *, default: float = 60.0) -> float:
    try:
        parsed = float(default if value is None else value)
    except (TypeError, ValueError):
        parsed = default
    if not math.isfinite(parsed):
        parsed = default
    return max(5.0, min(3600.0, parsed))


def _safe_code(value: Any, fallback: str = "probe_failed") -> str:
    text = str(value or fallback)[:120]
    return text if re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", text) else fallback


class HttpProbeMonitoringService:
    """Run stored per-service HTTP policies through one durable job lease.

    Service policy is reloaded from the repository for every cycle.  The
    scheduler has no URL or path argument, so a caller cannot redirect a probe
    at run time.
    """

    JOB_ID = "http_probe_sync"

    def __init__(
        self,
        service_repository,
        incident_service,
        scheduler_repository: PlatformSchedulerRepository,
        *,
        interval_s: float = 60.0,
        lease_s: float = 45.0,
        backoff_cap_s: float = 900.0,
        network_enabled: bool = False,
        allow_loopback: bool = False,
        allowed_origins: set[str] | frozenset[str] | None = None,
        resolver: Callable[..., Any] | None = None,
        client_factory: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.service_repository = service_repository
        self.incident_service = incident_service
        self.scheduler = scheduler_repository
        self.interval_s = _bounded_interval(interval_s)
        self.lease_s = max(5.0, min(3600.0, float(lease_s)))
        self.backoff_cap_s = max(self.interval_s, min(86400.0, float(backoff_cap_s)))
        self.network_enabled = bool(network_enabled)
        self.allow_loopback = bool(allow_loopback)
        self.allowed_origins = (
            None if allowed_origins is None else frozenset(allowed_origins)
        )
        self.resolver = resolver
        self.client_factory = client_factory or self._make_client
        self.clock = clock

    def _backoff(self, failures: int) -> float:
        return min(self.backoff_cap_s, self.interval_s * (2 ** min(max(0, int(failures)), 6)))

    def _make_client(self, policy: HttpProbePolicy, **_kwargs):
        return HttpProbeClient(
            policy.base_url,
            policy.path,
            timeout_s=policy.timeout_s,
            allow_network=self.network_enabled,
            allow_loopback=bool(policy.allow_loopback and self.allow_loopback),
            validate_addresses=True,
            resolver=self.resolver or __import__("socket").getaddrinfo,
        )

    def _policy(self, service: Mapping[str, Any]) -> HttpProbePolicy | None:
        checks = service.get("checks") or {}
        if not isinstance(checks, Mapping) or "http_probe" not in checks:
            return None
        policy = parse_http_probe_policy(checks["http_probe"])
        if not policy.enabled:
            return None
        if self.allowed_origins is not None and policy.base_url not in self.allowed_origins:
            raise HttpProbeError("endpoint_not_allowlisted")
        return policy

    def _is_due(
        self,
        owner_id: str,
        service: Mapping[str, Any],
        policy: HttpProbePolicy,
        now: float,
        *,
        force: bool,
    ) -> bool:
        if force:
            return True
        rows = self.service_repository.list_evidence(owner_id, service["service_id"], limit=500)
        latest = [
            float(row["observed_at"]) for row in rows
            if row.get("source") == "http_probe_v1"
            and (
                (row.get("detail") or {}).get("service_version") == service.get("version")
                or (
                    (row.get("detail") or {}).get("service_version") is None
                    and int(service.get("version", 0)) == 1
                )
            )
        ]
        return not latest or now - max(latest) >= policy.interval_s

    def sync_once(self, *, now: float | None = None, force: bool = False) -> dict[str, Any]:
        now = float(self.clock() if now is None else now)
        claim = self.scheduler.claim(self.JOB_ID, now=now, lease_s=self.lease_s, force=force)
        if claim is None:
            return {"ok": True, "skipped": True, "status": self.status()}
        lease_id = claim["lease_id"]
        failures = int(claim.get("consecutive_failures", 0))
        owners_count = 0
        probed = 0
        skipped = 0
        stale = 0
        failed = 0
        min_interval = self.interval_s
        try:
            owners = self.service_repository.list_monitoring_owners()
            for owner_id in owners[:100]:
                services = self.service_repository.list_services(owner_id)
                for service in services[:1000]:
                    try:
                        policy = self._policy(service)
                    except HttpProbeError as exc:
                        failure_result = self.incident_service.record_http_probe_failure(
                            owner_id, service, _safe_code(exc.code),
                            observed_at=now,
                            expected_service_version=service.get("version"),
                        )
                        if failure_result.get("stale_policy"):
                            stale += 1
                        else:
                            failed += 1
                        continue
                    if policy is None:
                        continue
                    owners_count += 1
                    min_interval = min(min_interval, policy.interval_s)
                    if not self._is_due(owner_id, service, policy, now, force=force):
                        skipped += 1
                        continue
                    try:
                        client = self.client_factory(
                            policy,
                            allow_network=self.network_enabled,
                            allow_loopback=self.allow_loopback,
                            resolver=self.resolver,
                        )
                        result = self.incident_service.sync_http_probe(
                            owner_id, service, client,
                            observed_at=now, ttl_s=policy.ttl_s,
                            expected_probe_id=policy.probe_id,
                            expected_service_version=service.get("version"),
                        )
                        if result.get("stale_policy"):
                            stale += 1
                        elif (result.get("evidence") or {}).get("state") == "unknown":
                            failed += 1
                        else:
                            probed += 1
                    except Exception as exc:
                        failure_result = self.incident_service.record_http_probe_failure(
                            owner_id, service, _safe_code(getattr(exc, "code", None)),
                            observed_at=now, ttl_s=policy.ttl_s,
                            expected_service_version=service.get("version"),
                        )
                        if failure_result.get("stale_policy"):
                            stale += 1
                        else:
                            failed += 1
            success = failed == 0
            next_run = now + (
                min_interval if success else self._backoff(failures + 1)
            )
            status = self.scheduler.finish(
                self.JOB_ID, lease_id, now=now, success=success,
                next_run_at=next_run,
                error_code=None if success else "probe_failed",
                result={
                    "services": owners_count,
                    "probed": probed,
                    "skipped": skipped,
                    "stale": stale,
                    "failed": failed,
                },
            )
            return {
                "ok": success, "services": owners_count, "probed": probed,
                "skipped": skipped, "stale": stale, "failed": failed,
                "status": status,
            }
        except Exception as exc:
            code = _safe_code(getattr(exc, "code", None))
            try:
                status = self.scheduler.finish(
                    self.JOB_ID, lease_id, now=now, success=False,
                    next_run_at=now + self._backoff(failures + 1),
                    error_code=code, result={"services": owners_count},
                )
            except Exception:
                status = self.status()
            logger.exception("http probe monitoring cycle failed")
            return {"ok": False, "error": code, "status": status}

    def status(self) -> dict[str, Any]:
        return self.scheduler.get(self.JOB_ID) or {
            "job_id": self.JOB_ID, "state": "idle", "next_run_at": 0.0,
        }

    def start(self, *, stop_event: threading.Event | None = None, initial_run: bool = True):
        stop = stop_event or threading.Event()

        def loop():
            if initial_run:
                try:
                    self.sync_once()
                except Exception:
                    logger.exception("initial HTTP probe cycle failed")
            while not stop.wait(self.interval_s):
                try:
                    self.sync_once()
                except Exception:
                    logger.exception("HTTP probe monitoring cycle failed")

        thread = threading.Thread(target=loop, name="http-probe-monitoring", daemon=True)
        thread.start()
        return stop


__all__ = ["HttpProbeMonitoringService"]
