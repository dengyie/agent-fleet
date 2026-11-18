"""Durable login and visible navigation/auth transitions."""
import os
from pathlib import Path
import subprocess
from threading import Thread

import pytest
from werkzeug.serving import make_server

from hub.accounts.store import AccountStore, digest
from hub.bootstrap import create_app
from hub.config import FleetConfig

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = 'Navigation-password-123!'


def test_session_survives_inactivity_restart_and_other_logins(tmp_path):
    now = [1_000_000.0]
    path = tmp_path / 'accounts.db'
    repo = AccountStore(path, clock=lambda: now[0])
    repo.bootstrap_admin('admin@example.test', PASSWORD)
    token, user = repo.login('admin@example.test', PASSWORD)
    now[0] += 10 * 365 * 86400
    assert repo.identity(token)['id'] == user['id']
    repo = AccountStore(path, clock=lambda: now[0])
    repo.login('admin@example.test', PASSWORD)
    assert repo.identity(token)['id'] == user['id']
    session = next(row for row in repo.sessions(user['id'], token) if row['current'])
    assert session['expires'] is None
    repo.revoke(user['id'], session['id'])
    assert repo.identity(token) is None
    assert AccountStore(path, clock=lambda: now[0]).identity(token) is None


def test_upgrade_preserves_valid_sessions_without_reviving_expired_ones(tmp_path):
    now = 1_000_000.0
    path = tmp_path / 'accounts.db'
    repo = AccountStore(path, clock=lambda: now)
    repo.bootstrap_admin('admin@example.test', PASSWORD)
    token, user = repo.login('admin@example.test', PASSWORD)
    with repo.connect() as db:
        db.execute('UPDATE sessions SET expires=?', (now + 100,))
        db.execute('INSERT INTO sessions VALUES (?,?,?,?)', (digest('expired'), user['id'], now - 200, now - 1))
    upgraded = AccountStore(path, clock=lambda: now)
    later = AccountStore(path, clock=lambda: now + 1000)
    assert later.identity(token)['id'] == user['id']
    assert upgraded.identity('expired') is None
    assert later.identity('expired') is None


@pytest.mark.parametrize('scenario', ['feature-401', 'logout-loading', 'login-loading', 'menus'])
def test_navigation_session_browser(tmp_path, scenario):
    if not os.environ.get('FLEET_PLAYWRIGHT_MODULE'):
        pytest.skip('Set FLEET_PLAYWRIGHT_MODULE for browser acceptance')
    app = create_app(FleetConfig.from_root(tmp_path, frontend_dir=ROOT / 'frontend', platform_enabled=True,
        accounts={'enabled': True, 'origin': 'http://127.0.0.1'}))
    accounts = app.extensions['accounts']
    accounts.bootstrap_admin('admin@example.test', PASSWORD)
    owner = accounts.users()[0]['id']
    repo = app.extensions['fleet']['platform_repository']
    repo.upsert_model(owner, {'profile_id': 'fixture', 'provider': 'deterministic', 'model': '验收模型'})
    repo.upsert_workspace(owner, {'workspace_id': 'home', 'root_path': str(tmp_path / 'workspace')})
    repo.update_defaults(owner, {'model_profile_id': 'fixture', 'workspace_id': 'home'}, 0)
    server = make_server('127.0.0.1', 0, app, threaded=True)
    origin = f'http://127.0.0.1:{server.server_port}'
    app.config['ACCOUNT_ORIGIN'] = origin
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(['node', str(ROOT / 'tests/fixtures/frontend/navigation_sessions_browser.cjs'),
            origin, scenario], capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.shutdown(); server.server_close(); thread.join(5)
