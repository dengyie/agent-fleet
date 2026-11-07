import base64
import json
from pathlib import Path
import time

import pytest

from fleet_core.control import generate_ed25519_keypair
from fleet_core.locking import LockBusy
from hub.bootstrap import create_app
from hub.config import FleetConfig
from support.fake_gh import fake_gh  # noqa: F401
from tools.plugins.node import announce, install_enrollment, load_runtime

OWNER = 'plugin-node@example.test'
BASE = '/api/platform/v1/plugins/github-cli'


class Transport:
    def __init__(self, app):
        self.app = app
        self.commands = []

    def post_json(self, url, body, headers):
        path = '/' + url.split('/', 3)[-1]
        response = self.app.test_client().post(path, json=body, headers=headers)
        result = response.get_json()
        if path.endswith('/poll') and result.get('commands'):
            self.commands.extend(result['commands'])
        return response.status_code, result


@pytest.fixture
def node_env(tmp_path, fake_gh):
    private, public = generate_ed25519_keypair()
    app = create_app(FleetConfig.from_root(tmp_path / 'hub', dev_operator=OWNER,
                                         frontend_dir=Path(__file__).resolve().parents[1] / 'frontend',
                                         platform_enabled=True, platform_command_signing_raw=private))
    @app.post('/_fixture/authorize')
    def authorize():
        (tmp_path / 'cli-state' / 'authorize').write_text('fixture-authorized')
        return {'ok': True}
    repo = app.extensions['fleet']['platform_repository']
    repo.upsert_node(OWNER, {'node_id': 'vm', 'label': 'Plugin VM'})
    credential = repo.provision_node_credential(OWNER, 'vm')['credential']
    secret_file, public_file = tmp_path / 'node-credential', tmp_path / 'public-key'
    secret_file.write_text(credential)
    public_file.write_text(base64.b64encode(public).decode())
    config = tmp_path / 'node.json'
    config.write_text(json.dumps({
        'node_id': 'vm', 'hub_url': 'https://fleet.example', 'credential_file': str(secret_file),
        'public_key_file': str(public_file), 'state_directory': str(tmp_path / 'node-state'),
        'plugins': {'github-cli': {'executable': str(fake_gh), 'contexts': [
            {'context_id': 'default', 'config_dir': str(tmp_path / 'cli-state')}]}}}))
    transport = Transport(app)
    runtime, _ = load_runtime(config, transport=transport)
    client = app.test_client()
    assert announce(runtime)
    client.put('/api/platform/v1/plugins/github-cli', json={'enabled': True, 'revision': 0})
    client.put(BASE + '/nodes/vm', json={'enabled': True})
    yield app, client, runtime, config, transport
    runtime.close_all()


def submit(client, action, args=None, key=None):
    response = client.post(BASE + '/operations', json={
        'node_id': 'vm', 'action': action, 'arguments': args or {},
        'idempotency_key': key or str(time.time_ns())})
    assert response.status_code == 202, response.get_json()
    return response.get_json()['operation']['operation_id']


def test_signed_hub_node_roundtrip_observation_and_duplicate(node_env):
    app, client, runtime, _, transport = node_env
    operation_id = submit(client, 'status')
    assert runtime.poll_once()['receipts'] == 1
    result = client.get(BASE + '/operations/' + operation_id).get_json()['operation']
    assert result['status'] == 'succeeded'
    assert result['result']['auth']['active_account'] == 'alice'
    targets = client.get(BASE + '/nodes').get_json()['nodes']
    assert targets[0]['observations'][0]['active_account'] == 'alice'
    assert runtime.client.handle(transport.commands[0])['status'] == 'succeeded'
    assert runtime.journal.get(operation_id)['state'] == 'succeeded'
    assert len(app.extensions['fleet']['services']['plugins'].operations(OWNER, 'github-cli')['operations']) == 1


def test_login_recovery_survives_history_limit_and_other_scopes(node_env):
    _, client, runtime, _, _ = node_env
    start = submit(client, 'login_start')
    runtime.poll_once()
    handler = runtime.plugin_executor.handlers['github-cli']
    deadline = time.monotonic() + 8
    while handler.store.get(start)['state'] != 'waiting_authorization':
        assert time.monotonic() < deadline
        time.sleep(0.05)
    submit(client, 'login_status', {'login_id': start})
    runtime.poll_once()
    for _ in range(55):
        submit(client, 'status')
    response = client.get(BASE + '/operations?node_id=vm&context_id=default&hostname=github.com').get_json()
    assert len(response['operations']) == 50
    assert all(op['operation_id'] != start for op in response['operations'])
    assert response['login']['login_id'] == start
    assert response['login']['state'] == 'waiting_authorization'
    assert response['login']['user_code'] == 'ABCD-1234'
    other = client.get(BASE + '/operations?node_id=vm&context_id=default&hostname=other.example').get_json()
    assert other['operations'] == [] and other['login'] is None
    assert client.get(BASE + '/operations?context_id=default').status_code == 400


def test_runtime_requires_single_owner_and_recovers_command_receipts(node_env):
    _, client, runtime, config, transport = node_env
    operation_id = submit(client, 'status')
    runtime.poll_once()
    with pytest.raises(LockBusy):
        load_runtime(config, transport=transport)
    runtime.close_all()
    restarted, _ = load_runtime(config, transport=transport)
    try:
        assert restarted.client.handle(transport.commands[0])['status'] == 'succeeded'
        assert restarted.journal.get(operation_id)['state'] == 'succeeded'
    finally:
        restarted.close_all()


def test_node_reports_missing_plugin_and_stale_offers(node_env):
    app, client, runtime, _, _ = node_env
    service = app.extensions['fleet']['services']['plugins']
    service.clock = lambda: time.time() + 121
    assert client.get(BASE + '/nodes').get_json()['nodes'][0]['readiness'] == 'offline'
    runtime.plugin_executor.handlers.clear()
    service.clock = time.time
    assert announce(runtime)
    assert client.get(BASE + '/nodes').get_json()['nodes'][0]['readiness'] == 'not_installed'


def test_plugin_operators_and_node_credentials_stay_in_their_domains(node_env):
    _, client, runtime, _, _ = node_env
    credential = runtime.config.credential
    assert client.get('/api/platform/v1/plugins', headers={'X-Platform-Node-Credential': credential}).status_code == 401
    assert 'credential' not in json.dumps(runtime.manifest())


def test_downloaded_enrollment_installs_private_files_and_runs_node(node_env, tmp_path, fake_gh):
    _, client, _, _, transport = node_env
    data = client.post('/api/platform/v1/plugins/nodes', json={'node_id': 'second', 'label': 'Second'}).get_json()['enrollment']
    data['hub_url'] = 'https://fleet.example'
    data['plugins']['github-cli'] = {'executable': str(fake_gh)}
    path = tmp_path / 'enrollment.json'
    path.write_text(json.dumps(data))
    config = install_enrollment(path, tmp_path / 'second-state')
    runtime, _ = load_runtime(config, transport=transport)
    try:
        assert announce(runtime)
        assert runtime.config.node_id == 'second'
        assert config.stat().st_mode & 0o077 == 0
        with pytest.raises(ValueError, match='enrollment_files_exist'):
            install_enrollment(path, tmp_path / 'second-state')
    finally:
        runtime.close_all()
