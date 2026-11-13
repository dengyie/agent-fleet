"""Real Chromium fixture coverage, isolated from the Node runtime integration."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


BRIDGE = Path(__file__).parent / "fixtures" / "browser" / "chromium_loopback_fixture.cjs"
HTML = b"""<!doctype html>
<html><head><title>Loopback fixture</title><link rel="icon" href="data:,">
<link rel="stylesheet" href="/fixture.css"></head>
<body><main><h1>Chromium fixture rendered</h1><img src="/pixel.svg">
<label>Credential fixture<input value="FIXTURE_SECRET_VALUE"></label></main></body></html>"""
CSS = b"body { background: #edf5ed; color: #173d25; font: 24px sans-serif; }"
SVG = b"<svg xmlns='http://www.w3.org/2000/svg' width='16' height='16'><rect width='16' height='16' fill='#277a43'/></svg>"


@pytest.fixture
def chromium_settings() -> tuple[str, str, str]:
    module = os.environ.get("FLEET_PLAYWRIGHT_MODULE")
    if not module:
        pytest.skip("FLEET_PLAYWRIGHT_MODULE is required for Chromium fixture acceptance")
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for Chromium fixture acceptance")
    probe = subprocess.run(
        [node, "-e", "require(process.env.FLEET_PLAYWRIGHT_MODULE)"],
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "FLEET_PLAYWRIGHT_MODULE": module},
        check=False,
    )
    if probe.returncode:
        pytest.skip("Configured Playwright module is unavailable")
    return module, node, os.environ.get("FLEET_BROWSER_CHANNEL", "chrome")


@pytest.fixture
def fixture_servers():
    allowed_hits: list[str] = []
    forbidden_hits: list[str] = []

    class ForbiddenHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            forbidden_hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, _format: str, *args: object) -> None:
            return None

    forbidden = ThreadingHTTPServer(("127.0.0.1", 0), ForbiddenHandler)
    forbidden_origin = f"http://127.0.0.1:{forbidden.server_port}"

    class FixtureHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            allowed_hits.append(self.path)
            if self.path == "/":
                status, content_type, body = 200, "text/html; charset=utf-8", HTML
            elif self.path == "/fixture.css":
                status, content_type, body = 200, "text/css; charset=utf-8", CSS
            elif self.path == "/pixel.svg":
                status, content_type, body = 200, "image/svg+xml", SVG
            elif self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", forbidden_origin + "/must-not-be-requested")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            else:
                status, content_type, body = 404, "text/plain; charset=utf-8", b"not found"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *args: object) -> None:
            return None

    allowed = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    servers = (forbidden, allowed)
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in servers
    ]
    for thread in threads:
        thread.start()
    try:
        yield (
            f"http://127.0.0.1:{allowed.server_port}",
            forbidden_origin,
            allowed_hits,
            forbidden_hits,
        )
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)


def test_real_chromium_loopback_fixture_enforces_local_scope_and_capture_budget(
    chromium_settings: tuple[str, str, str], fixture_servers,
) -> None:
    module, node, channel = chromium_settings
    origin, forbidden_origin, allowed_hits, forbidden_hits = fixture_servers
    process = subprocess.Popen(
        [
            node,
            str(BRIDGE),
            origin,
            origin + "/redirect",
            forbidden_origin + "/must-not-be-requested",
        ],
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "FLEET_PLAYWRIGHT_MODULE": module,
            "FLEET_BROWSER_CHANNEL": channel,
        },
    )
    try:
        stdout, stderr = process.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate(timeout=5)
        pytest.fail("Chromium fixture exceeded its 60 second deadline")

    assert process.returncode == 0, stderr[-2000:]
    payload = json.loads(stdout)
    screenshot = base64.b64decode(payload["pngBase64"], validate=True)
    assert payload["closed"] is True
    assert payload["title"] == "Loopback fixture"
    assert "Chromium fixture rendered" in payload["text"]
    assert "FIXTURE_SECRET_VALUE" not in payload["text"]
    assert payload["bodyBackground"] == "rgb(237, 245, 237)"
    assert payload["imageLoaded"] is True
    assert payload["imageWidth"] == 16
    assert payload["imageHeight"] == 16
    assert payload["maskedInputCenterPixel"] == [0, 0, 0]
    assert screenshot.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(screenshot) <= 256 * 1024
    assert payload["crossOriginNavigation"] == "origin_forbidden"
    assert payload["redirectNavigation"] == "redirect_denied"
    assert "/" in allowed_hits
    assert "/fixture.css" in allowed_hits
    assert "/pixel.svg" in allowed_hits
    assert "/redirect" in allowed_hits
    assert forbidden_hits == []
