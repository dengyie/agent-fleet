"""``app.extensions["fleet"]``: the service/repository registry used by routes."""


def register_fleet_extension(app, config, repositories, core, platform, sessions,
                             control, *, retention):
    """Publish every built component under its stable extension key.

    ``repositories`` is the caller's (possibly injected) mapping itself; built
    platform/session stores are added to it in place, as before the split.
    """
    from hub.application.reconciliation_service import ReconciliationService

    app.extensions["fleet"] = {
        "config": config,
        "repositories": repositories,
        "publisher": core.publisher,
        "services": {
            "observe": core.observe_service,
            "tasks": core.task_service,
            "runner": core.runner_service,
            "supervisor": control.supervisor_service,
            "client_releases": control.client_release_service,
            "reconciliation": ReconciliationService(
                observe_service=core.observe_service,
                task_repository=core.task_repository,
                event_publisher=core.publisher,
                retention=retention,
            ),
        },
    }
    fleet = app.extensions["fleet"]
    repos = fleet["repositories"]
    services = fleet["services"]
    p = platform
    if p.platform_repository is not None:
        fleet["platform_repository"] = p.platform_repository
        fleet["platform_commands"] = p.command_repository
        if p.browser_repository is not None:
            repos["browser"] = p.browser_repository
        if p.browser_capture_gc is not None:
            repos["browser_capture_gc"] = p.browser_capture_gc
        if p.submit_approval_service is not None:
            services["submit_approvals"] = p.submit_approval_service
    if p.platform_scheduler_repository is not None:
        repos["platform_scheduler"] = p.platform_scheduler_repository
    if p.platform_service is not None:
        services["platform_defaults"] = p.platform_service
        services["conversations"] = p.conversation_service
        services["runs"] = p.run_service
        if p.submit_approval_service is not None:
            services["submit_approvals"] = p.submit_approval_service
        services["run_events"] = p.run_event_service
        if p.usage_repository is not None:
            from hub.application.usage_summary_service import UsageSummaryService
            services["usage_repository"] = p.usage_repository
            services["usage_summary"] = UsageSummaryService(p.usage_repository)
        if p.platform_worker_service is not None:
            services["platform_worker"] = p.platform_worker_service
        if p.platform_run_scheduler is not None:
            services["platform_run_scheduler"] = p.platform_run_scheduler
        if core.legacy_task_bridge is not None:
            services["legacy_task_bridge"] = core.legacy_task_bridge
        services["platform_delivery"] = p.platform_delivery
        services["plugins"] = p.plugin_service
        services["command_inspection"] = p.command_inspection
        services["command_postcheck"] = p.command_postcheck
        services["platform_artifacts"] = p.artifact_store
        if p.mcp_catalog is not None:
            services["mcp_catalog"] = p.mcp_catalog
        if (config.platform_mcp_connectors_enabled and p.mcp_catalog is not None
                and getattr(p.mcp_catalog, "enabled", False) and getattr(p.mcp_catalog, "store", None) is not None):
            from hub.application.mcp_catalog_admin import MCPCatalogAdminService
            services["mcp_catalog_admin"] = MCPCatalogAdminService(p.mcp_catalog,
                registration_enabled=config.platform_mcp_catalog_registration_enabled)
        if p.mcp_catalog_repository is not None:
            repos["mcp_catalog"] = p.mcp_catalog_repository
        if p.service_health is not None:
            services["service_health"] = p.service_health
        if p.service_repository is not None:
            repos["platform_services"] = p.service_repository
        if p.incident_repository is not None:
            repos["platform_incidents"] = p.incident_repository
        if p.incident_service is not None:
            services["incidents"] = p.incident_service
        if p.diagnostic_service is not None:
            services["diagnostics"] = p.diagnostic_service
        if p.approval_repository is not None:
            repos["platform_approvals"] = p.approval_repository
        if p.service_actions is not None:
            services["service_actions"] = p.service_actions
        if p.execution_window_repository is not None:
            repos["execution_windows"] = p.execution_window_repository
        if p.execution_window_service is not None:
            services["execution_windows"] = p.execution_window_service
        if p.platform_monitoring is not None:
            services["platform_monitoring"] = p.platform_monitoring
        if p.http_probe_monitoring is not None:
            services["http_probe_monitoring"] = p.http_probe_monitoring
        if p.platform_schedule_repository is not None:
            repos["platform_schedules"] = p.platform_schedule_repository
        if p.platform_schedule_service is not None:
            services["platform_schedules"] = p.platform_schedule_service
        if p.platform_memory_repository is not None:
            repos["platform_memory"] = p.platform_memory_repository
        if p.platform_memory_service is not None:
            services["platform_memory"] = p.platform_memory_service
        if p.platform_memory_context_service is not None:
            services["platform_memory_context"] = p.platform_memory_context_service
            p.conversation_service.memory_context = p.platform_memory_context_service
        if p.komari_client is not None:
            fleet["integrations"] = {"komari": p.komari_client}
    if sessions.session_service is not None:
        services["sessions"] = sessions.session_service
    if sessions.transcript_repo is not None:
        repos["transcript"] = sessions.transcript_repo
    if sessions.session_service is not None:
        from hub.application.handoff_service import HandoffService
        services["handoffs"] = HandoffService(
            sessions.session_repo, sessions.transcript_repo, core.task_service,
            enabled=bool(app.config.get("TASKS_ENABLED") and config.supervisor_enabled),
            task_store_available=bool(app.config.get("TASKS_ENABLED")),
        )
    if sessions.session_search_service is not None:
        services["session_search"] = sessions.session_search_service
    if control.control_router is not None:
        services["control_router"] = control.control_router
    if control.adoption_service is not None:
        services["adoptions"] = control.adoption_service
