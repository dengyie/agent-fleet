from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hub.application.task_service import ApplicationError
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.integrations.health_events import incident_fingerprint
from hub.integrations.http_probe import (
    HttpProbeError,
    HttpProbeSnapshot,
    parse_http_probe_policy,
    validate_probe_endpoint,
)


class _ProbeHandler(BaseHTTPRequestHandler):
    payload = {
        "schema_version": 1,
        "probe": {
            "id": "api-health",
            "state": "healthy",
            "status_code": 200,
            "latency_ms": 2.5,
            "observed_at": 100.0,
        },
    }
    status = 200
    requests = []

    def do_GET(self):  # noqa: N802
        type(self).requests.append((self.command, self.path, self.headers.get("Accept")))
        body = json.dumps(type(self).payload).encode("utf-8")
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


@pytest.fixture
def probe_server():
    _ProbeHandler.payload = {
        "schema_version": 1,
        "probe": {
            "id": "api-health",
            "state": "healthy",
            "status_code": 200,
            "latency_ms": 2.5,
            "observed_at": 100.0,
        },
    }
    _ProbeHandler.status = 200
    _ProbeHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProbeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _app(tmp_path, origin, *, interval=5.0, scheduler=True):
    return create_app(FleetConfig.from_root(
        tmp_path,
        dev_operator="owner-a@example.test",
        platform_enabled=True,
        service_monitoring_enabled=True,
        http_probe_enabled=True,
        http_probe_network_enabled=True,
        http_probe_sync_enabled=scheduler,
        http_probe_sync_interval_s=interval,
        http_probe_allowed_origins=(origin,),
        http_probe_allow_loopback=True,
        incident_recovery_required=2,
    ))


def _register(app, *, origin, path="/health"):
    repository = app.extensions["fleet"]["platform_repository"]
    repository.upsert_node("owner-a@example.test", {"node_id": "node-a"})
    service = app.extensions["fleet"]["services"]["service_health"]
    result = service.register("owner-a@example.test", {
        "service_id": "api",
        "node_id": "node-a",
        "adapter": "http",
        "target_alias": "api-health",
        "checks": {
            "http_probe": {
                "base_url": origin,
                "path": path,
                "probe_id": "api-health",
                "interval_s": 5,
                "timeout_s": 2,
                "ttl_s": 12,
                "allow_loopback": True,
            },
        },
    })
    return repository, service, result["service"]


def test_policy_rejects_private_dns_and_allows_loopback_only_with_explicit_override():
    policy = parse_http_probe_policy({
        "base_url": "https://probe.example.test",
        "path": "/health",
    })
    public = lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
    ]
    validate_probe_endpoint(policy, resolver=public)

    private = lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.4", 443)),
    ]
    with pytest.raises(HttpProbeError) as exc:
        validate_probe_endpoint(policy, resolver=private)
    assert exc.value.code == "unsafe_address"

    rebinding = lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.4", 443)),
    ]
    with pytest.raises(HttpProbeError) as exc:
        validate_probe_endpoint(policy, resolver=rebinding)
    assert exc.value.code == "unsafe_address"

    loopback = parse_http_probe_policy({
        "base_url": "http://127.0.0.1:8799",
        "path": "/health",
        "allow_loopback": True,
    })
    local = lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 8799)),
    ]
    with pytest.raises(HttpProbeError) as exc:
        validate_probe_endpoint(loopback, resolver=local)
    assert exc.value.code == "unsafe_address"
    validate_probe_endpoint(loopback, allow_loopback_override=True, resolver=local)


def test_service_registration_requires_operator_allowlisted_origin(tmp_path, probe_server):
    port = probe_server.server_address[1]
    app = _app(tmp_path, f"http://127.0.0.1:{port}", scheduler=False)
    service = app.extensions["fleet"]["services"]["service_health"]
    repository = app.extensions["fleet"]["platform_repository"]
    repository.upsert_node("owner-a@example.test", {"node_id": "node-a"})
    with pytest.raises(ApplicationError) as exc:
        service.register("owner-a@example.test", {
            "service_id": "api",
            "node_id": "node-a",
            "adapter": "http",
            "target_alias": "api-health",
            "checks": {
                "http_probe": {
                    "base_url": "https://not-allowlisted.example.test",
                    "path": "/health",
                },
            },
        })
    assert exc.value.code == "http_probe_endpoint_not_allowlisted"


def test_default_configuration_does_not_construct_probe_scheduler(tmp_path):
    config = FleetConfig.from_root(tmp_path)
    assert config.http_probe_enabled is False
    assert config.http_probe_network_enabled is False
    assert config.http_probe_sync_enabled is False
    app = create_app(config)
    assert "http_probe_monitoring" not in app.extensions["fleet"]["services"]


def test_loopback_scheduler_writes_application_evidence_and_exact_get(
    tmp_path, probe_server,
):
    origin = f"http://127.0.0.1:{probe_server.server_address[1]}"
    app = _app(tmp_path, origin)
    _, service, _ = _register(app, origin=origin)
    monitoring = app.extensions["fleet"]["services"]["http_probe_monitoring"]

    result = monitoring.sync_once(now=100.0, force=True)

    assert result["ok"] is True
    assert result["probed"] == 1
    assert _ProbeHandler.requests == [("GET", "/health", "application/json")]
    evidence = service.get("owner-a@example.test", "api")["evidence"]
    assert evidence[0]["source"] == "http_probe_v1"
    assert evidence[0]["dimension"] == "application_health"
    assert evidence[0]["ttl_s"] == 12.0
    assert evidence[0]["detail"]["service_version"] == 1
    assert "127.0.0.1" not in json.dumps(evidence)
    status = app.test_client().get("/api/platform/v1/http-probe/status")
    assert status.status_code == 200
    assert status.get_json()["status"]["job_id"] == "http_probe_sync"


def test_failure_dedupes_and_http_recovery_does_not_close_komari_incident(
    tmp_path, probe_server,
):
    origin = f"http://127.0.0.1:{probe_server.server_address[1]}"
    app = _app(tmp_path, origin)
    repository, service, _ = _register(app, origin=origin)
    incidents = app.extensions["fleet"]["repositories"]["platform_incidents"]
    komari_fp = incident_fingerprint("api", "komari_node_reachability")
    komari = incidents.open_or_update(
        "owner-a@example.test",
        {
            "service_id": "api",
            "failure_class": "health_unhealthy",
            "fingerprint": komari_fp,
            "evidence_id": "komari-ev",
        },
        observed_at=90,
        source="komari",
        state="unhealthy",
    )
    _ProbeHandler.payload = {
        "schema_version": 1,
        "probe": {"id": "api-health", "state": "unhealthy", "status_code": 503},
    }
    _ProbeHandler.status = 200
    monitoring = app.extensions["fleet"]["services"]["http_probe_monitoring"]
    first = monitoring.sync_once(now=100.0, force=True)
    second = monitoring.sync_once(now=101.0, force=True)
    assert first["ok"] is True
    assert second["ok"] is True
    open_incidents = incidents.list("owner-a@example.test", state="open")
    http_open = [row for row in open_incidents if row["latest_source"] == "http_probe_v1"]
    assert len(http_open) == 1
    assert http_open[0]["evidence_ids"][-1].startswith("http_probe_")
    assert incidents.get("owner-a@example.test", komari["incident_id"])["state"] == "open"

    _ProbeHandler.payload = {
        "schema_version": 1,
        "probe": {"id": "api-health", "state": "healthy", "status_code": 200},
    }
    monitoring.sync_once(now=102.0, force=True)
    recovered = monitoring.sync_once(now=103.0, force=True)
    assert recovered["ok"] is True
    rows = incidents.list("owner-a@example.test")
    http_rows = [row for row in rows if row["latest_source"] == "http_probe_v1"]
    assert http_rows[0]["state"] == "closed"
    assert incidents.get("owner-a@example.test", komari["incident_id"])["state"] == "open"


def test_scheduler_backoff_and_policy_reload_fail_closed(tmp_path, probe_server):
    origin = f"http://127.0.0.1:{probe_server.server_address[1]}"
    app = _app(tmp_path, origin, interval=5)
    _, _, service_row = _register(app, origin=origin)
    _ProbeHandler.status = 503
    monitoring = app.extensions["fleet"]["services"]["http_probe_monitoring"]
    failed = monitoring.sync_once(now=100.0, force=True)
    assert failed["ok"] is False
    assert failed["status"]["consecutive_failures"] == 1
    assert failed["status"]["next_run_at"] == 110.0

    # The scheduler reloads the persisted policy and does not take a caller URL.
    assert service_row["checks"]["http_probe"]["base_url"] == origin + "/"
    assert monitoring.sync_once(now=105.0)["skipped"] is True


def test_old_version_evidence_does_not_suppress_new_endpoint_version(tmp_path, probe_server):
    origin = f"http://127.0.0.1:{probe_server.server_address[1]}"
    app = _app(tmp_path, origin, interval=60)
    _, service, service_row = _register(app, origin=origin)
    monitoring = app.extensions["fleet"]["services"]["http_probe_monitoring"]
    policy = monitoring._policy(service_row)
    service.ingest("owner-a@example.test", {
        "service_id": "api", "dimension": "application_health",
        "source": "http_probe_v1", "state": "healthy",
        "observed_at": 100.0, "ttl_s": 60.0,
        "evidence_id": "http_probe_old_version",
        "detail": {"probe_id": "api-health", "service_version": 1},
    })
    assert monitoring._is_due(
        "owner-a@example.test", service_row, policy, 100.0, force=False,
    ) is False

    service.register("owner-a@example.test", {
        **service_row,
        "checks": {
            **service_row["checks"],
            "http_probe": {
                **service_row["checks"]["http_probe"],
                "path": "/health-v2",
            },
        },
    })
    rotated = service.repository.get_service("owner-a@example.test", "api")
    rotated_policy = monitoring._policy(rotated)
    assert rotated["version"] == 2
    assert monitoring._is_due(
        "owner-a@example.test", rotated, rotated_policy, 101.0, force=False,
    ) is True


def test_endpoint_rotation_uses_distinct_http_incidents(tmp_path, probe_server):
    origin = f"http://127.0.0.1:{probe_server.server_address[1]}"
    app = _app(tmp_path, origin)
    _, service, service_row = _register(app, origin=origin)
    monitoring = app.extensions["fleet"]["services"]["http_probe_monitoring"]
    _ProbeHandler.payload = {
        "schema_version": 1,
        "probe": {"id": "api-health", "state": "unhealthy", "status_code": 503},
    }

    first = monitoring.sync_once(now=100.0, force=True)
    assert first["failed"] == 0
    service.register("owner-a@example.test", {
        **service_row,
        "checks": {
            **service_row["checks"],
            "http_probe": {
                **service_row["checks"]["http_probe"],
                "path": "/health-v2",
            },
        },
    })
    second = monitoring.sync_once(now=101.0, force=True)
    assert second["failed"] == 0

    evidence = service.get("owner-a@example.test", "api")["evidence"]
    versions = {
        row["detail"].get("service_version")
        for row in evidence if row["source"] == "http_probe_v1"
    }
    assert versions == {1, 2}
    incidents = app.extensions["fleet"]["repositories"]["platform_incidents"].list(
        "owner-a@example.test", state="open",
    )
    http_incidents = [row for row in incidents if row["latest_source"] == "http_probe_v1"]
    assert len(http_incidents) == 2
    assert len({row["fingerprint"] for row in http_incidents}) == 2


def test_inflight_old_version_is_discarded_and_does_not_backoff(tmp_path, probe_server):
    origin = f"http://127.0.0.1:{probe_server.server_address[1]}"
    app = _app(tmp_path, origin)
    _, service, service_row = _register(app, origin=origin)
    monitoring = app.extensions["fleet"]["services"]["http_probe_monitoring"]

    class RotatingClient:
        def fetch(self):
            service.register("owner-a@example.test", {
                **service_row,
                "checks": {
                    **service_row["checks"],
                    "http_probe": {
                        **service_row["checks"]["http_probe"],
                        "path": "/rotated",
                    },
                },
            })
            return HttpProbeSnapshot("api-health", "healthy", 200, 1.0, 100.0)

    monitoring.client_factory = lambda *_args, **_kwargs: RotatingClient()
    result = monitoring.sync_once(now=100.0, force=True)

    assert result["ok"] is True
    assert result["stale"] == 1
    assert result["failed"] == 0
    assert result["probed"] == 0
    assert app.extensions["fleet"]["services"]["service_health"].get(
        "owner-a@example.test", "api",
    )["evidence"] == []


def test_rotation_after_evidence_write_discards_sample_before_incident(tmp_path, probe_server):
    origin = f"http://127.0.0.1:{probe_server.server_address[1]}"
    app = _app(tmp_path, origin)
    _, service, service_row = _register(app, origin=origin)
    monitoring = app.extensions["fleet"]["services"]["http_probe_monitoring"]
    real_incidents = app.extensions["fleet"]["repositories"]["platform_incidents"]
    _ProbeHandler.payload = {
        "schema_version": 1,
        "probe": {"id": "api-health", "state": "unhealthy", "status_code": 503},
    }

    def rotate():
        service.register("owner-a@example.test", {
            **service_row,
            "checks": {
                **service_row["checks"],
                "http_probe": {
                    **service_row["checks"]["http_probe"],
                    "path": "/health-v2",
                },
            },
        })

    class RotatingIncidentRepository:
        def open_or_update(self, *args, **kwargs):
            rotate()
            return real_incidents.open_or_update(*args, **kwargs)

        def recover(self, *args, **kwargs):
            rotate()
            return real_incidents.recover(*args, **kwargs)

    monitoring.incident_service.repository = RotatingIncidentRepository()
    result = monitoring.sync_once(now=100.0, force=True)

    assert result["ok"] is True
    assert result["stale"] == 1
    assert result["failed"] == 0
    assert service.get("owner-a@example.test", "api")["evidence"] == []


def test_probe_scheduler_status_is_hidden_when_gate_is_off(tmp_path):
    app = create_app(FleetConfig.from_root(
        tmp_path,
        dev_operator="owner-a@example.test",
        platform_enabled=True,
        service_monitoring_enabled=True,
        http_probe_enabled=True,
        http_probe_network_enabled=True,
        http_probe_sync_enabled=False,
        http_probe_allowed_origins=("https://probe.example.test",),
    ))
    assert app.test_client().get(
        "/api/platform/v1/http-probe/status",
    ).status_code == 404
