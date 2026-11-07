import os
from pathlib import Path
import subprocess
from threading import Event, Thread

import pytest
from werkzeug.serving import make_server

from support.fake_gh import fake_gh  # noqa: F401
from test_plugin_node import node_env  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def test_plugin_center_device_login_recovery_and_accounts(node_env, tmp_path):
    if not os.environ.get('FLEET_PLAYWRIGHT_MODULE'):
        pytest.skip('Set FLEET_PLAYWRIGHT_MODULE for real-browser acceptance')
    app, client, runtime, _, _ = node_env
    client.put('/api/platform/v1/plugins/github-cli', json={'enabled': False, 'revision': 1})
    client.put('/api/platform/v1/plugins/github-cli/nodes/vm', json={'enabled': False})
    stop = Event()
    def poll():
        while not stop.is_set():
            runtime.poll_once(limit=10)
            stop.wait(0.05)
    polling = Thread(target=poll, daemon=True)
    polling.start()
    server = make_server('127.0.0.1', 0, app, threaded=True)
    serving = Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        screenshot = Path(os.environ.get('FLEET_SCREENSHOTS', tmp_path)) / 'plugin-center.png'
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(['node', str(ROOT / 'tests/fixtures/frontend/plugins_browser.cjs'),
                                 f'http://127.0.0.1:{server.server_port}'],
                                capture_output=True, text=True, timeout=90,
                                env={**os.environ, 'FLEET_PLUGIN_SCREENSHOT': str(screenshot)})
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        stop.set(); polling.join(5)
        server.shutdown(); server.server_close(); serving.join(5)
