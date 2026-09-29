from pathlib import Path

import pytest

from hub.infrastructure.execution_window_repository import (
    ExecutionWindowRepository,
    ExecutionWindowRepositoryError,
)
from hub.infrastructure.platform_db import PlatformRepository
from hub.bootstrap import create_app
from hub.config import FleetConfig


class Clock:
    def __init__(self, value=1000.0):
        self.value = float(value)

    def __call__(self):
        return self.value


def _repositories(tmp_path: Path):
    db = tmp_path / "platform.db"
    platform = PlatformRepository(db)
    platform.init()
    platform.create_conversation("owner-a@example.test", "conv-a", title="", workspace_id=None)
    created = platform.append_turn(
        "owner-a@example.test", "conv-a", "msg-a", "run-a", text="run",
        client_token="client-a", config_snapshot={}, now=1.0,
    )
    clock = Clock()
    windows = ExecutionWindowRepository(db, clock=clock)
    windows.init()
    return platform, windows, clock, created["run"]["run_id"]


def test_window_requires_existing_owner_run_and_redacts_credentials(tmp_path):
    _, repo, _, run_id = _repositories(tmp_path)
    with pytest.raises(ExecutionWindowRepositoryError) as missing:
        repo.create_window("owner-a@example.test", "missing-run")
    assert missing.value.code == "run_not_found"

    created = repo.create_window("owner-a@example.test", run_id, metadata={"surface": "assistant"})
    assert created["window"]["state"] == "pending"
    assert created["window"]["run_id"] == run_id
    assert "owner_id" not in created["window"]
    assert created["attach_ticket"]
    assert "token_hash" not in created["window"]
    assert repo.get_window("other@example.test", created["window"]["window_id"]) is None


def test_window_listing_is_owner_scoped_bounded_and_run_filterable(tmp_path):
    platform, repo, clock, run_id = _repositories(tmp_path)
    second = platform.append_turn(
        "owner-a@example.test", "conv-a", "msg-b", "run-b", text="run two",
        client_token="client-b", config_snapshot={}, now=2.0,
    )["run"]["run_id"]
    first_window = repo.create_window("owner-a@example.test", run_id)["window"]
    clock.value = 1001
    second_window = repo.create_window("owner-a@example.test", second)["window"]
    listed = repo.list_windows("owner-a@example.test", limit=1)
    assert len(listed["windows"]) == 1
    assert listed["windows"][0]["window_id"] == second_window["window_id"]
    assert listed["next_cursor"] == second_window["window_id"]
    filtered = repo.list_windows("owner-a@example.test", run_id=run_id, limit=20)
    assert [item["window_id"] for item in filtered["windows"]] == [first_window["window_id"]]
    assert "owner_id" not in str(listed)
    assert "attach_ticket" not in str(listed)
    assert repo.list_windows("owner-b@example.test")["windows"] == []
    with pytest.raises(ExecutionWindowRepositoryError) as bad_limit:
        repo.list_windows("owner-a@example.test", limit=0)
    assert bad_limit.value.code == "invalid_cursor"
    with pytest.raises(ExecutionWindowRepositoryError) as bad_run:
        repo.list_windows("owner-a@example.test", run_id="bad token")
    assert bad_run.value.code == "invalid_window"


def test_generated_window_id_remains_valid_when_random_text_contains_reserved_marker(
    tmp_path, monkeypatch
):
    _, repo, _, run_id = _repositories(tmp_path)
    monkeypatch.setattr(
        "hub.infrastructure.execution_window_repository.secrets.token_urlsafe",
        lambda size: "random-key-marker",
    )
    created = repo.create_window("owner-a@example.test", run_id)
    window_id = created["window"]["window_id"]
    assert repo.get_window("owner-a@example.test", window_id)["window_id"] == window_id
    assert repo.get_window("owner-b@example.test", window_id) is None


def test_ticket_is_single_use_and_reconnect_issues_a_new_ticket(tmp_path):
    _, repo, clock, run_id = _repositories(tmp_path)
    created = repo.create_window("owner-a@example.test", run_id)
    window_id = created["window"]["window_id"]

    attached = repo.redeem_ticket("owner-a@example.test", window_id, created["attach_ticket"])
    assert attached["window"]["state"] == "attached"
    with pytest.raises(ExecutionWindowRepositoryError) as reused:
        repo.redeem_ticket("owner-a@example.test", window_id, created["attach_ticket"])
    assert reused.value.code == "ticket_used"

    reconnected = repo.reconnect("owner-a@example.test", window_id)
    assert reconnected["attach_ticket"] != created["attach_ticket"]
    repo.redeem_ticket("owner-a@example.test", window_id, reconnected["attach_ticket"])

    clock.value = reconnected["ticket_expires_at"] + 1
    with pytest.raises(ExecutionWindowRepositoryError) as expired_ticket:
        repo.redeem_ticket("owner-a@example.test", window_id, reconnected["attach_ticket"])
    assert expired_ticket.value.code == "ticket_used"
    clock.value = created["window"]["expires_at"] + 1
    assert repo.get_window("owner-a@example.test", window_id)["state"] == "expired"
    with pytest.raises(ExecutionWindowRepositoryError) as expired:
        repo.reconnect("owner-a@example.test", window_id)
    assert expired.value.code == "window_closed"


def test_unconsumed_ticket_expiry_is_fail_closed(tmp_path):
    _, repo, clock, run_id = _repositories(tmp_path)
    created = repo.create_window("owner-a@example.test", run_id, ttl_s=300)
    reconnected = repo.reconnect(
        "owner-a@example.test", created["window"]["window_id"], ttl_s=30,
    )
    clock.value = reconnected["ticket_expires_at"] + 1
    with pytest.raises(ExecutionWindowRepositoryError) as expired:
        repo.redeem_ticket("owner-a@example.test", created["window"]["window_id"], reconnected["attach_ticket"])
    assert expired.value.code == "ticket_expired"


def test_writer_lease_is_exclusive_fenced_and_releasable(tmp_path):
    _, repo, clock, run_id = _repositories(tmp_path)
    created = repo.create_window("owner-a@example.test", run_id)
    window_id = created["window"]["window_id"]
    repo.redeem_ticket("owner-a@example.test", window_id, created["attach_ticket"])

    first = repo.acquire_writer("owner-a@example.test", window_id, "browser-a", ttl_s=30)
    assert first["lease"]["holder_id"] == "browser-a"
    assert "lease_token_hash" not in first["lease"]
    with pytest.raises(ExecutionWindowRepositoryError) as conflict:
        repo.acquire_writer("owner-a@example.test", window_id, "browser-b")
    assert conflict.value.code == "lease_conflict"

    renewed = repo.renew_writer(
        "owner-a@example.test", window_id, "browser-a", first["lease_token"], ttl_s=40,
    )
    assert renewed["lease"]["expires_at"] > first["lease"]["expires_at"]
    repo.release_writer("owner-a@example.test", window_id, "browser-a", first["lease_token"])
    second = repo.acquire_writer("owner-a@example.test", window_id, "browser-b")
    assert second["lease"]["holder_id"] == "browser-b"

    clock.value = second["lease"]["expires_at"] + 1
    with pytest.raises(ExecutionWindowRepositoryError) as expired:
        repo.renew_writer("owner-a@example.test", window_id, "browser-b", second["lease_token"])
    assert expired.value.code == "lease_expired"


def test_close_revokes_ticket_and_lease_and_is_idempotent(tmp_path):
    _, repo, _, run_id = _repositories(tmp_path)
    created = repo.create_window("owner-a@example.test", run_id)
    window_id = created["window"]["window_id"]
    repo.redeem_ticket("owner-a@example.test", window_id, created["attach_ticket"])
    lease = repo.acquire_writer("owner-a@example.test", window_id, "operator")

    closed = repo.close_window("owner-a@example.test", window_id)
    assert closed["window"]["state"] == "closed"
    assert repo.close_window("owner-a@example.test", window_id)["window"]["state"] == "closed"
    with pytest.raises(ExecutionWindowRepositoryError) as lease_error:
        repo.release_writer("owner-a@example.test", window_id, "operator", lease["lease_token"])
    assert lease_error.value.code == "window_closed"


def _app(tmp_path, *, owner="owner-a@example.test", enabled=False, platform=True):
    return create_app(FleetConfig.from_root(
        tmp_path, dev_operator=owner, platform_enabled=platform,
        execution_windows_enabled=enabled,
    ))


def _run(client):
    conversation = client.post("/api/platform/v1/conversations", json={})
    assert conversation.status_code == 200
    conversation_id = conversation.get_json()["conversation"]["conversation_id"]
    response = client.post(
        f"/api/platform/v1/conversations/{conversation_id}/turns",
        json={"text": "open window", "client_token": "window-run-1"},
    )
    assert response.status_code == 202
    return response.get_json()["run"]["run_id"]


def test_execution_window_gate_is_closed_by_default_and_requires_platform(tmp_path):
    disabled = _app(tmp_path / "disabled", enabled=False)
    assert "execution_windows" not in disabled.extensions["fleet"]["services"]
    assert disabled.test_client().post(
        "/api/platform/v1/execution-windows", json={"run_id": "run-a"}
    ).status_code == 404
    assert disabled.test_client().get(
        "/api/platform/v1/execution-windows/win_a/events"
    ).status_code == 404

    no_platform = _app(tmp_path / "no-platform", enabled=True, platform=False)
    assert no_platform.config["EXECUTION_WINDOWS_ENABLED"] is False
    assert no_platform.test_client().get(
        "/api/platform/v1/execution-windows/win_a"
    ).status_code == 404


def test_execution_window_http_lifecycle_and_redaction(tmp_path):
    app = _app(tmp_path, enabled=True)
    client = app.test_client()
    run_id = _run(client)
    created = client.post(
        "/api/platform/v1/execution-windows",
        json={"run_id": run_id, "metadata": {"surface": "assistant"}},
    )
    assert created.status_code == 200
    payload = created.get_json()
    window_id = payload["window"]["window_id"]
    ticket = payload["attach_ticket"]
    assert "lease_token" not in payload and "token_hash" not in str(payload)

    fetched = client.get(f"/api/platform/v1/execution-windows/{window_id}")
    assert fetched.status_code == 200
    assert "attach_ticket" not in fetched.get_json()
    attached = client.post(
        f"/api/platform/v1/execution-windows/{window_id}/attach",
        json={"ticket": ticket},
    )
    assert attached.status_code == 200
    assert attached.get_json()["window"]["state"] == "attached"

    reconnect = client.post(
        f"/api/platform/v1/execution-windows/{window_id}/reconnect", json={}
    )
    assert reconnect.status_code == 200
    reconnect_ticket = reconnect.get_json()["attach_ticket"]
    client.post(
        f"/api/platform/v1/execution-windows/{window_id}/attach",
        json={"ticket": reconnect_ticket},
    )
    acquired = client.post(
        f"/api/platform/v1/execution-windows/{window_id}/writer",
        json={"holder_id": "assistant", "ttl_s": 30},
    )
    assert acquired.status_code == 200
    lease = acquired.get_json()["lease"]
    lease_token = acquired.get_json()["lease_token"]
    assert "lease_token_hash" not in acquired.get_data(as_text=True)
    renewed = client.post(
        f"/api/platform/v1/execution-windows/{window_id}/writer/renew",
        json={"holder_id": "assistant", "lease_token": lease_token, "ttl_s": 30},
    )
    assert renewed.status_code == 200
    released = client.post(
        f"/api/platform/v1/execution-windows/{window_id}/writer/release",
        json={"holder_id": "assistant", "lease_token": lease_token},
    )
    assert released.status_code == 200
    closed = client.post(f"/api/platform/v1/execution-windows/{window_id}/close", json={})
    assert closed.status_code == 200
    assert closed.get_json()["window"]["state"] == "closed"
    assert lease["window_id"] == window_id


def test_execution_window_http_collection_is_bounded_and_owner_scoped(tmp_path):
    app = _app(tmp_path, enabled=True)
    client = app.test_client()
    run_id = _run(client)
    created = client.post("/api/platform/v1/execution-windows", json={"run_id": run_id}).get_json()
    response = client.get("/api/platform/v1/execution-windows?run_id=" + run_id + "&limit=1")
    assert response.status_code == 200
    payload = response.get_json()
    assert [item["window_id"] for item in payload["windows"]] == [created["window"]["window_id"]]
    assert "owner_id" not in response.get_data(as_text=True)
    assert "attach_ticket" not in response.get_data(as_text=True)
    assert client.get("/api/platform/v1/execution-windows?limit=0").status_code == 400
    foreign = _app(tmp_path / "foreign", owner="owner-b@example.test", enabled=True)
    assert foreign.test_client().get(
        "/api/platform/v1/execution-windows?run_id=" + run_id
    ).get_json()["windows"] == []


def test_execution_window_http_enforces_owner_and_rejects_host_control_fields(tmp_path):
    app = _app(tmp_path, enabled=True)
    client = app.test_client()
    run_id = _run(client)
    bad = client.post(
        "/api/platform/v1/execution-windows",
        json={"run_id": run_id, "command": "restart"},
    )
    assert bad.status_code == 400
    assert "restart" not in bad.get_data(as_text=True)
    nested_bad = client.post(
        "/api/platform/v1/execution-windows",
        json={"run_id": run_id, "metadata": {"nested": [{"argv": ["sh"]}]}},
    )
    assert nested_bad.status_code == 400

    created = client.post(
        "/api/platform/v1/execution-windows", json={"run_id": run_id}
    ).get_json()
    window_id = created["window"]["window_id"]
    other = _app(tmp_path, owner="owner-b@example.test", enabled=True)
    other_get = other.test_client().get(
        f"/api/platform/v1/execution-windows/{window_id}"
    )
    assert other_get.status_code == 404


def test_window_events_are_lease_fenced_cursored_and_idempotent(tmp_path):
    _, repo, clock, run_id = _repositories(tmp_path)
    created = repo.create_window("owner-a@example.test", run_id)
    window_id = created["window"]["window_id"]
    with pytest.raises(ExecutionWindowRepositoryError) as unattached:
        repo.append_event(
            "owner-a@example.test", window_id, "assistant", "x" * 32,
            "evt-before-attach", "text", {"text": "blocked"},
        )
    assert unattached.value.code == "window_not_attached"
    repo.redeem_ticket("owner-a@example.test", window_id, created["attach_ticket"])
    lease = repo.acquire_writer("owner-a@example.test", window_id, "assistant", ttl_s=30)

    first = repo.append_event(
        "owner-a@example.test", window_id, "assistant", lease["lease_token"],
        "evt-1", "text", {"text": "hello"},
    )
    assert first["sequence"] == 1
    duplicate = repo.append_event(
        "owner-a@example.test", window_id, "assistant", lease["lease_token"],
        "evt-1", "text", {"text": "hello"},
    )
    assert duplicate == first
    with pytest.raises(ExecutionWindowRepositoryError) as conflict:
        repo.append_event(
            "owner-a@example.test", window_id, "assistant", lease["lease_token"],
            "evt-1", "text", {"text": "different payload"},
        )
    assert conflict.value.code == "event_conflict"
    second = repo.append_event(
        "owner-a@example.test", window_id, "assistant", lease["lease_token"],
        "evt-2", "notice", {"level": "info"},
    )
    assert second["sequence"] == 2
    page = repo.list_events("owner-a@example.test", window_id, after=0, limit=1)
    assert [event["client_event_id"] for event in page["events"]] == ["evt-1"]
    assert page["next_cursor"] == 1
    resumed = repo.list_events("owner-a@example.test", window_id, after=page["next_cursor"])
    assert [event["sequence"] for event in resumed["events"]] == [2]

    with pytest.raises(ExecutionWindowRepositoryError) as forbidden:
        repo.append_event(
            "owner-a@example.test", window_id, "assistant", lease["lease_token"],
            "evt-3", "output", {"command": "restart"},
        )
    assert forbidden.value.code == "forbidden_event_field"
    with pytest.raises(ExecutionWindowRepositoryError) as forbidden_variant:
        repo.append_event(
            "owner-a@example.test", window_id, "assistant", lease["lease_token"],
            "evt-3b", "output", {"text": {"process_id": "opaque"}},
        )
    assert forbidden_variant.value.code == "forbidden_event_field"
    with pytest.raises(ExecutionWindowRepositoryError) as foreign:
        repo.list_events("owner-b@example.test", window_id)
    assert foreign.value.code == "window_not_found"

    clock.value = lease["lease"]["expires_at"] + 1
    with pytest.raises(ExecutionWindowRepositoryError) as expired:
        repo.append_event(
            "owner-a@example.test", window_id, "assistant", lease["lease_token"],
            "evt-4", "text", {"text": "late"},
        )
    assert expired.value.code == "lease_expired"


def test_window_event_http_routes_redact_credentials_and_reject_bad_payload(tmp_path):
    app = _app(tmp_path, enabled=True)
    client = app.test_client()
    run_id = _run(client)
    created = client.post("/api/platform/v1/execution-windows", json={"run_id": run_id}).get_json()
    window_id = created["window"]["window_id"]
    client.post(f"/api/platform/v1/execution-windows/{window_id}/attach", json={"ticket": created["attach_ticket"]})
    acquired = client.post(f"/api/platform/v1/execution-windows/{window_id}/writer", json={"holder_id": "assistant"}).get_json()
    token = acquired["lease_token"]
    response = client.post(
        f"/api/platform/v1/execution-windows/{window_id}/events",
        json={"holder_id": "assistant", "lease_token": token,
              "client_event_id": "evt-http-1", "kind": "text",
              "payload": {"text": "hello"}},
    )
    assert response.status_code == 200
    assert response.get_json()["event"]["sequence"] == 1
    assert token not in response.get_data(as_text=True)
    listed = client.get(f"/api/platform/v1/execution-windows/{window_id}/events?after=0")
    assert listed.status_code == 200
    assert listed.get_json()["events"][0]["payload"] == {"text": "hello"}
    bad = client.post(
        f"/api/platform/v1/execution-windows/{window_id}/events",
        json={"holder_id": "assistant", "lease_token": token,
              "client_event_id": "evt-http-2", "kind": "text",
              "payload": {"secret_ref": "do-not-return"}},
    )
    assert bad.status_code == 400
    assert "do-not-return" not in bad.get_data(as_text=True)
