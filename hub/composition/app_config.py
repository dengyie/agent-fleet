"""``app.config`` population: static settings plus the effective feature gates."""


def configure_app(app, config, platform, sessions, control):
    """Copy settings and *effective* gates (service actually wired) into config."""
    app.config["SESSION_SEARCH_DIAGNOSTIC"] = sessions.search_status
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
    app.config["PLATFORM_WORKFLOWS_ENABLED"] = bool(config.platform_workflows_enabled)
    app.config["PLATFORM_WORKFLOW_DISPATCH_ENABLED"] = bool(config.platform_workflow_dispatch_enabled)
    app.config["PLATFORM_CAPABILITY_ROUTING_ENABLED"] = bool(config.platform_capability_routing_enabled)
    app.config["PLATFORM_EXTERNAL_AGENTS_ENABLED"] = bool(config.platform_external_agents_enabled)
    app.config["PLATFORM_FLEET_MCP_ENABLED"] = bool(config.platform_fleet_mcp_enabled)
    app.config["PLATFORM_ARTIFACT_TRANSFER_ENABLED"] = bool(config.platform_artifact_transfer_enabled)
    app.config["PLATFORM_MCP_CONNECTORS_ENABLED"] = bool(config.platform_mcp_connectors_enabled)
    # 实际生效的会话/纳管门控（服务真正接线才为 True）。
    app.config["SESSION_REPOSITORIES_ENABLED"] = bool(sessions.session_service is not None)
    app.config["ADOPTION_REPOSITORIES_ENABLED"] = bool(control.adoption_service is not None)
    app.config["PLATFORM_DB"] = str(config.platform_db) if config.platform_db else None
    app.config["PLATFORM_REQUIRE_COMMAND_SIGNATURE"] = bool(
        getattr(config, "platform_require_command_signature", False))
    app.config["PLATFORM_WORKER_ENABLED"] = bool(platform.platform_worker_service is not None)
    app.config["PLATFORM_WORKER_INTERVAL_S"] = float(
        getattr(config, "platform_worker_interval_s", 1.0))
    app.config["PLATFORM_WORKER_SCHEDULER_ENABLED"] = bool(
        platform.platform_run_scheduler is not None)
    app.config["PLATFORM_PROVIDER_NETWORK_ENABLED"] = bool(
        getattr(config, "platform_provider_network_enabled", False))
    app.config["PLATFORM_REMOTE_EXECUTION_ENABLED"] = bool(
        getattr(config, "platform_remote_execution_enabled", False))
    app.config["PLATFORM_BROWSER_ENABLED"] = bool(
        getattr(config, "platform_browser_enabled", False))
    app.config["PLATFORM_BROWSER_SUBMIT_ENABLED"] = bool(getattr(config, "platform_browser_submit_enabled", False))
    app.config["PLATFORM_BROWSER_NETWORK_ENABLED"] = bool(
        getattr(config, "platform_browser_network_enabled", False))
    app.config["SERVICE_MONITORING_ENABLED"] = bool(config.service_monitoring_enabled)
    app.config["PLATFORM_SCHEDULES_ENABLED"] = bool(
        platform.platform_schedule_service is not None)
    app.config["PLATFORM_MEMORY_ENABLED"] = bool(
        platform.platform_memory_service is not None)
    app.config["PLATFORM_MEMORY_CONTEXT_ENABLED"] = bool(
        platform.platform_memory_context_service is not None)
    app.config["SERVICE_ACTIONS_ENABLED"] = bool(
        platform.service_actions is not None)
    app.config["EXECUTION_WINDOWS_ENABLED"] = bool(
        platform.execution_window_service is not None)
    app.config["KOMARI_ENABLED"] = bool(getattr(config, "komari_enabled", False))
    # Expose the effective gate: a requested sync gate without a complete
    # Komari integration never starts a scheduler and therefore has no status
    # surface to advertise.
    app.config["KOMARI_SYNC_ENABLED"] = bool(platform.platform_monitoring is not None)
    app.config["KOMARI_SYNC_INTERVAL_S"] = float(
        getattr(config, "komari_sync_interval_s", 60.0))
    app.config["HTTP_PROBE_ENABLED"] = bool(
        getattr(config, "http_probe_enabled", False))
    app.config["HTTP_PROBE_NETWORK_ENABLED"] = bool(
        getattr(config, "http_probe_network_enabled", False))
    app.config["HTTP_PROBE_SYNC_ENABLED"] = bool(
        platform.http_probe_monitoring is not None)
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
        from hub.auth import CredentialStoreUnavailable, load_supervisor_credentials
        try:
            supervisor_credentials = load_supervisor_credentials()
        except CredentialStoreUnavailable:
            # 凭据文件存在但不可读：不阻断启动；保持 None 让守卫在请求期重读，
            # 仍失败则返回 503（而非把节点误判为无凭据 403）。
            supervisor_credentials = None
    app.config["SUPERVISOR_CREDENTIALS"] = supervisor_credentials


def init_task_store(app, task_repository):
    """Initialise the task store; a failure disables tasks, not the hub."""
    if not app.config["TASKS_ENABLED"]:
        return
    # The task repository (injected or ``SqliteTaskRepository``) is the
    # init owner. When init fails the task store is disabled but
    # observation routes remain available.
    task_init = getattr(task_repository, "init", None)
    try:
        if task_init is not None:
            task_init()
    except Exception as exc:
        # Task storage is optional; observation routes remain available.
        app.config["TASKS_ENABLED"] = False
        from hub.diagnostics import log_failure
        log_failure(app.logger, "task_store_init_failed", exc)
