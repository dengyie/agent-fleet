"""Bounded AGY account protocol shared by Hub and node; no storage or I/O."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

ACCOUNT_RE = re.compile(r'pa_[a-f0-9]{32}\Z')
CLIENT_TOKEN_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z')
OPERATIONS = frozenset({'create', 'login', 'submit_login', 'cancel_login', 'enable', 'disable', 'logout'})
ACCOUNT_STATES = frozenset({'signed_out', 'ready', 'disabled', 'starting', 'waiting', 'verifying', 'failed'})
AUTH_TTL_S = 300
MAX_AUTHORIZATION_URL = 8192
_CODE_REDIRECT = 'https://antigravity.google/oauth-callback'


def account_id(value):
    if not isinstance(value, str) or not ACCOUNT_RE.fullmatch(value):
        raise ValueError('invalid_provider_account_id')
    return value


def client_token(value):
    if not isinstance(value, str) or not CLIENT_TOKEN_RE.fullmatch(value):
        raise ValueError('invalid_client_token')
    return value


def _url(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= MAX_AUTHORIZATION_URL
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError('invalid_authorization_url')
    try:
        parsed = urlsplit(value)
        # Accessing port performs urllib's malformed-port validation.
        if parsed.port is not None:
            raise ValueError('invalid_authorization_url')
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True,
                          max_num_fields=64)
    except (ValueError, UnicodeError):
        raise ValueError('invalid_authorization_url') from None
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError('invalid_authorization_url')
    query = {}
    for key, item in pairs:
        if key in query:
            raise ValueError('invalid_authorization_url')
        query[key] = item
    return parsed, query


def authorization_url(value):
    """Accept only AGY's remote code flow, never arbitrary browser/loopback URLs."""
    parsed, query = _url(value)
    state = query.get('state')
    if (parsed.scheme != 'https' or parsed.netloc != 'accounts.google.com'
            or parsed.path != '/o/oauth2/auth'
            or query.get('redirect_uri') != _CODE_REDIRECT
            or query.get('response_type') != 'code'
            or not state or len(state) > 512 or any(c.isspace() or ord(c) < 32 for c in state)):
        raise ValueError('invalid_authorization_url')
    return value


def login_response(url, value):
    """Bind a pasted callback to this flow; raw one-line native codes are allowed."""
    authorization_url(url)
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_AUTHORIZATION_URL:
        raise ValueError('invalid_login_response')
    if any(c in value for c in '\r\n\x00'):
        raise ValueError('invalid_login_response')
    value = value.strip()
    if re.fullmatch(r'[\w./~-]{1,2048}', value, flags=re.ASCII):
        return value
    try:
        parsed, query = _url(value)
        _, original = _url(url)
    except ValueError:
        raise ValueError('invalid_login_response') from None
    if (parsed.scheme != 'https' or parsed.netloc != 'antigravity.google'
            or parsed.path != '/oauth-callback' or query.get('state') != original['state']
            or not query.get('code') or len(query['code']) > 2048
            or any(c in query['code'] for c in '\r\n\x00')):
        raise ValueError('invalid_login_response')
    return value


def control_payload(value):
    if not isinstance(value, dict) or set(value) != {'account_id', 'revision', 'operation', 'owner_digest'}:
        raise ValueError('invalid_payload')
    account_id(value['account_id'])
    if (type(value['revision']) is not int or not 1 <= value['revision'] <= 2**53
            or not isinstance(value['operation'], str) or value['operation'] not in OPERATIONS
            or not isinstance(value['owner_digest'], str)
            or not re.fullmatch('[a-f0-9]{64}', value['owner_digest'])):
        raise ValueError('invalid_payload')
    return dict(value)
