"""Email eligibility must not select public status codes or response bodies."""
import json

import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig

ORIGIN = 'https://fleet.example.test'
PASSWORD = 'Review-password-123!'


@pytest.fixture
def public_codes(tmp_path):
    mail = []
    app = create_app(FleetConfig.from_root(tmp_path, ingest_token='test-machine-token', accounts={
        'enabled': True, 'origin': ORIGIN, 'sender': lambda *a: mail.append(a),
    }))
    repo = app.extensions['accounts']
    repo.bootstrap_admin('active@example.test', PASSWORD)
    admin = repo.users()[0]
    delivered = []
    repo.invite('disabled@example.test')
    repo.issue_code('disabled@example.test', 'register', 'invite', lambda *a: delivered.append(a))
    repo.complete_code('disabled@example.test', 'register', delivered[-1][2], PASSWORD)
    disabled = next(user for user in repo.users() if user['role'] == 'user')
    repo.manage(admin['id'], disabled['id'], changes={'active': False}, expected_revision=disabled['revision'])
    repo.invite('invited@example.test')
    now = [repo.clock()]
    repo.clock = lambda: now[0]
    return app, repo, mail, now


def request_code(client, email, purpose, *, remote='198.51.100.10'):
    return client.post('/api/accounts/code', json={'email': email, 'purpose': purpose},
                       base_url=ORIGIN, headers={'Origin': ORIGIN},
                       environ_overrides={'REMOTE_ADDR': remote})


@pytest.mark.parametrize('purpose', ['register', 'reset'])
def test_cooldown_responses_do_not_reveal_eligibility(public_codes, purpose):
    app, _, mail, _ = public_codes
    client = app.test_client()
    baseline = request_code(client, 'unknown@example.test', purpose)
    assert baseline.status_code == 200 and baseline.json['ok'] is True
    for email in ('active@example.test', 'disabled@example.test', 'invited@example.test', 'unknown@example.test'):
        for _ in range(2):
            result = request_code(client, email, purpose)
            assert (result.status_code, result.json) == (baseline.status_code, baseline.json), email
            assert result.headers['Cache-Control'] == 'no-store'
    assert len(mail) == 1
    assert mail[0][:2] == (('active@example.test' if purpose == 'reset' else 'invited@example.test'), purpose)


def test_daily_quota_is_private_and_counts_only_delivery_attempts(public_codes):
    app, repo, mail, now = public_codes
    client = app.test_client()
    baseline = request_code(client, 'unknown@example.test', 'reset')
    first_code = None
    for index in range(11):
        result = request_code(client, 'active@example.test', 'reset')
        assert (result.status_code, result.json) == (200, baseline.json)
        if index == 0:
            first_code = mail[0][2]
            # A private cooldown must not replace or invalidate the first code.
            assert request_code(client, 'active@example.test', 'reset').json == baseline.json
            repo.complete_code('active@example.test', 'reset', first_code, PASSWORD)
        now[0] += 61
    assert len(mail) == 10
    with repo.connect() as db:
        rows = dict(db.execute('SELECT key,count FROM limits WHERE key LIKE "mail-day:%"'))
    assert rows['mail-day:active@example.test'] == 10
    assert 'mail-day:unknown@example.test' not in rows


def test_sender_failure_keeps_public_response_and_safe_diagnostics(public_codes, caplog):
    app, repo, _, _ = public_codes
    secret = 'sensitive-provider-body-fixture'

    def failed_sender(*args):
        raise TimeoutError(secret)

    app.config['ACCOUNT_SENDER'] = failed_sender
    client = app.test_client()
    baseline = request_code(client, 'unknown@example.test', 'reset')
    result = request_code(client, 'active@example.test', 'reset')
    assert (result.status_code, result.json) == (200, baseline.json)
    assert secret not in caplog.text and 'active@example.test' not in caplog.text
    failures = [json.loads(record.message) for record in caplog.records if 'account_mail_delivery_failed' in record.message]
    assert len(failures) == 1 and failures[0]['exception_type'] == 'TimeoutError'
    with repo.connect() as db:
        assert db.execute('SELECT count(*) FROM challenges WHERE email=?', ('active@example.test',)).fetchone()[0] == 0


def test_ip_limit_applies_equally_without_eligibility(public_codes):
    app, _, mail, _ = public_codes
    client = app.test_client()
    for _ in range(60):
        assert request_code(client, 'unknown@example.test', 'reset').status_code == 200
    for email in ('unknown@example.test', 'active@example.test'):
        result = request_code(client, email, 'reset')
        assert result.status_code == 429 and result.json['error'] == 'rate_limited'
    assert mail == []
    assert request_code(client, 'active@example.test', 'reset', remote='198.51.100.20').status_code == 200
    assert len(mail) == 1


def test_global_configuration_and_unexpected_failures_are_not_acknowledged(public_codes, monkeypatch):
    app, repo, _, _ = public_codes
    client = app.test_client()
    app.config['ACCOUNT_SENDER'] = None
    for email in ('unknown@example.test', 'active@example.test'):
        assert request_code(client, email, 'reset').status_code == 503
    app.config['ACCOUNT_SENDER'] = lambda *a: None

    def broken_store(*args):
        raise RuntimeError('unexpected store error')

    monkeypatch.setattr(repo, 'issue_code', broken_store)
    assert request_code(client, 'active@example.test', 'reset').status_code == 500
