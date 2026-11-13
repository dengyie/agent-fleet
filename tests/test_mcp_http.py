"""Independent HTTPS MCP peers exercise the actual wire and SQLite boundary."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import socket
from threading import Thread
from typing import Any, Iterator

import pytest
from cryptography.hazmat.primitives import serialization
from werkzeug.serving import generate_adhoc_ssl_pair, make_server

from test_fleet_mcp_boundary import _app
from test_http_transport import tls_server
from test_mcp_connector import Broker, config, discover_payload
from test_provider_transport_boundaries import QuietHandler, serve
from test_workflow_http import body
from hub.platform.connectors.mcp import MCPConnector, MCPConnectorError, UrllibMCPTransport

VERSION = "2026-07-28"


@contextmanager
def fleet_server(app: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    cert, key = generate_adhoc_ssl_pair("localhost")
    cert_path, key_path = tmp_path / "fleet-cert.pem", tmp_path / "fleet-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    monkeypatch.setenv("SSL_CERT_FILE", str(cert_path))
    server = make_server("127.0.0.1", 0, app, threaded=True, ssl_context=(str(cert_path), str(key_path)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://localhost:{server.server_port}/api/platform/mcp"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


class MCPWireClient:
    def __init__(self, endpoint: str, token: str):
        self.endpoint = endpoint
        self.token = token
        self.transport = UrllibMCPTransport(private_aliases={endpoint: "127.0.0.1"})

    def request(self, method: str, params: dict[str, Any], *, request_id: str = "wire-1", headers: dict[str, str] | None = None):
        request_headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
            "Authorization": "Bearer " + self.token, "MCP-Protocol-Version": VERSION, "Mcp-Method": method}
        if method == "tools/call":
            request_headers["Mcp-Name"] = params.get("name", "")
        request_headers.update(headers or {})
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method,
                   "params": {**params, "_meta": {"io.modelcontextprotocol/protocolVersion": VERSION}}}
        response = self.transport.request(self.endpoint, headers=request_headers, body=json.dumps(payload).encode(), timeout_s=3)
        return response.status, json.loads(response.body)


def test_fleet_export_actual_https_client_and_restart_authorization(tmp_path, monkeypatch):
    from hub.infrastructure.integration_identity_repository import IntegrationIdentityRepository
    from hub.infrastructure.workflow_repository import WorkflowRepository
    app, _, setup, token, identity = _app(tmp_path)
    with fleet_server(app, tmp_path, monkeypatch) as endpoint:
        client = MCPWireClient(endpoint, token)
        assert client.request("server/discover", {})[1]["result"]["supportedVersions"] == [VERSION]
        status, listed = client.request("tools/list", {})
        assert status == 200 and len(listed["result"]["tools"]) == 3
        status, created = client.request("tools/call", {"name": "workflow.create_from_template", "arguments": body()})
        assert status == 200, created
        workflow = created["result"]["structuredContent"]["workflow_id"]
        assert client.request("tools/call", {"name": "workflow.create_from_template", "arguments": body()}) == (status, created)
        identities = IntegrationIdentityRepository(WorkflowRepository(setup.repository.db_path))
        identities.init()
        app.extensions["fleet"]["repositories"]["integration_identities"] = identities
        status, fetched = client.request("tools/call", {"name": "workflow.get", "arguments": {"workflow_id": workflow}})
        assert status == 200 and fetched["result"]["structuredContent"]["workflow_id"] == workflow
        status, artifacts = client.request("tools/call", {"name": "workflow.list_artifacts", "arguments": {"workflow_id": workflow, "limit": 1}})
        assert status == 200 and artifacts["result"]["structuredContent"]["artifacts"] == []
        assert client.request("tools/call", {"name": "workflow.get", "arguments": {"workflow_id": "workflow-a"}})[0] == 403
        identities.revoke(identity.owner_id, identity.integration_id, expected_digest=identity.definition_digest, now=101)
        assert client.request("tools/list", {})[0] == 401
    assert len(setup.repository.list(identity.owner_id)) == 2


@pytest.mark.parametrize("sse", [False, True])
def test_connector_independent_https_peer_validates_headers_and_structured_output(tmp_path, monkeypatch, sse):
    calls = []
    class Peer(QuietHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            method, params = request["method"], request["params"]
            calls.append((method, params, dict(self.headers)))
            assert self.path == "/mcp"
            assert self.headers["MCP-Protocol-Version"] == params["_meta"]["io.modelcontextprotocol/protocolVersion"] == VERSION
            assert self.headers["Mcp-Method"] == method
            assert self.headers["Authorization"] == "Bearer secret-token"
            if method == "tools/call":
                assert self.headers["Mcp-Name"] == params["name"] == "weather"
                result = {"structuredContent": {"temperature": 24}, "content": [{"type": "text", "text": "secret-token result"}]}
            else:
                result = json.loads(discover_payload()[0 if method == "server/discover" else 1].body)["result"]
            reply = {"jsonrpc": "2.0", "id": request["id"], "result": result}
            raw = json.dumps(reply).encode()
            if sse:
                raw = b": keepalive\n\ndata: " + raw + b"\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if sse else "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    with tls_server(tmp_path, monkeypatch, Peer) as server:
        endpoint = f"https://localhost:{server.server_port}/mcp"
        connector = MCPConnector(config(endpoint=endpoint), secret_broker=Broker(),
            transport=UrllibMCPTransport(private_aliases={endpoint: "127.0.0.1"}))
        connector.discover()
        result = connector.call("weather", {"city": "Singapore"})
        assert result.state == "succeeded" and result.structured_content == {"temperature": 24}
        assert "secret-token" not in repr(result)
    assert [item[0] for item in calls] == ["server/discover", "tools/list", "tools/call"]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_connector_redirect_never_replays_or_forwards_credentials(tmp_path, monkeypatch, status):
    seen, attempts = [], []
    class Destination(QuietHandler):
        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            self.reply()
        do_POST = do_GET
    with serve(Destination) as target:
        class Redirect(QuietHandler):
            def do_POST(self):
                attempts.append(True)
                self.drain()
                self.send_response(status)
                self.send_header("Location", f"http://127.0.0.1:{target.server_port}/leak")
                self.send_header("Content-Length", "0")
                self.end_headers()
        with tls_server(tmp_path, monkeypatch, Redirect) as origin:
            endpoint = f"https://localhost:{origin.server_port}/mcp"
            with pytest.raises(MCPConnectorError, match="redirect_rejected"):
                UrllibMCPTransport(private_aliases={endpoint: "127.0.0.1"}).request(endpoint,
                    headers={"Authorization": "Bearer sentinel"}, body=b"{}", timeout_s=2)
    assert seen == [] and len(attempts) == 1


def test_dns_result_is_checked_and_same_address_is_connected_without_second_lookup(tmp_path, monkeypatch):
    lookups, requests = [], []
    original = socket.getaddrinfo
    class Peer(QuietHandler):
        def do_POST(self):
            self.drain()
            requests.append(self.path)
            self.reply()
    with tls_server(tmp_path, monkeypatch, Peer) as server:
        def resolve(host, port, *args, **kwargs):
            lookups.append((host, port))
            assert len(lookups) == 1
            return original("127.0.0.2", port, *args, **kwargs) + original("127.0.0.1", port, *args, **kwargs)
        monkeypatch.setattr(socket, "getaddrinfo", resolve)
        endpoint = f"https://localhost:{server.server_port}/mcp"
        assert UrllibMCPTransport(private_aliases={endpoint: "127.0.0.1"}).request(endpoint, headers={}, body=b"{}", timeout_s=2).status == 200
    assert requests == ["/mcp"] and lookups == [("localhost", server.server_port)]


@pytest.mark.parametrize("trusted,hostname", [(False, "localhost"), (True, "127.0.0.1")])
def test_connector_tls_rejects_untrusted_or_wrong_hostname_before_credentials(tmp_path, monkeypatch, trusted, hostname):
    seen = []
    class Peer(QuietHandler):
        def do_POST(self):
            seen.append(True)
            self.reply()
    with tls_server(tmp_path, monkeypatch, Peer, trusted=trusted) as server:
        endpoint = f"https://{hostname}:{server.server_port}/mcp"
        with pytest.raises(MCPConnectorError, match="transport_error"):
            UrllibMCPTransport(private_aliases={endpoint: "127.0.0.1"}).request(endpoint,
                headers={"Authorization": "Bearer sentinel"}, body=b"{}", timeout_s=2)
    assert seen == []


@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
def test_sse_final_response_does_not_wait_for_http_eof(tmp_path, monkeypatch, newline):
    from threading import Event
    import threading
    import time
    disconnected = Event()
    timers = {thread.ident for thread in threading.enumerate() if thread.name == "fleet-http-deadline"}
    class Peer(QuietHandler):
        protocol_version = "HTTP/1.1"
        def do_POST(self):
            self.drain()
            notification = b'data: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}'
            result = json.dumps({"jsonrpc": "2.0", "id": "wire", "result": {"content": [], "message": "新加坡"}},
                                ensure_ascii=False, indent=2).encode()
            raw = notification + newline * 2 + newline.join(b"data: " + line for line in result.splitlines()) + newline * 2
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for byte in raw:
                self.wfile.write(b"1\r\n" + bytes((byte,)) + b"\r\n")
            self.wfile.flush()
            self.connection.settimeout(2)
            try:
                if self.connection.recv(1) == b"":
                    disconnected.set()
            except ConnectionResetError:
                disconnected.set()
    with tls_server(tmp_path, monkeypatch, Peer) as server:
        endpoint = f"https://localhost:{server.server_port}/mcp"
        started = time.monotonic()
        response = UrllibMCPTransport(private_aliases={endpoint: "127.0.0.1"}).request(endpoint, headers={},
            body=b'{"jsonrpc":"2.0","id":"wire","method":"tools/call"}', timeout_s=.5)
        from hub.platform.connectors.mcp import _json_body
        assert _json_body(response, "wire")["result"]["message"] == "新加坡"
        assert time.monotonic() - started < .4
        assert disconnected.wait(.5)
    assert {thread.ident for thread in threading.enumerate() if thread.name == "fleet-http-deadline"} == timers


@pytest.mark.parametrize("raw,code", [
    (b'data: {"jsonrpc":"2.0","id":"foreign","result":{}}\n\n', "invalid_response"),
    (b'data: {"jsonrpc":"2.0","id":"callback","method":"sampling/createMessage"}\n\n', "unsupported_callback"),
    (b'data: {"jsonrpc":"2.0","id":"wire","result":{},"result":{}}\n\n', "invalid_response"),
    (b'data: {"jsonrpc":"2.0","id":"wire","result":{}}\n', "invalid_response"),
    (b': keepalive\n\n', "invalid_response"),
    (b'data: ' + b'x' * (256 * 1024), "result_too_large"),
])
def test_sse_wire_rejects_malformed_incomplete_and_oversized_streams(tmp_path, monkeypatch, raw, code):
    calls = []
    class Peer(QuietHandler):
        def do_POST(self):
            self.drain()
            calls.append(True)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    with tls_server(tmp_path, monkeypatch, Peer) as server:
        endpoint = f"https://localhost:{server.server_port}/mcp"
        with pytest.raises(MCPConnectorError, match=code):
            UrllibMCPTransport(private_aliases={endpoint: "127.0.0.1"}).request(endpoint, headers={},
                body=b'{"jsonrpc":"2.0","id":"wire","method":"tools/call"}', timeout_s=2)
    assert calls == [True]


@pytest.mark.parametrize("enabled,acceptance", [(True, False), (False, False), (True, True)])
def test_native_run_calls_only_scoped_mcp_tools_through_the_injected_catalog(tmp_path, monkeypatch, enabled, acceptance):
    from hub.application.connector_catalog import ConnectorCatalog
    from hub.bootstrap import create_app
    from hub.config import FleetConfig
    from test_platform_run_worker import OWNER, _catalog, _queued
    from hub.platform.providers.base import ModelResponse

    calls, declarations = [], []
    class Peer(QuietHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            method = request["method"]
            calls.append(method)
            assert self.headers["Authorization"] == "Bearer secret-token"
            result = ({"structuredContent": {"temperature": 24},
                       "content": [{"type": "text", "text": "secret-token result"}]}
                      if method == "tools/call" else
                      json.loads(discover_payload()[0 if method == "server/discover" else 1].body)["result"])
            raw = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    class Provider:
        def complete(self, messages, tools, *, request_observer=None):
            declarations.append({tool["name"] for tool in tools})
            if len(declarations) == 1:
                return ModelResponse(kind="tool_call", tool="mcp.weather.weather", arguments={"city": "Singapore"})
            assert messages[-1]["result"]["structured_content"] == {"temperature": 24}
            assert "secret-token" not in repr(messages[-1]["result"])
            return ModelResponse(kind="final", text="24 degrees")

    with tls_server(tmp_path, monkeypatch, Peer) as server:
        endpoint = f"https://localhost:{server.server_port}/mcp"
        catalog = ConnectorCatalog(enabled=True)
        catalog.register(MCPConnector(config(endpoint=endpoint, owner_id=OWNER, workspace_id="home"),
            secret_broker=Broker(), transport=UrllibMCPTransport(private_aliases={endpoint: "127.0.0.1"})))
        catalog.discover("weather")
        app = create_app(FleetConfig.from_root(tmp_path, ingest_token="x", dev_operator=OWNER,
            platform_enabled=True, platform_worker_enabled=True, platform_mcp_connectors_enabled=enabled),
            repositories={"mcp_catalog": catalog})
        _catalog(app, tmp_path)
        if acceptance:
            conversation = app.test_client().post("/api/platform/v1/conversations", json={}).get_json()["conversation"]
            response = app.test_client().post(
                f"/api/platform/v1/conversations/{conversation['conversation_id']}/acceptance-turns",
                json={"text": "inspect", "client_token": "mcp-acceptance"})
            assert response.status_code == 202
            run = response.get_json()["run"]
        else:
            run = _queued(app)
        worker = app.extensions["fleet"]["services"]["platform_worker"]
        worker.provider_factory = lambda profile: Provider()
        result = worker.run_once(OWNER)
        assert result["state"] == ("succeeded" if enabled and not acceptance else "failed")
        assert ("mcp.weather.weather" in declarations[0]) == (enabled and not acceptance)
        events = app.test_client().get(f"/api/platform/v1/runs/{run['run_id']}/events").get_json()["events"]
        assert "secret-token" not in repr(events)
    assert calls == ["server/discover", "tools/list"] + (["tools/call"] if enabled and not acceptance else [])
