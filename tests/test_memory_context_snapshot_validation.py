"""A malformed frozen context cannot silently reach provider transport."""
import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from hub.application.platform_memory_context_service import (
    PlatformMemoryContextError,
    PlatformMemoryContextService,
    build_context_message,
)
from hub.application.run_worker_service import LocalRunWorkerService
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.infrastructure.platform_memory_repository import PlatformMemoryRepository
from tools.platform.providers.base import ModelResponse

OWNER = "owner@example.test"


@pytest.fixture
def frozen_context(tmp_path: Path) -> dict[str, Any]:
    repository = PlatformMemoryRepository(tmp_path / "memory.db")
    repository.init()
    repository.create(OWNER, {
        "memory_id": "memory-one", "kind": "note", "title": "Policy",
        "content": "Reference " * 200, "tags": ["ops"],
    }, now=1)
    return PlatformMemoryContextService(repository).resolve(OWNER, {
        "enabled": True, "memory_ids": ["memory-one"],
        "max_items": 1, "max_bytes": 512,
    })


@pytest.mark.parametrize("key,value", [
    ("enabled", "false"), ("enabled", 0), ("enabled", []),
    ("max_items", True), ("max_items", 1.9), ("max_items", "1"),
    ("max_bytes", 512.9), ("max_bytes", "512"), ("max_bytes", float("inf")),
    ("bytes", "512"), ("bytes", 512.9),
    ("item_count", True), ("item_count", "1"), ("item_count", 1.9),
    ("mode", "invalid"), ("mode", ["ids"]),
])
def test_snapshot_scalars_are_not_coerced(
    frozen_context: dict[str, Any], key: str, value: Any,
) -> None:
    frozen_context[key] = value
    with pytest.raises(PlatformMemoryContextError, match="memory_context_invalid"):
        build_context_message({"memory_context": frozen_context})


@pytest.mark.parametrize("context", [None, [], "", {}, {"items": []}])
def test_present_malformed_context_is_not_treated_as_absent(context: Any) -> None:
    with pytest.raises(PlatformMemoryContextError, match="memory_context_invalid"):
        build_context_message({"memory_context": context})


def test_snapshot_cannot_be_truncated_again(frozen_context: dict[str, Any]) -> None:
    assert frozen_context["bytes"] == frozen_context["max_bytes"]
    frozen_context["items"][0]["content"] += "unfrozen content" * 80
    with pytest.raises(PlatformMemoryContextError, match="memory_context_budget"):
        build_context_message({"memory_context": frozen_context})


@pytest.mark.parametrize("key,value", [
    ("memory_id", "bad/id"), ("memory_id", False),
    ("revision", True), ("revision", -1), ("revision", "0"),
    ("kind", "unsupported"), ("title", []), ("title", "x" * 161),
    ("content", {"text": "Reference"}), ("content", "\ud800"),
    ("content", "x" * 16385), ("tags", "ops"),
    ("tags", ["x"] * 17), ("tags", ["x" * 65]),
    ("source", []), ("content_truncated", "true"),
], ids=lambda value: str(value)[:40])
def test_snapshot_items_retain_public_field_contract(
    frozen_context: dict[str, Any], key: str, value: Any,
) -> None:
    frozen_context["items"][0][key] = value
    with pytest.raises(PlatformMemoryContextError, match="memory_context_invalid"):
        build_context_message({"memory_context": frozen_context})


def test_missing_snapshot_field_keeps_cause(frozen_context: dict[str, Any]) -> None:
    del frozen_context["max_items"]
    with pytest.raises(PlatformMemoryContextError) as failure:
        build_context_message({"memory_context": frozen_context})
    assert isinstance(failure.value.__cause__, KeyError)


def test_snapshot_unicode_error_keeps_cause(frozen_context: dict[str, Any]) -> None:
    frozen_context["items"][0]["title"] = "\ud800"
    with pytest.raises(PlatformMemoryContextError) as failure:
        build_context_message({"memory_context": frozen_context})
    assert isinstance(failure.value.__cause__, UnicodeEncodeError)


@pytest.mark.parametrize("field,value", [
    ("title", " Policy"), ("content", "  Reference"), ("source", "manual "),
    ("tags", ["ops", "ops"]), ("unexpected", "private"),
])
def test_snapshot_rejects_noncanonical_or_extra_fields(
    frozen_context: dict[str, Any], field: str, value: Any,
) -> None:
    if field == "unexpected":
        frozen_context["items"][0][field] = value
    else:
        frozen_context["items"][0][field] = value
    with pytest.raises(PlatformMemoryContextError, match="memory_context_invalid"):
        build_context_message({"memory_context": frozen_context})


def test_snapshot_rejects_duplicate_memory_ids(frozen_context: dict[str, Any]) -> None:
    frozen_context["items"].append(deepcopy(frozen_context["items"][0]))
    frozen_context["item_count"] = 2
    with pytest.raises(PlatformMemoryContextError, match="memory_context_invalid"):
        build_context_message({"memory_context": frozen_context})


def test_valid_snapshot_round_trip_is_immutable(frozen_context: dict[str, Any]) -> None:
    original = deepcopy(frozen_context)
    message = build_context_message({"memory_context": frozen_context})
    assert message is not None and message["role"] == "system"
    assert len(message["content"].encode("utf-8")) == original["bytes"]
    assert original["items"][0]["content"] in message["content"]
    assert frozen_context == original
    assert build_context_message({}) is None
    assert build_context_message({"memory_context": {"enabled": False}}) is None


@pytest.mark.parametrize("corruption", ["container", "enablement", "budget", "count", "unicode"])
def test_worker_rejects_corrupt_persisted_context_before_provider(
    tmp_path: Path, frozen_context: dict[str, Any], corruption: str,
) -> None:
    app = create_app(FleetConfig.from_root(tmp_path, platform_enabled=True, dev_operator=OWNER))
    repository = app.extensions["fleet"]["platform_repository"]
    repository.upsert_workspace(OWNER, {
        "workspace_id": "home", "backend": "directory", "root_path": str(tmp_path / "workspace"),
    })
    context: Any = frozen_context
    code = "memory_context_invalid"
    if corruption == "container":
        context = []
    elif corruption == "enablement":
        context["enabled"] = "false"
    elif corruption == "budget":
        context["items"][0]["content"] += "unfrozen content" * 80
        code = "memory_context_budget"
    elif corruption == "count":
        context["item_count"] = True
    else:
        context["items"][0]["title"] = "\ud800"
    repository.create_conversation(OWNER, "conversation-one", title="", workspace_id="home")
    repository.append_turn(
        OWNER, "conversation-one", "message-one", "run-one", text="Reference", client_token="turn-one",
        config_snapshot={"workspace_id": "home"}, now=1,
    )
    with closing(sqlite3.connect(repository.db_path)) as connection, connection:
        connection.execute(
            "UPDATE runs SET config_snapshot=? WHERE owner_id=? AND run_id=?",
            (json.dumps({"workspace_id": "home", "memory_context": context}), OWNER, "run-one"),
        )
    calls: list[str] = []

    class Provider:
        def complete(self, messages: Any, tools: Any, *, request_observer: Any = None) -> ModelResponse:
            calls.append("provider")
            return ModelResponse(kind="final", text="done")

    worker = LocalRunWorkerService(
        repository, app.extensions["fleet"]["services"]["run_events"],
        provider_factory=lambda profile: Provider(),
    )
    result = worker.run_once(OWNER)
    assert result is not None and result["state"] == "failed"
    assert calls == []
    persisted = repository.get_run(OWNER, "run-one")
    assert persisted["state"] == "failed" and persisted["result_text"] == code
    events = app.test_client().get("/api/platform/v1/runs/run-one/events").get_json()["events"]
    assert any(event["kind"] == "run_failed" and event["payload"]["error_code"] == code for event in events)
    assert all(event["kind"] not in {"provider_request_started", "memory_context_selected"} for event in events)
