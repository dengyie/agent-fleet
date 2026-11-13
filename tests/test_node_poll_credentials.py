"""Command lease acquisition fences the credential authenticated by HTTP."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from hub.application.command_delivery_service import CommandDeliveryService
from hub.application.task_service import ApplicationError
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.domain.capability_execution import RegisteredCapabilityAction
from fleet_core.platform_command import PlatformCommand
from hub.infrastructure.command_repository import CommandRepositoryError
from test_capability_dispatch import CAP, setup_dispatch
from test_capability_router import OWNER, WORKSPACE


POLL = "/api/platform/v1/nodes/poll"


def prepare_poll(tmp_path: Path, *, bound: bool) -> tuple[Any, Any, CommandDeliveryService, PlatformCommand]:
    fixture = setup_dispatch(tmp_path, action="tool.workspace.read")
    run_id = fixture.run() if bound else None
    config = replace(FleetConfig.from_root(tmp_path, platform_enabled=True, platform_workflows_enabled=True,
        platform_capability_routing_enabled=bound, platform_db=fixture.repository.workflows.db_path,
        dev_operator=OWNER, serve_frontend=False), platform_command_signing_raw=fixture.private)
    app = create_app(config, repositories={"workflows": fixture.repository.workflows,
        "capabilities": fixture.repository, "workflow_clock": lambda: fixture.clock[0],
        "workflow_capability_actions": (RegisteredCapabilityAction("cli", CAP, fixture.action),)})
    app.config["TESTING"] = True
    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    delivery.clock = delivery.repository.clock = lambda: fixture.clock[0]
    command = fixture.command(run_id) if bound else PlatformCommand.create(command_id="ordinary-read",
        owner_id=OWNER, target_node="node-a", action="tool.workspace.read", resource_id=WORKSPACE,
        arguments={"path": "report.txt"}, retry_class="read_only", expires_at=150)
    delivery.enqueue(command, idempotency_key=command.command_id)
    return fixture, app, delivery, command


@pytest.mark.parametrize("bound", [False, True], ids=["ordinary", "capability"])
@pytest.mark.parametrize("change", ["rotate", "disable_credential", "disable_node"])
def test_poll_rechecks_authenticated_credential_before_any_lease_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bound: bool, change: str,
) -> None:
    fixture, app, delivery, command = prepare_poll(tmp_path, bound=bound)
    expired = replace(command, command_id=command.command_id + "-expired")
    delivery.enqueue(expired, idempotency_key=expired.command_id)
    with fixture.repository.workflows.transaction() as connection:
        connection.execute("UPDATE platform_commands SET status='leased',lease_owner='old-worker',lease_until=103,attempt=1 WHERE command_id=?", (expired.command_id,))
        before_outbox = [tuple(row) for row in connection.execute("SELECT * FROM platform_command_outbox ORDER BY outbox_id")]
    before_expired = delivery.repository.get(expired.command_id)
    original = delivery.repository.claim_for_node
    current_credential = [fixture.credential]

    def revoke_before_transaction(*args: Any, **kwargs: Any) -> Any:
        if change == "rotate":
            current_credential[0] = fixture.platform.provision_node_credential(OWNER, "node-a", secret="changed" * 8)["credential"]
        else:
            table = "nodes" if change == "disable_node" else "node_credentials"
            with fixture.repository.workflows.transaction() as connection:
                connection.execute(f"UPDATE {table} SET enabled=0 WHERE owner_id=? AND node_id=?", (OWNER, "node-a"))
        return original(*args, **kwargs)

    monkeypatch.setattr(delivery.repository, "claim_for_node", revoke_before_transaction)
    client = app.test_client()
    response = client.post(POLL, json={"worker_id": "current-worker", "credential_fingerprint": "fake"},
        headers={"X-Platform-Node-Credential": fixture.credential})
    assert response.status_code == 409
    assert response.json["error"] == "command_credential_revoked"
    saved = delivery.repository.get(command.command_id)
    assert (saved["status"], saved["attempt"], saved["lease_owner"]) == ("queued", 0, None)
    assert delivery.repository.get(expired.command_id) == before_expired
    with fixture.repository.workflows.transaction() as connection:
        assert [tuple(row) for row in connection.execute("SELECT * FROM platform_command_outbox ORDER BY outbox_id")] == before_outbox
        if change != "rotate":
            table = "nodes" if change == "disable_node" else "node_credentials"
            connection.execute(f"UPDATE {table} SET enabled=1 WHERE owner_id=? AND node_id=?", (OWNER, "node-a"))
    monkeypatch.setattr(delivery.repository, "claim_for_node", original)
    if change == "rotate":
        assert client.post(POLL, json={"worker_id": "old-worker"}, headers={"X-Platform-Node-Credential": fixture.credential}).status_code == 403
    response = client.post(POLL, json={"worker_id": "current-worker"}, headers={"X-Platform-Node-Credential": current_credential[0]})
    assert response.status_code == 200
    assert [item["command_id"] for item in response.json["commands"]] == [command.command_id]
    assert delivery.repository.get(command.command_id)["attempt"] == 1
    assert delivery.repository.get(expired.command_id)["status"] == "unknown"


@pytest.mark.parametrize("owner,node,fingerprint", [
    (None, "node-a", "current"), ("other", "node-a", "current"),
    (OWNER, "other-node", "current"), (OWNER, "node-a", ""), (OWNER, "node-a", "0" * 64),
])
def test_repository_rejects_wrong_authenticated_scope_without_claiming(
    tmp_path: Path, owner: str | None, node: str, fingerprint: str,
) -> None:
    fixture, _, delivery, command = prepare_poll(tmp_path, bound=False)
    fingerprint = fixture.fingerprint if fingerprint == "current" else fingerprint
    with pytest.raises(CommandRepositoryError, match="command_credential_revoked"):
        delivery.repository.claim_for_node(node, "worker", owner_id=owner, credential_fingerprint=fingerprint)
    assert delivery.repository.get(command.command_id)["status"] == "queued"


def test_delivery_preserves_repository_credential_rejection_cause(tmp_path: Path) -> None:
    _, _, delivery, command = prepare_poll(tmp_path, bound=False)
    with pytest.raises(ApplicationError) as result:
        delivery.poll("node-a", "worker", owner_id=OWNER, credential_fingerprint="0" * 64)
    assert result.value.code == "command_credential_revoked"
    assert isinstance(result.value.__cause__, CommandRepositoryError)
    assert delivery.repository.get(command.command_id)["attempt"] == 0


def test_valid_fingerprint_and_legacy_internal_poll_keep_original_semantics(tmp_path: Path) -> None:
    fixture, _, delivery, command = prepare_poll(tmp_path, bound=False)
    rows = delivery.poll("node-a", "worker", owner_id=OWNER, credential_fingerprint=fixture.fingerprint)
    assert [row["command_id"] for row in rows] == [command.command_id]
    assert delivery.poll("node-a", "worker", owner_id=OWNER) == []


def test_failed_claim_rolls_back_expired_lease_and_preserves_sqlite_cause(tmp_path: Path) -> None:
    fixture, _, delivery, command = prepare_poll(tmp_path, bound=False)
    expired = replace(command, command_id="expired-read")
    delivery.enqueue(expired, idempotency_key=expired.command_id)
    with fixture.repository.workflows.transaction() as connection:
        connection.execute("UPDATE platform_commands SET status='leased',lease_owner='old-worker',lease_until=103,attempt=1 WHERE command_id=?", (expired.command_id,))
        connection.execute("CREATE TRIGGER fail_poll BEFORE UPDATE OF status ON platform_commands WHEN NEW.status='leased' BEGIN SELECT RAISE(ABORT,'claim write failed'); END")
    before = delivery.repository.get(expired.command_id)
    with pytest.raises(CommandRepositoryError) as result:
        delivery.repository.claim_for_node("node-a", "worker", owner_id=OWNER, credential_fingerprint=fixture.fingerprint)
    assert result.value.code == "command_store"
    assert isinstance(result.value.__cause__, sqlite3.IntegrityError)
    assert delivery.repository.get(command.command_id)["status"] == "queued"
    assert delivery.repository.get(expired.command_id) == before
