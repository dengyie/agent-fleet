"""Offline-testable pinned HTTP transport contract for browser egress."""
from __future__ import annotations

import http.client
import ipaddress
import math
import re
import socket
import ssl
import threading
import time
from functools import partial
from urllib.parse import urldefrag, urljoin, urlsplit


class BrowserTransportError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


_REDIRECTS = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 10
_MAX_DNS_ANSWERS = 16
_MAX_CONCURRENT = 16
_MAX_HEADER_BYTES = 64 * 1024
_MAX_BODY_BYTES = 16 * 1024 * 1024
_MAX_SESSION_BODY_BYTES = 64 * 1024 * 1024
_MAX_REQUEST_SECONDS = 20.0
_MAX_URL_LENGTH = 2048
_MAX_HEADER_LINE = 64 * 1024
_FORBIDDEN_HEADERS = {
    "host", ":authority", "cookie", "set-cookie", "set-cookie2",
    "authorization", "proxy-authorization", "proxy-connection",
    "connection", "keep-alive", "transfer-encoding", "upgrade", "te",
    "trailer",
}
_ALLOWED_REQUEST_HEADERS = {
    "accept", "accept-encoding", "accept-language", "cache-control",
    "if-modified-since", "if-none-match", "range", "user-agent",
}
_SENSITIVE_RESPONSE_HEADERS = {
    "set-cookie", "set-cookie2", "authorization", "proxy-authorization",
    "www-authenticate", "proxy-authenticate", "location", "alt-svc",
}
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def _origin(parsed) -> tuple[str, str, int]:
    scheme = parsed.scheme.lower()
    try:
        host = parsed.hostname.rstrip(".").lower()
        if not host or "%" in host:
            raise ValueError
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            host = host.encode("idna").decode("ascii")
        parsed_port = parsed.port
        port = (443 if scheme == "https" else 80) if parsed_port is None else parsed_port
        if not 1 <= port <= 65535:
            raise ValueError
    except (AttributeError, UnicodeError, ValueError):
        raise BrowserTransportError("invalid_url") from None
    if ":" not in host:
        labels = host.split(".")
        if len(host) > 253 or any(
            not label or len(label) > 63 or label[0] == "-" or label[-1] == "-"
            or any(not (char.isalnum() or char == "-") for char in label)
            for label in labels
        ):
            raise BrowserTransportError("invalid_url")
    return scheme, host, port


def _is_loopback(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(address, "ipv4_mapped", None)
    return (mapped.is_loopback if mapped is not None else address.is_loopback)


def _is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(address, "ipv4_mapped", None)
    return (mapped.is_global if mapped is not None else address.is_global)


def _parse_url(value: str, allowed_origins: frozenset[tuple[str, str, int]], resolver,
               *, fixture_loopback: bool, timeout_s: float, clock, deadline: float):
    if (not isinstance(value, str) or len(value) > _MAX_URL_LENGTH
            or any(ord(char) <= 0x20 or ord(char) == 0x7f for char in value)):
        raise BrowserTransportError("invalid_url")
    try:
        parsed = urlsplit(value)
        scheme, host, port = _origin(parsed)
        if (scheme not in {"http", "https"} or not host
                or parsed.username is not None or parsed.password is not None):
            raise ValueError
        parsed = parsed._replace(fragment="")
    except (ValueError, AttributeError):
        raise BrowserTransportError("invalid_url") from None

    origin = (scheme, host, port)
    if origin not in allowed_origins:
        raise BrowserTransportError("origin_forbidden")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if scheme == "http" and not fixture_loopback:
        raise BrowserTransportError("origin_forbidden")
    if literal is not None:
        if not fixture_loopback or not _is_loopback(literal):
            raise BrowserTransportError("origin_forbidden")
        rows = [(socket.AF_INET6 if literal.version == 6 else socket.AF_INET,
                 socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
                 (host, port, 0, 0) if literal.version == 6 else (host, port))]
    else:
        if resolver is None:
            raise BrowserTransportError("origin_unresolvable")
        remaining = deadline - clock()
        if remaining <= 0:
            raise BrowserTransportError("timeout")
        try:
            rows = resolver(host, port, type=socket.SOCK_STREAM, timeout=min(timeout_s, remaining))
        except (socket.timeout, TimeoutError):
            raise BrowserTransportError("timeout") from None
        except Exception:
            raise BrowserTransportError("origin_unresolvable") from None
        if deadline - clock() <= 0:
            raise BrowserTransportError("timeout")
    if not isinstance(rows, (list, tuple)) or not rows:
        raise BrowserTransportError("origin_unresolvable")
    if len(rows) > _MAX_DNS_ANSWERS:
        raise BrowserTransportError("request_limit_exceeded")

    addresses = []
    seen_addresses = set()
    classifications = set()
    for row in rows:
        try:
            family, socktype, proto, _canonname, sockaddr = row
            if not isinstance(sockaddr, tuple):
                raise ValueError
            address_text, answer_port = sockaddr[:2]
            if not isinstance(address_text, str) or "%" in address_text:
                raise ValueError
            address = ipaddress.ip_address(address_text)
            if (isinstance(answer_port, bool) or not isinstance(answer_port, int)
                    or socktype != socket.SOCK_STREAM
                    or proto not in {0, socket.IPPROTO_TCP} or answer_port != port):
                raise ValueError
            if ((family == socket.AF_INET and address.version != 4)
                    or (family == socket.AF_INET6 and address.version != 6)
                    or family not in {socket.AF_INET, socket.AF_INET6}):
                raise ValueError
            if family == socket.AF_INET and len(sockaddr) != 2:
                raise ValueError
            if family == socket.AF_INET6 and (
                    len(sockaddr) != 4
                    or any(isinstance(value, bool) or not isinstance(value, int)
                           for value in sockaddr[2:])
                    or not 0 <= sockaddr[2] <= 0xFFFFFFFF
                    or not 0 <= sockaddr[3] <= 0xFFFFFFFF
                    or sockaddr[3] != 0):
                raise ValueError
        except (TypeError, ValueError, IndexError, OverflowError):
            raise BrowserTransportError("origin_unresolvable") from None

        if scheme == "http":
            classification = "loopback" if fixture_loopback and _is_loopback(address) else "forbidden"
        elif _is_loopback(address):
            classification = "loopback" if fixture_loopback else "forbidden"
        elif _is_public(address):
            classification = "public"
        else:
            classification = "forbidden"
        if classification == "forbidden":
            raise BrowserTransportError("origin_forbidden")
        classifications.add(classification)
        normalized_sockaddr = ((str(address), port, sockaddr[2], sockaddr[3])
                               if family == socket.AF_INET6 else (str(address), port))
        candidate = (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", normalized_sockaddr)
        if candidate not in seen_addresses:
            addresses.append(candidate)
            seen_addresses.add(candidate)
    if scheme == "https" and len(classifications) > 1:
        raise BrowserTransportError("origin_forbidden")
    return parsed, tuple(addresses)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, port, addresses, *, timeout, socket_factory,
                 deadline=None, clock=time.monotonic):
        super().__init__(host, port, timeout=timeout)
        self.addresses = addresses
        self.socket_factory = socket_factory
        self.deadline = deadline
        self.clock = clock

    def _refresh_timeout(self):
        if self.deadline is None:
            return
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise BrowserTransportError("timeout")
        if self.sock is not None:
            self.sock.settimeout(remaining)

    def connect(self):
        last_error = None
        for family, socktype, proto, _, sockaddr in self.addresses:
            remaining = self.timeout
            if self.deadline is not None:
                remaining = self.deadline - self.clock()
                if remaining <= 0:
                    raise BrowserTransportError("timeout")
            sock = self.socket_factory(family, socktype, proto)
            try:
                sock.settimeout(remaining)
                sock.connect(sockaddr)
            except (socket.timeout, TimeoutError):
                last_error = BrowserTransportError("timeout")
                sock.close()
                continue
            except OSError as exc:
                last_error = exc
                sock.close()
                continue
            self.sock = sock
            return
        if isinstance(last_error, BrowserTransportError):
            raise last_error
        raise last_error or OSError("connect failed")

    def send(self, data):
        self._refresh_timeout()
        return super().send(data)


class _PinnedHTTPSConnection(_PinnedHTTPConnection):
    def __init__(self, host, port, addresses, *, timeout, socket_factory, tls_context,
                 deadline=None, clock=time.monotonic):
        super().__init__(host, port, addresses, timeout=timeout,
                         socket_factory=socket_factory, deadline=deadline,
                         clock=clock)
        self.tls_context = tls_context

    def connect(self):
        super().connect()
        try:
            self._refresh_timeout()
            self.sock = self.tls_context.wrap_socket(self.sock, server_hostname=self.host)
        except BrowserTransportError:
            self.close()
            raise
        except (socket.timeout, TimeoutError):
            self.close()
            raise BrowserTransportError("timeout") from None
        except (ssl.SSLError, OSError):
            self.close()
            raise BrowserTransportError("tls_failed") from None


class _HeaderBudgetReader:
    def __init__(self, stream, *, connection, charge, remaining, deadline, clock):
        self._stream = stream
        self._connection = connection
        self._charge = charge
        self._remaining = remaining
        self._deadline = deadline
        self._clock = clock

    def readline(self, size=-1):
        remaining_time = self._deadline - self._clock()
        if remaining_time <= 0:
            raise BrowserTransportError("timeout")
        self._connection._refresh_timeout()
        available = self._remaining()
        limit = _MAX_HEADER_LINE + 1 if size is None or size < 0 else size
        limit = min(limit, available + 1)
        line = self._stream.readline(limit)
        if line:
            self._charge(len(line))
        return line

    def __getattr__(self, name):
        return getattr(self._stream, name)


class _BoundedHTTPResponse(http.client.HTTPResponse):
    def __init__(self, sock, *, header_charge, header_remaining, connection,
                 deadline, clock, method=None):
        super().__init__(sock, method=method)
        self._header_charge = header_charge
        self._header_remaining = header_remaining
        self._header_connection = connection
        self._header_deadline = deadline
        self._header_clock = clock
        self._bounded_headers_counted = False

    def begin(self):
        self.fp = _HeaderBudgetReader(
            self.fp, connection=self._header_connection,
            charge=self._header_charge, remaining=self._header_remaining,
            deadline=self._header_deadline, clock=self._header_clock,
        )
        try:
            super().begin()
        finally:
            self._bounded_headers_counted = True


def _read_bounded(response, *, limit: int, deadline: float, clock, connection,
                  on_chunk) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= limit:
        remaining = deadline - clock()
        if remaining <= 0:
            raise BrowserTransportError("timeout")
        sock = getattr(connection, "sock", None)
        if sock is not None:
            sock.settimeout(remaining)
        chunk = response.read(min(64 * 1024, limit - total + 1))
        if chunk is None or not isinstance(chunk, (bytes, bytearray)):
            raise BrowserTransportError("response_invalid")
        if not chunk:
            return b"".join(chunks)
        chunk = bytes(chunk)
        on_chunk(len(chunk))
        total += len(chunk)
        if total > limit:
            raise BrowserTransportError("response_too_large")
        chunks.append(chunk)
    raise BrowserTransportError("response_too_large")


def _validate_tls_context(context):
    if (getattr(context, "verify_mode", None) != ssl.CERT_REQUIRED
            or getattr(context, "check_hostname", None) is not True):
        raise ValueError("TLS context must verify certificates and hostnames")


def _validate_limit(value, maximum, *, allow_zero=False):
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError("transport limit is outside its fixed maximum")
    return value


def _validate_timeout(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0 or value > _MAX_REQUEST_SECONDS):
        raise ValueError("request timeout is outside its fixed maximum")
    return float(value)


class PinnedBrowserTransport:
    """Fail-closed public shell; the direct transport remains a test contract only."""

    def __init__(self, allowed_origins):
        self._initialize(
            allowed_origins, resolver=None, socket_factory=socket.socket,
            tls_context=ssl.create_default_context(), clock=time.monotonic,
            connection_factory=None, timeout_s=_MAX_REQUEST_SECONDS,
            max_redirects=_MAX_REDIRECTS, max_body_bytes=_MAX_BODY_BYTES,
            max_session_body_bytes=_MAX_SESSION_BODY_BYTES,
            max_concurrent=_MAX_CONCURRENT, max_headers_bytes=_MAX_HEADER_BYTES,
            fixture_loopback=False,
        )

    @classmethod
    def _for_test(cls, allowed_origins, *, resolver, socket_factory=socket.socket,
                  tls_context=None, clock=time.monotonic, connection_factory=None,
                  timeout_s=_MAX_REQUEST_SECONDS, max_redirects=_MAX_REDIRECTS,
                  max_body_bytes=_MAX_BODY_BYTES,
                  max_session_body_bytes=_MAX_SESSION_BODY_BYTES,
                  max_concurrent=_MAX_CONCURRENT,
                  max_headers_bytes=_MAX_HEADER_BYTES, fixture_loopback=False):
        """Create an offline fixture transport; never wire this into NodeRuntime."""
        context = tls_context or ssl.create_default_context()
        _validate_tls_context(context)
        instance = cls.__new__(cls)
        instance._initialize(
            allowed_origins, resolver=resolver, socket_factory=socket_factory,
            tls_context=context, clock=clock, connection_factory=connection_factory,
            timeout_s=timeout_s, max_redirects=max_redirects,
            max_body_bytes=max_body_bytes,
            max_session_body_bytes=max_session_body_bytes,
            max_concurrent=max_concurrent, max_headers_bytes=max_headers_bytes,
            fixture_loopback=fixture_loopback,
        )
        return instance

    def _initialize(self, allowed_origins, *, resolver, socket_factory, tls_context,
                    clock, connection_factory, timeout_s, max_redirects,
                    max_body_bytes, max_session_body_bytes, max_concurrent,
                    max_headers_bytes, fixture_loopback):
        try:
            normalized_origins = [self._normalize_origin(value) for value in allowed_origins]
        except TypeError:
            raise BrowserTransportError("invalid_url") from None
        if len(normalized_origins) != len(set(normalized_origins)):
            raise BrowserTransportError("invalid_url")
        self.allowed_origins = frozenset(normalized_origins)
        self.resolver = resolver
        self.socket_factory = socket_factory
        self.tls_context = tls_context
        self.clock = clock
        self.connection_factory = connection_factory
        self.timeout_s = _validate_timeout(timeout_s)
        self.max_redirects = _validate_limit(max_redirects, _MAX_REDIRECTS, allow_zero=True)
        self.max_body_bytes = _validate_limit(max_body_bytes, _MAX_BODY_BYTES, allow_zero=True)
        self.max_session_body_bytes = _validate_limit(
            max_session_body_bytes, _MAX_SESSION_BODY_BYTES, allow_zero=True,
        )
        self.max_concurrent = _validate_limit(max_concurrent, _MAX_CONCURRENT)
        self.max_headers_bytes = _validate_limit(
            max_headers_bytes, _MAX_HEADER_BYTES, allow_zero=True,
        )
        self.fixture_loopback = bool(fixture_loopback)
        self._lock = threading.Lock()
        self._active = 0
        self._session_bytes = 0
        self._session_header_bytes = 0

    @staticmethod
    def _normalize_origin(value):
        try:
            parsed = urlsplit(value)
            if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
                raise ValueError
            scheme, host, port = _origin(parsed)
            if (scheme not in {"https", "http"} or parsed.username is not None
                    or parsed.password is not None):
                raise ValueError
            return scheme, host, port
        except (TypeError, ValueError, AttributeError):
            raise BrowserTransportError("invalid_url") from None

    def _enter(self):
        with self._lock:
            if self._active >= self.max_concurrent:
                raise BrowserTransportError("request_limit_exceeded")
            self._active += 1

    def _charge_session_bytes(self, amount: int) -> None:
        with self._lock:
            self._session_bytes += amount
            if self._session_bytes > self.max_session_body_bytes:
                raise BrowserTransportError("response_too_large")

    def _charge_header_bytes(self, amount: int) -> None:
        with self._lock:
            self._session_header_bytes += amount
            if self._session_header_bytes > self.max_headers_bytes:
                raise BrowserTransportError("response_too_large")

    def _remaining_header_bytes(self) -> int:
        with self._lock:
            return self.max_headers_bytes - self._session_header_bytes

    @staticmethod
    def _request_headers(headers):
        if headers is None:
            return {}
        try:
            values = dict(headers)
        except (TypeError, ValueError):
            raise BrowserTransportError("protocol_forbidden") from None
        normalized = {}
        for name, value in values.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise BrowserTransportError("protocol_forbidden")
            lowered = name.lower()
            if (not _HEADER_NAME.fullmatch(name) or lowered in _FORBIDDEN_HEADERS
                    or lowered not in _ALLOWED_REQUEST_HEADERS
                    or any((ord(char) < 0x20 and char != "\t") or ord(char) == 0x7f
                           for char in value)):
                raise BrowserTransportError("protocol_forbidden")
            normalized[name] = value
        return normalized

    def request(self, url: str, *, method="GET", headers=None) -> tuple[int, dict[str, str], bytes]:
        method = method.upper() if isinstance(method, str) else ""
        if method not in {"GET", "HEAD"}:
            raise BrowserTransportError("method_forbidden")
        request_headers = self._request_headers(headers)
        self._enter()
        deadline = self.clock() + self.timeout_s
        if not math.isfinite(deadline) or deadline <= self.clock():
            with self._lock:
                self._active -= 1
            raise BrowserTransportError("timeout")
        visited = set()
        current = url
        try:
            for hop in range(self.max_redirects + 1):
                parsed, addresses = _parse_url(
                    current, self.allowed_origins, self.resolver,
                    fixture_loopback=self.fixture_loopback,
                    timeout_s=self.timeout_s, clock=self.clock, deadline=deadline,
                )
                key = (method, parsed.geturl())
                if key in visited:
                    raise BrowserTransportError("redirect_denied")
                visited.add(key)
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise BrowserTransportError("timeout")
                _, host, port = _origin(parsed)
                connection_type = (_PinnedHTTPSConnection if parsed.scheme == "https"
                                   else _PinnedHTTPConnection)
                if self.connection_factory is not None:
                    connection = self.connection_factory(
                        parsed.scheme, host, port, addresses, remaining,
                        self.socket_factory, self.tls_context,
                    )
                else:
                    options = {
                        "timeout": remaining,
                        "socket_factory": self.socket_factory,
                        "deadline": deadline,
                        "clock": self.clock,
                    }
                    if parsed.scheme == "https":
                        options["tls_context"] = self.tls_context
                    connection = connection_type(host, port, addresses, **options)
                    connection.response_class = partial(
                        _BoundedHTTPResponse,
                        header_charge=self._charge_header_bytes,
                        header_remaining=self._remaining_header_bytes,
                        connection=connection, deadline=deadline, clock=self.clock,
                    )
                try:
                    path = parsed.path or "/"
                    if parsed.query:
                        path += "?" + parsed.query
                    connection.request(method, path, headers=request_headers)
                    if deadline - self.clock() <= 0:
                        raise BrowserTransportError("timeout")
                    response = connection.getresponse()
                    try:
                        response_headers = list(response.getheaders())
                        if any(not isinstance(key, str) or not isinstance(value, str)
                               for key, value in response_headers):
                            raise ValueError
                        header_bytes = sum(
                            len(key.encode("latin-1")) + len(value.encode("latin-1")) + 4
                            for key, value in response_headers
                        )
                    except (TypeError, ValueError, UnicodeError):
                        raise BrowserTransportError("response_invalid") from None
                    if header_bytes > self.max_headers_bytes:
                        raise BrowserTransportError("response_too_large")
                    if not getattr(response, "_bounded_headers_counted", False):
                        self._charge_header_bytes(header_bytes)
                    locations = [value for name, value in response_headers
                                 if name.lower() == "location"]
                    if response.status in _REDIRECTS:
                        redirect_body = _read_bounded(
                            response, limit=64 * 1024, deadline=deadline,
                            clock=self.clock, connection=connection,
                            on_chunk=self._charge_session_bytes,
                        )
                        if hop == self.max_redirects or len(locations) != 1:
                            raise BrowserTransportError("redirect_denied")
                        location = locations[0]
                        if any(ord(char) <= 0x20 or ord(char) == 0x7f for char in location):
                            raise BrowserTransportError("redirect_denied")
                        try:
                            joined = urljoin(parsed.geturl(), location)
                            next_url = urldefrag(joined).url
                            next_parsed = urlsplit(next_url)
                            next_origin = _origin(next_parsed)
                            if (not next_parsed.hostname or next_parsed.username is not None
                                    or next_parsed.password is not None):
                                raise ValueError
                        except (ValueError, AttributeError):
                            raise BrowserTransportError("invalid_url") from None
                        next_scheme, next_host, _ = next_origin
                        try:
                            next_literal = ipaddress.ip_address(next_host)
                        except ValueError:
                            next_literal = None
                        if (next_scheme != "https" and not (
                                self.fixture_loopback and next_literal
                                and _is_loopback(next_literal))):
                            raise BrowserTransportError("origin_forbidden")
                        if next_origin not in self.allowed_origins:
                            raise BrowserTransportError("origin_forbidden")
                        if next_origin != _origin(parsed):
                            request_headers = {}
                        current = next_url
                        continue
                    body = (b"" if method == "HEAD" else _read_bounded(
                        response, limit=self.max_body_bytes, deadline=deadline,
                        clock=self.clock, connection=connection,
                        on_chunk=self._charge_session_bytes,
                    ))
                    safe_headers = {
                        key: value for key, value in response_headers
                        if key.lower() not in _SENSITIVE_RESPONSE_HEADERS
                    }
                    return response.status, safe_headers, body
                except BrowserTransportError:
                    raise
                except (socket.timeout, TimeoutError):
                    raise BrowserTransportError("timeout") from None
                except (OSError, http.client.HTTPException):
                    raise BrowserTransportError("response_invalid") from None
                finally:
                    connection.close()
            raise BrowserTransportError("redirect_denied")
        finally:
            with self._lock:
                self._active -= 1


__all__ = ["BrowserTransportError", "PinnedBrowserTransport"]
