"""Account UI acceptance against a disposable Hub with an in-process mail sink."""
import json
import os
from pathlib import Path
import subprocess
import time
from threading import Event, Thread

import pytest
from werkzeug.serving import make_server
from hub.bootstrap import create_app
from hub.config import FleetConfig

ROOT = Path(__file__).resolve().parents[1]


def test_account_browser(tmp_path):
    if not os.environ.get('FLEET_PLAYWRIGHT_MODULE'):
        pytest.skip('Set FLEET_PLAYWRIGHT_MODULE for browser acceptance')
    mail, mailbox = [], tmp_path / 'mail.json'
    clock = [time.time()]
    def sender(*args):
        mail.append(args)
        mailbox.write_text(json.dumps(mail))
        clock[0] += 61  # Advance the resend cooldown without sleeping in acceptance.
    app = create_app(FleetConfig.from_root(tmp_path, frontend_dir=ROOT / 'frontend', platform_enabled=True,
        accounts={'enabled': True, 'origin': 'http://127.0.0.1', 'registration': 'invite', 'sender': sender}))
    app.extensions['accounts'].clock = lambda: clock[0]
    invite_code = app.extensions['accounts'].invite('browser@example.test')
    server = make_server('127.0.0.1', 0, app, threaded=True)
    origin = f'http://127.0.0.1:{server.server_port}'
    app.config['ACCOUNT_ORIGIN'] = origin
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        result = subprocess.run(['node', str(ROOT / 'tests/fixtures/frontend/accounts_browser.cjs'), origin, str(mailbox), invite_code],
                                capture_output=True, text=True, timeout=90)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.shutdown(); server.server_close(); thread.join(5)


@pytest.mark.parametrize('role', ['user', 'admin'])
def test_account_assistant_permissions_and_core_flows(tmp_path, role):
    if not os.environ.get('FLEET_PLAYWRIGHT_MODULE'):
        pytest.skip('Set FLEET_PLAYWRIGHT_MODULE for browser acceptance')
    from flask import request
    from tools.platform.providers.base import ModelResponse

    app = create_app(FleetConfig.from_root(
        tmp_path, frontend_dir=ROOT / 'frontend', ingest_token='browser-machine-token',
        platform_enabled=True, platform_worker_enabled=True,
        platform_memory_enabled=True, platform_memory_context_enabled=True,
        execution_windows_enabled=True,
        accounts={'enabled': True, 'origin': 'http://127.0.0.1', 'registration': 'invite'}))
    accounts = app.extensions['accounts']
    accounts.bootstrap_admin('admin@example.test', 'Review-password-123!', login_name='mango')
    if role == 'user':
        mail = []
        accounts.invite('user@example.test')
        accounts.issue_code('user@example.test', 'register', 'invite', lambda *a: mail.append(a))
        accounts.complete_code('user@example.test', 'register', mail[-1][2], 'Review-password-123!')
    owner = next(user['id'] for user in accounts.users() if user['role'] == role)
    repo = app.extensions['fleet']['platform_repository']
    repo.upsert_model(owner, {'profile_id': 'fixture', 'provider': 'deterministic', 'model': '本地验收模型'})
    repo.upsert_workspace(owner, {'workspace_id': 'home', 'root_path': str(tmp_path / 'workspace')})
    repo.update_defaults(owner, {'model_profile_id': 'fixture', 'workspace_id': 'home'}, 0)
    app.extensions['fleet']['repositories']['platform_memory'].create(owner, {
        'memory_id': 'fixture-memory', 'kind': 'note', 'title': 'fixture memory',
        'content': 'Use the owner workspace.', 'tags': ['fixture'],
    }, now=time.time())
    report = tmp_path / 'report.txt'
    report.write_text('Owner-scoped artifact preview')
    app.extensions['fleet']['services']['platform_artifacts'].put_file(owner, 'home', report)
    cancelled, stopping = Event(), Event()

    @app.after_request
    def acknowledge_cancel(response):
        if request.path.endswith('/cancel') and response.status_code == 200:
            cancelled.set()
        return response

    class Provider:
        def complete(self, messages, tools, *, request_observer=None):
            if messages[-1].get('content') == 'cancel this run':
                if not cancelled.wait(15):
                    raise RuntimeError('browser did not cancel the running task')
            return ModelResponse(kind='final', text='Account assistant completed')

    worker = app.extensions['fleet']['services']['platform_worker']
    worker.provider_factory = lambda _: Provider()

    def execute_runs():
        while not stopping.is_set():
            worker.run_once(owner)
            stopping.wait(.1)

    server = make_server('127.0.0.1', 0, app, threaded=True)
    origin = f'http://127.0.0.1:{server.server_port}'
    app.config['ACCOUNT_ORIGIN'] = origin
    serving = Thread(target=server.serve_forever, daemon=True)
    runner = Thread(target=execute_runs, daemon=True)
    serving.start(); runner.start()
    try:
        result = subprocess.run(['node', str(ROOT / 'tests/fixtures/frontend/account_assistant_browser.cjs'), origin, role],
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        stopping.set(); cancelled.set(); runner.join(5)
        server.shutdown(); server.server_close(); serving.join(5)


def test_account_switch_clears_previous_identity(tmp_path):
    if not os.environ.get('FLEET_PLAYWRIGHT_MODULE'):
        pytest.skip('Set FLEET_PLAYWRIGHT_MODULE for browser acceptance')
    app = create_app(FleetConfig.from_root(tmp_path, frontend_dir=ROOT / 'frontend', accounts={
        'enabled': True, 'origin': 'http://127.0.0.1', 'registration': 'invite'}))
    repo = app.extensions['accounts']
    repo.bootstrap_admin('admin@example.test', 'Review-password-123!')
    mail = []
    repo.invite('user@example.test')
    repo.issue_code('user@example.test', 'register', 'invite', lambda *a: mail.append(a))
    repo.complete_code('user@example.test', 'register', mail[0][2], 'Review-password-123!')
    repo.invite('user2@example.test')
    repo.issue_code('user2@example.test', 'register', 'invite', lambda *a: mail.append(a))
    repo.complete_code('user2@example.test', 'register', mail[-1][2], 'Review-password-123!')
    server = make_server('127.0.0.1', 0, app, threaded=True)
    origin = f'http://127.0.0.1:{server.server_port}'
    app.config['ACCOUNT_ORIGIN'] = origin
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        result = subprocess.run(['node', str(ROOT / 'tests/fixtures/frontend/account_switch_browser.cjs'), origin],
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.shutdown(); server.server_close(); thread.join(5)
