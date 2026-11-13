"""Malformed JSON types must not bypass the schedule and memory contracts."""
from pathlib import Path
from typing import Any

import pytest
from flask.testing import FlaskClient

from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.application.platform_memory_service import PlatformMemoryService
from hub.application.task_service import ApplicationError
from hub.infrastructure.platform_memory_repository import PlatformMemoryRepository


@pytest.fixture
def client(tmp_path: Path) -> FlaskClient:
    app = create_app(FleetConfig.from_root(
        tmp_path, dev_operator="owner@example.test", platform_enabled=True,
        platform_memory_enabled=True, platform_schedules_enabled=True,
    ))
    return app.test_client()


def payload(kind: str) -> dict[str, Any]:
    if kind == "schedules":
        return {"schedule_id": "item", "action": "service_health",
                "target": {"service_id": "api"}, "interval_s": 10}
    return {"memory_id": "item", "kind": "fact", "title": "Title", "content": "Content"}


@pytest.mark.parametrize(("kind", "field", "value", "code"), [
    ("schedules", "missed_policy", ["skip"], "invalid_missed_policy"),
    ("schedules", "overlap_policy", {"policy": "skip"}, "invalid_overlap_policy"),
    ("schedules", "name", {"name": "Name"}, "invalid_name"),
    ("schedules", "name", "\ud800", "invalid_name"),
    ("schedules", "interval_s", "10", "invalid_interval"),
    ("schedules", "next_run_at", "100", "invalid_next_run"),
    ("schedules", "next_run_at", "tomorrow", "invalid_next_run"),
    ("schedules", "next_run_at", 10**1000, "invalid_next_run"),
    ("schedules", "interval_s", 10**1000, "invalid_interval"),
    ("schedules", "missed_policy", [], "invalid_missed_policy"),
    ("schedules", "overlap_policy", {}, "invalid_overlap_policy"),
    ("schedules", "timezone", [], "invalid_timezone"),
    ("schedules", "name", [], "invalid_name"),
    ("schedules", "next_run_at", True, "invalid_next_run"),
    ("memory", "kind", ["fact"], "invalid_kind"),
    ("memory", "memory_id", [], "invalid_id"),
    ("memory", "source", [], "invalid_source"),
    ("memory", "title", "\ud800", "invalid_title"),
    ("memory", "content", "\ud800", "invalid_content"),
    ("memory", "source", "\ud800", "invalid_source"),
    ("memory", "tags", ["\ud800"], "invalid_tag"),
], ids=lambda value: "huge-int" if type(value) is int and value.bit_length() > 53 else None)
def test_invalid_field_types_return_bounded_client_error(client: FlaskClient, kind: str, field: str, value: Any, code: str) -> None:
    response = client.post(f"/api/platform/v1/{kind}", json={**payload(kind), field: value})
    assert response.status_code == 400
    assert response.get_json()["error"] == code
    collection = client.get(f"/api/platform/v1/{kind}").get_json()
    assert collection["schedules" if kind == "schedules" else "memories"] == []


@pytest.mark.parametrize("kind", ["schedules", "memory"])
@pytest.mark.parametrize("enabled", ["false", 0, [], None])
def test_enabled_must_be_a_boolean(client: FlaskClient, kind: str, enabled: Any) -> None:
    response = client.post(f"/api/platform/v1/{kind}", json={**payload(kind), "enabled": enabled})
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_enabled"


@pytest.mark.parametrize("kind", ["schedules", "memory"])
@pytest.mark.parametrize("revision", [False, 0.5, float("inf")])
def test_revision_cannot_be_coerced_to_current_integer(client: FlaskClient, kind: str, revision: Any) -> None:
    assert client.post(f"/api/platform/v1/{kind}", json=payload(kind)).status_code == 201
    response = client.put(f"/api/platform/v1/{kind}/item", json={"revision": revision, "enabled": False})
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_revision"
    current = client.get(f"/api/platform/v1/{kind}/item").get_json()["schedule" if kind == "schedules" else "memory"]
    assert current["revision"] == 0
    assert current["enabled"] is True


@pytest.mark.parametrize("kind", ["schedules", "memory"])
def test_explicit_false_and_integer_revision_remain_supported(client: FlaskClient, kind: str) -> None:
    assert client.post(f"/api/platform/v1/{kind}", json={**payload(kind), "enabled": False}).status_code == 201
    response = client.put(f"/api/platform/v1/{kind}/item", json={"revision": 0, "enabled": True})
    assert response.status_code == 200
    current = response.get_json()["schedule" if kind == "schedules" else "memory"]
    assert current["revision"] == 1
    assert current["enabled"] is True


def test_memory_validation_retains_unicode_error_cause(tmp_path: Path) -> None:
    repo = PlatformMemoryRepository(tmp_path / "platform.db")
    repo.init()
    service = PlatformMemoryService(repo)
    with pytest.raises(ApplicationError) as error:
        service.create("owner@example.test", {**payload("memory"), "content": "\ud800"})
    assert isinstance(error.value.__cause__.__cause__, UnicodeError)
