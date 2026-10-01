"""Session validation is independent of task/platform availability."""
import pytest
from hub.bootstrap import create_app
from hub.config import FleetConfig


def test_session_requires_verified_operator_even_without_feature_gates(tmp_path):
    app = create_app(FleetConfig.from_root(tmp_path, tasks_enabled=False, platform_enabled=False))
    client = app.test_client()
    assert client.get('/api/operator/session').status_code == 401
    assert client.get('/api/operator/session', headers={'X-Access-Token': 'unverified'}).status_code == 401
    response = client.get('/api/operator/session', headers={'Cf-Access-Authenticated-User-Email': 'operator@example.test'})
    assert response.status_code == 200
    assert response.get_json() == {'authenticated': True}
    assert 'no-store' in response.headers['Cache-Control']


@pytest.mark.parametrize('header', ['X-Runner-Credential', 'X-Agent-Fleet-Token', 'X-Supervisor-Credential', 'X-Platform-Node-Credential'])
def test_foreign_credentials_cannot_validate_operator_session(tmp_path, header):
    app = create_app(FleetConfig.from_root(tmp_path, dev_operator='operator@example.test'))
    client = app.test_client()
    assert client.get('/api/operator/session').status_code == 200
    assert client.get('/api/operator/session', headers={header: 'foreign'}).status_code == 401
