"""Real HTTP, SQLite, signed queue, local OAuth peer and owned ACP workers."""
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

from fleet_core.control import generate_ed25519_keypair
from hub.bootstrap import create_app
from support.hub_env import HubStores
from tools.adapters.agy import AgyAdapter
from tools.providers.agy_auth import AgyAccountControl
from tools.supervisor.control_client import ControlClient, NonceStore
from tools.supervisor.supervisor import Supervisor

PEER = Path(__file__).parent / 'fixtures/providers/agy_account_peer.py'
ACTOR = 'operator@example.test'


def eventually(read, predicate, timeout=8):
    end = time.monotonic() + timeout
    while True:
        value = read()
        if predicate(value):
            return value
        if time.monotonic() >= end:
            pytest.fail('fixture state deadline: ' + repr(value))
        time.sleep(.03)


@pytest.fixture
def chain(tmp_path, monkeypatch):
    if os.name == 'nt':
        pytest.skip('Full Hub uses POSIX ownership; actual node auth/worker suites run on Windows')
    monkeypatch.setenv('GOOGLE_API_KEY', 'ambient-private-google')
    monkeypatch.setenv('AGENT_FLEET_TOKEN', 'ambient-private-hub')
    stores = HubStores(tmp_path / 'hub')
    stores.store.save_snapshot('fixture', {'reachable': True, 'agents': {
        'agy': {'installed': True, 'provider_accounts': {'schema_version': 1}}}})
    private, public = generate_ed25519_keypair()
    app = create_app(stores.config(dev_operator=ACTOR, supervisor_enabled=True,
        supervisor_signing_raw=private, session_encryption_raw=b'k' * 32,
        supervisor_credentials={'fixture': 'secret'}, runner_credentials={'fixture': 'runner'},
        project_whitelist={'fixture': ['fleet']}))
    cfg = SimpleNamespace(supervisor_manifest_dir=tmp_path / 'node', machine='fixture',
        agents={'agy': {'command': [sys.executable, str(PEER)]}})
    supervisor = Supervisor(cfg.supervisor_manifest_dir, 'fixture')
    accounts = AgyAccountControl(cfg, supervisor)
    def transport(path, data, headers):
        with app.test_client() as client:
            response = client.post(path, json=data, headers=headers)
            return response.status_code, response.json
    control = ControlClient(hub_url='https://fixture.invalid', machine_id='fixture', credential='secret',
        hub_public_key=public, public_supervisor=supervisor, provider_accounts=accounts,
        nonce_store=NonceStore(tmp_path / 'nonce'), transport=transport)
    yield app.test_client(), control, accounts, supervisor, stores
    accounts.close()
    for session in list(supervisor._entries):
        supervisor.terminate_session(session, grace_s=.1)


def submit(chain, identifier, operation, token, **fields):
    response = chain[0].post('/api/provider-accounts/' + identifier + '/operations', json={
        'operation': operation, 'client_token': token, **fields})
    assert response.status_code == 202, response.json
    assert chain[1].poll_once()['ok']
    return response.json


def account(chain, identifier):
    response = chain[0].get('/api/provider-accounts/' + identifier)
    assert response.status_code == 200, response.json
    return response.json['account']


def login(chain, token):
    response = chain[0].post('/api/machines/fixture/provider-accounts', json={
        'name': token, 'client_token': 'create-' + token})
    assert response.status_code == 201, response.json
    identifier = response.json['account']['account_id']
    assert chain[1].poll_once()['ok']
    assert account(chain, identifier)['state'] == 'signed_out'
    flow = submit(chain, identifier, 'login', 'login-' + token)['operation']
    ready = eventually(lambda: account(chain, identifier), lambda a: a['state'] in {'waiting', 'failed'})
    assert ready['state'] == 'waiting', ready
    submitted = submit(chain, identifier, 'submit_login', 'answer-' + token,
        login_command_id=flow['command_id'], response='fixture-code')
    final = eventually(lambda: account(chain, identifier), lambda a: a['state'] in {'ready', 'failed'})
    assert final['state'] == 'ready', final
    # Browser response loss recovery neither starts another flow nor sends another code.
    replay = chain[0].post('/api/provider-accounts/' + identifier + '/operations', json={
        'operation': 'submit_login', 'client_token': 'answer-' + token,
        'login_command_id': flow['command_id'], 'response': 'fixture-code'})
    assert replay.status_code == 200
    assert replay.json['operation']['command_id'] == submitted['operation']['command_id']
    assert replay.json['operation']['state'] == 'ready'
    methods = [json.loads(r)['method'] for r in (chain[2].accounts.profile(identifier) /
        'fixture-trace.jsonl').read_text(encoding='utf-8').splitlines()]
    assert methods == ['login', 'initialize', 'authenticate', 'session/new']
    assert 'session/prompt' not in methods
    return identifier


def start_worker(chain, identifier, name, tmp_path):
    adapter = AgyAdapter(command=[sys.executable, str(PEER)])
    from hashlib import sha256
    adapter.bind_account(chain[2].accounts.root, identifier, sha256(ACTOR.encode()).hexdigest())
    argv = adapter.build_argv('hold')
    manifest = chain[3].launch(agent='agy', command=argv, cwd=str(tmp_path), env_allowlist={},
        session_id=name, capability_manifest={'provider_account_id': identifier})
    trace = chain[2].accounts.profile(identifier) / 'fixture-trace.jsonl'
    eventually(lambda: trace.read_text(encoding='utf-8'), lambda s: 'session/prompt' in s)
    return manifest


def test_two_accounts_signed_login_bound_tasks_disable_and_native_logout(chain, tmp_path):
    a, b = login(chain, 'work'), login(chain, 'personal')
    assert chain[2].accounts.profile(a) != chain[2].accounts.profile(b)
    request = {'machine': 'fixture', 'agent_type': 'agy', 'project': 'fleet',
        'instruction': 'Fixture only', 'client_token': 'bound-create', 'provider_account_id': a}
    task = chain[0].post('/api/tasks', json=request)
    assert task.status_code == 201, task.json
    assert task.json['task']['provider_account_id'] == a
    first = start_worker(chain, a, 'account_a', tmp_path)
    second = start_worker(chain, b, 'account_b', tmp_path)
    marker = chain[2].accounts.profile(a) / 'history-preserved.txt'
    marker.write_text('owned history', encoding='utf-8')
    submit(chain, a, 'disable', 'disable-work')
    assert account(chain, a)['state'] == 'disabled'
    assert chain[3].get(first.session_id).state == 'terminated'
    assert chain[3]._ops.proc_poll(chain[3]._entries[second.session_id].handle) is None
    assert (chain[2].accounts.profile(a) / 'fixture-credential.json').exists()
    denied = chain[0].post('/api/tasks', json={**request, 'client_token': 'disabled-new'})
    assert denied.status_code == 409 and denied.json['error'] == 'provider_account_not_ready'
    replay = chain[0].post('/api/tasks', json=request)
    assert replay.status_code == 200 and replay.json['task']['task_id'] == task.json['task']['task_id']
    submit(chain, a, 'enable', 'enable-work')
    assert account(chain, a)['state'] == 'ready'
    submit(chain, a, 'logout', 'logout-work')
    assert account(chain, a)['state'] == 'signed_out'
    assert not (chain[2].accounts.profile(a) / 'fixture-credential.json').exists()
    assert marker.read_text(encoding='utf-8') == 'owned history'
    assert (chain[2].accounts.profile(b) / 'fixture-credential.json').exists()
    assert account(chain, b)['state'] == 'ready'
    assert chain[3]._ops.proc_poll(chain[3]._entries[second.session_id].handle) is None
    submit(chain, a, 'enable', 'enable-after-logout')
    assert account(chain, a)['state'] == 'signed_out'


def test_disable_before_create_delivery_and_retry_after_missing_profile_can_recover(chain):
    response = chain[0].post('/api/machines/fixture/provider-accounts', json={
        'name': 'late-node', 'client_token': 'create-late-node'})
    assert response.status_code == 201
    identifier = response.json['account']['account_id']
    # Newer revision supersedes create before this node has seen any command.
    submit(chain, identifier, 'disable', 'disable-before-delivery')
    assert account(chain, identifier)['state'] == 'disabled'
    submit(chain, identifier, 'enable', 'enable-late-node')
    assert account(chain, identifier)['state'] == 'signed_out'
    flow = submit(chain, identifier, 'login', 'login-late-node')['operation']
    result = eventually(lambda: account(chain, identifier), lambda a: a['state'] in {'waiting', 'failed'})
    assert result['state'] == 'waiting', result
    submit(chain, identifier, 'cancel_login', 'cancel-late-node', login_command_id=flow['command_id'])
    assert account(chain, identifier)['state'] == 'signed_out'
