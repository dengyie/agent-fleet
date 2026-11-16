"""Release CLI is tested against account cookies and the real Hub/provider stack."""
import json

import pytest
from support.full_flow import PASSWORD, FullFlowHub
from support.strict_provider import StrictProvider

from tools.platform.acceptance_check import main


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
                state = 'running' if self.run_reads == 1 else 'succeeded'
                return {'state': state, 'result_text': '工作区检查完成'}
            if path.startswith('/api/platform/v1/runs/run-1/events'):
                return {'events': [
                    {'sequence': 1, 'kind': 'provider_request_started', 'payload': {}},
                    {'sequence': 2, 'kind': 'assistant_activity', 'payload': {'phase': 'thinking'}},
                    {'sequence': 3, 'kind': 'assistant_text_delta', 'payload': {'text': '正在检查'}},
                    {'sequence': 4, 'kind': 'tool_call', 'payload': {
                        'tool': 'workspace.list', 'title': '列出目录 · .'}},
                    {'sequence': 5, 'kind': 'tool_result', 'payload': {
                        'tool': 'workspace.list', 'state': 'succeeded'}},
                    {'sequence': 6, 'kind': 'run_finished', 'payload': {
                        'run_id': 'run-1', 'state': 'succeeded'}},
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


def test_live_acceptance_does_not_count_events_fetched_after_terminal_state(monkeypatch):
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
