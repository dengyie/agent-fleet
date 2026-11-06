"""Round-5 node-side regressions: spool, runner capture, native sync, result files.

N1  checkpoint dir-fsync failure (Windows) never fails a durable append.
N3  idle open/close cycles leave no empty segment files.
N4  a slow Hub POST never blocks ``RunnerCapture.line()``.
N5  segment lookup is exact per session; frames bind the session id.
N6  replay never opens a spool owned by a live writer.
N7  the machine quota spans every session under ``conversations/``.
N10 an oversized native JSONL line is skipped, not a permanent stall.
N11 a byte-truncated UTF-8 result file is kept (and flagged truncated).
"""
import json
import os
import struct
import threading
import time

import pytest

from tools.runner_config import RunnerConfig
from tools.session import spool as spool_mod
from tools.session.runner_capture import RunnerCapture, replay_pending
from tools.session.spool import (LocalSpool, SpoolBusy, acquire_owner_lock,
                                 owner_lock_held, release_owner_lock)
from fleet_core.session.crypto import AeadBox

KEY = bytes(range(32))


def _event(sid, i, text='hello'):
    return {'schema_version': 1, 'event_id': f'e{i}', 'stream_id': 'st',
            'machine_id': 'm', 'session_id': sid, 'sequence': i,
            'kind': 'user_message', 'capture_quality': 'structured',
            'emitted_at': '2026-01-01T00:00:00Z', 'payload': {'text': text}}


def _segments(root):
    return sorted(os.listdir(root / 'segments'))


def _cfg(tmp_path):
    return RunnerConfig(hub='http://localhost', machine='pxed', credential='secret',
                        runner_id='pxed', projects={}, agents={},
                        cache_dir=tmp_path / 'cache')


def _task(attempt='a1'):
    return {'attempt_id': attempt, 'agent_type': 'codex', 'task_id': 't1'}


def _ack_all(cfg, path, events):
    return 200, {'accepted_through': max(e['sequence'] for e in events)}


# -- N5 ------------------------------------------------------------------------

def test_prefix_session_never_reads_or_compacts_other_session(tmp_path):
    other = LocalSpool(tmp_path, 'm', 'adopt_ab', key=KEY)
    for i in range(1, 4):
        other.append(_event('adopt_ab', i))
    other.rotate()

    mine = LocalSpool(tmp_path, 'm', 'adopt', key=KEY)
    assert mine.read_after(0, 100, 1 << 20) == []
    assert mine.status()['last_sequence'] == 0
    assert mine.status()['segment_count'] == 0
    mine.append(_event('adopt', 1))
    mine.ack(mine.status()['last_sequence'])
    mine.close()

    assert 'adopt_ab_000000.sealed' in _segments(tmp_path)
    assert [e['sequence'] for e in other.read_after(0, 100, 1 << 20)] == [1, 2, 3]
    other.close()


def test_frames_bind_session_id(tmp_path):
    a = LocalSpool(tmp_path, 'm', 'alpha', key=KEY)
    a.append(_event('alpha', 1, text='secret-alpha'))
    a.close()
    b = LocalSpool(tmp_path, 'm', 'beta', key=KEY)
    b.append(_event('beta', 1))
    b.close()
    seg = tmp_path / 'segments'
    # Swap alpha's segment in under beta's name: same key, same sequence.
    (seg / 'beta_000000.sealed').unlink()
    (seg / 'alpha_000000.sealed').rename(seg / 'beta_000000.sealed')
    reopened = LocalSpool(tmp_path, 'm', 'beta', key=KEY)
    assert reopened.read_after(0, 100, 1 << 20) == []


def test_legacy_v1_segments_stay_readable_and_appendable(tmp_path):
    """Spools written before the session-bound AD still replay; a v1
    crash-survivor active segment keeps v1 frames until sealed."""
    box = AeadBox(KEY)
    seg_dir = tmp_path / 'segments'
    seg_dir.mkdir()

    def frame(i):
        body = json.dumps(_event('legacy', i), separators=(',', ':')).encode()
        blob = box.encrypt(body, associated_data=str(i).encode())
        return struct.pack('>QI', i, len(blob)) + blob

    header = b'SP4SEG01' + struct.pack('>Q', 0)
    (seg_dir / 'legacy_000000.sealed').write_bytes(header + frame(1) + frame(2))
    (seg_dir / 'legacy_000001.active').write_bytes(header + frame(3))

    spool = LocalSpool(tmp_path, 'm', 'legacy', key=KEY)
    assert spool.status()['last_sequence'] == 3
    spool.append(_event('legacy', 4))          # into the v1 active segment
    spool.rotate()
    spool.append(_event('legacy', 5))          # fresh v2 segment
    spool.close()
    assert (seg_dir / 'legacy_000002.sealed').read_bytes()[:8] == b'SP4SEG02'

    again = LocalSpool(tmp_path, 'm', 'legacy', key=KEY)
    assert [e['sequence'] for e in again.read_after(0, 100, 1 << 20)] == [1, 2, 3, 4, 5]
    again.close()


# -- N3 ------------------------------------------------------------------------

def test_idle_open_close_cycles_leave_no_segment_files(tmp_path):
    for _ in range(50):
        LocalSpool(tmp_path, 'm', 'sess', key=KEY).close()
    assert _segments(tmp_path) == []

    spool = LocalSpool(tmp_path, 'm', 'sess', key=KEY)
    spool.append(_event('sess', 1))
    spool.close()
    for _ in range(10):
        LocalSpool(tmp_path, 'm', 'sess', key=KEY).close()
    assert _segments(tmp_path) == ['sess_000000.sealed']
    reopened = LocalSpool(tmp_path, 'm', 'sess', key=KEY)
    assert [e['sequence'] for e in reopened.read_after(0, 10, 1 << 20)] == [1]
    assert reopened.append(_event('sess', 2)).sequence == 2
    reopened.close()


def test_header_only_sealed_segments_from_older_builds_are_dropped(tmp_path):
    seg_dir = tmp_path / 'segments'
    seg_dir.mkdir()
    for i in range(5):
        (seg_dir / f'sess_{i:06d}.sealed').write_bytes(b'SP4SEG01' + bytes(8))
    spool = LocalSpool(tmp_path, 'm', 'sess', key=KEY)
    assert spool.append(_event('sess', 1)).sequence == 1
    spool.close()
    assert _segments(tmp_path) == ['sess_000000.sealed']


# -- N1 ------------------------------------------------------------------------

def test_directory_fsync_failure_never_fails_durable_append(tmp_path, monkeypatch):
    real_open = os.open

    def no_dir_open(path, flags, *a, **k):
        if os.path.isdir(path):
            raise PermissionError(13, 'Permission denied')
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(spool_mod.os, 'open', no_dir_open)
    spool = LocalSpool(tmp_path, 'm', 's', key=KEY)
    assert spool.append(_event('s', 1)).sequence == 1
    assert spool.append(_event('s', 2)).sequence == 2
    cp = json.loads((tmp_path / 'checkpoints' / 's.json').read_text())
    assert cp['last_sequence'] == 2


def test_windows_skips_directory_fsync(tmp_path, monkeypatch):
    calls = []
    real_open = os.open
    monkeypatch.setattr(spool_mod, '_IS_WINDOWS', True)
    monkeypatch.setattr(spool_mod.os, 'open',
                        lambda p, f, *a, **k: calls.append(p) or real_open(p, f, *a, **k))
    spool = LocalSpool(tmp_path, 'm', 's', key=KEY)
    spool.append(_event('s', 1))
    assert not any(os.path.isdir(p) for p in calls)


def test_checkpoint_failure_after_durable_frame_is_retried(tmp_path, monkeypatch):
    spool = LocalSpool(tmp_path, 'm', 's', key=KEY)
    real_replace = os.replace
    state = {'fail': True}

    def flaky_replace(src, dst):
        if state['fail'] and str(dst).endswith('s.json'):
            raise OSError(5, 'EIO')
        return real_replace(src, dst)

    monkeypatch.setattr(spool_mod.os, 'replace', flaky_replace)
    result = spool.append(_event('s', 1))
    assert result.sequence == 1 and not result.capture_blocked
    assert spool.status()['last_sequence'] == 1
    assert not (tmp_path / 'checkpoints' / 's.json').exists()

    state['fail'] = False
    spool.close()   # the dirty checkpoint is persisted on the next operation
    cp = json.loads((tmp_path / 'checkpoints' / 's.json').read_text())
    assert cp['last_sequence'] == 1
    reopened = LocalSpool(tmp_path, 'm', 's', key=KEY)
    assert reopened.append(_event('s', 2)).sequence == 2


# -- N7 ------------------------------------------------------------------------

def test_machine_quota_spans_sibling_session_roots(tmp_path):
    quota_root = tmp_path / 'conversations'
    first = LocalSpool(quota_root / 'one', 'm', 'one', key=KEY,
                       quota_root=quota_root, max_machine_bytes=1 << 20)
    first.append(_event('one', 1, text='x' * 2000))
    used = first.status()['machine_bytes']
    first.close()

    second = LocalSpool(quota_root / 'two', 'm', 'two', key=KEY,
                        quota_root=quota_root, max_machine_bytes=used + 100)
    assert second.status()['machine_bytes'] == used
    blocked = second.append(_event('two', 1, text='x' * 2000))
    assert blocked.capture_blocked and blocked.sequence is None


def test_runner_capture_shares_machine_quota_root(tmp_path):
    cfg = _cfg(tmp_path)
    one = RunnerCapture(cfg, _task('one'), _ack_all)
    two = RunnerCapture(cfg, _task('two'), _ack_all)
    try:
        assert one.bridge.spool.status()['machine_bytes'] == \
            two.bridge.spool.status()['machine_bytes'] > \
            one.bridge.spool.status()['session_bytes'] > 0
    finally:
        one.bridge.close()
        two.bridge.close()


# -- N6 ------------------------------------------------------------------------

def test_owner_lock_excludes_second_holder(tmp_path):
    handle = acquire_owner_lock(tmp_path)
    try:
        assert owner_lock_held(tmp_path)
        with pytest.raises(SpoolBusy):
            acquire_owner_lock(tmp_path, timeout=0)
        with pytest.raises(SpoolBusy):
            LocalSpool(tmp_path, 'm', 's', key=KEY, owner_lock=True, owner_timeout=0)
    finally:
        release_owner_lock(handle)
    assert not owner_lock_held(tmp_path)
    spool = LocalSpool(tmp_path, 'm', 's', key=KEY, owner_lock=True, owner_timeout=0)
    assert owner_lock_held(tmp_path)
    spool.close()
    assert not owner_lock_held(tmp_path)


def test_replay_skips_spool_owned_by_live_writer(tmp_path):
    cfg = _cfg(tmp_path)
    posts = []

    def hub(cfg, path, events):
        posts.append(events)
        return _ack_all(cfg, path, events)

    live = RunnerCapture(cfg, _task('live'), hub)
    try:
        segments = live.root / live.session_id / 'segments'
        before = sorted(os.listdir(segments))
        replay_pending(cfg, hub)
        assert posts == []                         # never opened/uploaded
        assert sorted(os.listdir(segments)) == before
        assert live.record.exists()
        live.line('{"type":"x"}')                  # writer unaffected
    finally:
        live.bridge.close()
    replay_pending(cfg, hub)                       # released: replay proceeds
    assert posts


# -- N4 ------------------------------------------------------------------------

def test_line_not_blocked_by_slow_upload(tmp_path):
    cfg = _cfg(tmp_path)
    in_post = threading.Event()
    release = threading.Event()

    def slow_hub(cfg, path, events):
        in_post.set()
        release.wait(5)
        return _ack_all(cfg, path, events)

    capture = RunnerCapture(cfg, _task(), slow_hub)
    flusher = threading.Thread(target=capture.flush)
    flusher.start()
    try:
        assert in_post.wait(5)
        started = time.monotonic()
        assert capture.line('{"type":"x"}') is True
        assert time.monotonic() - started < 1.0
    finally:
        release.set()
        flusher.join(5)
    assert not flusher.is_alive()
    capture.flush()                                # acks the later line too
    assert capture.fully_acked()
    capture.bridge.close()


# -- N10 -----------------------------------------------------------------------

def test_oversized_native_line_is_skipped_not_stalled(tmp_path, monkeypatch):
    from tools.session import sync

    monkeypatch.setattr(sync, 'MAX_LINE_BYTES', 256)
    sent = []

    def hub(cfg, path, events):
        sent.extend(events)
        return _ack_all(cfg, path, events)

    def row(text):
        return json.dumps({'type': 'event_msg',
                           'payload': {'type': 'user_message', 'message': text}}) + '\n'

    native = tmp_path / 'native.jsonl'
    native.write_text(row('before') + row('y' * 1000) + row('after'))
    cfg = _cfg(tmp_path)
    assert sync.sync_file(cfg, 'codex', native, hub) == 2
    assert sync.sync_file(cfg, 'codex', native, hub) == 0    # checkpointed past it
    with native.open('a') as fh:
        fh.write(row('later'))
    assert sync.sync_file(cfg, 'codex', native, hub) == 1
    texts = [e['payload'].get('text') for e in sent if e['kind'] == 'user_message']
    assert texts == ['before', 'after', 'later']


# -- N11 -----------------------------------------------------------------------

def test_truncated_utf8_result_file_is_kept_and_flagged(tmp_path):
    from fleet_core.result_files import MAX_FILE_BYTES, collect_result_files

    # A 3-byte character straddles the MAX_FILE_BYTES cut.
    body = 'a' * (MAX_FILE_BYTES - 1) + '中' * (MAX_FILE_BYTES * 2)
    (tmp_path / 'notes.txt').write_text(body, encoding='utf-8')
    (tmp_path / 'binary.dat').write_bytes(b'\xff\xfe' * (MAX_FILE_BYTES * 3))
    files = {f['path']: f for f in collect_result_files(tmp_path)}
    assert 'binary.dat' not in files
    assert files['notes.txt']['truncated'] is True
    assert files['notes.txt']['content'].startswith('a' * 100)
