import socket
import ssl

import pytest

from tools.platform.browser_transport import (
    BrowserTransportError,
    PinnedBrowserTransport,
    _PinnedHTTPConnection,
    _PinnedHTTPSConnection,
)

PUBLIC = "93.184.216.34"


class _Socket:
    def __init__(self, _family, _kind, _proto, events):
        self.events = events

    def settimeout(self, value):
        self.events.append(("timeout", value))

    def connect(self, sockaddr):
        self.events.append(("connect", sockaddr))

    def close(self):
        self.events.append(("close",))


class _Response:
    def __init__(self, status, headers=(), body=b""):
        self.status = status
        self.headers = list(headers)
        self.body = body
        self.offset = 0

    def getheaders(self):
        return self.headers

    def read(self, limit=-1):
        if limit < 0:
            limit = len(self.body) - self.offset
        chunk = self.body[self.offset:self.offset + limit]
        self.offset += len(chunk)
        return chunk


class _Exchange:
    def __init__(self, host, port, addresses, responses, requests):
        self.host = host
        self.port = port
        self.addresses = addresses
        self.responses = responses
        self.requests = requests

    def request(self, method, path, headers):
        self.requests.append((self.host, self.port, method, path, dict(headers), self.addresses))

    def getresponse(self):
        return self.responses.pop(0)

    def close(self):
        return None


def _resolver_for(mapping, calls):
    def resolve(host, port, *, type, timeout):
        calls.append((host, port, timeout))
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                 "", (mapping[host], port))]

    return resolve


@pytest.fixture
def transport_factory():
    def make(responses, *, origins=("https://one.example",), resolver=None, **kwargs):
        requests = []

        def factory(_scheme, host, port, addresses, _timeout, _socket_factory, _tls_context):
            return _Exchange(host, port, addresses, responses, requests)

        transport = PinnedBrowserTransport._for_test(
            origins,
            resolver=resolver or _resolver_for(
                {"one.example": PUBLIC, "two.example": "1.1.1.1"}, []
            ),
            connection_factory=factory,
            **kwargs,
        )
        transport.test_requests = requests
        return transport

    return make


def test_pinned_connector_uses_only_validated_sockaddr():
    events = []
    address = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
               "", (PUBLIC, 443))
    connection = _PinnedHTTPConnection(
        "one.example", 443, (address,), timeout=5,
        socket_factory=lambda *args: _Socket(*args, events),
    )

    connection.connect()

    assert ("connect", (PUBLIC, 443)) in events
    connection.close()


@pytest.mark.parametrize(
    ("failure", "code"),
    [(socket.timeout(), "timeout"), (OSError("tls failed"), "tls_failed")],
)
def test_tls_failures_have_stable_codes(failure, code):
    class _TLSContext:
        verify_mode = ssl.CERT_REQUIRED
        check_hostname = True

        def wrap_socket(self, sock, *, server_hostname):
            raise failure

    address = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
               "", (PUBLIC, 443))
    connection = _PinnedHTTPSConnection(
        "original.example", 443, (address,), timeout=5,
        socket_factory=lambda *args: _Socket(*args, []),
        tls_context=_TLSContext(),
    )

    with pytest.raises(BrowserTransportError, match=code):
        connection.connect()


def test_https_uses_original_hostname_for_tls_sni():
    events = []

    class _TLSContext:
        def wrap_socket(self, sock, *, server_hostname):
            events.append(("sni", server_hostname))
            return sock

    address = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
               "", (PUBLIC, 443))
    connection = _PinnedHTTPSConnection(
        "original.example", 443, (address,), timeout=5,
        socket_factory=lambda *args: _Socket(*args, events),
        tls_context=_TLSContext(),
    )

    connection.connect()

    assert ("connect", (PUBLIC, 443)) in events
    assert ("sni", "original.example") in events
    connection.close()


def test_transport_resolves_once_and_connects_to_validated_address(transport_factory):
    calls = []
    transport = transport_factory(
        [_Response(200, body=b"ok")],
        resolver=_resolver_for({"one.example": PUBLIC}, calls),
    )

    status, _, body = transport.request("https://one.example/path?q=1")

    assert (status, body) == (200, b"ok")
    assert len(calls) == 1
    assert calls[0][:2] == ("one.example", 443)
    assert 0 < calls[0][2] <= 20
    assert transport.test_requests[0][5][0][4] == (PUBLIC, 443)


def test_mixed_dns_answers_are_denied_before_connection(transport_factory):
    calls = []

    def resolve(host, port, *, type, timeout):
        calls.append((host, port, timeout))
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        ]

    transport = transport_factory([_Response(200)], resolver=resolve)

    with pytest.raises(BrowserTransportError, match="origin_forbidden"):
        transport.request("https://one.example/")
    assert len(calls) == 1
    assert not transport.test_requests


@pytest.mark.parametrize("bad_sockaddr", [
    (PUBLIC, 443, 0),
    (PUBLIC, 443, 0, 0),
    ("::ffff:192.168.1.2", 443, 0, 0),
    ("fe80::1%en0", 443, 0, 0),
])
def test_invalid_or_mapped_resolver_answers_are_rejected(transport_factory, bad_sockaddr):
    family = socket.AF_INET6 if ":" in bad_sockaddr[0] else socket.AF_INET

    def resolve(_host, _port, *, type, timeout):
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", bad_sockaddr)]

    transport = transport_factory([_Response(200)], resolver=resolve)

    with pytest.raises(BrowserTransportError):
        transport.request("https://one.example/")
    assert not transport.test_requests


def test_fixture_https_rejects_mixed_public_and_loopback_answers(transport_factory):
    def resolve(_host, port, *, type, timeout):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (PUBLIC, port)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port)),
        ]

    transport = transport_factory(
        [_Response(200)], fixture_loopback=True, resolver=resolve,
    )

    with pytest.raises(BrowserTransportError, match="origin_forbidden"):
        transport.request("https://one.example/")
    assert not transport.test_requests


def test_fixture_https_allows_loopback_answer_without_mixing(transport_factory):
    def resolve(_host, port, *, type, timeout):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                 "", ("127.0.0.1", port))]

    transport = transport_factory(
        [_Response(200)], fixture_loopback=True, resolver=resolve,
    )

    assert transport.request("https://one.example/")[0] == 200
    assert len(transport.test_requests) == 1
    assert transport.test_requests[0][5][0][4] == ("127.0.0.1", 443)


def test_dns_rows_are_deduplicated_in_linear_result_size(transport_factory):
    calls = []

    def resolve(_host, port, *, type, timeout):
        calls.append(True)
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                 "", (PUBLIC, port))] * 16

    transport = transport_factory([_Response(200)], resolver=resolve)

    assert transport.request("https://one.example/")[0] == 200
    assert len(transport.test_requests[0][5]) == 1
    assert len(calls) == 1


def test_resolver_must_accept_remaining_deadline(transport_factory):
    def unbounded_resolver(host, port, *, type):
        raise AssertionError("unbounded resolver must never be called")

    transport = transport_factory([_Response(200)], resolver=unbounded_resolver)
    with pytest.raises(BrowserTransportError, match="origin_unresolvable"):
        transport.request("https://one.example/")
    assert not transport.test_requests


def test_dns_answer_limit_is_checked_before_connection(transport_factory):
    def many_answers(host, port, *, type, timeout):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, port))] * 17

    transport = transport_factory([_Response(200)], resolver=many_answers)
    with pytest.raises(BrowserTransportError, match="request_limit_exceeded"):
        transport.request("https://one.example/")
    assert not transport.test_requests


def test_allowlist_normalizes_default_port_and_hostname(transport_factory):
    transport = transport_factory(
        [_Response(200)], origins=("https://ONE.EXAMPLE:443/",),
    )
    assert transport.request("https://one.example/")[0] == 200


def test_same_origin_redirect_revalidates_and_resolves_each_hop(transport_factory):
    calls = []
    transport = transport_factory(
        [_Response(302, [("Location", "/next")]), _Response(200, body=b"done")],
        resolver=_resolver_for({"one.example": PUBLIC}, calls),
    )

    assert transport.request("https://one.example/start")[2] == b"done"
    assert len(calls) == 2
    assert all(call[:2] == ("one.example", 443) for call in calls)
    assert all(0 < call[2] <= 20 for call in calls)


@pytest.mark.parametrize("location", [
    "http://evil.example/",
    "https://unlisted.example/",
    "https://127.0.0.1/",
    "https://one.example/\nX:bad",
])
def test_redirect_target_policy_fails_closed(transport_factory, location):
    transport = transport_factory([_Response(302, [("Location", location)])])

    with pytest.raises(BrowserTransportError):
        transport.request("https://one.example/start")
    assert len(transport.test_requests) == 1


def test_cross_origin_redirect_requires_allowlist(transport_factory):
    transport = transport_factory(
        [_Response(302, [("Location", "https://two.example/next")]),
         _Response(200, body=b"ok")],
        origins=("https://one.example", "https://two.example"),
    )

    assert transport.request("https://one.example/")[2] == b"ok"
    assert transport.test_requests[1][0] == "two.example"


@pytest.mark.parametrize("headers", [
    {"Authorization": "secret"},
    {"Cookie": "a=b"},
    {"Host": "attacker"},
    {"Proxy-Authorization": "secret"},
])
def test_credentials_and_routing_headers_are_rejected(transport_factory, headers):
    transport = transport_factory([])
    with pytest.raises(BrowserTransportError, match="protocol_forbidden"):
        transport.request("https://one.example/", headers=headers)


def test_non_read_method_is_rejected(transport_factory):
    transport = transport_factory([])
    with pytest.raises(BrowserTransportError, match="method_forbidden"):
        transport.request("https://one.example/", method="POST")


def test_body_header_and_redirect_loop_limits(transport_factory):
    body = transport_factory([_Response(200, body=b"12345")], max_body_bytes=4)
    with pytest.raises(BrowserTransportError, match="response_too_large"):
        body.request("https://one.example/")

    headers = transport_factory(
        [_Response(200, [("X-Large", "12345")])], max_headers_bytes=8
    )
    with pytest.raises(BrowserTransportError, match="response_too_large"):
        headers.request("https://one.example/")

    loop = transport_factory(
        [_Response(302, [("Location", "/a")]),
         _Response(302, [("Location", "/")]),
         _Response(302, [("Location", "/a")])],
        max_redirects=4,
    )
    with pytest.raises(BrowserTransportError, match="redirect_denied"):
        loop.request("https://one.example/")


def test_redirect_and_response_bodies_share_session_budget(transport_factory):
    transport = transport_factory(
        [_Response(302, [("Location", "/next")], body=b"1234"),
         _Response(200, body=b"5678")],
        max_session_body_bytes=7,
    )
    with pytest.raises(BrowserTransportError, match="response_too_large"):
        transport.request("https://one.example/")


def test_response_sensitive_headers_are_not_returned(transport_factory):
    transport = transport_factory([_Response(200, [
        ("Set-Cookie", "secret=value"),
        ("Authorization", "private"),
        ("Content-Type", "text/plain"),
    ])])
    _, headers, _ = transport.request("https://one.example/")
    assert headers == {"Content-Type": "text/plain"}


def test_public_constructor_fails_closed_without_bounded_resolver():
    transport = PinnedBrowserTransport(("https://one.example",))
    with pytest.raises(BrowserTransportError, match="origin_unresolvable"):
        transport.request("https://one.example/")


def test_fixed_budget_maxima_cannot_be_increased(transport_factory):
    with pytest.raises(ValueError):
        transport_factory([], max_redirects=11)
    with pytest.raises(ValueError):
        transport_factory([], max_body_bytes=16 * 1024 * 1024 + 1)
    with pytest.raises(ValueError):
        transport_factory([], max_concurrent=17)


def test_redirect_fragment_is_discarded_before_following(transport_factory):
    transport = transport_factory(
        [_Response(302, [("Location", "/next#client-fragment")]),
         _Response(200, body=b"ok")],
    )
    assert transport.request("https://one.example/start")[2] == b"ok"
    assert transport.test_requests[1][3] == "/next"


def test_header_budget_is_aggregate_across_responses(transport_factory):
    transport = transport_factory(
        [_Response(302, [("Location", "/next")]),
         _Response(200, [("X-Test", "value")])],
        max_headers_bytes=28,
    )
    with pytest.raises(BrowserTransportError, match="response_too_large"):
        transport.request("https://one.example/start")


def test_explicit_loopback_fixture_override(transport_factory):
    transport = transport_factory(
        [_Response(200, body=b"fixture")],
        origins=("http://127.0.0.1:8080",), fixture_loopback=True,
    )

    assert transport.request("http://127.0.0.1:8080/")[2] == b"fixture"
