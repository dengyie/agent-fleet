"""Persist MCP authority with each Run instead of borrowing a live catalog."""
from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from hub.application.connector_catalog import ConnectorCatalog
from hub.bootstrap import create_app
from hub.config import FleetConfig
from test_mcp_connector import Broker, FakeTransport, config, discover_payload, response
from test_platform_run_worker import OWNER, _catalog, _queued
from hub.platform.connectors.mcp import MCPConnector, MCPConnectorError
from hub.platform.providers.base import ModelResponse

TOOL = "mcp.weather.weather"


def catalog_with_tool(*, changed: bool = False,
                      registration: dict[str, Any] | None = None) -> tuple[ConnectorCatalog, FakeTransport]:
    messages = discover_payload()
    if changed:
        value = json.loads(messages[1].body)
        value["result"]["tools"][0]["inputSchema"]["properties"]["country"] = {"type": "string"}
        messages[1] = response(value)
    transport = FakeTransport(messages + [response({"jsonrpc": "2.0", "id": "call", "result": {
        "structuredContent": {"temperature": 24}, "content": []}})])
    catalog = ConnectorCatalog(enabled=True)
    catalog.register(MCPConnector(config(**{"owner_id": OWNER, "workspace_id": "home", **(registration or {})}),
        transport=transport, secret_broker=Broker()))
    catalog.discover("weather")
    return catalog, transport


def app_with_catalog(tmp_path: Path, catalog: ConnectorCatalog | None, *, enabled: bool = True) -> Any:
    return create_app(FleetConfig.from_root(tmp_path, ingest_token="x", dev_operator=OWNER,
        platform_enabled=True, platform_worker_enabled=True, platform_mcp_connectors_enabled=enabled),
        repositories={"mcp_catalog": catalog})


class Provider:
    def __init__(self) -> None:
        self.declarations: list[set[str]] = []

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *,
                 request_observer: Any = None) -> ModelResponse:
        self.declarations.append({tool["name"] for tool in tools})
        if len(self.declarations) == 1:
            return ModelResponse(kind="tool_call", tool=TOOL, arguments={"city": "Singapore"})
        return ModelResponse(kind="final", text="24 degrees")


def execute(app: Any) -> tuple[dict[str, Any], Provider]:
    provider = Provider()
    worker = app.extensions["fleet"]["services"]["platform_worker"]
    worker.provider_factory = lambda profile: provider
    return worker.run_once(OWNER), provider


def test_queued_run_cannot_adopt_an_admitted_new_mcp_revision(tmp_path: Path) -> None:
    catalog, transport = catalog_with_tool()
    app = app_with_catalog(tmp_path, catalog)
    _catalog(app, tmp_path)
    _queued(app)
    changed = json.loads(discover_payload()[1].body)
    changed["result"]["tools"][0]["inputSchema"]["properties"]["country"] = {"type": "string"}
    transport.responses[:0] = [discover_payload()[0], response(changed)]
    with pytest.raises(MCPConnectorError, match="connector_revision_pending"):
        catalog.refresh("weather")
    catalog.admit_revision("weather", 2)

    result, provider = execute(app)

    assert result["state"] == "failed"
    assert provider.declarations == []
    assert all(call[2]["method"] != "tools/call" for call in transport.calls)


@pytest.mark.parametrize("changed", [False, True])
def test_rebuilt_catalog_requires_the_same_durable_definition_not_just_revision_one(
        tmp_path: Path, changed: bool) -> None:
    catalog, _ = catalog_with_tool()
    first = app_with_catalog(tmp_path, catalog)
    _catalog(first, tmp_path)
    _queued(first)
    replacement, transport = catalog_with_tool(changed=changed)
    restarted = app_with_catalog(tmp_path, replacement)

    result, provider = execute(restarted)

    assert result["state"] == ("failed" if changed else "succeeded")
    assert bool(provider.declarations) is (not changed)
    assert sum(call[2]["method"] == "tools/call" for call in transport.calls) == (0 if changed else 1)


@pytest.mark.parametrize("initially_enabled", [False, True])
def test_run_without_mcp_authority_cannot_gain_it_from_a_later_catalog(
        tmp_path: Path, initially_enabled: bool) -> None:
    empty = ConnectorCatalog(enabled=True)
    first = app_with_catalog(tmp_path, empty, enabled=initially_enabled)
    _catalog(first, tmp_path)
    _queued(first)
    replacement, transport = catalog_with_tool()
    restarted = app_with_catalog(tmp_path, replacement)

    result, provider = execute(restarted)

    assert result["state"] == "failed"
    assert TOOL not in provider.declarations[0]
    assert all(call[2]["method"] != "tools/call" for call in transport.calls)


def test_run_snapshot_pins_are_private_and_same_token_reuses_the_original_authority(tmp_path: Path) -> None:
    catalog, _ = catalog_with_tool()
    app = app_with_catalog(tmp_path, catalog)
    repository = _catalog(app, tmp_path)
    client = app.test_client()
    conversation = client.post("/api/platform/v1/conversations", json={}).get_json()["conversation"]
    url = f"/api/platform/v1/conversations/{conversation['conversation_id']}/turns"
    body = {"text": "inspect", "client_token": "pinned", "overrides": {"mcp_tools": {"forged": {}}}}
    first = client.post(url, json=body)
    assert first.status_code == 202
    run_id = first.get_json()["run"]["run_id"]
    frozen = repository.get_run(OWNER, run_id)["config_snapshot"].get("mcp_tools")

    assert isinstance(frozen, dict) and set(frozen) == {TOOL}
    assert "mcp_tools" not in repr(first.get_json())
    assert "secret://mcp" not in repr(frozen) and "secret-token" not in repr(frozen)
    catalog.enabled = False
    retry = client.post(url, json=body)
    assert retry.status_code == 202
    assert retry.get_json()["run"]["run_id"] == run_id
    assert repository.get_run(OWNER, run_id)["config_snapshot"]["mcp_tools"] == frozen


@pytest.mark.parametrize("value", [None, [], {TOOL: {}}, {TOOL: {"revision": True, "definition_digest": "a" * 64}},
    {TOOL: {"revision": 0, "definition_digest": "a" * 64}},
    {TOOL: {"revision": 9007199254740992, "definition_digest": "a" * 64}},
    {TOOL: {"revision": 1, "definition_digest": "a" * 63}},
    {TOOL: {"revision": 1, "definition_digest": "A" * 64}},
    {TOOL: {"revision": 1, "definition_digest": "a" * 64, "extra": True}},
    {"workspace.read": {"revision": 1, "definition_digest": "a" * 64}},
    {f"mcp.fixture.tool_{index}": {"revision": 1, "definition_digest": "a" * 64} for index in range(21)}])
def test_mcp_pin_parser_rejects_malformed_or_unbounded_authority(value: object) -> None:
    from hub.domain.mcp_tool_pins import MCPToolPinError, parse_mcp_tool_pins

    with pytest.raises(MCPToolPinError, match="invalid_mcp_tool_pins"):
        parse_mcp_tool_pins(value)


def test_catalog_snapshot_is_scoped_copied_and_validates_the_whole_selected_set() -> None:
    from hub.domain.mcp_tool_pins import parse_mcp_tool_pins

    catalog, _ = catalog_with_tool()
    assert catalog.run_snapshot(owner_id="another", workspace_id="home") == {}
    assert catalog.run_snapshot(owner_id=OWNER, workspace_id="another") == {}
    snapshot = catalog.run_snapshot(owner_id=OWNER, workspace_id="home")
    snapshot[TOOL]["definition_digest"] = "b" * 64
    assert snapshot != catalog.run_snapshot(owner_id=OWNER, workspace_id="home")
    pins = parse_mcp_tool_pins(snapshot)
    with pytest.raises(MCPConnectorError, match="mcp_tool_pin_changed"):
        catalog.tool_definitions(owner_id=OWNER, workspace_id="home", expected_pins=pins)


@pytest.mark.parametrize("registration", [{"endpoint": "https://other.example.test/mcp"},
    {"timeout_s": 4.0}, {"result_limit_bytes": 2048}, {"owner_id": "other@example.test"},
    {"workspace_id": "other"}])
def test_rebuilt_catalog_cannot_retarget_an_existing_run(
        tmp_path: Path, registration: dict[str, Any]) -> None:
    catalog, _ = catalog_with_tool()
    first = app_with_catalog(tmp_path, catalog)
    _catalog(first, tmp_path)
    _queued(first)
    replacement, transport = catalog_with_tool(registration=registration)

    result, provider = execute(app_with_catalog(tmp_path, replacement))

    assert result["state"] == "failed" and result["result_text"] == "mcp_tool_pin_changed"
    assert provider.declarations == []
    assert all(call[2]["method"] != "tools/call" for call in transport.calls)


@pytest.mark.parametrize("field", ["output_schema", "description", "server_version"])
def test_rebuilt_catalog_pins_the_output_and_model_visible_definition(
        tmp_path: Path, field: str) -> None:
    catalog, _ = catalog_with_tool()
    first = app_with_catalog(tmp_path, catalog)
    _catalog(first, tmp_path)
    _queued(first)
    discovery, listing = (json.loads(item.body) for item in discover_payload())
    tool = listing["result"]["tools"][0]
    if field == "output_schema":
        tool["outputSchema"]["properties"]["humidity"] = {"type": "number"}
    elif field == "description":
        tool["description"] = "A changed instruction from an external server"
    else:
        discovery["result"]["_meta"]["io.modelcontextprotocol/serverInfo"]["version"] = "2"
    transport = FakeTransport([response(discovery), response(listing)])
    replacement = ConnectorCatalog(enabled=True)
    replacement.register(MCPConnector(config(owner_id=OWNER, workspace_id="home"),
        transport=transport, secret_broker=Broker()))
    replacement.discover("weather")

    result, provider = execute(app_with_catalog(tmp_path, replacement))

    assert result["state"] == "failed" and result["result_text"] == "mcp_tool_pin_changed"
    assert provider.declarations == []
    assert len(transport.calls) == 2


@pytest.mark.parametrize("pins", [None, [], {TOOL: {"revision": True, "definition_digest": "a" * 64}}])
def test_corrupt_durable_pins_do_not_fall_back_to_the_live_catalog(tmp_path: Path, pins: object) -> None:
    catalog, transport = catalog_with_tool()
    app = app_with_catalog(tmp_path, catalog)
    repository = _catalog(app, tmp_path)
    run = _queued(app)
    with closing(repository._connect()) as connection, connection:
        snapshot = repository.get_run(OWNER, run["run_id"])["config_snapshot"]
        snapshot["mcp_tools"] = pins
        connection.execute("UPDATE runs SET config_snapshot=? WHERE run_id=?",
            (json.dumps(snapshot), run["run_id"]))

    result, provider = execute(app)

    assert result["state"] == "failed" and result["result_text"] == "invalid_mcp_tool_pins"
    assert provider.declarations == []
    assert len(transport.calls) == 2


def test_definition_is_checked_again_after_model_declaration_before_call(tmp_path: Path) -> None:
    catalog, transport = catalog_with_tool()
    app = app_with_catalog(tmp_path, catalog)
    _catalog(app, tmp_path)
    _queued(app)

    class ChangingProvider(Provider):
        def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *,
                     request_observer: Any = None) -> ModelResponse:
            model_response = super().complete(messages, tools, request_observer=request_observer)
            changed = json.loads(discover_payload()[1].body)
            changed["result"]["tools"][0]["inputSchema"]["properties"]["country"] = {"type": "string"}
            transport.responses[:0] = [discover_payload()[0], response(changed)]
            with pytest.raises(MCPConnectorError, match="connector_revision_pending"):
                catalog.refresh("weather")
            catalog.admit_revision("weather", 2)
            return model_response

    provider = ChangingProvider()
    worker = app.extensions["fleet"]["services"]["platform_worker"]
    worker.provider_factory = lambda profile: provider
    result = worker.run_once(OWNER)

    assert result["state"] == "failed"
    assert TOOL in provider.declarations[0]
    assert all(call[2]["method"] != "tools/call" for call in transport.calls)


def test_secret_rotation_behind_the_same_reference_preserves_the_pin(tmp_path: Path) -> None:
    catalog, transport = catalog_with_tool()
    app = app_with_catalog(tmp_path, catalog)
    _catalog(app, tmp_path)
    _queued(app)
    catalog._connectors["weather"].secret_broker.value = "rotated-secret"

    result, _ = execute(app)

    assert result["state"] == "succeeded"
    assert transport.calls[-1][1]["Authorization"] == "Bearer rotated-secret"
    assert "rotated-secret" not in repr(result)


@pytest.mark.parametrize("revocation", ["disabled_catalog", "missing_catalog", "disabled_gate"])
def test_revoked_pinned_run_stops_before_model_io(tmp_path: Path, revocation: str) -> None:
    catalog, transport = catalog_with_tool()
    app = app_with_catalog(tmp_path, catalog)
    _catalog(app, tmp_path)
    _queued(app)
    if revocation == "disabled_catalog":
        catalog.enabled = False
    else:
        replacement = None if revocation == "missing_catalog" else catalog
        app = app_with_catalog(tmp_path, replacement, enabled=revocation != "disabled_gate")

    result, provider = execute(app)

    expected = "mcp_connectors_disabled" if revocation == "disabled_catalog" else "mcp_disabled"
    assert result["state"] == "failed" and result["result_text"] == expected
    assert provider.declarations == []
    assert all(call[2]["method"] != "tools/call" for call in transport.calls)
