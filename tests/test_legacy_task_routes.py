"""HTTP and lifecycle contracts for the explicit legacy task bridge."""
from __future__ import annotations

import threading
import time

from hub.bootstrap import create_app, start_platform_worker
from hub.config import FleetConfig


OWNER = "owner@example.test"


def _app(tmp_path, *, enabled=True, worker=False, scheduler=False):
    return create_app(FleetConfig.from_root(
        tmp_path,
        ingest_token="ingest-only",
        dev_operator=OWNER,
        project_whitelist={"mac-local": ["agent-fleet"]},
        platform_enabled=enabled,
        platform_worker_enabled=worker,
        platform_worker_scheduler_enabled=scheduler,
    ))


def _run(client):
    conversation = client.post("/api/platform/v1/conversations", json={}).get_json()["conversation"]
    return client.post(
        f"/api/platform/v1/conversations/{conversation['conversation_id']}/turns",
        json={"text": "关联旧任务", "client_token": "legacy-route-1"},
    ).get_json()["run"]


def _online(app):
    app.extensions["fleet"]["services"]["observe"].repo.save_snapshot(
        "mac-local", {"machine": "mac-local", "reachable": True, "agents": {}, "system": {}}
    )


def test_platform_gate_controls_legacy_route_and_bridge_service(tmp_path):
    disabled = _app(tmp_path / "disabled", enabled=False)
    assert "legacy_task_bridge" not in disabled.extensions["fleet"]["services"]
    assert disabled.test_client().get("/api/platform/v1/runs/run-1/legacy-task").status_code == 404

    enabled = _app(tmp_path / "enabled")
    assert "legacy_task_bridge" in enabled.extensions["fleet"]["services"]


def test_legacy_route_requires_operator_and_is_owner_scoped(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    app.config["DEV_OPERATOR"] = None
    assert client.get("/api/platform/v1/runs/run-1/legacy-task").status_code == 401

    app.config["DEV_OPERATOR"] = OWNER
    _online(app)
    run = _run(client)
    response = client.post(
        f"/api/platform/v1/runs/{run['run_id']}/legacy-task",
        json={"task": {"machine": "mac-local", "agent_type": "codex", "project": "agent-fleet", "instruction": "运行测试"}},
    )
    assert response.status_code == 202
    body = response.get_json()
    assert body["task_id"]
    assert "client_token" not in str(body)
    assert "lease_owner" not in str(body)
    assert "diff_patch" not in str(body).lower()
    assert "raw" not in str(body).lower()

    other = client.get(
        f"/api/platform/v1/runs/{run['run_id']}/legacy-task",
        headers={"Cf-Access-Authenticated-User-Email": "other@example.test"},
    )
    assert other.status_code == 404


def test_legacy_route_validates_json_and_missing_link(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    assert client.post("/api/platform/v1/runs/run-1/legacy-task", data="bad", content_type="text/plain").status_code == 400
    missing = client.get("/api/platform/v1/runs/run-1/legacy-task")
    assert missing.status_code == 404
    assert missing.get_json()["error"] == "legacy_task_not_found"


def test_worker_lifecycle_drains_bridge_only_when_started(tmp_path):
    app = _app(tmp_path / "worker", worker=True)
    bridge = app.extensions["fleet"]["services"]["legacy_task_bridge"]
    called = threading.Event()
    owners = []

    def drain(owner):
        owners.append(owner)
        called.set()
        return None

    bridge.process_once = drain
    stop = start_platform_worker(app, interval_s=0.1)
    try:
        assert called.wait(1.0)
    finally:
        stop()
    assert owners and owners[0] == OWNER


def test_default_app_construction_does_not_start_bridge_thread(tmp_path):
    app = _app(tmp_path)
    assert not any(thread.name == "platform-run-worker" for thread in threading.enumerate())


def test_scheduler_owner_claim_includes_waiting_task_bridge_rows(tmp_path):
    app = _app(tmp_path / "scheduler", worker=True, scheduler=True)
    client = app.test_client()
    _online(app)
    run = _run(client)
    bridge = app.extensions["fleet"]["services"]["legacy_task_bridge"]
    bridge.enqueue(OWNER, run["run_id"], {
        "machine": "mac-local", "agent_type": "codex", "project": "agent-fleet",
        "instruction": "等待旧任务",
    })
    called = threading.Event()
    bridge.process_once = lambda owner: called.set() or None
    result = app.extensions["fleet"]["services"]["platform_run_scheduler"].tick_once()
    assert result["state"] == "owner_idle"
    assert called.is_set()
