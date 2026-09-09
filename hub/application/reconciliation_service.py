"""Application-layer reconciliation and lifecycle service.

Owns the background lifecycle use cases that previously lived in the ``web``
and ``scan`` entrypoints:

- stale push-observation reconciliation (mark expired snapshots offline)
- task lease expiration and requeue (``task_update`` per requeued task)
- task TTL expiration (queued -> expired)

The service is push-only. Reconciliation only calls the injected observation
service, the injected task repository, and the injected event publisher; it
never launches child processes, never opens SSH/hub-to-agent connections, never
touches runner adapters or agent commands, and never imports Flask request or
application globals. The daemon threads that drive these cycles are started
explicitly by the process entrypoint via :func:`start_reconciliation` /
:func:`start_lease_reconciler` (or ``hub.bootstrap.start_background_jobs``);
constructing the service itself spawns nothing.
"""

import logging
import threading
import time

logger = logging.getLogger(__name__)


class ReconciliationService:
    """Push-only lifecycle service for stale reports and task leases."""

    def __init__(self, observe_service, task_repository, event_publisher, *,
                 clock=time.time) -> None:
        self.observe = observe_service
        self.repo = task_repository
        self.publisher = event_publisher
        self._clock = clock

    # -- observation reconciliation ----------------------------------------

    def reconcile_observation(self, machine=None, *, now=None) -> list[dict]:
        """Mark stale push snapshots offline via the injected observe service.

        ``machine`` restricts reconciliation to one machine (None = all). The
        observe service persists the offline snapshot and emits
        ``state_changed``; no process or network access happens here.
        """
        if now is None:
            now = self._clock()
        return self.observe.reconcile(machine=machine, now=now)

    # -- task lease and TTL lifecycle --------------------------------------

    def expire_task_leases(self, *, now=None) -> list[str]:
        """Expire stale leases and requeue their tasks.

        Calls the task repository's ``expire_leases`` and emits exactly one
        ``task_update`` (``state=queued``) for each requeued task id. A
        repository failure is logged and degrades the lease lifecycle without
        bringing down the observation/service loop.
        """
        if now is None:
            now = self._clock()
        try:
            requeued = self.repo.expire_leases(now=now)
        except Exception as exc:
            logger.exception("task lease expiration failed: %r", exc)
            return []
        for task_id in requeued:
            self._announce_requeued(task_id)
        return requeued

    def expire_tasks(self, *, now=None) -> list[str]:
        """Mark queued tasks past their TTL as expired via the repository."""
        if now is None:
            now = self._clock()
        try:
            return self.repo.expire_tasks(now=now)
        except Exception as exc:
            logger.exception("task TTL expiration failed: %r", exc)
            return []

    def reconcile_leases(self, *, now=None) -> list[str]:
        """Run the full lease sweep: requeue expired leases, then expire stale tasks.

        Mirrors the legacy single-cycle behavior (``web.reconcile_leases_once``)
        while routing both writes through the repository facade.
        """
        if now is None:
            now = self._clock()
        requeued = self.expire_task_leases(now=now)
        self.expire_tasks(now=now)
        return requeued

    # -- helpers ------------------------------------------------------------

    def _announce_requeued(self, task_id: str) -> None:
        try:
            self.publisher.emit("task_update", task_id=task_id, state="queued")
        except Exception:
            pass


def _start_daemon(thread_name, callback, stop_event, interval_s):
    """Run ``callback`` every ``interval_s`` seconds until the stop event is set."""
    interval_s = max(1, int(interval_s))
    stop = stop_event if stop_event is not None else threading.Event()

    def loop():
        while not stop.wait(interval_s):
            try:
                callback()
            except Exception:
                pass

    thread = threading.Thread(target=loop, name=thread_name, daemon=True)
    thread.start()
    return stop


def start_reconciliation(callback, stop_event=None, interval_s=60):
    """Start the ingest-reconciler daemon for the running hub process.

    ``callback`` is injected (typically ``ReconciliationService.
    reconcile_observation``); the daemon never imports Flask globals and never
    executes remote processes. Returns the ``threading.Event`` used to stop it.
    """
    return _start_daemon("ingest-reconciler", callback, stop_event, interval_s)


def start_lease_reconciler(callback, stop_event=None, interval_s=60):
    """Start the lease-reconciler daemon for the running hub process.

    ``callback`` is injected (typically ``ReconciliationService.
    reconcile_leases``). Returns the ``threading.Event`` used to stop it.
    """
    return _start_daemon("lease-reconciler", callback, stop_event, interval_s)
