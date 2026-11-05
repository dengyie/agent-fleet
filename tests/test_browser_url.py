"""Fail-closed URL/origin normalization shared by browser policy and transport."""
from __future__ import annotations

import pytest

from tools.platform.browser_url import canonical_url, normalize_origin


@pytest.mark.parametrize(("value", "expected"), [
    ("https://Example.COM", ("https", "example.com", 443)),
    ("http://example.com/", ("http", "example.com", 80)),
    ("HTTPS://example.com.:8443/", ("https", "example.com", 8443)),
    ("http://[::1]:8080", ("http", "::1", 8080)),
    ("http://127.0.0.1", ("http", "127.0.0.1", 80)),
    ("https://bücher.example", ("https", "xn--bcher-kva.example", 443)),
])
def test_normalize_origin_canonicalizes(value, expected):
    assert normalize_origin(value) == expected


@pytest.mark.parametrize("value", [
    None, 42, "", "ftp://example.com", "javascript:alert(1)", "file:///etc/passwd",
    "https://user:pw@example.com", "https://user@example.com", "https://example.com:",
    "https://example.com:0", "https://example.com:65536", "https://example.com:abc",
    "https://exa mple.com", "https://example.com\t", "https://example.com\x7f",
    "https://-bad.example", "https://bad-.example", "https://under_score.example",
    "https://example..com", "https://example.com..", "https://[fe80::1%25eth0]",
    "https://" + "a" * 64 + ".example", "https://", "//example.com",
])
def test_normalize_origin_rejects_unsafe_input(value):
    with pytest.raises(ValueError):
        normalize_origin(value)


def test_require_root_rejects_path_query_and_fragment():
    assert normalize_origin("https://example.com/", require_root=True) == ("https", "example.com", 443)
    for value in ("https://example.com/x", "https://example.com/?q=1", "https://example.com/#f"):
        with pytest.raises(ValueError):
            normalize_origin(value, require_root=True)


@pytest.mark.parametrize(("value", "expected"), [
    ("https://Example.com:443", "https://example.com/"),
    ("http://example.com:80/a?b=1#frag", "http://example.com/a?b=1"),
    ("http://example.com:8080/x", "http://example.com:8080/x"),
    ("http://[::1]:9000/p", "http://[::1]:9000/p"),
    ("https://[::1]", "https://[::1]/"),
])
def test_canonical_url_drops_default_port_and_fragment(value, expected):
    assert canonical_url(value) == expected


def test_canonical_url_is_fail_closed():
    for value in ("https://a:b@example.com/", "data:text/html,x", "https://example.com:/"):
        with pytest.raises(ValueError):
            canonical_url(value)
