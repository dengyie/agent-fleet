"""Stdlib HTTP with one deadline, explicit ownership and no redirects/replay."""
from __future__ import annotations

import http.client
import socket
import ssl
from threading import Condition, Event, Lock, Thread, Timer
import time
from types import TracebackType
from typing import Any
from urllib.error import URLError
from urllib.request import HTTPErrorProcessor, HTTPHandler, HTTPSHandler, ProxyHandler, Request, build_opener


AddressInfo = tuple[int, int, int, str, tuple[Any, ...]]


def _remaining(deadline: float) -> float:
    budget = deadline - time.monotonic()
    if budget <= 0:
        raise TimeoutError('http_deadline_exceeded')
    return budget


class _Lookup:
    def __init__(self) -> None:
        self.done = Event()
        self.result: list[AddressInfo] = []
        self.error: Exception | None = None


class _Resolver:
    """Bound stuck libc lookups globally; abandoned work can only resolve DNS."""

    def __init__(self, capacity: int = 8) -> None:
        self.capacity = capacity
        self.pending: dict[tuple[str, int], _Lookup] = {}
        self.condition = Condition()

    def resolve(self, address: tuple[str, int], deadline: float) -> list[AddressInfo]:
        with self.condition:
            while address not in self.pending and len(self.pending) >= self.capacity:
                self.condition.wait(_remaining(deadline))
            _remaining(deadline)
            lookup = self.pending.get(address)
            if lookup is None:
                lookup = _Lookup()
                self.pending[address] = lookup
                thread = Thread(target=self._resolve, args=(address, lookup), name='fleet-http-dns', daemon=True)
                try:
                    thread.start()
                except Exception:
                    del self.pending[address]
                    self.condition.notify_all()
                    raise
        if not lookup.done.wait(_remaining(deadline)):
            raise TimeoutError('http_deadline_exceeded')
        _remaining(deadline)
        if lookup.error is not None:
            raise lookup.error
        return lookup.result

    def _resolve(self, address: tuple[str, int], lookup: _Lookup) -> None:
        try:
            lookup.result = socket.getaddrinfo(*address, 0, socket.SOCK_STREAM)
        except Exception as exc:
            lookup.error = exc
        finally:
            with self.condition:
                del self.pending[address]
                lookup.done.set()
                self.condition.notify_all()


_RESOLVER = _Resolver()


class _Deadline:
    def __init__(self, timeout_s: float) -> None:
        self.expires = time.monotonic() + timeout_s
        self.lock = Lock()
        self.sock: socket.socket | None = None
        self.timer = Timer(self.remaining(), self._interrupt)
        self.timer.name = 'fleet-http-deadline'
        self.timer.daemon = True
        self.timer.start()

    def remaining(self) -> float:
        return _remaining(self.expires)

    def _interrupt(self) -> None:
        with self.lock:
            if self.sock is not None:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    # The peer/HTTP parser may already have closed the socket.
                    return

    def connect(self, address: tuple[str, int], timeout: float,
                source_address: tuple[str, int] | None = None) -> socket.socket:
        last_error = OSError('no resolved addresses')
        for family, socktype, proto, _, sockaddr in _RESOLVER.resolve(address, self.expires):
            self.remaining()
            sock = socket.socket(family, socktype, proto)
            try:
                with self.lock:
                    sock.settimeout(self.remaining())
                    self.sock = sock
                if source_address:
                    sock.bind(source_address)
                sock.connect(sockaddr)
                sock.settimeout(self.remaining())
                return sock
            except OSError as exc:
                last_error = exc
                with self.lock:
                    if self.sock is sock:
                        self.sock = None
                sock.close()
        self.remaining()
        raise last_error

    def wrap_tls(self, sock: socket.socket, context: ssl.SSLContext, hostname: str) -> ssl.SSLSocket:
        # wrap_socket detaches the raw fd. Register its new owner before any
        # blocking handshake, so deadline expiry always interrupts the live fd.
        with self.lock:
            self.remaining()
            wrapped = context.wrap_socket(sock, server_hostname=hostname, do_handshake_on_connect=False)
            self.sock = wrapped
            wrapped.settimeout(self.remaining())
        wrapped.do_handshake()
        self.remaining()
        return wrapped

    def close(self) -> None:
        self.timer.cancel()
        self.timer.join()
        with self.lock:
            if self.sock is not None:
                self.sock.close()
                self.sock = None


class _HTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, *, deadline: _Deadline, **kwargs: Any) -> None:
        super().__init__(host, **kwargs)
        self._create_connection = deadline.connect


class _HTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, *, deadline: _Deadline, **kwargs: Any) -> None:
        super().__init__(host, **kwargs)
        self._create_connection = deadline.connect
        self.deadline = deadline

    def connect(self) -> None:
        http.client.HTTPConnection.connect(self)  # Includes a configured proxy CONNECT.
        self.sock = self.deadline.wrap_tls(self.sock, self._context, self._tunnel_host or self.host)


class _HTTPHandler(HTTPHandler):
    def __init__(self, deadline: _Deadline) -> None:
        super().__init__()
        self.deadline = deadline

    def http_open(self, request: Request) -> http.client.HTTPResponse:
        return self.do_open(lambda host, **kw: _HTTPConnection(host, deadline=self.deadline, **kw), request)


class _HTTPSHandler(HTTPSHandler):
    def __init__(self, deadline: _Deadline) -> None:
        super().__init__()
        self.deadline = deadline

    def https_open(self, request: Request) -> http.client.HTTPResponse:
        return self.do_open(lambda host, **kw: _HTTPSConnection(host, deadline=self.deadline, **kw), request)


class _ReturnStatus(HTTPErrorProcessor):
    def http_response(self, request: Request, response: http.client.HTTPResponse) -> http.client.HTTPResponse:
        # Do not enter urllib's error/redirect dispatch at all. Even relative
        # redirects must remain a visible, single HTTP attempt.
        return response

    https_response = http_response


class DeadlineResponse:
    """Own the response and timer even when the caller never starts reading."""

    def __init__(self, response: http.client.HTTPResponse, deadline: _Deadline) -> None:
        self.response = response
        self.deadline = deadline
        self.status = response.status
        self.headers = response.headers
        self.closed = False

    def _read(self, size: int, *, incremental: bool) -> bytes:
        self.deadline.remaining()
        try:
            data = self.response.read1(size) if incremental else self.response.read(size)
        except (OSError, http.client.HTTPException) as exc:
            try:
                self.deadline.remaining()
            except TimeoutError as expired:
                raise expired from exc
            raise
        self.deadline.remaining()  # Shutdown may look like EOF to the parser.
        return data

    def read(self, size: int) -> bytes:
        return self._read(size, incremental=False)

    def read1(self, size: int) -> bytes:
        return self._read(size, incremental=True)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.response.close()
        finally:
            self.deadline.close()

    def __enter__(self) -> DeadlineResponse:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.close()


def open_http(request: Request, *, timeout_s: float, use_proxy: bool = True) -> DeadlineResponse:
    deadline = _Deadline(timeout_s)
    transferred = False
    try:
        proxy = ProxyHandler() if use_proxy else ProxyHandler({})
        opener = build_opener(proxy, _HTTPHandler(deadline), _HTTPSHandler(deadline), _ReturnStatus())
        response = opener.open(request, timeout=deadline.remaining())
        try:
            deadline.remaining()
        except TimeoutError:
            response.close()
            raise
        owned = DeadlineResponse(response, deadline)
        transferred = True
        return owned
    except (OSError, URLError, http.client.HTTPException) as exc:
        try:
            deadline.remaining()
        except TimeoutError as expired:
            raise expired from exc
        raise
    finally:
        # Successful ownership passes to DeadlineResponse; all other paths
        # reclaim the timer/socket before returning to the synchronous caller.
        if not transferred:
            deadline.close()
