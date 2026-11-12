"""Real SQLite/Run coordination with deterministic clocks and OS processes."""
from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import sqlite3
import subprocess
import sys
import threading
from dataclasses import dataclass, replace
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hub.application.workflow_coordinator import RegisteredWorkflowStep, WorkflowCoordinator, WorkflowCoordinatorError
from hub.application.legacy_task_bridge import LegacyTaskBridge
from hub.application.task_service import TaskHostPolicy, TaskService
from hub.bootstrap import start_platform_workflow_coordinator
from hub.domain.workflow_contract import parse_definition, resolve_contract
from hub.domain.workflow_contract_types import AuthorizedInputs, CatalogRef, InputBinding, ResolvedContract, WorkflowDefinition
from hub.domain.workflow_execution import ExecutionBinding, ExecutionObservation
from hub.domain.workflow_evidence import ValidationEvidence, delivery_state
from hub.domain.workflow_identity import dispatch_token
from hub.domain.workflow_projection import project_workflow
from hub.domain.workflow_records import CoordinatorClaim, WorkflowSnapshot
from hub.infrastructure.platform_db import PlatformRepository
from hub.infrastructure.task_repository import SqliteTaskRepository
from hub.infrastructure.workflow_repository import WorkflowRepository, WorkflowRepositoryError
from hub.infrastructure.workflow_run_executor import LegacyTaskTarget, RegisteredRunTarget, RunStepExecutor
from hub.infrastructure import workflow_schema
from test_workflow_repository import OWNER, WORKSPACE, prepare_database, registered


class Clock:
    def __init__(self, now: float = 100) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class CountingExecutor(RunStepExecutor):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.created = 0
        self.looked_up = 0
        self.inspected = 0

    async def create_or_lookup(self, *args: Any, **kwargs: Any) -> ExecutionBinding:
        self.created += 1
        return await super().create_or_lookup(*args, **kwargs)

    async def lookup(self, *args: Any, **kwargs: Any) -> ExecutionBinding | None:
        self.looked_up += 1
        return await super().lookup(*args, **kwargs)

    async def inspect(self, *args: Any, **kwargs: Any) -> ExecutionObservation:
        self.inspected += 1
        return await super().inspect(*args, **kwargs)


@dataclass
class Setup:
    repository: WorkflowRepository
    platform: PlatformRepository
    clock: Clock
    definition: WorkflowDefinition
    registrations: tuple[RegisteredWorkflowStep, ...]
    executor: CountingExecutor

    def coordinator(self, **kwargs: Any) -> WorkflowCoordinator:
        values = {"worker_id": "coordinator", "clock": self.clock, "registrations": self.registrations,
                  "executors": ((OWNER, WORKSPACE, "node-a", self.executor),), "dispatch_enabled": True}
        values.update(kwargs)
        return WorkflowCoordinator(self.repository, **values)

    def tick(self, coordinator: WorkflowCoordinator | None = None):
        result = asyncio.run((coordinator or self.coordinator()).tick(self.clock()))
        self.clock.now += 1
        assert not result.failures, result.failures
        return result

    def run_id(self, step: str = "develop", workflow: str = "workflow-a") -> str:
        return "wf-run-" + dispatch_token(OWNER, workflow, step, 1)


def make_setup(tmp_path: Path, *, count: int = 1, steps: int = 1, outputs: bool = False, source: dict[str, Any] | None = None) -> Setup:
    repository = prepare_database(tmp_path / "platform.db")
    platform = PlatformRepository(repository.db_path)
    platform.create_conversation(OWNER, "execution", title="Execution", workspace_id=WORKSPACE)
    raw = json.loads(registered(source=source).snapshot)
    contract = raw["steps"][0]["contract"]
    if not outputs:
        contract["output_specs"] = []
    keys = ("develop", "review", "test")[:steps]
    raw["steps"] = [{"step_key": key, "kind": "coding_task", "requires": [keys[index - 1]] if index else [],
                     "contract": contract} for index, key in enumerate(keys)]
    definition = parse_definition(json.dumps(raw).encode())
    clock = Clock()
    for index in range(count):
        workflow = "workflow-a" if count == 1 else f"workflow-{index:03d}"
        repository.create_or_get(OWNER, WORKSPACE, workflow, client_token=workflow, definition=definition, now=clock())
    digest = hashlib.sha256(definition.snapshot).hexdigest()
    registrations = tuple(RegisteredWorkflowStep(
        digest, resolve_contract(definition, AuthorizedInputs(OWNER, WORKSPACE, key, CatalogRef("cli", 1),
                                tuple(InputBinding(item.ref_key, OWNER, WORKSPACE, item.reference) for item in definition.steps[0].contract.input_refs))),
        "node-a", max(1, count), 100, 2,
    ) for key in keys)
    target = RegisteredRunTarget(OWNER, WORKSPACE, "node-a", CatalogRef("cli", 1), CatalogRef("safe", 1),
                                 "execution", b"{}", LegacyTaskTarget("mac-local", "codex", "agent-fleet"))
    return Setup(repository, platform, clock, definition, registrations, CountingExecutor(repository, target, clock=clock))


def test_construction_and_empty_ticks_are_passive(tmp_path: Path) -> None:
    repository = prepare_database(tmp_path / "platform.db")
    clock = Clock()
    coordinator = WorkflowCoordinator(repository, worker_id="passive", clock=clock)
    summary = asyncio.run(coordinator.tick(clock()))
    assert (summary.claimed, summary.examined, summary.action_budget_used, summary.failures) == (0, 0, 0, ())
    assert not any(thread.name == "workflow-coordinator" for thread in threading.enumerate())
    with repository.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    with pytest.raises(RuntimeError, match="disabled"):
        start_platform_workflow_coordinator(SimpleNamespace(extensions={}))


def test_budget_projection_cannot_be_silently_disabled_by_an_action_limit(tmp_path: Path) -> None:
    from hub.application.workflow_budget import WorkflowBudgetService
    from hub.infrastructure.workflow_budget import WorkflowBudgetRepository

    setup = make_setup(tmp_path)
    budget = WorkflowBudgetService(WorkflowBudgetRepository(setup.repository), clock=setup.clock)
    with pytest.raises(WorkflowCoordinatorError, match="invalid_coordinator_configuration"):
        setup.coordinator(action_limit=1, budget_service=budget)


def test_fixed_linear_steps_use_original_runs_and_only_release_after_observation(tmp_path: Path) -> None:
    setup = make_setup(tmp_path, steps=3)
    for key in ("develop", "review", "test"):
        for _ in range(3):
            setup.tick()
        current = {step.step_key: step for step in setup.repository.current_steps(OWNER, "workflow-a")}
        assert current[key].state == "waiting_external"
        assert setup.platform.get_run(OWNER, setup.run_id(key))["state"] == "waiting_task"
        setup.platform.set_run_state(OWNER, setup.run_id(key), "succeeded", now=setup.clock())
        setup.tick()
        with setup.repository.transaction(write=False) as connection:
            reservation = connection.execute("SELECT state,release_evidence_digest FROM capability_reservations WHERE step_key=?", (key,)).fetchone()
            assert reservation[0] == "released" and len(reservation[1]) == 64
            budget = connection.execute("SELECT settlement,charged_tokens FROM workflow_budget_reservations WHERE step_key=?", (key,)).fetchone()
            assert tuple(budget) == ("unknown", None)
    workflow = setup.repository.get(OWNER, "workflow-a")
    assert (workflow.execution_state, workflow.validation_state, workflow.acceptance_state) == ("succeeded", "not_requested", "pending")
    before = (setup.executor.created, setup.executor.inspected, workflow.revision)
    assert setup.tick().claimed == 0
    assert (setup.executor.created, setup.executor.inspected, setup.repository.get(OWNER, "workflow-a").revision) == before
    assert setup.executor.created == 3


def test_no_change_poll_keeps_revision_events_generation_and_binding(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    for _ in range(3):
        setup.tick()
    before = setup.repository.get(OWNER, "workflow-a")
    events = setup.repository.events(OWNER, "workflow-a")
    record = setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1)
    for _ in range(4):
        setup.tick(setup.coordinator(worker_id="restarted"))
    assert setup.repository.get(OWNER, "workflow-a").revision == before.revision
    assert setup.repository.events(OWNER, "workflow-a") == events
    assert setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1).binding == record.binding
    assert setup.executor.created == 1 and setup.executor.looked_up == 0
    assert setup.repository.current_steps(OWNER, "workflow-a")[0].generation == 1


def test_unchanged_bound_observation_does_not_write_owner_queue(tmp_path: Path, monkeypatch) -> None:
    setup = make_setup(tmp_path)
    for _ in range(3):
        setup.tick()
    claim = setup.repository.claim_due(OWNER, worker_id="observer", now=setup.clock(),
        limit=1, lease_seconds=60)[0]
    record = setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1)
    observation = asyncio.run(setup.executor.inspect(record.binding))
    original = setup.repository.transaction
    writes = []

    @contextmanager
    def measured(*, write=True):
        with original(write=write) as connection:
            before = connection.total_changes
            yield connection
            if write:
                writes.append(connection.total_changes - before)

    monkeypatch.setattr(setup.repository, "transaction", measured)
    before = setup.repository.get(OWNER, "workflow-a")
    for _ in range(3):
        assert setup.repository.apply_observation(claim, record, observation, now=setup.clock()) == before
    assert writes == [0, 0, 0]


def test_coordinator_releases_tick_claims_in_one_transaction(tmp_path: Path, monkeypatch) -> None:
    setup = make_setup(tmp_path, count=4)
    claims = setup.repository.claim_due(OWNER, worker_id="coordinator", now=setup.clock(),
        limit=4, lease_seconds=60)
    original = setup.repository.transaction
    transactions = []
    refreshes = []
    refresh = setup.repository._refresh_owner_queue

    @contextmanager
    def measured(*, write=True):
        with original(write=write) as connection:
            if write:
                transactions.append(connection)
            yield connection

    def refreshed(connection, owner):
        refreshes.append(owner)
        return refresh(connection, owner)

    monkeypatch.setattr(setup.repository, "transaction", measured)
    monkeypatch.setattr(setup.repository, "_refresh_owner_queue", refreshed)
    asyncio.run(setup.coordinator()._release(claims))
    assert len(transactions) == 1
    assert refreshes == [OWNER]
    with original(write=False) as connection:
        rows = connection.execute("SELECT lease_owner,next_poll_at FROM workflow_runs").fetchall()
        assert len(rows) == 4 and all(tuple(row) == (None, 101) for row in rows)


def test_bound_dispatch_poll_reads_while_another_connection_owns_write_lock(tmp_path: Path, monkeypatch) -> None:
    setup = make_setup(tmp_path)
    for _ in range(3):
        setup.tick()
    claim = setup.repository.claim_due(OWNER, worker_id="observer", now=setup.clock(),
        limit=1, lease_seconds=60)[0]
    record = setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1)
    connect = sqlite3.connect

    def immediate_timeout(*args, **kwargs):
        kwargs["timeout"] = 0
        return connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", immediate_timeout)
    with connect(setup.repository.db_path, isolation_level=None) as writer:
        writer.execute("BEGIN IMMEDIATE")
        try:
            observed = setup.repository.begin_dispatch(claim, step_key="develop", generation=1,
                expected_revision=record.revision, now=setup.clock())
            assert observed == record
            assert writer.total_changes == 0
        finally:
            writer.rollback()


def test_unbound_dispatch_rechecks_revision_after_read_transaction(tmp_path: Path, monkeypatch) -> None:
    setup = make_setup(tmp_path)
    for _ in range(2):
        setup.tick()
    claim = setup.repository.claim_due(OWNER, worker_id="dispatcher", now=setup.clock(),
        limit=1, lease_seconds=60)[0]
    record = setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1)
    assert record.binding is None and record.stage == "intent"
    original = setup.repository.transaction
    modes = []

    @contextmanager
    def raced(*, write=True):
        if write:
            with original() as writer:
                writer.execute("UPDATE workflow_runs SET revision=revision+1 WHERE workflow_id='workflow-a'")
        modes.append(write)
        with original(write=write) as connection:
            yield connection

    monkeypatch.setattr(setup.repository, "transaction", raced)
    with pytest.raises(WorkflowRepositoryError, match="revision_conflict"):
        setup.repository.begin_dispatch(claim, step_key="develop", generation=1,
            expected_revision=record.revision, now=setup.clock())
    assert modes == [False, True]
    with original(write=False) as connection:
        row = connection.execute("SELECT stage,binding_id FROM workflow_dispatches").fetchone()
        assert tuple(row) == ("intent", None)
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


@pytest.mark.parametrize("mutation,error", [
    ("UPDATE workflow_runs SET coordinator_epoch=coordinator_epoch+1", "lease_mismatch"),
    ("UPDATE workflow_runs SET revision=revision+1", "revision_conflict"),
    ("UPDATE workflow_schema_meta SET admission_mode='frozen'", "admission_frozen"),
    ("UPDATE workflow_steps SET is_current=0", "generation_conflict"),
    ("UPDATE workflow_dispatches SET stage='unknown'", "reconciliation_required"),
])
def test_bound_dispatch_read_keeps_current_fences(tmp_path: Path, mutation: str, error: str) -> None:
    setup = make_setup(tmp_path)
    for _ in range(3):
        setup.tick()
    claim = setup.repository.claim_due(OWNER, worker_id="observer", now=setup.clock(),
        limit=1, lease_seconds=60)[0]
    record = setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1)
    with setup.repository.transaction() as connection:
        connection.execute(mutation)
    with pytest.raises(WorkflowRepositoryError, match=error):
        setup.repository.begin_dispatch(claim, step_key="develop", generation=1,
            expected_revision=record.revision, now=setup.clock())


def test_unknown_never_replays_or_releases_capacity_and_usage(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    for _ in range(3):
        setup.tick()
    setup.platform.set_run_state(OWNER, setup.run_id(), "unknown", now=setup.clock())
    setup.tick()
    assert setup.repository.get(OWNER, "workflow-a").execution_state == "unknown"
    assert setup.tick().claimed == 0
    with setup.repository.transaction(write=False) as connection:
        assert connection.execute("SELECT state FROM capability_reservations").fetchone()[0] == "held"
        assert connection.execute("SELECT settlement FROM workflow_budget_reservations").fetchone()[0] == "unknown"
    assert setup.executor.created == 1


def test_required_outputs_block_successor_and_delivery(tmp_path: Path) -> None:
    setup = make_setup(tmp_path, steps=2, outputs=True)
    for _ in range(3):
        setup.tick()
    setup.platform.set_run_state(OWNER, setup.run_id(), "succeeded", now=setup.clock())
    setup.tick()
    for _ in range(2):
        setup.tick()
    workflow = setup.repository.get(OWNER, "workflow-a")
    assert (workflow.execution_state, workflow.reason_code) == ("waiting_approval", "outputs_unavailable")
    assert setup.repository.current_steps(OWNER, "workflow-a")[1].state == "pending"
    assert setup.executor.created == 1
    assert delivery_state("succeeded", "not_requested", "not_requested", validation_required=False,
                          acceptance_required=False, required_outputs_ready=False) == "awaiting_validation"


@pytest.mark.parametrize("subject,validator,result,expected", [
    ("b" * 64, "review@1", "passed", "pending"),
    ("a" * 64, "review@2", "passed", "pending"),
    ("a" * 64, "review@1", "failed", "failed"),
    ("a" * 64, "review@1", "inconclusive", "inconclusive"),
    ("a" * 64, "review@1", "passed", "passed"),
])
def test_projection_uses_current_evidence_instead_of_cached_verdict(
    tmp_path: Path, subject: str, validator: str, result: str, expected: str,
) -> None:
    setup = make_setup(tmp_path, steps=2)
    raw = json.loads(setup.definition.snapshot)
    raw["steps"][0]["contract"]["validator_refs"] = [{"id": "review", "revision": 1}]
    definition = parse_definition(json.dumps(raw).encode())
    workflow = replace(setup.repository.get(OWNER, "workflow-a"), definition_snapshot=definition.snapshot)
    steps = setup.repository.current_steps(OWNER, "workflow-a")
    first = replace(steps[0], state="succeeded", validation_state="passed", subject_digest="a" * 64,
                    evidence=(ValidationEvidence("report", validator, subject, result),))
    projection = project_workflow(definition, WorkflowSnapshot(workflow, (first, steps[1]), "open"))
    assert projection.validation_state == expected
    assert projection.eligible_step == ("review" if expected == "passed" else None)
    if expected == "failed":
        assert projection.reason_code == "blocked_by_validation"


def test_tick_bounds_workflows_and_reserves_entire_dispatch_action_cost(tmp_path: Path) -> None:
    setup = make_setup(tmp_path, count=32)
    assert setup.tick().action_budget_used == 32
    assert setup.tick().action_budget_used == 64
    summary = setup.tick()
    assert (summary.claimed, summary.examined, summary.action_budget_used, summary.deferred) == (32, 21, 63, 11)
    assert setup.executor.created == 21
    setup.tick()
    assert setup.executor.created == 32
    assert max(step.generation for step in setup.repository.current_steps(OWNER, "workflow-000")) == 1


def test_owner_queue_rotates_at_identical_time_and_survives_new_repository(tmp_path: Path) -> None:
    setup = make_setup(tmp_path, count=40)
    for index in range(2):
        setup.repository.create_or_get("other@example.test", WORKSPACE, f"other-{index}", client_token=f"other-{index}",
                                       definition=setup.definition, now=100)
    claimed = WorkflowRepository(setup.repository.db_path).claim_fair_due(worker_id="fair", now=100, limit=4)
    assert [claim.owner_id for claim in claimed] == ["other@example.test", OWNER, "other@example.test", OWNER]
    assert len({claim.workflow_id for claim in claimed}) == 4
    assert WorkflowRepository(setup.repository.db_path).claim_fair_due(worker_id="next", now=100, limit=1)[0].owner_id == OWNER


def test_inspection_finishes_before_new_admission_uses_released_slot(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    for _ in range(3):
        setup.tick()
    setup.platform.set_run_state(OWNER, setup.run_id(), "succeeded", now=setup.clock())
    setup.repository.create_or_get(OWNER, WORKSPACE, "workflow-b", client_token="b", definition=setup.definition, now=setup.clock())
    claims = setup.repository.claim_due(OWNER, worker_id="prepare-b", now=setup.clock(), limit=2)
    claim = next(item for item in claims if item.workflow_id == "workflow-b")
    for item in claims:
        if item.workflow_id != "workflow-b":
            setup.repository.release_claim(item, now=setup.clock(), next_poll_at=setup.clock())
    setup.repository.freeze_inputs(claim, step_key="develop", generation=1, resolved=setup.registrations[0].resolved,
                                  expected_revision=1, now=setup.clock())
    setup.repository.release_claim(claim, now=setup.clock(), next_poll_at=setup.clock())
    setup.tick()
    assert setup.repository.current_steps(OWNER, "workflow-b")[0].state == "dispatching"
    setup.tick()
    assert setup.executor.created == 2


def test_capacity_wait_does_not_repeat_events_and_recovers_without_refreezing(tmp_path: Path) -> None:
    setup = make_setup(tmp_path, count=2)
    registration = replace(setup.registrations[0], capacity=1)
    coordinator = setup.coordinator(registrations=(registration,))
    setup.tick(coordinator)
    setup.tick(coordinator)
    current = {row.workflow_id: setup.repository.current_steps(OWNER, row.workflow_id)[0] for row in setup.repository.list(OWNER)}
    active = next(key for key, step in current.items() if step.state == "dispatching")
    waiting = next(key for key, step in current.items() if step.state == "waiting_node")
    events = setup.repository.events(OWNER, waiting)
    setup.tick(coordinator)
    setup.tick(coordinator)
    assert setup.repository.events(OWNER, waiting) == events
    setup.platform.set_run_state(OWNER, setup.run_id(workflow=active), "succeeded", now=setup.clock())
    setup.tick(coordinator)
    assert setup.repository.current_steps(OWNER, waiting)[0].state == "dispatching"
    ready_events = [event for event in setup.repository.events(OWNER, waiting).events if event.kind == "step.ready"]
    assert len(ready_events) == 1


def test_disabled_dispatch_and_drain_inspect_existing_but_never_create(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    setup.tick()
    setup.tick()
    with setup.repository.transaction() as connection:
        connection.execute("UPDATE workflow_schema_meta SET admission_mode='drain'")
    setup.tick(setup.coordinator(dispatch_enabled=False))
    assert setup.executor.created == 0
    assert setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1).stage == "intent"
    with setup.repository.transaction() as connection:
        connection.execute("UPDATE workflow_schema_meta SET admission_mode='open'")
    setup.tick()
    with setup.repository.transaction() as connection:
        connection.execute("UPDATE workflow_schema_meta SET admission_mode='drain'")
    before = setup.executor.inspected
    setup.platform.set_run_state(OWNER, setup.run_id(), "succeeded", now=setup.clock())
    setup.tick(setup.coordinator(dispatch_enabled=False))
    assert setup.executor.created == 1 and setup.executor.inspected == before + 1
    assert setup.repository.get(OWNER, "workflow-a").execution_state == "succeeded"


def test_frozen_recovery_does_no_claim_or_executor_io(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    with setup.repository.transaction() as connection:
        connection.execute("UPDATE workflow_schema_meta SET admission_mode='frozen'")
    assert setup.tick().claimed == 0
    assert (setup.executor.created, setup.executor.looked_up, setup.executor.inspected) == (0, 0, 0)


def test_missing_registration_is_an_idempotent_wait(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    coordinator = setup.coordinator(registrations=())
    setup.tick(coordinator)
    events = setup.repository.events(OWNER, "workflow-a")
    setup.tick(coordinator)
    assert setup.repository.events(OWNER, "workflow-a") == events
    assert setup.repository.get(OWNER, "workflow-a").reason_code == "step_registration_missing"
    setup.tick()
    assert setup.repository.current_steps(OWNER, "workflow-a")[0].state == "ready"


def test_stop_join_cancels_inflight_create_and_recovery_looks_up_original_run(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    setup.tick()
    setup.tick()
    entered, cancelled = threading.Event(), threading.Event()

    class InterruptedCreate(CountingExecutor):
        async def create_or_lookup(self, *args: Any, **kwargs: Any):
            await super().create_or_lookup(*args, **kwargs)
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

    executor = InterruptedCreate(setup.repository, setup.executor.target, clock=setup.clock)
    errors: list[WorkflowCoordinatorError] = []
    handle = setup.coordinator(executors=((OWNER, WORKSPACE, "node-a", executor),)).start(on_error=errors.append)
    try:
        assert entered.wait(5)
    finally:
        handle.stop()
        handle.join(10)
    assert cancelled.is_set() and not handle.thread.is_alive() and not errors
    record = setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1)
    assert record.stage == "sending" and record.binding is None
    assert setup.platform.get_run(OWNER, setup.run_id())["state"] == "waiting_task"
    setup.clock.now += 2
    setup.tick(setup.coordinator(worker_id="recovered"))
    assert setup.executor.created == 0 and setup.executor.looked_up == 1
    assert setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1).binding.run_id == setup.run_id()
    handle.stop()
    handle.join(0)


def test_worker_reports_failure_once_at_entrypoint_and_join_retains_fatal_cause(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    called = threading.Event()
    original = OSError("private failure")

    def fail(now: float):
        raise WorkflowCoordinatorError("test_failure") from original

    async def failed_tick(now: float):
        fail(now)

    coordinator = setup.coordinator()
    coordinator.tick = failed_tick

    def reporter(error: WorkflowCoordinatorError) -> None:
        called.set()
        assert error.__cause__ is original
        raise RuntimeError("log sink unavailable")

    handle = coordinator.start(on_error=reporter)
    assert called.wait(5)
    handle.stop()
    with pytest.raises(WorkflowCoordinatorError, match="workflow_worker_failed") as error:
        handle.join(10)
    assert isinstance(error.value.__cause__, RuntimeError)


def _process_tick(path: str, target: RegisteredRunTarget, registration: RegisteredWorkflowStep, barrier: Any, queue: Any) -> None:
    repository = WorkflowRepository(Path(path))
    clock = Clock(102)
    executor = RunStepExecutor(repository, target, clock=clock)
    coordinator = WorkflowCoordinator(repository, worker_id=f"process-{multiprocessing.current_process().pid}", clock=clock,
                                      registrations=(registration,), executors=((OWNER, WORKSPACE, "node-a", executor),), dispatch_enabled=True)
    barrier.wait(timeout=10)
    result = asyncio.run(coordinator.tick(clock()))
    queue.put((result.claimed, tuple(error.code for error in result.failures)))


def test_two_real_process_coordinators_produce_one_logical_run(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    setup.tick()
    setup.tick()
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(2), context.Queue()
    processes = [context.Process(target=_process_tick, args=(str(setup.repository.db_path), setup.executor.target,
                                                            setup.registrations[0], barrier, queue)) for _ in range(2)]
    for process in processes:
        process.start()
    outcomes = [queue.get(timeout=15) for _ in processes]
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    assert sorted(outcomes) == [(0, ()), (1, ())]
    with setup.repository.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM legacy_task_links").fetchone()[0] == 1
    assert setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1).binding.run_id == setup.run_id()


def _expired_writer(path: str, ready: Any, proceed: Any, queue: Any) -> None:
    repository = WorkflowRepository(Path(path))
    claim = repository.claim_fair_due(worker_id="old-process", now=100, limit=1, lease_seconds=2)[0]
    ready.set()
    assert proceed.wait(timeout=10)
    try:
        repository.refresh_projection(claim, expected_revision=1, now=103)
    except WorkflowRepositoryError as error:
        queue.put(error.code)
    else:
        queue.put("unfenced")


def _reclaimer(path: str, queue: Any) -> None:
    repository = WorkflowRepository(Path(path))
    claim = repository.claim_fair_due(worker_id="new-process", now=102, limit=1, lease_seconds=20)[0]
    queue.put(claim)


def test_real_expired_process_cannot_write_after_other_process_reclaims(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    context = multiprocessing.get_context("spawn")
    ready, proceed, queue = context.Event(), context.Event(), context.Queue()
    old = context.Process(target=_expired_writer, args=(str(setup.repository.db_path), ready, proceed, queue))
    old.start()
    assert ready.wait(timeout=10)
    new = context.Process(target=_reclaimer, args=(str(setup.repository.db_path), queue))
    new.start()
    claim = queue.get(timeout=10)
    assert type(claim) is CoordinatorClaim and claim.epoch == 2
    proceed.set()
    assert queue.get(timeout=10) == "lease_mismatch"
    for process in (old, new):
        process.join(timeout=10)
        assert process.exitcode == 0
    setup.repository.freeze_inputs(claim, step_key="develop", generation=1, resolved=setup.registrations[0].resolved,
                                  expected_revision=1, now=103)
    setup.repository.admit_step(claim, step_key="develop", generation=1, expected_revision=2, target_key="node-a", capacity=1,
                               reserve_tokens=100, reserve_requests=2, now=103)
    record = setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1)
    assert record.token == dispatch_token(OWNER, "workflow-a", "develop", 1) and record.generation == 1


@pytest.mark.parametrize("options", [{"workflow_limit": True}, {"action_limit": 65}, {"lease_seconds": float("nan")}, {"dispatch_enabled": 1}])
def test_coordinator_options_are_strict(tmp_path: Path, options: dict[str, Any]) -> None:
    with pytest.raises(WorkflowCoordinatorError, match="invalid_coordinator_configuration"):
        make_setup(tmp_path).coordinator(**options)


def test_coordinator_giant_integer_configuration_is_a_typed_error(tmp_path: Path) -> None:
    with pytest.raises(WorkflowCoordinatorError, match="invalid_coordinator_configuration"):
        make_setup(tmp_path).coordinator(lease_seconds=10 ** 400)


@pytest.mark.parametrize("operation", ["authorize", "bind", "fail", "observe"])
def test_dispatch_writes_require_original_token_hash_and_target(tmp_path: Path, operation: str) -> None:
    setup = make_setup(tmp_path)
    setup.tick()
    setup.tick()
    claim = setup.repository.claim_fair_due(worker_id="guarded", now=setup.clock(), limit=1)[0]
    record = setup.repository.begin_dispatch(claim, step_key="develop", generation=1, expected_revision=3, now=setup.clock())
    if operation == "authorize":
        with pytest.raises(WorkflowRepositoryError, match="dispatch_conflict"):
            setup.repository.authorize_dispatch(claim, replace(record, target_key="wrong-target"), now=setup.clock())
        return
    if operation == "fail":
        with pytest.raises(WorkflowRepositoryError, match="dispatch_conflict"):
            setup.repository.fail_dispatch(claim, replace(record, token="0" * 64), reason_code="transport_failed", uncertain=True, now=setup.clock())
        return
    binding = asyncio.run(setup.executor.create_or_lookup(record.token, record.payload_hash, record.resolved))
    if operation == "bind":
        with pytest.raises(WorkflowRepositoryError, match="dispatch_conflict"):
            setup.repository.bind_dispatch(claim, replace(record, target_key="wrong-target"), replace(binding, target_key="wrong-target"), now=setup.clock())
        return
    bound = setup.repository.bind_dispatch(claim, record, binding, now=setup.clock())
    observation = asyncio.run(setup.executor.inspect(binding))
    with pytest.raises(WorkflowRepositoryError, match="dispatch_conflict"):
        setup.repository.apply_observation(claim, replace(bound, payload_hash="0" * 64), observation, now=setup.clock())


def test_terminal_unknown_cannot_be_rewritten_as_never_started_failure(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    setup.tick()
    setup.tick()
    claim = setup.repository.claim_fair_due(worker_id="guarded", now=setup.clock(), limit=1)[0]
    record = setup.repository.begin_dispatch(claim, step_key="develop", generation=1, expected_revision=3, now=setup.clock())
    setup.repository.fail_dispatch(claim, record, reason_code="transport_failed", uncertain=True, now=setup.clock())
    current = setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1)
    with pytest.raises(WorkflowRepositoryError, match="reconciliation_required"):
        setup.repository.fail_dispatch(claim, current, reason_code="not_started", uncertain=False, now=setup.clock())
    assert setup.repository.get(OWNER, "workflow-a").execution_state == "unknown"


def test_huge_dispatch_timeout_is_a_typed_error(tmp_path: Path) -> None:
    from hub.application.workflow_dispatch import WorkflowDispatchError, WorkflowDispatchService

    setup = make_setup(tmp_path)
    with pytest.raises(WorkflowDispatchError, match="invalid_dispatch_timeout"):
        WorkflowDispatchService(setup.repository, setup.executor, clock=setup.clock, timeout_seconds=10 ** 400)


@pytest.mark.parametrize("pause,cancel", [(True, False), (False, True)])
def test_control_flags_stop_admission_but_preserve_existing_execution_facts(tmp_path: Path, pause: bool, cancel: bool) -> None:
    from hub.domain.workflow_control import WorkflowControlRequest
    from hub.infrastructure.workflow_control_repository import WorkflowControlRepository

    setup = make_setup(tmp_path)
    setup.tick()
    controls = WorkflowControlRepository(setup.repository)
    controls.control(WorkflowControlRequest(OWNER, "workflow-a", "pause" if pause else "cancel", 2, "control"), now=setup.clock())
    setup.tick()
    assert setup.executor.created == 0
    assert setup.repository.current_steps(OWNER, "workflow-a")[0].state == ("ready" if pause else "cancelled")
    if pause:
        current = setup.repository.get(OWNER, "workflow-a")
        controls.control(WorkflowControlRequest(OWNER, "workflow-a", "resume", current.revision, "resume"), now=setup.clock())
    else:
        # A cancelled workflow never reopens. Use a new execution for the race.
        other = tmp_path / "completion-race"
        other.mkdir()
        setup = make_setup(other)
        controls = WorkflowControlRepository(setup.repository)
        setup.tick()
    setup.tick()
    setup.tick()
    current = setup.repository.get(OWNER, "workflow-a")
    controls.control(WorkflowControlRequest(OWNER, "workflow-a", "pause" if pause else "cancel", current.revision, "race"), now=setup.clock())
    setup.platform.set_run_state(OWNER, setup.run_id(), "succeeded", now=setup.clock())
    setup.tick()
    assert setup.repository.current_steps(OWNER, "workflow-a")[0].state == "succeeded"
    assert setup.repository.get(OWNER, "workflow-a").execution_state == "succeeded"
    assert setup.executor.created == 1


def test_drain_recovers_sending_binding_without_permitting_create(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    setup.tick()
    setup.tick()
    claim = setup.repository.claim_fair_due(worker_id="crashed", now=setup.clock(), limit=1)[0]
    record = setup.repository.begin_dispatch(claim, step_key="develop", generation=1, expected_revision=3, now=setup.clock())
    binding = asyncio.run(setup.executor.create_or_lookup(record.token, record.payload_hash, record.resolved))
    setup.repository.release_claim(claim, now=setup.clock(), next_poll_at=setup.clock())
    with setup.repository.transaction() as connection:
        connection.execute("UPDATE workflow_schema_meta SET admission_mode='drain'")
    setup.tick(setup.coordinator(dispatch_enabled=False))
    current = setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1)
    assert current.binding == binding and current.stage == "bound"
    assert setup.executor.created == 1 and setup.executor.looked_up == 1


def test_missing_executor_retains_binding_and_slot_without_log_spam(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    for _ in range(3):
        setup.tick()
    coordinator = setup.coordinator(executors=())
    setup.tick(coordinator)
    events = setup.repository.events(OWNER, "workflow-a")
    setup.tick(coordinator)
    assert setup.repository.events(OWNER, "workflow-a") == events
    with setup.repository.transaction(write=False) as connection:
        assert connection.execute("SELECT state FROM capability_reservations").fetchone()[0] == "held"
    setup.tick()
    assert setup.executor.created == 1


def test_explicit_init_rebuilds_optional_queue_for_an_older_schema_v1(tmp_path: Path) -> None:
    platform = PlatformRepository(tmp_path / "older.db")
    platform.init()
    platform.upsert_workspace(OWNER, {"workspace_id": WORKSPACE, "root_path": "/unused"})
    repository = WorkflowRepository(platform.db_path)
    definition = registered()
    with repository.transaction() as connection:
        for statement in workflow_schema.SCHEMA_STATEMENTS:
            connection.execute(statement)
        connection.execute("INSERT INTO workflow_schema_meta VALUES(1,1,1,'frozen')")
        connection.execute(
            "INSERT INTO workflow_runs(workflow_id,owner_id,workspace_id,client_token,request_digest,definition_snapshot,definition_digest,"
            "acceptance_required,next_poll_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,1,100,100,100)",
            ("old", OWNER, WORKSPACE, "old", "a" * 64, definition.snapshot, hashlib.sha256(definition.snapshot).hexdigest()),
        )
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='workflow_coordinator_owners'").fetchone() is None
    repository.init()
    repository.init()
    with repository.transaction(write=False) as connection:
        assert tuple(connection.execute("SELECT owner_id,ready_at,service_sequence FROM workflow_coordinator_owners").fetchone()) == (OWNER, 100.0, 0)
        assert connection.execute("SELECT admission_mode FROM workflow_schema_meta").fetchone()[0] == "frozen"
    assert repository.claim_fair_due(worker_id="new", now=100) == ()


def test_fair_queue_database_work_is_bounded_by_claims_not_completed_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    setup = make_setup(tmp_path)
    with setup.repository.transaction() as connection:
        connection.executemany(
            "INSERT INTO workflow_runs(workflow_id,owner_id,workspace_id,client_token,request_digest,definition_snapshot,definition_digest,"
            "execution_state,acceptance_required,next_poll_at,created_at,updated_at) "
            "SELECT ?,owner_id,workspace_id,?,request_digest,definition_snapshot,definition_digest,'succeeded',acceptance_required,0,0,0 "
            "FROM workflow_runs WHERE workflow_id='workflow-a'", ((f"history-{index}", f"history-{index}") for index in range(2000)),
        )
    ticks = 0
    original = setup.repository.transaction

    def progress() -> int:
        nonlocal ticks
        ticks += 1
        return int(ticks > 100)

    @contextmanager
    def bounded_transaction(*, write: bool = True):
        with original(write=write) as connection:
            connection.set_progress_handler(progress, 100)
            yield connection

    monkeypatch.setattr(setup.repository, "transaction", bounded_transaction)
    claim = setup.repository.claim_fair_due(worker_id="bounded", now=100, limit=1)
    assert len(claim) == 1 and claim[0].workflow_id == "workflow-a"
    assert ticks <= 100


@pytest.mark.parametrize("state", ["cancelled", "expired"])
def test_legacy_terminal_projection_without_stop_evidence_keeps_unknown(tmp_path: Path, state: str) -> None:
    setup = make_setup(tmp_path)
    for _ in range(3):
        setup.tick()
    setup.platform.apply_legacy_task_projection(OWNER, setup.run_id(), {
        "bridge_state": "cancelled" if state == "cancelled" else "failed", "task_state": state,
        "task_id": "original-task", "attempt_id": "original-attempt", "summary": "",
    }, now=setup.clock())
    setup.tick()
    assert setup.repository.get(OWNER, "workflow-a").execution_state == "unknown"
    assert setup.repository.current_steps(OWNER, "workflow-a")[0].state == "unknown"
    with setup.repository.transaction(write=False) as connection:
        assert connection.execute("SELECT state FROM capability_reservations").fetchone()[0] == "held"
    assert setup.executor.created == 1


def test_real_task_cancel_and_bridge_cannot_claim_a_live_runner_has_stopped(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    for _ in range(3):
        setup.tick()
    tasks = SqliteTaskRepository(tmp_path / "tasks.db")
    tasks.init()
    observations = SimpleNamespace(read_current=lambda machine: {"reachable": True})
    publisher = SimpleNamespace(emit=lambda event, **kwargs: None)
    service = TaskService(tasks, observations, publisher, TaskHostPolicy(project_whitelist={"mac-local": ["agent-fleet"]}))
    bridge = LegacyTaskBridge(setup.platform, service, tasks, clock=setup.clock)
    linked = bridge.process_once(OWNER)
    lease = tasks.lease_task(machine="mac-local", runner_id="fixture-runner")
    assert lease["task_id"] == linked["task_id"]
    assert tasks.heartbeat(attempt_id=lease["attempt_id"], nonce=lease["nonce"]) is not None
    process = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    try:
        assert process.poll() is None
        service.cancel(linked["task_id"], OWNER)
        bridge.process_once(OWNER)
        assert process.poll() is None
        assert setup.platform.get_run(OWNER, setup.run_id())["state"] == "cancelled"
        setup.tick()
        assert setup.repository.get(OWNER, "workflow-a").execution_state == "unknown"
        with setup.repository.transaction(write=False) as connection:
            assert connection.execute("SELECT state FROM capability_reservations").fetchone()[0] == "held"
    finally:
        process.stdin.close()
        process.wait(timeout=5)
