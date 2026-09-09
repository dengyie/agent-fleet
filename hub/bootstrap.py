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

    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["MAX_CONTENT_LENGTH"] = 256 * 1024
    app.config["INGEST_TOKEN"] = config.ingest_token
    app.config["DEV_OPERATOR"] = config.dev_operator
    app.config["RUNNER_CREDENTIALS"] = config.runner_credentials
    app.config["PROJECT_WHITELIST"] = config.project_whitelist
    app.config["HOSTS_FILE"] = str(config.hosts_file)
    app.config["STATE_DIR"] = str(config.state_dir)
    app.config["EVENT_LOG"] = str(config.event_log)
    app.config["TASK_DB"] = str(config.task_db)
    app.config["TASKS_ENABLED"] = bool(config.tasks_enabled)
    app.config["FRONTEND_CUTOVER"] = bool(config.frontend_cutover)
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
    if session_service is not None:
        app.extensions["fleet"]["services"]["sessions"] = session_service
    if control_router is not None:
        app.extensions["fleet"]["services"]["control_router"] = control_router
    if adoption_service is not None:
        app.extensions["fleet"]["services"]["adoptions"] = adoption_service

    app.register_blueprint(observe_bp)
    app.register_blueprint(tasks_bp)
    app.register_blueprint(commands_bp)
    app.register_blueprint(pages_bp)
    # 版本化 /api/v1 兼容表面：复用旧 view，旧 /api/* 仍权威。
    app.register_blueprint(observe_v1_bp)
    app.register_blueprint(tasks_v1_bp)

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

    return {
        "reconciliation": start_reconciliation(
            service.reconcile_observation, interval_s=reconcile_interval_s),
        "lease_reconciler": start_lease_reconciler(
            service.reconcile_leases, interval_s=lease_reconciler_interval_s),
    }
