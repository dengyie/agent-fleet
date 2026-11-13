"""Real operator HTTP boundary and handoff UI journey against disposable stores."""
import os
from pathlib import Path
import subprocess
from threading import Thread

import pytest
from werkzeug.serving import make_server

from hub.bootstrap import create_app
from hub.config import FleetConfig

ROOT = Path(__file__).resolve().parents[1]
OWNER = "handoff@example.test"


def make_handoff_app(tmp_path, *, supervisor=True, tasks=True):
    app = create_app(FleetConfig.from_root(tmp_path, frontend_dir=ROOT / "frontend",
        ingest_token="ingest-only", dev_operator=OWNER,
        runner_credentials={"mac-local": "runner-only"},
        project_whitelist={"mac-local": ["agent-fleet", "other"]},
        tasks_enabled=tasks, supervisor_enabled=supervisor, session_repositories_enabled=True,
        session_encryption_raw=b"h" * 32))
    service = app.extensions["fleet"]["services"]["sessions"]
    source = None
    if tasks:
        source, _ = app.extensions["fleet"]["services"]["tasks"].task_repo.create_task(
            machine="mac-local", agent_type="codex", project="agent-fleet",
            instruction="Repair agent dispatch", requested_by=OWNER, confirm=True)
    service.session_repo.upsert_session({"session_id": "source-session", "machine_id": "mac-local",
        "managed": True, "process_group_id": "group-source", "capture_quality": "structured",
        **({"attempt_id": source["attempt_id"]} if source else {})})
    service.session_repo.upsert_session({"session_id": "unmanaged-session", "machine_id": "mac-local",
        "managed": False, "capture_quality": "best_effort"})
    envelope = {"schema_version": 1, "stream_id": "source-stream", "machine_id": "mac-local",
        "session_id": "source-session", "process_group_id": "group-source",
        "capture_quality": "structured", "source": "bridge", "emitted_at": "2026-10-11T00:00:00Z",
        **({"attempt_id": source["attempt_id"]} if source else {})}
    service.ingest_events([{**envelope, "event_id": "source-goal", "sequence": 1,
        "kind": "user_message", "payload": {"text": "Repair agent dispatch", "is_complete": True}},
        {**envelope, "event_id": "source-conclusion", "sequence": 2,
        "kind": "assistant_message", "payload": {"text": "API_KEY=must-not-leak", "is_complete": True}}])
    app.extensions["fleet"]["services"]["tasks"].observation_repo.read_current = lambda machine: {"reachable": True}
    return app


def body(**extra):
    return {"target_machine": "mac-local", "target_agent": "codex", "target_project": "agent-fleet",
            "instruction_override": "", "client_token": "browser-retry", **extra}


def test_preview_create_retry_gate_and_no_source_mutation(tmp_path):
    app = make_handoff_app(tmp_path)
    client = app.test_client()
    service = app.extensions["fleet"]["services"]["sessions"]
    before = service.session_repo.get_session("source-session")
    preview = client.get("/api/sessions/source-session/handoff")
    assert preview.status_code == 200
    assert preview.json["can_create"] is True
    assert "must-not-leak" not in preview.text
    first = client.post("/api/sessions/source-session/handoff", json=body())
    assert first.status_code == 202, first.json
    retry = client.post("/api/sessions/source-session/handoff", json=body())
    assert retry.status_code == 202 and retry.json == first.json
    task = app.extensions["fleet"]["services"]["tasks"].task_repo.get_task(first.json["task_id"])
    assert task["gate"]["state"] == "pending"
    assert service.session_repo.get_session("source-session") == before
    assert len(app.extensions["fleet"]["services"]["tasks"].task_repo.list_tasks()) == 2


@pytest.mark.parametrize("headers", [{"X-Agent-Fleet-Token": "ingest-only"},
    {"X-Runner-Credential": "mac-local:runner-only"}])
def test_foreign_credentials_cannot_preview_or_create(tmp_path, headers):
    client = make_handoff_app(tmp_path).test_client()
    assert client.get("/api/sessions/source-session/handoff", headers=headers).status_code == 401
    assert client.post("/api/sessions/source-session/handoff", headers=headers, json=body()).status_code == 401


@pytest.mark.parametrize("supervisor,tasks", [(False, True), (True, False)])
def test_closed_control_gates_keep_read_only_preview(tmp_path, supervisor, tasks):
    client = make_handoff_app(tmp_path, supervisor=supervisor, tasks=tasks).test_client()
    preview = client.get("/api/sessions/source-session/handoff")
    assert preview.status_code == 200 and preview.json["can_create"] is False
    assert preview.json["snapshot"]["objective"] == "Repair agent dispatch"
    assert client.post("/api/sessions/source-session/handoff", json=body()).status_code == 503


def test_cross_project_unknown_fields_and_unmanaged_fail_closed(tmp_path):
    client = make_handoff_app(tmp_path).test_client()
    for url, data, code in [("source-session", body(target_project="other"), 409),
        ("source-session", body(raw="injected"), 400), ("unmanaged-session", body(), 409)]:
        assert client.post(f"/api/sessions/{url}/handoff", json=data).status_code == code
    preview = client.get("/api/sessions/unmanaged-session/handoff")
    assert preview.json["snapshot"]["capture_quality"] == "best_effort"
    assert preview.json["can_create"] is False


def test_creation_failure_has_bounded_error_and_keeps_source(tmp_path, monkeypatch):
    app = make_handoff_app(tmp_path)
    service = app.extensions["fleet"]["services"]["handoffs"]
    before = service.sessions.get_session("source-session")
    def unavailable(*args, **kwargs):
        raise OSError("/private/secret/file API_KEY=internal-value")
    monkeypatch.setattr(service.tasks.task_repo, "get_handoff_receipt", unavailable)
    result = app.test_client().post("/api/sessions/source-session/handoff", json=body())
    assert result.status_code == 503
    assert "internal-value" not in result.text and "/private" not in result.text
    assert service.sessions.get_session("source-session") == before


def test_handoff_in_real_browser(tmp_path):
    if not os.environ.get("FLEET_PLAYWRIGHT_MODULE"):
        pytest.skip("Set FLEET_PLAYWRIGHT_MODULE to run Chromium acceptance")
    app = make_handoff_app(tmp_path)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    serving = Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        result = subprocess.run(["node", str(ROOT / "tests/fixtures/frontend/handoff_browser.cjs"),
            f"http://127.0.0.1:{server.server_port}"], capture_output=True, text=True, timeout=90)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.shutdown()
        server.server_close()
        serving.join(5)
