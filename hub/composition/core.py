"""Core observe/task services over the event, observation and task stores."""

from dataclasses import dataclass
from typing import Any


@dataclass
class CoreComponents:
    publisher: Any
    observation_repository: Any
    observe_service: Any
    task_repository: Any
    task_service: Any
    runner_service: Any
    legacy_task_bridge: Any = None


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


def build_core(config, repositories: dict[str, Any], publisher, sessions,
               platform) -> CoreComponents:
    """Event publisher, observe/task/runner services and the legacy bridge."""
    from hub.application.event_publisher import EventPublisher
    from hub.application.observe_service import HostConfig, ObserveService
    from hub.application.runner_service import RunnerService
    from hub.application.task_service import TaskHostPolicy, TaskService
    from hub.infrastructure.event_repository import JsonlEventRepository
    from hub.infrastructure.state_repository import JsonlObservationRepository
    from hub.infrastructure.task_repository import SqliteTaskRepository

    if publisher is None:
        event_repository = repositories.get("events")
        if event_repository is None:
            event_repository = JsonlEventRepository(config.event_log)
        publisher = EventPublisher(event_repository)
    if sessions.session_service is not None:
        sessions.session_service.event_publisher = publisher

    observation_repository = repositories.get("observation")
    if observation_repository is None:
        observation_repository = JsonlObservationRepository(config.state_dir)
    observe_service = ObserveService(
        observation_repo=observation_repository,
        event_publisher=publisher,
        host_config=HostConfig.load(config.hosts_file),
    )

    task_repository = repositories.get("task")
    if task_repository is None:
        task_repository = SqliteTaskRepository(config.task_db)
    if sessions.session_search_service is not None and config.tasks_enabled:
        sessions.session_search_service.task_repository = task_repository
    task_service = TaskService(
        task_repo=task_repository,
        observation_repo=observation_repository,
        event_publisher=publisher,
        host_policy=TaskHostPolicy(
            project_whitelist=config.project_whitelist,
            hosts=_load_hosts_rows(config.hosts_file),
        ),
        session_repo=sessions.session_repo,
    )
    legacy_task_bridge = None
    if platform.platform_repository is not None:
        from hub.application.legacy_task_bridge import LegacyTaskBridge

        legacy_task_bridge = LegacyTaskBridge(
            platform.platform_repository, task_service, task_repository,
            worker_id="legacy-bridge-" + (config.dev_operator or "local"),
        )
        if platform.platform_run_scheduler is not None:
            platform.platform_run_scheduler.legacy_bridge = legacy_task_bridge
    runner_service = RunnerService(task_repository, publisher)
    return CoreComponents(
        publisher=publisher,
        observation_repository=observation_repository,
        observe_service=observe_service,
        task_repository=task_repository,
        task_service=task_service,
        runner_service=runner_service,
        legacy_task_bridge=legacy_task_bridge,
    )
