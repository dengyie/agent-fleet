"""Machine-bound reporting only; no directory or execution permission API."""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import replace

import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig
from test_capability_reservations import setup_catalog
from test_capability_router import OWNER, WORKSPACE, offer_payload

PATH = "/api/platform/v1/nodes/capability-offers"


def app_setup(tmp_path, *, enabled=True):
    repository, platform, _, _, credential, _, _ = setup_catalog(tmp_path)
    config = FleetConfig.from_root(tmp_path, platform_enabled=True, platform_workflows_enabled=True,
        platform_capability_routing_enabled=enabled, platform_db=repository.workflows.db_path,
        dev_operator=OWNER, serve_frontend=False, ingest_token="ingest")
    app = create_app(config, repositories={"workflows": repository.workflows, "capabilities": repository,
        "workflow_clock": lambda: 100})
    app.config["TESTING"] = True
    headers = {"X-Platform-Node-Credential": credential["credential"]}
    return app, repository, platform, headers


def test_capability_gate_is_passive_default_off_and_depends_on_workflow_domain(tmp_path):
    before = {thread.ident for thread in threading.enumerate()}
    base = FleetConfig.from_root(tmp_path, platform_enabled=True, platform_workflows_enabled=True)
    assert not base.platform_capability_routing_enabled
    assert not FleetConfig.from_root(tmp_path, platform_enabled=True, platform_capability_routing_enabled=True).platform_capability_routing_enabled
    app = create_app(base)
    with sqlite3.connect(base.platform_db) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='capability_schema_meta'").fetchone() is None
    assert before == {thread.ident for thread in threading.enumerate()}
    with pytest.raises(ValueError, match="capability routing gate dependencies"):
        create_app(replace(base, platform_workflows_enabled=False, platform_capability_routing_enabled=True))


def test_report_derives_identity_and_returns_only_a_scoped_receipt(tmp_path):
    app, repository, platform, headers = app_setup(tmp_path)
    before = platform.get_node(OWNER, "node-a")["capabilities"]
    result = app.test_client().post(PATH, json=offer_payload(slots=1), headers=headers)
    assert result.status_code == 200
    assert result.json == {"ok": True, "offer": {"node_id": "node-a", "offer_revision": 1, "received_at": "1970-01-01T00:01:40Z"}}
    assert platform.get_node(OWNER, "node-a")["capabilities"] == before
    assert app.extensions["fleet"]["services"]["workflow_coordinator"].dispatch_enabled is False


@pytest.mark.parametrize("headers", [{}, {"X-Agent-Fleet-Token": "ingest"}, {"X-Runner-Credential": "node-a:secret"}])
def test_operator_and_other_machine_credentials_do_not_authorize_a_report(tmp_path, headers):
    app, _, _, _ = app_setup(tmp_path)
    result = app.test_client().post(PATH, json=offer_payload(slots=1), headers=headers)
    assert result.status_code in {401, 403}


@pytest.mark.parametrize("change", [{"node_id": "other"}, {"owner_id": "other"}, {"trusted": True}, {"readOnly": True}, {"os": {}}, {"health": []}])
def test_report_rejects_identity_overrides_untrusted_annotations_and_wrong_types(tmp_path, change):
    app, _, _, headers = app_setup(tmp_path)
    result = app.test_client().post(PATH, json=offer_payload(slots=1, **change), headers=headers)
    assert result.status_code == 400


def test_report_rejects_duplicate_keys_and_oversized_body(tmp_path):
    app, _, _, headers = app_setup(tmp_path)
    client = app.test_client()
    raw = json.dumps(offer_payload(slots=1))[:-1] + ',"slots":1}'
    result = client.post(PATH, data=raw, content_type="application/json", headers=headers)
    assert result.status_code == 400
    result = client.post(PATH, data=b" " * 65537, content_type="application/json", headers=headers)
    assert result.status_code == 413


@pytest.mark.parametrize("change", ["disable", "rotate"])
def test_report_refuses_credential_revoked_after_request_authentication(tmp_path, monkeypatch, change):
    app, repository, platform, headers = app_setup(tmp_path)
    original = repository.record_offer
    def race(*args, **kwargs):
        if change == "disable":
            with repository.workflows.transaction() as connection:
                connection.execute("UPDATE node_credentials SET enabled=0 WHERE owner_id=? AND node_id=?", (OWNER, "node-a"))
        else:
            platform.provision_node_credential(OWNER, "node-a", secret="changed" * 8)
        return original(*args, **kwargs)
    monkeypatch.setattr(repository, "record_offer", race)
    result = app.test_client().post(PATH, json=offer_payload(slots=1), headers=headers)
    assert result.status_code == 409
    assert result.json["error"] == "credential_revoked"


def test_gate_off_hides_report_route_without_changing_legacy_heartbeat(tmp_path):
    app, _, _, headers = app_setup(tmp_path, enabled=False)
    client = app.test_client()
    assert client.post(PATH, json=offer_payload(slots=1), headers=headers).status_code == 404
    assert client.post("/api/platform/v1/nodes/heartbeat", json={"status": "online"}, headers=headers).status_code == 200


def test_node_runtime_reports_over_real_http_then_executes_only_original_signed_binding(tmp_path):
    from hub.domain.capability_execution import CapabilityExecutionContext, RegisteredCapabilityAction
    from tools.platform.node_capabilities import LocalCapability, NodeCapabilityConfig, NodeCapabilityFacts
    from tools.platform.node_runtime import NodeRuntime, NodeRuntimeConfig
    from tools.transport import Transport
    from test_capability_dispatch import CAP, setup_dispatch
    from test_capability_router import definition
    from test_workflow_two_nodes import server

    fixture = setup_dispatch(tmp_path, action="tool.workspace.read")
    run_id = fixture.run()
    repository = fixture.repository
    config = FleetConfig.from_root(tmp_path, platform_enabled=True, platform_workflows_enabled=True,
        platform_capability_routing_enabled=True, platform_db=repository.workflows.db_path,
        dev_operator=OWNER, serve_frontend=False)
    config = replace(config, platform_command_signing_raw=fixture.private)
    app = create_app(config, repositories={"workflows": repository.workflows, "capabilities": repository,
        "workflow_clock": lambda: fixture.clock[0],
        "workflow_capability_actions": (RegisteredCapabilityAction("cli", CAP, "tool.workspace.read"),)})
    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    delivery.clock = delivery.repository.clock = lambda: fixture.clock[0]
    state = [NodeCapabilityFacts("linux", "x86_64", (("python", "3.10.21"),), 1000, 2000)]
    local_definition = definition(capability_id=CAP.id, retry_class="read_only")
    node_config = NodeCapabilityConfig(1, (LocalCapability(local_definition, ("tool.workspace.read",)),), (CAP,), 1)
    with server(app) as url:
        runtime = NodeRuntime(NodeRuntimeConfig(workspace_id=WORKSPACE, node_id="node-a", credential=fixture.credential,
            workspace_root=tmp_path / "node-files", journal_path=tmp_path / "node.db", hub_url=url,
            public_key=fixture.public, capabilities=frozenset({"workspace.read"}), capability_config=node_config),
            transport=Transport(mode="direct", attempts=1), clock=lambda: fixture.clock[0], capability_observer=lambda: state[0])
        try:
            (tmp_path / "node-files" / "report.txt").write_text("from original node", encoding="utf-8")
            receipt = runtime.report_capabilities()
            assert receipt.node_id == "node-a" and receipt.offer_revision == 1
            command = fixture.command(run_id)
            saved = delivery.enqueue(command, idempotency_key=command.command_id)
            assert CapabilityExecutionContext.from_command(saved).binding == fixture.admission.selection.binding
            assert runtime.poll_once() == {"ok": True, "commands": 1, "receipts": 1}
            saved = delivery.repository.get(command.command_id)
            assert saved["status"] == "succeeded"
            assert saved["result"]["result"]["content"] == "from original node"
            assert fixture.platform.get_node(OWNER, "node-a")["capabilities"] == {"legacy": True}
            state[0] = replace(state[0], software=(("python", "changed-version"),))
            changed = fixture.command(run_id, suffix="changed")
            delivery.enqueue(changed, idempotency_key=changed.command_id)
            result = runtime.poll_once()
            assert result == {"ok": True, "commands": 1, "receipts": 1,
                "capability_report_error": "capability_report_rejected"}
            saved = delivery.repository.get(changed.command_id)
            assert saved["status"] == "failed" and saved["result"]["error_code"] == "capability_changed"
            assert "content" not in saved["result"].get("result", {})
        finally:
            runtime.close_all()


@pytest.mark.parametrize("reservation", ["held", "isolated"])
@pytest.mark.parametrize("outcome", ["matched", "mismatch", "read_failed"])
def test_capability_postcheck_real_http_digest_is_evidence_only_and_journal_idempotent(tmp_path, monkeypatch, reservation, outcome):
    from hub.domain.capability_execution import CAPABILITY_ARGUMENT, CapabilityExecutionContext, RegisteredCapabilityAction
    from fleet_core.platform_command import verify_command
    from tools.platform.node_capabilities import LocalCapability, NodeCapabilityConfig, NodeCapabilityFacts
    from tools.platform.node_runtime import NodeRuntime, NodeRuntimeConfig
    from tools.transport import Transport
    from test_capability_dispatch import CAP, DIGEST_CAP, setup_dispatch
    from test_capability_router import definition
    from test_workflow_two_nodes import server

    fixture = setup_dispatch(tmp_path, digest_helper=True)
    run_id = fixture.run()
    repository = fixture.repository
    actions = (RegisteredCapabilityAction("cli", CAP, "tool.workspace.write"),
        RegisteredCapabilityAction("cli", DIGEST_CAP, "reconcile.workspace.digest"))
    config = FleetConfig.from_root(tmp_path, platform_enabled=True, platform_workflows_enabled=True,
        platform_capability_routing_enabled=True, platform_db=repository.workflows.db_path,
        platform_command_signing_raw=fixture.private, dev_operator=OWNER, serve_frontend=False)
    app = create_app(config, repositories={"workflows": repository.workflows, "capabilities": repository,
        "workflow_clock": lambda: fixture.clock[0], "workflow_capability_actions": actions})
    services = app.extensions["fleet"]["services"]
    delivery = services["platform_delivery"]
    delivery.clock = delivery.repository.clock = services["command_postcheck"].clock = lambda: fixture.clock[0]
    original = fixture.command(run_id)

    class LostWriteReceipt(Transport):
        def post_json(self, url, body, headers):
            if url.endswith("/nodes/receipts") and body.get("command_id") == original.command_id:
                return 503, {"ok": False}
            return super().post_json(url, body, headers)

    definitions = (definition(capability_id=CAP.id, retry_class=original.retry_class),
        definition(capability_id=DIGEST_CAP.id, retry_class="read_only"))
    local = NodeCapabilityConfig(1, (LocalCapability(definitions[0], (original.action,)),
        LocalCapability(definitions[1], ("reconcile.workspace.digest",))), (CAP, DIGEST_CAP), 1)
    facts = NodeCapabilityFacts("linux", "x86_64", (("python", "3.10.21"),), 1000, 2000)

    def snapshot():
        with repository.workflows.transaction(write=False) as connection:
            tables = ("runs", "workflow_runs", "workflow_steps", "workflow_dispatches", "workflow_budget_reservations",
                "capability_step_bindings", "capability_reservations")
            state = {table: tuple(tuple(row) for row in connection.execute("SELECT * FROM " + table)) for table in tables}
        return state, delivery.repository.get(original.command_id)

    with server(app) as url:
        node_config = NodeRuntimeConfig(workspace_id=WORKSPACE, node_id="node-a", credential=fixture.credential,
            workspace_root=tmp_path / "node-files", journal_path=tmp_path / "node.db", hub_url=url,
            public_key=fixture.public, capabilities=frozenset({"workspace.write", "reconcile.workspace.digest"}),
            capability_config=local)
        runtime = NodeRuntime(node_config, transport=LostWriteReceipt(mode="direct", attempts=1),
            clock=lambda: fixture.clock[0], capability_observer=lambda: facts)
        writes, reads = [], []
        real_write, real_digest = runtime.backend.write, runtime.backend.digest

        def write(path, content):
            writes.append(path)
            return real_write(path, content)

        def digest(path):
            reads.append(path)
            return real_digest(path)

        monkeypatch.setattr(runtime.backend, "write", write)
        monkeypatch.setattr(runtime.backend, "digest", digest)
        try:
            runtime.report_capabilities()
            saved = delivery.enqueue(original, idempotency_key=original.command_id)
            assert runtime.poll_once() == {"ok": True, "commands": 1, "receipts": 0}
            assert writes == ["report.txt"] and runtime.journal.get(original.command_id)["state"] == "succeeded"
            fixture.isolate_write(delivery, original.command_id, reservation=reservation)
            file = node_config.workspace_root / "report.txt"
            if outcome == "mismatch":
                file.write_text("changed after the write", encoding="utf-8")
            elif outcome == "read_failed":
                file.rename(file.with_suffix(".moved"))
            before = snapshot()
            client = app.test_client()
            route = "/api/platform/v1/commands/" + original.command_id + "/postcheck"
            responses = [client.post(route, json={"path": "attacker.txt", "node_id": "node-b"}), client.post(route)]
            assert all(response.status_code == 202 for response in responses)
            check_id = responses[0].json["postcheck"]["check_command_id"]
            assert responses[1].json["postcheck"]["check_command_id"] == check_id
            check = delivery.repository.get(check_id)
            assert verify_command(check, fixture.public)
            assert CapabilityExecutionContext.from_command(check).capability_id == DIGEST_CAP.id
            assert check["target_node"] == saved["target_node"] and check["run_id"] == run_id
            assert check["retry_class"] == "read_only" and set(check["arguments"]) == {"path", CAPABILITY_ARGUMENT}
            assert runtime.poll_once() == {"ok": True, "commands": 1, "receipts": 1}
            response = client.get(route)
            assert response.status_code == 200
            assert response.json["postcheck"]["state"] == ("remains_unknown" if outcome == "read_failed" else outcome)
            if outcome == "read_failed":
                assert response.json["postcheck"]["result"]["error_code"] == "not_found"
            for public in [*responses, response, client.post(route), client.get(route)]:
                wire = public.get_data(as_text=True)
                assert all(secret not in wire for secret in ("content", CAPABILITY_ARGUMENT, "expected_sha256", "fixed output"))
            assert client.get(route, headers={"Cf-Access-Authenticated-User-Email": "other@example.test"}).status_code == 404
            assert snapshot() == before
            assert writes == ["report.txt"] and reads == ["report.txt"]
            assert runtime.poll_once() == {"ok": True, "commands": 0, "receipts": 0}
            runtime.close_all()
            restarted = NodeRuntime(node_config, transport=Transport(mode="direct", attempts=1),
                clock=lambda: fixture.clock[0], capability_observer=lambda: facts)
            try:
                def unexpected_io(*args):
                    pytest.fail("completed journal entries must not repeat file I/O")
                monkeypatch.setattr(restarted.backend, "digest", unexpected_io)
                monkeypatch.setattr(restarted.backend, "write", unexpected_io)
                assert restarted.client.handle(check)["status"] == ("failed" if outcome == "read_failed" else "succeeded")
                assert restarted.client.handle(saved)["status"] == "succeeded"
                assert snapshot() == before
            finally:
                restarted.close_all()
            with repository.workflows.transaction(write=False) as connection:
                assert connection.execute("SELECT COUNT(*) FROM platform_commands").fetchone()[0] == 2
                assert connection.execute("SELECT COUNT(*) FROM platform_command_postchecks").fetchone()[0] == 1
                audit = connection.execute("SELECT outcome FROM platform_command_reconciliations WHERE command_id=?",
                    (original.command_id,)).fetchall()
            assert [row[0] for row in audit] == ["confirmed_succeeded" if outcome == "matched" else "remains_unknown"]
        finally:
            runtime.close_all()
