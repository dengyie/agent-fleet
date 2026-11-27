"""Read-only Hermes history through the actual spool and operator HTTP API."""
import hashlib
import json
import sqlite3

import pytest

from tests.test_review_node_sync import _cfg, _client
from tests.test_agy_native_history import _post


def _native(tmp_path, monkeypatch=None):
    root = tmp_path / '.hermes'
    root.mkdir()
    if monkeypatch:
        monkeypatch.setenv('HOME', str(tmp_path))
        monkeypatch.setenv('USERPROFILE', str(tmp_path))
        monkeypatch.delenv('HERMES_HOME', raising=False)
    path = root / 'state.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE sessions(id TEXT PRIMARY KEY,title TEXT,model TEXT,started_at REAL,source TEXT,cwd TEXT,system_prompt TEXT)')
        db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?)', ('session_fixture', 'Hermes 原生名称', 'visible-model', 1795700000, 'cli', str(tmp_path), 'PRIVATE_SYSTEM'))
        db.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,tool_calls TEXT,tool_call_id TEXT,tool_name TEXT,timestamp REAL,active INTEGER,compacted INTEGER,_compressed_summary INTEGER,display_kind TEXT,reasoning TEXT)')
        db.execute('CREATE INDEX idx_messages_session_id ON messages(session_id,id)')
        rows = [
            (1, 'user', '你好', None, None, None, 1, 0, 1, 0),
            (2, 'assistant', 'visible reply', json.dumps([{'id':'call_fixture','function':{'name':'read','arguments':'{"path":"file.txt"}'}}]), None, None, 2, 0, 1, 0),
            (3, 'tool', 'file contents', None, 'call_fixture', 'read', 3, 0, 1, 0),
            # A native compaction generation copies identical display records.
            (4, 'user', '你好', None, None, None, 1, 1, 0, 0),
            (5, 'assistant', 'visible reply', json.dumps([{'id':'call_fixture','function':{'name':'read','arguments':'{"path":"file.txt"}'}}]), None, None, 2, 1, 0, 0),
            (6, 'user', '你好', None, None, None, 6, 1, 0, 0),
            (7, 'user', 'PRIVATE_REWIND', None, None, None, 7, 0, 0, 0),
            (8, 'user', 'PRIVATE_SUMMARY', None, None, None, 8, 1, 0, 1),
            (9, 'system', 'PRIVATE_SYSTEM', None, None, None, 9, 1, 0, 0),
        ]
        for idx,role,text,calls,call,name,stamp,active,compacted,summary in rows:
            db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', (idx,'session_fixture',role,text,calls,call,name,stamp,active,compacted,summary,None,'PRIVATE_REASONING'))
    return path


def _events(client):
    sessions = client.get('/api/sessions?limit=100').get_json()['sessions']
    rows = [r for r in sessions if r['agent_family']=='hermes']
    assert len(rows)==1
    page=client.get('/api/sessions/'+rows[0]['session_id']+'/events?limit=1000&include_source=1').get_json()
    events=page['events']
    while page['has_more']:
        page=client.get('/api/sessions/'+rows[0]['session_id']+'/events',query_string={'after_cursor':page['next_cursor'],'limit':1000,'include_source':1}).get_json()
        events+=page['events']
    return rows[0],events


def _sync(tmp_path,path,*,cfg=None,client=None,send=None):
    from tools.session.hermes_archive import sync_hermes
    cfg,client=cfg or _cfg(tmp_path),client or _client(tmp_path)
    done=sync_hermes(cfg,path/'session_fixture',send or _post(client))
    return cfg,client,done


def test_default_worker_discovers_hermes_and_exact_visible_history(tmp_path,monkeypatch):
    from tools.session.sync import NativeArchiveSync
    path=_native(tmp_path,monkeypatch)
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    cfg,client=_cfg(tmp_path),_client(tmp_path)
    worker=NativeArchiveSync(cfg,send=_post(client))
    worker.scan_once()
    session,events=_events(client)
    assert session['native_title']=='Hermes 原生名称' and session['managed'] is False
    assert [e['payload']['text'] for e in events if e['kind']=='user_message']==['你好','你好']
    assert sum(e['kind']=='assistant_message' for e in events)==1
    assert [e['kind'] for e in events if e['kind'].startswith('tool_')]==['tool_call','tool_result']
    assert 'PRIVATE_' not in json.dumps(events) and str(path) not in json.dumps(events)
    worker.scan_once()
    assert _events(client)[1]==events
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before


def test_lost_http_response_and_restart_recover_exact_events(tmp_path):
    from tools.session.hermes_archive import sync_hermes
    from tools.session.runner_capture import replay_pending
    path=_native(tmp_path);cfg,client=_cfg(tmp_path),_client(tmp_path)
    def lost(config,url,body):
        _post(client)(config,url,body)
        return 0,{}
    with pytest.raises(RuntimeError,match='conversation_upload_pending'):
        sync_hermes(cfg,path/'session_fixture',lost)
    original=_events(client)[1]
    replay_pending(cfg,_post(client))
    assert sync_hermes(cfg,path/'session_fixture',_post(client))
    assert _events(client)[1]==original


def test_durable_append_checkpoint_failure_and_replay_do_not_duplicate(tmp_path,monkeypatch):
    from tools.session import hermes_archive as module
    from tools.session.runner_capture import replay_pending
    path=_native(tmp_path);cfg,client=_cfg(tmp_path),_client(tmp_path)
    real=module.atomic_private_json;blocked=[True]
    def write(p,state):
        if blocked[0] and state.get('cursor')==1:
            raise OSError('fixture disk failure')
        return real(p,state)
    monkeypatch.setattr(module,'atomic_private_json',write)
    with pytest.raises(OSError):module.sync_hermes(cfg,path/'session_fixture',_post(client))
    blocked[0]=False
    replay_pending(cfg,_post(client))
    module.sync_hermes(cfg,path/'session_fixture',_post(client))
    messages=[e for e in _events(client)[1] if e['kind']=='user_message']
    assert len(messages)==2 and len({e['event_id'] for e in messages})==2


def test_titles_wal_new_messages_and_structured_content(tmp_path):
    from tools.session.sync import NativeArchiveSync
    path=_native(tmp_path);cfg,client=_cfg(tmp_path),_client(tmp_path)
    worker=NativeArchiveSync(cfg,sources=lambda:[('hermes',path.parent)],send=_post(client));worker.scan_once()
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('UPDATE sessions SET title=?',('Renamed native',))
        text='\x00json:'+json.dumps([{'type':'text','text':'visible structured'},{'type':'image_url','image_url':'PRIVATE_IMAGE'}])
        db.execute('INSERT INTO messages VALUES (10,?,?,?,?,?,?,10,1,0,0,NULL,NULL)',('session_fixture','assistant',text,None,None,None));db.commit()
        worker.scan_once()
        session,events=_events(client)
        assert session['native_title']=='Renamed native'
        assert any(e['payload'].get('text')=='visible structured' for e in events)
        assert 'PRIVATE_IMAGE' not in json.dumps(events)


def test_corrupt_and_future_ledger_fail_closed(tmp_path):
    from tools.session.hermes_archive import ledger
    target=tmp_path/'ledger.sqlite'
    target.write_bytes(b'broken')
    with pytest.raises((ValueError,sqlite3.Error)):
        with ledger(target):pass
    assert target.read_bytes()==b'broken'
    future=tmp_path/'future.sqlite'
    with sqlite3.connect(future) as db:db.execute('PRAGMA user_version=99')
    before=future.read_bytes()
    with pytest.raises(ValueError):
        with ledger(future):pass
    assert future.read_bytes()==before


def test_session_catalog_and_messages_are_bounded(tmp_path):
    from tools.session.hermes_archive import session_paths,read_page
    path=_native(tmp_path)
    with sqlite3.connect(path) as db:
        db.executemany('INSERT INTO sessions(id,title) VALUES (?,?)',[(f'extra_{i:04d}','extra') for i in range(100)])
        db.executemany('INSERT INTO messages(id,session_id,role,content,timestamp,active,compacted) VALUES (?,\'session_fixture\',\'user\',?, ?,1,0)',[(i,str(i),i) for i in range(10,100)])
    assert len(list(session_paths(path.parent)))==101
    _,rows,done=read_page(path/'session_fixture',-1)
    assert len(rows)==32 and not done
    _,more,_=read_page(path/'session_fixture',rows[-1][0])
    assert rows[-1][0]<more[0][0]


def _checkpoint_path(cfg,path):
    from tools.session.sync import native_identity
    from tools.session.runner_capture import session_prefix
    return cfg.cache_dir/'conversations'/(session_prefix(cfg.machine)+native_identity('hermes',path/'session_fixture')+'.hermes-cursor')


def test_crash_immediately_after_append_then_replay_recovers_receipt(tmp_path,monkeypatch):
    from tools.session import hermes_archive as module
    from tools.session.runner_capture import replay_pending
    path=_native(tmp_path);cfg,client=_cfg(tmp_path),_client(tmp_path)
    real=module.atomic_private_json;fault=[True]
    def write(p,state):
        if fault[0] and state['pending'] is None and len(state['receipts'])==1:
            raise OSError('after append')
        return real(p,state)
    monkeypatch.setattr(module,'atomic_private_json',write)
    with pytest.raises(OSError):module.sync_hermes(cfg,path/'session_fixture',_post(client))
    assert json.loads(_checkpoint_path(cfg,path).read_text(encoding='utf-8'))['pending']
    fault[0]=False
    replay_pending(cfg,_post(client))
    assert module.sync_hermes(cfg,path/'session_fixture',_post(client))
    assert len([e for e in _events(client)[1] if e['kind']=='user_message'])==2


def test_real_quota_gap_is_not_a_source_receipt(tmp_path,monkeypatch):
    from tools.session.hermes_archive import sync_hermes
    from tools.session.runner_capture import replay_pending
    from tools.session.spool import LocalSpool
    path=_native(tmp_path);cfg,client=_cfg(tmp_path),_client(tmp_path)
    original,quota=LocalSpool.append,LocalSpool._within_quota
    target,failed=[False],[False]
    def append(self,event):
        target[0]=not failed[0] and event['kind']=='tool_result'
        try:return original(self,event)
        finally:target[0]=False
    def allowed(self,size):
        if target[0] and not failed[0]:
            failed[0]=True
            return False
        return quota(self,size)
    monkeypatch.setattr(LocalSpool,'append',append)
    monkeypatch.setattr(LocalSpool,'_within_quota',allowed)
    with pytest.raises(RuntimeError):sync_hermes(cfg,path/'session_fixture',_post(client))
    replay_pending(cfg,_post(client))
    sync_hermes(cfg,path/'session_fixture',_post(client))
    events=_events(client)[1]
    assert sum(e['kind']=='tool_result' for e in events)==1
    assert sum(e['kind']=='tool_call' for e in events)==1
    assert any(e['kind']=='capture_gap' for e in events)


def test_concurrent_scanners_publish_one_copy(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from tools.session.hermes_archive import sync_hermes
    path=_native(tmp_path);cfg,client=_cfg(tmp_path),_client(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:sync_hermes(cfg,path/'session_fixture',_post(client)),range(2)))
    assert all(results)
    assert sum(e['kind']=='tool_call' for e in _events(client)[1])==1


@pytest.mark.parametrize('raw',['broken','{}','{"version":99}',pytest.param('x'*2100000,id='oversized-checkpoint')])
def test_invalid_checkpoint_is_preserved_and_never_uploaded(tmp_path,raw):
    from tools.session.hermes_archive import sync_hermes
    path=_native(tmp_path);cfg,client=_cfg(tmp_path),_client(tmp_path)
    checkpoint=_checkpoint_path(cfg,path);checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(raw,encoding='utf-8')
    with pytest.raises(ValueError,match='hermes_checkpoint_invalid'):
        sync_hermes(cfg,path/'session_fixture',_post(client))
    assert checkpoint.read_text(encoding='utf-8')==raw
    assert client.get('/api/sessions').get_json()['sessions']==[]


def test_ledger_empty_initialization_is_atomic_and_foreign_v0_is_preserved(tmp_path,monkeypatch):
    from tools.session import hermes_archive as module
    original=sqlite3.connect
    class Failed:
        def __init__(self,path,**kwargs):self.db=original(path,**kwargs)
        def execute(self,sql,*args):
            if sql=='PRAGMA user_version=1':raise sqlite3.OperationalError('fixture commit boundary')
            return self.db.execute(sql,*args)
        def __getattr__(self,name):return getattr(self.db,name)
    path=tmp_path/'empty.sqlite'
    monkeypatch.setattr(module.sqlite3,'connect',Failed)
    with pytest.raises(sqlite3.OperationalError):
        with module.ledger(path):pass
    monkeypatch.setattr(module.sqlite3,'connect',original)
    with original(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==0
        assert db.execute('SELECT name FROM sqlite_master').fetchall()==[]
    with module.ledger(path) as db:assert db.execute('PRAGMA user_version').fetchone()[0]==1
    foreign=tmp_path/'foreign.sqlite'
    with original(foreign) as db:db.execute('CREATE TABLE unrelated(important TEXT)')
    before=foreign.read_bytes()
    with pytest.raises(ValueError):
        with module.ledger(foreign):pass
    assert foreign.read_bytes()==before


def test_large_visible_fields_are_complete_and_secret_redacted(tmp_path):
    path=_native(tmp_path)
    text='多字节🙂'*4000
    result='output '*6000+'\nAuthorization: Bearer fixture_secret_1234567890\n'
    with sqlite3.connect(path) as db:
        db.execute('UPDATE messages SET content=? WHERE id=1',(text,))
        db.execute('UPDATE messages SET content=? WHERE id=3',(result,))
    _,client,_=_sync(tmp_path,path)
    events=_events(client)[1]
    user=[e['payload']['text'] for e in events if e['kind']=='user_message']
    assert ''.join(user).startswith(text)
    archive=''.join(e['payload']['text'] for e in events if e['kind']=='source_record')
    assert 'output '*6000 in archive and 'fixture_secret_' not in json.dumps(events)


def test_stop_and_source_truncation_preserve_position(tmp_path):
    from tools.session.hermes_archive import sync_hermes
    path=_native(tmp_path);cfg,client=_cfg(tmp_path),_client(tmp_path)
    assert sync_hermes(cfg,path/'session_fixture',_post(client),should_stop=lambda:True) is False
    assert client.get('/api/sessions').get_json()['sessions']==[]
    assert sync_hermes(cfg,path/'session_fixture',_post(client))
    before=_checkpoint_path(cfg,path).read_bytes()
    with sqlite3.connect(path) as db:db.execute('DELETE FROM messages')
    with pytest.raises(ValueError,match='hermes_history_rewound'):
        sync_hermes(cfg,path/'session_fixture',_post(client))
    assert _checkpoint_path(cfg,path).read_bytes()==before


def test_old_optional_columns_and_empty_session(tmp_path):
    from tools.session.hermes_archive import sync_hermes
    path=_native(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute('DROP TABLE messages')
        db.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,tool_calls TEXT,tool_call_id TEXT,tool_name TEXT,timestamp REAL)')
        db.execute('CREATE INDEX idx_messages_session_id ON messages(session_id,id)')
        db.execute('ALTER TABLE sessions DROP COLUMN title')
        db.execute('ALTER TABLE sessions DROP COLUMN model')
    cfg,client=_cfg(tmp_path),_client(tmp_path)
    assert sync_hermes(cfg,path/'session_fixture',_post(client))
    assert not any(e['kind']=='user_message' for e in _events(client)[1])


def test_rewind_only_original_does_not_hide_visible_compaction_clone(tmp_path):
    path=_native(tmp_path)
    with sqlite3.connect(path) as db:
        # Native compaction marks the carried original as rewind-only and
        # inserts a visible clone, with the same native display-generation key.
        db.execute('UPDATE messages SET active=0,compacted=0 WHERE id=1')
    _,client,_=_sync(tmp_path,path)
    assert [e['payload']['text'] for e in _events(client)[1] if e['kind']=='user_message']==['你好','你好']


def test_missing_native_index_defers_instead_of_scanning_history(tmp_path):
    from tools.session.hermes_archive import read_page
    path=_native(tmp_path)
    with sqlite3.connect(path) as db:db.execute('DROP INDEX idx_messages_session_id')
    with pytest.raises(ValueError,match='hermes_index_unsupported'):
        read_page(path/'session_fixture',-1)
