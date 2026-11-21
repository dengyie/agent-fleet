"""Live task admission/capture must survive a full, slow historical archive."""
import importlib.util
import json
from pathlib import Path
import threading

import pytest

from tests.test_review_node_runner_capture import _cfg, _task, _Hub


def runner_module():
    spec = importlib.util.spec_from_file_location('dispatch_runner', Path(__file__).parents[1] / 'tools/agent-runner.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_historical_quota_cannot_block_managed_task_capture(tmp_path, monkeypatch):
    from tools.session.spool import LocalSpool
    from tools.session.runner_capture import RunnerCapture
    # Real production boundary: archive occupied the shared 128 MiB; no task
    # session_start or messages survived although the CLI itself ran.
    monkeypatch.setattr(LocalSpool, '_machine_bytes', lambda self: 134217325)
    capture = RunnerCapture(_cfg(tmp_path), _task(), _Hub(), process_group_id='group-task')
    try:
        capture.line(json.dumps({'type': 'item.completed', 'item': {
            'type': 'agent_message', 'text': 'real task response'}}), strict=True)
        events = capture.bridge.spool.read_after(0, 100, 262144)
        assert any(e['kind'] == 'session_start' for e in events)
        assert any(e['kind'] == 'assistant_message' for e in events)
    finally:
        capture.bridge.close()


def test_task_poll_is_independent_of_history_replay(tmp_path, monkeypatch):
    from tools.session import runner_capture
    runner = runner_module()
    replayed = []
    monkeypatch.setattr(runner_capture, 'replay_pending', lambda *a: replayed.append(True))
    monkeypatch.setattr(runner, 'flush_pending', lambda *a: None)
    monkeypatch.setattr(runner, 'post_json', lambda *a: (200, {'ok': True, 'task': None}))
    assert runner.poll_once(_cfg(tmp_path)) is False
    assert replayed == []


def test_background_replay_does_not_share_transport_or_pin_shutdown(tmp_path, monkeypatch):
    from tools.session import runner_capture
    from tools.transport import Transport
    entered, release = threading.Event(), threading.Event()
    cfg = _cfg(tmp_path); cfg.transport = Transport()
    def blocked(cfg, send, **kwargs):
        entered.set()
        release.wait(5)
    monkeypatch.setattr(runner_capture, 'replay_pending', blocked)
    worker = runner_capture.ConversationReplay(cfg, _Hub())
    assert worker.cfg is not cfg and worker.cfg.transport is not cfg.transport
    worker.start()
    try:
        assert entered.wait(2)
        worker.close()
        assert worker.stopping.is_set()
    finally:
        release.set()
        worker.thread.join(2)


@pytest.mark.parametrize('reason,exit_code,resumable', [('completed', 0, True),
    ('completed', 1, True), ('group_remaining', 124, False)])
def test_managed_capture_advertises_delivery_state_and_retains_worktree(
        tmp_path, monkeypatch, reason, exit_code, resumable):
    from tools.supervisor.supervisor import Supervisor, ManagedRunResult
    from tools.session.runner_capture import RunnerCapture
    from tests.test_supervisor_process import FakeOps
    runner = runner_module()
    cfg = _cfg(tmp_path)
    cfg.projects = {'proj': tmp_path}
    cfg.agents = {'codex': {'command': ['codex', 'exec', '{instruction}']}}
    cfg.heartbeat_interval_s = 100
    task = {**_task(), 'project': 'proj', 'instruction': 'first', 'nonce': 'n1'}
    supervisor = Supervisor(tmp_path / 'manifests', cfg.machine, ops=FakeOps())
    hub = _Hub()
    cleanup = []
    capture = None

    def start_capture(cfg, task, **kwargs):
        nonlocal capture
        capture = RunnerCapture(cfg, task, hub, **kwargs)
        return capture

    def finish(sid, **kwargs):
        entry = supervisor._entries[sid]
        supervisor._capture_native_identity(entry, '{"type":"thread.started","thread_id":"original-id"}')
        entry.handle.alive = False
        supervisor._mark_terminated(entry.manifest, reason)
        supervisor._release_handle(sid)
        return ManagedRunResult(exit_code=exit_code, outcome='finished' if reason == 'completed' else reason)

    monkeypatch.setattr(runner.worktree, 'create_worktree', lambda *a: tmp_path)
    monkeypatch.setattr(runner.worktree, 'cleanup_worktree', lambda *a: cleanup.append(a))
    monkeypatch.setattr(runner, '_start_capture', start_capture)
    monkeypatch.setattr(supervisor, 'wait_session', finish)
    monkeypatch.setattr(runner, '_collect_outputs', lambda *a: {'diff': '', 'files': [], 'patch': '', 'summary': ''})
    monkeypatch.setattr(runner, '_submit_result', lambda *a, **kw: None)
    assert runner.run_task_managed(cfg, task, supervisor) == {'exit_code': exit_code}
    metadata = [event['payload'] for batch in hub.batches for event in batch
                if event['kind'] == 'session_metadata' and 'native_resume' in event['payload']]
    assert metadata[-1] == {'native_resume': resumable, 'model_takeover': resumable}
    assert cleanup == []
    assert supervisor._continuations.path(capture.session_id).exists()


def test_failed_followup_clears_retained_delivery_flag(tmp_path):
    from tools.session.runner_capture import RunnerCapture
    hub = _Hub(status=503)  # Keep the unacknowledged record available for replay.
    capture = RunnerCapture(_cfg(tmp_path), _task(), hub)
    capture.begin_turn('one follow')
    capture.finish(124, resumable=False)
    record = json.loads(capture.record.read_text(encoding='utf-8'))
    assert record['closed'] and not record['resumable']


def test_replay_revokes_older_clients_failed_continuation_claim(tmp_path):
    from tools.session.runner_capture import RunnerCapture
    from tools.supervisor.model import ManagedSessionManifest
    cfg, hub = _cfg(tmp_path), _Hub()
    cfg.supervisor_manifest_dir = tmp_path / 'manifests'
    cfg.supervisor_manifest_dir.mkdir()
    capture = RunnerCapture(cfg, _task(), hub)
    capture.finish(124, resumable=True)  # An older client advertised retained identity.
    manifest = ManagedSessionManifest(session_id=capture.session_id, machine_id=cfg.machine,
        process_group_id='group', agent='codex', state='terminated', reason='group_remaining')
    path = cfg.supervisor_manifest_dir / (capture.session_id + '.json')
    path.write_text(manifest.to_json(), encoding='utf-8')
    original = path.read_bytes()
    replay = RunnerCapture(cfg, _task(), hub, replay=True)
    try:
        events = replay.bridge.spool.read_after(0, 100, 262144)
        metadata = [e['payload'] for e in events if e['kind'] == 'session_metadata']
        assert metadata[-1] == {'native_resume': False, 'model_takeover': False}
        assert not json.loads(replay.record.read_text(encoding='utf-8'))['resumable']
        assert path.read_bytes() == original
    finally:
        replay.bridge.close()
