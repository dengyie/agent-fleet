"""Real catalog registration, offline restart and explicit original discovery."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.domain.mcp_catalog import MCPCatalogError
from hub.platform.connectors.mcp import UrllibMCPTransport
from hub.platform.providers.openai_compatible import EnvironmentSecretBroker
from test_account_http import ORIGIN, PASSWORD, post, signup
from test_mcp_catalog_admin import request
from test_mcp_catalog_persistence import _messages
from test_mcp_connector import config, response

ROOT = Path(__file__).resolve().parents[1]
REAL_TRANSPORT_REQUEST = UrllibMCPTransport.request
REAL_ENVIRONMENT_RESOLVE = EnvironmentSecretBroker.resolve


def payload(**changes):
    row = asdict(config(secret_ref="env://MCP_TEST_TOKEN"))
    row["selected_tools"] = list(row["selected_tools"])
    return {"connector": {**row, **changes}}


def build_setup(tmp_path, monkeypatch):
    mail = []
    settings = FleetConfig.from_root(tmp_path, frontend_dir=ROOT / "frontend", platform_enabled=True,
        platform_mcp_connectors_enabled=True, platform_mcp_catalog_registration_enabled=True,
        accounts={"enabled": True, "origin": ORIGIN, "registration": "open",
                  "sender": lambda *args: mail.append(args)})
    app = create_app(settings)
    app.testing = True
    platform = app.extensions["fleet"]["platform_repository"]
    platform.upsert_workspace("owner", {"workspace_id": "workspace", "root_path": str(tmp_path)})
    app.extensions["accounts"].bootstrap_admin("admin@example.test", PASSWORD)
    admin = app.test_client()
    assert post(admin, "login", {"email": "admin@example.test", "password": PASSWORD}).status_code == 200

    def forbidden(*args, **kwargs):
        pytest.fail("offline operation resolved a secret or contacted the MCP service")

    monkeypatch.setattr(UrllibMCPTransport, "request", forbidden)
    monkeypatch.setattr(EnvironmentSecretBroker, "resolve", forbidden)
    return SimpleNamespace(app=app, admin=admin, platform=platform, settings=settings,
        catalog=app.extensions["fleet"]["services"]["mcp_catalog"], mail=mail, root=tmp_path)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    return build_setup(tmp_path, monkeypatch)


def register(setup, **changes):
    result = request(setup.admin, "post", "/register", json=payload(**changes))
    assert result.status_code == 200, result.get_json()
    return result.get_json()["connector"]


def install_peer(monkeypatch, *, changed=False, on_list=None):
    calls = []
    messages = {"server/discover": _messages()[0], "tools/list": _messages("schema" if changed else None)[1]}

    def transport(self, endpoint, *, headers, body, timeout_s):
        message = json.loads(body)
        calls.append(message["method"])
        if message["method"] == "tools/call":
            document = {"result": {"structuredContent": {"temperature": 24}, "content": []}}
        else:
            document = json.loads(messages[message["method"]].body)
        if message["method"] == "tools/list" and on_list is not None:
            on_list()
        return response({**document, "jsonrpc": "2.0", "id": message["id"]})

    monkeypatch.setattr(UrllibMCPTransport, "request", transport)
    monkeypatch.setattr(EnvironmentSecretBroker, "resolve", lambda self, ref: "mcp-registration-sentinel")
    return calls


def test_registration_is_offline_durable_and_attached(setup):
    detail = register(setup)
    item = detail["summary"]
    assert (item["connector_id"], item["owner_id"], item["workspace_id"]) == ("weather", "owner", "workspace")
    assert (item["state"], item["adapter_attached"], item["runtime_ready"], item["epoch"]) == ("registered", True, False, 1)
    assert detail["active"] is None and detail["pending"] is None
    assert setup.catalog.attached_ids() == frozenset({"weather"})
    assert setup.catalog.run_snapshot(owner_id="owner", workspace_id="workspace") == {}
    raw = json.dumps(detail)
    for private in ("secret_ref", "MCP_TEST_TOKEN", "config_digest", "config_snapshot"):
        assert private not in raw


def test_exact_registration_repeat_preserves_first_record_even_if_workspace_disabled(setup):
    register(setup)
    before = setup.catalog.store.get_many(("weather",))[0]
    setup.platform.upsert_workspace("owner", {"workspace_id": "workspace", "root_path": str(setup.root), "enabled": False})
    register(setup)
    assert setup.catalog.store.get_many(("weather",))[0] == before


@pytest.mark.parametrize("change", [
    {"endpoint": "https://other.example.test/mcp"}, {"secret_ref": "env://OTHER_TOKEN"},
    {"owner_id": "other"}, {"selected_tools": ["different"]}, {"timeout_s": 1},
])
def test_registration_conflicts_never_overwrite_original(setup, change):
    register(setup)
    before = setup.catalog.store.get_many(("weather",))[0]
    result = request(setup.admin, "post", "/register", json=payload(**change))
    assert result.status_code == 409
    assert setup.catalog.store.get_many(("weather",))[0] == before


@pytest.mark.parametrize("state", ["revoked", "restore_disabled"])
def test_disabled_repeat_and_restart_never_reactivate(setup, state):
    register(setup)
    record = setup.catalog.store.get_many(("weather",))[0]
    if state == "revoked":
        setup.catalog.store.revoke("weather", expected_digest=record.config_digest, now=record.updated_at + 1)
    else:
        with sqlite3.connect(setup.catalog.store.db_path) as connection:
            connection.execute("UPDATE mcp_catalog_connectors SET restore_disabled=1 WHERE connector_id='weather'")
    before = setup.catalog.store.get_many(("weather",))[0]
    assert register(setup)["summary"]["state"] == state
    assert setup.catalog.store.get_many(("weather",))[0] == before
    restarted = create_app(setup.settings)
    catalog = restarted.extensions["fleet"]["services"]["mcp_catalog"]
    assert catalog.attached_ids() == frozenset()
    assert catalog.store.get_many(("weather",))[0] == before
    assert catalog.run_snapshot(owner_id="owner", workspace_id="workspace") == {}


@pytest.mark.parametrize("change", [{"workspace_id": "missing"}, {"owner_id": "foreign"}])
def test_new_registration_requires_existing_owner_workspace(setup, change):
    result = request(setup.admin, "post", "/register", json=payload(**change))
    assert result.status_code == 409
    assert setup.catalog.store.get_many(("weather",)) == ()
    assert setup.catalog.attached_ids() == frozenset()


def test_new_registration_rejects_disabled_workspace(setup):
    setup.platform.upsert_workspace("owner", {"workspace_id": "workspace", "root_path": str(setup.root), "enabled": False})
    result = request(setup.admin, "post", "/register", json=payload())
    assert result.status_code == 409
    assert setup.catalog.store.get_many(("weather",)) == ()


def test_insert_failure_rolls_back_without_attaching_runtime(setup, monkeypatch):
    def broken(connection, connector_id):
        raise sqlite3.OperationalError("registration insertion failed")
    monkeypatch.setattr(setup.catalog.store, "_get", broken)
    result = request(setup.admin, "post", "/register", json=payload())
    assert result.status_code == 503
    assert setup.catalog.store.get_many(("weather",)) == ()
    assert setup.catalog.attached_ids() == frozenset()


def test_normal_restart_attaches_original_registration_without_file_or_io(setup):
    register(setup)
    before = setup.catalog.store.get_many(("weather",))[0]
    restarted = create_app(setup.settings)
    catalog = restarted.extensions["fleet"]["services"]["mcp_catalog"]
    assert catalog.attached_ids() == frozenset({"weather"})
    assert catalog.store.get_many(("weather",))[0] == before


def test_registration_gate_off_preserves_original_unattached_startup(setup):
    register(setup)
    settings = FleetConfig.from_root(setup.root, frontend_dir=ROOT / "frontend", platform_enabled=True,
        platform_mcp_connectors_enabled=True, accounts={"enabled": True, "origin": ORIGIN})
    restarted = create_app(settings)
    catalog = restarted.extensions["fleet"]["services"]["mcp_catalog"]
    assert catalog.attached_ids() == frozenset()
    assert len(catalog.store.get_many(("weather",))) == 1
    admin = restarted.test_client()
    assert post(admin, "login", {"email": "admin@example.test", "password": PASSWORD}).status_code == 200
    assert request(admin, "post", "/register", json=payload()).status_code == 503
    assert admin.get("/api/accounts/me", base_url=ORIGIN).get_json().get("features", {}).get("mcp_catalog_registration", False) is False


def test_registration_capability_is_only_advertised_to_current_admin(setup):
    me = setup.admin.get("/api/accounts/me", base_url=ORIGIN).get_json()
    assert me["features"]["mcp_catalog_registration"] is True
    ordinary = setup.app.test_client()
    assert signup(ordinary, setup.mail, "ordinary@example.test").status_code == 200
    assert ordinary.get("/api/accounts/me", base_url=ORIGIN).get_json().get("features", {}).get("mcp_catalog_registration", False) is False
    assert request(ordinary, "post", "/register", json=payload()).status_code == 403
    assert request(setup.app.test_client(), "post", "/register", json=payload()).status_code == 401


@pytest.mark.parametrize("change", [
    {"secret_ref": "raw-token"}, {"secret_ref": "secret://outside"}, {"secret_ref": "env://lowercase"},
    {"unknown": True}, {"selected_tools": "weather"}, {"selected_tools": ["weather", "weather"]},
    {"risk": "write"}, {"timeout_s": True}, {"max_tools": 101}, {"owner_id": "x" * 129},
])
def test_registration_strict_fields_and_original_bounds(setup, change):
    result = request(setup.admin, "post", "/register", json=payload(**change))
    assert result.status_code == 400
    assert setup.catalog.store.get_many(("weather",)) == ()


def test_original_optional_max_tools_and_exact_utf8_scope(setup):
    owner, workspace = "\ufefftenant", "workspace-a"
    setup.platform.upsert_workspace(owner, {"workspace_id": workspace, "root_path": str(setup.root)})
    body = payload(owner_id=owner, workspace_id=workspace)
    body["connector"].pop("max_tools")
    result = request(setup.admin, "post", "/register", json=body)
    assert result.status_code == 200
    assert result.get_json()["connector"]["summary"]["owner_id"] == owner
    assert result.get_json()["connector"]["summary"]["workspace_id"] == workspace


def test_registration_body_origin_query_and_no_store_boundaries(setup):
    assert request(setup.admin, "post", "/register", json=payload(), headers={"Origin": "https://other.example"}).status_code == 403
    assert request(setup.admin, "post", "/register?extra=1", json=payload()).status_code == 400
    assert request(setup.admin, "post", "/register", data='{"connector":{},"connector":{}}', content_type="application/json").status_code == 400
    oversized = request(setup.admin, "post", "/register", data=" " * 16385, content_type="application/json")
    assert oversized.status_code == 413 and oversized.headers["Cache-Control"] == "no-store"
    assert request(setup.admin, "post", "/register", data="x", content_type="text/plain").status_code == 415


def test_registration_command_is_frozen_and_snapshots_caller_values():
    from hub.application.mcp_registration import MCPRegistrationCommand
    body = payload()
    command = MCPRegistrationCommand.parse(body)
    body["connector"]["selected_tools"].append("changed")
    assert json.loads(command.config_snapshot)["selected_tools"] == ["weather"]
    with pytest.raises(FrozenInstanceError):
        command.config_snapshot = b"{}"
    with pytest.raises(MCPCatalogError):
        MCPRegistrationCommand.parse({"connector": payload()["connector"], "extra": True})


def test_explicit_discovery_and_original_tool_call_survive_normal_restart(setup, monkeypatch):
    initial = register(setup)
    calls = install_peer(monkeypatch)
    discovered = request(setup.admin, "post", "/discover?connector_id=weather", json={"expected_epoch": initial["summary"]["epoch"]})
    assert discovered.status_code == 200, discovered.get_json()
    assert discovered.get_json()["connector"]["summary"]["runtime_ready"] is True
    assert calls == ["server/discover", "tools/list"]
    pins = setup.catalog.run_snapshot(owner_id="owner", workspace_id="workspace")
    restarted = create_app(setup.settings)
    catalog = restarted.extensions["fleet"]["services"]["mcp_catalog"]
    assert catalog.run_snapshot(owner_id="owner", workspace_id="workspace") == pins
    assert calls == ["server/discover", "tools/list"]
    result = catalog.call("mcp.weather.weather", {"city": "Singapore"}, owner_id="owner", workspace_id="workspace")
    assert result.state == "succeeded" and result.structured_content == {"temperature": 24}
    assert calls == ["server/discover", "tools/list", "tools/call"]


def test_stale_discovery_epoch_rejects_before_io(setup):
    register(setup)
    result = request(setup.admin, "post", "/discover?connector_id=weather", json={"expected_epoch": 2})
    assert result.status_code == 409
    assert setup.catalog.store.get_many(("weather",))[0].epoch == 1


def test_changed_discovery_is_pending_and_preserves_original_run_pins(setup, monkeypatch):
    register(setup)
    calls = install_peer(monkeypatch)
    first = request(setup.admin, "post", "/discover?connector_id=weather", json={"expected_epoch": 1})
    assert first.status_code == 200
    pins = setup.catalog.run_snapshot(owner_id="owner", workspace_id="workspace")
    changed_calls = install_peer(monkeypatch, changed=True)
    second = request(setup.admin, "post", "/discover?connector_id=weather", json={"expected_epoch": first.get_json()["connector"]["summary"]["epoch"]})
    assert second.status_code == 200
    detail = second.get_json()["connector"]
    assert detail["summary"]["state"] == "pending" and detail["summary"]["pending_revision"] == 2
    assert setup.catalog.run_snapshot(owner_id="owner", workspace_id="workspace") == pins
    assert calls == changed_calls == ["server/discover", "tools/list"]


def test_revocation_during_discovery_prevents_publication(setup, monkeypatch):
    register(setup)
    def revoke():
        record = setup.catalog.store.get_many(("weather",))[0]
        setup.catalog.store.revoke("weather", expected_digest=record.config_digest, now=record.updated_at + 1)
    calls = install_peer(monkeypatch, on_list=revoke)
    result = request(setup.admin, "post", "/discover?connector_id=weather", json={"expected_epoch": 1})
    assert result.status_code == 409
    record = setup.catalog.store.get_many(("weather",))[0]
    assert record.revoked_at is not None and record.active_snapshot is None
    assert calls == ["server/discover", "tools/list"]


def test_existing_peer_hub_observes_registered_tools_without_restart(setup, monkeypatch):
    peer = create_app(setup.settings)
    peer_catalog = peer.extensions["fleet"]["services"]["mcp_catalog"]
    assert peer_catalog.attached_ids() == frozenset()
    register(setup)
    calls = install_peer(monkeypatch)
    discovered = request(setup.admin, "post", "/discover?connector_id=weather", json={"expected_epoch": 1})
    assert discovered.status_code == 200
    assert list(peer_catalog.run_snapshot(owner_id="owner", workspace_id="workspace")) == ["mcp.weather.weather"]
    assert calls == ["server/discover", "tools/list"]
    result = peer_catalog.call("mcp.weather.weather", {"city": "Singapore"}, owner_id="owner", workspace_id="workspace")
    assert result.state == "succeeded"
    assert calls == ["server/discover", "tools/list", "tools/call"]


def test_explicit_discovery_in_existing_peer_hub_attaches_original_registration(setup, monkeypatch):
    peer = create_app(setup.settings)
    admin = peer.test_client()
    assert post(admin, "login", {"email": "admin@example.test", "password": PASSWORD}).status_code == 200
    register(setup)
    calls = install_peer(monkeypatch)
    result = request(admin, "post", "/discover?connector_id=weather", json={"expected_epoch": 1})
    assert result.status_code == 200
    assert result.get_json()["connector"]["summary"]["runtime_ready"] is True
    assert calls == ["server/discover", "tools/list"]


@pytest.mark.parametrize("state", ["revoked", "restore_disabled"])
def test_disabled_registration_discovery_reports_original_state_after_restart(setup, state):
    register(setup)
    record = setup.catalog.store.get_many(("weather",))[0]
    if state == "revoked":
        setup.catalog.store.revoke("weather", expected_digest=record.config_digest, now=record.updated_at + 1)
    else:
        with sqlite3.connect(setup.catalog.store.db_path) as connection:
            connection.execute("UPDATE mcp_catalog_connectors SET restore_disabled=1 WHERE connector_id='weather'")
    before = setup.catalog.store.get_many(("weather",))[0]
    restarted = create_app(setup.settings)
    admin = restarted.test_client()
    assert post(admin, "login", {"email": "admin@example.test", "password": PASSWORD}).status_code == 200
    result = request(admin, "post", "/discover?connector_id=weather", json={"expected_epoch": before.epoch})
    assert result.status_code == 409
    assert setup.catalog.store.get_many(("weather",))[0] == before


@pytest.mark.parametrize("body", [{}, {"expected_epoch": True}, {"expected_epoch": 1.0},
    {"expected_epoch": 0}, {"expected_epoch": 9007199254740992}, {"expected_epoch": 1, "extra": True}])
def test_discovery_requires_exact_integer_epoch_without_io(setup, body):
    register(setup)
    before = setup.catalog.store.get_many(("weather",))[0]
    result = request(setup.admin, "post", "/discover?connector_id=weather", json=body)
    assert result.status_code == 400
    assert setup.catalog.store.get_many(("weather",))[0] == before


def test_failed_discovery_preserves_record_and_readback_performs_no_retry(setup, monkeypatch):
    register(setup)
    before = setup.catalog.store.get_many(("weather",))[0]
    calls = []
    def fail(self, endpoint, **kwargs):
        calls.append(json.loads(kwargs["body"])["method"])
        raise MCPCatalogError("timeout")
    monkeypatch.setattr(UrllibMCPTransport, "request", fail)
    monkeypatch.setattr(EnvironmentSecretBroker, "resolve", lambda self, ref: "mcp-registration-sentinel")
    result = request(setup.admin, "post", "/discover?connector_id=weather", json={"expected_epoch": 1})
    assert result.status_code == 503
    assert setup.catalog.store.get_many(("weather",))[0] == before
    assert request(setup.admin, path="/record?connector_id=weather").get_json()["connector"]["summary"]["state"] == "registered"
    assert calls == ["server/discover"]


def test_default_runtime_attachment_is_bounded_to_original_catalog_capacity(setup):
    from hub.platform.connectors.snapshots import encode_config
    for index in range(128):
        setup.catalog.store.register(encode_config(config(connector_id=f"c{index:03}", secret_ref="env://MCP_TEST_TOKEN")), now=100)
    restarted = create_app(setup.settings)
    catalog = restarted.extensions["fleet"]["services"]["mcp_catalog"]
    assert len(catalog.attached_ids()) == 128
    result = request(setup.admin, "post", "/register", json=payload())
    assert result.status_code == 409
    assert setup.catalog.store.get_many(("weather",)) == ()


def test_original_https_transport_private_alias_and_secret_reference_are_preserved(setup, monkeypatch):
    from test_http_transport import tls_server
    from test_provider_transport_boundaries import QuietHandler
    calls = []
    class Peer(QuietHandler):
        def do_POST(self):
            message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(message["method"])
            assert self.headers["Authorization"] == "Bearer https-registration-sentinel"
            if message["method"] == "tools/call":
                document = {"result": {"structuredContent": {"temperature": 24}, "content": []}}
            else:
                document = json.loads(_messages()[0 if message["method"] == "server/discover" else 1].body)
            raw = json.dumps({**document, "jsonrpc": "2.0", "id": message["id"]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    monkeypatch.setenv("MCP_TEST_TOKEN", "https-registration-sentinel")
    monkeypatch.setattr(UrllibMCPTransport, "request", REAL_TRANSPORT_REQUEST)
    monkeypatch.setattr(EnvironmentSecretBroker, "resolve", REAL_ENVIRONMENT_RESOLVE)
    with tls_server(setup.root, monkeypatch, Peer) as server:
        endpoint = f"https://localhost:{server.server_port}/mcp"
        registration = setup.root / "trusted-mcp.json"
        registration.write_text(json.dumps({"schema_version": 1, "connectors": [payload(endpoint=endpoint)["connector"]],
            "private_aliases": {endpoint: "127.0.0.1"}}), encoding="utf-8")
        settings = FleetConfig.from_root(setup.root, frontend_dir=ROOT / "frontend", platform_enabled=True,
            platform_mcp_connectors_enabled=True, platform_mcp_catalog_registration_enabled=True,
            platform_mcp_registration_file=registration, accounts={"enabled": True, "origin": ORIGIN})
        app = create_app(settings)
        admin = app.test_client()
        assert post(admin, "login", {"email": "admin@example.test", "password": PASSWORD}).status_code == 200
        created = request(admin, "post", "/register", json=payload(endpoint=endpoint))
        assert created.status_code == 200 and calls == []
        assert request(admin, "post", "/discover?connector_id=weather", json={"expected_epoch": 1}).status_code == 200
        catalog = app.extensions["fleet"]["services"]["mcp_catalog"]
        pins = catalog.run_snapshot(owner_id="owner", workspace_id="workspace")
        assert calls == ["server/discover", "tools/list"]
        restarted = create_app(settings)
        after = restarted.extensions["fleet"]["services"]["mcp_catalog"]
        assert after.run_snapshot(owner_id="owner", workspace_id="workspace") == pins
        assert calls == ["server/discover", "tools/list"]
        result = after.call("mcp.weather.weather", {"city": "Singapore"}, owner_id="owner", workspace_id="workspace")
        assert result.state == "succeeded" and result.structured_content == {"temperature": 24}
        assert calls == ["server/discover", "tools/list", "tools/call"]


def test_browser_registration_command_and_public_policy_correlation(setup):
    detail = register(setup)
    program = """
import assert from 'node:assert/strict';
import {parseMCPRegistrationCommand, registerMCPConnector} from './frontend/assets/api/integrations.js';
globalThis.window={FleetConfig:{apiBaseUrl:'/api'}};
const input=JSON.parse(process.env.MCP_REGISTRATION_COMMAND);
const original={ok:true,connector:JSON.parse(process.env.MCP_REGISTRATION_RESULT)};
assert.deepEqual(parseMCPRegistrationCommand(input),input);
for(const change of [v=>{v.connector.owner_id='x'.repeat(129)},v=>{v.connector.secret_ref='raw-token'},
 v=>{v.connector.endpoint='http://other.example/mcp'},v=>{v.connector.selected_tools=['weather','weather']},
 v=>{v.connector.timeout_s=true},v=>{v.connector.max_tools=101},v=>{v.connector.extra=true}]){
 const broken=structuredClone(input);change(broken);assert.throws(()=>parseMCPRegistrationCommand(broken),{name:'ContractError'});
}
let result=original,posted=[];
globalThis.fetch=async(url,options)=>{posted.push(JSON.parse(options.body));return new Response(JSON.stringify(result))};
assert.deepEqual(await registerMCPConnector(input),original);assert.deepEqual(posted[0],input);
for(const change of [v=>{v.connector.summary.owner_id='foreign'},v=>{v.connector.summary.connector_id='other'},
 v=>{v.connector.summary.workspace_id='other'},v=>{v.connector.summary.selected_tools=['other']},
 v=>{v.connector.summary.endpoint_origin='https://other.example'},v=>{v.connector.summary.timeout_s=1},
 v=>{v.connector.summary.result_limit_bytes=1}]){
 result=structuredClone(original);change(result);await assert.rejects(registerMCPConnector(input),{name:'ContractError'});
}
"""
    result = subprocess.run(["node", "--input-type=module", "-e", program], cwd=ROOT,
        env={**os.environ, "MCP_REGISTRATION_COMMAND": json.dumps(payload()),
             "MCP_REGISTRATION_RESULT": json.dumps(detail)}, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("encrypted", [False, True])
def test_real_backup_restore_keeps_registered_connection_disabled(setup, monkeypatch, encrypted):
    from hub.application.mcp_registration import MCPRegistrationCommand
    from hub.infrastructure.platform_backup import PlatformBackupService
    register(setup)
    calls = install_peer(monkeypatch)
    assert request(setup.admin, "post", "/discover?connector_id=weather", json={"expected_epoch": 1}).status_code == 200
    source = setup.settings.platform_artifact_root
    source.mkdir(parents=True, exist_ok=True)
    service = PlatformBackupService()
    backup, restored = setup.root / "mcp-backup", setup.root / "mcp-restored"
    if encrypted:
        service.create_encrypted_backup(setup.catalog.store.db_path, source, backup, b"\x01" * 32, "mcp-key")
        service.restore_encrypted_backup(backup, restored, b"\x01" * 32, expected_key_id="mcp-key")
    else:
        service.create_backup(setup.catalog.store.db_path, source, backup)
        service.restore_backup(backup, restored)
    reopened = create_app(FleetConfig.from_root(setup.root / "mcp-reopened", platform_enabled=True,
        platform_db=restored / "platform.db", platform_artifact_root=restored / "artifacts",
        platform_mcp_connectors_enabled=True, platform_mcp_catalog_registration_enabled=True))
    catalog = reopened.extensions["fleet"]["services"]["mcp_catalog"]
    before = catalog.store.get_many(("weather",))[0]
    assert before.restore_disabled is True and before.active_snapshot is not None
    assert catalog.attached_ids() == frozenset()
    assert catalog.run_snapshot(owner_id="owner", workspace_id="workspace") == {}
    admin = reopened.extensions["fleet"]["services"]["mcp_catalog_admin"]
    result = admin.register(MCPRegistrationCommand.parse(payload()))
    assert result.summary.state == "restore_disabled" and result.summary.runtime_ready is False
    assert catalog.store.get_many(("weather",))[0] == before
    assert calls == ["server/discover", "tools/list"]


@pytest.mark.parametrize("path", ["", "/record?connector_id=weather"])
def test_corrupt_durable_registration_is_store_unavailable_not_bad_input(setup, path):
    register(setup)
    with sqlite3.connect(setup.catalog.store.db_path) as connection:
        connection.execute("UPDATE mcp_catalog_connectors SET config_digest=? WHERE connector_id='weather'", ("0" * 64,))
    result = request(setup.admin, path=path)
    assert result.status_code == 503
    assert result.get_json()["error"] == "connector_snapshot_invalid"
