"""Security regression chain across the CoDock-inspired management surfaces."""
from datetime import datetime, timezone
import sqlite3

import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig

OWNER = "operator@example.test"
STAMP = datetime(2026, 10, 11, tzinfo=timezone.utc).timestamp()


class SupervisorRecorder:
    def __init__(self):
        self.commands = []

    def enqueue(self, *args, **kwargs):
        self.commands.append((args, kwargs))
        return {"command_id": "controlled-command"}


@pytest.fixture
def management(tmp_path):
    app = create_app(FleetConfig.from_root(tmp_path, dev_operator=OWNER,
        ingest_token="observe-only", runner_credentials={"node-1": "runner-only"},
        project_whitelist={"node-1": ["project-1", "project-2"]}, tasks_enabled=True,
        platform_enabled=True, supervisor_enabled=True, session_repositories_enabled=True,
        session_search_enabled=True, session_encryption_raw=b"s" * 32))
    services = app.extensions["fleet"]["services"]
    services["tasks"].observation_repo.read_current = lambda machine: {"reachable": True}
    source, _ = services["tasks"].task_repo.create_task(machine="node-1", agent_type="codex",
        project="project-1", instruction="security regression", requested_by=OWNER, confirm=True)
    session = services["sessions"]
    session.supervisor = SupervisorRecorder()
    base = {"schema_version": 1, "stream_id": "security-stream", "machine_id": "node-1",
        "session_id": "security-session", "process_group_id": "security-group", "attempt_id": source["attempt_id"],
        "source": "hooks", "capture_quality": "structured", "emitted_at": "2026-10-11T00:00:00Z"}
    rows = [
        ("session_start", {"agent_family": "codex", "adapter": "hooks", "interactive_questions": True}),
        ("user_message", {"text": "修复安全检查 API_KEY=source-credential", "is_complete": True}),
        ("source_record", {"text": "RAWONLY hidden original transcript", "is_complete": True}),
        ("tool_call", {"tool_name": "write_file", "call_id": "unsafe-write",
                       "arguments": '{"path":"../secret.json"}'}),
        ("interaction_request", {"request_id": "question-1", "question_type": "single",
            "prompt": "检查 API_KEY=question-credential", "options": [{"id": "yes", "label": "继续 API_KEY=choice-credential"}],
            "allow_custom": False, "tool_name": "AskUserQuestion"}),
    ]
    response = session.ingest_events([{**base, "sequence": index, "event_id": f"security-event-{index}",
                                      "kind": kind, "payload": payload}
                                     for index, (kind, payload) in enumerate(rows, 1)])
    assert not response.get("rejected"), response
    session.session_repo.upsert_session({"session_id": "unmanaged-security", "machine_id": "node-1",
                                        "managed": False, "capture_quality": "best_effort"})
    usage = services["usage_repository"]
    for index, owner in enumerate((OWNER, "other@example.test")):
        reservation = usage.admit(owner, "model-a", "usage-run-" + str(index), 1, 1, now=STAMP)
        usage.settle(reservation, {"input_tokens": 7, "output_tokens": 2}, "succeeded", now=STAMP)
    return app, services


def handoff_body(**extra):
    return {"target_machine": "node-1", "target_project": "project-1", "target_agent": "codex",
            "instruction_override": "", "client_token": "same-client-value", **extra}


def answer_body():
    return {"request_id": "question-1", "client_token": "answer-value", "selected": ["yes"], "text": ""}


@pytest.mark.parametrize("route,method", [("/api/sessions/security-session/handoff", "GET"),
    ("/api/sessions/security-session/handoff", "POST"), ("/api/session-search?q=安全", "GET"),
    ("/api/platform/v1/usage", "GET"), ("/api/sessions/security-session/messages", "POST")])
@pytest.mark.parametrize("headers", [{"X-Agent-Fleet-Token": "observe-only"},
    {"X-Runner-Credential": "node-1:runner-only"}])
def test_machine_credentials_never_gain_operator_capabilities(management, route, method, headers):
    app, services = management
    request = app.test_client().open(route, method=method, headers=headers,
                                   json=answer_body() if route.endswith("messages") else handoff_body())
    assert request.status_code == 401, request.json
    assert len(services["tasks"].task_repo.list_tasks()) == 1
    assert services["sessions"].supervisor.commands == []


def test_anonymous_cannot_read_management_surfaces(management):
    app, _ = management
    app.config["DEV_OPERATOR"] = None
    client = app.test_client()
    for route in ("/api/sessions/security-session/handoff", "/api/session-search?q=安全", "/api/platform/v1/usage"):
        assert client.get(route).status_code == 401


def test_redacted_search_and_handoff_exclude_raw_and_paths(management):
    app, services = management
    client = app.test_client()
    preview = client.get("/api/sessions/security-session/handoff")
    assert preview.status_code == 200
    assert preview.json["snapshot"]["changed_files"] == []
    for marker in ("source-credential", "RAWONLY", "../secret.json"):
        assert marker not in preview.text
    result = client.get("/api/session-search", query_string={"q": "安全"})
    assert result.status_code == 200 and result.json["items"], result.json
    assert "source-credential" not in result.text
    raw = client.get("/api/session-search", query_string={"q": "RAWONLY"})
    assert raw.status_code == 200 and raw.json["items"] == []
    raw = services["sessions"].transcript_repo.read_raw("security-session", "security-event-3", actor=OWNER)
    assert "RAWONLY" in str(raw)


def test_handoff_scope_restart_idempotency_and_new_attempt(management):
    app, services = management
    client = app.test_client()
    before = services["sessions"].session_repo.get_session("security-session")
    rejected = client.post("/api/sessions/security-session/handoff", json=handoff_body(target_project="project-2"))
    assert rejected.status_code == 409
    first = client.post("/api/sessions/security-session/handoff", json=handoff_body())
    assert first.status_code == 202
    services["tasks"].task_repo.init()
    second = client.post("/api/sessions/security-session/handoff", json=handoff_body())
    assert second.json == first.json
    task = services["tasks"].task_repo.get_task(first.json["task_id"])
    assert task["attempt_id"] != before["attempt_id"]
    assert task["client_token"] != handoff_body()["client_token"] and task["gate"]["state"] == "pending"
    assert services["sessions"].session_repo.get_session("security-session") == before
    assert not {"nonce", "client_token", "lease"}.intersection(first.json)


def test_interaction_unmanaged_and_capability_guards(management):
    app, services = management
    client = app.test_client()
    unmanaged = client.post("/api/sessions/unmanaged-security/messages", json=answer_body())
    assert unmanaged.status_code == 409 and unmanaged.json["error"] == "session_not_managed"
    session = services["sessions"]
    session.session_repo.set_capabilities("security-session", [])
    blocked = client.post("/api/sessions/security-session/messages", json=answer_body())
    assert blocked.status_code == 503 and blocked.json["error"] == "interaction_unavailable"
    assert session.supervisor.commands == []


def test_interaction_source_secrets_are_redacted_in_public_card_and_dispatch(management):
    app, services = management
    client = app.test_client()
    response = client.get("/api/sessions/security-session/events")
    question, = [row for row in response.json["events"] if row["kind"] == "interaction_request"]
    assert "question-credential" not in str(question)
    assert "choice-credential" not in str(question)
    receipt = client.post("/api/sessions/security-session/messages", json=answer_body())
    assert receipt.status_code == 202, receipt.json
    again = client.post("/api/sessions/security-session/messages", json=answer_body())
    assert again.json == receipt.json
    commands = services["sessions"].supervisor.commands
    assert len(commands) == 1
    assert "choice-credential" not in commands[0][1]["payload"]["text"]


def test_usage_owner_cannot_be_overridden_or_mixed(management):
    app, _ = management
    client = app.test_client()
    query = {"from": "2026-10-11", "to": "2026-10-12"}
    response = client.get("/api/platform/v1/usage", query_string=query)
    assert response.status_code == 200
    assert sum(row["requests"] for row in response.json["items"]) == 1
    assert client.get("/api/platform/v1/usage", query_string={**query, "owner_id": "other@example.test"}).status_code == 400
    assert client.get("/api/platform/v1/usage?group_by=day&group_by=model").status_code == 400


def test_unavailable_search_and_usage_do_not_expose_storage_errors(management, monkeypatch):
    app, services = management
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("/private/keys/storage API_KEY=private-value")
    monkeypatch.setattr(services["session_search"].repository, "_connect", broken)
    response = app.test_client().get("/api/session-search?q=安全")
    assert response.status_code == 503
    assert "private-value" not in response.text and "/private" not in response.text
    monkeypatch.setattr(services["usage_repository"], "_connect", broken)
    response = app.test_client().get("/api/platform/v1/usage")
    assert response.status_code == 503
    assert "private-value" not in response.text and "/private" not in response.text
