"""Real HTTP probes for credential redirects and bounded artifact responses."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
from typing import Any

import pytest

from tools.transport import Transport


@pytest.fixture
def peer() -> Any:
    state = type("Peer", (), {})()
    state.status, state.body, state.extra = 200, b'{"ok":true}', {}
    state.requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            state.requests.append((self.command, self.path, body))
            self.send_response(state.status)
            for name, value in state.extra.items():
                self.send_header(name, value)
            if "Content-Length" not in state.extra:
                self.send_header("Content-Length", str(len(state.body)))
            self.end_headers()
            self.wfile.write(state.body)
        do_POST = do_GET
        do_PUT = do_GET
        def log_message(self, *args: Any) -> None:
            return None
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever)
    thread.start()
    state.url = f"http://127.0.0.1:{httpd.server_port}/artifact"
    try:
        yield state
    finally:
        httpd.shutdown()
        thread.join(5)
        httpd.server_close()
        assert not thread.is_alive()


@pytest.mark.parametrize("method", ["POST", "PUT", "GET"])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_bounded_artifact_request_never_follows_credential_redirect(peer: Any, method: str, status: int) -> None:
    peer.status, peer.body, peer.extra = status, b"", {"Location": "/credential-sink"}
    result = Transport(mode="direct", attempts=3).request_bounded_json(method, peer.url,
        b"payload" if method != "GET" else None, {"X-Platform-Node-Credential": "synthetic"},
        deadline=time.monotonic() + 5, should_cancel=lambda: False)
    assert result == (status, {"error": "transport_redirect_forbidden"})
    assert len(peer.requests) == 1 and peer.requests[0][1] == "/artifact"


@pytest.mark.parametrize("body,headers", [(b"x" * 65537, {}), (b"[]", {}),
    (b"not-json", {}), (b'{"ok":true}', {"Content-Encoding": "gzip"}),
    (b'{"ok":true}', {"Transfer-Encoding": "chunked"}),
    (b'{"ok":true}', {"Content-Length": "999999999999999999999"})])
def test_bounded_artifact_response_rejects_oversize_encoded_and_invalid_json(peer: Any, body: bytes, headers: Any) -> None:
    peer.body, peer.extra = body, headers
    with pytest.raises(ValueError):
        Transport(mode="direct", attempts=1).request_bounded_json("POST", peer.url, b"{}", {},
            deadline=time.monotonic() + 5, should_cancel=lambda: False)
    assert len(peer.requests) == 1


def test_bounded_artifact_response_preserves_http_failure_without_retry(peer: Any) -> None:
    peer.status, peer.body = 409, b'{"error":"conflict"}'
    result = Transport(mode="direct", attempts=3).request_bounded_json("POST", peer.url, b"{}", {},
        deadline=time.monotonic() + 5, should_cancel=lambda: False)
    assert result == (409, {"error": "conflict"})
    assert len(peer.requests) == 1


@pytest.mark.parametrize("cancelled", [True, False])
def test_bounded_artifact_deadline_and_cancel_prevent_network(peer: Any, cancelled: bool) -> None:
    with pytest.raises(OSError):
        Transport(mode="direct", attempts=1).request_bounded_json("POST", peer.url, b"{}", {},
            deadline=time.monotonic() - 1 if not cancelled else time.monotonic() + 5,
            should_cancel=lambda: cancelled)
    assert not peer.requests
