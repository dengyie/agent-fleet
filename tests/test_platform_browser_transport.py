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


class _AdvanceClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class _RawSocket:
    """Fake stream socket serving raw response lines through makefile()."""

    def __init__(self, lines, *, clock=None, events=None):
        self.lines = list(lines)
        self.read_calls = []
        self.clock = clock
        self.events = events if events is not None else []

    def connect(self, sockaddr):
        self.events.append(("connect", sockaddr))

    def send(self, data):
        self.events.append(("send", len(data)))
        return len(data)

    def sendall(self, data):
        self.send(data)

    def settimeout(self, value):
        self.events.append(("timeout", value))

    def makefile(self, _mode, **_kwargs):
        return self

    def readline(self, size=-1):
        line = self.lines.pop(0) if self.lines else b""
        if 0 <= size < len(line):
            line = line[:size]
        self.read_calls.append(size)
        if self.clock is not None:
            self.clock.value += 7
        return line

    def read(self, _size=-1):
        return b""

    def flush(self):
        return None

    def close(self):
        self.events.append(("close",))


def _literal_transport(socket, *, clock=None, origins=("http://127.0.0.1:8080",), **kwargs):
    def unreachable_resolver(_host, _port, *, type, timeout):
        raise AssertionError("loopback literal must not resolve")

    return PinnedBrowserTransport._for_test(
        origins, resolver=unreachable_resolver, socket_factory=lambda *_: socket,
        fixture_loopback=True, clock=clock if clock is not None else _AdvanceClock(),
        **kwargs,
    )


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


def test_pinned_connector_falls_back_only_within_one_validated_resolution():
    events = []
    first = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
             "", ("198.51.100.10", 443))
    second = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
              "", ("198.51.100.11", 443))

    class Socket:
        def __init__(self, _family, _kind, _proto):
            self.address = None

        def settimeout(self, value):
            events.append(("timeout", value))

        def connect(self, sockaddr):
            self.address = sockaddr
            events.append(("connect", sockaddr))
            if sockaddr == first[4]:
                raise OSError("first address unavailable")

        def close(self):
            events.append(("close", self.address))

    connection = _PinnedHTTPConnection(
        "one.example", 443, (first, second), timeout=5,
        socket_factory=lambda *args: Socket(*args),
    )

    connection.connect()

    assert [(kind, value) for kind, value in events if kind == "connect"] == [
        ("connect", first[4]), ("connect", second[4]),
    ]
    assert ("close", first[4]) in events
    connection.close()


def test_pinned_connector_all_failures_do_not_resolve_or_expose_detail():
    events = []
    addresses = tuple(
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (host, 443))
        for host in ("198.51.100.10", "198.51.100.11")
    )

    class Socket:
        def __init__(self, _family, _kind, _proto):
            self.address = None

        def settimeout(self, value):
            return None

        def connect(self, sockaddr):
            self.address = sockaddr
            events.append(sockaddr)
            raise OSError("private connect detail")

        def close(self):
            return None

    connection = _PinnedHTTPConnection(
        "one.example", 443, addresses, timeout=5,
        socket_factory=lambda *args: Socket(*args),
    )

    with pytest.raises(OSError) as caught:
        connection.connect()

    assert events == [address[4] for address in addresses]
    assert str(caught.value) == "private connect detail"


def test_connect_deadline_blocks_later_validated_addresses():
    events = []
    clock = _AdvanceClock()
    addresses = tuple(
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (host, 443))
        for host in ("198.51.100.10", "198.51.100.11")
    )

    class Socket:
        def __init__(self, _family, _kind, _proto):
            self.address = None

        def settimeout(self, value):
            events.append(("timeout", value))

        def connect(self, sockaddr):
            self.address = sockaddr
            events.append(("connect", sockaddr))
            clock.value += 20
            raise OSError("first address unavailable")

        def close(self):
            events.append(("close", self.address))

    connection = _PinnedHTTPConnection(
        "one.example", 443, addresses, timeout=20,
        socket_factory=lambda *args: Socket(*args),
        deadline=20.0, clock=clock,
    )

    with pytest.raises(BrowserTransportError, match="timeout"):
        connection.connect()

    assert [event for event in events if event[0] == "connect"] == [
        ("connect", ("198.51.100.10", 443)),
    ]


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


def test_tls_handshake_consumes_shared_absolute_deadline():
    events = []
    clock = _AdvanceClock()

    class _TLSContext:
        verify_mode = ssl.CERT_REQUIRED
        check_hostname = True

        def wrap_socket(self, sock, *, server_hostname):
            events.append(("sni", server_hostname))
            clock.value += 21
            return sock

    address = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
               "", (PUBLIC, 443))
    connection = _PinnedHTTPSConnection(
        "one.example", 443, (address,), timeout=20,
        socket_factory=lambda *args: _Socket(*args, events),
        tls_context=_TLSContext(), deadline=20.0, clock=clock,
    )
    connection.connect()

    with pytest.raises(BrowserTransportError, match="timeout") as caught:
        connection.send(b"GET / HTTP/1.1\r\n\r\n")

    assert caught.value.code == "timeout"
    assert ("sni", "one.example") in events


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


@pytest.mark.parametrize("address", [
    "10.0.0.1",
    "172.16.0.1",
    "192.168.1.1",
    "169.254.1.1",
    "224.0.0.1",
    "0.0.0.0",
    "240.0.0.1",
    "::",
    "fd00::1",
    "fe80::1",
    "ff02::1",
    "::ffff:192.168.1.2",
])
def test_forbidden_address_classes_are_rejected_before_connection(transport_factory, address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET

    def resolve(_host, port, *, type, timeout):
        sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]

    transport = transport_factory([_Response(200)], resolver=resolve)

    with pytest.raises(BrowserTransportError, match="origin_forbidden"):
        transport.request("https://one.example/")

    assert not transport.test_requests


def test_mixed_public_and_private_answers_reject_the_entire_resolution(transport_factory):
    calls = []

    def resolve(_host, port, *, type, timeout):
        calls.append(True)
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (PUBLIC, port)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.8", port)),
        ]

    transport = transport_factory([_Response(200)], resolver=resolve)

    with pytest.raises(BrowserTransportError, match="origin_forbidden"):
        transport.request("https://one.example/")

    assert calls == [True]
    assert not transport.test_requests


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


def test_each_request_refreshes_resolution_without_re_resolving_fallback(transport_factory):
    calls = []
    addresses = ["93.184.216.34", "93.184.216.35"]

    def resolve(_host, port, *, type, timeout):
        index = len(calls)
        calls.append(index)
        address = addresses[index]
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                 "", (address, port))]

    transport = transport_factory(
        [_Response(200, body=b"first"), _Response(200, body=b"second")],
        resolver=resolve,
    )

    assert transport.request("https://one.example/")[2] == b"first"
    assert transport.request("https://one.example/")[2] == b"second"
    assert calls == [0, 1]
    assert [request[5][0][4][0] for request in transport.test_requests] == addresses


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
        [_Response(200)], origins=("HTTPS://ONE.EXAMPLE:443/",),
    )
    assert transport.request("HTTPS://one.example/")[0] == 200


@pytest.mark.parametrize("origin", [
    "https://one.example:", "https://one.example:0", "https://one.example..",
    "https://one.example/\n",
])
def test_allowlist_rejects_ambiguous_authorities(transport_factory, origin):
    with pytest.raises(BrowserTransportError, match="invalid_url"):
        transport_factory([], origins=(origin,))


def test_allowlist_rejects_duplicate_normalized_origins(transport_factory):
    with pytest.raises(BrowserTransportError, match="invalid_url"):
        transport_factory(
            [], origins=("https://one.example", "HTTPS://ONE.EXAMPLE:443/"),
        )


def test_ipv6_literal_origin_normalizes_and_uses_loopback_fixture(transport_factory):
    transport = transport_factory(
        [_Response(200)], origins=("https://[::1]",), fixture_loopback=True,
    )

    assert transport.request("HTTPS://[0:0:0:0:0:0:0:1]/")[0] == 200
    assert transport.test_requests[0][0:2] == ("::1", 443)
    assert transport.test_requests[0][5][0][4] == ("::1", 443, 0, 0)


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


def test_redirect_limit_allows_ten_hops_and_rejects_the_eleventh(transport_factory):
    ten_hop_responses = [
        _Response(302, [("Location", f"/hop/{index + 1}")])
        for index in range(10)
    ] + [_Response(200, body=b"done")]
    allowed = transport_factory(ten_hop_responses)

    assert allowed.request("https://one.example/start")[2] == b"done"
    assert len(allowed.test_requests) == 11

    eleven_hop_responses = [
        _Response(302, [("Location", f"/hop/{index + 1}")])
        for index in range(11)
    ] + [_Response(200, body=b"must-not-be-read")]
    denied = transport_factory(eleven_hop_responses)

    with pytest.raises(BrowserTransportError, match="redirect_denied"):
        denied.request("https://one.example/start")
    assert len(denied.test_requests) == 11
    assert len(eleven_hop_responses) == 1


@pytest.mark.parametrize("location", [
    (),
    (("Location", "/one"), ("location", "/two")),
])
def test_redirect_requires_exactly_one_location(transport_factory, location):
    transport = transport_factory([_Response(302, location), _Response(200)])

    with pytest.raises(BrowserTransportError, match="redirect_denied"):
        transport.request("https://one.example/start")

    assert len(transport.test_requests) == 1


@pytest.mark.parametrize("value", [
    "https://one.example:abc/path",
    "https://one.example:65536/path",
    "https://one.example:0/path",
    "https://one.example:/path",
    "https://one.example../path",
    "https:///missing-host",
    "https://[::1/path",
    "https://user@one.example/path",
])
def test_malformed_request_authority_and_port_are_invalid_url(transport_factory, value):
    transport = transport_factory([])

    with pytest.raises(BrowserTransportError, match="invalid_url"):
        transport.request(value)

    assert not transport.test_requests


@pytest.mark.parametrize(("location", "code"), [
    ("https://one.example:abc/next", "invalid_url"),
    ("https://one.example:65536/next", "invalid_url"),
    ("https://one.example:0/next", "invalid_url"),
    ("https://one.example:/next", "invalid_url"),
    ("https://one.example../next", "invalid_url"),
    ("https:///missing-host", "invalid_url"),
    ("https://[::1/next", "invalid_url"),
    ("https://user@one.example/next", "invalid_url"),
])
def test_malformed_redirect_authority_and_port_are_rejected(transport_factory, location, code):
    transport = transport_factory([_Response(302, [("Location", location)])])

    with pytest.raises(BrowserTransportError, match=code):
        transport.request("https://one.example/start")

    assert len(transport.test_requests) == 1


def test_same_origin_redirect_preserves_allowed_headers(transport_factory):
    transport = transport_factory(
        [_Response(302, [("Location", "/next")]), _Response(200)],
    )

    transport.request(
        "https://one.example/start",
        headers={"User-Agent": "browser-fixture", "Accept-Language": "en"},
    )

    assert transport.test_requests[0][4] == {
        "User-Agent": "browser-fixture", "Accept-Language": "en",
    }
    assert transport.test_requests[1][4] == transport.test_requests[0][4]


def test_cross_origin_redirect_strips_allowed_headers(transport_factory):
    transport = transport_factory(
        [_Response(302, [("Location", "https://two.example/next")]), _Response(200)],
        origins=("https://one.example", "https://two.example"),
    )

    transport.request(
        "https://one.example/start",
        headers={"User-Agent": "browser-fixture", "Accept-Language": "en"},
    )

    assert transport.test_requests[0][4] == {
        "User-Agent": "browser-fixture", "Accept-Language": "en",
    }
    assert transport.test_requests[1][4] == {}


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


@pytest.mark.parametrize("headers", [
    [("Accept", "a"), ("accept", "b")],
    [("User-Agent", "first"), ("User-Agent", "second")],
    [{"Accept", "a"}],
])
def test_duplicate_request_header_names_are_rejected(transport_factory, headers):
    transport = transport_factory([])

    with pytest.raises(BrowserTransportError, match="protocol_forbidden"):
        transport.request("https://one.example/", headers=headers)

    assert not transport.test_requests


def test_non_latin1_request_header_values_are_rejected(transport_factory):
    transport = transport_factory([])

    with pytest.raises(BrowserTransportError, match="protocol_forbidden"):
        transport.request(
            "https://one.example/", headers={"User-Agent": "en–dash"},
        )

    assert not transport.test_requests


def test_non_read_method_is_rejected(transport_factory):
    transport = transport_factory([])
    with pytest.raises(BrowserTransportError, match="method_forbidden"):
        transport.request("https://one.example/", method="POST")


@pytest.mark.parametrize("location", [
    "javascript:alert(1)", "file:///etc/passwd", "ws://one.example/socket",
    "wss://one.example/socket", "data:text/plain,blocked", "blob:https://one.example/id",
])
def test_unsupported_redirect_schemes_are_denied_without_follow_up(transport_factory, location):
    transport = transport_factory([_Response(302, [("Location", location)]), _Response(200)])

    with pytest.raises(BrowserTransportError):
        transport.request("https://one.example/start")

    assert len(transport.test_requests) == 1


def test_overlong_redirect_location_is_denied_without_follow_up(transport_factory):
    transport = transport_factory([
        _Response(302, [("Location", "/" + "a" * 2048)]), _Response(200),
    ])

    with pytest.raises(BrowserTransportError, match="redirect_denied"):
        transport.request("https://one.example/start")

    assert len(transport.test_requests) == 1


def test_canonical_equivalent_redirect_loop_is_denied_before_second_request(transport_factory):
    transport = transport_factory([
        _Response(302, [("Location", "https://ONE.EXAMPLE:443/a")]),
        _Response(302, [("Location", "https://one.example/a")]),
    ], max_redirects=4)

    with pytest.raises(BrowserTransportError, match="redirect_denied"):
        transport.request("https://one.example/a")

    assert len(transport.test_requests) == 1


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_redirect_statuses_preserve_read_method(transport_factory, status, method):
    transport = transport_factory([_Response(status, [("Location", "/next")]), _Response(200)])

    assert transport.request("https://one.example/start", method=method)[0] == 200
    assert [request[2] for request in transport.test_requests] == [method, method]


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


def test_response_duplicate_headers_keep_deterministic_shape(transport_factory):
    transport = transport_factory([_Response(200, [
        ("X-Tag", "first"), ("x-tag", "second"), ("Set-Cookie", "a=1"),
        ("set-cookie", "b=2"), ("X-Tag", "third"),
    ])])

    _, headers, _ = transport.request("https://one.example/")

    assert headers == {"X-Tag": "third", "x-tag": "second"}


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


def test_seventeenth_concurrent_request_is_rejected_and_capacity_recovers():
    import threading

    entered = threading.Condition()
    release = threading.Event()
    active_factories = 0
    results = []
    errors = []

    class BlockingExchange(_Exchange):
        def request(self, method, path, headers):
            with entered:
                entered.notify_all()
            if not release.wait(timeout=3):
                raise TimeoutError("fixture release timed out")
            super().request(method, path, headers)

    def factory(_scheme, host, port, addresses, _timeout, _socket_factory, _tls_context):
        nonlocal active_factories
        with entered:
            active_factories += 1
            entered.notify_all()
        return BlockingExchange(
            host, port, addresses, [_Response(200, body=b"ok")], [],
        )

    transport = PinnedBrowserTransport._for_test(
        ("https://one.example",),
        resolver=lambda _host, port, *, type, timeout: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (PUBLIC, port))
        ],
        connection_factory=factory,
        max_concurrent=16,
    )

    def run_request():
        try:
            results.append(transport.request("https://one.example/"))
        except Exception as exc:
            errors.append(exc)

    workers = [threading.Thread(target=run_request) for _ in range(16)]
    for worker in workers:
        worker.start()

    with entered:
        assert entered.wait_for(lambda: active_factories == 16, timeout=3)
    with pytest.raises(BrowserTransportError, match="request_limit_exceeded"):
        transport.request("https://one.example/")

    release.set()
    for worker in workers:
        worker.join(timeout=3)
    assert all(not worker.is_alive() for worker in workers)
    assert errors == []
    assert len(results) == 16
    assert transport.request("https://one.example/")[2] == b"ok"


def test_deadline_is_shared_with_dns_and_response_body(transport_factory):
    class Clock:
        def __init__(self):
            self.value = 0.0

        def __call__(self):
            return self.value

    dns_clock = Clock()
    dns_calls = []

    def slow_resolver(host, port, *, type, timeout):
        dns_calls.append(timeout)
        dns_clock.value += timeout
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (PUBLIC, port))]

    dns_transport = transport_factory(
        [_Response(200)], resolver=slow_resolver, clock=dns_clock,
    )
    with pytest.raises(BrowserTransportError, match="timeout"):
        dns_transport.request("https://one.example/")
    assert dns_calls and 0 < dns_calls[0] <= 20
    assert not dns_transport.test_requests

    body_clock = Clock()
    body_response = _Response(200, body=b"slow")
    original_read = body_response.read

    def slow_read(limit=-1):
        chunk = original_read(limit)
        body_clock.value += 21
        return chunk

    body_response.read = slow_read
    body_transport = transport_factory(
        [body_response], clock=body_clock,
        resolver=_resolver_for({"one.example": PUBLIC}, []),
    )
    with pytest.raises(BrowserTransportError, match="timeout"):
        body_transport.request("https://one.example/")
    assert body_transport.test_requests


def test_redirect_hop_respects_absolute_deadline_before_next_resolution(transport_factory):
    clock = _AdvanceClock()
    calls = []
    redirect = _Response(302, [("Location", "/next")])
    original_read = redirect.read

    def slow_read(limit=-1):
        chunk = original_read(limit)
        clock.value += 21
        return chunk

    redirect.read = slow_read
    transport = transport_factory(
        [redirect, _Response(200, body=b"unreachable")],
        clock=clock, resolver=_resolver_for({"one.example": PUBLIC}, calls),
    )

    with pytest.raises(BrowserTransportError, match="timeout") as caught:
        transport.request("https://one.example/start")

    assert caught.value.code == "timeout"
    assert len(transport.test_requests) == 1
    assert len(calls) == 1


def test_header_read_shares_absolute_deadline_in_real_parser():
    clock = _AdvanceClock()
    lines = [b"HTTP/1.1 200 OK\r\n"] + [b"X-A: 1\r\n"] * 4 + [b"\r\n"]
    sock = _RawSocket(lines, clock=clock)
    transport = _literal_transport(sock, clock=clock)

    with pytest.raises(BrowserTransportError, match="timeout") as caught:
        transport.request("http://127.0.0.1:8080/")

    assert caught.value.code == "timeout"
    assert len(sock.read_calls) == 3


def test_bounded_parser_header_budget_exact_boundary():
    lines = [b"HTTP/1.1 200 OK\r\n", b"X-A: b\r\n", b"\r\n"]

    exact = _literal_transport(_RawSocket(lines), max_headers_bytes=27)
    assert exact.request("http://127.0.0.1:8080/") == (200, {"X-A": "b"}, b"")

    tight = _literal_transport(_RawSocket(lines), max_headers_bytes=26)
    with pytest.raises(BrowserTransportError, match="response_too_large"):
        tight.request("http://127.0.0.1:8080/")


def test_bounded_parser_header_budget_clamps_single_line_reads():
    long_header = b"X-Long: " + b"a" * 20 + b"\r\n"
    lines = [b"HTTP/1.1 200 OK\r\n", long_header, b"\r\n"]
    sock = _RawSocket(lines)
    transport = _literal_transport(sock, max_headers_bytes=27)

    with pytest.raises(BrowserTransportError, match="response_too_large"):
        transport.request("http://127.0.0.1:8080/")

    assert sock.read_calls == [28, 11]


def test_transport_errors_never_include_url_location_or_response_body(transport_factory):
    secret_url = "https://one.example/private-path?token=url-marker"
    secret_ip = "203.0.113.77"
    secret_location = f"https://{secret_ip}/redirect-marker"
    secret_body = b"response-body-marker"
    transport = transport_factory([
        _Response(302, [("Location", secret_location)], body=secret_body),
    ])

    with pytest.raises(BrowserTransportError) as caught:
        transport.request(secret_url)

    assert str(caught.value) == "origin_forbidden"
    assert all(marker not in str(caught.value) for marker in (
        secret_url, "url-marker", secret_location, secret_ip, "redirect-marker",
        secret_body.decode("ascii"),
    ))


def test_explicit_loopback_fixture_override(transport_factory):
    transport = transport_factory(
        [_Response(200, body=b"fixture")],
        origins=("http://127.0.0.1:8080",), fixture_loopback=True,
    )

    assert transport.request("http://127.0.0.1:8080/")[2] == b"fixture"


@pytest.fixture
def tls_loopback_server(tmp_path):
    from datetime import datetime, timedelta, timezone
    import threading

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fixture.test")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(minutes=10))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("fixture.test")]), False)
        .sign(key, hashes.SHA256())
    )
    certificate_pem = certificate.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    cert_path = tmp_path / "fixture-cert.pem"
    key_path = tmp_path / "fixture-key.pem"
    cert_path.write_bytes(certificate_pem)
    key_path.write_bytes(key_pem)

    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    sni_names = []
    peer_addresses = []
    server_context.set_servername_callback(
        lambda _socket, server_name, _context: sni_names.append(server_name)
    )
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(3)
    port = listener.getsockname()[1]
    errors = []

    def serve_once():
        try:
            accepted, peer = listener.accept()
            with server_context.wrap_socket(accepted, server_side=True) as connection:
                peer_addresses.append((peer[0], connection.getsockname()[1]))
                request = connection.recv(4096)
                if not request.startswith(b"GET /tls HTTP/1.1\r\n"):
                    raise AssertionError("unexpected loopback HTTP request")
                connection.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                    b"Connection: close\r\n\r\nok"
                )
        except Exception as exc:
            errors.append(exc)
        finally:
            listener.close()

    thread = threading.Thread(target=serve_once, daemon=True)
    thread.start()
    try:
        yield port, certificate_pem, sni_names, peer_addresses, errors, thread
    finally:
        listener.close()
        thread.join(timeout=3)
        assert not thread.is_alive()


def _loopback_tls_transport(port, tls_context):
    def resolve(_host, effective_port, *, type, timeout):
        assert effective_port == port
        assert type == socket.SOCK_STREAM
        assert 0 < timeout <= 20
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                 "", ("127.0.0.1", port))]

    return PinnedBrowserTransport._for_test(
        (f"https://fixture.test:{port}",), resolver=resolve,
        tls_context=tls_context, fixture_loopback=True,
    )


def test_real_loopback_tls_verifies_ca_hostname_sni_and_pinned_address(tls_loopback_server):
    port, certificate_pem, sni_names, peer_addresses, server_errors, thread = (
        tls_loopback_server
    )
    client_context = ssl.create_default_context(cadata=certificate_pem.decode("ascii"))
    transport = _loopback_tls_transport(port, client_context)

    status, _, body = transport.request(f"https://fixture.test:{port}/tls")
    thread.join(timeout=3)

    assert (status, body) == (200, b"ok")
    assert sni_names == ["fixture.test"]
    assert peer_addresses == [("127.0.0.1", port)]
    assert server_errors == []


def test_real_loopback_tls_rejects_untrusted_certificate_with_bounded_code(
    tls_loopback_server,
):
    port, _certificate_pem, _sni_names, _peer_addresses, server_errors, thread = (
        tls_loopback_server
    )
    client_context = ssl.create_default_context()
    transport = _loopback_tls_transport(port, client_context)

    with pytest.raises(BrowserTransportError, match="tls_failed") as caught:
        transport.request(f"https://fixture.test:{port}/tls")
    thread.join(timeout=3)

    assert str(caught.value) == "tls_failed"
    assert all("fixture" not in str(error).lower() for error in server_errors)


def test_real_loopback_tls_rejects_hostname_mismatch_with_bounded_code(
    tls_loopback_server,
):
    port, certificate_pem, _sni_names, _peer_addresses, server_errors, thread = (
        tls_loopback_server
    )
    client_context = ssl.create_default_context(cadata=certificate_pem.decode("ascii"))
    transport = PinnedBrowserTransport._for_test(
        (f"https://wrong.test:{port}",),
        resolver=lambda _host, effective_port, *, type, timeout: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
             "", ("127.0.0.1", effective_port))
        ],
        tls_context=client_context,
        fixture_loopback=True,
    )

    with pytest.raises(BrowserTransportError, match="tls_failed") as caught:
        transport.request(f"https://wrong.test:{port}/tls")
    thread.join(timeout=3)

    assert str(caught.value) == "tls_failed"
    assert len(server_errors) == 1
    assert isinstance(server_errors[0], ssl.SSLError)
