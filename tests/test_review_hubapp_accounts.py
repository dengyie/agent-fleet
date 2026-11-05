"""评审修复 #4/#5/#6/#10：Turnstile 必填、只读连接与身份缓存、(账户, IP)
登录限流、非账户模式写请求同源校验。"""
import sqlite3

import pytest
from flask import Flask, jsonify

from hub import auth
from hub.accounts.store import AccountStore
from hub.bootstrap import create_app
from hub.config import FleetConfig

ORIGIN = 'https://fleet.example.test'
EMAIL = 'admin@example.test'
PASSWORD = 'Review-password-123!'
ATTACKER = '203.0.113.66'
OWNER = '198.51.100.7'


def _app(tmp_path, **accounts):
    mail = []
    settings = {'enabled': True, 'origin': ORIGIN, 'registration': 'open',
                'sender': lambda *a: mail.append(a)}
    settings.update(accounts)
    app = create_app(FleetConfig.from_root(tmp_path, ingest_token='machine-token',
                                           accounts=settings))
    return app, mail


def _post(client, action, data, remote='127.0.0.1'):
    return client.post('/api/accounts/' + action, json=data, base_url=ORIGIN,
                       headers={'Origin': ORIGIN},
                       environ_overrides={'REMOTE_ADDR': remote})


# -- #4 Turnstile -----------------------------------------------------------

def test_turnstile_token_required_when_secret_configured(tmp_path):
    app, mail = _app(tmp_path, turnstile_secret='cf-secret')
    calls = []
    app.config['TURNSTILE_VERIFIER'] = lambda t, s, remote_ip=None: calls.append(t) or t == 'ok'
    client = app.test_client()
    resp = _post(client, 'code', {'email': 'new@example.test', 'purpose': 'register'})
    assert resp.status_code == 400 and resp.json['error'] == 'turnstile_required'
    assert mail == [] and calls == []
    resp = _post(client, 'code', {'email': 'new@example.test', 'purpose': 'register',
                                  'turnstile_response': 'ok'})
    assert resp.status_code == 200 and len(mail) == 1


def test_turnstile_not_required_without_secret(tmp_path):
    app, mail = _app(tmp_path)
    resp = _post(app.test_client(), 'code', {'email': 'new@example.test', 'purpose': 'register'})
    assert resp.status_code == 200 and len(mail) == 1


# -- #6 login limit ---------------------------------------------------------

def test_attacker_ip_exhausted_owner_from_other_ip_still_logs_in(tmp_path):
    app, _ = _app(tmp_path)
    repo = app.extensions['accounts']
    repo.bootstrap_admin(EMAIL, PASSWORD)
    attacker, owner = app.test_client(), app.test_client()
    for _ in range(repo.LOGIN_LIMIT):
        assert _post(attacker, 'login', {'email': EMAIL, 'password': 'wrong'},
                     remote=ATTACKER).status_code == 401
    # attacker is locked out even with the right password
    assert _post(attacker, 'login', {'email': EMAIL, 'password': PASSWORD},
                 remote=ATTACKER).status_code == 429
    # owner from a different IP is unaffected
    assert _post(owner, 'login', {'email': EMAIL, 'password': PASSWORD},
                 remote=OWNER).status_code == 200


def test_account_soft_limit_still_caps_distributed_guessing(tmp_path):
    repo = AccountStore(tmp_path / 'accounts.db')
    repo.bootstrap_admin(EMAIL, PASSWORD)
    for _ in range(repo.LOGIN_ACCOUNT_LIMIT):
        repo.limit('login:' + EMAIL, repo.LOGIN_ACCOUNT_LIMIT, 900)
    with pytest.raises(Exception) as error:
        repo.login(EMAIL, PASSWORD, client=OWNER)
    assert error.value.code == 'rate_limited'
    assert repo.LOGIN_ACCOUNT_LIMIT > repo.LOGIN_LIMIT


# -- #5 read connections / identity cache / SSE revalidation ---------------

def test_read_paths_do_not_take_the_write_lock(tmp_path):
    repo = AccountStore(tmp_path / 'accounts.db')
    repo.bootstrap_admin(EMAIL, PASSWORD)
    token, user = repo.login(EMAIL, PASSWORD)
    blocker = sqlite3.connect(tmp_path / 'accounts.db', timeout=0)
    blocker.execute('BEGIN IMMEDIATE')  # 持有 RESERVED 写锁
    try:
        repo_fast = AccountStore.__new__(AccountStore)
        repo_fast.__dict__.update(repo.__dict__)
        assert repo_fast.identity(token)['id'] == user['id']
        assert repo_fast.is_active(user['id'])
        assert [u['id'] for u in repo_fast.users()] == [user['id']]
        assert repo_fast.sessions(user['id'], token)[0]['current']
    finally:
        blocker.rollback()
        blocker.close()


def test_identity_is_cached_per_request(tmp_path):
    app, _ = _app(tmp_path)
    repo = app.extensions['accounts']
    repo.bootstrap_admin(EMAIL, PASSWORD)
    client = app.test_client()
    assert _post(client, 'login', {'email': EMAIL, 'password': PASSWORD}).status_code == 200
    calls = []
    original = repo.identity
    repo.identity = lambda token: calls.append(token) or original(token)
    # boundary + require_operator 都解析身份，但每个请求只查一次库
    resp = client.get('/api/operator/session', base_url=ORIGIN)
    assert resp.status_code == 200
    assert len(calls) == 1


def test_stream_revalidates_on_interval_not_per_chunk(tmp_path):
    from flask import Response
    app, _ = _app(tmp_path)
    repo = app.extensions['accounts']
    repo.bootstrap_admin(EMAIL, PASSWORD)

    @app.get('/api/test-stream')
    def test_stream():
        return Response((b'x' for _ in range(50)), mimetype='text/event-stream')

    client = app.test_client()
    assert _post(client, 'login', {'email': EMAIL, 'password': PASSWORD}).status_code == 200
    calls = []
    original = repo.identity
    repo.identity = lambda token: calls.append(token) or original(token)
    result = client.get('/api/test-stream', base_url=ORIGIN)
    assert result.data == b'x' * 50
    assert len(calls) == 1  # boundary only; 30s interval not reached


# -- #10 non-account mode Origin check --------------------------------------

def _operator_app(**config):
    app = Flask(__name__)
    app.config.update(DEV_OPERATOR='dev@example.com', **config)

    @app.route('/api/thing', methods=['GET', 'POST'])
    @auth.require_operator
    def thing():
        return jsonify(ok=True)
    return app


@pytest.mark.parametrize('headers,status', [
    ({}, 200),                                                    # curl / script
    ({'Origin': 'http://localhost'}, 200),                        # same host
    ({'Origin': 'https://localhost'}, 200),                       # TLS at proxy
    ({'Origin': 'https://evil.example'}, 403),
    ({'Origin': 'null'}, 403),
    ({'Sec-Fetch-Site': 'cross-site'}, 403),
    ({'Sec-Fetch-Site': 'same-origin'}, 200),
])
def test_operator_writes_require_same_origin(headers, status):
    client = _operator_app().test_client()
    resp = client.post('/api/thing', headers=headers)
    assert resp.status_code == status
    if status == 403:
        assert resp.json['error'] == 'invalid_origin'


def test_operator_reads_and_allowlisted_origin():
    app = _operator_app(OPERATOR_ALLOWED_ORIGINS=['https://console.example'])
    client = app.test_client()
    assert client.get('/api/thing', headers={'Origin': 'https://evil.example'}).status_code == 200
    assert client.post('/api/thing', headers={'Origin': 'https://console.example'}).status_code == 200


def test_cf_access_identity_cross_site_post_rejected():
    client = _operator_app().test_client()
    resp = client.post('/api/thing', headers={
        'Cf-Access-Authenticated-User-Email': 'op@example.com',
        'Origin': 'https://evil.example'})
    assert resp.status_code == 403
