"""Explicit native Agent model protocols and bounded private delivery contract."""
from urllib.parse import urlsplit

from agent_profiles import PROFILES

AGENT_PROTOCOLS = {family: profile.model_protocol for family, profile in PROFILES.items()
                   if profile.model_protocol is not None}


def valid_base_url(value):
    if not isinstance(value, str) or not value or len(value) > 2048 or any(c.isspace() for c in value):
        return False
    try:
        url = urlsplit(value)
        return bool(url.scheme in {'http', 'https'} and url.hostname and not (
            url.username or url.password or url.query or url.fragment))
    except ValueError:
        return False


def validate_model_delivery(value, family):
    if not isinstance(value, dict):
        raise ValueError('invalid_model_config')
    if value == {'mode': 'original'}:
        return dict(value)
    if (set(value) != {'mode', 'profile_id', 'digest', 'family', 'model', 'base_url', 'api_key', 'headers'}
            or value.get('mode') != 'catalog' or value.get('family') != family
            or family not in AGENT_PROTOCOLS or not valid_base_url(value.get('base_url'))):
        raise ValueError('invalid_model_config')
    for key, cap in [('profile_id', 128), ('digest', 64), ('model', 256), ('api_key', 16384)]:
        item = value.get(key)
        if not isinstance(item, str) or not item or len(item) > cap or any(ord(c) < 32 for c in item):
            raise ValueError('invalid_model_config')
    headers = value.get('headers')
    if (not isinstance(headers, dict) or len(headers) > 32 or any(
            not isinstance(k, str) or not isinstance(v, str) or not k or len(k) > 80 or len(v) > 512
            or any(c in k + v for c in '\r\n') or ':' in k
            or any(p in k.lower() for p in ('authorization', 'api-key', 'token', 'secret', 'password', 'credential'))
            for k, v in headers.items())):
        raise ValueError('invalid_model_config')
    return {**value, 'headers': dict(headers)}
