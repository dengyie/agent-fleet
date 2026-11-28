"""Native web UI against real Hub/SQLite and signed queue; no vendor execution.

The target discovery fixture exposes TaskService.targets at the proposed thin
HTTP path. Production routing stays exclusively with the backend workstream.
"""
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from threading import Thread

import pytest
from flask import jsonify, request
from werkzeug.serving import make_server

from hub.bootstrap import create_app
from hub.config import FleetConfig
from tests.test_session_access_consistency import adopted, OWNER
from tests.test_session_interactions import event, question

ROOT = Path(__file__).resolve().parents[1]


def test_native_stream_interruption_projection():
    result = subprocess.run(['node', '--input-type=module'], cwd=ROOT, text=True, capture_output=True, input=r"""
import assert from 'node:assert/strict';
import {nativeMessages, nativeTranscript} from './frontend/assets/views/agent-chat/state.js';
import {operationFailure} from './frontend/assets/views/agent-chat/state.js';
import {errorText} from './frontend/assets/views/agent-chat/dom.js';
assert.match(operationFailure('escape_unverified'), /尚未确认原 Agent 进程已完全退出/);
assert.match(operationFailure('liveness_unknown'), /无法确认原 Agent 进程状态/);
assert.equal(operationFailure('new_reason'),'new_reason');
assert.equal(operationFailure('toString'),'toString');
assert.equal(operationFailure(null),'');
assert.match(errorText({code:'escape_unverified',detail:'server detail'}), /escape_unverified/);
assert.equal(errorText({code:'new_reason',detail:'original detail'}),'original detail');
const e = (cursor, stream, kind, payload) => ({cursor, stream_id:stream, kind, payload});
const events = [
 e('1','a','assistant_message',{message_id:'answer',text:'partial 🌲',is_complete:false}),
 e('2','a','tool_call',{call_id:'read',tool_name:'Read',arguments:'{"path":"README.md"}'}),
 e('3','b','assistant_message',{message_id:'other',text:'independent',is_complete:false}),
 e('4','b','tool_call',{call_id:'read',tool_name:'Read'}),
 e('5','a','agent_state_changed',{state:'failed'}),
];
const rows = nativeMessages(events);
assert.equal(rows[0].complete,true,'explicit failure must stop the incomplete answer spinner');
assert.equal(rows[0].interrupted,true);
assert.equal(rows[0].text,'partial 🌲');
assert.equal(rows[1].status,'unknown','missing tool result must never be claimed successful or still running');
assert.equal(rows[2].complete,false,'another capture stream must remain live');
assert.equal(rows[3].status,'running');
const late = nativeMessages([...events,e('6','a','tool_result',{call_id:'read',result:'late result',status:'ok'})]);
assert.equal(late[1].status,'success'); assert.equal(late[1].output,'late result');
for (const state of ['stopped','completed']) {
 const ended = nativeMessages([...events.slice(0,4),e('5','a','agent_state_changed',{state})]);
 assert.equal(ended[0].complete,true);
 assert.equal(ended[0].interrupted,state === 'stopped');
 assert.equal(ended[1].status,'unknown');
}
const unknown = nativeMessages([...events.slice(0,4),e('5','a','agent_state_changed',{state:'unknown'})]);
assert.equal(unknown[0].complete,false,'unconfirmed state is not a terminal signal');
const retry = nativeMessages([...events,e('6','a','assistant_message',{message_id:'next',text:'retry output',is_complete:false})]);
assert.equal(retry[0].complete,true); assert.equal(retry.at(-1).complete,false);
const closed = nativeMessages([...events.slice(0,4),e('5','a','session_close',{reason:'failed',exit_code:125})]);
assert.equal(closed[0].complete,true); assert.equal(closed[0].interrupted,true);
const trace = nativeTranscript([
 e('7','a','session_start',{agent_family:'codex',adapter:'structured'}),
 e('8','a','agent_state_changed',{state:'running',source:'structured_stream',reason:'user_prompt_submitted'}),
 e('9','a','step_start',{step_name:'检查工作区'}),
 e('10','a','step_end',{step_name:'检查工作区',status:'completed'}),
]);
const thinking = trace.find(row => row.kind === 'thinking');
assert.ok(thinking, 'public execution trace must be visible as a thinking block');
assert.match(thinking.content, /会话开始/); assert.match(thinking.content, /检查工作区/);
assert.equal(thinking.active, false, 'completed public steps stop the thinking indicator');
const separated = nativeTranscript([
 e('1','a','agent_state_changed',{state:'running'}),
 e('2','a','assistant_message',{text:'公开工作说明',is_complete:true}),
 e('3','a','step_start',{step_name:'读取文件'}),
 e('4','b','agent_state_changed',{state:'running'}),
 e('5','a','step_end',{step_name:'读取文件',status:'completed'}),
]);
assert.deepEqual(separated.map(row => row.key),['1','2','3','4','5'],'activity grouping preserves chronology and stream boundaries');
assert.equal(separated[0].active,false,'a later public message ends the earlier running display');
assert.equal(separated[2].active,false,'later same-stream activity ends older running display');
assert.equal(separated[3].active,true,'independent stream stays active');
assert.match(separated[1].text,/公开工作说明/);
""")
    assert result.returncode == 0, result.stdout + result.stderr


def test_native_chat_state_and_contracts():
    result = subprocess.run(['node', '--input-type=module'], cwd=ROOT, text=True, capture_output=True, input=r"""
import assert from 'node:assert/strict';
import {mergeNativeEvents, nativeMessages, nativeCaptureNotice, sessionTitle, agentName, uncertainWrite, operationStatus} from './frontend/assets/views/agent-chat/state.js';
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
assert.equal(agentName('agy'), 'Google Antigravity');
const desktop = {agent_family:'zcode',agent_evidence:{protocol:{name:'desktop_bridge',state:'observed'}}};
assert.match(nativeCaptureNotice(desktop), /空闲后同步正文与工具记录/);
assert.equal(nativeCaptureNotice({...desktop,current_state:'running'}), nativeCaptureNotice(desktop));
assert.equal(nativeCaptureNotice({...desktop,agent_family:'codex'}), '');
assert.equal(nativeCaptureNotice({...desktop,agent_evidence:{protocol:{name:'native_transcript',state:'observed'}}}), '');
assert.equal(nativeCaptureNotice({...desktop,agent_evidence:{protocol:{name:'desktop_bridge',state:'unknown'}}}), '');
assert.equal(nativeCaptureNotice({agent_family:'zcode'}), '');
assert.equal(nativeCaptureNotice(null), '');
const targets = parseTaskTargets({machine:'m1', projects:['fleet'], agents:{codex:{installed:null, credential:'hidden'}}, internal:'hidden'});
assert.equal(targets.agents.codex.installed, null); assert.equal(targets.agents.codex.credential, undefined);
assert.throws(() => parseTaskTargets({machine:'m1',projects:[],agents:{codex:{installed:'yes'}}}));
const denied = parseSession({session:{session_id:'adopt_a',machine_id:'m1',managed:true,adoption_status:'revoked',control_capability:'available',allowed_actions:['append_user_turn'],message_delivery:{available:true,reason:'node_receipt_required',mechanism:'native_session_resume'}}});
assert.deepEqual(denied.session.allowed_actions, []); assert.equal(denied.session.message_delivery.available, false);
assert.equal(uncertainWrite({status:503}), true); assert.equal(uncertainWrite({status:409}), false);
assert.equal(operationStatus({action:'append_user_turn',status:'succeeded'}),'已交付到原会话');
assert.equal(operationStatus({action:'apply_local_profile',status:'succeeded'}),'节点已执行');
assert.equal(operationStatus({action:'terminate_session',status:'queued'}),'已入队');
const positions = [], includeSources = [];
const api = {getSession: async id => ({session:{session_id:id}}), getSessionEvents: async (id, options) => {positions.push(options); includeSources.push(options.includeSource); return {events:[]};}};
const service = createNativeChatService(api);
await service.readSession('s', {position:{tail:true}});
await service.readSession('s', {position:{beforeCursor:'20'}});
await service.readSession('s', {position:{afterCursor:'30'}});
assert.deepEqual(positions.map(({tail,beforeCursor,afterCursor}) => [!!tail,beforeCursor || null,afterCursor || null]),
  [[true,null,null],[false,'20',null],[false,null,'30']], 'tail/history/live cursors stay independent');
assert.deepEqual(includeSources, [false,false,false], 'native chat never downloads archived source records');
assert.equal((await service.readSession('s')).authorityVerified,true);
const partial = await createNativeChatService({...api, getSession:async () => {throw new Error('authority unavailable');}}).readSession('s');
assert.equal(partial.authorityVerified,false);
assert.ok(partial.page,'transcript and authority failures remain independent');
const aborter = new AbortController(), resultSignals = [];
let binding = 's', resultReads = 0;
const resultService = createNativeChatService({
  getTask: async (id, options) => {resultSignals.push(options.signal);return {task:{task_id:id, session_id:binding,result:{has_diff_patch:true}}};},
  getTaskDiff: async (id, options) => {resultReads++;resultSignals.push(options.signal);return {task_id:id,diff_patch:'<script>literal</script>'};},
  listTaskFiles: async (id, options) => {resultReads++;resultSignals.push(options.signal);return {task_id:id,files:[]};},
  getTaskFile: async (id, path, options) => {resultSignals.push(options.signal);return {path,content:'literal code'};},
});
assert.equal((await resultService.readResults('t','s',aborter.signal)).diff.diff_patch,'<script>literal</script>');
assert.equal((await resultService.readResultFile('t','s','a.py',aborter.signal)).content,'literal code');
assert.ok(resultSignals.every(signal => signal === aborter.signal));
binding = 'other';
await assert.rejects(resultService.readResults('t','s',aborter.signal),/不一致/);
await assert.rejects(resultService.readResultFile('t','s','a.py',aborter.signal),/不一致/);
assert.equal(resultReads,2,'mismatched session cannot read snapshots');
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


@pytest.mark.parametrize('scenario', ['read-send', 'lost-response', 'reload-recovery', 'directory', 'directory-pagination', 'directory-capability-backfill', 'history-pagination', 'revoked', 'question-draft', 'missing-targets', 'create', 'task-recovery', 'long-message', 'snapshot-revision', 'receipt-failure', 'delivery-disclosure', 'refresh-targets', 'source-record', 'native-metadata', 'directory-metadata', 'local-profile', 'stale-authority', 'target-links', 'control-confirmation', 'profile-revocation', 'offline-target', 'results', 'result-file-concurrency', 'task-results-binding', 'continuation-results', 'zcode-desktop-tools', 'native-stream-interruption', 'cleanup-receipt'])
def test_native_chat_browser(tmp_path, scenario):
    if not os.environ.get('FLEET_PLAYWRIGHT_MODULE'):
        pytest.skip('Set FLEET_PLAYWRIGHT_MODULE for native web acceptance')
    app = create_app(FleetConfig.from_root(tmp_path, dev_operator=OWNER,
        frontend_dir=ROOT / 'frontend', platform_enabled=True, platform_worker_enabled=True,
        session_repositories_enabled=True, adoption_repositories_enabled=True,
        project_whitelist={'m1': ['agent-fleet']}, supervisor_enabled=True,
        supervisor_signing_raw=bytes(range(32)), supervisor_credentials={'m1':'browser-receipt'},
        append_user_turn_enabled=True, apply_local_profile_enabled=True))
    row = adopted(app, family='zcode' if scenario == 'zcode-desktop-tools' else 'codex', captured=False)
    services = app.extensions['fleet']['services']
    services['observe'].ingest({'machine':'m1', 'reachable':True, 'agents':{'codex':{'installed':True}}})
    result_task = None
    if scenario in {'results', 'result-file-concurrency', 'task-results-binding', 'continuation-results'}:
        services['tasks'].create({'machine':'m1', 'project':'agent-fleet', 'agent_type':'codex',
                                  'instruction':'retained result fixture'}, OWNER)
        result_task = services['runner'].poll('m1', 'results-fixture')['task']
        row = SimpleNamespace(session_id='runner_results_fixture')
    initial_events = [(1, 'session_start', {'agent_family':'codex','adapter':'hooks','interactive_questions':True}),
            (2, 'user_message', {'text':'Inspect native project', 'is_complete':True}),
            (3, 'assistant_message', {'text':'Native answer from persisted transcript', 'is_complete':True}),
            (4, 'tool_call', {'tool_name':'read_file','call_id':'call_1','arguments':'{"path":"README.md"}'}),
            (5, 'tool_result', {'tool_name':'read_file','call_id':'call_1','result':'README contents','status':'success'})]
    desktop_tools = []
    if scenario == 'zcode-desktop-tools':
        import json
        import sqlite3
        from tools.session.native_storage import read_events

        database = tmp_path / 'zcode.sqlite'
        with sqlite3.connect(database) as connection:
            connection.execute('CREATE TABLE message(id TEXT, session_id TEXT, data TEXT)')
            connection.execute('CREATE TABLE part(message_id TEXT, session_id TEXT, data TEXT)')
            connection.execute('INSERT INTO message VALUES (?, ?, ?)',
                ('msg_1', 'native_1', json.dumps({'role':'assistant'})))
            for part in [
                {'type':'reasoning','text':'PRIVATE_REASONING_SENTINEL'},
                {'type':'tool','tool':'Read','callID':'read_1','state':{'status':'completed',
                    'input':{'file_path':'README.md'},'output':'README <script>literal</script>'}},
                {'type':'tool','tool':'Bash','callID':'bash_1','state':{'status':'error',
                    'input':{'command':'npm test'},'error':'Tests failed <img src=x onerror=alert(1)>'}},
            ]:
                connection.execute('INSERT INTO part VALUES (?, ?, ?)',
                    ('msg_1', 'native_1', json.dumps(part)))
        desktop_tools, _ = read_events('zcode', database, 'native_1', 0, limit=256)
        assert len(desktop_tools) == 4
        initial_events = [
            (1, 'session_start', {'agent_family':'zcode','adapter':'structured_stream'}),
            (2, 'session_metadata', {'desktop_bridge':True}),
            (3, 'user_message', {'text':'Inspect native project','is_complete':True}),
            (4, 'assistant_message', {'text':'Native answer from persisted transcript','is_complete':True}),
            *[(index + 5, item['kind'], item['payload']) for index, item in enumerate(desktop_tools[:2])],
        ]
    for seq, kind, payload in initial_events:
        item = event(seq, kind, payload); item['session_id'] = row.session_id
        if result_task:
            item['attempt_id'] = result_task['attempt_id']
        assert services['sessions'].ingest_events([item])['rejected'] == []
    if result_task:
        item = event(6, 'session_metadata', {'task_id':result_task['task_id']})
        item.update(session_id=row.session_id, attempt_id=result_task['attempt_id'])
        assert services['sessions'].ingest_events([item])['rejected'] == []
        services['runner'].result('m1', result_task['attempt_id'], result_task['nonce'], 0,
            log_summary='snapshot complete', diff_stat='1 file changed',
            diff_patch='diff --git a/test.py b/test.py\n+print("<img src=x onerror=alert(1)>")\n',
            files=[{'path':'test.py', 'content':'print("<img src=x onerror=alert(1)>")\n'},
                   {'path':'other.py', 'content':'second retained snapshot\n'}],
            test_summary={'framework':'pytest','passed':12,'failed':0,'skipped':1,'errors':0,'duration_s':1.2})
        if scenario == 'continuation-results':
            from tests.test_continuation_results import result as turn_result
            for n in range(23):
                services['continuation_results'].repository.put('m1', turn_result(
                    command_id=f'cmd_turn_{n:02}', session_id=row.session_id, task_id=result_task['task_id'],
                    diff_patch='+<img src=x onerror=alert(1)> TURN_DETAIL\n',
                    test_summary={'framework':'pytest', 'passed':n, 'failed':0}))
    if scenario in {'source-record', 'native-metadata', 'directory-metadata'}:
        kind = 'source_record' if scenario == 'source-record' else 'session_metadata'
        payload = {'text':'PRIVATE_REASONING_SENTINEL'} if scenario == 'source-record' else {
            'title':'VM native title', 'model':'model-x', 'provider':'provider-y',
            'client_version':'client-1.2', 'native_started_at':'2026-11-21T10:00:00Z'}
        item = event(6, kind, payload); item.update(session_id=row.session_id,
            event_id='evt_' + scenario.replace('-', '_'))
        assert services['sessions'].ingest_events([item])['rejected'] == []
    if scenario in {'directory-pagination', 'directory-capability-backfill'}:
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
    if scenario in {'question-draft', 'stale-authority'}:
        item = event(6, 'interaction_request', question()); item['session_id'] = row.session_id
        assert services['sessions'].ingest_events([item])['rejected'] == []
    if scenario == 'long-message':
        item = event(6, 'assistant_message', {'text': 'x' * 33000, 'is_complete': False})
        item['session_id'] = row.session_id
        assert services['sessions'].ingest_events([item])['rejected'] == []
    if scenario == 'snapshot-revision':
        item = event(6, 'assistant_message', {'text':'OLD_SNAPSHOT_' + 'x' * 33000,
            'message_id':'revised-message', 'content_mode':'snapshot', 'is_complete':False})
        item['session_id'] = row.session_id
        assert services['sessions'].ingest_events([item])['rejected'] == []
    if scenario != 'missing-targets':
        app.add_url_rule('/api/machines/<machine>/task-targets', 'fixture_targets',
            lambda machine: jsonify(services['tasks'].targets(machine)))

    snapshot_sequence = 6
    stream_generation = 0

    @app.post('/_fixture/native-event')
    def native_event():
        nonlocal snapshot_sequence, stream_generation
        if scenario == 'native-stream-interruption':
            phase = request.get_json()['phase']
            if phase == 'start':
                stream_generation += 1
                rows = [('agent_state_changed', {'state':'running','source':'structured_stream','confidence':'high','reason':'explicit_state'}),
                    ('assistant_message', {'message_id':'answer','content_mode':'delta','text':f'LIVE_{stream_generation} 🌲','is_complete':False}),
                    ('tool_call', {'call_id':'live_tool','tool_name':f'Read {stream_generation}','arguments':'{"path":"README.md"}'})]
            elif phase == 'delta':
                rows = [('assistant_message', {'message_id':'answer','content_mode':'delta','text':' incremental','is_complete':False})]
            elif phase == 'fail':
                rows = [('agent_state_changed', {'state':'failed','source':'structured_stream','confidence':'high','reason':'source_unavailable'})]
            elif phase == 'late-result':
                rows = [('tool_result', {'call_id':'live_tool','tool_name':f'Read {stream_generation}','result':'late <img src=x onerror=alert(1)>','status':'ok'})]
            else:
                return jsonify({'error':'invalid_phase'}), 400
            items = []
            for kind, payload in rows:
                snapshot_sequence += 1
                item = event(snapshot_sequence, kind, payload)
                item.update(session_id=row.session_id, stream_id=f'live_{stream_generation}', event_id=f'live_{snapshot_sequence}')
                items.append(item)
            return jsonify(services['sessions'].ingest_events(items))
        if scenario == 'zcode-desktop-tools':
            items = []
            for seq, tool in enumerate(desktop_tools[2:], start=7):
                item = event(seq, tool['kind'], tool['payload'])
                item.update(session_id=row.session_id, event_id=tool['event_id'])
                items.append(item)
            return jsonify(services['sessions'].ingest_events(items))
        payload = {'text':'Live native update', 'is_complete':True}
        if scenario == 'snapshot-revision':
            payload.update(message_id='revised-message', content_mode='snapshot')
            if (request.get_json(silent=True) or {}).get('reset_snapshot'):
                payload.update(text='OLD_SNAPSHOT_' + 'x' * 33000, is_complete=False)
        item = event(7 if scenario in {'long-message', 'snapshot-revision'} else 6, 'assistant_message', payload)
        if scenario == 'snapshot-revision':
            snapshot_sequence += 1
            item.update(sequence=snapshot_sequence, event_id='snapshot_' + str(snapshot_sequence))
        item['session_id'] = row.session_id
        return jsonify(services['sessions'].ingest_events([item]))

    @app.post('/_fixture/fail-command')
    def fail_command():
        commands = services['supervisor'].poll('m1')
        for command in commands:
            services['supervisor'].receipt(command['command_id'], 'failed', 'escape_unverified' if scenario == 'cleanup-receipt' else 'native_session_unbound')
        return jsonify({'commands':len(commands)})

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
            f'http://127.0.0.1:{server.server_port}', scenario, row.session_id,
            result_task['task_id'] if result_task else ''], capture_output=True, text=True, timeout=100)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.shutdown(); server.server_close(); thread.join(5)
