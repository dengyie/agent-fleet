from pathlib import Path

import pytest

from hub.application.task_service import ApplicationError
from hub.bootstrap import create_app
from hub.config import FleetConfig


def _app(tmp_path: Path, *, monitoring=True, owner="owner-a@example.test"):
    return create_app(FleetConfig.from_root(
        tmp_path, dev_operator=owner, platform_enabled=True,
        service_monitoring_enabled=monitoring,
    ))


def _setup(app):
    repo = app.extensions["fleet"]["platform_repository"]
    repo.upsert_node("owner-a@example.test", {"node_id": "node-a"})
    service = app.extensions["fleet"]["services"]["service_health"]
    registered = service.register("owner-a@example.test", {
        "service_id": "api", "node_id": "node-a",
        "name": "API", "adapter": "http",
        "target_alias": "api-health",
        "checks": {"timeout_s": 2},
    })
    return repo, service, registered["service"]


def test_service_monitoring_gate_and_operator_catalog(tmp_path):
    disabled = _app(tmp_path / "disabled", monitoring=False)
    assert disabled.test_client().get("/api/platform/v1/services").status_code == 404

    app = _app(tmp_path / "enabled")
    _, service, _ = _setup(app)
    client = app.test_client()
    listing = client.get("/api/platform/v1/services")
    assert listing.status_code == 200
    payload = listing.get_json()
    assert payload["services"][0]["health"]["overall"] == "unknown"
    assert payload["services"][0]["health"]["dimensions"]["process_state"]["freshness"] == "missing"

    evidence = service.ingest("owner-a@example.test", {
        "service_id": "api", "dimension": "application_health",
        "source": "http_probe", "state": "healthy",
        "observed_at": 100.0, "ttl_s": 10, "evidence_id": "ev-app-1",
        "detail": {"status_code": 200},
    })
    assert evidence["evidence"]["evidence_id"] == "ev-app-1"
    duplicate = service.ingest("owner-a@example.test", {
        "service_id": "api", "dimension": "application_health",
        "source": "http_probe", "state": "unhealthy",
        "observed_at": 100.0, "ttl_s": 10, "evidence_id": "ev-app-1",
        "detail": {"status_code": 500},
    })
    assert duplicate["evidence"]["state"] == "healthy"


def test_health_aggregation_has_stale_and_unhealthy_precedence(tmp_path):
    app = _app(tmp_path)
    _, service, _ = _setup(app)
    service.clock = lambda: 200.0
    service.ingest("owner-a@example.test", {
        "service_id": "api", "dimension": "process_state",
        "source": "systemd", "state": "healthy",
        "observed_at": 100, "ttl_s": 10, "evidence_id": "ev-stale",
    })
    service.ingest("owner-a@example.test", {
        "service_id": "api", "dimension": "host_reachability",
        "source": "node", "state": "unhealthy",
        "observed_at": 200, "ttl_s": 300, "evidence_id": "ev-host",
    })
    health = service.get("owner-a@example.test", "api")["health"]
    assert health["dimensions"]["process_state"]["state"] == "stale"
    assert health["overall"] == "unhealthy"


def test_node_health_evidence_requires_matching_node_and_scope(tmp_path):
    app = _app(tmp_path)
    repo, _, _ = _setup(app)
    credential = repo.provision_node_credential("owner-a@example.test", "node-a", secret="a" * 40)["credential"]
    client = app.test_client()
    headers = {"X-Platform-Node-Credential": credential}
    good = client.post("/api/platform/v1/nodes/health-evidence", json={
        "service_id": "api", "dimension": "process_state",
        "source": "systemd", "state": "healthy",
        "observed_at": 100, "ttl_s": 60,
    }, headers=headers)
    assert good.status_code == 200
    wrong = client.post("/api/platform/v1/nodes/health-evidence", json={
        "service_id": "missing", "dimension": "process_state",
        "source": "systemd", "state": "healthy",
    }, headers=headers)
    assert wrong.status_code == 404
    assert client.post("/api/platform/v1/nodes/health-evidence", json={}, headers={}).status_code == 403


def test_service_owner_isolation_and_node_heartbeat_fallback(tmp_path):
    app = _app(tmp_path)
    repo, service, _ = _setup(app)
    repo.upsert_node("owner-b@example.test", {"node_id": "node-b"})
    other = create_app(FleetConfig.from_root(
        tmp_path, dev_operator="owner-b@example.test", platform_enabled=True,
        service_monitoring_enabled=True,
    ))
    assert other.test_client().get("/api/platform/v1/services/api").status_code == 404
    repo.record_node_heartbeat("owner-a@example.test", "node-a", status="online")
    summary = service.get("owner-a@example.test", "api")["health"]
    assert summary["dimensions"]["host_reachability"]["state"] == "healthy"


def test_service_api_rejects_invalid_adapter_disabled_node_and_oversized_detail(tmp_path):
    app = _app(tmp_path)
    repo = app.extensions["fleet"]["platform_repository"]
    repo.upsert_node("owner-a@example.test", {"node_id": "node-a"})
    client = app.test_client()
    invalid = client.post("/api/platform/v1/services", json={
        "service_id": "bad", "node_id": "node-a",
        "adapter": "shell", "target_alias": "x",
    })
    assert invalid.status_code == 400
    repo.upsert_node("owner-a@example.test", {"node_id": "disabled", "enabled": False})
    forbidden = client.post("/api/platform/v1/services", json={
        "service_id": "disabled-svc", "node_id": "disabled",
        "adapter": "http", "target_alias": "x",
    })
    assert forbidden.status_code == 409
    service = app.extensions["fleet"]["services"]["service_health"]
    service.register("owner-a@example.test", {
        "service_id": "api", "node_id": "node-a",
        "adapter": "http", "target_alias": "x",
    })
    with pytest.raises(ApplicationError) as oversized:
        service.ingest("owner-a@example.test", {
            "service_id": "api", "dimension": "application_health",
            "source": "http", "state": "healthy",
            "detail": {"blob": "x" * (64 * 1024)},
        })
    assert oversized.value.code == "evidence_detail_too_large"


def test_service_action_policy_is_explicit_and_versioned(tmp_path):
    app = _app(tmp_path)
    repo = app.extensions["fleet"]["platform_repository"]
    repo.upsert_node("owner-a@example.test", {"node_id": "node-a"})
    service = app.extensions["fleet"]["services"]["service_health"]
    first = service.register("owner-a@example.test", {
        "service_id": "api", "node_id": "node-a",
        "adapter": "systemd", "target_alias": "api.service",
        "allowed_actions": ["restart", "inspect", "inspect"],
    })["service"]
    assert first["allowed_actions"] == ["inspect", "restart"]
    second = service.register("owner-a@example.test", {
        "service_id": "api", "node_id": "node-a",
        "adapter": "systemd", "target_alias": "api.service",
        "allowed_actions": ["inspect"],
    })["service"]
    assert second["version"] == first["version"] + 1
    assert second["allowed_actions"] == ["inspect"]
    with pytest.raises(ApplicationError) as invalid:
        service.register("owner-a@example.test", {
            "service_id": "bad", "node_id": "node-a",
            "adapter": "systemd", "target_alias": "bad.service",
            "allowed_actions": ["shell"],
        })
    assert invalid.value.code == "unsupported_action"
