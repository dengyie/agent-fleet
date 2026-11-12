"""Account registration uses the original durable Fleet MCP authority."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from hub.domain.integration_identity import IntegrationIdentity, IntegrationIdentityError
from hub.domain.workflow_identity import canonical_json
from hub.infrastructure.integration_identity_repository import IntegrationIdentityRepository
from test_account_http import ORIGIN
from test_fleet_mcp_boundary import mcp
from test_integration_identity_admin import call, identity_admin  # noqa: F401

TOKEN = "registration-fixture-" + "x" * 48


def payload(**changes):
    identity = IntegrationIdentity.register("new-integration", "tenant", TOKEN,
        template_ids={"fixture"}, workspace_ids={"ready"})
    return {"identity": {**json.loads(identity.snapshot), **changes}}


def workspace(setup, tmp_path, name="ready", owner="tenant", enabled=True):
    return setup.app.extensions["fleet"]["platform_repository"].upsert_workspace(owner,
        {"workspace_id": name, "name": name[:30], "root_path": str(tmp_path), "enabled": enabled})


def register(setup, values=None, client=None, **kwargs):
    return call(client or setup.admin, "post", "/register", json=payload() if values is None else values, **kwargs)


def test_identity_registration_is_usable_idempotent_and_survives_fresh_store(identity_admin, tmp_path):
    setup = identity_admin
    workspace(setup, tmp_path)
    first = register(setup)
    assert first.status_code == 200, first.get_json()
    result = first.get_json()
    assert result["identity"]["summary"]["state"] == "active"
    assert result["identity"]["template_ids"] == ["fixture"]
    assert result["identity"]["workspace_ids"] == ["ready"]
    assert first.headers["Cache-Control"] == "no-store"
    for secret in (TOKEN, hashlib.sha256(TOKEN.encode()).hexdigest(), "token_digest", "definition_digest"):
        assert secret not in first.get_data(as_text=True)
    setup.clock[0] = 120
    assert register(setup).get_json() == result
    fresh = IntegrationIdentityRepository(setup.store.workflows)
    assert fresh.authenticate(TOKEN).integration_id == "new-integration"
    listed = mcp(setup.app.test_client(), TOKEN, "tools/list", {})
    assert listed.status_code == 200, listed.get_json()
    assert len(listed.get_json()["result"]["tools"]) == 3
    from hub.bootstrap import create_app
    from hub.config import FleetConfig
    restarted = create_app(FleetConfig.from_root(tmp_path, platform_enabled=True, platform_workflows_enabled=True,
        platform_fleet_mcp_enabled=True, platform_db=setup.store.workflows.db_path,
        accounts={"enabled": True, "origin": ORIGIN}))
    assert mcp(restarted.test_client(), TOKEN, "tools/list", {}).status_code == 200
    with setup.store.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM workflow_integration_identities WHERE integration_id='new-integration'").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM workflow_runs").fetchone()[0] == 0


@pytest.mark.parametrize("mode", ["revoked", "restore_disabled"])
def test_identity_registration_repeat_preserves_disable_and_current_unavailability(identity_admin, tmp_path, mode):
    setup = identity_admin
    workspace(setup, tmp_path)
    assert register(setup).status_code == 200
    if mode == "revoked":
        item = setup.store.authenticate(TOKEN)
        setup.store.revoke(item.owner_id, item.integration_id, expected_digest=item.definition_digest, now=110)
    else:
        with setup.store.workflows.transaction() as connection:
            connection.execute("UPDATE workflow_integration_identities SET restore_disabled=1 WHERE integration_id='new-integration'")
    workspace(setup, tmp_path, enabled=False)
    repeated = register(setup)
    assert repeated.status_code == 200, repeated.get_json()
    assert repeated.get_json()["identity"]["summary"]["state"] == mode
    assert setup.store.authenticate(TOKEN) is None


@pytest.mark.parametrize("mode", ["definition", "token"])
def test_identity_registration_conflict_never_changes_original(identity_admin, tmp_path, mode):
    setup = identity_admin
    workspace(setup, tmp_path)
    assert register(setup).status_code == 200
    original = setup.store.authenticate(TOKEN)
    value = payload(max_budget_tokens=1) if mode == "definition" else payload(integration_id="other-id")
    result = register(setup, value)
    assert result.status_code == 409, result.get_json()
    assert setup.store.authenticate(TOKEN) == original


@pytest.mark.parametrize("mode", ["missing", "disabled", "foreign", "frozen"])
def test_identity_registration_checks_current_workspace_and_admission_before_insert(identity_admin, tmp_path, mode):
    setup = identity_admin
    if mode != "missing":
        workspace(setup, tmp_path, owner="other" if mode == "foreign" else "tenant", enabled=mode != "disabled")
    if mode == "frozen":
        with setup.store.workflows.transaction() as connection:
            connection.execute("UPDATE workflow_schema_meta SET admission_mode='frozen'")
    result = register(setup)
    assert result.status_code == 409, result.get_json()
    assert setup.store.authenticate(TOKEN) is None


@pytest.mark.parametrize("change", ["extra", "raw_token", "digest", "scope", "duplicate", "budget", "owner", "array", "count", "surrogate"])
def test_identity_registration_rejects_invalid_definition_without_store_write(identity_admin, tmp_path, change):
    workspace(identity_admin, tmp_path)
    value = payload()
    row = value["identity"]
    if change == "extra":
        value["extra"] = True
    elif change == "raw_token":
        row["token"] = TOKEN
    elif change == "digest":
        row["token_digest"] = "wrong"
    elif change == "scope":
        row["scopes"] = ["workflow.acceptance"]
    elif change == "duplicate":
        row["workspace_ids"] *= 2
    elif change == "budget":
        row["max_budget_tokens"] = True
    elif change == "owner":
        row["owner_id"] = "bad/owner"
    elif change == "array":
        row["template_ids"] = "fixture"
    elif change == "count":
        row["template_ids"] = [str(i) for i in range(129)]
    else:
        row["integration_id"] = "\ud800"
    result = register(identity_admin, value)
    assert result.status_code == 400, result.get_json()
    assert identity_admin.store.authenticate(TOKEN) is None


@pytest.mark.parametrize("mode", ["anonymous", "ordinary", "origin"])
def test_identity_registration_keeps_original_account_and_origin_boundary(identity_admin, tmp_path, mode):
    setup = identity_admin
    workspace(setup, tmp_path)
    client = setup.app.test_client() if mode == "anonymous" else setup.ordinary if mode == "ordinary" else setup.admin
    headers = {"Origin": "https://foreign.example"} if mode == "origin" else {"Origin": ORIGIN}
    result = register(setup, client=client, headers=headers)
    assert result.status_code == (401 if mode == "anonymous" else 403)
    assert setup.store.authenticate(TOKEN) is None


@pytest.mark.parametrize("raw,status", [('{"identity":{},"identity":{}}', 400), ('{"identity":NaN}', 400),
    ('{"identity":', 400), ('"' + "x" * 65536 + '"', 413)], ids=["duplicate", "nan", "incomplete", "oversized"])
def test_identity_registration_strict_bounded_json(identity_admin, raw, status):
    result = call(identity_admin.admin, "post", "/register", data=raw, content_type="application/json")
    assert result.status_code == status, result.get_json()


def test_identity_registration_supports_full_original_allowlists(identity_admin, tmp_path):
    setup = identity_admin
    names = ["w" + str(i).zfill(3) + "x" * 120 for i in range(128)]
    for name in names:
        workspace(setup, tmp_path, name=name)
    value = payload(template_ids=["t" + name for name in names], workspace_ids=names)
    assert len(json.dumps(value).encode()) > 2048
    result = register(setup, value)
    assert result.status_code == 200, result.get_json()
    assert result.get_json()["identity"]["summary"]["template_count"] == 128
    assert result.get_json()["identity"]["workspace_ids"] == names


def test_identity_registration_insert_failure_rolls_back_and_keeps_error_chain(identity_admin, tmp_path):
    setup = identity_admin
    workspace(setup, tmp_path)
    with setup.store.workflows.transaction() as connection:
        connection.execute("CREATE TRIGGER fail_new_identity BEFORE INSERT ON workflow_integration_identities WHEN NEW.integration_id='new-integration' BEGIN SELECT RAISE(ABORT,'fixture_insert_failure'); END")
    result = register(setup)
    assert result.status_code == 503, result.get_json()
    assert setup.store.authenticate(TOKEN) is None


def test_identity_registration_command_and_frontend_parser_match_original_definition():
    from hub.domain.integration_administration import IntegrationRegisterCommand
    value = payload(template_ids=["a", "\ue000", "𐀀"])
    command = IntegrationRegisterCommand.parse(value)
    assert command.identity.snapshot == canonical_json(value["identity"])
    with pytest.raises(IntegrationIdentityError):
        IntegrationRegisterCommand.parse({"identity": {**value["identity"], "scopes": None}})
    root = Path(__file__).resolve().parents[1]
    program = """
import assert from 'node:assert/strict';
import {parseIdentityRegistrationCommand} from './frontend/assets/api/integration-identities.js';
const value = JSON.parse(process.env.IDENTITY_COMMAND);
assert.deepEqual(parseIdentityRegistrationCommand(value), value);
for (const change of [d => {d.identity.token='raw';}, d => {d.identity.scopes=null;},
  d => {d.identity.max_artifacts=101;}, d => {d.identity.workspace_ids=['w','w'];},
  d => {d.identity.template_ids.reverse();}, d => {d.identity.integration_id='\\ud800';}]) {
  const broken=structuredClone(value); change(broken);
  assert.throws(() => parseIdentityRegistrationCommand(broken));
}
"""
    result = subprocess.run(["node", "--input-type=module", "-e", program], cwd=root,
        env={**os.environ, "IDENTITY_COMMAND": json.dumps(value, ensure_ascii=True)}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_identity_registration_frontend_checks_complete_returned_policy(identity_admin, tmp_path):
    workspace(identity_admin, tmp_path)
    response = register(identity_admin)
    assert response.status_code == 200
    program = """
import assert from 'node:assert/strict';
import {registerIntegrationIdentity} from './frontend/assets/api/integration-identities.js';
const command=JSON.parse(process.env.IDENTITY_COMMAND), original=JSON.parse(process.env.IDENTITY_RESPONSE);
globalThis.window={FleetConfig:{apiBaseUrl:'/api'}};
let response=original, posted=[];
globalThis.fetch=async (url, options) => { posted.push(JSON.parse(options.body)); return new Response(JSON.stringify(response)); };
assert.deepEqual(await registerIntegrationIdentity(command), original);
assert.deepEqual(posted[0], command);
for (const change of [d => {d.identity.summary.owner_id='other';},
  d => {d.identity.summary.integration_id='other';}, d => {d.identity.summary.max_budget_tokens=1;},
  d => {d.identity.summary.scopes=[];}, d => {d.identity.template_ids=['other'];},
  d => {d.identity.workspace_ids=['other'];}]) {
  response=structuredClone(original); change(response);
  await assert.rejects(registerIntegrationIdentity(command), {name:'ContractError'});
}
"""
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(["node", "--input-type=module", "-e", program], cwd=root,
        env={**os.environ, "IDENTITY_COMMAND": json.dumps(payload()), "IDENTITY_RESPONSE": json.dumps(response.get_json())},
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_identity_registration_logs_actor_and_key_without_credentials(identity_admin, tmp_path, caplog):
    workspace(identity_admin, tmp_path)
    with caplog.at_level("INFO"):
        result = register(identity_admin)
    assert result.status_code == 200
    rows = [row.message for row in caplog.records if row.message.startswith("integration_identity_admin_action ")]
    assert len(rows) == 1
    value = json.loads(rows[0].split(" ", 1)[1])
    assert value["action"] == "register" and value["integration_id"] == "new-integration"
    assert value["actor"] and value["state"] == "active"
    assert TOKEN not in rows[0] and payload()["identity"]["token_digest"] not in rows[0]
