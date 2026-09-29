from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from hub.application.task_service import ApplicationError
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.integrations.http_probe import (
    HttpProbeClient, HttpProbeError, HttpProbePayloadContract, HttpProbeSnapshot,
    probe_evidence,
)

FIXTURE = Path(__file__).parent / "fixtures/platform/http_probe_schema_v1.json"


def test_versioned_fixture_normalizes_and_drops_unknown_fields():
    payload = json.loads(FIXTURE.read_text())
    snapshot = HttpProbePayloadContract().parse(payload)
    assert snapshot.probe_id == "api-health"
    assert snapshot.state == "healthy"
    assert snapshot.status_code == 200
    assert snapshot.latency_ms == 12.5
    assert snapshot.observed_at == 100.0
    assert "secret" not in snapshot.as_dict()
    assert "fixture-secret-sentinel" not in str(snapshot.as_dict())

    evidence = probe_evidence(snapshot, "api")
    assert evidence["dimension"] == "application_health"
    assert evidence["source"] == "http_probe_v1"
    assert evidence["state"] == "healthy"
    assert evidence["evidence_id"].startswith("http_probe_")
    assert "fixture-raw-sentinel" not in str(evidence)


def test_probe_evidence_can_bind_service_version_without_endpoint_details():
    snapshot = HttpProbeSnapshot("api-health", "healthy", 200, 1.0, 100.0)
    evidence = probe_evidence(snapshot, "api", service_version=3)
    assert evidence["detail"] == {
        "probe_id": "api-health", "status_code": 200,
        "latency_ms": 1.0, "service_version": 3,
    }
    assert evidence["evidence_id"] != probe_evidence(
        snapshot, "api", service_version=4,
    )["evidence_id"]


@pytest.mark.parametrize("service_version", [0, -1, 1.5, "3", True])
def test_probe_evidence_rejects_invalid_service_version(service_version):
    snapshot = HttpProbeSnapshot("api-health", "healthy", 200, 1.0, 100.0)
    with pytest.raises(HttpProbeError) as exc:
        probe_evidence(snapshot, "api", service_version=service_version)
    assert exc.value.code == "invalid_evidence"


def test_contract_uses_bounded_current_time_when_observation_is_omitted():
    snapshot = HttpProbePayloadContract().parse({
        "schema_version": 1,
        "probe": {"id": "api-health", "state": "healthy"},
    })
    assert snapshot.observed_at > 0


def test_public_snapshot_constructor_rejects_unbounded_fields():
    with pytest.raises(ValueError):
        HttpProbeSnapshot("api-health\nsecret", "healthy", 200, 1.0, 100.0)


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"schema_version": 2, "probe": {}}, "unsupported_schema"),
        ({"schema_version": "1", "probe": {}}, "unsupported_schema"),
        ({"schema_version": 1}, "invalid_response"),
        ({"schema_version": 1, "probe": {"state": "sideways"}}, "invalid_response"),
        ({"schema_version": 1, "probe": {"status_code": 999}}, "invalid_response"),
    ],
)
def test_contract_rejects_unsupported_or_malformed_payloads(payload, code):
    with pytest.raises(HttpProbeError) as exc:
        HttpProbePayloadContract().parse(payload)
    assert exc.value.code == code
    assert str(exc.value) == code


def test_network_gate_fails_before_requester_or_socket():
    client = HttpProbeClient(
        "http://127.0.0.1:9", "/health",
    )
    with pytest.raises(HttpProbeError) as exc:
        client.fetch()
    assert exc.value.code == "network_disabled"


class _Handler(BaseHTTPRequestHandler):
    payload = FIXTURE.read_bytes()
    seen = []
    status = 200

    def do_GET(self):  # noqa: N802
        type(self).seen.append((self.command, self.path, self.headers.get("Accept")))
        body = self.payload
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


@pytest.fixture
def loopback_server():
    _Handler.seen = []
    _Handler.status = 200
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_loopback_urllib_get_is_exact_and_health_evidence_is_bounded(loopback_server, tmp_path):
    port = loopback_server.server_address[1]
    client = HttpProbeClient(
        f"http://127.0.0.1:{port}", "/health",
        allow_network=True, allow_loopback=True, timeout_s=2,
    )
    snapshot = client.fetch()
    assert snapshot.probe_id == "api-health"
    assert _Handler.seen == [("GET", "/health", "application/json")]
    assert "fixture-token-sentinel" not in str(snapshot.as_dict())

    app = create_app(FleetConfig.from_root(
        tmp_path, dev_operator="owner-a@example.test",
        platform_enabled=True, service_monitoring_enabled=True,
    ))
    repo = app.extensions["fleet"]["platform_repository"]
    repo.upsert_node("owner-a@example.test", {"node_id": "node-a"})
    health = app.extensions["fleet"]["services"]["service_health"]
    health.register("owner-a@example.test", {
        "service_id": "api", "node_id": "node-a",
        "adapter": "http", "target_alias": "api-health",
    })
    saved = health.ingest("owner-a@example.test", probe_evidence(snapshot, "api"))
    assert saved["evidence"]["state"] == "healthy"
    assert saved["evidence"]["detail"] == {
        "probe_id": "api-health", "status_code": 200, "latency_ms": 12.5,
    }


@pytest.mark.parametrize(("status", "code"), [(401, "unauthorized"), (429, "rate_limited"), (503, "upstream_error")])
def test_loopback_http_errors_are_stable(loopback_server, status, code):
    _Handler.status = status
    port = loopback_server.server_address[1]
    client = HttpProbeClient(
        f"http://127.0.0.1:{port}", "/health",
        allow_network=True, allow_loopback=True,
    )
    with pytest.raises(HttpProbeError) as exc:
        client.fetch()
    assert exc.value.code == code
    assert str(exc.value) == code


def test_injected_malformed_json_and_oversized_body_are_bounded():
    with pytest.raises(HttpProbeError) as malformed:
        HttpProbeClient("http://example.test", "/health", requester=lambda **_: b"not-json").fetch()
    assert malformed.value.code == "invalid_response"
    with pytest.raises(HttpProbeError) as oversized:
        HttpProbeClient("http://example.test", "/health", requester=lambda **_: b"x" * (HttpProbeClient.MAX_BODY_BYTES + 1)).fetch()
    assert oversized.value.code == "response_too_large"


@pytest.mark.parametrize("path", ["/health?x=1", "/health#fragment", "/../admin", "/health//nested", "/health\nX-Test: injected"])
def test_probe_path_rejects_injection_or_normalization(path):
    with pytest.raises(HttpProbeError) as exc:
        HttpProbeClient("http://example.test", path)
    assert exc.value.code == "invalid_endpoint"
def test_pinned_resolver_address_avoids_hostname_re_resolution(loopback_server, monkeypatch):
    port = loopback_server.server_address[1]
    resolver_calls = []

    def resolver(host, service, **kwargs):
        resolver_calls.append((host, service, kwargs))
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port))]

    real_getaddrinfo = socket.getaddrinfo

    def reject_hostname_resolution(host, *args, **kwargs):
        if host == "probe.invalid":
            raise AssertionError("hostname was resolved after policy validation")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", reject_hostname_resolution)
    client = HttpProbeClient(
        f"http://probe.invalid:{port}", "/health",
        allow_network=True, allow_loopback=True, timeout_s=2, resolver=resolver,
    )

    snapshot = client.fetch()

    assert snapshot.probe_id == "api-health"
    assert resolver_calls == [("probe.invalid", port, {"type": socket.SOCK_STREAM})]
    assert _Handler.seen == [("GET", "/health", "application/json")]
