"""Node retirement cannot strand bindings or transfer a previous VM's state."""
import pytest

from fleet_core.plugins import PluginError
from hub.infrastructure.platform_db_base import PlatformRepositoryError
from hub.infrastructure.platform_db_catalog import catalog_digest
from tests.test_plugins import env, enable, OWNER  # noqa: F401 - shared pytest fixture


def retire(repo):
    repo.upsert_node(OWNER, {'node_id': 'vm', 'enabled': False})
    repo.remove_node(OWNER, 'vm', catalog_digest(repo.get_node(OWNER, 'vm')))


def test_service_binding_blocks_retirement_even_when_service_is_disabled(env):
    app, _, _ = env
    from hub.infrastructure.service_repository import ServiceRepository
    repo = app.extensions['fleet']['platform_repository']
    services = ServiceRepository(repo.db_path)
    services.init()
    services.upsert_service(OWNER, {'service_id': 'app', 'node_id': 'vm', 'name': 'app',
        'adapter': 'systemd', 'target_alias': 'app.service', 'enabled': False})
    with pytest.raises(PlatformRepositoryError, match='node_in_use'):
        retire(repo)
    assert repo.get_node(OWNER, 'vm') is not None
    assert services.get_service(OWNER, 'app')['node_id'] == 'vm'


def test_enabled_plugin_binding_blocks_retirement(env):
    app, client, _ = env
    enable(client)
    with pytest.raises(PlatformRepositoryError, match='node_in_use'):
        retire(app.extensions['fleet']['platform_repository'])


def test_reenrollment_does_not_inherit_disabled_targets_offers_or_observations(env):
    app, client, _ = env
    enable(client)
    repo = app.extensions['fleet']['platform_repository']
    plugins = app.extensions['fleet']['services']['plugins']
    plugins.repository.observe(OWNER, 'vm', 'github-cli', 'default', 'github.com',
        {'state': 'authenticated', 'username': 'old-user'}, 1)
    plugins.set_target(OWNER, 'github-cli', 'vm', False)
    retire(repo)
    plugins.enroll(OWNER, 'vm', 'Replacement')
    repo.record_node_heartbeat(OWNER, 'vm')
    target = plugins.targets(OWNER, 'github-cli')['nodes'][0]
    assert target['enabled'] is False
    assert target['offer'] is None
    assert target['observations'] == []
    assert target['readiness'] == 'not_installed'


def test_target_admission_rechecks_node_after_service_validation(env, monkeypatch):
    app, _, _ = env
    repo = app.extensions['fleet']['platform_repository']
    plugins = app.extensions['fleet']['services']['plugins']
    write = plugins.repository.set_target

    def interleave(*args):
        retire(repo)
        return write(*args)

    monkeypatch.setattr(plugins.repository, 'set_target', interleave)
    with pytest.raises(PluginError, match='node_not_found'):
        plugins.set_target(OWNER, 'github-cli', 'vm', True)
    assert plugins.repository.node_data(OWNER, 'github-cli')[0] == {}


def test_late_authenticated_report_cannot_repopulate_replacement_inventory(env, monkeypatch):
    from tests.test_plugins import announce
    app, client, _ = env
    repo = app.extensions['fleet']['platform_repository']
    plugins = app.extensions['fleet']['services']['plugins']
    report = plugins.repository.report

    def interleave(*args, **kwargs):
        retire(repo)
        plugins.enroll(OWNER, 'vm', 'Replacement')
        return report(*args, **kwargs)

    monkeypatch.setattr(plugins.repository, 'report', interleave)
    response = announce(client)
    assert response.status_code == 403
    assert response.json['error'] == 'node_credential_revoked'
    assert plugins.repository.node_data(OWNER, 'github-cli')[1] == {}


def test_late_receipt_preserves_history_but_cannot_restore_old_observation(env):
    app, _, _ = env
    repo = app.extensions['fleet']['platform_repository']
    plugins = app.extensions['fleet']['services']['plugins']
    fingerprint = repo.authenticate_node_credential('vm', 'n' * 40)['credential_fingerprint']
    # The durable command receipt can commit just before retirement. Projection
    # runs afterward using the credential of that original authenticated request.
    row = {'action': 'plugin.github-cli.status', 'status': 'succeeded',
           'result': {'result': {'auth': {'state': 'authenticated', 'username': 'old'}}},
           'arguments': {'context_id': 'default', 'hostname': 'github.com'},
           'created_at': 1, 'updated_at': 2}
    retire(repo)
    plugins.enroll(OWNER, 'vm', 'Replacement')
    plugins.record_receipt(OWNER, 'vm', row, credential_fingerprint=fingerprint)
    assert plugins.repository.node_data(OWNER, 'github-cli')[2] == {}


def test_service_registration_and_retirement_serialize(env, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from hub.infrastructure.service_repository import ServiceRepository
    app, _, _ = env
    repo = app.extensions['fleet']['platform_repository']
    services = ServiceRepository(repo.db_path)
    services.init()
    at_insert, removal_started = Event(), Event()
    connect = services._connect

    def traced_connection():
        conn = connect()
        def trace(sql):
            if sql.startswith('INSERT INTO service_definitions'):
                assert conn.in_transaction, 'reference validation must share the write transaction'
                at_insert.set()
                assert removal_started.wait(5)
        conn.set_trace_callback(trace)
        return conn

    monkeypatch.setattr(services, '_connect', traced_connection)
    def remove():
        assert at_insert.wait(5)
        removal_started.set()
        with pytest.raises(PlatformRepositoryError, match='node_in_use'):
            retire(repo)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(remove)
        services.upsert_service(OWNER, {'service_id': 'app', 'node_id': 'vm', 'name': 'app',
            'adapter': 'systemd', 'target_alias': 'app.service'})
        future.result(timeout=10)
    assert repo.get_node(OWNER, 'vm') is not None
