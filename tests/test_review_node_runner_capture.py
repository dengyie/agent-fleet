"""Review fix #11: runner conversation capture.

* A capture failure degrades (logged/marked) instead of aborting a healthy
  task: ``line()`` never raises; failures are counted on ``degraded`` /
  ``failed_lines`` only (agent-runner has no capture-abort path).
* Task spools are pruned once closed and acknowledged (or past the retention
  window), instead of being replayed on every poll forever; the record shrinks
  to a content-free tombstone that is never replayed and expires later.
"""
import json
import os
import time

import pytest

from tools.runner_config import RunnerConfig
from tools.session import runner_capture
from tools.session.runner_capture import (RunnerCapture, prune_if_done,
                                          replay_pending)


def _cfg(tmp_path):
    return RunnerConfig(hub='http://localhost', machine='pxed', credential='secret',
                        runner_id='pxed', projects={}, agents={}, cache_dir=tmp_path / 'cache')


def _task(attempt='a1'):
    return {'attempt_id': attempt, 'agent_type': 'codex', 'task_id': 't1'}


class _Hub:
    """Minimal ack-everything endpoint for /api/runner-session-events."""

    def __init__(self, status=200):
        self.status = status
        self.batches = []

    def __call__(self, cfg, path, events):
        self.batches.append(events)
        if self.status != 200:
            return self.status, {}
        last = max(e['sequence'] for e in events)
        return 200, {'accepted_through': last}


def _session_files(cfg, capture):
    """(未清理的记录存在, spool 目录存在)；墓碑记录不算未清理。"""
    root = cfg.cache_dir / 'conversations'
    live = capture.record.exists() and not json.loads(capture.record.read_text()).get('pruned')
    return live, (root / capture.session_id).exists()


def test_capture_failure_degrades_without_aborting(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    capture = RunnerCapture(cfg, _task(), _Hub())

    def boom(raw):
        raise RuntimeError('source_record_not_persisted')

    monkeypatch.setattr(capture.bridge, 'record_source', boom)
    assert capture.line('plain text line') is False
    assert capture.line('another') is False
    assert capture.degraded == 'RuntimeError'
    assert capture.failed_lines == 2
    with pytest.raises(RuntimeError):
        capture.line('strict', strict=True)
    capture.bridge.close()


def test_finish_survives_capture_failure(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    capture = RunnerCapture(cfg, _task(), _Hub())
    monkeypatch.setattr(capture.bridge, 'ingest_one',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('disk full')))
    capture.finish(0)  # 不抛出


def test_closed_and_acked_session_is_pruned_on_finish(tmp_path):
    cfg = _cfg(tmp_path)
    hub = _Hub()
    capture = RunnerCapture(cfg, _task(), hub)
    capture.line(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'hi'}}))
    capture.finish(0)
    assert hub.batches
    assert _session_files(cfg, capture) == (False, False)
    tomb = json.loads(capture.record.read_text())
    assert tomb == {'attempt_id': 'a1', 'agent_type': 'codex', 'task_id': 't1',
                    'closed': True, 'pruned': True}
    sent = len(hub.batches)
    replay_pending(cfg, hub)  # 墓碑不重放、不重建 spool
    assert len(hub.batches) == sent
    assert _session_files(cfg, capture) == (False, False)
    old = time.time() - runner_capture.CLOSED_RETENTION_S - 60
    os.utime(capture.record, (old, old))
    replay_pending(cfg, hub)  # 墓碑过期后删除
    assert not list((cfg.cache_dir / 'conversations').glob('runner_*.json'))


def test_unacked_closed_session_kept_then_pruned_after_ack(tmp_path):
    cfg = _cfg(tmp_path)
    capture = RunnerCapture(cfg, _task(), _Hub(status=503))
    capture.line('output')
    capture.finish(1)
    assert _session_files(cfg, capture) == (True, True)
    assert json.loads(capture.record.read_text())['closed'] is True
    replay_pending(cfg, _Hub())  # Hub 恢复：重放并确认后清理
    assert _session_files(cfg, capture) == (False, False)


def test_closed_unacked_session_pruned_after_retention(tmp_path):
    cfg = _cfg(tmp_path)
    capture = RunnerCapture(cfg, _task(), _Hub(status=503))
    capture.finish(0)
    old = time.time() - runner_capture.CLOSED_RETENTION_S - 60
    os.utime(capture.record, (old, old))
    replay_pending(cfg, _Hub(status=503))
    assert _session_files(cfg, capture) == (False, False)
    assert not capture.record.exists()  # 超保留期：记录一并删除


def test_open_session_and_native_sync_session_not_pruned(tmp_path):
    cfg = _cfg(tmp_path)
    running = RunnerCapture(cfg, _task('running'), _Hub())
    running.flush()
    assert prune_if_done(running) is False  # 未结束、刚确认：保留
    native = RunnerCapture(cfg, _task('native_abc'), _Hub())
    native.flush()
    native._mark_closed()
    assert prune_if_done(native, now=time.time() + 10 * runner_capture.CLOSED_RETENTION_S) is False
    assert prune_if_done(running, now=time.time() + runner_capture.ORPHAN_RETENTION_S + 60) is True
    running.bridge.close()
    native.bridge.close()
