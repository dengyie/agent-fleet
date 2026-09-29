import json
import sys
from tools.adapters.base import BaseAdapter
from tools.session.bridge import SessionBridge


def test_adapter_delivers_complete_line_before_summary_truncation(tmp_path):
    lines = []
    adapter = BaseAdapter(command=[sys.executable, '-c', "print('x'*6000)"])
    result = adapter.run('', tmp_path, on_line=lines.append)
    assert result.exit_code == 0
    assert lines[0] == 'x' * 6000
    assert len(result.log_tail) <= 10240


def test_codex_exec_item_stream_preserves_reply_and_command(tmp_path):
    bridge = SessionBridge({'agent': 'codex', 'structured_stream': True}, {
        'session_id': 'session_test', 'machine_id': 'pxed',
        'spool_root': tmp_path, 'spool_key': b'x' * 32})
    bridge.ingest_structured([json.dumps({'type': 'item.completed', 'item': {
        'id': 'i1', 'type': 'agent_message', 'text': 'reply' * 1000}})])
    bridge.ingest_structured([json.dumps({'type': 'item.completed', 'item': {
        'id': 'i2', 'type': 'command_execution', 'command': 'python3 -m unittest',
        'aggregated_output': 'ok' * 2000, 'exit_code': 0, 'status': 'completed'}})])
    rows = bridge.spool.read_after(0, 100, 262144)
    assert any(r['kind'] == 'assistant_message' and r['payload']['text'] == 'reply' * 1000 for r in rows)
    assert any(r['kind'] == 'tool_result' and r['payload']['result'] == 'ok' * 2000 for r in rows)
    bridge.close()


def test_pending_conversation_replays_after_restart_and_large_source_is_complete(tmp_path):
    from tools.runner_config import RunnerConfig
    from tools.session.runner_capture import RunnerCapture, replay_pending
    from hub.bootstrap import create_app
    from hub.config import FleetConfig
    config = FleetConfig.from_root(tmp_path / 'hub', dev_operator='test@local',
        session_repositories_enabled=True, runner_credentials={'pxed': 'secret'})
    app = create_app(config)
    client = app.test_client()
    online = False
    def post(cfg, path, body):
        if not online:
            return 0, {}
        response = client.post(path, json=body, headers={'X-Runner-Credential': 'pxed:secret'})
        return response.status_code, response.get_json()
    cfg = RunnerConfig(hub='http://localhost', machine='pxed', credential='secret',
        runner_id='pxed', projects={}, agents={}, cache_dir=tmp_path / 'cache')
    task = {'task_id': 'task1', 'attempt_id': 'attempt1', 'agent_type': 'codex', 'instruction': 'hello'}
    capture = RunnerCapture(cfg, task, post)
    source = json.dumps({'type': 'turn.completed', 'usage': {'output_tokens': 10}, 'text': '漢🌲' * 30000}, ensure_ascii=False)
    capture.line(source)
    capture.finish(0)
    online = True
    replay_pending(cfg, post)
    session_id = capture.session_id
    response = client.get('/api/sessions/' + session_id + '/events?limit=1000').get_json()
    records = [e['payload']['text'] for e in response['events'] if e['kind'] == 'source_record']
    assert source in ''.join(records)
    before = response['events']
    replay_pending(cfg, post)
    assert client.get('/api/sessions/' + session_id + '/events?limit=1000').get_json()['events'] == before
    assert any(e['kind'] == 'user_message' and e['payload']['text'] == 'hello' for e in before)


def test_runner_cannot_upload_another_machine(tmp_path):
    from hub.bootstrap import create_app
    from hub.config import FleetConfig
    client = create_app(FleetConfig.from_root(tmp_path, session_repositories_enabled=True,
        runner_credentials={'pxed': 'secret'})).test_client()
    response = client.post('/api/runner-session-events', json=[{'machine_id': 'other'}],
        headers={'X-Runner-Credential': 'pxed:secret'})
    assert response.status_code == 403


def test_codex_resume_uses_actual_noninteractive_subcommand():
    from tools.supervisor.supervisor import _native_resume_argv
    assert _native_resume_argv('codex', '/usr/local/bin/codex', 'thread1', 'continue') == [
        '/usr/local/bin/codex', 'exec', 'resume', '--json', 'thread1', 'continue']


def test_native_history_and_new_turn_sync_without_reuploading_old_records(tmp_path):
    from tools.runner_config import RunnerConfig
    from tools.session.sync import sync_file
    from hub.bootstrap import create_app
    from hub.config import FleetConfig
    client = create_app(FleetConfig.from_root(tmp_path / 'hub', dev_operator='test@local',
        session_repositories_enabled=True, runner_credentials={'pxed':'secret'})).test_client()
    def send(cfg, path, body):
        response = client.post(path, json=body, headers={'X-Runner-Credential': 'pxed:secret'})
        return response.status_code, response.get_json()
    cfg = RunnerConfig(hub='http://localhost', machine='pxed', credential='secret',
        runner_id='pxed', projects={}, agents={}, cache_dir=tmp_path/'cache')
    native = tmp_path/'native.jsonl'
    native.write_text(json.dumps({'type':'event_msg', 'payload':{'type':'user_message','message':'first'}})+'\n')
    assert sync_file(cfg, 'codex', native, send) == 1
    assert sync_file(cfg, 'codex', native, send) == 0
    with native.open('a') as f:
        f.write(json.dumps({'type':'event_msg', 'payload':{'type':'agent_message','message':'second'}})+'\n')
    assert sync_file(cfg, 'codex', native, send) == 1
    sid=client.get('/api/sessions').get_json()['sessions'][0]['session_id']
    events=client.get('/api/sessions/'+sid+'/events').get_json()['events']
    messages=[e['payload']['text'] for e in events if e['kind'] in ('user_message','assistant_message')]
    assert messages == ['first','second']


def test_regular_session_keeps_attempt_binding_without_claiming_control():
    from hub.domain.session import validate_session_spec
    session=validate_session_spec({'session_id':'s1','machine_id':'pxed','attempt_id':'a1',
        'managed':False,'capture_quality':'best_effort'})
    assert session['attempt_id']=='a1'
    assert session['managed'] is False


def test_managed_session_message_queues_for_its_machine_without_adoption(tmp_path):
    from hub.bootstrap import create_app
    from hub.config import FleetConfig
    app=create_app(FleetConfig.from_root(tmp_path, dev_operator='test@local',
        session_repositories_enabled=True, supervisor_enabled=True,
        supervisor_signing_raw=b'x'*32, append_user_turn_enabled=True,
        supervisor_credentials={'pxed':'secret'}))
    sessions=app.extensions['fleet']['services']['sessions']
    sessions.session_repo.upsert_session({'session_id':'s1','machine_id':'pxed',
        'process_group_id':'g1','managed':True,'capture_quality':'structured'})
    client=app.test_client()
    response=client.post('/api/sessions/s1/messages',json={'text':'continue'})
    assert response.status_code==202,response.get_json()
    polled=client.post('/api/supervisor/poll',json={},headers={'X-Supervisor-Credential':'pxed:secret'})
    commands=polled.get_json()['commands']
    assert commands[0]['action']=='append_user_turn'
    assert commands[0]['payload']['text']=='continue'


def test_source_archive_preserves_values_rewritten_in_preview(tmp_path):
    from tools.runner_config import RunnerConfig
    from tools.session.runner_capture import RunnerCapture
    from hub.bootstrap import create_app
    from hub.config import FleetConfig
    client=create_app(FleetConfig.from_root(tmp_path/'hub',dev_operator='test@local',
        session_repositories_enabled=True,runner_credentials={'pxed':'secret'})).test_client()
    def send(cfg,path,body):
        r=client.post(path,json=body,headers={'X-Runner-Credential':'pxed:secret'})
        return r.status_code,r.get_json()
    cfg=RunnerConfig(hub='http://localhost',machine='pxed',credential='secret',runner_id='pxed',
        projects={},agents={},cache_dir=tmp_path/'cache')
    capture=RunnerCapture(cfg,{'attempt_id':'archive','agent_type':'codex','task_id':'task'},send)
    source={'type':'item.completed','item':{'type':'agent_message','text':'Inspect /home/example/private/project.py; password=fixture-only-value'}}
    capture.line(json.dumps(source));capture.finish(0)
    rows=client.get('/api/sessions/'+capture.session_id+'/events?limit=100').get_json()['events']
    texts=[e['payload']['text'] for e in rows if e['kind']=='source_record']
    assert json.dumps(source) in texts


def test_natural_completion_retains_native_identity_and_distinct_reason(tmp_path):
    from tools.supervisor.supervisor import Supervisor
    sv=Supervisor(manifest_dir=tmp_path/'manifests',machine_id='pxed')
    m=sv.launch(agent='codex',command=[sys.executable,'-c',
        'print(\'{"type":"thread.started","thread_id":"native-thread"}\')'],
        cwd=str(tmp_path),env_allowlist={},capability_manifest={'resume':True})
    result=sv.wait_session(m.session_id,timeout_s=10)
    assert result.exit_code==0
    assert sv.get(m.session_id).reason=='completed'
    assert sv._entries[m.session_id].resume_token=='native-thread'


def test_resume_identity_accepts_empty_environment_allowlist(tmp_path):
    from types import SimpleNamespace
    from tools.supervisor.supervisor import _native_resume_identity
    entry=SimpleNamespace(cwd=str(tmp_path),env={},resume_token='native',exe_path='/usr/local/bin/codex',handle=None)
    assert _native_resume_identity(entry)==(str(tmp_path),{},'native','/usr/local/bin/codex')


def test_followup_receipt_does_not_claim_terminated_is_success():
    from types import SimpleNamespace
    from tools.supervisor.control_client import ControlClient
    client=ControlClient(hub_url='http://localhost',machine_id='pxed',credential='test',
        hub_public_key=b'x'*32,public_supervisor=SimpleNamespace(append_user_turn=lambda *args:'terminated'))
    status,reason=client._execute_payload('append_user_turn','c','s',{'text':'hello'})
    assert status!='succeeded'


def test_task_runner_result_links_to_uploaded_conversation(tmp_path, monkeypatch):
    import importlib.util
    import subprocess
    from pathlib import Path
    from hub.bootstrap import create_app
    from hub.config import FleetConfig
    from tools.runner_config import RunnerConfig
    project=tmp_path/'project';project.mkdir()
    subprocess.run(['git','init','-q',str(project)],check=True)
    subprocess.run(['git','-C',str(project),'-c','user.name=test','-c','user.email=test@local',
                    'commit','-q','--allow-empty','-m','initial'],check=True)
    app=create_app(FleetConfig.from_root(tmp_path/'hub',dev_operator='test@local',
        ingest_token='ingest',session_repositories_enabled=True,
        runner_credentials={'pxed':'secret'},project_whitelist={'pxed':['demo']}))
    client=app.test_client()
    client.post('/api/ingest',json={'machine':'pxed','agents':{'codex':{'installed':True}},
        'system':{'platform':'linux'}},headers={'X-Agent-Fleet-Token':'ingest'})
    created=client.post('/api/tasks',json={'machine':'pxed','agent_type':'codex','project':'demo',
        'instruction':'return a reply','client_token':'test-conversation'}).get_json()
    tid=created['task']['task_id']
    spec=importlib.util.spec_from_file_location('runner_capture_integration',Path(__file__).parents[1]/'tools/agent-runner.py')
    runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
    def send(cfg,path,body,timeout=15):
        r=app.test_client().post(path,json=body,headers={'X-Runner-Credential':'pxed:secret'})
        return r.status_code,r.get_json()
    monkeypatch.setattr(runner,'post_json',send)
    output=json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'complete reply'}})
    cfg=RunnerConfig(hub='http://localhost',machine='pxed',credential='secret',runner_id='pxed',
        projects={'demo':project},agents={'codex':{'command':[sys.executable,'-c','print('+repr(output)+')']}},
        cache_dir=tmp_path/'cache')
    assert runner.poll_once(cfg)
    task=client.get('/api/tasks/'+tid).get_json()['task']
    assert task['state']=='succeeded'
    sid=task['session_id']
    events=client.get('/api/sessions/'+sid+'/events').get_json()['events']
    assert any(e['kind']=='assistant_message' and e['payload']['text']=='complete reply' for e in events)


def test_current_codex_native_item_completed_messages(tmp_path):
    bridge=SessionBridge({'agent':'codex','native_transcript':True},{'session_id':'native','machine_id':'pxed',
        'spool_root':tmp_path,'spool_key':b'x'*32})
    for kind,content_kind,text in [('UserMessage','text','question'),('AgentMessage','Text','answer')]:
        bridge.ingest_one({'type':'event_msg','payload':{'type':'item_completed',
            'item':{'type':kind,'content':[{'type':content_kind,'text':text}]}}})
    rows=bridge.spool.read_after(0,10,262144)
    assert [(e['kind'],e['payload']['text']) for e in rows]==[('user_message','question'),('assistant_message','answer')]
    bridge.close()


def test_complete_native_capture_does_not_skip_large_jsonl_record(tmp_path):
    native=tmp_path/'large.jsonl'
    value={'type':'attachment','content':'x'*300000}
    native.write_text(json.dumps(value)+'\n')
    bridge=SessionBridge({'agent':'codex','native_transcript':True},{'session_id':'large','machine_id':'pxed',
        'native_file_path':str(native),'preserve_source':True,'spool_root':tmp_path/'spool','spool_key':b'x'*32})
    bridge.ingest_native()
    rows=bridge.spool.read_after(0,1000,2<<20)
    text=''.join(e['payload']['text'] for e in rows if e['kind']=='source_record')
    assert json.loads(text)==value
    bridge.close()
