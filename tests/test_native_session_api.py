"""Real HTTP and SQLite behavior for the public native-session APIs."""
from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig
from tests.test_session_interactions import event


OPERATOR = "owner@example.test"
SESSION = "sess_native_1"
MESSAGE_URL = f"/api/sessions/{SESSION}/messages"


@pytest.fixture
def native_hub(tmp_path):
    app = create_app(FleetConfig.from_root(
        tmp_path,
        dev_operator=OPERATOR,
        supervisor_enabled=True,
        supervisor_signing_raw=bytes(range(32)),
        session_repositories_enabled=True,
        adoption_repositories_enabled=True,
        append_user_turn_enabled=True,
    ))
    services = app.extensions["fleet"]["services"]
    services["sessions"].session_repo.upsert_session({
        "session_id": SESSION,
        "machine_id": "m1",
        "managed": True,
        "capture_quality": "structured",
        "process_group_id": "group_native_1",
        "attempt_id": "attempt_native_1",
        "agent_family": "codex",
    })
    return app, services


def post_message(app, session_id=SESSION, body=None, *, operator=None):
    headers = {}
    if operator:
        headers["Cf-Access-Authenticated-User-Email"] = operator
    with app.test_client() as client:
        return client.post(
            f"/api/sessions/{session_id}/messages",
            json=body or {"text": "continue"}, headers=headers)


def command_count(services):
    repository = services["supervisor"]._command_repository
    with sqlite3.connect(repository.db_path) as connection:
        return connection.execute(
            "SELECT count(*) FROM supervisor_commands").fetchone()[0]


def test_message_replays_original_command_and_current_terminal_status(native_hub):
    app, services = native_hub
    request = {"text": "continue after response loss", "client_token": "web-op-001"}

    accepted = post_message(app, body=request)
    assert accepted.status_code == 202
    # Drop the client-visible response. The test harness only inspects SQLite
    # so it can compare the later recovery to the command actually persisted.
    with sqlite3.connect(services["supervisor"]._command_repository.db_path) as connection:
        original_command_id = connection.execute(
            "SELECT command_id FROM session_message_operations").fetchone()[0]

    replay = post_message(app, body=request)
    assert replay.status_code == 202
    assert replay.get_json()["command_id"] == original_command_id
    assert replay.get_json()["status"] == "queued"
    with sqlite3.connect(services["supervisor"]._command_repository.db_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM supervisor_commands WHERE action='append_user_turn'").fetchone()[0] == 1

    services["supervisor"].poll("m1")
    delivered_replay = post_message(app, body=request)
    assert delivered_replay.status_code == 202
    assert delivered_replay.get_json()["command_id"] == original_command_id
    assert delivered_replay.get_json()["status"] == "delivered"
    services["supervisor"].receipt(
        original_command_id, "failed", "agent_rejected", machine="m1")
    terminal_replay = post_message(app, body=request)
    assert terminal_replay.status_code == 202
    assert terminal_replay.get_json()["command_id"] == original_command_id
    assert terminal_replay.get_json()["status"] == "failed"
    assert command_count(services) == 1


def test_message_replay_refreshes_expired_outbox_status(native_hub):
    app, services = native_hub
    request = {"text": "expire before recovery", "client_token": "expired-op"}
    accepted = post_message(app, body=request)
    assert accepted.status_code == 202
    services["supervisor"]._inner._now_fn = lambda: 4_000_000_000.0

    replay = post_message(app, body=request)

    assert replay.status_code == 202
    assert replay.get_json()["status"] == "expired"
    assert command_count(services) == 1


def test_concurrent_identical_message_posts_create_one_operation_and_command(native_hub):
    app, services = native_hub
    barrier = Barrier(8)
    request = {"text": "concurrent retry", "client_token": "web-op-concurrent"}

    def submit(_):
        barrier.wait(timeout=5)
        response = post_message(app, body=request)
        return response.status_code, response.get_json()

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(submit, range(8)))

    assert all(status == 202 for status, _ in results)
    assert len({body["command_id"] for _, body in results}) == 1
    assert command_count(services) == 1
    with sqlite3.connect(services["supervisor"]._command_repository.db_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM session_message_operations").fetchone()[0] == 1


@pytest.mark.parametrize("session_id,body,operator", [
    (SESSION, {"text": "changed body", "client_token": "conflict-marker"}, None),
    ("sess_native_2", {"text": "original body", "client_token": "conflict-marker"}, None),
    (SESSION, {"text": "original body", "client_token": "conflict-marker"}, "other@example.test"),
])
def test_message_token_binding_conflicts_are_bounded_and_secret_free(
        native_hub, session_id, body, operator):
    app, services = native_hub
    services["sessions"].session_repo.upsert_session({
        "session_id": "sess_native_2", "machine_id": "m1", "managed": True,
        "capture_quality": "structured", "process_group_id": "group_native_2",
    })
    original = {"text": "original body", "client_token": "conflict-marker"}
    first = post_message(app, body=original)
    assert first.status_code == 202
    command_id = first.get_json()["command_id"]

    response = post_message(app, session_id, body, operator=operator)
    assert response.status_code == 409
    assert response.get_json()["error"] == "idempotency_conflict"
    payload = response.get_data(as_text=True)
    assert "original body" not in payload
    assert "changed body" not in payload
    assert "conflict-marker" not in payload
    assert command_id not in payload
    assert command_count(services) == 1


@pytest.mark.parametrize("token", [None, "", "\ninvalid", "x" * 129, 3, True, [], {}])
def test_message_token_format_is_validated_when_provided(native_hub, token):
    app, services = native_hub
    response = post_message(app, body={"text": "valid message", "client_token": token})
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_client_token"
    assert command_count(services) == 0


def test_legacy_text_only_message_remains_supported(native_hub):
    app, services = native_hub
    response = post_message(app, body={"text": "legacy caller"})
    assert response.status_code == 202
    assert response.get_json()["status"] == "pending"
    assert command_count(services) == 1
    with sqlite3.connect(services["supervisor"]._command_repository.db_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM session_message_operations").fetchone()[0] == 0


def test_message_body_over_control_limit_is_rejected_without_enqueue(native_hub):
    app, services = native_hub
    response = post_message(app, body={
        "text": "x" * 2001, "client_token": "oversized-message"})
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_message"
    assert command_count(services) == 0


def test_revoked_adoption_allows_only_recovery_of_an_already_accepted_message(native_hub):
    app, services = native_hub
    services["observe"].ingest({
        "machine": "m1", "reachable": True, "agents": {},
        "instances": [{
            "pid": 4242, "pgid": 4242, "exe_path": "/usr/bin/codex",
            "cmdline": "codex", "agent_family": "codex",
            "native_file_path": "/sessions/codex.jsonl",
            "started_at": "2026-07-30T09:15:00Z",
            "attachable": True,
        }],
    })
    adoption = services["adoptions"].adopt(
        "m1", 4242, "2026-07-30T09:15:00Z", OPERATOR)
    services["adoptions"].adoption_repo.update_status(adoption.session_id, "adopted")
    body = {"text": "accepted before revoke", "client_token": "adopt-message-1"}
    first = post_message(app, adoption.session_id, body)
    assert first.status_code == 202, first.get_json()
    services["adoptions"].revoke(adoption.session_id, OPERATOR)

    replay = post_message(app, adoption.session_id, body)
    assert replay.status_code == 202
    assert replay.get_json()["command_id"] == first.get_json()["command_id"]
    denied = post_message(app, adoption.session_id, {
        "text": "cannot enqueue after revoke", "client_token": "adopt-message-2"})
    assert denied.status_code == 409
    assert denied.get_json()["error"] == "adoption_revoked"
    with sqlite3.connect(services["supervisor"]._command_repository.db_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM supervisor_commands WHERE action='append_user_turn'").fetchone()[0] == 1


def test_interaction_answer_keeps_its_request_id_token_protocol(native_hub):
    app, services = native_hub
    services["sessions"].session_repo.set_capabilities(
        SESSION, ["interactive_questions"])
    services["sessions"].session_repo.project_interaction(SESSION, 1, "interaction_request", {
        "request_id": "question-1", "question_type": "single", "prompt": "Choose",
        "options": [{"id": "yes", "label": "Yes"}], "allow_custom": False,
        "tool_name": "AskUserQuestion",
    })
    body = {"request_id": "question-1", "client_token": "answer-token",
            "selected": ["yes"], "text": ""}
    first = post_message(app, body=body)
    replay = post_message(app, body=body)
    assert first.status_code == replay.status_code == 202
    assert first.get_json()["command_id"] == replay.get_json()["command_id"]
    with sqlite3.connect(services["supervisor"]._command_repository.db_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM session_message_operations").fetchone()[0] == 0


def test_session_event_tail_before_pages_and_after_increment(native_hub):
    app, services = native_hub
    for sequence in range(1, 6):
        row = event(sequence, "assistant_message", {
            "text": f"event-{sequence}", "is_complete": True})
        row.update(event_id=f"native-event-{sequence}", session_id=SESSION,
                   stream_id="native-stream", process_group_id="group_native_1")
        services["sessions"].ingest_events([row])

    with app.test_client() as client:
        tail = client.get(f"/api/sessions/{SESSION}/events?tail=1&limit=2").get_json()
        assert [e["payload"]["text"] for e in tail["events"]] == ["event-4", "event-5"]
        assert tail["has_more"] is True

        older = client.get(f"/api/sessions/{SESSION}/events", query_string={
            "before_cursor": tail["events"][0]["cursor"], "limit": 2}).get_json()
        oldest = client.get(f"/api/sessions/{SESSION}/events", query_string={
            "before_cursor": older["events"][0]["cursor"], "limit": 2}).get_json()
        assert [e["payload"]["text"] for e in older["events"]] == ["event-2", "event-3"]
        assert [e["payload"]["text"] for e in oldest["events"]] == ["event-1"]

        row = event(6, "assistant_message", {"text": "event-6", "is_complete": True})
        row.update(event_id="native-event-6", session_id=SESSION,
                   stream_id="native-stream", process_group_id="group_native_1")
        services["sessions"].ingest_events([row])
        live = client.get(f"/api/sessions/{SESSION}/events", query_string={
            "after_cursor": tail["next_cursor"], "limit": 2}).get_json()
        assert [e["payload"]["text"] for e in live["events"]] == ["event-6"]


@pytest.mark.parametrize("cursor", ["-1", "nope", "9223372036854775808"])
def test_invalid_or_future_event_cursors_have_fixed_errors(native_hub, cursor):
    app, _ = native_hub
    with app.test_client() as client:
        response = client.get(f"/api/sessions/{SESSION}/events", query_string={
            "after_cursor": cursor})
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_cursor"


def test_at_sequence_deep_link_allows_legacy_zero_cursor_but_rejects_conflicting_after(
        native_hub):
    app, services = native_hub
    for sequence in range(1, 4):
        row = event(sequence, "assistant_message", {
            "text": f"deep-link-{sequence}", "is_complete": True})
        row.update(event_id=f"deep-link-event-{sequence}", session_id=SESSION,
                   stream_id="native-stream", process_group_id="group_native_1")
        services["sessions"].ingest_events([row])

    with app.test_client() as client:
        compatible = client.get(
            f"/api/sessions/{SESSION}/events?after_cursor=0&at_sequence=2")
        conflicting = client.get(
            f"/api/sessions/{SESSION}/events?after_cursor=2&at_sequence=3")

    assert compatible.status_code == 200
    assert [row["payload"]["text"] for row in compatible.get_json()["events"]] == [
        "deep-link-2", "deep-link-3"]
    assert conflicting.status_code == 400
    assert conflicting.get_json()["error"] == "invalid_cursor"


def test_empty_event_tail_is_an_empty_page(native_hub):
    app, _ = native_hub
    with app.test_client() as client:
        response = client.get(f"/api/sessions/{SESSION}/events?tail=1")
    assert response.status_code == 200
    assert response.get_json()["events"] == []
    assert response.get_json()["has_more"] is False


def test_empty_tail_checkpoint_does_not_skip_concurrent_ingestion(native_hub, monkeypatch):
    app, services = native_hub
    repository = services["sessions"].transcript_repo
    read_redacted = repository.read_redacted
    injected = False

    def read_then_ingest(*args, **kwargs):
        nonlocal injected
        rows = read_redacted(*args, **kwargs)
        if kwargs.get("tail") and not injected:
            injected = True
            row = event(1, "assistant_message", {
                "text": "arrived after empty tail", "is_complete": True})
            row.update(event_id="tail-race-event", session_id=SESSION,
                       stream_id="tail-race-stream", process_group_id="group_native_1")
            services["sessions"].ingest_events([row])
        return rows

    monkeypatch.setattr(repository, "read_redacted", read_then_ingest)
    with app.test_client() as client:
        tail = client.get(f"/api/sessions/{SESSION}/events?tail=1").get_json()
        live = client.get(f"/api/sessions/{SESSION}/events", query_string={
            "after_cursor": tail["next_cursor"]}).get_json()

    assert tail["events"] == []
    assert tail["next_cursor"] == "0"
    assert [row["payload"]["text"] for row in live["events"]] == [
        "arrived after empty tail"]


def test_event_query_clamps_public_limit_and_keeps_lookahead_bounded(
        native_hub, monkeypatch):
    app, services = native_hub
    repository = services["sessions"].transcript_repo
    read_redacted = repository.read_redacted
    requested_limits = []

    def record_limit(*args, **kwargs):
        requested_limits.append(kwargs["limit"])
        return read_redacted(*args, **kwargs)

    monkeypatch.setattr(repository, "read_redacted", record_limit)
    with app.test_client() as client:
        response = client.get(
            f"/api/sessions/{SESSION}/events?tail=1&limit=9000")

    assert response.status_code == 200
    assert requested_limits == [1001]


def test_deleted_event_cursor_is_a_stable_boundary(native_hub):
    app, services = native_hub
    cursors = []
    for sequence in range(1, 4):
        row = event(sequence, "assistant_message", {
            "text": f"clipped-{sequence}", "is_complete": True})
        row.update(event_id=f"clipped-event-{sequence}", session_id=SESSION,
                   stream_id="clipped-stream", process_group_id="group_native_1")
        services["sessions"].ingest_events([row])
        stored = services["sessions"].transcript_repo.read_redacted(SESSION, limit=10)
        cursors = [item["cursor"] for item in stored]
    with sqlite3.connect(services["sessions"].transcript_repo.db_path) as connection:
        connection.execute("DELETE FROM redacted_events WHERE event_id='clipped-event-2'")

    with app.test_client() as client:
        after = client.get(f"/api/sessions/{SESSION}/events", query_string={
            "after_cursor": str(cursors[1])}).get_json()
        before = client.get(f"/api/sessions/{SESSION}/events", query_string={
            "before_cursor": str(cursors[2])}).get_json()
    assert [row["payload"]["text"] for row in after["events"]] == ["clipped-3"]
    assert [row["payload"]["text"] for row in before["events"]] == ["clipped-1"]


def test_session_directory_cursor_covers_snapshot_without_reordering(native_hub):
    app, services = native_hub
    repo = services["sessions"].session_repo
    for session_id in ("sess_native_2", "sess_native_3", "sess_native_4"):
        repo.upsert_session({
            "session_id": session_id, "machine_id": "m1", "managed": True,
            "capture_quality": "structured", "process_group_id": "group_" + session_id,
        })
    with sqlite3.connect(repo.db_path) as connection:
        connection.execute(
            "UPDATE sessions SET started_at='2026-01-01T00:00:00Z',"
            "updated_at='2026-01-01T00:00:00Z'")
    with app.test_client() as client:
        first = client.get("/api/sessions?limit=2").get_json()
        assert len(first["sessions"]) == 2
        assert first["has_more"] is True
        seen = [row["session_id"] for row in first["sessions"]]

        repo.upsert_session({
            "session_id": "sess_native_new", "machine_id": "m1", "managed": True,
            "capture_quality": "structured", "process_group_id": "group_native_new",
        })
        with sqlite3.connect(repo.db_path) as connection:
            connection.execute(
                "UPDATE sessions SET started_at='2026-01-01T00:00:00Z',"
                "updated_at='2026-01-01T00:00:00Z' WHERE session_id='sess_native_3'")

        page = first
        while page.get("next_cursor"):
            page = client.get("/api/sessions", query_string={
                "limit": 2, "cursor": page["next_cursor"]}).get_json()
            seen.extend(row["session_id"] for row in page["sessions"])

        assert len(seen) == len(set(seen))
        assert seen == ["sess_native_4", "sess_native_3", "sess_native_2", SESSION]
        assert set(seen) == {SESSION, "sess_native_2", "sess_native_3", "sess_native_4"}


def test_session_list_cursor_is_bound_to_machine_filter(native_hub):
    app, services = native_hub
    services["sessions"].session_repo.upsert_session({
        "session_id": "sess_native_2", "machine_id": "m1", "managed": True,
        "capture_quality": "structured", "process_group_id": "group_native_2",
    })
    with app.test_client() as client:
        page = client.get("/api/sessions?machine=m1&limit=1").get_json()
        assert page["next_cursor"]
        response = client.get("/api/sessions", query_string={
            "machine": "other", "limit": 1, "cursor": page["next_cursor"]})
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_cursor"


def test_session_list_rejects_snapshot_beyond_directory_high_water(native_hub):
    from hub.application.session_service import _encode_directory_cursor

    app, services = native_hub
    high_water = services["sessions"].session_repo.list_sessions_page(limit=1)[
        "snapshot_cursor"]
    cursor = _encode_directory_cursor(high_water + 1, high_water, None)
    with app.test_client() as client:
        response = client.get("/api/sessions", query_string={
            "limit": 1, "cursor": cursor})
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_cursor"


def test_active_session_slices_do_not_accept_unstable_cursors(native_hub):
    app, _ = native_hub
    with app.test_client() as client:
        response = client.get("/api/sessions?active=1&cursor=eyJ2IjoxfQ")
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_cursor"


def test_operator_auth_and_sqlite_errors_remain_bounded(native_hub, monkeypatch):
    app, services = native_hub
    with app.test_client() as client:
        denied = client.post(MESSAGE_URL, json={"text": "private"}, headers={
            "X-Runner-Credential": "m1:runner-secret"})
    assert denied.status_code == 401
    assert denied.get_json()["error"] == "unauthorized"

    repository = services["supervisor"]._command_repository

    def fail_insert(**_kwargs):
        from hub.infrastructure.supervisor_command_repository import SupervisorCommandRepositoryError
        raise SupervisorCommandRepositoryError("command_unavailable")

    monkeypatch.setattr(repository, "insert", fail_insert)
    failed = post_message(app, body={"text": "private marker", "client_token": "db-failure"})
    assert failed.status_code == 503
    assert failed.get_json()["error"] == "command_unavailable"
    assert "private marker" not in failed.get_data(as_text=True)
    assert "db-failure" not in failed.get_data(as_text=True)


def test_sqlite_failure_between_command_and_binding_rolls_back_both(native_hub):
    app, services = native_hub
    repository = services["supervisor"]._command_repository
    with sqlite3.connect(repository.db_path) as connection:
        connection.execute(
            "CREATE TRIGGER fail_message_binding BEFORE INSERT ON "
            "session_message_operations BEGIN SELECT RAISE(ABORT,'private sqlite marker'); END")

    response = post_message(app, body={
        "text": "rollback both writes", "client_token": "sqlite-trigger-token"})

    assert response.status_code == 503
    assert response.get_json()["error"] == "command_unavailable"
    assert "private sqlite marker" not in response.get_data(as_text=True)
    assert "rollback both writes" not in response.get_data(as_text=True)
    assert "sqlite-trigger-token" not in response.get_data(as_text=True)
    assert command_count(services) == 0
    with sqlite3.connect(repository.db_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM session_message_operations").fetchone()[0] == 0


def test_session_control_fails_closed_when_metadata_migration_is_unavailable(native_hub):
    app, services = native_hub
    services["sessions"].session_repo.management_available = False

    response = post_message(app, body={
        "text": "do not enqueue against unsupported metadata", "client_token": "store-down"})

    assert response.status_code == 503
    assert response.get_json()["error"] == "feature_unavailable"
    assert command_count(services) == 0


def test_supervisor_message_schema_migration_is_atomic_and_preserves_old_commands(
        tmp_path, monkeypatch):
    from hub.infrastructure.supervisor_command_repository import (
        SupervisorCommandRepository,
        SupervisorCommandRepositoryError,
    )

    db = tmp_path / "supervisor" / "commands.db"
    old_repo = SupervisorCommandRepository(db)
    old_repo.init()
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO supervisor_commands(command_id,machine_id,session_id,attempt_id,"
            "action,nonce,idempotency_key,wire_json,issued_s,expires_s,status) "
            "VALUES('old-command','m1','sess_native_1',NULL,'pause_session','old-nonce',"
            "NULL,'{}',1,100,'queued')")
        connection.execute("DROP TABLE session_message_operations")
        connection.execute("DROP TABLE supervisor_meta")

    failed_repo = SupervisorCommandRepository(db)
    migrate = failed_repo._migrate_session_message_operations

    def fail_after_create(connection):
        migrate(connection)
        raise sqlite3.OperationalError("private migration marker")

    monkeypatch.setattr(failed_repo, "_migrate_session_message_operations", fail_after_create)
    with pytest.raises(SupervisorCommandRepositoryError) as caught:
        failed_repo.init()
    assert caught.value.code == "command_unavailable"
    with sqlite3.connect(db) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        commands = connection.execute(
            "SELECT command_id FROM supervisor_commands").fetchall()
    assert "session_message_operations" not in tables
    assert "supervisor_meta" not in tables
    assert commands == [("old-command",)]

    migrated = SupervisorCommandRepository(db)
    migrated.init()
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT count(*) FROM session_message_operations").fetchone()[0] == 0
        assert connection.execute(
            "SELECT command_id FROM supervisor_commands").fetchone()[0] == "old-command"


@pytest.mark.parametrize("version", ["3", "-1", "broken", "9" * 5000])
def test_supervisor_schema_version_is_not_downgraded_or_repaired_implicitly(
        tmp_path, version):
    from hub.infrastructure.supervisor_command_repository import (
        SupervisorCommandRepository,
        SupervisorCommandRepositoryError,
    )

    db = tmp_path / "supervisor-version.db"
    repository = SupervisorCommandRepository(db)
    repository.init()
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE supervisor_meta SET value=? WHERE key='schema_version'",
            (version,))

    with pytest.raises(SupervisorCommandRepositoryError) as caught:
        SupervisorCommandRepository(db).init()

    assert caught.value.code == "command_unavailable"
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT value FROM supervisor_meta WHERE key='schema_version'").fetchone()[0] == version


def test_corrupt_supervisor_operation_schema_fails_closed(tmp_path):
    from hub.infrastructure.supervisor_command_repository import (
        SupervisorCommandRepository,
        SupervisorCommandRepositoryError,
    )

    db = tmp_path / "supervisor-corrupt-operation.db"
    repository = SupervisorCommandRepository(db)
    repository.init()
    with sqlite3.connect(db) as connection:
        connection.execute("DROP TABLE session_message_operations")
        connection.execute(
            "CREATE TABLE session_message_operations(token_digest TEXT,command_id TEXT)")

    with pytest.raises(SupervisorCommandRepositoryError) as caught:
        SupervisorCommandRepository(db).init()

    assert caught.value.code == "command_unavailable"


def test_supervisor_rejects_lookalike_message_schema_missing_constraints(tmp_path):
    from hub.infrastructure.supervisor_command_repository import (
        SupervisorCommandRepository,
        SupervisorCommandRepositoryError,
    )

    db = tmp_path / "supervisor-lookalike-operation.db"
    repository = SupervisorCommandRepository(db)
    repository.init()
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO supervisor_commands(command_id,machine_id,session_id,attempt_id,"
            "action,nonce,idempotency_key,wire_json,issued_s,expires_s,status) "
            "VALUES('kept-command','m1','session-1',NULL,'pause_session','kept-nonce',"
            "NULL,'{}',1,100,'queued')")
        connection.execute("DROP TABLE session_message_operations")
        connection.execute(
            "CREATE TABLE session_message_operations("
            "token_digest TEXT PRIMARY KEY,operator_id TEXT,session_id TEXT,"
            "operation TEXT,content_digest TEXT,command_id TEXT)")
        connection.execute(
            "CREATE UNIQUE INDEX idx_lookalike_command ON "
            "session_message_operations(command_id)")
        connection.execute(
            "UPDATE supervisor_meta SET value='1' WHERE key='schema_version'")

    with pytest.raises(SupervisorCommandRepositoryError) as caught:
        SupervisorCommandRepository(db).init()

    assert caught.value.code == "command_unavailable"
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT value FROM supervisor_meta WHERE key='schema_version'").fetchone()[0] == "1"
        assert connection.execute(
            "SELECT command_id FROM supervisor_commands").fetchall() == [("kept-command",)]
        assert connection.execute(
            "SELECT count(*) FROM session_message_operations").fetchone()[0] == 0


def test_valid_supervisor_v1_message_schema_adds_null_guard_and_preserves_operations(
        tmp_path):
    from hub.infrastructure.supervisor_command_repository import (
        SupervisorCommandRepository,
    )

    db = tmp_path / "supervisor-v1-operation.db"
    repository = SupervisorCommandRepository(db)
    repository.init()
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO supervisor_commands(command_id,machine_id,session_id,attempt_id,"
            "action,nonce,idempotency_key,wire_json,issued_s,expires_s,status) "
            "VALUES('legacy-command','m1','session-1',NULL,'append_user_turn','legacy-nonce',"
            "'legacy-idempotency','{}',1,100,'queued')")
        connection.execute("DROP TABLE session_message_operations")
        connection.execute(
            "CREATE TABLE session_message_operations("
            "token_digest TEXT PRIMARY KEY CHECK(length(token_digest)=64),"
            "operator_id TEXT NOT NULL,session_id TEXT NOT NULL,"
            "operation TEXT NOT NULL CHECK(operation='append_user_turn'),"
            "content_digest TEXT NOT NULL CHECK(length(content_digest)=64),"
            "command_id TEXT NOT NULL UNIQUE REFERENCES supervisor_commands(command_id))")
        connection.execute(
            "INSERT INTO session_message_operations VALUES(?,?,?,?,?,?)",
            ("a" * 64, "operator@example.test", "session-1", "append_user_turn",
             "b" * 64, "legacy-command"))
        connection.execute(
            "UPDATE supervisor_meta SET value='1' WHERE key='schema_version'")

    SupervisorCommandRepository(db).init()

    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT value FROM supervisor_meta WHERE key='schema_version'").fetchone()[0] == "1"
        token_column = next(row for row in connection.execute(
            "PRAGMA table_info(session_message_operations)") if row[1] == "token_digest")
        assert token_column[3] == 0
        assert connection.execute(
            "SELECT token_digest,operator_id,session_id,operation,content_digest,command_id "
            "FROM session_message_operations").fetchall() == [(
                "a" * 64, "operator@example.test", "session-1", "append_user_turn",
                "b" * 64, "legacy-command")]
        with pytest.raises(sqlite3.IntegrityError, match="token_digest_required"):
            connection.execute(
                "INSERT INTO session_message_operations VALUES(NULL,?,?,?,?,?)",
                ("operator@example.test", "session-1", "append_user_turn",
                 "c" * 64, "another-command"))
        with pytest.raises(sqlite3.IntegrityError, match="token_digest_required"):
            connection.execute(
                "UPDATE session_message_operations SET token_digest=NULL WHERE command_id=?",
                ("legacy-command",))


def test_supervisor_v1_null_token_migration_failure_preserves_legacy_database(tmp_path):
    from hub.infrastructure.supervisor_command_repository import (
        SupervisorCommandRepository,
        SupervisorCommandRepositoryError,
    )

    db = tmp_path / "supervisor-v1-null-operation.db"
    SupervisorCommandRepository(db).init()
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO supervisor_commands(command_id,machine_id,session_id,attempt_id,"
            "action,nonce,idempotency_key,wire_json,issued_s,expires_s,status) "
            "VALUES('legacy-null-command','m1','session-1',NULL,'append_user_turn',"
            "'legacy-null-nonce','legacy-null-idempotency','{}',1,100,'queued')")
        connection.execute("DROP TABLE session_message_operations")
        connection.execute(
            "CREATE TABLE session_message_operations("
            "token_digest TEXT PRIMARY KEY CHECK(length(token_digest)=64),"
            "operator_id TEXT NOT NULL,session_id TEXT NOT NULL,"
            "operation TEXT NOT NULL CHECK(operation='append_user_turn'),"
            "content_digest TEXT NOT NULL CHECK(length(content_digest)=64),"
            "command_id TEXT NOT NULL UNIQUE REFERENCES supervisor_commands(command_id))")
        connection.execute(
            "INSERT INTO session_message_operations VALUES(NULL,?,?,?,?,?)",
            ("operator@example.test", "session-1", "append_user_turn",
             "b" * 64, "legacy-null-command"))
        connection.execute(
            "UPDATE supervisor_meta SET value='1' WHERE key='schema_version'")

    with pytest.raises(SupervisorCommandRepositoryError) as caught:
        SupervisorCommandRepository(db).init()

    assert caught.value.code == "command_unavailable"
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT value FROM supervisor_meta WHERE key='schema_version'").fetchone()[0] == "1"
        assert connection.execute(
            "SELECT token_digest,command_id FROM session_message_operations").fetchall() == [
                (None, "legacy-null-command")]
        assert connection.execute(
            "SELECT command_id FROM supervisor_commands").fetchall() == [
                ("legacy-null-command",)]


@pytest.mark.parametrize("not_null", [False, True])
def test_preversioned_supervisor_message_table_initializes_without_losing_rows(
        tmp_path, not_null):
    from hub.infrastructure.supervisor_command_repository import SupervisorCommandRepository

    db = tmp_path / "supervisor-preversioned.db"
    SupervisorCommandRepository(db).init()
    with sqlite3.connect(db) as connection:
        connection.execute("DROP TABLE session_message_operations")
        connection.execute(
            "CREATE TABLE session_message_operations("
            f"token_digest TEXT {'NOT NULL ' if not_null else ''}PRIMARY KEY "
            "CHECK(length(token_digest)=64),operator_id TEXT NOT NULL,"
            "session_id TEXT NOT NULL,operation TEXT NOT NULL "
            "CHECK(operation='append_user_turn'),content_digest TEXT NOT NULL "
            "CHECK(length(content_digest)=64),command_id TEXT NOT NULL UNIQUE "
            "REFERENCES supervisor_commands(command_id))")
        connection.execute(
            "INSERT INTO supervisor_commands(command_id,machine_id,session_id,action,"
            "nonce,wire_json,issued_s,expires_s,status) "
            "VALUES('kept','m1','s1','append_user_turn','kept-nonce','{}',1,100,'queued')")
        connection.execute(
            "INSERT INTO session_message_operations VALUES(?,?,?,?,?,?)",
            ("a" * 64, "operator", "s1", "append_user_turn", "b" * 64, "kept"))
        connection.execute("DELETE FROM supervisor_meta WHERE key='schema_version'")

    SupervisorCommandRepository(db).init()
    # Repeated startup also exercises validation of a populated v1-compatible table.
    SupervisorCommandRepository(db).init()

    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT value FROM supervisor_meta WHERE key='schema_version'").fetchone() == ("1",)
        token_column = next(row for row in connection.execute(
            "PRAGMA table_info(session_message_operations)") if row[1] == "token_digest")
        assert token_column[3] == 0
        assert connection.execute(
            "SELECT token_digest,command_id FROM session_message_operations").fetchall() == [
                ("a" * 64, "kept")]
        assert connection.execute(
            "SELECT command_id FROM supervisor_commands").fetchall() == [("kept",)]


def test_supervisor_v2_schema_downgrades_to_prior_reader_compatible_shape(tmp_path):
    from hub.infrastructure.supervisor_command_repository import SupervisorCommandRepository

    db = tmp_path / "supervisor-v2-rollback-compatible.db"
    SupervisorCommandRepository(db).init()
    with sqlite3.connect(db) as connection:
        connection.execute("DROP TABLE session_message_operations")
        connection.execute(
            "CREATE TABLE session_message_operations("
            "token_digest TEXT NOT NULL PRIMARY KEY CHECK(length(token_digest)=64),"
            "operator_id TEXT NOT NULL,session_id TEXT NOT NULL,"
            "operation TEXT NOT NULL CHECK(operation='append_user_turn'),"
            "content_digest TEXT NOT NULL CHECK(length(content_digest)=64),"
            "command_id TEXT NOT NULL UNIQUE REFERENCES supervisor_commands(command_id))")
        connection.execute(
            "INSERT INTO supervisor_commands(command_id,machine_id,session_id,action,"
            "nonce,wire_json,issued_s,expires_s,status) "
            "VALUES('v2-command','m1','s1','append_user_turn','v2-nonce','{}',1,100,'queued')")
        connection.execute(
            "INSERT INTO session_message_operations VALUES(?,?,?,?,?,?)",
            ("d" * 64, "operator", "s1", "append_user_turn", "e" * 64, "v2-command"))
        connection.execute("UPDATE supervisor_meta SET value='2' WHERE key='schema_version'")

    SupervisorCommandRepository(db).init()

    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT value FROM supervisor_meta WHERE key='schema_version'").fetchone() == ("1",)
        token_column = next(row for row in connection.execute(
            "PRAGMA table_info(session_message_operations)") if row[1] == "token_digest")
        assert token_column[3] == 0
        assert connection.execute(
            "SELECT token_digest,command_id FROM session_message_operations").fetchall() == [
                ("d" * 64, "v2-command")]
        with pytest.raises(sqlite3.IntegrityError, match="token_digest_required"):
            connection.execute(
                "INSERT INTO session_message_operations VALUES(NULL,?,?,?,?,?)",
                ("operator", "s1", "append_user_turn", "f" * 64, "v2-command"))


def test_supervisor_v2_compatibility_migration_failure_rolls_back_atomically(
        tmp_path, monkeypatch):
    from hub.infrastructure.supervisor_command_repository import (
        SupervisorCommandRepository, SupervisorCommandRepositoryError,
    )

    db = tmp_path / "supervisor-v2-migration-rollback.db"
    SupervisorCommandRepository(db).init()
    with sqlite3.connect(db) as connection:
        connection.execute("DROP TABLE session_message_operations")
        connection.execute(
            "CREATE TABLE session_message_operations("
            "token_digest TEXT NOT NULL PRIMARY KEY CHECK(length(token_digest)=64),"
            "operator_id TEXT NOT NULL,session_id TEXT NOT NULL,"
            "operation TEXT NOT NULL CHECK(operation='append_user_turn'),"
            "content_digest TEXT NOT NULL CHECK(length(content_digest)=64),"
            "command_id TEXT NOT NULL UNIQUE REFERENCES supervisor_commands(command_id))")
        connection.execute(
            "INSERT INTO supervisor_commands(command_id,machine_id,session_id,action,"
            "nonce,wire_json,issued_s,expires_s,status) "
            "VALUES('v2-command','m1','s1','append_user_turn','v2-nonce','{}',1,100,'queued')")
        connection.execute(
            "INSERT INTO session_message_operations VALUES(?,?,?,?,?,?)",
            ("d" * 64, "operator", "s1", "append_user_turn", "e" * 64, "v2-command"))
        connection.execute("UPDATE supervisor_meta SET value='2' WHERE key='schema_version'")

    repository = SupervisorCommandRepository(db)
    migrate = repository._migrate_session_message_operations

    def fail_after_conversion(connection):
        migrate(connection)
        raise sqlite3.OperationalError("private conversion failure")

    monkeypatch.setattr(repository, "_migrate_session_message_operations", fail_after_conversion)
    with pytest.raises(SupervisorCommandRepositoryError) as caught:
        repository.init()
    assert caught.value.code == "command_unavailable"

    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT value FROM supervisor_meta WHERE key='schema_version'").fetchone() == ("2",)
        token_column = next(row for row in connection.execute(
            "PRAGMA table_info(session_message_operations)") if row[1] == "token_digest")
        assert token_column[3] == 1
        assert connection.execute(
            "SELECT token_digest,command_id FROM session_message_operations").fetchall() == [
                ("d" * 64, "v2-command")]
        names = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "session_message_operations_v2" not in names


def test_supervisor_v2_migration_does_not_drop_unrelated_trigger_on_name_collision(
        tmp_path):
    from hub.infrastructure.supervisor_command_repository import (
        SupervisorCommandRepository, SupervisorCommandRepositoryError,
    )

    db = tmp_path / "supervisor-v2-trigger-collision.db"
    SupervisorCommandRepository(db).init()
    with sqlite3.connect(db) as connection:
        connection.execute("DROP TABLE session_message_operations")
        connection.execute(
            "CREATE TABLE session_message_operations("
            "token_digest TEXT NOT NULL PRIMARY KEY CHECK(length(token_digest)=64),"
            "operator_id TEXT NOT NULL,session_id TEXT NOT NULL,"
            "operation TEXT NOT NULL CHECK(operation='append_user_turn'),"
            "content_digest TEXT NOT NULL CHECK(length(content_digest)=64),"
            "command_id TEXT NOT NULL UNIQUE REFERENCES supervisor_commands(command_id))")
        connection.execute(
            "CREATE TRIGGER session_message_operations_require_token_insert "
            "AFTER UPDATE ON supervisor_commands BEGIN SELECT 1; END")
        connection.execute("UPDATE supervisor_meta SET value='2' WHERE key='schema_version'")

    with pytest.raises(SupervisorCommandRepositoryError) as caught:
        SupervisorCommandRepository(db).init()
    assert caught.value.code == "command_unavailable"

    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT value FROM supervisor_meta WHERE key='schema_version'").fetchone() == ("2",)
        assert connection.execute(
            "SELECT tbl_name FROM sqlite_master WHERE type='trigger' AND "
            "name='session_message_operations_require_token_insert'").fetchone() == (
                "supervisor_commands",)


def test_preversioned_corrupt_supervisor_message_table_has_bounded_error(tmp_path):
    from hub.infrastructure.supervisor_command_repository import (
        SupervisorCommandRepository, SupervisorCommandRepositoryError,
    )

    db = tmp_path / "supervisor-preversioned-corrupt.db"
    SupervisorCommandRepository(db).init()
    with sqlite3.connect(db) as connection:
        connection.execute("DROP TABLE session_message_operations")
        connection.execute("CREATE TABLE session_message_operations(command_id TEXT)")
        connection.execute("DELETE FROM supervisor_meta WHERE key='schema_version'")

    with pytest.raises(SupervisorCommandRepositoryError, match="^command_unavailable$"):
        SupervisorCommandRepository(db).init()
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT value FROM supervisor_meta WHERE key='schema_version'").fetchone() is None
        assert [row[1] for row in connection.execute(
            "PRAGMA table_info(session_message_operations)")] == ["command_id"]


@pytest.mark.parametrize("missing_check", ["operation", "content_digest"])
@pytest.mark.parametrize("version", [1, 2])
def test_populated_supervisor_schema_cannot_mask_missing_check_with_duplicate_token(
        tmp_path, missing_check, version):
    from hub.infrastructure.supervisor_command_repository import (
        SupervisorCommandRepository, SupervisorCommandRepositoryError,
    )

    db = tmp_path / "supervisor-populated-lookalike.db"
    SupervisorCommandRepository(db).init()
    with sqlite3.connect(db) as connection:
        connection.execute("DROP TABLE session_message_operations")
        operation_check = "" if missing_check == "operation" else " CHECK(operation='append_user_turn')"
        content_check = "" if missing_check == "content_digest" else " CHECK(length(content_digest)=64)"
        probe_operation = "invalid_operation" if missing_check == "operation" else "append_user_turn"
        probe_content_digest = "short" if missing_check == "content_digest" else "b" * 64
        connection.execute(
            "CREATE TABLE session_message_operations("
            f"token_digest TEXT {'NOT NULL ' if version == 2 else ''}PRIMARY KEY "
            "CHECK(length(token_digest)=64),operator_id TEXT NOT NULL,session_id TEXT NOT NULL,"
            f"operation TEXT NOT NULL{operation_check},content_digest TEXT NOT NULL{content_check},"
            "command_id TEXT NOT NULL UNIQUE REFERENCES supervisor_commands(command_id))")
        connection.execute(
            "INSERT INTO supervisor_commands(command_id,machine_id,session_id,action,"
            "nonce,wire_json,issued_s,expires_s,status) "
            "VALUES('kept','m1','s1','append_user_turn','kept-nonce','{}',1,100,'queued')")
        connection.execute(
            "INSERT INTO session_message_operations VALUES(?,?,?,?,?,?)",
            ("a" * 64, "operator", "s1", probe_operation, probe_content_digest, "kept"))
        connection.execute("UPDATE supervisor_meta SET value=? WHERE key='schema_version'",
                           (str(version),))

    with pytest.raises(SupervisorCommandRepositoryError, match="^command_unavailable$"):
        SupervisorCommandRepository(db).init()

    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT value FROM supervisor_meta WHERE key='schema_version'").fetchone() == (str(version),)
        assert connection.execute(
            "SELECT token_digest,command_id FROM session_message_operations").fetchall() == [
                ("a" * 64, "kept")]
        assert connection.execute(
            "SELECT command_id FROM supervisor_commands").fetchall() == [("kept",)]
