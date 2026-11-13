"""Settings must edit the same catalogs and credentials the worker consumes."""
from pathlib import Path

import pytest
from test_account_http import ORIGIN, PASSWORD, post, signup

from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.infrastructure.model_secrets import ModelSecrets


@pytest.fixture
def setup(tmp_path):
    app = create_app(FleetConfig.from_root(tmp_path, platform_enabled=True,
        platform_worker_enabled=True, platform_worker_scheduler_enabled=True, platform_provider_network_enabled=True, service_monitoring_enabled=True, accounts={'enabled': True, 'origin': ORIGIN, 'registration': 'open', 'sender': lambda *a: mail.append(a)}))
    mail = []
    app.extensions['accounts'].bootstrap_admin('admin@example.test', PASSWORD)
    client = app.test_client()
    owner = post(client, 'login', {'email': 'admin@example.test', 'password': PASSWORD}).json['user']['id']
    return app, client, owner, tmp_path, mail


def get(client, suffix=''):
    return client.get('/api/platform/v1/settings' + suffix, base_url=ORIGIN)


def put(client, suffix, body):
    return client.put('/api/platform/v1/settings' + suffix, json=body, headers={'Origin': ORIGIN}, base_url=ORIGIN)


def model():
    return {'provider': 'openai_compatible', 'model': 'example-model', 'provider_config': {'base_url': 'https://models.example.test/v1', 'timeout_s': 90}, 'enabled': True}


def test_model_edit_secret_preservation_rotation_and_restart(setup):
    app, client, owner, root, _ = setup
    first = put(client, '/models/main-model', {'expected_digest': '', 'value': model(), 'api_key': 'private-value-one'})
    assert first.status_code == 200, first.json
    row = first.json['value']
    assert row['provider_config']['timeout_s'] == 90
    assert row['secret_configured']
    assert 'secret_ref' not in row and 'private-value' not in str(first.json)
    stored = app.extensions['fleet']['platform_repository'].get_model(owner, 'main-model')
    ref = stored['secret_ref']
    assert app.extensions['fleet']['services']['platform_worker'].provider_factory.secret_broker.resolve(ref) == 'private-value-one'
    changed = model(); changed['provider_config']['base_url'] = 'https://new.example.test/v1'
    saved = put(client, '/models/main-model', {'expected_digest': row['digest'], 'value': changed})
    assert saved.status_code == 200
    assert app.extensions['fleet']['platform_repository'].get_model(owner, 'main-model')['secret_ref'] == ref
    stale = put(client, '/models/main-model', {'expected_digest': row['digest'], 'value': model()})
    assert stale.status_code == 409
    rotated = put(client, '/models/main-model', {'expected_digest': saved.json['value']['digest'], 'value': changed, 'api_key': 'private-value-two'})
    assert rotated.status_code == 200
    current = app.extensions['fleet']['platform_repository'].get_model(owner, 'main-model')
    broker = ModelSecrets(root / 'state/model-secrets')
    assert broker.resolve(current['secret_ref']) == 'private-value-two'
    assert broker.resolve(ref) == 'private-value-one'
    restarted = create_app(app.extensions['fleet']['config'])
    provider = restarted.extensions['fleet']['services']['platform_worker'].provider_factory(current)
    assert provider.api_key == 'private-value-two'
    assert provider.endpoint == 'https://new.example.test/v1/chat/completions'

    listing = get(client)
    assert listing.status_code == 200 and listing.headers['Cache-Control'] == 'no-store'
    assert listing.json['models'][0]['provider_config']['base_url'] == 'https://new.example.test/v1'
    assert 'private-value' not in listing.text and 'managed://' not in listing.text


def test_settings_admin_boundary_and_catalog_ownership(setup):
    app, admin, _owner, root, mail = setup
    user = app.test_client()
    signup(user, mail, 'ordinary@example.test')
    assert get(user).status_code == 403
    assert get(app.test_client()).status_code == 401
    assert put(admin, '/nodes/node-a', {'expected_digest': '', 'value': {'label': 'VM A', 'capabilities': {'workspace.exec': True}}}).status_code == 200
    assert put(admin, '/workspaces/work-a', {'expected_digest': '', 'value': {'name': 'Work', 'root_path': str(root / 'work'), 'default_node_id': 'node-a'}}).status_code == 200
    assert get(admin).json['workspaces'][0]['default_node_id'] == 'node-a'
    assert app.extensions['fleet']['platform_repository'].list_nodes('other-owner') == []
    assert put(admin, '/models/model-b', {'expected_digest': '', 'value': {**model(), 'secret_ref': 'env://BAD'}}).status_code == 400


def test_runtime_edit_is_atomic_conflict_fenced_and_requires_restart(setup):
    app, client, _owner, root, _ = setup
    path = root / 'fleet-gates.conf'
    path.write_text('# preserved\nCUSTOM_SETTING=hello\nAGENT_FLEET_PLATFORM_MEMORY_ENABLED=0\n', encoding='utf-8')
    initial = get(client, '/runtime')
    assert initial.status_code == 200
    payload = {'digest': initial.json['digest'], 'values': {'AGENT_FLEET_PLATFORM_MEMORY_ENABLED': True}}
    saved = put(client, '/runtime', payload)
    assert saved.status_code == 200, saved.json
    assert 'CUSTOM_SETTING=hello' in path.read_text()
    assert 'AGENT_FLEET_PLATFORM_MEMORY_ENABLED=1' in path.read_text()
    assert app.config['PLATFORM_MEMORY_ENABLED'] is False
    assert saved.json['restart_required'] is True
    assert Path(saved.json['backup_path']).read_text().startswith('# preserved')
    assert put(client, '/runtime', payload).status_code == 409
    before = path.read_bytes()
    assert put(client, '/runtime', {'digest': saved.json['digest'], 'values': {'SECRET': 'not-allowed'}}).status_code == 400
    assert path.read_bytes() == before


def test_hosts_validation_and_conflict_leave_bytes_unchanged(setup):
    _app, client, _owner, root, _ = setup
    before = get(client, '/hosts').json
    saved = put(client, '/hosts', {'digest': before['digest'], 'content': 'hosts:\n  - name: vm-a\n    desc: VM A\n    stale_after_s: 180\n'})
    assert saved.status_code == 200, saved.json
    raw = (root / 'hosts.yaml').read_bytes()
    assert put(client, '/hosts', {'digest': saved.json['digest'], 'content': 'hosts:\n  - name: duplicate\n  - name: duplicate\n'}).status_code == 400
    assert (root / 'hosts.yaml').read_bytes() == raw
    assert put(client, '/hosts', {'digest': before['digest'], 'content': 'hosts: []\n'}).status_code == 409
    assert (root / 'hosts.yaml').read_bytes() == raw


def test_file_settings_are_consumed_by_the_real_startup_loader(tmp_path, monkeypatch):
    from hub import web
    monkeypatch.setattr(web, 'FLEET_HOME', tmp_path)
    (tmp_path / 'fleet-gates.conf').write_text('AGENT_FLEET_PLATFORM_ENABLED=1\nAGENT_FLEET_PLATFORM_MEMORY_ENABLED=1\nAGENT_FLEET_PLATFORM_WORKER_INTERVAL_S=2.5\nAGENT_FLEET_PLATFORM_WORKER_MAX_CONCURRENCY=3\nAGENT_FLEET_PLATFORM_BROWSER_ALLOWED_ORIGINS=https://one.example.test,https://two.example.test\nAGENT_FLEET_PLATFORM_SANDBOX_LAUNCHER=["sandbox", "--run"]\n', encoding='utf-8')
    app = web.make_app(ingest_token="test-ingest")
    config = app.extensions['fleet']['config']
    assert config.platform_memory_enabled is True
    assert config.platform_worker_interval_s == 2.5
    assert config.platform_worker_max_concurrency == 3
    assert config.platform_browser_allowed_origins == ('https://one.example.test', 'https://two.example.test')
    assert config.platform_sandbox_launcher == ('sandbox', '--run')


def test_catalog_compare_and_write_is_atomic_between_writers(setup):
    from concurrent.futures import ThreadPoolExecutor

    from hub.infrastructure.platform_db_base import PlatformRepositoryError
    app, _client, owner, _root, _ = setup
    repository = app.extensions['fleet']['platform_repository']

    def create(label):
        try:
            repository.upsert_node(owner, {'node_id': 'racing-node', 'label': label}, expected_digest='')
            return 'saved'
        except PlatformRepositoryError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(create, ['first', 'second']))
    assert sorted(outcomes) == ['revision_conflict', 'saved']


def test_admin_can_provision_defaults_for_explicit_owner_without_affecting_self(setup):
    app, client, owner, _root, _ = setup
    repository = app.extensions['fleet']['platform_repository']
    repository.upsert_node('provisioned-user', {'node_id': 'assigned-node'})
    response = put(client, '/defaults/current?owner_id=provisioned-user', {'defaults': {'execution_node_id': 'assigned-node'}, 'revision': 0})
    assert response.status_code == 200, response.json
    assert repository.get_defaults('provisioned-user')['execution_node_id'] == 'assigned-node'
    assert repository.get_defaults(owner)['execution_node_id'] is None


def test_settings_model_is_ready_via_the_existing_readiness_check(setup):
    app, client, owner, root, _ = setup
    created = put(client, '/models/model-ready', {'expected_digest': '', 'value': model(), 'api_key': 'readiness-private-value'})
    assert created.status_code == 200
    repository = app.extensions['fleet']['platform_repository']
    (root / 'workspace').mkdir()
    repository.upsert_workspace(owner, {'workspace_id': 'workspace-ready', 'root_path': str(root / 'workspace')})
    repository.update_defaults(owner, {'model_profile_id': 'model-ready', 'workspace_id': 'workspace-ready'}, 0)
    response = client.get('/api/platform/v1/readiness', base_url=ORIGIN)
    assert response.status_code == 200, response.json
    assert response.json['configuration_ready'] is True



def test_selected_owner_node_credential_and_invalid_model_do_not_write_secrets(setup):
    app, client, owner, root, _ = setup
    repository = app.extensions['fleet']['platform_repository']
    repository.upsert_node('provisioned-user', {'node_id': 'assigned-node'})
    issued = client.post('/api/platform/v1/settings/nodes/assigned-node/credential?owner_id=provisioned-user', json={}, headers={'Origin': ORIGIN}, base_url=ORIGIN)
    assert issued.status_code == 200, issued.json
    assert issued.json['credential'].startswith('assigned-node:')
    assert issued.headers['Cache-Control'] == 'no-store'
    bad = model(); bad['provider_config'] = {}
    assert put(client, '/models/no-url', {'expected_digest': '', 'value': bad, 'api_key': 'never-persist-this'}).status_code == 400
    assert repository.get_model(owner, 'no-url') is None
    assert not (root / 'state/model-secrets').exists()


@pytest.mark.parametrize('values', [
    {'AGENT_FLEET_PLATFORM_SANDBOX_LAUNCHER': ['']},
    {'AGENT_FLEET_HTTP_PROBE_ALLOWED_ORIGINS': ['https://example.test/private/path']},
    {'AGENT_FLEET_PLATFORM_BROWSER_ALLOWED_ORIGINS': ['not-an-origin']},
])
def test_runtime_rejects_values_that_startup_cannot_consume(setup, values):
    _app, client, _owner, root, _ = setup
    initial = get(client, '/runtime').json
    response = put(client, '/runtime', {'digest': initial['digest'], 'values': values})
    assert response.status_code == 400
    assert not (root / 'fleet-gates.conf').exists()


def test_model_save_checks_actual_provider_header_contract(setup):
    app, client, owner, root, _ = setup
    value = model(); value['provider_config']['headers'] = {'unsupported-header': 'value'}
    response = put(client, '/models/bad-header', {'expected_digest': '', 'value': value, 'api_key': 'example-value'})
    assert response.status_code == 400
    assert app.extensions['fleet']['platform_repository'].get_model(owner, 'bad-header') is None
    assert not (root / 'state/model-secrets').exists()


@pytest.mark.parametrize('catalog_first', [False, True])
def test_managed_credentials_fence_old_readers_and_preserve_mcp_backup(setup, monkeypatch, catalog_first):
    import sqlite3

    from hub.infrastructure import platform_db
    from hub.infrastructure.mcp_catalog_repository import MCPCatalogRepository
    from hub.infrastructure.platform_backup import PlatformBackupService

    app, client, _owner, root, _ = setup
    repository = app.extensions['fleet']['platform_repository']
    catalog = MCPCatalogRepository(repository.db_path)
    if catalog_first:
        catalog.init()
    response = put(client, '/models/fenced-model', {'expected_digest': '', 'value': model(), 'api_key': 'immutable-key'})
    assert response.status_code == 200, response.json
    assert repository.schema_version() == 5
    catalog.init()
    assert repository.schema_version() == 5
    platform_db.PlatformRepository(repository.db_path).init()
    artifacts = root / 'backup-artifacts'; artifacts.mkdir()
    backup = PlatformBackupService()
    report = backup.create_backup(repository.db_path, artifacts, root / 'backup')
    assert report['platform_schema_version'] == 5
    backup.restore_backup(root / 'backup', root / 'restored')
    platform_db.PlatformRepository(root / 'restored/platform.db').init()
    monkeypatch.setattr(platform_db, 'MAX_READER_VERSION', 4)
    with pytest.raises(platform_db.PlatformRepositoryError, match='unsupported_schema_version'):
        platform_db.PlatformRepository(repository.db_path).init()
    # Removing MCP tables must still be detected after the credential version bump.
    with sqlite3.connect(repository.db_path) as connection:
        connection.execute('DROP TABLE mcp_catalog_connectors')
        connection.execute('DROP TABLE mcp_catalog_meta')
    monkeypatch.setattr(platform_db, 'MAX_READER_VERSION', 5)
    with pytest.raises(platform_db.PlatformRepositoryError, match='incomplete_mcp_catalog_schema'):
        platform_db.PlatformRepository(repository.db_path).init()



def test_runtime_settings_remain_accessible_before_platform_enabled(tmp_path):
    app = create_app(FleetConfig.from_root(tmp_path, dev_operator='operator@example.test'))
    client = app.test_client()
    assert client.get('/api/platform/v1/settings/runtime').status_code == 200
    disabled = client.get('/api/platform/v1/settings')
    assert disabled.status_code == 503 and disabled.json['error'] == 'platform_disabled'


def test_administrator_can_disable_model_with_unavailable_old_secret(setup, monkeypatch):
    app, client, owner, _root, _ = setup
    monkeypatch.delenv('MISSING_PROVIDER_VALUE', raising=False)
    repository = app.extensions['fleet']['platform_repository']
    repository.upsert_model(owner, {'profile_id': 'broken-model', **model(), 'secret_ref': 'env://MISSING_PROVIDER_VALUE'})
    row = get(client).json['models'][0]
    response = put(client, '/models/broken-model', {'expected_digest': row['digest'], 'value': {**model(), 'enabled': False}})
    assert response.status_code == 200, response.json
    assert repository.get_model(owner, 'broken-model')['enabled'] is False
