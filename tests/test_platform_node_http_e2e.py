from pathlib import Path

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


def _app(tmp_path: Path):
    return create_app(FleetConfig.from_root(
        tmp_path, ingest_token="ingest", dev_operator=OWNER,
        platform_enabled=True,
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
