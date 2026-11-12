"""Secret-free administration over the existing durable Fleet authority."""
from __future__ import annotations

from dataclasses import dataclass, replace
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any
from urllib.parse import urlencode

import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.domain.integration_identity import IntegrationIdentity, IntegrationIdentityError
from hub.infrastructure.integration_identity_repository import IntegrationIdentityRepository
from test_account_http import ORIGIN, PASSWORD, post, signup
from test_integration_identity import store_at

ROOT = Path(__file__).resolve().parents[1]
PATH = "/api/platform/v1/integrations/fleet"
TOKEN = "admin-fixture-private-token-" + "a" * 32


def bearer(owner: str = "tenant", name: str = "integration") -> str:
    return TOKEN + hashlib.sha256((owner + "\0" + name).encode("utf-8")).hexdigest()


def registration(owner: str = "tenant", name: str = "integration", **changes: Any) -> IntegrationIdentity:
    identity = IntegrationIdentity.register(name, owner, bearer(owner, name),
        template_ids={"fixture", "另一模板"}, workspace_ids={"home", "另一个工作区"})
    return replace(identity, **changes)


def call(client: Any, method: str = "get", action: str = "", *, key: tuple[str, str] | None = None, **kwargs: Any) -> Any:
    kwargs.setdefault("base_url", ORIGIN)
    if method == "post":
        kwargs.setdefault("headers", {"Origin": ORIGIN})
    query = "?" + urlencode({"owner_id": key[0], "integration_id": key[1]}) if key is not None else ""
    return getattr(client, method)(PATH + action + query, **kwargs)


@dataclass
class IdentityAdminSetup:
    app: Any
    admin: Any
    ordinary: Any
    store: IntegrationIdentityRepository
    clock: list[float]


@pytest.fixture
def identity_admin(tmp_path: Path) -> IdentityAdminSetup:
    mail: list[Any] = []
    clock = [100.0]
    app = create_app(FleetConfig.from_root(tmp_path, frontend_dir=ROOT / "frontend", platform_enabled=True,
        platform_workflows_enabled=True, platform_fleet_mcp_enabled=True,
        accounts={"enabled": True, "origin": ORIGIN, "registration": "open", "sender": lambda *args: mail.append(args)}),
        repositories={"integration_identities": (registration(),), "workflow_clock": lambda: clock[0]})
    app.testing = True
    app.extensions["accounts"].bootstrap_admin("admin@example.test", PASSWORD)
    admin, ordinary = app.test_client(), app.test_client()
    assert post(admin, "login", {"email": "admin@example.test", "password": PASSWORD}).status_code == 200
    assert signup(ordinary, mail, "ordinary@example.test").status_code == 200
    return IdentityAdminSetup(app, admin, ordinary,
        app.extensions["fleet"]["repositories"]["integration_identities"], clock)


@pytest.mark.parametrize("payload,code", [({}, "precondition_required"),
    ({"expected_state": True}, "invalid_identity_command"), ({"expected_state": "revoked"}, "invalid_identity_command"),
    ({"expected_state": "active", "token": "x"}, "invalid_identity_command"), ([], "invalid_identity_command")])
def test_identity_admin_command_is_exact(payload: object, code: str) -> None:
    from hub.domain.integration_administration import IntegrationRevokeCommand
    with pytest.raises(IntegrationIdentityError, match=code):
        IntegrationRevokeCommand.parse(payload)


def test_identity_admin_command_is_frozen_and_revalidates_direct_calls(identity_admin: IdentityAdminSetup) -> None:
    from dataclasses import FrozenInstanceError
    from hub.domain.integration_administration import IntegrationRevokeCommand
    command = IntegrationRevokeCommand.parse({"expected_state": "active"})
    with pytest.raises(FrozenInstanceError):
        command.expected_state = "revoked"
    service = identity_admin.app.extensions["fleet"]["services"]["integration_identity_admin"]
    with pytest.raises(IntegrationIdentityError, match="invalid_identity_command"):
        service.revoke("tenant", "integration", IntegrationRevokeCommand("revoked"))
    assert identity_admin.store.authenticate(bearer()) is not None


def test_identity_admin_list_detail_are_complete_secret_free_and_detached(identity_admin: IdentityAdminSetup) -> None:
    setup = identity_admin
    response = call(setup.admin)
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    data = response.get_json()
    assert set(data) == {"ok", "identities", "next_cursor"} and data["next_cursor"] is None
    item = data["identities"][0]
    assert item["owner_id"] == "tenant" and item["integration_id"] == "integration"
    assert item["state"] == "active" and item["created_at"] == "1970-01-01T00:01:40.000Z"
    assert item["template_count"] == item["workspace_count"] == 2 and item["revoked_at"] is None
    detail = call(setup.admin, action="/record", key=("tenant", "integration")).get_json()
    assert detail["identity"]["summary"] == item
    assert detail["identity"]["template_ids"] == ["fixture", "另一模板"]
    assert detail["identity"]["workspace_ids"] == ["home", "另一个工作区"]
    value = registration()
    for private in (TOKEN, value.token_digest, value.definition_digest, "snapshot", "token_digest", "definition_digest", "namespace"):
        assert private not in json.dumps((data, detail))
    detail["identity"]["workspace_ids"].clear()
    assert setup.store.authenticate(bearer()).workspace_ids == value.workspace_ids


def test_identity_admin_feature_is_independent_and_admin_only(identity_admin: IdentityAdminSetup) -> None:
    for client, expected in ((identity_admin.admin, True), (identity_admin.ordinary, False)):
        result = client.get("/api/accounts/me", base_url=ORIGIN).get_json()
        assert result.get("features", {}).get("fleet_identity_management", False) is expected
        assert "mcp_catalog_management" not in result.get("features", {})


def test_both_management_features_are_advertised_together(tmp_path: Path) -> None:
    app = create_app(FleetConfig.from_root(tmp_path, platform_enabled=True, platform_workflows_enabled=True,
        platform_fleet_mcp_enabled=True, platform_mcp_connectors_enabled=True,
        accounts={"enabled": True, "origin": ORIGIN}))
    app.extensions["accounts"].bootstrap_admin("admin@example.test", PASSWORD)
    client = app.test_client()
    assert post(client, "login", {"email": "admin@example.test", "password": PASSWORD}).status_code == 200
    assert client.get("/api/accounts/me", base_url=ORIGIN).get_json()["features"] == {
        "mcp_catalog_management": True, "fleet_identity_management": True, "workflow_summary_management": True}


@pytest.mark.parametrize("headers", [{}, {"Cf-Access-Authenticated-User-Email": "admin@example.test"},
    {"Authorization": "Bearer " + bearer()}, {"X-Runner-Credential": "node:secret"}])
def test_foreign_credentials_cannot_read_identity_metadata(identity_admin: IdentityAdminSetup, monkeypatch: Any, headers: dict[str, str]) -> None:
    def no_io(*args: Any, **kwargs: Any) -> None:
        pytest.fail("foreign credential reached identity store")
    monkeypatch.setattr(identity_admin.store, "list_page", no_io)
    response = call(identity_admin.app.test_client(), headers=headers)
    assert response.status_code == 401 and response.headers["Cache-Control"] == "no-store"


def test_ordinary_account_cannot_read_or_revoke_identity(identity_admin: IdentityAdminSetup, monkeypatch: Any) -> None:
    def no_io(*args: Any, **kwargs: Any) -> None:
        pytest.fail("ordinary account reached identity store")
    for method in ("list_page", "get_record", "revoke"):
        monkeypatch.setattr(identity_admin.store, method, no_io)
    assert call(identity_admin.ordinary).status_code == 403
    assert call(identity_admin.ordinary, action="/record", key=("tenant", "integration")).status_code == 403
    assert call(identity_admin.ordinary, "post", "/revoke", key=("tenant", "integration"), json={"expected_state": "active"}).status_code == 403


def test_identity_admin_revoke_returns_transaction_receipt_and_preserves_original_contract(identity_admin: IdentityAdminSetup) -> None:
    setup = identity_admin
    setup.clock[0] = 110
    result = call(setup.admin, "post", "/revoke", key=("tenant", "integration"), json={"expected_state": "active"})
    assert result.status_code == 200
    assert result.get_json() == {"ok": True, "revocation": {"owner_id": "tenant", "integration_id": "integration", "state": "revoked"}}
    assert setup.store.authenticate(bearer()) is None
    setup.clock[0] = 120
    assert call(setup.admin, "post", "/revoke", key=("tenant", "integration"), json={"expected_state": "active"}).status_code == 409
    assert setup.store.revoke("tenant", "integration", expected_digest=registration().definition_digest, now=130).revoked
    record = setup.store.get_record("tenant", "integration")
    assert record.revoked_at == 110
    with setup.store.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM workflow_runs").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["revoke", "restore"])
def test_concurrent_identity_disable_is_checked_in_original_write_transaction(identity_admin: IdentityAdminSetup, monkeypatch: Any, change: str) -> None:
    setup = identity_admin
    other = IntegrationIdentityRepository(setup.store.workflows)
    original = setup.store.get_record
    def read_then_disable(owner: str, integration: str) -> Any:
        result = original(owner, integration)
        if change == "revoke":
            other.revoke(owner, integration, expected_digest=result.identity.definition_digest, now=105)
        else:
            with setup.store.workflows.transaction() as connection:
                connection.execute("UPDATE workflow_integration_identities SET restore_disabled=1")
        return result
    monkeypatch.setattr(setup.store, "get_record", read_then_disable)
    setup.clock[0] = 110
    result = call(setup.admin, "post", "/revoke", key=("tenant", "integration"), json={"expected_state": "active"})
    assert result.status_code == 409 and result.get_json()["error"] == "integration_state_changed"
    current = original("tenant", "integration")
    assert current.revoked_at == (105 if change == "revoke" else None)
    assert current.restore_disabled is (change == "restore")


def test_restored_identity_stays_visible_without_reactivation(identity_admin: IdentityAdminSetup) -> None:
    setup = identity_admin
    with setup.store.workflows.transaction() as connection:
        connection.execute("UPDATE workflow_integration_identities SET restore_disabled=1")
    result = call(setup.admin, action="/record", key=("tenant", "integration"))
    assert result.get_json()["identity"]["summary"]["state"] == "restore_disabled"
    assert call(setup.admin, "post", "/revoke", key=("tenant", "integration"), json={"expected_state": "active"}).status_code == 409
    assert setup.store.register(registration(), now=110).revoked
    assert setup.store.get_record("tenant", "integration").revoked_at is None


def test_identity_admin_keyset_is_bounded_and_uses_composite_index(tmp_path: Path, monkeypatch: Any) -> None:
    from hub.domain.integration_administration import IntegrationIdentityKey
    store = store_at(tmp_path)
    keys = [(owner, name) for owner in ("a", "b", "\ue000", "𐀀") for name in (".", "..", "x")]
    for owner, name in keys:
        store.register(registration(owner, name), now=100)
    statements: list[str] = []
    transaction = store.workflows.transaction
    @contextmanager
    def traced(*, write: bool = True) -> Any:
        with transaction(write=write) as connection:
            connection.set_trace_callback(statements.append)
            yield connection
    monkeypatch.setattr(store.workflows, "transaction", traced)
    first, cursor = store.list_page(None, 8)
    assert len(first) == 8 and cursor == IntegrationIdentityKey(*sorted(keys)[7])
    last, tail = store.list_page(cursor, 8)
    assert tail is None
    assert [(row.identity.owner_id, row.identity.integration_id) for row in (*first, *last)] == sorted(keys)
    queries = [sql for sql in statements if sql.startswith("SELECT ") and " FROM workflow_integration_identities " in sql]
    assert len(queries) == 2 and all(sql.endswith("LIMIT 9") and "OFFSET" not in sql and "SELECT *" not in sql for sql in queries)
    with transaction(write=False) as connection:
        plan = connection.execute("EXPLAIN QUERY PLAN " + queries[1]).fetchall()
    assert any("SEARCH" in row[3] and "INDEX" in row[3] for row in plan)
    assert not any("TEMP B-TREE" in row[3] for row in plan)


@pytest.mark.parametrize("owner,name", [("another", "integration"), ("租户 / ?#", "../记录?x#"), ("\ue000", "𐀀"), ("\ufeffowner", "\ufeffid"), ("a:b", "c"), ("a", "b:c")])
def test_original_utf8_composite_identifiers_roundtrip(identity_admin: IdentityAdminSetup, owner: str, name: str) -> None:
    setup = identity_admin
    setup.store.register(registration(owner, name), now=100)
    result = call(setup.admin, action="/record", key=(owner, name))
    assert result.status_code == 200
    assert result.get_json()["identity"]["summary"]["integration_id"] == name
    assert call(setup.admin, "post", "/revoke", key=(owner, name), json={"expected_state": "active"}).status_code == 200
    assert setup.store.authenticate(bearer()) is not None


@pytest.mark.parametrize("query", ["?limit=0", "?limit=9", "?limit=01", "?limit=1&limit=2", "?after_owner_id=tenant",
    "?after_integration_id=integration", "?after_owner_id=&after_integration_id=integration", "?owner_id=tenant",
    "?after_owner_id=a&after_owner_id=b&after_integration_id=c"])
def test_identity_list_rejects_ambiguous_query(identity_admin: IdentityAdminSetup, query: str) -> None:
    assert call(identity_admin.admin, action=query).status_code == 400


@pytest.mark.parametrize("query", ["", "?owner_id=tenant", "?owner_id=tenant&integration_id=integration&limit=1",
    "?owner_id=tenant&owner_id=tenant&integration_id=integration", "?owner_id=tenant&integration_id=%00"])
def test_identity_detail_rejects_ambiguous_selector(identity_admin: IdentityAdminSetup, query: str) -> None:
    assert call(identity_admin.admin, action="/record" + query).status_code == 400


@pytest.mark.parametrize("data,status", [('{}', 428), ('[]', 400), ('{"expected_state":true}', 400),
    ('{"expected_state":"active","expected_state":"active"}', 400), ('{"expected_state":NaN}', 400),
    ('{"expected_state":"active","extra":1}', 400), ('{"expected_state":"active","extra":"' + 'x' * 2048 + '"}', 413)])
def test_identity_admin_strict_body(identity_admin: IdentityAdminSetup, data: str, status: int) -> None:
    response = call(identity_admin.admin, "post", "/revoke", key=("tenant", "integration"), data=data, content_type="application/json")
    assert response.status_code == status and response.headers["Cache-Control"] == "no-store"


def test_identity_admin_origin_type_and_missing_record(identity_admin: IdentityAdminSetup) -> None:
    setup = identity_admin
    assert call(setup.admin, "post", "/revoke", key=("tenant", "integration"), json={"expected_state": "active"}, headers={}).status_code == 403
    assert call(setup.admin, "post", "/revoke", key=("tenant", "integration"), data="{}", content_type="text/plain").status_code == 415
    assert call(setup.admin, action="/record", key=("missing", "integration")).status_code == 404


@pytest.mark.parametrize("workflow,fleet,accounts", [(False, False, True), (True, False, True), (True, True, False)])
def test_identity_management_gate_and_admin_proof(tmp_path: Path, workflow: bool, fleet: bool, accounts: bool) -> None:
    settings = {"accounts": {"enabled": True, "origin": ORIGIN}} if accounts else {}
    app = create_app(FleetConfig.from_root(tmp_path, platform_enabled=True, platform_workflows_enabled=workflow,
        platform_fleet_mcp_enabled=fleet, dev_operator="legacy", **settings))
    client = app.test_client()
    if accounts:
        app.extensions["accounts"].bootstrap_admin("admin@example.test", PASSWORD)
        assert post(client, "login", {"email": "admin@example.test", "password": PASSWORD}).status_code == 200
        features = client.get("/api/accounts/me", base_url=ORIGIN).get_json().get("features", {})
        assert features == ({"workflow_summary_management": True} if workflow else {})
    assert call(client).status_code == (403 if workflow and fleet else 404)


def test_current_admin_role_and_session_are_rechecked(identity_admin: IdentityAdminSetup) -> None:
    setup = identity_admin
    with setup.app.extensions["accounts"].connect() as connection:
        connection.execute("UPDATE users SET role='user',revision=revision+1 WHERE email='admin@example.test'")
    assert call(setup.admin).status_code == 403
    assert call(setup.admin, "post", "/revoke", key=("tenant", "integration"), json={"expected_state": "active"}).status_code == 403
    assert post(setup.admin, "logout", {}).status_code == 200
    assert call(setup.admin).status_code == 401


@pytest.mark.parametrize("damage", ["record", "schema"])
def test_corrupt_identity_metadata_is_safe_server_failure(identity_admin: IdentityAdminSetup, damage: str) -> None:
    setup = identity_admin
    with setup.store.workflows.transaction() as connection:
        if damage == "record":
            connection.execute("UPDATE workflow_integration_identities SET definition_digest=?", ("a" * 64,))
        else:
            connection.execute("UPDATE workflow_integration_schema_meta SET schema_version=99")
    response = call(setup.admin)
    assert response.status_code == 503
    assert set(response.get_json()) == {"ok", "error", "detail", "request_id"}
    assert TOKEN not in response.get_data(as_text=True) and "snapshot" not in response.get_json()["detail"]


def test_identity_admin_boundary_keeps_the_original_error_chain(identity_admin: IdentityAdminSetup, monkeypatch: Any) -> None:
    from hub.http import integration_admin_routes
    cause = OSError("PRIVATE_PATH private-token")
    captured: list[Any] = []
    respond = integration_admin_routes.error_response
    def unavailable(*args: Any, **kwargs: Any) -> None:
        raise IntegrationIdentityError("integration_store_unavailable") from cause
    def capture(error: Any) -> Any:
        captured.append(error)
        return respond(error)
    monkeypatch.setattr(identity_admin.store, "get_record", unavailable)
    monkeypatch.setattr(integration_admin_routes, "error_response", capture)
    result = call(identity_admin.admin, action="/record", key=("tenant", "integration"))
    assert result.status_code == 503 and "PRIVATE_PATH" not in result.get_data(as_text=True)
    assert captured[0].__cause__.__cause__ is cause


def test_identity_admin_action_logs_escape_original_ids_and_omit_secrets(identity_admin: IdentityAdminSetup, caplog: Any) -> None:
    setup = identity_admin
    owner, name = "owner\nforged=1", "id\r\nentry"
    value = registration(owner, name)
    setup.store.register(value, now=100)
    with caplog.at_level("INFO"):
        response = call(setup.admin, "post", "/revoke", key=(owner, name), json={"expected_state": "active"})
    assert response.status_code == 200
    messages = [record.message for record in caplog.records if record.message.startswith("integration_identity_admin_action ")]
    assert len(messages) == 1 and "\n" not in messages[0] and "\r" not in messages[0]
    context = json.loads(messages[0].split(" ", 1)[1])
    assert context["owner_id"] == owner and context["integration_id"] == name and context["action"] == "revoke"
    for private in (TOKEN, value.token_digest, value.definition_digest, "template_ids", "workspace_ids"):
        assert private not in caplog.text


def test_identity_frontend_parser_accepts_real_utf8_pages_and_rejects_private_or_invalid_data(identity_admin: IdentityAdminSetup) -> None:
    setup = identity_admin
    for owner in ("\ue000", "𐀀"):
        setup.store.register(registration(owner, "../记录?x#"), now=100)
    setup.store.register(registration("\ufeffowner", "\ufeffid"), now=100)
    page = call(setup.admin).get_json()
    detail = call(setup.admin, action="/record", key=("tenant", "integration")).get_json()
    script = r'''
import assert from 'node:assert/strict';
const c = await import(process.env.CONTRACTS_URL);
const page = JSON.parse(process.env.PAGE); const detail = JSON.parse(process.env.DETAIL);
assert.equal(c.parseIdentityPage(page).identities.length, 4);
assert.equal(c.parseIdentityDetail(detail).identity.summary.integration_id, 'integration');
for (const mutate of [
  d => { d.identity.summary.token_digest = 'private'; },
  d => { d.identity.summary.owner_id = '\ud800'; },
  d => { d.identity.summary.owner_id = '界'.repeat(43); },
  d => { d.identity.summary.template_count = 129; },
  d => { d.identity.template_ids.reverse(); },
  d => { d.identity.workspace_ids.push(d.identity.workspace_ids[0]); },
  d => { d.identity.summary.max_budget_tokens = Number.MAX_SAFE_INTEGER + 1; },
  d => { d.identity.summary.created_at = '2026-02-30T00:00:00.000Z'; },
  d => { d.identity.summary.state = 'revoked'; },
  d => { d.identity.summary.scopes = ['workflow.acceptance']; }
]) {
  const copy = structuredClone(detail); mutate(copy);
  assert.throws(() => c.parseIdentityDetail(copy), {name: 'ContractError'});
}
const duplicate = structuredClone(page); duplicate.identities.push(duplicate.identities[0]);
assert.throws(() => c.parseIdentityPage(duplicate), {name: 'ContractError'});
const wrong = structuredClone(page); wrong.next_cursor = {owner_id: 'wrong', integration_id: 'wrong'};
assert.throws(() => c.parseIdentityPage(wrong), {name: 'ContractError'});
assert.deepEqual(c.parseIdentityRevocation({ok:true, revocation:{owner_id:'tenant',integration_id:'integration',state:'revoked'}}).revocation.state, 'revoked');
'''
    result = subprocess.run(["node", "--input-type=module", "-e", script], env={**os.environ,
        "CONTRACTS_URL": (ROOT / "frontend/assets/api/integration-identities.js").as_uri(), "PAGE": json.dumps(page),
        "DETAIL": json.dumps(detail)}, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
