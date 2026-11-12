"""Real command/HTTP peers for the repository-owned model reviewer."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

import pytest

ENTRY = Path(__file__).resolve().parents[1] / 'hub/workflow_model_review.py'
KEY = 'review-only-synthetic-key'
REQUEST = {'schema_version': 1, 'source_revision': {'repository_id': 'repo-a',
    'base_commit': 'a' * 40, 'head_commit': 'b' * 40, 'tree_id': 'c' * 40},
    'subject_digest': 'd' * 64, 'executor': {'id': 'independent-model', 'kind': 'model', 'model_id': 'review-model'}}


@contextmanager
def peer(*, verdict='passed', failure=None):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append((self.path, dict(self.headers), body))
            if failure == 'timeout':
                time.sleep(2)
            if failure in ('http', 'redirect'):
                self.send_response(503 if failure == 'http' else 307)
                self.send_header('Location', '/should-not-replay')
                self.end_headers()
                self.wfile.write(KEY.encode())
                return
            request = json.loads(body['messages'][1]['content'])['request']
            report = {**request, 'verdict': verdict, 'findings': []}
            if failure == 'identity': report['executor'] = {**request['executor'], 'id': 'forged'}
            if failure == 'subject': report['subject_digest'] = 'e' * 64
            if failure == 'findings': report['findings'] = [{'severity': 'error'}]
            content = json.dumps(report)
            if failure == 'json': content = 'not-json'
            if failure == 'duplicate': content = content[:-1] + ', "verdict":"passed"}'
            if failure == 'oversize': content = 'x' * 65537
            message = {'role': 'assistant', 'content': content}
            if failure == 'tool': message['tool_calls'] = [{'id': 'call-x', 'type': 'function',
                'function': {'name': 'unknown_tool', 'arguments': '{}'}}]
            data = json.dumps({'choices': [{'message': message,
                'finish_reason': 'length' if failure == 'length' else 'stop'}]}).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data))); self.end_headers()
            try: self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try: yield f'http://127.0.0.1:{server.server_port}/v1/chat/completions', calls
    finally: server.shutdown(); server.server_close(); thread.join(timeout=3)


def prepare(root, endpoint, *, timeout=5):
    source = root / 'source'; source.mkdir()
    (source / 'TASK.md').write_text('Implement the requested behavior.\n', encoding='utf-8')
    (source / 'module.py').write_text('answer = 42\n', encoding='utf-8')
    control = root / 'control'; control.mkdir()
    (control / 'request.json').write_text(json.dumps(REQUEST), encoding='utf-8')
    scratch = root / 'scratch'; scratch.mkdir()
    key = root / 'review.key'; key.write_text(KEY + '\n', encoding='utf-8')
    config = root / 'review.json'
    value = {'schema_version': 1, 'endpoint': endpoint, 'model_id': 'review-model',
        'api_key_file': str(key.resolve()), 'timeout_seconds': timeout,
        'max_source_bytes': 262144, 'instructions': 'Review correctness and requirements. Security hardening is deferred.'}
    config.write_text(json.dumps(value), encoding='utf-8')
    return source, control, scratch, config, value


def command(config, mode='run'):
    return [sys.executable, '-I', str(ENTRY), mode, '--config', str(config.resolve()),
        '--config-sha256', hashlib.sha256(config.read_bytes()).hexdigest()]


def run(root, source, control, scratch, config, *, argv=None):
    env = {'PATH': os.defpath, 'HOME': str(scratch), 'TMPDIR': str(scratch),
        'FLEET_REVIEW_REQUEST': str(control / 'request.json'), 'FLEET_REVIEW_SOURCE': str(source)}
    return subprocess.run(argv or command(config), env=env, cwd=root, capture_output=True, timeout=12)


def test_model_review_isolated_script_help(tmp_path):
    result = subprocess.run([sys.executable, '-I', str(ENTRY), '--help'], cwd=tmp_path,
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert 'launch' in result.stdout and 'run' in result.stdout
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('verdict', ['passed', 'failed', 'inconclusive'])
def test_model_review_real_http_preserves_verdict_and_complete_source(tmp_path, verdict):
    with peer(verdict=verdict) as (endpoint, calls):
        source, control, scratch, config, _value = prepare(tmp_path, endpoint)
        # A single provider message would silently truncate this escaped file.
        text = ('"\\\n汉字' * 15000) + 'COMPLETE-SOURCE-END'
        (source / 'large.txt').write_text(text, encoding='utf-8')
        before = {p.name: p.read_bytes() for p in source.iterdir()}
        result = run(tmp_path, source, control, scratch, config)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {**REQUEST, 'verdict': verdict, 'findings': []}
    assert len(calls) == 1 and calls[0][0] == '/v1/chat/completions'
    headers, body = calls[0][1:]
    assert headers['Authorization'] == 'Bearer ' + KEY
    assert body['model'] == 'review-model' and body['tools'] == [] and body['stream'] is False
    files = [json.loads(item['content']) for item in body['messages'][2:]]
    large = [item for item in files if item['path'] == 'large.txt']
    assert ''.join(item['text'] for item in large) == text
    assert all(len(item['content']) <= 128 * 1024 for item in body['messages'])
    assert {item['path'] for item in files} == set(before)
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before
    assert KEY.encode() not in result.stdout + result.stderr


@pytest.mark.parametrize('failure', ['http', 'redirect', 'identity', 'subject', 'findings', 'json',
    'duplicate', 'oversize', 'tool', 'length', 'timeout'])
def test_model_review_bad_provider_never_publishes_or_replays(tmp_path, failure):
    with peer(failure=failure) as (endpoint, calls):
        source, control, scratch, config, _ = prepare(tmp_path, endpoint, timeout=1)
        result = run(tmp_path, source, control, scratch, config)
    assert result.returncode != 0
    assert result.stdout == b'' and len(calls) == 1
    assert KEY.encode() not in result.stderr
    assert b'Traceback' not in result.stderr


@pytest.mark.parametrize('failure', ['config_digest', 'model', 'binary', 'source_limit', 'symlink', 'request', 'empty_key'])
def test_model_review_invalid_inputs_fail_before_network(tmp_path, failure):
    with peer() as (endpoint, calls):
        source, control, scratch, config, value = prepare(tmp_path, endpoint)
        if failure == 'model': value['model_id'] = 'other-model'
        if failure == 'binary': (source / 'image.bin').write_bytes(b'\xff\x00')
        if failure == 'source_limit': value['max_source_bytes'] = 1
        if failure == 'symlink': (source / 'outside').symlink_to(config)
        if failure == 'request': (control / 'request.json').write_text('{}', encoding='utf-8')
        if failure == 'empty_key': Path(value['api_key_file']).write_text('\n', encoding='utf-8')
        config.write_text(json.dumps(value), encoding='utf-8')
        argv = command(config)
        if failure == 'config_digest': argv[-1] = '0' * 64
        result = run(tmp_path, source, control, scratch, config, argv=argv)
    assert result.returncode != 0 and not calls
    assert result.stdout == b'' and b'Traceback' not in result.stderr


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux namespace launcher')
@pytest.mark.parametrize('verdict', ['passed', 'failed'])
def test_model_review_linux_original_run_worker_and_restart(tmp_path, verdict):
    from fleet_core.workflow.contract_types import CatalogRef
    from fleet_core.workflow.review import CommandReviewAdapter
    from fleet_core.workflow.review_protocol import ReviewIdentity
    from hub.infrastructure.workflow_run_executor import RunStepExecutor
    from test_workflow_review_execution import ReviewFixture
    from test_workflow_repository import OWNER

    fixture_root = tmp_path / 'fixture'; fixture_root.mkdir()
    fixture = ReviewFixture(fixture_root)
    with peer(verdict=verdict) as (endpoint, calls):
        setup = tmp_path / 'setup'; setup.mkdir()
        _source, _control, _scratch, config, _ = prepare(setup, endpoint)
        launcher = command(config, 'launch') + ['--bwrap', shutil.which('bwrap'),
            '--source', '{source}', '--control', '{control}', '--scratch', '{scratch}', '--']
        fixture.adapter = CommandReviewAdapter(CatalogRef('registered-http-review', 1),
            ReviewIdentity('independent-model', 'model', 'review-model'), tuple(command(config)),
            launcher=tuple(launcher), timeout_seconds=10)
        fixture.target = replace(fixture.target, adapter_ref=fixture.adapter.spec.adapter_ref,
            review_adapter=fixture.adapter.spec)
        fixture.executor = RunStepExecutor(fixture.repository, fixture.target, clock=fixture.clock)
        observation = fixture.dispatch()
        result = fixture.worker().run_once(OWNER)
        assert result['state'] == 'succeeded', result
        assert fixture.worker().run_once(OWNER) is None
        assert len(calls) == 1
        assert fixture.evidence()[0]['result'] == verdict
        import asyncio
        assert asyncio.run(fixture.executor.inspect(observation.binding)).state == 'succeeded'


def test_model_review_same_config_handles_fresh_workdirs(tmp_path):
    with peer() as (endpoint, calls):
        first = tmp_path / 'first'; first.mkdir()
        source, control, scratch, config, _ = prepare(first, endpoint)
        assert run(tmp_path, source, control, scratch, config).returncode == 0
        second = tmp_path / 'second'; second.mkdir()
        source2, control2, scratch2, _config2, _ = prepare(second, endpoint)
        result = run(tmp_path, source2, control2, scratch2, config)
        assert result.returncode == 0, result.stderr
    assert len(calls) == 2
    assert not list(scratch.iterdir()) and not list(scratch2.iterdir())


def test_model_review_argv_registers_with_normal_hub(tmp_path):
    from hub.bootstrap import create_app
    from hub.config import FleetConfig
    from hub.infrastructure.platform_db import PlatformRepository
    from test_workflow_repository import OWNER, WORKSPACE
    from test_workflow_startup import registration, seed

    manifest = registration(OWNER, WORKSPACE)
    config = tmp_path / 'review.json'; config.write_text('{}', encoding='utf-8')
    row = manifest['coding_delivery'][0]['review']
    row.update(argv=command(config), launcher=command(config, 'launch') + ['--bwrap', '/usr/bin/bwrap',
        '--source', '{source}', '--control', '{control}', '--scratch', '{scratch}', '--'],
        kind='model', model_id='review-model')
    path = tmp_path / 'manifest.json'; path.write_text(json.dumps(manifest), encoding='utf-8')
    db = tmp_path / 'platform.db'; seed(PlatformRepository(db), tmp_path)
    app = create_app(FleetConfig.from_root(tmp_path, platform_enabled=True, platform_db=db,
        platform_workflows_enabled=True, platform_workflow_registration_file=str(path)))
    coordinator = app.extensions['fleet']['services']['workflow_coordinator']
    assert len(coordinator.dispatchers) == 3 and not coordinator.dispatch_enabled


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux namespace launcher')
def test_model_review_linux_mounts_keep_source_readonly_and_only_own_key(tmp_path, monkeypatch):
    from argparse import Namespace
    from hub.workflow_model_review import _launch

    with peer() as (endpoint, _calls):
        source, control, scratch, config, value = prepare(tmp_path, endpoint)
        unrelated = tmp_path / 'hub-credential'; unrelated.write_text('unrelated-secret', encoding='utf-8')
        captured = []
        monkeypatch.setattr(os, 'execv', lambda _path, argv: captured.append(argv))
        options = Namespace(config=str(config.resolve()), config_sha256=command(config)[-1],
            bwrap=shutil.which('bwrap'), source=str(source), control=str(control), scratch=str(scratch))
        _launch(options, command(config))
        probe = '''import json,os,pathlib,socket,sys
source,config,key,unrelated,scratch,endpoint = map(pathlib.Path, sys.argv[1:])
assert source.joinpath('module.py').read_text() == 'answer = 42\\n'
assert key.read_text().strip() == 'review-only-synthetic-key'
assert config.is_file() and not unrelated.exists()
for target in (source/'module.py', config, key):
    try: target.write_text('tampered')
    except OSError: pass
    else: raise AssertionError('readonly mount was writable')
assert 'FLEET_TEST_SECRET' not in os.environ
(scratch/'allowed').write_text('scratch')
sock=socket.create_connection(('127.0.0.1', int(str(endpoint))),timeout=2);sock.close()
print('verified-model-review-mounts')
'''
        port = str(endpoint.split(':')[-1].split('/')[0])
        argv = captured[0][:-len(command(config))] + [sys.executable, '-I', '-c', probe,
            str(source), str(config), value['api_key_file'], str(unrelated), str(scratch), port]
        result = subprocess.run(argv, env={**os.environ, 'FLEET_TEST_SECRET': 'unrelated-secret'},
            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == b'verified-model-review-mounts'
    assert (source / 'module.py').read_text() == 'answer = 42\n'
    assert Path(value['api_key_file']).read_text().strip() == KEY
    assert unrelated.read_text() == 'unrelated-secret'


@pytest.mark.parametrize('failure', ['unknown', 'duplicate', 'timeout_bool', 'budget_bool',
    'port', 'endpoint_path', 'instructions', 'relative_key'])
def test_model_review_invalid_configuration_is_rejected(tmp_path, failure):
    with peer() as (endpoint, calls):
        source, control, scratch, config, value = prepare(tmp_path, endpoint)
        if failure == 'unknown': value['unknown'] = True
        if failure == 'timeout_bool': value['timeout_seconds'] = True
        if failure == 'budget_bool': value['max_source_bytes'] = True
        if failure == 'port': value['endpoint'] = 'http://127.0.0.1:999999/v1/chat/completions'
        if failure == 'endpoint_path': value['endpoint'] = endpoint + '/extra'
        if failure == 'instructions': value['instructions'] = ''
        if failure == 'relative_key': value['api_key_file'] = 'review.key'
        raw = json.dumps(value)
        if failure == 'duplicate': raw = raw[:-1] + ',"schema_version":1}'
        config.write_text(raw, encoding='utf-8')
        result = run(tmp_path, source, control, scratch, config)
    assert result.returncode != 0 and not calls and result.stdout == b''
    assert b'Traceback' not in result.stderr


@pytest.mark.parametrize('failure', ['schema_bool', 'revision', 'subject', 'human', 'identity_type', 'duplicate'])
def test_model_review_invalid_request_is_rejected_before_http(tmp_path, failure):
    with peer() as (endpoint, calls):
        source, control, scratch, config, _ = prepare(tmp_path, endpoint)
        request = json.loads(json.dumps(REQUEST))
        if failure == 'schema_bool': request['schema_version'] = True
        if failure == 'revision': request['source_revision']['head_commit'] = []
        if failure == 'subject': request['subject_digest'] = []
        if failure == 'human': request['executor'] = {'id': 'human', 'kind': 'human', 'model_id': None}
        if failure == 'identity_type': request['executor']['id'] = []
        raw = json.dumps(request)
        if failure == 'duplicate': raw = raw[:-1] + ',"schema_version":1}'
        (control / 'request.json').write_text(raw, encoding='utf-8')
        result = run(tmp_path, source, control, scratch, config)
    assert result.returncode != 0 and result.stdout == b'' and not calls
    assert b'Traceback' not in result.stderr
