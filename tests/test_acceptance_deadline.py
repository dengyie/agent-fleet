"""Wall-clock deadlines apply to HTTP headers, bodies and all model checks."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Event, Thread
import time

import pytest

from tools.platform.acceptance_check import AccountClient, check_model


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
