"""Validate explicit memory selections before accepting an assistant turn."""
from pathlib import Path
from typing import Any

import pytest
from flask import Flask

from hub.application.platform_memory_context_service import PlatformMemoryContextError
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.http.errors import ApplicationError
from hub.infrastructure.platform_memory_repository import PlatformMemoryRepositoryError

OWNER = "owner@example.test"


@pytest.fixture
def context_app(tmp_path: Path) -> tuple[Flask, str]:
    app = create_app(FleetConfig.from_root(
        tmp_path, dev_operator=OWNER, platform_enabled=True,
        platform_memory_enabled=True, platform_memory_context_enabled=True,
    ))
    repo = app.extensions["fleet"]["repositories"]["platform_memory"]
    repo.create(OWNER, {"memory_id": "memory-one", "kind": "note", "title": "Policy", "content": "Reference."}, now=1)
    client = app.test_client()
    conversation = client.post("/api/platform/v1/conversations", json={}).get_json()["conversation"]
    return app, conversation["conversation_id"]


@pytest.mark.parametrize("selection,code", [
    ({}, "memory_selector"),
    ({"memory_ids": ["memory-one"], "query": "Policy"}, "memory_selector"),
    ({"memory_ids": []}, "invalid_memory_ids"),
    ({"memory_ids": "memory-one"}, "invalid_memory_ids"),
    ({"memory_ids": ["memory-one", "memory-one"]}, "invalid_memory_ids"),
    ({"memory_ids": [False]}, "invalid_memory_id"),
    ({"memory_ids": ["memory-one"], "revisions": []}, "invalid_revisions"),
    ({"memory_ids": ["memory-one"], "revisions": {"memory-one": True}}, "invalid_revisions"),
    ({"memory_ids": ["memory-one"], "max_items": 0}, "invalid_max_items"),
    ({"memory_ids": ["memory-one"], "max_items": 21}, "invalid_max_items"),
    ({"memory_ids": ["memory-one"], "max_items": True}, "invalid_max_items"),
    ({"memory_ids": ["memory-one"], "max_bytes": 255}, "invalid_max_bytes"),
    ({"memory_ids": ["memory-one"], "max_bytes": 32769}, "invalid_max_bytes"),
    ({"memory_ids": ["memory-one"], "max_bytes": "1024"}, "invalid_max_bytes"),
    ({"query": []}, "invalid_query"),
    ({"query": " "}, "invalid_query"),
    ({"query": "x" * 513}, "invalid_query"),
    ({"query": "中" * 171}, "invalid_query"),
    ({"query": "\ud800"}, "invalid_query"),
])
def test_invalid_selection_is_client_error_without_accepting_turn(
    context_app: tuple[Flask, str], selection: dict[str, Any], code: str,
) -> None:
    app, conversation_id = context_app
    client = app.test_client()
    path = f"/api/platform/v1/conversations/{conversation_id}"
    response = client.post(path + "/turns", json={
        "text": "Use explicit memory", "client_token": "invalid-memory",
        "memory_context": {"enabled": True, **selection},
    })
    assert response.status_code == 400, response.get_json()
    assert response.get_json()["error"] == code
    conversation = client.get(path).get_json()["conversation"]
    assert conversation["runs"] == [] and conversation["messages"] == []


def test_query_unicode_validation_keeps_original_cause(context_app: tuple[Flask, str]) -> None:
    app, _ = context_app
    service = app.extensions["fleet"]["services"]["conversations"].memory_context
    with pytest.raises(PlatformMemoryContextError, match="invalid_query") as failure:
        service.resolve(OWNER, {"enabled": True, "query": "\ud800"})
    assert isinstance(failure.value.__cause__, UnicodeEncodeError)


@pytest.mark.parametrize("owner,selection,code", [
    ("", {"enabled": False}, "invalid_owner"),
    (OWNER, {"enabled": True, "memory_ids": [False]}, "invalid_memory_id"),
])
def test_identity_validation_keeps_original_cause(
    context_app: tuple[Flask, str], owner: str, selection: dict[str, Any], code: str,
) -> None:
    app, _ = context_app
    service = app.extensions["fleet"]["services"]["conversations"].memory_context
    with pytest.raises(PlatformMemoryContextError, match=code) as failure:
        service.resolve(owner, selection)
    assert isinstance(failure.value.__cause__, ValueError)


def test_search_failure_preserves_cause_through_turn_boundary(
    context_app: tuple[Flask, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, conversation_id = context_app
    service = app.extensions["fleet"]["services"]["conversations"]
    root = RuntimeError("private-storage-marker")
    store_error = PlatformMemoryRepositoryError("memory_search_unavailable")

    def unavailable(owner_id: str, query: str, *, limit: int) -> list[dict[str, Any]]:
        raise store_error from root

    monkeypatch.setattr(service.memory_context.repository, "search", unavailable)
    selection = {"enabled": True, "query": "Policy"}
    with pytest.raises(ApplicationError) as failure:
        service.turn(OWNER, conversation_id, text="Use memory", client_token="search-failure", memory_context=selection)
    assert failure.value.status == 503
    assert isinstance(failure.value.__cause__, PlatformMemoryContextError)
    assert failure.value.__cause__.__cause__ is store_error
    assert store_error.__cause__ is root
    response = app.test_client().post(f"/api/platform/v1/conversations/{conversation_id}/turns", json={
        "text": "Use memory", "client_token": "search-failure", "memory_context": selection,
    })
    assert response.status_code == 503
    assert "private-storage-marker" not in response.get_data(as_text=True)


@pytest.mark.parametrize("selection", [
    {"enabled": False},
    {"enabled": True, "memory_ids": ["memory-one"], "revisions": {"memory-one": 0}},
    {"enabled": True, "query": "x" * 512},
    {"enabled": True, "query": "中" * 170 + "ab"},
])
def test_valid_selection_remains_accepted(context_app: tuple[Flask, str], selection: dict[str, Any]) -> None:
    app, conversation_id = context_app
    response = app.test_client().post(f"/api/platform/v1/conversations/{conversation_id}/turns", json={
        "text": "Use memory", "client_token": "valid-memory", "memory_context": selection,
    })
    assert response.status_code == 202
