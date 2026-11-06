"""Platform (``platform_enabled``) repositories and application services."""

from dataclasses import dataclass
from typing import Any


@dataclass
class PlatformComponents:
    """Everything the platform gate builds; all ``None`` while it is off."""

    platform_service: Any = None
    platform_repository: Any = None
    platform_delivery: Any = None
    command_repository: Any = None
    command_inspection: Any = None
    command_postcheck: Any = None
    usage_repository: Any = None
    conversation_service: Any = None
    run_service: Any = None
    run_event_service: Any = None
    submit_approval_service: Any = None
    artifact_store: Any = None
    browser_repository: Any = None
    browser_capture_gc: Any = None
    execution_window_repository: Any = None
    execution_window_service: Any = None
    service_health: Any = None
    service_repository: Any = None
    incident_repository: Any = None
    incident_service: Any = None
    diagnostic_service: Any = None
    approval_repository: Any = None
    service_actions: Any = None
    komari_client: Any = None
    platform_scheduler_repository: Any = None
    platform_monitoring: Any = None
    http_probe_monitoring: Any = None
    platform_schedule_repository: Any = None
    platform_schedule_service: Any = None
    platform_memory_repository: Any = None
    platform_memory_service: Any = None
    platform_memory_context_service: Any = None
    platform_worker_service: Any = None
    platform_run_scheduler: Any = None


def build_platform(config, repositories: dict[str, Any]) -> PlatformComponents:
    """Build the platform stores/services in their historical order."""
    p = PlatformComponents()
    if not config.platform_enabled:
        return p
    from hub.composition.platform_monitoring import (
        build_monitoring,
        build_schedules,
    )

    _build_command_plane(p, config, repositories)
    _build_conversations(p, config, repositories)
    _build_execution_windows(p, config, repositories)
    if config.service_monitoring_enabled:
        build_monitoring(p, config, repositories)
    if getattr(config, "platform_schedules_enabled", False):
        build_schedules(p, config, repositories)
    _build_memory(p, config, repositories)
    _build_worker(p, config)
    return p


def _build_command_plane(p, config, repositories):
    """Platform/usage/command stores, signed command delivery, defaults."""
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
    p.platform_repository = repositories.get("platform")
    if p.platform_repository is None:
        p.platform_repository = PlatformRepository(config.platform_db)
        p.platform_repository.init()
    p.usage_repository = repositories.get("platform_usage")
    if p.usage_repository is None:
        p.usage_repository = UsageRepository(
            config.platform_db,
            enforce_limits=bool(getattr(config, "platform_usage_limits_enabled", False)),
        )
        p.usage_repository.init()
    p.command_repository = repositories.get("platform_commands")
    if p.command_repository is None:
        p.command_repository = CommandRepository(config.platform_db)
        p.command_repository.init()
    command_signing_key = getattr(config, "platform_command_signing_raw", None)
    if command_signing_key is None:
        command_signing_key = load_platform_command_signing_key()
    browser_write_guard = None
    if getattr(config, "execution_windows_enabled", False):
        from hub.infrastructure.execution_window_repository import ExecutionWindowRepository
        browser_write_guard = ExecutionWindowRepository.assert_browser_write_allowed
    p.platform_delivery = CommandDeliveryService(
        p.command_repository,
        signing_key=command_signing_key,
        require_signature=bool(getattr(config, "platform_require_command_signature", False)),
        browser_write_guard=browser_write_guard,
    )
    p.command_inspection = CommandInspectionService(p.command_repository)
    p.command_postcheck = CommandPostcheckService(p.command_repository, p.platform_delivery)
    p.platform_service = DefaultsService(p.platform_repository)


def _build_conversations(p, config, repositories):
    """Conversations/runs, the optional browser store and the artifact store."""
    from hub.application.conversation_service import ConversationService, RunService
    from hub.application.run_event_service import RunEventService
    from hub.platform.artifacts import ArtifactStore
    if getattr(config, "platform_browser_enabled", False):
        from hub.infrastructure.browser_repository import BrowserRepository
        p.browser_repository = repositories.get("browser")
        if p.browser_repository is None:
            p.browser_repository = BrowserRepository(config.platform_db)
        p.browser_repository.init()
        if getattr(config, "platform_browser_submit_enabled", False):
            from hub.application.conversation_service import SubmitApprovalService
            p.submit_approval_service = SubmitApprovalService(
                p.browser_repository, p.platform_repository)
    p.conversation_service = ConversationService(p.platform_repository, p.platform_service)
    p.run_service = RunService(p.platform_repository,
                               browser_repository=p.browser_repository)
    p.run_event_service = RunEventService(p.platform_repository)
    if config.platform_artifact_root is None:
        raise RuntimeError("platform_enabled requires a platform_artifact_root")
    p.artifact_store = ArtifactStore(config.platform_artifact_root)
    if p.browser_repository is not None:
        from hub.infrastructure.browser_capture_gc import BrowserCaptureGcRepository

        p.browser_capture_gc = BrowserCaptureGcRepository(
            config.platform_db, config.platform_artifact_root,
        )
        p.browser_capture_gc.init()


def _build_execution_windows(p, config, repositories):
    if not getattr(config, "execution_windows_enabled", False):
        return
    from hub.infrastructure.execution_window_repository import ExecutionWindowRepository
    from hub.application.execution_window_service import ExecutionWindowService

    p.execution_window_repository = repositories.get("execution_windows")
    if p.execution_window_repository is None:
        p.execution_window_repository = ExecutionWindowRepository(config.platform_db)
        p.execution_window_repository.init()
    p.execution_window_service = ExecutionWindowService(p.execution_window_repository)


def _build_memory(p, config, repositories):
    if not getattr(config, "platform_memory_enabled", False):
        return
    from hub.infrastructure.platform_memory_repository import (
        PlatformMemoryRepository,
    )
    from hub.application.platform_memory_service import (
        PlatformMemoryService,
    )
    p.platform_memory_repository = repositories.get(
        "platform_memory")
    if p.platform_memory_repository is None:
        p.platform_memory_repository = PlatformMemoryRepository(
            config.platform_db)
    p.platform_memory_repository.init()
    p.platform_memory_service = PlatformMemoryService(
        p.platform_memory_repository)
    if getattr(config, "platform_memory_context_enabled", False):
        from hub.application.platform_memory_context_service import (
            PlatformMemoryContextService,
        )
        p.platform_memory_context_service = PlatformMemoryContextService(
            p.platform_memory_repository)


def _build_worker(p, config):
    """Local Run worker and its optional multi-owner scheduler (no threads)."""
    if not getattr(config, "platform_worker_enabled", False):
        return
    from hub.application.run_worker_service import LocalRunWorkerService
    p.platform_worker_service = LocalRunWorkerService(
        p.platform_repository, p.run_event_service,
        lease_s=getattr(config, "platform_worker_lease_s", 60.0),
        sandbox_launcher=getattr(config, "platform_sandbox_launcher", ()),
        artifact_store=p.artifact_store, diagnostics=p.diagnostic_service,
        provider_network_enabled=bool(getattr(
            config, "platform_provider_network_enabled", False)),
        remote_execution_enabled=bool(getattr(
            config, "platform_remote_execution_enabled", False)),
        remote_delivery=p.platform_delivery,
        usage_meter=p.usage_repository,
        conversation_service=p.conversation_service,
        service_actions=p.service_actions,
        defaults_service=p.platform_service,
        browser_enabled=bool(getattr(config, "platform_browser_enabled", False)),
        browser_network_enabled=bool(getattr(
            config, "platform_browser_network_enabled", False)),
        browser_allowed_origins=tuple(getattr(
            config, "platform_browser_allowed_origins", ()) or ()),
        browser_submit_enabled=bool(getattr(
            config, "platform_browser_submit_enabled", False)),
        submit_approvals=p.submit_approval_service,
    )
    if getattr(config, "platform_worker_scheduler_enabled", False):
        from hub.application.run_scheduler_service import RunSchedulerService
        p.platform_run_scheduler = RunSchedulerService(
            p.platform_repository, p.platform_worker_service,
            owner_lease_s=getattr(config, "platform_worker_lease_s", 60.0),
            interval_s=getattr(config, "platform_worker_interval_s", 1.0),
            max_concurrency=getattr(config, "platform_worker_max_concurrency", 1),
            max_workspace_concurrency=getattr(
                config, "platform_worker_max_workspace_concurrency", 1),
        )
