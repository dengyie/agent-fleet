"""Browser module + real local HTTP + SQLite regression for turn submission."""
import json
from pathlib import Path
import shutil
import subprocess
from threading import Thread

import pytest
from werkzeug.serving import make_server

from hub.bootstrap import create_app
from hub.config import FleetConfig

ROOT = Path(__file__).resolve().parents[1]
OWNER = 'owner@example.test'


@pytest.mark.parametrize('scenario', ['retry', 'model'])
def test_assistant_submission_uses_selected_model_and_stable_request(tmp_path, scenario):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js required to execute browser modules')
    app = create_app(FleetConfig.from_root(tmp_path, dev_operator=OWNER, platform_enabled=True))
    repo = app.extensions['fleet']['platform_repository']
    for profile in ('model-default', 'model-selected'):
        repo.upsert_model(OWNER, {'profile_id': profile, 'provider': 'deterministic', 'model': profile})
    repo.upsert_workspace(OWNER, {'workspace_id': 'home', 'root_path': str(tmp_path / 'workspace')})
    repo.update_defaults(OWNER, {'model_profile_id': 'model-default', 'workspace_id': 'home'}, 0)
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run([
            node, str(ROOT / 'tests/fixtures/frontend/assistant_submission.mjs'),
            f'http://127.0.0.1:{server.server_port}', str(ROOT / 'frontend/assets/views/assistant.js'), scenario,
        ], capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        observed = json.loads(result.stdout)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
    if scenario == 'retry':
        assert observed['requests'][0] == observed['requests'][1]
        assert observed['replies'][0]['run']['run_id'] == observed['replies'][1]['run']['run_id']
        conversation_id = observed['replies'][0]['conversation_id']
        assert len(repo.get_conversation(OWNER, conversation_id)['runs']) == 1
    assert observed['requests'][0].get('overrides', {}).get('model_profile_id') == 'model-selected'
    assert observed['replies'][-1]['run']['config']['model_profile_id'] == 'model-selected'
    assert '本次模型：model-selected' in observed['text']
