"""Only the original Linux validation target and receive receipt can dispatch."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from typing import Any

import pytest

from hub.application.task_service import ApplicationError
from hub.application.workflow_dispatch import WorkflowDispatchError, WorkflowDispatchService
from hub.domain.capability import CapabilityStoreError
from fleet_core.platform_command import PlatformCommand
from hub.domain.remote_workflow_test import REMOTE_TEST_ACTION, remote_test_command_id
from hub.domain.workflow_contract_types import CatalogRef
from hub.domain.workflow_errors import WorkflowValidationError
from hub.infrastructure.capability_workflow import CapabilityWorkflowAdmission
from hub.infrastructure.workflow_repository import WorkflowRepositoryError
from hub.infrastructure.workflow_run_executor import RegisteredRunTarget
from test_remote_workflow_test_execution import RemoteExecutionFixture
from test_workflow_repository import OWNER, WORKSPACE


def target(**changes: Any) -> RegisteredRunTarget:
    values = dict(owner_id=OWNER, workspace_id=WORKSPACE, target_key="node-a", adapter_ref=CatalogRef("fixed-tests", 1),
        policy_ref=CatalogRef("coding-delivery", 1), conversation_id="test-execution", config_snapshot=b'{"execution_node_id":"node-a"}',
        test_recipe=CatalogRef("coding-delivery-tests", 1), remote_test=True)
    return RegisteredRunTarget(**(values | changes))


@pytest.mark.parametrize("changes", [
    {"test_recipe": CatalogRef("coding.test", 1)}, {"test_recipe": CatalogRef("coding-delivery-tests", 2)},
    {"test_recipe": None}, {"target_key": "local", "config_snapshot": b'{"execution_node_id":"local"}'},
    {"config_snapshot": b'{}'}, {"config_snapshot": b'{"execution_node_id":"node-b"}'},
    {"config_snapshot": b'{"execution_node_id":"node-a","argv":["sh"]}'},
    {"config_snapshot": b'{"execution_node_id":"node-a","workflow_remote_test":{"revision":1}}'},
    {"remote_test": 1},
])
def test_invalid_remote_target_registration_is_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(WorkflowValidationError):
        target(**changes)


def test_receive_precedes_binding_admission_and_run(tmp_path: Path) -> None:
    fixture = RemoteExecutionFixture(tmp_path)
    fixture.advance()
    fixture.advance()
    step = fixture.repository.current_steps(OWNER, "workflow-a")[2]
    assert (step.state, step.reason_code, step.target_key) == ("waiting_node", "inputs_receiving", "node-a")
    with fixture.repository.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM platform_commands").fetchone()[0] == 1
        for table in ("runs", "capability_step_bindings", "capability_reservations"):
            scope = "" if table == "runs" else " WHERE step_key='test'"
            assert connection.execute(f"SELECT count(*) FROM {table}" + scope).fetchone()[0] == 0
    fixture.receive()
    fixture.advance()
    assert fixture.repository.current_steps(OWNER, "workflow-a")[2].state == "ready"
    fixture.advance()
    assert fixture.reservation() == "held"
    assert fixture.target.hub_input_target() is None


@pytest.mark.parametrize("os", [(), ("windows",), ("linux", "windows")])
def test_remote_validation_requires_exact_linux_route(tmp_path: Path, os: tuple[str, ...]) -> None:
    fixture = RemoteExecutionFixture(tmp_path)
    fixture.route = replace(fixture.route, selection=replace(fixture.route.selection, operating_systems=os))
    with pytest.raises(CapabilityStoreError, match="unsupported_capability_target"):
        fixture.advance()


@pytest.mark.parametrize("kind", ["native", "local_test"])
def test_native_and_local_test_targets_cannot_satisfy_remote_route(tmp_path: Path, kind: str) -> None:
    fixture = RemoteExecutionFixture(tmp_path)
    alternate = replace(fixture.target, remote_test=False, test_recipe=None) if kind == "native" else replace(
        fixture.target, remote_test=False, config_snapshot=b"{}")
    fixture.admission = CapabilityWorkflowAdmission(fixture.capabilities, targets=(alternate,), inputs=fixture.inputs)
    with pytest.raises(CapabilityStoreError, match="executor_unavailable"):
        fixture.advance()


def test_remote_run_cannot_bypass_original_capability_admission(tmp_path: Path) -> None:
    fixture = RemoteExecutionFixture(tmp_path)
    fixture.advance()
    fixture.receive()
    fixture.advance()
    revision = fixture.repository.get(OWNER, "workflow-a").revision
    fixture.repository.admit_step(fixture.claim, step_key="test", generation=1, expected_revision=revision,
        target_key="node-a", capacity=1, reserve_tokens=0, reserve_requests=0, now=fixture.now)
    with pytest.raises(WorkflowDispatchError, match="capability_unavailable"):
        asyncio.run(WorkflowDispatchService(fixture.repository, fixture.executor, clock=fixture.clock).dispatch(
            fixture.claim, step_key="test", generation=1, expected_revision=fixture.repository.get(OWNER, "workflow-a").revision))
    with fixture.repository.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


@pytest.mark.parametrize("mutation", ["generation", "token", "binding", "subject", "receipt", "recipe", "retry", "id", "action", "receive_status"])
def test_actual_command_preparation_rechecks_each_original_identity(tmp_path: Path, mutation: str) -> None:
    fixture = RemoteExecutionFixture(tmp_path)
    fixture.dispatch()
    claim = fixture.platform.claim_run(OWNER, worker_id="test-worker", now=fixture.now)
    lease = fixture.lease(claim)
    delivery_target = fixture.delivery.target_for_remote_test(lease, now=fixture.now)
    context, revision, _ = asyncio.run(fixture.service.source_context(delivery_target))
    request = fixture.delivery.remote_test_request(delivery_target, source_revision=revision,
        subject_digest=context.source_subject, now=fixture.now).payload()
    changes = {"generation": {"generation": 2}, "token": {"dispatch_token": "0" * 64},
        "binding": {"binding_digest": "0" * 64}, "subject": {"subject_digest": "0" * 64},
        "receipt": {"receive_receipt": request["receive_receipt"] | {"grant_id": "0" * 64}}, "recipe": {"recipe_id": "shell"}}
    request.update(changes.get(mutation, {}))
    if mutation == "receive_status":
        with fixture.repository.transaction() as connection:
            connection.execute("UPDATE platform_commands SET status='unknown' WHERE action='artifact.receive'")
    command = PlatformCommand.create(command_id="another" if mutation == "id" else remote_test_command_id(delivery_target.binding.token),
        owner_id=OWNER, resource_id=WORKSPACE, target_node="node-a", action="tool.workspace.read" if mutation == "action" else REMOTE_TEST_ACTION,
        retry_class="read_only" if mutation == "retry" else "manual_only", run_id=lease.run_id,
        arguments=request, expires_at=fixture.now + 90)
    with pytest.raises(ApplicationError):
        fixture.command_delivery.enqueue(command, idempotency_key=command.command_id)
    assert fixture.commands.get(command.command_id) is None


def test_remote_lookup_rejects_tampered_persisted_config(tmp_path: Path) -> None:
    fixture = RemoteExecutionFixture(tmp_path)
    fixture.dispatch()
    claim = fixture.platform.claim_run(OWNER, worker_id="test-worker", now=fixture.now)
    config = claim["config_snapshot"] | {"workflow_remote_test": {"revision": True}}
    with fixture.repository.transaction() as connection:
        connection.execute("UPDATE runs SET config_snapshot=? WHERE run_id=?", (json.dumps(config), claim["run_id"]))
    with pytest.raises(WorkflowRepositoryError, match="binding_conflict"):
        fixture.delivery.target_for_remote_test(fixture.lease(claim), now=fixture.now)


def test_remote_run_pins_reader_before_old_workers_can_open_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hub.infrastructure import workflow_schema

    fixture = RemoteExecutionFixture(tmp_path)
    fixture.dispatch()
    with fixture.repository.transaction(write=False) as connection:
        assert connection.execute("SELECT minimum_reader_version FROM workflow_schema_meta").fetchone()[0] >= 10
        monkeypatch.setattr(workflow_schema, "READER_VERSION", 9)
        with pytest.raises(workflow_schema.WorkflowSchemaError, match="unsupported_workflow_schema"):
            workflow_schema.inspect_schema(connection)


@pytest.mark.parametrize("encrypted", [False, True])
def test_remote_run_backup_restores_reader_ten_frozen_without_dispatch(tmp_path: Path, encrypted: bool) -> None:
    from hub.infrastructure.platform_backup import PlatformBackupService
    from hub.infrastructure.workflow_repository import WorkflowRepository
    from hub.infrastructure.workflow_run_executor import RunStepExecutor

    fixture = RemoteExecutionFixture(tmp_path)
    observation = fixture.dispatch()
    backup, restored = tmp_path / "remote-backup", tmp_path / "remote-restored"
    service = PlatformBackupService()
    if encrypted:
        summary = service.create_encrypted_backup(fixture.repository.db_path, fixture.store.root, backup, b"k" * 32, "remote-key")["source"]
        service.restore_encrypted_backup(backup, restored, b"k" * 32)
    else:
        summary = service.create_backup(fixture.repository.db_path, fixture.store.root, backup)
        service.restore_backup(backup, restored)
    assert summary["workflow"]["minimum_reader_version"] == 10
    repository = WorkflowRepository(restored / "platform.db")
    repository.init()
    with repository.transaction(write=False) as connection:
        assert tuple(connection.execute("SELECT minimum_reader_version,admission_mode FROM workflow_schema_meta").fetchone()) == (10, "frozen")
        assert connection.execute("SELECT state FROM capability_reservations WHERE step_key='test'").fetchone()[0] == "held"
        row = connection.execute("SELECT action,status FROM platform_commands").fetchone()
        assert tuple(row) == ("artifact.receive", "succeeded")
    executor = RunStepExecutor(repository, fixture.target, clock=fixture.clock)
    assert asyncio.run(executor.lookup(observation.binding.token, observation.binding.payload_hash, fixture.resolved())) == observation.binding
    assert fixture.executions == 0 and fixture.test_command() is None
