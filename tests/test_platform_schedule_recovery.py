"""Schedule service recovery must respect real, transaction-fenced leases."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import threading
from threading import Barrier
from typing import Any

import pytest

from hub.application.platform_schedule_service import DurableReadOnlyScheduleService
from hub.infrastructure.platform_schedule_repository import (
    PlatformScheduleRepository,
    PlatformScheduleRepositoryError,
)

OWNER = "owner@example.test"


def repository(tmp_path: Path, **overrides: Any) -> PlatformScheduleRepository:
    repo = PlatformScheduleRepository(tmp_path / "platform.db")
    repo.init()
    repo.create(OWNER, {
        "schedule_id": "health", "action": "service_health",
        "target": {"service_id": "api"}, "interval_s": 10,
        "next_run_at": 100, "missed_policy": "catch_up", **overrides,
    }, now=90)
    return repo


@pytest.mark.parametrize("missed_policy", ["skip", "catch_up"])
@pytest.mark.parametrize("recovery_time", [105, 106])
def test_expired_trigger_is_reclaimed_by_service(tmp_path: Path, missed_policy: str, recovery_time: int) -> None:
    repo = repository(tmp_path, missed_policy=missed_policy)
    original = repo.claim_trigger(OWNER, "health", scheduled_at=100, worker_id="crashed", now=100, lease_s=5)
    calls: list[Any] = []
    restarted = PlatformScheduleRepository(repo.db_path)
    service = DurableReadOnlyScheduleService(
        restarted, executor=lambda *args: calls.append(args) or {"state": "healthy"},
        worker_id="replacement", clock=lambda: recovery_time,
    )
    assert service.tick_once(owner_id=OWNER)["executed"] == 1
    assert len(calls) == 1
    trigger = restarted.get_trigger(OWNER, "health", 100)
    assert trigger["state"] == "succeeded"
    assert trigger["attempt"] == 2
    assert trigger["finished_at"] == recovery_time
    assert restarted.get(OWNER, "health")["next_run_at"] == 110
    with pytest.raises(PlatformScheduleRepositoryError, match="lease_mismatch"):
        repo.finish_trigger(OWNER, "health", 100, original["lease_id"],
                            state="failed", next_run_at=999, now=recovery_time)
    assert restarted.get_trigger(OWNER, "health", 100) == trigger


@pytest.mark.parametrize("overlap_policy", ["skip", "coalesce"])
def test_live_duplicate_tick_preserves_occurrence_for_crash_recovery(tmp_path: Path, overlap_policy: str) -> None:
    repo = repository(tmp_path, overlap_policy=overlap_policy)
    repo.claim_trigger(OWNER, "health", scheduled_at=100, worker_id="first", now=100, lease_s=5)
    calls: list[Any] = []
    now = [101]
    service = DurableReadOnlyScheduleService(
        repo, executor=lambda *args: calls.append(args) or {"state": "healthy"}, clock=lambda: now[0],
    )
    assert service.tick_once(owner_id=OWNER)["executed"] == 0
    assert calls == []
    assert repo.get(OWNER, "health")["next_run_at"] == 100
    now[0] = 106
    assert service.tick_once(owner_id=OWNER)["executed"] == 1
    assert repo.get_trigger(OWNER, "health", 100)["attempt"] == 2


def test_advanced_schedule_retains_recoverable_trigger(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    repo.claim_trigger(OWNER, "health", scheduled_at=100, advance_to=110,
                       worker_id="crashed", now=100, lease_s=5)
    assert repo.get(OWNER, "health")["next_run_at"] == 110
    calls: list[Any] = []
    service = DurableReadOnlyScheduleService(
        PlatformScheduleRepository(repo.db_path),
        executor=lambda *args: calls.append(args) or {"state": "healthy"}, clock=lambda: 106,
    )
    assert service.tick_once(owner_id=OWNER)["executed"] == 1
    assert repo.get_trigger(OWNER, "health", 100)["attempt"] == 2
    assert repo.get(OWNER, "health")["next_run_at"] == 110
    assert len(calls) == 1


def test_due_schedule_is_not_starved_by_first_page_of_inactive_schedules(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    for index in range(100):
        repo.create(OWNER, {
            "schedule_id": f"ahead-{index:03}", "action": "service_health",
            "target": {"service_id": "api"}, "interval_s": 10,
            "next_run_at": 1000, "enabled": index % 2 == 0,
        }, now=90)
    calls: list[Any] = []
    service = DurableReadOnlyScheduleService(
        repo, executor=lambda *args: calls.append(args) or {"state": "healthy"}, clock=lambda: 100,
    )
    assert service.tick_once(owner_id=OWNER)["executed"] == 1
    assert len(calls) == 1
    assert repo.get_trigger(OWNER, "health", 100)["state"] == "succeeded"


@pytest.mark.parametrize("failure", [False, True])
def test_expired_executor_cannot_finish_with_start_time(tmp_path: Path, failure: bool) -> None:
    repo = repository(tmp_path)
    now = [100]

    def executor(*_args: Any) -> dict[str, str]:
        now[0] = 106
        if failure:
            raise RuntimeError("private executor failure")
        return {"state": "healthy"}

    service = DurableReadOnlyScheduleService(repo, executor=executor, clock=lambda: now[0], lease_s=5)
    outcome = service.tick_once(owner_id=OWNER)
    assert outcome["executed"] == outcome["failed"] == 0
    assert outcome["results"][0]["error_code"] == "lease_mismatch"
    trigger = repo.get_trigger(OWNER, "health", 100)
    assert trigger["state"] == "running"
    assert trigger["finished_at"] is None
    assert repo.get(OWNER, "health")["last_run_at"] is None
    replacement = DurableReadOnlyScheduleService(repo, executor=lambda *_args: {"state": "healthy"}, clock=lambda: 107)
    assert replacement.tick_once(owner_id=OWNER)["executed"] == 1
    assert repo.get_trigger(OWNER, "health", 100)["attempt"] == 2


def test_success_records_completion_time_and_next_future_interval(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    now = [100]

    def executor(*_args: Any) -> dict[str, str]:
        now[0] = 112
        return {"state": "healthy"}

    service = DurableReadOnlyScheduleService(repo, executor=executor, clock=lambda: now[0], lease_s=60)
    assert service.tick_once(owner_id=OWNER)["executed"] == 1
    assert repo.get_trigger(OWNER, "health", 100)["finished_at"] == 112
    assert repo.get(OWNER, "health")["last_success_at"] == 112
    assert repo.get(OWNER, "health")["next_run_at"] == 120


def test_reclaimed_worker_result_survives_original_late_return(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    now = [100]
    replacement = DurableReadOnlyScheduleService(
        PlatformScheduleRepository(repo.db_path),
        executor=lambda *_args: {"state": "healthy", "detail": {"worker": "replacement"}},
        clock=lambda: now[0], lease_s=5,
    )

    def executor(*_args: Any) -> dict[str, str]:
        now[0] = 106
        assert replacement.tick_once(owner_id=OWNER)["executed"] == 1
        return {"state": "unhealthy"}

    original = DurableReadOnlyScheduleService(repo, executor=executor, clock=lambda: now[0], lease_s=5)
    outcome = original.tick_once(owner_id=OWNER)
    assert outcome["executed"] == 0
    assert outcome["results"][0]["error_code"] == "lease_mismatch"
    trigger = repo.get_trigger(OWNER, "health", 100)
    assert trigger["state"] == "succeeded"
    assert trigger["last_result"]["detail"]["worker"] == "replacement"
    assert trigger["attempt"] == 2


def test_background_scheduler_eventually_services_owners_after_first_page(tmp_path: Path) -> None:
    repo = PlatformScheduleRepository(tmp_path / "platform.db")
    repo.init()
    final_owner = "owner-100@example.test"
    reached_final_owner = threading.Event()

    for index in range(101):
        owner_id = f"owner-{index:03}@example.test"
        repo.create(owner_id, {
            "schedule_id": "health", "action": "service_health",
            "target": {"service_id": "api"}, "interval_s": 10,
            "next_run_at": 100, "missed_policy": "catch_up",
        }, now=90)

    def execute(owner_id: str, _action: str, _target: Any) -> dict[str, str]:
        if owner_id == final_owner:
            reached_final_owner.set()
        return {"state": "healthy"}

    service = DurableReadOnlyScheduleService(
        repo, executor=execute, clock=lambda: 100, poll_interval_s=1,
    )
    existing_threads = set(threading.enumerate())
    stop = service.start()
    scheduler_thread = next(
        thread for thread in threading.enumerate()
        if thread not in existing_threads and thread.name == "platform-schedules"
    )
    try:
        assert reached_final_owner.wait(timeout=5), "owner past the first 100 remains unscheduled"
    finally:
        stop()
    assert not scheduler_thread.is_alive()


def test_background_scheduler_logs_failure_and_continues_to_other_owners(
    tmp_path: Path, caplog: Any,
) -> None:
    repo = PlatformScheduleRepository(tmp_path / "platform.db")
    repo.init()
    for owner_id in ("a@example.test", "b@example.test"):
        repo.create(owner_id, {
            "schedule_id": "health", "action": "service_health",
            "target": {"service_id": "api"}, "interval_s": 10,
            "next_run_at": 100, "missed_policy": "catch_up",
        }, now=90)
    serviced: list[str] = []
    serviced_second_owner = threading.Event()
    service = DurableReadOnlyScheduleService(repo, clock=lambda: 100, poll_interval_s=1)

    def tick(*, owner_id: str, **_kwargs: Any) -> dict[str, Any]:
        if owner_id == "a@example.test":
            raise RuntimeError("private schedule storage marker")
        serviced.append(owner_id)
        serviced_second_owner.set()
        return {"executed": 0}

    service.tick_once = tick
    stop = service.start()
    try:
        assert serviced_second_owner.wait(timeout=3)
    finally:
        stop()

    assert not stop.thread.is_alive()
    assert "platform_schedule_owner_tick_failed" in caplog.text
    assert "private schedule storage marker" not in caplog.text


def test_concurrent_recovery_claims_execute_once(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    repo.claim_trigger(OWNER, "health", scheduled_at=100, worker_id="crashed", now=100, lease_s=5)
    barrier = Barrier(2)
    calls: list[Any] = []

    class CompetingRepository(PlatformScheduleRepository):
        def claim_trigger(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
            barrier.wait(timeout=5)
            return super().claim_trigger(*args, **kwargs)

    def recover() -> dict[str, Any]:
        service = DurableReadOnlyScheduleService(
            CompetingRepository(repo.db_path),
            executor=lambda *args: calls.append(args) or {"state": "healthy"}, clock=lambda: 106,
        )
        return service.tick_once(owner_id=OWNER)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(recover), pool.submit(recover)
        outcomes = [first.result(timeout=10), second.result(timeout=10)]
    assert sum(outcome["executed"] for outcome in outcomes) == 1
    assert len(calls) == 1
    assert repo.get_trigger(OWNER, "health", 100)["attempt"] == 2
