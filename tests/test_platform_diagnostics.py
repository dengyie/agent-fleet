from pathlib import Path

import pytest

from hub.application.diagnostic_service import DiagnosticService
from hub.application.task_service import ApplicationError
from hub.infrastructure.incident_repository import IncidentRepository
from hub.infrastructure.platform_db import PlatformRepository
from hub.infrastructure.service_repository import ServiceRepository
from hub.application.service_health_service import ServiceHealthService
from tools.platform.backends.directory import DirectoryBackend
from tools.platform.resource_lease import ResourceLeaseManager
from tools.platform.tool_broker import ToolBroker


def _services(tmp_path: Path):
    db = tmp_path / "platform.db"
    platform = PlatformRepository(db)
    platform.init()
    platform.upsert_node("owner-a@example.test", {"node_id": "node-a"})
    repository = ServiceRepository(db, clock=lambda: 100.0)
    repository.init()
    health = ServiceHealthService(repository, clock=lambda: 100.0)
    health.register("owner-a@example.test", {
        "service_id": "api", "node_id": "node-a", "adapter": "http",
        "target_alias": "api-health", "name": "API",
    })
    health.ingest("owner-a@example.test", {
        "service_id": "api", "dimension": "host_reachability",
        "source": "komari", "state": "unhealthy", "observed_at": 99,
        "ttl_s": 300, "evidence_id": "ev-api-1",
        "detail": {"status": "offline"},
    })
    incidents = IncidentRepository(db, clock=lambda: 100.0)
    incidents.init()
    incident = incidents.open_or_update(
        "owner-a@example.test", {
            "service_id": "api", "failure_class": "health_unhealthy",
            "fingerprint": "fp-api", "evidence_id": "ev-api-1",
        }, observed_at=99, source="komari", state="unhealthy",
        detail={"status": "offline"},
    )
    return health, incidents, repository, incident


def test_diagnostics_are_bounded_owner_scoped_and_redacted(tmp_path):
    health, incidents, _, incident = _services(tmp_path)
    diagnostics = DiagnosticService(
        health, incidents,
        log_reader=lambda service, **kwargs: {
            "text": "password=correct horse battery staple token=secret-value /Users/mango/private.txt",
            "truncated": False, "observed_at": 99,
        },
    )
    listed = diagnostics.list_services("owner-a@example.test")
    assert listed["services"][0]["service"]["service_id"] == "api"
    assert "owner_id" not in listed["services"][0]["service"]
    evidence = diagnostics.get_incident_evidence("owner-a@example.test", incident["incident_id"])
    assert evidence["evidence"][0]["evidence_id"] == "ev-api-1"
    assert "owner_id" not in evidence["incident"]
    logs = diagnostics.read_logs("owner-a@example.test", "api", window_s=60, max_bytes=1024)
    assert "correct horse" not in logs["logs"]["text"]
    assert "secret-value" not in logs["logs"]["text"]
    assert "Users/mango" not in logs["logs"]["text"]
    with pytest.raises(ApplicationError) as too_wide:
        diagnostics.read_logs("owner-a@example.test", "api", window_s=999999)
    assert too_wide.value.code == "invalid_window_s"
    with pytest.raises(ApplicationError) as wrong_owner:
        diagnostics.get_health("owner-b@example.test", "api")
    assert wrong_owner.value.code == "service_not_found"


def test_diagnostic_tools_stay_read_only_and_require_lease(tmp_path):
    health, incidents, _, incident = _services(tmp_path)
    diagnostics = DiagnosticService(health, incidents, log_reader=lambda service, **kwargs: "safe")
    leases = ResourceLeaseManager()
    lease = leases.acquire("workspace-1", "owner-a@example.test")
    broker = ToolBroker(DirectoryBackend(tmp_path / "workspace"), leases, resource_id="workspace-1", diagnostics=diagnostics)
    result = broker.execute(
        command_id="diag-1", tool="service.get_health",
        arguments={"service_id": "api"}, owner_id="owner-a@example.test", epoch=lease["epoch"],
    )
    assert result.state == "succeeded"
    assert result.result["service"]["service_id"] == "api"
    assert broker.execute(
        command_id="diag-2", tool="service.restart",
        arguments={"service_id": "api"}, owner_id="owner-a@example.test", epoch=lease["epoch"],
    ).error_code == "unknown_tool"
    assert broker.execute(
        command_id="diag-3", tool="incident.get_evidence",
        arguments={"incident_id": incident["incident_id"]}, owner_id="other", epoch=lease["epoch"],
    ).error_code == "lease_mismatch"


def test_missing_log_reader_fails_closed(tmp_path):
    health, incidents, _, _ = _services(tmp_path)
    diagnostics = DiagnosticService(health, incidents)
    with pytest.raises(ApplicationError) as exc:
        diagnostics.read_logs("owner-a@example.test", "api")
    assert exc.value.code == "diagnostics_unavailable"


def test_diagnostic_tool_rejects_non_object_arguments(tmp_path):
    health, incidents, _, _ = _services(tmp_path)
    diagnostics = DiagnosticService(health, incidents, log_reader=lambda service, **kwargs: "safe")
    leases = ResourceLeaseManager()
    lease = leases.acquire("workspace-1", "owner-a@example.test")
    broker = ToolBroker(
        DirectoryBackend(tmp_path / "workspace"), leases,
        resource_id="workspace-1", diagnostics=diagnostics,
    )
    result = broker.execute(
        command_id="diag-invalid-args", tool="service.get_health",
        arguments=None, owner_id="owner-a@example.test", epoch=lease["epoch"],
    )
    assert result.state == "failed"
    assert result.error_code == "invalid_arguments"
