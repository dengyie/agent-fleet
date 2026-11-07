import base64
import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig


def test_catalog_exposes_missing_command_key_before_enrollment(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENT_FLEET_PLATFORM_COMMAND_SIGNING_KEY', raising=False)
    monkeypatch.setattr('hub.auth.FLEET_HOME', tmp_path)
    app = create_app(FleetConfig.from_root(tmp_path, dev_operator='setup@example.test', platform_enabled=True))
    client = app.test_client()
    catalog = client.get('/api/platform/v1/plugins').get_json()
    assert catalog['enrollment_ready'] is False
    assert catalog['setup_error'] == 'command_signature_required'
    response = client.post('/api/platform/v1/plugins/nodes', json={'node_id': 'vm'})
    assert response.status_code == 503
    assert response.get_json()['error'] == 'command_signature_required'


def test_hub_setup_persists_key_and_restart_enables_signed_enrollment(tmp_path, monkeypatch):
    from hub.plugins_setup import initialize
    from fleet_core.platform_command import PlatformCommand, verify_command
    monkeypatch.delenv('AGENT_FLEET_PLATFORM_COMMAND_SIGNING_KEY', raising=False)
    monkeypatch.setattr('hub.auth.FLEET_HOME', tmp_path)
    before = initialize(tmp_path)
    key_file = tmp_path / 'credentials/platform-command-signing.key'
    key = key_file.read_bytes()
    assert before['state'] == 'created'
    assert len(base64.b64decode(key.strip(), validate=True)) == 32
    assert initialize(tmp_path)['state'] == 'configured'
    assert key_file.read_bytes() == key
    app = create_app(FleetConfig.from_root(tmp_path, dev_operator='setup@example.test', platform_enabled=True))
    client = app.test_client()
    assert client.get('/api/platform/v1/plugins').get_json()['enrollment_ready'] is True
    response = client.post('/api/platform/v1/plugins/nodes', json={'node_id': 'vm'})
    assert response.status_code == 201
    enrollment = response.get_json()['enrollment']
    delivery = app.extensions['fleet']['services']['plugins'].delivery
    command = PlatformCommand.create(command_id='setup-command', target_node='vm', owner_id='setup@example.test',
                                     action='plugin.github-cli.status', resource_id='default', arguments={},
                                     retry_class='read_only', expires_at=2**31).signed(delivery.signing_key)
    assert verify_command(command.as_dict(), base64.b64decode(enrollment['public_key']))


def test_setup_preserves_environment_and_rejects_invalid_existing_material(tmp_path, monkeypatch):
    from hub.plugins_setup import initialize
    monkeypatch.setenv('AGENT_FLEET_PLATFORM_COMMAND_SIGNING_KEY', base64.b64encode(b'k' * 32).decode())
    assert initialize(tmp_path)['state'] == 'configured'
    assert not (tmp_path / 'credentials').exists()
    monkeypatch.setenv('AGENT_FLEET_PLATFORM_COMMAND_SIGNING_KEY', 'invalid')
    with pytest.raises(ValueError, match='invalid_command_signing_key'):
        initialize(tmp_path)
    monkeypatch.delenv('AGENT_FLEET_PLATFORM_COMMAND_SIGNING_KEY')
    file = tmp_path / 'credentials/platform-command-signing.key'
    file.parent.mkdir()
    file.write_text('invalid', encoding='utf-8')
    with pytest.raises(ValueError, match='invalid_command_signing_key'):
        initialize(tmp_path)
    assert file.read_text() == 'invalid'
