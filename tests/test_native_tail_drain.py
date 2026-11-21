"""Slow capture must not turn a progressing, completed native tail into failure."""

import json
from types import SimpleNamespace

import pytest

from tools.supervisor import supervisor as adapter
from tools.supervisor.supervisor import Supervisor
from tests.test_supervisor_process import FakeOps


@pytest.fixture
def finished(tmp_path, monkeypatch):
    ops = FakeOps()
    supervisor = Supervisor(tmp_path / 'manifests', 'win', ops=ops)
    manifest = supervisor.launch(
        agent='claude_code', command=['claude', '-p', 'original prompt'],
        cwd=str(tmp_path), env_allowlist={'ANTHROPIC_MODEL': 'original-model'},
    )
    handle = ops.children[0]
    handle.alive = False
    handle.exit_code = 0
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(adapter, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    return supervisor, manifest.session_id, handle, clock


def test_completed_tail_survives_slow_capture_and_retains_native_continuation(finished):
    supervisor, sid, handle, clock = finished
    lines = [json.dumps({'type': 'system', 'session_id': 'same-native-id'}),
             json.dumps({'type': 'assistant', 'message': {'content': 'actual tool result'}}),
             json.dumps({'type': 'result', 'session_id': 'same-native-id', 'result': '# Agent Fleet'})]
    handle.stdio = lines.copy()
    seen = []

    def capture(line):
        seen.append(line)
        clock.now += 1.1

    result = supervisor.wait_session(sid, timeout_s=10, on_line=capture)
    assert result.outcome == 'finished' and result.exit_code == 0
    assert seen == lines
    assert supervisor.get(sid).reason == 'completed'
    assert supervisor.continuation_ready(sid)
    config = {'mode': 'catalog', 'profile_id': 'working', 'digest': 'a' * 64,
              'family': 'claude_code', 'model': 'working-model',
              'base_url': 'https://gateway.example', 'api_key': 'test-secret', 'headers': {}}
    assert supervisor.configure_model(sid, config) == 'model_configured'
    assert supervisor.append_user_turn(sid, 'one follow-up') == 'appended'
    argv = supervisor._ops.create_calls[-1]['argv']
    assert argv[argv.index('--resume') + 1] == 'same-native-id'
    assert argv[-1] == 'one follow-up'


def test_continuously_progressing_member_still_reaches_total_flush_cap(finished, monkeypatch):
    supervisor, sid, handle, clock = finished
    handle.hold_stdout = True
    calls = []
    monkeypatch.setattr(adapter, '_FLUSH_TOTAL_BUDGET', 3.0, raising=False)

    def write_forever(handle, timeout, on_line=None, should_abort=None):
        clock.now += min(timeout, 0.1)
        calls.append(clock.now)
        on_line('still writing')
        return 'line'

    monkeypatch.setattr(supervisor._ops, 'pump', write_forever)
    result = supervisor.wait_session(sid, timeout_s=10)
    assert calls and clock.now == pytest.approx(3.0)
    assert result.exit_code == 124 and result.outcome == 'group_remaining'
    assert supervisor.get(sid).reason == 'group_remaining'


def test_quiet_member_keeps_the_original_inactivity_deadline(finished, monkeypatch):
    supervisor, sid, handle, clock = finished
    handle.hold_stdout = True

    def no_progress(handle, timeout, on_line=None, should_abort=None):
        clock.now += timeout
        return 'timeout'

    monkeypatch.setattr(supervisor._ops, 'pump', no_progress)
    result = supervisor.wait_session(sid, timeout_s=10)
    assert clock.now == pytest.approx(adapter._FLUSH_BUDGET)
    assert result.exit_code == 124 and result.outcome == 'group_remaining'


def test_dense_pi_tail_persists_final_message_before_bounded_completion(tmp_path, monkeypatch):
    ops = FakeOps(platform='windows')
    supervisor = Supervisor(tmp_path / 'manifests', 'win', ops=ops)
    manifest = supervisor.launch(agent='pi', command=['node', 'pi.js', '--print', '--mode', 'json', 'first'],
                                 cwd=str(tmp_path), env_allowlist={})
    handle = ops.children[0]
    handle.alive, handle.exit_code = False, 0
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(adapter, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    lines = [json.dumps({'type': 'session', 'id': 'same-pi-id'})]
    lines += [json.dumps({'type': 'message_update', 'assistantMessageEvent': {
        'type': 'text_delta', 'delta': str(i)}}) for i in range(160)]
    lines += [json.dumps({'type': 'message_end', 'message': {'role': 'assistant',
        'content': [{'type': 'text', 'text': 'complete final reply'}]}})]
    handle.stdio = lines.copy()
    seen = []

    def durable_capture(line):
        seen.append(line)
        clock.now += 0.3

    result = supervisor.wait_session(manifest.session_id, timeout_s=10, on_line=durable_capture)
    assert seen == lines
    assert 30 < clock.now < adapter._FLUSH_TOTAL_BUDGET
    assert result.outcome == 'finished' and result.exit_code == 0
    assert supervisor.continuation_ready(manifest.session_id)
    assert supervisor._entries[manifest.session_id].resume_token == 'same-pi-id'


@pytest.mark.parametrize('reason', ['group_remaining', 'timed_out', 'terminated'])
def test_retained_failed_identity_is_not_deliverable(finished, reason):
    supervisor, sid, handle, clock = finished
    entry = supervisor._entries[sid]
    supervisor._capture_native_identity(entry, json.dumps({'type': 'system', 'session_id': 'retained-id'}))
    supervisor._mark_terminated(entry.manifest, reason)
    supervisor._release_handle(sid)
    identity = supervisor._continuations.path(sid).read_bytes()
    assert not supervisor.continuation_ready(sid)
    assert supervisor.continuation_retained(sid)
    before = len(supervisor._ops.create_calls)
    assert supervisor.append_user_turn(sid, 'must not spawn') != 'appended'
    assert len(supervisor._ops.create_calls) == before
    assert supervisor._continuations.path(sid).read_bytes() == identity
    restored = Supervisor(supervisor._manifest_dir, 'win', ops=FakeOps())
    assert not restored.continuation_ready(sid)
    assert restored.continuation_retained(sid)


def test_completed_nonzero_exit_remains_deliverable(finished):
    supervisor, sid, handle, clock = finished
    handle.exit_code = 1
    handle.stdio = [json.dumps({'type': 'system', 'session_id': 'recoverable-upstream-error'})]
    result = supervisor.wait_session(sid, timeout_s=10)
    assert result.exit_code == 1 and result.outcome == 'finished'
    assert supervisor.continuation_ready(sid)
