"""Wall-clock deadlines apply to HTTP headers, bodies and all model checks."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import socketserver
import ssl
from threading import Event, Thread
import time

import pytest

from tools.platform.acceptance_check import AcceptanceFailure, AccountClient, check_model


@contextmanager
def delayed_target(stage, mode):
    requests = []
    stop = Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', '0')))
            self.respond()

        def do_GET(self):
            self.respond()

        def respond(self):
            requests.append(self.path)
            status = 200
            if self.path.endswith('/conversations'):
                current = 'create'
                payload = {'conversation': {'conversation_id': 'conv-deadline'}}
            elif self.path.endswith(('/turns', '/acceptance-turns')):
                current, status = 'submit', 202
                payload = {'run': {'run_id': 'run-deadline'}}
            elif self.path.endswith('/events'):
                current = 'events'
                payload = {'events': [{'kind': 'tool_result', 'payload': {'tool': 'workspace.list', 'state': 'succeeded'}}]}
            elif '/runs/' in self.path:
                current = 'status'
                payload = {'state': 'succeeded', 'result_text': 'done'}
            else:
                current = 'recovery'
                payload = {'conversation': {'runs': [{'run_id': 'run-deadline', 'state': 'succeeded', 'result_text': 'done'}]}}
            raw = json.dumps(payload).encode()
            try:
                if stage == 'all':
                    stop.wait(.08)
                if stage == current and mode == 'headers':
                    stop.wait(.7)
                if stage == current and mode == 'header_trickle':
                    for byte in b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n':
                        self.wfile.write(bytes([byte])); self.wfile.flush()
                        if stop.wait(.025):
                            return
                    self.wfile.write(f'Content-Length: {len(raw)}\r\n\r\n'.encode() + raw)
                    return
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                if stage == current and mode == 'body':
                    self.wfile.write(raw[:1]); self.wfile.flush()
                    stop.wait(.7)
                    self.wfile.write(raw[1:])
                elif stage == current and mode == 'trickle':
                    for byte in raw:
                        self.wfile.write(bytes([byte])); self.wfile.flush()
                        if stop.wait(.025):
                            break
                else:
                    self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield AccountClient(f'http://127.0.0.1:{server.server_port}'), requests
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize('stage,mode', [
    ('status', 'headers'), ('status', 'body'), ('status', 'trickle'),
    ('events', 'body'), ('recovery', 'body'), ('submit', 'headers'), ('create', 'headers'),
    ('status', 'header_trickle'), ('all', 'headers'),
])
def test_model_deadline_rejects_late_success_without_replay(stage, mode):
    with delayed_target(stage, mode) as (client, requests):
        start = time.monotonic()
        row = check_model(client, 'sample', 'home', .25)
        elapsed = time.monotonic() - start
        assert row['status'] == 'failed', row
        assert row['error'] == 'acceptance_timeout', row
        assert row['outcome'] == 'unconfirmed'
        assert elapsed < .6, elapsed
        submissions = [p for p in requests if p.endswith(('/turns', '/acceptance-turns'))]
        assert len(submissions) == (0 if stage == 'create' else 1)
        if stage != 'create':
            assert row['conversation_id'] == 'conv-deadline'
            assert row['client_token'].startswith('acceptance-')
        if stage not in ('create', 'submit'):
            assert row['run_id'] == 'run-deadline'


def test_model_completes_within_deadline():
    with delayed_target(None, None) as (client, _):
        assert check_model(client, 'sample', 'home', 1)['status'] == 'passed'


@pytest.mark.parametrize('resolve_call', [1, 2, 3])
def test_dns_deadline_preserves_known_ids_without_late_requests(monkeypatch, resolve_call):
    original = socket.getaddrinfo
    release, finished = Event(), Event()
    calls = []

    def resolve(*args, **kwargs):
        calls.append(True)
        if len(calls) == resolve_call:
            release.wait(1)
        try:
            return original(*args, **kwargs)
        finally:
            if len(calls) == resolve_call:
                finished.set()

    with delayed_target(None, None) as (client, requests):
        monkeypatch.setattr(socket, 'getaddrinfo', resolve)
        try:
            start = time.monotonic()
            row = check_model(client, 'sample', 'home', .25)
            elapsed = time.monotonic() - start
            assert row['error'] == 'acceptance_timeout' and row['outcome'] == 'unconfirmed'
            assert elapsed < .6, elapsed
            assert len(requests) == resolve_call - 1
            if resolve_call >= 2:
                assert row['conversation_id'] == 'conv-deadline'
                assert row['client_token'].startswith('acceptance-')
            if resolve_call == 3:
                assert row['run_id'] == 'run-deadline'
        finally:
            release.set()
            assert finished.wait(2)
        # Completing an abandoned resolver must never advance to HTTP I/O.
        assert len(requests) == resolve_call - 1


def test_repeated_dns_timeouts_keep_only_one_pending_resolver(monkeypatch):
    release, entered, finished = Event(), Event(), Event()
    calls = []

    def resolve(*args, **kwargs):
        calls.append(True)
        entered.set()
        release.wait(1)
        finished.set()
        raise OSError('resolver unavailable')

    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    client = AccountClient('http://127.0.0.1:1')
    try:
        start = time.monotonic()
        for _ in range(3):
            with pytest.raises(AcceptanceFailure) as error:
                client.request('/unused', deadline=time.monotonic() + .1)
            assert error.value.code == 'acceptance_timeout'
        assert time.monotonic() - start < .6
        assert entered.is_set() and len(calls) == 1
    finally:
        release.set()
        assert finished.wait(2)


def test_tcp_address_attempts_share_remaining_budget(monkeypatch):
    attempts, closed = [], []

    class UnreachableSocket:
        def __init__(self, *args):
            pass

        def settimeout(self, timeout):
            self.timeout = timeout

        def connect(self, address):
            attempts.append(self.timeout)
            time.sleep(min(.2, self.timeout))
            raise TimeoutError('unreachable address')

        def close(self):
            closed.append(True)

    addresses = [(socket.AF_INET, socket.SOCK_STREAM, 0, '', ('127.0.0.1', port)) for port in (1, 2, 3)]
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: addresses)
    monkeypatch.setattr(socket, 'socket', UnreachableSocket)
    start = time.monotonic()
    row = check_model(AccountClient('http://127.0.0.1'), 'sample', 'home', .3)
    assert row['error'] == 'acceptance_timeout'
    assert time.monotonic() - start < .5
    assert len(attempts) == len(closed) == 2
    assert attempts[1] < attempts[0] - .15


def test_tls_handshake_uses_budget_left_after_dns(monkeypatch):
    release, connected = Event(), Event()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            connected.set()
            release.wait(2)  # Accept TCP, but never answer the TLS handshake.

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True

    server = Server(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original = socket.getaddrinfo

    def resolve(*args, **kwargs):
        time.sleep(.25)
        return original(*args, **kwargs)

    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    try:
        start = time.monotonic()
        row = check_model(AccountClient(f'https://127.0.0.1:{server.server_address[1]}'), 'sample', 'home', .4)
        elapsed = time.monotonic() - start
        assert connected.is_set()
        assert row['error'] == 'acceptance_timeout'
        assert elapsed < .57, elapsed
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize('trusted,hostname', [(True, 'localhost'), (False, 'localhost'), (True, '127.0.0.1')])
def test_deadline_connection_preserves_tls_verification(tmp_path, monkeypatch, trusted, hostname):
    from cryptography.hazmat.primitives import serialization
    from werkzeug.serving import generate_adhoc_ssl_pair

    cert, key = generate_adhoc_ssl_pair('localhost')
    cert_path, key_path = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    if trusted:
        monkeypatch.setenv('SSL_CERT_FILE', str(cert_path))
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            requests.append(self.path)
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = AccountClient(f'https://{hostname}:{server.server_port}')
        if trusted and hostname == 'localhost':
            assert client.request('/probe', deadline=time.monotonic() + 2) == {'ok': True}
            assert requests == ['/probe']
        else:
            with pytest.raises(AcceptanceFailure) as error:
                client.request('/probe', deadline=time.monotonic() + 2)
            assert error.value.code == 'transport_unknown'
            assert requests == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
