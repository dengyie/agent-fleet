from pathlib import Path

from flask import Request

from hub.application.run_worker_service import LocalRunWorkerService
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.domain.platform_command import PlatformCommand
from tools.platform.backends.directory import DirectoryBackend
from tools.platform.node_client import NodeClient
from tools.platform.node_executor import NodeToolExecutor
from tools.platform.journal import NodeJournal
from tools.platform.providers.base import ModelResponse


OWNER = "owner@example.test"
PNG = b"\x89PNG\r\n\x1a\nfixture-screenshot"


def _app(tmp_path: Path, *, browser=False):
    return create_app(FleetConfig.from_root(
        tmp_path, ingest_token="ingest", dev_operator=OWNER,
        platform_enabled=True, platform_browser_enabled=browser,
    ))


class FlaskTransport:
    """Injected in-process transport; no network socket is opened."""

    def __init__(self, client):
        self.client = client
        self.requests = []

    def post_json(self, url, body, headers):
        path = "/" + url.split("/", 3)[-1]
        self.requests.append((path, dict(body), dict(headers)))
        response = self.client.post(path, json=body, headers=headers)
        return response.status_code, response.get_json()

    def post_bytes(self, url, body, headers):
        path = "/" + url.split("/", 3)[-1]
        self.requests.append((path, bytes(body), dict(headers)))
        response = self.client.post(path, data=body, headers=headers)
        return response.status_code, response.get_json()


def test_worker_node_http_receipt_closes_remote_run_without_local_side_effect(tmp_path):
    app = _app(tmp_path)
    repo = app.extensions["fleet"]["platform_repository"]
    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    workspace_root = tmp_path / "remote-workspace"
    workspace_root.mkdir()
    (workspace_root / "report.md").write_text("from-node", encoding="utf-8")
    repo.upsert_node(OWNER, {"node_id": "node-http", "label": "HTTP node"})
    repo.provision_node_credential(OWNER, "node-http", secret="n" * 40)
    repo.upsert_model(OWNER, {"profile_id": "model", "provider": "deterministic", "model": "test"})
    repo.upsert_workspace(OWNER, {"workspace_id": "remote", "root_path": str(workspace_root)})
    conversation = repo.create_conversation(OWNER, "conv-http", title="", workspace_id="remote")
    repo.append_turn(
        OWNER, conversation["conversation_id"], "msg-http", "run-http",
        text="read report", client_token="http-e2e",
        config_snapshot={
            "workspace_id": "remote", "model_profile_id": "model",
            "execution_node_id": "node-http",
        }, now=1,
    )

    calls = []

    def executor(command):
        calls.append(command["command_id"])
        return NodeToolExecutor(DirectoryBackend(workspace_root))(command)

    journal = NodeJournal(tmp_path / "node-journal.db")
    journal.init()
    transport = FlaskTransport(app.test_client())
    node = NodeClient(
        journal, executor=executor, node_id="node-http",
        credential="node-http:" + "n" * 40,
        hub_url="https://hub.invalid", transport=transport,
        worker_id="node-worker",
    )

    class Provider:
        def __init__(self):
            self.calls = 0

        def complete(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(kind="tool_call", tool="workspace.read", arguments={"path": "report.md"})
            return ModelResponse(kind="final", text="node read complete")

    class DeliveryBridge:
        def enqueue(self, command, *, idempotency_key=None):
            return delivery.enqueue(command, idempotency_key=idempotency_key)

        def wait_for_receipt(self, command_id, *, timeout_s=30.0):
            # A real Node loop would run independently. The bridge advances
            # the injected fixture once before the Hub performs bounded wait.
            node.poll_once()
            return delivery.wait_for_receipt(command_id, timeout_s=timeout_s, poll_interval_s=0.01)

    worker = LocalRunWorkerService(
        repo, app.extensions["fleet"]["services"]["run_events"],
        worker_id="hub-worker", provider_factory=lambda profile: Provider(),
        remote_execution_enabled=True, remote_delivery=DeliveryBridge(),
    )
    result = worker.run_once(OWNER)
    assert result["state"] == "succeeded"
    assert calls == ["run-http:step:1"]
    assert repo.get_run(OWNER, "run-http")["state"] == "succeeded"
    assert not (tmp_path / "assistant-report.md").exists()
    events = app.extensions["fleet"]["services"]["run_events"].list(OWNER, "run-http")["events"]
    assert [event["kind"] for event in events] == [
        "run_started", "tool_call", "node_command_queued", "node_receipt",
        "tool_result", "run_finished",
    ]
    assert [path for path, _, _ in transport.requests] == [
        "/api/platform/v1/nodes/poll", "/api/platform/v1/nodes/receipts",
    ]
    command_row = delivery.repository.get("run-http:step:1")
    assert command_row["status"] == "succeeded"

    # A duplicate delivery is absorbed by NodeJournal; the backend is not
    # called a second time and the terminal Hub receipt remains unchanged.
    duplicate = node.handle({
        "command_id": "run-http:step:1", "target_node": "node-http",
        "action": "tool.workspace.read", "resource_id": "remote",
        "arguments": {"path": "report.md"}, "retry_class": "read_only",
        "expires_at": 9_999_999_999,
        "args_hash": PlatformCommand.create(
            command_id="tmp", target_node="node-http", action="tool.workspace.read",
            resource_id="remote", arguments={"path": "report.md"},
            retry_class="read_only", expires_at=9_999_999_999,
        ).args_digest,
        "run_id": "run-http", "grant_id": None, "signature": None,
    })
    assert duplicate["status"] == "succeeded"
    assert calls == ["run-http:step:1"]


def test_browser_screenshot_uses_node_ticket_and_artifact_metadata(tmp_path):
    app = _app(tmp_path, browser=True)
    repo = app.extensions["fleet"]["platform_repository"]
    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    repo.upsert_node(OWNER, {"node_id": "node-browser", "label": "Browser"})
    credential = repo.provision_node_credential(
        OWNER, "node-browser", secret="b" * 40)["credential"]
    command = PlatformCommand.create(
        command_id="run-browser:step:1", target_node="node-browser",
        owner_id=OWNER, action="tool.browser.screenshot", resource_id="workspace-browser",
        arguments={"session_id": "session-opaque"}, retry_class="manual_only",
        expires_at=9_999_999_999, run_id="run-browser",
    )
    delivery.enqueue(command, idempotency_key=command.command_id)
    journal = NodeJournal(tmp_path / "browser-journal.db")
    journal.init()
    transport = FlaskTransport(app.test_client())
    node = NodeClient(
        journal, executor=lambda _command: {
            "state": "succeeded", "result": {"result": PNG},
        }, node_id="node-browser", credential=credential,
        hub_url="https://hub.invalid", transport=transport,
        worker_id="browser-worker",
    )

    result = node.poll_once()

    assert result == {"ok": True, "commands": 1, "receipts": 1}
    receipt = delivery.repository.get(command.command_id)
    assert receipt["status"] == "succeeded"
    assert receipt["result"]["result"]["artifact"]["content_type"] == "image/png"
    assert "result" not in receipt["result"]["result"]
    journal_row = journal.get(command.command_id)
    assert "result" not in journal_row["result"]["result"]
    artifact = app.extensions["fleet"]["services"]["platform_artifacts"].list(
        OWNER, "workspace-browser")
    assert len(artifact) == 1
    assert app.extensions["fleet"]["services"]["platform_artifacts"].read(
        OWNER, "workspace-browser", artifact[0]["artifact_id"]) == PNG
    paths = [item[0] for item in transport.requests]
    assert paths == [
        "/api/platform/v1/nodes/poll",
        "/api/platform/v1/nodes/browser-artifact-tickets",
        "/api/platform/v1/nodes/browser-artifact-tickets/" +
        transport.requests[2][0].rsplit("/", 2)[-2] + "/content",
        "/api/platform/v1/nodes/receipts",
    ]
    ticket_headers = transport.requests[1][2]
    upload_headers = transport.requests[2][2]
    assert ticket_headers["X-Platform-Node-Credential"] == credential
    assert upload_headers["X-Platform-Node-Credential"] == credential
    assert upload_headers["X-Platform-Worker-ID"] == "browser-worker"
    assert "X-Agent-Fleet-Token" not in ticket_headers
    assert "X-Agent-Fleet-Token" not in upload_headers




def test_browser_upload_reads_a_bounded_stream(tmp_path, monkeypatch):
    app = _app(tmp_path, browser=True)
    repo = app.extensions["fleet"]["platform_repository"]
    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    repo.upsert_node(OWNER, {"node_id": "node-upload", "label": "Browser"})
    credential = repo.provision_node_credential(
        OWNER, "node-upload", secret="u" * 40)["credential"]
    command = PlatformCommand.create(
        command_id="run-upload:step:1", target_node="node-upload", owner_id=OWNER,
        action="tool.browser.screenshot", resource_id="workspace-upload",
        arguments={"session_id": "session-opaque"}, retry_class="manual_only",
        expires_at=9_999_999_999, run_id="run-upload",
    )
    delivery.enqueue(command, idempotency_key=command.command_id)
    client = app.test_client()
    headers = {"X-Platform-Node-Credential": credential}
    assert client.post(
        "/api/platform/v1/nodes/poll", json={"worker_id": "upload-worker"},
        headers=headers,
    ).status_code == 200
    ticket_response = client.post(
        "/api/platform/v1/nodes/browser-artifact-tickets", json={
            "command_id": command.command_id, "worker_id": "upload-worker",
            "idempotency_key": command.command_id + ":screenshot",
        }, headers=headers,
    )
    assert ticket_response.status_code == 200
    ticket = ticket_response.get_json()["ticket"]

    def fail_get_data(self, *args, **kwargs):
        raise AssertionError("upload route must use a bounded request stream")

    monkeypatch.setattr(Request, "get_data", fail_get_data)
    uploaded = client.post(
        "/api/platform/v1/nodes/browser-artifact-tickets/"
        + ticket["ticket_id"] + "/content",
        data=PNG,
        headers={
            **headers,
            "Content-Type": "image/png",
            "X-Platform-Artifact-Upload-Token": ticket["upload_token"],
            "X-Platform-Command-ID": command.command_id,
            "X-Platform-Worker-ID": "upload-worker",
            "X-Platform-Artifact-Idempotency-Key": command.command_id + ":screenshot",
        },
    )
    assert uploaded.status_code == 200


def test_browser_upload_rejects_wrong_credential_and_invalid_png(tmp_path):
    app = _app(tmp_path, browser=True)
    repo = app.extensions["fleet"]["platform_repository"]
    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    repo.upsert_node(OWNER, {"node_id": "node-negative", "label": "Browser"})
    credential = repo.provision_node_credential(
        OWNER, "node-negative", secret="v" * 40)["credential"]
    command = PlatformCommand.create(
        command_id="run-negative:step:1", target_node="node-negative", owner_id=OWNER,
        action="tool.browser.screenshot", resource_id="workspace-negative",
        arguments={"session_id": "session-opaque"}, retry_class="manual_only",
        expires_at=9_999_999_999, run_id="run-negative",
    )
    delivery.enqueue(command, idempotency_key=command.command_id)
    client = app.test_client()
    headers = {"X-Platform-Node-Credential": credential}
    assert client.post(
        "/api/platform/v1/nodes/poll", json={"worker_id": "negative-worker"},
        headers=headers,
    ).status_code == 200
    ticket_response = client.post(
        "/api/platform/v1/nodes/browser-artifact-tickets", json={
            "command_id": command.command_id, "worker_id": "negative-worker",
            "idempotency_key": command.command_id + ":screenshot",
        }, headers=headers,
    )
    ticket = ticket_response.get_json()["ticket"]
    upload_path = "/api/platform/v1/nodes/browser-artifact-tickets/" + ticket["ticket_id"] + "/content"
    upload_headers = {
        "Content-Type": "image/png",
        "X-Platform-Artifact-Upload-Token": ticket["upload_token"],
        "X-Platform-Command-ID": command.command_id,
        "X-Platform-Worker-ID": "negative-worker",
        "X-Platform-Artifact-Idempotency-Key": command.command_id + ":screenshot",
    }
    wrong_credential = client.post(
        upload_path, data=PNG,
        headers={**upload_headers, "X-Platform-Node-Credential": "node-negative:" + "w" * 40},
    )
    assert wrong_credential.status_code == 403
    invalid_png = client.post(
        upload_path, data=b"not-png",
        headers={**upload_headers, "X-Platform-Node-Credential": credential},
    )
    assert invalid_png.status_code == 415
    wrong_mime = client.post(
        upload_path, data=PNG,
        headers={**upload_headers, "Content-Type": "application/octet-stream",
                "X-Platform-Node-Credential": credential},
    )
    assert wrong_mime.status_code == 415


def test_browser_upload_rejects_oversized_body_before_artifact_write(tmp_path):
    app = _app(tmp_path, browser=True)
    repo = app.extensions["fleet"]["platform_repository"]
    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    repo.upsert_node(OWNER, {"node_id": "node-large", "label": "Browser"})
    credential = repo.provision_node_credential(
        OWNER, "node-large", secret="l" * 40)["credential"]
    command = PlatformCommand.create(
        command_id="run-large:step:1", target_node="node-large", owner_id=OWNER,
        action="tool.browser.screenshot", resource_id="workspace-large",
        arguments={"session_id": "session-opaque"}, retry_class="manual_only",
        expires_at=9_999_999_999, run_id="run-large",
    )
    delivery.enqueue(command, idempotency_key=command.command_id)
    client = app.test_client()
    headers = {"X-Platform-Node-Credential": credential}
    assert client.post(
        "/api/platform/v1/nodes/poll", json={"worker_id": "large-worker"},
        headers=headers,
    ).status_code == 200
    ticket = client.post(
        "/api/platform/v1/nodes/browser-artifact-tickets", json={
            "command_id": command.command_id, "worker_id": "large-worker",
            "idempotency_key": command.command_id + ":screenshot",
        }, headers=headers,
    ).get_json()["ticket"]
    response = client.post(
        "/api/platform/v1/nodes/browser-artifact-tickets/"
        + ticket["ticket_id"] + "/content",
        data=b"\x89PNG\r\n\x1a\n" + b"x" * (256 * 1024),
        headers={
            **headers,
            "Content-Type": "image/png",
            "X-Platform-Artifact-Upload-Token": ticket["upload_token"],
            "X-Platform-Command-ID": command.command_id,
            "X-Platform-Worker-ID": "upload-worker",
            "X-Platform-Artifact-Idempotency-Key": command.command_id + ":screenshot",
        },
    )
    assert response.status_code == 413
    assert response.get_json()["error"] == "artifact_too_large"
    assert app.extensions["fleet"]["services"]["platform_artifacts"].list(
        OWNER, "workspace-large") == []


def test_browser_artifact_ticket_rejects_wrong_command_action_worker_and_lease(tmp_path):
    app = _app(tmp_path, browser=True)
    repo = app.extensions["fleet"]["platform_repository"]
    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    repo.upsert_node(OWNER, {"node_id": "node-ticket", "label": "Browser"})
    credential = repo.provision_node_credential(
        OWNER, "node-ticket", secret="t" * 40)["credential"]
    screenshot = PlatformCommand.create(
        command_id="run-ticket:step:1", target_node="node-ticket", owner_id=OWNER,
        action="tool.browser.screenshot", resource_id="workspace-ticket",
        arguments={"session_id": "session-opaque"}, retry_class="manual_only",
        expires_at=9_999_999_999, run_id="run-ticket",
    )
    wrong_action = PlatformCommand.create(
        command_id="run-ticket:step:2", target_node="node-ticket", owner_id=OWNER,
        action="tool.browser.snapshot", resource_id="workspace-ticket",
        arguments={"session_id": "session-opaque"}, retry_class="manual_only",
        expires_at=9_999_999_999, run_id="run-ticket",
    )
    delivery.enqueue(screenshot, idempotency_key=screenshot.command_id)
    delivery.enqueue(wrong_action, idempotency_key=wrong_action.command_id)
    client = app.test_client()
    headers = {"X-Platform-Node-Credential": credential}
    assert client.post(
        "/api/platform/v1/nodes/poll", json={"worker_id": "ticket-worker"},
        headers=headers,
    ).status_code == 200
    assert client.post(
        "/api/platform/v1/nodes/poll", json={"worker_id": "other-worker"},
        headers=headers,
    ).status_code == 200

    def issue(command_id, worker_id):
        return client.post(
            "/api/platform/v1/nodes/browser-artifact-tickets", json={
                "command_id": command_id, "worker_id": worker_id,
                "idempotency_key": command_id + ":screenshot",
            }, headers=headers,
        )

    assert issue("missing-command", "ticket-worker").status_code == 404
    assert issue(wrong_action.command_id, "other-worker").status_code == 400
    assert issue(screenshot.command_id, "other-worker").status_code == 409
    assert issue(wrong_action.command_id, "ticket-worker").status_code == 400

    commands = delivery.repository
    conn = commands._connect()
    try:
        conn.execute(
            "UPDATE platform_commands SET lease_until=0 WHERE command_id=?",
            (screenshot.command_id,),
        )
    finally:
        conn.close()
    expired_lease = issue(screenshot.command_id, "ticket-worker")
    assert expired_lease.status_code == 409
    assert expired_lease.get_json()["error"] == "artifact_lease_expired"

    second = PlatformCommand.create(
        command_id="run-ticket:step:3", target_node="node-ticket", owner_id=OWNER,
        action="tool.browser.screenshot", resource_id="workspace-ticket",
        arguments={"session_id": "session-opaque"}, retry_class="manual_only",
        expires_at=9_999_999_999, run_id="run-ticket",
    )
    delivery.enqueue(second, idempotency_key=second.command_id)
    client.post("/api/platform/v1/nodes/poll", json={"worker_id": "ticket-worker"}, headers=headers)
    connection = commands._connect()
    try:
        connection.execute(
            "UPDATE platform_commands SET expires_at=0 WHERE command_id=?",
            (second.command_id,),
        )
    finally:
        connection.close()
    expired_command = issue(second.command_id, "ticket-worker")
    assert expired_command.status_code == 409
    assert expired_command.get_json()["error"] == "artifact_command_expired"


def test_browser_artifact_upload_duplicate_and_concurrent_replay(tmp_path):
    app = _app(tmp_path, browser=True)
    repo = app.extensions["fleet"]["platform_repository"]
    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    repo.upsert_node(OWNER, {"node_id": "node-replay", "label": "Browser"})
    credential = repo.provision_node_credential(
        OWNER, "node-replay", secret="r" * 40)["credential"]
    command = PlatformCommand.create(
        command_id="run-replay:step:1", target_node="node-replay", owner_id=OWNER,
        action="tool.browser.screenshot", resource_id="workspace-replay",
        arguments={"session_id": "session-opaque"}, retry_class="manual_only",
        expires_at=9_999_999_999, run_id="run-replay",
    )
    delivery.enqueue(command, idempotency_key=command.command_id)
    client = app.test_client()
    headers = {"X-Platform-Node-Credential": credential}
    assert client.post(
        "/api/platform/v1/nodes/poll", json={"worker_id": "replay-worker"},
        headers=headers,
    ).status_code == 200
    ticket = client.post(
        "/api/platform/v1/nodes/browser-artifact-tickets", json={
            "command_id": command.command_id, "worker_id": "replay-worker",
            "idempotency_key": command.command_id + ":screenshot",
        }, headers=headers,
    ).get_json()["ticket"]
    upload_path = "/api/platform/v1/nodes/browser-artifact-tickets/" + ticket["ticket_id"] + "/content"
    upload_headers = {
        **headers, "Content-Type": "image/png",
        "X-Platform-Artifact-Upload-Token": ticket["upload_token"],
        "X-Platform-Command-ID": command.command_id,
        "X-Platform-Worker-ID": "replay-worker",
        "X-Platform-Artifact-Idempotency-Key": command.command_id + ":screenshot",
    }
    repository = app.extensions["fleet"]["repositories"]["browser"]
    repository.begin_artifact_upload(
        ticket["ticket_id"], ticket["upload_token"], node_id="node-replay",
        command_id=command.command_id,
        idempotency_key=command.command_id + ":screenshot",
    )
    concurrent = client.post(upload_path, data=PNG, headers=upload_headers)
    assert concurrent.status_code == 409
    assert concurrent.get_json()["error"] == "artifact_upload_in_progress"
    repository.reset_artifact_upload(ticket["ticket_id"])
    wrong_command = client.post(
        upload_path, data=PNG,
        headers={**upload_headers, "X-Platform-Command-ID": "other-command"},
    )
    assert wrong_command.status_code == 404
    assert wrong_command.get_json()["error"] == "command_not_found"

    uploaded = client.post(upload_path, data=PNG, headers=upload_headers)
    assert uploaded.status_code == 200
    consumed = client.post(
        "/api/platform/v1/nodes/browser-artifact-tickets", json={
            "command_id": command.command_id, "worker_id": "replay-worker",
            "idempotency_key": command.command_id + ":screenshot",
        }, headers=headers,
    ).get_json()["ticket"]
    assert consumed["state"] == "consumed"
    assert "upload_token" not in consumed
    receipt = client.post(
        "/api/platform/v1/nodes/receipts", json={
            "command_id": command.command_id, "status": "succeeded",
            "worker_id": "replay-worker", "result": {},
        }, headers=headers,
    )
    assert receipt.status_code == 200
    replayed = client.post(upload_path, data=PNG, headers=upload_headers)
    assert replayed.status_code == 200
    assert replayed.get_json()["artifact"]["artifact_id"] == uploaded.get_json()["artifact"]["artifact_id"]

    expiring_command = PlatformCommand.create(
        command_id="run-replay:step:2", target_node="node-replay", owner_id=OWNER,
        action="tool.browser.screenshot", resource_id="workspace-replay",
        arguments={"session_id": "session-opaque"}, retry_class="manual_only",
        expires_at=9_999_999_999, run_id="run-replay",
    )
    delivery.enqueue(expiring_command, idempotency_key=expiring_command.command_id)
    client.post(
        "/api/platform/v1/nodes/poll", json={"worker_id": "replay-worker"},
        headers=headers,
    )
    expiring_ticket = client.post(
        "/api/platform/v1/nodes/browser-artifact-tickets", json={
            "command_id": expiring_command.command_id, "worker_id": "replay-worker",
            "idempotency_key": expiring_command.command_id + ":screenshot",
        }, headers=headers,
    ).get_json()["ticket"]
    browser_repo = app.extensions["fleet"]["repositories"]["browser"]
    conn = browser_repo._connect()
    try:
        conn.execute(
            "UPDATE browser_artifact_tickets SET expires_at=0 WHERE ticket_id=?",
            (expiring_ticket["ticket_id"],),
        )
    finally:
        conn.close()
    expired = client.post(
        "/api/platform/v1/nodes/browser-artifact-tickets/"
        + expiring_ticket["ticket_id"] + "/content", data=PNG,
        headers={
            **headers, "Content-Type": "image/png",
            "X-Platform-Artifact-Upload-Token": expiring_ticket["upload_token"],
            "X-Platform-Command-ID": expiring_command.command_id,
            "X-Platform-Worker-ID": "replay-worker",
            "X-Platform-Artifact-Idempotency-Key": expiring_command.command_id + ":screenshot",
        },
    )
    assert expired.status_code == 409
    assert expired.get_json()["error"] == "artifact_ticket_expired"


def test_browser_upload_rejects_late_worker_after_lease_expiry(tmp_path):
    app = _app(tmp_path, browser=True)
    repo = app.extensions["fleet"]["platform_repository"]
    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    repo.upsert_node(OWNER, {"node_id": "node-late", "label": "Browser"})
    credential = repo.provision_node_credential(
        OWNER, "node-late", secret="z" * 40)["credential"]
    command = PlatformCommand.create(
        command_id="run-late:step:1", target_node="node-late", owner_id=OWNER,
        action="tool.browser.screenshot", resource_id="workspace-late",
        arguments={"session_id": "session-opaque"}, retry_class="manual_only",
        expires_at=9_999_999_999, run_id="run-late",
    )
    delivery.enqueue(command, idempotency_key=command.command_id)
    client = app.test_client()
    headers = {"X-Platform-Node-Credential": credential}
    client.post("/api/platform/v1/nodes/poll", json={"worker_id": "late-worker"}, headers=headers)
    ticket = client.post(
        "/api/platform/v1/nodes/browser-artifact-tickets", json={
            "command_id": command.command_id, "worker_id": "late-worker",
            "idempotency_key": command.command_id + ":screenshot",
        }, headers=headers,
    ).get_json()["ticket"]
    conn = delivery.repository._connect()
    try:
        conn.execute(
            "UPDATE platform_commands SET lease_until=0 WHERE command_id=?",
            (command.command_id,),
        )
    finally:
        conn.close()
    response = client.post(
        "/api/platform/v1/nodes/browser-artifact-tickets/"
        + ticket["ticket_id"] + "/content", data=PNG,
        headers={
            **headers, "Content-Type": "image/png",
            "X-Platform-Artifact-Upload-Token": ticket["upload_token"],
            "X-Platform-Command-ID": command.command_id,
            "X-Platform-Worker-ID": "late-worker",
            "X-Platform-Artifact-Idempotency-Key": command.command_id + ":screenshot",
        },
    )
    assert response.status_code == 409
    assert response.get_json()["error"] == "artifact_lease_expired"
    assert app.extensions["fleet"]["services"]["platform_artifacts"].list(
        OWNER, "workspace-late") == []


def test_browser_upload_hash_mismatch_resets_ticket_and_deletes_artifact(tmp_path, monkeypatch):
    app = _app(tmp_path, browser=True)
    repo = app.extensions["fleet"]["platform_repository"]
    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    repo.upsert_node(OWNER, {"node_id": "node-cleanup", "label": "Browser"})
    credential = repo.provision_node_credential(
        OWNER, "node-cleanup", secret="c" * 40)["credential"]
    command = PlatformCommand.create(
        command_id="run-cleanup:step:1", target_node="node-cleanup", owner_id=OWNER,
        action="tool.browser.screenshot", resource_id="workspace-cleanup",
        arguments={"session_id": "session-opaque"}, retry_class="manual_only",
        expires_at=9_999_999_999, run_id="run-cleanup",
    )
    delivery.enqueue(command, idempotency_key=command.command_id)
    client = app.test_client()
    headers = {"X-Platform-Node-Credential": credential}
    client.post("/api/platform/v1/nodes/poll", json={"worker_id": "cleanup-worker"}, headers=headers)
    ticket = client.post(
        "/api/platform/v1/nodes/browser-artifact-tickets", json={
            "command_id": command.command_id, "worker_id": "cleanup-worker",
            "idempotency_key": command.command_id + ":screenshot",
        }, headers=headers,
    ).get_json()["ticket"]
    store = app.extensions["fleet"]["services"]["platform_artifacts"]
    original_put = store.put_bytes
    written = []

    def mismatched_put(*args, **kwargs):
        artifact = original_put(*args, **kwargs)
        written.append(artifact["artifact_id"])
        return {**artifact, "sha256": "0" * 64}

    monkeypatch.setattr(store, "put_bytes", mismatched_put)
    response = client.post(
        "/api/platform/v1/nodes/browser-artifact-tickets/"
        + ticket["ticket_id"] + "/content", data=PNG,
        headers={
            **headers, "Content-Type": "image/png",
            "X-Platform-Artifact-Upload-Token": ticket["upload_token"],
            "X-Platform-Command-ID": command.command_id,
            "X-Platform-Worker-ID": "cleanup-worker",
            "X-Platform-Artifact-Idempotency-Key": command.command_id + ":screenshot",
        },
    )
    assert response.status_code == 503
    browser_repo = app.extensions["fleet"]["repositories"]["browser"]
    row = browser_repo._connect()
    try:
        state = row.execute(
            "SELECT state FROM browser_artifact_tickets WHERE ticket_id=?",
            (ticket["ticket_id"],),
        ).fetchone()[0]
    finally:
        row.close()
    assert state == "issued"
    assert store.get(OWNER, "workspace-cleanup", written[0]) is None


def test_browser_session_register_and_close_is_command_bound(tmp_path):
    app = _app(tmp_path, browser=True)
    repo = app.extensions["fleet"]["platform_repository"]
    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    repo.upsert_node(OWNER, {"node_id": "node-session", "label": "Browser"})
    credential = repo.provision_node_credential(
        OWNER, "node-session", secret="s" * 40)["credential"]
    open_command = PlatformCommand.create(
        command_id="run-session:step:1", target_node="node-session", owner_id=OWNER,
        action="tool.browser.open", resource_id="workspace-session",
        arguments={"url": "http://localhost:3000"}, retry_class="manual_only",
        expires_at=9_999_999_999, run_id="run-session",
    )
    delivery.enqueue(open_command, idempotency_key=open_command.command_id)
    client = app.test_client()
    headers = {"X-Platform-Node-Credential": credential}
    polled = client.post(
        "/api/platform/v1/nodes/poll",
        json={"worker_id": "session-worker"}, headers=headers,
    )
    assert polled.status_code == 200
    session_id = "opaque-session-id-1234"
    registered = client.post(
        "/api/platform/v1/nodes/browser-sessions", json={
            "command_id": open_command.command_id, "worker_id": "session-worker",
            "session_id": session_id, "backend": "cdp_local",
        }, headers=headers,
    )
    assert registered.status_code == 200
    session = app.extensions["fleet"]["repositories"]["browser"].get_session(
        OWNER, session_id)
    assert session["workspace_id"] == "workspace-session"
    assert session["run_id"] == "run-session"
    assert session["node_id"] == "node-session"
    assert session["state"] == "open"

    close_command = PlatformCommand.create(
        command_id="run-session:step:2", target_node="node-session", owner_id=OWNER,
        action="tool.browser.close", resource_id="workspace-session",
        arguments={"session_id": session_id}, retry_class="manual_only",
        expires_at=9_999_999_999, run_id="run-session",
    )
    delivery.enqueue(close_command, idempotency_key=close_command.command_id)
    polled = client.post(
        "/api/platform/v1/nodes/poll",
        json={"worker_id": "session-worker"}, headers=headers,
    )
    assert polled.status_code == 200
    closed = client.post(
        "/api/platform/v1/nodes/browser-sessions/" + session_id + "/close",
        json={"command_id": close_command.command_id, "worker_id": "session-worker"},
        headers=headers,
    )
    assert closed.status_code == 200
    assert app.extensions["fleet"]["repositories"]["browser"].get_session(
        OWNER, session_id)["state"] == "closed"

    reopened = client.post(
        "/api/platform/v1/nodes/browser-sessions", json={
            "command_id": open_command.command_id, "worker_id": "session-worker",
            "session_id": session_id, "backend": "cdp_local",
        }, headers=headers,
    )
    assert reopened.status_code == 409
    assert reopened.get_json()["error"] == "browser_session_terminal"


def test_browser_session_registration_rejects_wrong_worker_and_scope(tmp_path):
    app = _app(tmp_path, browser=True)
    repo = app.extensions["fleet"]["platform_repository"]
    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    repo.upsert_node(OWNER, {"node_id": "node-scope", "label": "Browser"})
    credential = repo.provision_node_credential(
        OWNER, "node-scope", secret="q" * 40)["credential"]
    command = PlatformCommand.create(
        command_id="run-scope:step:1", target_node="node-scope", owner_id=OWNER,
        action="tool.browser.open", resource_id="workspace-scope",
        arguments={"url": "http://localhost:3000"}, retry_class="manual_only",
        expires_at=9_999_999_999, run_id="run-scope",
    )
    delivery.enqueue(command, idempotency_key=command.command_id)
    client = app.test_client()
    headers = {"X-Platform-Node-Credential": credential}
    assert client.post(
        "/api/platform/v1/nodes/poll", json={"worker_id": "scope-worker"},
        headers=headers,
    ).status_code == 200

    body = {"command_id": command.command_id, "worker_id": "other-worker",
            "session_id": "opaque-session-scope-1234", "backend": "cdp_local"}
    wrong_worker = client.post(
        "/api/platform/v1/nodes/browser-sessions", json=body, headers=headers,
    )
    assert wrong_worker.status_code == 409
    assert wrong_worker.get_json()["error"] == "artifact_lease_mismatch"

    body["worker_id"] = "scope-worker"
    registered = client.post(
        "/api/platform/v1/nodes/browser-sessions", json=body, headers=headers,
    )
    assert registered.status_code == 200
    other_run = PlatformCommand.create(
        command_id="run-other:step:1", target_node="node-scope", owner_id=OWNER,
        action="tool.browser.open", resource_id="workspace-scope",
        arguments={"url": "http://localhost:3000"}, retry_class="manual_only",
        expires_at=9_999_999_999, run_id="run-other",
    )
    delivery.enqueue(other_run, idempotency_key=other_run.command_id)
    assert client.post(
        "/api/platform/v1/nodes/poll", json={"worker_id": "scope-worker"},
        headers=headers,
    ).status_code == 200
    conflict = client.post(
        "/api/platform/v1/nodes/browser-sessions", json={
            "command_id": other_run.command_id, "worker_id": "scope-worker",
            "session_id": body["session_id"], "backend": "cdp_local",
        }, headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.get_json()["error"] == "browser_session_scope"
