"""Native launch directory and accepted task replay through real Hub/SQLite."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import sqlite3
from threading import Barrier

import pytest

from agent_profiles import EXECUTABLE_AGENT_TYPES
from hub.application.errors import ApplicationError
from hub.domain.task import DomainError
from support.hub_env import HubStores


ACTOR = "operator@example.test"
SUBMISSION = {
    "machine": "mac-local", "agent_type": "codex", "project": "agent-fleet",
    "instruction": "保留原生身份，检查测试。", "client_token": "native-web-acceptance",
}


@pytest.fixture
def hub(tmp_path):
    stores = HubStores(tmp_path)
    stores.store.save_snapshot("mac-local", {
        "machine": "mac-local", "reachable": True, "timestamp": "2026-11-21T12:00:00Z",
        "agents": {"codex": {"installed": True, "exe_path": "/private/codex"},
                   "claude_code": {"error": "probe failed: private"},
                   "pi": {"installed": False}, "hermes": {}},
        "credential": "private-marker",
    })
    app = stores.make_app(dev_operator=ACTOR, ingest_token="fixture-ingest",
        project_whitelist={"mac-local": ["agent-fleet"], "win-local": ["windows-project"]})
    return stores, app, app.test_client()


def _assert_conflict(response):
    assert response.status_code == 409, response.json
    assert response.json["error"] == "idempotency_conflict"
    assert response.json["ok"] is False and response.json["request_id"]
    assert SUBMISSION["instruction"] not in response.get_data(as_text=True)
    assert SUBMISSION["client_token"] not in response.get_data(as_text=True)
    assert "task" not in response.json


def _counts(stores):
    with closing(sqlite3.connect(stores.task_db)) as conn:
        return tuple(conn.execute(sql).fetchone()[0] for sql in (
            "SELECT count(*) FROM tasks", "SELECT count(*) FROM audit WHERE action='create_task'"))


def test_target_directory_projects_registry_and_installation_tristate(hub):
    stores, app, client = hub
    before = _counts(stores)
    response = client.get("/api/machines/mac-local/task-targets")
    assert response.status_code == 200, response.json
    directory = response.json
    assert directory == app.extensions["fleet"]["services"]["tasks"].targets("mac-local")
    assert set(directory) == {"machine", "projects", "observed_at", "agents"}
    assert directory["projects"] == ["agent-fleet"]
    assert directory["observed_at"] == "2026-11-21T12:00:00Z"
    assert set(directory["agents"]) == set(EXECUTABLE_AGENT_TYPES)
    assert directory["agents"]["codex"] == {"installed": True}
    assert directory["agents"]["pi"] == {"installed": False}
    assert directory["agents"]["claude_code"] == {"installed": None}
    assert directory["agents"]["hermes"] == {"installed": None}
    assert directory["agents"]["zcode"] == {"installed": False}
    assert "private" not in response.get_data(as_text=True)
    assert _counts(stores) == before


@pytest.mark.parametrize("machine,projects", [("win-local", ["windows-project"]), ("unknown", [])])
def test_target_directory_retains_projects_without_observations(hub, machine, projects):
    _, _, client = hub
    response = client.get(f"/api/machines/{machine}/task-targets")
    assert response.status_code == 200
    assert response.json["projects"] == projects
    assert response.json["observed_at"] is None
    assert all(agent == {"installed": None} for agent in response.json["agents"].values())


def test_target_directory_invalid_machine_and_unavailable_task_store(hub):
    _, app, client = hub
    invalid = client.get("/api/machines/bad%20machine/task-targets")
    assert invalid.status_code == 400
    assert invalid.json["error"] == "invalid_machine" and invalid.json["request_id"]
    app.config["TASKS_ENABLED"] = False
    assert client.get("/api/machines/mac-local/task-targets").status_code == 200
    assert client.post("/api/tasks", json=SUBMISSION).status_code == 503


def test_target_directory_never_opens_task_storage(hub, monkeypatch):
    _, app, client = hub

    def forbidden():
        raise AssertionError("target discovery does not depend on SQLite")

    monkeypatch.setattr(app.extensions["fleet"]["services"]["tasks"].task_repo, "_connect", forbidden)
    assert client.get("/api/machines/mac-local/task-targets").status_code == 200


def test_target_directory_account_admin_boundary(tmp_path):
    from hub.bootstrap import create_app
    from hub.config import FleetConfig
    from tests.test_account_http import ORIGIN, PASSWORD, post, signup

    mail = []
    app = create_app(FleetConfig.from_root(tmp_path, accounts={"enabled": True,
        "origin": ORIGIN, "registration": "open", "sender": lambda *args: mail.append(args)}))
    app.extensions["accounts"].bootstrap_admin("admin@example.test", PASSWORD)
    admin, user, anonymous = app.test_client(), app.test_client(), app.test_client()
    assert post(admin, "login", {"email": "admin@example.test", "password": PASSWORD}).status_code == 200
    assert signup(user, mail, "reader@example.test").status_code == 200
    url = "/api/machines/mac-local/task-targets"
    assert anonymous.get(url, base_url=ORIGIN).status_code == 401
    assert user.get(url, base_url=ORIGIN).status_code == 403
    response = admin.get(url, base_url=ORIGIN)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert admin.get(url, base_url=ORIGIN, headers={"X-Runner-Credential": "foreign"}).status_code == 401


@pytest.mark.parametrize("headers", [{}, {"X-Agent-Fleet-Token": "fixture-ingest"},
    {"X-Runner-Credential": "mac-local:fixture"}, {"X-Supervisor-Credential": "fixture"},
    {"X-Platform-Node-Credential": "fixture"}])
def test_target_directory_requires_operator_domain(hub, headers):
    _, app, client = hub
    # Even the dev fallback must not turn another credential domain into an operator.
    if not headers:
        app.config["DEV_OPERATOR"] = None
    response = client.get("/api/machines/mac-local/task-targets", headers=headers)
    assert response.status_code == 401
    assert response.json["request_id"]


@pytest.mark.parametrize("change", ["offline", "uninstalled", "unregistered", "observation_error"])
def test_accepted_submission_recovers_before_current_launch_policy(hub, monkeypatch, change):
    stores, app, client = hub
    original = client.post("/api/tasks", json=SUBMISSION)
    assert original.status_code == 201
    service = app.extensions["fleet"]["services"]["tasks"]
    if change == "offline":
        stores.store.save_snapshot("mac-local", {"machine": "mac-local", "reachable": False})
    elif change == "uninstalled":
        stores.store.save_snapshot("mac-local", {"machine": "mac-local", "reachable": True,
                                                 "agents": {"codex": {"installed": False}}})
    elif change == "unregistered":
        monkeypatch.setattr(service.hosts, "project_allowed", lambda *args: False)
    else:
        def unavailable(machine):
            raise OSError("private observation failure")
        monkeypatch.setattr(service.observation_repo, "read_current", unavailable)
    replay = client.post("/api/tasks", json=SUBMISSION)
    assert replay.status_code == 200, replay.json
    assert replay.json == {**original.json, "created": False}
    assert _counts(stores) == (1, 1)
    events = app.extensions["fleet"]["publisher"].read_recent(100)
    assert len([event for event in events if event["event"] == "task_queued"]) == 1
    if change != "observation_error":
        fresh = client.post("/api/tasks", json={**SUBMISSION, "client_token": "fresh"})
        assert fresh.status_code == 400
        assert _counts(stores) == (1, 1)


@pytest.mark.parametrize("field,value", [("machine", "win-local"), ("agent_type", "pi"),
    ("project", "other-project"), ("instruction", "Do something else"), ("confirm", True)])
def test_token_cannot_identify_a_different_request(hub, field, value):
    stores, _, client = hub
    assert client.post("/api/tasks", json=SUBMISSION).status_code == 201
    _assert_conflict(client.post("/api/tasks", json={**SUBMISSION, field: value}))
    assert _counts(stores) == (1, 1)


def test_token_cannot_disclose_another_actor_submission(hub):
    stores, app, client = hub
    assert client.post("/api/tasks", json=SUBMISSION).status_code == 201
    app.config["DEV_OPERATOR"] = "another-operator@example.test"
    _assert_conflict(client.post("/api/tasks", json=SUBMISSION))
    assert _counts(stores) == (1, 1)


def test_replay_projects_the_original_bound_session_and_current_task_state(hub):
    from hub.bootstrap import create_app
    from tests.test_session_interactions import event

    stores, _, _ = hub
    app = create_app(stores.config(dev_operator=ACTOR, session_repositories_enabled=True,
                                  project_whitelist={"mac-local": ["agent-fleet"]}))
    client = app.test_client()
    services = app.extensions["fleet"]["services"]
    original = client.post("/api/tasks", json=SUBMISSION).json["task"]
    lease = services["runner"].poll("mac-local", "native-test-runner")["task"]
    start = event(1, "session_start", {"agent_family": "codex", "adapter": "hooks"})
    start.update(machine_id="mac-local", session_id="native_original", attempt_id=lease["attempt_id"])
    services["sessions"].ingest_events([start])
    services["runner"].result("mac-local", lease["attempt_id"], lease["nonce"], 0)
    stores.store.save_snapshot("mac-local", {"machine": "mac-local", "reachable": False})
    replay = client.post("/api/tasks", json=SUBMISSION)
    assert replay.status_code == 200 and replay.json["created"] is False
    assert replay.json["task"]["task_id"] == original["task_id"]
    assert replay.json["task"]["session_id"] == "native_original"
    assert replay.json["task"]["state"] == "succeeded"
    assert stores.tasks.get_task(original["task_id"])["attempt_id"] == lease["attempt_id"]
    assert "attempt_id" not in replay.json["task"] and "client_token" not in replay.json["task"]
    assert _counts(stores) == (1, 1)


@pytest.mark.parametrize("token", [False, 0, True, 1, 1.5, [], {}, "x" * 129])
def test_task_token_types_do_not_silently_disable_or_alias_idempotency(hub, token):
    stores, _, client = hub
    response = client.post("/api/tasks", json={**SUBMISSION, "client_token": token})
    assert response.status_code == 400, response.json
    assert response.json["error"] == "invalid_client_token"
    assert _counts(stores) == (0, 0)


@pytest.mark.parametrize("decision", ["pending", "confirmed", "rejected"])
def test_original_confirmation_requirement_survives_gate_decisions(hub, decision):
    stores, _, client = hub
    payload = {**SUBMISSION, "confirm": True}
    original = client.post("/api/tasks", json=payload).json["task"]
    if decision != "pending":
        action = "confirm" if decision == "confirmed" else "reject"
        assert client.post(f"/api/tasks/{original['task_id']}/{action}", json={}).status_code == 200
    _assert_conflict(client.post("/api/tasks", json=SUBMISSION))
    replay = client.post("/api/tasks", json=payload)
    assert replay.status_code == 200
    assert replay.json["task"]["task_id"] == original["task_id"]
    stored = stores.tasks.get_task(original["task_id"])
    assert stored["gate"]["state"] == decision
    assert stored["state"] == ("cancelled" if decision == "rejected" else "queued")
    assert _counts(stores) == (1, 1)


@pytest.mark.parametrize("conflicting", [False, True])
def test_racing_submissions_recheck_request_binding_inside_create_transaction(hub, monkeypatch, conflicting):
    stores, app, _ = hub
    service = app.extensions["fleet"]["services"]["tasks"]
    both_missed = Barrier(2)
    find = service.task_repo.find_task_submission

    def miss_together(**request):
        result = find(**request)
        assert result is None
        both_missed.wait(timeout=10)
        return result

    monkeypatch.setattr(service.task_repo, "find_task_submission", miss_together)

    def submit(index):
        payload = {**SUBMISSION, "instruction": f"request {index}" if conflicting else "same request"}
        with app.test_client() as client:
            response = client.post("/api/tasks", json=payload)
            return response.status_code, response.json

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(submit, [0, 1]))
    assert sorted(status for status, _ in results) == ([201, 409] if conflicting else [200, 201])
    assert _counts(stores) == (1, 1)
    if not conflicting:
        assert len({body["task"]["task_id"] for _, body in results}) == 1


def test_replay_retains_cancelled_task_without_requeue_or_new_attempt(hub):
    stores, app, client = hub
    original = client.post("/api/tasks", json=SUBMISSION).json["task"]
    before = stores.tasks.get_task(original["task_id"])
    stores.tasks.fence_submission(SUBMISSION["client_token"], ACTOR, now=1_795_000_000)
    stores.tasks.cancel_task(original["task_id"], ACTOR)
    app.config["DEV_OPERATOR"] = ACTOR
    replay = client.post("/api/tasks", json=SUBMISSION)
    assert replay.status_code == 200
    assert replay.json["task"]["state"] == "cancelled"
    assert stores.tasks.get_task(original["task_id"])["attempt_id"] == before["attempt_id"]
    assert _counts(stores) == (1, 1)


def test_cancelled_before_creation_stays_fenced(hub):
    stores, _, client = hub
    stores.tasks.fence_submission(SUBMISSION["client_token"], ACTOR, now=1_795_000_000)
    response = client.post("/api/tasks", json=SUBMISSION)
    assert response.status_code == 400 and response.json["error"] == "task_submission_cancelled"
    assert _counts(stores) == (0, 0)


@pytest.mark.parametrize("token", [None, ""])
def test_omitted_optional_tokens_continue_to_create_independent_tasks(hub, token):
    stores, _, client = hub
    request = {**SUBMISSION, "client_token": token}
    first, second = (client.post("/api/tasks", json=request) for _ in range(2))
    assert first.status_code == second.status_code == 201
    assert first.json["task"]["task_id"] != second.json["task"]["task_id"]
    assert _counts(stores) == (2, 2)


@pytest.mark.parametrize("origin", ["legacy", "handoff"])
def test_recovery_cannot_drop_an_internal_origin_binding(hub, origin):
    stores, _, client = hub
    request = {**SUBMISSION, "requested_by": ACTOR}
    internal = ({"legacy_origin": {"owner_id": ACTOR, "run_id": "run-one", "repository_id": None}}
        if origin == "legacy" else {"handoff": {"request_digest": "digest", "source_session_id": "session-one"}})
    original, _ = stores.tasks.create_task(**request, **internal)
    for recover in (stores.tasks.find_task_submission, stores.tasks.create_task):
        with pytest.raises(DomainError) as caught:
            recover(**request)
        assert caught.value.code == "idempotency_conflict"
    _assert_conflict(client.post("/api/tasks", json=SUBMISSION))
    recovered = stores.tasks.find_task_submission(**request, **internal)
    assert recovered["task_id"] == original["task_id"]
    assert _counts(stores) == (1, 1)


@pytest.mark.parametrize("field,value", [("requested_by", "another@example.test"),
    ("machine", "win-local"), ("agent_type", "pi"), ("project", "another-project"),
    ("instruction", "another request"), ("confirm", True)])
def test_repository_direct_create_cannot_bypass_immutable_request_binding(hub, field, value):
    stores, _, _ = hub
    request = {**SUBMISSION, "requested_by": ACTOR}
    original, _ = stores.tasks.create_task(**request)
    with pytest.raises(DomainError) as caught:
        stores.tasks.create_task(**{**request, field: value})
    assert caught.value.code == "idempotency_conflict"
    assert stores.tasks.get_task(original["task_id"])["instruction"] == SUBMISSION["instruction"]
    assert _counts(stores) == (1, 1)


def test_recovery_storage_errors_use_existing_bounded_503(hub, monkeypatch):
    _, app, client = hub
    service = app.extensions["fleet"]["services"]["tasks"]

    def broken(**request):
        raise sqlite3.OperationalError("private path /secret/task.db")

    monkeypatch.setattr(service.task_repo, "find_task_submission", broken)
    response = client.post("/api/tasks", json=SUBMISSION)
    assert response.status_code == 503
    assert response.json["error"] == "tasks_unavailable"
    assert "private" not in response.get_data(as_text=True)


def test_replayed_conflict_is_an_application_409_for_non_http_callers(hub):
    _, app, _ = hub
    service = app.extensions["fleet"]["services"]["tasks"]
    service.create(SUBMISSION, ACTOR)
    with pytest.raises(ApplicationError) as caught:
        service.create({**SUBMISSION, "instruction": "different"}, ACTOR)
    assert caught.value.code == "idempotency_conflict" and caught.value.status == 409
