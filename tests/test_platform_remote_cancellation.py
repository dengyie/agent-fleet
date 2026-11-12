"""Cancellation through real Run, command and scheduler state boundaries."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from fleet_core.backends.directory import DirectoryBackend
from fleet_core.command_timing import CommandTimingPolicy
from fleet_core.platform_command import PlatformCommand
from hub.application.command_delivery_service import CommandDeliveryService
from hub.application.run_event_service import RunEventService
from hub.application.run_scheduler_service import RunSchedulerService
from hub.application.run_worker_service import LocalRunWorkerService
from hub.infrastructure.command_repository import CommandRepository
from hub.platform.providers.base import ModelResponse
from hub.platform.remote_tool_broker import RemoteToolBroker
from tests.test_platform_run_worker import OWNER, _app, _catalog
from tools.platform.node_executor import NodeToolExecutor


def _command(command_id="run-cancel:step:1"):
    return PlatformCommand.create(command_id=command_id, target_node="remote",
        owner_id=OWNER, resource_id="home", run_id="run-cancel",
        action="tool.workspace.write", arguments={"path": "side-effect.txt", "content": "written"},
        retry_class="reconcile_before_retry", expires_at=500)


@pytest.mark.parametrize("timed", [False, True])
def test_cancelling_unclaimed_command_proves_non_execution_after_restart(tmp_path, timed):
    repo = CommandRepository(tmp_path / "commands.db", clock=lambda: 100)
    repo.init()
    command = _command()
    if timed:
        timing = CommandTimingPolicy().start(140)
        repo.enqueue(command, timing=timing)
    else:
        repo.enqueue(command)
    delivery = CommandDeliveryService(repo, clock=lambda: 100)
    settled = delivery.wait_for_receipt(command.command_id, should_cancel=lambda: True)

    assert settled["status"] == "expired"
    assert settled["result"] == {"reason": "execution_cancelled"}
    assert settled["attempt"] == 0
    reopened = CommandRepository(repo.db_path, clock=lambda: 101)
    reopened.init()
    assert reopened.claim_for_node("remote", "late-node", owner_id=OWNER) == []
    assert reopened.get(command.command_id) == settled


@pytest.mark.parametrize("state", ["leased", "accepted", "running"])
def test_cancelling_dispatched_command_keeps_uncertainty_and_rejects_late_success(tmp_path, state):
    repo = CommandRepository(tmp_path / "commands.db", clock=lambda: 100)
    repo.init()
    command = _command()
    repo.enqueue(command)
    repo.claim_for_node("remote", "node-worker", owner_id=OWNER)
    if state != "leased":
        repo.record_receipt(command.command_id, state, worker_id="node-worker")
    delivery = CommandDeliveryService(repo, clock=lambda: 100)

    settled = delivery.wait_for_receipt(command.command_id, should_cancel=lambda: True)

    assert settled["status"] == "unknown"
    assert settled["result"] == {"reason": "execution_cancelled"}
    assert repo.record_receipt(command.command_id, "succeeded", worker_id="node-worker") == settled
    assert repo.claim_for_node("remote", "late-node", owner_id=OWNER) == []


@pytest.mark.parametrize("state", ["succeeded", "failed", "unknown", "expired"])
def test_terminal_receipt_wins_cancellation_without_rewriting_evidence(tmp_path, state):
    now = [100.0]
    repo = CommandRepository(tmp_path / "commands.db", clock=lambda: now[0])
    repo.init()
    command = _command()
    repo.enqueue(command)
    if state == "expired":
        now[0] = 500
        saved = repo.expire_command(command.command_id)
    else:
        repo.claim_for_node("remote", "node-worker", owner_id=OWNER)
        saved = repo.record_receipt(command.command_id, state, worker_id="node-worker", result={"evidence": "original"})
    delivery = CommandDeliveryService(repo, clock=lambda: now[0])

    assert delivery.wait_for_receipt(command.command_id, should_cancel=lambda: True) == saved
    assert repo.cancel_wait(command.command_id) == saved


def test_claim_between_wait_read_and_cancellation_stays_unknown(tmp_path):
    repo = CommandRepository(tmp_path / "commands.db", clock=lambda: 100)
    repo.init()
    command = _command()
    repo.enqueue(command)

    def cancel_after_claim():
        assert len(repo.claim_for_node("remote", "node-worker", owner_id=OWNER)) == 1
        return True

    delivery = CommandDeliveryService(repo, clock=lambda: 100)
    assert delivery.wait_for_receipt(command.command_id, should_cancel=cancel_after_claim)["status"] == "unknown"


def test_concurrent_cancel_and_node_claim_have_one_durable_outcome(tmp_path):
    repo = CommandRepository(tmp_path / "commands.db", clock=lambda: 100)
    repo.init()
    other = CommandRepository(repo.db_path, clock=lambda: 100)
    with ThreadPoolExecutor(max_workers=2) as pool:
        for index in range(8):
            command = _command(f"run-cancel:step:{index}")
            repo.enqueue(command)
            barrier = threading.Barrier(2)

            def claim():
                barrier.wait(timeout=3)
                return other.claim_for_node("remote", "node-worker", owner_id=OWNER)

            def cancel():
                barrier.wait(timeout=3)
                return repo.cancel_wait(command.command_id)

            claiming, cancelling = pool.submit(claim), pool.submit(cancel)
            claims, settled = claiming.result(timeout=5), cancelling.result(timeout=5)
            assert settled["status"] == ("unknown" if claims else "expired")
            assert settled["attempt"] == len(claims)
            assert repo.get(command.command_id) == settled
            assert other.claim_for_node("remote", "late-node", owner_id=OWNER) == []


def test_broker_rechecks_cancellation_before_enqueue(tmp_path):
    repo = CommandRepository(tmp_path / "commands.db", clock=lambda: 100)
    repo.init()
    delivery = CommandDeliveryService(repo, clock=lambda: 100)
    broker = RemoteToolBroker(delivery, node_id="remote", resource_id="home",
        run_id="run-cancel", clock=lambda: 100, should_cancel=lambda: True)

    receipt = broker.execute(command_id="run-cancel:step:1", tool="workspace.write",
        arguments={"path": "side-effect.txt", "content": "written"}, owner_id=OWNER, epoch=1)

    assert receipt.state == "failed" and receipt.error_code == "execution_cancelled"
    assert repo.get("run-cancel:step:1") is None


def test_receipt_cancellation_checks_do_not_renew_sqlite_lease_on_every_poll(tmp_path, monkeypatch):
    app = _app(tmp_path)
    platform = _catalog(app, tmp_path)
    platform.upsert_node(OWNER, {"node_id": "remote", "label": "remote"})
    conversation = platform.create_conversation(OWNER, "conv-polls", title="", workspace_id="home")
    platform.append_turn(OWNER, conversation["conversation_id"], "msg-polls", "run-polls",
        text="read", client_token="polls", now=1,
        config_snapshot={"workspace_id": "home", "model_profile_id": "local", "execution_node_id": "remote"})
    now = [100.0]
    commands = CommandRepository(platform.db_path, clock=lambda: now[0])
    commands.init()
    renewals, samples = [], []
    renew = platform.renew_run_lease

    def measured_renew(*args, **kwargs):
        renewals.append(now[0])
        return renew(*args, **kwargs)

    monkeypatch.setattr(platform, "renew_run_lease", measured_renew)

    def sleep(interval):
        now[0] += interval
        samples.append(len(renewals))
        if len(samples) == 1:
            assert len(commands.claim_for_node("remote", "node-worker", owner_id=OWNER)) == 1
        if len(samples) == 20:
            commands.record_receipt("run-polls:step:1", "succeeded", worker_id="node-worker", result={"content": "read"})

    class Provider:
        def complete(self, messages, tools, *, request_observer=None):
            if messages[-1]["role"] == "tool":
                return ModelResponse(kind="final", text="done")
            return ModelResponse(kind="tool_call", tool="workspace.read", arguments={"path": "report.md"})

    delivery = CommandDeliveryService(commands, clock=lambda: now[0], sleep=sleep)
    # Retain coverage for callers explicitly supplying the synchronous waiter.
    worker = LocalRunWorkerService(platform, RunEventService(platform, clock=lambda: now[0]),
        worker_id="poll-worker", clock=lambda: now[0], provider_factory=lambda _: Provider(),
        remote_execution_enabled=True, remote_delivery=delivery, remote_waiter=delivery)

    assert worker.run_once(OWNER)["state"] == "succeeded"
    assert len(samples) == 20
    assert len(set(samples)) == 1, "receipt polling must not write a fresh Run lease every 100ms"


@pytest.mark.parametrize("dispatched", [False, True])
def test_http_run_cancel_interrupts_remote_wait_and_releases_scheduler_slot(tmp_path, dispatched):
    app = _app(tmp_path)
    platform = _catalog(app, tmp_path)
    platform.upsert_node(OWNER, {"node_id": "remote", "label": "remote"})
    conversation = platform.create_conversation(OWNER, "conv-cancel", title="", workspace_id="home")
    platform.append_turn(OWNER, conversation["conversation_id"], "msg-cancel", "run-cancel",
        text="write", client_token="cancel", now=1,
        config_snapshot={"workspace_id": "home", "model_profile_id": "local", "execution_node_id": "remote"})
    now = [100.0]
    commands = CommandRepository(platform.db_path, clock=lambda: now[0])
    commands.init()
    backend = DirectoryBackend(tmp_path / "remote-workspace")
    executor = NodeToolExecutor(backend, workspace_id="home")
    sleeps, model_calls = [], []

    def sleep(interval):
        sleeps.append(interval)
        pytest.fail('native remote waits must suspend without sleeping')

    class Provider:
        def complete(self, messages, tools, *, request_observer=None):
            model_calls.append(messages)
            return ModelResponse(kind="tool_call", tool="workspace.write",
                arguments={"path": "side-effect.txt", "content": "written"})

    delivery = CommandDeliveryService(commands, clock=lambda: now[0], sleep=sleep)
    worker = LocalRunWorkerService(platform, RunEventService(platform, clock=lambda: now[0]),
        worker_id="cancel-worker", clock=lambda: now[0], provider_factory=lambda _: Provider(),
        remote_execution_enabled=True, remote_delivery=delivery)
    scheduler = RunSchedulerService(platform, worker, scheduler_id="cancel-scheduler", clock=lambda: now[0])

    assert scheduler.tick_once()['state'] == 'waiting_node'
    if dispatched:
        assert len(commands.claim_for_node('remote', 'node-worker', owner_id=OWNER)) == 1
    response = app.test_client().post('/api/platform/v1/runs/run-cancel/cancel')
    assert response.status_code == 200 and response.get_json()['cancel_requested']
    result = scheduler.tick_once()
    # A real Node poll after the cancellation must not execute the write.
    for claimed in commands.claim_for_node('remote', 'late-node', owner_id=OWNER):
        executor(claimed)
    assert not (tmp_path / "remote-workspace/side-effect.txt").exists(), "cancelled queue still executed on the Node"
    assert sleeps == [], "remote cancellation waited instead of observing the original command"
    assert len(model_calls) == 1
    assert result["state"] == ("unknown" if dispatched else "cancelled")
    if not dispatched:
        assert result["result_text"] == "运行已取消。"
    command = commands.get("run-cancel:step:1")
    assert command["status"] == ("unknown" if dispatched else "expired")
    assert command["result"] == {"reason": "execution_cancelled"}
    assert commands.claim_for_node("remote", "late-node", owner_id=OWNER) == []
    # The scheduler's global capacity is available to another owner's work.
    assert platform.claim_worker_slot("next@example.test", "run-next", worker_id="next-worker",
        workspace_key="next:other", max_concurrency=1, max_workspace_concurrency=1,
        now=now[0], lease_s=60) is not None
