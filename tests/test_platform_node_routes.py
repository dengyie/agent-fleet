from pathlib import Path

from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.domain.platform_command import PlatformCommand


def _app(tmp_path: Path, *, enabled=True):
    return create_app(FleetConfig.from_root(
        tmp_path,
        ingest_token="ingest-only",
        dev_operator="owner-a@example.test",
        platform_enabled=enabled,
    ))


def _setup(tmp_path):
    app = _app(tmp_path)
    repo = app.extensions["fleet"]["platform_repository"]
    commands = app.extensions["fleet"]["platform_commands"]
    repo.upsert_node("owner-a@example.test", {
        "node_id": "node-a", "label": "A", "capabilities": {"workspace.read": True},
    })
    repo.upsert_node("owner-b@example.test", {
        "node_id": "node-b", "label": "B", "capabilities": {"workspace.read": True},
    })
    cred_a = repo.provision_node_credential("owner-a@example.test", "node-a", secret="a" * 40)["credential"]
    cred_b = repo.provision_node_credential("owner-b@example.test", "node-b", secret="b" * 40)["credential"]
    return app, repo, commands, cred_a, cred_b


def _command(command_id, node, owner):
    return PlatformCommand.create(
        command_id=command_id, target_node=node, owner_id=owner,
        action="workspace.read", resource_id="workspace-1",
        arguments={"path": "report.md"}, retry_class="read_only", expires_at=9_999_999_999,
    )


def test_platform_node_gate_and_identity_domains(tmp_path):
    disabled = _app(tmp_path / "disabled", enabled=False)
    # The node surface is POST-only; an unregistered platform path must still
    # be absent when the feature gate is off.
    assert disabled.test_client().get("/api/platform/v1/nodes/heartbeat").status_code == 404

    app, _, _, _, _ = _setup(tmp_path / "enabled")
    client = app.test_client()
    assert client.post("/api/platform/v1/nodes/heartbeat").status_code == 403
    assert client.post(
        "/api/platform/v1/nodes/heartbeat",
        headers={"X-Agent-Fleet-Token": "ingest-only"},
    ).status_code == 403
    assert client.post(
        "/api/platform/v1/nodes/heartbeat",
        headers={"Cf-Access-Authenticated-User-Email": "owner-a@example.test"},
    ).status_code == 403


def test_node_credential_is_one_time_and_rotation_revokes_old(tmp_path):
    app = _app(tmp_path)
    repo = app.extensions["fleet"]["platform_repository"]
    repo.upsert_node("owner-a@example.test", {"node_id": "node-a"})
    first = repo.provision_node_credential("owner-a@example.test", "node-a", secret="a" * 40)
    assert first["credential"] == "node-a:" + "a" * 40
    with app.test_request_context():
        assert repo.authenticate_node_credential("node-a", "a" * 40)["node_id"] == "node-a"
    second = repo.provision_node_credential("owner-a@example.test", "node-a", secret="b" * 40)
    assert repo.authenticate_node_credential("node-a", "a" * 40) is None
    assert repo.authenticate_node_credential("node-a", "b" * 40)["node_id"] == "node-a"
    assert first["credential"] not in str(second)


def test_operator_credential_provisioning_is_owner_scoped_and_one_time(tmp_path):
    app, repo, _, _, _ = _setup(tmp_path)
    client = app.test_client()
    created = client.post(
        "/api/platform/v1/nodes/node-a/credential",
        json={"secret": "c" * 40},
    )
    assert created.status_code == 200
    payload = created.get_json()
    assert payload["credential"] == "node-a:" + "c" * 40
    assert "secret_digest" not in created.get_data(as_text=True)
    assert repo.authenticate_node_credential("node-a", "c" * 40) is not None
    # DEV_OPERATOR is owner-a; provisioning an owner-b node is forbidden even
    # when the node id is known.
    forbidden = client.post("/api/platform/v1/nodes/node-b/credential", json={})
    assert forbidden.status_code == 409
    rotated = client.post("/api/platform/v1/nodes/node-a/credential", json={})
    assert rotated.status_code == 200
    assert repo.authenticate_node_credential("node-a", "c" * 40) is None


def test_node_poll_is_scoped_and_response_redacts_lease_and_owner(tmp_path):
    app, _, commands, cred_a, _ = _setup(tmp_path)
    commands.enqueue(_command("cmd-a", "node-a", "owner-a@example.test"), idempotency_key="a")
    commands.enqueue(_command("cmd-b", "node-b", "owner-b@example.test"), idempotency_key="b")
    client = app.test_client()
    response = client.post(
        "/api/platform/v1/nodes/poll", json={"worker_id": "worker-a", "node_id": "node-b"},
        headers={"X-Platform-Node-Credential": cred_a},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert [item["command_id"] for item in payload["commands"]] == ["cmd-a"]
    text = response.get_data(as_text=True)
    assert "owner_id" not in text
    assert "lease_owner" not in text
    assert "lease_until" not in text
    assert "cmd-b" not in text


def test_node_receipt_requires_worker_lease_and_owner_scope(tmp_path):
    app, _, commands, cred_a, _ = _setup(tmp_path)
    commands.enqueue(_command("cmd-a", "node-a", "owner-a@example.test"), idempotency_key="a")
    commands.enqueue(_command("cmd-owner-b", "node-a", "owner-b@example.test"), idempotency_key="owner-b")
    client = app.test_client()
    base = {"X-Platform-Node-Credential": cred_a}
    assert client.post("/api/platform/v1/nodes/receipts", json={
        "command_id": "cmd-a", "status": "succeeded", "result": {},
    }, headers=base).status_code == 400
    client.post("/api/platform/v1/nodes/poll", json={"worker_id": "worker-a"}, headers=base)
    wrong = client.post("/api/platform/v1/nodes/receipts", json={
        "command_id": "cmd-a", "status": "succeeded", "worker_id": "other-worker",
    }, headers=base)
    assert wrong.status_code == 409
    ok = client.post("/api/platform/v1/nodes/receipts", json={
        "command_id": "cmd-a", "status": "succeeded", "worker_id": "worker-a", "result": {"ok": True},
    }, headers=base)
    assert ok.status_code == 200
    assert ok.get_json()["receipt"] == {"command_id": "cmd-a", "status": "succeeded", "result": {"ok": True}}
    replay = client.post("/api/platform/v1/nodes/receipts", json={
        "command_id": "cmd-a", "status": "failed", "worker_id": "other-worker", "result": {"late": True},
    }, headers=base)
    assert replay.status_code == 200
    assert replay.get_json()["receipt"]["status"] == "succeeded"
    owner_mismatch = client.post("/api/platform/v1/nodes/receipts", json={
        "command_id": "cmd-owner-b", "status": "succeeded", "worker_id": "worker-a",
    }, headers=base)
    assert owner_mismatch.status_code == 403


def test_node_poll_and_receipts_enforce_bounded_lease_protocol(tmp_path):
    app, _, commands, cred_a, _ = _setup(tmp_path)
    commands.enqueue(_command("cmd-a", "node-a", "owner-a@example.test"), idempotency_key="a")
    client = app.test_client()
    headers = {"X-Platform-Node-Credential": cred_a}
    assert client.post("/api/platform/v1/nodes/poll", json={"limit": 0}, headers=headers).status_code == 400
    assert client.post("/api/platform/v1/nodes/poll", json={"lease_s": 3601}, headers=headers).status_code == 400
    assert client.post("/api/platform/v1/nodes/poll", json={"worker_id": "worker-a", "lease_s": 1}, headers=headers).status_code == 200
    intermediate = client.post("/api/platform/v1/nodes/receipts", json={
        "command_id": "cmd-a", "status": "running", "worker_id": "worker-a", "result": {"step": 1},
    }, headers=headers)
    assert intermediate.status_code == 200
    assert intermediate.get_json()["receipt"]["status"] == "running"


def test_node_heartbeat_validates_status_and_does_not_leak_credential(tmp_path):
    app, repo, _, cred_a, _ = _setup(tmp_path)
    client = app.test_client()
    bad = client.post("/api/platform/v1/nodes/heartbeat", json={"status": "unknown"}, headers={
        "X-Platform-Node-Credential": cred_a,
    })
    assert bad.status_code == 400
    good = client.post("/api/platform/v1/nodes/heartbeat", json={"status": "degraded"}, headers={
        "X-Platform-Node-Credential": cred_a,
    })
    assert good.status_code == 200
    assert good.get_json()["node"]["status"] == "degraded"
    body = good.get_data(as_text=True)
    assert "secret_digest" not in body and "salt" not in body and "a" * 40 not in body
    assert repo.get_node("owner-a@example.test", "node-a")["status"] == "degraded"
