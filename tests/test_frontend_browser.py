"""Optional local loopback Chromium UI acceptance; no network-boundary proof.

FLEET_PLAYWRIGHT_MODULE=/path/to/playwright enables this test. No browser or
Node dependency is shipped in the production frontend.
"""
import os
from pathlib import Path
import subprocess
from threading import Event, Thread
import time

import pytest
from werkzeug.serving import make_server

from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub import state, events, task_store
from hub.infrastructure.session_repository import SessionRepository
from tools.platform.providers.base import ModelResponse

ROOT = Path(__file__).resolve().parents[1]
OWNER = 'browser@example.test'


def test_assistant_auth_in_real_browser(tmp_path):
    if not os.environ.get('FLEET_PLAYWRIGHT_MODULE'):
        pytest.skip('Set FLEET_PLAYWRIGHT_MODULE to run Chromium acceptance')
    app = create_app(FleetConfig.from_root(
        tmp_path, frontend_dir=ROOT / 'frontend', platform_enabled=True,
        tasks_enabled=True,
    ))
    repo = app.extensions['fleet']['platform_repository']
    repo.upsert_model(OWNER, {'profile_id': 'fixture', 'provider': 'deterministic', 'model': '验收模型'})
    for workspace_id in ('default', 'saved'):
        repo.upsert_workspace(OWNER, {'workspace_id': workspace_id, 'name': workspace_id,
            'root_path': str(tmp_path / workspace_id)})
    repo.update_defaults(OWNER, {'model_profile_id': 'fixture', 'workspace_id': 'default'}, 0)
    repo.create_conversation(OWNER, 'conv-auth', title='登录恢复验收', workspace_id='saved')
    hub = app.wsgi_app

    def edge(environ, start_response):
        # Local test proxy mirrors the production edge's token-to-identity map.
        # No DEV_OPERATOR fallback: anonymous and bad-token requests hit real 401s.
        environ.pop('HTTP_CF_ACCESS_AUTHENTICATED_USER_EMAIL', None)
        if environ.get('HTTP_X_ACCESS_TOKEN') == 'browser-fixture-token':
            environ['HTTP_CF_ACCESS_AUTHENTICATED_USER_EMAIL'] = OWNER
        return hub(environ, start_response)

    app.wsgi_app = edge
    server = make_server('127.0.0.1', 0, app, threaded=True)
    serving = Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        result = subprocess.run(['node', str(ROOT / 'tests/fixtures/frontend/assistant_auth_browser.cjs'),
            f'http://127.0.0.1:{server.server_port}'], capture_output=True, text=True, timeout=90)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.shutdown(); server.server_close(); serving.join(5)


def test_console_in_real_browser(tmp_path):
    if not os.environ.get('FLEET_PLAYWRIGHT_MODULE'):
        pytest.skip('Set FLEET_PLAYWRIGHT_MODULE to run Chromium acceptance')
    session_db = tmp_path / 'sessions/meta.db'
    app = create_app(FleetConfig.from_root(
        tmp_path, frontend_dir=ROOT / 'frontend', dev_operator=OWNER,
        platform_enabled=True, platform_worker_enabled=True,
        service_monitoring_enabled=True, session_repositories_enabled=True,
        session_db=session_db, session_transcript_root=tmp_path / 'sessions/transcripts',
        session_encryption_raw=b'b' * 32, tasks_enabled=True,
    ))
    repo = app.extensions['fleet']['platform_repository']
    for name in ('studio-mac', 'compute-01', 'gateway-01'):
        state.save_snapshot(name, {'machine': name, 'source': 'ingest', 'ts': time.time(), 'timestamp': '2026-09-30T00:00:00Z',
            'reachable': True, 'system': {'platform': 'Linux', 'load': '0.42', 'uptime': '8 days'},
            'agents': {'codex': {'installed': True, 'active_count': 1}}})
        events.emit('state_changed', machine=name, summary='本地浏览器验收节点已同步')
        repo.upsert_node(OWNER, {'node_id': name})
        app.extensions['fleet']['services']['service_health'].register(OWNER, {
            'service_id': name + '-api', 'node_id': name, 'adapter': 'systemd',
            'target_alias': 'fixture.service', 'allowed_actions': ['inspect']})
    for _ in range(150):
        state.save_snapshot('studio-mac', {'machine': 'studio-mac', 'source': 'ingest', 'ts': time.time(), 'timestamp': '2026-09-30T00:00:00Z',
            'reachable': True, 'system': {'platform': 'Linux'}, 'agents': {}})
    SessionRepository(session_db).upsert_session({'session_id': 'browser-session',
        'machine_id': 'studio-mac', 'managed': False, 'capture_quality': 'best_effort'})
    session_service = app.extensions['fleet']['services']['sessions']
    for seq in range(1, 206):
        session_service.ingest_events([{
            'schema_version': 1, 'event_id': f'browser-evt-{seq}',
            'stream_id': 'browser-stream', 'machine_id': 'studio-mac',
            'session_id': 'browser-session', 'sequence': seq,
            'kind': 'assistant_message', 'capture_quality': 'best_effort',
            'source': 'fixture', 'emitted_at': '2026-09-30T00:00:00Z',
            'payload': {'text': ('LONG_' + '中' * 5000 + '_END') if seq == 1 else f'History row {seq}',
                        'is_complete': True},
        }])
    task, _ = task_store.create_task(machine='studio-mac', agent_type='codex', project='demo',
        instruction='浏览器验收任务', requested_by=OWNER, client_token='browser-task')
    repo.upsert_model(OWNER, {'profile_id': 'fixture', 'provider': 'deterministic', 'model': '本地验收模型'})
    repo.upsert_workspace(OWNER, {'workspace_id': 'workspace', 'name': '我的工作区', 'root_path': str(tmp_path / 'workspace')})
    repo.update_defaults(OWNER, {'model_profile_id': 'fixture', 'workspace_id': 'workspace'}, 0)
    class Provider:
        def complete(self, messages, tools):
            if messages[-1].get('content') == 'diagnostic-failure':
                from tools.platform.providers.openai_compatible import ProviderError
                raise ProviderError('transient_http', status=503, retryable=True)
            return ModelResponse(kind='final', text='## 浏览器验收结果\n\n通过真实 API 持久化。\n\n- 支持刷新恢复\n- 运行与产物分离\n\n```python\nprint("hello")\n```\n<img src=x onerror=alert(1)>')
    worker = app.extensions['fleet']['services']['platform_worker']
    worker.provider_factory = lambda _: Provider()
    stopping = Event()
    def execute_runs():
        while not stopping.is_set():
            worker.run_once(OWNER)
            stopping.wait(.1)
    runner = Thread(target=execute_runs, daemon=True)
    server = make_server('127.0.0.1', 0, app, threaded=True)
    serving = Thread(target=server.serve_forever, daemon=True)
    runner.start(); serving.start()
    try:
        result = subprocess.run(['node', str(ROOT / 'tests/fixtures/frontend/console_browser.cjs'),
            f'http://127.0.0.1:{server.server_port}', task['task_id']],
            capture_output=True, text=True, timeout=90)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        stopping.set(); runner.join(5)
        server.shutdown(); server.server_close(); serving.join(5)
