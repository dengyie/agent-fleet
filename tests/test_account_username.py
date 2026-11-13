import pytest

from hub.accounts.store import AccountStore
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.http.errors import ApplicationError

PASSWORD = 'Username-test-password!'


def test_username_and_email_use_same_identity_limits_and_revocation(tmp_path):
    store = AccountStore(tmp_path / 'accounts.db')
    store.bootstrap_admin('admin@example.test', PASSWORD, login_name='mango')
    token, user = store.login(' MANGO ', PASSWORD)
    other, same = store.login('admin@example.test', PASSWORD)
    assert user['id'] == same['id']
    assert user['role'] == 'admin'
    store.revoke(user['id'])
    assert store.identity(token) is None and store.identity(other) is None
    for n in range(8):
        with pytest.raises(ApplicationError) as error:
            store.login('mango' if n % 2 else 'admin@example.test', 'wrong')
        assert error.value.status == 401
    with pytest.raises(ApplicationError) as error:
        store.login('mango', PASSWORD)
    assert error.value.status == 429


def test_username_login_http_and_email_only_registration(tmp_path):
    origin = 'https://fleet.example.test'
    app = create_app(FleetConfig.from_root(tmp_path, accounts={'enabled': True, 'origin': origin}))
    store = app.extensions['accounts']
    store.bootstrap_admin('admin@example.test', PASSWORD, login_name='mango')
    client = app.test_client()
    def post(path, data):
        return client.post('/api/accounts/' + path, json=data, base_url=origin, headers={'Origin': origin})
    response = post('login', {'email': 'mango', 'password': PASSWORD})
    assert response.status_code == 200
    assert response.json['user']['role'] == 'admin'
    assert client.get('/api/accounts/users', base_url=origin).status_code == 200
    assert post('code', {'email': 'mango', 'purpose': 'register'}).status_code == 400
    assert post('logout', {}).status_code == 200
    assert client.get('/api/operator/session', base_url=origin).status_code == 401


def test_username_schema_upgrade_preserves_existing_account(tmp_path):
    import sqlite3
    path = tmp_path / 'accounts.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE users (id TEXT PRIMARY KEY,email TEXT UNIQUE NOT NULL,name TEXT NOT NULL,password TEXT NOT NULL,role TEXT NOT NULL,active INTEGER NOT NULL,created REAL NOT NULL,revision INTEGER NOT NULL DEFAULT 0)')
        db.execute("INSERT INTO users VALUES ('old','old@example.test','old','hash','user',1,1,4)")
    store = AccountStore(path)
    with store.connect() as db:
        row = db.execute("SELECT * FROM users WHERE id='old'").fetchone()
        assert row['username'] is None and row['password'] == 'hash' and row['revision'] == 4


def test_username_does_not_bypass_disabled_account(tmp_path):
    store = AccountStore(tmp_path / 'accounts.db')
    store.bootstrap_admin('admin@example.test', PASSWORD, login_name='mango')
    with store.connect() as db:
        db.execute("UPDATE users SET active=0 WHERE username='mango'")
    with pytest.raises(ApplicationError) as error:
        store.login('mango', PASSWORD)
    assert error.value.status == 401
