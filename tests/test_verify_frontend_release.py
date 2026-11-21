"""The deployment gate must inspect public bytes, not just a current manifest."""
import json
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'deploy/verify_frontend_release.py'


@pytest.mark.parametrize('fault', [None, 'stale-css', 'stale-entry', 'wrong-version', 'missing-js'])
def test_public_frontend_verification(tmp_path, fault):
    release = tmp_path / 'release'
    release.mkdir()
    files = {'index.html': b'<script src="/app.js?v=release-2"></script>',
             'app.js': b"import './nested.js?v=release-2';",
             'nested.js': b'window.currentRelease = true;',
             'style.css': b'body { color: green; }'}
    manifest = json.dumps({'version': 'release-2', 'files': list(files)}).encode()
    for name, content in {**files, 'manifest.json': manifest}.items():
        (release / name).write_bytes(content)
    public = {'/': files['index.html'], '/manifest.json': manifest,
              **{'/' + name + '?v=release-2': data for name, data in files.items()}}
    if fault == 'stale-css':
        public['/style.css?v=release-2'] = b'body { color: red; }'
    elif fault == 'stale-entry':
        public['/'] = b'<script src="/app.js"></script>'
    elif fault == 'wrong-version':
        public['/manifest.json'] = manifest.replace(b'release-2', b'release-1')
    elif fault == 'missing-js':
        del public['/nested.js?v=release-2']

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            content = public.get(self.path)
            self.send_response(200 if content is not None else 404)
            self.end_headers()
            self.wfile.write(content or b'not found')

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run([
            'python3', str(SCRIPT), '--release', str(release), '--version', 'release-2',
            '--origin', f'http://127.0.0.1:{server.server_port}',
        ], text=True, capture_output=True, timeout=30)
        if fault is None:
            assert result.returncode == 0, result.stdout + result.stderr
            assert json.loads(result.stdout)['verified_resources'] == len(files) + 2
        else:
            assert result.returncode != 0
            assert 'public_frontend_mismatch' in result.stderr or 'HTTP Error 404' in result.stderr
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
