"""Account-to-artifact acceptance through actual HTTP and scheduler boundaries."""
import hashlib
import json
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from support.full_flow import FullFlowHub, Client, PASSWORD
from support.strict_provider import StrictProvider, REPORT


@pytest.fixture
def full_flow(tmp_path, monkeypatch):
    monkeypatch.setenv('FLEET_FULL_FLOW_SECRET', 'full-flow-provider-secret')
    with StrictProvider() as provider:
        with FullFlowHub(tmp_path, provider) as hub:
            yield hub


def test_strict_provider_rejects_the_production_dotted_tool_regression():
    with StrictProvider() as provider:
        body = {'model': 'read-only', 'messages': [{'role': 'user', 'content': 'task'}], 'tools': [{
            'type': 'function', 'function': {'name': 'workspace.list', 'description': 'list', 'parameters': {'type': 'object'}},
        }]}
        headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer full-flow-provider-secret'}
        with pytest.raises(HTTPError) as caught:
            urlopen(Request(provider.url, data=json.dumps(body).encode(), headers=headers))
        with caught.value as response:
            assert response.code == 400
        assert provider.requests == []
        # Change only the tool name: a valid request must pass the same peer.
        body['tools'][0]['function']['name'] = 'workspace_list'
        with urlopen(Request(provider.url, data=json.dumps(body).encode(), headers=headers)) as response:
            assert response.status == 200
        assert len(provider.requests) == 1


def test_account_to_real_http_tools_artifact_and_restart(full_flow):
    hub = full_flow
    client = Client(hub.origin)
    client.request('/api/operator/session', expected=401)
    user = client.login()
    assert client.request('/api/platform/v1/readiness')['configuration_ready']
    turn = client.submit()
    run = client.terminal(turn['run']['run_id'])
    assert run['state'] == 'succeeded'
    assert run['result_text'] == 'Full flow completed: 中文回复'
    assert run['usage']['provider_requests'] == 5
    assert (hub.workspace / 'flow.txt').read_bytes() == REPORT.encode()
    duplicate = client.submit(conversation=turn['conversation_id'])
    assert duplicate['run']['run_id'] == run['run_id'] and not duplicate['created']
    events = client.request('/api/platform/v1/runs/' + run['run_id'] + '/events')['events']
    assert [e['payload']['tool'] for e in events if e['kind'] == 'tool_result'] == [
        'workspace.list', 'workspace.write', 'workspace.read', 'workspace.artifact']
    assert all(e['payload']['state'] == 'succeeded' for e in events if e['kind'] == 'tool_result')
    assert len({e['sequence'] for e in events}) == len(events)
    assert client.request('/api/platform/v1/runs/' + run['run_id'] + '/events?after=' + str(events[-1]['sequence']))['events'] == []
    artifacts = client.request('/api/platform/v1/workspaces/home/artifacts')['artifacts']
    assert len(artifacts) == 1
    artifact = artifacts[0]
    content = client.request('/api/platform/v1/artifacts/' + artifact['artifact_id'] + '/content?workspace_id=home')
    assert content == REPORT.encode()
    assert hashlib.sha256(content).hexdigest() == artifact['sha256']
    # A completely new app instance reads the persisted conversation/account DBs.
    port = hub.server.server_port
    hub.stop(); hub.start(port=port)
    assert client.request('/api/accounts/me')['user']['id'] == user['id']
    recovered = client.request('/api/platform/v1/conversations/' + turn['conversation_id'])['conversation']
    assert recovered['runs'][0]['result_text'] == run['result_text']
    assert len(hub.provider.requests) == 5  # No replay on restart or duplicate submit.
    assert hub.provider.errors == []
    client.request('/api/accounts/logout', {})
    client.request('/api/operator/session', expected=401)


def test_invitation_registration_recovery_and_owner_isolation(full_flow):
    hub = full_flow
    admin, member, other = Client(hub.origin), Client(hub.origin), Client(hub.origin)
    admin.login()
    member.request('/api/accounts/code', {'email': 'member@example.test', 'purpose': 'register'})
    assert hub.mail == []
    admin.request('/api/accounts/invitations', {'email': 'member@example.test'})
    member.request('/api/accounts/code', {'email': 'member@example.test', 'purpose': 'register'})
    code = hub.mail[-1][2]
    member.request('/api/accounts/register', {'email': 'member@example.test', 'code': 'wrong', 'password': PASSWORD}, expected=400)
    member.request('/api/accounts/register', {'email': 'member@example.test', 'code': code, 'password': PASSWORD})
    user = member.login('member@example.test')
    hub.provision(user['id'])  # Explicit tenant catalog fixture; not a production registration side effect.
    turn = member.submit()
    assert member.terminal(turn['run']['run_id'])['state'] == 'succeeded'
    admin.request('/api/platform/v1/conversations/' + turn['conversation_id'], expected=404)
    admin.request('/api/platform/v1/runs/' + turn['run']['run_id'], expected=404)
    artifact = member.request('/api/platform/v1/workspaces/home/artifacts')['artifacts'][0]
    admin.request('/api/platform/v1/artifacts/' + artifact['artifact_id'] + '/content?workspace_id=home', expected=404)
    member.request('/api/accounts/users', expected=403)
    other.login('member@example.test')
    member.request('/api/accounts/code', {'email': 'member@example.test', 'purpose': 'reset'})
    other.request('/api/accounts/reset', {'email': 'member@example.test', 'code': hub.mail[-1][2], 'password': 'Changed-password-456!'})
    member.request('/api/operator/session', expected=401)
    other.request('/api/operator/session', expected=401)
    member.request('/api/accounts/login', {'email': 'member@example.test', 'password': PASSWORD}, expected=401)
    member.login('member@example.test', 'Changed-password-456!')
    current = next(u for u in admin.request('/api/accounts/users')['users'] if u['id'] == user['id'])
    admin.request('/api/accounts/users/' + user['id'], {'revision': current['revision'], 'active': False})
    member.request('/api/operator/session', expected=401)
    member.request('/api/accounts/login', {'email': 'member@example.test', 'password': 'Changed-password-456!'}, expected=401)


@pytest.mark.parametrize('prompt,status', [('http-401', 401), ('http-429', 429), ('http-500', 500), ('http-502', 502), ('http-503', 503), ('bad-json', None), ('invalid-tool', None)])
def test_provider_fault_is_durable_visible_and_never_replayed(full_flow, prompt, status):
    hub = full_flow
    client = Client(hub.origin); client.login()
    turn = client.submit(prompt)
    run = client.terminal(turn['run']['run_id'])
    assert run['state'] == 'unknown'
    events = client.request('/api/platform/v1/runs/' + run['run_id'] + '/events')['events']
    failures = [e['payload'] for e in events if e['kind'] == 'run_unknown']
    assert len(failures) == 1
    if status:
        assert failures[0]['provider_status'] == status
        assert str(status) in run['result_text']
    assert 'must-not-leak' not in json.dumps(events) + run['result_text']
    assert not list(hub.workspace.iterdir())
    hub.stop(); hub.start(port=int(client.origin.rsplit(':', 1)[1]))
    recovered = client.request('/api/platform/v1/runs/' + run['run_id'])
    assert recovered['state'] == 'unknown'
    # Dispatch another scheduler tick after restart; no provider replay.
    hub.app.extensions['fleet']['services']['platform_run_scheduler'].run_once()
    assert len(hub.provider.requests) == 1


def test_tool_failure_prevents_final_success_and_path_escape(full_flow):
    client = Client(full_flow.origin); client.login()
    turn = client.submit('unsafe-path')
    run = client.terminal(turn['run']['run_id'])
    assert run['state'] == 'failed'
    assert not (full_flow.workspace.parent / 'escape.txt').exists()
    assert len(full_flow.provider.requests) == 2


def test_real_transport_timeout_records_unknown_without_retry(full_flow):
    client = Client(full_flow.origin); client.login()
    turn = client.submit('timeout')
    assert full_flow.provider.entered.wait(5)
    run = client.terminal(turn['run']['run_id'])
    assert run['state'] == 'unknown' and 'timeout' in run['result_text']
    assert len(full_flow.provider.requests) == 1


def test_running_and_queued_cancellation_have_no_tool_side_effects(full_flow):
    hub = full_flow
    client = Client(hub.origin); client.login()
    port = hub.server.server_port
    hub.stop(); hub.start(port=port, scheduler=False)
    hub.provider.release.clear()
    queued = client.submit('queued cancellation')
    rid = queued['run']['run_id']
    client.request('/api/platform/v1/runs/' + rid + '/cancel', {})
    hub.start_scheduler()
    assert client.terminal(rid)['state'] == 'cancelled'
    assert hub.provider.requests == []
    running = client.submit('slow cancellation', token='second')
    rid = running['run']['run_id']
    assert hub.provider.entered.wait(5)
    client.request('/api/platform/v1/runs/' + rid + '/cancel', {})
    hub.provider.release.set()
    assert client.terminal(rid)['state'] == 'cancelled'
    assert not list(hub.workspace.iterdir())
