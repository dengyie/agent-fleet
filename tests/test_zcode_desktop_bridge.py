import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tools.zcode_desktop import BridgeError, ZCodeDesktopClient


@pytest.mark.filterwarnings('error::pytest.PytestUnhandledThreadExceptionWarning')
def test_desktop_bridge_contracts():
    node = shutil.which('node')
    assert node, 'Node is required for the ZCode desktop bridge contracts'
    result = subprocess.run([node, '--test', 'tests/fixtures/zcode_desktop_bridge.test.mjs'],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True,
                            text=True, encoding='utf-8', timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('url', ['https://remote.invalid', 'http://localhost:81',
                                'http://127.0.0.1:81/redirect', 'http://u@127.0.0.1:81'])
def test_client_never_sends_local_capability_to_external_endpoint(tmp_path, url):
    path = tmp_path / 'bridge.json'
    path.write_text(json.dumps({'url': url, 'protocol': 1, 'active': True, 'token': 'x' * 64}))
    path.chmod(0o600)
    with pytest.raises(BridgeError, match='desktop_bridge_unavailable'):
        ZCodeDesktopClient(path).request('/health')


def test_missing_extension_is_not_cli_fallback(tmp_path):
    with pytest.raises(BridgeError, match='desktop_bridge_unavailable'):
        ZCodeDesktopClient(tmp_path / 'absent').send('sess_existing', 'same-request', 'hello')
