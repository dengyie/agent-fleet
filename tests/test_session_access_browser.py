"""Real HTTP, queue and browser checks; all process identities are synthetic."""
import os
from pathlib import Path
import subprocess
from threading import Thread

import pytest
from werkzeug.serving import make_server

from tests.test_session_access_consistency import app, adopted, OWNER  # noqa: F401
from tests.test_session_interactions import event

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('scenario', ['authority', 'resume', 'pagination', 'refresh_failure', 'receipt_retry', 'long_receipt', 'resume_inflight', 'admission_feedback'])
def test_session_access_browser(app, scenario):
    if not os.environ.get('FLEET_PLAYWRIGHT_MODULE'):
        pytest.skip('Set FLEET_PLAYWRIGHT_MODULE for Chromium acceptance')
    old = adopted(app)
    services = app.extensions['fleet']['services']
    services['adoptions'].revoke(old.session_id, OWNER)
    current = adopted(app, captured=False)
    if scenario != 'authority':
        for seq in range(1, 104):
            row = event(seq, 'assistant_message', {'text': f'Access event {seq}', 'is_complete': True})
            row['session_id'] = current.session_id
            services['sessions'].ingest_events([row])
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(['node', str(ROOT / 'tests/fixtures/frontend/session_access_browser.cjs'),
            f'http://127.0.0.1:{server.server_port}', scenario, old.session_id, current.session_id],
            capture_output=True, text=True, timeout=65)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
