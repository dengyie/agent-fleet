"""Repeatable operator bootstrap uses the original workspace/conversation store."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from hub import web
from hub.infrastructure.platform_db import PlatformRepository
from hub.infrastructure.workflow_setup import prepare_workflow_workspace
from test_workflow_repository import OWNER, WORKSPACE
from test_workflow_startup import registration

ROOT = Path(__file__).resolve().parents[1]


def setup_command(root: Path, *, owner=OWNER, workspace=WORKSPACE):
    manifest = root / "registration.json"
    manifest.write_text(json.dumps(registration(owner, workspace)), encoding="utf-8")
    db = root / "state" / "platform.db"
    return [sys.executable, "-m", "hub.workflow_setup", "--database", str(db),
        "--registration", str(manifest), "--owner", owner, "--workspace", workspace,
        "--root", str(root)], db


def invoke(command, *extra):
    return subprocess.run([*command, *extra], cwd=ROOT, capture_output=True, text=True, timeout=20)


def test_workflow_setup_cli_preview_leaves_missing_store_absent(tmp_path: Path) -> None:
    command, db = setup_command(tmp_path)
    result = invoke(command)
    assert result.returncode == 0, result.stderr + result.stdout
    report = json.loads(result.stdout)
    assert report["mode"] == "preview" and report["create"] == {"workspaces": 1, "conversations": 3}
    assert not db.parent.exists()


def test_workflow_setup_cli_applies_original_bindings_and_repeats_without_updates(tmp_path: Path) -> None:
    command, db = setup_command(tmp_path)
    first = invoke(command, "--apply")
    assert first.returncode == 0, first.stderr + first.stdout
    repository = PlatformRepository(db)
    assert repository.get_workspace(OWNER, WORKSPACE)["root_path"] == str(tmp_path.resolve())
    for phase in ("develop", "review", "test"):
        assert repository.get_conversation_metadata(OWNER, phase + "-chat")["workspace_id"] == WORKSPACE
    second = invoke(command, "--apply")
    assert second.returncode == 0, second.stderr + second.stdout
    assert json.loads(second.stdout)["create"] == {"workspaces": 0, "conversations": 0}
    assert json.loads(second.stdout)["existing"] == {"workspaces": 1, "conversations": 3}


def test_workflow_setup_existing_labels_overrides_and_history_are_preserved(tmp_path: Path) -> None:
    command, db = setup_command(tmp_path)
    repository = PlatformRepository(db)
    repository.init()
    repository.upsert_workspace(OWNER, {"workspace_id": WORKSPACE, "name": "Existing user workspace",
        "root_path": str(tmp_path.resolve()), "default_node_id": "existing-node"})
    repository.create_conversation(OWNER, "develop-chat", title="User title", workspace_id=WORKSPACE,
        overrides={"temperature": 0.3})
    before_workspace = repository.get_workspace(OWNER, WORKSPACE)
    before_conversation = repository.get_conversation_metadata(OWNER, "develop-chat")
    with sqlite3.connect(db) as connection:
        connection.execute("INSERT INTO messages(message_id,owner_id,conversation_id,role,content,client_token,turn_sequence,content_ref,created_at) "
            "VALUES(?,?,?,?,?,?,1,'','2026-11-11T00:00:00Z')", ("message-a", OWNER, "develop-chat", "user", "Original history", "history-a"))
    preview = invoke(command)
    assert preview.returncode == 0, preview.stdout + preview.stderr
    assert json.loads(preview.stdout)["create"] == {"workspaces": 0, "conversations": 2}
    assert repository.get_conversation_metadata(OWNER, "review-chat") is None
    applied = invoke(command, "--apply")
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert repository.get_workspace(OWNER, WORKSPACE) == before_workspace
    assert repository.get_conversation_metadata(OWNER, "develop-chat") == before_conversation
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT content FROM messages WHERE message_id='message-a'").fetchone()[0] == "Original history"


@pytest.mark.parametrize("conflict", ["owner", "workspace", "archived", "root", "disabled", "backend"])
def test_workflow_setup_conflicts_leave_all_original_bindings_unchanged(tmp_path: Path, conflict: str) -> None:
    command, db = setup_command(tmp_path)
    repository = PlatformRepository(db)
    repository.init()
    other_owner = "other@example.test" if conflict == "owner" else OWNER
    other_workspace = "other-workspace" if conflict == "workspace" else WORKSPACE
    repository.upsert_workspace(other_owner, {"workspace_id": other_workspace, "root_path": str(tmp_path.resolve())})
    if conflict in {"owner", "workspace", "archived"}:
        repository.create_conversation(other_owner, "test-chat", title="Original", workspace_id=other_workspace)
    with sqlite3.connect(db) as connection:
        if conflict == "archived":
            connection.execute("UPDATE conversations SET archived_at=1 WHERE conversation_id='test-chat'")
        elif conflict == "root":
            connection.execute("UPDATE workspaces SET root_path=?", (str(tmp_path / "other-root"),))
        elif conflict == "disabled":
            connection.execute("UPDATE workspaces SET enabled=0")
        elif conflict == "backend":
            connection.execute("UPDATE workspaces SET backend='external'")
    with sqlite3.connect(db) as connection:
        before = {t: connection.execute("SELECT * FROM " + t).fetchall() for t in ("workspaces", "conversations")}
    for extra in ((), ("--apply",)):
        rejected = invoke(command, *extra)
        assert rejected.returncode == 2, rejected.stdout + rejected.stderr
        assert json.loads(rejected.stdout)["error"] in {"workflow_setup_conversation_conflict", "workflow_setup_workspace_conflict"}
        with sqlite3.connect(db) as connection:
            assert {t: connection.execute("SELECT * FROM " + t).fetchall() for t in before} == before


def test_workflow_setup_mid_write_failure_rolls_back_workspace_and_conversations(tmp_path: Path) -> None:
    command, db = setup_command(tmp_path)
    repository = PlatformRepository(db)
    repository.init()
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TRIGGER fixture_reject_test BEFORE INSERT ON conversations "
            "WHEN NEW.conversation_id='test-chat' BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
    failed = invoke(command, "--apply")
    assert failed.returncode == 2, failed.stderr + failed.stdout
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0


@pytest.mark.parametrize("initialized", [False, True])
def test_workflow_setup_concurrent_processes_create_once_then_original_hub_loads(tmp_path: Path, monkeypatch, initialized: bool) -> None:
    command, db = setup_command(tmp_path)
    if initialized:
        PlatformRepository(db).init()
    processes = [subprocess.Popen([*command, "--apply"], cwd=ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    reports = []
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, stdout + stderr
            reports.append(json.loads(stdout))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
    assert sum(r["create"]["workspaces"] for r in reports) == 1
    assert sum(r["create"]["conversations"] for r in reports) == 3
    monkeypatch.setattr(web, "FLEET_HOME", tmp_path)
    app = web.make_app(require_token=False, dev_operator=OWNER, serve_frontend=False,
        platform_enabled=True, platform_db=db, platform_workflows_enabled=True,
        platform_workflow_registration_file=tmp_path / "registration.json")
    templates = app.test_client().get("/api/platform/v1/workflow-templates").get_json()["templates"]
    assert [item["template_id"] for item in templates] == ["coding-delivery"]


@pytest.mark.parametrize("mutation", ["missing_selection", "duplicate_selection", "duplicate_conversation", "relative_root", "missing_root", "relative_database"])
def test_workflow_setup_invalid_manifest_or_paths_do_not_create_store(tmp_path: Path, mutation: str) -> None:
    command, db = setup_command(tmp_path)
    path = tmp_path / "registration.json"
    manifest = json.loads(path.read_text())
    if mutation == "missing_selection":
        manifest["coding_delivery"][0]["owner_id"] = "other@example.test"
    elif mutation == "duplicate_selection":
        manifest["coding_delivery"].append(manifest["coding_delivery"][0])
    elif mutation == "duplicate_conversation":
        manifest["coding_delivery"][0]["test"]["conversation_id"] = "develop-chat"
    elif mutation == "relative_root":
        command[-1] = "."
    elif mutation == "missing_root":
        command[-1] = str(tmp_path / "missing")
    else:
        command[command.index("--database") + 1] = "relative.db"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    rejected = invoke(command, "--apply")
    assert rejected.returncode == 2, rejected.stdout + rejected.stderr
    assert not db.parent.exists()


def test_workflow_setup_preview_uses_readonly_connection_and_no_schema_init(tmp_path: Path, monkeypatch) -> None:
    _, db = setup_command(tmp_path)
    repository = PlatformRepository(db)
    repository.init()
    before = db.read_bytes()

    def no_write():
        pytest.fail("preview must not initialize or open a writer connection")

    monkeypatch.setattr(repository, "init", no_write)
    monkeypatch.setattr(repository, "_connect", no_write)
    assert prepare_workflow_workspace(repository, owner_id=OWNER, workspace_id=WORKSPACE,
        root_path=tmp_path, conversation_ids=("develop-chat", "review-chat", "test-chat"))["create"]["conversations"] == 3
    assert db.read_bytes() == before


@pytest.mark.parametrize("version", [9999, "not-a-version"])
def test_workflow_setup_future_or_corrupt_store_is_rejected_without_content_changes(tmp_path: Path, version) -> None:
    command, db = setup_command(tmp_path)
    repository = PlatformRepository(db)
    repository.init()
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(version),))
    for flags in ((), ("--apply",)):
        response = invoke(command, *flags)
        assert response.returncode == 2, response.stdout + response.stderr
        with sqlite3.connect(db) as connection:
            assert connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(version)
            assert connection.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0


def test_workflow_setup_rejects_owner_that_workflow_registration_cannot_load(tmp_path: Path) -> None:
    command, db = setup_command(tmp_path, owner="a" * 129)
    rejected = invoke(command, "--apply")
    assert rejected.returncode == 2, rejected.stdout + rejected.stderr
    assert not db.parent.exists()
