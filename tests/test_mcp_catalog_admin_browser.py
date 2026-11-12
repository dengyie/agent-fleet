"""Real account UI against disposable Flask/SQLite and fake MCP transport."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from hub.application.connector_catalog import ConnectorCatalog
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.domain.mcp_catalog import MCPCatalogError
from test_account_http import PASSWORD
from test_mcp_catalog_admin import AdminSetup, ROOT, admin_setup
from test_mcp_catalog_persistence import _connector, _messages
from test_mcp_connector import response
from test_workflow_browser import _browser_acceptance
from hub.platform.connectors.snapshots import encode_config, encode_discovery


@pytest.fixture
def browser_management(admin_setup: AdminSetup) -> AdminSetup:
    setup = admin_setup
    setup.app.config["ACCOUNT_COOKIE_SECURE"] = False
    messages = _messages("schema")
    listing = json.loads(messages[-1].body)
    listing["result"]["tools"][0]["description"] = '<img src=x onerror="window.mcpXss=1"> [probe](javascript:window.mcpXss=1) ' + '长中文定义' * 100
    messages[-1] = response(listing)
    setup.catalog._connectors["weather"].transport.responses.extend(messages)
    with pytest.raises(MCPCatalogError, match="connector_revision_pending"):
        setup.catalog.refresh("weather")
    setup.catalog.register(_connector(_messages(), connector_id="a-active"))
    discovery = setup.catalog.discover("a-active")
    for key in (".", "b-unattached", "c-revoked", "d-restored", "e", "f", "g", "h", "i"):
        record = setup.catalog.store.register(encode_config(_connector(connector_id=key).config), now=100)
        if key in {"b-unattached", "c-revoked", "d-restored"}:
            record = setup.catalog.store.publish(key, expected_epoch=record.epoch,
                discovery_snapshot=encode_discovery(discovery), now=100)
        if key == "c-revoked":
            setup.catalog.store.revoke(key, expected_digest=record.config_digest, now=100)
    with sqlite3.connect(setup.catalog.store.db_path) as connection:
        connection.execute("UPDATE mcp_catalog_connectors SET restore_disabled=1 WHERE connector_id='d-restored'")
    return setup


def _browser(setup: AdminSetup, mode: str, viewport: str = "desktop") -> None:
    if not os.environ.get("FLEET_PLAYWRIGHT_MODULE"):
        pytest.skip("Set FLEET_PLAYWRIGHT_MODULE for real browser acceptance")
    before = {key: len(value.transport.calls) for key, value in setup.catalog._connectors.items()}
    _browser_acceptance(setup.app, "integrations_browser.cjs", mode, viewport)
    assert before == {key: len(value.transport.calls) for key, value in setup.catalog._connectors.items()}


@pytest.mark.parametrize("viewport", ["desktop", "mobile"])
def test_mcp_admin_review_and_revocation_in_real_browser(browser_management: AdminSetup, viewport: str) -> None:
    _browser(browser_management, "review", viewport)


def test_mcp_admin_unknown_result_and_conflict_require_readback(browser_management: AdminSetup) -> None:
    _browser(browser_management, "recovery")


def test_mcp_admin_view_lifecycle_and_account_switch_clear_authority(browser_management: AdminSetup) -> None:
    _browser(browser_management, "lifecycle")


@pytest.mark.parametrize("mode", ["off", "ephemeral", "empty"])
def test_mcp_admin_feature_discovery_in_real_browser(tmp_path: Path, mode: str) -> None:
    if not os.environ.get("FLEET_PLAYWRIGHT_MODULE"):
        pytest.skip("Set FLEET_PLAYWRIGHT_MODULE for real browser acceptance")
    repositories: dict[str, Any] = {"mcp_catalog": ConnectorCatalog(enabled=True)} if mode == "ephemeral" else {}
    app = create_app(FleetConfig.from_root(tmp_path, frontend_dir=ROOT / "frontend", platform_enabled=True,
        platform_mcp_connectors_enabled=mode != "off", accounts={"enabled": True, "origin": "http://127.0.0.1"}),
        repositories=repositories)
    app.extensions["accounts"].bootstrap_admin("admin@example.test", PASSWORD)
    _browser_acceptance(app, "integrations_browser.cjs", "empty" if mode == "empty" else "hidden", "desktop")
