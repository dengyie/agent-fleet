"""Real HTTP/SQLite behavior for machine-bound personal Google accounts."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import os
import sqlite3
from threading import Barrier
from urllib.parse import urlencode

import pytest

from fleet_core.control import generate_ed25519_keypair
from fleet_core.provider_accounts import authorization_url, login_response
from hub.application.errors import ApplicationError
from hub.application.provider_account_service import ProviderAccountService
from hub.bootstrap import create_app
from hub.http.provider_account_routes import bp
from hub.infrastructure.provider_account_repository import ProviderAccountRepository, ProviderAccountStoreError
from support.hub_env import HubStores

ACTOR = 'operator@example.test'
AUTH_URL = 'https://accounts.google.com/o/oauth2/auth?' + urlencode({
    'state': 'fixture-state', 'response_type': 'code',
    'redirect_uri': 'https://antigravity.google/oauth-callback', 'client_id': 'fixture-client'})
SUPERVISOR = {'X-Supervisor-Credential': 'mac-local:sup-secret'}


@pytest.fixture
def hub(tmp_path):
    if os.name == 'nt':
        pytest.skip('Hub signed-command store requires POSIX ownership; node contracts run on Windows')
    stores = HubStores(tmp_path)
    stores.store.save_snapshot('mac-local', {'machine': 'mac-local', 'reachable': True,
        'agents': {'agy': {'installed': True, 'provider_accounts': {'schema_version': 1}}}})
    private, _ = generate_ed25519_keypair()
    app = create_app(stores.config(dev_operator=ACTOR, ingest_token='ingest-secret',
        supervisor_enabled=True, supervisor_signing_raw=private,
        supervisor_credentials={'mac-local': 'sup-secret', 'other': 'other-secret'},
        runner_credentials={'mac-local': 'runner-secret'}, session_encryption_raw=b'k' * 32))
    supervisor = app.extensions['fleet']['services']['supervisor']
    clock = [1800000000.0]
    # A stable clock makes all TTL boundaries deterministic without sleeping.
    delegate = getattr(supervisor, '_supervisor', supervisor)
    while not hasattr(delegate, '_now_fn'):
        delegate = delegate._service
    delegate._now_fn = lambda: clock[0]
    repository = ProviderAccountRepository(tmp_path / 'var/supervisor/commands.db', key=b'k' * 32, clock=lambda: clock[0])
    repository.init()
    service = ProviderAccountService(repository, supervisor, stores.store)
    app.extensions['fleet']['services']['provider_accounts'] = service
    if 'provider_accounts' not in app.blueprints:
        app.register_blueprint(bp)
    return stores, app, app.test_client(), service, repository, clock


def create(hub, token='create-google', name='Google work'):
    response = hub[2].post('/api/machines/mac-local/provider-accounts', json={'name': name, 'client_token': token})
    assert response.status_code == 201, response.json
    return response.json


def operate(hub, account, operation, token, **kwargs):
    return hub[2].post('/api/provider-accounts/' + account + '/operations',
        json={'operation': operation, 'client_token': token, **kwargs})


def dispatch(hub):
    response = hub[2].post('/api/supervisor/poll', json={}, headers=SUPERVISOR)
    assert response.status_code == 200, response.json
    return response.json['commands']


def report(hub, operation, state, **kwargs):
    return hub[2].post('/api/supervisor/provider-accounts/report', headers=SUPERVISOR,
        json={**{key: operation[key] for key in ('command_id', 'account_id', 'revision')}, 'state': state, **kwargs})


def signed_out(hub):
    result = create(hub)
    dispatch(hub)
    assert report(hub, result['operation'], 'signed_out').json == {'ok': True, 'accepted': True}
    return result['account']['account_id']


def waiting(hub):
    account = signed_out(hub)
    result = operate(hub, account, 'login', 'login-google')
    assert result.status_code == 202, result.json
    dispatch(hub)
    op = result.json['operation']
    assert report(hub, op, 'waiting', authorization_url=AUTH_URL, expires_at=hub[5][0] + 299).json['accepted']
    return account, op


def counts(repository):
    with closing(repository._connect()) as conn:
        return tuple(conn.execute('SELECT count(*) FROM ' + table).fetchone()[0] for table in
            ('provider_accounts', 'provider_account_operations', 'supervisor_commands'))


def test_create_replay_one_signed_command_and_public_directory(hub):
    first = create(hub)
    replay = hub[2].post('/api/machines/mac-local/provider-accounts', json={'name': 'Google work', 'client_token': 'create-google'})
    assert replay.status_code == 200 and replay.json['created'] is False
    assert replay.json['operation'] == first['operation']
    assert counts(hub[4]) == (1, 1, 1)
    assert first['account']['provider'] == 'agy' and first['account']['state'] == 'starting'
    listed = hub[2].get('/api/machines/mac-local/provider-accounts')
    assert listed.json['accounts'] == [first['account']]
    assert listed.headers['Cache-Control'] == 'no-store'
    wire = dispatch(hub)[0]
    assert wire['action'] == 'manage_provider_account'
    assert wire['target']['session_id'] == first['account']['account_id']
    assert wire['payload']['owner_digest'] == hashlib.sha256(ACTOR.encode()).hexdigest()
    assert set(wire['payload']) == {'account_id', 'revision', 'operation', 'owner_digest'}


@pytest.mark.parametrize('change', ['name', 'operator', 'machine'])
def test_create_token_conflicts_bounded_without_original_receipt(hub, change):
    original = create(hub)
    headers, machine = {}, 'mac-local'
    body = {'name': 'different' if change == 'name' else 'Google work', 'client_token': 'create-google'}
    if change == 'operator': headers['Cf-Access-Authenticated-User-Email'] = 'other@example.test'
    if change == 'machine': machine = 'other'
    response = hub[2].post('/api/machines/' + machine + '/provider-accounts', json=body, headers=headers)
    assert response.status_code == 409 and response.json['error'] == 'idempotency_conflict'
    for forbidden in ('Google work', 'create-google', original['operation']['command_id'], original['account']['account_id']):
        assert forbidden not in response.get_data(as_text=True)
    assert counts(hub[4]) == (1, 1, 1)


def test_concurrent_same_token_creates_one_account_operation_command(hub):
    barrier = Barrier(6)
    def submit(_):
        barrier.wait()
        return hub[3].create('mac-local', {'name': 'same', 'client_token': 'parallel-create'}, ACTOR)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(submit, range(6)))
    assert sum(row['created'] for row in results) == 1
    assert len({row['operation']['command_id'] for row in results}) == 1
    assert counts(hub[4]) == (1, 1, 1)


def test_queue_failure_rolls_back_account_and_idempotency(hub, monkeypatch):
    def failed(*args, **kwargs):
        raise sqlite3.OperationalError('/private/profile secret')
    monkeypatch.setattr(hub[3], '_enqueue', failed)
    response = hub[2].post('/api/machines/mac-local/provider-accounts', json={'name': 'work', 'client_token': 'failed'})
    assert response.status_code == 503 and response.json['error'] == 'provider_accounts_unavailable'
    assert 'private' not in response.get_data(as_text=True)
    assert counts(hub[4]) == (0, 0, 0)


def test_operation_insert_failure_rolls_back_already_created_signed_command(hub):
    with closing(hub[4]._connect()) as conn:
        conn.execute("CREATE TRIGGER fail_account_op BEFORE INSERT ON provider_account_operations "
                     "BEGIN SELECT RAISE(ABORT,'fixture private failure'); END")
        conn.commit()
    response = hub[2].post('/api/machines/mac-local/provider-accounts',
        json={'name': 'work', 'client_token': 'transaction-failure'})
    assert response.status_code == 503 and response.json['error'] == 'provider_accounts_unavailable'
    assert 'private failure' not in response.get_data(as_text=True)
    assert counts(hub[4]) == (0, 0, 0)


@pytest.mark.parametrize('headers', [{}, {'X-Runner-Credential': 'mac-local:runner-secret'},
    {'X-Agent-Fleet-Token': 'ingest-secret'}, SUPERVISOR, {'X-Platform-Node-Credential': 'foreign'}])
def test_machine_credentials_never_authorize_operator_directory(hub, headers):
    hub[1].config['DEV_OPERATOR'] = None
    response = hub[2].get('/api/machines/mac-local/provider-accounts', headers=headers)
    assert response.status_code == 401


def test_other_owner_cannot_read_or_mutate_account(hub):
    result = create(hub)
    identifier = result['account']['account_id']
    headers = {'Cf-Access-Authenticated-User-Email': 'other@example.test'}
    assert hub[2].get('/api/provider-accounts/' + identifier, headers=headers).status_code == 404
    assert hub[2].post('/api/provider-accounts/' + identifier + '/operations',
        headers=headers, json={'operation': 'logout', 'client_token': 'not-owner'}).status_code == 404
    assert counts(hub[4]) == (1, 1, 1)


def test_node_resolve_machine_delivery_bound_and_no_secrets_in_public(hub):
    result = create(hub)
    command = result['operation']['command_id']
    endpoint = '/api/supervisor/provider-accounts/resolve'
    assert hub[2].post(endpoint, json={'command_id': command}, headers=SUPERVISOR).status_code == 409
    dispatch(hub)
    assert hub[2].post(endpoint, json={'command_id': command}, headers={'X-Supervisor-Credential': 'other:other-secret'}).status_code == 404
    response = hub[2].post(endpoint, json={'command_id': command}, headers=SUPERVISOR)
    assert response.status_code == 200
    assert response.json['configuration']['operation'] == 'create'
    assert hub[2].post(endpoint, json={'command_id': command}).status_code in {401, 403}


def test_login_response_encrypted_recoverable_and_cleared_on_ready(hub):
    identifier, flow = waiting(hub)
    callback = 'https://antigravity.google/oauth-callback?' + urlencode({'state': 'fixture-state', 'code': 'unique-private-code'})
    response = operate(hub, identifier, 'submit_login', 'send-code', response=callback, login_command_id=flow['command_id'])
    assert response.status_code == 202, response.json
    assert 'unique-private-code' not in response.get_data(as_text=True)
    op = response.json['operation']
    replay = operate(hub, identifier, 'submit_login', 'send-code', response=callback, login_command_id=flow['command_id'])
    assert replay.status_code == 200 and replay.json['operation'] == op
    dispatch(hub)
    resolved = hub[3].resolve('mac-local', op['command_id'])['configuration']
    assert resolved['response'] == callback and resolved['login_command_id'] == flow['command_id']
    with closing(hub[4]._connect()) as conn:
        raw = conn.execute('SELECT secret FROM provider_account_operations WHERE command_id=?', (op['command_id'],)).fetchone()[0]
        assert callback.encode() not in raw
        for row in conn.execute('SELECT wire_json FROM supervisor_commands'):
            assert 'unique-private-code' not in row[0]
    assert report(hub, op, 'ready').json['accepted']
    assert report(hub, flow, 'failed', error='login_failed').json == {'ok': True, 'accepted': False}
    assert hub[3].assert_ready(identifier, 'mac-local', ACTOR)['state'] == 'ready'
    with closing(hub[4]._connect()) as conn:
        assert conn.execute('SELECT secret FROM provider_account_operations WHERE command_id=?', (op['command_id'],)).fetchone()[0] is None
    fetched = hub[2].get('/api/provider-accounts/' + identifier)
    assert fetched.json['account']['state'] == 'ready'
    assert 'authorization_url' not in fetched.json['account']


def test_response_flow_state_and_deadline_validation(hub):
    identifier, flow = waiting(hub)
    wrong = 'https://antigravity.google/oauth-callback?state=wrong&code=private'
    assert operate(hub, identifier, 'submit_login', 'wrong-state', response=wrong, login_command_id=flow['command_id']).status_code == 400
    assert operate(hub, identifier, 'submit_login', 'wrong-flow', response='code', login_command_id='cmd_' + 'f' * 32).status_code == 409
    hub[5][0] += 301
    expired = hub[2].get('/api/provider-accounts/' + identifier).json['account']
    assert expired['state'] == 'failed' and expired['error'] == 'login_expired'
    assert 'authorization_url' not in expired
    assert operate(hub, identifier, 'submit_login', 'expired-code', response='code', login_command_id=flow['command_id']).status_code == 409
    assert report(hub, flow, 'ready').json == {'ok': True, 'accepted': False}


def test_no_encryption_key_refuses_code_without_persisting(hub):
    identifier, flow = waiting(hub)
    hub[4].box = None
    before = counts(hub[4])
    response = operate(hub, identifier, 'submit_login', 'no-key', response='private-code', login_command_id=flow['command_id'])
    assert response.status_code == 503 and response.json['error'] == 'provider_login_encryption_unavailable'
    assert counts(hub[4]) == before


def test_disable_fences_immediately_and_old_report_cannot_restore(hub):
    identifier, flow = waiting(hub)
    disabled = operate(hub, identifier, 'disable', 'disable-google')
    assert disabled.status_code == 202
    with pytest.raises(ApplicationError) as error:
        hub[3].assert_ready(identifier, 'mac-local', ACTOR)
    assert error.value.code == 'provider_account_not_ready'
    assert report(hub, flow, 'ready').json['accepted'] is False
    dispatch(hub)
    assert report(hub, disabled.json['operation'], 'disabled').json['accepted']
    assert hub[3].get(identifier, ACTOR)['account']['enabled'] is False


def test_unsupported_node_refused_but_accepted_creation_replay_recovers(hub):
    accepted = create(hub)
    hub[0].store.save_snapshot('mac-local', {'machine': 'mac-local', 'reachable': True, 'agents': {'agy': {'installed': True}}})
    response = hub[2].post('/api/machines/mac-local/provider-accounts', json={'name': 'Google work', 'client_token': 'create-google'})
    assert response.status_code == 200 and response.json['operation'] == accepted['operation']
    response = hub[2].post('/api/machines/mac-local/provider-accounts', json={'name': 'other', 'client_token': 'new'})
    assert response.status_code == 409 and response.json['error'] == 'provider_accounts_unavailable'
    assert counts(hub[4]) == (1, 1, 1)


def test_migration_existing_queue_idempotent_and_future_schema_preserved(hub):
    create(hub)
    hub[4].init()
    assert counts(hub[4]) == (1, 1, 1)
    with closing(hub[4]._connect()) as conn:
        conn.execute("UPDATE supervisor_meta SET value='9' WHERE key='provider_accounts_schema'")
    with pytest.raises(ProviderAccountStoreError):
        hub[4].init()
    with closing(hub[4]._connect()) as conn:
        assert conn.execute("SELECT value FROM supervisor_meta WHERE key='provider_accounts_schema'").fetchone()[0] == '9'
    assert counts(hub[4]) == (1, 1, 1)


def test_populated_schema_probe_does_not_collide_with_existing_token_digest(hub):
    create(hub)
    with closing(hub[4]._connect()) as conn:
        conn.execute("UPDATE provider_account_operations SET token_digest=?", ('a' * 64,))
        conn.commit()
    hub[4].init()
    assert counts(hub[4]) == (1, 1, 1)


def test_atomic_schema_failure_rolls_back_partial_additions(tmp_path):
    if os.name == 'nt':
        pytest.skip('Hub signed-command store requires POSIX ownership')
    from hub.infrastructure.supervisor_command_repository import SupervisorCommandRepository
    path = tmp_path / 'queue/commands.db'
    SupervisorCommandRepository(path).init()
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('CREATE TABLE provider_accounts(broken TEXT)')
    repository = ProviderAccountRepository(path, key=b'k' * 32)
    with pytest.raises(ProviderAccountStoreError): repository.init()
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='provider_account_operations'").fetchone() is None
        assert conn.execute("SELECT name FROM pragma_table_info('provider_accounts')").fetchone()[0] == 'broken'


@pytest.mark.parametrize('url', [AUTH_URL.replace('accounts.google.com', 'accounts.google.com.evil.test'),
    AUTH_URL + '&state=duplicate', AUTH_URL.replace('https://', 'http://', 1),
    AUTH_URL.replace('/o/oauth2/auth', '/o/oauth2/v2/auth'), AUTH_URL + '#fragment',
    AUTH_URL.replace('accounts.google.com', 'accounts.google.com:443')])
def test_authorization_url_rejects_non_google_and_ambiguous_urls(url):
    with pytest.raises(ValueError): authorization_url(url)


def test_login_callback_binds_state_and_rejects_multiline():
    assert login_response(AUTH_URL, 'native-code') == 'native-code'
    for value in ('https://antigravity.google/oauth-callback?state=wrong&code=x', 'code\nsecond',
                  'https://evil.test/oauth-callback?state=fixture-state&code=x'):
        with pytest.raises(ValueError): login_response(AUTH_URL, value)


def test_login_fails_before_command_without_encryption_key(hub):
    identifier = signed_out(hub)
    hub[4].box = None
    before = counts(hub[4])
    result = operate(hub, identifier, 'login', 'login-no-key')
    assert result.status_code == 503 and result.json['error'] == 'provider_login_encryption_unavailable'
    assert counts(hub[4]) == before


@pytest.mark.parametrize('terminal', ['failed', 'rejected', 'expired'])
def test_pending_operations_terminal_command_recover_failure(hub, terminal):
    result = create(hub)
    identifier = result['account']['account_id']
    with closing(hub[4]._connect()) as conn:
        conn.execute('UPDATE supervisor_commands SET status=? WHERE command_id=?', (terminal, result['operation']['command_id']))
    account = hub[3].get(identifier, ACTOR)['account']
    assert account['state'] == 'failed' and account['error'] == 'provider_account_failed'
    assert hub[3].operation(identifier, result['operation']['command_id'], ACTOR)['operation']['state'] == 'failed'


def test_undelivered_report_never_confirms_account(hub):
    result = create(hub)
    assert report(hub, result['operation'], 'signed_out').json == {'ok': True, 'accepted': False}
    assert hub[3].get(result['account']['account_id'], ACTOR)['account']['state'] == 'starting'


def test_expired_command_erases_oauth_secret(hub):
    identifier, flow = waiting(hub)
    result = operate(hub, identifier, 'submit_login', 'private-submit', response='short-lived-private-code', login_command_id=flow['command_id'])
    command = result.json['operation']['command_id']
    with closing(hub[4]._connect()) as conn:
        conn.execute("UPDATE supervisor_commands SET status='expired' WHERE command_id=?", (command,))
    assert hub[3].get(identifier, ACTOR)['account']['state'] == 'failed'
    with closing(hub[4]._connect()) as conn:
        assert conn.execute('SELECT secret FROM provider_account_operations WHERE command_id=?', (command,)).fetchone()[0] is None


@pytest.mark.parametrize('data', [None, [], {}, {'name': 'x', 'client_token': 'bad token'},
    {'name': 'x' * 81, 'client_token': 'bounded'}, {'name': '\x00', 'client_token': 'bounded'},
    {'name': 'x', 'client_token': 'bounded', 'credential': 'private'}])
def test_create_rejects_malformed_and_extra_credentials(hub, data):
    response = hub[2].post('/api/machines/mac-local/provider-accounts', json=data)
    assert response.status_code == 400
    assert counts(hub[4]) == (0, 0, 0)


def test_provider_body_limit_and_sqlite_failure_are_bounded(hub, monkeypatch):
    response = hub[2].post('/api/machines/mac-local/provider-accounts',
        data='{"name":"' + 'x' * 17000 + '","client_token":"bounded"}', content_type='application/json')
    assert response.status_code == 413
    def broken(): raise sqlite3.DatabaseError('/private/token/unique')
    monkeypatch.setattr(hub[4], '_connect', broken)
    response = hub[2].get('/api/machines/mac-local/provider-accounts')
    assert response.status_code == 503
    assert '/private' not in response.get_data(as_text=True)


def test_account_mode_admin_permission_revocation_blocks_future_mutations(tmp_path):
    if os.name == 'nt':
        pytest.skip('Hub signed-command store requires POSIX ownership')
    from test_account_http import ORIGIN, PASSWORD, post, signup
    stores = HubStores(tmp_path)
    stores.store.save_snapshot('mac-local', {'machine': 'mac-local', 'reachable': True,
        'agents': {'agy': {'installed': True, 'provider_accounts': {'schema_version': 1}}}})
    private, _ = generate_ed25519_keypair()
    mail = []
    app = create_app(stores.config(supervisor_enabled=True, supervisor_signing_raw=private,
        session_encryption_raw=b'k' * 32, accounts={'enabled': True, 'origin': ORIGIN,
        'registration': 'open', 'sender': lambda *args: mail.append(args)}))
    accounts = app.extensions['accounts']
    accounts.bootstrap_admin('admin@example.test', PASSWORD)
    admin, operator, anonymous = app.test_client(), app.test_client(), app.test_client()
    assert post(admin, 'login', {'email': 'admin@example.test', 'password': PASSWORD}).status_code == 200
    user = signup(operator, mail, 'operator@example.test').json['user']
    url = '/api/machines/mac-local/provider-accounts'
    assert anonymous.get(url, base_url=ORIGIN).status_code == 401
    assert operator.get(url, base_url=ORIGIN).status_code == 403
    promoted = post(admin, 'users/' + user['id'], {'role': 'admin', 'revision': user['revision']}).json['user']
    assert post(operator, 'login', {'email': 'operator@example.test', 'password': PASSWORD}).status_code == 200
    accepted = operator.post(url, base_url=ORIGIN, headers={'Origin': ORIGIN},
        json={'name': 'owned', 'client_token': 'owned-first'})
    assert accepted.status_code == 201, accepted.json
    repo = app.extensions['fleet']['services']['provider_accounts'].repository
    assert counts(repo) == (1, 1, 1)
    assert post(admin, 'users/' + user['id'], {'active': False, 'revision': promoted['revision']}).status_code == 200
    denied = operator.post(url, base_url=ORIGIN, headers={'Origin': ORIGIN},
        json={'name': 'owned', 'client_token': 'owned-first'})
    assert denied.status_code == 401
    assert counts(repo) == (1, 1, 1)


def test_revoked_operator_cannot_resolve_queued_response_or_confirm_ready(hub):
    identifier, flow = waiting(hub)
    result = operate(hub, identifier, 'submit_login', 'revoke-before-delivery', response='never-disclose-code', login_command_id=flow['command_id'])
    assert result.status_code == 202
    op = result.json['operation']
    dispatch(hub)
    hub[3].authorize = lambda actor: False
    response = hub[2].post('/api/supervisor/provider-accounts/resolve', headers=SUPERVISOR,
                          json={'command_id': op['command_id']})
    assert response.status_code == 403 and 'never-disclose-code' not in response.get_data(as_text=True)
    assert report(hub, op, 'ready').json == {'ok': True, 'accepted': False}
    with pytest.raises(ApplicationError) as error:
        hub[3].assert_ready(identifier, 'mac-local', ACTOR)
    assert error.value.code == 'forbidden'
