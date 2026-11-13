"""Real SQLite retention keeps Workflow recovery and accounting evidence."""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from hub.domain.task import DomainError
from hub.infrastructure.platform_retention import DAY_S, PlatformRetention, RetentionPolicy
from hub.infrastructure.task_repository import SqliteTaskRepository
from test_workflow_budget import reservation, running
from test_workflow_repository import OWNER

NOW = 1000 * DAY_S


@pytest.mark.parametrize("state", ["reserved", "unknown", "settled"])
def test_usage_retention_preserves_original_workflow_ledger_and_late_projection(
    tmp_path: Path, state: str,
) -> None:
    setup, usage, service = running(tmp_path)
    request = usage.admit(OWNER, "model", setup.run_id(), 1, 1, now=setup.clock())
    usage.settle(request, {"input_tokens": 4, "output_tokens": 6}, "succeeded", now=setup.clock())
    if state == "unknown":
        usage.admit(OWNER, "model", setup.run_id(), 1, 2, now=setup.clock())
    if state != "reserved":
        setup.platform.set_run_state(OWNER, setup.run_id(), "succeeded", now=setup.clock())
        setup.tick()
    asyncio.run(service.tick(limit=8))
    assert reservation(setup) == (state, 10, 1)
    ordinary = usage.admit(OWNER, "model", "ordinary-run", 1, 1, now=setup.clock())
    usage.settle(ordinary, {"input_tokens": 1, "output_tokens": 1}, "succeeded", now=setup.clock())

    deleted = PlatformRetention(setup.repository.db_path)(now=NOW)

    with setup.repository.transaction(write=False) as connection:
        rows = connection.execute("SELECT run_id,total_tokens FROM platform_usage_events").fetchall()
        assert [tuple(row) for row in rows] == [(setup.run_id(), 10)]
        assert connection.execute("SELECT count(*) FROM platform_usage_reservations").fetchone()[0] == (state == "unknown")
    assert deleted["platform_usage_events"] == 1
    if state == "settled":
        # A delayed settlement wakes the original budget projection. Even an
        # already settled generation still needs its original request ledger.
        with setup.repository.transaction() as connection:
            connection.execute("UPDATE workflow_budget_checks SET active=1")
    asyncio.run(service.tick(limit=8))
    assert reservation(setup) == (state, 10, 1)


def test_protected_usage_does_not_starve_bounded_ordinary_cleanup(tmp_path: Path) -> None:
    setup, usage, _ = running(tmp_path)
    request = usage.admit(OWNER, "model", setup.run_id(), 1, 1, now=setup.clock())
    usage.settle(request, {"input_tokens": 4, "output_tokens": 6}, "succeeded", now=setup.clock())
    for index in range(3):
        ordinary = usage.admit(OWNER, "model", f"ordinary-{index}", 1, 1, now=setup.clock())
        usage.settle(ordinary, {"input_tokens": 1, "output_tokens": 1}, "succeeded", now=setup.clock())
    retention = PlatformRetention(setup.repository.db_path, RetentionPolicy(batch_size=1, max_batches=2))

    assert retention(now=NOW)["platform_usage_events"] == 2
    assert retention(now=NOW)["platform_usage_events"] == 1
    assert "platform_usage_events" not in retention(now=NOW)
    with setup.repository.transaction(write=False) as connection:
        assert connection.execute("SELECT run_id FROM platform_usage_events").fetchone()[0] == setup.run_id()


def _task(repository: SqliteTaskRepository, machine: str, *, origin: bool = False) -> str:
    task, _ = repository.create_task(
        machine=machine, agent_type="codex", project="project", instruction="work",
        requested_by=OWNER, client_token=machine, now=100,
        **({"legacy_origin": {"owner_id": OWNER, "run_id": machine, "repository_id": None}} if origin else {}),
    )
    lease = repository.lease_task(machine=machine, runner_id="runner", now=100)
    assert lease is not None
    assert repository.complete_task(
        attempt_id=lease["attempt_id"], nonce=lease["nonce"], exit_code=0,
        log_summary="done", diff_stat="", duration_s=1, now=101,
    )["stored"]
    return task["task_id"]


def test_task_retention_keeps_workflow_history_and_collects_ordinary_tasks_in_bounded_batches(tmp_path: Path) -> None:
    path = tmp_path / "tasks.db"
    repository = SqliteTaskRepository(path)
    repository.init()
    protected = [_task(repository, f"workflow-{index}", origin=True) for index in range(3)]
    ordinary = [_task(repository, f"ordinary-{index}") for index in range(3)]
    policy = RetentionPolicy(batch_size=1, max_batches=2)

    deleted = PlatformRetention(None, policy, task_db=path)(now=NOW)

    assert deleted["tasks"] == 2
    assert all(repository.get_task(task) is not None for task in protected)
    assert sum(repository.get_task(task) is None for task in ordinary) == 2
    with sqlite3.connect(path) as connection:
        for task in protected:
            assert connection.execute("SELECT count(*) FROM leases WHERE task_id=?", (task,)).fetchone()[0] == 1
            assert connection.execute("SELECT count(*) FROM results WHERE task_id=?", (task,)).fetchone()[0] == 1
            assert connection.execute("SELECT count(*) FROM audit WHERE task_id=?", (task,)).fetchone()[0] > 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert PlatformRetention(None, policy, task_db=path)(now=NOW)["tasks"] == 1


def test_task_retention_preserves_source_receipt_and_submission_stop_proof(tmp_path: Path) -> None:
    path = tmp_path / "tasks.db"
    repository = SqliteTaskRepository(path)
    repository.init()
    source = _task(repository, "source")
    stopped = _task(repository, "stopped")
    repository.fence_submission("stopped", OWNER, now=102)
    repository.fence_submission("not-created", OWNER, now=102)
    ordinary = _task(repository, "ordinary")
    with sqlite3.connect(path) as connection:
        task = repository.get_task(source)
        connection.execute("INSERT INTO task_source_receipts VALUES(?,?,?,?,?,?)",
                           (source, task["attempt_id"], "artifact", "a" * 64, 1, "1970-01-01T00:01:41Z"))

    deleted = PlatformRetention(None, task_db=path)(now=NOW)

    assert deleted["tasks"] == 1
    assert repository.get_task(ordinary) is None
    assert repository.get_task(source) is not None
    assert repository.get_task(stopped) is not None
    proof = repository.submission_stop("stopped", OWNER)
    assert proof is not None and proof.task_id == stopped and proof.state == "succeeded" and proof.stopped
    with pytest.raises(DomainError, match="task_submission_cancelled"):
        repository.create_task(machine="late", agent_type="codex", project="project", instruction="late",
                               requested_by=OWNER, client_token="not-created", now=NOW)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM task_source_receipts").fetchone()[0] == 1
        for task in (source, stopped):
            assert connection.execute("SELECT count(*) FROM audit WHERE task_id=?", (task,)).fetchone()[0] > 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_workflow_task_exclusion_uses_reference_indexes(tmp_path: Path) -> None:
    path = tmp_path / "tasks.db"
    repository = SqliteTaskRepository(path)
    repository.init()
    with sqlite3.connect(path) as connection:
        plan = " | ".join(row[3] for row in connection.execute(
            "EXPLAIN QUERY PLAN SELECT t.task_id FROM tasks t WHERE t.state='succeeded' AND t.expires_at<? "
            "AND NOT EXISTS (SELECT 1 FROM legacy_task_origins o WHERE o.task_id=t.task_id) "
            "AND NOT EXISTS (SELECT 1 FROM task_source_receipts s WHERE s.task_id=t.task_id) "
            "AND NOT EXISTS (SELECT 1 FROM task_submission_cancellations c WHERE c.client_token=t.client_token) LIMIT ?",
            ("2000-01-01", 1),
        ))
    assert "SEARCH t USING" in plan
    assert "SCAN o" not in plan and "SCAN s" not in plan and "SCAN c" not in plan, plan
