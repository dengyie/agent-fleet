"""Workflow budget projects the original usage ledger without charging twice."""
from __future__ import annotations

import asyncio
import multiprocessing
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hub.application.workflow_budget import WorkflowBudgetService
from hub.infrastructure.workflow_budget import WorkflowBudgetRepository
from hub.infrastructure.usage_repository import UsageLimitExceeded, UsageRepository
from test_workflow_coordinator import Setup, make_setup
from test_workflow_control import control
from test_workflow_repository import OWNER


def running(tmp_path: Path) -> tuple[Setup, UsageRepository, WorkflowBudgetService]:
    setup = make_setup(tmp_path)
    usage = UsageRepository(setup.repository.db_path, enforce_limits=False)
    usage.init()
    for _ in range(3):
        setup.tick()
    return setup, usage, WorkflowBudgetService(WorkflowBudgetRepository(setup.repository), clock=setup.clock)


def reservation(setup: Setup) -> tuple[object, ...]:
    with setup.repository.transaction(write=False) as connection:
        return tuple(connection.execute("SELECT settlement,charged_tokens,charged_requests FROM workflow_budget_reservations").fetchone())


def test_missing_usage_is_unknown_instead_of_zero_cost(tmp_path: Path) -> None:
    setup, _, service = running(tmp_path)
    setup.platform.set_run_state(OWNER, setup.run_id(), "succeeded", now=setup.clock())
    setup.tick()
    asyncio.run(service.tick(limit=8))
    assert reservation(setup) == ("unknown", None, None)


def test_projected_usage_is_idempotent_and_late_usage_can_settle(tmp_path: Path) -> None:
    setup, usage, service = running(tmp_path)
    pending = usage.admit(OWNER, "model", setup.run_id(), 1, 1, now=setup.clock())
    setup.platform.set_run_state(OWNER, setup.run_id(), "succeeded", now=setup.clock())
    setup.tick()
    asyncio.run(service.tick(limit=8))
    assert reservation(setup)[0] == "unknown"
    usage.settle(pending, {"input_tokens": 4, "output_tokens": 6}, "succeeded", now=setup.clock())
    asyncio.run(service.tick(limit=8))
    assert reservation(setup) == ("settled", 10, 1)
    for _ in range(3):
        asyncio.run(service.tick(limit=8))
    assert usage.get_run_usage(OWNER, setup.run_id())["provider_requests"] == 1
    assert usage.get_run_usage(OWNER, setup.run_id())["total_tokens"] == 10
    with setup.repository.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM platform_usage_events").fetchone()[0] == 1


def test_missing_provider_usage_keeps_reservation_even_if_ledger_normalizes_zero(tmp_path: Path) -> None:
    setup, usage, service = running(tmp_path)
    pending = usage.admit(OWNER, "model", setup.run_id(), 1, 1, now=setup.clock())
    usage.settle(pending, None, "unknown", now=setup.clock())
    setup.platform.set_run_state(OWNER, setup.run_id(), "failed", now=setup.clock())
    setup.tick()
    asyncio.run(service.tick(limit=8))
    assert reservation(setup) == ("unknown", 0, 1)
    with pytest.raises(UsageLimitExceeded):
        usage.admit(OWNER, "model", setup.run_id(), 1, 2, now=setup.clock())


def test_request_limit_is_checked_in_original_ledger_with_limits_gate_off(tmp_path: Path) -> None:
    setup, usage, _ = running(tmp_path)
    for step in (1, 2):
        pending = usage.admit(OWNER, "model", setup.run_id(), 1, step, now=setup.clock())
        usage.settle(pending, {"input_tokens": 1, "output_tokens": 1}, "succeeded", now=setup.clock())
    with pytest.raises(UsageLimitExceeded) as error:
        usage.admit(OWNER, "model", setup.run_id(), 1, 3, now=setup.clock())
    assert error.value.dimension == "requests"


def test_actual_tokens_and_pending_estimates_bound_next_provider_call(tmp_path: Path) -> None:
    setup, usage, _ = running(tmp_path)
    pending = usage.admit(OWNER, "model", setup.run_id(), 1, 1, now=setup.clock(), estimated_tokens=80)
    with pytest.raises(UsageLimitExceeded):
        usage.admit(OWNER, "model", setup.run_id(), 1, 2, now=setup.clock(), estimated_tokens=21)
    usage.settle(pending, {"input_tokens": 80, "output_tokens": 20}, "succeeded", now=setup.clock())
    with pytest.raises(UsageLimitExceeded) as error:
        usage.admit(OWNER, "model", setup.run_id(), 1, 2, now=setup.clock())
    assert error.value.dimension == "tokens"


def test_parallel_pending_model_calls_are_counted_without_serializing_their_execution(tmp_path: Path) -> None:
    setup, usage, _ = running(tmp_path)
    first = usage.admit(OWNER, "model", setup.run_id(), 1, 1, now=setup.clock(), estimated_tokens=40)
    second = usage.admit(OWNER, "model", setup.run_id(), 1, 2, now=setup.clock(), estimated_tokens=40)
    assert first.reservation_id != second.reservation_id
    with pytest.raises(UsageLimitExceeded) as error:
        usage.admit(OWNER, "model", setup.run_id(), 1, 3, now=setup.clock())
    assert error.value.dimension == "requests"


def test_request_checks_respect_active_deadline_before_the_next_poll(tmp_path: Path) -> None:
    setup, usage, _ = running(tmp_path)
    with setup.repository.transaction() as connection:
        connection.execute("UPDATE workflow_steps SET state='running',active_deadline=?", (setup.clock() - 1,))
    with pytest.raises(UsageLimitExceeded):
        usage.admit(OWNER, "model", setup.run_id(), 1, 1, now=setup.clock())
    setup.tick()
    assert setup.repository.current_steps(OWNER, "workflow-a")[0].state == "cancelling"


def test_retry_can_use_only_remaining_actual_budget(tmp_path: Path) -> None:
    setup, usage, service = running(tmp_path)
    pending = usage.admit(OWNER, "model", setup.run_id(), 1, 1, now=setup.clock())
    usage.settle(pending, {"input_tokens": 40, "output_tokens": 40}, "succeeded", now=setup.clock())
    setup.platform.set_run_state(OWNER, setup.run_id(), "failed", now=setup.clock())
    setup.tick()
    asyncio.run(service.tick(limit=8))
    control(setup, "retry", step="develop")
    setup.registrations = tuple(replace(item, reserve_tokens=20, reserve_requests=1) for item in setup.registrations)
    for _ in range(3):
        setup.tick()
    record = setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 2)
    with pytest.raises(UsageLimitExceeded):
        usage.admit(OWNER, "model", record.binding.run_id, 1, 1, now=setup.clock(), estimated_tokens=21)
    admitted = usage.admit(OWNER, "model", record.binding.run_id, 1, 1, now=setup.clock(), estimated_tokens=20)
    assert admitted.run_id == record.binding.run_id


def test_unsent_wait_deadline_fails_without_creating_an_execution(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    setup.tick()
    control(setup, "pause")
    setup.clock.now += 121
    setup.tick()
    assert setup.repository.get(OWNER, "workflow-a").execution_state == "failed"
    assert setup.repository.current_steps(OWNER, "workflow-a")[0].reason_code == "wait_deadline_exceeded"
    assert setup.executor.created == 0


def test_external_wait_deadline_requests_stop_then_preserves_unknown(tmp_path: Path) -> None:
    setup, _, _ = running(tmp_path)
    setup.clock.now += 121
    setup.tick()
    assert setup.repository.current_steps(OWNER, "workflow-a")[0].state == "cancelling"
    assert setup.platform.get_run(OWNER, setup.run_id())["cancel_requested"]
    setup.clock.now += 31
    setup.tick()
    assert setup.repository.get(OWNER, "workflow-a").execution_state == "unknown"
    assert reservation(setup)[0] == "unknown"


def test_budget_projection_round_robin_is_bounded_even_with_an_unchanged_clock(tmp_path: Path) -> None:
    setup = make_setup(tmp_path, count=9)
    for _ in range(3):
        setup.tick()
    service = WorkflowBudgetService(WorkflowBudgetRepository(setup.repository), clock=setup.clock)
    first = asyncio.run(service.tick(limit=3))
    second = asyncio.run(service.tick(limit=3))
    third = asyncio.run(service.tick(limit=3))
    assert (first.examined, second.examined, third.examined) == (3, 3, 3)
    with setup.repository.transaction(write=False) as connection:
        sequences = [row[0] for row in connection.execute("SELECT service_sequence FROM workflow_budget_checks ORDER BY service_sequence")]
    assert sequences == list(range(1, 10))


def _last_request(path: str, run: str, step: int, barrier: Any, outcomes: Any) -> None:
    usage = UsageRepository(Path(path), enforce_limits=False)
    barrier.wait(timeout=15)
    try:
        usage.admit(OWNER, "model", run, 1, step, now=105)
        outcomes.put("admitted")
    except UsageLimitExceeded as exc:
        outcomes.put(exc.dimension)


def test_two_processes_cannot_spend_the_last_model_request_twice(tmp_path: Path) -> None:
    setup, usage, _ = running(tmp_path)
    usage.admit(OWNER, "model", setup.run_id(), 1, 1, now=setup.clock())
    context = multiprocessing.get_context("spawn")
    barrier, outcomes = context.Barrier(2), context.Queue()
    processes = [context.Process(target=_last_request, args=(str(setup.repository.db_path), setup.run_id(), step, barrier, outcomes)) for step in (2, 3)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    assert sorted(outcomes.get(timeout=2) for _ in processes) == ["admitted", "requests"]
    outcomes.close()
    outcomes.join_thread()


def test_original_late_charge_wakes_settled_projection_without_double_charging(tmp_path: Path) -> None:
    setup, usage, service = running(tmp_path)
    first = usage.admit(OWNER, "model", setup.run_id(), 1, 1, now=setup.clock())
    late = usage.admit(OWNER, "model", setup.run_id(), 1, 2, now=setup.clock())
    usage.settle(first, {"input_tokens": 1, "output_tokens": 1}, "succeeded", now=setup.clock())
    setup.platform.set_run_state(OWNER, setup.run_id(), "succeeded", now=setup.clock())
    setup.tick()
    assert reservation(setup)[0] == "unknown"
    usage.settle(late, {"input_tokens": 3, "output_tokens": 3}, "succeeded", now=setup.clock())
    asyncio.run(service.tick(limit=8))
    assert reservation(setup) == ("settled", 8, 2)
    asyncio.run(service.tick(limit=8))
    assert usage.get_run_usage(OWNER, setup.run_id())["total_tokens"] == 8


def test_missing_control_schema_member_fails_closed(tmp_path: Path) -> None:
    from hub.infrastructure import workflow_schema
    from hub.infrastructure.workflow_repository import WorkflowRepositoryError

    setup = make_setup(tmp_path)
    with setup.repository.transaction() as connection:
        connection.execute("ALTER TABLE workflow_stop_requests RENAME TO misplaced_stop_requests")
    with pytest.raises(WorkflowRepositoryError, match="incomplete_workflow_schema") as failure:
        with setup.repository.transaction(write=False) as connection:
            workflow_schema.inspect_schema(connection)
    assert isinstance(failure.value.__cause__, workflow_schema.WorkflowSchemaError)
