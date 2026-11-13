"""Native manifests initialize through the real create-only operator CLI."""
from __future__ import annotations

from copy import deepcopy
import json
import sqlite3
import subprocess
import sys

import pytest

from hub import web
from hub.infrastructure.platform_db import PlatformRepository
from test_workflow_native_startup import manifest
from test_workflow_repository import OWNER, WORKSPACE
from test_workflow_setup import ROOT, invoke
from test_workflow_startup import registration


def native_command(root, *, steps=1, shared=False):
    value = manifest()
    row = value["native"][0]
    for index in range(1, steps):
        step = deepcopy(row["definition"]["steps"][0])
        step.update(step_key=f"step-{index}", requires=[row["definition"]["steps"][-1]["step_key"]])
        row["definition"]["steps"].append(step)
        binding = deepcopy(row["bindings"][0])
        binding.update(step_key=step["step_key"], target_key=f"target-{index}",
            conversation_id="native-chat" if shared else f"chat-{index}")
        row["bindings"].append(binding)
    path = root / "native.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    db = root / "store" / "platform.db"
    command = [sys.executable, "-m", "hub.workflow_setup", "--database", str(db),
        "--registration", str(path), "--owner", OWNER, "--workspace", WORKSPACE, "--root", str(root)]
    return command, db, path, value


@pytest.mark.parametrize("steps,shared,count", [(1, False, 1), (8, False, 8), (3, True, 1)])
def test_native_setup_preview_apply_idempotence_and_normal_hub_load(tmp_path, monkeypatch, steps, shared, count):
    command, db, path, _ = native_command(tmp_path, steps=steps, shared=shared)
    preview = invoke(command)
    assert preview.returncode == 0, preview.stdout + preview.stderr
    assert json.loads(preview.stdout)["create"] == {"workspaces": 1, "conversations": count}
    assert not db.parent.exists()
    first = invoke(command, "--apply")
    assert first.returncode == 0, first.stdout + first.stderr
    second = invoke(command, "--apply")
    assert second.returncode == 0, second.stdout + second.stderr
    assert json.loads(second.stdout)["existing"] == {"workspaces": 1, "conversations": count}
    assert json.loads(second.stdout)["create"] == {"workspaces": 0, "conversations": 0}
    platform = PlatformRepository(db)
    platform.upsert_model(OWNER, {"profile_id": "local", "provider": "deterministic", "model": "local"})
    monkeypatch.setattr(web, "FLEET_HOME", tmp_path)
    app = web.make_app(require_token=False, dev_operator=OWNER, serve_frontend=False,
        platform_enabled=True, platform_db=db, platform_workflows_enabled=True,
        platform_workflow_registration_file=path)
    response = app.test_client().get("/api/platform/v1/workflow-templates")
    assert response.status_code == 200
    assert [item["template_id"] for item in response.get_json()["templates"]] == ["native-input"]
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM conversations").fetchone()[0] == count
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def test_native_setup_explicit_template_and_legacy_mixed_selection(tmp_path):
    command, db, path, value = native_command(tmp_path)
    value["coding_delivery"] = registration(OWNER, WORKSPACE)["coding_delivery"]
    second = deepcopy(value["native"][0])
    second["definition"]["template_id"] = "second-native"
    second["bindings"][0].update(target_key="second-target", conversation_id="second-chat")
    value["native"].append(second)
    path.write_text(json.dumps(value), encoding="utf-8")
    for selector, count in [((), 3), (("--template", "native-input"), 1), (("--template", "second-native"), 1),
                            (("--template", "coding-delivery"), 3)]:
        result = invoke(command, *selector)
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout)["create"]["conversations"] == count
    assert not db.parent.exists()


@pytest.mark.parametrize("mutation", ["ambiguous", "missing", "extra_field", "missing_binding", "duplicate_binding",
    "invalid_mode", "provider", "unsupported_step", "unknown_template", "bad_conversation", "duplicate_target",
    "oversized_target", "invalid_profile"])
def test_native_setup_invalid_selection_or_registration_never_creates_store(tmp_path, mutation):
    command, db, path, value = native_command(tmp_path, steps=2)
    row = value["native"][0]
    if mutation == "ambiguous":
        value["native"].append(deepcopy(row))
    elif mutation == "missing":
        row["owner_id"] = "other@example.test"
    elif mutation == "extra_field":
        row["extra"] = True
    elif mutation == "missing_binding":
        row["bindings"].pop()
    elif mutation == "duplicate_binding":
        row["bindings"][1] = deepcopy(row["bindings"][0])
    elif mutation == "invalid_mode":
        row["bindings"][0]["input_mode"] = "anything"
    elif mutation == "provider":
        row["bindings"][0]["provider_snapshot"] = {}
    elif mutation == "unsupported_step":
        row["definition"]["steps"][0]["kind"] = "coding_task"
    elif mutation == "unknown_template":
        command.extend(["--template", "missing"])
    elif mutation == "bad_conversation":
        row["bindings"][0]["conversation_id"] = "bad/id"
    elif mutation == "duplicate_target":
        row["bindings"][1]["target_key"] = row["bindings"][0]["target_key"]
    elif mutation == "oversized_target":
        row["bindings"][0]["target_key"] = "a" * 129
    else:
        row["bindings"][0]["model_profile_id"] = "bad/profile"
    path.write_text(json.dumps(value), encoding="utf-8")
    rejected = invoke(command, "--apply")
    assert rejected.returncode == 2, rejected.stdout + rejected.stderr
    assert not db.parent.exists()


def test_native_setup_coding_template_name_uses_actual_manifest_entries(tmp_path):
    command, db, path, value = native_command(tmp_path)
    value["native"][0]["definition"]["template_id"] = "coding-delivery"
    path.write_text(json.dumps(value), encoding="utf-8")
    result = invoke(command, "--template", "coding-delivery")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["create"]["conversations"] == 1
    value["coding_delivery"] = registration(OWNER, WORKSPACE)["coding_delivery"]
    path.write_text(json.dumps(value), encoding="utf-8")
    rejected = invoke(command, "--template", "coding-delivery", "--apply")
    assert rejected.returncode == 2
    assert not db.parent.exists()


def test_native_setup_insert_failure_rolls_back_all_new_bindings(tmp_path):
    command, db, _, _ = native_command(tmp_path, steps=8)
    PlatformRepository(db).init()
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TRIGGER fixture_reject_last BEFORE INSERT ON conversations "
            "WHEN NEW.conversation_id='chat-7' BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
    rejected = invoke(command, "--apply")
    assert rejected.returncode == 2
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM workspaces").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM conversations").fetchone()[0] == 0


def test_native_setup_existing_store_preview_opens_no_writer(tmp_path, monkeypatch):
    from hub.infrastructure.workflow_setup import prepare_workflow_conversations

    _, db, _, _ = native_command(tmp_path)
    repository = PlatformRepository(db)
    repository.init()
    before = db.read_bytes()

    def no_writer():
        pytest.fail("preview must not initialize schema or open a writer")

    monkeypatch.setattr(repository, "init", no_writer)
    monkeypatch.setattr(repository, "_connect", no_writer)
    result = prepare_workflow_conversations(repository, owner_id=OWNER, workspace_id=WORKSPACE,
        root_path=tmp_path, conversations=(("first", "native-chat"), ("second", "native-chat")))
    assert result["create"] == {"workspaces": 1, "conversations": 1}
    assert result["existing"] == {"workspaces": 0, "conversations": 0}
    assert db.read_bytes() == before


def test_native_setup_existing_conversation_preserves_data_and_conflict_is_atomic(tmp_path):
    command, db, _, _ = native_command(tmp_path, steps=2)
    repository = PlatformRepository(db)
    repository.init()
    repository.upsert_workspace(OWNER, {"workspace_id": WORKSPACE, "root_path": str(tmp_path)})
    repository.create_conversation(OWNER, "native-chat", title="User title", workspace_id=WORKSPACE,
        overrides={"temperature": 0.2})
    before = repository.get_conversation_metadata(OWNER, "native-chat")
    applied = invoke(command, "--apply")
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert repository.get_conversation_metadata(OWNER, "native-chat") == before
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE conversations SET archived_at=1 WHERE conversation_id='chat-1'")
        rows = connection.execute("SELECT * FROM conversations ORDER BY conversation_id").fetchall()
    rejected = invoke(command, "--apply")
    assert rejected.returncode == 2
    assert json.loads(rejected.stdout)["error"] == "workflow_setup_conversation_conflict"
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT * FROM conversations ORDER BY conversation_id").fetchall() == rows


def test_native_setup_concurrent_apply_creates_each_binding_once(tmp_path):
    command, db, _, _ = native_command(tmp_path, steps=8)
    processes = [subprocess.Popen([*command, "--apply"], cwd=ROOT, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True) for _ in range(2)]
    reports = []
    try:
        for process in processes:
            out, err = process.communicate(timeout=20)
            assert process.returncode == 0, out + err
            reports.append(json.loads(out))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
    assert sum(r["create"]["workspaces"] for r in reports) == 1
    assert sum(r["create"]["conversations"] for r in reports) == 8
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM conversations").fetchone()[0] == 8
