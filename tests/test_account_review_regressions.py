"""Production review regressions: proxy identity and stale admin snapshots."""
import pytest
from hub.bootstrap import create_app
from hub.config import FleetConfig

ORIGIN = 'https://fleet.example.test'
PASSWORD = 'Review-password-123!'


def make_app(tmp_path, *, trusted=()):
    mail = []
    app = create_app(FleetConfig.from_root(tmp_path, accounts={
        'enabled': True, 'origin': ORIGIN, 'registration': 'invite',
        'trusted_proxies': trusted, 'sender': lambda *a: mail.append(a),
    }))
    app.extensions['accounts'].bootstrap_admin('admin@example.test', PASSWORD)
    return app, mail


def post(client, action, data, *, forwarded='198.51.100.10', remote='127.0.0.1'):
    return client.post('/api/accounts/' + action, json=data, base_url=ORIGIN,
                       headers={'Origin': ORIGIN, 'X-Forwarded-For': forwarded},
                       environ_overrides={'REMOTE_ADDR': remote})


def test_proxy_attack_does_not_lock_out_other_clients(tmp_path):
    app, _ = make_app(tmp_path, trusted=('127.0.0.1/32',))
    attacker, victim = app.test_client(), app.test_client()
    for index in range(60):
        post(attacker, 'login', {'email': f'bad{index}@example.test', 'password': 'wrong-password'})
    blocked = post(attacker, 'login', {'email': 'another@example.test', 'password': 'wrong-password'})
    assert blocked.status_code == 429
    response = post(victim, 'login', {'email': 'admin@example.test', 'password': PASSWORD}, forwarded='198.51.100.20')
    assert response.status_code == 200, response.json


def test_login_ip_limit_does_not_consume_recovery_quota(tmp_path):
    app, mail = make_app(tmp_path, trusted=('127.0.0.1/32',))
    client = app.test_client()
    for index in range(60):
        post(client, 'login', {'email': f'bad{index}@example.test', 'password': 'wrong-password'})
    result = post(client, 'code', {'email': 'admin@example.test', 'purpose': 'reset'})
    assert result.status_code == 200, result.json
    assert len(mail) == 1


def test_stale_promotion_cannot_reactivate_disabled_user(tmp_path):
    app, mail = make_app(tmp_path)
    repo = app.extensions['accounts']
    repo.invite('user@example.test')
    repo.issue_code('user@example.test', 'register', 'invite', lambda *a: mail.append(a))
    repo.complete_code('user@example.test', 'register', mail[-1][2], PASSWORD)
    a, b = app.test_client(), app.test_client()
    for client in (a, b):
        assert post(client, 'login', {'email': 'admin@example.test', 'password': PASSWORD}).status_code == 200
    user = next(u for u in a.get('/api/accounts/users', base_url=ORIGIN).json['users'] if u['role'] == 'user')
    revision = user.get('revision', 0)
    assert post(b, 'users/' + user['id'], {'role': 'user', 'active': False, 'revision': revision}).status_code == 200
    response = post(a, 'users/' + user['id'], {'role': 'admin', 'active': True, 'revision': revision})
    assert response.status_code == 409, response.json
    saved = next(u for u in repo.users() if u['id'] == user['id'])
    assert saved['active'] == 0 and saved['role'] == 'user'


def test_untrusted_forwarding_headers_cannot_evade_limits(tmp_path):
    app, _ = make_app(tmp_path, trusted=('10.0.0.1/32',))
    client = app.test_client()
    for index in range(60):
        post(client, 'login', {'email': f'bad{index}@example.test', 'password': 'wrong-password'},
             remote='198.51.100.5', forwarded=f'203.0.113.{index + 1}')
    result = post(client, 'login', {'email': 'admin@example.test', 'password': PASSWORD},
                  remote='198.51.100.5', forwarded='203.0.113.200')
    assert result.status_code == 429


@pytest.mark.parametrize('remote,forwarded,networks,expected', [
    ('10.0.0.1', '203.0.113.99, 198.51.100.5, 10.0.0.2', ('10.0.0.0/24',), '198.51.100.5'),
    ('2001:db8::1', '2001:db8:1::7', ('2001:db8::1/128',), '2001:db8:1::7'),
    ('198.51.100.5', '203.0.113.99', (), '198.51.100.5'),
])
def test_proxy_chain_stops_at_first_untrusted_hop(remote, forwarded, networks, expected):
    from types import SimpleNamespace
    from hub.accounts.client_address import client_address, proxy_networks
    request = SimpleNamespace(remote_addr=remote, headers={'X-Forwarded-For': forwarded})
    assert client_address(request, proxy_networks(networks)) == expected


@pytest.mark.parametrize('forwarded', ['', 'garbage', '10.0.0.2', '198.51.100.5,' * 17])
def test_trusted_proxy_requires_valid_bounded_client_chain(forwarded):
    from types import SimpleNamespace
    from hub.accounts.client_address import client_address, proxy_networks
    from hub.http.errors import ApplicationError
    request = SimpleNamespace(remote_addr='10.0.0.1', headers={'X-Forwarded-For': forwarded})
    with pytest.raises(ApplicationError) as error:
        client_address(request, proxy_networks(('10.0.0.0/24',)))
    assert error.value.code == 'invalid_client_address'


def test_partial_changes_preserve_other_field_and_require_revision(tmp_path):
    app, mail = make_app(tmp_path)
    repo = app.extensions['accounts']
    repo.invite('user@example.test')
    repo.issue_code('user@example.test', 'register', 'invite', lambda *a: mail.append(a))
    repo.complete_code('user@example.test', 'register', mail[-1][2], PASSWORD)
    client = app.test_client()
    post(client, 'login', {'email': 'admin@example.test', 'password': PASSWORD})
    user = next(u for u in repo.users() if u['role'] == 'user')
    url = 'users/' + user['id']
    assert post(client, url, {'active': False}).status_code == 428
    disabled = post(client, url, {'active': False, 'revision': user['revision']}).json['user']
    promoted = post(client, url, {'role': 'admin', 'revision': disabled['revision']}).json['user']
    assert promoted['active'] == 0 and promoted['role'] == 'admin'
    enabled = post(client, url, {'active': True, 'revision': promoted['revision']}).json['user']
    assert enabled['role'] == 'admin' and enabled['revision'] == user['revision'] + 3
    assert post(client, url, {'revision': enabled['revision'], 'email': 'new@example.test'}).status_code == 400


def test_concurrent_updates_have_one_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from hub.http.errors import ApplicationError
    app, mail = make_app(tmp_path)
    repo = app.extensions['accounts']
    admin = repo.users()[0]
    repo.invite('user@example.test')
    repo.issue_code('user@example.test', 'register', 'invite', lambda *a: mail.append(a))
    repo.complete_code('user@example.test', 'register', mail[-1][2], PASSWORD)
    user = next(u for u in repo.users() if u['role'] == 'user')
    barrier = Barrier(2)
    def update(changes):
        barrier.wait()
        try:
            repo.manage(admin['id'], user['id'], changes=changes, expected_revision=user['revision'])
            return 'ok'
        except ApplicationError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(update, [{'role': 'admin'}, {'active': False}]))
    assert sorted(outcomes) == ['ok', 'revision_conflict']
    current = next(u for u in repo.users() if u['id'] == user['id'])
    assert current['revision'] == user['revision'] + 1


def test_simultaneous_admin_demotion_preserves_one_admin(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from hub.http.errors import ApplicationError
    app, mail = make_app(tmp_path)
    repo = app.extensions['accounts']; first = repo.users()[0]
    repo.invite('second@example.test')
    repo.issue_code('second@example.test', 'register', 'invite', lambda *a: mail.append(a))
    repo.complete_code('second@example.test', 'register', mail[-1][2], PASSWORD)
    second = next(u for u in repo.users() if u['role'] == 'user')
    second = repo.manage(first['id'], second['id'], changes={'role': 'admin'}, expected_revision=second['revision'])
    barrier = Barrier(2)
    def demote(user):
        barrier.wait()
        try:
            repo.manage(user['id'], user['id'], changes={'role': 'user'}, expected_revision=user['revision'])
            return 'ok'
        except ApplicationError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(demote, [first, second]))
    assert sorted(outcomes) == ['last_admin', 'ok']
    assert len([u for u in repo.users() if u['role'] == 'admin' and u['active']]) == 1


def test_existing_account_database_migrates_without_identity_loss(tmp_path):
    import sqlite3
    from werkzeug.security import generate_password_hash
    from hub.accounts.store import AccountStore, digest
    path = tmp_path / 'old.db'
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE users (
            id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
            password TEXT NOT NULL, role TEXT NOT NULL, active INTEGER NOT NULL, created REAL NOT NULL);
            CREATE TABLE sessions(token TEXT PRIMARY KEY,user_id TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL);''')
        db.execute('INSERT INTO users VALUES (?,?,?,?,?,?,?)', ('acct_old', 'old@example.test', 'Old', generate_password_hash(PASSWORD), 'admin', 1, 100))
        db.execute('INSERT INTO sessions VALUES (?,?,?,?)', (digest('fixture-session'), 'acct_old', 100, 1000))
    repo = AccountStore(path, clock=lambda: 200)
    assert repo.identity('fixture-session')['id'] == 'acct_old'
    assert repo.identity('fixture-session')['revision'] == 0
    assert repo.login('old@example.test', PASSWORD)[1]['id'] == 'acct_old'
    assert AccountStore(path, clock=lambda: 200).identity('fixture-session')['id'] == 'acct_old'
