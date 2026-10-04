"""Tests for email verification code CAPTCHA protection (Cloudflare Turnstile and progressive hCaptcha)."""
import pytest
from hub.bootstrap import create_app
from hub.config import FleetConfig

PASSWORD = 'safe-password-123!'
ORIGIN = 'https://fleet.example.test'
HCAPTCHA_SITEKEY = 'test-hcaptcha-sitekey'
HCAPTCHA_SECRET = 'test-hcaptcha-secret-dummy'


@pytest.fixture
def captcha_app(tmp_path):
    mail = []
    # Mock captchas verifier
    mock_hcaptcha_calls = []
    mock_turnstile_calls = []

    def mock_hcaptcha(token, secret, remote_ip=None):
        mock_hcaptcha_calls.append((token, secret, remote_ip))
        return token == 'valid-hcaptcha-token'

    def mock_turnstile(token, secret, remote_ip=None):
        mock_turnstile_calls.append((token, secret, remote_ip))
        return token == 'valid-turnstile-token'

    app = create_app(FleetConfig.from_root(
        tmp_path,
        platform_enabled=True,
        accounts={
            'enabled': True,
            'origin': ORIGIN,
            'registration': 'open',
            'sender': lambda *a: mail.append(a),
            'hcaptcha_sitekey': HCAPTCHA_SITEKEY,
            'hcaptcha_secret': HCAPTCHA_SECRET,
            'turnstile_sitekey': 'cf_test_sitekey',
            'turnstile_secret': 'cf_test_secret',
        }
    ))
    app.config['HCAPTCHA_VERIFIER'] = mock_hcaptcha
    app.config['TURNSTILE_VERIFIER'] = mock_turnstile
    app.testing = True
    return app, mail, mock_hcaptcha_calls, mock_turnstile_calls


def post(client, path, data, origin=ORIGIN):
    return client.post('/api/accounts/' + path, json=data, headers={'Origin': origin}, base_url=ORIGIN)


def test_options_exposes_captcha_keys(captcha_app):
    app, _, _, _ = captcha_app
    client = app.test_client()
    resp = client.get('/api/accounts/options', base_url=ORIGIN)
    assert resp.status_code == 200
    data = resp.json
    assert data['hcaptcha_sitekey'] == HCAPTCHA_SITEKEY
    assert data['turnstile_sitekey'] == 'cf_test_sitekey'


def test_turnstile_verification_when_provided(captcha_app):
    app, mail, _, turnstile_calls = captcha_app
    client = app.test_client()
    email = 'user_ts@example.test'

    # Fail with invalid turnstile response
    resp = post(client, 'code', {
        'email': email,
        'purpose': 'register',
        'turnstile_response': 'invalid-token',
    })
    assert resp.status_code == 400
    assert resp.json['error'] == 'invalid_turnstile'
    assert len(mail) == 0

    # Succeed with valid turnstile response
    resp = post(client, 'code', {
        'email': email,
        'purpose': 'register',
        'turnstile_response': 'valid-turnstile-token',
    })
    assert resp.status_code == 200
    assert len(mail) == 1
    assert len(turnstile_calls) == 2


def test_progressive_hcaptcha_after_three_attempts(captcha_app):
    app, mail, hcaptcha_calls, _ = captcha_app
    client = app.test_client()

    # Attempt 1: succeeds without hCaptcha (email 1)
    resp1 = post(client, 'code', {'email': 'user1@example.test', 'purpose': 'register'})
    assert resp1.status_code == 200
    assert len(mail) == 1

    # Attempt 2: succeeds without hCaptcha (email 2)
    resp2 = post(client, 'code', {'email': 'user2@example.test', 'purpose': 'register'})
    assert resp2.status_code == 200
    assert len(mail) == 2

    # Attempt 3: succeeds without hCaptcha (email 3, count reaches 3 upon completion)
    resp3 = post(client, 'code', {'email': 'user3@example.test', 'purpose': 'register'})
    assert resp3.status_code == 200
    assert len(mail) == 3

    # Attempt 4 (3+ consecutive attempts from this client IP): blocked by captcha_required if token missing
    resp4_blocked = post(client, 'code', {'email': 'user4@example.test', 'purpose': 'register'})
    assert resp4_blocked.status_code == 400
    assert resp4_blocked.json['error'] == 'captcha_required'
    assert len(mail) == 3  # No new mail dispatched

    # Attempt 5: blocked if invalid hCaptcha token
    resp5_invalid = post(client, 'code', {
        'email': 'user5@example.test',
        'purpose': 'register',
        'hcaptcha_response': 'bad-token'
    })
    assert resp5_invalid.status_code == 400
    assert resp5_invalid.json['error'] == 'invalid_captcha'
    assert len(mail) == 3

    # Attempt 6: succeeds when valid hCaptcha response supplied
    resp6_success = post(client, 'code', {
        'email': 'user6@example.test',
        'purpose': 'register',
        'hcaptcha_response': 'valid-hcaptcha-token'
    })
    assert resp6_success.status_code == 200
    assert len(mail) == 4
    assert len(hcaptcha_calls) == 2
    assert hcaptcha_calls[-1][1] == HCAPTCHA_SECRET


def test_invite_code_bound_to_first_email_and_rate_limited(tmp_path):
    mail = []
    app = create_app(FleetConfig.from_root(
        tmp_path,
        platform_enabled=True,
        accounts={
            'enabled': True,
            'origin': ORIGIN,
            'registration': 'invite',
            'sender': lambda *a: mail.append(a),
        }
    ))
    repo = app.extensions['accounts']
    client = app.test_client()

    # Generate generic invite code
    code = repo.invite()

    # Request code for userA
    resp_a = post(client, 'code', {
        'email': 'userA@example.test',
        'purpose': 'register',
        'invite_code': code,
    })
    assert resp_a.status_code == 200
    assert len(mail) == 1
    assert mail[-1][0] == 'usera@example.test'

    # Try requesting code for userB using the same invite code (must not deliver code for userB)
    resp_b = post(client, 'code', {
        'email': 'userb@example.test',
        'purpose': 'register',
        'invite_code': code,
    })
    # Eligibility fails quietly or reports
    assert resp_b.status_code == 200
    # Crucial: No mail should be sent to userB because the invite was bound to userA
    assert len(mail) == 1

    # Verify atomic counter increment behavior
    c1 = repo.check_and_increment_limit('test-atomic-key', 60)
    assert c1 == 0
    c2 = repo.check_and_increment_limit('test-atomic-key', 60)
    assert c2 == 1
    c3 = repo.check_and_increment_limit('test-atomic-key', 60)
    assert c3 == 2

