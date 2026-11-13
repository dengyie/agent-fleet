"""Ingest-only reconciliation.

The hub never executes commands on agent machines. It only marks stale
push snapshots offline and emits state-change events.

Business rules live in :mod:`hub.application.observe_service` and
:mod:`hub.application.reconciliation_service`; this module is the cron/CLI
entrypoint. It assembles its dependencies explicitly from a
:class:`hub.config.FleetConfig` (default: ``FleetConfig.from_root`` at the
release root, i.e. ``state/``, ``state/events.jsonl``, ``state/fleet.db`` and
``hosts.yaml``) instead of module-level path globals.
"""

import argparse
from pathlib import Path

# Direct ``python hub/scan.py`` execution (cron: ``scan-agent-fleet.sh``) has
# ``hub/`` as sys.path[0], so ``from hub.application...`` fails with
# ``ModuleNotFoundError: No module named 'hub'``. Anchor the release root the
# same way ``hub/web.py`` does; package imports stay free of path mutation.
if __package__ in (None, ""):
    import sys

    _script_dir = str(Path(__file__).resolve().parent)
    while _script_dir in sys.path:
        sys.path.remove(_script_dir)
    _root = str(Path(__file__).resolve().parent.parent)
    if _root not in sys.path:
        sys.path.insert(0, _root)

from hub.application.event_publisher import EventPublisher
from hub.application.observe_service import HostConfig, ObserveService
from hub.application.reconciliation_service import ReconciliationService
from hub.config import FleetConfig
from hub.infrastructure.event_repository import JsonlEventRepository
from hub.infrastructure.state_repository import JsonlObservationRepository
from hub.infrastructure.task_repository import SqliteTaskRepository

FLEET_HOME = Path(__file__).resolve().parent.parent


def build_publisher(config: FleetConfig) -> EventPublisher:
    """Event publisher appending to ``config.event_log``."""
    return EventPublisher(JsonlEventRepository(config.event_log))


def build_reconciliation_service(config: FleetConfig, *, publisher=None):
    """Push-only ReconciliationService over the stores named by ``config``."""
    if publisher is None:
        publisher = build_publisher(config)
    observe_service = ObserveService(
        observation_repo=JsonlObservationRepository(config.state_dir),
        event_publisher=publisher,
        host_config=HostConfig.load(config.hosts_file),
    )
    return ReconciliationService(
        observe_service=observe_service,
        task_repository=SqliteTaskRepository(config.task_db),
        event_publisher=publisher,
    )


def main(argv=None, *, config: FleetConfig | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host")
    parser.add_argument("--scan-notify", action="store_true")
    args = parser.parse_args(argv)
    if config is None:
        config = FleetConfig.from_root(FLEET_HOME)
    publisher = build_publisher(config)
    if args.scan_notify:
        from hub.notifier import setup_alert_notify
        setup_alert_notify(publisher)
    service = build_reconciliation_service(config, publisher=publisher)
    for result in service.reconcile_observation(machine=args.host):
        print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
