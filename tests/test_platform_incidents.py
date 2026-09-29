from pathlib import Path

import pytest

from hub.infrastructure.incident_repository import IncidentRepository, IncidentRepositoryError


def _data(*, fingerprint="fp-api", evidence_id="ev-1", failure_class="health_unhealthy", service_id="api"):
    return {
        "service_id": service_id,
        "failure_class": failure_class,
        "fingerprint": fingerprint,
        "evidence_id": evidence_id,
    }


@pytest.fixture
def repo(tmp_path: Path):
    current = [100.0]
    repository = IncidentRepository(tmp_path / "platform.db", clock=lambda: current[0])
    repository.init()
    repository._test_clock = current
    return repository


def test_repeated_fingerprint_is_one_open_incident_and_owner_scoped(repo):
    first = repo.open_or_update(
        "owner-a@example.test", _data(), observed_at=10, source="komari",
        state="unhealthy", detail={"status": "offline"},
    )
    second = repo.open_or_update(
        "owner-a@example.test", _data(evidence_id="ev-2"), observed_at=11,
        source="komari", state="unhealthy", detail={"status": "offline"},
    )
    assert first["incident_id"] == second["incident_id"]
    assert len(repo.list("owner-a@example.test", state="open")) == 1
    assert repo.get("owner-b@example.test", first["incident_id"]) is None
    other = repo.open_or_update(
        "owner-b@example.test", _data(evidence_id="ev-b"), observed_at=11,
        source="komari", state="unhealthy",
    )
    assert other["incident_id"] != first["incident_id"]


def test_older_evidence_only_adds_audit_id(repo):
    current = repo.open_or_update(
        "owner-a@example.test", _data(evidence_id="ev-new"), observed_at=20,
        source="local", state="unhealthy", detail={"code": "new"},
    )
    older = repo.open_or_update(
        "owner-a@example.test", _data(evidence_id="ev-old"), observed_at=10,
        source="komari", state="degraded", detail={"code": "old"},
    )
    assert older["incident_id"] == current["incident_id"]
    assert older["last_evidence_at"] == 20
    assert older["latest_state"] == "unhealthy"
    assert older["latest_source"] == "local"
    assert older["latest_detail"] == {"code": "new"}
    assert older["evidence_ids"] == ["ev-new", "ev-old"]


def test_two_consecutive_healthy_samples_close_and_failure_resets_streak(repo):
    repo.open_or_update(
        "owner-a@example.test", _data(), observed_at=10, source="komari", state="unhealthy",
    )
    first = repo.recover(
        "owner-a@example.test", fingerprint="fp-api", observed_at=11,
        evidence_id="ev-ok-1", source="komari", state="healthy",
    )
    assert first["state"] == "open"
    assert first["recovery_streak"] == 1
    second = repo.recover(
        "owner-a@example.test", fingerprint="fp-api", observed_at=12,
        evidence_id="ev-ok-2", source="komari", state="healthy",
    )
    assert second["state"] == "closed"
    assert second["closed_at"] == 100.0

    reopened = repo.open_or_update(
        "owner-a@example.test", _data(evidence_id="ev-fail-2"), observed_at=13,
        source="komari", state="unhealthy",
    )
    assert reopened["state"] == "open"
    assert reopened["recovery_streak"] == 0
    assert reopened["incident_id"] != second["incident_id"]


def test_non_healthy_recovery_sample_does_not_close(repo):
    repo.open_or_update(
        "owner-a@example.test", _data(), observed_at=10, source="komari", state="unhealthy",
    )
    result = repo.recover(
        "owner-a@example.test", fingerprint="fp-api", observed_at=11,
        evidence_id="ev-unknown", source="komari", state="unknown",
    )
    assert result["state"] == "open"
    assert result["recovery_streak"] == 0
    assert result["latest_state"] == "unknown"


def test_out_of_order_healthy_sample_cannot_close_incident(repo):
    repo.open_or_update(
        "owner-a@example.test", _data(), observed_at=20, source="local", state="unhealthy",
    )
    result = repo.recover(
        "owner-a@example.test", fingerprint="fp-api", observed_at=10,
        evidence_id="ev-old-ok", source="komari", state="healthy",
    )
    assert result["state"] == "open"
    assert result["recovery_streak"] == 0
    assert result["last_evidence_at"] == 20
    assert result["latest_state"] == "unhealthy"
    assert "ev-old-ok" in result["evidence_ids"]


def test_recovery_required_and_bounds_are_enforced(repo):
    row = repo.open_or_update(
        "owner-a@example.test", _data(), observed_at=1, source="local", state="unhealthy",
        recovery_required=99,
    )
    assert row["recovery_required"] == 10
    for index in range(10):
        row = repo.recover(
            "owner-a@example.test", fingerprint="fp-api", observed_at=index + 2,
            evidence_id=f"ev-ok-{index}", source="local", state="healthy",
            recovery_required=99,
        )
    assert row["state"] == "closed"

    with pytest.raises(IncidentRepositoryError) as invalid:
        repo.recover(
            "owner-a@example.test", fingerprint="fp-api", observed_at=100,
            evidence_id="ev-invalid", source="local", state="healthy",
            recovery_required="bad",
        )
    assert invalid.value.code == "invalid_recovery_required"


def test_evidence_ids_and_detail_are_bounded(repo):
    repo.open_or_update(
        "owner-a@example.test", _data(), observed_at=1, source="local", state="unhealthy",
    )
    for index in range(150):
        repo.open_or_update(
            "owner-a@example.test", _data(evidence_id=f"ev-{index}"),
            observed_at=index + 2, source="local", state="unhealthy",
        )
    row = repo.list("owner-a@example.test")[0]
    assert len(row["evidence_ids"]) == 100
    assert row["evidence_ids"][0] == "ev-50"
    with pytest.raises(IncidentRepositoryError) as oversized:
        repo.open_or_update(
            "owner-a@example.test", _data(evidence_id="ev-large"), observed_at=200,
            source="local", state="unhealthy", detail={"blob": "x" * (64 * 1024)},
        )
    assert oversized.value.code == "detail_too_large"
