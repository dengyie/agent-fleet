"""Workspace enrollment is generated from owner-scoped catalog bindings."""
import json
from dataclasses import replace
from pathlib import Path

import pytest

from fleet_core.control import generate_ed25519_keypair
from hub.bootstrap import create_app
from hub.config import FleetConfig
from tools.plugins.node import install_enrollment, load_runtime

OWNER = 'vm-owner@example.test'
URL = '/api/platform/v1/settings/nodes/vm/enrollment'
BODY = {'hub_url': 'https://hub.example', 'execution_mode': 'vm_direct', 'sandbox_launcher': []}


def test_oversized_unicode_enrollment_preserves_live_credential(setup):
    _app, client, repo, old = setup
    for i in range(31):
        repo.upsert_workspace(OWNER, {'workspace_id': 'work-' + str(i),
            'root_path': '/tmp/' + ('中' * 60 + '/') * 16 + str(i), 'default_node_id': 'vm'})
    response = client.post(URL, json=BODY)
    assert response.status_code == 400
    assert repo.authenticate_node_credential('vm', old) is not None


def test_oversized_launcher_preserves_live_credential(setup):
    _app, client, repo, old = setup
    for i in range(15):
        repo.upsert_workspace(OWNER, {'workspace_id': 'work-' + str(i),
            'root_path': '/tmp/' + ('中' * 60 + '/') * 16 + str(i), 'default_node_id': 'vm'})
    response = client.post(URL, json={**BODY, 'execution_mode': 'sandbox',
                                      'sandbox_launcher': ['x' * 4096] * 5})
    assert response.status_code == 400
    assert repo.authenticate_node_credential('vm', old) is not None


@pytest.mark.parametrize('size,accepted', [(65536, True), (65537, False)])
def test_node_serialization_exact_byte_boundary(size, accepted):
    from fleet_core.workspace_execution import node_config_text
    # {"v": "..."} with the same pretty encoding used for the file.
    overhead = len(node_config_text({'v': ''}).encode('utf-8'))
    value = {'v': 'x' * (size - overhead)}
    if accepted:
        assert len(node_config_text(value).encode('utf-8')) == size
    else:
        with pytest.raises(ValueError):
            node_config_text(value)


def test_node_config_size_rejection_creates_no_partial_credentials(tmp_path):
    from fleet_core.control import generate_ed25519_keypair
    import base64
    _, public = generate_ed25519_keypair()
    data = {'node_id': 'vm', 'hub_url': 'https://hub.example', 'credential': 'vm:' + 's' * 40,
        'public_key': base64.b64encode(public).decode('ascii'), 'plugins': {},
        'workspace': {'execution_mode': 'vm_direct', 'sandbox_launcher': [], 'roots': {'work': '/work'}}}
    source = tmp_path / 'enrollment.json'
    source.write_text(json.dumps(data), encoding='utf-8')
    state = Path('/' + 'x' * 23000)
    # Long installation paths expand three times in node.json; validation must
    # happen before attempting any mkdir/open in that location.
    with pytest.raises(ValueError):
        install_enrollment(source, state)


@pytest.fixture
def setup(tmp_path):
    private, _ = generate_ed25519_keypair()
    app = create_app(FleetConfig.from_root(tmp_path, dev_operator=OWNER, platform_enabled=True,
        platform_remote_execution_enabled=True, platform_command_signing_raw=private))
    repo = app.extensions['fleet']['platform_repository']
    repo.upsert_node(OWNER, {'node_id': 'vm', 'capabilities': {'workspace.exec': True}})
    repo.upsert_workspace(OWNER, {'workspace_id': 'work', 'root_path': str(tmp_path / 'work'), 'default_node_id': 'vm'})
    old = repo.provision_node_credential(OWNER, 'vm')['credential'].partition(':')[2]
    return app, app.test_client(), repo, old


def test_enrollment_download_round_trips_into_original_node_cli(setup, tmp_path):
    app, client, repo, old = setup
    repo.upsert_workspace('other-owner', {'workspace_id': 'private', 'root_path': '/private/other'})
    response = client.post(URL, json=BODY)
    assert response.status_code == 200, response.json
    assert response.headers['Cache-Control'] == 'no-store'
    data = response.json['enrollment']
    assert data['workspace'] == {'execution_mode': 'vm_direct', 'sandbox_launcher': [],
                                  'roots': {'work': str(tmp_path / 'work')}}
    assert data['plugins'] == {}
    assert repo.authenticate_node_credential('vm', old) is None
    assert repo.authenticate_node_credential('vm', data['credential'].partition(':')[2])['owner_id'] == OWNER
    source = tmp_path / 'enrollment.json'
    source.write_text(json.dumps(data), encoding='utf-8')
    node, _ = load_runtime(install_enrollment(source, tmp_path / 'state'))
    try:
        assert node.manifest()['workspace_execution']['mode'] == 'vm_direct'
    finally:
        node.close_all()
    catalog = client.get('/api/platform/v1/settings').text
    assert data['credential'] not in catalog


@pytest.mark.parametrize('invalid', [
    {'execution_mode': 'host'}, {'execution_mode': None}, {'sandbox_launcher': ['sh']},
    {'execution_mode': 'sandbox'}, {'hub_url': 'http://public.example'},
    {'hub_url': 'https://example/path'}, {'extra': True},
])
def test_invalid_enrollment_does_not_rotate_credential(setup, invalid):
    _app, client, repo, old = setup
    assert client.post(URL, json={**BODY, **invalid}).status_code == 400
    assert repo.authenticate_node_credential('vm', old) is not None


@pytest.mark.parametrize('gate', ['remote', 'signing', 'node', 'workspace', 'capability'])
def test_unavailable_node_configuration_does_not_rotate_credential(setup, gate):
    app, client, repo, old = setup
    fleet = app.extensions['fleet']
    if gate == 'remote':
        fleet['config'] = replace(fleet['config'], platform_remote_execution_enabled=False)
    elif gate == 'signing':
        fleet['services']['platform_delivery'].signing_key = None
    elif gate == 'node':
        repo.upsert_node(OWNER, {'node_id': 'vm', 'enabled': False})
    elif gate == 'capability':
        repo.upsert_node(OWNER, {'node_id': 'vm', 'capabilities': {}})
    else:
        repo.upsert_workspace(OWNER, {'workspace_id': 'work', 'root_path': '/work', 'enabled': False})
    response = client.post(URL, json=BODY)
    assert response.status_code in {409, 503}, response.json
    # Disabled nodes cannot authenticate, but issuing timestamp/digest is unchanged.
    repo.upsert_node(OWNER, {'node_id': 'vm'})
    assert repo.authenticate_node_credential('vm', old) is not None


def test_enrollment_respects_selected_owner_and_account_default_node(setup, tmp_path):
    _app, client, repo, _ = setup
    assert client.post(URL + '?owner_id=other-owner', json=BODY).status_code == 409
    repo.upsert_node('other-owner', {'node_id': 'vm', 'capabilities': {'workspace.exec': True}})
    repo.upsert_workspace('other-owner', {'workspace_id': 'other', 'root_path': str(tmp_path / 'other')})
    repo.update_defaults('other-owner', {'workspace_id': 'other', 'execution_node_id': 'vm'}, 0)
    response = client.post(URL + '?owner_id=other-owner', json=BODY)
    assert response.status_code == 200, response.json
    assert response.json['enrollment']['workspace']['roots'] == {'other': str(tmp_path / 'other')}
