import json
import sqlite3

import pytest

from hub.application.task_service import ApplicationError
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.infrastructure.platform_db import PlatformRepositoryError


def _app(tmp_path, *, memory=False, memory_context=False):
    return create_app(FleetConfig.from_root(
        tmp_path, ingest_token="ingest", dev_operator="owner@example.test",
        platform_enabled=True,
        platform_memory_enabled=memory,
        platform_memory_context_enabled=memory_context,
    ))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_conversation_rejects_non_finite_overrides_before_persistence(tmp_path, value):
    app = _app(tmp_path)
    response = app.test_client().post(
        "/api/platform/v1/conversations",
        json={"overrides": {"metric": value}},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_value"
    assert "NaN" not in response.get_data(as_text=True)
    repository = app.extensions["fleet"]["platform_repository"]
    with sqlite3.connect(repository.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0


def test_platform_repository_preserves_json_encoding_cause(tmp_path):
    app = _app(tmp_path)
    repository = app.extensions["fleet"]["platform_repository"]

    with pytest.raises(PlatformRepositoryError) as rejected:
        repository.create_conversation(
            "owner@example.test", "conv_invalid_json", title="",
            workspace_id=None, overrides={"metric": float("nan")},
        )

    assert rejected.value.code == "invalid_value"
    assert isinstance(rejected.value.__cause__, ValueError)


@pytest.mark.parametrize("overrides", [[], "", 0, False])
def test_platform_repository_rejects_non_mapping_overrides_before_writes(
    tmp_path, overrides,
):
    app = _app(tmp_path)
    repository = app.extensions["fleet"]["platform_repository"]

    with pytest.raises(PlatformRepositoryError) as rejected:
        repository.create_conversation(
            "owner@example.test", "conv_invalid_overrides", title="",
            workspace_id=None, overrides=overrides,
        )

    assert rejected.value.code == "invalid_value"
    with sqlite3.connect(repository.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0


def test_conversation_service_preserves_repository_error_chain(tmp_path):
    app = _app(tmp_path)
    service = app.extensions["fleet"]["services"]["conversations"]

    with pytest.raises(ApplicationError) as rejected:
        service.create("owner@example.test", overrides={"metric": float("nan")})

    assert rejected.value.code == "invalid_value"
    assert isinstance(rejected.value.__cause__, PlatformRepositoryError)
    assert isinstance(rejected.value.__cause__.__cause__, ValueError)


def test_conversation_rejects_invalid_unicode_override_before_persistence(tmp_path):
    app = _app(tmp_path)
    body = json.dumps(
        {"overrides": {"text": "\ud800"}}, ensure_ascii=True,
    ).encode("utf-8")
    response = app.test_client().post(
        "/api/platform/v1/conversations", data=body,
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_value"
    repository = app.extensions["fleet"]["platform_repository"]
    with sqlite3.connect(repository.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0


@pytest.mark.parametrize("overrides", [[], "", 0, False, None])
def test_conversation_rejects_falsey_non_object_overrides(tmp_path, overrides):
    app = _app(tmp_path)
    response = app.test_client().post(
        "/api/platform/v1/conversations", json={"overrides": overrides},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_value"
    repository = app.extensions["fleet"]["platform_repository"]
    with sqlite3.connect(repository.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0


@pytest.mark.parametrize("overrides", [[], "", 0, False, None])
def test_turn_rejects_falsey_non_object_overrides(tmp_path, overrides):
    app = _app(tmp_path)
    client = app.test_client()
    conversation = client.post("/api/platform/v1/conversations", json={})
    conversation_id = conversation.get_json()["conversation"]["conversation_id"]

    response = client.post(
        f"/api/platform/v1/conversations/{conversation_id}/turns",
        json={"text": "hello", "client_token": "invalid-overrides",
              "overrides": overrides},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_value"
    repository = app.extensions["fleet"]["platform_repository"]
    with sqlite3.connect(repository.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


@pytest.mark.parametrize("overrides", [[], "", 0, False])
@pytest.mark.parametrize("operation", ["create", "turn"])
def test_conversation_service_rejects_non_mapping_overrides_before_writes(
    tmp_path, operation, overrides,
):
    app = _app(tmp_path)
    service = app.extensions["fleet"]["services"]["conversations"]
    if operation == "turn":
        conversation = service.create("owner@example.test")
        conversation_id = conversation["conversation"]["conversation_id"]
        invoke = lambda: service.turn(
            "owner@example.test", conversation_id, text="hello",
            client_token="invalid-overrides", overrides=overrides,
        )
    else:
        invoke = lambda: service.create("owner@example.test", overrides=overrides)

    with pytest.raises(ApplicationError) as rejected:
        invoke()

    assert rejected.value.code == "invalid_value"
    repository = app.extensions["fleet"]["platform_repository"]
    with sqlite3.connect(repository.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        if operation == "create":
            assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0


def test_turn_is_idempotent_and_run_config_is_frozen(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    repo = app.extensions["fleet"]["services"]["platform_defaults"].repository
    repo.upsert_model("owner@example.test", {"profile_id": "model-a", "provider": "compatible", "model": "a"})
    repo.upsert_workspace("owner@example.test", {"workspace_id": "home", "root_path": str(tmp_path / "home")})
    repo.upsert_node("owner@example.test", {"node_id": "host-default"})
    defaults = client.put("/api/platform/v1/defaults", json={"defaults": {"model_profile_id": "model-a", "workspace_id": "home", "execution_node_id": "host-default"}}, headers={"If-Match": "0"})
    assert defaults.status_code == 200
    conversation = client.post("/api/platform/v1/conversations", json={"title": "Test", "workspace_id": "home"})
    assert conversation.status_code == 200
    conversation_id = conversation.get_json()["conversation"]["conversation_id"]
    first = client.post(f"/api/platform/v1/conversations/{conversation_id}/turns", json={"text": "生成报告", "client_token": "turn-1"})
    second = client.post(f"/api/platform/v1/conversations/{conversation_id}/turns", json={"text": "生成报告", "client_token": "turn-1"})
    assert first.status_code == second.status_code == 202
    assert first.get_json()["run"]["run_id"] == second.get_json()["run"]["run_id"]
    assert first.get_json()["run"]["config"]["model_profile_id"] == "model-a"
    conflict = client.post(f"/api/platform/v1/conversations/{conversation_id}/turns", json={"text": "另一内容", "client_token": "turn-1"})
    assert conflict.status_code == 409
    assert conflict.get_json()["error"] == "idempotency_conflict"


def test_run_cancel_is_explicit_and_idempotent(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    conversation_id = client.post("/api/platform/v1/conversations", json={}).get_json()["conversation"]["conversation_id"]
    created = client.post(f"/api/platform/v1/conversations/{conversation_id}/turns", json={"text": "x", "client_token": "cancel-1"}).get_json()
    run_id = created["run"]["run_id"]
    cancelled = client.post(f"/api/platform/v1/runs/{run_id}/cancel")
    again = client.post(f"/api/platform/v1/runs/{run_id}/cancel")
    assert cancelled.status_code == again.status_code == 200
    assert cancelled.get_json()["state"] == "cancelling"
    assert again.get_json()["cancel_requested"] is True


def test_run_snapshot_freezes_non_secret_provider_settings(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    repo = app.extensions["fleet"]["services"]["platform_defaults"].repository
    repo.upsert_model("owner@example.test", {
        "profile_id": "remote", "provider": "openai_compatible", "model": "gpt-a",
        "secret_ref": "env://KEY_A",
        "provider_config": {"endpoint": "https://one.example.test", "timeout_s": 10},
    })
    repo.upsert_workspace("owner@example.test", {"workspace_id": "home", "root_path": str(tmp_path / "home")})
    client.put("/api/platform/v1/defaults", json={"defaults": {"model_profile_id": "remote", "workspace_id": "home"}}, headers={"If-Match": "0"})
    conversation_id = client.post("/api/platform/v1/conversations", json={}).get_json()["conversation"]["conversation_id"]
    run = client.post(f"/api/platform/v1/conversations/{conversation_id}/turns", json={"text": "x", "client_token": "snapshot-1"}).get_json()["run"]
    profile = repo.get_model("owner@example.test", "remote")
    repo.upsert_model("owner@example.test", {
        **profile, "model": "gpt-b", "secret_ref": "env://KEY_B",
        "provider_config": {"endpoint": "https://two.example.test", "timeout_s": 20},
    })
    persisted = repo.get_run("owner@example.test", run["run_id"])
    snapshot = persisted["config_snapshot"]
    assert snapshot["provider_snapshot"]["model"] == "gpt-a"
    assert snapshot["provider_snapshot"]["provider_config"]["endpoint"] == "https://one.example.test"
    assert "secret_ref" not in snapshot
    assert "KEY_A" not in str(snapshot)


def test_memory_context_is_explicit_frozen_and_publicly_redacted(tmp_path):
    app = _app(tmp_path, memory=True, memory_context=True)
    client = app.test_client()
    repo = app.extensions["fleet"]["repositories"]["platform_memory"]
    repo.create("owner@example.test", {
        "memory_id": "memory-policy", "kind": "note",
        "title": "Policy", "content": "Use the dedicated host.",
    }, now=1)
    conversation = client.post("/api/platform/v1/conversations", json={}).get_json()["conversation"]
    response = client.post(
        f"/api/platform/v1/conversations/{conversation['conversation_id']}/turns",
        json={
            "text": "prepare a report", "client_token": "memory-turn",
            "memory_context": {
                "enabled": True, "memory_ids": ["memory-policy"],
                "max_items": 1, "max_bytes": 1024,
            },
        },
    )
    assert response.status_code == 202
    run = response.get_json()["run"]
    assert run["config"]["memory_context"] == {
        "enabled": True, "mode": "ids", "item_count": 1,
        "bytes": run["config"]["memory_context"]["bytes"],
        "max_items": 1, "max_bytes": 1024,
    }
    persisted = app.extensions["fleet"]["platform_repository"].get_run(
        "owner@example.test", run["run_id"])
    assert persisted["config_snapshot"]["memory_context"]["items"][0]["content"] == "Use the dedicated host."
    assert "Use the dedicated host." in str(persisted["config_snapshot"])
    assert "owner_id" not in str(run)

    conversation_view = client.get(
        f"/api/platform/v1/conversations/{conversation['conversation_id']}"
    )
    assert conversation_view.status_code == 200
    public_run = conversation_view.get_json()["conversation"]["runs"][0]
    assert "config_snapshot" not in public_run
    assert public_run["config"]["memory_context"] == run["config"]["memory_context"]
    assert "Use the dedicated host." not in str(public_run)

    repo.update("owner@example.test", "memory-policy", {
        "content": "Changed after enqueue.",
    }, expected_revision=0, now=2)
    again = client.post(
        f"/api/platform/v1/conversations/{conversation['conversation_id']}/turns",
        json={
            "text": "prepare a report", "client_token": "memory-turn",
            "memory_context": {
                "enabled": True, "memory_ids": ["memory-policy"],
                "max_items": 1, "max_bytes": 1024,
            },
        },
    )
    assert again.get_json()["run"]["run_id"] == run["run_id"]
    assert app.extensions["fleet"]["platform_repository"].get_run(
        "owner@example.test", run["run_id"])["config_snapshot"]["memory_context"]["items"][0]["content"] == "Use the dedicated host."


def test_memory_context_is_rejected_when_gate_is_closed(tmp_path):
    app = _app(tmp_path, memory=True, memory_context=False)
    client = app.test_client()
    conversation = client.post("/api/platform/v1/conversations", json={}).get_json()["conversation"]
    response = client.post(
        f"/api/platform/v1/conversations/{conversation['conversation_id']}/turns",
        json={
            "text": "x", "client_token": "closed-memory",
            "memory_context": {"enabled": True, "memory_ids": ["memory-policy"]},
        },
    )
    assert response.status_code == 409
    assert response.get_json()["error"] == "memory_context_disabled"


def test_conversation_inbox_is_owner_scoped_bounded_and_redacted(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    repo = app.extensions["fleet"]["services"]["platform_defaults"].repository
    first = repo.create_conversation("owner@example.test", "conv-first", title="<b>unsafe</b>", workspace_id=None)
    second = repo.create_conversation("owner@example.test", "conv-second", title="Second", workspace_id=None)
    repo.create_conversation("other@example.test", "conv-other", title="Other owner", workspace_id=None)
    repo.append_turn("owner@example.test", first["conversation_id"], "msg-first", "run-first", text="<script>alert(1)</script>", client_token="token-first", config_snapshot={}, now=1)
    repo.append_turn("owner@example.test", second["conversation_id"], "msg-second", "run-second", text="latest", client_token="token-second", config_snapshot={}, now=2)
    response = client.get("/api/platform/v1/conversations?limit=1")
    assert response.status_code == 200
    payload = response.get_json()
    assert len(payload["conversations"]) == 1
    item = payload["conversations"][0]
    assert item["conversation_id"] == "conv-second"
    assert item["last_message_preview"] == "latest"
    assert "client_token" not in item
    assert "messages" not in item
    assert "overrides" not in item
    assert "owner_id" not in item
    assert "conv-other" not in str(payload)
    assert client.get("/api/platform/v1/conversations?limit=bad").status_code == 400

def test_conversation_lifecycle_renames_archives_restores_and_deletes_atomically(tmp_path):
    from hub.infrastructure.browser_repository import BrowserRepository
    from hub.infrastructure.execution_window_repository import ExecutionWindowRepository

    app = _app(tmp_path)
    client = app.test_client()
    repository = app.extensions["fleet"]["platform_repository"]
    events = app.extensions["fleet"]["services"]["run_events"]
    created = client.post("/api/platform/v1/conversations", json={"title": "Original"})
    conversation_id = created.get_json()["conversation"]["conversation_id"]
    turn = client.post(
        f"/api/platform/v1/conversations/{conversation_id}/turns",
        json={"text": "keep this history", "client_token": "lifecycle"},
    ).get_json()
    run_id = turn["run"]["run_id"]
    events.append("owner@example.test", run_id, "run_started", {"state": "running"})
    windows = ExecutionWindowRepository(repository.db_path)
    windows.init()
    window = windows.create_window("owner@example.test", run_id)["window"]
    browser = BrowserRepository(repository.db_path)
    browser.init()
    session = browser.create_session(
        "owner@example.test", workspace_id="workspace", run_id=run_id,
        node_id="node-a", profile_id="profile-a", session_id="browser-session",
    )
    with sqlite3.connect(repository.db_path) as conn:
        conn.execute(
            "INSERT INTO execution_window_leases(lease_id,window_id,owner_id,holder_id,lease_token_hash,expires_at,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            ("lease-fixture", window["window_id"], "owner@example.test", "browser", "lease-hash", 200, 100),
        )
        conn.execute(
            "INSERT INTO execution_window_events(window_id,owner_id,sequence,client_event_id,kind,payload,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (window["window_id"], "owner@example.test", 1, "event-fixture", "snapshot", "{}", 101),
        )
        conn.execute(
            "INSERT INTO execution_window_frames(window_id,owner_id,sequence,workspace_id,artifact_id,sha256,width,height,captured_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (window["window_id"], "owner@example.test", 1, "workspace", "artifact-fixture", "0" * 64, 1, 1, "2026-10-09T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO browser_artifact_tickets(ticket_id,token_digest,owner_id,workspace_id,run_id,node_id,command_id,content_type,max_bytes,expires_at,state,session_id,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("artifact-ticket", b"digest", "owner@example.test", "workspace", run_id, "node-a", "command-a", "image/png", 100, 200, "active", session["session_id"], 100),
        )
        conn.execute(
            "INSERT INTO browser_submit_approvals(approval_id,owner_id,workspace_id,run_id,node_id,session_id,selector,state,granted_at,expires_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("approval-fixture", "owner@example.test", "workspace", run_id, "node-a", session["session_id"], "#submit", "active", 100, 200),
        )

    renamed = client.patch(
        f"/api/platform/v1/conversations/{conversation_id}",
        json={"title": "Renamed"},
    )
    assert renamed.status_code == 200
    assert renamed.get_json()["conversation"]["title"] == "Renamed"

    archived = client.post(f"/api/platform/v1/conversations/{conversation_id}/archive")
    assert archived.status_code == 200
    assert archived.get_json()["conversation"]["archived_at"] is not None
    assert client.get("/api/platform/v1/conversations").get_json()["conversations"] == []
    archived_rows = client.get("/api/platform/v1/conversations?archived=true").get_json()["conversations"]
    assert [row["conversation_id"] for row in archived_rows] == [conversation_id]
    assert client.post(
        f"/api/platform/v1/conversations/{conversation_id}/turns",
        json={"text": "blocked while archived", "client_token": "archived-turn"},
    ).status_code == 409

    active_delete = client.delete(f"/api/platform/v1/conversations/{conversation_id}")
    assert active_delete.status_code == 409
    assert active_delete.get_json()["error"] == "conversation_run_active"

    with sqlite3.connect(repository.db_path) as conn:
        conn.execute("UPDATE runs SET state='succeeded' WHERE run_id=?", (run_id,))
    deleted = client.delete(f"/api/platform/v1/conversations/{conversation_id}")
    assert deleted.status_code == 200
    assert deleted.get_json()["deleted"] is True
    with sqlite3.connect(repository.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=?", (conversation_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM runs WHERE conversation_id=?", (conversation_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM run_events WHERE run_id=?", (run_id,)).fetchone()[0] == 0
        for table in (
            "browser_sessions", "browser_artifact_tickets", "browser_submit_approvals",
            "execution_windows", "execution_window_tickets", "execution_window_leases",
            "execution_window_events", "execution_window_frames",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0

    assert repository.get_conversation("owner@example.test", conversation_id) is None
    assert repository.get_conversation("other@example.test", conversation_id) is None


def test_conversation_delete_waits_for_remote_commands_then_cleans_command_records(tmp_path):
    from hub.domain.platform_command import PlatformCommand

    app = _app(tmp_path)
    client = app.test_client()
    repository = app.extensions["fleet"]["platform_repository"]
    command_repository = app.extensions["fleet"]["platform_commands"]
    conversation = client.post("/api/platform/v1/conversations", json={}).get_json()["conversation"]
    conversation_id = conversation["conversation_id"]
    turn = client.post(
        f"/api/platform/v1/conversations/{conversation_id}/turns",
        json={"text": "command cleanup", "client_token": "command-cleanup"},
    ).get_json()
    run_id = turn["run"]["run_id"]
    command = PlatformCommand.create(
        command_id="conversation-command", target_node="node-a", owner_id="owner@example.test",
        action="tool.workspace.write", resource_id="workspace-a",
        arguments={"path": "output.txt", "content": "fixture"},
        retry_class="reconcile_before_retry", expires_at=2_000_000_000,
        run_id=run_id,
    )
    command_repository.enqueue(command, idempotency_key="conversation-command")
    with sqlite3.connect(repository.db_path) as conn:
        conn.execute("UPDATE runs SET state='succeeded' WHERE run_id=?", (run_id,))
    assert client.post(f"/api/platform/v1/conversations/{conversation_id}/archive").status_code == 200

    for state in ("queued", "leased", "accepted", "running", "unknown"):
        with sqlite3.connect(repository.db_path) as conn:
            conn.execute("UPDATE platform_commands SET status=? WHERE command_id=?", (state, command.command_id))
        response = client.delete(f"/api/platform/v1/conversations/{conversation_id}")
        assert response.status_code == 409
        assert response.get_json()["error"] == "conversation_command_unresolved"
        assert repository.get_conversation("owner@example.test", conversation_id) is not None
        assert command_repository.get_for_owner("owner@example.test", command.command_id) is not None

    with sqlite3.connect(repository.db_path) as conn:
        conn.execute("UPDATE platform_commands SET status='succeeded' WHERE command_id=?", (command.command_id,))
        conn.execute(
            "INSERT INTO platform_command_reconciliations VALUES(?,?,?,?,?,?,?,?)",
            ("rec-command", command.command_id, "owner@example.test", "owner@example.test",
             "confirmed_succeeded", "operator", "verified", 1),
        )
        conn.execute(
            "INSERT INTO platform_command_postchecks VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("owner@example.test", command.command_id, "workspace", "postcheck-command",
             "output.txt", "0" * 64, "matched", "{}", "evidence", 1, 1),
        )
        conn.execute(
            "INSERT INTO platform_service_postchecks VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("owner@example.test", command.command_id, "service", "service-postcheck-command",
             "service-a", 1, "matched", "{}", "evidence", 1, 1),
        )

    deleted = client.delete(f"/api/platform/v1/conversations/{conversation_id}")
    assert deleted.status_code == 200
    with sqlite3.connect(repository.db_path) as conn:
        for table in (
            "platform_commands", "platform_command_outbox", "platform_command_reconciliations",
            "platform_command_postchecks", "platform_service_postchecks",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0

def test_conversation_delete_uses_run_scope_for_legacy_commands_without_owner_id(tmp_path):
    from hub.domain.platform_command import PlatformCommand

    app = _app(tmp_path)
    client = app.test_client()
    repository = app.extensions["fleet"]["platform_repository"]
    command_repository = app.extensions["fleet"]["platform_commands"]
    conversation = client.post("/api/platform/v1/conversations", json={}).get_json()["conversation"]
    conversation_id = conversation["conversation_id"]
    turn = client.post(
        f"/api/platform/v1/conversations/{conversation_id}/turns",
        json={"text": "legacy command cleanup", "client_token": "legacy-command-cleanup"},
    ).get_json()
    run_id = turn["run"]["run_id"]
    command = PlatformCommand.create(
        command_id="legacy-conversation-command", target_node="node-a",
        owner_id="owner@example.test", action="tool.workspace.write",
        resource_id="workspace-a", arguments={"path": "output.txt", "content": "fixture"},
        retry_class="reconcile_before_retry", expires_at=2_000_000_000, run_id=run_id,
    )
    command_repository.enqueue(command, idempotency_key="legacy-conversation-command")
    with sqlite3.connect(repository.db_path) as conn:
        conn.execute("UPDATE platform_commands SET owner_id=NULL WHERE command_id=?", (command.command_id,))
        conn.execute("UPDATE runs SET state='succeeded' WHERE run_id=?", (run_id,))
    assert client.post(f"/api/platform/v1/conversations/{conversation_id}/archive").status_code == 200

    unresolved = client.delete(f"/api/platform/v1/conversations/{conversation_id}")

    assert unresolved.status_code == 409
    assert unresolved.get_json()["error"] == "conversation_command_unresolved"
    assert repository.get_conversation("owner@example.test", conversation_id) is not None
    with sqlite3.connect(repository.db_path) as conn:
        conn.execute("UPDATE platform_commands SET status='succeeded' WHERE command_id=?", (command.command_id,))
        conn.execute(
            "INSERT INTO platform_command_reconciliations VALUES(?,?,?,?,?,?,?,?)",
            ("rec-legacy-command", command.command_id, "owner@example.test",
             "owner@example.test", "confirmed_succeeded", "operator", "verified", 1),
        )
        conn.execute(
            "INSERT INTO platform_command_postchecks VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("owner@example.test", command.command_id, "workspace", "legacy-postcheck",
             "output.txt", "0" * 64, "matched", "{}", "evidence", 1, 1),
        )
        conn.execute(
            "INSERT INTO platform_service_postchecks VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("owner@example.test", command.command_id, "service","legacy-service-postcheck",
             "service-a", 1, "matched", "{}", "evidence", 1, 1),
        )

    deleted = client.delete(f"/api/platform/v1/conversations/{conversation_id}")

    assert deleted.status_code == 200
    with sqlite3.connect(repository.db_path) as conn:
        for table in (
            "platform_commands", "platform_command_outbox",
            "platform_command_reconciliations", "platform_command_postchecks",
            "platform_service_postchecks",
        ):
            assert conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE command_id=?", (command.command_id,),
            ).fetchone()[0] == 0

def test_conversation_lifecycle_requires_archive_and_is_owner_scoped(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    repository = app.extensions["fleet"]["platform_repository"]
    repository.create_conversation(
        "other@example.test", "conv-private", title="Private", workspace_id=None,
    )

    foreign_rename = client.patch(
        "/api/platform/v1/conversations/conv-private", json={"title": "No"},
    )
    foreign_archive = client.post("/api/platform/v1/conversations/conv-private/archive")
    foreign_delete = client.delete("/api/platform/v1/conversations/conv-private")
    assert [response.status_code for response in
            (foreign_rename, foreign_archive, foreign_delete)] == [404, 404, 404]

    conversation = client.post("/api/platform/v1/conversations", json={}).get_json()["conversation"]
    conversation_id = conversation["conversation_id"]
    active_delete = client.delete(f"/api/platform/v1/conversations/{conversation_id}")
    assert active_delete.status_code == 409
    assert active_delete.get_json()["error"] == "conversation_not_archived"
    invalid_title = client.patch(
        f"/api/platform/v1/conversations/{conversation_id}", json={"title": " "},
    )
    assert invalid_title.status_code == 400
    assert client.get("/api/platform/v1/conversations?archived=yes").status_code == 400


@pytest.mark.parametrize("state", [
    "waiting_node", "waiting_approval", "waiting_task", "paused", "unknown",
])
def test_archived_conversation_cannot_be_deleted_while_run_is_unfinished(tmp_path, state):
    app = _app(tmp_path)
    client = app.test_client()
    conversation = client.post("/api/platform/v1/conversations", json={}).get_json()["conversation"]
    conversation_id = conversation["conversation_id"]
    turn = client.post(
        f"/api/platform/v1/conversations/{conversation_id}/turns",
        json={"text": "still in progress", "client_token": "unfinished-run"},
    ).get_json()
    run_id = turn["run"]["run_id"]

    repository = app.extensions["fleet"]["platform_repository"]
    with sqlite3.connect(repository.db_path) as conn:
        conn.execute("UPDATE runs SET state=? WHERE run_id=?", (state, run_id))
    assert client.post(f"/api/platform/v1/conversations/{conversation_id}/archive").status_code == 200

    deleted = client.delete(f"/api/platform/v1/conversations/{conversation_id}")

    assert deleted.status_code == 409
    assert deleted.get_json()["error"] == "conversation_run_active"
    assert repository.get_conversation("owner@example.test", conversation_id) is not None

def test_conversation_archive_migration_adds_column_to_existing_database(tmp_path):
    app = _app(tmp_path)
    repository = app.extensions["fleet"]["platform_repository"]
    repository.create_conversation(
        "owner@example.test", "conv-legacy", title="Legacy", workspace_id=None,
    )
    with sqlite3.connect(repository.db_path) as conn:
        conn.execute("DROP INDEX idx_conversations_owner_archive")
        conn.execute("ALTER TABLE conversations DROP COLUMN archived_at")
        conn.execute("UPDATE meta SET value='2' WHERE key='schema_version'")

    repository.init()

    assert repository.schema_version() == 3
    assert repository.get_conversation("owner@example.test", "conv-legacy")["archived_at"] is None
    assert [row[1] for row in sqlite3.connect(repository.db_path).execute(
        "PRAGMA table_info(conversations)") if row[1] == "archived_at"] == ["archived_at"]


def test_retry_of_accepted_turn_survives_model_catalog_change(tmp_path):
    app = _app(tmp_path)
    repo = app.extensions['fleet']['platform_repository']
    owner = 'owner@example.test'
    repo.upsert_model(owner, {'profile_id': 'selected', 'provider': 'deterministic', 'model': 'selected'})
    client = app.test_client()
    conversation = client.post('/api/platform/v1/conversations', json={}).get_json()['conversation']
    url = f"/api/platform/v1/conversations/{conversation['conversation_id']}/turns"
    payload = {'text': 'one turn', 'client_token': 'lost-response', 'overrides': {'model_profile_id': 'selected'}}
    first = client.post(url, json=payload)
    assert first.status_code == 202
    repo.upsert_model(owner, {'profile_id': 'selected', 'provider': 'deterministic', 'model': 'selected', 'enabled': False})
    retry = client.post(url, json=payload)
    assert retry.status_code == 202
    assert retry.get_json()['run']['run_id'] == first.get_json()['run']['run_id']
    assert retry.get_json()['created'] is False


def test_message_order_migration_is_stable_across_restart_and_vacuum(tmp_path):
    import sqlite3

    app = _app(tmp_path)
    repo = app.extensions['fleet']['platform_repository']
    owner = 'owner@example.test'
    repo.create_conversation(owner, 'conv-order', title='', workspace_id=None)
    for name in ('z', 'a'):
        repo.append_turn(owner, 'conv-order', 'msg-' + name, 'run-' + name,
                         text=name, client_token=name, config_snapshot={}, now=1)
    # Simulate a pre-remediation database: no explicit ordering column.
    with sqlite3.connect(repo.db_path) as conn:
        conn.execute('DROP TRIGGER assign_message_turn_sequence')
        conn.execute('DROP INDEX idx_message_turn_order')
        conn.execute('ALTER TABLE messages DROP COLUMN turn_sequence')
    repo.init()
    with sqlite3.connect(repo.db_path) as conn:
        conn.execute('VACUUM')
    repo.init()
    assert [m['content'] for m in repo.get_conversation(owner, 'conv-order')['messages']] == ['z', 'a']
    assert [m['content'] for m in repo.get_run_execution_context(owner, 'run-z')['messages']] == ['z']
