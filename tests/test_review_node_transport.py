"""Review fix #8: credential-bearing outbound requests.

* 3xx（含跨主机 302）绝不跟随，凭据头不会被带到 Location 指向的主机。
* 只发 https；明文 http 仅限 loopback 或显式 allow_insecure。
* agent-self-report 经 tools.transport 统一出站。
"""
import importlib.util
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tools.platform.services import http as service_http
from tools.transport import (InsecureTransportError, NoRedirectHandler,
                             Transport, require_secure_url)


def _serve(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def redirect_pair():
    seen = []

    class Target(BaseHTTPRequestHandler):
        def _any(self):
            seen.append(dict(self.headers))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        do_GET = do_POST = _any

        def log_message(self, *args):
            pass

    target = _serve(Target)
    # 跨主机：源是 127.0.0.1，Location 指向 localhost（不同 host 名）
    location = f"http://localhost:{target.server_address[1]}/stolen"

    class Origin(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    origin = _serve(Origin)
    yield f"http://127.0.0.1:{origin.server_address[1]}", seen
    origin.shutdown()
    target.shutdown()


def test_cross_host_302_does_not_forward_credential(redirect_pair):
    base, seen = redirect_pair
    transport = Transport(mode="direct", attempts=1)
    status, _ = transport.post_json(base + "/api/x", {"a": 1},
                                    {"X-Runner-Credential": "m:secret"}, timeout=5)
    assert status == 302
    assert seen == []


def _self_report():
    path = Path(__file__).resolve().parents[1] / "tools" / "agent-self-report.py"
    spec = importlib.util.spec_from_file_location("agent_self_report_review", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_self_report_cross_host_302_does_not_forward_token(redirect_pair):
    base, seen = redirect_pair
    module = _self_report()
    import urllib.error
    with pytest.raises(urllib.error.HTTPError) as info:
        module.send_payload(base, "tok-secret", {"machine": "m"})
    assert info.value.code == 302
    assert seen == []


def test_https_required_except_loopback():
    assert require_secure_url("https://hub.example.com/api")
    assert require_secure_url("http://127.0.0.1:8080/api")
    assert require_secure_url("http://localhost/api")
    assert require_secure_url("http://[::1]:9/api")
    for bad in ("http://hub.example.com/api", "http://10.0.0.5/api",
                "ftp://hub.example.com", "file:///etc/passwd", "hub.example.com"):
        with pytest.raises(InsecureTransportError):
            require_secure_url(bad)
    assert require_secure_url("http://10.0.0.5/api", allow_insecure=True)


def test_transport_refuses_plain_http_without_sending(monkeypatch):
    calls = []
    monkeypatch.setattr(Transport, "_open",
                        lambda self, opener, req, timeout: calls.append(req))
    status, payload = Transport().post_json("http://hub.example.com/api", {},
                                            {"X-Runner-Credential": "m:s"})
    assert (status, payload) == (0, {"error": "insecure_transport_url"})
    status, _ = Transport().post_bytes("http://hub.example.com/api", b"x", {})
    assert status == 0 and calls == []


def test_self_report_refuses_plain_http_endpoint(monkeypatch):
    module = _self_report()
    calls = []
    monkeypatch.setattr(Transport, "_open",
                        lambda self, opener, req, timeout: calls.append(req))
    with pytest.raises(InsecureTransportError):
        module.send_payload_with_retry("http://hub.example.com", "tok", {}, attempts=2, backoff=0)
    assert calls == []


def test_service_http_reuses_transport_no_redirect_handler():
    assert service_http._NoRedirectHandler is NoRedirectHandler
