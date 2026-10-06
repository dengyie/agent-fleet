"""Failure evidence must survive the real provider -> worker -> API chain."""
import json
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig
from tools.platform.providers.openai_compatible import OpenAICompatibleProvider, TransportResponse, ProviderError

OWNER = 'diagnostics@example.test'
SECRET = 'never-log-provider-secret'


def setup_run(tmp_path, provider):
    app = create_app(FleetConfig.from_root(tmp_path, dev_operator=OWNER, platform_enabled=True, platform_worker_enabled=True))
    repo = app.extensions['fleet']['platform_repository']
    repo.upsert_model(OWNER, {'profile_id': 'test-model', 'provider': 'deterministic', 'model': 'test-model'})
    repo.upsert_workspace(OWNER, {'workspace_id': 'home', 'root_path': str(tmp_path / 'workspace')})
    repo.update_defaults(OWNER, {'model_profile_id': 'test-model', 'workspace_id': 'home'}, 0)
    client = app.test_client()
    conversation = client.post('/api/platform/v1/conversations', json={}).get_json()['conversation']
    run = client.post('/api/platform/v1/conversations/' + conversation['conversation_id'] + '/turns', json={'text': SECRET, 'client_token': 'test'}).get_json()['run']
    worker = app.extensions['fleet']['services']['platform_worker']
    worker.provider_factory = lambda _: provider
    return app, client, worker, run


@pytest.fixture
def fault_server(status):
    state = {'calls': 0}
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            state['calls'] += 1
            body = json.dumps({'error': {'code': 'do_request_failed' if status == 500 else 'system_cpu_overloaded', 'message': SECRET}}).encode()
            self.send_response(status); self.send_header('Content-Length', str(len(body))); self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    state['url'] = f'http://127.0.0.1:{server.server_port}/v1/chat/completions'
    try: yield state
    finally: server.shutdown(); server.server_close(); thread.join(2)


@pytest.mark.parametrize('status,code', [(403,'auth_error'), (429,'rate_limit'), (500,'transient_http'), (503,'transient_http')])
def test_provider_error_is_correlated_durable_and_visible_without_raw_body(tmp_path, caplog, status, code, fault_server):
    provider = OpenAICompatibleProvider(model='test', endpoint=fault_server['url'],
        api_key=SECRET, allow_network=True)
    app, client, worker, run = setup_run(tmp_path, provider)
    result = worker.run_once(OWNER)
    events = client.get('/api/platform/v1/runs/' + run['run_id'] + '/events').get_json()['events']
    terminal = [event for event in events if event['kind'] == 'run_unknown']
    assert len(terminal) == 1
    failure = terminal[0]['payload']
    assert failure['provider_error'] == code
    assert failure['provider_status'] == status
    assert failure['upstream_code'] == ('do_request_failed' if status == 500 else 'system_cpu_overloaded')
    assert failure['step'] == 1
    assert failure['attempt'] == 1
    assert code in result['result_text'] and str(status) in result['result_text']
    assert run['run_id'] in caplog.text and code in caplog.text
    assert SECRET not in caplog.text + json.dumps(events) + result['result_text']
    assert fault_server['calls'] == 1
    assert worker.run_once(OWNER) is None  # uncertain runs are never blindly replayed


def test_unexpected_provider_exception_retains_location_not_secret(tmp_path, caplog):
    class Broken:
        def complete(self, *_):
            raise ValueError(SECRET)
    _, client, worker, run = setup_run(tmp_path, Broken())
    result = worker.run_once(OWNER)
    assert result['state'] == 'unknown'
    assert run['run_id'] in caplog.text
    assert 'ValueError' in caplog.text
    assert 'test_platform_diagnostics_regressions.py' in caplog.text
    assert SECRET not in caplog.text


def test_http_error_has_request_id_in_response_header_and_log(tmp_path, caplog):
    app = create_app(FleetConfig.from_root(tmp_path))
    response = app.test_client().get('/api/platform/v1/models?token=' + SECRET)
    assert response.status_code == 404
    rid = response.get_json()['request_id']
    assert response.headers['X-Request-ID'] == rid
    assert rid in caplog.text
    assert SECRET not in caplog.text


def test_scheduler_failure_is_logged_and_loop_recovers(tmp_path, caplog):
    import threading
    from hub.application.run_scheduler_service import RunSchedulerService
    stop = threading.Event()
    recovered = threading.Event()
    scheduler = RunSchedulerService(None, None, interval_s=.1)
    calls = []
    def tick():
        calls.append(1)
        if len(calls) == 1:
            raise OSError(SECRET)
        recovered.set(); stop.set()
    scheduler.run_once = tick
    shutdown = scheduler.start(stop_event=stop)
    try:
        assert recovered.wait(3)
    finally:
        shutdown()
    assert 'platform_scheduler_tick_failed' in caplog.text
    assert 'OSError' in caplog.text and SECRET not in caplog.text


def test_unhandled_http_exception_logs_location_and_request_id_without_message(tmp_path, caplog):
    app = create_app(FleetConfig.from_root(tmp_path))
    @app.get('/api/diagnostic-broken')
    def broken_http_handler():
        raise RuntimeError(SECRET)
    response = app.test_client().get('/api/diagnostic-broken')
    assert response.status_code == 500
    assert response.get_json()['request_id'] in caplog.text
    assert 'broken_http_handler' in caplog.text
    assert SECRET not in caplog.text + response.text


@pytest.mark.parametrize('provider_fails', [True, False])
def test_accounting_failure_preserves_original_boundary(provider_fails, caplog):
    from types import SimpleNamespace
    from tools.platform.runtime.native import NativeAssistantRuntime, ProviderBoundaryUnknown
    class Provider:
        def complete(self, *_):
            if provider_fails:
                raise ProviderError('transient_http', retryable=True, status=503)
            return SimpleNamespace(usage={})
    class Meter:
        def admit(self, *args, **kwargs): return 'reservation'
        def settle(self, *args, **kwargs): raise OSError(SECRET)
    runtime = NativeAssistantRuntime(Provider(), None, usage_meter=Meter())
    with pytest.raises(ProviderBoundaryUnknown) as caught:
        runtime.run(run_id='run-test', owner_id=OWNER, epoch=1, messages=[], tools=[])
    diagnostic = caught.value.diagnostic
    assert diagnostic['provider_error'] == ('transient_http' if provider_fails else 'usage_accounting_unknown')
    assert isinstance(caught.value.__cause__, ProviderError if provider_fails else OSError)
    assert 'platform_usage_settlement_failed' in caplog.text
    assert SECRET not in caplog.text


def test_heartbeat_exception_marks_lease_lost_and_releases_slot(caplog):
    import threading
    from types import SimpleNamespace
    from hub.application.run_scheduler_service import RunSchedulerService
    failed = threading.Event()
    released = []
    class Repository:
        calls = 0
        def renew_worker_slot(self, *args, **kwargs):
            self.calls += 1
            if self.calls > 1:
                failed.set()
                raise OSError(SECRET)
            return True
        def release_worker_slot(self, *args, **kwargs): released.append(True)
    def execute(claim):
        assert failed.wait(3)
        # Wait for the heartbeat handler to finish, not just its failing I/O.
        for thread in threading.enumerate():
            if thread.name == 'platform-run-heartbeat': thread.join(2)
        return {'state': 'succeeded'}
    worker = SimpleNamespace(lease_s=1.5, worker_id='worker', execute_claim=execute)
    scheduler = RunSchedulerService(Repository(), worker)
    result = scheduler._execute({'owner_id':OWNER,'run_id':'run-test','lease_id':'run-lease','attempt':1}, {}, {'lease_id':'slot'})
    assert result['state'] == 'lease_lost'
    assert released == [True]
    assert 'platform_heartbeat_failed' in caplog.text and SECRET not in caplog.text

@pytest.mark.parametrize('error_type', [ValueError, RuntimeError])
def test_worker_setup_failure_has_correlated_diagnostics(tmp_path, caplog, error_type):
    _, _, worker, run = setup_run(tmp_path, None)
    def broken_factory(_): raise error_type('fixture_failure')
    worker.provider_factory = broken_factory
    worker.run_once(OWNER)
    assert 'platform_run_failure' in caplog.text
    assert run['run_id'] in caplog.text and 'broken_factory' in caplog.text
