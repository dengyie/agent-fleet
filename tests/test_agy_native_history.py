"""Real native-shaped SQLite -> encrypted spool -> Hub HTTP; no Agent calls."""
import hashlib
import json
import sqlite3

import pytest

from tests.test_review_node_sync import _cfg, _client

CID = '11111111-2222-4333-8444-555555555555'


def _varint(value):
    data = bytearray()
    while value > 127:
        data.append((value & 127) | 128)
        value >>= 7
    return bytes(data + bytes([value]))


def _pb(*fields):
    result = b''
    for number, value in fields:
        if isinstance(value, str):
            value = value.encode('utf-8')
        result += _varint(number << 3 | (2 if isinstance(value, bytes) else 0))
        result += _varint(len(value)) + value if isinstance(value, bytes) else _varint(value)
    return result


def _step(kind, content, *, status=3, model='Native model', call=None):
    metadata = _pb((1, _pb((1, 1795700000), (2, 123000000))),
                   (24, _pb((8, model))))
    if call:
        metadata += _pb((4, _pb((1, 'native_call'), (2, call), (3, '{"path":"file.txt"}'),
                                (7, b'PRIVATE_SIGNATURE_SENTINEL'))))
    field = {14: 19, 15: 20, 132: 140, 8: 14, 101: 114}[kind]
    if kind == 14:
        body = _pb((2, content), (3, _pb((1, content))),
                   (4, b'PRIVATE_CONTEXT_SENTINEL'))
    elif kind == 15:
        body = _pb((1, content), (3, 'PRIVATE_THINKING_SENTINEL'),
                   (16, 'PRIVATE_RAW_THINKING_SENTINEL'), (4, 'PRIVATE_SIGNATURE_SENTINEL'))
    elif kind == 132:
        body = _pb((2, _pb((1, content), (6, b'PRIVATE_ANY_SENTINEL'))))
    elif kind == 8:
        body = _pb((4, content))
    else:
        body = _pb((1, content))
    return _pb((1, kind), (4, status), (5, metadata), (field, body)), metadata


def _native(tmp_path, monkeypatch=None, *, rows=None):
    if monkeypatch:
        monkeypatch.setenv('HOME', str(tmp_path))
        monkeypatch.setenv('USERPROFILE', str(tmp_path))
    root = tmp_path / '.gemini/antigravity-cli/conversations'
    root.mkdir(parents=True)
    path = root / (CID + '.db')
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE trajectory_meta(trajectory_id TEXT PRIMARY KEY, cascade_id TEXT, trajectory_type INTEGER, source INTEGER)')
        db.execute('INSERT INTO trajectory_meta VALUES (?,?,4,17)', ('distinct-trajectory', CID))
        db.execute('CREATE TABLE steps(idx INTEGER PRIMARY KEY, step_type INTEGER, status INTEGER, metadata BLOB, step_payload BLOB, step_format INTEGER DEFAULT 0)')
        for index, (kind, text, status, call) in enumerate(rows or [
            (14, 'native user', 3, None), (15, 'native reply', 3, None),
            (132, 'native tool output', 3, 'read_file'),
            (101, 'PRIVATE_SYSTEM_SENTINEL', 3, None),
        ]):
            payload, metadata = _step(kind, text, status=status, call=call)
            db.execute('INSERT INTO steps VALUES (?,?,?,?,?,0)', (index, kind, status, metadata, payload))
    with sqlite3.connect(root.parent / 'conversation_summaries.db') as db:
        db.execute('CREATE TABLE conversation_summaries(conversation_id TEXT PRIMARY KEY,title TEXT,preview TEXT)')
        db.execute('INSERT INTO conversation_summaries VALUES (?,?,?)', (CID, '本机原生会话名', 'native preview'))
    return path


def _sync(tmp_path, path, *, send=None, cfg=None, client=None):
    from tools.session.sync import NativeArchiveSync
    cfg, client = cfg or _cfg(tmp_path), client or _client(tmp_path)
    def post(config, url, body):
        response = client.post(url, json=body, headers={'X-Runner-Credential': 'pxed:secret'})
        return response.status_code, response.get_json()
    worker = NativeArchiveSync(cfg, sources=lambda: [('agy', path.parent)], send=send or post)
    worker.scan_once()
    return cfg, client, worker


def _events(client):
    rows = client.get('/api/sessions?limit=100').get_json()['sessions']
    agy = [row for row in rows if row['agent_family'] == 'agy']
    assert len(agy) == 1
    sid = agy[0]['session_id']
    page = client.get(f'/api/sessions/{sid}/events?limit=1000').get_json()
    result = page['events']
    while page.get('has_more'):
        page = client.get(f'/api/sessions/{sid}/events', query_string={
            'after_cursor': page['next_cursor'], 'limit': 1000}).get_json()
        result += page['events']
    return agy[0], result


def test_default_sources_include_exact_agy_conversation_directory(tmp_path, monkeypatch):
    from tools.session.sync import default_sources
    path = _native(tmp_path, monkeypatch)
    assert ('agy', path.parent) in default_sources()


def test_native_agy_history_real_http_keeps_title_messages_tools_and_private_fields_out(tmp_path):
    path = _native(tmp_path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    cfg, client, worker = _sync(tmp_path, path)
    session, events = _events(client)
    assert session['native_title'] == '本机原生会话名'
    assert session['managed'] is False
    assert [e['payload']['text'] for e in events if e['kind'] in {'user_message', 'assistant_message'}] == ['native user', 'native reply']
    assert [e['kind'] for e in events if e['kind'].startswith('tool_')] == ['tool_call', 'tool_result']
    assert next(e for e in events if e['kind'] == 'tool_result')['payload']['result'] == 'native tool output'
    text = json.dumps(events, ensure_ascii=False)
    assert 'PRIVATE_' not in text
    assert str(path) not in text and CID not in text
    assert any(e['payload'].get('model') == 'Native model' for e in events)
    assert any(e['payload'].get('native_started_at') for e in events)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    ids = [e['event_id'] for e in events]
    worker.scan_once()
    _sync(tmp_path, path, cfg=cfg, client=client)
    assert [e['event_id'] for e in _events(client)[1]] == ids


def _post(client):
    def send(config, url, body):
        response = client.post(url, json=body, headers={'X-Runner-Credential': 'pxed:secret'})
        return response.status_code, response.get_json()
    return send


def _checkpoint_path(cfg, path):
    from tools.session.runner_capture import session_prefix
    from tools.session.sync import native_identity
    return cfg.cache_dir / 'conversations' / (session_prefix(cfg.machine) + native_identity('agy', path) + '.agy-cursor')


def test_lost_response_replays_original_events_once_without_reading_again(tmp_path):
    from tools.session.agy_archive import sync_agy
    path, cfg, client = _native(tmp_path), _cfg(tmp_path), _client(tmp_path)
    accepted = []
    def lost(config, url, body):
        accepted.extend(e['event_id'] for e in body)
        _post(client)(config, url, body)
        return 0, {}
    with pytest.raises(RuntimeError, match='conversation_upload_pending'):
        sync_agy(cfg, path, lost)
    original = _events(client)[1]
    assert accepted
    assert sync_agy(cfg, path, _post(client)) is True
    assert _events(client)[1] == original
    assert len({e['event_id'] for e in original}) == len(original)


def test_crash_after_persist_before_checkpoint_deduplicates_all_events(tmp_path, monkeypatch):
    from tools.session import agy_archive
    path, cfg, client = _native(tmp_path), _cfg(tmp_path), _client(tmp_path)
    real = agy_archive.atomic_private_json
    fail = [True]
    def checkpoint(file, state):
        if fail[0] and state['cursor'] == 2:
            raise OSError('fixture disk failure')
        return real(file, state)
    monkeypatch.setattr(agy_archive, 'atomic_private_json', checkpoint)
    with pytest.raises(OSError):
        agy_archive.sync_agy(cfg, path, _post(client))
    assert json.loads(_checkpoint_path(cfg, path).read_text())['cursor'] == 1
    fail[0] = False
    agy_archive.sync_agy(cfg, path, _post(client))
    _, events = _events(client)
    assert sum(e['kind'] == 'tool_call' for e in events) == 1
    assert sum(e['kind'] == 'tool_result' for e in events) == 1


def test_quota_failure_preserves_source_position_and_recovers(tmp_path, monkeypatch):
    from tools.session.agy_archive import sync_agy
    from tools.session.bridge import SessionBridge
    path, cfg, client = _native(tmp_path), _cfg(tmp_path), _client(tmp_path)
    real, blocked = SessionBridge.ingest_one, [True]
    def ingest(self, event, **kwargs):
        if blocked[0] and event.get('kind') == 'tool_result':
            raise RuntimeError('event_not_persisted')
        return real(self, event, **kwargs)
    monkeypatch.setattr(SessionBridge, 'ingest_one', ingest)
    with pytest.raises(RuntimeError, match='event_not_persisted'):
        sync_agy(cfg, path, _post(client))
    assert json.loads(_checkpoint_path(cfg, path).read_text())['cursor'] == 1
    blocked[0] = False
    sync_agy(cfg, path, _post(client))
    assert sum(e['kind'] == 'tool_call' for e in _events(client)[1]) == 1


def test_active_snapshot_same_message_updates_and_releases_next_rows(tmp_path):
    from tools.session.agy_archive import sync_agy
    path = _native(tmp_path, rows=[(14, 'prompt', 3, None),
        (15, 'first', 2, None), (15, 'later', 3, None)])
    cfg, client = _cfg(tmp_path), _client(tmp_path)
    assert sync_agy(cfg, path, _post(client)) is False
    initial = _events(client)[1]
    reply = next(e for e in initial if e['kind'] == 'assistant_message')
    assert reply['payload']['is_complete'] is False
    assert sync_agy(cfg, path, _post(client)) is False
    assert _events(client)[1] == initial
    with sqlite3.connect(path) as db:
        payload, meta = _step(15, 'first completed', status=3)
        db.execute('UPDATE steps SET step_payload=?,metadata=?,status=3 WHERE idx=1', (payload, meta))
    assert sync_agy(cfg, path, _post(client)) is True
    replies = [e for e in _events(client)[1] if e['kind'] == 'assistant_message']
    assert [e['payload']['text'] for e in replies] == ['first', 'first completed', 'later']
    assert replies[0]['payload']['message_id'] == replies[1]['payload']['message_id']
    assert replies[1]['payload']['is_complete'] is True
    assert json.loads(_checkpoint_path(cfg, path).read_text())['cursor'] == 2


def test_large_history_pages_are_finite_and_continue_at_idx_zero(tmp_path):
    from tools.session.agy_archive import sync_agy
    path = _native(tmp_path, rows=[(14, f'message {index}', 3, None) for index in range(70)])
    cfg, client = _cfg(tmp_path), _client(tmp_path)
    assert sync_agy(cfg, path, _post(client)) is False
    assert json.loads(_checkpoint_path(cfg, path).read_text())['cursor'] == 31
    assert sync_agy(cfg, path, _post(client)) is False
    assert sync_agy(cfg, path, _post(client)) is True
    messages = [e['payload']['text'] for e in _events(client)[1] if e['kind'] == 'user_message']
    assert messages == [f'message {index}' for index in range(70)]


def test_title_update_only_and_wal_invalidate_completed_scan(tmp_path):
    path = _native(tmp_path)
    cfg, client, worker = _sync(tmp_path, path)
    initial = _events(client)[1]
    with sqlite3.connect(path.parent.parent / 'conversation_summaries.db') as db:
        db.execute('UPDATE conversation_summaries SET title=?', ('重命名的本地会话',))
    worker.scan_once()
    session, events = _events(client)
    assert session['native_title'] == '重命名的本地会话'
    assert len(events) == len(initial) + 1
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA journal_mode=WAL')
        payload, meta = _step(15, 'new WAL message')
        db.execute('INSERT INTO steps VALUES (4,15,3,?,?,0)', (meta, payload))
        db.commit()
        worker.scan_once()
        assert any(e['payload'].get('text') == 'new WAL message' for e in _events(client)[1])


def test_large_visible_text_and_tools_keep_full_content_secret_redacted(tmp_path):
    from tools.session.agy_archive import sync_agy
    # Secret spans an archive chunk boundary; full-string redaction must precede splitting.
    text = '多字节🙂' * 3000
    prefix, suffix = 'visible input line\n' * 221, 'visible output line\n' * 800
    tool = prefix + 'Authorization: Bearer fixture_secret_1234567890\n' + suffix
    path = _native(tmp_path, rows=[(15, text, 3, None), (132, tool, 3, 'read')])
    cfg, client = _cfg(tmp_path), _client(tmp_path)
    assert sync_agy(cfg, path, _post(client)) is True
    _, events = _events(client)
    messages = [e for e in events if e['kind'] == 'assistant_message']
    assert ''.join(e['payload']['text'] for e in messages) == text
    assert len({e['payload']['message_id'] for e in messages}) == len(messages)
    assert all(len(e['payload']['text']) <= 4000 for e in messages)
    full = ''.join(e['payload']['text'] for e in events if e['kind'] == 'source_record')
    assert full.startswith(prefix) and full.endswith(suffix)
    assert 'fixture_secret_1234567890' not in json.dumps(events)
    assert 'full tool field retained' in next(e['payload']['result'] for e in events if e['kind'] == 'tool_result')
    originals = [e['event_id'] for e in events]
    sync_agy(cfg, path, _post(client))
    assert [e['event_id'] for e in _events(client)[1]] == originals


@pytest.mark.parametrize('raw', ['{}', '{', '{"version":2}', '{"version":1,"cursor":true}',
                                pytest.param('x' * 100000, id='oversized-checkpoint')])
def test_corrupt_or_future_checkpoint_fails_closed_without_public_session(tmp_path, raw):
    from tools.session.agy_archive import sync_agy
    path, cfg, client = _native(tmp_path), _cfg(tmp_path), _client(tmp_path)
    checkpoint = _checkpoint_path(cfg, path)
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(raw, encoding='utf-8')
    with pytest.raises(ValueError, match='agy_checkpoint_invalid'):
        sync_agy(cfg, path, _post(client))
    assert checkpoint.read_text() == raw
    assert client.get('/api/sessions').get_json()['sessions'] == []


@pytest.mark.parametrize('mutation', [
    "UPDATE trajectory_meta SET cascade_id='different'",
    'DROP TABLE steps',
    'UPDATE steps SET step_format=1 WHERE idx=0',
    "UPDATE steps SET step_payload=X'00' WHERE idx=0",
    "UPDATE steps SET step_payload=X'9a01ff' WHERE idx=0",
])
def test_bad_source_identity_schema_or_proto_never_creates_public_session(tmp_path, mutation):
    from tools.session.agy_archive import sync_agy
    path, cfg, client = _native(tmp_path), _cfg(tmp_path), _client(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute(mutation)
    with pytest.raises((ValueError, sqlite3.Error)):
        sync_agy(cfg, path, _post(client))
    assert not _checkpoint_path(cfg, path).exists()
    assert client.get('/api/sessions').get_json()['sessions'] == []


def test_discovery_does_not_scan_nested_or_non_uuid_databases(tmp_path):
    from tools.session.sync import native_paths
    path = _native(tmp_path)
    nested = path.parent / 'unrelated'
    nested.mkdir()
    (nested / 'credentials.db').write_bytes(b'not sqlite')
    assert list(native_paths('agy', path.parent)) == [path]


def test_empty_history_is_catalogued_once_and_has_no_messages(tmp_path):
    from tools.session.agy_archive import sync_agy
    path, cfg, client = _native(tmp_path), _cfg(tmp_path), _client(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute('DELETE FROM steps')
    assert sync_agy(cfg, path, _post(client)) is True
    session, events = _events(client)
    assert session['native_title'] == '本机原生会话名'
    assert not any(e['kind'] in {'user_message', 'assistant_message', 'tool_call'} for e in events)
    original = events
    sync_agy(cfg, path, _post(client))
    assert _events(client)[1] == original


def test_stop_before_capture_never_publishes_or_advances(tmp_path):
    from tools.session.agy_archive import sync_agy
    path, cfg, client = _native(tmp_path), _cfg(tmp_path), _client(tmp_path)
    assert sync_agy(cfg, path, _post(client), should_stop=lambda: True) is False
    assert not _checkpoint_path(cfg, path).exists()
    assert client.get('/api/sessions').get_json()['sessions'] == []


@pytest.mark.parametrize('cursor', [True, -2, 1 << 63, '0'])
def test_invalid_read_cursor_is_bounded(tmp_path, cursor):
    from tools.session.agy_storage import read_page
    path = _native(tmp_path)
    with pytest.raises(ValueError, match='agy_cursor_invalid'):
        read_page(path, cursor)


def test_reader_query_is_read_only_and_blob_bounds_fail_closed(tmp_path, monkeypatch):
    from tools.session import agy_storage
    path = _native(tmp_path)
    before = path.read_bytes()
    with pytest.raises(ValueError, match='agy_blob_invalid'):
        monkeypatch.setattr(agy_storage, 'MAX_BLOB', 16)
        agy_storage.read_page(path, -1)
    assert path.read_bytes() == before


@pytest.mark.parametrize('replay_first', [False, True])
def test_crash_immediately_after_append_recovers_pending_receipt(tmp_path, monkeypatch, replay_first):
    from tools.session import agy_archive
    from tools.session.runner_capture import replay_pending
    path, cfg, client = _native(tmp_path), _cfg(tmp_path), _client(tmp_path)
    real, fault = agy_archive.atomic_private_json, [True]
    def checkpoint(file, state):
        # Pending intention is already on disk; simulate crash before publishing
        # the successful append receipt, not merely after a whole source step.
        if fault[0] and state['pending'] is None and len(state['receipts']) == 1 and state['cursor'] == -1:
            raise OSError('crash after append')
        real(file, state)
    monkeypatch.setattr(agy_archive, 'atomic_private_json', checkpoint)
    with pytest.raises(OSError):
        agy_archive.sync_agy(cfg, path, _post(client))
    state = json.loads(_checkpoint_path(cfg, path).read_text())
    assert state['pending'] is not None
    fault[0] = False
    if replay_first:
        replay_pending(cfg, _post(client))
    agy_archive.sync_agy(cfg, path, _post(client))
    _, events = _events(client)
    assert sum(e['payload'].get('title') == '本机原生会话名' for e in events) == 1
    assert json.loads(_checkpoint_path(cfg, path).read_text())['pending'] is None


def test_concurrent_scanners_refresh_cursor_under_spool_writer_lock(tmp_path):
    import concurrent.futures
    import threading
    from tools.session.agy_archive import sync_agy
    path, cfg, client = _native(tmp_path), _cfg(tmp_path), _client(tmp_path)
    gate, started = threading.Event(), threading.Event()
    def slow(config, url, body):
        started.set()
        assert gate.wait(5)
        return _post(client)(config, url, body)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        first = pool.submit(sync_agy, cfg, path, slow)
        assert started.wait(5)
        second = pool.submit(sync_agy, cfg, path, _post(client))
        gate.set()
        assert first.result(timeout=10) is True
        assert second.result(timeout=10) is True
    _, events = _events(client)
    assert sum(e['kind'] == 'tool_call' for e in events) == 1
    assert sum(e['kind'] == 'user_message' for e in events) == 1


def test_reverted_title_and_active_snapshot_are_new_observations(tmp_path):
    from tools.session.agy_archive import sync_agy
    path = _native(tmp_path, rows=[(15, 'A', 2, None)])
    cfg, client = _cfg(tmp_path), _client(tmp_path)
    for title, text in [('A', 'A'), ('B', 'B'), ('A', 'A')]:
        with sqlite3.connect(path.parent.parent / 'conversation_summaries.db') as db:
            db.execute('UPDATE conversation_summaries SET title=?', (title,))
        with sqlite3.connect(path) as db:
            payload, metadata = _step(15, text, status=2)
            db.execute('UPDATE steps SET step_payload=?,metadata=? WHERE idx=0', (payload, metadata))
        sync_agy(cfg, path, _post(client))
    session, events = _events(client)
    assert session['native_title'] == 'A'
    messages = [e for e in events if e['kind'] == 'assistant_message']
    assert [e['payload']['text'] for e in messages] == ['A', 'B', 'A']
    assert len({e['payload']['message_id'] for e in messages}) == 1
    assert len({e['event_id'] for e in messages}) == 3


def test_symlink_source_is_refused_and_sqlite_lock_is_bounded(tmp_path):
    from tools.session.agy_storage import read_page
    path = _native(tmp_path)
    alias = path.parent.parent / path.name
    try:
        alias.symlink_to(path)
    except OSError:
        pytest.skip('symlink unavailable')
    with pytest.raises(ValueError, match='native_metadata_unsafe'):
        read_page(alias, -1)
    with sqlite3.connect(path) as db:
        db.execute('BEGIN EXCLUSIVE')
        with pytest.raises(sqlite3.OperationalError):
            read_page(path, -1)


def test_stop_mid_page_preserves_exact_prefix_for_restart(tmp_path):
    from tools.session.agy_archive import sync_agy
    path, cfg, client = _native(tmp_path), _cfg(tmp_path), _client(tmp_path)
    checks = [0]
    def stop():
        checks[0] += 1
        return checks[0] >= 3
    assert sync_agy(cfg, path, _post(client), should_stop=stop) is False
    assert json.loads(_checkpoint_path(cfg, path).read_text())['cursor'] == 0
    assert sync_agy(cfg, path, _post(client)) is True
    assert sum(e['kind'] == 'user_message' for e in _events(client)[1]) == 1


def test_real_best_effort_quota_gap_ack_cannot_be_mistaken_for_message_receipt(tmp_path, monkeypatch):
    from tools.session import agy_archive
    from tools.session.runner_capture import replay_pending
    from tools.session.spool import LocalSpool
    path, cfg, client = _native(tmp_path), _cfg(tmp_path), _client(tmp_path)
    real_append, real_quota = LocalSpool.append, LocalSpool._within_quota
    failure, in_target = [True], [False]
    def append(self, event):
        in_target[0] = failure[0] and event['kind'] == 'tool_result'
        try:
            return real_append(self, event)
        finally:
            in_target[0] = False
    def quota(self, size):
        if in_target[0] and failure[0]:
            failure[0] = False  # the smaller actual gap marker fits afterward
            return False
        return real_quota(self, size)
    monkeypatch.setattr(LocalSpool, 'append', append)
    monkeypatch.setattr(LocalSpool, '_within_quota', quota)
    with pytest.raises(RuntimeError, match='event_not_persisted'):
        agy_archive.sync_agy(cfg, path, _post(client))
    assert json.loads(_checkpoint_path(cfg, path).read_text())['pending'] is not None
    replay_pending(cfg, _post(client))  # ACK and compact the real quota gap
    assert agy_archive.sync_agy(cfg, path, _post(client)) is True
    events = _events(client)[1]
    assert sum(e['kind'] == 'tool_result' for e in events) == 1
    assert sum(e['kind'] == 'tool_call' for e in events) == 1
    assert any(e['kind'] == 'capture_gap' for e in events)


def test_truncated_native_history_defers_without_resetting_cursor(tmp_path):
    from tools.session.agy_archive import sync_agy
    path, cfg, client = _native(tmp_path), _cfg(tmp_path), _client(tmp_path)
    sync_agy(cfg, path, _post(client))
    checkpoint = _checkpoint_path(cfg, path).read_bytes()
    with sqlite3.connect(path) as db:
        db.execute('DELETE FROM steps WHERE idx>0')
    with pytest.raises(ValueError, match='agy_history_rewound'):
        sync_agy(cfg, path, _post(client))
    assert _checkpoint_path(cfg, path).read_bytes() == checkpoint


def test_schema_without_index_and_unsupported_step_are_refused(tmp_path):
    from tools.session.agy_storage import read_page
    path = _native(tmp_path)
    with sqlite3.connect(path) as db:
        payload, metadata = _step(15, 'text')
        payload = _pb((1, 999), (4, 3))
        db.execute('UPDATE steps SET step_type=999,step_payload=? WHERE idx=0', (payload,))
    with pytest.raises(ValueError, match='agy_step_unsupported'):
        read_page(path, -1)
    with sqlite3.connect(path) as db:
        db.execute('ALTER TABLE steps RENAME TO old_steps')
        db.execute('CREATE TABLE steps AS SELECT * FROM old_steps')
    with pytest.raises(ValueError, match='agy_schema_unsupported'):
        read_page(path, -1)


def test_byte_budget_returns_prefix_and_continues_without_omissions(tmp_path, monkeypatch):
    from tools.session import agy_storage
    path = _native(tmp_path)
    monkeypatch.setattr(agy_storage, 'MAX_PAGE_BYTES', 350)
    cursor, seen, passes = -1, [], 0
    while True:
        metadata, rows, complete = agy_storage.read_page(path, cursor)
        assert rows and len(rows) <= 2
        seen.extend(index for index, _status, _events in rows)
        cursor = rows[-1][0]
        passes += 1
        if complete:
            break
        assert passes < 10
    assert seen == [0, 1, 2, 3]


@pytest.mark.parametrize('kind,field,body,expected', [
    (8, 14, _pb((4, 'visible file'), (9, 'PRIVATE_RAW_FILE')), 'visible file'),
    (9, 15, _pb((3, _pb((1, 'file-a'))), (3, _pb((1, 'file-b')))), 'file-a\nfile-b'),
    (7, 13, _pb((3, 'visible search')), 'visible search'),
    (21, 28, _pb((26, _pb((1, 'visible command output'))), (24, 'PRIVATE_DEBUG')), 'visible command output'),
    (33, 42, _pb((5, 'visible web summary')), 'visible web summary'),
    (5, 10, _pb((26, 'visible edit description')), 'visible edit description'),
])
def test_legacy_visible_tool_fields_are_allowlisted(kind, field, body, expected):
    from tools.session.agy_storage import project_step
    payload = _pb((1, kind), (4, 3), (field, body))
    events = project_step(CID, 0, kind, 3, payload, b'')
    assert events[-1]['kind'] == 'tool_result'
    assert events[-1]['payload']['result'] == expected
    assert 'PRIVATE_' not in json.dumps(events)
