"""Release CLI is tested against account cookies and the real Hub/provider stack."""
import json

import pytest

from tools.platform.acceptance_check import main
from support.full_flow import FullFlowHub, PASSWORD
from support.strict_provider import StrictProvider


@pytest.fixture
def release_target(tmp_path, monkeypatch):
    monkeypatch.setenv('FLEET_FULL_FLOW_SECRET', 'full-flow-provider-secret')
    with StrictProvider() as provider:
        with FullFlowHub(tmp_path, provider) as hub:
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
    _, _, args = release_target
    assert main(args + ['--model', 'full-flow']) == 1
    assert json.loads(capsys.readouterr().out)['models'][0]['error'] == 'read_only_tool_verification_failed'


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
    assert json.loads(capsys.readouterr().out)['error'] == 'selected_model_unavailable'
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
    assert len(hub.provider.requests) == 8
    assert report['checks']['logout'] == 'passed'


def test_release_cli_timeout_preserves_queued_run_without_resubmission(release_target, capsys):
    hub, _, args = release_target
    port = hub.server.server_port
    hub.stop()
    hub.start(scheduler=False, port=port)
    args[args.index('--timeout') + 1] = '1'
    assert main(args + ['--model', 'read-only']) == 1
    report = json.loads(capsys.readouterr().out)
    row = report['models'][0]
    assert row['error'] == 'run_not_terminal'
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
    from flask import request, redirect
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
