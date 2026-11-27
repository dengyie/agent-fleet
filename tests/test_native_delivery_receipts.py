"""A native retry under a new sequence must not strand the durable ACK."""
import sqlite3

import pytest

from hub.infrastructure.transcript_repository import TranscriptRepository
from tests.test_transcript_repository import _event
from tests.test_review_node_sync import _cfg, _client
from tests.test_agy_native_history import _post


def test_original_native_event_at_new_sequence_advances_ack_without_new_public_event(tmp_path):
    repo=TranscriptRepository(tmp_path/'transcripts.db');repo.init()
    original=_event(seq=1)
    assert repo.ingest(original).status=='accepted'
    retry=dict(original,sequence=2,emitted_at='2026-11-28T00:00:00Z')
    assert repo.ingest(retry).status=='duplicate'
    assert repo.contiguous_sequence(original['session_id'])==2
    assert len(repo.read_redacted(original['session_id']))==1
    repo=TranscriptRepository(repo.db_path);repo.init()
    assert repo.contiguous_sequence(original['session_id'])==2
    changed=dict(retry,sequence=3,payload={'text':'different body','is_complete':True})
    assert repo.ingest(changed).status=='rejected'
    assert repo.contiguous_sequence(original['session_id'])==2


def test_receipt_never_overwrites_another_stream_sequence(tmp_path):
    repo=TranscriptRepository(tmp_path/'transcripts.db');repo.init()
    original=_event(seq=1);repo.ingest(original)
    repo.ingest(_event(seq=2,event_id='evt_other',text='another event'))
    assert repo.ingest(dict(original,sequence=2)).status=='rejected'
    assert [x['event_id'] for x in repo.read_redacted(original['session_id'])]==[original['event_id'],'evt_other']


def test_actual_runner_spool_retry_receipt_recovers_and_compacts(tmp_path):
    from tools.session.runner_capture import RunnerCapture
    client,cfg=_client(tmp_path),_cfg(tmp_path)
    task={'attempt_id':'native_fixture','agent_type':'zcode','task_id':''}
    capture=RunnerCapture(cfg,task,_post(client),preserve_source=False)
    try:
        row={'kind':'tool_call','event_id':'evt_'+'a'*40,'payload':{'call_id':'call_fixture','tool_name':'read','arguments':'{}'}}
        capture.bridge.ingest_one(row,strict=True);capture.flush()
        first=capture.bridge.spool.status()['ack_sequence']
        capture.bridge.ingest_one(row,strict=True);capture.flush()
        assert capture.bridge.spool.status()['ack_sequence']==first+1
        sid=capture.session_id
        events=client.get('/api/sessions/'+sid+'/events?limit=100').get_json()['events']
        assert sum(e['kind']=='tool_call' for e in events)==1
    finally:capture.bridge.close()


def test_v1_receipt_migration_is_atomic_and_future_version_is_preserved(tmp_path,monkeypatch):
    path=tmp_path/'transcripts.db';repo=TranscriptRepository(path);repo.init()
    with sqlite3.connect(path) as db:
        db.execute('DROP TABLE delivery_receipts')
        db.execute("DELETE FROM meta WHERE key='delivery_receipts_schema'")
    real=repo._connect
    class Failing:
        def __init__(self):self.db=real()
        def execute(self,sql,*args):
            if 'INSERT INTO meta' in sql and 'delivery_receipts_schema' in sql:raise sqlite3.OperationalError('fixture migration')
            return self.db.execute(sql,*args)
        def __getattr__(self,key):return getattr(self.db,key)
    monkeypatch.setattr(repo,'_connect',Failing)
    with pytest.raises(sqlite3.OperationalError):repo.init()
    with sqlite3.connect(path) as db:
        assert db.execute("select name from sqlite_master where name='delivery_receipts'").fetchone() is None
    monkeypatch.setattr(repo,'_connect',real);repo.init()
    with sqlite3.connect(path) as db:db.execute("UPDATE meta SET value='99' WHERE key='delivery_receipts_schema'")
    with pytest.raises(ValueError):repo.init()
    with sqlite3.connect(path) as db:assert db.execute("SELECT value FROM meta WHERE key='delivery_receipts_schema'").fetchone()[0]=='99'


def test_receipts_are_stream_scoped_and_concurrent_retries_are_one_row(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    repo=TranscriptRepository(tmp_path/'transcripts.db');repo.init()
    original=_event(seq=1);repo.ingest(original)
    with ThreadPoolExecutor(max_workers=3) as pool:
        rows=list(pool.map(lambda _:repo.ingest(dict(original,sequence=2)),range(3)))
    assert all(r.status=='duplicate' for r in rows)
    with sqlite3.connect(repo.db_path) as db:assert db.execute('SELECT COUNT(*) FROM delivery_receipts').fetchone()[0]==1
    other=_event(seq=2,event_id='evt_other_stream');other['stream_id']='other'
    assert repo.ingest(other).status=='accepted'
    changed=dict(original,sequence=3,stream_id='other')
    assert repo.ingest(changed).status=='rejected'


def test_corrupt_declared_receipt_schema_fails_closed(tmp_path):
    repo=TranscriptRepository(tmp_path/'transcripts.db');repo.init()
    with sqlite3.connect(repo.db_path) as db:db.execute('DROP TABLE delivery_receipts')
    with pytest.raises(ValueError,match='delivery_receipts_schema'):repo.init()
    with sqlite3.connect(repo.db_path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='delivery_receipts'").fetchone() is None


@pytest.mark.parametrize('damage', ['missing_foreign_key', 'wrong_type', 'nullable_event'])
def test_lookalike_receipt_schema_is_preserved_and_rejected(tmp_path, damage):
    repo = TranscriptRepository(tmp_path / 'transcripts.db'); repo.init()
    with sqlite3.connect(repo.db_path) as db:
        db.execute('DROP TABLE delivery_receipts')
        sequence_type = 'TEXT' if damage == 'wrong_type' else 'INTEGER'
        required = '' if damage == 'nullable_event' else ' NOT NULL'
        relation = '' if damage == 'missing_foreign_key' else (
            ', FOREIGN KEY(session_id,event_id) REFERENCES '
            'redacted_events(session_id,event_id) ON DELETE CASCADE')
        db.execute('CREATE TABLE delivery_receipts (session_id TEXT NOT NULL, '
                   'machine_id TEXT NOT NULL, stream_id TEXT NOT NULL, sequence '
                   + sequence_type + ' NOT NULL, event_id TEXT' + required
                   + ', PRIMARY KEY(session_id,sequence,machine_id,stream_id)'
                   + relation + ') WITHOUT ROWID')
        original = db.execute("SELECT sql FROM sqlite_master WHERE name='delivery_receipts'").fetchone()[0]
    with pytest.raises(ValueError, match='delivery_receipts_schema'):
        repo.init()
    with sqlite3.connect(repo.db_path) as db:
        assert db.execute("SELECT sql FROM sqlite_master WHERE name='delivery_receipts'").fetchone()[0] == original
