"""Real SQLite admission, redelivery and terminal-state fencing."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from hub.domain.control import generate_ed25519_keypair
from hub.domain.platform_command import PlatformCommand, args_hash, sign_command
from tools.platform.journal import NodeJournal
from tools.platform.node_client import NodeClient


def command(command_id="cmd-once"):
    return PlatformCommand.create(
        command_id=command_id, target_node="node-a", action="tool.workspace.write",
        resource_id="workspace-a", arguments={"path": "result.txt", "content": "once"},
        retry_class="manual_only", expires_at=1000, run_id="run-a",
    )


def journal_at(path):
    journal = NodeJournal(path)
    journal.init()
    return journal


def test_restarted_client_never_replays_running_record(tmp_path):
    journal = journal_at(tmp_path / "node.db")
    journal.begin("cmd-once", now=100)
    calls = []
    reopened = NodeJournal(journal.path)
    client = NodeClient(reopened, executor=lambda cmd: calls.append(cmd) or {}, clock=lambda: 101)
    receipt = client.handle(command().as_dict())
    assert calls == []
    assert receipt["status"] == "unknown"
    assert reopened.get("cmd-once")["state"] == "unknown"


def test_concurrent_journal_admission_has_one_winner(tmp_path):
    journal = journal_at(tmp_path / "node.db")
    barrier = Barrier(2)

    def begin():
        barrier.wait(timeout=5)
        return NodeJournal(journal.path).begin("cmd-once", now=100)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(begin) for _ in range(2)]
        admissions = [future.result(timeout=10) for future in futures]
    assert sum(row["claimed"] for row in admissions) == 1
    assert journal.get("cmd-once")["state"] == "unknown"


def test_concurrent_delivery_and_late_completion_cannot_replay_or_overwrite(tmp_path):
    journal = journal_at(tmp_path / "node.db")
    entered, release = Event(), Event()
    calls = []

    def first_executor(cmd):
        calls.append(cmd["command_id"])
        entered.set()
        assert release.wait(timeout=10)
        return {"state": "succeeded", "result": {"written": True}}

    first = NodeClient(journal, executor=first_executor, clock=lambda: 100)
    second = NodeClient(NodeJournal(journal.path), executor=lambda cmd: calls.append(cmd["command_id"]) or {}, clock=lambda: 101)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(first.handle, command().as_dict())
        try:
            assert entered.wait(timeout=5)
            duplicate = second.handle(command().as_dict())
        finally:
            release.set()
        original = future.result(timeout=5)
    assert calls == ["cmd-once"]
    assert duplicate["status"] == original["status"] == "unknown"
    assert journal.get("cmd-once")["state"] == "unknown"
    assert first.handle(command().as_dict())["status"] == "unknown"
    assert calls == ["cmd-once"]


@pytest.mark.parametrize("state", ["succeeded", "failed", "unknown"])
def test_journal_terminal_result_is_immutable(tmp_path, state):
    journal = journal_at(tmp_path / "node.db")
    journal.begin("cmd-once", now=100)
    first = journal.finish("cmd-once", state, {"version": "first"}, now=101)
    late = journal.finish("cmd-once", "failed", {"version": "late"}, now=102)
    assert late == first
    assert journal.finish("cmd-once", "failed", {"invalid_late_result": float("nan")}, now=103) == first


def test_independent_commands_do_not_hold_database_lock_during_execution(tmp_path):
    journal = journal_at(tmp_path / "node.db")
    entered, release = Event(), Event()

    def executor(cmd):
        entered.set()
        assert release.wait(timeout=10)
        return {"ok": True}

    first = NodeClient(journal, executor=executor, clock=lambda: 100)
    second = NodeClient(NodeJournal(journal.path), executor=lambda cmd: {"ok": True}, clock=lambda: 101)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(first.handle, command("cmd-first").as_dict())
        try:
            assert entered.wait(timeout=5)
            assert second.handle(command("cmd-second").as_dict())["status"] == "succeeded"
        finally:
            release.set()
        assert future.result(timeout=5)["status"] == "succeeded"


def test_crashed_submit_keeps_signed_approval_attribution_without_dispatch(tmp_path):
    journal = journal_at(tmp_path / "node.db")
    journal.begin("cmd-once", now=100)
    private, public = generate_ed25519_keypair()
    submit = PlatformCommand.create(
        command_id="cmd-once", target_node="node-a", action="tool.browser.submit",
        resource_id="workspace-a", arguments={"session_id": "session-aaaaaaaaaa", "selector": "#go", "approval_id": "approval-aaaaaaaaaa"},
        retry_class="manual_only", expires_at=1000, run_id="run-a",
    ).signed(private)
    calls = []
    client = NodeClient(journal, executor=lambda cmd: calls.append(cmd) or {}, clock=lambda: 101,
                        node_id="node-a", public_key=public, require_signature=True)
    receipt = client.handle(submit.as_dict())
    assert calls == []
    assert receipt["status"] == "unknown"
    assert receipt["result"]["approval_id"] == "approval-aaaaaaaaaa"
    assert journal.get("cmd-once")["result"]["approval_id"] == "approval-aaaaaaaaaa"


@pytest.mark.parametrize("arguments", [None, []])
def test_malformed_signed_submit_has_bounded_failure(tmp_path, arguments):
    from tools.platform.browser_backend import LocalBrowserBackend
    from tools.platform.node_executor import NodeToolExecutor
    journal = journal_at(tmp_path / "node.db")
    private, public = generate_ed25519_keypair()
    envelope = {**command().as_dict(), "action": "tool.browser.submit",
                "arguments": arguments, "args_hash": args_hash(arguments)}
    envelope["signature"] = sign_command(envelope, private)
    backend = LocalBrowserBackend(object, submit_enabled=True)
    client = NodeClient(journal, executor=NodeToolExecutor(None, browser_backend=backend, browser_enabled=True),
                        clock=lambda: 101, node_id="node-a", public_key=public, require_signature=True)
    receipt = client.handle(envelope)
    assert receipt["status"] == "failed"
    assert receipt["result"]["error_code"] == "invalid_arguments"
