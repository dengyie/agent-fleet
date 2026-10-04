"""Fail-closed URL and argument policy for browser commands."""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlsplit

from tools.platform.browser_url import normalize_origin


MAX_URL = 2048
MAX_TEXT = 4096
MAX_SELECTOR = 512
MAX_SCROLL = 2000
_ALLOWED_TOOLS = frozenset({
    "browser.open", "browser.navigate", "browser.snapshot", "browser.screenshot",
    "browser.click", "browser.type", "browser.scroll", "browser.back", "browser.close",
})
_SENSITIVE_MARKERS = ("password", "passwd", "token", "secret", "credential", "cookie", "authorization", "api_key")


class BrowserPolicyError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _normalized_origin(value: object, *, require_root: bool = False) -> tuple[str, str, int]:
    try:
        return normalize_origin(value, require_root=require_root)
    except (AttributeError, TypeError, ValueError):
        raise BrowserPolicyError("invalid_url") from None


def _resolved_addresses(host: str, port: int, resolver=None) -> tuple[ipaddress._BaseAddress, ...]:
    """Resolve a hostname and return all stream addresses, fail closed."""
    if resolver is None:
        resolver = socket.getaddrinfo
    try:
        rows = resolver(host, port, type=socket.SOCK_STREAM)
    except (OSError, socket.gaierror, TypeError, ValueError):
        raise BrowserPolicyError("origin_unresolvable") from None
    addresses = []
    for row in rows or ():
        try:
            sockaddr = row[4]
            address = ipaddress.ip_address(sockaddr[0])
        except (IndexError, KeyError, TypeError, ValueError):
            raise BrowserPolicyError("origin_unresolvable") from None
        addresses.append(address)
    if not addresses:
        raise BrowserPolicyError("origin_unresolvable")
    return tuple(addresses)


def validate_url(value: object, *, network_enabled: bool = False, allowed_origins=(),
                 resolver=None) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_URL:
        raise BrowserPolicyError("invalid_url")
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in value):
        raise BrowserPolicyError("invalid_url")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        username = parsed.username
        password = parsed.password
    except ValueError:
        raise BrowserPolicyError("invalid_url") from None
    if parsed.scheme.lower() not in {"http", "https"} or not hostname or username or password:
        raise BrowserPolicyError("invalid_url")
    try:
        scheme, host, port = _normalized_origin(value)
    except BrowserPolicyError:
        raise
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    loopback = host == "localhost" or (address is not None and address.is_loopback)
    if loopback:
        return value
    if not network_enabled:
        raise BrowserPolicyError("network_disabled")
    configured_origins = [
        _normalized_origin(origin, require_root=True)
        for origin in (allowed_origins or ())
    ]
    if len(configured_origins) != len(set(configured_origins)):
        raise BrowserPolicyError("invalid_url")
    configured = set(configured_origins)
    if (scheme, host, port) not in configured:
        raise BrowserPolicyError("origin_forbidden")
    if address is not None:
        if not address.is_global:
            raise BrowserPolicyError("origin_forbidden")
    elif any(not resolved.is_global for resolved in _resolved_addresses(host, port, resolver)):
        raise BrowserPolicyError("origin_forbidden")
    return value


def validate_action(tool: object, arguments: object, *, network_enabled: bool = False,
                    allowed_origins=(), resolver=None) -> dict:
    if tool not in _ALLOWED_TOOLS:
        raise BrowserPolicyError("unknown_tool")
    if not isinstance(arguments, dict):
        raise BrowserPolicyError("invalid_arguments")
    allowed = {
        "browser.open": {"url"}, "browser.navigate": {"url"},
        "browser.snapshot": set(), "browser.screenshot": set(),
        "browser.click": {"selector"}, "browser.type": {"selector", "text"},
        "browser.scroll": {"delta_y"}, "browser.back": set(), "browser.close": set(),
    }[tool]
    allowed = set(allowed)
    if tool != "browser.open":
        allowed.add("session_id")
    if set(arguments) - allowed:
        raise BrowserPolicyError("invalid_arguments")
    result = dict(arguments)
    if tool != "browser.open":
        session_id = arguments.get("session_id")
        if not isinstance(session_id, str) or not 16 <= len(session_id) <= 128:
            raise BrowserPolicyError("invalid_session")
    if tool in {"browser.open", "browser.navigate"}:
        result["url"] = validate_url(
            arguments.get("url"), network_enabled=network_enabled,
            allowed_origins=allowed_origins, resolver=resolver)
    if tool in {"browser.click", "browser.type"}:
        selector = arguments.get("selector")
        if not isinstance(selector, str) or not selector or len(selector) > MAX_SELECTOR or re.search(r"[\x00-\x1f\x7f]", selector):
            raise BrowserPolicyError("invalid_selector")
        if any(marker in selector.lower() for marker in _SENSITIVE_MARKERS):
            raise BrowserPolicyError("sensitive_field_forbidden")
        result["selector"] = selector
    if tool == "browser.type":
        text = arguments.get("text")
        if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_TEXT:
            raise BrowserPolicyError("invalid_text")
        result["text"] = text
    if tool == "browser.scroll":
        delta = arguments.get("delta_y")
        if isinstance(delta, bool) or not isinstance(delta, int) or abs(delta) > MAX_SCROLL:
            raise BrowserPolicyError("invalid_scroll")
    return result


__all__ = ["BrowserPolicyError", "validate_action", "validate_url"]
