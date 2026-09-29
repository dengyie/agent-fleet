from hub.domain.platform_command import PlatformCommand, sign_command, verify_command
from hub.domain.control import generate_ed25519_keypair
from hub.infrastructure.command_repository import CommandRepository
from hub.application.command_delivery_service import CommandDeliveryService
from tools.platform.journal import NodeJournal
from tools.platform.node_client import NodeClient
import json
import sqlite3


def _command(command_id="cmd-1", expires_at=9999999999):
    return PlatformCommand.create(
        command_id=command_id, target_node="node-1", action="workspace.read",
        resource_id="workspace-1", arguments={"path": "report.md"},
        retry_class="read_only", expires_at=expires_at,
    )


def test_command_repository_claim_and_terminal_receipt_are_idempotent(tmp_path):
    repo = CommandRepository(tmp_path / "platform.db")
    repo.init()
    first = repo.enqueue(_command(), idempotency_key="turn-1")
    again = repo.enqueue(_command(), idempotency_key="turn-1")
    assert first["command_id"] == again["command_id"]
    claimed = repo.claim_for_node("node-1", "worker-1")
    assert [row["command_id"] for row in claimed] == ["cmd-1"]
    assert repo.claim_for_node("node-1", "worker-1") == []
    receipt = repo.record_receipt("cmd-1", "succeeded", result={"ok": True}, worker_id="worker-1")
    replay = repo.record_receipt("cmd-1", "failed", result={"late": True}, worker_id="worker-1")
    assert receipt["status"] == replay["status"] == "succeeded"
    assert replay["result"] == {"ok": True}


def test_platform_command_signature_covers_arguments_and_scope():
    private_key, public_key = generate_ed25519_keypair()
    command = _command().as_dict()
    command["signature"] = sign_command(command, private_key)
    assert verify_command(command, public_key)
    command["resource_id"] = "other"
    assert not verify_command(command, public_key)


def test_node_journal_prevents_duplicate_completed_execution(tmp_path):
    journal = NodeJournal(tmp_path / "node.db")
    journal.init()
    calls = []
    client = NodeClient(journal, executor=lambda command: calls.append(command["command_id"]) or {"bytes": 3})
    command = _command().as_dict()
    assert client.handle(command)["status"] == "succeeded"
    assert client.handle(command)["status"] == "succeeded"
    assert calls == ["cmd-1"]


def test_node_client_requires_signature_when_configured(tmp_path):
    private_key, public_key = generate_ed25519_keypair()
    journal = NodeJournal(tmp_path / "node.db")
    journal.init()
    calls = []
    client = NodeClient(
        journal, executor=lambda command: calls.append(command["command_id"]) or {"ok": True},
        public_key=public_key, require_signature=True,
    )
    unsigned = _command("cmd-unsigned").as_dict()
    assert client.handle(unsigned)["reason"] == "invalid_signature"
    assert calls == []
    signed = _command("cmd-signed").signed(private_key).as_dict()
    assert client.handle(signed)["status"] == "succeeded"
    assert calls == ["cmd-signed"]


def test_node_client_rejects_wrong_target_and_expired_command(tmp_path):
    private_key, public_key = generate_ed25519_keypair()
    journal = NodeJournal(tmp_path / "node.db")
    journal.init()
    client = NodeClient(
        journal, executor=lambda command: {"ok": True}, node_id="node-1",
        public_key=public_key, require_signature=True, clock=lambda: 100.0,
    )
    wrong = PlatformCommand.create(
        command_id="cmd-wrong-node", target_node="node-2",
        action="workspace.read", resource_id="workspace-1",
        arguments={"path": "a"}, retry_class="read_only", expires_at=200,
    ).signed(private_key).as_dict()
    expired = _command("cmd-expired", expires_at=99).signed(private_key).as_dict()
    assert client.handle(wrong)["reason"] == "invalid_signature"
    assert client.handle(expired)["reason"] == "invalid_signature"


def test_node_client_transport_poll_receipt_and_heartbeat(tmp_path):
    journal = NodeJournal(tmp_path / "node.db")
    journal.init()
    calls = []
    requests = []

    class FakeTransport:
        def post_json(self, url, body, headers):
            requests.append((url, body, headers))
            if url.endswith("/poll"):
                return 200, {"ok": True, "commands": [_command("cmd-net").as_dict()]}
            if url.endswith("/receipts"):
                return 200, {"ok": True}
            return 200, {"ok": True, "node": {"status": "online"}}

    client = NodeClient(
        journal, executor=lambda command: calls.append(command["command_id"]) or {"bytes": 1},
        node_id="node-1", credential="node-1:" + "x" * 40,
        hub_url="https://hub.example.test", transport=FakeTransport(),
        worker_id="worker-1",
    )
    polled = client.poll_once()
    assert polled == {"ok": True, "commands": 1, "receipts": 1}
    assert client.heartbeat()["ok"] is True
    assert calls == ["cmd-net"]
    assert requests[0][0].endswith("/poll")
    assert requests[1][1]["status"] == "succeeded"
    assert requests[0][2]["X-Platform-Node-Credential"].startswith("node-1:")


def test_node_crash_recovery_is_unknown_until_reconciliation(tmp_path):
    journal = NodeJournal(tmp_path / "node.db")
    journal.init()
    journal.begin("cmd-crashed", now=1)
    journal.recover_unknown(now=2)
    assert journal.get("cmd-crashed")["state"] == "unknown"


def test_expired_hub_lease_becomes_unknown_instead_of_redelivery(tmp_path):
    now = [10.0]
    repo = CommandRepository(tmp_path / "platform.db", clock=lambda: now[0])
    repo.init()
    repo.enqueue(_command(expires_at=100), idempotency_key="lease-1")
    assert repo.claim_for_node("node-1", "worker-1", lease_s=1)[0]["status"] == "leased"
    now[0] = 12.0
    assert repo.claim_for_node("node-1", "worker-2") == []
    assert repo.get("cmd-1")["status"] == "unknown"


def test_idempotency_key_cannot_cross_owner_scope(tmp_path):
    repo = CommandRepository(tmp_path / "platform.db")
    repo.init()
    repo.enqueue(_command(command_id="cmd-a").__class__.create(
        command_id="cmd-a", target_node="node-1", owner_id="owner-a",
        action="workspace.read", resource_id="workspace-1", arguments={"path": "report.md"},
        retry_class="read_only", expires_at=9999999999,
    ), idempotency_key="same-key")
    try:
        repo.enqueue(_command(command_id="cmd-b").__class__.create(
            command_id="cmd-b", target_node="node-1", owner_id="owner-b",
            action="workspace.read", resource_id="workspace-1", arguments={"path": "report.md"},
            retry_class="read_only", expires_at=9999999999,
        ), idempotency_key="same-key")
    except Exception as exc:
        assert getattr(exc, "code", None) == "idempotency_conflict"
    else:
        raise AssertionError("cross-owner idempotency key was accepted")


def test_enqueue_writes_command_and_outbox_atomically(tmp_path):
    repo = CommandRepository(tmp_path / "platform.db")
    repo.init()
    row = repo.enqueue(_command("cmd-outbox"), idempotency_key="outbox-1")
    items = repo.claim_outbox("dispatcher-1")
    assert len(items) == 1
    assert items[0]["command_id"] == row["command_id"]
    assert items[0]["payload"]["target_node"] == "node-1"
    assert repo.ack_outbox(items[0]["outbox_id"], "dispatcher-1") is True
    assert repo.get_outbox(items[0]["outbox_id"])["state"] == "sent"
    assert repo.claim_outbox("dispatcher-2") == []


def test_init_backfills_outbox_for_legacy_command_rows(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE platform_commands (
          command_id TEXT PRIMARY KEY, target_node TEXT NOT NULL, action TEXT NOT NULL,
          resource_id TEXT NOT NULL, arguments TEXT NOT NULL, retry_class TEXT NOT NULL,
          expires_at REAL NOT NULL, args_hash TEXT NOT NULL, run_id TEXT, grant_id TEXT,
          signature TEXT, status TEXT NOT NULL, lease_owner TEXT, lease_until REAL,
          attempt INTEGER NOT NULL DEFAULT 0, result TEXT, created_at REAL NOT NULL,
          updated_at REAL NOT NULL, idempotency_key TEXT UNIQUE
        );
    """)
    command = _command("legacy-command").as_dict()
    conn.execute(
        "INSERT INTO platform_commands(command_id,target_node,action,resource_id,arguments,retry_class,expires_at,args_hash,run_id,grant_id,signature,status,created_at,updated_at,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (command["command_id"], command["target_node"], command["action"], command["resource_id"], json.dumps(command["arguments"]), command["retry_class"], command["expires_at"], command["args_hash"], None, None, None, "queued", 1.0, 1.0, "legacy-key"),
    )
    conn.commit()
    conn.close()
    repo = CommandRepository(db_path)
    repo.init()
    items = repo.claim_outbox("legacy-dispatcher")
    assert len(items) == 1
    assert items[0]["command_id"] == "legacy-command"
    assert items[0]["payload"]["target_node"] == "node-1"


def test_outbox_expired_lease_is_reclaimable(tmp_path):
    now = [10.0]
    repo = CommandRepository(tmp_path / "platform.db", clock=lambda: now[0])
    repo.init()
    repo.enqueue(_command("cmd-reclaim"), idempotency_key="reclaim-1")
    first = repo.claim_outbox("dispatcher-1", lease_s=1)
    assert first[0]["state"] == "leased"
    now[0] = 12.0
    second = repo.claim_outbox("dispatcher-2")
    assert [item["command_id"] for item in second] == ["cmd-reclaim"]
    assert repo.ack_outbox(second[0]["outbox_id"], "wrong-worker") is False


def test_delivery_signs_commands_and_strict_repository_rejects_unsigned(tmp_path):
    private_key, public_key = generate_ed25519_keypair()
    repo = CommandRepository(tmp_path / "platform.db")
    repo.init()
    delivery = CommandDeliveryService(repo, signing_key=private_key, require_signature=True)
    row = delivery.enqueue(_command("cmd-signed-delivery"), idempotency_key="signed-1")
    assert row["signature"]
    assert verify_command(row, public_key)
    strict = CommandDeliveryService(repo, require_signature=True)
    try:
        strict.enqueue(_command("cmd-unsigned-delivery"), idempotency_key="unsigned-1")
    except Exception as exc:
        assert getattr(exc, "code", None) == "command_signature_required"
    else:
        raise AssertionError("unsigned command entered strict queue")


def test_delivery_waiter_fences_timeout_without_requeue(tmp_path):
    now = [10.0]
    repo = CommandRepository(tmp_path / "platform.db", clock=lambda: now[0])
    repo.init()
    repo.enqueue(_command("cmd-wait"), idempotency_key="wait-1")
    repo.claim_for_node("node-1", "worker-1")

    def sleep(_interval):
        now[0] += 1.0

    delivery = CommandDeliveryService(repo, clock=lambda: now[0], sleep=sleep)
    row = delivery.wait_for_receipt("cmd-wait", timeout_s=0.5, poll_interval_s=0.1)
    assert row["status"] == "unknown"
    assert repo.claim_for_node("node-1", "worker-2") == []
