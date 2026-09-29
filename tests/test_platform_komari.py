from pathlib import Path
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError

import pytest

from hub.application.service_health_service import ServiceHealthService
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.infrastructure.incident_repository import IncidentRepository
from hub.infrastructure.service_repository import ServiceRepository
from hub.integrations.health_events import IncidentService, incident_fingerprint
from hub.integrations.komari import KomariClient, KomariError, normalize_nodes


def _data(*, fingerprint="fp-api", evidence_id="ev-1", failure_class="health_unhealthy", service_id="api"):
    return {
        "service_id": service_id,
        "failure_class": failure_class,
        "fingerprint": fingerprint,
        "evidence_id": evidence_id,
    }


def test_normalize_nodes_accepts_allowlisted_shapes_and_drops_unknown_rows():
    rows = normalize_nodes(
        {
            "data": [
                {
                    "uuid": "komari-uuid-1",
                    "hostname": "edge-1",
                    "online": True,
                    "status": "online",
                    "version": "1.4.2",
                    "secret": "must-not-leak",
                },
                {"name": "missing-id"},
            ],
        },
        observed_at=100,
    )
    assert len(rows) == 1
    assert rows[0].external_id == "komari-uuid-1"
    assert rows[0].online is True
    assert "secret" not in rows[0].detail
    assert rows[0].as_dict()["observed_at"] == 100


def test_normalize_nodes_accepts_sanitized_schema_v1_fixture_without_leaking_unknown_fields():
    fixture = json.loads((Path(__file__).parent / "fixtures/platform/komari_schema_v1.json").read_text())
    rows = normalize_nodes(fixture, observed_at=100)

    assert [(row.external_id, row.online) for row in rows] == [
        ("fixture-node-1", True), ("fixture-node-2", False),
    ]
    rendered = json.dumps([row.as_dict() for row in rows], sort_keys=True)
    assert "fixture-sentinel-ignore" not in rendered
    assert "fixture-token-ignore" not in rendered
    assert "fixture-raw-output-ignore" not in rendered
    assert "fixture-envelope-ignore" not in rendered


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 2, "nodes": []},
        {"schema_version": "1", "nodes": []},
        {"schema_version": 1, "nodes": "not-a-list"},
        {"error": "upstream failure"},
        {"data": {"message": "not node rows"}},
        "not-json-object",
    ],
)
def test_normalize_nodes_rejects_malformed_or_unsupported_schema(payload):
    with pytest.raises(KomariError) as exc:
        normalize_nodes(payload, observed_at=100)
    assert exc.value.code in {"invalid_response", "unsupported_schema"}


def test_unversioned_injected_payload_remains_backward_compatible():
    rows = normalize_nodes({"nodes": [{"id": "legacy-1", "online": True}]}, observed_at=100)
    assert rows[0].external_id == "legacy-1"


def test_komari_client_bounds_response_and_never_exposes_token():
    token = "token-value-that-must-not-appear"
    calls = []
    client = KomariClient(
        "https://vps.example.test",
        token,
        nodes_path="/api/clients",
        requester=lambda **kwargs: calls.append(kwargs) or b'{"nodes":[{"id":"node-1","online":false}]}',
    )
    snapshots = client.fetch_nodes(observed_at=12)
    assert snapshots[0].online is False
    assert calls[0]["url"] == "https://vps.example.test/api/clients"
    assert calls[0]["token"] == token
    assert token not in str(snapshots)

    oversized = KomariClient(
        "https://vps.example.test",
        token,
        nodes_path="/api/clients",
        requester=lambda **kwargs: b"x" * (KomariClient.MAX_BODY_BYTES + 1),
    )
    with pytest.raises(KomariError) as exc:
        oversized.fetch_nodes()
    assert exc.value.code == "response_too_large"
    assert token not in str(exc.value)


def test_malformed_komari_payload_becomes_unknown_incident_evidence(tmp_path):
    _, service_repository, health, incidents = _monitoring(tmp_path)
    integration = IncidentService(
        health, incidents, node_mapping={"node-a": "fixture-node-1"}, clock=lambda: 100.0,
    )
    services = service_repository.list_services("owner-a@example.test")
    client = KomariClient(
        "https://vps.example.test", "opaque-token", nodes_path="/api/clients",
        requester=lambda **kwargs: {"error": "fixture-sentinel-ignore"},
    )

    result = integration.sync_from_client("owner-a@example.test", services, client)

    evidence = result["results"][0]["evidence"]
    assert evidence["state"] == "unknown"
    assert evidence["detail"]["collector_status"] == "invalid_response"
    assert "fixture-sentinel-ignore" not in json.dumps(result)


def test_komari_client_requires_explicit_path_and_read_only_requester():
    with pytest.raises(KomariError) as invalid:
        KomariClient("https://vps.example.test", "secret", nodes_path="api/clients")
    assert invalid.value.code == "invalid_endpoint"
    with pytest.raises(KomariError) as missing:
        KomariClient("https://vps.example.test", "secret", nodes_path="/api/clients").fetch_nodes()
    assert missing.value.code == "network_disabled"
    with pytest.raises(KomariError) as query:
        KomariClient("https://vps.example.test?tenant=secret", "secret", nodes_path="/api/clients")
    assert query.value.code == "invalid_endpoint"


class _KomariFixtureHandler(BaseHTTPRequestHandler):
    requests = []
    response_status = 200
    response_body = b'{"nodes":[{"uuid":"fixture-1","hostname":"loopback","online":true}]}'

    def do_GET(self):
        self.__class__.requests.append({
            "method": self.command,
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
        })
        payload = self.__class__.response_body
        self.send_response(self.__class__.response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        return


@pytest.fixture
def komari_http_fixture():
    _KomariFixtureHandler.requests = []
    _KomariFixtureHandler.response_status = 200
    _KomariFixtureHandler.response_body = b'{"nodes":[{"uuid":"fixture-1","hostname":"loopback","online":true}]}'
    server = ThreadingHTTPServer(("127.0.0.1", 0), _KomariFixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, _KomariFixtureHandler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_komari_real_urllib_loopback_canary_is_read_only_and_redacted(komari_http_fixture):
    server, handler = komari_http_fixture
    token = "loopback-secret-token"
    client = KomariClient(
        f"http://127.0.0.1:{server.server_port}", token,
        nodes_path="/api/clients", allow_network=True,
    )

    snapshots = client.fetch_nodes(observed_at=12)

    assert snapshots[0].external_id == "fixture-1"
    assert snapshots[0].online is True
    assert handler.requests == [{
        "method": "GET", "path": "/api/clients",
        "authorization": "Bearer " + token,
    }]
    assert token not in str(snapshots)


def test_komari_network_gate_fails_before_socket_and_maps_http_errors(komari_http_fixture):
    server, handler = komari_http_fixture
    client = KomariClient(
        f"http://127.0.0.1:{server.server_port}", "secret",
        nodes_path="/api/clients",
    )
    with pytest.raises(KomariError) as disabled:
        client.fetch_nodes()
    assert disabled.value.code == "network_disabled"
    assert handler.requests == []

    handler.response_status = 401
    authorized = KomariClient(
        f"http://127.0.0.1:{server.server_port}", "secret",
        nodes_path="/api/clients", allow_network=True,
    )
    with pytest.raises(KomariError) as unauthorized:
        authorized.fetch_nodes()
    assert unauthorized.value.code == "unauthorized"
    assert "secret" not in str(unauthorized.value)


@pytest.mark.parametrize(
    ("status", "expected"),
    [(429, "rate_limited"), (500, "upstream_error"), (503, "upstream_error"), (404, "http_error")],
)
def test_komari_loopback_maps_http_statuses(komari_http_fixture, status, expected):
    server, handler = komari_http_fixture
    handler.response_status = status
    client = KomariClient(
        f"http://127.0.0.1:{server.server_port}", "status-secret",
        nodes_path="/api/clients", allow_network=True,
    )
    with pytest.raises(KomariError) as exc:
        client.fetch_nodes()
    assert exc.value.code == expected
    assert "status-secret" not in str(exc.value)


def test_komari_loopback_rejects_oversized_and_invalid_json(komari_http_fixture):
    server, handler = komari_http_fixture
    handler.response_body = b"x" * (KomariClient.MAX_BODY_BYTES + 1)
    client = KomariClient(
        f"http://127.0.0.1:{server.server_port}", "body-secret",
        nodes_path="/api/clients", allow_network=True,
    )
    with pytest.raises(KomariError) as oversized:
        client.fetch_nodes()
    assert oversized.value.code == "response_too_large"

    handler.response_body = b"not-json"
    with pytest.raises(KomariError) as malformed:
        client.fetch_nodes()
    assert malformed.value.code == "invalid_response"


def test_komari_maps_transport_failures_without_secret(monkeypatch):
    client = KomariClient(
        "https://vps.example.test", "transport-secret",
        nodes_path="/api/clients", allow_network=True,
    )

    monkeypatch.setattr(
        "hub.integrations.komari.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(socket.timeout()),
    )
    with pytest.raises(KomariError) as timeout:
        client.fetch_nodes()
    assert timeout.value.code == "source_unavailable"

    monkeypatch.setattr(
        "hub.integrations.komari.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("offline")),
    )
    with pytest.raises(KomariError) as unavailable:
        client.fetch_nodes()
    assert unavailable.value.code == "source_unavailable"
    assert "transport-secret" not in str(unavailable.value)


def test_komari_loopback_monitoring_sync_writes_incident_evidence(tmp_path, komari_http_fixture):
    server, _handler = komari_http_fixture
    platform, service_repository, health, incidents = _monitoring(tmp_path)
    integration = IncidentService(
        health, incidents, node_mapping={"node-a": "fixture-1"}, clock=lambda: 100.0,
    )
    service = KomariClient(
        f"http://127.0.0.1:{server.server_port}", "secret",
        nodes_path="/api/clients", allow_network=True,
    )
    result = integration.sync_from_client(
        "owner-a@example.test", service_repository.list_services("owner-a@example.test"), service,
    )
    assert result["results"][0]["evidence"]["state"] == "healthy"
    assert result["results"][0]["incident"] is None


def _monitoring(tmp_path: Path):
    service_repository = ServiceRepository(tmp_path / "platform.db", clock=lambda: 100.0)
    service_repository.init()
    service_repository._connect().close()
    platform = __import__("hub.infrastructure.platform_db", fromlist=["PlatformRepository"]).PlatformRepository(tmp_path / "platform.db")
    platform.init()
    platform.upsert_node("owner-a@example.test", {"node_id": "node-a"})
    health = ServiceHealthService(service_repository, clock=lambda: 100.0)
    health.register(
        "owner-a@example.test",
        {
            "service_id": "api",
            "node_id": "node-a",
            "name": "API",
            "adapter": "http",
            "target_alias": "api-health",
        },
    )
    incidents = IncidentRepository(tmp_path / "platform.db", clock=lambda: 100.0)
    incidents.init()
    return platform, service_repository, health, incidents


def test_incident_service_uses_explicit_mapping_and_source_unavailable(tmp_path):
    platform, service_repository, health, incidents = _monitoring(tmp_path)
    integration = IncidentService(
        health,
        incidents,
        node_mapping={"node-a": "komari-1"},
        clock=lambda: 100.0,
    )
    services = service_repository.list_services("owner-a@example.test")
    client = KomariClient(
        "https://vps.example.test",
        "opaque-token",
        nodes_path="/api/clients",
        requester=lambda **kwargs: {"nodes": [{"uuid": "komari-1", "online": False}]},
    )
    failed = integration.sync_from_client("owner-a@example.test", services, client)
    assert failed["results"][0]["evidence"]["state"] == "unhealthy"
    assert failed["results"][0]["incident"]["failure_class"] == "health_unhealthy"
    assert failed["results"][0]["incident"]["fingerprint"] == incident_fingerprint("api", "komari_node_reachability")

    unavailable = integration.sync_from_client(
        "owner-a@example.test",
        services,
        KomariClient(
            "https://vps.example.test",
            "opaque-token",
            nodes_path="/api/clients",
            requester=lambda **kwargs: (_ for _ in ()).throw(TimeoutError()),
        ),
    )
    assert unavailable["results"][0]["evidence"]["state"] == "unknown"
    assert unavailable["results"][0]["incident"]["failure_class"] == "source_unavailable"
    assert unavailable["results"][0]["incident"]["latest_state"] == "unknown"


def test_incident_service_does_not_guess_unmapped_node(tmp_path):
    _, service_repository, health, incidents = _monitoring(tmp_path)
    integration = IncidentService(health, incidents, node_mapping={}, clock=lambda: 100.0)
    result = integration.sync(
        "owner-a@example.test",
        service_repository.list_services("owner-a@example.test"),
        [],
    )
    assert result["results"][0]["evidence"]["state"] == "unknown"
    assert result["results"][0]["evidence"]["detail"]["collector_status"] == "node_missing"


def test_incident_api_is_gate_scoped_owner_scoped_and_read_only(tmp_path):
    disabled = create_app(FleetConfig.from_root(
        tmp_path / "disabled", dev_operator="owner-a@example.test",
        platform_enabled=True, service_monitoring_enabled=False,
    ))
    assert disabled.test_client().get("/api/platform/v1/incidents").status_code == 404

    app = create_app(FleetConfig.from_root(
        tmp_path / "enabled", dev_operator="owner-a@example.test",
        platform_enabled=True, service_monitoring_enabled=True,
    ))
    repository = app.extensions["fleet"]["repositories"]["platform_incidents"]
    row = repository.open_or_update(
        "owner-a@example.test", _data(), observed_at=10,
        source="komari", state="unhealthy",
    )
    client = app.test_client()
    assert client.get("/api/platform/v1/incidents").get_json()["incidents"][0]["incident_id"] == row["incident_id"]
    assert client.get(f"/api/platform/v1/incidents/{row['incident_id']}").status_code == 200
    assert client.get("/api/platform/v1/incidents", headers={"Cf-Access-Authenticated-User-Email": "owner-b@example.test"}).get_json()["incidents"] == []
    assert client.get("/api/platform/v1/incidents?state=bad").status_code == 400


def test_komari_sync_route_is_gate_scoped_and_uses_injected_client(tmp_path):
    app = create_app(FleetConfig.from_root(
        tmp_path, dev_operator="owner-a@example.test", platform_enabled=True,
        service_monitoring_enabled=True, komari_enabled=True,
        komari_base_url="https://vps.example.test", komari_nodes_path="/api/clients",
        komari_token="opaque-token", komari_node_mapping={"node-a": "komari-1"},
    ))
    service = app.extensions["fleet"]["services"]["service_health"]
    repo = app.extensions["fleet"]["platform_repository"]
    repo.upsert_node("owner-a@example.test", {"node_id": "node-a"})
    service.register("owner-a@example.test", {
        "service_id": "api", "node_id": "node-a", "adapter": "http",
        "target_alias": "api-health",
    })
    class FakeKomari:
        def fetch_nodes(self, **kwargs):
            from hub.integrations.komari import KomariNodeSnapshot
            return [KomariNodeSnapshot("komari-1", "edge", False, 100, {})]
    app.extensions["fleet"]["integrations"]["komari"] = FakeKomari()
    response = app.test_client().post("/api/platform/v1/komari/sync")
    assert response.status_code == 200
    assert response.get_json()["results"][0]["incident"]["failure_class"] == "health_unhealthy"
