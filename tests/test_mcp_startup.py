"""Trusted configuration through the real Hub startup and operator CLI."""
from __future__ import annotations

import json
from dataclasses import asdict

import pytest
from test_mcp_connector import config, discover_payload

from hub import web
from hub.infrastructure.platform_db import PlatformRepository
from hub.platform.connectors.mcp import UrllibMCPTransport
from hub.platform.providers.openai_compatible import EnvironmentSecretBroker


def manifest(tmp_path, *, endpoint="https://mcp.example.test/mcp", private_aliases=None):
    path = tmp_path / "mcp.json"
    value = {"schema_version": 1, "connectors": [asdict(config(endpoint=endpoint, secret_ref="env://MCP_TEST_TOKEN"))],
             "private_aliases": private_aliases or {}}
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def seed(tmp_path):
    db = tmp_path / "platform.db"
    platform = PlatformRepository(db)
    platform.init()
    platform.upsert_workspace("owner", {"workspace_id": "workspace", "root_path": str(tmp_path)})
    return db


@pytest.mark.parametrize("source", ["environment", "file", "explicit_off", "environment_off"])
def test_normal_startup_attaches_without_network_or_secret_resolution(tmp_path, monkeypatch, source):
    monkeypatch.setattr(web, "FLEET_HOME", tmp_path)
    monkeypatch.delenv("AGENT_FLEET_PLATFORM_MCP_REGISTRATION_FILE", raising=False)
    path, db = manifest(tmp_path), seed(tmp_path)
    (tmp_path / "fleet-gates.conf").write_text(
        f"AGENT_FLEET_PLATFORM_MCP_REGISTRATION_FILE={path}\n", encoding="utf-8")
    options = {}
    if source in ("environment", "explicit_off"):
        monkeypatch.setenv("AGENT_FLEET_PLATFORM_MCP_REGISTRATION_FILE", str(path))
    if source == "explicit_off":
        options["platform_mcp_registration_file"] = ""
    if source == "environment_off":
        monkeypatch.setenv("AGENT_FLEET_PLATFORM_MCP_REGISTRATION_FILE", "")
    def forbidden(*args, **kwargs):
        pytest.fail("startup performed MCP network or credential IO")
    monkeypatch.setattr(UrllibMCPTransport, "request", forbidden)
    monkeypatch.setattr(EnvironmentSecretBroker, "resolve", forbidden)
    app = web.make_app(require_token=False, serve_frontend=False, platform_enabled=True,
        platform_db=db, platform_mcp_connectors_enabled=True, **options)
    catalog = app.extensions["fleet"]["services"]["mcp_catalog"]
    expected = frozenset() if source.endswith("off") else frozenset({"weather"})
    assert catalog.attached_ids() == expected
    assert len(catalog.store.get_many(("weather",))) == len(expected)
    assert catalog.run_snapshot(owner_id="owner", workspace_id="workspace") == {}


def test_cli_preview_does_not_create_store_or_resolve_secret(tmp_path, monkeypatch, capsys):
    from hub.mcp_setup import main
    path = manifest(tmp_path)
    db = tmp_path / "missing.db"
    def forbidden(*args, **kwargs):
        pytest.fail("preview performed network or credential IO")
    monkeypatch.setattr(UrllibMCPTransport, "request", forbidden)
    monkeypatch.setattr(EnvironmentSecretBroker, "resolve", forbidden)
    assert main(["--registration", str(path), "--database", str(db)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {"ok": True, "mode": "preview", "connector_ids": ["weather"]}
    assert not db.exists()


def test_cli_https_discovery_restart_call_and_pending_refresh(tmp_path, monkeypatch, capsys):
    from test_http_transport import tls_server
    from test_provider_transport_boundaries import QuietHandler

    from hub.mcp_setup import main
    calls = []
    changed = [False]
    class Peer(QuietHandler):
        def do_POST(self):
            message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            method = message["method"]
            calls.append(method)
            assert self.headers["Authorization"] == "Bearer startup-sentinel"
            if method == "tools/call":
                result = {"structuredContent": {"temperature": 24}, "content": []}
            else:
                result = json.loads(discover_payload()[0 if method == "server/discover" else 1].body)["result"]
                if changed[0] and method == "tools/list":
                    result["tools"][0]["description"] = "updated description"
            raw = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    monkeypatch.setenv("MCP_TEST_TOKEN", "startup-sentinel")
    monkeypatch.setattr(web, "FLEET_HOME", tmp_path)
    db = seed(tmp_path)
    with tls_server(tmp_path, monkeypatch, Peer) as server:
        endpoint = f"https://localhost:{server.server_port}/mcp"
        path = manifest(tmp_path, endpoint=endpoint, private_aliases={endpoint: "127.0.0.1"})
        args = ["--registration", str(path), "--database", str(db), "--connector", "weather", "--discover"]
        assert main(args) == 0
        assert json.loads(capsys.readouterr().out)["state"] == "active"
        assert calls == ["server/discover", "tools/list"]
        app = web.make_app(require_token=False, serve_frontend=False, platform_enabled=True,
            platform_db=db, platform_mcp_connectors_enabled=True, platform_mcp_registration_file=path)
        catalog = app.extensions["fleet"]["services"]["mcp_catalog"]
        pins = catalog.run_snapshot(owner_id="owner", workspace_id="workspace")
        assert list(pins) == ["mcp.weather.weather"]
        assert calls == ["server/discover", "tools/list"]
        result = catalog.call("mcp.weather.weather", {"city": "Singapore"}, owner_id="owner", workspace_id="workspace")
        assert result.state == "succeeded" and result.structured_content == {"temperature": 24}
        changed[0] = True
        assert main(args) == 0
        pending = json.loads(capsys.readouterr().out)
        assert pending["state"] == "pending" and pending["revision"] == 2
        assert catalog.run_snapshot(owner_id="owner", workspace_id="workspace") == pins
        record = catalog.store.get_many(("weather",))[0]
        catalog.revoke("weather", expected_digest=record.config_digest)
        assert main(args) == 2
        assert json.loads(capsys.readouterr().out)["error"] == "connector_revoked"
        restarted = web.make_app(require_token=False, serve_frontend=False, platform_enabled=True,
            platform_db=db, platform_mcp_connectors_enabled=True, platform_mcp_registration_file=path)
        assert restarted.extensions["fleet"]["services"]["mcp_catalog"].run_snapshot(
            owner_id="owner", workspace_id="workspace") == {}
        assert b"startup-sentinel" not in db.read_bytes()
    assert calls == ["server/discover", "tools/list", "tools/call", "server/discover", "tools/list"]


@pytest.mark.parametrize("change", ["version", "fields", "row_fields", "empty", "duplicate", "many", "tools",
    "secret", "endpoint", "alias", "unused_alias", "alias_type", "json_duplicate", "oversized", "unicode"])
def test_manifest_rejects_invalid_complete_configuration(tmp_path, change):
    from hub.composition.mcp_registration import (
        MAX_REGISTRATION_BYTES,
        read_mcp_registration,
    )
    from hub.domain.mcp_catalog import MCPCatalogError
    path = manifest(tmp_path)
    value = json.loads(path.read_text())
    if change == "version": value["schema_version"] = True
    elif change == "fields": value["unknown"] = 1
    elif change == "row_fields": value["connectors"][0]["token"] = "never-printed"
    elif change == "empty": value["connectors"] = []
    elif change == "duplicate": value["connectors"] *= 2
    elif change == "many": value["connectors"] *= 129
    elif change == "tools": value["connectors"][0]["selected_tools"] = "weather"
    elif change == "secret": value["connectors"][0]["secret_ref"] = "raw-token"
    elif change == "endpoint": value["connectors"][0]["endpoint"] = "http://mcp.example.test/mcp"
    elif change == "alias": value["private_aliases"] = {value["connectors"][0]["endpoint"]: "invalid"}
    elif change == "unused_alias": value["private_aliases"] = {"https://unused.test/mcp": "127.0.0.1"}
    elif change == "alias_type": value["private_aliases"] = []
    path.write_text(json.dumps(value), encoding="utf-8")
    if change == "json_duplicate": path.write_text('{"schema_version":1,"schema_version":1,"connectors":[]}')
    elif change == "oversized": path.write_bytes(b" " * (MAX_REGISTRATION_BYTES + 1))
    elif change == "unicode": path.write_bytes(b"\xff")
    with pytest.raises(MCPCatalogError):
        read_mcp_registration(path)


def test_default_limits_and_optional_aliases_use_original_contract(tmp_path):
    from hub.composition.mcp_registration import read_mcp_registration
    path = manifest(tmp_path)
    value = json.loads(path.read_text())
    del value["private_aliases"]
    del value["connectors"][0]["max_tools"]
    value["connectors"][0]["selected_tools"] = ["z", "a"]
    path.write_text(json.dumps(value), encoding="utf-8")
    adapters = read_mcp_registration(path)
    assert adapters[0].config.max_tools == 100
    assert adapters[0].config.selected_tools == ("z", "a")


@pytest.mark.parametrize("mode", ["gate_off", "platform_off", "workspace_missing", "injected", "relative", "missing"])
def test_startup_gate_scope_and_config_authority(tmp_path, mode):
    from test_mcp_catalog_persistence import _connector

    from hub.bootstrap import create_app
    from hub.config import FleetConfig
    path = manifest(tmp_path)
    if mode in ("gate_off", "platform_off", "missing"):
        path = tmp_path / "missing.json"
    elif mode == "relative":
        path = path.relative_to(tmp_path)
    seed(tmp_path)
    if mode == "workspace_missing":
        value = json.loads(path.read_text())
        value["connectors"][0]["workspace_id"] = "absent"
        path.write_text(json.dumps(value), encoding="utf-8")
    cfg = FleetConfig.from_root(tmp_path, serve_frontend=False, platform_enabled=mode != "platform_off",
        platform_mcp_connectors_enabled=mode != "gate_off", platform_mcp_registration_file=path)
    if mode in ("gate_off", "platform_off"):
        create_app(cfg)
    else:
        with pytest.raises((ValueError, RuntimeError)):
            create_app(cfg, repositories={"mcp_connectors": (_connector(),)} if mode == "injected" else {})


@pytest.mark.parametrize("mode", ["missing_database", "relative_database", "no_connector", "unknown_connector", "secret_missing", "conflict"])
def test_cli_errors_preserve_authority_and_do_not_print_configuration(tmp_path, monkeypatch, capsys, mode):
    from test_mcp_catalog_persistence import _connector, _store

    from hub.application.connector_catalog import ConnectorCatalog
    from hub.mcp_setup import main
    path, db = manifest(tmp_path), seed(tmp_path)
    args = ["--registration", str(path), "--discover", "--database",
            "relative.db" if mode == "relative_database" else str(tmp_path / "absent.db" if mode == "missing_database" else db)]
    if mode != "no_connector": args += ["--connector", "unknown" if mode == "unknown_connector" else "weather"]
    before = None
    if mode == "conflict":
        store = _store(db)
        catalog = ConnectorCatalog(enabled=True, store=store)
        catalog.register(_connector())
        before = store.get_many(("weather",))[0]
    monkeypatch.delenv("MCP_TEST_TOKEN", raising=False)
    def forbidden(*args, **kwargs):
        pytest.fail("invalid setup performed network IO")
    monkeypatch.setattr(UrllibMCPTransport, "request", forbidden)
    assert main(args) == 2
    output = capsys.readouterr().out
    assert json.loads(output)["ok"] is False
    assert all(value not in output for value in ("env://", "mcp.example.test", "secret-token", "Traceback"))
    if before is not None:
        assert store.get_many(("weather",))[0] == before


def test_cli_module_entry_point_preview(tmp_path):
    import subprocess
    import sys
    path = manifest(tmp_path)
    result = subprocess.run([sys.executable, "-m", "hub.mcp_setup", "--registration", str(path)],
        capture_output=True, text=True, timeout=10, check=True)
    assert json.loads(result.stdout) == {"ok": True, "mode": "preview", "connector_ids": ["weather"]}
    assert result.stderr == ""
