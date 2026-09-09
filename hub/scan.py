"""Ingest-only reconciliation.

The hub never executes commands on agent machines. It only marks stale
push snapshots offline and emits state-change events.

Business rules live in :mod:`hub.application.observe_service`; this module is
a thin compatibility layer for CLI, web, and legacy callers that still use
``reconcile_ingest`` / ``scan_all`` / ``collect_all`` and the private diff
helpers.
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

import yaml

from hub.application.observe_service import (
    HostConfig,
    ObserveService,
    diff_snapshots as _diff,
    normalize_agents as _normalize_agents,
)

FLEET_HOME = Path(__file__).resolve().parent.parent
DEFAULT_STALE_AFTER_S = 300


def load_config():
    with open(FLEET_HOME / "hosts.yaml") as stream:
        return yaml.safe_load(stream) or {}


def _legacy_service():
    """ObserveService bound to the current module-level state/event facades.

    New code should inject an explicit repository, publisher, and host config
    instead of importing this module. This path keeps legacy callers (web
    startup, CLI, regression tests) working untouched.
    """
    from hub import events as events_facade
    from hub import state as state_facade

    return ObserveService(
        observation_repo=state_facade,
        event_publisher=events_facade,
        host_config=HostConfig.load(FLEET_HOME / "hosts.yaml"),
    )


def _reconciliation_service():
    """ReconciliationService bound to the module-level observation/task facades.

    New code should inject explicit services via ``hub.bootstrap``. This path
    keeps legacy callers (web startup wrappers, CLI, regression tests) working
    while routing every reconciliation entry point through the push-only
    application service.
    """
    from hub import events as events_facade
    from hub import task_store as task_facade
    from hub.application.reconciliation_service import ReconciliationService

    return ReconciliationService(
        observe_service=_legacy_service(),
        task_repository=task_facade,
        event_publisher=events_facade,
    )


def reconcile_ingest(now=None, machine=None):
    """Mark expired push reports offline without executing anything.

    machine: 只 reconcile 指定机器（None = 全部）。
    """
    return _reconciliation_service().reconcile_observation(machine=machine, now=now)


def scan_all(filter_host=None):
    return _reconciliation_service().reconcile_observation(machine=filter_host)


def collect_all(filter_host=None):
    return scan_all(filter_host)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host")
    parser.add_argument("--scan-notify", action="store_true")
    args = parser.parse_args()
    if args.scan_notify:
        from hub.notifier import setup_alert_notify
        setup_alert_notify()
    for result in scan_all(args.host):
        print(result)
