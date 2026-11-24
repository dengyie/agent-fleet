import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig
from tools.platform.services.actions import (
    ServiceActionError,
    action_argv_from_command,
    execute_service_action,
)


def _app(tmp_path: Path, *, actions=False, owner="owner-a@example.test"):
    return create_app(FleetConfig.from_root(
        tmp_path, dev_operator=owner, platform_enabled=True,
        service_monitoring_enabled=True, service_actions_enabled=actions,
    ))


def _service(app, *, allowed=("inspect", "restart"), adapter="systemd"):
    repo = app.extensions["fleet"]["platform_repository"]
    repo.upsert_node("owner-a@example.test", {"node_id": "node-a"})
    return app.extensions["fleet"]["services"]["service_health"].register(
        "owner-a@example.test", {
            "service_id": "api", "node_id": "node-a",
            "adapter": adapter, "target_alias": "api.service",
            "allowed_actions": list(allowed),
        })["service"]


def test_actions_gate_is_closed_by_default(tmp_path):
    app = _app(tmp_path)
    assert "service_actions" not in app.extensions["fleet"]["services"]
    assert app.test_client().post("/api/platform/v1/services/api/actions", json={"action": "inspect"}, headers={"X-Operator-Identity": "owner-a@example.test"}).status_code == 404


@pytest.mark.parametrize('action', ['inspect', 'restart'])
def test_generated_service_action_identifiers_always_pass_their_own_validation(tmp_path, monkeypatch, action):
    import secrets

    from platform_schema import validate_id

    app = _app(tmp_path, actions=True)
    _service(app)
    # A valid URL-safe random token can contain a reserved ID substring.
    monkeypatch.setattr(secrets, 'token_urlsafe', lambda _: 'abcKEYdef0123456789012345')
    response = app.test_client().post('/api/platform/v1/services/api/actions', json={'action': action})
    assert response.status_code == 200
    payload = response.get_json()
    if action == 'inspect':
        validate_id(payload['command']['command_id'])
    else:
        grant_id = payload['grant']['grant_id']
        validate_id(grant_id)
        assert app.test_client().get('/api/platform/v1/approvals/' + grant_id).status_code == 200


def test_restart_requires_approval_and_is_one_time(tmp_path):
    app = _app(tmp_path, actions=True)
    _service(app)
    client = app.test_client(); headers = {"X-Operator-Identity": "owner-a@example.test"}
    grant = client.post("/api/platform/v1/services/api/actions", json={"action": "restart"}, headers=headers).get_json()["grant"]
    assert grant["state"] == "pending"
    approved = client.post(f"/api/platform/v1/approvals/{grant['grant_id']}/decisions", json={"decision": "approve"}, headers=headers)
    assert approved.status_code == 200
    payload = approved.get_json()
    assert payload["grant"]["state"] == "consumed"
    replay = client.post(f"/api/platform/v1/approvals/{grant['grant_id']}/decisions", json={"decision": "approve"}, headers=headers)
    assert replay.status_code == 200
    assert replay.get_json()["command"]["command_id"] == payload["command"]["command_id"]
    again = client.post("/api/platform/v1/services/api/actions", json={"action": "restart", "idempotency_key": "restart-1"}, headers=headers)
    same = client.post("/api/platform/v1/services/api/actions", json={"action": "restart", "idempotency_key": "restart-1"}, headers=headers)
    assert again.status_code == same.status_code == 200
    assert again.get_json()["grant"]["grant_id"] == same.get_json()["grant"]["grant_id"]


def test_concurrent_http_approval_requests_replay_one_grant(tmp_path, monkeypatch):
    app = _app(tmp_path, actions=True)
    _service(app, allowed=("restart",))
    repository = app.extensions["fleet"]["services"]["service_actions"].approval_repository
    both_looked_up = threading.Barrier(2)
    connect = repository._connect

    class RacingConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, *args):
            cursor = self.connection.execute(sql, *args)
            if sql.startswith("SELECT * FROM platform_approval_grants WHERE owner_id=? AND idempotency_key=?"):
                try:
                    both_looked_up.wait(timeout=0.5)
                except threading.BrokenBarrierError:
                    pass
            return cursor

        def __getattr__(self, name):
            return getattr(self.connection, name)

    monkeypatch.setattr(repository, "_connect", lambda: RacingConnection(connect()))

    def submit(_):
        response = app.test_client().post(
            "/api/platform/v1/services/api/actions",
            json={"action": "restart", "idempotency_key": "parallel-approval"})
        return response.status_code, response.get_json()

    with ThreadPoolExecutor(max_workers=2) as workers:
        responses = list(workers.map(submit, range(2)))

    assert [status for status, _ in responses] == [200, 200]
    grants = [body["grant"] for _, body in responses]
    assert grants[0]["grant_id"] == grants[1]["grant_id"]
    assert grants[0]["state"] == grants[1]["state"] == "pending"
    connection = connect()
    try:
        count = connection.execute("SELECT COUNT(*) FROM platform_approval_grants").fetchone()[0]
    finally:
        connection.close()
    assert count == 1


def test_concurrent_approval_decisions_recover_the_same_command(tmp_path, monkeypatch):
    app = _app(tmp_path, actions=True)
    _service(app, allowed=("restart",))
    client = app.test_client()
    requested = client.post("/api/platform/v1/services/api/actions", json={"action": "restart"})
    assert requested.status_code == 200
    grant_id = requested.get_json()["grant"]["grant_id"]
    service = app.extensions["fleet"]["services"]["service_actions"]
    repository = service.approval_repository
    both_read_pending = threading.Barrier(2)
    get_grant = repository.get

    def synchronized_get(owner_id, selected_grant_id):
        row = get_grant(owner_id, selected_grant_id)
        if row is not None and row["state"] == "pending":
            both_read_pending.wait(timeout=5)
        return row

    monkeypatch.setattr(repository, "get", synchronized_get)

    def approve(_):
        response = app.test_client().post(
            f"/api/platform/v1/approvals/{grant_id}/decisions",
            json={"decision": "approve"})
        return response.status_code, response.get_json()

    with ThreadPoolExecutor(max_workers=2) as workers:
        responses = list(workers.map(approve, range(2)))

    assert [status for status, _ in responses] == [200, 200], responses
    commands = [body["command"]["command_id"] for _, body in responses]
    assert commands[0] == commands[1]
    assert commands[0] == "cmd_" + hashlib.sha256(grant_id.encode()).hexdigest()[:48]
    assert repository.get("owner-a@example.test", grant_id)["state"] == "consumed"
    connection = service.command_repository._connect()
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM platform_commands WHERE grant_id=?", (grant_id,)).fetchone()[0]
        outbox_count = connection.execute(
            "SELECT COUNT(*) FROM platform_command_outbox WHERE command_id=?",
            (commands[0],)).fetchone()[0]
    finally:
        connection.close()
    assert count == outbox_count == 1


def test_consuming_approval_without_command_recovers_after_app_restart(tmp_path):
    app = _app(tmp_path, actions=True)
    _service(app, allowed=("restart",))
    client = app.test_client()
    requested = client.post("/api/platform/v1/services/api/actions", json={"action": "restart"})
    grant_id = requested.get_json()["grant"]["grant_id"]
    service = app.extensions["fleet"]["services"]["service_actions"]
    command_id = "cmd_" + hashlib.sha256(grant_id.encode()).hexdigest()[:48]

    service.approval_repository.decide("owner-a@example.test", grant_id, approve=True)
    service.approval_repository.begin_consume(
        "owner-a@example.test", grant_id, command_id=command_id)
    assert service.command_repository.get(command_id) is None

    restarted = _app(tmp_path, actions=True)
    response = restarted.test_client().post(
        f"/api/platform/v1/approvals/{grant_id}/decisions",
        json={"decision": "approve"})

    assert response.status_code == 200, response.get_json()
    assert response.get_json()["command"]["command_id"] == command_id
    assert response.get_json()["grant"]["state"] == "consumed"
    assert restarted.extensions["fleet"]["platform_commands"].get(command_id)["grant_id"] == grant_id


def test_persisted_approval_command_finishes_consumption_after_restart(tmp_path, monkeypatch):
    app = _app(tmp_path, actions=True)
    _service(app, allowed=("restart",))
    client = app.test_client()
    requested = client.post("/api/platform/v1/services/api/actions", json={"action": "restart"})
    grant_id = requested.get_json()["grant"]["grant_id"]
    service = app.extensions["fleet"]["services"]["service_actions"]
    original_finish = service.approval_repository.finish_consume

    def fail_after_command_persist(owner_id, selected_grant_id, *, command_id, success):
        if success:
            raise RuntimeError("simulated Hub interruption after command commit")
        return original_finish(owner_id, selected_grant_id,
                               command_id=command_id, success=success)

    monkeypatch.setattr(service.approval_repository, "finish_consume",
                        fail_after_command_persist)
    failed = client.post(
        f"/api/platform/v1/approvals/{grant_id}/decisions",
        json={"decision": "approve"})
    assert failed.status_code == 500
    command_id = "cmd_" + hashlib.sha256(grant_id.encode()).hexdigest()[:48]
    assert service.approval_repository.get("owner-a@example.test", grant_id)["state"] == "consuming"
    assert service.command_repository.get(command_id)["grant_id"] == grant_id

    restarted = _app(tmp_path, actions=True)
    recovered = restarted.test_client().post(
        f"/api/platform/v1/approvals/{grant_id}/decisions",
        json={"decision": "approve"})

    assert recovered.status_code == 200
    assert recovered.get_json()["grant"]["state"] == "consumed"
    assert recovered.get_json()["command"]["command_id"] == command_id
    connection = restarted.extensions["fleet"]["platform_commands"]._connect()
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM platform_command_outbox WHERE command_id=?",
            (command_id,)).fetchone()[0] == 1
    finally:
        connection.close()


def test_failed_concurrent_enqueue_does_not_reset_another_approval_consumer(tmp_path, monkeypatch):
    from hub.application.errors import ApplicationError

    app = _app(tmp_path, actions=True)
    _service(app, allowed=("restart",))
    service = app.extensions["fleet"]["services"]["service_actions"]
    grant_id = service.request("owner-a@example.test", "api", "restart")["grant"]["grant_id"]
    service.approval_repository.decide("owner-a@example.test", grant_id, approve=True)
    both_consuming = threading.Barrier(2)
    failure_returned = threading.Event()
    enqueue = service.delivery.enqueue

    def racing_enqueue(command, **kwargs):
        both_consuming.wait(timeout=5)
        if threading.current_thread().name.endswith("_0"):
            raise ApplicationError("command_store", "command store unavailable", 503)
        assert failure_returned.wait(timeout=5)
        return enqueue(command, **kwargs)

    monkeypatch.setattr(service.delivery, "enqueue", racing_enqueue)

    def approve(_):
        response = app.test_client().post(
            f"/api/platform/v1/approvals/{grant_id}/decisions",
            json={"decision": "approve"})
        if threading.current_thread().name.endswith("_0"):
            failure_returned.set()
        return response.status_code, response.get_json()

    with ThreadPoolExecutor(max_workers=2) as workers:
        responses = list(workers.map(approve, range(2)))

    assert sorted(status for status, _ in responses) == [200, 503], responses
    assert service.approval_repository.get("owner-a@example.test", grant_id)["state"] == "consumed"
    monkeypatch.setattr(service.delivery, "enqueue", enqueue)
    replay = app.test_client().post(
        f"/api/platform/v1/approvals/{grant_id}/decisions", json={"decision": "approve"})
    assert replay.status_code == 200
    connection = service.command_repository._connect()
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM platform_commands WHERE grant_id=?", (grant_id,)).fetchone()[0] == 1
    finally:
        connection.close()


def test_version_fence_and_owner_scope(tmp_path):
    app = _app(tmp_path, actions=True)
    _service(app, allowed=("restart",))
    client = app.test_client(); headers = {"X-Operator-Identity": "owner-a@example.test"}
    grant = client.post("/api/platform/v1/services/api/actions", json={"action": "restart"}, headers=headers).get_json()["grant"]
    health = app.extensions["fleet"]["services"]["service_health"]
    health.register("owner-a@example.test", {"service_id": "api", "node_id": "node-a", "adapter": "systemd", "target_alias": "api.service", "allowed_actions": ["restart"]})
    approved = client.post(f"/api/platform/v1/approvals/{grant['grant_id']}/decisions", json={"decision": "approve"}, headers=headers)
    assert approved.status_code == 409 and approved.get_json()["error"] == "service_version_conflict"
    other = create_app(FleetConfig.from_root(tmp_path, dev_operator="owner-b@example.test", platform_enabled=True, service_monitoring_enabled=True, service_actions_enabled=True))
    assert other.test_client().get(f"/api/platform/v1/approvals/{grant['grant_id']}").status_code == 404


def test_inspect_command_is_read_only_and_rejects_user_arguments(tmp_path):
    app = _app(tmp_path, actions=True); _service(app, allowed=("inspect",))
    client = app.test_client(); headers = {"X-Operator-Identity": "owner-a@example.test"}
    response = client.post("/api/platform/v1/services/api/actions", json={"action": "inspect", "arguments": {"command": "rm -rf /"}}, headers=headers)
    assert response.status_code == 400
    response = client.post("/api/platform/v1/services/api/actions", json={"action": "inspect"}, headers=headers)
    assert response.status_code == 200 and response.get_json()["command"]["retry_class"] == "read_only"


def test_fixed_argv_and_shell_false(tmp_path):
    command = {"action": "service.restart", "resource_id": "api", "arguments": {"adapter": "docker", "target_alias": "api", "service_version": 1, "action": "restart"}}
    assert action_argv_from_command(command) == ("docker", "restart", "--", "api")
    try:
        action_argv_from_command({**command, "arguments": {**command["arguments"], "argv": ["rm"]}})
    except ServiceActionError as exc:
        assert exc.code == "invalid_arguments"
    else:
        raise AssertionError("arbitrary argv accepted")
    calls = []
    def runner(argv, **kwargs):
        calls.append((argv, kwargs)); return type("R", (), {"stdout": "active", "stderr": "", "returncode": 0})()
    execute_service_action(command, runner=runner)
    assert calls[0][1]["shell"] is False


def test_completed_inspect_nonzero_status_is_unhealthy_evidence():
    command = {"action": "service.inspect", "resource_id": "api", "arguments": {
        "adapter": "systemd", "target_alias": "api.service", "service_version": 1,
        "action": "inspect",
    }}

    def runner(argv, **kwargs):
        return type("Result", (), {"stdout": "inactive", "stderr": "", "returncode": 3})()

    result = execute_service_action(command, runner=runner)
    assert result["state"] == "unhealthy"
    assert result["returncode"] == 3


@pytest.mark.parametrize('output,expected', [
    ('running', 'healthy'), ('exited', 'unhealthy'), ('paused', 'unhealthy'),
    ('restarting', 'unhealthy'), ('dead', 'unhealthy'), ('created', 'unhealthy'),
    ('removing', 'unhealthy'), ('unexpected-output', 'unknown'), ('', 'unknown'),
])
@pytest.mark.parametrize('action', ['service.inspect', 'reconcile.service.inspect'])
def test_docker_inspect_health_uses_container_state_not_cli_success(output, expected, action):
    command = {'action': action, 'resource_id': 'api', 'arguments': {
        'adapter': 'docker', 'target_alias': 'api', 'service_version': 1, 'action': 'inspect',
    }}
    result = execute_service_action(command, runner=lambda *args, **kwargs: type('Result', (), {
        'stdout': output, 'stderr': '', 'returncode': 0,
    })())
    assert result['state'] == expected


def test_action_observation_is_owner_target_scoped_and_does_not_replay(tmp_path):
    from hub.application.task_service import ApplicationError

    owner = 'owner-a@example.test'
    app = _app(tmp_path, actions=True)
    _service(app)
    service = app.extensions['fleet']['services']['service_actions']
    command = service.request(owner, 'api', 'inspect')['command']
    observed = service.get_action(owner, 'api', command['command_id'])
    assert observed['command']['status'] == 'queued'
    commands = app.extensions['fleet']['platform_commands']
    commands.mark_unknown(command['command_id'], reason='receipt_timeout')
    assert service.get_action(owner, 'api', command['command_id'])['command']['status'] == 'unknown'
    assert service.get_action(owner, 'api', command['command_id'])['command']['status'] == 'unknown'
    assert commands.get(command['command_id'])['status'] == 'unknown'
    for actor, target in [('owner-b@example.test', 'api'), (owner, 'another-service')]:
        with pytest.raises(ApplicationError) as error:
            service.get_action(actor, target, command['command_id'])
        assert error.value.code == 'command_not_found'


def test_action_observation_keeps_only_bounded_execution_fields(tmp_path, monkeypatch):
    app = _app(tmp_path, actions=True)
    _service(app)
    service = app.extensions['fleet']['services']['service_actions']
    row = {'resource_id': 'api', 'action': 'service.inspect', 'status': 'succeeded',
           'result': {'state': 'succeeded', 'result': {
               'state': 'healthy', 'returncode': 0, 'output': 'private output', 'token': 'private-key',
           }}}
    monkeypatch.setattr(service.command_repository, 'get_for_owner', lambda *args: row)
    result = service.get_action('owner-a@example.test', 'api', 'cmd-fixture')
    assert result['result'] == {'state': 'healthy', 'returncode': 0}
    assert 'private' not in str(result)
    row['result']['result']['state'] = {'invalid': 'value'}
    assert service.get_action('owner-a@example.test', 'api', 'cmd-fixture')['result'] == {'returncode': 0}


def test_action_observation_rejects_a_non_service_command(tmp_path, monkeypatch):
    from hub.application.task_service import ApplicationError

    app = _app(tmp_path, actions=True)
    _service(app)
    service = app.extensions['fleet']['services']['service_actions']
    monkeypatch.setattr(service.command_repository, 'get_for_owner', lambda *args: {
        'resource_id': 'api', 'action': 'tool.workspace.exec', 'status': 'succeeded'})
    with pytest.raises(ApplicationError) as error:
        service.get_action('owner-a@example.test', 'api', 'cmd-fixture')
    assert error.value.code == 'command_not_found'
