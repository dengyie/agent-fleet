"""Operator controls against real SQLite and original Run execution facts."""
from __future__ import annotations

import asyncio
import multiprocessing
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hub.application.workflow_control import WorkflowControlError, WorkflowControlService
from hub.domain.workflow_control import WorkflowControlRequest
from hub.domain.workflow_contract import parse_contract, resolve_contract
from hub.domain.workflow_contract_types import ArtifactReference, AuthorizedInputs, CatalogRef, InputBinding, NamedReference
from hub.domain.workflow_errors import WorkflowValidationError
from hub.domain.workflow_execution import ExecutionBinding, ExecutionObservation
from hub.domain.task import DomainError
from hub.infrastructure.workflow_control_repository import WorkflowControlRepository
from hub.infrastructure.workflow_repository import WorkflowRepository, WorkflowRepositoryError
from hub.infrastructure.workflow_run_executor import RunStepExecutor
from hub.infrastructure.workflow_inputs import WorkflowArtifactInputAuthorizer
from hub.platform.artifacts import ArtifactStore
from hub.application.legacy_task_bridge import legacy_client_token
from test_workflow_coordinator import Setup, make_setup
from test_workflow_repository import OWNER
from test_legacy_task_bridge import _bridge, REQUEST


def control(setup: Setup, action: str, *, token: str | None = None, step: str | None = None) -> Any:
    current = setup.repository.get(OWNER, "workflow-a")
    generation = next((item.generation for item in setup.repository.current_steps(OWNER, "workflow-a") if item.step_key == step), None)
    request = WorkflowControlRequest(OWNER, "workflow-a", action, current.revision, token or f"{action}-{current.revision}",
                                     step, generation, "operator_revision" if action == "invalidate" else None)
    service = WorkflowControlService(WorkflowControlRepository(setup.repository), clock=setup.clock, cancel_seconds=5)
    return asyncio.run(service.execute(request))


@pytest.mark.parametrize("counter", [True, False, 0, -1, 1.0, "1", 10 ** 400])
def test_control_rejects_inexact_counters(counter: Any) -> None:
    with pytest.raises(WorkflowValidationError, match="invalid_integer"):
        WorkflowControlRequest(OWNER, "workflow-a", "pause", counter, "key")


def test_control_cas_idempotence_scope_and_root_cause(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    store = WorkflowControlRepository(setup.repository)
    request = WorkflowControlRequest(OWNER, "workflow-a", "pause", 1, "key")
    receipt = store.control(request, now=100)
    setup.tick()
    assert store.control(request, now=102) == receipt
    with pytest.raises(WorkflowRepositoryError, match="idempotency_conflict"):
        store.control(replace(request, action="resume"), now=102)
    with pytest.raises(WorkflowRepositoryError, match="revision_conflict"):
        store.control(replace(request, client_token="stale"), now=102)
    with pytest.raises(WorkflowRepositoryError, match="workflow_not_found"):
        store.control(replace(request, owner_id="other"), now=102)
    service = WorkflowControlService(store, clock=setup.clock)
    with pytest.raises(WorkflowControlError) as error:
        asyncio.run(service.execute(replace(request, client_token="stale")))
    assert isinstance(error.value.__cause__, WorkflowRepositoryError)


def test_pause_preserves_current_execution_and_resume_only_admits_successor(tmp_path: Path) -> None:
    setup = make_setup(tmp_path, steps=2)
    for _ in range(3):
        setup.tick()
    control(setup, "pause")
    setup.platform.set_run_state(OWNER, setup.run_id(), "succeeded", now=setup.clock())
    setup.tick()
    setup.tick()
    assert setup.repository.get(OWNER, "workflow-a").execution_state == "paused"
    assert setup.executor.created == 1
    control(setup, "resume")
    for _ in range(3):
        setup.tick()
    assert setup.executor.created == 2
    assert [item.state for item in setup.repository.current_steps(OWNER, "workflow-a")] == ["succeeded", "waiting_external"]


@pytest.mark.parametrize("ticks", [0, 1, 2])
def test_unsent_cancel_releases_only_proven_unused_reservations(tmp_path: Path, ticks: int) -> None:
    setup = make_setup(tmp_path, steps=2)
    for _ in range(ticks):
        setup.tick()
    receipt = control(setup, "cancel")
    assert receipt.execution_state == "cancelled"
    setup.tick()
    assert setup.executor.created == 0
    assert all(item.state == "cancelled" for item in setup.repository.current_steps(OWNER, "workflow-a"))
    with setup.repository.transaction(write=False) as connection:
        assert not connection.execute("SELECT 1 FROM capability_reservations WHERE state='held'").fetchone()
        assert not connection.execute("SELECT 1 FROM workflow_budget_reservations WHERE settlement!='settled' OR charged_tokens!=0 OR charged_requests!=0").fetchone()
    with pytest.raises(WorkflowControlError, match="terminal_workflow"):
        control(setup, "resume")


def test_cancel_timeout_is_unknown_and_keeps_slot_and_money(tmp_path: Path) -> None:
    setup = make_setup(tmp_path, steps=2)
    for _ in range(3):
        setup.tick()
    receipt = control(setup, "cancel")
    assert receipt.execution_state == "cancelling"
    setup.tick()
    assert setup.repository.get(OWNER, "workflow-a").execution_state == "cancelling"
    setup.clock.now += 5
    setup.tick()
    assert setup.repository.get(OWNER, "workflow-a").execution_state == "unknown"
    for _ in range(3):
        setup.tick()
    assert setup.executor.created == 1
    with setup.repository.transaction(write=False) as connection:
        assert connection.execute("SELECT state FROM capability_reservations").fetchone()[0] == "held"
        assert tuple(connection.execute("SELECT settlement,charged_tokens FROM workflow_budget_reservations").fetchone()) == ("unknown", None)


def test_explicit_reconciliation_uses_terminal_evidence_without_rewriting_unknown_run(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    for _ in range(3):
        setup.tick()
    setup.platform.set_run_state(OWNER, setup.run_id(), "unknown", now=setup.clock())
    setup.tick()
    assert setup.repository.get(OWNER, "workflow-a").execution_state == "unknown"

    async def stopped(binding: ExecutionBinding) -> ExecutionObservation:
        return ExecutionObservation(binding, "failed", reason_code="verified_exit")

    setup.executor.inspect = stopped
    setup.tick()
    assert setup.repository.get(OWNER, "workflow-a").execution_state == "unknown"
    control(setup, "reconcile", step="develop")
    setup.tick()
    assert setup.repository.current_steps(OWNER, "workflow-a")[0].state == "failed"
    assert setup.repository.get(OWNER, "workflow-a").execution_state == "failed"
    assert setup.platform.get_run(OWNER, setup.run_id())["state"] == "unknown"
    assert setup.executor.created == 1
    with setup.repository.transaction(write=False) as connection:
        assert connection.execute("SELECT state FROM capability_reservations").fetchone()[0] == "released"


def test_retry_generations_keep_history_and_reject_old_receipts(tmp_path: Path) -> None:
    setup = make_setup(tmp_path, steps=2)
    setup.registrations = tuple(replace(item, reserve_tokens=20, reserve_requests=0) for item in setup.registrations)
    for generation in (1, 2, 3):
        for _ in range(3):
            setup.tick()
        current = setup.repository.get_dispatch(OWNER, "workflow-a", "develop", generation)
        setup.platform.set_run_state(OWNER, current.binding.run_id, "failed", now=setup.clock())
        setup.tick()
        if generation < 3:
            receipt = control(setup, "retry", step="develop")
            assert receipt.generations == (("develop", generation + 1), ("review", generation + 1))
    with pytest.raises(WorkflowControlError, match="generation_exhausted"):
        control(setup, "retry", step="develop")
    with setup.repository.transaction(write=False) as connection:
        rows = connection.execute("SELECT generation,is_current FROM workflow_steps WHERE step_key='develop' ORDER BY generation").fetchall()
        assert [tuple(row) for row in rows] == [(1, 0), (2, 0), (3, 1)]


@pytest.mark.parametrize("action", ["retry", "invalidate"])
def test_isolated_reservation_blocks_new_generation(tmp_path: Path, action: str) -> None:
    setup = make_setup(tmp_path)
    setup.registrations = tuple(replace(item, reserve_tokens=1, reserve_requests=0) for item in setup.registrations)
    for _ in range(3):
        setup.tick()
    with setup.repository.transaction() as connection:
        connection.execute("UPDATE capability_reservations SET state='isolated' WHERE owner_id=? AND workflow_id=?", (OWNER, "workflow-a"))
    setup.platform.set_run_state(OWNER, setup.run_id(), "failed", now=setup.clock())
    setup.tick()
    before = setup.repository.get(OWNER, "workflow-a")
    assert before.execution_state == "failed"
    before_events = setup.repository.events(OWNER, "workflow-a")
    with pytest.raises(WorkflowControlError, match="unresolved_execution") as error:
        control(setup, action, step="develop")
    assert isinstance(error.value.__cause__, WorkflowRepositoryError)
    assert isinstance(error.value.__cause__.__cause__, WorkflowValidationError)
    assert setup.repository.get(OWNER, "workflow-a") == before
    assert setup.repository.events(OWNER, "workflow-a") == before_events
    assert setup.repository.current_steps(OWNER, "workflow-a")[0].generation == 1
    with setup.repository.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM workflow_steps").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM workflow_operations").fetchone()[0] == 0
        assert connection.execute("SELECT state FROM capability_reservations").fetchone()[0] == "isolated"


def _race_control(path: str, barrier: Any, outcomes: Any, action: str) -> None:
    repository = WorkflowControlRepository(WorkflowRepository(Path(path)))
    barrier.wait(timeout=15)
    try:
        receipt = repository.control(WorkflowControlRequest(OWNER, "workflow-a", action, 1, action), now=100)
        outcomes.put((action, receipt.revision))
    except WorkflowRepositoryError as exc:
        outcomes.put((action, exc.code))


def test_two_os_process_controls_share_one_cas_revision(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    context = multiprocessing.get_context("spawn")
    barrier, outcomes = context.Barrier(2), context.Queue()
    processes = [context.Process(target=_race_control, args=(str(setup.repository.db_path), barrier, outcomes, action)) for action in ("pause", "cancel")]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    results = [outcomes.get(timeout=2)[1] for _ in processes]
    assert sorted(map(str, results)) == ["2", "revision_conflict"]
    outcomes.close()
    outcomes.join_thread()


def test_cancel_before_legacy_link_blocks_a_stale_bridge_submission(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    for _ in range(3):
        setup.tick()
    _, tasks, bridge = _bridge(tmp_path, setup.platform)
    executor = RunStepExecutor(setup.repository, setup.executor.target, clock=setup.clock, legacy_control=bridge)
    coordinator = setup.coordinator(executors=((OWNER, "workspace-a", "node-a", executor),))
    stale = setup.platform.claim_legacy_task(OWNER, worker_id="crashed", now=setup.clock())
    control(setup, "cancel")
    setup.tick(coordinator)
    assert setup.repository.get(OWNER, "workflow-a").execution_state == "cancelled"
    token = legacy_client_token(OWNER, setup.run_id())
    with pytest.raises(DomainError, match="task_submission_cancelled"):
        tasks.create_task(**{key: value for key, value in REQUEST.items() if key != "confirm"}, requested_by=OWNER, client_token=token)
    assert tasks.list_tasks(limit=10) == []
    assert stale["task_id"] is None


def test_active_legacy_cancel_reaches_original_task_but_cannot_prove_process_exit(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    for _ in range(3):
        setup.tick()
    _, tasks, bridge = _bridge(tmp_path, setup.platform)
    link = bridge.process_once(OWNER, run_id=setup.run_id())
    task = tasks.get_task(link["task_id"])
    leased = tasks.lease_task(machine="mac-local", runner_id="runner", now=setup.clock())
    assert leased["task_id"] == task["task_id"]
    executor = RunStepExecutor(setup.repository, setup.executor.target, clock=setup.clock, legacy_control=bridge)
    coordinator = setup.coordinator(executors=((OWNER, "workspace-a", "node-a", executor),))
    control(setup, "cancel")
    setup.tick(coordinator)
    assert tasks.get_task(task["task_id"])["state"] == "cancelled"
    assert setup.repository.get(OWNER, "workflow-a").execution_state == "cancelling"
    setup.clock.now += 5
    setup.tick(coordinator)
    assert setup.repository.get(OWNER, "workflow-a").execution_state == "unknown"


def test_invalidate_preserves_template_history_and_authorizes_revised_inputs(tmp_path: Path) -> None:
    artifacts = ArtifactStore(tmp_path / "artifacts")
    source = tmp_path / "source.txt"
    source.write_text("first", encoding="utf-8")
    old = artifacts.put_file(OWNER, "workspace-a", source)
    source.write_text("second", encoding="utf-8")
    new = artifacts.put_file(OWNER, "workspace-a", source)
    setup = make_setup(tmp_path, steps=2, source={key: old[key] for key in ("artifact_id", "sha256", "size")})
    setup.registrations = tuple(replace(item, reserve_tokens=20, reserve_requests=0) for item in setup.registrations)
    for _ in range(3):
        setup.tick()
    setup.platform.set_run_state(OWNER, setup.run_id(), "succeeded", now=setup.clock())
    setup.tick()
    original = setup.repository.get(OWNER, "workflow-a")
    references = (NamedReference("source", ArtifactReference(new["artifact_id"], new["sha256"], new["size"])),)
    request = WorkflowControlRequest(OWNER, "workflow-a", "invalidate", original.revision, "change", "develop", 1, "revise_source", references)
    with pytest.raises(WorkflowControlError, match="reference_forbidden"):
        asyncio.run(WorkflowControlService(WorkflowControlRepository(setup.repository), clock=setup.clock).execute(request))
    authorizer = WorkflowArtifactInputAuthorizer(setup.repository, artifacts)
    service = WorkflowControlService(WorkflowControlRepository(setup.repository), clock=setup.clock, input_authorizer=authorizer)
    receipt = asyncio.run(service.execute(request))
    assert receipt.generations == (("develop", 2), ("review", 2))
    current = setup.repository.get(OWNER, "workflow-a")
    assert current.definition_snapshot == original.definition_snapshot
    assert current.acceptance_state == "pending"
    steps = setup.repository.current_steps(OWNER, "workflow-a")
    revised = parse_contract(steps[0].contract_snapshot)
    assert revised.input_refs == references and steps[0].resolved_snapshot is None
    setup.tick()
    assert setup.repository.get(OWNER, "workflow-a").reason_code == "registration_conflict"
    resolved = resolve_contract(setup.definition, AuthorizedInputs(OWNER, "workspace-a", "develop", CatalogRef("cli", 1),
                                (InputBinding("source", OWNER, "workspace-a", references[0].reference),)), contract_override=revised)
    setup.registrations = (replace(setup.registrations[0], resolved=resolved), setup.registrations[1])
    setup.tick()
    setup.tick()
    setup.tick()
    assert setup.executor.created == 2
    with setup.repository.transaction(write=False) as connection:
        rows = connection.execute("SELECT generation,artifact_id FROM workflow_artifact_refs WHERE step_key='develop' ORDER BY generation").fetchall()
        assert [tuple(row) for row in rows] == [(1, old["artifact_id"]), (2, new["artifact_id"])]


def test_invalidate_rejects_active_execution_without_discarding_history(tmp_path: Path) -> None:
    setup = make_setup(tmp_path, steps=2)
    for _ in range(3):
        setup.tick()
    with pytest.raises(WorkflowControlError, match="unresolved_execution"):
        control(setup, "invalidate", step="develop")
    assert [item.generation for item in setup.repository.current_steps(OWNER, "workflow-a")] == [1, 1]


def test_foreign_artifact_cannot_be_mounted_as_a_revised_input(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    source = tmp_path / "foreign.txt"
    source.write_text("foreign", encoding="utf-8")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    manifest = artifacts.put_file("other@example.test", "workspace-a", source)
    refs = (NamedReference("source", ArtifactReference(manifest["artifact_id"], manifest["sha256"], manifest["size"])),)
    request = WorkflowControlRequest(OWNER, "workflow-a", "invalidate", 1, "foreign", "develop", 1, "change", refs)
    service = WorkflowControlService(WorkflowControlRepository(setup.repository), clock=setup.clock,
                                     input_authorizer=WorkflowArtifactInputAuthorizer(setup.repository, artifacts))
    with pytest.raises(WorkflowControlError, match="reference_forbidden"):
        asyncio.run(service.execute(request))
    assert setup.repository.get(OWNER, "workflow-a").revision == 1


def test_cancel_unknown_legacy_execution_uses_stop_proof_and_keeps_unknown_run(tmp_path: Path) -> None:
    setup = make_setup(tmp_path, steps=2)
    for _ in range(3):
        setup.tick()
    _, tasks, bridge = _bridge(tmp_path, setup.platform)
    link = bridge.process_once(OWNER, run_id=setup.run_id())
    setup.platform.set_run_state(OWNER, setup.run_id(), "unknown", now=setup.clock())
    setup.tick()
    executor = RunStepExecutor(setup.repository, setup.executor.target, clock=setup.clock, legacy_control=bridge)
    coordinator = setup.coordinator(executors=((OWNER, "workspace-a", "node-a", executor),))
    control(setup, "cancel")
    setup.tick(coordinator)
    assert tasks.get_task(link["task_id"])["state"] == "cancelled"
    assert setup.repository.get(OWNER, "workflow-a").execution_state == "cancelled"
    assert setup.platform.get_run(OWNER, setup.run_id())["state"] == "unknown"


def test_sending_cancel_recovers_original_binding_without_a_second_create(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    setup.tick()
    setup.tick()
    claim = setup.repository.claim_fair_due(worker_id="crashed", now=setup.clock(), limit=1)[0]
    current = setup.repository.get(OWNER, "workflow-a")
    record = setup.repository.begin_dispatch(claim, step_key="develop", generation=1, expected_revision=current.revision, now=setup.clock())
    binding = asyncio.run(setup.executor.create_or_lookup(record.token, record.payload_hash, record.resolved))
    setup.repository.release_claim(claim, now=setup.clock(), next_poll_at=setup.clock())
    control(setup, "cancel")
    setup.tick()
    recovered = setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1)
    assert recovered.binding == binding and recovered.step_state == "cancelling"
    assert setup.executor.created == 1 and setup.executor.looked_up == 1
    assert setup.platform.get_run(OWNER, binding.run_id)["cancel_requested"]


def test_reconciliation_inconclusive_is_audited_once_and_does_not_start_a_poll_loop(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    for _ in range(3):
        setup.tick()
    setup.platform.set_run_state(OWNER, setup.run_id(), "unknown", now=setup.clock())
    setup.tick()
    control(setup, "reconcile", step="develop")
    setup.tick()
    calls = setup.executor.inspected
    setup.tick()
    setup.tick()
    assert setup.executor.inspected == calls
    assert setup.repository.get(OWNER, "workflow-a").reason_code == "inspection_inconclusive"
    events = setup.repository.events(OWNER, "workflow-a").events
    assert sum(item.kind == "step.reconciled" for item in events) == 1


def test_old_receipt_cannot_overwrite_a_new_generation_even_with_a_fresh_lease(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    setup.registrations = tuple(replace(item, reserve_tokens=20, reserve_requests=0) for item in setup.registrations)
    for _ in range(3):
        setup.tick()
    old = setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1)
    setup.platform.set_run_state(OWNER, old.binding.run_id, "failed", now=setup.clock())
    setup.tick()
    control(setup, "retry", step="develop")
    claim = setup.repository.claim_fair_due(worker_id="new", now=setup.clock(), limit=1)[0]
    latest = setup.repository.get(OWNER, "workflow-a")
    with pytest.raises(WorkflowRepositoryError, match="generation_conflict"):
        setup.repository.apply_observation(claim, replace(old, revision=latest.revision), ExecutionObservation(old.binding, "succeeded"), now=setup.clock())
    assert setup.repository.current_steps(OWNER, "workflow-a")[0].generation == 2
    assert setup.repository.current_steps(OWNER, "workflow-a")[0].state == "pending"
