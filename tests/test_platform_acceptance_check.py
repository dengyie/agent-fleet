"""Release CLI is tested against account cookies and the real Hub/provider stack."""
import json

import pytest
from support.full_flow import PASSWORD, FullFlowHub
from support.strict_provider import StrictProvider

from tools.platform.acceptance_check import main


@pytest.mark.parametrize('terminal_only', [False, True])
@pytest.mark.parametrize('chunk_size', [7, 8192])
def test_live_observer_requires_partial_text_in_a_received_nonterminal_snapshot(monkeypatch, terminal_only, chunk_size):
    import time

    import tools.platform.acceptance_check as acceptance

    states = [{
        'run_id': 'run-live', 'state': 'running',
        'progress': {'activity': {'phase': 'responding'}, 'segments': [
            {'kind': 'text', 'text': '正在检查目录', 'complete': False},
            {'kind': 'tool', 'title': '列出目录 · .'},
        ]},
    }, {'run_id': 'run-live', 'state': 'succeeded', 'progress': {'segments': [
        {'kind': 'text', 'text': '目录检查完成', 'complete': True},
    ]}}]
    if terminal_only:
        states = states[1:]
    payload = ''.join('id: ' + str(i) + '\nevent: run\ndata: '
                      + json.dumps(row, ensure_ascii=False) + '\n\n'
                      for i, row in enumerate(states, 1)).encode('utf-8')
    calls = []

    class Response:
        status = 200
        closed = False

        def __init__(self):
            self.headers = {'Content-Type': 'text/event-stream; charset=utf-8'}
            # Split JSON, frame separators and multibyte characters.
            self.chunks = iter(payload[i:i + chunk_size] for i in range(0, len(payload), chunk_size))

        def read1(self, size):
            return next(self.chunks, b'')

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True

    response = Response()

    def open_stream(request, **kwargs):
        calls.append(request)
        return response

    monkeypatch.setattr(acceptance, 'open_http', open_stream)
    client = acceptance.AccountClient('https://example.test')
    client.cookie = 'session=private'
    observed = client.observe_live_run('run-live', deadline=time.monotonic() + 5)

    live = not terminal_only and chunk_size == 7
    assert observed == {
        'streamed_progress_before_terminal': live,
        'activity_phase_seen': live,
        'explained_tool_call_seen': live,
    }
    assert len(calls) == 1
    assert calls[0].get_method() == 'GET'
    assert calls[0].full_url.endswith('/runs/run-live/stream?after=0')
    assert calls[0].get_header('Cookie') == 'session=private'
    assert response.closed


def test_live_observer_reconnects_real_http_stream_with_cursor_and_cookie():
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    from tools.platform.acceptance_check import AccountClient

    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append((self.path, self.headers.get('Cookie')))
            first = len(requests) == 1
            run = {'run_id': 'run-live', 'state': 'running' if first else 'succeeded',
                   'progress': {'activity': {'phase': 'responding'}, 'segments': [
                       {'kind': 'text', 'text': '正在检查目录', 'complete': not first},
                       {'kind': 'tool', 'title': '列出目录 · .'},
                   ]}}
            body = (': heartbeat\r\n\r\nid: ' + ('7' if first else '8')
                    + '\r\nevent: run\r\ndata: ' + json.dumps(run, ensure_ascii=False) + '\r\n\r\n').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            self.close_connection = True

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = AccountClient('http://127.0.0.1:' + str(server.server_port))
        client.cookie = 'session=private-test-cookie'
        observed = client.observe_live_run('run-live', deadline=time.monotonic() + 5)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    assert observed == {'streamed_progress_before_terminal': True,
                        'activity_phase_seen': True, 'explained_tool_call_seen': True}
    assert requests == [('/api/platform/v1/runs/run-live/stream?after=0', client.cookie),
                        ('/api/platform/v1/runs/run-live/stream?after=7', client.cookie)]
    assert not thread.is_alive()


@pytest.mark.parametrize('payload,error', [
    (b'event: run\ndata: {bad json}\n\n', 'invalid_stream_response'),
    (b'event: run\ndata: {"run_id":"another","state":"running"}\n\n', 'invalid_stream_response'),
    (b'event: run\ndata: {"run_id":"run-live","state":"invented"}\n\n', 'invalid_stream_response'),
    (b'event: run\ndata: {"run_id":"run-live","state":"running","progress":[]}\n\n',
     'invalid_stream_response'),
    (b'event: run\ndata: {"run_id":"run-live","state":"running","progress":{"segments":{}}}\n\n',
     'invalid_stream_response'),
    (b'id: invalid\nevent: run\ndata: {}\n\n', 'invalid_stream_response'),
    (b'event: run\ndata: {', 'invalid_stream_response'),
    (b'event: run\ndata: ' + b'x' * (2 * 1024 * 1024 + 1), 'response_too_large'),
])
def test_live_observer_rejects_invalid_stream_and_closes_without_replay(monkeypatch, payload, error):
    import io
    import time

    import tools.platform.acceptance_check as acceptance

    class Response(io.BytesIO):
        status = 200
        headers = {'Content-Type': 'text/event-stream'}

    response = Response(payload)
    requests = []

    def open_stream(request, **kwargs):
        requests.append(request)
        return response

    monkeypatch.setattr(acceptance, 'open_http', open_stream)
    with pytest.raises(acceptance.AcceptanceFailure) as caught:
        acceptance.AccountClient('https://example.test').observe_live_run(
            'run-live', deadline=time.monotonic() + 5)

    assert caught.value.code == error
    assert response.closed
    assert len(requests) == 1 and requests[0].get_method() == 'GET'


def test_live_observer_deadline_interrupts_real_silent_stream_without_replay():
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Event, Thread

    from tools.platform.acceptance_check import AcceptanceFailure, AccountClient

    requests = []
    stop = Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            self.wfile.flush()
            stop.wait(5)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        client = AccountClient('http://127.0.0.1:' + str(server.server_port))
        with pytest.raises(AcceptanceFailure) as caught:
            client.observe_live_run('run-live', deadline=started + 0.3)
        assert caught.value.code == 'acceptance_timeout'
        assert time.monotonic() - started < 3
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    assert requests == ['/api/platform/v1/runs/run-live/stream?after=0']


@pytest.fixture
def release_target(tmp_path, monkeypatch):
    monkeypatch.setenv('FLEET_FULL_FLOW_SECRET', 'full-flow-provider-secret')
    with StrictProvider() as provider, FullFlowHub(tmp_path, provider) as hub:
        credentials = tmp_path / 'credentials.json'
        credentials.write_text(json.dumps({'username': 'mango', 'password': PASSWORD}))
        credentials.chmod(0o600)
        args = ['--endpoint', hub.origin, '--credentials-file', str(credentials), '--workspace', 'home', '--timeout', '5']
        yield hub, credentials, args


def test_release_cli_requires_real_tool_final_reply_and_revocation(release_target, capsys):
    hub, _, args = release_target
    assert main(args + ['--model', 'read-only']) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report['status'] == 'passed'
    assert report['models'][0]['recovered']
    assert report['checks']['logout'] == 'passed'
    assert len(hub.provider.requests) == 2
    assert all([t['function']['name'] for t in r['tools']] == ['workspace_list'] for r in hub.provider.requests)
    assert PASSWORD not in output and 'full-flow-provider-secret' not in output
    assert 'Full flow completed' not in output


def test_release_cli_http_failure_does_not_retry_or_report_success(release_target, capsys):
    hub, _, args = release_target
    assert main(args + ['--model', 'broken']) == 1
    report = json.loads(capsys.readouterr().out)
    assert report['models'][0]['run_state'] == 'unknown'
    assert report['models'][0]['error'] == 'run_unknown'
    assert len(hub.provider.requests) == 1
    assert report['checks']['logout'] == 'passed'


def test_release_cli_rejects_unexpected_writing_tools(release_target, capsys):
    hub, _, args = release_target
    target = hub.workspace / 'flow.txt'
    target.write_bytes(b'preserved')
    assert main(args + ['--model', 'full-flow']) == 1
    assert json.loads(capsys.readouterr().out)['models'][0]['error'] == 'run_unknown'
    assert target.read_bytes() == b'preserved'


@pytest.mark.parametrize('tool,arguments', [
    ('workspace.write', {'path': 'flow.txt', 'content': 'overwritten'}),
    ('workspace.artifact', {'path': 'flow.txt'}),
    ('workspace.exec', {'argv': ['touch', 'executed.txt']}),
    ('workspace.read', {'path': 'flow.txt'}),
])
def test_acceptance_denies_tools_before_side_effects(release_target, capsys, monkeypatch, tool, arguments):
    from support.full_flow import Client

    from fleet_core.backends.sandbox import SandboxBackend
    from hub.platform.providers.base import ModelResponse

    hub, _, args = release_target
    original = b'original workspace bytes\x00\xff'
    target = hub.workspace / 'flow.txt'
    target.write_bytes(original)
    executed = []
    declarations = []

    def execute(*args, **kwargs):
        executed.append(True)
        raise AssertionError('execution backend must never be reached')

    monkeypatch.setattr(SandboxBackend, 'execute', execute)

    class Provider:
        def complete(self, messages, tools, *, request_observer=None):
            declarations.append([item['name'] for item in tools])
            return ModelResponse(kind='tool_call', tool=tool, arguments=arguments)

    hub.app.extensions['fleet']['services']['platform_worker'].provider_factory = lambda profile: Provider()
    assert main(args + ['--model', 'read-only']) == 1
    row = json.loads(capsys.readouterr().out)['models'][0]
    client = Client(hub.origin)
    client.login()
    assert target.read_bytes() == original
    assert client.request('/api/platform/v1/workspaces/home/artifacts')['artifacts'] == []
    assert executed == []
    events = client.request('/api/platform/v1/runs/' + row['run_id'] + '/events')['events']
    receipt = next(e['payload'] for e in events if e['kind'] == 'tool_result')
    assert receipt['state'] == 'failed' and receipt['error_code'] == 'tool_not_allowed'
    assert declarations == [['workspace.list']]


def test_release_cli_invalid_credentials_cannot_create_runs(release_target, capsys):
    hub, credentials, args = release_target
    credentials.write_text(json.dumps({'username': 'mango', 'password': 'wrong-password'}))
    assert main(args + ['--model', 'read-only']) == 1
    assert json.loads(capsys.readouterr().out)['http_status'] == 401
    assert hub.provider.requests == []


def test_release_cli_private_credentials_and_catalog_are_required(release_target, capsys):
    hub, credentials, args = release_target
    credentials.chmod(0o644)
    assert main(args + ['--model', 'read-only']) == 1
    assert json.loads(capsys.readouterr().out)['error'] == 'credentials_file_not_private'
    credentials.chmod(0o600)
    assert main(args + ['--model', 'missing']) == 1
    report = json.loads(capsys.readouterr().out)
    assert report['models'] == [{
        'model_profile_id': 'missing', 'status': 'failed', 'error': 'selected_model_unavailable',
    }]
    assert hub.provider.requests == []


def test_release_cli_all_models_reports_each_failure_and_keeps_testing(release_target, capsys, tmp_path):
    hub, _, args = release_target
    evidence = tmp_path / 'evidence' / 'models.json'
    assert main(args + ['--all-models', '--report', str(evidence)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert json.loads(evidence.read_text()) == report
    assert [(r['model_profile_id'], r['status']) for r in report['models']] == [
        ('broken', 'failed'), ('full-flow', 'failed'), ('read-only', 'passed')]
    assert len({r['run_id'] for r in report['models']}) == 3
    assert len(hub.provider.requests) == 5
    assert report['checks']['logout'] == 'passed'


def test_unavailable_selected_profile_does_not_skip_later_fallback(release_target, capsys):
    hub, _, args = release_target

    assert main(args + ['--model', 'missing-primary', '--model', 'read-only']) == 1

    report = json.loads(capsys.readouterr().out)
    assert [(row['model_profile_id'], row['status'], row.get('error'))
            for row in report['models']] == [
                ('missing-primary', 'failed', 'selected_model_unavailable'),
                ('read-only', 'passed', None),
            ]
    assert len(hub.provider.requests) == 2


def test_failed_primary_profile_still_runs_later_fallback(release_target, capsys):
    hub, _, args = release_target

    assert main(args + ['--model', 'broken', '--model', 'read-only']) == 1

    report = json.loads(capsys.readouterr().out)
    assert [(row['model_profile_id'], row['status']) for row in report['models']] == [
        ('broken', 'failed'), ('read-only', 'passed'),
    ]
    assert hub.provider.requests[-1]['model'] == 'read-only'
    assert sum(request['model'] == 'read-only' for request in hub.provider.requests) == 2


def test_live_acceptance_workflow_defaults_to_only_requested_profiles():
    from pathlib import Path

    workflow = Path('.github/workflows/live-acceptance.yml').read_text()
    assert 'default: deepseek-v4-flash,glm-5.3-flash,deepseek-v4-pro,gpt-6-luna' in workflow
    assert '--require-streaming' in workflow
    assert '--all-models' not in workflow


def test_live_acceptance_requires_progress_and_explained_tool_receipt(monkeypatch):
    import tools.platform.acceptance_check as acceptance
    from tools.platform.acceptance_check import check_model

    monkeypatch.setattr(acceptance.time, 'sleep', lambda _: None)

    class Client:
        def __init__(self):
            self.run_reads = 0

        def request(self, path, body=None, *, expected=200, deadline=None):
            if path == '/api/platform/v1/conversations':
                return {'conversation': {'conversation_id': 'conversation-1'}}
            if path.endswith('/acceptance-turns'):
                return {'run': {'run_id': 'run-1', 'state': 'queued'}}
            if path == '/api/platform/v1/runs/run-1':
                self.run_reads += 1
                state = 'running' if self.run_reads <= 2 else 'succeeded'
                return {'state': state, 'result_text': '工作区检查完成'}
            if path.startswith('/api/platform/v1/runs/run-1/events'):
                if self.run_reads > 2:
                    return {'events': [
                        {'sequence': 6, 'kind': 'run_finished', 'payload': {
                            'run_id': 'run-1', 'state': 'succeeded'}},
                    ]}
                return {'events': [
                    {'sequence': 1, 'kind': 'provider_request_started', 'payload': {}},
                    {'sequence': 2, 'kind': 'assistant_activity', 'payload': {'phase': 'thinking'}},
                    {'sequence': 3, 'kind': 'assistant_text_delta', 'payload': {'text': '正在检查'}},
                    {'sequence': 4, 'kind': 'tool_call', 'payload': {
                        'tool': 'workspace.list', 'title': '列出目录 · .'}},
                    {'sequence': 5, 'kind': 'tool_result', 'payload': {
                        'tool': 'workspace.list', 'state': 'succeeded'}},
                ]}
            if path == '/api/platform/v1/conversations/conversation-1':
                return {'conversation': {'runs': [{
                    'run_id': 'run-1', 'state': 'succeeded', 'result_text': '工作区检查完成',
                }]}}
            raise AssertionError(f'unexpected path: {path}')

    row = check_model(Client(), 'read-only', 'acceptance', 5, require_streaming=True)

    assert row['status'] == 'passed'
    assert row['streamed_progress_before_terminal'] is True
    assert row['activity_phase_seen'] is True
    assert row['explained_tool_call_seen'] is True
    assert row['read_only_tool_succeeded'] is True


def test_live_acceptance_fails_when_text_only_arrives_after_terminal(monkeypatch):
    import tools.platform.acceptance_check as acceptance
    from tools.platform.acceptance_check import check_model

    monkeypatch.setattr(acceptance.time, 'sleep', lambda _: None)

    class Client:
        def request(self, path, body=None, *, expected=200, deadline=None):
            if path == '/api/platform/v1/conversations':
                return {'conversation': {'conversation_id': 'conversation-1'}}
            if path.endswith('/acceptance-turns'):
                return {'run': {'run_id': 'run-1', 'state': 'queued'}}
            if path == '/api/platform/v1/runs/run-1':
                return {'state': 'succeeded', 'result_text': '完成'}
            if path.startswith('/api/platform/v1/runs/run-1/events'):
                return {'events': [
                    {'sequence': 1, 'kind': 'assistant_text_delta', 'payload': {'text': '完成'}},
                    {'sequence': 2, 'kind': 'tool_call', 'payload': {
                        'tool': 'workspace.list', 'title': '列出目录 · .'}},
                    {'sequence': 3, 'kind': 'tool_result', 'payload': {
                        'tool': 'workspace.list', 'state': 'succeeded'}},
                    {'sequence': 4, 'kind': 'run_finished', 'payload': {
                        'run_id': 'run-1', 'state': 'succeeded'}},
                ]}
            if path == '/api/platform/v1/conversations/conversation-1':
                return {'conversation': {'runs': [{
                    'run_id': 'run-1', 'state': 'succeeded', 'result_text': '完成',
                }]}}
            raise AssertionError(f'unexpected path: {path}')

    row = check_model(Client(), 'read-only', 'acceptance', 5, require_streaming=True)

    assert row['status'] == 'failed'
    assert row['error'] == 'live_progress_missing'


@pytest.mark.parametrize('terminal_kind', ['run_finished', 'run_failed', 'run_unknown'])
@pytest.mark.parametrize('terminal_status_read', [2, 3])
def test_live_acceptance_does_not_count_terminal_event_page_as_live(monkeypatch, terminal_kind, terminal_status_read):
    import tools.platform.acceptance_check as acceptance
    from tools.platform.acceptance_check import check_model

    monkeypatch.setattr(acceptance.time, 'sleep', lambda _: None)

    class Client:
        def __init__(self):
            self.run_reads = 0

        def request(self, path, body=None, *, expected=200, deadline=None):
            if path == '/api/platform/v1/conversations':
                return {'conversation': {'conversation_id': 'conversation-1'}}
            if path.endswith('/acceptance-turns'):
                return {'run': {'run_id': 'run-1', 'state': 'queued'}}
            if path == '/api/platform/v1/runs/run-1':
                self.run_reads += 1
                return {'state': 'running' if self.run_reads < terminal_status_read else 'succeeded',
                        'result_text': '完成'}
            if path.startswith('/api/platform/v1/runs/run-1/events'):
                return {'events': [
                    {'sequence': 1, 'kind': 'assistant_text_delta', 'payload': {'text': '完成'}},
                    {'sequence': 2, 'kind': 'tool_call', 'payload': {
                        'tool': 'workspace.list', 'title': '列出目录 · .'}},
                    {'sequence': 3, 'kind': 'tool_result', 'payload': {
                        'tool': 'workspace.list', 'state': 'succeeded'}},
                    {'sequence': 4, 'kind': terminal_kind, 'payload': {
                        'run_id': 'run-1', 'state': 'succeeded'}},
                ]}
            if path == '/api/platform/v1/conversations/conversation-1':
                return {'conversation': {'runs': [{
                    'run_id': 'run-1', 'state': 'succeeded', 'result_text': '完成',
                }]}}
            raise AssertionError(f'unexpected path: {path}')

    row = check_model(Client(), 'read-only', 'acceptance', 5, require_streaming=True)

    assert row['status'] == 'failed'
    assert row['error'] == 'live_progress_missing'
    assert row['streamed_progress_before_terminal'] is False


def test_release_cli_timeout_preserves_queued_run_without_resubmission(release_target, capsys):
    hub, _, args = release_target
    port = hub.server.server_port
    hub.stop()
    hub.start(scheduler=False, port=port)
    args[args.index('--timeout') + 1] = '1'
    assert main(args + ['--model', 'read-only']) == 1
    report = json.loads(capsys.readouterr().out)
    row = report['models'][0]
    assert row['error'] == 'acceptance_timeout'
    assert row['outcome'] == 'unconfirmed'
    assert row['run_state'] == 'queued'
    from support.full_flow import Client
    client = Client(hub.origin)
    client.login()
    recovered = client.request('/api/platform/v1/conversations/' + row['conversation_id'])['conversation']
    assert [(r['run_id'], r['state']) for r in recovered['runs']] == [(row['run_id'], 'queued')]
    assert hub.provider.requests == []
    assert report['checks']['logout'] == 'passed'


@pytest.mark.parametrize('expected,exit_code', [('current', 0), ('stale', 1)])
def test_release_cli_manifest_is_checked_before_login(release_target, capsys, expected, exit_code):
    from flask import request
    hub, _, args = release_target
    logins = []

    @hub.app.before_request
    def manifest():
        if request.path == '/manifest.json':
            return {'version': 'current'}
        if request.path == '/api/accounts/login':
            logins.append(True)

    assert main(args + ['--model', 'read-only', '--expected-release', expected]) == exit_code
    report = json.loads(capsys.readouterr().out)
    if exit_code:
        assert report['error'] == 'release_version_mismatch'
        assert not logins and not hub.provider.requests
    else:
        assert report['release'] == 'current' and len(logins) == 1


@pytest.mark.parametrize('status', [302, 307, 308])
def test_release_cli_does_not_forward_credentials_on_redirect(release_target, capsys, status):
    from flask import redirect, request
    hub, _, args = release_target
    forwarded = []

    @hub.app.before_request
    def redirect_login():
        if request.path == '/api/accounts/login':
            return redirect(hub.origin + '/credential-capture', code=status)
        if request.path == '/credential-capture':
            forwarded.append(True)
            return {'ok': True}

    assert main(args + ['--model', 'read-only']) == 1
    report = json.loads(capsys.readouterr().out)
    assert report['http_status'] == status
    assert not forwarded and not hub.provider.requests


def test_release_cli_not_ready_cannot_create_runs(release_target, capsys):
    from flask import request
    hub, _, args = release_target

    @hub.app.before_request
    def not_ready():
        if request.path == '/api/platform/v1/readiness':
            return {'configuration_ready': False}

    assert main(args + ['--model', 'read-only']) == 1
    report = json.loads(capsys.readouterr().out)
    assert report['error'] == 'configuration_not_ready'
    assert report['checks']['logout'] == 'passed'
    assert not hub.provider.requests


@pytest.mark.parametrize('payload', [[], None, 'unexpected'])
def test_release_cli_malformed_credentials_produce_failed_evidence(release_target, capsys, payload):
    hub, credentials, args = release_target
    credentials.write_text(json.dumps(payload))
    assert main(args + ['--model', 'read-only']) == 1
    report = json.loads(capsys.readouterr().out)
    assert report['error'] == 'invalid_credentials_file'
    assert not hub.provider.requests


@pytest.mark.parametrize('endpoint', ['http://public.example', 'https://user:password@example.test', 'https://example.test/path', 'https://example.test/?token=x'])
def test_release_cli_rejects_unsafe_endpoint_before_login(endpoint):
    with pytest.raises(SystemExit) as error:
        main(['--endpoint', endpoint, '--credentials-file', '/missing', '--workspace', 'home', '--all-models'])
    assert error.value.code == 2
