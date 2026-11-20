"""Live task admission/capture must survive a full, slow historical archive."""
import importlib.util
import json
from pathlib import Path
import threading

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
