"""Real settings UI saves to Flask/SQLite/files without using a model service."""
import os
from pathlib import Path

import pytest
from test_account_http import PASSWORD
from test_workflow_browser import _browser_acceptance

from hub.bootstrap import create_app
from hub.config import FleetConfig


@pytest.mark.parametrize('viewport', ['desktop', 'mobile'])
def test_settings_save_reload_and_navigation(tmp_path, viewport):
    if not os.environ.get('FLEET_PLAYWRIGHT_MODULE'):
        pytest.skip('Set FLEET_PLAYWRIGHT_MODULE to run Chromium acceptance')
    app = create_app(FleetConfig.from_root(tmp_path, frontend_dir=Path(__file__).resolve().parents[1] / 'frontend', platform_enabled=True,
        accounts={'enabled': True, 'origin': 'http://127.0.0.1'}))
    app.config['ACCOUNT_COOKIE_SECURE'] = False
    app.extensions['accounts'].bootstrap_admin('admin@example.test', PASSWORD)
    _browser_acceptance(app, 'settings_browser.cjs', viewport)


@pytest.mark.parametrize('scenario', ['failed-switch', 'inflight-save'])
def test_settings_owner_switch_failure_and_inflight_save(tmp_path, scenario):
    if not os.environ.get('FLEET_PLAYWRIGHT_MODULE'):
        pytest.skip('Set FLEET_PLAYWRIGHT_MODULE to run Chromium acceptance')
    app = create_app(FleetConfig.from_root(tmp_path, frontend_dir=Path(__file__).resolve().parents[1] / 'frontend',
        platform_enabled=True, accounts={'enabled': True, 'origin': 'http://127.0.0.1'}))
    app.config['ACCOUNT_COOKIE_SECURE'] = False
    app.extensions['accounts'].bootstrap_admin('admin@example.test', PASSWORD)
    _browser_acceptance(app, 'settings_scope_browser.cjs', scenario)
