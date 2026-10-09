"""Memory storage errors keep their causes without leaking them over HTTP."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from hub.application.platform_memory_service import PlatformMemoryService
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.http.errors import ApplicationError
from hub.infrastructure.platform_memory_repository import (
    PlatformMemoryRepository,
    PlatformMemoryRepositoryError,
)

OWNER = "owner@example.test"
MEMORY = {
    "memory_id": "memory-one", "kind": "note", "title": "Policy",
    "content": "Reference", "tags": ["ops"], "source": "manual",
}


class FaultConnection:
    def __init__(
        self,
        connection: Any,
        root: sqlite3.Error,
        rollback_error: sqlite3.Error | None = None,
    ):
        self.connection = connection
        self.root = root
        self.rollback_error = rollback_error

    def execute(self, statement: str, values: Any = ()) -> Any:
        if statement.startswith("INSERT INTO platform_memory_fts"):
            raise self.root
        if statement == "ROLLBACK" and self.rollback_error is not None:
            raise self.rollback_error
        return self.connection.execute(statement, values)

    def close(self) -> None:
        self.connection.close()


def test_invalid_memory_id_preserves_validation_cause(tmp_path: Path) -> None:
    repository = PlatformMemoryRepository(tmp_path / "memory.db")
    repository.init()
    with pytest.raises(PlatformMemoryRepositoryError, match="invalid_id") as failure:
        repository.create(OWNER, {**MEMORY, "memory_id": "bad/id"}, now=1)
    assert isinstance(failure.value.__cause__, ValueError)


def test_sqlite_storage_cause_survives_service_without_http_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(FleetConfig.from_root(
        tmp_path, dev_operator=OWNER, platform_enabled=True,
        platform_memory_enabled=True,
    ))
    repository = app.extensions["fleet"]["repositories"]["platform_memory"]
    original_connect = repository._connect
    root = sqlite3.OperationalError("private storage marker")
    monkeypatch.setattr(
        repository, "_connect",
        lambda: FaultConnection(original_connect(), root),
    )
    service: PlatformMemoryService = app.extensions["fleet"]["services"]["platform_memory"]
    with pytest.raises(ApplicationError) as failure:
        service.create(OWNER, MEMORY)
    assert failure.value.status == 503
    assert isinstance(failure.value.__cause__, PlatformMemoryRepositoryError)
    assert failure.value.__cause__.__cause__ is root

    response = app.test_client().post("/api/platform/v1/memory", json={"memory": MEMORY})
    assert response.status_code == 503
    assert response.get_json()["error"] == "memory_store"
    assert "private storage marker" not in response.get_data(as_text=True)
    assert repository.get(OWNER, MEMORY["memory_id"]) is None


def test_rollback_failure_does_not_replace_sqlite_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = PlatformMemoryRepository(tmp_path / "memory.db")
    repository.init()
    original_connect = repository._connect
    root = sqlite3.OperationalError("primary storage failure")
    rollback_error = sqlite3.OperationalError("rollback failure")
    monkeypatch.setattr(
        repository, "_connect",
        lambda: FaultConnection(original_connect(), root, rollback_error),
    )

    with pytest.raises(PlatformMemoryRepositoryError, match="memory_store") as failure:
        repository.create(OWNER, MEMORY, now=1)
    assert failure.value.__cause__ is root

    monkeypatch.setattr(repository, "_connect", original_connect)
    assert repository.get(OWNER, MEMORY["memory_id"]) is None


def test_malformed_persisted_tags_fail_with_decode_cause(tmp_path: Path) -> None:
    app = create_app(FleetConfig.from_root(
        tmp_path, dev_operator=OWNER, platform_enabled=True,
        platform_memory_enabled=True,
    ))
    repository: PlatformMemoryRepository = app.extensions["fleet"]["repositories"][
        "platform_memory"
    ]
    repository.create(OWNER, MEMORY, now=1)
    with closing(sqlite3.connect(repository.db_path)) as connection, connection:
        connection.execute(
            "UPDATE platform_memory_items SET tags=? WHERE owner_id=? AND memory_id=?",
            ("not-json", OWNER, MEMORY["memory_id"]),
        )

    service: PlatformMemoryService = app.extensions["fleet"]["services"]["platform_memory"]
    with pytest.raises(ApplicationError) as failure:
        service.get(OWNER, MEMORY["memory_id"])
    assert failure.value.status == 503
    assert failure.value.code == "memory_store_corrupt"
    assert isinstance(failure.value.__cause__, PlatformMemoryRepositoryError)
    assert isinstance(failure.value.__cause__.__cause__, json.JSONDecodeError)

    response = app.test_client().get(f"/api/platform/v1/memory/{MEMORY['memory_id']}")
    assert response.status_code == 503
    assert response.get_json()["error"] == "memory_store_corrupt"
    assert "not-json" not in response.get_data(as_text=True)
    assert "JSONDecodeError" not in response.get_data(as_text=True)
