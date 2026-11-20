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
