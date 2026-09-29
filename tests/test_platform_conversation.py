from hub.bootstrap import create_app
from hub.config import FleetConfig


def _app(tmp_path, *, memory=False, memory_context=False):
    return create_app(FleetConfig.from_root(
        tmp_path, ingest_token="ingest", dev_operator="owner@example.test",
        platform_enabled=True,
        platform_memory_enabled=memory,
        platform_memory_context_enabled=memory_context,
    ))


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
