"""Application assembly for the hub Flask process."""

from typing import Any

from flask import Flask

from hub.config import FleetConfig


def _platform_retention(config, platform_repository, session_search=None):
    """Bounded platform-table retention, run hourly by the lease reconciler.

    Also prunes ``redacted_events``, transcript ``audit`` and
    ``policy_signals`` in the transcript DB when session/adoption repositories
    are enabled (they live in their own SQLite file), the session search index
    when search is enabled, and finished tasks plus task audit in the task DB.
    History horizons follow ``config.history_retention_days``.
    """
    platform_db = config.platform_db if platform_repository is not None else None
    transcript_db = None
    if (getattr(config, "session_repositories_enabled", False)
            or getattr(config, "adoption_repositories_enabled", False)):
        transcript_db = getattr(config, "session_transcript_root", None)
    task_db = getattr(config, "task_db", None) if getattr(config, "tasks_enabled", False) else None
    if platform_db is None and transcript_db is None and task_db is None and session_search is None:
        return None
    from hub.infrastructure.platform_retention import PlatformRetention, RetentionPolicy
    policy = RetentionPolicy.with_history_days(
        float(getattr(config, "history_retention_days", 90.0)))
    return PlatformRetention(platform_db, policy, transcript_db=transcript_db,
                             task_db=task_db, session_search=session_search)


def create_app(
    config: FleetConfig,
    *,
    repositories: dict[str, Any] | None = None,
    publisher: Any | None = None,
) -> Flask:
    """Assemble a hub app from explicit configuration and optional adapters.

    Thin orchestrator over :mod:`hub.composition`: stores and services are
    built first (each step raises on a misconfigured enabled gate), then the
    Flask app is configured, the ``fleet`` extension is published and the
    HTTP surface is registered.  The step order is the historical order.
    """
    from hub.composition.app_config import configure_app, init_task_store
    from hub.composition.control_plane import build_control_plane
    from hub.composition.core import build_core
    from hub.composition.extensions import register_fleet_extension
    from hub.composition.platform import build_platform
    from hub.composition.routes import register_http
    from hub.composition.sessions import build_sessions

    repositories = repositories or {}
    platform = build_platform(config, repositories)
    sessions = build_sessions(config, repositories)
    core = build_core(config, repositories, publisher, sessions, platform)
    control = build_control_plane(config, repositories, core, sessions)

    app = Flask(__name__, template_folder=None, static_folder=None)
    configure_app(app, config, platform, sessions, control)
    init_task_store(app, core.task_repository)
    register_fleet_extension(
        app, config, repositories, core, platform, sessions, control,
        retention=_platform_retention(config, platform.platform_repository,
                                      sessions.session_search_service),
    )
    register_http(app, config, platform, sessions, control)
    return app


def start_background_jobs(
    app,
    *,
    reconcile_interval_s=60,
    lease_reconciler_interval_s=60,
    transcript_retention_interval_s=300,
    browser_session_reconciliation_interval_s=60,
    browser_capture_staging_cleanup_interval_s=300,
    browser_capture_retention_interval_s=300,
):
    """Start the backend lifecycle daemons for the real hub process.

    Pulls the app-bound ``ReconciliationService`` from the fleet extension, runs
    one synchronous stale-observation sweep (preserving the legacy boot
    ``collect_all`` behavior), then starts the ingest-reconciler and
    lease-reconciler daemons with injected callbacks. Returns
    ``{"reconciliation": stop, "lease_reconciler": stop}`` so callers can stop
    them. Entrypoint-only: ``create_app`` itself spawns no threads.
    """
    from hub.application.reconciliation_service import (
        start_lease_reconciler,
        start_reconciliation,
        start_transcript_retention,
        start_browser_session_reconciliation,
        start_browser_capture_staging_cleanup,
        start_browser_capture_retention,
    )

    fleet = app.extensions.get("fleet", {})
    service = fleet.get("services", {}).get("reconciliation")
    if service is None:
        raise RuntimeError("reconciliation service not wired in fleet extensions")

    try:
        service.reconcile_observation()
    except Exception as exc:
        from hub.diagnostics import log_failure
        log_failure(app.logger, "initial_observation_reconciliation_failed", exc)

    stops = {
        "reconciliation": start_reconciliation(
            service.reconcile_observation, interval_s=reconcile_interval_s),
        "lease_reconciler": start_lease_reconciler(
            service.reconcile_leases, interval_s=lease_reconciler_interval_s),
    }
    fleet_config = fleet.get("config")
    transcript_enabled = bool(
        getattr(fleet_config, "session_repositories_enabled", False)
        or getattr(fleet_config, "adoption_repositories_enabled", False)
    )
    transcript_repo = (
        fleet.get("repositories", {}).get("transcript")
        if transcript_enabled else None
    )
    if transcript_repo is not None:
        from datetime import datetime, timezone

        def purge_expired_transcript_batch():
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            return transcript_repo.purge_expired_raw(now=now)

        stops["transcript_retention"] = start_transcript_retention(
            purge_expired_transcript_batch,
            interval_s=transcript_retention_interval_s,
        )
    browser_reconciliation_enabled = bool(
        getattr(fleet_config, "platform_browser_enabled", False)
        and getattr(fleet_config, "platform_remote_execution_enabled", False)
        and getattr(fleet_config, "platform_require_command_signature", False)
    )
    browser_repo = fleet.get("repositories", {}).get("browser")
    if browser_reconciliation_enabled and browser_repo is not None:
        stops["browser_session_reconciliation"] = start_browser_session_reconciliation(
            browser_repo.reconcile_stale_sessions,
            interval_s=browser_session_reconciliation_interval_s,
        )
    artifact_store = fleet.get("services", {}).get("platform_artifacts")
    if browser_reconciliation_enabled and artifact_store is not None:
        stops["browser_capture_staging_cleanup"] = start_browser_capture_staging_cleanup(
            artifact_store.cleanup_staging,
            interval_s=browser_capture_staging_cleanup_interval_s,
        )
    capture_gc = fleet.get("repositories", {}).get("browser_capture_gc")
    if browser_reconciliation_enabled and capture_gc is not None:
        stops["browser_capture_retention"] = start_browser_capture_retention(
            capture_gc.cleanup_expired,
            interval_s=browser_capture_retention_interval_s,
        )
    muse_service = fleet.get("services", {}).get("muse")
    # Lazy persistence; only imported, explicitly opted-in accounts are
    # polled. A blank installation makes no outbound Muse requests.
    if muse_service is not None and muse_service.repository.path.exists():
        muse_service.start()
        stops["muse_approvals"] = muse_service.stop_event
    platform_monitoring = fleet.get("services", {}).get("platform_monitoring")
    if platform_monitoring is not None:
        stops["platform_monitoring"] = platform_monitoring.start()
    http_probe_monitoring = fleet.get("services", {}).get("http_probe_monitoring")
    if http_probe_monitoring is not None:
        stops["http_probe_monitoring"] = http_probe_monitoring.start()
    platform_schedules = fleet.get("services", {}).get("platform_schedules")
    if platform_schedules is not None:
        stops["platform_schedules"] = platform_schedules.start()
    if fleet.get("services", {}).get("platform_run_scheduler") is not None:
        stops["platform_run_scheduler"] = start_platform_run_scheduler(app)
    elif fleet.get("services", {}).get("platform_worker") is not None and app.config.get("DEV_OPERATOR"):
        stops["platform_worker"] = start_platform_worker(app)
    return stops


def start_platform_worker(app, *, owner_id: str | None = None, interval_s: float | None = None, stop_event=None):
    """Explicitly start the local platform Run worker.

    The worker is deliberately separate from legacy reconciliation jobs and is
    never started by ``create_app``. A single-owner deployment supplies
    ``owner_id``; multi-owner deployments use ``start_platform_run_scheduler``.
    """
    import threading

    fleet = app.extensions.get("fleet", {})
    worker = fleet.get("services", {}).get("platform_worker")
    if worker is None:
        raise RuntimeError("platform worker is disabled")
    owner = owner_id or app.config.get("DEV_OPERATOR")
    if not owner:
        raise RuntimeError("platform worker requires owner_id")
    stop = stop_event or threading.Event()
    delay = float(interval_s if interval_s is not None else app.config.get("PLATFORM_WORKER_INTERVAL_S", 1.0))
    delay = max(0.1, min(60.0, delay))

    def loop():
        while not stop.is_set():
            try:
                worker.run_once(owner)
            except Exception as exc:
                from hub.diagnostics import log_failure
                log_failure(app.logger, 'platform_worker_tick_failed', exc,
                            worker_id=worker.worker_id)
            bridge = fleet.get("services", {}).get("legacy_task_bridge")
            if bridge is not None:
                try:
                    bridge.process_once(owner)
                except Exception as exc:
                    from hub.diagnostics import log_failure
                    log_failure(app.logger, 'platform_legacy_bridge_failed', exc,
                                worker_id=worker.worker_id)
            stop.wait(delay)

    thread = threading.Thread(target=loop, name="platform-run-worker", daemon=True)
    thread.start()

    def shutdown():
        stop.set()
        thread.join(timeout=max(1.0, delay * 2))

    return shutdown


def start_platform_run_scheduler(app, *, interval_s: float | None = None, stop_event=None):
    """Explicitly start the durable multi-owner Run scheduler."""
    fleet = app.extensions.get("fleet", {})
    scheduler = fleet.get("services", {}).get("platform_run_scheduler")
    if scheduler is None:
        raise RuntimeError("platform worker scheduler is disabled")
    if interval_s is not None:
        scheduler.interval_s = max(0.1, min(60.0, float(interval_s)))
    return scheduler.start(stop_event=stop_event)
