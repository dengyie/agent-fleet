"""Normal service actions must traverse the same Node runtime as workspace work."""
import base64
import json
import os
import subprocess

import pytest

from fleet_core.platform_command import PlatformCommand
from hub.bootstrap import create_app
from hub.config import FleetConfig
from tests.test_platform_node_runtime import (
    OWNER, PRIVATE_KEY, PUBLIC_KEY, _Transport,
)
from tools.platform.node_executor import NodeToolExecutor
from tools.platform.node_runtime import NodeRuntime, NodeRuntimeConfig
from tools.plugins.node import load_runtime
from hub.platform.resource_lease import ResourceLeaseManager
from hub.platform.tool_broker import ToolBroker


@pytest.mark.skipif(os.name == 'nt', reason='POSIX executable adapter fixtures')
@pytest.mark.parametrize('adapter,alias,executable', [
    ('systemd', 'fleet-test.service', 'systemctl'),
    ('supervisor', 'fleet-test', 'supervisorctl'),
    ('docker', 'fleet-test', 'docker'),
])
@pytest.mark.parametrize('action', ['inspect', 'restart'])
def test_approved_service_action_crosses_hub_node_process_and_receipt(
        tmp_path, adapter, alias, executable, action):
    app = create_app(FleetConfig.from_root(
        tmp_path, dev_operator=OWNER, platform_enabled=True,
        service_monitoring_enabled=True, service_actions_enabled=True,
        platform_command_signing_raw=PRIVATE_KEY, platform_require_command_signature=True,
    ))
    repo = app.extensions['fleet']['platform_repository']
    commands = app.extensions['fleet']['platform_commands']
    capability = 'service.' + action
    repo.upsert_node(OWNER, {'node_id': 'node-a', 'capabilities': {capability: True}})
    repo.provision_node_credential(OWNER, 'node-a', secret='a' * 40)
    app.extensions['fleet']['services']['service_health'].register(OWNER, {
        'service_id': 'fixture-service', 'node_id': 'node-a', 'adapter': adapter,
        'target_alias': alias, 'allowed_actions': [action],
    })
    shim_dir = tmp_path / 'action-shims'
    shim_dir.mkdir()
    record = tmp_path / 'action-argv.txt'
    shim = shim_dir / executable
    shim.write_text(
        '#!/bin/sh\n'
        f'printf "%s\\n" "$0|$@" >> {str(record)!r}\n'
        + ('printf "running\\n"\n' if adapter == 'docker' else 'printf "active\\n"\n')
        + 'printf "runtime-secret /tmp/runtime-private\\n" >&2\n', encoding='utf-8',
    )
    shim.chmod(0o755)
    client = app.test_client()
    transport = _Transport(client)
    runtime = NodeRuntime(NodeRuntimeConfig(
        node_id='node-a', credential='node-a:' + 'a' * 40,
        workspace_root=tmp_path / 'workspace', journal_path=tmp_path / 'actions.db',
        hub_url='https://hub.invalid', public_key=PUBLIC_KEY,
        capabilities=frozenset({capability}), service_path_prefix=(shim_dir,),
    ), transport=transport)
    assert runtime.manifest()['capabilities'] == {capability: True}
    assert capability in runtime._capability_actions()

    response = client.post('/api/platform/v1/services/fixture-service/actions',
                           json={'action': action, 'idempotency_key': 'only-once'})
    assert response.status_code == 200
    payload = response.get_json()
    if action == 'restart':
        assert payload['approval_required'] is True
        assert payload['grant']['state'] == 'pending'
        assert runtime.poll_once() == {'ok': True, 'commands': 0, 'receipts': 0}
        assert not record.exists()
        grant_id = payload['grant']['grant_id']
        approved = client.post('/api/platform/v1/approvals/' + grant_id + '/decisions',
                               json={'decision': 'approve'})
        assert approved.status_code == 200
        payload = approved.get_json()
        duplicate = client.post('/api/platform/v1/approvals/' + grant_id + '/decisions',
                                json={'decision': 'approve'})
        assert duplicate.get_json()['command']['command_id'] == payload['command']['command_id']
    command_id = payload['command']['command_id']
    assert runtime.poll_once() == {'ok': True, 'commands': 1, 'receipts': 1}
    stored = commands.get(command_id)
    assert stored['status'] == 'succeeded'
    assert stored['result']['result']['state'] == ('healthy' if action == 'inspect' else 'succeeded')
    assert runtime.journal.get(command_id)['state'] == 'succeeded'
    assert 'runtime-secret' not in str(stored['result'])
    assert str(tmp_path) not in str(stored['result'])
    assert len(transport.poll_commands) == 1
    assert transport.poll_commands[0]['action'] == capability
    assert runtime.client.handle(transport.poll_commands[0])['status'] == 'succeeded'
    assert record.read_text(encoding='utf-8').count(executable) == 1
    assert runtime.poll_once() == {'ok': True, 'commands': 0, 'receipts': 0}
    leases = ResourceLeaseManager()
    lease = leases.acquire('read-action', OWNER)
    broker = ToolBroker(None, leases, resource_id='read-action',
                        service_actions=app.extensions['fleet']['services']['service_actions'])
    observed = broker.execute(command_id='read-original-action', tool='service.get_action',
        arguments={'service_id': 'fixture-service', 'command_id': command_id},
        owner_id=OWNER, epoch=lease['epoch'])
    assert observed.state == 'succeeded'
    assert observed.result['command']['status'] == 'succeeded'
    assert observed.result['result']['state'] == ('healthy' if action == 'inspect' else 'succeeded')
    assert 'runtime-secret' not in str(observed.result)


@pytest.mark.parametrize('action', ['inspect', 'restart'])
def test_service_action_requires_explicit_node_capability(action):
    calls = []
    executor = NodeToolExecutor(None, service_executor=lambda command: calls.append(command))
    command = PlatformCommand.create(
        command_id='not-enabled', target_node='node-a', owner_id=OWNER,
        action='service.' + action, resource_id='fixture-service',
        arguments={'adapter': 'systemd', 'target_alias': 'fleet-test.service',
                   'service_version': 1, 'action': action},
        retry_class='read_only' if action == 'inspect' else 'manual_only',
        expires_at=9_999_999_999,
    ).as_dict()
    result = executor(command)
    assert result['state'] == 'failed'
    assert result['error_code'] == 'capability_unavailable'
    assert not calls


def test_action_status_tools_are_gated_and_read_only():
    expected = {'service.get_action', 'service.get_approval'}
    disabled = {tool['name'] for tool in ToolBroker.tool_definitions()}
    enabled = {tool['name']: tool for tool in ToolBroker.tool_definitions(service_actions_enabled=True)}
    assert not expected & disabled
    assert expected <= enabled.keys()
    assert all(enabled[name]['risk'] == 'read_only' for name in expected)


@pytest.mark.parametrize('service_actions,accepted', [
    (['inspect'], True), (['restart'], True), (['inspect', 'restart'], True),
    (['logs'], True), (['inspect', 'logs'], True), (['logs', 'logs'], False),
    (['inspect', 'inspect'], False), (['destroy'], False), ('inspect', False),
    ([None], False), ([{}], False), ([], False),
])
def test_normal_node_cli_loads_explicit_service_actions(tmp_path, service_actions, accepted):
    credential = tmp_path / 'credential'
    credential.write_text('node-a:' + 'a' * 40, encoding='utf-8')
    public = tmp_path / 'public-key'
    public.write_text(base64.b64encode(PUBLIC_KEY).decode('ascii'), encoding='utf-8')
    state = tmp_path / 'state'
    state.mkdir()
    config = tmp_path / 'node.json'
    config.write_text(json.dumps({
        'node_id': 'node-a', 'hub_url': 'https://hub.invalid',
        'credential_file': str(credential), 'public_key_file': str(public),
        'state_directory': str(state), 'plugins': {}, 'service_actions': service_actions,
    }), encoding='utf-8')
    if not accepted:
        with pytest.raises(ValueError):
            load_runtime(config, transport=object())
        return
    runtime, _ = load_runtime(config, transport=object())
    try:
        expected = {'service.' + action for action in service_actions}
        assert set(runtime.manifest()['capabilities']) == expected
        assert runtime.executor.allowed_service_actions == expected
    finally:
        runtime.close_all()


@pytest.mark.parametrize('failure', ['timeout', 'nonzero', 'interrupted'])
def test_uncertain_restart_is_journaled_once_without_replay(tmp_path, failure):
    runtime = NodeRuntime(NodeRuntimeConfig(
        node_id='node-a', credential='node-a:' + 'a' * 40,
        workspace_root=tmp_path / 'workspace', journal_path=tmp_path / 'uncertain.db',
        hub_url='https://hub.invalid', public_key=PUBLIC_KEY,
        capabilities=frozenset({'service.restart'}),
    ), transport=object())
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(argv, 1)
        if failure == 'interrupted':
            raise RuntimeError('interrupted')
        return type('Result', (), {'stdout': '', 'stderr': 'failed', 'returncode': 1})()

    runtime.executor.service_executor.runner = runner
    command = PlatformCommand.create(
        command_id='restart-once', target_node='node-a', owner_id=OWNER,
        action='service.restart', resource_id='fixture-service', grant_id='grant-fixture',
        arguments={'adapter': 'systemd', 'target_alias': 'fleet-test.service',
                   'service_version': 1, 'action': 'restart'},
        retry_class='manual_only', expires_at=9_999_999_999,
    ).signed(PRIVATE_KEY).as_dict()
    assert runtime.client.handle(command)['status'] == 'unknown'
    assert runtime.client.handle(command)['status'] == 'unknown'
    assert len(calls) == 1


@pytest.mark.parametrize('arguments,grant_id', [
    ({'adapter': 'systemd', 'target_alias': 'fixture.service', 'service_version': 1, 'action': 'restart'}, None),
    ({'adapter': 'systemd', 'target_alias': 'fixture.service', 'service_version': 1, 'action': 'inspect'}, 'grant-fixture'),
    ({'adapter': 'systemd', 'target_alias': 'fixture.service', 'service_version': 0, 'action': 'restart'}, 'grant-fixture'),
])
def test_restart_rejects_missing_grant_or_malformed_envelope(arguments, grant_id):
    calls = []
    executor = NodeToolExecutor(None, allowed_service_actions={'service.restart'},
                               service_executor=lambda command: calls.append(command))
    command = {'command_id': 'invalid-restart', 'action': 'service.restart',
               'resource_id': 'fixture-service', 'grant_id': grant_id, 'arguments': arguments}
    result = executor(command)
    assert result['state'] == 'failed' and result['error_code'] == 'invalid_arguments'
    assert not calls
