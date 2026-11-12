"""Actual Chrome identity management against disposable account/Workflow databases."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig
from test_account_http import PASSWORD
from test_integration_identity_admin import IdentityAdminSetup, ROOT, identity_admin, registration
from test_workflow_browser import _browser_acceptance


@pytest.fixture
def browser_identities(identity_admin: IdentityAdminSetup) -> IdentityAdminSetup:
    setup = identity_admin
    setup.app.config["ACCOUNT_COOKIE_SECURE"] = False
    for owner, name in (("a", "."), ("a", "b-restored"), ("a", "c-revoked"),
        ("a", "d"), ("a", "e"), ("a", "f"), ("a", "g"), ("a", "h"), ("z 租户 / ?#", "../记录?x#")):
        value = registration(owner, name)
        if name == ".":
            value = registration(owner, name, scopes=frozenset(),
                template_ids=frozenset({'<img src=x onerror="window.identityXss=1">', '[probe](javascript:window.identityXss=1)'}))
        setup.store.register(value, now=100)
        if name == "c-revoked":
            setup.store.revoke(owner, name, expected_digest=value.definition_digest, now=100)
    with setup.store.workflows.transaction() as connection:
        connection.execute("UPDATE workflow_integration_identities SET restore_disabled=1 WHERE integration_id='b-restored'")
    return setup


@pytest.mark.parametrize("viewport", ["desktop", "mobile"])
def test_fleet_identity_review_and_revoke_in_real_browser(browser_identities: IdentityAdminSetup, viewport: str) -> None:
    if not os.environ.get("FLEET_PLAYWRIGHT_MODULE"):
        pytest.skip("Set FLEET_PLAYWRIGHT_MODULE for real browser acceptance")
    _browser_acceptance(browser_identities.app, "integration_identities_browser.cjs", "review", viewport)


@pytest.mark.parametrize("mode", ["recovery", "lifecycle"])
def test_fleet_identity_recovery_and_account_isolation_in_real_browser(browser_identities: IdentityAdminSetup, mode: str) -> None:
    if not os.environ.get("FLEET_PLAYWRIGHT_MODULE"):
        pytest.skip("Set FLEET_PLAYWRIGHT_MODULE for real browser acceptance")
    _browser_acceptance(browser_identities.app, "integration_identities_browser.cjs", mode, "desktop")


@pytest.mark.parametrize("enabled", [False, True])
def test_fleet_identity_feature_discovery_in_real_browser(tmp_path: Path, enabled: bool) -> None:
    if not os.environ.get("FLEET_PLAYWRIGHT_MODULE"):
        pytest.skip("Set FLEET_PLAYWRIGHT_MODULE for real browser acceptance")
    app = create_app(FleetConfig.from_root(tmp_path, frontend_dir=ROOT / "frontend", platform_enabled=True,
        platform_workflows_enabled=True, platform_fleet_mcp_enabled=enabled,
        accounts={"enabled": True, "origin": "http://127.0.0.1"}))
    app.extensions["accounts"].bootstrap_admin("admin@example.test", PASSWORD)
    _browser_acceptance(app, "integration_identities_browser.cjs", "empty" if enabled else "hidden", "desktop")


def test_both_integration_panels_resume_in_real_browser(tmp_path: Path) -> None:
    if not os.environ.get("FLEET_PLAYWRIGHT_MODULE"):
        pytest.skip("Set FLEET_PLAYWRIGHT_MODULE for real browser acceptance")
    app = create_app(FleetConfig.from_root(tmp_path, frontend_dir=ROOT / "frontend", platform_enabled=True,
        platform_workflows_enabled=True, platform_fleet_mcp_enabled=True, platform_mcp_connectors_enabled=True,
        accounts={"enabled": True, "origin": "http://127.0.0.1"}))
    app.extensions["accounts"].bootstrap_admin("admin@example.test", PASSWORD)
    _browser_acceptance(app, "integration_identities_browser.cjs", "both", "desktop")


@pytest.mark.parametrize("mode,viewport", [("create", "desktop"), ("create", "mobile"),
    ("retry", "desktop"), ("hash-close", "desktop"), ("lifecycle", "desktop"), ("exact-ids", "desktop")])
def test_fleet_identity_registration_in_real_browser(identity_admin: IdentityAdminSetup, tmp_path: Path, mode: str, viewport: str) -> None:
    if not os.environ.get("FLEET_PLAYWRIGHT_MODULE"):
        pytest.skip("Set FLEET_PLAYWRIGHT_MODULE for real browser acceptance")
    from test_integration_identity_registration import workspace
    setup = identity_admin
    setup.app.config["ACCOUNT_COOKIE_SECURE"] = False
    workspace(setup, tmp_path)
    if mode == "exact-ids":
        workspace(setup, tmp_path, owner="\ufefftenant")
    _browser_acceptance(setup.app, "identity_registration_browser.cjs", mode, viewport)
