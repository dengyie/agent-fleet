"""Durable dispatch with real SQLite Runs/Tasks and interrupted boundaries."""
from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import os
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from hub.application.legacy_task_bridge import LegacyTaskBridge
from hub.application.task_service import TaskHostPolicy, TaskService
from hub.application.workflow_dispatch import WorkflowDispatchError, WorkflowDispatchService
from hub.domain.workflow_contract import parse_definition, parse_resolved, resolve_contract
from hub.domain.workflow_contract_types import AuthorizedInputs, CatalogRef, ResolvedContract
from hub.domain.workflow_execution import ExecutionBinding, ExecutionObservation, ExecutorCapabilities
from hub.domain.workflow_records import CoordinatorClaim
from hub.infrastructure.platform_db import PlatformRepository, PlatformRepositoryError
from hub.infrastructure.task_repository import SqliteTaskRepository
from hub.infrastructure.workflow_repository import WorkflowRepository, WorkflowRepositoryError
from hub.infrastructure.workflow_run_executor import LegacyTaskTarget, RegisteredRunTarget, RunStepExecutor
from test_workflow_repository import OWNER, WORKSPACE, prepare_database, registered


class Clock:
    def __init__(self) -> None:
        self.now = 104.0

    def __call__(self) -> float:
        return self.now


@dataclass
class Setup:
    repository: WorkflowRepository
    platform: PlatformRepository
    clock: Clock
    claim: CoordinatorClaim
    resolved: ResolvedContract
    target: RegisteredRunTarget

    def executor(self) -> RunStepExecutor:
        return RunStepExecutor(self.repository, self.target, clock=self.clock)

    def dispatch(self, executor: Any | None = None) -> ExecutionObservation:
        service = WorkflowDispatchService(self.repository, executor or self.executor(), clock=self.clock)
        return asyncio.run(service.dispatch(self.claim, step_key="develop", generation=1,
                                           expected_revision=self.repository.get(OWNER, "workflow-a").revision))

    def recover(self) -> None:
        self.clock.now = 200
        self.claim = self.repository.claim_due(OWNER, worker_id="recovered", now=200, limit=1, lease_seconds=60)[0]


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    repository = prepare_database(tmp_path / "platform.db")
    platform = PlatformRepository(repository.db_path)
    platform.create_conversation(OWNER, "workflow-chat", title="Workflow", workspace_id=WORKSPACE)
    platform.create_conversation(OWNER, "execution-chat", title="Step", workspace_id=WORKSPACE)
    definition = registered()
    repository.create_or_get(OWNER, WORKSPACE, "workflow-a", client_token="a", definition=definition,
                             conversation_id="workflow-chat", now=100)
    claim = repository.claim_due(OWNER, worker_id="worker", now=100, limit=1, lease_seconds=60)[0]
    resolved = resolve_contract(definition, AuthorizedInputs(OWNER, WORKSPACE, "develop", CatalogRef("cli", 1), ()))
    ready = repository.freeze_inputs(claim, step_key="develop", generation=1, resolved=resolved, expected_revision=1, now=101)
    repository.admit_step(claim, step_key="develop", generation=1, expected_revision=ready.revision,
                          target_key="node-a", capacity=1, reserve_tokens=100, reserve_requests=2, now=102)
    target = RegisteredRunTarget(OWNER, WORKSPACE, "node-a", CatalogRef("cli", 1), CatalogRef("safe", 1),
                                 "execution-chat", b"{}", LegacyTaskTarget("mac-local", "codex", "agent-fleet"))
    return Setup(repository, platform, Clock(), claim, resolved, target)


class SimulatedCrash(BaseException):
    """Process termination bypasses ordinary exception recovery."""


def counts(setup: Setup) -> tuple[int, int, int]:
    with setup.repository.transaction(write=False) as conn:
        return tuple(conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                     for name in ("runs", "messages", "legacy_task_links"))


def test_resolved_snapshot_rebuild_is_strict_and_immutable(setup: Setup) -> None:
    rebuilt = parse_resolved(setup.resolved.snapshot)
    assert rebuilt == setup.resolved
    raw = json.loads(rebuilt.snapshot)
    raw["extra"] = True
    with pytest.raises(ValueError):
        parse_resolved(json.dumps(raw).encode())
    assert rebuilt.contract.objective == "Produce a checked result"


def test_intent_after_restart_creates_one_real_run_and_atomic_legacy_link(setup: Setup) -> None:
    setup.recover()
    first = setup.dispatch()
    second = setup.dispatch()
    assert first.binding == second.binding
    assert first.binding.run_id != "workflow-a"
    assert first.state == "waiting_external"
    assert counts(setup) == (1, 1, 1)
    run = setup.platform.get_run(OWNER, first.binding.run_id)
    assert run["state"] == "waiting_task"
    assert setup.platform.claim_run(OWNER, worker_id="model", now=201) is None
    detail = setup.platform.get_conversation(OWNER, "execution-chat")
    assert detail["messages"][0]["content"] == setup.resolved.snapshot.decode()


def test_same_token_with_different_payload_or_target_configuration_conflicts(setup: Setup) -> None:
    binding = setup.dispatch().binding
    raw = json.loads(registered().snapshot)
    raw["steps"][0]["contract"]["objective"] = "A different objective"
    different = resolve_contract(parse_definition(json.dumps(raw).encode()),
                                 AuthorizedInputs(OWNER, WORKSPACE, "develop", CatalogRef("cli", 1), ()))
    with pytest.raises(WorkflowDispatchError, match="dispatch_conflict"):
        asyncio.run(setup.executor().create_or_lookup(binding.token, different.digest, different))
    setup.platform.create_conversation(OWNER, "other-chat", title="", workspace_id=WORKSPACE)
    changed = RunStepExecutor(setup.repository, replace(setup.target, conversation_id="other-chat"), clock=setup.clock)
    with pytest.raises(WorkflowDispatchError, match="dispatch_conflict"):
        asyncio.run(changed.lookup(binding.token, binding.payload_hash, setup.resolved))
    assert counts(setup) == (1, 1, 1)


def test_existing_execution_is_visible_after_workspace_revocation(setup: Setup) -> None:
    binding = setup.dispatch().binding
    with setup.repository.transaction() as conn:
        conn.execute("UPDATE workspaces SET enabled=0 WHERE owner_id=?", (OWNER,))
    assert asyncio.run(setup.executor().lookup(binding.token, binding.payload_hash, setup.resolved)) == binding
    assert setup.dispatch().binding == binding


def test_run_creation_and_final_binding_have_recoverable_failure_windows(setup: Setup, tmp_path: Path) -> None:
    class CrashAfterCreate(RunStepExecutor):
        async def create_or_lookup(self, token: str, payload_hash: str, resolved: ResolvedContract) -> ExecutionBinding:
            await super().create_or_lookup(token, payload_hash, resolved)
            raise SimulatedCrash()

    with pytest.raises(SimulatedCrash):
        setup.dispatch(CrashAfterCreate(setup.repository, setup.target, clock=setup.clock))
    assert counts(setup) == (1, 1, 1)
    assert setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1).binding is None
    tasks = SqliteTaskRepository(tmp_path / "tasks.db")
    tasks.init()

    class Observations:
        def read_current(self, machine: str) -> dict[str, bool]:
            return {"reachable": True}

    class Publisher:
        def __init__(self) -> None:
            self.events: list[str] = []

        def emit(self, event: str, **kwargs: Any) -> None:
            self.events.append(event)

    service = TaskService(tasks, Observations(), Publisher(), TaskHostPolicy(project_whitelist={"mac-local": ["agent-fleet"]}))
    bridge = LegacyTaskBridge(setup.platform, service, tasks, clock=setup.clock)
    linked = bridge.process_once(OWNER)
    assert linked["task_id"] is not None
    run_id = linked["run_id"]
    setup.platform.set_run_state(OWNER, run_id, "succeeded", now=105)
    setup.platform.set_conversation_archived(OWNER, "execution-chat", archived=True, now=105)
    with pytest.raises(PlatformRepositoryError, match="conversation_workflow_referenced"):
        setup.platform.delete_archived_conversation(OWNER, "execution-chat")
    setup.recover()
    restored = setup.dispatch()
    assert restored.binding.run_id == run_id
    assert bridge.process_once(OWNER)["task_id"] == linked["task_id"]
    assert len(tasks.list_tasks()) == 1
    assert counts(setup) == (1, 1, 1)


def test_crash_after_binding_inspects_without_recreating(setup: Setup) -> None:
    class CrashOnInspect(RunStepExecutor):
        async def inspect(self, binding: ExecutionBinding) -> Any:
            raise SimulatedCrash()

    with pytest.raises(SimulatedCrash):
        setup.dispatch(CrashOnInspect(setup.repository, setup.target, clock=setup.clock))
    original = setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1).binding
    setup.recover()

    class InspectOnly(RunStepExecutor):
        async def create_or_lookup(self, *args: Any) -> Any:
            raise AssertionError("a bound execution must only be inspected")

        async def lookup(self, *args: Any) -> Any:
            raise AssertionError("a binding is already available")

    assert setup.dispatch(InspectOnly(setup.repository, setup.target, clock=setup.clock)).binding == original
    assert counts(setup) == (1, 1, 1)


def test_lookup_failure_becomes_unknown_and_preserves_cause_and_reservations(setup: Setup) -> None:
    setup.repository.begin_dispatch(setup.claim, step_key="develop", generation=1, expected_revision=3, now=104)
    setup.recover()

    class Unavailable(RunStepExecutor):
        async def lookup(self, *args: Any) -> Any:
            raise OSError("private transport details")

    with pytest.raises(WorkflowDispatchError, match="dispatch_unknown") as error:
        setup.dispatch(Unavailable(setup.repository, setup.target, clock=setup.clock))
    assert isinstance(error.value.__cause__, OSError)
    assert setup.repository.get(OWNER, "workflow-a").execution_state == "unknown"
    with setup.repository.transaction(write=False) as conn:
        assert conn.execute("SELECT state FROM capability_reservations").fetchone()[0] == "held"
        assert conn.execute("SELECT settlement FROM workflow_budget_reservations").fetchone()[0] == "reserved"
    assert b"private" not in b"".join(event.payload for event in setup.repository.events(OWNER, "workflow-a").events)
    assert counts(setup) == (0, 0, 0)


@pytest.mark.parametrize("capabilities", [ExecutorCapabilities(False, False), ExecutorCapabilities(True, False), ExecutorCapabilities(False, True)])
def test_uncertain_nonrecoverable_executor_is_never_replayed(setup: Setup, capabilities: ExecutorCapabilities) -> None:
    setup.repository.begin_dispatch(setup.claim, step_key="develop", generation=1, expected_revision=3, now=104)
    setup.recover()

    class Nonrecoverable(RunStepExecutor):
        async def lookup(self, *args: Any) -> None:
            return None

        async def create_or_lookup(self, *args: Any) -> Any:
            raise AssertionError("must not replay an uncertain execution")

    executor = Nonrecoverable(setup.repository, setup.target, clock=setup.clock)
    executor.capabilities = capabilities
    with pytest.raises(WorkflowDispatchError):
        setup.dispatch(executor)
    assert setup.repository.get(OWNER, "workflow-a").execution_state == "unknown"
    assert counts(setup) == (0, 0, 0)


def test_external_call_never_holds_sqlite_writer_lock(setup: Setup) -> None:
    class LockProbe(RunStepExecutor):
        async def create_or_lookup(self, token: str, payload_hash: str, resolved: ResolvedContract) -> ExecutionBinding:
            with setup.repository.transaction() as conn:
                assert conn.execute("SELECT count(*) FROM workflow_dispatches").fetchone()[0] == 1
            return await super().create_or_lookup(token, payload_hash, resolved)

    assert setup.dispatch(LockProbe(setup.repository, setup.target, clock=setup.clock)).state == "waiting_external"


def test_expired_coordinator_cannot_write_binding_but_successor_recovers_same_run(setup: Setup) -> None:
    class LoseLease(RunStepExecutor):
        async def create_or_lookup(self, token: str, payload_hash: str, resolved: ResolvedContract) -> ExecutionBinding:
            binding = await super().create_or_lookup(token, payload_hash, resolved)
            setup.recover()
            return binding

    original = setup.claim
    with pytest.raises(WorkflowDispatchError, match="lease_mismatch"):
        setup.dispatch(LoseLease(setup.repository, setup.target, clock=setup.clock))
    assert original.epoch < setup.claim.epoch
    assert setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1).binding is None
    assert setup.dispatch().binding.run_id is not None
    assert counts(setup) == (1, 1, 1)


def test_timeout_is_unknown_and_does_not_abandon_an_async_task(setup: Setup) -> None:
    class Hanging(RunStepExecutor):
        async def create_or_lookup(self, *args: Any) -> Any:
            await asyncio.Event().wait()

    async def run() -> None:
        service = WorkflowDispatchService(setup.repository, Hanging(setup.repository, setup.target, clock=setup.clock),
                                          clock=setup.clock, timeout_seconds=0.02)
        with pytest.raises(WorkflowDispatchError, match="dispatch_unknown") as error:
            await service.dispatch(setup.claim, step_key="develop", generation=1, expected_revision=3)
        assert isinstance(error.value.__cause__, asyncio.TimeoutError)
        assert len(asyncio.all_tasks()) == 1

    asyncio.run(run())
    assert setup.repository.get(OWNER, "workflow-a").execution_state == "unknown"


def test_caller_cancellation_retains_sending_for_safe_lookup(setup: Setup) -> None:
    async def run() -> None:
        started = asyncio.Event()

        class Hanging(RunStepExecutor):
            async def create_or_lookup(self, *args: Any) -> Any:
                started.set()
                await asyncio.Event().wait()

        service = WorkflowDispatchService(setup.repository, Hanging(setup.repository, setup.target, clock=setup.clock), clock=setup.clock)
        task = asyncio.create_task(service.dispatch(setup.claim, step_key="develop", generation=1, expected_revision=3))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(asyncio.all_tasks()) == 1

    asyncio.run(run())
    assert setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1).stage == "sending"
    setup.recover()
    assert setup.dispatch().binding.run_id is not None


@pytest.mark.parametrize("mode", ["drain", "frozen"])
def test_closed_admission_never_creates_a_new_run(setup: Setup, mode: str) -> None:
    with setup.repository.transaction() as conn:
        conn.execute("UPDATE workflow_schema_meta SET admission_mode=?", (mode,))
    with pytest.raises(WorkflowDispatchError):
        setup.dispatch()
    assert counts(setup) == (0, 0, 0)


def test_long_instruction_is_rejected_without_truncation_or_child_records(setup: Setup) -> None:
    raw = json.loads(registered().snapshot)
    raw["steps"][0]["contract"]["objective"] = "Complete objective " + "x" * 2001
    definition = parse_definition(json.dumps(raw).encode())
    resolved = resolve_contract(definition, AuthorizedInputs(OWNER, WORKSPACE, "develop", CatalogRef("cli", 1), ()))
    # Persist an independently validated replacement in the fixture before any dispatch.
    with setup.repository.transaction() as conn:
        conn.execute("UPDATE workflow_runs SET definition_snapshot=?,definition_digest=?", (definition.snapshot, hashlib.sha256(definition.snapshot).hexdigest()))
        conn.execute("UPDATE workflow_steps SET contract_snapshot=?,resolved_snapshot=?,resolved_digest=?", (resolved.contract.snapshot, resolved.snapshot, resolved.digest))
        conn.execute("UPDATE workflow_dispatches SET payload_hash=?", (resolved.digest,))
    with pytest.raises(WorkflowDispatchError, match="context_too_large"):
        setup.dispatch()
    assert setup.repository.current_steps(OWNER, "workflow-a")[0].state == "failed"
    assert counts(setup) == (0, 0, 0)


def test_native_run_receives_complete_snapshot_without_a_legacy_task(setup: Setup) -> None:
    raw = json.loads(registered().snapshot)
    raw["steps"][0]["kind"] = "native_run"
    definition = parse_definition(json.dumps(raw).encode())
    with setup.repository.transaction() as conn:
        conn.execute("UPDATE workflow_runs SET definition_snapshot=?,definition_digest=?", (definition.snapshot, hashlib.sha256(definition.snapshot).hexdigest()))
        conn.execute("UPDATE workflow_steps SET kind='native_run'")
    setup.platform.upsert_model(OWNER, {"profile_id": "local", "provider": "deterministic", "model": "local"})
    config = json.dumps({"owner_id": OWNER, "workspace_id": WORKSPACE, "model_profile_id": "local",
                         "provider_snapshot": {"provider": "deterministic", "model": "local", "provider_config": {}, "capabilities": {}}}).encode()
    setup.target = replace(setup.target, config_snapshot=config, legacy_task=None)
    observed = setup.dispatch()
    assert observed.state == "running"
    assert counts(setup) == (1, 1, 0)
    run = setup.platform.get_run(OWNER, observed.binding.run_id)
    assert run["config_snapshot"]["model_profile_id"] == "local"
    assert run["config_snapshot"]["provider_snapshot"]["model"] == "local"


def _create_in_process(path: str, target: RegisteredRunTarget, resolved: ResolvedContract, token: str,
                       barrier: Any, queue: Any, terminate: bool) -> None:
    executor = RunStepExecutor(WorkflowRepository(Path(path)), target, clock=lambda: 104)
    if barrier is not None:
        barrier.wait(timeout=15)
    binding = asyncio.run(executor.create_or_lookup(token, resolved.digest, resolved))
    if terminate:
        os._exit(73)
    queue.put(binding.run_id)


def test_two_os_processes_creating_same_dispatch_return_one_logical_run(setup: Setup) -> None:
    record = setup.repository.begin_dispatch(setup.claim, step_key="develop", generation=1, expected_revision=3, now=104)
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(2), context.Queue()
    processes = [context.Process(target=_create_in_process, args=(str(setup.repository.db_path), setup.target,
                 setup.resolved, record.token, barrier, queue, False)) for _ in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    run_ids = [queue.get(timeout=5) for _ in processes]
    queue.close()
    queue.join_thread()
    assert len(set(run_ids)) == 1
    assert counts(setup) == (1, 1, 1)
    assert setup.dispatch().binding.run_id == run_ids[0]


def test_process_exit_after_subrun_commit_recovers_before_final_binding(setup: Setup) -> None:
    record = setup.repository.begin_dispatch(setup.claim, step_key="develop", generation=1, expected_revision=3, now=104)
    process = multiprocessing.get_context("spawn").Process(target=_create_in_process,
              args=(str(setup.repository.db_path), setup.target, setup.resolved, record.token, None, None, True))
    process.start()
    process.join(timeout=20)
    assert process.exitcode == 73
    assert setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1).binding is None
    assert counts(setup) == (1, 1, 1)
    setup.recover()
    assert setup.dispatch().binding.run_id is not None
    assert counts(setup) == (1, 1, 1)


def test_before_sending_commit_interrupt_rolls_back_without_calling_executor(setup: Setup) -> None:
    # A trigger aborts the same transaction after the phase UPDATE but before its event can commit.
    with setup.repository.transaction() as connection:
        connection.execute("CREATE TRIGGER interrupt_dispatch BEFORE INSERT ON workflow_events "
                           "WHEN NEW.kind='step.dispatch_started' BEGIN SELECT RAISE(ABORT,'interrupt'); END")
    with pytest.raises(WorkflowDispatchError, match="workflow_store"):
        setup.dispatch()
    record = setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1)
    assert record.stage == "intent" and record.revision == 3
    assert counts(setup) == (0, 0, 0)


def test_old_generation_and_cross_scope_binding_are_rejected(setup: Setup) -> None:
    record = setup.repository.begin_dispatch(setup.claim, step_key="develop", generation=1, expected_revision=3, now=104)
    binding = asyncio.run(setup.executor().create_or_lookup(record.token, record.payload_hash, record.resolved))
    with pytest.raises(WorkflowRepositoryError, match="dispatch_conflict"):
        setup.repository.bind_dispatch(setup.claim, record, replace(binding, owner_id="other@example.test"), now=104)
    with setup.repository.transaction() as connection:
        connection.execute("UPDATE workflow_steps SET is_current=0")
    with pytest.raises(WorkflowDispatchError, match="generation_conflict"):
        setup.dispatch()
    assert setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1).binding is None
    assert counts(setup) == (1, 1, 1)


def test_bound_inspection_failure_cannot_claim_execution_never_started(setup: Setup) -> None:
    setup.dispatch()

    class RejectInspection(RunStepExecutor):
        async def inspect(self, binding: ExecutionBinding) -> Any:
            raise WorkflowDispatchError("reference_forbidden", uncertain=False)

    with pytest.raises(WorkflowDispatchError, match="dispatch_unknown"):
        setup.dispatch(RejectInspection(setup.repository, setup.target, clock=setup.clock))
    assert setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1).stage == "unknown"
    assert setup.repository.get(OWNER, "workflow-a").execution_state == "unknown"


def test_recovery_configuration_conflict_preserves_possibly_active_execution(setup: Setup) -> None:
    record = setup.repository.begin_dispatch(setup.claim, step_key="develop", generation=1, expected_revision=3, now=104)
    asyncio.run(setup.executor().create_or_lookup(record.token, record.payload_hash, record.resolved))
    setup.platform.create_conversation(OWNER, "other-chat", title="", workspace_id=WORKSPACE)
    setup.target = replace(setup.target, conversation_id="other-chat")
    with pytest.raises(WorkflowDispatchError, match="dispatch_unknown"):
        setup.dispatch()
    assert setup.repository.get(OWNER, "workflow-a").execution_state == "unknown"
    assert counts(setup) == (1, 1, 1)


def test_dispatch_run_reference_has_database_enforced_owner_scope(setup: Setup) -> None:
    other = "other@example.test"
    setup.platform.create_conversation(other, "other-chat", title="", workspace_id=WORKSPACE)
    setup.platform.append_turn(other, "other-chat", "other-message", "other-run", text="Other owner",
                               client_token="other", config_snapshot={}, now=104)
    with pytest.raises(WorkflowRepositoryError) as error:
        with setup.repository.transaction() as connection:
            connection.execute("UPDATE workflow_dispatches SET binding_run_id='other-run'")
    assert isinstance(error.value.__cause__, sqlite3.IntegrityError)
