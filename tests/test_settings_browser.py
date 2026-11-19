"""Real settings UI saves to Flask/SQLite/files without using a model service."""
import os
from pathlib import Path

import pytest
from test_account_http import PASSWORD
from test_workflow_browser import _browser_acceptance

from hub.bootstrap import create_app
from hub.config import FleetConfig
from fleet_core.control import generate_ed25519_keypair
from tools.plugins.node import install_enrollment, load_runtime


@pytest.mark.parametrize('viewport', ['desktop', 'mobile'])
def test_settings_save_reload_and_navigation(tmp_path, viewport):
    if not os.environ.get('FLEET_PLAYWRIGHT_MODULE'):
        pytest.skip('Set FLEET_PLAYWRIGHT_MODULE to run Chromium acceptance')
    private, _ = generate_ed25519_keypair()
    app = create_app(FleetConfig.from_root(tmp_path, frontend_dir=Path(__file__).resolve().parents[1] / 'frontend', platform_enabled=True,
        platform_remote_execution_enabled=True, platform_command_signing_raw=private,
        accounts={'enabled': True, 'origin': 'http://127.0.0.1'}))
    app.config['ACCOUNT_COOKIE_SECURE'] = False
    app.extensions['accounts'].bootstrap_admin('admin@example.test', PASSWORD)
    _browser_acceptance(app, 'settings_browser.cjs', viewport)


@pytest.mark.parametrize('viewport', ['desktop', 'mobile'])
@pytest.mark.parametrize('scenario', ['interrupted-opening', 'category-navigation'])
def test_settings_overlay_motion(tmp_path, viewport, scenario):
    if not os.environ.get('FLEET_PLAYWRIGHT_MODULE'):
        pytest.skip('Set FLEET_PLAYWRIGHT_MODULE to run Chromium acceptance')
    app = create_app(FleetConfig.from_root(tmp_path, frontend_dir=Path(__file__).resolve().parents[1] / 'frontend',
        platform_enabled=True, accounts={'enabled': True, 'origin': 'http://127.0.0.1'}))
    app.config['ACCOUNT_COOKIE_SECURE'] = False
    app.extensions['accounts'].bootstrap_admin('admin@example.test', PASSWORD)
    _browser_acceptance(app, 'settings_motion_browser.cjs', viewport, scenario)


@pytest.mark.parametrize('scenario', ['failed-switch', 'inflight-save', 'inflight-editor'])
def test_settings_owner_switch_failure_and_inflight_save(tmp_path, scenario):
    if not os.environ.get('FLEET_PLAYWRIGHT_MODULE'):
        pytest.skip('Set FLEET_PLAYWRIGHT_MODULE to run Chromium acceptance')
    app = create_app(FleetConfig.from_root(tmp_path, frontend_dir=Path(__file__).resolve().parents[1] / 'frontend',
        platform_enabled=True, accounts={'enabled': True, 'origin': 'http://127.0.0.1'}))
    app.config['ACCOUNT_COOKIE_SECURE'] = False
    app.extensions['accounts'].bootstrap_admin('admin@example.test', PASSWORD)
    _browser_acceptance(app, 'settings_scope_browser.cjs', scenario)


@pytest.fixture
def lifecycle_app(tmp_path):
    if not os.environ.get('FLEET_PLAYWRIGHT_MODULE'):
        pytest.skip('Set FLEET_PLAYWRIGHT_MODULE to run Chromium acceptance')
    private, _ = generate_ed25519_keypair()
    app = create_app(FleetConfig.from_root(tmp_path, frontend_dir=Path(__file__).resolve().parents[1] / 'frontend',
        platform_enabled=True, platform_remote_execution_enabled=True, platform_command_signing_raw=private,
        platform_mcp_connectors_enabled=True,
        accounts={'enabled': True, 'origin': 'http://127.0.0.1'}))
    app.config['ACCOUNT_COOKIE_SECURE'] = False
    app.extensions['accounts'].bootstrap_admin('admin@example.test', PASSWORD)
    client = app.test_client()
    owner = client.post('/api/accounts/login', json={'email': 'admin@example.test', 'password': PASSWORD},
        base_url='http://127.0.0.1', headers={'Origin': 'http://127.0.0.1'}).json['user']['id']
    repo = app.extensions['fleet']['platform_repository']
    for scope, label in ((owner, 'Review VM'), ('other-owner', 'Other VM')):
        repo.upsert_node(scope, {'node_id': 'review-node', 'label': label, 'capabilities': {'workspace.exec': True}})
        repo.upsert_workspace(scope, {'workspace_id': 'review-work', 'root_path': str(tmp_path / 'work' / scope),
                                     'default_node_id': 'review-node'})
    old = repo.provision_node_credential(owner, 'review-node')['credential'].partition(':')[2]
    return app, repo, owner, old


@pytest.mark.parametrize('scenario', ['category', 'close', 'editor', 'owner', 'evict', 'logout', 'assistant-evict'])
def test_settings_enrollment_delivery_lifetime(lifecycle_app, tmp_path, scenario):
    app, repo, owner, old = lifecycle_app
    output = tmp_path / 'downloaded-enrollment.json'
    _browser_acceptance(app, 'settings_lifecycle_browser.cjs', 'enrollment', scenario, str(output))
    assert repo.authenticate_node_credential('review-node', old) is None
    if scenario == 'logout':
        assert not output.exists()
        return
    node, _ = load_runtime(install_enrollment(output, tmp_path / 'node-state'))
    try:
        assert repo.authenticate_node_credential('review-node', node.config.credential.partition(':')[2])['owner_id'] == owner
    finally:
        node.close_all()


@pytest.mark.parametrize('kind', ['runtime', 'hosts'])
@pytest.mark.parametrize('outcome', ['success', 'failure'])
def test_settings_runtime_late_receipt_preserves_new_draft(lifecycle_app, kind, outcome):
    app, _repo, _owner, _old = lifecycle_app
    _browser_acceptance(app, 'settings_lifecycle_browser.cjs', 'runtime', kind, outcome)


@pytest.mark.parametrize('scenario', ['retry', 'reenter', 'inflight'])
def test_settings_integrations_load_recovery(lifecycle_app, scenario):
    app, _repo, _owner, _old = lifecycle_app
    _browser_acceptance(app, 'settings_lifecycle_browser.cjs', 'integrations', scenario)
