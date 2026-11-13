"""Review fix #12: per-event / per-batch hot paths.

* SessionService.ingest_events batches session-side writes per request:
  one upsert per distinct spec, one init_stream / note_event per session, one
  summary publish per batch (instead of 4 connections + 3 commits per event).
* TranscriptRepository keeps a trigger-maintained per-session raw byte
  counter instead of a ``SUM(length(...))`` scan per ingested event.
* LocalSpool.read_after skips acknowledged frames by header and whole acked
  sealed segments, never decrypting the acked prefix on every batch.
"""
import sqlite3

from hub.application.session_service import SessionService
from hub.infrastructure.session_repository import SessionRepository
from hub.infrastructure.transcript_repository import TranscriptRepository
from tools.session.spool import LocalSpool

KEY = bytes(range(32))


def _event(seq, session_id="sess_perf_1", text="hello"):
    return {
        "schema_version": 1, "event_id": f"evt_{seq:04d}", "stream_id": "stream_perf",
        "machine_id": "mac-perf", "session_id": session_id, "attempt_id": "att_perf",
        "process_group_id": "grp_perf", "sequence": seq, "kind": "user_message",
        "capture_quality": "structured", "source": "bridge",
        "emitted_at": "2026-11-06T00:00:00Z",
        "payload": {"text": text, "is_complete": True},
    }


class _Spy:
    def __init__(self, target, names):
        self._target = target
        self.calls = {n: [] for n in names}

    def __getattr__(self, name):
        attr = getattr(self._target, name)
        if name in self.calls:
            def wrapped(*args, **kwargs):
                self.calls[name].append(args)
                return attr(*args, **kwargs)
            return wrapped
        return attr


class _Publisher:
    def __init__(self):
        self.emitted = []

    def emit(self, name, **kwargs):
        self.emitted.append((name, kwargs))


def _repos(tmp_path, **kwargs):
    repo = SessionRepository(tmp_path / "meta.db")
    repo.init()
    tr = TranscriptRepository(tmp_path / "tr.db", key=KEY, **kwargs)
    tr.init()
    return repo, tr


def test_ingest_batch_coalesces_session_writes(tmp_path):
    repo, tr = _repos(tmp_path)
    spy = _Spy(repo, ("upsert_session", "init_stream", "note_event"))
    publisher = _Publisher()
    service = SessionService(spy, tr, event_publisher=publisher)
    result = service.ingest_events([_event(i) for i in range(1, 21)])
    assert result["accepted_through"] == 20 and result["status"] == "accepted"
    assert len(spy.calls["upsert_session"]) == 1
    assert len(spy.calls["init_stream"]) == 1
    assert [c[0] for c in spy.calls["note_event"]] == ["sess_perf_1"]
    assert spy.calls["note_event"][0][2] == 20
    assert repo.stream_cursor("sess_perf_1") == 20
    updates = [kw for name, kw in publisher.emitted if name == "session_update"]
    assert len(updates) == 1 and updates[0]["cursor"] == 20

    # 全重复批次：不再推送摘要，ACK 不回退
    publisher.emitted.clear()
    again = service.ingest_events([_event(i) for i in range(1, 21)])
    assert again["accepted_through"] == 20
    assert publisher.emitted == []


def test_ingest_failure_mid_batch_still_notes_observed_sequence(tmp_path):
    repo, tr = _repos(tmp_path)
    real_ingest = tr.ingest

    def flaky(event):
        if event["sequence"] == 3:
            raise sqlite3.OperationalError("database is locked")
        return real_ingest(event)

    tr.ingest = flaky
    service = SessionService(repo, tr)
    try:
        service.ingest_events([_event(i) for i in range(1, 5)])
    except Exception as exc:  # SessionServiceError
        assert getattr(exc, "code", "") == "session_unavailable"
    else:
        raise AssertionError("expected session_unavailable")
    assert repo.stream_cursor("sess_perf_1") == 3


def _sum_raw(tr, session_id):
    conn = sqlite3.connect(str(tr.db_path))
    try:
        return conn.execute(
            "SELECT COALESCE(SUM(length(nonce_ciphertext)),0) FROM raw_events"
            " WHERE session_id=?", (session_id,)).fetchone()[0]
    finally:
        conn.close()


def _counter(tr, session_id=None):
    conn = tr._connect()
    try:
        return tr._raw_store_bytes(conn, session_id)
    finally:
        conn.close()


def test_raw_byte_counter_tracks_inserts_and_purge_without_sum_scan(tmp_path):
    _, tr = _repos(tmp_path)
    statements = []
    real_connect = tr._connect

    def traced():
        conn = real_connect()
        conn.set_trace_callback(statements.append)
        return conn

    tr._connect = traced
    for i in range(1, 6):
        assert tr.ingest(_event(i)).raw_written
        assert tr.ingest(_event(i, session_id="sess_perf_2")).raw_written
    assert not [s for s in statements if "SUM(length(nonce_ciphertext))" in s]
    tr._connect = real_connect

    assert _counter(tr, "sess_perf_1") == _sum_raw(tr, "sess_perf_1") > 0
    assert _counter(tr) == _sum_raw(tr, "sess_perf_1") + _sum_raw(tr, "sess_perf_2")
    tr.purge_expired_raw("9999-12-31T00:00:00Z", limit=3)
    assert _counter(tr, "sess_perf_1") == _sum_raw(tr, "sess_perf_1")
    assert _counter(tr, "sess_perf_2") == _sum_raw(tr, "sess_perf_2")
    assert _counter(tr, "sess_unknown") == 0


def test_raw_byte_counter_quota_and_legacy_backfill(tmp_path):
    _, tr = _repos(tmp_path)
    for i in range(1, 4):
        tr.ingest(_event(i))
    # 模拟旧库：无计数表/触发器/种子标记，重新 init 时一次性回填
    conn = sqlite3.connect(str(tr.db_path))
    conn.executescript(
        "DROP TRIGGER trg_raw_bytes_ins; DROP TRIGGER trg_raw_bytes_del;"
        " DROP TRIGGER trg_raw_bytes_upd; DROP TABLE raw_bytes;"
        " DELETE FROM meta WHERE key='raw_bytes_counter';")
    conn.close()
    tr.init()
    used = _sum_raw(tr, "sess_perf_1")
    assert _counter(tr, "sess_perf_1") == used
    tr.init()  # 幂等：不重复累加
    assert _counter(tr, "sess_perf_1") == used

    # 配额仍按会话执行
    tight = TranscriptRepository(tr.db_path, key=KEY, max_raw_bytes=used + 10)
    tight.init()
    result = tight.ingest(_event(4))
    assert result.reason == "raw_quota_exceeded" and not result.raw_written


def _spool(tmp_path):
    return LocalSpool(tmp_path / "spool", "mac-perf", "sess_perf_1", key=KEY,
                      segment_target_bytes=600)


def test_read_after_does_not_decrypt_acked_prefix(tmp_path):
    spool = _spool(tmp_path)
    for i in range(1, 41):
        spool.append(_event(i, text="x" * 40))
    segments = list((tmp_path / "spool" / "segments").glob("*.sealed"))
    assert len(segments) > 3
    spool.ack(30)

    decrypted = []
    real_decrypt = spool._box.decrypt

    def counting(blob, associated_data=b""):
        decrypted.append(int(associated_data.rsplit(b"\0", 1)[-1]))  # v2 AD: ...\0<seq>
        return real_decrypt(blob, associated_data=associated_data)

    spool._box.decrypt = counting
    out = spool.read_after(0, 100, 1 << 20)
    assert [e["sequence"] for e in out] == list(range(31, 41))
    assert decrypted and min(decrypted) > 30

    decrypted.clear()
    out = spool.read_after(35, 3, 1 << 20)
    assert [e["sequence"] for e in out] == [36, 37, 38]
    assert min(decrypted) > 35
    spool.close()


def test_read_after_after_reopen_matches_full_scan(tmp_path):
    spool = _spool(tmp_path)
    for i in range(1, 25):
        spool.append(_event(i, text="y" * 40))
    spool.ack(10)
    spool.close()
    reopened = _spool(tmp_path)
    assert [e["sequence"] for e in reopened.read_after(0, 100, 1 << 20)] == list(range(11, 25))
    assert [e["sequence"] for e in reopened.read_after(20, 100, 1 << 20)] == [21, 22, 23, 24]
    assert [s for s, _ in reopened._scan_all()] == sorted(s for s, _ in reopened._scan_all())
    reopened.close()
