"""Production HTTP boundary regressions using disposable loopback peers."""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import socket
from threading import Event, Thread
import time
from typing import Any, Iterator

import pytest

from tools.platform.providers.openai_compatible import OpenAICompatibleProvider, ProviderError, ProviderFactory


REPLY = b'{"choices":[{"message":{"content":"done"}}]}'


class QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        return

    def drain(self) -> None:
        self.rfile.read(int(self.headers.get('Content-Length', '0')))

    def reply(self) -> None:
        self.send_response(200)
        self.send_header('Content-Length', str(len(REPLY)))
        self.end_headers()
        self.wfile.write(REPLY)


@contextmanager
def serve(handler: type[BaseHTTPRequestHandler]) -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def provider(server: ThreadingHTTPServer, **kwargs: Any) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(model='fixture', api_key='private-boundary-sentinel',
        endpoint=f'http://127.0.0.1:{server.server_port}/v1/chat/completions',
        allow_network=True, timeout_s=1, **kwargs)


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
def test_redirect_never_forwards_credentials_or_replays(status: int) -> None:
    destinations: list[str | None] = []
    attempts: list[bool] = []
    events: list[dict[str, Any]] = []
    class Target(QuietHandler):
        def do_GET(self) -> None:
            destinations.append(self.headers.get('Authorization'))
            self.reply()
        def do_POST(self) -> None:
            self.drain()
            self.do_GET()
    with serve(Target) as target:
        class Redirect(QuietHandler):
            def do_POST(self) -> None:
                self.drain()
                attempts.append(True)
                self.send_response(status)
                self.send_header('Location', f'http://localhost:{target.server_port}/untrusted')
                self.send_header('Content-Length', '0')
                self.end_headers()
        with serve(Redirect) as origin:
            with pytest.raises(ProviderError) as caught:
                provider(origin, max_retries=2, sleeper=lambda _: None).complete([], [],
                    request_observer=lambda kind, data: events.append(data))
    assert destinations == [] and len(attempts) == 1
    assert caught.value.code == 'redirect_rejected' and not caught.value.retryable
    assert len(events) == 2 and events[-1]['http_status'] == status
    assert 'private-boundary-sentinel' not in json.dumps(events)


def test_chunked_done_finishes_before_http_eof_and_closes_socket() -> None:
    disconnected = Event()
    class Done(QuietHandler):
        protocol_version = 'HTTP/1.1'
        def do_POST(self) -> None:
            self.drain()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            raw = b'data: {"choices":[{"delta":{"content":"done"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
            self.wfile.write(f'{len(raw):x}\r\n'.encode() + raw + b'\r\n')
            self.wfile.flush()
            self.connection.settimeout(2)
            if self.connection.recv(1) == b'':
                disconnected.set()
    with serve(Done) as origin:
        start = time.monotonic()
        assert provider(origin, stream=True).complete([], []).text == 'done'
        assert time.monotonic() - start < .75
        assert disconnected.wait(.5)


@pytest.mark.parametrize('stage', ['headers', 'body'])
def test_trickle_is_bounded_by_total_deadline_without_replay(stage: str) -> None:
    stop = Event()
    attempts: list[bool] = []
    events: list[dict[str, Any]] = []
    class Trickle(QuietHandler):
        def do_POST(self) -> None:
            self.drain()
            attempts.append(True)
            raw = REPLY
            if stage == 'headers':
                raw = b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(REPLY)).encode() + b'\r\n\r\n' + REPLY
            else:
                self.send_response(200)
                self.send_header('Content-Length', str(len(REPLY)))
                self.end_headers()
            try:
                for offset in range(0, len(raw), 4):
                    self.wfile.write(raw[offset:offset + 4])
                    self.wfile.flush()
                    if stop.wait(.15):
                        return
            except OSError:
                return  # The deadline deliberately closes this peer.
    with serve(Trickle) as origin:
        start = time.monotonic()
        try:
            with pytest.raises(ProviderError) as caught:
                provider(origin, max_retries=2, sleeper=lambda _: None).complete([], [],
                    request_observer=lambda kind, data: events.append(data))
            assert caught.value.code == 'timeout'
            assert time.monotonic() - start < 1.4
            assert len(attempts) == 1 and len(events) == 2
            assert events[-1]['status'] == 'unknown'
        finally:
            stop.set()


def test_worker_secret_failure_retains_safe_cause_chain(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    from test_request_metadata_api import setup, OWNER
    app, _, client, _, run = setup(tmp_path)
    class BrokenBroker:
        def resolve(self, secret_ref: str) -> str | None:
            raise OSError('private-boundary-sentinel')
    worker = app.extensions['fleet']['services']['platform_worker']
    worker.provider_factory = ProviderFactory(secret_broker=BrokenBroker())
    with caplog.at_level(logging.WARNING):
        assert worker.run_once(OWNER)['state'] == 'failed'
    records = [json.loads(r.message) for r in caplog.records if r.name == 'hub.application.run_worker_service']
    failure = next(r for r in records if r['event'] == 'platform_run_failure')
    assert failure['exception_chain'] == ['RuntimeError', 'ProviderUnavailable', 'OSError']
    assert any(frame['function'] == 'resolve' for frame in failure['frames'])
    result = client.get(f"/api/platform/v1/runs/{run['run_id']}").get_json()
    events = client.get(f"/api/platform/v1/runs/{run['run_id']}/events").get_json()
    assert 'private-boundary-sentinel' not in caplog.text + json.dumps(result) + json.dumps(events)


def test_request_timeout_releases_scheduler_slot_and_next_run_executes(tmp_path: Path) -> None:
    from threading import enumerate as threads
    from test_platform_run_scheduler import _queued, _scheduler, OWNER_A
    from hub.infrastructure.platform_db import PlatformRepository
    stop = Event()
    attempts: list[bool] = []
    class Trickle(QuietHandler):
        def do_POST(self) -> None:
            self.drain()
            attempts.append(True)
            if len(attempts) > 1:
                self.reply()
                return
            self.send_response(200)
            self.send_header('Content-Length', str(len(REPLY)))
            self.end_headers()
            try:
                for byte in REPLY:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    if stop.wait(.15):
                        return
            except OSError:
                return
    with serve(Trickle) as origin:
        repo = PlatformRepository(tmp_path / 'platform.db')
        repo.init()
        _queued(repo, OWNER_A, 'run-slow', 'home', tmp_path / 'work')
        scheduler = _scheduler(repo, provider_factory=lambda profile: provider(origin, max_retries=2))
        try:
            start = time.monotonic()
            assert scheduler.run_once()[0]['state'] == 'unknown'
            assert time.monotonic() - start < 1.6
            _queued(repo, OWNER_A, 'run-next', 'home', tmp_path / 'work')
            assert scheduler.run_once()[0]['state'] == 'succeeded'
            assert len(attempts) == 2
        finally:
            stop.set()
            if scheduler._pool is not None:
                scheduler._pool.shutdown(wait=True)
    assert not any(t.name in ('fleet-http-deadline', 'platform-run-heartbeat') or t.name.startswith('run-lease-') for t in threads())
