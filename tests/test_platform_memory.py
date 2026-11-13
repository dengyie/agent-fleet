import pytest

from hub.application.platform_memory_service import PlatformMemoryService
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.infrastructure.platform_db import PlatformRepository
from hub.infrastructure.platform_memory_repository import (
    PlatformMemoryRepository,
    PlatformMemoryRepositoryError,
)


OWNER = "owner@example.test"
OTHER = "other@example.test"


def _repo(tmp_path):
    repo = PlatformMemoryRepository(tmp_path / "platform.db")
    repo.init()
    return repo


def _item(**overrides):
    value = {
        "memory_id": "memory-one",
        "kind": "fact",
        "title": "Deployment host",
        "content": "The Hub uses a dedicated execution host.",
        "tags": ["ops", "hub"],
        "source": "manual",
    }
    value.update(overrides)
    return value


def test_memory_repository_is_owner_scoped_and_public_dto_is_bounded(tmp_path):
    repo = _repo(tmp_path)
    created = repo.create(OWNER, _item(), now=10)
    assert created["memory_id"] == "memory-one"
    assert "owner_id" not in created
    assert repo.get(OTHER, "memory-one") is None
    assert repo.list(OTHER) == []
    assert repo.get(OWNER, "memory-one")["revision"] == 0


def test_memory_repository_validates_kind_content_and_tags(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(PlatformMemoryRepositoryError) as kind:
        repo.create(OWNER, _item(kind="secret"))
    assert kind.value.code == "invalid_kind"
    with pytest.raises(PlatformMemoryRepositoryError) as content:
        repo.create(OWNER, _item(content="x" * (16 * 1024 + 1)))
    assert content.value.code == "value_too_large"
    with pytest.raises(PlatformMemoryRepositoryError) as tags:
        repo.create(OWNER, _item(tags=["x"] * 17))
    assert tags.value.code == "invalid_tags"


def test_memory_repository_fts_search_and_delete(tmp_path):
    repo = _repo(tmp_path)
    repo.create(OWNER, _item(), now=10)
    repo.create(OWNER, _item(memory_id="memory-two", kind="note", title="Runbook", content="Restart policy is approval gated."), now=20)
    repo.create(OTHER, _item(memory_id="memory-other", content="approval gated"), now=30)
    found = repo.search(OWNER, "approval policy")
    assert [item["memory_id"] for item in found] == ["memory-two"]
    assert repo.search(OWNER, "approval") == [repo.get(OWNER, "memory-two")]
    assert repo.delete(OWNER, "memory-two", expected_revision=0) is True
    assert repo.search(OWNER, "approval") == []


def test_memory_repository_update_requires_current_revision(tmp_path):
    repo = _repo(tmp_path)
    created = repo.create(OWNER, _item(), now=10)
    updated = repo.update(OWNER, "memory-one", {"content": "Changed"}, expected_revision=0, now=20)
    assert updated["revision"] == 1
    assert updated["content"] == "Changed"
    with pytest.raises(PlatformMemoryRepositoryError) as conflict:
        repo.update(OWNER, "memory-one", {"content": "stale"}, expected_revision=0, now=30)
    assert conflict.value.code == "revision_conflict"
    with pytest.raises(PlatformMemoryRepositoryError) as delete_conflict:
        repo.delete(OWNER, "memory-one", expected_revision=0)
    assert delete_conflict.value.code == "revision_conflict"


def test_memory_service_maps_not_found_and_returns_public_payload(tmp_path):
    repo = _repo(tmp_path)
    service = PlatformMemoryService(repo, clock=lambda: 10)
    created = service.create(OWNER, _item())
    assert created["ok"] is True
    assert "owner_id" not in created["memory"]
    assert service.list(OWNER)["memories"][0]["memory_id"] == "memory-one"
    with pytest.raises(Exception) as missing:
        service.get(OWNER, "missing")
    assert getattr(missing.value, "code", None) == "memory_not_found"


def test_memory_repository_initializes_tables_on_shared_platform_db(tmp_path):
    repo = PlatformRepository(tmp_path / "platform.db")
    repo.init()
    memory = PlatformMemoryRepository(tmp_path / "platform.db")
    memory.init()
    with repo._connect() as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','virtual table')")}
    assert "platform_memory_items" in tables
    assert "platform_memory_fts" in tables


def _app(tmp_path, *, memory_enabled=False):
    return create_app(FleetConfig.from_root(
        tmp_path,
        ingest_token="ingest-only",
        dev_operator=OWNER,
        platform_enabled=True,
        platform_memory_enabled=memory_enabled,
    ))


def test_memory_gate_is_default_off_and_routes_are_absent(tmp_path):
    app = _app(tmp_path)
    assert app.extensions["fleet"]["services"].get("platform_memory") is None
    assert app.test_client().get("/api/platform/v1/memory").status_code == 404
    assert app.config["PLATFORM_MEMORY_ENABLED"] is False


def test_memory_http_crud_search_and_revision_fence(tmp_path):
    app = _app(tmp_path, memory_enabled=True)
    client = app.test_client()
    assert app.config["PLATFORM_MEMORY_ENABLED"] is True
    created = client.post("/api/platform/v1/memory", json={
        "memory": {
            "memory_id": "memory-api",
            "kind": "preference",
            "title": "Output style",
            "content": "Prefer concise status updates.",
            "tags": ["assistant"],
        },
    })
    assert created.status_code == 201
    memory = created.get_json()["memory"]
    assert memory["revision"] == 0
    assert "owner_id" not in str(created.get_json())
    assert client.get("/api/platform/v1/memory/search?q=concise").status_code == 200
    assert client.get("/api/platform/v1/memory/memory-api").status_code == 200
    missing_revision = client.put(
        "/api/platform/v1/memory/memory-api",
        json={"content": "new"},
    )
    assert missing_revision.status_code == 428
    updated = client.put(
        "/api/platform/v1/memory/memory-api",
        json={"content": "Use concise status updates.", "revision": 0},
    )
    assert updated.status_code == 200
    assert updated.get_json()["memory"]["revision"] == 1
    conflict = client.delete(
        "/api/platform/v1/memory/memory-api",
        headers={"If-Match": "0"},
    )
    assert conflict.status_code == 409
    deleted = client.delete(
        "/api/platform/v1/memory/memory-api",
        headers={"If-Match": "1"},
    )
    assert deleted.status_code == 200
    assert client.get("/api/platform/v1/memory/memory-api").status_code == 404
