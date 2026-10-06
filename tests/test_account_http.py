import pytest
from hub.bootstrap import create_app
from hub.config import FleetConfig

PASSWORD = 'safe-password-123!'
ORIGIN = 'https://fleet.example.test'

@pytest.fixture
def account_app(tmp_path):
    mail = []
    app = create_app(FleetConfig.from_root(tmp_path, platform_enabled=True,
        accounts={'enabled': True, 'origin': ORIGIN, 'registration': 'open',
                  'sender': lambda *a: mail.append(a)}))
    app.testing = True
    return app, mail


def post(client, path, data, origin=ORIGIN):
    return client.post('/api/accounts/' + path, json=data, headers={'Origin': origin}, base_url=ORIGIN)


def signup(client, mail, email):
    assert post(client, 'code', {'email': email, 'purpose': 'register'}).status_code == 200
    assert post(client, 'register', {'email': email, 'code': mail[-1][2], 'password': PASSWORD}).status_code == 200
    return post(client, 'login', {'email': email, 'password': PASSWORD})


def test_full_registration_login_profile_logout(account_app):
    app, mail = account_app
    client = app.test_client()
    assert client.get('/api/operator/session', base_url=ORIGIN).status_code == 401
    response = signup(client, mail, 'alice@example.test')
    assert response.status_code == 200
    cookie = response.headers['Set-Cookie']
    assert cookie.startswith('__Host-fleet_session=')
    assert 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=Lax' in cookie
    assert response.headers['Cache-Control'] == 'no-store'
    assert client.get('/api/operator/session', base_url=ORIGIN).json['user']['role'] == 'user'
    assert post(client, 'profile', {'name': 'Alice'}).status_code == 200
    assert client.get('/api/accounts/me', base_url=ORIGIN).json['user']['name'] == 'Alice'
    assert post(client, 'logout', {}).status_code == 200
    assert client.get('/api/accounts/me', base_url=ORIGIN).status_code == 401


def test_csrf_edge_identity_and_role_boundaries(account_app):
    app, mail = account_app
    client = app.test_client()
    assert post(client, 'login', {}, 'https://evil.test').status_code == 403
    assert client.post('/api/accounts/login', json={}).status_code == 403
    assert client.get('/api/operator/session', headers={'Cf-Access-Authenticated-User-Email': 'admin@example.test'}, base_url=ORIGIN).status_code == 401
    signup(client, mail, 'alice@example.test')
    for url in ['/api/status', '/api/tasks', '/api/stream', '/api/accounts/users', '/api/sessions', '/api/platform/v1/nodes']:
        assert client.get(url, base_url=ORIGIN).status_code in (403, 404), url
    assert post(client, 'invitations', {'email': 'other@example.test'}).status_code == 403
    assert client.get('/api/operator/session', headers={'X-Runner-Credential': 'bad'}, base_url=ORIGIN).status_code == 401


def test_platform_owners_are_distinct(account_app):
    app, mail = account_app
    a, b = app.test_client(), app.test_client()
    user_a = signup(a, mail, 'a@example.test').json['user']
    user_b = signup(b, mail, 'b@example.test').json['user']
    assert user_a['id'] != user_b['id']
    repo = app.extensions['fleet']['platform_repository']
    repo.create_conversation(user_a['id'], 'private', title='private', workspace_id=None)
    assert a.get('/api/platform/v1/conversations/private', base_url=ORIGIN).status_code == 200
    assert b.get('/api/platform/v1/conversations/private', base_url=ORIGIN).status_code == 404


def test_administrator_controls_and_revocation(account_app):
    app, mail = account_app
    app.extensions['accounts'].bootstrap_admin('admin@example.test', PASSWORD)
    admin, user = app.test_client(), app.test_client()
    assert post(admin, 'login', {'email': 'admin@example.test', 'password': PASSWORD}).status_code == 200
    # Test admin invitations creation & list
    inv_res = post(admin, 'invitations', {})
    assert inv_res.status_code == 200 and 'code' in inv_res.json
    code = inv_res.json['code']
    list_res = admin.get('/api/accounts/invitations', base_url=ORIGIN)
    assert list_res.status_code == 200 and len(list_res.json['invitations']) >= 1
    assert any(i['code'] == code for i in list_res.json['invitations'])
    person = signup(user, mail, 'a@example.test').json['user']
    assert admin.get('/api/accounts/users', base_url=ORIGIN).status_code == 200
    assert post(admin, 'users/' + person['id'], {'active': False, 'revision': person['revision']}).status_code == 200
    assert user.get('/api/operator/session', base_url=ORIGIN).status_code == 401


def test_account_mode_off_leaves_legacy_identity(tmp_path):
    app = create_app(FleetConfig.from_root(tmp_path, dev_operator='legacy@example.test'))
    client = app.test_client()
    assert client.get('/api/operator/session').status_code == 200
    assert client.get('/api/accounts/options').status_code == 404


def test_stream_stops_after_session_revocation(account_app):
    from flask import Response
    app, _ = account_app
    app.extensions['accounts'].bootstrap_admin('admin@example.test', PASSWORD)
    @app.get('/api/test-stream')
    def test_stream():
        def chunks():
            yield b'first'
            app.extensions['accounts'].revoke(user_id)
            yield b'must-not-leak'
        return Response(chunks(), mimetype='text/event-stream')
    client = app.test_client()
    response = post(client, 'login', {'email': 'admin@example.test', 'password': PASSWORD})
    user_id = response.json['user']['id']
    result = client.get('/api/test-stream', base_url=ORIGIN)
    assert result.data == b'first'


def test_password_reset_invalidates_http_session(account_app):
    app, mail = account_app
    client = app.test_client()
    signup(client, mail, 'a@example.test')
    store = app.extensions['accounts']
    now = store.clock()
    store.clock = lambda: now + 61
    assert post(client, 'code', {'email': 'a@example.test', 'purpose': 'reset'}).status_code == 200
    other = app.test_client()
    assert post(other, 'reset', {'email': 'a@example.test', 'code': mail[-1][2], 'password': 'new-password-123!'}).status_code == 200
    assert client.get('/api/operator/session', base_url=ORIGIN).status_code == 401
    assert post(other, 'login', {'email': 'a@example.test', 'password': 'new-password-123!'}).status_code == 200
