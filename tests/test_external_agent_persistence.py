"""External admission, passive assembly, bounded backup and process recovery."""
from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import sqlite3
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hub.application.workflow_coordinator import RegisteredWorkflowStep, WorkflowCoordinator
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.domain.workflow_contract import parse_definition, resolve_contract
from hub.domain.external_agent import ExternalAgentStatus
from hub.domain.workflow_contract_types import AuthorizedInputs, CatalogRef
from hub.domain.workflow_external import RegisteredExternalTarget
from hub.domain.workflow_identity import canonical_json, dispatch_token
from hub.infrastructure.platform_backup import BackupError, PlatformBackupService
from hub.composition.workflows import assert_workflow_visibility
from hub.infrastructure.workflow_external_repository import ExternalWorkflowRepository
from hub.infrastructure.workflow_repository import WorkflowRepository, WorkflowRepositoryError
from hub.infrastructure.workflow_schema import WorkflowSchemaError, inspect_schema
from tools.browser_bridge.muse import MuseBridge
from connectors.external_agents.muse import MuseExternalAgentAdapter
from test_external_agent_workflow import ExternalSetup, external, held, make_external
from test_muse_browser_bridge import Transport
from test_workflow_repository import OWNER, WORKSPACE


def test_external_missing_admission_pin_cannot_create_intent_or_reservation(tmp_path: Path) -> None:
    setup = make_external(tmp_path, admit=False)
    with pytest.raises(WorkflowRepositoryError, match="external_registration_required"):
        setup.admit()
    with setup.repository.transaction(write=False) as connection:
        for name in ("workflow_dispatches", "workflow_external_bindings", "capability_reservations", "workflow_budget_reservations"):
            assert connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0] == 0


def test_external_pin_failure_rolls_back_original_admission_atomically(tmp_path: Path) -> None:
    setup = make_external(tmp_path, admit=False)
    with setup.repository.transaction() as connection:
        connection.execute("CREATE TRIGGER external_pin_failure BEFORE INSERT ON workflow_external_bindings BEGIN SELECT RAISE(ABORT,'injected'); END")
    before = setup.repository.get(OWNER, "workflow-a")
    with pytest.raises(WorkflowRepositoryError, match="workflow_store") as error:
        setup.admit(setup.target)
    assert isinstance(error.value.__cause__, sqlite3.Error)
    assert setup.repository.get(OWNER, "workflow-a") == before
    with setup.repository.transaction(write=False) as connection:
        for name in ("workflow_dispatches", "workflow_external_bindings", "capability_reservations", "workflow_budget_reservations"):
            assert connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0] == 0


@pytest.mark.parametrize("changes,expected", [
    ({"output_specs": [{"output_key": "result", "kind": "artifact", "required": True, "max_bytes": 100}]}, "external_contract_unsupported"),
    ({"capability_requirements": [{"id": "workspace.read", "revisions": [1]}]}, "external_capability_unavailable"),
])
def test_external_contract_is_rejected_before_send(tmp_path: Path, changes: dict[str, Any], expected: str) -> None:
    setup = make_external(tmp_path, admit=False, contract_changes=changes)
    with pytest.raises(WorkflowRepositoryError, match=expected):
        setup.admit(setup.target)
    assert setup.transport.sends == []
    with setup.repository.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM workflow_dispatches").fetchone()[0] == 0


@pytest.mark.parametrize("encrypted", [False, True])
def test_external_backup_and_restore_keep_original_task_and_reader_frozen(external: ExternalSetup, tmp_path: Path, encrypted: bool) -> None:
    original = external.dispatch()
    external.apply(original)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    backup, restored = tmp_path / "backup", tmp_path / "restore"
    service = PlatformBackupService()
    if encrypted:
        created = service.create_encrypted_backup(external.repository.db_path, artifacts, backup, b"e" * 32, "external-key")
        summary = created["source"]
        service.restore_encrypted_backup(backup, restored, b"e" * 32)
    else:
        summary = service.create_backup(external.repository.db_path, artifacts, backup)
        service.restore_backup(backup, restored)
    assert summary["workflow"] == {"schema_version": 1, "minimum_reader_version": 6}
    workflows = WorkflowRepository(restored / "platform.db")
    workflows.init()
    store = ExternalWorkflowRepository(workflows)
    pin = store.read_binding(original.binding, external.target)
    assert pin.task.dispatch_token == original.binding.token
    with workflows.transaction(write=False) as connection:
        assert inspect_schema(connection)["admission_mode"] == "frozen"
        store.validate_snapshot(connection)
    with pytest.raises(WorkflowRepositoryError, match="workflow_domain_in_use"):
        assert_workflow_visibility(workflows.db_path, enabled=False)
    assert len(external.transport.sends) == 1


@pytest.mark.parametrize("change,code", [
    ("UPDATE workflow_external_schema_meta SET schema_version=99", "unsupported_external_schema"),
    ("UPDATE workflow_external_schema_meta SET minimum_reader_version=4", "unsupported_external_schema"),
    ("UPDATE workflow_schema_meta SET minimum_reader_version=2", "invalid_external_reader"),
    ("UPDATE workflow_schema_meta SET minimum_reader_version=99", "unsupported_workflow_schema"),
])
def test_external_gate_off_readers_and_backup_reject_incompatible_schema(external: ExternalSetup, tmp_path: Path, change: str, code: str) -> None:
    with external.repository.transaction() as connection:
        connection.execute(change)
    with pytest.raises(WorkflowRepositoryError, match=code):
        external.repository.init()
    with pytest.raises(WorkflowRepositoryError, match="workflow_visibility_unavailable"):
        assert_workflow_visibility(external.repository.db_path, enabled=False)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    with pytest.raises(BackupError, match=code):
        PlatformBackupService().create_backup(external.repository.db_path, artifacts, tmp_path / "backup")


@pytest.mark.parametrize("change", ["digest", "task_without_send", "payload", "foreign_task", "noncanonical", "resolved_noncanonical"])
def test_external_snapshot_corruption_blocks_restore_and_io(external: ExternalSetup, tmp_path: Path, change: str) -> None:
    token = dispatch_token(OWNER, "workflow-a", "develop", 1)
    if change == "foreign_task":
        external.dispatch()
    with external.repository.transaction() as connection:
        if change == "digest":
            connection.execute("UPDATE workflow_external_bindings SET target_digest=?", ("a" * 64,))
        elif change == "payload":
            connection.execute("UPDATE workflow_dispatches SET payload_hash=?", ("b" * 64,))
        elif change == "noncanonical":
            connection.execute("UPDATE workflow_external_bindings SET target_snapshot=?", (json.dumps(json.loads(external.target.snapshot)).encode(),))
        elif change == "resolved_noncanonical":
            connection.execute("UPDATE workflow_steps SET resolved_snapshot=?", (json.dumps(json.loads(external.resolved.snapshot)).encode(),))
        else:
            task = {"agent_id": "muse", "dispatch_token": "foreign" if change == "foreign_task" else token,
                    "external_id": token, "session_id": None}
            connection.execute("UPDATE workflow_external_bindings SET task_snapshot=?", (canonical_json(task),))
    with external.repository.transaction(write=False) as connection:
        with pytest.raises(WorkflowRepositoryError, match="external_snapshot_invalid"):
            external.store.validate_snapshot(connection)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    with pytest.raises(BackupError, match="external_snapshot_invalid"):
        PlatformBackupService().create_backup(external.repository.db_path, artifacts, tmp_path / "backup")


def test_external_reader_and_capability_reader_remain_independent(external: ExternalSetup) -> None:
    from hub.infrastructure.capability_repository import CapabilityRepository

    CapabilityRepository(external.repository, boot_id="external-tests").init()
    with external.repository.transaction(write=False) as connection:
        assert inspect_schema(connection)["minimum_reader_version"] == 6
    external.repository.init()
    external.store.init()


def registration(setup: ExternalSetup) -> RegisteredWorkflowStep:
    return RegisteredWorkflowStep(setup.repository.get(OWNER, "workflow-a").definition_digest, setup.resolved,
        setup.target.target_key, 1, 100, 2, external_target=setup.target)


def test_original_coordinator_admits_external_pin_and_observes_original_task(tmp_path: Path) -> None:
    setup = make_external(tmp_path, admit=False)
    setup.repository.release_claim(setup.claim, now=setup.clock(), next_poll_at=setup.clock())
    coordinator = WorkflowCoordinator(setup.repository, worker_id="coordinator", clock=setup.clock,
        registrations=(registration(setup),), executors=((OWNER, WORKSPACE, setup.target.target_key, setup.executor()),),
        dispatch_enabled=True, external_agents_enabled=True)
    for _ in range(2):
        result = asyncio.run(coordinator.tick(setup.clock()))
        assert not result.failures
        setup.clock.now += 1
    assert setup.repository.get(OWNER, "workflow-a").execution_state == "waiting_external"
    assert len(setup.transport.sends) == 1 and held(setup) == "held"
    with setup.repository.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert connection.execute("SELECT target_digest FROM workflow_external_bindings").fetchone()[0] == setup.target.digest


def test_external_coordinator_gate_off_cannot_admit_or_reserve(tmp_path: Path) -> None:
    setup = make_external(tmp_path, admit=False)
    setup.repository.release_claim(setup.claim, now=setup.clock(), next_poll_at=setup.clock())
    coordinator = WorkflowCoordinator(setup.repository, worker_id="coordinator", clock=setup.clock,
        registrations=(registration(setup),), executors=((OWNER, WORKSPACE, setup.target.target_key, setup.executor()),),
        dispatch_enabled=True)
    result = asyncio.run(coordinator.tick(setup.clock()))
    assert not result.failures and not setup.transport.sends
    with setup.repository.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM workflow_external_bindings").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM capability_reservations").fetchone()[0] == 0


def test_external_coordinator_gate_off_also_blocks_previously_admitted_intent(external: ExternalSetup) -> None:
    external.repository.release_claim(external.claim, now=external.clock(), next_poll_at=external.clock())
    coordinator = WorkflowCoordinator(external.repository, worker_id="gate-off", clock=external.clock,
        executors=((OWNER, WORKSPACE, external.target.target_key, external.executor(enabled=True)),),
        dispatch_enabled=True, external_agents_enabled=False)
    result = asyncio.run(coordinator.tick(external.clock()))
    assert not result.failures
    assert not external.transport.sends
    assert held(external) == "held"


def test_external_wait_and_unsupported_stop_expire_to_unknown_without_replay(external: ExternalSetup) -> None:
    external.apply(external.dispatch())
    external.repository.release_claim(external.claim, now=external.clock(), next_poll_at=external.clock())
    coordinator = WorkflowCoordinator(external.repository, worker_id="deadline", clock=external.clock,
        executors=((OWNER, WORKSPACE, external.target.target_key, external.executor(enabled=False)),))
    external.clock.now = 223
    result = asyncio.run(coordinator.tick(external.clock()))
    assert not result.failures
    assert external.repository.get(OWNER, "workflow-a").execution_state == "cancelling"
    external.clock.now = 254
    result = asyncio.run(coordinator.tick(external.clock()))
    assert not result.failures
    assert external.repository.get(OWNER, "workflow-a").execution_state == "unknown"
    assert held(external) == "held" and len(external.transport.sends) == 1


def test_external_explicit_reconciliation_reads_terminal_without_submit(external: ExternalSetup) -> None:
    from hub.domain.workflow_control import WorkflowControlRequest
    from hub.infrastructure.workflow_control_repository import WorkflowControlRepository
    from test_muse_browser_bridge import message

    first = external.dispatch()
    external.adapter.inspect = lambda task: ExternalAgentStatus("unknown", "unknown", task.external_id)
    workflow = external.apply(external.dispatch())
    assert workflow.execution_state == "unknown"
    del external.adapter.inspect
    journal = external.adapter.bridge.lookup(first.binding.token, external.resolved.snapshot.decode(), scope=WORKSPACE)
    external.transport.messages.append(message("done", "assistant", "Done\n" + journal["completion_marker"]))
    WorkflowControlRepository(external.repository).control(WorkflowControlRequest(OWNER, "workflow-a", "reconcile",
        workflow.revision, "inspect-original", "develop", 1), now=external.clock())
    external.repository.release_claim(external.claim, now=external.clock(), next_poll_at=external.clock())
    coordinator = WorkflowCoordinator(external.repository, worker_id="reconcile", clock=external.clock,
        executors=((OWNER, WORKSPACE, external.target.target_key, external.executor(enabled=False)),))
    result = asyncio.run(coordinator.tick(external.clock()))
    assert not result.failures
    workflow = external.repository.get(OWNER, "workflow-a")
    assert workflow.execution_state == "succeeded" and workflow.acceptance_state == "pending"
    assert held(external) == "released" and len(external.transport.sends) == 1


def test_external_historical_backup_scan_is_keyset_bounded_and_checks_last_batch(external: ExternalSetup) -> None:
    definition = parse_definition(external.repository.get(OWNER, "workflow-a").definition_snapshot)
    for index in range(128):
        workflow = "history-" + str(index)
        external.repository.create_or_get(OWNER, WORKSPACE, workflow, client_token=workflow, definition=definition, now=100)
        claim = external.repository.claim_due(OWNER, worker_id="historical", now=100, limit=1, lease_seconds=20)[0]
        ready = external.repository.freeze_inputs(claim, step_key="develop", generation=1, resolved=external.resolved,
            expected_revision=1, now=101)
        external.repository.admit_step(claim, step_key="develop", generation=1, expected_revision=ready.revision,
            target_key=external.target.target_key, capacity=1024, reserve_tokens=100, reserve_requests=2,
            now=102, external_target=external.target)
    with external.repository.transaction() as connection:
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        external.store.validate_snapshot(connection)
        batches = [statement for statement in statements if "ORDER BY e.rowid LIMIT 128" in statement]
        assert len(batches) == 3 and all("LIMIT 128" in statement for statement in batches)
        connection.set_trace_callback(None)
        connection.execute("UPDATE workflow_external_bindings SET target_digest=? WHERE rowid=(SELECT max(rowid) FROM workflow_external_bindings)", ("f" * 64,))
        with pytest.raises(WorkflowRepositoryError, match="external_snapshot_invalid"):
            external.store.validate_snapshot(connection)


@pytest.mark.parametrize("enabled", [False, True])
def test_external_bootstrap_is_passive_and_only_explicit_gate_creates_metadata(tmp_path: Path, enabled: bool) -> None:
    before = {thread.ident for thread in threading.enumerate()}
    config = FleetConfig.from_root(tmp_path, platform_enabled=True, platform_workflows_enabled=True,
        platform_external_agents_enabled=enabled, dev_operator=OWNER, serve_frontend=False)
    app = create_app(config)
    assert app.extensions["fleet"]["services"]["workflow_coordinator"].external_agents_enabled is enabled
    assert before == {thread.ident for thread in threading.enumerate()}
    with sqlite3.connect(config.platform_db) as connection:
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE name='workflow_external_bindings'").fetchone() is not None
        assert exists is enabled


def test_external_bootstrap_assembles_only_explicit_admin_target(external: ExternalSetup, tmp_path: Path) -> None:
    config = FleetConfig.from_root(tmp_path, platform_enabled=True, platform_workflows_enabled=True,
        platform_db=external.repository.db_path, platform_external_agents_enabled=True, dev_operator=OWNER, serve_frontend=False)
    before = {thread.ident for thread in threading.enumerate()}
    app = create_app(config, repositories={"workflow_clock": external.clock, "workflow_external_agents": ((external.target, external.adapter),)})
    coordinator = app.extensions["fleet"]["services"]["workflow_coordinator"]
    assert len(coordinator.dispatchers) == 1 and not external.transport.sends
    assert before == {thread.ident for thread in threading.enumerate()}


def _process_dispatch(db: str, snapshot: bytes, resolved_snapshot: bytes, output: Any) -> None:
    try:
        from hub.application.workflow_external_execution import ExternalStepExecutor
        from hub.domain.workflow_contract import parse_resolved
        from hub.domain.workflow_external import parse_external_target

        target = parse_external_target(snapshot)
        resolved = parse_resolved(resolved_snapshot)
        workflows = WorkflowRepository(Path(db))
        adapter = MuseExternalAgentAdapter(MuseBridge(Path(db).with_name("muse.db"), transport=Transport()),
            workspace_id=WORKSPACE, account_ref="account-a", transport_ref="fixture-v1")
        executor = ExternalStepExecutor(ExternalWorkflowRepository(workflows), target, adapter, clock=lambda: 104, enabled=True)
        binding = asyncio.run(executor.create_or_lookup(dispatch_token(OWNER, "workflow-a", "develop", 1), resolved.digest, resolved))
        output.put(("ok", binding.execution_id))
    except Exception as exc:
        output.put(("error", getattr(exc, "code", type(exc).__name__)))


def test_two_process_external_recovery_keeps_one_journal_task_and_binding(external: ExternalSetup) -> None:
    # Commit sending exactly as the original dispatcher does; race concrete adapters.
    external.repository.begin_dispatch(external.claim, step_key="develop", generation=1,
        expected_revision=external.repository.get(OWNER, "workflow-a").revision, now=104)
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    processes = [context.Process(target=_process_dispatch, args=(str(external.repository.db_path), external.target.snapshot, external.resolved.snapshot, output)) for _ in range(2)]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(15)
            assert not process.is_alive() and process.exitcode == 0
        results = [output.get(timeout=2) for _ in processes]
        assert any(item[0] == "ok" for item in results)
        assert all(item[0] == "ok" or item[1] in {"external_lookup_unavailable", "external_submit_unknown"} for item in results)
        with sqlite3.connect(external.adapter.bridge.path) as connection:
            assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        pin = external.store.lookup(dispatch_token(OWNER, "workflow-a", "develop", 1), external.resolved.digest, external.resolved, external.target)
        assert pin.task is not None
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
        output.close()
        output.join_thread()
