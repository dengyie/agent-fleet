from __future__ import annotations

import sqlite3

import pytest

from hub.application.platform_schedule_service import (
    DurableReadOnlyScheduleService,
    PlatformScheduleError,
)
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.infrastructure.platform_db import PlatformRepository
from hub.infrastructure.platform_schedule_repository import (
    PlatformScheduleRepository,
    PlatformScheduleRepositoryError,
)


OWNER_A = "owner-a@example.test"
OWNER_B = "owner-b@example.test"


def _repo(tmp_path):
    db = tmp_path / "platform.db"
    platform = PlatformRepository(db)
    platform.init()
    schedules = PlatformScheduleRepository(db)
    schedules.init()
    return schedules


def _schedule(repo, owner=OWNER_A, **overrides):
    data = {
        "schedule_id": "health",
        "name": "Health check",
        "action": "service_health",
        "target": {"service_id": "api"},
        "interval_s": 10,
        "timezone": "UTC",
        "missed_policy": "catch_up",
        "overlap_policy": "skip",
        "next_run_at": 100,
        "enabled": True,
    }
    data.update(overrides)
    return repo.create(owner, data, now=90)


def test_schedule_repository_is_owner_scoped_and_publicly_bounded(tmp_path):
    repo = _repo(tmp_path)
    first = _schedule(repo, OWNER_A)
    second = _schedule(repo, OWNER_B)
    assert first["schedule_id"] == second["schedule_id"] == "health"
    assert repo.get(OWNER_A, "health")["target"] == {"service_id": "api"}
    assert repo.get(OWNER_B, "health")["target"] == {"service_id": "api"}
    assert repo.get(OWNER_A, "missing") is None
    public = {key: value for key, value in first.items() if key in {
        "schedule_id", "name", "action", "target", "interval_s", "timezone",
        "missed_policy", "overlap_policy", "next_run_at", "enabled",
        "revision", "consecutive_failures",
    }}
    assert "owner_id" not in public
    assert "lease_id" not in public
    assert "lease_expires_at" not in public


def test_schedule_repository_enforces_fixed_contract_and_trigger_idempotency(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(PlatformScheduleError):
        _schedule(repo, action="restart")
    with pytest.raises(PlatformScheduleError):
        _schedule(repo, interval_s=4)
    with pytest.raises(PlatformScheduleError):
        _schedule(repo, timezone="not/a-timezone")
    _schedule(repo)
    claim = repo.claim_trigger(
        OWNER_A, "health", scheduled_at=100, advance_to=110,
        worker_id="worker-a", now=100, lease_s=5,
    )
    assert claim["scheduled_at"] == 100
    assert claim["lease_id"]
    assert repo.claim_trigger(
        OWNER_A, "health", scheduled_at=100, advance_to=110,
        worker_id="worker-b", now=101, lease_s=5,
    ) is None
    assert repo.claim_trigger(
        OWNER_A, "health", scheduled_at=100, advance_to=110,
        worker_id="worker-b", now=106, lease_s=5,
    )["attempt"] == 2
    public = repo.status(OWNER_A, "health")
    assert public["triggers"][0]["scheduled_at"] == 100
    assert "lease_id" not in public["triggers"][0]
    assert "secret" not in str(public)


def test_schedule_owner_listing_uses_an_exclusive_bounded_cursor(tmp_path):
    repo = _repo(tmp_path)
    for index in range(205):
        owner_id = f"owner-{index:03}@example.test"
        _schedule(repo, owner=owner_id, schedule_id="health")

    first = repo.list_owners(limit=100)
    second = repo.list_owners(after_owner_id=first[-1], limit=100)
    third = repo.list_owners(after_owner_id=second[-1], limit=100)

    assert len(first) == len(second) == 100
    assert len(third) == 5
    assert first[-1] < second[0] and second[-1] < third[0]
    assert len(set(first + second + third)) == 205


def test_schedule_service_skips_old_occurrences_or_catches_up_once(tmp_path):
    repo = _repo(tmp_path)
    calls = []

    def executor(owner_id, action, target):
        calls.append((owner_id, action, target))
        return {"state": "healthy", "service_id": target["service_id"], "secret": "drop"}

    service = DurableReadOnlyScheduleService(
        repo, executor=executor, worker_id="worker-a", clock=lambda: 150,
    )
    _schedule(repo, missed_policy="catch_up")
    result = service.tick_once(owner_id=OWNER_A, now=150)
    assert result["executed"] == 1
    assert len(calls) == 1
    assert repo.get(OWNER_A, "health")["next_run_at"] == 160
    assert "secret" not in str(result)
    assert service.tick_once(owner_id=OWNER_A, now=150)["executed"] == 0

    _schedule(repo, owner=OWNER_B, missed_policy="skip")
    skipped = service.tick_once(owner_id=OWNER_B, now=150)
    assert skipped["skipped"] == 1
    assert len(calls) == 1
    assert repo.status(OWNER_B, "health")["triggers"][0]["state"] == "skipped"


def test_schedule_service_overlap_coalesces_without_duplicate_executor_call(tmp_path):
    repo = _repo(tmp_path)
    calls = []
    service = DurableReadOnlyScheduleService(
        repo,
        executor=lambda owner, action, target: calls.append(target) or {"state": "healthy"},
        worker_id="worker-a",
        clock=lambda: 110,
    )
    _schedule(repo, next_run_at=100, interval_s=10, overlap_policy="coalesce")
    claim = repo.claim_trigger(
        OWNER_A, "health", scheduled_at=100, advance_to=110,
        worker_id="blocking-worker", now=100, lease_s=100,
    )
    assert claim
    result = service.tick_once(owner_id=OWNER_A, now=110)
    assert result["executed"] == 0
    assert result["coalesced"] == 1
    assert calls == []
    assert repo.get(OWNER_A, "health")["next_run_at"] == 120


def test_schedule_service_detects_another_active_occurrence(tmp_path):
    repo = _repo(tmp_path)
    calls = []
    service = DurableReadOnlyScheduleService(
        repo,
        executor=lambda owner, action, target: calls.append(target) or {"state": "healthy"},
        worker_id="worker-a",
        clock=lambda: 110,
    )
    _schedule(repo, next_run_at=100, interval_s=10, overlap_policy="skip")
    first = repo.claim_trigger(
        OWNER_A, "health", scheduled_at=100, worker_id="blocking-worker",
        now=100, lease_s=100,
    )
    assert first
    repo.update(OWNER_A, "health", {"next_run_at": 110}, expected_revision=0, now=100)
    result = service.tick_once(owner_id=OWNER_A, now=110)
    assert result["executed"] == 0
    assert result["skipped"] == 1
    assert calls == []
    assert repo.get_trigger(OWNER_A, "health", 110)["state"] == "skipped"


def test_schedule_service_failure_is_bounded_and_backed_off(tmp_path):
    repo = _repo(tmp_path)

    def executor(*_args):
        raise PlatformScheduleError("executor_failed")

    service = DurableReadOnlyScheduleService(
        repo, executor=executor, worker_id="worker-a", clock=lambda: 100,
    )
    _schedule(repo, next_run_at=100, interval_s=10)
    result = service.tick_once(owner_id=OWNER_A, now=100)
    assert result["failed"] == 1
    status = repo.status(OWNER_A, "health")
    assert status["last_error_code"] == "executor_failed"
    assert status["next_run_at"] == 120
    assert len(str(status["last_result"])) < 8192


def test_schedule_api_is_default_off_and_owner_scoped(tmp_path):
    disabled = create_app(FleetConfig.from_root(tmp_path, dev_operator=OWNER_A))
    assert "platform_schedules" not in disabled.extensions["fleet"]["services"]
    assert disabled.test_client().get("/api/platform/v1/schedules").status_code == 404

    app = create_app(FleetConfig.from_root(
        tmp_path / "enabled", dev_operator=OWNER_A,
        platform_enabled=True, platform_schedules_enabled=True,
    ))
    client = app.test_client()
    created = client.post("/api/platform/v1/schedules", json={
        "schedule_id": "health",
        "name": "Health check",
        "action": "service_health",
        "target": {"service_id": "api"},
        "interval_s": 10,
        "timezone": "UTC",
    })
    assert created.status_code == 201
    assert created.get_json()["schedule"]["schedule_id"] == "health"
    assert client.get("/api/platform/v1/schedules").get_json()["schedules"][0]["schedule_id"] == "health"
    assert "lease_id" not in created.get_data(as_text=True)
    app.config["DEV_OPERATOR"] = OWNER_B
    assert client.get("/api/platform/v1/schedules").get_json()["schedules"] == []


def test_schedule_api_rejects_unbounded_or_mutating_inputs(tmp_path):
    app = create_app(FleetConfig.from_root(
        tmp_path, dev_operator=OWNER_A, platform_enabled=True,
        platform_schedules_enabled=True,
    ))
    response = app.test_client().post("/api/platform/v1/schedules", json={
        "schedule_id": "unsafe",
        "action": "exec",
        "target": {"command": "rm -rf /"},
        "interval_s": 1,
    })
    assert response.status_code == 400
    assert response.get_json()["error"] in {"invalid_action", "invalid_target", "invalid_interval"}


@pytest.mark.parametrize(("column", "stored_json"), [
    ("target", '{"service_id":"api","marker":"persisted-schedule-secret"'),
    ("target", "[]"),
    ("last_result", '{"marker":"persisted-schedule-secret"'),
    ("last_result", "[]"),
])
def test_corrupt_persisted_schedule_json_is_bounded_on_read(tmp_path, column, stored_json):
    app = create_app(FleetConfig.from_root(
        tmp_path, dev_operator=OWNER_A, platform_enabled=True,
        platform_schedules_enabled=True,
    ))
    service = app.extensions["fleet"]["services"]["platform_schedules"]
    service.create(OWNER_A, {
        "schedule_id": "health", "name": "Health check",
        "action": "service_health", "target": {"service_id": "api"},
        "interval_s": 10, "timezone": "UTC",
    })
    marker = "persisted-schedule-secret"
    with sqlite3.connect(service.repository.db_path) as connection:
        connection.execute(
            f"UPDATE platform_schedules SET {column}=? WHERE owner_id=? AND schedule_id=?",
            (stored_json, OWNER_A, "health"),
        )

    response = app.test_client().get("/api/platform/v1/schedules/health")
    assert response.status_code == 503
    body = response.get_json()
    assert body["ok"] is False
    assert body["error"] == "schedule_store"
    assert marker not in str(body)

    with pytest.raises(PlatformScheduleError) as error:
        service.repository.get(OWNER_A, "health")
    assert error.value.code == "schedule_store"
    assert error.value.__cause__ is not None


@pytest.mark.parametrize("non_finite", [float("nan"), float("inf"), float("-inf")])
def test_schedule_rejects_non_finite_result_before_mutation(tmp_path, non_finite):
    repo = _repo(tmp_path)
    before = _schedule(repo)

    with pytest.raises(PlatformScheduleRepositoryError) as error:
        repo.advance_schedule(
            OWNER_A, "health", next_run_at=120, now=100,
            result={"value": non_finite},
        )

    assert error.value.code == "invalid_result"
    after = repo.get(OWNER_A, "health")
    assert after["last_result"] == {}
    assert after["next_run_at"] == before["next_run_at"]
