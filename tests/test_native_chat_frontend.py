"""Native web UI against real Hub/SQLite and signed queue; no vendor execution.

The target discovery fixture exposes TaskService.targets at the proposed thin
HTTP path. Production routing stays exclusively with the backend workstream.
"""
import os
from pathlib import Path
import subprocess
from threading import Thread

import pytest
from flask import jsonify
from werkzeug.serving import make_server

from hub.bootstrap import create_app
from hub.config import FleetConfig
from tests.test_session_access_consistency import adopted, OWNER
from tests.test_session_interactions import event, question

ROOT = Path(__file__).resolve().parents[1]


def test_native_chat_state_and_contracts():
    result = subprocess.run(['node', '--input-type=module'], cwd=ROOT, text=True, capture_output=True, input=r"""
import assert from 'node:assert/strict';
import {mergeNativeEvents, nativeMessages, sessionTitle, uncertainWrite} from './frontend/assets/views/agent-chat/state.js';
import {parseTaskTargets, parseSession} from './frontend/assets/api/contracts.js';
import {createNativeReader} from './frontend/assets/views/agent-chat/live.js';
import {createNativeChatService, createMessageJournal, createTaskJournal} from './frontend/assets/views/agent-chat/service.js';
import {resolveRoute, isPageAllowedForAccount} from './frontend/assets/shell/navigation.js';
assert.equal(resolveRoute(new URL('https://fixture/assistant?view=computer')).nativeKind, 'new');
assert.equal(resolveRoute(new URL('https://fixture/session/s1?view=chat')).page, 'agent-chat');
assert.equal(resolveRoute(new URL('https://fixture/session/s1?sequence=6')).page, 'session');
assert.equal(isPageAllowedForAccount('agent-chat',{role:'member'}),false);
const event = (cursor, stream, text, complete = true) => ({cursor, sequence:1, stream_id:stream, kind:'user_message', payload:{text, is_complete:complete}});
const first = event('9007199254740993', 'a', '继续');
const second = event('9007199254740994', 'b', '继续');
assert.deepEqual(mergeNativeEvents([second], [first, second]).events, [first, second]);
assert.equal(nativeMessages([first, second]).length, 2, 'same text and vendor sequence are distinct native messages');
const chunks = nativeMessages([event('1','a','ab',false), event('2','b','other'), event('3','a','cd')]);
assert.equal(chunks[0].text, 'abcd'); assert.equal(chunks.length, 2);
const snapshots = nativeMessages([
  {cursor:'10',sequence:1,stream_id:'s',kind:'assistant_message',emitted_at:'2026-09-01T00:00:00Z',payload:{text:'partial',message_id:'m1',content_mode:'snapshot',is_complete:false}},
  {cursor:'11',sequence:2,stream_id:'s',kind:'assistant_message',emitted_at:'2026-09-01T00:00:01Z',payload:{text:'final answer',message_id:'m1',content_mode:'snapshot',is_complete:true}},
  {cursor:'12',sequence:3,stream_id:'s',kind:'assistant_message',payload:{text:'delta ',message_id:'m2',content_mode:'delta',is_complete:false}},
  {cursor:'13',sequence:4,stream_id:'s',kind:'assistant_message',payload:{text:'answer',message_id:'m2',content_mode:'delta',is_complete:false}},
  {cursor:'14',sequence:5,stream_id:'s',kind:'assistant_message',payload:{text:'',message_id:'m2',content_mode:'delta',is_complete:true}},
  {cursor:'15',sequence:6,stream_id:'s',kind:'assistant_message',payload:{text:'revised final answer',message_id:'m1',content_mode:'snapshot',is_complete:true}},
]);
assert.equal(snapshots.length, 2);
assert.deepEqual(snapshots.map(row => [row.text,row.complete]), [['revised final answer',true],['delta answer',true]]);
const interactions = nativeMessages([
  {cursor:'20',stream_id:'s',kind:'interaction_request',emitted_at:'2026-09-01T00:00:02Z',payload:{request_id:'q1',prompt:'Choose a route',options:[{id:'safe',label:'Safe route'}]}},
  {cursor:'21',stream_id:'s',kind:'interaction_response',emitted_at:'2026-09-01T00:00:03Z',payload:{request_id:'q1',selected:['safe'],text:'Proceed carefully'}},
]);
assert.deepEqual(interactions.map(row => row.text), ['Agent 提问 · Choose a route','你的回答 · Safe route · Proceed carefully']);
assert.equal(sessionTitle({native_title:'VM native title',title_preview:'fallback'}), 'VM native title');
assert.equal(sessionTitle({title_preview:'first observed user turn'}), 'first observed user turn');
const targets = parseTaskTargets({machine:'m1', projects:['fleet'], agents:{codex:{installed:null, credential:'hidden'}}, internal:'hidden'});
assert.equal(targets.agents.codex.installed, null); assert.equal(targets.agents.codex.credential, undefined);
assert.throws(() => parseTaskTargets({machine:'m1',projects:[],agents:{codex:{installed:'yes'}}}));
const denied = parseSession({session:{session_id:'adopt_a',machine_id:'m1',managed:true,adoption_status:'revoked',control_capability:'available',allowed_actions:['append_user_turn'],message_delivery:{available:true,reason:'node_receipt_required',mechanism:'native_session_resume'}}});
assert.deepEqual(denied.session.allowed_actions, []); assert.equal(denied.session.message_delivery.available, false);
assert.equal(uncertainWrite({status:503}), true); assert.equal(uncertainWrite({status:409}), false);
const positions = [], includeSources = [];
const api = {getSession: async id => ({session:{session_id:id}}), getSessionEvents: async (id, options) => {positions.push(options); includeSources.push(options.includeSource); return {events:[]};}};
const service = createNativeChatService(api);
await service.readSession('s', {position:{tail:true}});
await service.readSession('s', {position:{beforeCursor:'20'}});
await service.readSession('s', {position:{afterCursor:'30'}});
assert.deepEqual(positions.map(({tail,beforeCursor,afterCursor}) => [!!tail,beforeCursor || null,afterCursor || null]),
  [[true,null,null],[false,'20',null],[false,null,'30']], 'tail/history/live cursors stay independent');
assert.deepEqual(includeSources, [false,false,false], 'native chat never downloads archived source records');
const values = new Map();
const journal = createMessageJournal({getItem:key => values.get(key) || null,
  setItem:(key,value) => values.set(key,value), removeItem:key => values.delete(key)}, 's');
const operation = {text:'继续',client_token:'stable-token'};
journal.write(operation);
assert.deepEqual(journal.read(), operation, 'message and idempotency token survive page remount');
journal.clear(); assert.equal(journal.read(), null);
const taskJournal = createTaskJournal({getItem:key => values.get(key) || null,
  setItem:(key,value) => values.set(key,value), removeItem:key => values.delete(key)}, 'task');
const taskRequest = Object.freeze({machine:'m1',project:'fleet',agent_type:'codex',instruction:'inspect',client_token:'task-token'});
taskJournal.write(taskRequest);
assert.deepEqual(taskJournal.read(), taskRequest, 'task response recovery reuses the frozen target and token');
taskJournal.clear();
let reads = 0, active = 0, maximum = 0, release, publications = 0;
const held = new Promise(resolve => {release = resolve;});
const reader = createNativeReader({interval:100000, onError: error => {throw error;}, read:async (signal,current) => {
  reads++; active++; maximum = Math.max(maximum, active);
  if (reads === 1) await held;
  if (current()) publications++;
  active--;
}});
reader.start(); await new Promise(resolve => setTimeout(resolve,10));
reader.wake(); reader.wake(); reader.stop(); reader.start(); release();
await new Promise(resolve => setTimeout(resolve,20)); reader.dispose();
assert.equal(maximum, 1); assert.equal(reads, 2); assert.equal(publications, 1, 'stale pre-suspend request cannot publish');
""")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('scenario', ['read-send', 'lost-response', 'reload-recovery', 'directory', 'directory-pagination', 'history-pagination', 'revoked', 'question-draft', 'missing-targets', 'create', 'task-recovery', 'long-message', 'delivery-disclosure', 'refresh-targets', 'source-record', 'native-metadata', 'directory-metadata'])
def test_native_chat_browser(tmp_path, scenario):
    if not os.environ.get('FLEET_PLAYWRIGHT_MODULE'):
        pytest.skip('Set FLEET_PLAYWRIGHT_MODULE for native web acceptance')
    app = create_app(FleetConfig.from_root(tmp_path, dev_operator=OWNER,
        frontend_dir=ROOT / 'frontend', platform_enabled=True, platform_worker_enabled=True,
        session_repositories_enabled=True, adoption_repositories_enabled=True,
        project_whitelist={'m1': ['agent-fleet']}, supervisor_enabled=True,
        supervisor_signing_raw=bytes(range(32)), supervisor_credentials={'m1':'browser-receipt'},
        append_user_turn_enabled=True))
    row = adopted(app, captured=False)
    services = app.extensions['fleet']['services']
    services['observe'].ingest({'machine':'m1', 'reachable':True, 'agents':{'codex':{'installed':True}}})
    for seq, kind, payload in [(1, 'session_start', {'agent_family':'codex','adapter':'hooks','interactive_questions':True}),
            (2, 'user_message', {'text':'Inspect native project', 'is_complete':True}),
            (3, 'assistant_message', {'text':'Native answer from persisted transcript', 'is_complete':True}),
            (4, 'tool_call', {'tool_name':'read_file','call_id':'call_1','arguments':'{"path":"README.md"}'}),
            (5, 'tool_result', {'tool_name':'read_file','call_id':'call_1','result':'README contents','status':'success'})]:
        item = event(seq, kind, payload); item['session_id'] = row.session_id
        assert services['sessions'].ingest_events([item])['rejected'] == []
    if scenario in {'source-record', 'native-metadata', 'directory-metadata'}:
        kind = 'source_record' if scenario == 'source-record' else 'session_metadata'
        payload = {'text':'PRIVATE_REASONING_SENTINEL'} if scenario == 'source-record' else {
            'title':'VM native title', 'model':'model-x', 'provider':'provider-y',
            'client_version':'client-1.2', 'native_started_at':'2026-11-21T10:00:00Z'}
        item = event(6, kind, payload); item.update(session_id=row.session_id,
            event_id='evt_' + scenario.replace('-', '_'))
        assert services['sessions'].ingest_events([item])['rejected'] == []
    if scenario == 'directory-pagination':
        for index in range(105):
            services['sessions'].session_repo.upsert_session({
                'session_id':f'pagination_{index:03}', 'machine_id':'m1', 'managed':False,
                'agent_family':'codex', 'status':'idle', 'started_at':f'2026-11-21T00:{index // 60:02}:{index % 60:02}Z'})
    if scenario == 'history-pagination':
        for index in range(105):
            item = event(index + 6, 'assistant_message', {'text':f'History page message {index}', 'is_complete':True})
            item['session_id'] = row.session_id
            item['stream_id'] = f'history_{index}'
            assert services['sessions'].ingest_events([item])['rejected'] == []
    if scenario == 'revoked':
        services['adoptions'].revoke(row.session_id, OWNER)
    if scenario == 'question-draft':
        item = event(6, 'interaction_request', question()); item['session_id'] = row.session_id
        assert services['sessions'].ingest_events([item])['rejected'] == []
    if scenario == 'long-message':
        item = event(6, 'assistant_message', {'text': 'x' * 33000, 'is_complete': False})
        item['session_id'] = row.session_id
        assert services['sessions'].ingest_events([item])['rejected'] == []
    if scenario != 'missing-targets':
        app.add_url_rule('/api/machines/<machine>/task-targets', 'fixture_targets',
            lambda machine: jsonify(services['tasks'].targets(machine)))

    @app.post('/_fixture/native-event')
    def native_event():
        item = event(7 if scenario == 'long-message' else 6, 'assistant_message',
                     {'text':'Live native update', 'is_complete':True})
        item['session_id'] = row.session_id
        return jsonify(services['sessions'].ingest_events([item]))

    @app.post('/_fixture/bind-task')
    def bind_task():
        task = services['runner'].poll('m1','frontend-acceptance')['task']
        item = event(1, 'session_start', {'agent_family':'codex','adapter':'hooks','native_resume':True})
        suffix = task['task_id'].replace('-', '_')
        item.update(session_id='runner_fixture_' + suffix, stream_id='stream_' + suffix,
                    event_id='start_' + suffix, attempt_id=task['attempt_id'])
        assert services['sessions'].ingest_events([item])['rejected'] == []
        services['runner'].result('m1', task['attempt_id'], task['nonce'], 0, log_summary='fixture execution complete')
        return jsonify({'session_id':item['session_id']})

    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        result = subprocess.run(['node', str(ROOT / 'tests/fixtures/frontend/native_chat_browser.cjs'),
            f'http://127.0.0.1:{server.server_port}', scenario, row.session_id], capture_output=True, text=True, timeout=100)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.shutdown(); server.server_close(); thread.join(5)
