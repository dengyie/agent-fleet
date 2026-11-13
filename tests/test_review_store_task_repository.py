"""Review regressions for the task repository: lease fencing, gate retry,
corruption quarantine and domain-derived state sets."""

import contextlib
import logging
import sqlite3
from pathlib import Path

import pytest

from hub.domain import task as task_domain
from hub.infrastructure import task_repository
from hub.infrastructure.task_repository import LEASE_TTL_S, SqliteTaskRepository

NOW = 1_800_000_000.0


@pytest.fixture
def repo(tmp_path: Path) -> SqliteTaskRepository:
    r = SqliteTaskRepository(tmp_path / "fleet.db")
    r.init()
    return r


def _create(repo, **kw):
    task, created = repo.create_task(
        machine="m1", agent_type="codex", project="p", instruction="do it",
        requested_by="owner", now=NOW, **kw)
    assert created
    return task


def test_result_after_ttl_but_before_sweep_is_accepted(repo):
    task = _create(repo)
    lease = repo.lease_task(machine="m1", runner_id="r1", now=NOW)
    late = NOW + LEASE_TTL_S + 30  # TTL 已过，expire_leases 尚未运行
    beat = repo.heartbeat(attempt_id=lease["attempt_id"], nonce=lease["nonce"], now=late)
    assert beat is not None and beat["task_id"] == task["task_id"]
    done = repo.complete_task(
        attempt_id=lease["attempt_id"], nonce=lease["nonce"], exit_code=0,
        log_summary="ok", diff_stat="", duration_s=1.0, now=late + LEASE_TTL_S + 30)
    assert done == {"task_id": task["task_id"], "state": "succeeded", "stored": True}


def test_result_after_sweep_and_release_is_rejected(repo):
    task = _create(repo)
    old = repo.lease_task(machine="m1", runner_id="r1", now=NOW)
    late = NOW + LEASE_TTL_S + 30
    assert repo.expire_leases(now=late) == [task["task_id"]]
    # sweep 之后、重派之前：任务已回 queued，旧 attempt 不能续租/交付
    assert repo.heartbeat(attempt_id=old["attempt_id"], nonce=old["nonce"], now=late) is None
    new = repo.lease_task(machine="m1", runner_id="r2", now=late + 1)
    assert new["attempt_id"] != old["attempt_id"]
    stale = repo.complete_task(
        attempt_id=old["attempt_id"], nonce=old["nonce"], exit_code=0,
        log_summary="stale", diff_stat="", duration_s=1.0, now=late + 2)
    assert stale is None
    assert repo.heartbeat(attempt_id=old["attempt_id"], nonce=old["nonce"], now=late + 2) is None
    assert repo.get_task(task["task_id"])["state"] == "leased"
    # 错误 nonce 依旧拒绝
    assert repo.complete_task(
        attempt_id=new["attempt_id"], nonce="wrong", exit_code=0,
        log_summary="", diff_stat="", duration_s=0, now=late + 3) is None


def test_retry_after_gate_reject_requires_fresh_confirmation(repo):
    task = _create(repo, confirm=True)
    assert repo.lease_task(machine="m1", runner_id="r1", now=NOW) is None
    _, rejected = repo.reject_task(task["task_id"], "owner", now=NOW + 1)
    assert rejected
    _, retried = repo.retry_task(task["task_id"], "owner", now=NOW + 2)
    assert retried
    gate = repo.get_task_gate(task["task_id"])
    assert gate["state"] == "pending" and gate["decided_by"] is None
    # 重试后仍需人工确认才可派发
    assert repo.lease_task(machine="m1", runner_id="r1", now=NOW + 3) is None
    _, confirmed = repo.confirm_task(task["task_id"], "owner", now=NOW + 4)
    assert confirmed
    lease = repo.lease_task(machine="m1", runner_id="r1", now=NOW + 5)
    assert lease is not None and lease["task_id"] == task["task_id"]


def test_retry_keeps_confirmed_gate_confirmed(repo):
    task = _create(repo, confirm=True)
    repo.confirm_task(task["task_id"], "owner", now=NOW + 1)
    repo.cancel_task(task["task_id"], "owner", now=NOW + 2)
    repo.retry_task(task["task_id"], "owner", now=NOW + 3)
    assert repo.get_task_gate(task["task_id"])["state"] == "confirmed"


def _corrupt_once(repo, monkeypatch):
    connect = repo._connect

    def fail():
        monkeypatch.setattr(repo, "_connect", connect)
        exc = sqlite3.DatabaseError("database disk image is malformed")
        exc.sqlite_errorcode = 11
        raise exc

    monkeypatch.setattr(repo, "_connect", fail)


def test_corrupt_database_quarantines_wal_and_shm_together(tmp_path, monkeypatch, caplog):
    db = tmp_path / "fleet.db"
    db.write_bytes(b"corrupt main")
    wal = tmp_path / "fleet.db-wal"
    shm = tmp_path / "fleet.db-shm"
    wal.write_bytes(b"stale wal frames")
    shm.write_bytes(b"stale shm")
    repo = SqliteTaskRepository(db)
    _corrupt_once(repo, monkeypatch)
    with caplog.at_level(logging.ERROR, logger=task_repository.__name__):
        repo.init()
    quarantined = sorted(p.name for p in tmp_path.glob("fleet.db*.corrupt-*"))
    assert len(quarantined) == 3
    suffix = quarantined[0].split(".corrupt-", 1)[1]
    assert quarantined == sorted([
        f"fleet.db.corrupt-{suffix}", f"fleet.db-wal.corrupt-{suffix}",
        f"fleet.db-shm.corrupt-{suffix}"])
    # 旧 WAL 帧随主库一起隔离，不会被回放进重建后的新库
    assert (tmp_path / f"fleet.db-wal.corrupt-{suffix}").read_bytes() == b"stale wal frames"
    assert (tmp_path / f"fleet.db.corrupt-{suffix}").read_bytes() == b"corrupt main"
    assert any(r.levelno == logging.ERROR and "quarantined" in r.getMessage()
               for r in caplog.records)
    assert repo.get_task("missing") is None
    # 同一秒内第二次损坏不覆盖第一次的证据
    repo2 = SqliteTaskRepository(db)
    _corrupt_once(repo2, monkeypatch)
    repo2.init()
    assert len(list(tmp_path.glob("fleet.db.corrupt-*"))) == 2


def test_repository_state_sets_derive_from_domain():
    assert task_repository.TASK_STATES is task_domain.TASK_STATES
    schema_states = task_repository.SCHEMA.split("state TEXT NOT NULL CHECK(state IN ", 1)[1]
    assert schema_states.startswith(task_repository._sql_in(task_domain.TASK_STATES))
    assert not hasattr(task_repository, "AGENT_TYPES")
    for state in task_domain.TASK_STATES:
        assert task_domain.can_retry(state) == (state in task_domain.TERMINAL_STATES)
        assert task_domain.holds_lease(state) == (state in ("leased", "running"))
