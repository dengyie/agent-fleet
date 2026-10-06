"""Acceptance routes through real accounts, provider HTTP, node HTTP and restart."""
from contextlib import nullcontext
import json
from threading import Event

from flask import request
import pytest

from support.full_flow import Client, FullFlowHub, PASSWORD
from support.loopback_node import LoopbackNode
from support.strict_provider import StrictProvider
from tools.platform.acceptance_check import AccountClient, check_model, main


@pytest.fixture(params=[False, True], ids=['local', 'remote'])
def chain_target(tmp_path, monkeypatch, request):
    monkeypatch.setenv('FLEET_FULL_FLOW_SECRET', 'full-flow-provider-secret')
    with StrictProvider() as provider:
        with FullFlowHub(tmp_path, provider, remote=request.param) as hub:
            with LoopbackNode(hub) if request.param else nullcontext() as node:
                credentials = tmp_path / 'credentials.json'
                credentials.write_text(json.dumps({'username': 'mango', 'password': PASSWORD}))
                credentials.chmod(0o600)
                args = ['--endpoint', hub.origin, '--credentials-file', str(credentials),
                        '--workspace', 'remote' if node else 'home',
                        '--model', 'read-only', '--timeout', '10']
                yield hub, node, args


@pytest.fixture
def idle_hub(tmp_path, monkeypatch):
    monkeypatch.setenv('FLEET_FULL_FLOW_SECRET', 'full-flow-provider-secret')
    with StrictProvider() as provider:
        hub = FullFlowHub(tmp_path, provider).start(scheduler=False)
        try:
            yield hub
        finally:
            hub.stop()


@pytest.fixture
def paused_target(idle_hub):
    client = Client(idle_hub.origin)
    client.login()
    conversation = client.request('/api/platform/v1/conversations', {
        'title': 'Acceptance chain', 'workspace_id': 'home',
    })['conversation']['conversation_id']
    return idle_hub, client, '/api/platform/v1/conversations/' + conversation


def acceptance_body():
    return {'text': 'inspect workspace', 'client_token': 'acceptance-chain',
            'overrides': {'model_profile_id': 'read-only'}}


def assert_only_list_advertised(provider):
    assert provider.requests
    assert not provider.errors
    assert all([t['function']['name'] for t in p['tools']] == ['workspace_list']
               for p in provider.requests)


def test_acceptance_http_chain_closes_report_and_node_journal(chain_target, capsys):
    hub, node, args = chain_target
    workspace = node.workspace if node else hub.workspace
    original = b'preserved\x00\xff'
    (workspace / 'sentinel.txt').write_bytes(original)
    assert main(args) == 0
    report = json.loads(capsys.readouterr().out)
    row = report['models'][0]
    assert report['status'] == 'passed' and report['checks']['logout'] == 'passed'
    assert row['recovered'] and row['read_only_tool_succeeded'] and row['reply_present']
    assert_only_list_advertised(hub.provider)
    assert len(hub.provider.requests) == 2
    assert 'sentinel.txt' in hub.provider.requests[1]['messages'][-1]['content']
    assert [p.name for p in workspace.iterdir()] == ['sentinel.txt']
    assert (workspace / 'sentinel.txt').read_bytes() == original
    client = Client(hub.origin)
    client.login()
    workspace_id = 'remote' if node else 'home'
    assert client.request(f'/api/platform/v1/workspaces/{workspace_id}/artifacts')['artifacts'] == []
    events = client.request('/api/platform/v1/runs/' + row['run_id'] + '/events')['events']
    middle = ['node_command_queued', 'node_receipt'] if node else []
    assert [e['kind'] for e in events] == [
        'run_started', 'provider_request_started', 'provider_request_finished',
        'tool_call', *middle, 'tool_result', 'provider_request_started',
        'provider_request_finished', 'run_finished',
    ]
    if node:
        assert list(hub.workspace.iterdir()) == []  # Distinct node filesystem was listed.
        assert len(node.commands) == 1
        command = node.commands[0]
        assert command['action'] == 'tool.workspace.list'
        assert node.journal.get(command['command_id'])['state'] == 'succeeded'
        delivery = hub.app.extensions['fleet']['services']['platform_delivery']
        assert delivery.repository.get(command['command_id'])['status'] == 'succeeded'
        assert node.client.handle(command)['status'] == 'succeeded'
        assert len(node.commands) == 1  # Duplicate delivery uses the durable journal.


@pytest.mark.parametrize('tool,arguments', [
    ('workspace_write', {'path': 'sentinel.txt', 'content': 'overwritten'}),
    ('workspace_artifact', {'path': 'sentinel.txt'}),
    ('workspace_exec', {'argv': ['touch', 'executed.txt']}),
    ('workspace_read', {'path': 'sentinel.txt'}),
])
def test_adversarial_wire_tool_cannot_reach_local_or_remote_backend(chain_target, capsys, tool, arguments):
    hub, node, args = chain_target
    hub.provider.forced_tool = (tool, arguments)
    for workspace in [hub.workspace] + ([node.workspace] if node else []):
        (workspace / 'sentinel.txt').write_bytes(b'preserved\x00\xff')
    assert main(args) == 1
    row = json.loads(capsys.readouterr().out)['models'][0]
    assert row['error'] == 'run_unknown'
    assert_only_list_advertised(hub.provider)
    assert len(hub.provider.requests) == 1
    client = Client(hub.origin)
    client.login()
    events = client.request('/api/platform/v1/runs/' + row['run_id'] + '/events')['events']
    assert not any(e['kind'] in ('tool_call', 'tool_result', 'node_command_queued') for e in events)
    workspace_id = 'remote' if node else 'home'
    assert client.request(f'/api/platform/v1/workspaces/{workspace_id}/artifacts')['artifacts'] == []
    for workspace in [hub.workspace] + ([node.workspace] if node else []):
        assert [p.name for p in workspace.iterdir()] == ['sentinel.txt']
        assert (workspace / 'sentinel.txt').read_bytes() == b'preserved\x00\xff'
    if node:
        assert node.commands == []


@pytest.mark.parametrize('case,status', [
    ('anonymous', 401), ('foreign_origin', 403), ('other_owner', 404),
    ('revoked', 401),
])
def test_acceptance_route_enforces_account_owner_origin_and_policy(paused_target, case, status):
    hub, client, path = paused_target
    caller, headers, body = client, {}, acceptance_body()
    if case == 'anonymous':
        caller = Client(hub.origin)
    elif case == 'foreign_origin':
        headers['Origin'] = 'https://foreign.invalid'
    elif case == 'other_owner':
        caller = Client(hub.origin)
        client.request('/api/accounts/invitations', {'email': 'other@example.test'})
        caller.request('/api/accounts/code', {'email': 'other@example.test', 'purpose': 'register'})
        caller.request('/api/accounts/register', {
            'email': 'other@example.test', 'code': hub.mail[-1][2], 'password': PASSWORD,
        })
        caller.login('other@example.test')
    elif case == 'revoked':
        cookie = client.cookie
        client.request('/api/accounts/logout', {})
        client.cookie = cookie
    caller.request(path + '/acceptance-turns', body, expected=status, headers=headers)
    repo = hub.app.extensions['fleet']['platform_repository']
    conversation = repo.get_conversation(hub.owner, path.rsplit('/', 1)[1])
    assert conversation['messages'] == [] and conversation['runs'] == []
    assert hub.provider.requests == []


@pytest.mark.parametrize('first,second', [('turns', 'acceptance-turns'), ('acceptance-turns', 'turns')])
def test_acceptance_http_idempotency_cannot_cross_policy(paused_target, first, second):
    hub, client, path = paused_target
    body = acceptance_body()
    turn = client.request(path + '/' + first, body, expected=202)
    duplicate = client.request(path + '/' + first, body, expected=202)
    assert duplicate['run']['run_id'] == turn['run']['run_id']
    client.request(path + '/' + second, body, expected=409)
    recovered = client.request(path)['conversation']
    assert len(recovered['runs']) == len(recovered['messages']) == 1
    assert hub.provider.requests == []


@pytest.mark.parametrize('policy_override', [None, 'unrestricted', ['workspace.write']])
def test_acceptance_queued_policy_survives_hub_restart(paused_target, policy_override):
    hub, client, path = paused_target
    body = acceptance_body()
    body['overrides']['tool_policy'] = policy_override
    turn = client.request(path + '/acceptance-turns', body, expected=202)
    assert turn['run']['state'] == 'queued'
    port = hub.server.server_port
    hub.stop()
    hub.start(port=port)
    run = client.terminal(turn['run']['run_id'])
    assert run['state'] == 'succeeded'
    repo = hub.app.extensions['fleet']['platform_repository']
    assert repo.get_run(hub.owner, run['run_id'])['config_snapshot']['tool_policy'] == 'acceptance_read_only'
    assert_only_list_advertised(hub.provider)
    recovered = client.request(path)['conversation']
    assert len(recovered['runs']) == 1 and recovered['runs'][0]['result_text'] == run['result_text']
    hub.app.extensions['fleet']['services']['platform_run_scheduler'].run_once()
    assert len(hub.provider.requests) == 2


def test_accepted_response_timeout_recovers_unique_run_by_token(idle_hub):
    hub = idle_hub
    release, accepted = Event(), Event()
    submissions = []

    @hub.app.after_request
    def hold_accepted_response(response):
        if request.path.endswith('/acceptance-turns') and response.status_code == 202:
            submissions.append(response.get_json()['run']['run_id'])
            accepted.set()
            release.wait(5)
        return response

    client = AccountClient(hub.origin)
    client.request('/api/accounts/login', {'email': 'mango', 'password': PASSWORD})
    try:
        row = check_model(client, 'read-only', 'home', .5)
        assert accepted.is_set()
        assert row['error'] == 'acceptance_timeout' and row['outcome'] == 'unconfirmed'
        assert 'run_id' not in row and row['client_token'].startswith('acceptance-')
    finally:
        release.set()
    path = '/api/platform/v1/conversations/' + row['conversation_id']
    recovered = client.request(path)['conversation']
    message = next(m for m in recovered['messages'] if m['client_token'] == row['client_token'])
    assert len(recovered['runs']) == 1 and len(submissions) == 1
    run = recovered['runs'][0]
    assert recovered['messages'] == [message]
    assert run['conversation_id'] == row['conversation_id']
    assert run['run_id'] == submissions[0] and run['state'] == 'queued'
    port = hub.server.server_port
    hub.stop()
    hub.start(port=port)
    reader = Client(hub.origin)
    reader.login()
    assert reader.terminal(run['run_id'])['state'] == 'succeeded'
    assert len(reader.request(path)['conversation']['runs']) == 1
    assert len(hub.provider.requests) == 2


def test_legacy_hub_does_not_fallback_to_unrestricted_turn(idle_hub):
    hub = idle_hub
    submissions = []

    @hub.app.before_request
    def legacy_server():
        if request.path.endswith(('/acceptance-turns', '/turns')):
            submissions.append(request.path)
        if request.path.endswith('/acceptance-turns'):
            return {'error': 'not_found'}, 404

    client = AccountClient(hub.origin)
    client.request('/api/accounts/login', {'email': 'mango', 'password': PASSWORD})
    row = check_model(client, 'read-only', 'home', 5)
    assert row['status'] == 'failed' and row['http_status'] == 404
    assert len(submissions) == 1 and submissions[0].endswith('/acceptance-turns')
    recovered = client.request('/api/platform/v1/conversations/' + row['conversation_id'])['conversation']
    assert recovered['runs'] == [] and recovered['messages'] == []
    assert hub.provider.requests == []
