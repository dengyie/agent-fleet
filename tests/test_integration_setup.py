"""Operator CLI provisioning reaches the normal durable Fleet MCP endpoint."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest
from test_fleet_mcp_boundary import mcp
from test_workflow_repository import OWNER, WORKSPACE, prepare_database

from hub import web
from hub.domain.integration_identity import IntegrationIdentity
from hub.infrastructure.integration_identity_repository import (
    IntegrationIdentityRepository,
)

TOKEN = "setup-identity-" + "a" * 40


def manifest(root, **changes):
    identity = IntegrationIdentity.register("client-a", OWNER, TOKEN, template_ids={"coding-delivery"},
        workspace_ids={WORKSPACE})
    row = {**json.loads(identity.snapshot), **changes}
    path = root / "identities.json"
    path.write_text(json.dumps({"schema_version": 1, "identities": [row]}), encoding="utf-8")
    return path, identity


def command(path, db):
    return [sys.executable, "-m", "hub.integration_setup", "--registration", str(path),
            "--database", str(db), "--owner", OWNER, "--integration", "client-a"]


def invoke(args, *extra):
    return subprocess.run([*args, *extra], check=False, capture_output=True, text=True, timeout=15)


def test_cli_preview_does_not_create_or_open_database(tmp_path):
    path, _ = manifest(tmp_path)
    db = tmp_path / "not-created" / "platform.db"
    result = invoke(command(path, db))
    assert result.returncode == 0, result.stderr + result.stdout
    assert json.loads(result.stdout) == {"ok": True, "mode": "preview", "owner_id": OWNER, "integration_id": "client-a"}
    assert not db.parent.exists()


def test_cli_registration_normal_hub_restart_and_revoke(tmp_path, monkeypatch):
    path, identity = manifest(tmp_path)
    db = tmp_path / "platform.db"
    repository = prepare_database(db)
    result = invoke(command(path, db), "--apply")
    assert result.returncode == 0, result.stderr + result.stdout
    assert json.loads(result.stdout)["state"] == "active"
    store = IntegrationIdentityRepository(repository)
    first = store.get_record(OWNER, "client-a")
    assert store.authenticate(TOKEN) == identity
    repeated = invoke(command(path, db), "--apply")
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert store.get_record(OWNER, "client-a") == first
    monkeypatch.setattr(web, "FLEET_HOME", tmp_path)
    def start():
        return web.make_app(require_token=False, serve_frontend=False, platform_enabled=True, platform_db=db,
            platform_workflows_enabled=True, platform_fleet_mcp_enabled=True)
    app = start()
    reply = mcp(app.test_client(), TOKEN, "tools/list", {})
    assert reply.status_code == 200, reply.get_json()
    assert [tool["name"] for tool in reply.get_json()["result"]["tools"]] == [
        "workflow.create_from_template", "workflow.get", "workflow.list_artifacts"]
    store.revoke(OWNER, "client-a", expected_digest=identity.definition_digest, now=first.created_at + 1)
    assert mcp(app.test_client(), TOKEN, "tools/list", {}).status_code == 401
    applied_revoked = invoke(command(path, db), "--apply")
    assert applied_revoked.returncode == 0, applied_revoked.stdout + applied_revoked.stderr
    assert json.loads(applied_revoked.stdout)["state"] == "revoked"
    assert mcp(start().test_client(), TOKEN, "tools/list", {}).status_code == 401
    assert TOKEN.encode() not in db.read_bytes()


@pytest.mark.parametrize("change", ["version", "fields", "row_fields", "empty", "many", "duplicate", "token_duplicate",
    "raw_token", "scope", "budget", "duplicate_workspace", "wrong_type", "unsorted", "duplicate_json", "unicode", "oversized"])
def test_complete_manifest_validation_precedes_any_registration(tmp_path, change):
    from hub.composition.integration_registration import (
        MAX_REGISTRATION_BYTES,
        read_integration_registration,
    )
    from hub.domain.integration_identity import IntegrationIdentityError
    path, _ = manifest(tmp_path)
    value = json.loads(path.read_text())
    row = value["identities"][0]
    if change == "version": value["schema_version"] = True
    elif change == "fields": value["unknown"] = 1
    elif change == "row_fields": row["token"] = TOKEN
    elif change == "empty": value["identities"] = []
    elif change == "many": value["identities"] *= 129
    elif change == "duplicate": value["identities"] *= 2
    elif change == "token_duplicate": value["identities"].append({**row, "integration_id": "client-b"})
    elif change == "raw_token": row["token_digest"] = TOKEN
    elif change == "scope": row["scopes"] = ["workflow.acceptance"]
    elif change == "budget": row["max_budget_tokens"] = True
    elif change == "duplicate_workspace": row["workspace_ids"] *= 2
    elif change == "wrong_type": value["identities"] = {}
    elif change == "unsorted": row["template_ids"] = ["z", "a"]
    path.write_text(json.dumps(value), encoding="utf-8")
    if change == "duplicate_json": path.write_text('{"schema_version":1,"schema_version":1,"identities":[]}', encoding="utf-8")
    elif change == "unicode": path.write_bytes(b"\xff")
    elif change == "oversized": path.write_bytes(b" " * (MAX_REGISTRATION_BYTES + 1))
    with pytest.raises(IntegrationIdentityError):
        read_integration_registration(path)


@pytest.mark.parametrize("mode", ["missing_store", "missing_workspace", "disabled_workspace", "identity_conflict", "token_conflict", "uninitialized_store"])
def test_apply_errors_preserve_original_store_and_hide_tokens(tmp_path, mode):
    from hub.infrastructure.platform_db import PlatformRepository
    path, identity = manifest(tmp_path)
    db = tmp_path / "platform.db"
    store, before = None, None
    if mode == "uninitialized_store":
        platform = PlatformRepository(db)
        platform.init()
        platform.upsert_workspace(OWNER, {"workspace_id": WORKSPACE, "root_path": str(tmp_path)})
    elif mode != "missing_store":
        repository = prepare_database(db)
        store = IntegrationIdentityRepository(repository)
        store.init()
        if mode == "missing_workspace":
            path, identity = manifest(tmp_path, workspace_ids=["absent"])
        elif mode == "disabled_workspace":
            PlatformRepository(db).upsert_workspace(OWNER, {"workspace_id": WORKSPACE, "root_path": str(tmp_path), "enabled": False})
        elif mode in ("identity_conflict", "token_conflict"):
            store.register(identity, now=100)
            before = store.get_record(OWNER, "client-a")
            path, _ = manifest(tmp_path, **({"max_budget_tokens": 1} if mode == "identity_conflict" else {"integration_id": "client-b"}))
    args = command(path, db)
    if mode == "token_conflict": args[-1] = "client-b"
    result = invoke(args, "--apply")
    assert result.returncode == 2, result.stdout + result.stderr
    report = json.loads(result.stdout)
    expected = {"missing_store": "integration_setup_requires_existing_database",
        "missing_workspace": "integration_setup_workspace_unavailable", "disabled_workspace": "integration_setup_workspace_unavailable",
        "identity_conflict": "integration_registration_conflict", "token_conflict": "integration_token_conflict",
        "uninitialized_store": "integration_setup_invalid_configuration_or_store"}
    assert report == {"ok": False, "error": expected[mode]}
    assert all(secret not in result.stdout + result.stderr for secret in (TOKEN, identity.token_digest, "Traceback"))
    if mode == "missing_store": assert not db.exists()
    if before is not None: assert store.get_record(OWNER, "client-a") == before
    if store is not None:
        assert len(store.list_page()[0]) == int(before is not None)


def test_preview_ignores_invalid_database_and_selects_exact_composite_key(tmp_path):
    from hub.composition.integration_registration import read_integration_registration
    from hub.integration_setup import main
    path, _ = manifest(tmp_path)
    value = json.loads(path.read_text())
    other = IntegrationIdentity.register("client-a", "another@example.test", "different-token-" + "b" * 40,
        template_ids={"coding-delivery"}, workspace_ids={WORKSPACE})
    value["identities"].append(json.loads(other.snapshot))
    path.write_text(json.dumps(value), encoding="utf-8")
    assert len(read_integration_registration(path)) == 2
    db = tmp_path / "invalid.db"
    db.write_bytes(b"not sqlite")
    result = invoke(command(path, db))
    assert result.returncode == 0 and json.loads(result.stdout)["owner_id"] == OWNER
    assert db.read_bytes() == b"not sqlite"
    assert main(["--registration", str(path), "--owner", "absent@example.test", "--integration", "client-a"]) == 2


def test_apply_restored_disabled_identity_cannot_enable_it(tmp_path):
    path, identity = manifest(tmp_path)
    db = tmp_path / "platform.db"
    repository = prepare_database(db)
    store = IntegrationIdentityRepository(repository)
    store.init()
    store.register(identity, now=100)
    with repository.transaction() as connection:
        connection.execute("UPDATE workflow_integration_identities SET restore_disabled=1 WHERE owner_id=? AND integration_id=?",
            (OWNER, identity.integration_id))
    before = store.get_record(OWNER, identity.integration_id)
    result = invoke(command(path, db), "--apply")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["state"] == "restore_disabled"
    assert store.get_record(OWNER, identity.integration_id) == before
    assert store.authenticate(TOKEN) is None


def test_cli_registered_identity_authenticates_actual_https_peer(tmp_path, monkeypatch):
    from test_mcp_http import MCPWireClient, fleet_server

    path, _ = manifest(tmp_path)
    db = tmp_path / "platform.db"
    prepare_database(db)
    result = invoke(command(path, db), "--apply")
    assert result.returncode == 0, result.stdout + result.stderr
    monkeypatch.setattr(web, "FLEET_HOME", tmp_path)
    app = web.make_app(require_token=False, serve_frontend=False, platform_enabled=True, platform_db=db,
        platform_workflows_enabled=True, platform_fleet_mcp_enabled=True)
    with fleet_server(app, tmp_path, monkeypatch) as endpoint:
        client = MCPWireClient(endpoint, TOKEN)
        status, discovery = client.request("server/discover", {})
        assert status == 200 and discovery["result"]["supportedVersions"] == ["2026-07-28"]
        status, tools = client.request("tools/list", {})
        assert status == 200 and len(tools["result"]["tools"]) == 3
        assert MCPWireClient(endpoint, "wrong-token-" + "b" * 40).request("tools/list", {})[0] == 401
