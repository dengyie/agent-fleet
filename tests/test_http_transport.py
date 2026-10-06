"""Resource, DNS, proxy and TLS contracts of the shared HTTP boundary."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
import select
import socket
import ssl
from threading import Event, Thread, enumerate as threads
import time
from typing import Any, Iterator
from urllib.error import URLError
from urllib.request import Request

import pytest

from test_provider_transport_boundaries import QuietHandler, REPLY, serve
from tools.platform.http_transport import open_http


@contextmanager
def tls_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handler: type[QuietHandler], *, trusted: bool = True) -> Iterator[ThreadingHTTPServer]:
    from cryptography.hazmat.primitives import serialization
    from werkzeug.serving import generate_adhoc_ssl_pair
    cert, key = generate_adhoc_ssl_pair('localhost')
    cert_path, key_path = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    if trusted:
        monkeypatch.setenv('SSL_CERT_FILE', str(cert_path))
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize('trusted,hostname', [(True, 'localhost'), (False, 'localhost'), (True, '127.0.0.1')])
def test_tls_verification_and_shutdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trusted: bool, hostname: str) -> None:
    seen: list[str] = []
    class Target(QuietHandler):
        def do_POST(self) -> None:
            self.drain()
            seen.append(self.path)
            self.reply()
    with tls_server(tmp_path, monkeypatch, Target, trusted=trusted) as origin:
        request = Request(f'https://{hostname}:{origin.server_port}/probe', data=b'{}')
        if trusted and hostname == 'localhost':
            with open_http(request, timeout_s=2, use_proxy=False) as response:
                assert response.read(1000) == REPLY
            assert seen == ['/probe']
        else:
            with pytest.raises(URLError) as caught:
                open_http(request, timeout_s=2, use_proxy=False)
            assert isinstance(caught.value.reason, ssl.SSLCertVerificationError)
            assert seen == []
    assert not any(t.name == 'fleet-http-deadline' for t in threads())


def test_https_downgrade_redirect_is_not_followed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tools.platform.providers.openai_compatible import OpenAICompatibleProvider, ProviderError
    seen: list[bool] = []
    class Target(QuietHandler):
        def do_GET(self) -> None:
            seen.append(True)
            self.reply()
    with serve(Target) as target:
        class Redirect(QuietHandler):
            def do_POST(self) -> None:
                self.drain()
                self.send_response(302)
                self.send_header('Location', f'http://127.0.0.1:{target.server_port}/untrusted')
                self.end_headers()
        with tls_server(tmp_path, monkeypatch, Redirect) as origin:
            p = OpenAICompatibleProvider(model='fixture', api_key='fixture', allow_network=True,
                endpoint=f'https://localhost:{origin.server_port}/v1/chat/completions')
            with pytest.raises(ProviderError, match='redirect_rejected'):
                p.complete([], [])
    assert seen == []


@pytest.mark.parametrize('secure', [False, True])
def test_system_proxy_and_connect_preserve_origin_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secure: bool) -> None:
    origin_headers: list[dict[str, str]] = []
    proxy_headers: list[dict[str, str]] = []
    paths: list[str] = []
    class Target(QuietHandler):
        def do_POST(self) -> None:
            self.drain()
            origin_headers.append(dict(self.headers))
            self.reply()
    target_context = tls_server(tmp_path, monkeypatch, Target) if secure else serve(Target)
    with target_context as origin:
        class Proxy(QuietHandler):
            def do_POST(self) -> None:
                self.drain()
                proxy_headers.append(dict(self.headers))
                paths.append(self.path)
                self.reply()
            def do_CONNECT(self) -> None:
                proxy_headers.append(dict(self.headers))
                paths.append(self.path)
                with socket.create_connection(('127.0.0.1', origin.server_port), timeout=2) as upstream:
                    self.send_response(200)
                    self.end_headers()
                    peers = [self.connection, upstream]
                    while True:
                        ready, _, _ = select.select(peers, [], [], 2)
                        if not ready:
                            return
                        for peer in ready:
                            data = peer.recv(8192)
                            if not data:
                                return
                            (upstream if peer is self.connection else self.connection).sendall(data)
        with serve(Proxy) as proxy:
            scheme = 'https' if secure else 'http'
            monkeypatch.setenv(f'{scheme}_proxy', f'http://proxy-user:proxy-password@127.0.0.1:{proxy.server_port}')
            monkeypatch.setenv('no_proxy', '')
            monkeypatch.delenv('NO_PROXY', raising=False)
            request = Request(f'{scheme}://localhost:{origin.server_port}/probe', data=b'{}', headers={'Authorization': 'Bearer fixture'})
            with open_http(request, timeout_s=2) as response:
                assert response.read(1000) == REPLY
    assert len(proxy_headers) == 1 and 'Proxy-Authorization' in proxy_headers[0]
    if secure:
        assert paths == [f'localhost:{origin.server_port}']
        assert 'Authorization' not in proxy_headers[0]
        assert origin_headers[0]['Authorization'] == 'Bearer fixture'
        assert 'Proxy-Authorization' not in origin_headers[0]
    else:
        assert paths[0].startswith('http://localhost:')
        assert proxy_headers[0]['Authorization'] == 'Bearer fixture'


def test_unread_response_close_reclaims_socket_and_timer() -> None:
    disconnected = Event()
    class Target(QuietHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header('Content-Length', '100')
            self.end_headers()
            self.connection.settimeout(1)
            if self.connection.recv(1) == b'':
                disconnected.set()
    with serve(Target) as origin:
        response = open_http(Request(f'http://127.0.0.1:{origin.server_port}'), timeout_s=5, use_proxy=False)
        response.close()
        response.close()
        assert disconnected.wait(.5)
    assert not any(t.name == 'fleet-http-deadline' for t in threads())


def test_dns_saturation_is_bounded_and_late_resolution_cannot_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools.platform import http_transport
    release = Event()
    calls: list[tuple[Any, ...]] = []
    original = socket.getaddrinfo
    seen: list[bool] = []
    class Target(QuietHandler):
        def do_GET(self) -> None:
            seen.append(True)
            self.reply()
    def resolve(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        release.wait(2)
        return original('127.0.0.1', args[1], 0, socket.SOCK_STREAM)
    resolver = http_transport._Resolver(capacity=2)
    monkeypatch.setattr(http_transport, '_RESOLVER', resolver)
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    with serve(Target) as origin:
        def request(index: int) -> None:
            with pytest.raises(TimeoutError):
                open_http(Request(f'http://dns-{index}.invalid:{origin.server_port}'), timeout_s=.2, use_proxy=False)
        try:
            with ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(request, range(6)))
            assert len(calls) == 2 and seen == []
            assert not any(t.name == 'fleet-http-deadline' for t in threads())
        finally:
            release.set()
            with resolver.condition:
                assert resolver.condition.wait_for(lambda: not resolver.pending, timeout=2)
        assert seen == []


def test_tls_handshake_and_dns_share_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    release, connected = Event(), Event()
    original = socket.getaddrinfo
    def resolve(*args: Any, **kwargs: Any) -> Any:
        time.sleep(.15)
        return original(*args, **kwargs)
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        def peer() -> None:
            sock, _ = listener.accept()
            with sock:
                connected.set()
                release.wait(2)
        thread = Thread(target=peer, daemon=True)
        thread.start()
        try:
            start = time.monotonic()
            with pytest.raises(TimeoutError):
                open_http(Request(f'https://127.0.0.1:{listener.getsockname()[1]}'), timeout_s=.3, use_proxy=False)
            assert time.monotonic() - start < .5 and connected.is_set()
        finally:
            release.set()
            thread.join(2)
    assert not any(t.name == 'fleet-http-deadline' for t in threads())
