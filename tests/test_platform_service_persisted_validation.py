import json
import sqlite3
from pathlib import Path

import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.infrastructure.service_repository import ServiceRepositoryError


OWNER = "owner-a@example.test"


def _app(tmp_path: Path):
    return create_app(FleetConfig.from_root(
        tmp_path,
        dev_operator=OWNER,
        platform_enabled=True,
        service_monitoring_enabled=True,
    ))


def _seed_service(app):
    platform_repo = app.extensions["fleet"]["platform_repository"]
    platform_repo.upsert_node(OWNER, {"node_id": "node-a"})
    repository = app.extensions["fleet"]["services"]["service_health"].repository
    repository.upsert_service(OWNER, {
        "service_id": "api",
        "node_id": "node-a",
        "name": "API",
        "adapter": "http",
        "target_alias": "api-health",
        "checks": {},
    })
    return repository


@pytest.mark.parametrize(("column", "value"), [
    ("checks", "{"),
    ("checks", "[]"),
    ("checks", '{"timeout":NaN}'),
    ("allowed_actions", '["inspect",7]'),
])
def test_corrupt_persisted_service_json_is_a_bounded_service_store_error(tmp_path, column, value):
    app = _app(tmp_path)
    repository = _seed_service(app)
    marker = "persisted-service-secret"
    with sqlite3.connect(repository.db_path) as connection:
        if column == "checks":
            value = value.replace("timeout", marker)
        else:
            value = value.replace("inspect", marker)
        connection.execute(f"UPDATE service_definitions SET {column}=? WHERE owner_id=? AND service_id=?", (value, OWNER, "api"))

    response = app.test_client().get("/api/platform/v1/services")

    assert response.status_code == 503
    body = response.get_json()
    assert body["ok"] is False
    assert body["error"] == "service_store"
    assert marker not in json.dumps(body)


def test_corrupt_persisted_checks_retain_json_decode_cause(tmp_path):
    app = _app(tmp_path)
    repository = _seed_service(app)
    with sqlite3.connect(repository.db_path) as connection:
        connection.execute(
            "UPDATE service_definitions SET checks=? WHERE owner_id=? AND service_id=?",
            ("{", OWNER, "api"),
        )

    try:
        repository.get_service(OWNER, "api")
    except ServiceRepositoryError as exc:
        assert exc.code == "service_store"
        assert exc.__cause__ is not None
        assert type(exc.__cause__).__name__ == "JSONDecodeError"
    else:
        raise AssertionError("corrupt checks were accepted")


def test_corrupt_persisted_allowed_actions_are_not_filtered_into_success(tmp_path):
    app = _app(tmp_path)
    repository = _seed_service(app)
    with sqlite3.connect(repository.db_path) as connection:
        connection.execute(
            "UPDATE service_definitions SET allowed_actions=? WHERE owner_id=? AND service_id=?",
            (json.dumps(["inspect", 7]), OWNER, "api"),
        )

    response = app.test_client().get("/api/platform/v1/services")

    assert response.status_code == 503
    assert response.get_json()["error"] == "service_store"


@pytest.mark.parametrize("non_finite", [float("nan"), float("inf"), float("-inf")])
def test_service_definition_rejects_non_finite_checks_before_persistence(tmp_path, non_finite):
    app = _app(tmp_path)
    repository = _seed_service(app)

    with pytest.raises(ServiceRepositoryError) as error:
        repository.upsert_service(OWNER, {
            "service_id": "api", "node_id": "node-a",
            "name": "API", "adapter": "http",
            "target_alias": "api-health", "checks": {"value": non_finite},
        })

    assert error.value.code == "invalid_evidence_detail"
    assert repository.get_service(OWNER, "api")["checks"] == {}


@pytest.mark.parametrize("non_finite", [float("nan"), float("inf"), float("-inf")])
def test_health_evidence_rejects_non_finite_detail_before_persistence(tmp_path, non_finite):
    app = _app(tmp_path)
    repository = _seed_service(app)

    with pytest.raises(ServiceRepositoryError) as error:
        repository.record_evidence(OWNER, {
            "service_id": "api", "dimension": "application_health",
            "source": "local", "state": "healthy",
            "detail": {"value": non_finite},
        })

    assert error.value.code == "invalid_evidence_detail"
    assert repository.list_evidence(OWNER, "api") == []
