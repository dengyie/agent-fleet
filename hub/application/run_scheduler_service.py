"""Durable multi-owner scheduler for the platform Run worker."""
from __future__ import annotations

import logging
import secrets
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable

from hub.diagnostics import log_failure

logger = logging.getLogger(__name__)


class RunSchedulerService:
    """Fair owner selection with durable process-shared concurrency slots."""

    def __init__(self, repository, worker, *, scheduler_id: str | None = None,
                 owner_lease_s: float = 60.0, interval_s: float = 1.0,
                 max_concurrency: int = 1, max_workspace_concurrency: int = 1,
                 clock: Callable[[], float] = time.time):
        self.repository = repository
        self.worker = worker
        self.legacy_bridge = None
        self.scheduler_id = scheduler_id or ("run-scheduler_" + secrets.token_hex(8))
        self.owner_lease_s = max(5.0, min(3600.0, float(owner_lease_s)))
        self.interval_s = max(0.1, min(60.0, float(interval_s)))
        self.max_concurrency = max(1, min(64, int(max_concurrency)))
        self.max_workspace_concurrency = max(1, min(64, int(max_workspace_concurrency)))
        if self.worker is not None:
            self.worker.configure_workspace_concurrency(self.max_workspace_concurrency)
        self.clock = clock
        self._pool: ThreadPoolExecutor | None = None
        self._inflight: dict[Future, str] = {}
        self._dispatch_lock = threading.Lock()

    @staticmethod
    def _workspace_key(claim: dict) -> str | None:
        config = claim.get("config_snapshot") or {}
        workspace_id = config.get("workspace_id") or claim.get("conversation_workspace_id")
        return f"{claim['owner_id']}:{workspace_id}" if workspace_id else None

    def _execute(self, claim: dict, owner_lease: dict, slot: dict) -> dict:
        owner_id = claim["owner_id"]
        run_id = claim["run_id"]
        now = float(self.clock())
        if not self.repository.renew_worker_slot(
                slot["lease_id"], worker_id=self.scheduler_id,
                now=now, lease_s=self.worker.lease_s):
            try:
                self.repository.release_worker_slot(
                    slot["lease_id"], worker_id=self.scheduler_id)
            except Exception as exc:
                log_failure(logger, 'platform_slot_release_failed', exc, run_id=run_id)
            return {"run_id": run_id, "state": "scheduler_slot_lost"}
        heartbeat_stop = threading.Event()
        heartbeat_lost = threading.Event()

        def heartbeat():
            delay = max(0.5, min(10.0, min(self.owner_lease_s, self.worker.lease_s) / 3.0))
            while not heartbeat_stop.wait(delay):
                current = float(self.clock())
                try:
                    checks = (
                        self.repository.renew_worker_slot(
                            slot["lease_id"], worker_id=self.scheduler_id, now=current,
                            lease_s=self.worker.lease_s),
                        self.repository.renew_run_lease(
                            owner_id, run_id, lease_id=claim["lease_id"],
                            worker_id=self.worker.worker_id, now=current,
                            lease_s=self.worker.lease_s),
                    )
                except Exception as exc:
                    heartbeat_lost.set()
                    log_failure(logger, 'platform_heartbeat_failed', exc, run_id=run_id)
                    return
                if not all(checks):
                    heartbeat_lost.set()
                    return

        heartbeat_thread = threading.Thread(
            target=heartbeat, name="platform-run-heartbeat", daemon=True)
        heartbeat_thread.start()
        try:
            result = self.worker.execute_claim(claim)
            if heartbeat_lost.is_set():
                return {"run_id": run_id, "state": "lease_lost",
                        "attempt": claim["attempt"]}
            return result
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=2.0)
            try:
                self.repository.release_worker_slot(
                    slot["lease_id"], worker_id=self.scheduler_id)
            except Exception as exc:
                # Slot cleanup is durable housekeeping.  It must not rewrite
                # the worker result (or mask a worker exception); the expired
                # lease remains reclaimable by the repository's next sweep.
                log_failure(logger, 'platform_slot_release_failed', exc,
                            run_id=run_id, worker_id=self.scheduler_id)

    def _prepare_claim(self) -> tuple[dict, dict, dict] | dict | None:
        owner_lease = self.repository.claim_worker_owner(
            worker_id=self.scheduler_id, now=float(self.clock()), lease_s=self.owner_lease_s,
            max_workspace_concurrency=self.max_workspace_concurrency)
        if owner_lease is None:
            return None
        owner_id = owner_lease["owner_id"]
        if self.legacy_bridge is not None:
            try:
                self.legacy_bridge.process_once(owner_id)
            except Exception as exc:
                log_failure(logger, 'platform_legacy_bridge_failed', exc, worker_id=self.scheduler_id)
        claim = self.repository.claim_run(
            owner_id, worker_id=self.worker.worker_id, now=float(self.clock()),
            lease_s=self.worker.lease_s,
            max_workspace_concurrency=self.max_workspace_concurrency)
        if claim is None:
            self.repository.release_worker_owner(
                owner_id, lease_id=owner_lease["lease_id"], worker_id=self.scheduler_id,
                now=float(self.clock()))
            return {"owner_id": owner_id, "state": "owner_idle"}
        if claim.get("cancelled_before_claim") or claim.get("recovered_before_claim"):
            self.repository.release_worker_owner(
                owner_id, lease_id=owner_lease["lease_id"], worker_id=self.scheduler_id,
                now=float(self.clock()))
            return {"owner_id": owner_id, "run_id": claim["run_id"], "state": claim["state"]}
        workspace_key = self._workspace_key(claim)
        slot = self.repository.claim_worker_slot(
            owner_id, claim["run_id"], worker_id=self.scheduler_id,
            workspace_key=workspace_key, max_concurrency=self.max_concurrency,
            max_workspace_concurrency=self.max_workspace_concurrency,
            now=float(self.clock()), lease_s=self.worker.lease_s)
        if slot is None:
            self.repository.requeue_claimed_run(
                owner_id, claim["run_id"], lease_id=claim["lease_id"],
                worker_id=self.worker.worker_id, now=float(self.clock()))
            self.repository.release_worker_owner(
                owner_id, lease_id=owner_lease["lease_id"], worker_id=self.scheduler_id,
                now=float(self.clock()))
            return {"owner_id": owner_id, "run_id": claim["run_id"],
                    "state": "concurrency_deferred"}
        self.repository.release_worker_owner(
            owner_id, lease_id=owner_lease["lease_id"], worker_id=self.scheduler_id,
            now=float(self.clock()))
        return claim, owner_lease, slot

    def tick_once(self) -> dict | None:
        prepared = self._prepare_claim()
        if prepared is None or isinstance(prepared, dict):
            return prepared
        claim, owner_lease, slot = prepared
        return self._execute(claim, owner_lease, slot)

    def run_once(self, *, max_ticks: int | None = None) -> list[dict]:
        """Dispatch up to ``max_ticks`` runs and wait for their results."""
        results, futures = self._admit(max_ticks=max_ticks)
        for future in futures:
            with self._dispatch_lock:
                run_id = self._inflight.get(future)
            result = self._future_result(future, run_id)
            with self._dispatch_lock:
                if future in self._inflight:
                    del self._inflight[future]
                    results.append(result)
        return results

    def _future_result(self, future: Future, run_id: str | None) -> dict:
        try:
            return future.result()
        except Exception as exc:
            log_failure(logger, 'platform_worker_future_failed', exc,
                        run_id=run_id, worker_id=self.scheduler_id)
            return {"run_id": run_id, "state": "worker_error"}

    def _admit(self, *, max_ticks: int | None = None, stop=None) -> tuple[list[dict], list[Future]]:
        """Collect completed work and fill available slots without waiting."""
        with self._dispatch_lock:
            return self._admit_locked(max_ticks=max_ticks, stop=stop)

    def _admit_locked(self, *, max_ticks: int | None = None, stop=None):
        results = []
        for future, run_id in list(self._inflight.items()):
            if future.done():
                results.append(self._future_result(future, run_id))
                del self._inflight[future]
        ticks = max(1, min(64, int(max_ticks or self.max_concurrency)))
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=self.max_concurrency,
                                            thread_name_prefix="platform-run")
        futures = []
        for _ in range(ticks):
            if (len(self._inflight) >= self.max_concurrency
                    or (stop is not None and stop.is_set())):
                break
            prepared = self._prepare_claim()
            if prepared is None:
                break
            if isinstance(prepared, dict):
                results.append(prepared)
                if prepared.get("state") == "concurrency_deferred":
                    break
                continue
            claim, owner_lease, slot = prepared
            future = self._pool.submit(self._execute, claim, owner_lease, slot)
            self._inflight[future] = claim["run_id"]
            futures.append(future)
        return results, futures

    def snapshot(self) -> dict:
        return {
            "scheduler_id": self.scheduler_id,
            "max_concurrency": self.max_concurrency,
            "max_workspace_concurrency": self.max_workspace_concurrency,
        }

    def start(self, *, stop_event=None):
        stop = stop_event or threading.Event()

        def loop():
            delay = self.interval_s
            while not stop.is_set():
                try:
                    self._admit(stop=stop)
                    delay = self.interval_s
                except Exception as exc:
                    log_failure(logger, 'platform_scheduler_tick_failed', exc, worker_id=self.scheduler_id)
                    delay = min(30.0, max(self.interval_s, delay * 2))
                stop.wait(delay)

        thread = threading.Thread(target=loop, name="platform-run-scheduler", daemon=True)
        thread.start()

        def shutdown():
            stop.set()
            thread.join()
            if self._pool is not None:
                self._pool.shutdown(wait=True, cancel_futures=False)

        return shutdown


__all__ = ["RunSchedulerService"]
