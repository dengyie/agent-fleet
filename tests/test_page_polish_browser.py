"""Responsive layout acceptance for the shared console page grammar."""
import os
from pathlib import Path
import subprocess
from threading import Thread

import pytest
from werkzeug.serving import make_server

from hub.bootstrap import create_app
from hub.config import FleetConfig

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('viewport', ['desktop', 'mobile'])
def test_console_pages_fit_the_viewport(tmp_path, viewport):
    if not os.environ.get('FLEET_PLAYWRIGHT_MODULE'):
        pytest.skip('Set FLEET_PLAYWRIGHT_MODULE to run Chromium acceptance')
    app = create_app(FleetConfig.from_root(
        tmp_path, frontend_dir=ROOT / 'frontend', dev_operator='ui@example.test', platform_enabled=True,
        platform_workflows_enabled=True, platform_fleet_mcp_enabled=True,
        platform_mcp_connectors_enabled=True,
    ))
    server = make_server('127.0.0.1', 0, app, threaded=True)
    serving = Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        result = subprocess.run([
            'node', str(ROOT / 'tests/fixtures/frontend/page_polish_browser.cjs'),
            f'http://127.0.0.1:{server.server_port}', viewport,
        ], capture_output=True, text=True, timeout=90,
        env={**os.environ, 'FLEET_PLAYWRIGHT_MODULE': os.environ['FLEET_PLAYWRIGHT_MODULE']})
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.shutdown(); server.server_close(); serving.join(5)
