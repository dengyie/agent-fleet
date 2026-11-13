"""Recovery grants and delivery quotas must follow account state transitions."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from hub.accounts import store as account_store
from hub.accounts.store import AccountStore
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.http.errors import ApplicationError

ORIGIN = 'https://fleet.example.test'
EMAIL = 'admin@example.test'
PASSWORD = 'Review-password-123!'
CHANGED = 'Changed-password-123!'
RESET = 'Reset-password-123!'


@pytest.fixture
def recovery(tmp_path):
    repo = AccountStore(tmp_path / 'accounts.db')
    now = [repo.clock()]
    repo.clock = lambda: now[0]
    repo.bootstrap_admin(EMAIL, PASSWORD)
    token, user = repo.login(EMAIL, PASSWORD)
    mail = []
    repo.issue_code(EMAIL, 'reset', 'invite', lambda *a: mail.append(a))
    return repo, now, token, user, mail[-1][2]


def test_password_change_revokes_pending_reset_code(recovery):
    repo, now, token, user, old_code = recovery
    repo.change_password(user['id'], PASSWORD, CHANGED)
    assert repo.identity(token) is None
    with pytest.raises(ApplicationError) as error:
        repo.complete_code(EMAIL, 'reset', old_code, RESET)
    assert error.value.code == 'invalid_code'
    assert repo.login(EMAIL, CHANGED)[1]['id'] == user['id']
    now[0] += 61
    mail = []
    repo.issue_code(EMAIL, 'reset', 'invite', lambda *a: mail.append(a))
    repo.complete_code(EMAIL, 'reset', mail[-1][2], RESET)
    assert repo.login(EMAIL, RESET)[1]['id'] == user['id']


def test_inflight_reset_cannot_overwrite_completed_password_change(recovery, monkeypatch):
    repo, _, _, user, old_code = recovery
    started, resume = Event(), Event()
    hash_password = account_store.password_hash

    def paused_hash(value):
        hashed = hash_password(value)
        if value == RESET:
            started.set()
            assert resume.wait(5)
        return hashed

    monkeypatch.setattr(account_store, 'password_hash', paused_hash)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(repo.complete_code, EMAIL, 'reset', old_code, RESET)
        try:
            assert started.wait(5)
            repo.change_password(user['id'], PASSWORD, CHANGED)
        finally:
            resume.set()
        with pytest.raises(ApplicationError) as error:
            pending.result(timeout=5)
        assert error.value.code == 'invalid_code'
    assert repo.login(EMAIL, CHANGED)[1]['id'] == user['id']


def test_delayed_mail_does_not_restore_revoked_reset_code(recovery):
    repo, now, _, user, _ = recovery
    now[0] += 61
    started, resume = Event(), Event()
    mail = []

    def delayed_sender(*args):
        mail.append(args)
        started.set()
        assert resume.wait(5)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(repo.issue_code, EMAIL, 'reset', 'invite', delayed_sender)
        try:
            assert started.wait(5)
            repo.change_password(user['id'], PASSWORD, CHANGED)
        finally:
            resume.set()
        pending.result(timeout=5)
    with pytest.raises(ApplicationError) as error:
        repo.complete_code(EMAIL, 'reset', mail[-1][2], RESET)
    assert error.value.code == 'invalid_code'


def test_ineligible_registration_cannot_exhaust_http_recovery_quota(tmp_path):
    mail = []
    app = create_app(FleetConfig.from_root(tmp_path, ingest_token='test-machine-token', accounts={
        'enabled': True, 'origin': ORIGIN, 'registration': 'invite',
        'sender': lambda *a: mail.append(a),
    }))
    repo = app.extensions['accounts']
    repo.bootstrap_admin(EMAIL, PASSWORD)
    now = [repo.clock()]
    repo.clock = lambda: now[0]
    client = app.test_client()

    def request_code(purpose):
        return client.post('/api/accounts/code', json={'email': EMAIL, 'purpose': purpose},
                           base_url=ORIGIN, headers={'Origin': ORIGIN})

    for _ in range(10):
        assert request_code('register').status_code == 200
        now[0] += 61
    assert mail == []
    result = request_code('reset')
    assert result.status_code == 200, result.json
    assert len(mail) == 1 and mail[0][:2] == (EMAIL, 'reset')


def test_ineligible_requests_do_not_delay_new_invitation(tmp_path):
    repo = AccountStore(tmp_path / 'accounts.db')
    mail = []
    send = lambda *args: mail.append(args)
    repo.issue_code(EMAIL, 'register', 'invite', send)
    repo.issue_code(EMAIL, 'reset', 'invite', send)
    assert mail == []
    repo.invite(EMAIL)
    repo.issue_code(EMAIL, 'register', 'invite', send)
    assert len(mail) == 1
    repo.complete_code(EMAIL, 'register', mail[0][2], PASSWORD)
    assert repo.login(EMAIL, PASSWORD)[1]['email'] == EMAIL


def test_concurrent_delivery_reserves_one_code(recovery):
    repo, now, _, user, _ = recovery
    now[0] += 61
    barrier = Barrier(2)
    mail = []

    def send():
        barrier.wait(timeout=5)
        try:
            repo.issue_code(EMAIL, 'reset', 'invite', lambda *a: mail.append(a))
            return 'ok'
        except ApplicationError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: send(), range(2)))
    assert sorted(results) == ['ok', 'rate_limited']
    assert len(mail) == 1
    repo.complete_code(EMAIL, 'reset', mail[0][2], RESET)
    assert repo.login(EMAIL, RESET)[1]['id'] == user['id']


def test_daily_rejection_does_not_reserve_a_new_cooldown(recovery):
    repo, now, _, _, _ = recovery
    day_start = now[0]
    mail = []
    for _ in range(9):
        now[0] += 61
        repo.issue_code(EMAIL, 'reset', 'invite', lambda *a: mail.append(a))
    now[0] = day_start + 86400 - 30
    with pytest.raises(ApplicationError) as error:
        repo.issue_code(EMAIL, 'reset', 'invite', lambda *a: mail.append(a))
    assert error.value.code == 'rate_limited'
    assert len(mail) == 9
    now[0] += 31
    repo.issue_code(EMAIL, 'reset', 'invite', lambda *a: mail.append(a))
    assert len(mail) == 10
