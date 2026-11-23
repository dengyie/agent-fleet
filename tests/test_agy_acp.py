"""Actual supervised ACP worker against an independent strict stdio peer."""
import json
import sys
import threading
import time
from pathlib import Path

import pytest

from tools.supervisor.supervisor import Supervisor

PEER = Path(__file__).parent / 'fixtures/providers/agy_acp_peer.py'
SESSION = 'agy_native_fixture'


def worker(tmp_path, scenario='success', *, resume=None, timeout=3):
    from tools.adapters.agy import AgyAdapter
    trace = tmp_path / 'wire.jsonl'
    command = [sys.executable, str(PEER), scenario, str(trace)]
    adapter = AgyAdapter(command=command, timeout_s=timeout)
    argv = adapter.build_argv('literal --help ; $(touch nope) 🌲')
    if resume:
        argv[2:2] = ['--resume-session', resume]
    argv[2:2] = ['--request-timeout', '.3', '--settle-gap', '.05']
    return argv, trace


def run(tmp_path, scenario='success', *, resume=None):
    argv, trace = worker(tmp_path, scenario, resume=resume)
    supervisor = Supervisor(tmp_path / 'manifests', 'fixture-node')
    manifest = supervisor.launch(agent='agy', command=argv, cwd=str(tmp_path),
        env_allowlist={}, session_id='runner_acp_fixture')
    rows = []
    result = supervisor.wait_session(manifest.session_id, timeout_s=4, on_line=rows.append)
    requests = [json.loads(line) for line in trace.read_text(encoding='utf-8').splitlines()] if trace.exists() else []
    return result, rows, requests, supervisor


def test_acp_worker_handshake_stream_tools_and_retained_identity(tmp_path):
    result, rows, requests, supervisor = run(tmp_path)
    assert result.exit_code == 0, rows
    assert [r['method'] for r in requests] == ['initialize', 'authenticate', 'session/new', 'session/prompt']
    values = [json.loads(line) for line in rows]
    assert any(v.get('runtime_protocol') == 'acp' for v in values)
    texts = [v for v in values if v.get('type') == 'AssistantMessage' and v.get('text')]
    assert [v['text'] for v in texts] == ['hello ', '🌲 world']
    assert len({v['message_id'] for v in texts}) == 1
    assert any(v.get('type') == 'PostToolUse' and v.get('result') == 'passed' for v in values)
    assert 'PRIVATE_THOUGHT' not in '\n'.join(rows)
    assert supervisor.continuation_ready('runner_acp_fixture')
    restored = Supervisor(tmp_path / 'manifests', 'fixture-node')
    assert restored.continuation_ready('runner_acp_fixture')
    assert restored._entries['runner_acp_fixture'].resume_token == SESSION
    assert SESSION not in (tmp_path / 'manifests/runner_acp_fixture.json').read_text()


def test_acp_exact_resume_failure_does_not_create_sibling(tmp_path):
    result, rows, requests, _ = run(tmp_path, 'resume_fail', resume=SESSION)
    assert result.exit_code != 0
    assert [r['method'] for r in requests] == ['initialize', 'authenticate', 'session/resume']
    assert 'SECRET_NATIVE_ID' not in '\n'.join(rows)


def test_acp_false_resume_advertisement_is_refused_before_rpc(tmp_path):
    result, rows, requests, _ = run(tmp_path, 'invalid_resume', resume=SESSION)
    assert result.exit_code != 0
    assert [r['method'] for r in requests] == ['initialize', 'authenticate']


def test_acp_new_without_resume_support_retains_identity_without_grant(tmp_path):
    result, rows, requests, supervisor = run(tmp_path, 'invalid_resume')
    assert result.exit_code == 0
    assert not supervisor.continuation_ready('runner_acp_fixture')
    assert supervisor.continuation_retained('runner_acp_fixture')
    restored = Supervisor(tmp_path / 'manifests', 'fixture-node')
    assert not restored.continuation_ready('runner_acp_fixture')
    assert restored._entries['runner_acp_fixture'].resume_token == SESSION


def test_acp_early_metadata_is_bound_after_new_session_response(tmp_path):
    result, rows, _, _ = run(tmp_path, 'early_metadata')
    assert result.exit_code == 0, rows
    assert any(json.loads(line).get('title') == 'Native fixture title' for line in rows)


def test_acp_negotiated_configuration_precedes_single_prompt(tmp_path):
    argv, trace = worker(tmp_path, 'configure')
    argv[2:2] = ['--requested-model', 'model-next', '--requested-mode', 'plan']
    supervisor = Supervisor(tmp_path / 'manifests', 'fixture-node')
    manifest = supervisor.launch(agent='agy', command=argv, cwd=str(tmp_path), env_allowlist={})
    rows = []
    result = supervisor.wait_session(manifest.session_id, timeout_s=4, on_line=rows.append)
    assert result.exit_code == 0, rows
    requests = [json.loads(line) for line in trace.read_text(encoding='utf-8').splitlines()]
    assert [r.get('method') for r in requests] == ['initialize', 'authenticate', 'session/new',
        'session/set_config_option', 'session/set_config_option', 'session/prompt']
    options = supervisor._entries[manifest.session_id].resume_options
    assert options[options.index('--requested-model') + 1] == 'model-next'
    assert options[options.index('--requested-mode') + 1] == 'plan'


def test_acp_unknown_model_fails_before_prompt(tmp_path):
    argv, trace = worker(tmp_path, 'configure')
    argv[2:2] = ['--requested-model', 'unadvertised-model']
    supervisor = Supervisor(tmp_path / 'manifests', 'fixture-node')
    manifest = supervisor.launch(agent='agy', command=argv, cwd=str(tmp_path), env_allowlist={})
    rows = []
    result = supervisor.wait_session(manifest.session_id, timeout_s=4, on_line=rows.append)
    assert result.exit_code != 0
    assert 'session/prompt' not in trace.read_text(encoding='utf-8')
    assert 'unadvertised-model' not in '\n'.join(rows)


@pytest.mark.parametrize('scenario', ['auth_fail', 'wrong_version', 'startup_timeout', 'eof',
                                      'oversized', 'invalid_utf8', 'wrong_session', 'malformed_update'])
def test_acp_protocol_failure_is_bounded_and_not_retried(tmp_path, scenario):
    result, rows, requests, _ = run(tmp_path, scenario)
    assert result.exit_code != 0
    assert len([r for r in requests if r.get('method') == 'session/prompt']) <= 1
    assert not any(word in '\n'.join(rows) for word in ('SECRET_AUTH_TEXT', 'WRONG', '\ufffd'))
    assert all(len(line) < 65536 for line in rows)
    states = [json.loads(line)['state'] for line in rows if json.loads(line).get('type') == 'agent_state_changed']
    assert states[-1:] == ['failed'], 'a failed ACP connection cannot leave a running projection'


def test_acp_prompt_result_during_permission_waits_for_native_ack(tmp_path):
    from tools.providers.acp_mailbox import AcpMailbox
    argv, trace = worker(tmp_path, 'permission_early')
    supervisor = Supervisor(tmp_path / 'manifests', 'fixture-node')
    manifest = supervisor.launch(agent='agy', command=argv, cwd=str(tmp_path), env_allowlist={})
    result, rows = [], []
    thread = threading.Thread(target=lambda: result.append(supervisor.wait_session(
        manifest.session_id, timeout_s=4, on_line=rows.append)))
    thread.start()
    root = AcpMailbox.session_root(tmp_path / 'manifests', manifest.session_id)
    deadline = time.monotonic() + 2
    pending = None
    while time.monotonic() < deadline:
        pending = AcpMailbox._read(root / 'pending.json')
        if pending: break
        time.sleep(.01)
    assert pending
    time.sleep(.15)
    assert not result, 'prompt end_turn is not completed while native answer is pending'
    assert AcpMailbox.answer(root, pending['request_id'], 'option_0') == 'answered'
    thread.join(3)
    assert not thread.is_alive()
    assert result[0].exit_code == 0, rows
    assert any(json.loads(line).get('type') == 'interaction_response' for line in rows)


def test_acp_prompt_result_waits_for_background_tool(tmp_path):
    result, rows, _, _ = run(tmp_path, 'background')
    assert result.exit_code == 0
    values = [json.loads(line) for line in rows]
    assert any(v.get('text') == 'background done' for v in values)
    assert any(v.get('type') == 'PostToolUse' and v.get('result') == 'passed' for v in values)


def test_acp_stderr_saturation_does_not_block_or_leak(tmp_path):
    result, rows, _, _ = run(tmp_path, 'stderr')
    assert result.exit_code == 0
    assert 'SECRET_STDERR' not in '\n'.join(rows)


def test_acp_worker_uses_utf8_even_under_legacy_python_stdio(tmp_path):
    argv, _ = worker(tmp_path)
    supervisor = Supervisor(tmp_path / 'manifests', 'fixture-node')
    manifest = supervisor.launch(agent='agy', command=argv, cwd=str(tmp_path),
        env_allowlist={'PYTHONIOENCODING': 'ascii:strict'})
    rows = []
    result = supervisor.wait_session(manifest.session_id, timeout_s=4, on_line=rows.append)
    assert result.exit_code == 0, rows
    assert any(json.loads(line).get('text') == '🌲 world' for line in rows)


def test_acp_large_unicode_chunk_preserves_text_with_bounded_events(tmp_path):
    result, rows, _, _ = run(tmp_path, 'large_text')
    assert result.exit_code == 0
    values = [json.loads(line) for line in rows]
    texts = [row for row in values if row.get('type') == 'AssistantMessage']
    assert ''.join(row['text'] for row in texts) == '🌲' * 30000
    assert all(len(line.encode('utf-8')) < 50000 for line in rows)


def test_acp_large_tool_has_explicit_preview_and_complete_bounded_source(tmp_path):
    result, rows, _, _ = run(tmp_path, 'large_tool')
    assert result.exit_code == 0
    values = [json.loads(line) for line in rows]
    previews = [row for row in values if row.get('type') in {'PreToolUse', 'PostToolUse'}]
    assert all('[truncated;' in row.get('arguments', row.get('result', '')) for row in previews)
    source = ''.join(row['payload']['text'] for row in values if row.get('kind') == 'source_record')
    assert 'INPUT_TAIL' in source and '🌲' * 10000 + 'OUTPUT_TAIL' in source
    assert all(len(line.encode('utf-8')) < 50000 for line in rows)


def test_acp_large_tool_archive_survives_real_capture_and_hub_persistence(tmp_path):
    from hub.bootstrap import create_app
    from hub.config import FleetConfig
    from tools.runner_config import RunnerConfig
    from tools.session.runner_capture import RunnerCapture

    app = create_app(FleetConfig.from_root(tmp_path / 'hub', dev_operator='test@local',
        session_repositories_enabled=True, runner_credentials={'fixture-node': 'secret'}))
    client = app.test_client()

    def post(_cfg, path, events):
        response = client.post(path, json=events,
            headers={'X-Runner-Credential': 'fixture-node:secret'})
        assert response.status_code == 200 and not response.get_json().get('rejected')
        return response.status_code, response.get_json()

    cfg = RunnerConfig(hub='https://localhost', machine='fixture-node', credential='secret',
        runner_id='fixture-node', projects={}, agents={}, cache_dir=tmp_path / 'cache')
    task = {'task_id': 'archive', 'attempt_id': 'archive', 'agent_type': 'agy', 'instruction': 'read'}
    capture = RunnerCapture(cfg, task, post, process_group_id='archive-group')
    try:
        result, rows, _, _ = run(tmp_path, 'large_tool')
        assert result.exit_code == 0
        for line in rows:
            assert capture.line(line, strict=True)
        capture.finish(0, resumable=True)
        assert capture.failed_lines == 0 and capture.degraded is None
        route = '/api/sessions/' + capture.session_id + '/events'
        archived = client.get(route + '?include_source=1&limit=200')
        assert archived.status_code == 200
        # Capture also archives raw worker rows. The complete visible field's
        # own chunks must survive in order, alongside that diagnostic evidence.
        chunks = iter(row['payload']['text'] for row in archived.get_json()['events']
            if row['kind'] == 'source_record')
        for expected in ['🌲' * 4000, '🌲' * 4000, '🌲' * 2000 + 'OUTPUT_TAIL']:
            assert any(chunk == expected for chunk in chunks)
        chat = client.get(route + '?include_source=0&limit=200')
        assert chat.status_code == 200
        assert all(row['kind'] != 'source_record' for row in chat.get_json()['events'])
        assert 'OUTPUT_TAIL' not in chat.get_data(as_text=True)
        assert '[truncated;' in chat.get_data(as_text=True)
    finally:
        capture.bridge.close()


def test_acp_unadvertised_client_file_request_is_refused(tmp_path):
    result, rows, requests, _ = run(tmp_path, 'unknown_client_request')
    assert result.exit_code == 0
    assert next(r for r in requests if r.get('id') == 'fs-request')['error']['code'] == -32601
    assert '/private/secret' not in '\n'.join(rows)


def test_acp_owned_cancel_sends_native_cancel_before_exit(tmp_path):
    argv, trace = worker(tmp_path, 'cancel')
    supervisor = Supervisor(tmp_path / 'manifests', 'fixture-node')
    manifest = supervisor.launch(agent='agy', command=argv, cwd=str(tmp_path), env_allowlist={})
    result = []
    thread = threading.Thread(target=lambda: result.append(supervisor.wait_session(manifest.session_id, timeout_s=4)))
    thread.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if trace.exists() and 'session/prompt' in trace.read_text():
            break
        time.sleep(.01)
    supervisor.terminate_session(manifest.session_id, grace_s=1)
    thread.join(3)
    assert not thread.is_alive()
    assert 'session/cancel' in trace.read_text()


def test_acp_cross_supervisor_followups_reuse_native_session_and_hub_stream(tmp_path, monkeypatch):
    from hub.bootstrap import create_app
    from hub.config import FleetConfig
    from tools.adapters.agy import AgyAdapter
    from tools.runner_config import RunnerConfig
    from tools.session.runner_capture import RunnerCapture, session_prefix
    from tools.supervisor.continuation_capture import ContinuationCapture
    from tools.supervisor import continuation_capture
    app = create_app(FleetConfig.from_root(tmp_path / 'hub', dev_operator='test@local',
        session_repositories_enabled=True, runner_credentials={'mac': 'secret'}))
    client = app.test_client()

    def post(cfg, path, events):
        response = client.post(path, json=events, headers={'X-Runner-Credential': 'mac:secret'})
        assert response.status_code == 200 and not response.get_json().get('rejected')
        return response.status_code, response.get_json()

    cfg = RunnerConfig(hub='https://localhost', machine='mac', credential='secret',
        runner_id='mac', projects={}, agents={}, cache_dir=tmp_path / 'cache')
    trace = tmp_path / 'repeated-wire.jsonl'
    task = {'attempt_id': 'acp_repeated', 'task_id': 'task_acp', 'agent_type': 'agy', 'instruction': 'first'}
    sid = session_prefix('mac') + task['attempt_id']
    argv = AgyAdapter(command=[sys.executable, str(PEER.resolve()), 'echo', str(trace)]).build_argv('first')
    argv[2:2] = ['--request-timeout', '2', '--settle-gap', '.03']
    owner = Supervisor(tmp_path / 'manifests', 'mac')
    manifest = owner.launch(agent='agy', command=argv, cwd=str(tmp_path), env_allowlist={},
                            session_id=sid, attempt_id=task['attempt_id'])
    capture = RunnerCapture(cfg, task, post, process_group_id=manifest.process_group_id)
    try:
        assert owner.wait_session(sid, timeout_s=5, on_line=capture.line).exit_code == 0
        capture.finish(0, resumable=owner.continuation_ready(sid))
        monkeypatch.setattr(continuation_capture, 'post', post)
        for text in ['followup one', 'followup two']:
            controller = Supervisor(tmp_path / 'manifests', 'mac')
            continuation = ContinuationCapture(cfg, controller)
            assert controller.continuation_ready(sid)
            assert continuation.send(sid, text) == 'appended'
            deadline = time.monotonic() + 8
            while continuation.active and time.monotonic() < deadline:
                time.sleep(.01)
            assert not continuation.active
            assert controller.continuation_ready(sid)
        events = client.get('/api/sessions/' + sid + '/events?limit=100').get_json()['events']
        visible = [e['payload']['text'] for e in events if e['kind'] in {'user_message', 'assistant_message'}
                   and e['payload'].get('text')]
        assert visible == ['first', 'ACK first', 'followup one', 'ACK followup one', 'followup two', 'ACK followup two']
        answers = [e['payload'] for e in events if e['kind'] == 'assistant_message' and e['payload'].get('text')]
        assert len({a['message_id'] for a in answers}) == 3
        wire = [json.loads(line) for line in trace.read_text(encoding='utf-8').splitlines()]
        assert [r['method'] for r in wire if r.get('method') in {'session/new', 'session/resume'}] == [
            'session/new', 'session/resume', 'session/resume']
        assert all(r['params']['sessionId'] == SESSION for r in wire if r.get('method') == 'session/resume')
        session = client.get('/api/sessions/' + sid).get_json()['session']
        assert session['status'] == 'closed' and session['current_state'] == 'completed'
    finally:
        capture.bridge.close()
