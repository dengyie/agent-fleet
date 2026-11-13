"""Real process, upstream output and frozen historical Native text boundaries."""
from __future__ import annotations

import asyncio
from contextlib import closing
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from hub.application.workflow_dispatch import WorkflowDispatchService
from hub.application.workflow_text_inputs import WorkflowTextInputs
from hub.domain.workflow_contract import parse_definition
from hub.domain.workflow_contract_types import ArtifactReference, CatalogRef, ResolvedContract
from hub.domain.workflow_errors import WorkflowValidationError
from hub.domain.workflow_identity import canonical_json
from hub.infrastructure.platform_backup import BackupError, PlatformBackupService
from hub.infrastructure.platform_db import PlatformRepository
from hub.infrastructure.platform_memory_repository import PlatformMemoryRepository
from hub.composition.workflows import assert_workflow_visibility
from hub.infrastructure.workflow_input_resolver import WorkflowArtifactInputResolver
from hub.infrastructure.workflow_native_memory_repository import validate_native_memory_runs
from hub.infrastructure.workflow_repository import WorkflowRepository, WorkflowRepositoryError
from hub.infrastructure.workflow_run_executor import RegisteredRunTarget, RunStepExecutor
from hub.platform.artifacts import ArtifactStore
from test_external_agent_outputs import output_setup
from test_workflow_native_text_inputs import TEXT, ObservedArtifacts, TextSetup, assert_no_run, make_text
from test_workflow_repository import OWNER, WORKSPACE, legacy_budget_links, registered


def begin(setup: TextSetup) -> Any:
    return setup.workflows.begin_dispatch(setup.claim, step_key="develop", generation=1,
        expected_revision=setup.workflows.get(OWNER, "workflow-a").revision, now=setup.clock())


def run_state(setup: TextSetup) -> tuple[Any, ...]:
    with setup.workflows.transaction(write=False) as connection:
        return tuple(tuple(tuple(row) for row in connection.execute(query)) for query in (
            "SELECT run_id,trigger_message_id,config_snapshot FROM runs ORDER BY run_id LIMIT 2",
            "SELECT message_id,content,client_token FROM messages WHERE role='user' ORDER BY message_id LIMIT 2",
            "SELECT minimum_reader_version FROM workflow_schema_meta WHERE singleton=1",
            "SELECT binding_run_id FROM workflow_dispatches LIMIT 1",
            "SELECT child_request_id FROM workflow_budget_reservations LIMIT 1",
        ))


def _create_text_process(database: str, artifact_root: str, target: RegisteredRunTarget,
                         resolved: ResolvedContract, token: str, barrier: Any, queue: Any, crash: str) -> None:
    store = ObservedArtifacts(ArtifactStore(Path(artifact_root)))

    class Reader(WorkflowTextInputs):
        def collect(self, contract: ResolvedContract, *, references=None) -> Any:
            if barrier is not None:
                barrier.wait(timeout=15)
            if crash == "before_read":
                os._exit(73)
            inputs = super().collect(contract, references=references)
            if crash == "after_read":
                os._exit(73)
            return inputs

    executor = RunStepExecutor(WorkflowRepository(Path(database)), target, clock=lambda: 104,
        text_inputs=Reader(store))
    binding = asyncio.run(executor.create_or_lookup(token, resolved.digest, resolved))
    if crash == "after_commit":
        os._exit(73)
    queue.put((binding.run_id, store.opens))


def join_processes(processes: list[Any], expected: int) -> None:
    try:
        for process in processes:
            process.join(timeout=20)
            assert process.exitcode == expected
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            process.close()


def test_native_text_two_os_processes_read_before_one_atomic_original_run(tmp_path: Path) -> None:
    setup = make_text(tmp_path, memories=True)
    record = begin(setup)
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(2), context.Queue()
    processes = [context.Process(target=_create_text_process, args=(str(setup.workflows.db_path),
        str(setup.artifacts.root), setup.target, setup.resolved, record.token, barrier, queue, "")) for _ in range(2)]
    try:
        for process in processes:
            process.start()
        join_processes(processes, 0)
        results = [queue.get(timeout=5) for _ in processes]
    finally:
        queue.close()
        queue.join_thread()
    assert len({run_id for run_id, _opens in results}) == 1 and [opens for _run_id, opens in results] == [1, 1]
    binding = setup.dispatch().binding
    assert binding.run_id == results[0][0]
    state = run_state(setup)
    assert len(state[0]) == len(state[1]) == 1 and state[2] == ((9,),)
    assert state[3] == state[4] == ((binding.run_id,),)
    captured: list[Any] = []
    assert setup.worker(captured).run_once(OWNER)["state"] == "succeeded" and len(captured) == 1
    assert json.loads(captured[0][0]["content"].split("\n", 1)[1])["text_inputs"][0]["text"] == TEXT


@pytest.mark.parametrize("crash", ["before_read", "after_read", "after_commit"])
def test_native_text_real_process_exit_recovers_original_input_transaction(tmp_path: Path, crash: str) -> None:
    setup = make_text(tmp_path, memories=True)
    record = begin(setup)
    process = multiprocessing.get_context("spawn").Process(target=_create_text_process, args=(
        str(setup.workflows.db_path), str(setup.artifacts.root), setup.target, setup.resolved, record.token, None, None, crash))
    process.start()
    join_processes([process], 73)
    if crash != "after_commit":
        assert_no_run(setup)
        setup.workflows = WorkflowRepository(setup.workflows.db_path)
        setup.workflows.init()
        setup.dispatch()
        captured: list[Any] = []
        assert setup.worker(captured).run_once(OWNER)["state"] == "succeeded" and len(captured) == 1
        return
    before = run_state(setup)
    setup.memories.update(OWNER, "memory-a", {"content": "changed after committed process exit"}, expected_revision=0, now=105)
    (setup.artifacts.root / "native-input-a" / "content").write_bytes(b"changed after commit")
    setup.workflows = WorkflowRepository(setup.workflows.db_path)
    setup.workflows.init()
    reader = ObservedArtifacts(setup.artifacts, metadata=lambda _value: None)
    executor = setup.executor()
    executor.text_inputs = WorkflowTextInputs(reader)
    binding = setup.dispatch(executor).binding
    assert reader.opens == 0 and run_state(setup) == before
    assert binding.run_id == "wf-run-" + record.token
    captured: list[Any] = []
    assert setup.worker(captured).run_once(OWNER)["state"] == "failed" and captured == []


def test_external_published_output_flows_through_frozen_contract_to_native_provider(tmp_path: Path) -> None:
    following = json.loads(registered().snapshot)["steps"][0]["contract"]
    following.update(input_refs=[{"ref_key": "previous", "kind": "prior_step_output", "step_key": "develop", "output_key": "report"}],
        output_specs=[])
    fixture = output_setup(tmp_path, kind="structured", following_steps=(
        {"step_key": "consume", "kind": "native_run", "requires": ["develop"], "contract": following},))
    upstream = fixture["setup"]
    upstream.apply(upstream.dispatch(fixture["executor"]()))
    snapshot = upstream.repository.snapshots((upstream.claim,), now=upstream.clock())[0]
    resolved = asyncio.run(WorkflowArtifactInputResolver(upstream.repository, fixture["artifacts"]).resolve(
        snapshot, "consume", CatalogRef("native", 1)))
    reference = resolved.contract.input_refs[0].reference
    assert type(reference) is ArtifactReference
    platform = PlatformRepository(upstream.repository.db_path)
    platform.upsert_workspace(OWNER, {"workspace_id": WORKSPACE, "root_path": str(tmp_path / "workspace")})
    platform.upsert_model(OWNER, {"profile_id": "local", "provider": "deterministic", "model": "local"})
    platform.create_conversation(OWNER, "native-chat", title="Native", workspace_id=WORKSPACE)
    ready = upstream.repository.freeze_inputs(upstream.claim, step_key="consume", generation=1, resolved=resolved,
        expected_revision=snapshot.workflow.revision, now=upstream.clock())
    upstream.repository.admit_step(upstream.claim, step_key="consume", generation=1, expected_revision=ready.revision,
        target_key="native-a", capacity=1, reserve_tokens=100, reserve_requests=2, now=upstream.clock())
    target = RegisteredRunTarget(OWNER, WORKSPACE, "native-a", CatalogRef("native", 1), CatalogRef("safe", 1),
        "native-chat", canonical_json({"model_profile_id": "local", "provider_snapshot": {
            "provider": "deterministic", "model": "local", "provider_config": {}, "capabilities": {}}}), inline_text_inputs=True)
    executor = RunStepExecutor(upstream.repository, target, clock=upstream.clock, text_inputs=WorkflowTextInputs(fixture["artifacts"]))
    binding = asyncio.run(WorkflowDispatchService(upstream.repository, executor, clock=upstream.clock).dispatch(
        upstream.claim, step_key="consume", generation=1, expected_revision=upstream.repository.get(OWNER, "workflow-a").revision)).binding
    memories = PlatformMemoryRepository(upstream.repository.db_path, clock=upstream.clock)
    memories.init()
    setup = TextSetup(upstream.repository, platform, memories, target, resolved, upstream.clock, upstream.claim, fixture["artifacts"], 8)
    captured: list[Any] = []
    assert setup.worker(captured).run_once(OWNER)["state"] == "succeeded" and len(captured) == 1
    text = json.loads(captured[0][0]["content"].split("\n", 1)[1])["text_inputs"][0]
    assert text == {"ref_key": "previous", "artifact_id": reference.artifact_id, "sha256": reference.sha256,
        "size": reference.size, "content_type": "application/json", "text": '{"result":"complete"}'}
    assert len(upstream.transport.sends) == len(fixture["exports"]) == 1
    with upstream.repository.transaction(write=False) as connection:
        refs = [tuple(row) for row in connection.execute("SELECT step_key,output_key,artifact_id FROM workflow_artifact_refs ORDER BY step_key")]
        assert refs == [("consume", "input:previous", reference.artifact_id), ("develop", "output:report", reference.artifact_id)]
        assert connection.execute("SELECT child_request_id FROM workflow_budget_reservations WHERE step_key='consume'").fetchone()[0] == binding.run_id
        assert connection.execute("SELECT minimum_reader_version FROM workflow_schema_meta").fetchone()[0] == 9


def test_native_text_full_128_row_16_input_batch_and_signed_last_page(tmp_path: Path) -> None:
    setup = make_text(tmp_path, raw=b"", count=16)
    setup.dispatch()
    definition = parse_definition(setup.workflows.get(OWNER, "workflow-a").definition_snapshot)
    for index in range(128):
        workflow_id = f"native-page-{index:03d}"
        setup.workflows.create_or_get(OWNER, WORKSPACE, workflow_id, client_token=workflow_id, definition=definition, now=104)
        claim = setup.workflows.claim_due(OWNER, worker_id="page-worker", now=104, limit=1, lease_seconds=60)[0]
        assert claim.workflow_id == workflow_id
        ready = setup.workflows.freeze_inputs(claim, step_key="develop", generation=1, resolved=setup.resolved, expected_revision=1, now=104)
        setup.workflows.admit_step(claim, step_key="develop", generation=1, expected_revision=ready.revision,
            target_key="native-a", capacity=1024, reserve_tokens=1, reserve_requests=1, now=104)
        asyncio.run(WorkflowDispatchService(setup.workflows, setup.executor(), clock=setup.clock).dispatch(
            claim, step_key="develop", generation=1, expected_revision=3))
    with setup.workflows.transaction() as connection:
        tokens = [row[0] for row in connection.execute("SELECT token FROM workflow_dispatches ORDER BY rowid LIMIT 130")]
        rowids = (-9223372036854775808, *range(-127, 0), 9223372036854775807)
        connection.executemany("UPDATE workflow_dispatches SET rowid=? WHERE token=?", zip(rowids, tokens))
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        validate_native_memory_runs(connection)
        pages = [sql for sql in statements if sql.startswith("SELECT d.rowid AS dispatch_rowid,")]
        batches = [sql for sql in statements if sql.startswith("WITH selected(owner_id,workspace_id,workflow_id,step_key,generation)")]
        assert len(pages) == len(batches) == 2
        assert "WHERE" not in pages[0] and "WHERE d.rowid>-1" in pages[1]
        assert batches[0].endswith("LIMIT 2049") and batches[1].endswith("LIMIT 17")
        assert not any("WHERE d.token=" in sql or "OFFSET" in sql for sql in statements)
        connection.execute("DELETE FROM workflow_artifact_refs WHERE workflow_id=(SELECT workflow_id FROM workflow_dispatches WHERE rowid=9223372036854775807)")
        with pytest.raises(WorkflowValidationError, match="native_memory_reference_mismatch"):
            validate_native_memory_runs(connection)


@pytest.mark.parametrize("damage", ["body", "configuration", "reader", "reference", "missing_run"])
def test_native_text_final_history_page_rejects_corruption_and_rolls_back_repairs(tmp_path: Path, damage: str) -> None:
    setup = make_text(tmp_path)
    binding = setup.dispatch().binding
    with setup.workflows.transaction() as connection:
        connection.execute("UPDATE workflow_dispatches SET rowid=9223372036854775807")
    legacy_budget_links(setup.workflows, tuple(range(-128, 0)))
    with closing(sqlite3.connect(setup.workflows.db_path)) as connection:
        config = setup.platform.get_run(OWNER, binding.run_id)["config_snapshot"]
        if damage == "body":
            config["workflow_memory"]["text_inputs"][0]["text"] = "corrupt final text page"
        if damage == "configuration":
            config["provider_snapshot"]["provider_config"] = {"changed": True}
        if damage in {"body", "configuration"}:
            connection.execute("UPDATE runs SET config_snapshot=? WHERE run_id=?", (canonical_json(config).decode(), binding.run_id))
        if damage == "reader":
            connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=8")
        if damage == "reference":
            connection.execute("UPDATE workflow_artifact_refs SET sha256=? WHERE workflow_id='workflow-a'", ("f" * 64,))
        if damage == "missing_run":
            connection.execute("UPDATE runs SET run_id='lost-native-run' WHERE run_id=?", (binding.run_id,))
        connection.commit()
    with pytest.raises(WorkflowRepositoryError):
        setup.workflows.init()
    with setup.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM workflow_budget_reservations WHERE child_request_id IS NULL").fetchone()[0] == 128
    with pytest.raises(WorkflowRepositoryError):
        assert_workflow_visibility(setup.workflows.db_path, enabled=False)
    with pytest.raises(BackupError):
        PlatformBackupService().create_backup(setup.workflows.db_path, setup.artifacts.root, tmp_path / "bad-backup")


@pytest.mark.parametrize("encrypted", [False, True])
def test_native_text_frozen_plain_and_encrypted_restore_preserve_original_without_current_memory(tmp_path: Path, encrypted: bool) -> None:
    setup = make_text(tmp_path, memories=True)
    binding = setup.dispatch().binding
    original = setup.platform.get_run(OWNER, binding.run_id)["config_snapshot"]
    setup.memories.update(OWNER, "memory-a", {"enabled": False, "content": "current value unavailable"}, expected_revision=0, now=105)
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    service = PlatformBackupService()
    if encrypted:
        summary = service.create_encrypted_backup(setup.workflows.db_path, setup.artifacts.root, backup, b"k" * 32, "native-text-key")["source"]
        service.restore_encrypted_backup(backup, restored, b"k" * 32)
    else:
        summary = service.create_backup(setup.workflows.db_path, setup.artifacts.root, backup)
        service.restore_backup(backup, restored)
    assert summary["workflow"] == {"schema_version": 1, "minimum_reader_version": 9}
    repository = WorkflowRepository(restored / "platform.db")
    repository.init()
    executor = RunStepExecutor(repository, setup.target, clock=setup.clock)
    assert asyncio.run(executor.lookup(binding.token, setup.resolved.digest, setup.resolved)) == binding
    setup.workflows, setup.platform = repository, PlatformRepository(repository.db_path)
    assert setup.platform.get_run(OWNER, binding.run_id)["config_snapshot"] == original
    with repository.transaction(write=False) as connection:
        assert tuple(connection.execute("SELECT admission_mode,minimum_reader_version FROM workflow_schema_meta").fetchone()) == ("frozen", 9)
        assert connection.execute("SELECT state FROM capability_reservations").fetchone()[0] == "held"
        assert connection.execute("SELECT count(*) FROM workflow_artifact_refs").fetchone()[0] == 1
    captured: list[Any] = []
    assert setup.worker(captured).run_once(OWNER)["state"] == "failed" and captured == []


@pytest.mark.parametrize("reader", [1, 6, 7, 8])
def test_native_text_requires_reader_9_at_startup_backup_and_gate_off(tmp_path: Path, reader: int) -> None:
    setup = make_text(tmp_path)
    setup.dispatch()
    with setup.workflows.transaction() as connection:
        connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=?", (reader,))
    with pytest.raises(WorkflowRepositoryError):
        setup.workflows.init()
    with pytest.raises(WorkflowRepositoryError):
        assert_workflow_visibility(setup.workflows.db_path, enabled=False)
    with pytest.raises(BackupError):
        PlatformBackupService().create_backup(setup.workflows.db_path, setup.artifacts.root, tmp_path / "bad-reader-backup")


def test_native_text_accepts_newer_supported_reader_ten(tmp_path: Path) -> None:
    setup = make_text(tmp_path)
    setup.dispatch()
    with setup.workflows.transaction() as connection:
        connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=10")
    setup.workflows.init()
    with pytest.raises(WorkflowRepositoryError, match="workflow_domain_in_use"):
        assert_workflow_visibility(setup.workflows.db_path, enabled=False)
    summary = PlatformBackupService().create_backup(setup.workflows.db_path, setup.artifacts.root, tmp_path / "reader-ten-backup")
    assert summary["workflow"]["minimum_reader_version"] == 10


@pytest.mark.parametrize("marker", ["workflow_memory", "workflow_text_mode", "workflow_code_mode"])
def test_native_text_trusted_markers_cannot_be_injected_through_ordinary_http_turn(tmp_path: Path, marker: str) -> None:
    from hub.bootstrap import create_app
    from hub.config import FleetConfig

    setup = make_text(tmp_path, admit=False)
    app = create_app(FleetConfig.from_root(tmp_path, platform_enabled=True, platform_workflows_enabled=True,
        platform_db=setup.workflows.db_path, dev_operator=OWNER, serve_frontend=False))
    response = app.test_client().post("/api/platform/v1/conversations/native-chat/turns", json={
        "text": "reference", "client_token": "injected", "overrides": {marker: {"revision": 1}}},
        headers={"Origin": "http://localhost"})
    assert response.status_code == 400 and response.get_json()["error"] == "invalid_value"
    with setup.workflows.transaction(write=False) as connection:
        for table in ("runs", "messages", "legacy_task_links", "workflow_budget_reservations"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert connection.execute("SELECT minimum_reader_version FROM workflow_schema_meta").fetchone()[0] == setup.base_reader
