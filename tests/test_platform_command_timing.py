"""Real-clock-boundary regressions for remote queue/execution/receipt budgets."""
import sqlite3

import pytest

from fleet_core.platform_command import PlatformCommand
from fleet_core.command_timing import CommandTiming, CommandTimingPolicy, DEFAULT_TRANSPORT_BUDGET_S
from hub.application.command_delivery_service import CommandDeliveryService
from hub.infrastructure.command_repository import CommandRepository
from hub.platform.remote_tool_broker import RemoteToolBroker
from tools.platform.journal import NodeJournal
from tools.platform.node_client import NodeClient


def command(command_id="command-1", *, expires_at=500):
    return PlatformCommand.create(command_id=command_id, target_node="node-1",
        action="tool.workspace.read", resource_id="workspace-1", arguments={"path": "report.md"},
        expires_at=expires_at)


def repository(tmp_path, now):
    repo = CommandRepository(tmp_path / "platform.db", clock=lambda: now[0])
    repo.init()
    return repo


def test_prepared_deadline_bounds_all_three_budgets_without_extension():
    timing = CommandTimingPolicy().start(100)
    bounded = timing.bounded_deadline(now=100, deadline=160)
    assert bounded.queue_deadline == pytest.approx(105)
    assert bounded.execution_timeout_s == pytest.approx(5)
    assert bounded.receipt_timeout_s == pytest.approx(50)
    assert bounded.hard_deadline == pytest.approx(160)
    assert timing.bounded_deadline(now=100, deadline=999) is timing
    with pytest.raises(ValueError, match="invalid_command_timing"):
        timing.bounded_deadline(now=100, deadline=100)


def test_prepared_command_keeps_original_budget_and_idempotent_deadline(tmp_path):
    from dataclasses import replace

    now = [100.0]
    repo = repository(tmp_path, now)
    timing = CommandTimingPolicy().start(100)
    original = command(expires_at=timing.hard_deadline)
    prepared = repo.enqueue(original, idempotency_key="original",
        prepare=lambda conn, pending: replace(pending, expires_at=160), timing=timing)
    assert prepared["expires_at"] == 160
    stored = repo.get_timing(original.command_id)
    assert stored["queue_deadline"] == pytest.approx(105)
    assert stored["execution_timeout_s"] == pytest.approx(5)
    assert stored["receipt_timeout_s"] == pytest.approx(50)
    now[0] = 110
    assert repo.enqueue(command(expires_at=999), idempotency_key="original",
                        timing=CommandTimingPolicy().start(110))["expires_at"] == 160
    assert repo.get_timing(original.command_id) == stored


def test_default_remote_wait_includes_queue_delivery_and_normal_execution(tmp_path):
    now = [100.0]
    repo = repository(tmp_path, now)
    claimed = False

    def sleep(interval):
        nonlocal claimed
        now[0] += interval
        if not claimed and now[0] >= 102:
            assert len(repo.claim_for_node("node-1", "worker-1", owner_id="owner-1")) == 1
            claimed = True
        if now[0] >= 131:
            repo.record_receipt("run-1:step:1", "succeeded", worker_id="worker-1", result={"content": "ok"})

    delivery = CommandDeliveryService(repo, clock=lambda: now[0], sleep=sleep)
    broker = RemoteToolBroker(delivery, node_id="node-1", resource_id="workspace-1",
                              run_id="run-1", clock=lambda: now[0])
    receipt = broker.execute(command_id="run-1:step:1", tool="workspace.read",
        arguments={"path": "report.md"}, owner_id="owner-1", epoch=1)
    assert receipt.state == "succeeded"
    assert repo.get("run-1:step:1")["status"] == "succeeded"


def test_node_limit_claims_only_the_command_it_can_execute_now(tmp_path):
    now = [100.0]
    repo = repository(tmp_path, now)
    for index in range(2):
        repo.enqueue(command(f"command-{index}"))
    delivery = CommandDeliveryService(repo, clock=lambda: now[0])
    requested_limits = []

    class Transport:
        def post_json(self, url, body, headers):
            if url.endswith("/poll"):
                requested_limits.append(body["limit"])
                return 200, {"ok": True, "commands": delivery.poll("node-1", body["worker_id"],
                    limit=body["limit"], lease_s=body["lease_s"])}
            try:
                delivery.receipt(body["command_id"], body["status"], worker_id=body["worker_id"], result=body["result"])
                return 200, {"ok": True}
            except Exception:
                return 409, {"ok": False}

    journal = NodeJournal(tmp_path / "node.db")
    journal.init()

    def execute(_command):
        now[0] += 40
        return {"state": "succeeded", "result": {"content": "ok"}}

    node = NodeClient(journal, executor=execute, clock=lambda: now[0], node_id="node-1",
        worker_id="worker-1", credential="credential", hub_url="https://hub.example", transport=Transport())
    assert node.poll_once(limit=2, lease_s=60) == {"ok": True, "commands": 2, "receipts": 2}
    assert requested_limits == [1, 1]
    assert [repo.get(f"command-{index}")["status"] for index in range(2)] == ["succeeded", "succeeded"]


@pytest.mark.parametrize("state", ["leased", "accepted", "running"])
def test_expiry_of_dispatched_work_remains_unknown(tmp_path, state):
    now = [100.0]
    repo = repository(tmp_path, now)
    repo.enqueue(command(expires_at=110))
    repo.claim_for_node("node-1", "worker-1", lease_s=60)
    if state != "leased":
        repo.record_receipt("command-1", state, worker_id="worker-1")
    now[0] = 110
    assert repo.expire_stale() == 1
    assert repo.get("command-1")["status"] == "unknown"
    assert repo.record_receipt("command-1", "succeeded", worker_id="worker-1")["status"] == "unknown"


@pytest.mark.parametrize("state", ["accepted", "running"])
def test_expired_intermediate_lease_is_settled_without_redelivery(tmp_path, state):
    now = [100.0]
    repo = repository(tmp_path, now)
    repo.enqueue(command())
    repo.claim_for_node("node-1", "worker-1", lease_s=1)
    repo.record_receipt("command-1", state, worker_id="worker-1")
    now[0] = 101
    assert repo.claim_for_node("node-1", "worker-2") == []
    assert repo.get("command-1")["status"] == "unknown"


def enqueue_timed(repo, now, *, command_id="command-1", policy=None):
    timing = (policy or CommandTimingPolicy()).start(now[0])
    original = command(command_id, expires_at=timing.hard_deadline)
    return repo.enqueue(original, idempotency_key=command_id, timing=timing)


def test_default_receipt_budget_covers_screenshot_materialization_and_upload():
    policy = CommandTimingPolicy()
    assert policy.receipt_timeout_s > 3 * DEFAULT_TRANSPORT_BUDGET_S
    assert policy.total_timeout_s <= 3600


@pytest.mark.parametrize("field", ["queue_timeout_s", "execution_timeout_s", "receipt_timeout_s"])
@pytest.mark.parametrize("value", [True, False, 0, -1, float("nan"), float("inf"), "30"])
def test_policy_rejects_invalid_budgets(field, value):
    with pytest.raises(ValueError, match="invalid_command_timing"):
        CommandTimingPolicy(**{field: value})


def test_policy_limits_total_lifetime():
    assert CommandTimingPolicy(queue_timeout_s=1, execution_timeout_s=1, receipt_timeout_s=3598).total_timeout_s == 3600
    with pytest.raises(ValueError, match="invalid_command_timing"):
        CommandTimingPolicy(queue_timeout_s=1, execution_timeout_s=1, receipt_timeout_s=3599)


@pytest.mark.parametrize("offset,expected", [(29.999, "leased"), (30, "expired"), (30.001, "expired")])
def test_queue_admission_stops_at_its_persisted_boundary_after_restart(tmp_path, offset, expected):
    now = [100.0]
    repo = repository(tmp_path, now)
    enqueue_timed(repo, now)
    reopened = CommandRepository(repo.db_path, clock=lambda: now[0])
    reopened.init()
    now[0] += offset
    claimed = reopened.claim_for_node("node-1", "worker-1", lease_s=1)
    assert bool(claimed) == (expected == "leased")
    assert reopened.get("command-1")["status"] == expected
    timing = reopened.get_timing("command-1")
    assert timing["queue_deadline"] == 130
    if claimed:
        assert timing["claimed_at"] == now[0]
        assert timing["execution_deadline"] == now[0] + 30
        assert timing["receipt_deadline"] == now[0] + 330
        assert claimed[0]["lease_until"] == 460


def test_timed_lease_and_receipt_survive_original_sixty_second_ceiling(tmp_path):
    now = [100.0]
    repo = repository(tmp_path, now)
    enqueue_timed(repo, now)
    claimed = repo.claim_for_node("node-1", "worker-1", lease_s=1)[0]
    assert claimed["lease_until"] == 460
    assert claimed["lease_until"] - now[0] <= 3600
    now[0] = 220
    saved = repo.record_receipt("command-1", "succeeded", worker_id="worker-1", result={"ok": True})
    assert saved["status"] == "succeeded"


def test_local_wait_does_not_override_timed_execution_and_late_success_is_accepted(tmp_path):
    now = [100.0]
    repo = repository(tmp_path, now)
    enqueue_timed(repo, now)
    repo.claim_for_node("node-1", "worker-1")
    delivery = CommandDeliveryService(repo, clock=lambda: now[0], sleep=lambda interval: now.__setitem__(0, now[0] + interval))
    assert delivery.wait_for_receipt("command-1", timeout_s=0.1)["status"] == "leased"
    assert repo.get("command-1")["status"] == "leased"
    now[0] = 131
    assert repo.record_receipt("command-1", "succeeded", worker_id="worker-1")["status"] == "succeeded"


def test_waiting_uses_reads_until_a_durable_deadline_is_due(tmp_path, monkeypatch):
    now = [100.0]
    repo = repository(tmp_path, now)
    enqueue_timed(repo, now)
    expirations = []
    settle = repo.expire_command

    def expire(command_id):
        expirations.append(now[0])
        return settle(command_id)

    monkeypatch.setattr(repo, "expire_command", expire)
    delivery = CommandDeliveryService(repo, clock=lambda: now[0], sleep=lambda interval: now.__setitem__(0, now[0] + interval))
    assert delivery.wait_for_receipt("command-1", timeout_s=1)["status"] == "queued"
    assert expirations == []
    now[0] = 130
    assert delivery.wait_for_receipt("command-1", timeout_s=1)["status"] == "expired"
    assert expirations == [130]


def test_timing_policy_accepts_epoch_zero_without_extending_command_lifetime():
    timing = CommandTimingPolicy().start(0)
    assert timing.queue_deadline == 30 and timing.hard_deadline == 360


def test_node_does_not_execute_a_response_exceeding_its_single_slot(tmp_path):
    journal = NodeJournal(tmp_path / "node.db")
    journal.init()
    calls = []

    class Transport:
        def post_json(self, url, body, headers):
            assert body["limit"] == 1
            return 200, {"ok": True, "commands": [command("a").as_dict(), command("b").as_dict()]}

    node = NodeClient(journal, executor=lambda value: calls.append(value), node_id="node-1",
        credential="credential", hub_url="https://hub.example", transport=Transport(), clock=lambda: 100)
    assert node.poll_once(limit=2) == {"ok": False, "commands": 0, "error": "malformed_commands"}
    assert calls == []


@pytest.mark.parametrize("offset,expected", [(329.999, "succeeded"), (330, "unknown"), (330.001, "unknown")])
def test_receipt_deadline_boundary_does_not_depend_on_the_sweep(tmp_path, offset, expected):
    now = [100.0]
    repo = repository(tmp_path, now)
    enqueue_timed(repo, now)
    repo.claim_for_node("node-1", "worker-1")
    now[0] += offset
    saved = repo.record_receipt("command-1", "succeeded", worker_id="worker-1")
    assert saved["status"] == expected


def test_timing_is_atomic_with_enqueue_and_idempotency_keeps_original_deadlines(tmp_path):
    now = [100.0]
    repo = repository(tmp_path, now)
    original = enqueue_timed(repo, now)
    now[0] = 110
    retried = enqueue_timed(repo, now)
    assert original["expires_at"] == retried["expires_at"] == 460
    assert repo.get_timing("command-1")["queue_deadline"] == 130
    bad = CommandTiming(140, 30, 300)
    with pytest.raises(Exception, match="invalid_command_timing"):
        repo.enqueue(command("bad", expires_at=499), idempotency_key="bad", timing=bad)
    assert repo.get("bad") is None and repo.get_timing("bad") is None
    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM platform_command_outbox WHERE command_id='bad'").fetchone()[0] == 0


def test_broker_waits_against_original_idempotent_command_deadline(tmp_path):
    now = [100.0]
    repo = repository(tmp_path, now)
    delivery = CommandDeliveryService(repo, clock=lambda: now[0])
    waits = []

    class Waiter:
        def wait_for_receipt(self, command_id, *, timeout_s):
            waits.append(timeout_s)
            return {"status": "succeeded", "result": {}}

    broker = RemoteToolBroker(delivery, node_id="node-1", resource_id="workspace-1",
        run_id="run-1", waiter=Waiter(), clock=lambda: now[0])
    for current in (100, 110):
        now[0] = current
        assert broker.execute(command_id="run-1:step:1", tool="workspace.read",
            arguments={"path": "report.md"}, owner_id="owner-1", epoch=1).state == "succeeded"
    assert waits == [360, 350]


def test_timing_is_not_added_to_signed_wire_and_cascades_on_retention(tmp_path):
    from fleet_core.control import generate_ed25519_keypair
    from fleet_core.platform_command import verify_command

    now = [100.0]
    repo = repository(tmp_path, now)
    private, public = generate_ed25519_keypair()
    timing = CommandTimingPolicy().start(now[0])
    original = command(expires_at=timing.hard_deadline).signed(private)
    repo.enqueue(original, timing=timing)
    assert verify_command(repo.get("command-1"), public)
    assert "queue_deadline" not in repo.get("command-1")
    with repo._connect() as conn:
        conn.execute("DELETE FROM platform_commands WHERE command_id='command-1'")
    assert repo.get_timing("command-1") is None
