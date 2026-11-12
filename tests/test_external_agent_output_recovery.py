"""Actual output transactions, restart fencing, historical closure and backups."""
from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hub.application.workflow_dispatch import WorkflowDispatchError, WorkflowDispatchService
from hub.application.workflow_external_execution import ExternalStepExecutor
from hub.domain.workflow_contract import parse_definition
from hub.domain.workflow_control import WorkflowControlRequest
from hub.domain.workflow_execution import ExecutionObservation
from hub.domain.workflow_identity import canonical_json
from hub.infrastructure.platform_backup import BackupError, PlatformBackupService
from hub.composition.workflows import assert_workflow_visibility
from hub.infrastructure.workflow_control_repository import WorkflowControlRepository
from hub.infrastructure.workflow_external_repository import ExternalWorkflowRepository
from hub.infrastructure.workflow_repository import WorkflowRepository, WorkflowRepositoryError
from hub.infrastructure.workflow_schema import READER_VERSION, inspect_schema
from test_external_agent_outputs import output_setup, source_setup
from test_external_agent_workflow import held
from test_workflow_repository import OWNER, WORKSPACE


def saved_state(repository: WorkflowRepository) -> tuple[Any, ...]:
    queries = (
        "SELECT state,subject_digest,reason_code,updated_at FROM workflow_steps ORDER BY step_key,generation LIMIT 8",
        "SELECT stage,observation_snapshot,updated_at FROM workflow_dispatches ORDER BY token LIMIT 8",
        "SELECT state,release_evidence_digest,updated_at FROM capability_reservations ORDER BY step_key,generation LIMIT 8",
        "SELECT settlement,updated_at FROM workflow_budget_reservations ORDER BY step_key,generation LIMIT 8",
        "SELECT revision,execution_state,validation_state,acceptance_state,updated_at FROM workflow_runs LIMIT 1",
        "SELECT kind,payload,sequence FROM workflow_events ORDER BY sequence LIMIT 100",
        "SELECT output_key,artifact_id,sha256,size FROM workflow_artifact_refs ORDER BY output_key LIMIT 33",
    )
    with repository.transaction(write=False) as connection:
        return tuple(tuple(tuple(row) for row in connection.execute(query)) for query in queries)


@pytest.mark.parametrize("table,event", [
    ("workflow_steps", "UPDATE"), ("workflow_dispatches", "UPDATE"), ("workflow_events", "INSERT"),
    ("workflow_runs", "UPDATE"), ("capability_reservations", "UPDATE"), ("workflow_budget_reservations", "UPDATE"),
])
def test_external_output_every_commit_failure_rolls_back_all_original_facts(tmp_path: Path, table: str, event: str) -> None:
    fixture = output_setup(tmp_path)
    setup = fixture["setup"]
    observation = setup.dispatch(fixture["executor"]())
    before = saved_state(setup.repository)
    with setup.repository.transaction() as connection:
        connection.execute(f"CREATE TRIGGER output_commit_failure BEFORE {event} ON {table} BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(WorkflowRepositoryError) as failed:
        setup.apply(observation)
    assert isinstance(failed.value.__cause__, sqlite3.Error)
    assert saved_state(setup.repository) == before
    assert len(setup.transport.sends) == 1


@pytest.mark.parametrize("change", ["task", "token", "payload", "target", "epoch", "revision", "reservation", "budget"])
def test_external_output_commit_rejects_inexact_original_fencing(tmp_path: Path, change: str) -> None:
    fixture = output_setup(tmp_path)
    setup = fixture["setup"]
    observation = setup.dispatch(fixture["executor"]())
    record = setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1)
    if change == "epoch":
        setup.claim = replace(setup.claim, epoch=setup.claim.epoch + 1)
    elif change == "token":
        observation = replace(observation, binding=replace(observation.binding, token="f" * 64))
    else:
        with setup.repository.transaction() as connection:
            if change == "task":
                value = json.loads(connection.execute("SELECT task_snapshot FROM workflow_external_bindings").fetchone()[0])
                value["external_id"] = "different"
                connection.execute("UPDATE workflow_external_bindings SET task_snapshot=?", (canonical_json(value),))
            else:
                updates = {"payload": "UPDATE workflow_dispatches SET payload_hash='" + "f" * 64 + "'",
                    "target": "UPDATE workflow_dispatches SET target_key='different'",
                    "revision": "UPDATE workflow_runs SET revision=revision+1",
                    "reservation": "UPDATE capability_reservations SET state='released'",
                    "budget": "UPDATE workflow_budget_reservations SET settlement='settled'"}
                connection.execute(updates[change])
    before = saved_state(setup.repository)
    with pytest.raises(WorkflowRepositoryError):
        setup.repository.apply_observation(setup.claim, record, observation, now=setup.clock())
    assert saved_state(setup.repository) == before


def test_external_required_output_cannot_succeed_without_verified_proof(tmp_path: Path) -> None:
    fixture = output_setup(tmp_path)
    setup = fixture["setup"]
    observation = setup.dispatch(fixture["executor"]())
    with pytest.raises(WorkflowRepositoryError, match="external_output_proof_missing"):
        setup.apply(replace(observation, external_outputs=None))
    assert held(setup) == "held"


@pytest.mark.parametrize("change", ["export", "source"])
def test_external_committed_output_set_is_immutable(tmp_path: Path, change: str) -> None:
    fixture = output_setup(tmp_path)
    setup = fixture["setup"]
    observation = setup.dispatch(fixture["executor"]())
    setup.apply(observation)
    before = saved_state(setup.repository)
    if change == "export":
        export = observation.external_outputs.export
        output = export.outputs[0]
        proof = replace(observation.external_outputs, export=replace(export, outputs=(replace(output,
            artifact=replace(output.artifact, sha256="f" * 64)),)))
        altered = replace(observation, external_outputs=proof)
    else:
        altered = replace(observation, evidence_source="confirmed", reason_code="external_confirmed")
    with pytest.raises(WorkflowRepositoryError, match="external_output_conflict"):
        setup.apply(altered)
    assert saved_state(setup.repository) == before


@pytest.mark.parametrize("isolated", [False, True])
def test_external_unknown_outputs_need_explicit_reconciliation_and_never_resubmit(tmp_path: Path, isolated: bool) -> None:
    fixture = output_setup(tmp_path)
    setup = fixture["setup"]
    export = setup.adapter.export_outputs
    def unavailable(*args: Any) -> None:
        raise OSError("private export failure")
    setup.adapter.export_outputs = unavailable
    with pytest.raises(WorkflowDispatchError):
        setup.dispatch(fixture["executor"]())
    record = setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1)
    setup.adapter.export_outputs = export
    with pytest.raises(WorkflowDispatchError, match="reconciliation_required"):
        asyncio.run(fixture["executor"]().inspect(record.binding))
    if isolated:
        with setup.repository.transaction() as connection:
            connection.execute("UPDATE capability_reservations SET state='isolated'")
    current = setup.repository.get(OWNER, "workflow-a")
    controls = WorkflowControlRepository(setup.repository)
    controls.control(WorkflowControlRequest(OWNER, "workflow-a", "reconcile", current.revision, "reconcile-output", "develop", 1), now=104)
    setup.clock.now = 104
    setup.claim = setup.repository.claim_due(OWNER, worker_id="output-reconciler", now=104, limit=1, lease_seconds=60)[0]
    observation = asyncio.run(fixture["executor"]().inspect(record.binding))
    current = setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1)
    with pytest.raises(WorkflowRepositoryError):
        setup.repository.apply_observation(setup.claim, current, observation, now=104)
    result = setup.repository.apply_observation(setup.claim, current, observation, now=104, reconciled=True)
    assert result.execution_state == "succeeded"
    assert held(setup) == ("isolated" if isolated else "released")
    assert len(setup.transport.sends) == 1 and len(fixture["exports"]) == 1
    with setup.repository.transaction(write=False) as connection:
        assert connection.execute("SELECT state FROM workflow_reconciliation_requests").fetchone()[0] == "inspected"
        setup.store.validate_snapshot(connection)


@pytest.mark.parametrize("failure", ["legacy_reader", "reservation"])
def test_external_output_reader_barrier_and_admission_rollback_are_atomic(tmp_path: Path, monkeypatch: Any, failure: str) -> None:
    from hub.infrastructure import workflow_schema

    fixture = output_setup(tmp_path, admit=False)
    setup = fixture["setup"]
    with setup.repository.transaction() as connection:
        assert inspect_schema(connection)["minimum_reader_version"] == 6
        if failure == "reservation":
            connection.execute("CREATE TRIGGER output_admission_failure BEFORE INSERT ON capability_reservations BEGIN SELECT RAISE(ABORT,'injected'); END")
    if failure == "legacy_reader":
        monkeypatch.setattr(workflow_schema, "READER_VERSION", 7)
    before = saved_state(setup.repository)
    with pytest.raises(WorkflowRepositoryError):
        setup.admit(setup.target)
    assert saved_state(setup.repository) == before
    with setup.repository.transaction(write=False) as connection:
        assert connection.execute("SELECT minimum_reader_version FROM workflow_schema_meta").fetchone()[0] == 6
        assert connection.execute("SELECT count(*) FROM workflow_external_bindings").fetchone()[0] == 0


@pytest.mark.parametrize("reader", [6, 7, READER_VERSION + 1])
def test_external_output_reader_fences_startup_backup_and_gate_off(tmp_path: Path, reader: int) -> None:
    fixture = output_setup(tmp_path)
    setup = fixture["setup"]
    with setup.repository.transaction() as connection:
        connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=?", (reader,))
    with pytest.raises(WorkflowRepositoryError):
        setup.repository.init()
    with pytest.raises(WorkflowRepositoryError):
        assert_workflow_visibility(setup.repository.db_path, enabled=False)
    with pytest.raises(BackupError):
        PlatformBackupService().create_backup(setup.repository.db_path, fixture["artifacts"].root, tmp_path / "backup")


@pytest.mark.parametrize("encrypted", [False, True])
@pytest.mark.parametrize("source", [False, True])
def test_external_published_outputs_survive_plain_and_encrypted_frozen_restore(tmp_path: Path, encrypted: bool, source: bool) -> None:
    fixture = source_setup(tmp_path) if source else output_setup(tmp_path, kind="structured")
    setup = fixture["setup"]
    observation = setup.dispatch(fixture["executor"]())
    setup.apply(observation)
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    service = PlatformBackupService()
    if encrypted:
        summary = service.create_encrypted_backup(setup.repository.db_path, fixture["artifacts"].root, backup, b"k" * 32, "output-key")["source"]
        service.restore_encrypted_backup(backup, restored, b"k" * 32)
    else:
        summary = service.create_backup(setup.repository.db_path, fixture["artifacts"].root, backup)
        service.restore_backup(backup, restored)
    assert summary["workflow"] == {"schema_version": 1, "minimum_reader_version": 8}
    repository = WorkflowRepository(restored / "platform.db")
    repository.init()
    store = ExternalWorkflowRepository(repository)
    with repository.transaction(write=False) as connection:
        assert inspect_schema(connection)["admission_mode"] == "frozen"
        store.validate_snapshot(connection)
        assert connection.execute("SELECT count(*) FROM workflow_artifact_refs").fetchone()[0] == (2 if source else 1)
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("frozen restore attempted external I/O")
    setup.adapter.inspect = setup.adapter.export_outputs = forbidden
    restored_executor = ExternalStepExecutor(store, setup.target, setup.adapter, clock=setup.clock)
    assert asyncio.run(restored_executor.inspect(observation.binding)) == observation
    assert len(setup.transport.sends) == 1


@pytest.mark.parametrize("change", ["proof", "subject", "reference", "missing_reference", "extra_reference", "content"])
def test_external_output_corruption_is_rejected_by_history_or_backup(tmp_path: Path, change: str) -> None:
    fixture = output_setup(tmp_path)
    setup = fixture["setup"]
    observation = setup.dispatch(fixture["executor"]())
    setup.apply(observation)
    if change == "content":
        (fixture["artifacts"].root / fixture["manifest"]["artifact_id"] / "content").write_bytes(b"corrupt bytes")
    else:
        with setup.repository.transaction() as connection:
            if change == "proof":
                value = json.loads(connection.execute("SELECT observation_snapshot FROM workflow_dispatches").fetchone()[0])
                value["external_outputs"]["export"]["generation"] = 2
                connection.execute("UPDATE workflow_dispatches SET observation_snapshot=?", (canonical_json(value),))
            elif change == "subject":
                connection.execute("UPDATE workflow_steps SET subject_digest=?", ("f" * 64,))
            elif change == "reference":
                connection.execute("UPDATE workflow_artifact_refs SET sha256=?", ("f" * 64,))
            elif change == "missing_reference":
                connection.execute("DELETE FROM workflow_artifact_refs WHERE output_key='output:report'")
            else:
                connection.execute("INSERT INTO workflow_artifact_refs SELECT owner_id,workspace_id,workflow_id,step_key,generation,"
                    "'output:undeclared',artifact_id,sha256,size FROM workflow_artifact_refs")
        with pytest.raises(WorkflowRepositoryError):
            setup.repository.init()
        with pytest.raises(WorkflowRepositoryError):
            assert_workflow_visibility(setup.repository.db_path, enabled=False)
    with pytest.raises(BackupError):
        PlatformBackupService().create_backup(setup.repository.db_path, fixture["artifacts"].root, tmp_path / "backup")


def _crash_output_commit(db: str, claim: Any, observation: ExecutionObservation, window: str, now: float) -> None:
    repository = WorkflowRepository(Path(db))
    record = repository.get_dispatch(OWNER, "workflow-a", "develop", 1)
    if window == "after_refs":
        original = ExternalWorkflowRepository.commit_outputs
        def crash(*args: Any, **kwargs: Any) -> None:
            original(*args, **kwargs)
            os._exit(88)
        ExternalWorkflowRepository.commit_outputs = staticmethod(crash)
    elif window == "after_observation":
        original_event = repository._event
        def event(connection: Any, owner: str, workflow: str, kind: str, payload: Any, now: float) -> None:
            if kind == "step.observed":
                os._exit(88)
            original_event(connection, owner, workflow, kind, payload, now)
        repository._event = event
    elif window == "before_commit":
        def project(*args: Any, **kwargs: Any) -> None:
            os._exit(88)
        repository._project = project
    repository.apply_observation(claim, record, observation, now=now)
    os._exit(88)


@pytest.mark.parametrize("window", ["after_refs", "after_observation", "before_commit", "after_commit"])
def test_external_output_real_process_crash_has_no_partial_commit_or_resubmit(tmp_path: Path, window: str) -> None:
    fixture = output_setup(tmp_path)
    setup = fixture["setup"]
    observation = setup.dispatch(fixture["executor"]())
    before = saved_state(setup.repository)
    process = multiprocessing.get_context("spawn").Process(target=_crash_output_commit,
        args=(str(setup.repository.db_path), setup.claim, observation, window, setup.clock()))
    process.start()
    try:
        process.join(10)
        assert process.exitcode == 88
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)
        process.close()
    setup.repository.init()
    if window != "after_commit":
        assert saved_state(setup.repository) == before
    recovered = asyncio.run(fixture["executor"]().inspect(observation.binding))
    setup.apply(recovered)
    assert recovered == observation
    assert len(setup.transport.sends) == 1
    assert len(fixture["exports"]) == (1 if window == "after_commit" else 2)


@pytest.mark.parametrize("signed", [False, True])
def test_external_output_history_batches_signed_rowids_and_checks_last_reference_page(tmp_path: Path, signed: bool) -> None:
    fixture = output_setup(tmp_path)
    setup = fixture["setup"]
    original = setup.dispatch(fixture["executor"]())
    setup.apply(original)
    from test_muse_browser_bridge import message

    pin = setup.store.read_binding(original.binding, setup.target)
    journal = setup.adapter.bridge.lookup(original.binding.token, pin.request.prompt, scope=WORKSPACE)
    setup.transport.messages.append(message("finished-original", "assistant", "Done\n" + journal["completion_marker"]))
    assert setup.adapter.bridge.read(original.binding.token, scope=WORKSPACE)["state"] == "completed"
    definition = parse_definition(setup.repository.get(OWNER, "workflow-a").definition_snapshot)
    for index in range(128):
        workflow = f"history-{index:03d}"
        setup.repository.create_or_get(OWNER, WORKSPACE, workflow, client_token=workflow, definition=definition, now=100)
        claim = setup.repository.claim_due(OWNER, worker_id="history", now=100, limit=1, lease_seconds=20)[0]
        ready = setup.repository.freeze_inputs(claim, step_key="develop", generation=1, resolved=setup.resolved, expected_revision=1, now=101)
        setup.repository.admit_step(claim, step_key="develop", generation=1, expected_revision=ready.revision,
            target_key=setup.target.target_key, capacity=1024, reserve_tokens=100, reserve_requests=2, now=102, external_target=setup.target)
    setup.adapter.export_outputs = lambda task, specs: replace(original.external_outputs.export, workflow_id=workflow, task=task)
    service = WorkflowDispatchService(setup.repository, fixture["executor"](), clock=setup.clock)
    observation = asyncio.run(service.dispatch(claim, step_key="develop", generation=1,
        expected_revision=setup.repository.get(OWNER, workflow).revision))
    setup.repository.apply_observation(claim, setup.repository.get_dispatch(OWNER, workflow, "develop", 1), observation, now=setup.clock())
    with setup.repository.transaction() as connection:
        if signed:
            connection.execute("UPDATE workflow_external_bindings SET rowid=-2 WHERE workflow_id='workflow-a'")
            connection.execute("UPDATE workflow_external_bindings SET rowid=0 WHERE workflow_id='history-000'")
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        setup.store.validate_snapshot(connection)
        connection.set_trace_callback(None)
        assert len([sql for sql in statements if "ORDER BY e.rowid LIMIT 128" in sql]) == 3
        assert len([sql for sql in statements if "JOIN workflow_artifact_refs a" in sql and "WHERE e.rowid IN" in sql]) == 2
        connection.execute("UPDATE workflow_artifact_refs SET sha256=? WHERE workflow_id=?", ("f" * 64, workflow))
        with pytest.raises(WorkflowRepositoryError, match="external_output_reference_invalid"):
            setup.store.validate_snapshot(connection)


@pytest.mark.parametrize("key", ["unclassified", "input:undeclared", "source:undeclared"])
def test_external_history_rejects_references_outside_the_original_closure(tmp_path: Path, key: str) -> None:
    fixture = output_setup(tmp_path)
    setup = fixture["setup"]
    setup.apply(setup.dispatch(fixture["executor"]()))
    with setup.repository.transaction() as connection:
        connection.execute("INSERT INTO workflow_artifact_refs SELECT owner_id,workspace_id,workflow_id,step_key,generation,"
            "?,artifact_id,sha256,size FROM workflow_artifact_refs", (key,))
        with pytest.raises(WorkflowRepositoryError, match="external_output_reference_invalid"):
            setup.store.validate_snapshot(connection)


def _concurrent_output_commit(db: str, claim: Any, observation: ExecutionObservation,
                              ready: Any, release: Any, results: Any, now: float) -> None:
    repository = WorkflowRepository(Path(db))
    record = repository.get_dispatch(OWNER, "workflow-a", "develop", 1)
    ready.put(True)
    if not release.wait(10):
        results.put(("error", "barrier_timeout"))
        return
    try:
        result = repository.apply_observation(claim, record, observation, now=now)
        results.put(("ok", result.revision))
    except WorkflowRepositoryError as exc:
        results.put(("error", exc.code))


def test_two_process_identical_output_commits_keep_one_revision_event_and_reference_set(tmp_path: Path) -> None:
    fixture = source_setup(tmp_path)
    setup = fixture["setup"]
    observation = setup.dispatch(fixture["executor"]())
    before = setup.repository.get(OWNER, "workflow-a").revision
    context = multiprocessing.get_context("spawn")
    ready, results, release = context.Queue(), context.Queue(), context.Event()
    workers = [context.Process(target=_concurrent_output_commit,
        args=(str(setup.repository.db_path), setup.claim, observation, ready, release, results, setup.clock())) for _ in range(2)]
    for worker in workers:
        worker.start()
    try:
        assert ready.get(timeout=10) and ready.get(timeout=10)
        release.set()
        outcomes = [results.get(timeout=10), results.get(timeout=10)]
        for worker in workers:
            worker.join(10)
            assert worker.exitcode == 0
        assert sorted(outcomes, key=lambda item: item[0]) == [("error", "revision_conflict"), ("ok", before + 1)]
    finally:
        release.set()
        for worker in workers:
            if worker.is_alive():
                worker.kill()
                worker.join(5)
            worker.close()
        ready.close()
        ready.join_thread()
        results.close()
        results.join_thread()
    committed = saved_state(setup.repository)
    setup.apply(observation)
    assert saved_state(setup.repository) == committed
    with setup.repository.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM workflow_artifact_refs").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM workflow_events WHERE kind='step.observed'").fetchone()[0] == 1
        setup.store.validate_snapshot(connection)
    assert len(setup.transport.sends) == len(fixture["exports"]) == 1


def _crash_readonly_export(db: str, artifact_root: str, binding: Any, exported: Any, window: str) -> None:
    from hub.application.workflow_external_outputs import WorkflowExternalOutputs
    from hub.domain.external_agent import ExternalAgentStatus
    from hub.domain.workflow_external import parse_external_target
    from tools.browser_bridge.muse import MuseBridge
    from hub.platform.artifacts import ArtifactStore
    from connectors.external_agents.muse import MuseExternalAgentAdapter
    from test_muse_browser_bridge import Transport

    repository = WorkflowRepository(Path(db))
    with repository.transaction(write=False) as connection:
        target = parse_external_target(bytes(connection.execute("SELECT target_snapshot FROM workflow_external_bindings").fetchone()[0]))
    adapter = MuseExternalAgentAdapter(MuseBridge(Path(db).with_name("muse.db"), transport=Transport()),
        workspace_id=WORKSPACE, account_ref="account-a", transport_ref="fixture-v1")
    adapter.capabilities = target.capabilities
    adapter.inspect = lambda task: ExternalAgentStatus("succeeded", "confirmed", task.external_id, session_id=task.session_id)
    def export(task: Any, specs: Any) -> Any:
        if window == "before_export":
            os._exit(87)
        return exported
    adapter.export_outputs = export
    verifier = WorkflowExternalOutputs(ArtifactStore(Path(artifact_root)))
    original_verify = verifier.verify
    def verify(*args: Any) -> Any:
        if window == "after_export":
            os._exit(87)
        return original_verify(*args)
    verifier.verify = verify
    executor = ExternalStepExecutor(ExternalWorkflowRepository(repository), target, adapter, clock=lambda: 104, outputs=verifier)
    asyncio.run(executor.inspect(binding))
    os._exit(87)


@pytest.mark.parametrize("window", ["before_export", "after_export", "after_verification"])
def test_readonly_export_process_crashes_recover_the_original_task_without_resubmit(tmp_path: Path, window: str) -> None:
    from hub.domain.external_agent import ExternalAgentStatus

    fixture = output_setup(tmp_path, kind="structured")
    setup = fixture["setup"]
    setup.adapter.inspect = lambda task: ExternalAgentStatus("running", "accepted", task.external_id)
    initial = setup.dispatch(fixture["executor"]())
    setup.apply(initial)
    pin = setup.store.read_binding(initial.binding, setup.target)
    exported = setup.adapter.export_outputs(pin.task, ())
    before = saved_state(setup.repository)
    process = multiprocessing.get_context("spawn").Process(target=_crash_readonly_export,
        args=(str(setup.repository.db_path), str(fixture["artifacts"].root), initial.binding, exported, window))
    process.start()
    try:
        process.join(10)
        assert process.exitcode == 87
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)
        process.close()
    assert saved_state(setup.repository) == before and held(setup) == "held"
    setup.adapter.inspect = lambda task: ExternalAgentStatus("succeeded", "confirmed", task.external_id)
    setup.apply(asyncio.run(fixture["executor"]().inspect(initial.binding)))
    assert setup.repository.get(OWNER, "workflow-a").execution_state == "succeeded"
    assert len(setup.transport.sends) == 1
