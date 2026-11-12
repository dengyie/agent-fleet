"""Workflow gates are independent, passive by default, and preserve live facts."""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.infrastructure.workflow_repository import WorkflowRepositoryError
from test_workflow_coordinator import make_setup
from test_workflow_repository import OWNER


def test_workflow_gates_default_off_and_depend_on_platform(tmp_path: Path) -> None:
    defaults = FleetConfig.from_root(tmp_path)
    assert not defaults.platform_workflows_enabled and not defaults.platform_workflow_dispatch_enabled
    assert not FleetConfig.from_root(tmp_path, platform_workflows_enabled=True).platform_workflows_enabled
    assert not FleetConfig.from_root(tmp_path, platform_enabled=True, platform_workflow_dispatch_enabled=True).platform_workflow_dispatch_enabled
    enabled = FleetConfig.from_root(tmp_path, platform_enabled=True, platform_workflows_enabled=True, platform_workflow_dispatch_enabled=True)
    assert enabled.platform_workflows_enabled and enabled.platform_workflow_dispatch_enabled


def test_gate_off_does_not_initialize_workflow_schema_or_start_a_thread(tmp_path: Path) -> None:
    before = {thread.ident for thread in threading.enumerate()}
    config = FleetConfig.from_root(tmp_path, platform_enabled=True, dev_operator=OWNER, serve_frontend=False)
    app = create_app(config)
    assert app.test_client().get("/api/platform/v1/workflows").status_code == 404
    assert not app.extensions["fleet"]["services"].get("workflow_coordinator")
    assert before == {thread.ident for thread in threading.enumerate()}
    with sqlite3.connect(config.platform_db) as connection:
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name LIKE 'workflow_%'").fetchall()


@pytest.mark.parametrize("unknown", [False, True])
def test_domain_cannot_be_hidden_with_inflight_or_unknown_execution(tmp_path: Path, unknown: bool) -> None:
    setup = make_setup(tmp_path)
    for _ in range(3):
        setup.tick()
    if unknown:
        setup.platform.set_run_state(OWNER, setup.run_id(), "unknown", now=setup.clock())
        setup.tick()
    config = FleetConfig.from_root(tmp_path, platform_enabled=True, platform_db=setup.repository.db_path,
                                   dev_operator=OWNER, serve_frontend=False)
    with pytest.raises(WorkflowRepositoryError, match="workflow_domain_in_use"):
        create_app(config)


def test_mcp_catalog_is_injected_only_when_the_worker_gate_is_explicit(tmp_path: Path) -> None:
    from hub.application.connector_catalog import ConnectorCatalog
    catalog = ConnectorCatalog(enabled=True)
    config = FleetConfig.from_root(tmp_path, platform_enabled=True, dev_operator=OWNER, serve_frontend=False)
    app = create_app(config, repositories={"mcp_catalog": catalog})
    assert app.extensions["fleet"]["services"].get("platform_worker") is None
    config = FleetConfig.from_root(tmp_path, platform_enabled=True, dev_operator=OWNER, serve_frontend=False,
                                   platform_worker_enabled=True, platform_mcp_connectors_enabled=True)
    app = create_app(config, repositories={"mcp_catalog": catalog})
    worker = app.extensions["fleet"]["services"]["platform_worker"]
    assert worker.mcp_catalog is catalog and worker.mcp_enabled


def test_gate_enabled_construction_is_passive_and_uses_injected_clock(tmp_path: Path) -> None:
    before = {thread.ident for thread in threading.enumerate()}
    config = FleetConfig.from_root(tmp_path, platform_enabled=True, platform_workflows_enabled=True, dev_operator=OWNER, serve_frontend=False)
    app = create_app(config, repositories={"workflow_clock": lambda: 100})
    assert app.test_client().get("/api/platform/v1/workflows").get_json()["workflows"] == []
    assert app.extensions["fleet"]["services"]["workflow_coordinator"].dispatch_enabled is False
    assert {thread.ident for thread in threading.enumerate()} == before


@pytest.mark.parametrize("platform,domain,dispatch", [(False, True, False), (True, False, True)])
def test_direct_configuration_cannot_bypass_workflow_gate_dependencies(tmp_path: Path, platform: bool, domain: bool, dispatch: bool) -> None:
    config = replace(FleetConfig.from_root(tmp_path), platform_enabled=platform,
                     platform_workflows_enabled=domain, platform_workflow_dispatch_enabled=dispatch)
    with pytest.raises(ValueError, match="workflow gate dependencies"):
        create_app(config)
