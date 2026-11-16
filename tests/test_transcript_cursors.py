"""Hub ingestion cursors are independent of every source stream's sequence."""
import json
import sqlite3

from hub.infrastructure.transcript_repository import TranscriptRepository
from tests.test_transcript_repository import _event
from tests.test_assistant_vm_sessions import app, execute  # noqa: F401 - shared pytest fixture
from tests.test_session_interactions import event


def test_equal_source_sequences_and_late_lower_sequences_are_all_pageable(tmp_path):
    repo = TranscriptRepository(tmp_path / 'transcripts.db')
    repo.init()
    for stream, sequence in [('a', 10), ('b', 10), ('c', 1)]:
        value = _event(seq=sequence, event_id='evt_' + stream)
        value['stream_id'] = stream
        assert repo.ingest(value).status in {'accepted', 'gap'}
    cursor, ids = 0, []
    for _ in range(4):
        rows = repo.read_redacted('sess_t5_tr1', limit=1, after_cursor=cursor)
        if rows:
            ids.append(rows[0]['event_id'])
            assert rows[0]['cursor'] > cursor
            cursor = rows[0]['cursor']
    assert ids == ['evt_a', 'evt_b', 'evt_c']
    # Duplicate delivery does not allocate a new public position.
    assert repo.ingest(value).status == 'duplicate'
    assert repo.read_redacted('sess_t5_tr1', after_cursor=cursor) == []


def test_legacy_backfill_and_reopen_preserve_cursors_and_append_order(tmp_path):
    path = tmp_path / 'transcripts.db'
    with sqlite3.connect(path) as conn:
        conn.executescript('''CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT INTO meta VALUES('schema_version','1');
            CREATE TABLE redacted_events(session_id TEXT NOT NULL,event_id TEXT NOT NULL,
                stream_id TEXT,machine_id TEXT,sequence INTEGER NOT NULL,kind TEXT NOT NULL,
                capture_quality TEXT NOT NULL,emitted_at TEXT NOT NULL,redacted_json TEXT NOT NULL,
                PRIMARY KEY(session_id,event_id));''')
        for seq in [7, 2]:
            row = _event(seq=seq, text='legacy')
            conn.execute('INSERT INTO redacted_events VALUES(?,?,?,?,?,?,?,?,?)',
                (row['session_id'], row['event_id'], row['stream_id'], row['machine_id'], seq,
                 row['kind'], row['capture_quality'], row['emitted_at'], json.dumps(row)))
    repo = TranscriptRepository(path)
    repo.init()
    first = repo.read_redacted('sess_t5_tr1')
    assert [r['sequence'] for r in first] == [7, 2]
    cursors = [r['cursor'] for r in first]
    repo.init()
    assert [r['cursor'] for r in repo.read_redacted('sess_t5_tr1')] == cursors
    assert repo.ingest(_event(seq=3)).status in {'accepted', 'gap'}
    assert repo.read_redacted('sess_t5_tr1', after_cursor=cursors[-1])[0]['sequence'] == 3
    with repo._connect() as conn:
        conn.execute('DELETE FROM redacted_events')
        conn.execute('VACUUM')
    repo.ingest(_event(seq=1))
    assert repo.read_redacted('sess_t5_tr1')[0]['cursor'] > cursors[-1]


def test_assistant_and_http_cursor_continue_across_streams_and_late_ingestion(app):
    sessions = app.extensions['fleet']['services']['sessions']
    first = execute(app, 'session.read_events', {'session_id': 's1', 'limit': 2})
    for stream in ['other', 'late']:
        row = event(1, 'assistant_message', {'text': stream, 'is_complete': True})
        row.update(stream_id=stream, event_id='evt_' + stream)
        assert sessions.ingest_events([row])['rejected'] == []
    second = execute(app, 'session.read_events', {'session_id': 's1', 'limit': 1,
        'after_cursor': first['next_cursor']})
    third = app.test_client().get('/api/sessions/s1/events',
        query_string={'limit': 1, 'after_cursor': second['next_cursor']}).json
    assert second['events'][0]['payload']['text'] == 'other'
    assert third['events'][0]['payload']['text'] == 'late'
    assert int(third['next_cursor']) > int(second['next_cursor'])
    assert third['has_more'] is False
