import hashlib
import secrets
from dataclasses import replace
from pathlib import Path

import pytest

from hub.application.integration_identity import IntegrationIdentity
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.infrastructure.integration_identity_repository import IntegrationIdentityRepository
from hub.infrastructure.workflow_repository import WorkflowRepository
from test_workflow_http import http_setup, body


def _identity(owner, workspace):
    token = "mcp-" + secrets.token_urlsafe(32)
    return IntegrationIdentity.register("integration-1", owner, token,
                                        template_ids={"fixture"}, workspace_ids={workspace}), token


def _app(tmp_path: Path, **identity_changes):
    app, client, setup = http_setup(tmp_path)
    identity, token = _identity("owner@example.test", "workspace-a")
    identity = replace(identity, **identity_changes)
    app.config.update(PLATFORM_FLEET_MCP_ENABLED=True)
    identities = IntegrationIdentityRepository(setup.repository)
    identities.init()
    identities.register(identity, now=setup.clock())
    app.extensions["fleet"]["repositories"]["integration_identities"] = identities
    app.register_blueprint(__import__("hub.http.fleet_mcp_routes", fromlist=["bp"]).bp)
    return app, client, setup, token, identity


def mcp(client, token, method, params, request_id="1"):
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": method,
    }
    if method == "tools/call":
        headers["Mcp-Name"] = params.get("name", "")
    return client.post("/api/platform/mcp", headers=headers, json={"jsonrpc": "2.0", "id": request_id, "method": method,
             "params": {**params, "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}})


def test_discovery_exposes_exactly_three_tools_and_rejects_forbidden_methods(tmp_path):
    _, client, _, token, _ = _app(tmp_path)
    listed = mcp(client, token, "tools/list", {}).get_json()
    assert [item["name"] for item in listed["result"]["tools"]] == [
        "workflow.create_from_template", "workflow.get", "workflow.list_artifacts"]
    forbidden = mcp(client, token, "workflow.acceptance", {})
    assert forbidden.status_code == 404


def test_create_is_scoped_and_stable_token_is_namespaced(tmp_path):
    _, client, setup, token, identity = _app(tmp_path)
    args = {**body(), "client_token": "same"}
    first = mcp(client, token, "tools/call", {"name": "workflow.create_from_template", "arguments": args})
    assert first.status_code == 200, first.get_json()
    workflow_id = first.get_json()["result"]["structuredContent"]["workflow_id"]
    with setup.repository.transaction(write=False) as connection:
        persisted = connection.execute("SELECT client_token FROM workflow_runs WHERE workflow_id=?", (workflow_id,)).fetchone()[0]
    assert persisted == identity.namespace_token("same")
    repeated = mcp(client, token, "tools/call", {"name": "workflow.create_from_template", "arguments": args})
    assert repeated.get_json() == first.get_json()
    foreign = mcp(client, token, "tools/call", {"name": "workflow.get", "arguments": {"workflow_id": "other"}})
    assert foreign.status_code == 403


def test_workflow_ownership_survives_identity_object_recreation(tmp_path):
    app, client, setup, token, identity = _app(tmp_path)
    args = {**body(), "client_token": "restart-safe"}
    created = mcp(client, token, "tools/call", {"name": "workflow.create_from_template", "arguments": args})
    workflow_id = created.get_json()["result"]["structuredContent"]["workflow_id"]

    replacement = IntegrationIdentityRepository(WorkflowRepository(setup.repository.db_path))
    replacement.init()
    app.extensions["fleet"]["repositories"]["integration_identities"] = replacement
    assert replacement.authenticate(token) == identity
    fetched = mcp(client, token, "tools/call", {"name": "workflow.get", "arguments": {"workflow_id": workflow_id}})
    assert fetched.status_code == 200, fetched.get_json()


def test_revocation_immediately_blocks_and_no_sensitive_tools_exist(tmp_path):
    app, client, _, token, identity = _app(tmp_path)
    app.extensions["fleet"]["repositories"]["integration_identities"].revoke(identity.owner_id, identity.integration_id,
        expected_digest=identity.definition_digest, now=101)
    response = mcp(client, token, "tools/list", {})
    assert response.status_code == 401
    assert "SecretBroker" not in response.get_data(as_text=True)
    assert "shell" not in response.get_data(as_text=True)


@pytest.mark.parametrize("header,value", [
    ("MCP-Protocol-Version", None), ("MCP-Protocol-Version", "2025-06-18"),
    ("Mcp-Method", None), ("Mcp-Method", "tools/call"),
])
def test_protocol_headers_must_match_body(tmp_path, header, value):
    _, client, _, token, _ = _app(tmp_path)
    headers = {"Authorization": "Bearer " + token, "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/list",
               "Accept": "application/json, text/event-stream"}
    if value is None:
        del headers[header]
    else:
        headers[header] = value
    reply = client.post("/api/platform/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": "header-check", "method": "tools/list",
        "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}})
    assert reply.status_code == 400, reply.get_json()
    assert reply.get_json()["id"] == "header-check"


@pytest.mark.parametrize("request_id", [None, True, [], {}, 1.5, 9007199254740992])
def test_request_ids_are_bounded_strings_or_exact_safe_integers(tmp_path, request_id):
    _, client, _, token, _ = _app(tmp_path)
    reply = mcp(client, token, "tools/list", {}, request_id=request_id)
    assert reply.status_code == 400, reply.get_json()
    assert reply.get_json()["id"] is None


def test_integration_budget_is_checked_before_workflow_is_created(tmp_path):
    _, client, setup, token, identity = _app(tmp_path, max_budget_tokens=0)
    before = len(setup.repository.list(identity.owner_id))
    reply = mcp(client, token, "tools/call", {"name": "workflow.create_from_template", "arguments": body()})
    assert reply.status_code == 403, reply.get_json()
    assert len(setup.repository.list(identity.owner_id)) == before


def test_revocation_during_create_authorization_blocks_new_workflow(tmp_path, monkeypatch):
    app, client, setup, token, identity = _app(tmp_path)
    before = len(setup.repository.list(identity.owner_id))
    authorizer = app.extensions["fleet"]["services"]["workflows"].repository.input_authorizer
    authorize = authorizer.authorize_references

    def revoke_then_authorize(*args, **kwargs):
        app.extensions["fleet"]["repositories"]["integration_identities"].revoke(identity.owner_id, identity.integration_id,
            expected_digest=identity.definition_digest, now=101)
        return authorize(*args, **kwargs)

    monkeypatch.setattr(authorizer, "authorize_references", revoke_then_authorize)
    reply = mcp(client, token, "tools/call", {"name": "workflow.create_from_template", "arguments": body()})
    assert reply.status_code == 401, reply.get_json()
    assert len(setup.repository.list(identity.owner_id)) == before


def test_same_client_token_different_valid_template_revision_conflicts(tmp_path):
    import json
    from hub.application.workflow_templates import RegisteredWorkflowTemplate
    from hub.domain.workflow_contract import parse_definition

    app, client, setup, token, identity = _app(tmp_path)
    catalog = app.extensions["fleet"]["services"]["workflows"].catalog
    changed = json.loads(setup.definition.snapshot)
    changed["revision"] = 2
    revision = parse_definition(json.dumps(changed).encode())
    catalog.templates[(identity.owner_id, "workspace-a", "fixture", 2)] = RegisteredWorkflowTemplate(identity.owner_id, "workspace-a", revision)
    args = {**body(), "client_token": "persistent-conflict"}
    first = mcp(client, token, "tools/call", {"name": "workflow.create_from_template", "arguments": args})
    conflict = mcp(client, token, "tools/call", {"name": "workflow.create_from_template", "arguments": {**args, "revision": 2}})
    assert first.status_code == 200
    assert conflict.status_code == 409, conflict.get_json()
    assert conflict.get_json()["error"]["message"] == "idempotency_conflict"
    assert len(setup.repository.list(identity.owner_id)) == 2


@pytest.mark.parametrize("raw", [b"{", b"[]", b"null", b'{"jsonrpc":"2.0","jsonrpc":"2.0"}', b'{"id":NaN}', b'{"id":1e999}', b'"\xff"'])
def test_malformed_json_is_rejected_without_workflow_mutation(tmp_path, raw):
    _, client, setup, token, identity = _app(tmp_path)
    reply = client.post("/api/platform/mcp", headers={"Authorization": "Bearer " + token}, data=raw, content_type="application/json")
    assert reply.status_code == 400 and "error" in reply.get_json()
    assert len(setup.repository.list(identity.owner_id)) == 1


@pytest.mark.parametrize("name", [None, "workflow.get", "workflow.create_from_template"])
def test_tool_name_header_is_required_and_matches_body(tmp_path, name):
    _, client, setup, token, identity = _app(tmp_path)
    headers = {"Authorization": "Bearer " + token, "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/call",
               "Accept": "application/json, text/event-stream"}
    if name is not None:
        headers["Mcp-Name"] = name
    reply = client.post("/api/platform/mcp", headers=headers, json={"jsonrpc": "2.0", "id": "names", "method": "tools/call",
        "params": {"name": "workflow.list_artifacts", "arguments": {"workflow_id": "workflow-a"},
                   "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}})
    assert reply.status_code == 400 and reply.get_json()["error"]["code"] == -32020
    assert len(setup.repository.list(identity.owner_id)) == 1


def test_cross_integration_and_scope_reduction_block_existing_workflow(tmp_path):
    app, client, _, token, identity = _app(tmp_path)
    created = mcp(client, token, "tools/call", {"name": "workflow.create_from_template", "arguments": body()})
    workflow = created.get_json()["result"]["structuredContent"]["workflow_id"]
    restrictions = ({}, {"workspace_ids": frozenset({"other"})}, {"template_ids": frozenset({"other"})},
                    {"scopes": frozenset({"workflow.list_artifacts"})})
    for index, fields in enumerate(restrictions):
        restricted_token = "restricted-" + str(index) * 32
        replacement = replace(identity, integration_id=f"other-{index}",
            token_digest=hashlib.sha256(restricted_token.encode()).hexdigest(), **fields)
        app.extensions["fleet"]["repositories"]["integration_identities"].register(replacement, now=101)
        assert mcp(client, restricted_token, "tools/call", {"name": "workflow.get", "arguments": {"workflow_id": workflow}}).status_code == 403
    listed = mcp(client, restricted_token, "tools/list", {}).get_json()["result"]["tools"]
    assert [tool["name"] for tool in listed] == ["workflow.list_artifacts"]


@pytest.mark.parametrize("limit", [0, 101, True, 1.0, "1", -1])
def test_artifact_limits_require_exact_integer_before_access(tmp_path, limit):
    _, client, _, token, _ = _app(tmp_path)
    reply = mcp(client, token, "tools/call", {"name": "workflow.list_artifacts", "arguments": {"workflow_id": "workflow-a", "limit": limit}})
    assert reply.status_code == 400, reply.get_json()


def test_disabled_gate_and_missing_bearer_never_reach_workflow(tmp_path):
    app, client, _, token, _ = _app(tmp_path)
    app.config["PLATFORM_FLEET_MCP_ENABLED"] = False
    assert mcp(client, token, "tools/list", {}).status_code == 503
    app.config["PLATFORM_FLEET_MCP_ENABLED"] = True
    assert client.post("/api/platform/mcp", json={}).status_code == 401


def test_oversized_mcp_json_is_bounded_before_workflow_creation(tmp_path):
    _, client, setup, token, identity = _app(tmp_path)
    response = client.post("/api/platform/mcp", headers={"Authorization": "Bearer " + token},
                           data=b'{"pad":"' + b"x" * 65536 + b'"}', content_type="application/json")
    assert response.status_code == 413
    assert len(setup.repository.list(identity.owner_id)) == 1


def test_empty_scopes_never_expand_to_default_permissions():
    identity = IntegrationIdentity.register("none", "owner", "x" * 32, template_ids={"fixture"}, workspace_ids={"workspace"}, scopes=())
    assert identity.scopes == frozenset()


def test_mcp_bearer_boundary_remains_separate_when_accounts_are_enabled(tmp_path):
    from hub.application.workflow_templates import RegisteredWorkflowTemplate, WorkflowTemplateCatalog
    from test_workflow_coordinator import make_setup
    setup = make_setup(tmp_path)
    identity, token = _identity("owner@example.test", "workspace-a")
    catalog = WorkflowTemplateCatalog((RegisteredWorkflowTemplate(identity.owner_id, "workspace-a", setup.definition),))
    config = FleetConfig.from_root(tmp_path, platform_enabled=True, platform_db=setup.repository.db_path,
        platform_workflows_enabled=True, platform_fleet_mcp_enabled=True, serve_frontend=False,
        accounts={"enabled": True, "origin": "https://fleet.example.test", "registration": "invite"})
    app = create_app(config, repositories={"workflow_templates": catalog, "integration_identities": (identity,)})
    client = app.test_client()
    assert mcp(client, token, "tools/list", {}).status_code == 200
    assert client.post("/api/platform/mcp", json={}).status_code == 401
