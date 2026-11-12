"""Real account browser workflow for explicit summary version administration."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig
from test_account_http import ORIGIN, PASSWORD, signup
from test_workflow_browser import _browser_acceptance
from test_workflow_repository import OWNER, WORKSPACE, prepare_database

ROOT = Path(__file__).resolve().parents[1]


def test_admin_summary_versions_in_real_browser(tmp_path: Path) -> None:
    if not os.environ.get("FLEET_PLAYWRIGHT_MODULE"):
        pytest.skip("Set FLEET_PLAYWRIGHT_MODULE for real browser acceptance")
    repository = prepare_database(tmp_path / "platform.db")
    config = FleetConfig.from_root(tmp_path, frontend_dir=ROOT / "frontend", platform_enabled=True,
        platform_db=repository.db_path, platform_workflows_enabled=True, accounts={"enabled": True, "origin": ORIGIN,
        "registration": "open", "sender": lambda *_args: None})
    app = create_app(config)
    app.testing = True
    app.config["ACCOUNT_COOKIE_SECURE"] = False
    app.extensions["accounts"].bootstrap_admin(OWNER, PASSWORD)
    admin = app.test_client()
    assert admin.post("/api/accounts/login", json={"email": OWNER, "password": PASSWORD}, headers={"Origin": ORIGIN}, base_url=ORIGIN).status_code == 200
    admin_id = admin.get("/api/accounts/me", base_url=ORIGIN).get_json()["user"]["id"]
    app.extensions["fleet"]["platform_repository"].upsert_workspace(admin_id,
        {"workspace_id": WORKSPACE, "root_path": "/unused"})
    mail: list[tuple] = []
    app.config["ACCOUNT_SENDER"] = lambda *args: mail.append(args)
    signup(app.test_client(), mail, "user@example.test")
    _browser_acceptance(app, "workflow_summaries_browser.cjs", "admin")
    _browser_acceptance(app, "workflow_summaries_browser.cjs", "user")
