from __future__ import annotations

import threading

import pytest

from hub.application.platform_monitoring_service import PlatformMonitoringService
from hub.bootstrap import create_app, start_background_jobs
from hub.config import FleetConfig
from hub.infrastructure.platform_scheduler_repository import (
    PlatformSchedulerRepository,
    PlatformSchedulerRepositoryError,
)
from hub.integrations.komari import KomariError


class _ServiceRepository:
    def __init__(self):
        self.services = {
            "owner-a@example.test": [{"service_id": "api", "node_id": "node-a"}],
            "owner-b@example.test": [],
        }

    def list_monitoring_owners(self):
        return ["owner-a@example.test", "owner-b@example.test"]

    def list_services(self, owner_id):
        return list(self.services.get(owner_id, []))


class _IncidentService:
    def __init__(self):
        self.calls = []

    def sync(self, owner_id, services, snapshots, *, source_error=None):
        self.calls.append((owner_id, services, snapshots, source_error))
        return {"ok": True, "results": []}


class _Komari:
    def __init__(self, error=None):
        self.error = error
        self.calls = 0

    def fetch_nodes(self, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return []


def test_monitoring_bounds_untrusted_exception_codes(tmp_path):
    class LeakyError(RuntimeError):
        code = "secret=opaque-token"

    incidents = _IncidentService()
    service = PlatformMonitoringService(
        _ServiceRepository(), _IncidentService(), _Komari(LeakyError()),
        _scheduler(tmp_path), interval_s=60, clock=lambda: 100,
    )
    result = service.sync_once(now=100)
    assert result["source_error"] == "source_unavailable"
    assert "opaque-token" not in str(result)


def _scheduler(tmp_path):
    repository = PlatformSchedulerRepository(tmp_path / "platform.db")
    repository.init()
    return repository


def test_scheduler_claims_are_durable_and_old_leases_cannot_finish(tmp_path):
    repository = _scheduler(tmp_path)

    first = repository.claim("komari_sync", now=100, lease_s=10)
    assert first["attempt"] == 1
    assert repository.claim("komari_sync", now=105, lease_s=10) is None

    second = repository.claim("komari_sync", now=111, lease_s=10)
    assert second["attempt"] == 2
    assert second["lease_id"] != first["lease_id"]
    with pytest.raises(PlatformSchedulerRepositoryError) as mismatch:
        repository.finish(
            "komari_sync", first["lease_id"], now=112, success=True,
            next_run_at=200,
        )
    assert mismatch.value.code == "lease_mismatch"

    status = repository.finish(
        "komari_sync", second["lease_id"], now=112, success=True,
        next_run_at=172, result={"owners": 1},
    )
    assert status["state"] == "idle"
    assert status["attempt"] == 2
    assert status["last_success_at"] == 112
    assert status["last_result"] == {"owners": 1}
    assert "lease_id" not in status
    assert "lease_expires_at" not in status


def test_expired_lease_cannot_finish_before_reclaim(tmp_path):
    repository = _scheduler(tmp_path)
    claim = repository.claim("komari_sync", now=100, lease_s=10)
    with pytest.raises(PlatformSchedulerRepositoryError) as expired:
        repository.finish(
            "komari_sync", claim["lease_id"], now=110, success=True,
            next_run_at=170,
        )
    assert expired.value.code == "lease_mismatch"


def test_scheduler_failure_count_and_bounded_result(tmp_path):
    repository = _scheduler(tmp_path)
    claim = repository.claim("komari_sync", now=100)
    status = repository.finish(
        "komari_sync", claim["lease_id"], now=101, success=False,
        next_run_at=220, error_code="source_unavailable",
        result={"source_error": "source_unavailable"},
    )
    assert status["consecutive_failures"] == 1
    assert status["last_error_code"] == "source_unavailable"
    assert status["last_success_at"] is None

    claim = repository.claim("komari_sync", now=220)
    with pytest.raises(PlatformSchedulerRepositoryError) as oversized:
        repository.finish(
            "komari_sync", claim["lease_id"], now=221, success=True,
            next_run_at=281, result={"blob": "x" * (16 * 1024)},
        )
    assert oversized.value.code == "result_too_large"


def test_monitoring_sync_writes_source_error_for_each_owner_and_skips_empty_owner(tmp_path):
    incidents = _IncidentService()
    komari = _Komari(KomariError("source_unavailable"))
    service = PlatformMonitoringService(
        _ServiceRepository(), incidents, komari, _scheduler(tmp_path),
        interval_s=60, clock=lambda: 100,
    )

    result = service.sync_once(now=100)

    assert result["ok"] is False
    assert result["source_error"] == "source_unavailable"
    assert komari.calls == 1
    assert len(incidents.calls) == 1
    assert incidents.calls[0][0] == "owner-a@example.test"
    assert incidents.calls[0][3] == "source_unavailable"
    assert result["status"]["consecutive_failures"] == 1
    assert result["status"]["next_run_at"] == 220


def test_monitoring_success_resets_failures_and_force_respects_active_lease(tmp_path):
    scheduler = _scheduler(tmp_path)
    incidents = _IncidentService()
    komari = _Komari(KomariError("source_unavailable"))
    service = PlatformMonitoringService(
        _ServiceRepository(), incidents, komari, scheduler,
        interval_s=60, clock=lambda: 100,
    )
    failed = service.sync_once(now=100)
    assert failed["status"]["consecutive_failures"] == 1

    # force bypasses next_run_at, but the lease still protects an active run.
    active = scheduler.claim("another_job", now=100, lease_s=60)
    assert scheduler.claim("another_job", now=101, lease_s=60, force=True) is None

    komari.error = None
    succeeded = service.sync_once(now=220, force=True)
    assert succeeded["ok"] is True
    assert succeeded["status"]["consecutive_failures"] == 0
    assert succeeded["status"]["last_error_code"] is None
    assert succeeded["status"]["next_run_at"] == 280

    scheduler.finish(
        "another_job", active["lease_id"], now=101, success=True,
        next_run_at=161,
    )


def test_scheduler_status_api_is_gate_scoped_and_does_not_leak_lease(tmp_path):
    disabled = create_app(FleetConfig.from_root(
        tmp_path / "disabled", dev_operator="owner-a@example.test",
        platform_enabled=True, service_monitoring_enabled=True,
        komari_enabled=True, komari_base_url="https://komari.example.test",
        komari_nodes_path="/api/nodes", komari_token="opaque-token",
        komari_sync_enabled=False,
    ))
    assert disabled.test_client().get("/api/platform/v1/komari/status").status_code == 404

    app = create_app(FleetConfig.from_root(
        tmp_path / "enabled", dev_operator="owner-a@example.test",
        platform_enabled=True, service_monitoring_enabled=True,
        komari_enabled=True, komari_base_url="https://komari.example.test",
        komari_nodes_path="/api/nodes", komari_token="opaque-token",
        komari_sync_enabled=True,
    ))
    response = app.test_client().get("/api/platform/v1/komari/status")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"]["job_id"] == "komari_sync"
    assert payload["status"]["state"] == "idle"
    assert "lease_id" not in response.get_data(as_text=True)
    assert "opaque-token" not in response.get_data(as_text=True)

    incomplete = create_app(FleetConfig.from_root(
        tmp_path / "incomplete", dev_operator="owner-a@example.test",
        platform_enabled=True, service_monitoring_enabled=True,
        komari_sync_enabled=True,
    ))
    assert "platform_monitoring" not in incomplete.extensions["fleet"]["services"]
    assert incomplete.config["KOMARI_SYNC_ENABLED"] is False
    assert incomplete.test_client().get("/api/platform/v1/komari/status").status_code == 404


def test_background_jobs_only_starts_platform_monitoring_when_gate_is_on(tmp_path, monkeypatch):
    disabled = create_app(FleetConfig.from_root(tmp_path / "disabled"))
    stopped = start_background_jobs(disabled)
    assert "platform_monitoring" not in stopped
    for event in stopped.values():
        event.set()

    app = create_app(FleetConfig.from_root(
        tmp_path / "enabled", dev_operator="owner-a@example.test",
        platform_enabled=True, service_monitoring_enabled=True,
        komari_enabled=True, komari_base_url="https://komari.example.test",
        komari_nodes_path="/api/nodes", komari_token="opaque-token",
        komari_sync_enabled=True,
    ))
    platform_monitoring = app.extensions["fleet"]["services"]["platform_monitoring"]
    platform_stop = threading.Event()
    monkeypatch.setattr(
        platform_monitoring, "start", lambda: platform_stop,
    )
    started = start_background_jobs(app)
    assert started["platform_monitoring"] is platform_stop
    for event in started.values():
        event.set()
