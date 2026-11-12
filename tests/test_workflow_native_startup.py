"""Native inputs from trusted files through normal Hub, HTTP, worker and restart."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
import json
import sqlite3
import time

import pytest

from hub import web
from hub.application.defaults_service import DefaultsService
from hub.domain.workflow_contract_types import CatalogRef
from hub.domain.workflow_identity import canonical_json
from hub.infrastructure.platform_db import PlatformRepository
from hub.infrastructure.workflow_run_executor import RegisteredRunTarget
from hub.platform.providers.base import ModelResponse
from test_workflow_repository import OWNER, WORKSPACE, registered
from test_workflow_startup import registration, seed

ORIGIN = {"Origin": "http://localhost"}


def manifest(mode="text"):
    definition = json.loads(registered().snapshot)
    definition["template_id"] = "native-input"
    definition["steps"][0]["kind"] = "native_run"
    definition["steps"][0]["contract"]["output_specs"] = []
    return {"schema_version": 1, "native": [{
        "owner_id": OWNER, "workspace_id": WORKSPACE, "definition": definition,
        "bindings": [{"step_key": "develop", "target_key": "native-a",
            "conversation_id": "native-chat", "model_profile_id": "local",
            "provider_snapshot": {"provider": "deterministic", "model": "local",
                "provider_config": {}, "capabilities": {"image_input": True, "audio_input": True},
                "secret_ref": "env://NATIVE_A"}, "input_mode": mode}],
    }]}


def prepare(tmp_path, monkeypatch, *, mode="text", dispatch=False, coding=False):
    monkeypatch.setattr(web, "FLEET_HOME", tmp_path)
    root = tmp_path / "workspace"
    root.mkdir()
    db = tmp_path / "platform.db"
    platform = PlatformRepository(db)
    seed(platform, root)
    platform.create_conversation(OWNER, "native-chat", title="Native", workspace_id=WORKSPACE)
    platform.upsert_model(OWNER, {"profile_id": "local", "provider": "deterministic", "model": "local",
        "secret_ref": "env://NATIVE_A", "capabilities": {"image_input": True, "audio_input": True}})
    value = manifest(mode)
    if coding:
        value["coding_delivery"] = registration(OWNER, WORKSPACE)["coding_delivery"]
    path = tmp_path / "workflow.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    options = dict(require_token=False, dev_operator=OWNER, serve_frontend=False,
        platform_enabled=True, platform_db=db, platform_workflows_enabled=True,
        platform_worker_enabled=True, platform_workflow_dispatch_enabled=dispatch,
        platform_workflow_registration_file=path)
    return platform, path, value, options


def startup(options):
    return web.make_app(**options)


def tick(app, *, attempts=4):
    coordinator = app.extensions["fleet"]["services"]["workflow_coordinator"]
    coordinator.interval_seconds = 0.1
    for _ in range(attempts):
        result = asyncio.run(coordinator.tick(coordinator.clock()))
        assert not result.failures, result.failures
        time.sleep(0.11)


@pytest.mark.parametrize("mode", ["text", "code", "text_code", "media"])
def test_normal_provider_snapshot_is_accepted_by_native_target(tmp_path, mode):
    platform = PlatformRepository(tmp_path / "platform.db")
    seed(platform, tmp_path)
    platform.upsert_model(OWNER, {"profile_id": "local", "provider": "deterministic", "model": "local",
        "secret_ref": "env://NATIVE_A", "capabilities": {"image_input": True}})
    snapshot = DefaultsService(platform).resolve_run_config(OWNER,
        overrides={"workspace_id": WORKSPACE, "model_profile_id": "local"})
    target = RegisteredRunTarget(OWNER, WORKSPACE, "native-a", CatalogRef("native", 1), CatalogRef("safe", 1),
        "develop-chat", canonical_json(snapshot), inline_text_inputs=mode in ("text", "text_code"),
        inline_code_inputs=mode in ("code", "text_code"), inline_media_inputs=mode == "media")
    assert json.loads(target.config_snapshot)["provider_snapshot"]["secret_ref"] == "env://NATIVE_A"
    old = json.loads(target.config_snapshot)
    old["provider_snapshot"].pop("secret_ref")
    assert replace(target, config_snapshot=canonical_json(old)).config_snapshot == canonical_json(old)
    for invalid in (False, 12, {}, "", " " * 2, "x" * 257):
        broken = json.loads(target.config_snapshot)
        broken["provider_snapshot"]["secret_ref"] = invalid
        with pytest.raises(ValueError):
            replace(target, config_snapshot=canonical_json(broken))


@pytest.mark.parametrize(("mode", "content_type", "raw", "part"), [
    ("text", "text/plain", "部署文本 🥭".encode(), "text"),
    ("media", "image/png", b"\x89PNG\r\n\x1a\nimage", "image_url"),
    ("media", "audio/wav", b"RIFF\x04\x00\x00\x00WAVE", "input_audio"),
])
def test_file_registered_native_consumes_inputs_and_restores_original_run(
        tmp_path, monkeypatch, mode, content_type, raw, part):
    platform, path, value, options = prepare(tmp_path, monkeypatch, mode=mode, dispatch=True)
    app = startup(options)
    services = app.extensions["fleet"]["services"]
    target = services["workflow_coordinator"].dispatchers[(OWNER, WORKSPACE, "native-a")].executor.target
    assert target.hub_input_target().kind == "native_run"
    source = tmp_path / "input"
    source.write_bytes(raw)
    artifact = services["platform_artifacts"].put_file(OWNER, WORKSPACE, source, content_type=content_type)
    reference = {"ref_key": "input", "kind": "artifact",
        **{key: artifact[key] for key in ("artifact_id", "sha256", "size")}}
    value["native"][0]["definition"]["steps"][0]["contract"]["input_refs"] = [reference]
    path.write_text(json.dumps(value), encoding="utf-8")
    app = startup(options)
    client = app.test_client()
    templates = client.get("/api/platform/v1/workflow-templates").get_json()["templates"]
    assert templates[0]["input_refs"] == [reference]
    body = {"template_id": "native-input", "revision": 1, "workspace_id": WORKSPACE,
        "input_refs": [reference], "client_token": "native-startup", "expected_policy_revision": 1}
    response = client.post("/api/platform/v1/workflows", json=body, headers=ORIGIN)
    assert response.status_code == 201, response.get_json()
    workflow_id = response.get_json()["workflow"]["workflow_id"]
    tick(app)
    workflows = app.extensions["fleet"]["repositories"]["workflows"]
    binding = workflows.get_dispatch(OWNER, workflow_id, "develop", 1).binding
    run = platform.get_run(OWNER, binding.run_id)
    assert run["state"] == "queued"
    assert run["config_snapshot"]["provider_snapshot"]["secret_ref"] == "env://NATIVE_A"
    # Recreate the actual app from the same file and database, not injected executors.
    app = startup(options)
    tick(app)
    repeated = app.test_client().post("/api/platform/v1/workflows", json=body, headers=ORIGIN)
    assert repeated.status_code == 201
    assert repeated.get_json()["workflow"]["workflow_id"] == workflow_id
    captured, profiles = [], []

    class Provider:
        def complete(self, messages, tools, *, request_observer=None):
            captured.append(messages)
            return ModelResponse(kind="final", text="done")

    worker = app.extensions["fleet"]["services"]["platform_worker"]

    def factory(profile):
        profiles.append(profile)
        return Provider()

    worker.provider_factory = factory
    platform.upsert_model(OWNER, {"profile_id": "local", "provider": "deterministic", "model": "changed",
        "secret_ref": "env://NATIVE_B", "provider_config": {"endpoint": "https://changed.example.test"}})
    result = worker.run_once(OWNER)
    assert result["state"] == "succeeded", result
    assert profiles[0]["secret_ref"] == "env://NATIVE_A" and profiles[0]["model"] == "local"
    assert len(captured) == 1
    if part == "text":
        assert raw.decode() in json.dumps(captured[0], ensure_ascii=False)
    else:
        user = next(message for message in captured[0] if message["role"] == "user")
        last = user["content"][-1]
        assert last["type"] == part
        if part == "image_url":
            assert last[part]["url"].endswith(base64.b64encode(raw).decode())
        else:
            assert last[part] == {"data": base64.b64encode(raw).decode(), "format": "wav"}
    app = startup(options)
    tick(app)
    current = workflows.get(OWNER, workflow_id)
    assert (current.execution_state, current.acceptance_state) == ("succeeded", "pending")
    with workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM legacy_task_links").fetchone()[0] == 0


def test_native_startup_coexists_with_coding_and_legacy_cli_reader(tmp_path, monkeypatch):
    from hub.composition.workflow_registration import read_workflow_registration

    _, path, _, options = prepare(tmp_path, monkeypatch, coding=True)
    app = startup(options)
    templates = app.test_client().get("/api/platform/v1/workflow-templates").get_json()["templates"]
    assert {item["template_id"] for item in templates} == {"coding-delivery", "native-input"}
    assert read_workflow_registration(path) == registration(OWNER, WORKSPACE)["coding_delivery"]
    assert len(app.extensions["fleet"]["services"]["workflow_coordinator"].dispatchers) == 4


@pytest.mark.parametrize("change", ["missing_model", "disabled_model", "foreign_conversation", "missing_binding",
    "duplicate_binding", "extra_binding", "unknown_mode", "remote", "output", "kind", "extra_field", "bool_version"])
def test_native_startup_rejects_unusable_configuration(tmp_path, monkeypatch, change):
    platform, path, value, options = prepare(tmp_path, monkeypatch)
    row = value["native"][0]
    binding = row["bindings"][0]
    if change == "missing_model": binding["model_profile_id"] = "missing"
    elif change == "disabled_model": platform.upsert_model(OWNER, {"profile_id": "local", "provider": "deterministic", "model": "local", "enabled": False})
    elif change == "foreign_conversation": binding["conversation_id"] = "foreign"
    elif change == "missing_binding": row["bindings"] = []
    elif change == "duplicate_binding": row["bindings"].append(dict(binding))
    elif change == "extra_binding": row["bindings"].append({**binding, "step_key": "extra"})
    elif change == "unknown_mode": binding["input_mode"] = "video"
    elif change == "remote":
        platform.upsert_node(OWNER, {"node_id": "remote"})
        platform.upsert_workspace(OWNER, {"workspace_id": WORKSPACE, "root_path": str(tmp_path / "workspace"), "default_node_id": "remote"})
    elif change == "output": row["definition"]["steps"][0]["contract"]["output_specs"] = json.loads(registered().snapshot)["steps"][0]["contract"]["output_specs"]
    elif change == "kind": row["definition"]["steps"][0]["kind"] = "coding_task"
    elif change == "extra_field": binding["shell"] = "echo nope"
    else: value["schema_version"] = True
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        startup(options)
    with sqlite3.connect(platform.db_path) as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def test_native_manifest_is_not_loaded_when_workflows_are_disabled(tmp_path, monkeypatch):
    _, _, _, options = prepare(tmp_path, monkeypatch)
    app = startup({**options, "platform_workflows_enabled": False,
        "platform_workflow_registration_file": tmp_path / "missing.json"})
    assert app.test_client().get("/api/platform/v1/workflow-templates").status_code == 404
