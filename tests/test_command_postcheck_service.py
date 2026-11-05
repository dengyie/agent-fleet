"""Deterministic workspace post-checks for ambiguous (unknown) write commands."""
from __future__ import annotations

import hashlib

import pytest

from hub.application.command_postcheck_service import (
    KIND, POSTCHECK_ACTION, CommandPostcheckService,
)
from hub.application.task_service import ApplicationError

OWNER = "alice@example.com"
CONTENT = "hello\n"
DIGEST = hashlib.sha256(CONTENT.encode()).hexdigest()


class StoreError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class FakeRepository:
    def __init__(self):
        self.commands: dict[tuple[str, str], dict] = {}
        self.postchecks: dict[tuple[str, str, str], dict] = {}
        self.reconciliations: list[dict] = []
        self.fail_with: str | None = None

    def add_command(self, owner, command):
        self.commands[(owner, command["command_id"])] = command

    def get_for_owner(self, owner, command_id):
        if self.fail_with:
            raise StoreError(self.fail_with)
        return self.commands.get((owner, command_id))

    def create_postcheck(self, owner, command_id, *, kind, check_command_id, path, expected_sha256, now):
        key = (owner, command_id, kind)
        self.postchecks.setdefault(key, {
            "command_id": command_id, "kind": kind, "check_command_id": check_command_id,
            "path": path, "expected_sha256": expected_sha256, "state": "pending",
            "result": None, "evidence_id": None, "created_at": now, "updated_at": now,
        })
        return dict(self.postchecks[key])

    def get_postcheck(self, owner, command_id, *, kind):
        row = self.postchecks.get((owner, command_id, kind))
        return dict(row) if row else None

    def finish_postcheck(self, owner, command_id, *, kind, state, result, evidence_id, now):
        row = self.postchecks[(owner, command_id, kind)]
        row.update(state=state, result=result, evidence_id=evidence_id, updated_at=now)
        return dict(row)

    def record_reconciliation(self, owner, command_id, **kwargs):
        self.reconciliations.append({"owner": owner, "command_id": command_id, **kwargs})


class FakeDelivery:
    def __init__(self):
        self.enqueued = []

    def enqueue(self, command, *, idempotency_key):
        self.enqueued.append((command, idempotency_key))


def _write_command(**overrides):
    command = {"command_id": "cmd-1", "status": "unknown", "action": "tool.workspace.write",
               "arguments": {"path": "notes.txt", "content": CONTENT},
               "target_node": "node-a", "resource_id": "ws-1", "run_id": None}
    command.update(overrides)
    return command


@pytest.fixture
def setup():
    repo, delivery = FakeRepository(), FakeDelivery()
    repo.add_command(OWNER, _write_command())
    service = CommandPostcheckService(repo, delivery, clock=lambda: 2000.0)
    return service, repo, delivery


def _complete_check(repo, service, status, result):
    check_id = service._check_command_id("cmd-1")
    repo.add_command(OWNER, {"command_id": check_id, "status": status, "result": result})
    return check_id


def test_request_enqueues_one_read_only_check(setup):
    service, repo, delivery = setup
    first = service.request(OWNER, "cmd-1")["postcheck"]
    assert first["state"] == "pending" and first["kind"] == KIND and first["path"] == "notes.txt"
    command, key = delivery.enqueued[0]
    assert command.action == POSTCHECK_ACTION and command.retry_class == "read_only"
    assert command.arguments == {"path": "notes.txt"} and command.target_node == "node-a"
    assert key == command.command_id == first["check_command_id"]
    assert repo.postchecks[(OWNER, "cmd-1", KIND)]["expected_sha256"] == DIGEST
    # The check id is deterministic, so a repeated request converges on it.
    assert service.request(OWNER, "cmd-1")["postcheck"]["check_command_id"] == first["check_command_id"]


@pytest.mark.parametrize(("status", "result", "state", "outcome"), [
    ("succeeded", {"result": {"path": "notes.txt", "sha256": DIGEST, "size": 6}}, "matched", "confirmed_succeeded"),
    ("succeeded", {"result": {"path": "notes.txt", "sha256": "0" * 64, "size": 1}}, "mismatch", "remains_unknown"),
    ("failed", {"error_code": "not_found"}, "remains_unknown", "remains_unknown"),
    ("expired", {}, "remains_unknown", "remains_unknown"),
])
def test_check_receipt_classifies_original_write(setup, status, result, state, outcome):
    service, repo, _ = setup
    service.request(OWNER, "cmd-1")
    check_id = _complete_check(repo, service, status, result)
    public = service.get(OWNER, "cmd-1")["postcheck"]
    assert public["state"] == state
    assert public["evidence_id"] == "postcheck-" + check_id
    [audit] = repo.reconciliations
    assert audit["outcome"] == outcome and audit["reconciliation_id"] == public["evidence_id"]
    if status == "failed":
        assert public["result"] == {"error_code": "not_found"}
    # A settled post-check is not re-evaluated or re-audited.
    service.get(OWNER, "cmd-1")
    assert len(repo.reconciliations) == 1


def test_running_check_stays_pending(setup):
    service, repo, _ = setup
    service.request(OWNER, "cmd-1")
    _complete_check(repo, service, "delivered", {})
    assert service.get(OWNER, "cmd-1")["postcheck"]["state"] == "pending"
    assert repo.reconciliations == []


@pytest.mark.parametrize(("overrides", "code", "status"), [
    ({"status": "succeeded"}, "reconcile_requires_unknown", 409),
    ({"action": "tool.shell.run"}, "postcheck_not_supported", 409),
    ({"arguments": {"path": "a", "content": "b", "mode": "x"}}, "postcheck_not_supported", 409),
    ({"arguments": {"path": "", "content": "b"}}, "postcheck_not_supported", 409),
])
def test_request_rejects_non_deterministic_commands(setup, overrides, code, status):
    service, repo, delivery = setup
    repo.add_command(OWNER, _write_command(**overrides))
    with pytest.raises(ApplicationError) as caught:
        service.request(OWNER, "cmd-1")
    assert (caught.value.code, caught.value.status) == (code, status)
    assert delivery.enqueued == []


def test_unknown_command_and_missing_postcheck_are_404(setup):
    service, _, _ = setup
    with pytest.raises(ApplicationError) as caught:
        service.request("bob@example.com", "cmd-1")
    assert (caught.value.code, caught.value.status) == ("command_not_found", 404)
    with pytest.raises(ApplicationError) as caught:
        service.get(OWNER, "cmd-1")
    assert (caught.value.code, caught.value.status) == ("postcheck_not_found", 404)


def test_store_errors_are_translated(setup):
    service, repo, _ = setup
    repo.fail_with = "command_conflict"
    with pytest.raises(ApplicationError) as caught:
        service.request(OWNER, "cmd-1")
    assert (caught.value.code, caught.value.status) == ("command_conflict", 409)
    repo.fail_with = "disk_io"
    with pytest.raises(ApplicationError) as caught:
        service.request(OWNER, "cmd-1")
    assert caught.value.status == 503
