"""Server-side verification for hCaptcha and Cloudflare Turnstile challenges."""
import json
import logging
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

HCAPTCHA_VERIFY_URL = 'https://api.hcaptcha.com/siteverify'
TURNSTILE_VERIFY_URL = 'https://challenges.cloudflare.com/turnstile/v0/siteverify'


def verify_hcaptcha(token: str, secret: str, remote_ip: str | None = None, *, timeout: float = 5.0) -> bool:
    if not token or not secret:
        return False
    data = {'secret': secret, 'response': token}
    if remote_ip:
        data['remoteip'] = remote_ip
    encoded = urlencode(data).encode('utf-8')
    req = Request(
        HCAPTCHA_VERIFY_URL,
        data=encoded,
        headers={'Content-Type': 'application/x-www-form-urlencoded', 'User-Agent': 'agent-fleet/captcha-verifier'},
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                logger.warning('hCaptcha verify returned HTTP %s', resp.status)
                return False
            payload = json.loads(resp.read(65536).decode('utf-8'))
            return bool(payload.get('success'))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        logger.warning('hCaptcha verify error: %s', exc)
        return False


def verify_turnstile(token: str, secret: str, remote_ip: str | None = None, *, timeout: float = 5.0) -> bool:
    if not token or not secret:
        return False
    data = {'secret': secret, 'response': token}
    if remote_ip:
        data['remoteip'] = remote_ip
    encoded = urlencode(data).encode('utf-8')
    req = Request(
        TURNSTILE_VERIFY_URL,
        data=encoded,
        headers={'Content-Type': 'application/x-www-form-urlencoded', 'User-Agent': 'agent-fleet/turnstile-verifier'},
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                logger.warning('Turnstile verify returned HTTP %s', resp.status)
                return False
            payload = json.loads(resp.read(65536).decode('utf-8'))
            return bool(payload.get('success'))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        logger.warning('Turnstile verify error: %s', exc)
        return False
