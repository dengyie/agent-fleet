from pathlib import Path

from hub.infrastructure.command_repository import CommandRepository
from hub.domain.platform_command import PlatformCommand
from tools.platform.backends.base import ToolReceipt
from tools.platform.node_executor import NodeToolExecutor
from tools.platform.node_client import NodeClient
from tools.platform.journal import NodeJournal
from tools.platform.remote_tool_broker import RemoteToolBroker
from tools.platform.browser_policy import BrowserPolicyError


def _command(command_id="cmd-remote", node="node-remote", owner="owner@example.test"):
    return PlatformCommand.create(
        command_id=command_id,
        target_node=node,
        owner_id=owner,
        action="tool.workspace.read",
        resource_id="workspace-remote",
        arguments={"path": "report.md"},
        retry_class="read_only",
        expires_at=9_999_999_999,
        run_id="run-remote",
    )


def test_command_repository_marks_non_terminal_receipt_unknown_without_replay(tmp_path):
    now = [10.0]
    repo = CommandRepository(tmp_path / "platform.db", clock=lambda: now[0])
    repo.init()
    repo.enqueue(_command(), idempotency_key="remote-1")
    repo.claim_for_node("node-remote", "worker-1", lease_s=60)
    marked = repo.mark_unknown("cmd-remote", reason="receipt_timeout")
    assert marked["status"] == "unknown"
    assert marked["result"] == {"reason": "receipt_timeout"}
    replay = repo.mark_unknown("cmd-remote", reason="late_retry")
    assert replay["status"] == "unknown"
    assert replay["result"] == {"reason": "receipt_timeout"}
    assert repo.claim_for_node("node-remote", "worker-2") == []


def test_remote_tool_broker_enqueues_one_fixed_command_and_maps_receipt():
    queued = []
    events = []

    class Delivery:
        def enqueue(self, command, *, idempotency_key=None):
            queued.append((command, idempotency_key))
            return command.as_dict() | {"status": "queued", "result": None}

        def wait_for_receipt(self, command_id, *, timeout_s=30.0):
            return {"command_id": command_id, "status": "succeeded", "result": {"content": "ok"}}

    broker = RemoteToolBroker(
        Delivery(), node_id="node-remote", resource_id="workspace-remote",
        run_id="run-remote", event_sink=lambda kind, payload: events.append((kind, payload)),
    )
    receipt = broker.execute(
        command_id="run-remote:step:1", tool="workspace.read",
        arguments={"path": "report.md"}, owner_id="owner@example.test", epoch=1,
    )
    assert receipt == ToolReceipt("run-remote:step:1", "succeeded", {"content": "ok"})
    assert len(queued) == 1
    command, key = queued[0]
    assert key == command.command_id
    assert command.action == "tool.workspace.read"
    assert command.target_node == "node-remote"
    assert command.resource_id == "workspace-remote"
    assert events == [
        ("node_command_queued", {"command_id": command.command_id, "node_id": "node-remote", "tool": "workspace.read"}),
        ("node_receipt", {"command_id": command.command_id, "node_id": "node-remote", "tool": "workspace.read", "status": "succeeded"}),
    ]


def test_remote_tool_broker_rejects_invalid_browser_arguments_before_enqueue():
    queued = []

    class Delivery:
        def enqueue(self, command, *, idempotency_key=None):
            queued.append(command)
            return command.as_dict() | {"status": "queued", "result": None}

        def wait_for_receipt(self, command_id, *, timeout_s=30.0):
            raise AssertionError("invalid browser command reached receipt wait")

    broker = RemoteToolBroker(
        Delivery(), node_id="node-remote", resource_id="workspace-remote",
        run_id="run-remote", browser_enabled=True,
    )

    invalid_url = broker.execute(
        command_id="run-remote:step:invalid-url", tool="browser.open",
        arguments={"url": "https://not-allowed.example"},
        owner_id="owner@example.test", epoch=1,
    )
    sensitive_selector = broker.execute(
        command_id="run-remote:step:sensitive", tool="browser.click",
        arguments={"session_id": "session-opaque-123", "selector": "#password"},
        owner_id="owner@example.test", epoch=1,
    )

    assert invalid_url == ToolReceipt(
        "run-remote:step:invalid-url", "failed", {}, "network_disabled",
    )
    assert sensitive_selector == ToolReceipt(
        "run-remote:step:sensitive", "failed", {}, "sensitive_field_forbidden",
    )
    assert queued == []


def test_remote_tool_broker_passes_valid_loopback_browser_arguments_to_node():
    queued = []

    class Delivery:
        def enqueue(self, command, *, idempotency_key=None):
            queued.append(command)
            return command.as_dict() | {"status": "queued", "result": None}

        def wait_for_receipt(self, command_id, *, timeout_s=30.0):
            return {"command_id": command_id, "status": "succeeded", "result": {}}

    broker = RemoteToolBroker(
        Delivery(), node_id="node-remote", resource_id="workspace-remote",
        run_id="run-remote", browser_enabled=True,
    )
    receipt = broker.execute(
        command_id="run-remote:step:loopback", tool="browser.open",
        arguments={"url": "http://localhost:3000"},
        owner_id="owner@example.test", epoch=1,
    )

    assert receipt.state == "succeeded"
    assert len(queued) == 1
    assert queued[0].arguments == {"url": "http://localhost:3000"}


def test_remote_tool_broker_allows_only_configured_global_external_origin():
    queued = []

    class Delivery:
        def enqueue(self, command, *, idempotency_key=None):
            queued.append(command)
            return command.as_dict() | {"status": "queued", "result": None}

        def wait_for_receipt(self, command_id, *, timeout_s=30.0):
            return {"command_id": command_id, "status": "succeeded", "result": {}}

    def resolver(host, _port, *, type):
        import socket
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    broker = RemoteToolBroker(
        Delivery(), node_id="node-remote", resource_id="workspace-remote",
        run_id="run-remote", browser_enabled=True, browser_network_enabled=True,
        browser_allowed_origins=("https://example.test",), browser_resolver=resolver,
    )
    receipt = broker.execute(
        command_id="run-remote:step:external", tool="browser.open",
        arguments={"url": "https://example.test/page"},
        owner_id="owner@example.test", epoch=1,
    )

    assert receipt.state == "succeeded"
    assert queued[0].arguments == {"url": "https://example.test/page"}


def test_remote_tool_broker_never_replays_unknown_receipt():
    calls = []

    class Delivery:
        def enqueue(self, command, *, idempotency_key=None):
            calls.append(command.command_id)
            return command.as_dict() | {"status": "queued", "result": None}

        def wait_for_receipt(self, command_id, *, timeout_s=30.0):
            return {"command_id": command_id, "status": "unknown", "result": {"reason": "receipt_timeout"}}

    broker = RemoteToolBroker(
        Delivery(), node_id="node-remote", resource_id="workspace-remote", run_id="run-remote",
    )
    receipt = broker.execute(
        command_id="run-remote:step:1", tool="workspace.write",
        arguments={"path": "report.md", "content": "x"}, owner_id="owner@example.test", epoch=1,
    )
    assert receipt.state == "unknown"
    assert receipt.error_code == "receipt_unknown"
    assert calls == ["run-remote:step:1"]


def test_node_tool_executor_uses_only_fixed_workspace_actions(tmp_path):
    class Backend:
        def read(self, path):
            return ToolReceipt("", "succeeded", {"path": path})

        def write(self, path, content):
            return ToolReceipt("", "succeeded", {"path": path, "size": len(content)})

        def list(self, path=""):
            return ToolReceipt("", "succeeded", {"path": path})

        def execute(self, command_id, argv, *, timeout_s=30):
            return ToolReceipt(command_id, "succeeded", {"argv": argv})

    executor = NodeToolExecutor(Backend())
    assert executor(_command("cmd-list").as_dict() | {"action": "tool.workspace.list", "arguments": {"path": ""}})["state"] == "succeeded"
    assert executor(_command("cmd-read").as_dict())["result"] == {"path": "report.md"}
    rejected = executor(_command("cmd-bad").as_dict() | {"action": "tool.shell", "arguments": {"argv": ["sh", "-c", "id"]}})
    assert rejected["state"] == "failed"
    assert rejected["error_code"] == "unknown_tool"


def test_node_client_preserves_fixed_executor_failure_receipt(tmp_path):
    journal = NodeJournal(tmp_path / "node.db")
    journal.init()

    def executor(command):
        return {"state": "failed", "result": {}, "error_code": "unknown_tool"}

    client = NodeClient(journal, executor=executor)
    command = _command("cmd-failed").as_dict() | {
        "action": "tool.workspace.read",
    }
    result = client.handle(command)
    assert result["status"] == "failed"
    assert journal.get("cmd-failed")["state"] == "failed"


def test_node_tool_executor_backend_interruption_is_unknown_in_journal(tmp_path):
    class Backend:
        def read(self, path):
            raise RuntimeError("interrupted")

    journal = NodeJournal(tmp_path / "node-unknown.db")
    journal.init()
    client = NodeClient(journal, executor=NodeToolExecutor(Backend()))
    result = client.handle(_command("cmd-unknown").as_dict())
    assert result["status"] == "unknown"
    assert journal.get("cmd-unknown")["state"] == "unknown"
