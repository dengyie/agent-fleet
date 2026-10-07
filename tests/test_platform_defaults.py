import sqlite3
from pathlib import Path

import pytest

from hub.application.defaults_service import DefaultsService
from hub.domain.platform import resolve_run_config
from hub.infrastructure.platform_db import PlatformRepository


def _repo(tmp_path: Path) -> PlatformRepository:
    repo = PlatformRepository(tmp_path / "platform.db")
    repo.init()
    repo.upsert_model("owner@example.test", {
        "profile_id": "model-default", "provider": "compatible",
        "model": "test-model", "secret_ref": "secret://hidden",
        "capabilities": {"streaming": True},
    })
    repo.upsert_workspace("owner@example.test", {
        "workspace_id": "workspace-default", "root_path": str(tmp_path / "workspace"),
    })
    repo.upsert_node("owner@example.test", {
        "node_id": "node-default", "capabilities": {"workspace.exec": True},
    })
    return repo


def test_platform_store_isolated_and_wal(tmp_path):
    repo = _repo(tmp_path)
    assert repo.schema_version() == 2
    assert repo.path == tmp_path / "platform.db"
    with sqlite3.connect(repo.path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"meta", "model_profiles", "workspaces", "nodes", "owner_defaults"}.issubset(tables)


def test_defaults_use_revision_and_reject_cross_owner_reference(tmp_path):
    repo = _repo(tmp_path)
    first = repo.update_defaults("owner@example.test", {
        "model_profile_id": "model-default",
        "workspace_id": "workspace-default",
        "execution_node_id": "node-default",
    }, 0)
    assert first["revision"] == 1
    with pytest.raises(Exception) as conflict:
        repo.update_defaults("owner@example.test", {}, 0)
    assert getattr(conflict.value, "code", None) == "revision_conflict"
    with pytest.raises(Exception) as forbidden:
        repo.update_defaults("owner@example.test", {"model_profile_id": "other-model"}, 1)
    assert getattr(forbidden.value, "code", None) == "reference_forbidden"


def test_default_resolution_precedence_is_request_conversation_workspace_owner():
    snapshot = resolve_run_config("owner@example.test", owner={
        "model_profile_id": "owner-model", "workspace_id": "owner-workspace",
    }, workspace={
        "model_profile_id": "workspace-model", "workspace_id": "workspace-id",
    }, conversation={
        "model_profile_id": "conversation-model",
    }, overrides={
        "model_profile_id": "request-model",
    })
    assert snapshot.model_profile_id == "request-model"
    assert snapshot.workspace_id == "workspace-id"
    assert snapshot.execution_node_id is None
    assert snapshot.source == {
        "model_profile_id": "request",
        "workspace_id": "workspace",
        "execution_node_id": "unset",
    }


@pytest.mark.parametrize("overrides", [[], "", 0, False])
def test_default_resolution_rejects_non_mapping_overrides(overrides):
    with pytest.raises(TypeError, match="overrides must be a mapping"):
        resolve_run_config("owner@example.test", overrides=overrides)


def test_public_model_never_returns_secret_ref(tmp_path):
    service = DefaultsService(_repo(tmp_path))
    payload = service.list_models("owner@example.test")
    assert payload["models"][0]["secret_configured"] is True
    assert "secret_ref" not in payload["models"][0]
    assert "secret://hidden" not in str(payload)


def test_model_profile_persists_non_secret_provider_config_without_public_secret(tmp_path):
    repo = _repo(tmp_path)
    profile = repo.get_model("owner@example.test", "model-default")
    assert profile["provider_config"] == {}
    repo.upsert_model("owner@example.test", {
        "profile_id": "remote", "provider": "openai_compatible", "model": "gpt-test",
        "secret_ref": "env://PROVIDER_KEY",
        "provider_config": {
            "endpoint": "https://llm.example.test/v1/chat/completions",
            "timeout_s": 12, "max_retries": 2, "stream": True,
        },
    })
    profile = repo.get_model("owner@example.test", "remote")
    assert profile["provider_config"]["endpoint"].endswith("chat/completions")
    assert profile["secret_ref"] == "env://PROVIDER_KEY"
    public = DefaultsService(repo).list_models("owner@example.test")["models"]
    remote = next(item for item in public if item["profile_id"] == "remote")
    assert "secret_ref" not in remote
    assert "provider_config" not in remote


def test_provider_endpoint_credentials_and_query_are_rejected_before_persistence(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(Exception) as query:
        repo.upsert_model("owner@example.test", {
            "profile_id": "bad-query", "provider": "openai_compatible",
            "model": "gpt", "secret_ref": "env://KEY",
            "provider_config": {"endpoint": "https://llm.example.test/v1?api_key=secret"},
        })
    assert getattr(query.value, "code", None) == "invalid_model"
    with pytest.raises(Exception) as userinfo:
        repo.upsert_model("owner@example.test", {
            "profile_id": "bad-userinfo", "provider": "openai_compatible",
            "model": "gpt", "secret_ref": "env://KEY",
            "provider_config": {"endpoint": "https://user:secret@llm.example.test"},
        })
    assert getattr(userinfo.value, "code", None) == "invalid_model"
