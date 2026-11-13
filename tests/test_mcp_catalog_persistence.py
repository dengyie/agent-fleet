"""Durable MCP authority across restarts, processes and offline restore."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from hub.application.connector_catalog import ConnectorCatalog
from test_mcp_connector import Broker, FakeTransport, config, discover_payload, response
from hub.platform.connectors.mcp import MCPConnector, MCPConnectorError

TOOL = "mcp.weather.weather"


def _messages(change: str | None = None):
    values = discover_payload()
    server, listing = (json.loads(item.body) for item in values)
    if change == "description":
        listing["result"]["tools"][0]["description"] = "new instructions"
    elif change == "server":
        server["result"]["_meta"]["io.modelcontextprotocol/serverInfo"]["version"] = "2"
    elif change == "capabilities":
        server["result"]["capabilities"]["resources"] = {}
    elif change == "annotations":
        listing["result"]["tools"][0]["annotations"]["readOnlyHint"] = True
    elif change == "schema":
        listing["result"]["tools"][0]["inputSchema"]["properties"]["country"] = {"type": "string"}
    return [response(server), response(listing)]


def _connector(messages=None, **registration):
    return MCPConnector(config(**registration), transport=FakeTransport(messages or []), secret_broker=Broker())


@pytest.mark.parametrize("change", ["description", "server", "capabilities", "annotations"])
def test_full_discovery_change_requires_pending_revision(change: str) -> None:
    catalog = ConnectorCatalog(enabled=True)
    catalog.register(_connector(_messages() + _messages(change)))
    catalog.discover("weather")
    original = catalog.tool_definitions(owner_id="owner", workspace_id="workspace")
    with pytest.raises(MCPConnectorError, match="connector_revision_pending"):
        catalog.refresh("weather")
    assert catalog.tool_definitions(owner_id="owner", workspace_id="workspace") == original
    assert catalog.admit_revision("weather", 2).revision == 2


def test_returned_discovery_cannot_mutate_catalog_authority() -> None:
    catalog = ConnectorCatalog(enabled=True)
    catalog.register(_connector(_messages()))
    discovery = catalog.discover("weather")
    original = catalog.tool_definitions(owner_id="owner", workspace_id="workspace")
    discovery.tools[0].input_schema.clear()
    discovery.tools[0].server_annotations.clear()
    discovery.capabilities.clear()
    assert catalog.tool_definitions(owner_id="owner", workspace_id="workspace") == original


def _store(path: Path):
    from hub.infrastructure.mcp_catalog_repository import MCPCatalogRepository
    from hub.infrastructure.platform_db import PlatformRepository

    PlatformRepository(path).init()
    store = MCPCatalogRepository(path)
    store.init()
    return store


def test_restart_restores_admitted_and_pending_without_discovery(tmp_path: Path) -> None:
    store = _store(tmp_path / "platform.db")
    first = ConnectorCatalog(enabled=True, store=store)
    first.register(_connector(_messages() + _messages("schema")))
    first.discover("weather")
    pins = first.run_snapshot(owner_id="owner", workspace_id="workspace")
    with pytest.raises(MCPConnectorError, match="connector_revision_pending"):
        first.refresh("weather")
    adapter = _connector()
    restarted = ConnectorCatalog(enabled=True, store=_store(tmp_path / "platform.db"))
    restarted.register(adapter)
    assert adapter.transport.calls == []
    assert restarted.run_snapshot(owner_id="owner", workspace_id="workspace") == pins
    assert restarted.admit_revision("weather", 2).revision == 2
    assert first.run_snapshot(owner_id="owner", workspace_id="workspace") != pins


def test_revoke_is_shared_and_cannot_be_reactivated_by_registration(tmp_path: Path) -> None:
    store = _store(tmp_path / "platform.db")
    first = ConnectorCatalog(enabled=True, store=store)
    first.register(_connector(_messages()))
    first.discover("weather")
    second = ConnectorCatalog(enabled=True, store=_store(tmp_path / "platform.db"))
    second.register(_connector())
    record = store.get_many(("weather",))[0]
    second.revoke("weather", expected_digest=record.config_digest)
    assert first.run_snapshot(owner_id="owner", workspace_id="workspace") == {}
    third = ConnectorCatalog(enabled=True, store=_store(tmp_path / "platform.db"))
    third.register(_connector(_messages()))
    assert third.run_snapshot(owner_id="owner", workspace_id="workspace") == {}
    with pytest.raises(MCPConnectorError, match="connector_revoked"):
        third.refresh("weather")


def test_unattached_persistent_records_grant_no_tools(tmp_path: Path) -> None:
    store = _store(tmp_path / "platform.db")
    first = ConnectorCatalog(enabled=True, store=store)
    first.register(_connector(_messages()))
    first.discover("weather")
    unattached = ConnectorCatalog(enabled=True, store=store)
    assert unattached.run_snapshot(owner_id="owner", workspace_id="workspace") == {}
    from hub.domain.mcp_tool_pins import parse_mcp_tool_pins
    pins = parse_mcp_tool_pins(first.run_snapshot(owner_id="owner", workspace_id="workspace"))
    with pytest.raises(MCPConnectorError, match="mcp_disabled"):
        unattached.tool_definitions(owner_id="owner", workspace_id="workspace", expected_pins=pins)


@pytest.mark.parametrize("changes", [{"endpoint": "https://other.example.test/mcp"},
    {"owner_id": "another"}, {"workspace_id": "elsewhere"}, {"secret_ref": "env://OTHER"},
    {"selected_tools": ("different",)}, {"timeout_s": 5}, {"result_limit_bytes": 2048}])
def test_registration_cannot_replace_persisted_authority(tmp_path: Path, changes: dict) -> None:
    store = _store(tmp_path / "platform.db")
    first = ConnectorCatalog(enabled=True, store=store)
    first.register(_connector(_messages()))
    first.discover("weather")
    pins = first.run_snapshot(owner_id="owner", workspace_id="workspace")
    with pytest.raises(MCPConnectorError, match="connector_registration_conflict"):
        ConnectorCatalog(enabled=True, store=store).register(_connector(**changes))
    assert first.run_snapshot(owner_id="owner", workspace_id="workspace") == pins


def test_pending_revision_is_monotonic_across_restart_and_reversion(tmp_path: Path) -> None:
    store = _store(tmp_path / "platform.db")
    first = ConnectorCatalog(enabled=True, store=store)
    first.register(_connector(_messages() + _messages("schema")))
    first.discover("weather")
    with pytest.raises(MCPConnectorError, match="connector_revision_pending"):
        first.refresh("weather")
    second = ConnectorCatalog(enabled=True, store=store)
    second.register(_connector(_messages("schema") + _messages() + _messages("schema")))
    with pytest.raises(MCPConnectorError, match="connector_revision_pending"):
        second.refresh("weather")
    assert store.get_many(("weather",))[0].last_revision == 2
    with pytest.raises(MCPConnectorError, match="connector_revision_pending"):
        second.refresh("weather")
    with pytest.raises(MCPConnectorError, match="connector_revision_changed"):
        first.admit_revision("weather", 2)
    assert first.admit_revision("weather", 3).revision == 3
    with pytest.raises(MCPConnectorError, match="connector_revision_pending"):
        second.refresh("weather")
    assert second.admit_revision("weather", 4).revision == 4


@pytest.mark.parametrize("mutation", ["approve", "revoke", "refresh"])
def test_inflight_refresh_cannot_publish_over_another_process_change(tmp_path: Path, mutation: str) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    store = _store(tmp_path / "platform.db")
    first = ConnectorCatalog(enabled=True, store=store)
    adapter = _connector(_messages() + _messages("schema") + _messages("description"))
    first.register(adapter)
    first.discover("weather")
    with pytest.raises(MCPConnectorError, match="connector_revision_pending"):
        first.refresh("weather")
    second = ConnectorCatalog(enabled=True, store=_store(tmp_path / "platform.db"))
    second.register(_connector(_messages("annotations")))
    entered, resume = Event(), Event()
    request = adapter.transport.request
    def delayed(*args, **kwargs):
        entered.set()
        assert resume.wait(5)
        return request(*args, **kwargs)
    adapter.transport.request = delayed
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(first.refresh, "weather")
        assert entered.wait(5)
        try:
            if mutation == "approve":
                second.admit_revision("weather", 2)
            elif mutation == "revoke":
                second.revoke("weather", expected_digest=store.get_many(("weather",))[0].config_digest)
            else:
                with pytest.raises(MCPConnectorError, match="connector_revision_pending"):
                    second.refresh("weather")
        finally:
            resume.set()
        with pytest.raises(MCPConnectorError, match="connector_revoked" if mutation == "revoke" else "connector_revision_changed"):
            future.result(timeout=5)
    record = store.get_many(("weather",))[0]
    assert record.last_revision == (3 if mutation == "refresh" else 2)


def _process_catalog_change(path: str, action: str, output) -> None:
    from hub.infrastructure.mcp_catalog_repository import MCPCatalogRepository

    try:
        store = MCPCatalogRepository(Path(path))
        record = store.get_many(("weather",))[0]
        if action == "approve":
            record = store.admit("weather", expected_revision=2, now=record.updated_at + 1)
        else:
            record = store.revoke("weather", expected_digest=record.config_digest, now=record.updated_at + 1)
        output.put(("ok", record.epoch))
    except BaseException as exc:
        output.put(("error", repr(exc)))


def test_real_process_approval_and_revoke_invalidate_live_catalog(tmp_path: Path) -> None:
    import multiprocessing
    from hub.domain.mcp_tool_pins import parse_mcp_tool_pins

    context = multiprocessing.get_context("spawn")
    store = _store(tmp_path / "platform.db")
    catalog = ConnectorCatalog(enabled=True, store=store)
    adapter = _connector(_messages() + _messages("schema"))
    catalog.register(adapter)
    catalog.discover("weather")
    pins = parse_mcp_tool_pins(catalog.run_snapshot(owner_id="owner", workspace_id="workspace"))
    with pytest.raises(MCPConnectorError, match="connector_revision_pending"):
        catalog.refresh("weather")
    for action in ("approve", "revoke"):
        output = context.Queue()
        process = context.Process(target=_process_catalog_change, args=(str(store.db_path), action, output))
        process.start()
        process.join(10)
        assert process.exitcode == 0
        assert output.get(timeout=2)[0] == "ok"
        output.close()
        output.join_thread()
        with pytest.raises(MCPConnectorError, match="mcp_tool_pin_changed"):
            catalog.tool_definitions(owner_id="owner", workspace_id="workspace", expected_pins=pins)
    assert catalog.run_snapshot(owner_id="owner", workspace_id="workspace") == {}
    assert all(call[2]["method"] != "tools/call" for call in adapter.transport.calls)


def test_discovery_order_does_not_create_new_revision(tmp_path: Path) -> None:
    tools = [{"name": name, "inputSchema": {"type": "object"}} for name in ("weather", "other")]
    messages = discover_payload(tools=tools) + discover_payload(tools=list(reversed(tools)))
    catalog = ConnectorCatalog(enabled=True, store=_store(tmp_path / "platform.db"))
    catalog.register(_connector(messages, selected_tools=("other", "weather")))
    assert catalog.discover("weather").revision == 1
    assert catalog.refresh("weather").revision == 1


def test_bootstrap_default_catalog_is_passive_and_independent_of_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sqlite3
    import threading
    from hub.bootstrap import create_app
    from hub.config import FleetConfig

    started: list[str] = []
    def guarded_start(thread, *args, **kwargs):
        started.append(thread.name)
        raise AssertionError("catalog construction started a thread")
    monkeypatch.setattr(threading.Thread, "start", guarded_start)
    adapter = _connector(_messages())
    settings = FleetConfig.from_root(tmp_path, platform_enabled=True, platform_worker_enabled=True,
        platform_mcp_connectors_enabled=True, dev_operator="owner")
    app = create_app(settings, repositories={"mcp_connectors": (adapter,)})
    assert adapter.transport.calls == []
    services = app.extensions["fleet"]["services"]
    catalog = services["mcp_catalog"]
    assert catalog.store is not None
    assert services["conversations"].mcp_catalog is catalog
    assert services["platform_worker"].mcp_catalog is catalog
    catalog.discover("weather")
    pins = catalog.run_snapshot(owner_id="owner", workspace_id="workspace")
    restarted = create_app(settings, repositories={"mcp_connectors": (_connector(),)})
    assert restarted.extensions["fleet"]["services"]["mcp_catalog"].run_snapshot(owner_id="owner", workspace_id="workspace") == pins
    assert not started
    with sqlite3.connect(settings.platform_db) as connection:
        assert connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() == ("4",)
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name GLOB 'workflow_*'").fetchall()


def test_gate_off_does_not_register_resolve_or_create_catalog(tmp_path: Path) -> None:
    import sqlite3
    from hub.bootstrap import create_app
    from hub.config import FleetConfig

    adapter = _connector(_messages())
    settings = FleetConfig.from_root(tmp_path, platform_enabled=True, dev_operator="owner")
    app = create_app(settings, repositories={"mcp_connectors": (adapter,)})
    assert adapter.transport.calls == []
    assert "mcp_catalog" not in app.extensions["fleet"]["services"]
    with sqlite3.connect(settings.platform_db) as connection:
        assert connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() == ("3",)
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name GLOB 'mcp_catalog_*'").fetchall()


@pytest.mark.parametrize("encrypted", [False, True])
@pytest.mark.parametrize("workflow", [False, True])
def test_backup_restore_disables_catalog_even_without_workflow(tmp_path: Path, encrypted: bool, workflow: bool) -> None:
    from hub.infrastructure.platform_backup import PlatformBackupService

    store = _store(tmp_path / "platform.db")
    catalog = ConnectorCatalog(enabled=True, store=store)
    catalog.register(_connector(_messages()))
    catalog.discover("weather")
    original = catalog.run_snapshot(owner_id="owner", workspace_id="workspace")
    if workflow:
        from hub.infrastructure.workflow_repository import WorkflowRepository

        WorkflowRepository(store.db_path).init()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    service = PlatformBackupService()
    if encrypted:
        service.create_encrypted_backup(store.db_path, artifacts, backup, b"m" * 32, "mcp-key")
        service.restore_encrypted_backup(backup, restored, b"m" * 32)
    else:
        service.create_backup(store.db_path, artifacts, backup)
        service.restore_backup(backup, restored)
    recovered = ConnectorCatalog(enabled=True, store=_store(restored / "platform.db"))
    adapter = _connector(_messages())
    recovered.register(adapter)
    assert recovered.run_snapshot(owner_id="owner", workspace_id="workspace") == {}
    with pytest.raises(MCPConnectorError, match="connector_revoked"):
        recovered.refresh("weather")
    assert adapter.transport.calls == []
    assert catalog.run_snapshot(owner_id="owner", workspace_id="workspace") == original
    if workflow:
        import sqlite3
        from hub.infrastructure.workflow_schema import inspect_schema

        with sqlite3.connect(restored / "platform.db") as connection:
            assert inspect_schema(connection)["admission_mode"] == "frozen"


@pytest.mark.parametrize("sql,code", [
    ("UPDATE mcp_catalog_meta SET schema_version=2", "unsupported_mcp_catalog_schema"),
    ("UPDATE mcp_catalog_meta SET minimum_reader_version=2", "unsupported_mcp_catalog_schema"),
    ("UPDATE meta SET value='3' WHERE key='schema_version'", "unsupported_mcp_platform_reader"),
    ("ALTER TABLE mcp_catalog_connectors RENAME TO old_catalog", "incomplete_mcp_catalog_schema"),
    ("CREATE TABLE mcp_catalog_future(value TEXT)", "unsupported_mcp_catalog_schema"),
])
def test_gate_off_and_backup_reject_unknown_or_incomplete_catalog(tmp_path: Path, sql: str, code: str) -> None:
    import sqlite3
    from hub.infrastructure.platform_backup import BackupError, PlatformBackupService
    from hub.infrastructure.platform_db import PlatformRepository, PlatformRepositoryError

    store = _store(tmp_path / "platform.db")
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(sql)
    with pytest.raises(PlatformRepositoryError, match=code):
        PlatformRepository(store.db_path).init()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    with pytest.raises(BackupError, match=code):
        PlatformBackupService().create_backup(store.db_path, artifacts, tmp_path / "backup")


@pytest.mark.parametrize("field", ["config_snapshot", "active_snapshot", "pending_snapshot", "config_digest", "epoch", "last_revision"])
def test_runtime_and_backup_reject_corrupt_catalog_record(tmp_path: Path, field: str) -> None:
    import sqlite3
    from hub.infrastructure.platform_backup import BackupError, PlatformBackupService

    store = _store(tmp_path / "platform.db")
    catalog = ConnectorCatalog(enabled=True, store=store)
    catalog.register(_connector(_messages() + _messages("schema")))
    catalog.discover("weather")
    with pytest.raises(MCPConnectorError, match="connector_revision_pending"):
        catalog.refresh("weather")
    bad = {"config_snapshot": b"{}", "active_snapshot": b"{}", "pending_snapshot": b"{}",
        "config_digest": "a" * 64, "epoch": 1.5, "last_revision": 0}[field]
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(f"UPDATE mcp_catalog_connectors SET {field}=?", (bad,))
    with pytest.raises(MCPConnectorError, match="connector_snapshot_invalid"):
        catalog.run_snapshot(owner_id="owner", workspace_id="workspace")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    with pytest.raises(BackupError, match="connector_snapshot_invalid"):
        PlatformBackupService().create_backup(store.db_path, artifacts, tmp_path / "backup")


def test_registration_history_bound_and_global_tool_name_uniqueness(tmp_path: Path) -> None:
    from hub.platform.connectors.snapshots import encode_config

    store = _store(tmp_path / "platform.db")
    store.register(encode_config(config(connector_id="a.b", selected_tools=("c",))), now=100)
    with pytest.raises(MCPConnectorError, match="connector_tool_name_conflict"):
        store.register(encode_config(config(connector_id="a", selected_tools=("b.c",))), now=100)
    for index in range(127):
        store.register(encode_config(config(connector_id=f"c{index}")), now=100)
    with pytest.raises(MCPConnectorError, match="too_many_connectors"):
        store.register(encode_config(config(connector_id="overflow")), now=100)


def test_startup_history_validation_does_not_hold_sqlite_writer(tmp_path: Path, monkeypatch) -> None:
    import sqlite3
    from hub.infrastructure.mcp_catalog_repository import MCPCatalogRepository

    store = _store(tmp_path / "platform.db")
    validate = MCPCatalogRepository.validate_snapshot
    calls = []
    def observe(connection):
        calls.append(True)
        with sqlite3.connect(store.db_path, timeout=0.1) as writer:
            writer.execute("UPDATE meta SET value=value WHERE key='schema_version'")
        validate(connection)
    monkeypatch.setattr(MCPCatalogRepository, "validate_snapshot", staticmethod(observe))
    store.init()
    assert calls == [True]


@pytest.mark.parametrize("revocation", ["approve", "revoke", "unattached"])
def test_durable_run_blocks_stale_authority_before_model_io(tmp_path: Path, revocation: str) -> None:
    from hub.bootstrap import create_app
    from hub.config import FleetConfig
    from test_mcp_run_pinning import execute
    from test_platform_run_worker import OWNER, _catalog, _queued

    adapter = _connector(_messages() + _messages("schema"), owner_id=OWNER, workspace_id="home")
    settings = FleetConfig.from_root(tmp_path, platform_enabled=True, platform_worker_enabled=True,
        platform_mcp_connectors_enabled=True, dev_operator=OWNER)
    app = create_app(settings, repositories={"mcp_connectors": (adapter,)})
    catalog = app.extensions["fleet"]["services"]["mcp_catalog"]
    catalog.discover("weather")
    _catalog(app, tmp_path)
    _queued(app)
    if revocation == "unattached":
        app = create_app(settings)
    else:
        second = create_app(settings, repositories={"mcp_connectors": (_connector(owner_id=OWNER, workspace_id="home"),)})
        other = second.extensions["fleet"]["services"]["mcp_catalog"]
        if revocation == "approve":
            with pytest.raises(MCPConnectorError, match="connector_revision_pending"):
                catalog.refresh("weather")
            other.admit_revision("weather", 2)
        else:
            record = other.store.get_many(("weather",))[0]
            other.revoke("weather", expected_digest=record.config_digest)
    result, provider = execute(app)
    assert result["state"] == "failed"
    assert provider.declarations == []
    assert all(call[2]["method"] != "tools/call" for call in adapter.transport.calls)


def test_durable_native_run_uses_original_https_snapshot_after_restart(tmp_path: Path, monkeypatch) -> None:
    from hub.bootstrap import create_app
    from hub.config import FleetConfig
    from test_http_transport import tls_server
    from test_mcp_run_pinning import execute
    from test_platform_run_worker import OWNER, _catalog, _queued
    from test_provider_transport_boundaries import QuietHandler
    from hub.platform.connectors.mcp import UrllibMCPTransport

    calls = []
    class Peer(QuietHandler):
        def do_POST(self):
            message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            method = message["method"]
            calls.append(method)
            assert self.headers["Authorization"] == "Bearer secret-token"
            if method == "tools/call":
                assert self.headers["Mcp-Name"] == "weather"
                assert message["params"]["arguments"] == {"city": "Singapore"}
                result = {"structuredContent": {"temperature": 24}, "content": []}
            else:
                result = json.loads(_messages()[0 if method == "server/discover" else 1].body)["result"]
            raw = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    with tls_server(tmp_path, monkeypatch, Peer) as server:
        endpoint = f"https://localhost:{server.server_port}/mcp"
        registration = config(endpoint=endpoint, owner_id=OWNER, workspace_id="home")
        def adapter():
            return MCPConnector(registration, transport=UrllibMCPTransport(private_aliases={endpoint: "127.0.0.1"}), secret_broker=Broker())
        settings = FleetConfig.from_root(tmp_path, platform_enabled=True, platform_worker_enabled=True,
            platform_mcp_connectors_enabled=True, dev_operator=OWNER)
        app = create_app(settings, repositories={"mcp_connectors": (adapter(),)})
        catalog = app.extensions["fleet"]["services"]["mcp_catalog"]
        catalog.discover("weather")
        _catalog(app, tmp_path)
        run = _queued(app)
        restarted = create_app(settings, repositories={"mcp_connectors": (adapter(),)})
        assert calls == ["server/discover", "tools/list"]
        result, provider = execute(restarted)
        assert result["state"] == "succeeded"
        assert TOOL in provider.declarations[0]
        assert calls == ["server/discover", "tools/list", "tools/call"]
        repository = restarted.extensions["fleet"]["platform_repository"]
        assert "secret-token" not in repr(repository.get_run(OWNER, run["run_id"]))


def test_call_rechecks_persistent_revocation_after_model_declaration(tmp_path: Path) -> None:
    from hub.domain.mcp_tool_pins import parse_mcp_tool_pins

    store = _store(tmp_path / "platform.db")
    catalog = ConnectorCatalog(enabled=True, store=store)
    adapter = _connector(_messages())
    catalog.register(adapter)
    catalog.discover("weather")
    pins = parse_mcp_tool_pins(catalog.run_snapshot(owner_id="owner", workspace_id="workspace"))
    assert catalog.tool_definitions(owner_id="owner", workspace_id="workspace", expected_pins=pins)
    record = store.get_many(("weather",))[0]
    store.revoke("weather", expected_digest=record.config_digest, now=record.updated_at + 1)
    with pytest.raises(MCPConnectorError, match="tool_not_allowed"):
        catalog.call(TOOL, {"city": "Singapore"}, owner_id="owner", workspace_id="workspace", expected_pin=pins[0])
    assert len(adapter.transport.calls) == 2


def test_catalog_history_checks_zero_and_negative_rowids(tmp_path: Path) -> None:
    import sqlite3
    from hub.infrastructure.mcp_catalog_repository import MCPCatalogRepository
    from hub.platform.connectors.snapshots import encode_config

    store = _store(tmp_path / "platform.db")
    for index in range(128):
        store.register(encode_config(config(connector_id=f"history{index:03d}")), now=100)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("UPDATE mcp_catalog_connectors SET rowid=-1 WHERE connector_id='history000'")
        connection.execute("UPDATE mcp_catalog_connectors SET rowid=0 WHERE connector_id='history001'")
        connection.execute("UPDATE mcp_catalog_connectors SET config_digest=? WHERE rowid=-1", ("b" * 64,))
        with pytest.raises(MCPConnectorError, match="connector_snapshot_invalid"):
            MCPCatalogRepository.validate_snapshot(connection)


def test_catalog_init_ddl_rolls_back_atomically(tmp_path: Path, monkeypatch) -> None:
    import sqlite3
    import hub.infrastructure.mcp_catalog_repository as module
    from hub.infrastructure.platform_db import PlatformRepository

    path = tmp_path / "platform.db"
    PlatformRepository(path).init()
    monkeypatch.setattr(module, "STATEMENTS", (*module.STATEMENTS, "INVALID SQL"))
    with pytest.raises(MCPConnectorError, match="mcp_catalog_store_unavailable") as caught:
        module.MCPCatalogRepository(path).init()
    assert isinstance(caught.value.__cause__, sqlite3.Error)
    with sqlite3.connect(path) as connection:
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name GLOB 'mcp_catalog_*'").fetchall()
        assert connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() == ("3",)


def test_discovery_mutation_cannot_reach_durable_schema_or_call(tmp_path: Path) -> None:
    store = _store(tmp_path / "platform.db")
    catalog = ConnectorCatalog(enabled=True, store=store)
    adapter = _connector(_messages() + [response({"jsonrpc": "2.0", "id": "call", "result": {
        "structuredContent": {"temperature": 24}, "content": []}})])
    catalog.register(adapter)
    discovery = catalog.discover("weather")
    discovery.tools[0].input_schema.clear()
    definitions = catalog.tool_definitions(owner_id="owner", workspace_id="workspace")
    definitions[0]["parameters"].clear()
    assert catalog.call(TOOL, {"city": "Singapore"}, owner_id="owner", workspace_id="workspace").state == "succeeded"


@pytest.mark.parametrize("change", ["config_extra", "config_noncanonical", "tool_digest", "tool_extra", "duplicate_tool", "revision_bool", "external_schema"])
def test_snapshot_codec_rejects_forged_private_authority(change: str) -> None:
    from hub.platform.connectors.snapshots import decode_config, decode_discovery, encode_config, encode_discovery
    from hub.platform.connectors.mcp import _canonical

    connector = _connector(_messages())
    discovery = connector.discover()
    if change.startswith("config"):
        value = json.loads(encode_config(config()))
        if change == "config_extra":
            value["admin"] = True
        raw = _canonical(value) if change == "config_extra" else json.dumps(value, indent=2).encode()
        with pytest.raises(MCPConnectorError):
            decode_config(raw)
        return
    value = json.loads(encode_discovery(discovery))
    tool = value["tools"][0]
    if change == "tool_digest":
        tool["schema_digest"] = "c" * 64
    elif change == "tool_extra":
        tool["risk"] = "restricted_exec"
    elif change == "duplicate_tool":
        value["tools"].append(tool)
    elif change == "revision_bool":
        value["revision"] = True
    else:
        import hashlib
        tool["input_schema"] = {"$ref": "https://evil.test/schema"}
        tool["schema_digest"] = hashlib.sha256(_canonical(tool["input_schema"])).hexdigest()
    with pytest.raises(MCPConnectorError):
        decode_discovery(_canonical(value), config())


def test_active_revision_and_high_water_mark_cannot_diverge(tmp_path: Path) -> None:
    import sqlite3

    store = _store(tmp_path / "platform.db")
    catalog = ConnectorCatalog(enabled=True, store=store)
    catalog.register(_connector(_messages()))
    catalog.discover("weather")
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("UPDATE mcp_catalog_connectors SET last_revision=50")
    with pytest.raises(MCPConnectorError, match="connector_snapshot_invalid"):
        catalog.run_snapshot(owner_id="owner", workspace_id="workspace")


def test_runtime_and_backup_reject_future_platform_reader(tmp_path: Path) -> None:
    import sqlite3
    from hub.infrastructure.platform_backup import BackupError, PlatformBackupService
    from hub.infrastructure.platform_db import PlatformRepository, PlatformRepositoryError

    store = _store(tmp_path / "platform.db")
    catalog = ConnectorCatalog(enabled=True, store=store)
    catalog.register(_connector(_messages()))
    catalog.discover("weather")
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    with pytest.raises(PlatformRepositoryError, match="unsupported_schema_version"):
        PlatformRepository(store.db_path).init()
    with pytest.raises(MCPConnectorError, match="unsupported_mcp_platform_reader"):
        catalog.run_snapshot(owner_id="owner", workspace_id="workspace")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    with pytest.raises(BackupError, match="unsupported_mcp_platform_reader"):
        PlatformBackupService().create_backup(store.db_path, artifacts, tmp_path / "backup")


def test_catalog_history_scan_reaches_overflow_page(tmp_path: Path) -> None:
    import sqlite3
    from hub.infrastructure.mcp_catalog_repository import MCPCatalogRepository
    from hub.platform.connectors.snapshots import encode_config

    store = _store(tmp_path / "platform.db")
    for index in range(128):
        store.register(encode_config(config(connector_id=f"c{index}")), now=100)
    with sqlite3.connect(store.db_path) as connection:
        raw = encode_config(config(connector_id="overflow"))
        import hashlib
        connection.execute("INSERT INTO mcp_catalog_connectors(connector_id,config_snapshot,config_digest,last_revision,epoch,created_at,updated_at) "
            "VALUES(?,?,?,0,1,100,100)", ("overflow", raw, hashlib.sha256(raw).hexdigest()))
        with pytest.raises(MCPConnectorError, match="too_many_connectors"):
            MCPCatalogRepository.validate_snapshot(connection)


def test_catalog_revision_exhaustion_keeps_last_approved_snapshot(tmp_path: Path) -> None:
    import hashlib
    import sqlite3
    from hub.domain.mcp_catalog import MAX_CATALOG_INTEGER
    from hub.platform.connectors.snapshots import decode_config, decode_discovery, encode_discovery

    store = _store(tmp_path / "platform.db")
    catalog = ConnectorCatalog(enabled=True, store=store)
    catalog.register(_connector(_messages() + _messages("schema")))
    catalog.discover("weather")
    record = store.get_many(("weather",))[0]
    original = decode_discovery(record.active_snapshot, decode_config(record.config_snapshot))
    raw = encode_discovery(replace(original, revision=MAX_CATALOG_INTEGER))
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("UPDATE mcp_catalog_connectors SET active_snapshot=?,active_digest=?,last_revision=?",
            (raw, hashlib.sha256(raw).hexdigest(), MAX_CATALOG_INTEGER))
    pins = catalog.run_snapshot(owner_id="owner", workspace_id="workspace")
    with pytest.raises(MCPConnectorError, match="connector_revision_exhausted"):
        catalog.refresh("weather")
    assert catalog.run_snapshot(owner_id="owner", workspace_id="workspace") == pins


def test_registration_revoke_repetition_and_failed_cas_do_not_change_record(tmp_path: Path) -> None:
    from hub.platform.connectors.snapshots import encode_config

    store = _store(tmp_path / "platform.db")
    first = store.register(encode_config(config()), now=100)
    assert store.register(encode_config(config()), now=200) == first
    for digest in ("b" * 64, None, True):
        with pytest.raises(MCPConnectorError, match="connector_registration_conflict"):
            store.revoke("weather", expected_digest=digest, now=101)
    assert store.get_many(("weather",)) == (first,)
    revoked = store.revoke("weather", expected_digest=first.config_digest, now=101)
    assert store.revoke("weather", expected_digest=first.config_digest, now=200) == revoked
    assert store.register(encode_config(config()), now=201) == revoked


def _register_race(path: str, index: int, ready, proceed, output) -> None:
    from hub.infrastructure.mcp_catalog_repository import MCPCatalogRepository
    from hub.platform.connectors.snapshots import encode_config

    ready.put(index)
    if not proceed.wait(5):
        output.put((index, "timeout"))
        return
    try:
        MCPCatalogRepository(Path(path)).register(encode_config(config(endpoint=f"https://peer{index}.example.test/mcp")), now=100)
        output.put((index, "registered"))
    except MCPConnectorError as exc:
        output.put((index, exc.code))


def test_two_real_processes_cannot_replace_one_registration(tmp_path: Path) -> None:
    import multiprocessing
    from hub.platform.connectors.snapshots import decode_config

    store = _store(tmp_path / "platform.db")
    context = multiprocessing.get_context("spawn")
    ready, output, proceed = context.Queue(), context.Queue(), context.Event()
    processes = [context.Process(target=_register_race, args=(str(store.db_path), index, ready, proceed, output)) for index in range(2)]
    for process in processes:
        process.start()
    assert {ready.get(timeout=5), ready.get(timeout=5)} == {0, 1}
    proceed.set()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    outcomes = [output.get(timeout=2), output.get(timeout=2)]
    assert sorted(value for _, value in outcomes) == ["connector_registration_conflict", "registered"]
    winner = next(index for index, value in outcomes if value == "registered")
    assert decode_config(store.get_many(("weather",))[0].config_snapshot).endpoint == f"https://peer{winner}.example.test/mcp"
    for queue in (ready, output):
        queue.close()
        queue.join_thread()


def test_large_but_allowed_schema_survives_persistence_without_64k_truncation(tmp_path: Path) -> None:
    from hub.platform.connectors.snapshots import decode_config, decode_discovery

    tools = [{"name": name, "inputSchema": {"type": "object", "description": "x" * 30000}} for name in ("weather", "other", "third")]
    store = _store(tmp_path / "platform.db")
    catalog = ConnectorCatalog(enabled=True, store=store)
    catalog.register(_connector(discover_payload(tools=tools), selected_tools=("weather", "other", "third")))
    catalog.discover("weather")
    record = store.get_many(("weather",))[0]
    assert len(record.active_snapshot) > 65536
    assert len(decode_discovery(record.active_snapshot, decode_config(record.config_snapshot)).tools) == 3
    assert len(catalog.run_snapshot(owner_id="owner", workspace_id="workspace")) == 3


def test_bootstrap_can_enable_workflow_after_catalog_reader_upgrade(tmp_path: Path) -> None:
    from hub.bootstrap import create_app
    from hub.config import FleetConfig

    settings = FleetConfig.from_root(tmp_path, platform_enabled=True, platform_mcp_connectors_enabled=True,
        platform_workflows_enabled=True, dev_operator="owner")
    app = create_app(settings)
    assert app.extensions["fleet"]["services"]["workflow_coordinator"] is not None
    assert app.extensions["fleet"]["services"]["mcp_catalog"].store is not None
    assert app.test_client().get("/api/platform/v1/workflows").status_code == 200


def test_reader_upgrade_cannot_hide_the_whole_catalog_schema(tmp_path: Path) -> None:
    import sqlite3
    from hub.infrastructure.platform_db import PlatformRepository, PlatformRepositoryError

    store = _store(tmp_path / "platform.db")
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("ALTER TABLE mcp_catalog_meta RENAME TO old_catalog_meta")
        connection.execute("ALTER TABLE mcp_catalog_connectors RENAME TO old_catalog_connectors")
    with pytest.raises(PlatformRepositoryError, match="incomplete_mcp_catalog_schema"):
        PlatformRepository(store.db_path).init()


def test_revocation_during_a_pinned_call_preserves_its_result_and_blocks_next_call(tmp_path: Path) -> None:
    store = _store(tmp_path / "platform.db")
    catalog = ConnectorCatalog(enabled=True, store=store)
    adapter = _connector(_messages() + [response({"jsonrpc": "2.0", "id": "call", "result": {
        "structuredContent": {"temperature": 24}, "content": []}})])
    catalog.register(adapter)
    catalog.discover("weather")
    record = store.get_many(("weather",))[0]
    request = adapter.transport.request
    def revoke_at_transport(*args, **kwargs):
        # This write would fail/deadlock if invocation held its read/write
        # transaction or used revocation to rewrite an already-started call.
        store.revoke("weather", expected_digest=record.config_digest, now=record.updated_at + 1)
        return request(*args, **kwargs)
    adapter.transport.request = revoke_at_transport
    result = catalog.call(TOOL, {"city": "Singapore"}, owner_id="owner", workspace_id="workspace")
    assert result.state == "succeeded"
    assert result.structured_content == {"temperature": 24}
    with pytest.raises(MCPConnectorError, match="tool_not_allowed"):
        catalog.call(TOOL, {"city": "Singapore"}, owner_id="owner", workspace_id="workspace")
    assert len(adapter.transport.calls) == 3
