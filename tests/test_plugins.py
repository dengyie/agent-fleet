import time

import pytest

from fleet_core.plugins import OFFICIAL_PLUGINS, PluginError
from hub.bootstrap import create_app
from hub.config import FleetConfig
from fleet_core.control import generate_ed25519_keypair

OWNER = 'plugins@example.test'
BASE = '/api/platform/v1/plugins'


@pytest.fixture
def env(tmp_path):
    private, public = generate_ed25519_keypair()
    app = create_app(FleetConfig.from_root(
        tmp_path, dev_operator=OWNER, platform_enabled=True,
        platform_command_signing_raw=private))
    repo = app.extensions['fleet']['platform_repository']
    repo.upsert_node(OWNER, {'node_id': 'vm', 'label': 'Development VM'})
    repo.provision_node_credential(OWNER, 'vm', secret='n' * 40)
    return app, app.test_client(), public


def announce(client, **changes):
    offer = dict(plugin_id='github-cli', version='1.0.0', api_version=1,
                 available=True, dependency_version='gh version 2.88.0',
                 contexts=[dict(context_id='default', label='Developer',
                                os_user='developer', config_dir='/home/developer/.config/gh',
                                env_override=False)])
    offer.update(changes)
    return client.post('/api/platform/v1/nodes/plugins', json={'plugins': [offer]},
                       headers={'X-Platform-Node-Credential': 'vm:' + 'n' * 40})


def enable(client):
    assert client.put(BASE + '/github-cli', json={'enabled': True, 'revision': 0}).status_code == 200
    assert client.put(BASE + '/github-cli/nodes/vm', json={'enabled': True}).status_code == 200
    assert announce(client).status_code == 200


def submit(client, action='status', arguments=None, key='operation-1'):
    return client.post(BASE + '/github-cli/operations', json={
        'node_id': 'vm', 'action': action,
        'arguments': arguments or {'context_id': 'default', 'hostname': 'github.com'},
        'idempotency_key': key})


def test_registry_has_validated_namespaced_actions():
    plugin = OFFICIAL_PLUGINS.get('github-cli')
    assert plugin.version == '1.0.0'
    assert plugin.validate('switch', {'username': 'mango'}) == {
        'context_id': 'default', 'hostname': 'github.com', 'username': 'mango'}
    for arguments in ({'argv': ['sh']}, {'hostname': 'https://github.com'}, {'context_id': '../root'}):
        with pytest.raises(PluginError):
            plugin.validate('status', arguments)
    with pytest.raises(PluginError):
        plugin.validate('repo_check', {'repository': 'owner/repo/extra'})


def test_settings_persist_and_revision_conflict(env):
    app, client, _ = env
    item = client.get(BASE).get_json()['plugins'][0]
    assert item['plugin_id'] == 'github-cli' and item['enabled'] is False
    assert client.put(BASE + '/github-cli', json={'enabled': True, 'revision': 0}).status_code == 200
    assert client.put(BASE + '/github-cli', json={'enabled': False, 'revision': 0}).status_code == 409
    assert app.extensions['fleet']['services']['plugins'].catalog(OWNER)['plugins'][0]['enabled']
    assert client.get(BASE + '/unknown/nodes').status_code == 404


def test_node_enablement_offer_and_dependency_are_distinct(env):
    _, client, _ = env
    assert submit(client).status_code == 409
    enable(client)
    nodes = client.get(BASE + '/github-cli/nodes').get_json()['nodes']
    assert nodes[0]['readiness'] == 'ready'
    assert announce(client, available=False).status_code == 200
    assert submit(client).get_json()['error'] == 'dependency_missing'
    assert announce(client, api_version=2).status_code == 200
    assert submit(client).get_json()['error'] == 'plugin_incompatible'


def test_commands_are_signed_idempotent_and_owner_scoped(env):
    app, client, public = env
    enable(client)
    created = submit(client)
    assert created.status_code == 202
    operation = created.get_json()['operation']
    assert operation['status'] == 'queued'
    assert submit(client).get_json()['operation']['operation_id'] == operation['operation_id']
    assert submit(client, arguments={'hostname': 'enterprise.example'}).status_code == 409
    commands = client.post('/api/platform/v1/nodes/poll', json={'worker_id': 'vm'},
                           headers={'X-Platform-Node-Credential': 'vm:' + 'n' * 40}).get_json()['commands']
    from fleet_core.platform_command import verify_command
    assert len(commands) == 1 and verify_command(commands[0], public)
    assert commands[0]['action'] == 'plugin.github-cli.status'
    other = app.extensions['fleet']['services']['plugins']
    with pytest.raises(Exception) as exc:
        other.operation('other@example.test', 'github-cli', operation['operation_id'])
    assert exc.value.code == 'operation_not_found'
    assert other.operations('other@example.test', 'github-cli')['operations'] == []


def test_disabling_preserves_inspection_and_login_cancel(env):
    _, client, _ = env
    enable(client)
    operation = submit(client, 'login_start').get_json()['operation']
    assert client.put(BASE + '/github-cli', json={'enabled': False, 'revision': 1}).status_code == 200
    assert submit(client, key='disabled').status_code == 409
    assert client.get(BASE + '/github-cli/operations/' + operation['operation_id']).status_code == 200
    assert submit(client, 'login_cancel', {'login_id': operation['operation_id']}, key='cancel').status_code == 202


def test_offer_validation_and_context_selection(env):
    _, client, _ = env
    enable(client)
    assert submit(client, arguments={'context_id': 'root'}).get_json()['error'] == 'context_unavailable'
    assert announce(client, contexts=[]).status_code == 400
    assert client.post('/api/platform/v1/nodes/plugins', json={'plugins': []}).status_code == 403
    nodes = client.get(BASE + '/github-cli/nodes').get_json()['nodes']
    assert nodes[0]['offer']['contexts'][0]['os_user'] == 'developer'
    assert nodes[0]['offer']['reported_at'] <= time.time()


@pytest.mark.parametrize('source', [{}, [], 42, None])
def test_offer_rejects_non_string_environment_sources(env, source):
    _, client, _ = env
    response = announce(client, contexts=[dict(context_id='default', env_override=False,
                                              environment_sources=[source])])
    assert response.status_code == 400


def test_enrollment_is_one_time_atomic_and_does_not_rotate_existing_node(env):
    app, client, _ = env
    response = client.post(BASE + '/nodes', json={'node_id': 'new-vm', 'label': 'New VM'})
    assert response.status_code == 201
    enrollment = response.get_json()['enrollment']
    assert enrollment['credential'].startswith('new-vm:')
    assert response.headers['Cache-Control'] == 'no-store'
    assert client.post(BASE + '/nodes', json={'node_id': 'new-vm', 'label': 'Again'}).status_code == 409
    repo = app.extensions['fleet']['platform_repository']
    secret = enrollment['credential'].split(':', 1)[1]
    assert repo.authenticate_node_credential('new-vm', secret)['owner_id'] == OWNER
    assert client.post(BASE + '/nodes', json={'node_id': '../bad'}).status_code == 400


def test_login_recovery_requires_matching_node_context_and_host(env):
    _, client, _ = env
    enable(client)
    start = submit(client, 'login_start').get_json()['operation']['operation_id']
    response = submit(client, 'login_status', {'login_id': start, 'hostname': 'other.example'}, key='wrong-host')
    assert response.status_code == 404
    assert submit(client, 'login_status', {'login_id': 'missing'}, key='missing').status_code == 404
