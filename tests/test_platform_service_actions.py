from pathlib import Path

import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig
from tools.platform.services.actions import (
    ServiceActionError, action_argv_from_command, execute_service_action,
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
