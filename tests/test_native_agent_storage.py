"""Read exact native identities and real tool evidence from local SQLite stores."""
import json
import sqlite3

import pytest

from tools.session.native_storage import read_events, verify_identity


def test_zcode_filters_synthetic_and_thinking_and_reads_actual_tool_parts(tmp_path):
    path = tmp_path / 'db.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('create table session(id text, directory text)')
        db.execute('insert into session values (?,?)', ('sess_test', str(tmp_path)))
        db.execute('create table message(id text,session_id text,data text)')
        db.execute('create table part(message_id text,session_id text,data text)')
        for mid, message, part in [
            ('hidden', {'role':'user','synthetic':True}, {'type':'text','text':'private system'}),
            ('think', {'role':'assistant'}, {'type':'reasoning','text':'private reasoning'}),
            ('reply', {'role':'assistant'}, {'type':'text','text':'已读取 🌲'}),
            ('tool', {'role':'assistant'}, {'type':'tool','callID':'c1','tool':'Read',
                'state':{'status':'completed','input':{'file_path':'probe.txt'},'output':'real file contents'}}),
        ]:
            db.execute('insert into message values (?,?,?)',(mid,'sess_test',json.dumps(message)))
            db.execute('insert into part values (?,?,?)',(mid,'sess_test',json.dumps(part)))
    assert verify_identity('zcode', path, 'sess_test', str(tmp_path))
    events, cursor = read_events('zcode', path, 'sess_test', 0)
    assert [x['kind'] for x in events] == ['assistant_message','tool_call','tool_result']
    assert events[-1]['payload']['result'] == 'real file contents'
    assert 'private' not in json.dumps(events)
    assert read_events('zcode', path, 'sess_test', cursor) == ([],cursor)
    first_page, first_cursor = read_events('zcode', path, 'sess_test', 2, limit=1)
    second_page, second_cursor = read_events('zcode', path, 'sess_test', first_cursor, limit=1)
    assert [event['kind'] for event in first_page] == ['assistant_message']
    assert [event['kind'] for event in second_page] == ['tool_call', 'tool_result']
    assert first_cursor < second_cursor == cursor
    assert [event['event_id'] for event in first_page] == [events[0]['event_id']]
    assert [event['event_id'] for event in second_page] == [
        event['event_id'] for event in events[1:]]
    assert not verify_identity('zcode',path,'sess_test',str(tmp_path/'wrong'))
    assert not verify_identity('zcode',path,'another',str(tmp_path))


def test_hermes_tools_are_native_messages_not_answer_claims(tmp_path):
    path = tmp_path/'state.db'
    with sqlite3.connect(path) as db:
        db.execute('create table sessions(id text,cwd text)')
        db.execute('insert into sessions values (?,?)',('native',str(tmp_path)))
        db.execute('create table messages(id integer primary key,session_id text,role text,content text,tool_calls text,tool_call_id text,tool_name text)')
        db.execute('insert into messages values (1,?,?,?,?,?,?)',('native','assistant','',json.dumps([
            {'id':'c1','function':{'name':'terminal','arguments':'{"command":"cat probe.txt"}'}}]),None,None))
        db.execute('insert into messages values (2,?,?,?,?,?,?)',('native','tool','{"output":"actual bytes","exit_code":0}',None,'c1','terminal'))
        db.execute('insert into messages values (3,?,?,?,?,?,?)',('native','assistant','verified reply',None,None,None))
    events,cursor=read_events('hermes',path,'native',0)
    assert [x['kind'] for x in events]==['tool_call','tool_result','assistant_message']
    assert events[1]['payload']['call_id']=='c1'
    assert 'actual bytes' in events[1]['payload']['result']
    assert read_events('hermes',path,'native',cursor)==([],cursor)


def test_missing_malformed_or_excessive_database_fails_closed(tmp_path):
    path=tmp_path/'missing.db'
    assert not verify_identity('hermes',path,'native',str(tmp_path))
    assert not path.exists()
    path.write_bytes(b'not sqlite')
    assert not verify_identity('hermes',path,'native',str(tmp_path))
    with pytest.raises((sqlite3.Error, ValueError)):
        read_events('hermes',path,'native',0)


@pytest.mark.parametrize('terminal_status', ['completed', 'error'])
def test_zcode_running_tool_is_a_cursor_barrier_until_in_place_completion(
        tmp_path, terminal_status):
    path = tmp_path / 'db.sqlite'
    running = {'type': 'tool', 'callID': 'c_running', 'tool': 'Read',
               'state': {'status': 'running', 'input': {'file_path': 'a.txt'}}}
    later = {'type': 'tool', 'callID': 'c_later', 'tool': 'Read',
             'state': {'status': 'completed', 'input': {'file_path': 'b.txt'},
                       'output': 'later result'}}
    with sqlite3.connect(path) as db:
        db.execute('create table message(id text,session_id text,data text)')
        db.execute('create table part(message_id text,session_id text,data text)')
        hidden = {'type': 'tool', 'callID': 'c_hidden', 'tool': 'Read',
                  'state': {'status': 'running', 'input': {}}}
        db.execute('insert into message values (?,?,?)',
                   ('hidden', 'sess_mutable', json.dumps(
                       {'role': 'assistant', 'visibility': 'model-only'})))
        db.execute('insert into part values (?,?,?)',
                   ('hidden', 'sess_mutable', json.dumps(hidden)))
        for mid, part in [('running', running), ('later', later)]:
            db.execute('insert into message values (?,?,?)',
                       (mid, 'sess_mutable', json.dumps({'role': 'assistant'})))
            db.execute('insert into part values (?,?,?)',
                       (mid, 'sess_mutable', json.dumps(part)))

    events, cursor = read_events('zcode', path, 'sess_mutable', 0)
    assert events == []
    assert cursor == 1

    running['state'] = {'status': terminal_status, 'input': {'file_path': 'a.txt'},
                        'output': 'running result', 'error': 'failed to read'}
    with sqlite3.connect(path) as db:
        db.execute('update part set data=? where message_id=?',
                   (json.dumps(running), 'running'))
    events, cursor = read_events('zcode', path, 'sess_mutable', cursor)
    calls = [event['payload']['call_id'] for event in events if event['kind'] == 'tool_call']
    assert calls == ['c_running', 'c_later']
    assert cursor == 3
    result = next(event for event in events
                  if event['kind'] == 'tool_result'
                  and event['payload']['call_id'] == 'c_running')
    assert result['payload']['status'] == ('error' if terminal_status == 'error' else 'ok')
    assert not any(event['payload'].get('call_id') == 'c_hidden' for event in events)
