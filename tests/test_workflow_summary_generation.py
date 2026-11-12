"""Model summary provenance and original SQLite generation authority."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from hub.domain.workflow_errors import WorkflowValidationError
from hub.domain.workflow_identity import canonical_json
from hub.domain.workflow_summary import WorkflowSummaryVersion, parse_summary
from hub.infrastructure.workflow_schema import WorkflowSchemaError
from hub.infrastructure.workflow_summary_schema import inspect_summary_schema
from test_workflow_repository import OWNER, WORKSPACE
from test_workflow_summary import publish, store_for


def provenance(**changes):
    from hub.domain.workflow_summary import WorkflowSummaryGenerationProvenance

    return WorkflowSummaryGenerationProvenance(**{
        "run_id": "summary-run-a", "source_run_ids": ("source-run-a", "source-run-b"),
        "source_sha256": "a" * 64, "model_profile_id": "model-a", "provider_digest": "b" * 64,
        **changes,
    })


def generated(**changes):
    return WorkflowSummaryVersion(OWNER, WORKSPACE, "summary-a", 1, "生成摘要", "完整模型内容\n", OWNER, 100000,
        source_kind="model", generation=provenance(), **changes)


def test_model_summary_roundtrips_exact_generated_provenance_and_full_content():
    item = generated()
    value = json.loads(item.snapshot)
    assert value["schema_version"] == 2 and value["source_kind"] == "model"
    assert value["generation"] == {"run_id": "summary-run-a", "source_run_ids": ["source-run-a", "source-run-b"],
        "source_sha256": "a" * 64, "model_profile_id": "model-a", "provider_digest": "b" * 64}
    assert parse_summary(value) == item and parse_summary(value).snapshot == item.snapshot
    assert "完整模型内容" not in repr(item)


def test_operator_summary_preserves_original_schema1_bytes_and_rejects_model_mislabel():
    item = WorkflowSummaryVersion(OWNER, WORKSPACE, "summary-a", 1, "人工摘要", "原正文\n", OWNER, 100000)
    expected = {"schema_version": 1, "owner_id": OWNER, "workspace_id": WORKSPACE, "summary_id": "summary-a",
        "revision": 1, "title": "人工摘要", "content": "原正文\n", "created_by": OWNER, "created_at_ms": 100000,
        "source_kind": "operator"}
    assert item.snapshot == canonical_json(expected) and parse_summary(expected) == item
    with pytest.raises(WorkflowValidationError):
        replace(item, generation=provenance())
    with pytest.raises(WorkflowValidationError):
        replace(item, source_kind="model")


@pytest.mark.parametrize("changes", [
    {"source_run_ids": []}, {"source_run_ids": ()}, {"source_run_ids": ("a", "a")},
    {"source_run_ids": tuple(f"run-{i}" for i in range(17))}, {"source_run_ids": (False,)},
    {"source_sha256": "A" * 64}, {"provider_digest": "b" * 63}, {"run_id": ""}, {"model_profile_id": False},
])
def test_generation_provenance_rejects_missing_duplicate_unbounded_or_invalid_identity(changes):
    with pytest.raises(WorkflowValidationError):
        provenance(**changes)


@pytest.mark.parametrize("kind", ["extra", "schema", "source_kind", "missing", "provenance_extra", "content_size"])
def test_model_summary_parser_rejects_forged_or_incomplete_provenance(kind):
    value = json.loads(generated().snapshot)
    if kind == "extra": value["extra"] = "invalid"
    elif kind == "schema": value["schema_version"] = 1
    elif kind == "source_kind": value["source_kind"] = "operator"
    elif kind == "missing": del value["generation"]
    elif kind == "provenance_extra": value["generation"]["provider_config"] = {"secret": "forged"}
    else: value["content"] = "x" * 8192
    with pytest.raises(WorkflowValidationError):
        parse_summary(value)


def test_summary_generation_schema_is_optional_and_preserves_greater_workflow_reader(tmp_path: Path):
    from hub.infrastructure.workflow_summary_schema import GENERATION_STATEMENTS

    store = store_for(tmp_path / "platform.db")
    with store.workflows.transaction(write=False) as connection:
        assert not inspect_summary_schema(connection)
    publish(store)
    with store.workflows.transaction() as connection:
        for statement in GENERATION_STATEMENTS: connection.execute(statement)
        connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=max(minimum_reader_version,16)")
        assert inspect_summary_schema(connection)
    store.workflows.init()
    with store.workflows.transaction(write=False) as connection:
        assert tuple(connection.execute("SELECT schema_version,minimum_reader_version FROM workflow_summary_schema_meta").fetchone()) == (2, 2)
        assert connection.execute("SELECT minimum_reader_version FROM workflow_schema_meta").fetchone()[0] == 16


@pytest.mark.parametrize("broken", ["reader", "metadata", "missing_table"])
def test_generation_schema_rejects_reader_downgrade_and_incomplete_authority(tmp_path: Path, broken: str):
    from hub.infrastructure.workflow_summary_schema import GENERATION_STATEMENTS

    store = store_for(tmp_path / "platform.db")
    publish(store)
    with store.workflows.transaction() as connection:
        for statement in GENERATION_STATEMENTS: connection.execute(statement)
        connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=16")
        if broken == "reader": connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=15")
        elif broken == "metadata": connection.execute("UPDATE workflow_summary_schema_meta SET minimum_reader_version=1")
        else: connection.execute("DROP TABLE workflow_summary_generation_sources")
        with pytest.raises(WorkflowSchemaError): inspect_summary_schema(connection)


def generation_setup(tmp_path, *, source_output="完整成功结果 🌱\n"):
    from hub.infrastructure.platform_db import PlatformRepository
    from hub.infrastructure.workflow_summary_generation_repository import WorkflowSummaryGenerationRepository
    from test_workflow_repository import prepare_database

    workflows = prepare_database(tmp_path / "platform.db")
    platform = PlatformRepository(workflows.db_path)
    platform.upsert_workspace(OWNER, {"workspace_id": WORKSPACE, "root_path": str(tmp_path)})
    platform.upsert_model(OWNER, {"profile_id": "model-a", "provider": "deterministic", "model": "frozen-model"})
    platform.create_conversation(OWNER, "source-chat", workspace_id=WORKSPACE, title="来源")
    platform.append_turn(OWNER, "source-chat", "source-message-a", "source-run-a", text="完整用户输入 🌱\n",
        client_token="source-token-a", config_snapshot={"workspace_id": WORKSPACE}, now=99)
    platform.set_run_state(OWNER, "source-run-a", "succeeded", now=100, result_text=source_output)
    values = {"workspace_id": WORKSPACE, "summary_id": "summary-a", "title": "模型摘要", "expected_epoch": 0,
        "model_profile_id": "model-a", "source_run_ids": ["source-run-a"], "client_token": "generation-token-a"}
    return workflows, platform, WorkflowSummaryGenerationRepository(workflows), values


def test_generation_creation_freezes_full_source_and_model_in_original_run_atomically(tmp_path):
    workflows, platform, store, values = generation_setup(tmp_path)
    item = store.create(OWNER, values, now=101)
    assert item["state"] == "queued" and item["source_run_ids"] == ["source-run-a"] and item["candidate"] is None
    execution = platform.get_run_execution_context(OWNER, item["run_id"])
    body = json.loads(execution["message"]["content"])
    assert body["sources"][0]["input"] == "完整用户输入 🌱\n"
    assert body["sources"][0]["output"] == "完整成功结果 🌱\n"
    assert execution["config_snapshot"]["provider_snapshot"]["model"] == "frozen-model"
    assert execution["config_snapshot"]["execution_node_id"] is None
    assert "完整成功结果" not in json.dumps(item, ensure_ascii=False)
    assert store.create(OWNER, values, now=500) == item
    with workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM workflow_summary_generation_sources").fetchone()[0] == 1
        assert connection.execute("SELECT minimum_reader_version FROM workflow_schema_meta").fetchone()[0] == 16


@pytest.mark.parametrize("kind", ["source_state", "source_scope", "source_size", "model", "epoch"])
def test_generation_rejects_unavailable_or_changed_authority_before_creating_run(tmp_path, kind):
    from hub.infrastructure.workflow_repository import WorkflowRepositoryError

    workflows, platform, store, values = generation_setup(tmp_path)
    with workflows.transaction() as connection:
        if kind == "source_state": connection.execute("UPDATE runs SET state='unknown'")
        elif kind == "source_scope": connection.execute("UPDATE conversations SET owner_id='other@example.test'")
        elif kind == "source_size": connection.execute("UPDATE runs SET result_text=?", ("x" * 49153,))
        elif kind == "model": connection.execute("UPDATE model_profiles SET enabled=0")
        else: values = {**values, "expected_epoch": 1}
    with pytest.raises((WorkflowValidationError, WorkflowRepositoryError)): store.create(OWNER, values, now=101)
    with workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        assert not inspect_summary_schema(connection)


def test_generation_failure_rolls_back_new_schema_message_run_and_reader(tmp_path, monkeypatch):
    from hub.infrastructure.workflow_repository import WorkflowRepositoryError

    workflows, platform, store, values = generation_setup(tmp_path)
    original = workflows.transaction
    @contextmanager
    def rejected(**kwargs):
        with original(**kwargs) as connection:
            connection.set_authorizer(lambda action, table, *rest: sqlite3.SQLITE_DENY
                if (action, table) == (sqlite3.SQLITE_INSERT, "workflow_summary_generation_sources") else sqlite3.SQLITE_OK)
            yield connection
    monkeypatch.setattr(workflows, "transaction", rejected)
    with pytest.raises(WorkflowRepositoryError): store.create(OWNER, values, now=101)
    with original(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM conversations").fetchone()[0] == 1
        assert connection.execute("SELECT minimum_reader_version FROM workflow_schema_meta").fetchone()[0] == 1
        assert not inspect_summary_schema(connection)


def test_model_summary_publication_uses_original_success_and_returns_same_version_on_retry(tmp_path):
    from hub.infrastructure.workflow_summary_repository import WorkflowSummaryRepository

    workflows, platform, store, values = generation_setup(tmp_path)
    generation = store.create(OWNER, values, now=101)
    platform.set_run_state(OWNER, generation["run_id"], "succeeded", now=102, result_text="模型生成的完整正文\n")
    summaries = WorkflowSummaryRepository(workflows)
    item = summaries.publish_generated(OWNER, generation["run_id"], now=103)
    assert item.content == "模型生成的完整正文\n" and item.source_kind == "model"
    assert item.generation.run_id == generation["run_id"] and item.generation.source_run_ids == ("source-run-a",)
    assert item.generation.source_sha256 == generation["source_sha256"]
    assert summaries.publish_generated(OWNER, generation["run_id"], now=999) == item
    assert summaries.get(OWNER, WORKSPACE, "summary-a", revision=1) == item
    workflows.init()


@pytest.mark.parametrize("state", ["queued", "running", "failed", "unknown", "cancelled"])
def test_only_successful_generation_can_publish_model_summary(tmp_path, state):
    from hub.infrastructure.workflow_repository import WorkflowRepositoryError
    from hub.infrastructure.workflow_summary_repository import WorkflowSummaryRepository

    workflows, platform, store, values = generation_setup(tmp_path)
    item = store.create(OWNER, values, now=101)
    with workflows.transaction() as connection:
        connection.execute("UPDATE runs SET state=?,result_text='untrusted' WHERE run_id=?", (state, item["run_id"]))
    with pytest.raises(WorkflowRepositoryError): WorkflowSummaryRepository(workflows).publish_generated(OWNER, item["run_id"], now=102)
    assert WorkflowSummaryRepository(workflows).list(OWNER, WORKSPACE)["summaries"] == []


def generation_worker(platform, store, captured, *, guard=True, before_result=None):
    from hub.application.run_event_service import RunEventService
    from hub.application.run_worker_service import LocalRunWorkerService
    from hub.platform.providers.base import ModelResponse

    class Provider:
        def complete(self, messages, tools, *, request_observer=None):
            captured.append((messages, tools))
            if before_result: before_result()
            return ModelResponse(kind="final", text="完整模型摘要\n")
    worker = LocalRunWorkerService(platform, RunEventService(platform, clock=lambda: 101),
        provider_factory=lambda _profile: Provider(), clock=lambda: 101)
    if guard: worker.workflow_summary_generation_guard = store
    return worker


def test_original_worker_generates_once_with_full_frozen_sources_and_zero_tools(tmp_path):
    from hub.infrastructure.workflow_summary_repository import WorkflowSummaryRepository

    workflows, platform, store, values = generation_setup(tmp_path)
    item = store.create(OWNER, values, now=101)
    captured = []
    worker = generation_worker(platform, store, captured)
    result = worker.run_once(OWNER)
    assert result["state"] == "succeeded", result
    assert len(captured) == 1 and captured[0][1] == []
    assert len(captured[0][0]) == 1 and "完整成功结果" in captured[0][0][0]["content"]
    assert worker.run_once(OWNER) is None
    assert WorkflowSummaryRepository(workflows).publish_generated(OWNER, item["run_id"], now=102).content == "完整模型摘要\n"


def test_generation_cancel_during_model_keeps_original_cancelled_and_unpublishable(tmp_path):
    from hub.infrastructure.workflow_repository import WorkflowRepositoryError
    from hub.infrastructure.workflow_summary_repository import WorkflowSummaryRepository

    workflows, platform, store, values = generation_setup(tmp_path)
    item = store.create(OWNER, values, now=101)
    calls = []
    worker = generation_worker(platform, store, calls,
        before_result=lambda: platform.cancel_run(OWNER, item["run_id"], now=101))
    assert worker.run_once(OWNER)["state"] == "cancelled"
    assert len(calls) == 1
    with pytest.raises(WorkflowRepositoryError): WorkflowSummaryRepository(workflows).publish_generated(OWNER, item["run_id"], now=102)


def test_generation_source_conversation_cannot_be_deleted_after_archiving(tmp_path):
    from hub.infrastructure.platform_db import PlatformRepositoryError

    workflows, platform, store, values = generation_setup(tmp_path)
    store.create(OWNER, values, now=101)
    platform.set_conversation_archived(OWNER, "source-chat", archived=True, now=102)
    with pytest.raises(PlatformRepositoryError, match="conversation_workflow_referenced"):
        platform.delete_archived_conversation(OWNER, "source-chat")
    assert platform.get_conversation_metadata(OWNER, "source-chat") is not None
    workflows.init()


def test_internal_generation_conversation_stays_out_of_owner_inbox_and_source_picker(tmp_path):
    _, platform, store, values = generation_setup(tmp_path)
    platform.create_conversation(OWNER, "summary-conv-user", workspace_id=WORKSPACE, title="普通用户对话")
    item = store.create(OWNER, values, now=101)
    execution = platform.get_run_execution_context(OWNER, item["run_id"])
    internal = execution["conversation_id"]
    assert {row["conversation_id"] for row in platform.list_conversations(OWNER)} == {"source-chat", "summary-conv-user"}
    platform.set_conversation_archived(OWNER, internal, archived=True, now=102)
    platform.set_conversation_archived(OWNER, "source-chat", archived=True, now=102)
    assert [row["conversation_id"] for row in platform.list_conversations(OWNER, archived=True)] == ["source-chat"]
    assert store.get(OWNER, item["run_id"])["run_id"] == item["run_id"]


@pytest.mark.parametrize("encrypted", [False, True])
def test_generated_summary_backup_preserves_original_run_sources_and_model_provenance(tmp_path, encrypted):
    from hub.infrastructure.platform_backup import PlatformBackupService
    from hub.infrastructure.workflow_repository import WorkflowRepository
    from hub.infrastructure.workflow_summary_generation_repository import WorkflowSummaryGenerationRepository
    from hub.infrastructure.workflow_summary_repository import WorkflowSummaryRepository

    workflows, platform, store, values = generation_setup(tmp_path)
    generation = store.create(OWNER, values, now=101)
    platform.set_run_state(OWNER, generation["run_id"], "succeeded", now=102, result_text="完整生成内容\n")
    expected = WorkflowSummaryRepository(workflows).publish_generated(OWNER, generation["run_id"], now=103)
    artifacts, backup, restored = tmp_path / "artifacts", tmp_path / "backup", tmp_path / "restored"
    artifacts.mkdir()
    service = PlatformBackupService()
    if encrypted:
        service.create_encrypted_backup(workflows.db_path, artifacts, backup, b"k" * 32, "test-key")
        service.restore_encrypted_backup(backup, restored, b"k" * 32)
    else:
        service.create_backup(workflows.db_path, artifacts, backup)
        service.restore_backup(backup, restored)
    recovered = WorkflowRepository(restored / "platform.db")
    recovered.init()
    assert WorkflowSummaryRepository(recovered).get(OWNER, WORKSPACE, "summary-a", revision=1) == expected
    assert WorkflowSummaryGenerationRepository(recovered).get(OWNER, generation["run_id"])["source_run_ids"] == ["source-run-a"]
    with recovered.transaction(write=False) as connection:
        assert connection.execute("SELECT admission_mode FROM workflow_schema_meta").fetchone()[0] == "frozen"


def generation_http(tmp_path, *, source_output="完整成功结果 🌱\n"):
    from hub.bootstrap import create_app
    from hub.config import FleetConfig

    workflows, platform, store, values = generation_setup(tmp_path, source_output=source_output)
    app = create_app(FleetConfig.from_root(tmp_path, platform_enabled=True, platform_workflows_enabled=True,
        platform_db=workflows.db_path, dev_operator=OWNER, serve_frontend=False), repositories={"workflow_clock": lambda: 101})
    return app, app.test_client(), platform, values


def test_generation_http_creates_and_recovers_original_run_without_model_io_then_publishes(tmp_path):
    app, client, platform, values = generation_http(tmp_path)
    url = "/api/platform/v1/workflow-summary-generations"
    headers = {"Origin": "http://localhost"}
    response = client.post(url, json=values, headers=headers)
    assert response.status_code == 201, response.get_json()
    item = response.get_json()["generation"]
    assert item["state"] == "queued" and item["candidate"] is None
    assert client.post(url, json=values, headers=headers).get_json()["generation"]["run_id"] == item["run_id"]
    assert client.get(url + "/run", query_string={"run_id": item["run_id"]}).get_json()["generation"] == item
    assert client.get(url + "/token", query_string={"client_token": values["client_token"]}).get_json()["generation"] == item
    platform.set_run_state(OWNER, item["run_id"], "succeeded", now=102, result_text="模型摘要 HTTP\n")
    result = client.post(url + "/publish", json={"run_id": item["run_id"]}, headers=headers)
    assert result.status_code == 201, result.get_json()
    summary = result.get_json()["summary"]
    assert summary["source_kind"] == "model" and summary["generation"]["run_id"] == item["run_id"]
    assert summary["content"] == "模型摘要 HTTP\n"
    assert client.post(url + "/publish", json={"run_id": item["run_id"]}, headers=headers).get_json() == result.get_json()


@pytest.mark.parametrize("kind,status", [("unknown_run", 404), ("unknown_token", 404),
    ("pending_publish", 409), ("source", 409), ("large", 413), ("extra", 400), ("profile", 400)])
def test_generation_http_returns_stable_errors_without_creating_extra_runs(tmp_path, kind, status):
    app, client, platform, values = generation_http(tmp_path,
        source_output="界" * 17000 if kind == "large" else "完整成功结果 🌱\n")
    url = "/api/platform/v1/workflow-summary-generations"
    headers = {"Origin": "http://localhost"}
    if kind == "unknown_run": response = client.get(url + "/run", query_string={"run_id": "unknown"})
    elif kind == "unknown_token": response = client.get(url + "/token", query_string={"client_token": "unknown"})
    elif kind == "pending_publish":
        item = client.post(url, json=values, headers=headers).get_json()["generation"]
        response = client.post(url + "/publish", json={"run_id": item["run_id"]}, headers=headers)
    else:
        if kind == "source": values["source_run_ids"] = ["unknown"]
        elif kind == "large": pass
        elif kind == "extra": values["content"] = "forged model summary"
        else: values["model_profile_id"] = "bad\x00model"
        response = client.post(url, json=values, headers=headers)
    assert response.status_code == status, response.get_json()
    with app.extensions["fleet"]["repositories"]["workflow_summary_generations"].workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == (2 if kind == "pending_publish" else 1)


@pytest.mark.parametrize("text,error", [("x" * 8000, "summary_output_too_large"), (" ", "summary_output_empty")], ids=["oversize", "empty"])
def test_generation_rejects_unpublishable_complete_version_before_offering_candidate(tmp_path, text, error):
    app, client, platform, values = generation_http(tmp_path)
    url = "/api/platform/v1/workflow-summary-generations"
    item = client.post(url, json=values, headers={"Origin": "http://localhost"}).get_json()["generation"]
    platform.set_run_state(OWNER, item["run_id"], "succeeded", now=102, result_text=text)
    candidate = client.get(url + "/run", query_string={"run_id": item["run_id"]}).get_json()["generation"]
    assert candidate["candidate"] is None and candidate["error_code"] == error
    response = client.post(url + "/publish", json={"run_id": item["run_id"]}, headers={"Origin": "http://localhost"})
    assert response.status_code == (413 if error == "summary_output_too_large" else 409)
    assert response.get_json()["error"] == error


def test_generation_original_token_survives_disabled_model_and_conflicts_on_payload_change(tmp_path):
    from hub.infrastructure.workflow_repository import WorkflowRepositoryError

    workflows, _, store, values = generation_setup(tmp_path)
    original = store.create(OWNER, values, now=101)
    with workflows.transaction() as connection:
        connection.execute("UPDATE model_profiles SET enabled=0")
        connection.execute("UPDATE workspaces SET enabled=0")
    assert store.create(OWNER, values, now=500) == original
    with pytest.raises(WorkflowRepositoryError, match="idempotency_conflict"):
        store.create(OWNER, {**values, "title": "changed"}, now=501)


@pytest.mark.parametrize("change", ["no_guard", "config", "model_disabled", "source_archived"])
def test_generation_invalid_binding_blocks_provider_factory(tmp_path, change):
    workflows, platform, store, values = generation_setup(tmp_path)
    item = store.create(OWNER, values, now=101)
    if change == "source_archived": platform.set_conversation_archived(OWNER, "source-chat", archived=True, now=102)
    with workflows.transaction() as connection:
        if change == "model_disabled": connection.execute("UPDATE model_profiles SET enabled=0")
        if change == "config":
            config = platform.get_run(OWNER, item["run_id"])["config_snapshot"]
            config["provider_snapshot"]["model"] = "forged"
            connection.execute("UPDATE runs SET config_snapshot=? WHERE run_id=?", (canonical_json(config).decode(), item["run_id"]))
    calls = []
    worker = generation_worker(platform, store, calls, guard=change != "no_guard")
    worker.provider_factory = lambda profile: calls.append(profile)
    assert worker.run_once(OWNER)["state"] == "failed"
    assert calls == []


def test_generation_fixes_actual_provider_model_after_catalog_edit(tmp_path):
    _, platform, store, values = generation_setup(tmp_path)
    store.create(OWNER, values, now=101)
    platform.upsert_model(OWNER, {"profile_id": "model-a", "provider": "deterministic", "model": "changed-model"})
    calls, profiles = [], []
    worker = generation_worker(platform, store, calls)
    factory = worker.provider_factory
    def capture(profile):
        profiles.append(profile)
        return factory(profile)
    worker.provider_factory = capture
    assert worker.run_once(OWNER)["state"] == "succeeded"
    assert profiles[0]["model"] == "frozen-model" and len(calls) == 1


def test_generation_does_not_repeat_upstream_request_with_catalog_retry_setting(tmp_path):
    from hub.platform.providers.openai_compatible import ProviderFactory
    from test_platform_provider import Broker, FakeTransport, _response

    _, platform, store, values = generation_setup(tmp_path)
    platform.upsert_model(OWNER, {"profile_id": "model-a", "provider": "openai_compatible", "model": "summary",
        "secret_ref": "env://TEST_PROVIDER_KEY", "provider_config": {"endpoint": "https://llm.example.test", "max_retries": 1}})
    store.create(OWNER, values, now=101)
    transport = FakeTransport([_response({}, status=503), _response({"choices": [{"message": {"content": "duplicate success"}}]})])
    worker = generation_worker(platform, store, [])
    worker.provider_factory = ProviderFactory(secret_broker=Broker({"env://TEST_PROVIDER_KEY": "test"}), transport=transport)
    worker.run_once(OWNER)
    assert len(transport.calls) == 1, "A model-summary Run cannot silently resend an upstream request"


def test_generation_provider_tool_request_never_writes_or_requests_again(tmp_path):
    from hub.platform.providers.base import ModelResponse

    _, platform, store, values = generation_setup(tmp_path)
    store.create(OWNER, values, now=101)
    calls = []
    class ToolProvider:
        def complete(self, messages, tools, *, request_observer=None):
            calls.append((messages, tools))
            return ModelResponse(kind="tool_call", tool="workspace.write", arguments={"path": "forbidden.txt", "content": "wrong"})
    worker = generation_worker(platform, store, [])
    worker.provider_factory = lambda _: ToolProvider()
    assert worker.run_once(OWNER)["state"] == "failed"
    assert len(calls) == 1 and calls[0][1] == [] and not (tmp_path / "forbidden.txt").exists()


@pytest.mark.parametrize("phase", ["provider_pending", "response"])
def test_generation_original_checkpoint_recovers_without_duplicate_model_request(tmp_path, phase, monkeypatch):
    from test_workflow_dispatch import SimulatedCrash

    _, platform, store, values = generation_setup(tmp_path)
    item = store.create(OWNER, values, now=101)
    calls = []
    worker = generation_worker(platform, store, calls)
    original = platform.save_runtime_checkpoint
    def crash(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[2]["phase"] == phase: raise SimulatedCrash()
        return result
    monkeypatch.setattr(platform, "save_runtime_checkpoint", crash)
    with pytest.raises(SimulatedCrash): worker.run_once(OWNER)
    monkeypatch.setattr(platform, "save_runtime_checkpoint", original)
    worker.clock = lambda: 500
    recovered = worker.run_once(OWNER)
    assert recovered["state"] == ("unknown" if phase == "provider_pending" else "succeeded")
    assert len(calls) == (0 if phase == "provider_pending" else 1)
    assert store.get(OWNER, item["run_id"])["state"] == recovered["state"]


@pytest.mark.parametrize("mutate", ["manual_publish", "disable"])
def test_generation_publication_preserves_concurrent_operator_head(tmp_path, mutate):
    from hub.infrastructure.workflow_repository import WorkflowRepositoryError
    from hub.infrastructure.workflow_summary_repository import WorkflowSummaryRepository

    workflows, platform, store, values = generation_setup(tmp_path)
    summaries = WorkflowSummaryRepository(workflows)
    publish(summaries, content="original", epoch=0)
    item = store.create(OWNER, {**values, "expected_epoch": 1}, now=101)
    platform.set_run_state(OWNER, item["run_id"], "succeeded", now=102, result_text="model")
    if mutate == "manual_publish": publish(summaries, content="new operator", epoch=1, token="operator-new")
    else: summaries.set_enabled(OWNER, WORKSPACE, "summary-a", enabled=False, expected_epoch=1)
    with pytest.raises(WorkflowRepositoryError, match="revision_conflict"):
        summaries.publish_generated(OWNER, item["run_id"], now=103)
    assert summaries.head(OWNER, WORKSPACE, "summary-a")["epoch"] == 2


def publish_generated_input(summaries):
    from hub.infrastructure.platform_db import PlatformRepository
    from hub.infrastructure.workflow_summary_generation_repository import WorkflowSummaryGenerationRepository

    platform = PlatformRepository(summaries.workflows.db_path)
    platform.upsert_model(OWNER, {"profile_id": "generation-model", "provider": "deterministic", "model": "summary"})
    platform.create_conversation(OWNER, "generation-source", title="generation source", workspace_id=WORKSPACE)
    platform.append_turn(OWNER, "generation-source", "generation-message", "generation-source-run", text="complete input",
        client_token="generation-source-token", config_snapshot={"workspace_id": WORKSPACE}, now=99)
    platform.set_run_state(OWNER, "generation-source-run", "succeeded", now=100, result_text="complete output")
    generation = WorkflowSummaryGenerationRepository(summaries.workflows).create(OWNER,
        {"workspace_id": WORKSPACE, "summary_id": "summary-a", "title": "generated", "expected_epoch": 0,
            "model_profile_id": "generation-model", "source_run_ids": ["generation-source-run"], "client_token": "generation-token"}, now=100)
    platform.set_run_state(OWNER, generation["run_id"], "succeeded", now=100, result_text="完整模型摘要\n")
    return summaries.publish_generated(OWNER, generation["run_id"], now=100)


@pytest.mark.parametrize("kind", ["native", "external"])
def test_generated_version_reaches_original_native_and_external_consumer(tmp_path, kind):
    from test_workflow_summary_inputs import native_setup, external_setup

    if kind == "native":
        setup, _, expected = native_setup(tmp_path, summary_publisher=publish_generated_input)
        binding = setup.dispatch().binding
        captured = []
        assert setup.worker(captured).run_once(OWNER)["state"] == "succeeded"
        value = setup.platform.get_run(OWNER, binding.run_id)["config_snapshot"]["workflow_memory"]
    else:
        setup, executor, _, expected = external_setup(tmp_path, summary_publisher=publish_generated_input)
        dispatched = setup.dispatch(executor)
        assert len(setup.transport.sends) == 1
        pin = setup.store.lookup(dispatched.binding.token, setup.resolved.digest, setup.resolved, setup.target)
        assert pin.request.prompt in setup.transport.sends[0]["prompt"]
        value = json.loads(pin.request.prompt)
    version = value["summary_inputs"][0]["item"]
    assert version["content"] == expected.content and version["schema_version"] == 2
    assert version["generation"] == expected.generation.value


def test_generation_history_validates_tuple_connection_and_corruption_after_row_128(tmp_path):
    from hub.infrastructure.workflow_repository import WorkflowRepositoryError

    workflows, _, store, values = generation_setup(tmp_path)
    for index in range(129):
        store.create(OWNER, {**values, "client_token": f"generation-{index}"}, now=101)
    with sqlite3.connect(workflows.db_path) as connection:
        assert connection.row_factory is None
        store.validate_snapshot(connection)
        connection.execute("UPDATE workflow_summary_generations SET config_sha256=? WHERE rowid=129", ("0" * 64,))
        with pytest.raises((WorkflowRepositoryError, WorkflowValidationError)):
            store.validate_snapshot(connection)


def test_generation_http_owner_isolation_and_machine_credentials_never_reveal_sources(tmp_path):
    _, client, _, values = generation_http(tmp_path)
    url = "/api/platform/v1/workflow-summary-generations"
    item = client.post(url, json=values, headers={"Origin": "http://localhost"}).get_json()["generation"]
    foreign = {"Cf-Access-Authenticated-User-Email": "other@example.test", "Origin": "http://localhost"}
    for path, query in (("/run", {"run_id": item["run_id"]}), ("/token", {"client_token": values["client_token"]})):
        assert client.get(url + path, query_string=query, headers=foreign).status_code == 404
    assert client.post(url + "/publish", json={"run_id": item["run_id"]}, headers=foreign).status_code == 404
    assert client.post(url, json=values, headers=foreign).status_code == 404
    assert client.post(url, json={**values, "client_token": "cross-site"}, headers={"Origin": "https://evil.test"}).status_code == 403
    assert client.get(url + "/run", query_string={"run_id": item["run_id"]},
        headers={"Authorization": "Bearer test", "Cf-Access-Authenticated-User-Email": OWNER}).status_code == 401
