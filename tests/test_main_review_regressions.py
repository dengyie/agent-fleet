"""Behavioral regressions from the main review: real processes and HTTP."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from urllib.request import urlopen

import pytest
from werkzeug.serving import make_server

from hub.bootstrap import create_app
from hub.config import FleetConfig
from tools.adapters.base import BaseAdapter
from tools.probe import discovery
from tests.support.processes import assert_process_exited


@pytest.mark.parametrize('abort', [False, True])
def test_adapter_bounds_descendant_pipe_after_leader_exit(tmp_path, abort):
    child = tmp_path / 'child.py'
    child.write_text(
        "import os,pathlib,time\n"
        "pathlib.Path('child.pid').write_text(str(os.getpid()))\n"
        "time.sleep(7)\n", encoding='utf-8')
    code = (
        "import pathlib,subprocess,sys,time\n"
        "subprocess.Popen([sys.executable, 'child.py'])\n"
        "while not pathlib.Path('child.pid').exists(): time.sleep(.01)\n"
        "print('leader finished', flush=True)\n"
    )
    started = time.monotonic()
    result = BaseAdapter([sys.executable, '-c', code], timeout_s=1).run(
        '', tmp_path, should_abort=lambda: abort and time.monotonic() - started > .5)
    assert time.monotonic() - started < 4
    assert result.exit_code == (130 if abort else 124)
    assert result.aborted == abort
    assert result.timed_out == (not abort)
    assert 'leader finished' in result.log_tail
    assert_process_exited(int((tmp_path / 'child.pid').read_text()))


@pytest.mark.skipif(os.name == 'nt', reason='POSIX SIGTERM handler semantics')
def test_adapter_timeout_cannot_be_overridden_by_graceful_exit(tmp_path):
    result = BaseAdapter([sys.executable, '-c',
        "import signal,sys,time; signal.signal(signal.SIGTERM, lambda *a: sys.exit(0)); "
        "print('ready',flush=True); time.sleep(7)"], timeout_s=1).run('', tmp_path)
    assert result.timed_out
    assert result.exit_code == 124
    assert 'ready' in result.log_tail


def test_runner_machine_binding_includes_idempotent_result(tmp_path):
    app = create_app(FleetConfig.from_root(tmp_path, runner_credentials={
        'machine-a': 'secret-a', 'machine-b': 'secret-b'}))
    repo = app.extensions['fleet']['services']['runner'].repo
    row, _ = repo.create_task(machine='machine-a', agent_type='codex',
        project='fixture', instruction='fixture', requested_by='operator')
    client = app.test_client()
    a = {'X-Runner-Credential': 'machine-a:secret-a'}
    b = {'X-Runner-Credential': 'machine-b:secret-b'}
    lease = client.post('/api/commands/poll', headers=a, json={}).get_json()['task']
    path = '/api/commands/' + lease['attempt_id']
    body = {'nonce': lease['nonce'], 'exit_code': 0, 'machine': 'machine-a'}
    assert client.post(path + '/heartbeat', headers=b, json=body).status_code == 409
    assert client.post(path + '/result', headers=b, json=body).status_code == 409
    assert repo.get_task(row['task_id'])['state'] == 'leased'
    assert client.post(path + '/heartbeat', headers=a, json=body).status_code == 200
    assert client.post(path + '/result', headers=a, json=body).status_code == 200
    assert client.post(path + '/result', headers=a, json=body).get_json()['stored'] is False
    # The replay path must enforce identity as well as the first write.
    app.config['RUNNER_CREDENTIALS'] = {'machine-b': 'secret-b'}
    assert client.post(path + '/result', headers=b, json=body).status_code == 409
    assert client.post(path + '/result', headers=a, json=body).status_code == 403


def test_unicode_artifact_download_over_real_http(tmp_path):
    app = create_app(FleetConfig.from_root(tmp_path,
        dev_operator='owner@example.test', platform_enabled=True))
    store = app.extensions['fleet']['services']['platform_artifacts']
    source = tmp_path / '报告 🧪.md'
    source.write_text('报告内容', encoding='utf-8')
    manifest = store.put_file('owner@example.test', 'workspace-a', source)
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = (f'http://127.0.0.1:{server.server_port}/api/platform/v1/artifacts/'
               f"{manifest['artifact_id']}/content?workspace_id=workspace-a")
        with urlopen(url, timeout=3) as response:
            assert response.read() == source.read_bytes()
            assert "filename*=UTF-8''" in response.headers['Content-Disposition']
            assert '%E6%8A%A5%E5%91%8A' in response.headers['Content-Disposition']
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


SID = '019a5123-1234-7123-8123-123456789abc'


@pytest.mark.parametrize('command', [
    f'codex resume {SID}', f'codex exec resume {SID}',
    f'codex exec resume {SID} -', f'codex exec resume --json {SID} prompt',
])
def test_codex_explicit_resume_uses_rollout_metadata(tmp_path, monkeypatch, command):
    day = tmp_path / '2026' / '11' / '05'
    day.mkdir(parents=True)
    rollout = day / f'rollout-2026-11-05T12-00-00-{SID}.jsonl'
    rollout.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': SID}}) + '\n', encoding='utf-8')
    monkeypatch.setattr(discovery.os.path, 'expanduser',
        lambda path: str(tmp_path) if path == '~/.codex/sessions' else path)
    assert discovery._codex_resume_token(command) == SID
    assert discovery._codex_native_path(command) == str(rollout)


def test_codex_rollout_binding_rejects_mismatched_or_ambiguous_files(tmp_path, monkeypatch):
    day = tmp_path / '2026' / '11' / '05'
    day.mkdir(parents=True)
    first = day / f'rollout-2026-11-05T12-00-00-{SID}.jsonl'
    first.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'other'}}) + '\n',
                     encoding='utf-8')
    monkeypatch.setattr(discovery.os.path, 'expanduser',
        lambda path: str(tmp_path) if path == '~/.codex/sessions' else path)
    command = f'codex exec resume --model fixture {SID}'
    assert discovery._codex_native_path(command) is None
    first.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': SID}}) + '\n',
                     encoding='utf-8')
    second = day / f'rollout-2026-11-05T13-00-00-{SID}.jsonl'
    second.write_text(first.read_text(encoding='utf-8'), encoding='utf-8')
    assert discovery._codex_native_path(command) is None


def test_frontend_deadline_covers_body_read():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js required for real fetch regression')
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node,
        str(root / 'tests/fixtures/frontend/request_deadline.mjs')],
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
