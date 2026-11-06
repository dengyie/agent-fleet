"""Platform service monitoring: health, incidents, actions, probes, schedules."""


def build_monitoring(p, config, repositories):
    """Service health/incident/diagnostic stack plus its optional schedulers."""
    from hub.infrastructure.service_repository import ServiceRepository
    from hub.application.service_health_service import ServiceHealthService

    p.service_repository = repositories.get("platform_services")
    if p.service_repository is None:
        p.service_repository = ServiceRepository(config.platform_db)
        p.service_repository.init()
    p.service_health = ServiceHealthService(
        p.service_repository,
        probe_allowed_origins=getattr(config, "http_probe_allowed_origins", ()),
        probe_allow_loopback=getattr(config, "http_probe_allow_loopback", False),
    )
    p.command_postcheck.service_repository = p.service_repository
    p.command_postcheck.service_health = p.service_health
    from hub.infrastructure.incident_repository import IncidentRepository
    from hub.integrations.health_events import IncidentService
    from hub.application.diagnostic_service import DiagnosticService
    p.incident_repository = repositories.get("platform_incidents")
    if p.incident_repository is None:
        p.incident_repository = IncidentRepository(config.platform_db)
        p.incident_repository.init()
    p.incident_service = IncidentService(
        p.service_health, p.incident_repository,
        node_mapping=getattr(config, "komari_node_mapping", None),
        recovery_required=getattr(config, "incident_recovery_required", 2),
    )
    p.diagnostic_service = DiagnosticService(p.service_health, p.incident_repository)
    if getattr(config, "service_actions_enabled", False):
        from hub.infrastructure.approval_repository import ApprovalRepository
        from hub.application.service_action_service import ServiceActionService

        p.approval_repository = repositories.get("platform_approvals")
        if p.approval_repository is None:
            p.approval_repository = ApprovalRepository(config.platform_db)
            p.approval_repository.init()
        p.service_actions = ServiceActionService(
            service_repository=p.service_repository,
            approval_repository=p.approval_repository,
            delivery=p.platform_delivery,
            command_repository=p.command_repository,
        )
    if getattr(config, "komari_enabled", False):
        _build_komari(p, config, repositories)
    if (
        getattr(config, "http_probe_enabled", False)
        and getattr(config, "http_probe_network_enabled", False)
        and getattr(config, "http_probe_sync_enabled", False)
    ):
        _build_http_probe(p, config, repositories)


def _build_komari(p, config, repositories):
    from hub.integrations.komari import KomariClient
    if not (config.komari_base_url and config.komari_nodes_path and config.komari_token):
        raise RuntimeError("komari_enabled requires endpoint, nodes path, and token")
    p.komari_client = KomariClient(
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

        p.platform_scheduler_repository = repositories.get(
            "platform_scheduler")
        if p.platform_scheduler_repository is None:
            p.platform_scheduler_repository = PlatformSchedulerRepository(
                config.platform_db)
        p.platform_scheduler_repository.init()
        p.platform_monitoring = PlatformMonitoringService(
            service_repository=p.service_repository,
            incident_service=p.incident_service,
            komari_client=p.komari_client,
            scheduler_repository=p.platform_scheduler_repository,
            interval_s=getattr(config, "komari_sync_interval_s", 60.0),
        )


def _build_http_probe(p, config, repositories):
    from hub.application.http_probe_monitoring_service import (
        HttpProbeMonitoringService,
    )
    from hub.infrastructure.platform_scheduler_repository import (
        PlatformSchedulerRepository,
    )
    if p.platform_scheduler_repository is None:
        p.platform_scheduler_repository = repositories.get(
            "platform_scheduler")
        if p.platform_scheduler_repository is None:
            p.platform_scheduler_repository = PlatformSchedulerRepository(
                config.platform_db)
        p.platform_scheduler_repository.init()
    p.http_probe_monitoring = HttpProbeMonitoringService(
        service_repository=p.service_repository,
        incident_service=p.incident_service,
        scheduler_repository=p.platform_scheduler_repository,
        interval_s=getattr(config, "http_probe_sync_interval_s", 60.0),
        network_enabled=getattr(config, "http_probe_network_enabled", False),
        allow_loopback=getattr(config, "http_probe_allow_loopback", False),
        allowed_origins=getattr(config, "http_probe_allowed_origins", ()),
    )


def build_schedules(p, config, repositories):
    """Durable read-only schedules over the monitoring services built above."""
    from hub.infrastructure.platform_schedule_repository import (
        PlatformScheduleRepository,
    )
    from hub.application.platform_schedule_service import (
        DurableReadOnlyScheduleService,
    )

    p.platform_schedule_repository = repositories.get(
        "platform_schedules")
    if p.platform_schedule_repository is None:
        p.platform_schedule_repository = PlatformScheduleRepository(
            config.platform_db)
    p.platform_schedule_repository.init()
    # Both are final by now; the executor closes over them as before.
    service_health = p.service_health
    http_probe_monitoring = p.http_probe_monitoring

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

    p.platform_schedule_service = DurableReadOnlyScheduleService(
        p.platform_schedule_repository,
        executor=_read_only_schedule_executor,
        worker_id="hub-schedule",
    )
