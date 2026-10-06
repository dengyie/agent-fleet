"""one-mail protocol contract: scoped credential, bounded response, no retry."""
import json
from unittest.mock import MagicMock, patch

import pytest
from hub.accounts.mail import OneMailSender


def connection(status=200, body=b'{"status":"ok"}'):
    conn = MagicMock()
    conn.getresponse.return_value.status = status
    conn.getresponse.return_value.read.return_value = body
    return conn


def test_one_mail_protocol_and_credential_scope():
    conn = connection()
    with patch('http.client.HTTPSConnection', return_value=conn) as factory:
        sender = OneMailSender(origin='https://mail.example.test', token='address-fixture', site_password='site-fixture')
        sender('user@example.test', 'register', '123456')
    args, kwargs = conn.request.call_args
    assert args == ('POST', '/api/send_mail')
    assert kwargs['headers']['Authorization'] == 'Bearer address-fixture'
    assert kwargs['headers']['x-custom-auth'] == 'site-fixture'
    assert 'x-admin-auth' not in kwargs['headers']
    assert len(kwargs['headers']['x-idempotency-key']) == 32
    body = json.loads(kwargs['body'])
    assert body['to_mail'] == 'user@example.test' and body['is_html'] is False
    assert '123456' in body['content'] and 'token' not in body
    assert factory.call_args.kwargs['timeout'] == 10
    conn.getresponse.return_value.read.assert_called_once_with(8193)
    conn.close.assert_called_once()


@pytest.mark.parametrize('status,body', [
    (302, b''), (401, b'private provider details'), (503, b'delivery unknown'),
    (200, b'<html>login</html>'), (200, b'{}'), (200, b'[]'), (200, b'x' * 8193),
])
def test_failure_does_not_retry_or_accept_ambiguous_delivery(status, body):
    conn = connection(status, body)
    with patch('http.client.HTTPSConnection', return_value=conn):
        with pytest.raises((RuntimeError, ValueError)):
            OneMailSender(origin='https://mail.example.test', token='address-fixture')('a@example.test', 'reset', '123456')
    conn.request.assert_called_once()
    conn.close.assert_called_once()


@pytest.mark.parametrize('origin', ['http://mail.example.test', 'https://user:pass@mail.example.test', 'https://mail.example.test/path', 'https://mail.example.test?token=x'])
def test_invalid_origin(origin):
    with pytest.raises(ValueError):
        OneMailSender(origin=origin, token='fixture')


def test_runtime_prefers_one_mail_and_requires_address_token(tmp_path, monkeypatch):
    from hub.bootstrap import create_app
    from hub.config import FleetConfig
    monkeypatch.setenv('AGENT_FLEET_ONEMAIL_ORIGIN', 'https://mail.example.test')
    monkeypatch.setenv('AGENT_FLEET_ONEMAIL_TOKEN', 'address-fixture')
    app = create_app(FleetConfig.from_root(tmp_path, accounts={'enabled': True, 'origin': 'https://fleet.example.test'}))
    assert isinstance(app.config['ACCOUNT_SENDER'], OneMailSender)
    assert app.config['ACCOUNT_REGISTRATION'] == 'invite'
    monkeypatch.delenv('AGENT_FLEET_ONEMAIL_TOKEN')
    with pytest.raises(ValueError, match='address JWT'):
        create_app(FleetConfig.from_root(tmp_path, accounts={'enabled': True, 'origin': 'https://fleet.example.test'}))
