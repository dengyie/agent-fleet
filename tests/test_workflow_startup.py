"""The real Hub entry point loads operator-owned coding delivery registration."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from fleet_core.control import generate_ed25519_keypair
from hub import web
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.domain.capability import CapabilityDefinition
from hub.infrastructure.platform_db import PlatformRepository
from test_workflow_repository import OWNER, WORKSPACE


def registration(owner: str, workspace: str) -> dict:
    return {"schema_version": 1, "coding_delivery": [{
        "owner_id": owner, "workspace_id": workspace,
        "develop": {"machine": "mac-local", "agent_type": "codex", "project": "agent-fleet",
                    "repository_id": "repo-a", "conversation_id": "develop-chat"},
        "review": {"adapter_id": "fixture-review", "revision": 1, "executor_id": "fixture-reviewer",
                   "kind": "service", "model_id": None, "argv": [sys.executable, "-V"],
                   "launcher": [sys.executable, "-V"], "timeout_seconds": 30, "conversation_id": "review-chat"},
        "test": {"conversation_id": "test-chat"},
    }]}


def seed(platform, root: Path, *, owner=OWNER, workspace=WORKSPACE) -> None:
    platform.init()
    platform.upsert_workspace(owner, {"workspace_id": workspace, "root_path": str(root)})
    for stage in ("develop", "review", "test"):
        platform.create_conversation(owner, stage + "-chat", title=stage, workspace_id=workspace)


def test_normal_startup_loads_registered_workflow(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(web, "FLEET_HOME", tmp_path)
    db = tmp_path / "platform.db"
    seed(PlatformRepository(db), tmp_path)
    path = tmp_path / "coding.json"
    path.write_text(json.dumps(registration(OWNER, WORKSPACE)), encoding="utf-8")
    monkeypatch.setenv("AGENT_FLEET_PLATFORM_WORKFLOW_REGISTRATION_FILE", str(path))
    app = web.make_app(require_token=False, dev_operator=OWNER, serve_frontend=False,
        platform_enabled=True, platform_db=db, platform_workflows_enabled=True,
        platform_workflow_dispatch_enabled=False)
    body = app.test_client().get("/api/platform/v1/workflow-templates").get_json()
    assert [item["template_id"] for item in body["templates"]] == ["coding-delivery"]
    coordinator = app.extensions["fleet"]["services"]["workflow_coordinator"]
    assert len(coordinator.dispatchers) == 3 and not coordinator.dispatch_enabled


@pytest.mark.parametrize("source", ["file", "explicit_off", "environment_off"])
def test_startup_registration_path_precedence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str) -> None:
    monkeypatch.setattr(web, "FLEET_HOME", tmp_path)
    monkeypatch.delenv("AGENT_FLEET_PLATFORM_WORKFLOW_REGISTRATION_FILE", raising=False)
    db = tmp_path / "platform.db"
    seed(PlatformRepository(db), tmp_path)
    path = tmp_path / "coding.json"
    path.write_text(json.dumps(registration(OWNER, WORKSPACE)), encoding="utf-8")
    (tmp_path / "fleet-gates.conf").write_text(
        f"AGENT_FLEET_PLATFORM_WORKFLOW_REGISTRATION_FILE={path}\n", encoding="utf-8")
    options = {}
    if source == "explicit_off":
        options["platform_workflow_registration_file"] = ""
        monkeypatch.setenv("AGENT_FLEET_PLATFORM_WORKFLOW_REGISTRATION_FILE", str(path))
    elif source == "environment_off":
        monkeypatch.setenv("AGENT_FLEET_PLATFORM_WORKFLOW_REGISTRATION_FILE", "")
    app = web.make_app(require_token=False, dev_operator=OWNER, serve_frontend=False,
        platform_enabled=True, platform_db=db, platform_workflows_enabled=True, **options)
    templates = app.test_client().get("/api/platform/v1/workflow-templates").get_json()["templates"]
    assert len(templates) == (1 if source == "file" else 0)


def test_startup_registration_validates_existing_bindings_and_gates(tmp_path: Path) -> None:
    path = tmp_path / "coding.json"
    config = FleetConfig.from_root(tmp_path, platform_enabled=True, platform_workflows_enabled=True,
        platform_workflow_registration_file=path, dev_operator=OWNER, serve_frontend=False)
    seed(PlatformRepository(config.platform_db), tmp_path)
    value = registration(OWNER, WORKSPACE)
    value["coding_delivery"][0]["test"]["conversation_id"] = "missing-chat"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="workflow_registration_conversation_unavailable"):
        create_app(config)
    with pytest.raises(ValueError, match="workflow_registration_requires_worker_scheduler"):
        create_app(FleetConfig.from_root(tmp_path, platform_enabled=True, platform_workflows_enabled=True,
            platform_workflow_dispatch_enabled=True, platform_workflow_registration_file=path, serve_frontend=False))
    disabled = create_app(FleetConfig.from_root(tmp_path, platform_enabled=True,
        platform_workflow_registration_file=tmp_path / "missing.json", serve_frontend=False))
    assert disabled.test_client().get("/api/platform/v1/workflow-templates").status_code == 404


def test_remote_test_startup_registration_wires_signed_node_path(tmp_path: Path) -> None:
    path = tmp_path / "coding.json"
    signing_key, _ = generate_ed25519_keypair()
    config = FleetConfig.from_root(tmp_path, platform_enabled=True, platform_workflows_enabled=True,
        platform_workflow_registration_file=path, platform_workflow_dispatch_enabled=True,
        platform_worker_enabled=True, platform_worker_scheduler_enabled=True, platform_remote_execution_enabled=True,
        platform_capability_routing_enabled=True, platform_artifact_transfer_enabled=True,
        platform_command_signing_raw=signing_key, project_whitelist={"mac-local": ["agent-fleet"]}, serve_frontend=False)
    platform = PlatformRepository(config.platform_db)
    seed(platform, tmp_path)
    platform.upsert_node(OWNER, {"node_id": "node-a"})
    capability = CapabilityDefinition("coding.test", 1, "fixed-tests", "in-v1", "out-v1", b'{"type":"object"}',
        b'{"type":"object"}', "workspace_read", "manual_only", ("operator",), 3600, 65536)
    value = registration(OWNER, WORKSPACE)
    value["coding_delivery"][0]["test"]["remote"] = {"node_id": "node-a", "capability": capability.payload(),
        "architecture": "x86_64", "software": {"python": "3.11.2"}, "residency_alias": "private"}
    path.write_text(json.dumps(value), encoding="utf-8")
    app = create_app(config)
    services = app.extensions["fleet"]["services"]
    coordinator = services["workflow_coordinator"]
    target = coordinator.dispatchers[(OWNER, WORKSPACE, "node-a")].executor.target
    assert target.remote_test and target.hub_input_target() is None
    assert coordinator.capability_admission is services["capability_admission"]
    assert services["artifact_inputs"].targets[(OWNER, WORKSPACE, "node-a")].aliases == (("source", "source", ".tar"),)
    assert services["platform_worker"].workflow_remote_test_executor is services["workflow_remote_test_executor"]
    assert services["platform_delivery"].signing_key == signing_key
