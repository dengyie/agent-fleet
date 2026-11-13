"""Offline MCP management authority, public DTOs and exact transaction guards."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from hub.application.connector_catalog import ConnectorCatalog
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.domain.mcp_catalog import MCPCatalogError
from test_account_http import ORIGIN, PASSWORD, post, signup
from test_mcp_catalog_persistence import _connector, _messages, _store
from hub.platform.connectors.snapshots import encode_config

PATH = "/api/platform/v1/integrations/mcp"
ROOT = Path(__file__).resolve().parents[1]


def request(client: Any, method: str = "get", path: str = "", **kwargs: Any) -> Any:
    kwargs.setdefault("base_url", ORIGIN)
    if method == "post":
        kwargs.setdefault("headers", {"Origin": ORIGIN})
    return getattr(client, method)(PATH + path, **kwargs)


def seeded_store(tmp_path: Path) -> Any:
    store = _store(tmp_path / "platform.db")
    catalog = ConnectorCatalog(enabled=True, store=store, clock=lambda: 100)
    catalog.register(_connector(_messages() + _messages("schema")))
    catalog.discover("weather")
    with pytest.raises(MCPCatalogError, match="connector_revision_pending"):
        catalog.refresh("weather")
    return store


@dataclass
class AdminSetup:
    app: Any
    admin: Any
    ordinary: Any
    catalog: ConnectorCatalog
    clock: list[float]


@pytest.fixture
def admin_setup(tmp_path: Path) -> AdminSetup:
    mail: list[Any] = []
    clock = [100.0]
    connector = _connector(_messages() + _messages("schema"),
        endpoint="https://mcp.example.test/PRIVATE_PATH?token=PRIVATE_QUERY")
    app = create_app(FleetConfig.from_root(tmp_path, frontend_dir=ROOT / "frontend", platform_enabled=True,
        platform_mcp_connectors_enabled=True, accounts={"enabled": True, "origin": ORIGIN,
        "registration": "open", "sender": lambda *args: mail.append(args)}),
        repositories={"mcp_connectors": (connector,), "mcp_clock": lambda: clock[0]})
    app.testing = True
    app.extensions["accounts"].bootstrap_admin("admin@example.test", PASSWORD)
    admin, ordinary = app.test_client(), app.test_client()
    assert post(admin, "login", {"email": "admin@example.test", "password": PASSWORD}).status_code == 200
    assert signup(ordinary, mail, "ordinary@example.test").status_code == 200
    catalog = app.extensions["fleet"]["services"]["mcp_catalog"]
    catalog.discover("weather")
    with pytest.raises(MCPCatalogError, match="connector_revision_pending"):
        catalog.refresh("weather")
    return AdminSetup(app, admin, ordinary, catalog, clock)


@pytest.mark.parametrize("action,payload", [
    ("admit", {}), ("admit", {"expected_epoch": 3}),
    ("admit", {"expected_epoch": True, "pending_revision": 2}),
    ("admit", {"expected_epoch": 3, "pending_revision": 2.0}),
    ("admit", {"expected_epoch": 3, "pending_revision": 9007199254740992}),
    ("revoke", {"expected_epoch": 0}), ("revoke", {"expected_epoch": "3"}),
    ("revoke", {"expected_epoch": 3, "owner_id": "foreign"}),
    ("refresh", {"expected_epoch": 3}), ("revoke", []),
])
def test_admin_commands_reject_missing_extra_and_unsafe_fields(action: str, payload: object) -> None:
    from hub.domain.mcp_administration import MCPAdminCommand
    with pytest.raises(MCPCatalogError):
        MCPAdminCommand.parse(action, payload)


def test_admin_command_is_a_frozen_exact_boundary_dto() -> None:
    from dataclasses import FrozenInstanceError
    from hub.domain.mcp_administration import MCPAdminCommand
    command = MCPAdminCommand.parse("admit", {"expected_epoch": 3, "pending_revision": 2})
    assert (command.action, command.expected_epoch, command.pending_revision) == ("admit", 3, 2)
    with pytest.raises(FrozenInstanceError):
        command.expected_epoch = 4


@pytest.mark.parametrize("action", ["admit", "revoke"])
def test_epoch_cas_preserves_pending_record(tmp_path: Path, action: str) -> None:
    store = seeded_store(tmp_path)
    before = store.get_many(("weather",))[0]
    args = {"expected_revision": 2} if action == "admit" else {"expected_digest": before.config_digest}
    with pytest.raises(MCPCatalogError, match="connector_revision_changed"):
        getattr(store, action)("weather", expected_epoch=before.epoch - 1, now=110, **args)
    assert store.get_many(("weather",))[0] == before


def test_admin_list_and_detail_are_secret_free_complete_snapshots(admin_setup: AdminSetup) -> None:
    setup = admin_setup
    response = request(setup.admin)
    assert response.status_code == 200
    page = response.get_json()
    assert set(page) == {"ok", "connectors", "next_cursor"}
    assert page["next_cursor"] is None
    summary = page["connectors"][0]
    assert summary["connector_id"] == "weather"
    assert summary["state"] == "pending" and summary["runtime_ready"] is True
    assert summary["adapter_attached"] is True
    assert (summary["active_revision"], summary["pending_revision"], summary["epoch"]) == (1, 2, 3)
    assert summary["endpoint_origin"] == "https://mcp.example.test"
    assert summary["created_at"].endswith("Z") and summary["revoked_at"] is None
    detail = request(setup.admin, path="/record?connector_id=weather")
    assert detail.status_code == 200 and detail.headers["Cache-Control"] == "no-store"
    data = detail.get_json()["connector"]
    assert data["summary"] == summary
    assert data["active"]["tools"][0]["input_schema"] != data["pending"]["tools"][0]["input_schema"]
    assert len(data["pending"]["content_digest"]) == 64
    raw = json.dumps(data)
    for private in ("secret_ref", "secret://mcp", "PRIVATE_PATH", "PRIVATE_QUERY", "config_snapshot",
                    "config_digest", "transport", "private_alias", "secret-token"):
        assert private not in raw
    record = setup.catalog.store.get_many(("weather",))[0]
    data["active"]["tools"][0]["input_schema"].clear()
    assert setup.catalog.store.get_many(("weather",))[0] == record
    assert len(setup.catalog._connectors["weather"].transport.calls) == 4


def test_management_feature_is_advertised_only_to_current_admin(admin_setup: AdminSetup) -> None:
    for client, expected in ((admin_setup.admin, True), (admin_setup.ordinary, False)):
        result = client.get("/api/accounts/me", base_url=ORIGIN).get_json()
        assert result.get("features", {}).get("mcp_catalog_management", False) is expected


def test_ordinary_account_cannot_inspect_catalog(admin_setup: AdminSetup) -> None:
    setup = admin_setup
    original = setup.catalog.store.get_many(("weather",))[0]
    for suffix in ("", "/record?connector_id=weather"):
        assert request(setup.ordinary, path=suffix).status_code == 403
    for action in ("admit", "revoke"):
        assert request(setup.ordinary, "post", "/" + action + "?connector_id=weather", json={"expected_epoch": 3}).status_code == 403
    assert setup.catalog.store.get_many(("weather",))[0] == original


@pytest.mark.parametrize("headers", [{}, {"Cf-Access-Authenticated-User-Email": "admin@example.test"},
    {"X-Runner-Credential": "node:secret"}, {"Authorization": "Bearer fleet-integration"}])
def test_anonymous_and_foreign_headers_do_not_reach_catalog(admin_setup: AdminSetup, monkeypatch: Any, headers: dict[str, str]) -> None:
    def forbidden_io(*args: Any, **kwargs: Any) -> None:
        pytest.fail("unauthorized request reached durable catalog")
    monkeypatch.setattr(admin_setup.catalog.store, "list_page", forbidden_io)
    client = admin_setup.app.test_client()
    response = request(client, headers=headers)
    assert response.status_code == 401 and response.headers["Cache-Control"] == "no-store"
    if "X-Runner-Credential" in headers:
        assert request(admin_setup.admin, headers=headers).status_code == 401


def test_admit_is_exact_and_returns_its_transaction_snapshot(admin_setup: AdminSetup) -> None:
    setup = admin_setup
    response = request(setup.admin, "post", "/admit?connector_id=weather", json={"expected_epoch": 3, "pending_revision": 2})
    assert response.status_code == 200
    detail = response.get_json()["connector"]
    assert detail["summary"]["epoch"] == 4 and detail["summary"]["state"] == "active"
    assert detail["pending"] is None and detail["active"]["revision"] == 2
    before = setup.catalog.store.get_many(("weather",))[0]
    for payload in ({"expected_epoch": 3, "pending_revision": 2}, {"expected_epoch": 4, "pending_revision": 2}):
        assert request(setup.admin, "post", "/admit?connector_id=weather", json=payload).status_code == 409
    assert setup.catalog.store.get_many(("weather",))[0] == before
    assert len(setup.catalog._connectors["weather"].transport.calls) == 4


def test_revoke_uses_private_digest_preserves_first_fact_and_fences_old_epoch(admin_setup: AdminSetup) -> None:
    setup = admin_setup
    setup.clock[0] = 110
    response = request(setup.admin, "post", "/revoke?connector_id=weather", json={"expected_epoch": 3})
    assert response.status_code == 200
    summary = response.get_json()["connector"]["summary"]
    assert (summary["state"], summary["epoch"], summary["runtime_ready"]) == ("revoked", 4, False)
    before = setup.catalog.store.get_many(("weather",))[0]
    setup.clock[0] = 120
    assert request(setup.admin, "post", "/revoke?connector_id=weather", json={"expected_epoch": 3}).status_code == 409
    assert request(setup.admin, "post", "/revoke?connector_id=weather", json={"expected_epoch": 4}).get_json()["connector"]["summary"] == summary
    assert request(setup.admin, "post", "/admit?connector_id=weather", json={"expected_epoch": 4, "pending_revision": 2}).status_code == 409
    assert setup.catalog.store.get_many(("weather",))[0] == before
    assert setup.catalog.run_snapshot(owner_id="owner", workspace_id="workspace") == {}


@pytest.mark.parametrize("query", ["?limit=0", "?limit=9", "?limit=01", "?limit=1.0", "?limit=1&limit=2",
    "?after=weather&after=other", "?unknown=1", "?after=", "?after=../weather"])
def test_list_rejects_ambiguous_and_unbounded_query(admin_setup: AdminSetup, query: str) -> None:
    assert request(admin_setup.admin, path=query).status_code == 400


@pytest.mark.parametrize("suffix", ["/record?connector_id=weather&limit=1", "/admit?connector_id=weather&owner_id=foreign", "/revoke?connector_id=weather&after=weather"])
def test_detail_and_mutation_reject_query(admin_setup: AdminSetup, suffix: str) -> None:
    if "/admit" in suffix or "/revoke" in suffix:
        result = request(admin_setup.admin, "post", suffix, json={"expected_epoch": 3, "pending_revision": 2})
    else:
        result = request(admin_setup.admin, path=suffix)
    assert result.status_code == 400


@pytest.mark.parametrize("data,status", [
    ('{"expected_epoch":3,"expected_epoch":4}', 400), ('[]', 400), ('{"expected_epoch":NaN}', 400),
    ('{"expected_epoch":3.0}', 400), ('{}', 428), ('{"expected_epoch":3,"extra":1}', 400),
    ('{"expected_epoch":3,"extra":"' + 'x' * 2048 + '"}', 413),
])
def test_mutation_strict_json_and_body_limit(admin_setup: AdminSetup, data: str, status: int) -> None:
    result = request(admin_setup.admin, "post", "/revoke?connector_id=weather", data=data, content_type="application/json")
    assert result.status_code == status
    assert result.headers["Cache-Control"] == "no-store"


def test_mutation_origin_content_type_and_missing_identity_are_fenced(admin_setup: AdminSetup) -> None:
    setup = admin_setup
    assert request(setup.admin, "post", "/revoke?connector_id=weather", json={"expected_epoch": 3}, headers={}).status_code == 403
    assert request(setup.admin, "post", "/revoke?connector_id=weather", json={"expected_epoch": 3}, headers={"Origin": "https://evil.test"}).status_code == 403
    assert request(setup.admin, "post", "/revoke?connector_id=weather", data="{}", content_type="text/plain").status_code == 415
    assert request(setup.app.test_client(), "post", "/revoke?connector_id=weather", json={"expected_epoch": 3}).status_code == 401
    assert request(setup.admin, path="/record?connector_id=missing").status_code == 404


def test_list_keyset_is_bounded_complete_and_includes_unattached_history(admin_setup: AdminSetup) -> None:
    setup = admin_setup
    for key in ("A", "b", "d", "e", "f", "g", "h", "i", "j"):
        setup.catalog.store.register(encode_config(
            _connector(connector_id=key).config), now=100)
    first = request(setup.admin).get_json()
    assert len(first["connectors"]) == 8 and first["next_cursor"] == "i"
    last = request(setup.admin, path="?after=" + first["next_cursor"]).get_json()
    assert [item["connector_id"] for item in last["connectors"]] == ["j", "weather"] and last["next_cursor"] is None
    assert first["connectors"][0]["state"] == "registered"
    assert first["connectors"][0]["adapter_attached"] is False
    assert first["connectors"][0]["runtime_ready"] is False
    assert request(setup.admin, path="/record?connector_id=A").get_json()["connector"]["active"] is None
    single = request(setup.admin, path="?limit=1").get_json()
    assert single["next_cursor"] == "A" and len(single["connectors"]) == 1


@pytest.mark.parametrize("accounts,enabled,durable", [(True, False, True), (True, True, False), (False, True, True)])
def test_management_gate_and_role_proof_are_explicit(tmp_path: Path, accounts: bool, enabled: bool, durable: bool) -> None:
    extra = {"accounts": {"enabled": True, "origin": ORIGIN}} if accounts else {}
    repositories = {} if durable else {"mcp_catalog": ConnectorCatalog(enabled=True)}
    app = create_app(FleetConfig.from_root(tmp_path, dev_operator="legacy-admin", platform_enabled=True,
        platform_mcp_connectors_enabled=enabled, **extra), repositories=repositories)
    client = app.test_client()
    if accounts:
        app.extensions["accounts"].bootstrap_admin("admin@example.test", PASSWORD)
        assert post(client, "login", {"email": "admin@example.test", "password": PASSWORD}).status_code == 200
        assert "features" not in client.get("/api/accounts/me", base_url=ORIGIN).get_json()
    assert request(client).status_code == (403 if enabled and durable else 404)


def test_current_session_and_role_rechecked_on_every_management_request(admin_setup: AdminSetup) -> None:
    setup = admin_setup
    accounts = setup.app.extensions["accounts"]
    with accounts.connect() as db:
        target = db.execute("SELECT id,revision FROM users WHERE email='admin@example.test'").fetchone()
        db.execute("UPDATE users SET role='user',revision=revision+1 WHERE id=?", (target["id"],))
    assert request(setup.admin).status_code == 403
    assert request(setup.admin, "post", "/revoke?connector_id=weather", json={"expected_epoch": 3}).status_code == 403
    assert post(setup.admin, "logout", {}).status_code == 200
    assert request(setup.admin).status_code == 401


def test_repository_error_has_safe_public_detail_and_preserved_cause(admin_setup: AdminSetup, monkeypatch: Any) -> None:
    original = OSError("PRIVATE_PATH secret://mcp secret-token")
    def unavailable(*args: Any, **kwargs: Any) -> None:
        raise MCPCatalogError("mcp_catalog_store_unavailable", cause=original) from original
    monkeypatch.setattr(admin_setup.catalog.store, "list_page", unavailable)
    result = request(admin_setup.admin)
    assert result.status_code == 503
    assert set(result.get_json()) == {"ok", "error", "detail", "request_id"}
    assert "PRIVATE_PATH" not in result.get_data(as_text=True) and "secret" not in result.get_data(as_text=True)


def test_restored_records_are_visible_and_cannot_be_reactivated(admin_setup: AdminSetup) -> None:
    import sqlite3
    setup = admin_setup
    with sqlite3.connect(setup.catalog.store.db_path) as connection:
        connection.execute("UPDATE mcp_catalog_connectors SET restore_disabled=1")
    detail = request(setup.admin, path="/record?connector_id=weather").get_json()["connector"]
    assert detail["summary"]["state"] == "restore_disabled" and detail["summary"]["runtime_ready"] is False
    assert request(setup.admin, "post", "/admit?connector_id=weather", json={"expected_epoch": 3, "pending_revision": 2}).status_code == 409
    assert request(setup.admin, "post", "/revoke?connector_id=weather", json={"expected_epoch": 3}).get_json()["connector"] == detail


@pytest.mark.parametrize("identifier", [".", "..", "record", "admit", "revoke", "a.b:c_d-e"])
def test_every_original_identifier_has_an_unambiguous_management_route(admin_setup: AdminSetup, identifier: str) -> None:
    setup = admin_setup
    setup.catalog.store.register(encode_config(_connector(connector_id=identifier).config), now=100)
    detail = request(setup.admin, path="/record?connector_id=" + identifier)
    assert detail.status_code == 200
    assert detail.get_json()["connector"]["summary"]["connector_id"] == identifier
    assert request(setup.admin, "post", "/revoke?connector_id=" + identifier, json={"expected_epoch": 1}).status_code == 200


@pytest.mark.parametrize("suffix", ["/record", "/record?connector_id=weather&connector_id=weather", "/revoke?connector_id=weather&extra=1"])
def test_record_selector_rejects_missing_or_ambiguous_ids(admin_setup: AdminSetup, suffix: str) -> None:
    result = request(admin_setup.admin, "post" if suffix.startswith("/revoke") else "get", suffix,
        **({"json": {"expected_epoch": 3}} if suffix.startswith("/revoke") else {}))
    assert result.status_code == 400


def test_frontend_management_contracts_validate_actual_http_dtos(admin_setup: AdminSetup) -> None:
    import os
    import subprocess
    page = request(admin_setup.admin).get_json()
    detail = request(admin_setup.admin, path="/record?connector_id=weather").get_json()
    script = r'''
import assert from 'node:assert/strict';
const c = await import(process.env.CONTRACTS_URL);
const page = JSON.parse(process.env.PAGE);
const detail = JSON.parse(process.env.DETAIL);
assert.equal(c.parseMCPPage(page).connectors[0].connector_id, 'weather');
assert.equal(c.parseMCPDetail(detail).connector.pending.revision, 2);
for (const mutate of [
  d => { d.connector.summary.secret_ref = 'private'; },
  d => { d.connector.summary.epoch = 9007199254740992; },
  d => { d.connector.summary.adapter_attached = 'true'; },
  d => { d.connector.summary.endpoint_origin += '/private'; },
  d => { d.connector.summary.state = 'ready'; },
  d => { d.connector.summary.runtime_ready = false; },
  d => { d.connector.pending.revision = 1; },
  d => { d.connector.pending.tools[0].name = 'other'; },
  d => { d.connector.pending.tools[0].input_schema = []; },
  d => { d.connector.pending.content_digest = 'wrong'; },
  d => { d.connector.pending.tools[0].secret_ref = 'private'; },
  d => { d.connector.summary.created_at = '2026-02-30T00:00:00.000Z'; },
]) {
  const copy = structuredClone(detail); mutate(copy);
  assert.throws(() => c.parseMCPDetail(copy), {name: 'ContractError'});
}
const duplicate = structuredClone(page); duplicate.connectors.push(duplicate.connectors[0]);
assert.throws(() => c.parseMCPPage(duplicate), {name: 'ContractError'});
const over = structuredClone(page); over.connectors = Array(9).fill(page.connectors[0]);
assert.throws(() => c.parseMCPPage(over), {name: 'ContractError'});
'''
    result = subprocess.run(["node", "--input-type=module", "-e", script], env={**os.environ,
        "CONTRACTS_URL": (ROOT / "frontend/assets/api/integrations.js").as_uri(),
        "PAGE": json.dumps(page), "DETAIL": json.dumps(detail)}, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_corrupt_catalog_schema_is_a_server_failure_not_client_blame(admin_setup: AdminSetup) -> None:
    import sqlite3
    with sqlite3.connect(admin_setup.catalog.store.db_path) as connection:
        connection.execute("UPDATE mcp_catalog_meta SET schema_version='invalid'")
    result = request(admin_setup.admin)
    assert result.status_code == 503
    assert result.get_json()["error"] == "invalid_mcp_catalog_schema"
    assert result.headers["Cache-Control"] == "no-store"


def test_management_logs_only_bounded_public_action_context(admin_setup: AdminSetup, caplog: Any) -> None:
    with caplog.at_level("INFO"):
        result = request(admin_setup.admin, "post", "/admit?connector_id=weather", json={"expected_epoch": 3, "pending_revision": 2})
    assert result.status_code == 200
    actions = [record.message for record in caplog.records if "mcp_catalog_admin_action" in record.message]
    assert len(actions) == 1 and "action=admit epoch=4" in actions[0] and "connector=weather" in actions[0]
    for private in ("PRIVATE_PATH", "PRIVATE_QUERY", "secret-token", "secret://mcp", "input_schema"):
        assert private not in caplog.text


@pytest.mark.parametrize("endpoint", ["https://MCP.example.test:443/private?token=hidden", "https://[2001:db8::1]:8443/private"])
def test_original_endpoint_origin_variants_remain_parseable(admin_setup: AdminSetup, endpoint: str) -> None:
    import os
    import subprocess
    admin_setup.catalog.store.register(encode_config(_connector(connector_id="origin-case", endpoint=endpoint).config), now=100)
    payload = request(admin_setup.admin, path="/record?connector_id=origin-case").get_json()
    assert payload["connector"]["summary"]["created_at"] == "1970-01-01T00:01:40.000Z"
    script = "const c = await import(process.env.CONTRACTS_URL); c.parseMCPDetail(JSON.parse(process.env.DETAIL));"
    result = subprocess.run(["node", "--input-type=module", "-e", script], env={**os.environ,
        "CONTRACTS_URL": (ROOT / "frontend/assets/api/integrations.js").as_uri(), "DETAIL": json.dumps(payload)},
        text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("mode", ["cancel", "timeout", "success", "failure"])
def test_shared_request_keeps_deadline_and_cancel_through_response_body(mode: str) -> None:
    import os
    import subprocess
    script = r'''
import assert from 'node:assert/strict';
globalThis.window = {FleetConfig: {apiBaseUrl: '/api'}, location: {href: 'https://fleet.example.test/account'}, dispatchEvent() {}};
const {platformRequest} = await import(process.env.CLIENT_URL);
const mode = process.env.MODE;
const external = new AbortController(); let active = 0;
const add = external.signal.addEventListener.bind(external.signal);
const remove = external.signal.removeEventListener.bind(external.signal);
external.signal.addEventListener = (name, fn, options) => { if (name === 'abort') active += 1; return add(name, fn, options); };
external.signal.removeEventListener = (name, fn, options) => { if (name === 'abort') active -= 1; return remove(name, fn, options); };
let entered, resolveBody, rejectBody;
const reading = new Promise(resolve => { entered = resolve; });
const rootCause = new SyntaxError('invalid fixture JSON');
globalThis.fetch = async (_url, options) => ({ok: true, status: 200, json() {
  entered();
  return new Promise((resolve, reject) => {
    resolveBody = resolve; rejectBody = reject;
    options.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), {once: true});
  });
}});
const result = platformRequest('/platform/v1/integrations/mcp', {signal: external.signal, timeoutMs: mode === 'timeout' ? 20 : 1000})
  .then(value => ({value}), error => ({error}));
await reading;
assert.equal(active, 1, 'external cancellation must remain attached during body read');
if (mode === 'cancel') external.abort();
if (mode === 'success') resolveBody({ok: true});
if (mode === 'failure') rejectBody(rootCause);
const outcome = await Promise.race([result, new Promise(resolve => setTimeout(() => resolve({pending: true}), 80))]);
assert.equal(outcome.pending, undefined, 'body must settle before the bounded deadline');
assert.equal(active, 0, 'every settled request must release the external listener');
if (mode === 'success') assert.deepEqual(outcome.value, {ok: true});
else {
  assert.equal(outcome.error.code, mode === 'failure' ? 'invalid_json' : 'request_timeout');
  assert.ok(outcome.error.cause instanceof Error, 'the original body exception must remain available');
  if (mode === 'failure') assert.equal(outcome.error.cause, rootCause);
}
'''
    result = subprocess.run(["node", "--input-type=module", "-e", script], env={**os.environ,
        "CLIENT_URL": (ROOT / "frontend/assets/api/client.js").as_uri(), "MODE": mode},
        capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
