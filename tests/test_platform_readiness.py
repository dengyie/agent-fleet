from hub.bootstrap import create_app
from hub.config import FleetConfig

OWNER = 'readiness@example.test'


def test_readiness_catches_disabled_worker_and_empty_catalog(tmp_path):
    app = create_app(FleetConfig.from_root(tmp_path, dev_operator=OWNER, platform_enabled=True))
    response = app.test_client().get('/api/platform/v1/readiness')
    assert response.status_code == 503
    data = response.get_json()
    assert data['configuration_ready'] is False
    assert {'worker_disabled','scheduler_disabled','default_model_unavailable','default_workspace_unavailable','service_monitoring_disabled'} <= set(data['issues'])


def test_readiness_checks_owner_secret_and_workspace_without_calling_provider(tmp_path, monkeypatch):
    app = create_app(FleetConfig.from_root(tmp_path, dev_operator=OWNER, platform_enabled=True,
        platform_worker_enabled=True, platform_worker_scheduler_enabled=True,
        platform_provider_network_enabled=True, service_monitoring_enabled=True))
    repo = app.extensions['fleet']['platform_repository']
    repo.upsert_model(OWNER, {'profile_id':'model','provider':'openai_compatible','model':'model',
        'secret_ref':'env://FLEET_READINESS_SECRET','provider_config':{'endpoint':'https://provider.example.test/v1/chat/completions'}})
    workspace = tmp_path / 'workspace'; workspace.mkdir()
    repo.upsert_workspace(OWNER, {'workspace_id':'home','root_path':str(workspace)})
    repo.update_defaults(OWNER, {'model_profile_id':'model','workspace_id':'home'},0)
    client = app.test_client()
    monkeypatch.delenv('FLEET_READINESS_SECRET',raising=False)
    assert 'provider_secret_unavailable' in client.get('/api/platform/v1/readiness').get_json()['issues']
    monkeypatch.setenv('FLEET_READINESS_SECRET','private-secret-value')
    result = client.get('/api/platform/v1/readiness')
    assert result.status_code == 200
    assert result.get_json()['configuration_ready']
    assert result.get_json()['provider_connectivity'] == 'not_tested'
    assert 'private-secret-value' not in result.text and str(workspace) not in result.text
    assert client.get('/api/platform/v1/readiness',headers={'Cf-Access-Authenticated-User-Email':'other@example.test'}).status_code == 503
    app.config['DEV_OPERATOR'] = None
    assert client.get('/api/platform/v1/readiness').status_code == 401


def test_deployment_cli_fails_when_platform_is_not_enabled(tmp_path, capsys):
    from threading import Thread
    from werkzeug.serving import make_server
    from tools.platform.readiness_check import main
    app = create_app(FleetConfig.from_root(tmp_path))
    server = make_server('127.0.0.1', 0, app)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        assert main(['--endpoint', f'http://127.0.0.1:{server.server_port}']) == 1
    finally:
        server.shutdown(); server.server_close(); thread.join(2)
    assert '"http_status": 404' in capsys.readouterr().out
