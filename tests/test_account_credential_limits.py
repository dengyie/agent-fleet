"""Only verified credential replacement clears that account's old attempt limits."""
import sqlite3

import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.http.errors import ApplicationError

ORIGIN = 'https://fleet.example.test'
EMAIL = 'admin@example.test'
PASSWORD = 'Review-password-123!'
NEW = 'Recovered-password-123!'


@pytest.fixture
def credentials(tmp_path):
    mail = []
    app = create_app(FleetConfig.from_root(tmp_path, ingest_token='test-machine-token', accounts={
        'enabled': True, 'origin': ORIGIN, 'sender': lambda *a: mail.append(a),
    }))
    repo = app.extensions['accounts']
    repo.bootstrap_admin(EMAIL, PASSWORD)
    return app, repo, mail


def post(client, action, data):
    return client.post('/api/accounts/' + action, json=data, base_url=ORIGIN, headers={'Origin': ORIGIN})


@pytest.mark.parametrize('action', ['reset', 'password'])
def test_verified_replacement_allows_new_password_after_lockout(credentials, action):
    app, repo, mail = credentials
    client, attacker = app.test_client(), app.test_client()
    assert post(client, 'login', {'email': EMAIL, 'password': PASSWORD}).status_code == 200
    for _ in range(9):
        assert post(attacker, 'login', {'email': EMAIL, 'password': 'wrong'}).status_code == 401
    assert post(attacker, 'login', {'email': EMAIL, 'password': PASSWORD}).status_code == 429
    user = repo.users()[0]
    for _ in range(10):
        repo.limit('login:other@example.test', 10, 900)
    for _ in range(9):
        repo.limit('password:' + user['id'], 10, 900)
    if action == 'reset':
        assert post(client, 'code', {'email': EMAIL, 'purpose': 'reset'}).status_code == 200
        data = {'email': EMAIL, 'code': mail[-1][2], 'password': NEW}
    else:
        data = {'old_password': PASSWORD, 'password': NEW}
    with repo.connect() as db:
        unaffected = [tuple(row) for row in db.execute('SELECT * FROM limits WHERE key LIKE "ip:%" OR key LIKE "mail-%" OR key=? ORDER BY key', ('login:other@example.test',))]
    assert post(client, action, data).status_code == 200
    with repo.connect() as db:
        assert db.execute('SELECT count(*) FROM limits WHERE key IN (?,?)',
                          ('login:' + EMAIL, 'password:' + user['id'])).fetchone()[0] == 0
        assert [tuple(row) for row in db.execute('SELECT * FROM limits WHERE key LIKE "ip:%" OR key LIKE "mail-%" OR key=? ORDER BY key', ('login:other@example.test',)) if not row['key'].endswith(':accounts.reset')] == unaffected
    assert post(client, 'login', {'email': EMAIL, 'password': NEW}).status_code == 200
    with pytest.raises(ApplicationError) as error:
        repo.login('other@example.test', PASSWORD)
    assert error.value.code == 'rate_limited'


@pytest.mark.parametrize('invalid', ['wrong', 'expired'])
def test_unverified_reset_cannot_clear_login_limit(credentials, invalid):
    app, repo, mail = credentials
    client = app.test_client()
    repo.limit('login:' + EMAIL, 1, 900)
    # Fill the real account limit, independently of the request IP limit.
    for _ in range(9):
        repo.limit('login:' + EMAIL, 10, 900)
    assert post(client, 'code', {'email': EMAIL, 'purpose': 'reset'}).status_code == 200
    code = mail[-1][2]
    if invalid == 'wrong':
        code = '000000' if code != '000000' else '111111'
    else:
        now = repo.clock()
        repo.clock = lambda: now + 601
    assert post(client, 'reset', {'email': EMAIL, 'code': code, 'password': NEW}).status_code == 400
    assert post(client, 'login', {'email': EMAIL, 'password': PASSWORD}).status_code == 429


@pytest.mark.parametrize('action', ['reset', 'password'])
def test_password_failure_rolls_back_limit_cleanup(credentials, action):
    _, repo, _ = credentials
    token, user = repo.login(EMAIL, PASSWORD)
    mail = []
    repo.issue_code(EMAIL, 'reset', 'invite', lambda *a: mail.append(a))
    repo.limit('password:' + user['id'], 10, 900)
    with repo.connect() as db:
        old_limits = [tuple(row) for row in db.execute('SELECT * FROM limits ORDER BY key')]
        old_challenge = tuple(db.execute('SELECT * FROM challenges').fetchone())
        db.execute('''CREATE TRIGGER fail_challenge_revoke BEFORE DELETE ON challenges
                      BEGIN SELECT RAISE(ABORT, 'fixture failure'); END''')
    with pytest.raises(sqlite3.IntegrityError):
        if action == 'reset':
            repo.complete_code(EMAIL, 'reset', mail[-1][2], NEW)
        else:
            repo.change_password(user['id'], PASSWORD, NEW)
    assert repo.identity(token)['id'] == user['id']
    # Authenticated-change attempts are reserved before the credential transaction.
    expected_limits = [(key, start, count + int(action == 'password' and key == 'password:' + user['id']))
                       for key, start, count in old_limits]
    with repo.connect() as db:
        assert [tuple(row) for row in db.execute('SELECT * FROM limits ORDER BY key')] == expected_limits
        assert tuple(db.execute('SELECT * FROM challenges').fetchone()) == old_challenge
    assert repo.login(EMAIL, PASSWORD)[1]['id'] == user['id']
