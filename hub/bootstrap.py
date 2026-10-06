"""Application assembly for the hub Flask process."""

from typing import Any

from flask import Flask

from hub.config import FleetConfig


def _wire_legacy_paths(config: FleetConfig) -> None:
    """Keep legacy modules pointed at the explicitly selected repositories.

    The compatibility facades are still used by the current blueprints. This
    bridge is intentionally kept in bootstrap so new application code can use
    injected repositories without adding more module-level path lookups.
    """
    from hub import events, state, task_store

    state.STATE_DIR = config.state_dir
    events.EVENT_LOG = config.event_log
    task_store.DB_PATH = config.task_db


def _load_hosts_rows(hosts_file) -> list[dict]:
    """Read hosts.yaml rows (name / projects) for the task whitelist fallback.

    Missing or malformed hosts files yield an empty list so task creation
    simply falls back to the explicit project whitelist, if any.
    """
    from pathlib import Path as _Path

    try:
        import yaml

        path = _Path(hosts_file)
        if not path.exists():
            return []
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, ImportError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    return [dict(host) for host in (data.get("hosts") or []) if isinstance(host, dict)]


def create_app(
    config: FleetConfig,
    *,
    repositories: dict[str, Any] | None = None,
    publisher: Any | None = None,
) -> Flask:
    """Assemble a hub app from explicit configuration and optional adapters."""
    _wire_legacy_paths(config)

    from hub import events, task_store
    from hub.http.command_routes import bp as commands_bp
    from hub.http.errors import attach_request_id
    from hub.http.observe_routes import bp as observe_bp
    from hub.http.pages import bp as pages_bp
    from hub.http.task_routes import bp as tasks_bp
    from hub.http.v1.observe_routes import bp as observe_v1_bp
    from hub.http.v1.task_routes import bp as tasks_v1_bp
    from hub.application.event_publisher import EventPublisher
    from hub.application.observe_service import HostConfig, ObserveService
    from hub.application.reconciliation_service import ReconciliationService
    from hub.application.runner_service import RunnerService
    from hub.application.task_service import TaskHostPolicy, TaskService
    from hub.infrastructure.event_repository import JsonlEventRepository
    from hub.infrastructure.state_repository import JsonlObservationRepository
    from hub.infrastructure.task_repository import SqliteTaskRepository

    platform_service = None
    platform_repository = None
    platform_delivery = None
    conversation_service = None
    run_service = None
    run_event_service = None
    artifact_store = None
    service_health = None
    service_repository = None
    incident_repository = None
    incident_service = None
    diagnostic_service = None
    komari_client = None
    platform_scheduler_repository = None
    platform_monitoring = None
    http_probe_monitoring = None
    platform_schedule_repository = None
    platform_schedule_service = None
    platform_memory_repository = None
    platform_memory_service = None
    platform_memory_context_service = None
    approval_repository = None
    service_actions = None
    execution_window_repository = None
    execution_window_service = None
    platform_worker_service = None
    usage_repository = None
    platform_run_scheduler = None
    legacy_task_bridge = None
    command_inspection = None
    command_postcheck = None
    browser_repository = None
    submit_approval_service = None
    if config.platform_enabled:
        from hub.application.defaults_service import DefaultsService
        from hub.infrastructure.platform_db import PlatformRepository
        from hub.infrastructure.usage_repository import UsageRepository
        from hub.infrastructure.command_repository import CommandRepository
        from hub.application.command_delivery_service import CommandDeliveryService
        from hub.application.command_inspection_service import CommandInspectionService
        from hub.application.command_postcheck_service import CommandPostcheckService
        from hub.auth import load_platform_command_signing_key
        if config.platform_db is None:
            raise RuntimeError("platform_enabled requires a platform_db path")
        platform_repository = (repositories or {}).get("platform")
        if platform_repository is None:
            platform_repository = PlatformRepository(config.platform_db)
            platform_repository.init()
        usage_repository = (repositories or {}).get("platform_usage")
        if usage_repository is None:
            usage_repository = UsageRepository(
                config.platform_db,
                enforce_limits=bool(getattr(config, "platform_usage_limits_enabled", False)),
            )
            usage_repository.init()
        command_repository = (repositories or {}).get("platform_commands")
        if command_repository is None:
            command_repository = CommandRepository(config.platform_db)
            command_repository.init()
        command_signing_key = getattr(config, "platform_command_signing_raw", None)
        if command_signing_key is None:
            command_signing_key = load_platform_command_signing_key()
        platform_delivery = CommandDeliveryService(
            command_repository,
            signing_key=command_signing_key,
            require_signature=bool(getattr(config, "platform_require_command_signature", False)),
        )
        command_inspection = CommandInspectionService(command_repository)
        command_postcheck = CommandPostcheckService(command_repository, platform_delivery)
        platform_service = DefaultsService(platform_repository)
        from hub.application.conversation_service import ConversationService, RunService
        from hub.application.run_event_service import RunEventService
        from tools.platform.artifacts import ArtifactStore
        if config.service_monitoring_enabled:
            from hub.infrastructure.service_repository import ServiceRepository
            from hub.application.service_health_service import ServiceHealthService
        if getattr(config, "platform_browser_enabled", False):
            from hub.infrastructure.browser_repository import BrowserRepository
            browser_repository = (repositories or {}).get("browser")
            if browser_repository is None:
                browser_repository = BrowserRepository(config.platform_db)
            browser_repository.init()
            if getattr(config, "platform_browser_submit_enabled", False):
                from hub.application.conversation_service import SubmitApprovalService
                submit_approval_service = SubmitApprovalService(
                    browser_repository)
        conversation_service = ConversationService(platform_repository, platform_service)
        run_service = RunService(platform_repository,
                                 browser_repository=browser_repository)
        run_event_service = RunEventService(platform_repository)
        if config.platform_artifact_root is None:
            raise RuntimeError("platform_enabled requires a platform_artifact_root")
        artifact_store = ArtifactStore(config.platform_artifact_root)
        if getattr(config, "execution_windows_enabled", False):
            from hub.infrastructure.execution_window_repository import ExecutionWindowRepository
            from hub.application.execution_window_service import ExecutionWindowService

            execution_window_repository = (repositories or {}).get("execution_windows")
            if execution_window_repository is None:
                execution_window_repository = ExecutionWindowRepository(config.platform_db)
                execution_window_repository.init()
            execution_window_service = ExecutionWindowService(execution_window_repository)
        if config.service_monitoring_enabled:
            service_repository = (repositories or {}).get("platform_services")
            if service_repository is None:
                service_repository = ServiceRepository(config.platform_db)
                service_repository.init()
            service_health = ServiceHealthService(
                service_repository,
                probe_allowed_origins=getattr(config, "http_probe_allowed_origins", ()),
                probe_allow_loopback=getattr(config, "http_probe_allow_loopback", False),
            )
            command_postcheck.service_repository = service_repository
            command_postcheck.service_health = service_health
            from hub.infrastructure.incident_repository import IncidentRepository
            from hub.integrations.health_events import IncidentService
            from hub.application.diagnostic_service import DiagnosticService
            incident_repository = (repositories or {}).get("platform_incidents")
            if incident_repository is None:
                incident_repository = IncidentRepository(config.platform_db)
                incident_repository.init()
            incident_service = IncidentService(
                service_health, incident_repository,
                node_mapping=getattr(config, "komari_node_mapping", None),
                recovery_required=getattr(config, "incident_recovery_required", 2),
            )
            diagnostic_service = DiagnosticService(service_health, incident_repository)
            if getattr(config, "service_actions_enabled", False):
                from hub.infrastructure.approval_repository import ApprovalRepository
                from hub.application.service_action_service import ServiceActionService

                approval_repository = (repositories or {}).get("platform_approvals")
                if approval_repository is None:
                    approval_repository = ApprovalRepository(config.platform_db)
                    approval_repository.init()
                service_actions = ServiceActionService(
                    service_repository=service_repository,
                    approval_repository=approval_repository,
                    delivery=platform_delivery,
                    command_repository=command_repository,
                )
            if getattr(config, "komari_enabled", False):
                from hub.integrations.komari import KomariClient
                if not (config.komari_base_url and config.komari_nodes_path and config.komari_token):
                    raise RuntimeError("komari_enabled requires endpoint, nodes path, and token")
                komari_client = KomariClient(
                    config.komari_base_url, config.komari_token,
                    nodes_path=config.komari_nodes_path,
                    allow_network=bool(getattr(config, "komari_network_enabled", False)),
                )
                # The durable scheduler is a separate gate from the
                # request-triggered sync route.  Construct it only when the
                # complete read-only monitoring stack is explicitly enabled;
                # otherwise the default app creates no scheduler state or
                # background lifecycle.
                if getattr(config, "komari_sync_enabled", False):
                    from hub.application.platform_monitoring_service import (
                        PlatformMonitoringService,
                    )
                    from hub.infrastructure.platform_scheduler_repository import (
                        PlatformSchedulerRepository,
                    )

                    platform_scheduler_repository = (repositories or {}).get(
                        "platform_scheduler")
                    if platform_scheduler_repository is None:
                        platform_scheduler_repository = PlatformSchedulerRepository(
                            config.platform_db)
                    platform_scheduler_repository.init()
                    platform_monitoring = PlatformMonitoringService(
                        service_repository=service_repository,
                        incident_service=incident_service,
                        komari_client=komari_client,
                        scheduler_repository=platform_scheduler_repository,
                        interval_s=getattr(config, "komari_sync_interval_s", 60.0),
                    )
            if (
                getattr(config, "http_probe_enabled", False)
                and getattr(config, "http_probe_network_enabled", False)
                and getattr(config, "http_probe_sync_enabled", False)
            ):
                from hub.application.http_probe_monitoring_service import (
                    HttpProbeMonitoringService,
                )
                from hub.infrastructure.platform_scheduler_repository import (
                    PlatformSchedulerRepository,
                )
                if platform_scheduler_repository is None:
                    platform_scheduler_repository = (repositories or {}).get(
                        "platform_scheduler")
                    if platform_scheduler_repository is None:
                        platform_scheduler_repository = PlatformSchedulerRepository(
                            config.platform_db)
                    platform_scheduler_repository.init()
                http_probe_monitoring = HttpProbeMonitoringService(
                    service_repository=service_repository,
                    incident_service=incident_service,
                    scheduler_repository=platform_scheduler_repository,
                    interval_s=getattr(config, "http_probe_sync_interval_s", 60.0),
                    network_enabled=getattr(config, "http_probe_network_enabled", False),
                    allow_loopback=getattr(config, "http_probe_allow_loopback", False),
                    allowed_origins=getattr(config, "http_probe_allowed_origins", ()),
                )
        if getattr(config, "platform_schedules_enabled", False):
            from hub.infrastructure.platform_schedule_repository import (
                PlatformScheduleRepository,
            )
            from hub.application.platform_schedule_service import (
                DurableReadOnlyScheduleService,
            )

            platform_schedule_repository = (repositories or {}).get(
                "platform_schedules")
            if platform_schedule_repository is None:
                platform_schedule_repository = PlatformScheduleRepository(
                    config.platform_db)
            platform_schedule_repository.init()

            def _read_only_schedule_executor(owner_id, action, target):
                if service_health is None:
                    raise RuntimeError("service_monitoring_unavailable")
                service_id = target.get("service_id")
                if action == "service_health":
                    public = service_health.get(owner_id, service_id)
                    return {
                        "state": (public.get("health") or {}).get("overall", "unknown"),
                        "service_id": service_id,
                        "dimension": "overall",
                    }
                if action == "http_probe":
                    if http_probe_monitoring is None:
                        raise RuntimeError("http_probe_scheduler_unavailable")
                    public = service_health.get(owner_id, service_id)
                    dimension = ((public.get("health") or {}).get(
                        "dimensions") or {}).get("application_health") or {}
                    return {
                        "state": dimension.get("state", "unknown"),
                        "service_id": service_id,
                        "dimension": "application_health",
                        "freshness": dimension.get("freshness"),
                    }
                raise RuntimeError("invalid_action")

            platform_schedule_service = DurableReadOnlyScheduleService(
                platform_schedule_repository,
                executor=_read_only_schedule_executor,
                worker_id="hub-schedule",
            )

        if getattr(config, "platform_memory_enabled", False):
            from hub.infrastructure.platform_memory_repository import (
                PlatformMemoryRepository,
            )
            from hub.application.platform_memory_service import (
                PlatformMemoryService,
            )
            platform_memory_repository = (repositories or {}).get(
                "platform_memory")
            if platform_memory_repository is None:
                platform_memory_repository = PlatformMemoryRepository(
                    config.platform_db)
            platform_memory_repository.init()
            platform_memory_service = PlatformMemoryService(
                platform_memory_repository)
            if getattr(config, "platform_memory_context_enabled", False):
                from hub.application.platform_memory_context_service import (
                    PlatformMemoryContextService,
                )
                platform_memory_context_service = PlatformMemoryContextService(
                    platform_memory_repository)

        if getattr(config, "platform_worker_enabled", False):
            from hub.application.run_worker_service import LocalRunWorkerService
            platform_worker_service = LocalRunWorkerService(
                platform_repository, run_event_service,
                lease_s=getattr(config, "platform_worker_lease_s", 60.0),
                sandbox_launcher=getattr(config, "platform_sandbox_launcher", ()),
                artifact_store=artifact_store, diagnostics=diagnostic_service,
                provider_network_enabled=bool(getattr(
                    config, "platform_provider_network_enabled", False)),
                remote_execution_enabled=bool(getattr(
                    config, "platform_remote_execution_enabled", False)),
                remote_delivery=platform_delivery,
                usage_meter=usage_repository,
                browser_enabled=bool(getattr(config, "platform_browser_enabled", False)),
                browser_network_enabled=bool(getattr(
                    config, "platform_browser_network_enabled", False)),
                browser_allowed_origins=tuple(getattr(
                    config, "platform_browser_allowed_origins", ()) or ()),
                browser_submit_enabled=bool(getattr(
                    config, "platform_browser_submit_enabled", False)),
                submit_approvals=submit_approval_service,
            )
            if getattr(config, "platform_worker_scheduler_enabled", False):
                from hub.application.run_scheduler_service import RunSchedulerService
                platform_run_scheduler = RunSchedulerService(
                    platform_repository, platform_worker_service,
                    owner_lease_s=getattr(config, "platform_worker_lease_s", 60.0),
                    interval_s=getattr(config, "platform_worker_interval_s", 1.0),
                    max_concurrency=getattr(config, "platform_worker_max_concurrency", 1),
                    max_workspace_concurrency=getattr(
                        config, "platform_worker_max_workspace_concurrency", 1),
                )

    # Session (Task 6) services/repositories are optional.  When enabled they
    # use their own independent durable stores (session metadata + transcript),
    # never the legacy observation/event/task paths.  Imported lazily so the
    # disabled default adds no import cost or startup behavior change.
    session_service = None
    session_repo = None
    transcript_repo = None
    if config.session_repositories_enabled:
        from hub.application.session_service import SessionService
        from hub.infrastructure.session_repository import SessionRepository
        from hub.infrastructure.transcript_repository import TranscriptRepository
        from tools.session.crypto import load_encryption_key

        session_repo = (repositories or {}).get("sessions")
        if session_repo is None:
            if config.session_db is None:
                raise RuntimeError(
                    "session_repositories_enabled requires a session_db path")
            session_repo = SessionRepository(config.session_db)
            session_repo.init()
        transcript_repo = (repositories or {}).get("transcript")
        if transcript_repo is None:
            if config.session_transcript_root is None:
                raise RuntimeError(
                    "session_repositories_enabled requires a transcript root")
            transcript_repo = TranscriptRepository(
                config.session_transcript_root,
                key=load_encryption_key(config),
            )
            transcript_repo.init()
        session_service = SessionService(session_repo, transcript_repo)

    if publisher is None:
        event_repository = (repositories or {}).get("events")
        if event_repository is None:
            event_repository = JsonlEventRepository(config.event_log)
        publisher = EventPublisher(event_repository)
    if session_service is not None:
        session_service.event_publisher = publisher
    # Keep old Blueprints working while they receive injected services. New
    # callers should use the app extension rather than this facade.
    events.set_publisher(publisher)

    observation_repository = (repositories or {}).get("observation")
    if observation_repository is None:
        observation_repository = JsonlObservationRepository(config.state_dir)
    observe_service = ObserveService(
        observation_repo=observation_repository,
        event_publisher=publisher,
        host_config=HostConfig.load(config.hosts_file),
    )

    task_repository = (repositories or {}).get("task")
    if task_repository is None:
        task_repository = SqliteTaskRepository(config.task_db)
    task_service = TaskService(
        task_repo=task_repository,
        observation_repo=observation_repository,
        event_publisher=publisher,
        host_policy=TaskHostPolicy(
            project_whitelist=config.project_whitelist,
            hosts=_load_hosts_rows(config.hosts_file),
        ),
        session_repo=session_repo,
    )
    if platform_repository is not None:
        from hub.application.legacy_task_bridge import LegacyTaskBridge

        legacy_task_bridge = LegacyTaskBridge(
            platform_repository, task_service, task_repository,
            worker_id="legacy-bridge-" + (config.dev_operator or "local"),
        )
        if platform_run_scheduler is not None:
            platform_run_scheduler.legacy_bridge = legacy_task_bridge
    runner_service = RunnerService(task_repository, publisher)

    # Task 9: supervisor control-plane.  Construction is additive — when
    # ``supervisor_enabled`` is off, the service is built with no signing key
    # (fail-closed issuance) and no route is registered.  Signing key/credential
    # VALUES are never logged or printed; only their resolved source is noted.
    from hub.application.supervisor_service import SupervisorService
    from hub.auth import (
        load_supervisor_credentials,
        load_supervisor_signing_key,
    )

    # Signing key resolution: explicit raw bytes > env/file.  ``None`` (no
    # key anywhere) is the fail-closed state — commands can never be issued.
    supervisor_key = (
        config.supervisor_signing_raw
        if config.supervisor_signing_raw is not None
        else load_supervisor_signing_key()
    )
    supervisor_service = SupervisorService(
        signing_key=supervisor_key,
        ttl_s=float(config.supervisor_ttl_s or 3600.0),
        append_user_turn_enabled=bool(
            getattr(config, "append_user_turn_enabled", False)),
        apply_local_profile_enabled=bool(
            getattr(config, "apply_local_profile_enabled", False)),
    )

    if session_service is not None and config.supervisor_enabled:
        session_service.supervisor = supervisor_service

    # Task 10: connect task cancel to managed-attempt termination.  Additive
    # and gated: the control router exists only when BOTH the supervisor plane
    # is enabled AND session repositories (managed-session metadata) are on.
    # When the gate is off, ``cancel_router`` stays ``None`` and the TaskService
    # cancel surface is byte-identical to the pre-Task-10 behavior (no command,
    # no audit).  The receipt hook only overrides the ``receipt`` transport;
    # every other supervisor contract is forwarded unchanged.  No key/secret
    # VALUE is ever stored/logged by this wiring.
    control_router = None
    if bool(config.supervisor_enabled
            and session_service is not None
            and session_repo is not None):
        from hub.application.control_router import (
            ControlRouter,
            SupervisorReceiptHook,
        )

        control_router = ControlRouter(
            supervisor=supervisor_service,
            task_repository=task_repository,
            session_repo=session_repo,
        )
        supervisor_service = SupervisorReceiptHook(
            supervisor_service, control_router)
        task_service.cancel_router = control_router

    # Task 6: operator adoption (纳管) wiring.  Additive and gated by
    # ``adoption_repositories_enabled``: when off, no adoption service and no
    # route exist and the old surface is byte-identical.  The adoption store is
    # exactly the Task 4 store; the transcript audit store reuses the session
    # block's repository when both systems are enabled, otherwise it is built
    # the same way (a missing transcript root fails loudly like the session
    # block, never silently using None).
    adoption_service = None
    if config.adoption_repositories_enabled:
        from hub.application.adoption_service import AdoptionService
        from hub.infrastructure.adoption_repository import AdoptionRepository

        adoption_repo = (repositories or {}).get("adoptions")
        if adoption_repo is None:
            if config.adoption_db is None:
                raise RuntimeError(
                    "adoption_repositories_enabled requires an adoption_db path")
            adoption_repo = AdoptionRepository(config.adoption_db)
            adoption_repo.init()
        if transcript_repo is None:
            if config.session_transcript_root is None:
                raise RuntimeError(
                    "adoption_repositories_enabled requires a transcript root")
            from hub.infrastructure.transcript_repository import (
                TranscriptRepository,
            )
            from tools.session.crypto import load_encryption_key

            transcript_repo = TranscriptRepository(
                config.session_transcript_root,
                key=load_encryption_key(config),
            )
            transcript_repo.init()
        # Task 12: the FINAL receipt hook must also notify the adoption
        # service.  When the Task 10 gate already wrapped the supervisor in a
        # ``SupervisorReceiptHook`` it is reused (its router stays intact);
        # otherwise the same thin hook is built now so EVERY receipt the hub
        # records converges on the ONE wrapper.  Adoption-disabled mode never
        # reaches this branch — the hook stays adoption-free and the receipt
        # flow is byte-identical to today.
        from hub.application.control_router import SupervisorReceiptHook

        if not isinstance(supervisor_service, SupervisorReceiptHook):
            supervisor_service = SupervisorReceiptHook(
                supervisor_service, None)
        adoption_service = AdoptionService(
            observation_repository, adoption_repo, supervisor_service,
            transcript_repo)
        supervisor_service.set_adoption(adoption_service)

    app = Flask(__name__, template_folder=None, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 256 * 1024
    app.config["INGEST_TOKEN"] = config.ingest_token
    app.config["DEV_OPERATOR"] = config.dev_operator
    app.config["RUNNER_CREDENTIALS"] = config.runner_credentials
    app.config["PROJECT_WHITELIST"] = config.project_whitelist
    app.config["HOSTS_FILE"] = str(config.hosts_file)
    app.config["STATE_DIR"] = str(config.state_dir)
    app.config["EVENT_LOG"] = str(config.event_log)
    app.config["TASK_DB"] = str(config.task_db)
    app.config["PLATFORM_ENABLED"] = bool(config.platform_enabled)
    app.config["PLATFORM_DB"] = str(config.platform_db) if config.platform_db else None
    app.config["PLATFORM_REQUIRE_COMMAND_SIGNATURE"] = bool(
        getattr(config, "platform_require_command_signature", False))
    app.config["PLATFORM_WORKER_ENABLED"] = bool(platform_worker_service is not None)
    app.config["PLATFORM_WORKER_INTERVAL_S"] = float(
        getattr(config, "platform_worker_interval_s", 1.0))
    app.config["PLATFORM_WORKER_SCHEDULER_ENABLED"] = bool(platform_run_scheduler is not None)
    app.config["PLATFORM_PROVIDER_NETWORK_ENABLED"] = bool(
        getattr(config, "platform_provider_network_enabled", False))
    app.config["PLATFORM_REMOTE_EXECUTION_ENABLED"] = bool(
        getattr(config, "platform_remote_execution_enabled", False))
    app.config["PLATFORM_BROWSER_ENABLED"] = bool(
        getattr(config, "platform_browser_enabled", False))
    app.config["PLATFORM_BROWSER_NETWORK_ENABLED"] = bool(
        getattr(config, "platform_browser_network_enabled", False))
    app.config["SERVICE_MONITORING_ENABLED"] = bool(config.service_monitoring_enabled)
    app.config["PLATFORM_SCHEDULES_ENABLED"] = bool(
        platform_schedule_service is not None)
    app.config["PLATFORM_MEMORY_ENABLED"] = bool(
        platform_memory_service is not None)
    app.config["PLATFORM_MEMORY_CONTEXT_ENABLED"] = bool(
        platform_memory_context_service is not None)
    app.config["SERVICE_ACTIONS_ENABLED"] = bool(
        service_actions is not None)
    app.config["EXECUTION_WINDOWS_ENABLED"] = bool(
        execution_window_service is not None)
    app.config["KOMARI_ENABLED"] = bool(getattr(config, "komari_enabled", False))
    # Expose the effective gate: a requested sync gate without a complete
    # Komari integration never starts a scheduler and therefore has no status
    # surface to advertise.
    app.config["KOMARI_SYNC_ENABLED"] = bool(platform_monitoring is not None)
    app.config["KOMARI_SYNC_INTERVAL_S"] = float(
        getattr(config, "komari_sync_interval_s", 60.0))
    app.config["HTTP_PROBE_ENABLED"] = bool(
        getattr(config, "http_probe_enabled", False))
    app.config["HTTP_PROBE_NETWORK_ENABLED"] = bool(
        getattr(config, "http_probe_network_enabled", False))
    app.config["HTTP_PROBE_SYNC_ENABLED"] = bool(
        http_probe_monitoring is not None)
    app.config["HTTP_PROBE_SYNC_INTERVAL_S"] = float(
        getattr(config, "http_probe_sync_interval_s", 60.0))
    app.config["TASKS_ENABLED"] = bool(config.tasks_enabled)
    app.config["SERVE_FRONTEND"] = bool(config.serve_frontend)
    app.config["FRONTEND_DIR"] = str(config.frontend_dir)
    app.config["SUPERVISOR_ENABLED"] = bool(config.supervisor_enabled)
    app.config["SUPERVISOR_TTL_S"] = float(config.supervisor_ttl_s or 3600.0)
    app.config["APPEND_USER_TURN_ENABLED"] = bool(
        getattr(config, "append_user_turn_enabled", False))
    app.config["APPLY_LOCAL_PROFILE_ENABLED"] = bool(
        getattr(config, "apply_local_profile_enabled", False))
    # Credentials mapping (machine -> secret) — the mapping itself is stored
    # for comparison; the VALUES are secrets and never appear in any log/error/
    # report.  When supplied in the config it is used verbatim by
    # ``require_supervisor``; otherwise it is loaded from env/file.
    supervisor_credentials = config.supervisor_credentials
    if supervisor_credentials is None:
        supervisor_credentials = load_supervisor_credentials()
    app.config["SUPERVISOR_CREDENTIALS"] = supervisor_credentials

    if app.config["TASKS_ENABLED"]:
        # The injected task repository is the canonical init owner; it is
        # equivalent to the legacy ``task_store.init_db()`` (which delegates to
        # ``SqliteTaskRepository.init``). When init fails the task store is
        # disabled but observation routes remain available.
        task_init = getattr(task_repository, "init", None)
        try:
            if task_init is not None:
                task_init()
            else:
                task_store.init_db()
        except Exception:
            # Task storage is optional; observation routes remain available.
            app.config["TASKS_ENABLED"] = False

    app.extensions["fleet"] = {
        "config": config,
        "repositories": repositories or {},
        "publisher": publisher,
        "services": {
            "observe": observe_service,
            "tasks": task_service,
            "runner": runner_service,
            "supervisor": supervisor_service,
            "reconciliation": ReconciliationService(
                observe_service=observe_service,
                task_repository=task_repository,
                event_publisher=publisher,
            ),
        },
    }
    if platform_repository is not None:
        app.extensions["fleet"]["platform_repository"] = platform_repository
        app.extensions["fleet"]["platform_commands"] = command_repository
        if browser_repository is not None:
            app.extensions["fleet"]["repositories"]["browser"] = browser_repository
        if submit_approval_service is not None:
            app.extensions["fleet"]["services"]["submit_approvals"] = submit_approval_service
    if platform_scheduler_repository is not None:
        app.extensions["fleet"]["repositories"]["platform_scheduler"] = (
            platform_scheduler_repository)
    if platform_service is not None:
        app.extensions["fleet"]["services"]["platform_defaults"] = platform_service
        app.extensions["fleet"]["services"]["conversations"] = conversation_service
        app.extensions["fleet"]["services"]["runs"] = run_service
        app.extensions["fleet"]["services"]["run_events"] = run_event_service
        if usage_repository is not None:
            app.extensions["fleet"]["services"]["usage_repository"] = usage_repository
        if platform_worker_service is not None:
            app.extensions["fleet"]["services"]["platform_worker"] = platform_worker_service
        if platform_run_scheduler is not None:
            app.extensions["fleet"]["services"]["platform_run_scheduler"] = platform_run_scheduler
        if legacy_task_bridge is not None:
            app.extensions["fleet"]["services"]["legacy_task_bridge"] = legacy_task_bridge
        app.extensions["fleet"]["services"]["platform_delivery"] = platform_delivery
        app.extensions["fleet"]["services"]["command_inspection"] = command_inspection
        app.extensions["fleet"]["services"]["command_postcheck"] = command_postcheck
        app.extensions["fleet"]["services"]["platform_artifacts"] = artifact_store
        if service_health is not None:
            app.extensions["fleet"]["services"]["service_health"] = service_health
        if service_repository is not None:
            app.extensions["fleet"]["repositories"]["platform_services"] = (
                service_repository)
        if incident_repository is not None:
            app.extensions["fleet"]["repositories"]["platform_incidents"] = incident_repository
        if incident_service is not None:
            app.extensions["fleet"]["services"]["incidents"] = incident_service
        if diagnostic_service is not None:
            app.extensions["fleet"]["services"]["diagnostics"] = diagnostic_service
        if approval_repository is not None:
            app.extensions["fleet"]["repositories"]["platform_approvals"] = (
                approval_repository)
        if service_actions is not None:
            app.extensions["fleet"]["services"]["service_actions"] = service_actions
        if execution_window_repository is not None:
            app.extensions["fleet"]["repositories"]["execution_windows"] = (
                execution_window_repository)
        if execution_window_service is not None:
            app.extensions["fleet"]["services"]["execution_windows"] = (
                execution_window_service)
        if platform_monitoring is not None:
            app.extensions["fleet"]["services"]["platform_monitoring"] = (
                platform_monitoring)
        if http_probe_monitoring is not None:
            app.extensions["fleet"]["services"]["http_probe_monitoring"] = (
                http_probe_monitoring)
        if platform_schedule_repository is not None:
            app.extensions["fleet"]["repositories"]["platform_schedules"] = (
                platform_schedule_repository)
        if platform_schedule_service is not None:
            app.extensions["fleet"]["services"]["platform_schedules"] = (
                platform_schedule_service)
        if platform_memory_repository is not None:
            app.extensions["fleet"]["repositories"]["platform_memory"] = (
                platform_memory_repository)
        if platform_memory_service is not None:
            app.extensions["fleet"]["services"]["platform_memory"] = (
                platform_memory_service)
        if platform_memory_context_service is not None:
            app.extensions["fleet"]["services"]["platform_memory_context"] = (
                platform_memory_context_service)
            conversation_service.memory_context = platform_memory_context_service
        if komari_client is not None:
            app.extensions["fleet"]["integrations"] = {"komari": komari_client}
    if session_service is not None:
        app.extensions["fleet"]["services"]["sessions"] = session_service
    if control_router is not None:
        app.extensions["fleet"]["services"]["control_router"] = control_router
    if adoption_service is not None:
        app.extensions["fleet"]["services"]["adoptions"] = adoption_service

    from hub.http.operator_routes import bp as operator_bp
    app.register_blueprint(operator_bp)
    app.register_blueprint(observe_bp)
    app.register_blueprint(tasks_bp)
    app.register_blueprint(commands_bp)
    app.register_blueprint(pages_bp)
    # 版本化 /api/v1 兼容表面：复用旧 view，旧 /api/* 仍权威。
    app.register_blueprint(observe_v1_bp)
    app.register_blueprint(tasks_v1_bp)
    if platform_service is not None:
        from hub.http.platform_routes import bp as platform_bp
        from hub.http.conversation_routes import bp as conversation_bp
        from hub.http.node_routes import bp as node_bp
        from hub.http.artifact_routes import bp as artifact_bp
        from hub.http.legacy_task_routes import bp as legacy_task_bp
        from hub.http.execution_window_routes import bp as execution_window_bp
        app.register_blueprint(platform_bp)
        app.register_blueprint(conversation_bp)
        app.register_blueprint(node_bp)
        app.register_blueprint(artifact_bp)
        app.register_blueprint(legacy_task_bp)
        # Keep a stable 404 transport while the execution-window gate is
        # closed; the service/repository are still absent in that mode.
        app.register_blueprint(execution_window_bp)
        if service_health is not None:
            from hub.http.service_routes import bp as service_bp
            app.register_blueprint(service_bp)
            from hub.http.incident_routes import bp as incident_bp
            app.register_blueprint(incident_bp)
            # Register the bounded transport even while the gate is closed so
            # an attempted action receives the stable 404 contract rather than
            # Flask's method-not-allowed response caused by the service detail
            # route sharing the same prefix.
            from hub.http.service_action_routes import bp as service_action_bp
            app.register_blueprint(service_action_bp)
        if platform_schedule_service is not None:
            from hub.http.schedule_routes import bp as schedule_bp
            app.register_blueprint(schedule_bp)
        if platform_memory_service is not None:
            from hub.http.memory_routes import bp as memory_bp
            app.register_blueprint(memory_bp)

    # Task 6: session 蓝图仅在显式启用时注册。禁用（默认）不注册任何 session
    # 路由，旧 observation/task/runner/SSE 行为与 startup 完全不变。
    if session_service is not None:
        from hub.http.session_routes import bp as session_bp
        from hub.http.v1.session_routes import bp as session_v1_bp

        app.register_blueprint(session_bp)
        app.register_blueprint(session_v1_bp)

    # Task 9: supervisor 蓝图仅在显式启用时注册（无 503/404 歧义，旧 surface 不变）。
    # 有签名 key 时 supervisor_service 才真正签发命令；无 key = fail-closed。
    if app.config["SUPERVISOR_ENABLED"]:
        from hub.http.supervisor_routes import bp as supervisor_bp

        app.register_blueprint(supervisor_bp)

    # Task 6: adoption 蓝图仅在显式启用时注册（无 503/404 歧义，旧 surface 不变）。
    # ``adoption_service`` is None unless ``adoption_repositories_enabled`` so a
    # disabled flag never exposes the operator adoption surface.
    if adoption_service is not None:
        from hub.http.adoption_routes import bp as adoption_bp

        app.register_blueprint(adoption_bp)

    # 每个请求在 transport 边界打上不透明 request_id（错误响应携带）
    app.before_request(attach_request_id)

    # 统一 HTTP 错误契约：/api/* 的 404/405/意外异常使用有界 JSON，
    # 页面路由保留兼容 plain-text。
    from hub.http.errors import register_error_handlers

    register_error_handlers(app)

    return app


def start_background_jobs(app, *, reconcile_interval_s=60, lease_reconciler_interval_s=60):
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
    )

    fleet = app.extensions.get("fleet", {})
    service = fleet.get("services", {}).get("reconciliation")
    if service is None:
        raise RuntimeError("reconciliation service not wired in fleet extensions")

    try:
        service.reconcile_observation()
    except Exception:
        pass

    stops = {
        "reconciliation": start_reconciliation(
            service.reconcile_observation, interval_s=reconcile_interval_s),
        "lease_reconciler": start_lease_reconciler(
            service.reconcile_leases, interval_s=lease_reconciler_interval_s),
    }
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
    import time

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
