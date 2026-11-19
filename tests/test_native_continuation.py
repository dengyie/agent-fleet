"""Regressions from real production follow-up rejections on 2026-11-19."""
from tools.supervisor.model import ManagedSessionManifest
from agent_profiles import default_command


def test_completed_reason_survives_cross_process_manifest_read():
    original = ManagedSessionManifest(session_id='session', machine_id='mac',
        process_group_id='group', state='terminated', reason='completed')
    assert ManagedSessionManifest.from_json(original.to_json()).reason == 'completed'


def test_default_agents_keep_native_sessions_and_provider_config():
    assert not {'--ephemeral', '--ignore-user-config'} & set(default_command('codex'))
    claude = default_command('claude_code')
    assert '--no-session-persistence' not in claude
    assert '--output-format' in claude and 'stream-json' in claude


def test_completed_session_identity_survives_a_new_supervisor(tmp_path):
    import sys
    from tools.supervisor.supervisor import Supervisor
    root = tmp_path / 'manifests'
    first = Supervisor(root, 'mac')
    m = first.launch(agent='codex', command=[sys.executable, '-c',
        'print(\'{"type":"thread.started","thread_id":"native-test-thread"}\')'],
        cwd=str(tmp_path), env_allowlist={})
    assert first.wait_session(m.session_id, timeout_s=5).exit_code == 0
    second = Supervisor(root, 'mac')
    assert second.get(m.session_id).reason == 'completed'
    assert second.continuation_ready(m.session_id)
    entry = second._entries[m.session_id]
    assert entry.resume_token == 'native-test-thread'
    assert entry.cwd == str(tmp_path)
    assert entry.manifest.capability_manifest['resume'] is True
    private = root / 'continuations' / (m.session_id + '.json')
    assert private.stat().st_mode & 0o077 == 0
    assert 'native-test-thread' not in (root / (m.session_id + '.json')).read_text()


def test_claude_stream_identity_persists_but_ephemeral_codex_does_not(tmp_path):
    import sys
    from tools.supervisor.supervisor import Supervisor
    sv = Supervisor(tmp_path/'m', 'mac')
    m = sv.launch(agent='claude_code', command=[sys.executable, '-c',
        'print(\'{"type":"system","subtype":"init","session_id":"native-claude"}\')'],
        cwd=str(tmp_path), env_allowlist={})
    sv.wait_session(m.session_id, timeout_s=5)
    assert sv.continuation_ready(m.session_id)
    assert sv._entries[m.session_id].resume_token == 'native-claude'
    n = sv.launch(agent='codex', command=[sys.executable, '-c',
        'print(\'{"type":"thread.started","thread_id":"ephemeral"}\')', '--ephemeral'],
        cwd=str(tmp_path), env_allowlist={})
    sv.wait_session(n.session_id, timeout_s=5)
    assert not sv.continuation_ready(n.session_id)


def test_followup_cross_instance_uses_original_stream_and_can_repeat(tmp_path, monkeypatch):
    import json
    import sys
    import time
    from tools.runner_config import RunnerConfig
    from tools.session.runner_capture import RunnerCapture
    from tools.supervisor.supervisor import Supervisor
    from tools.supervisor.continuation_capture import ContinuationCapture
    from tools.supervisor import native_resume
    from tools.supervisor import continuation_capture
    from hub.bootstrap import create_app
    from hub.config import FleetConfig
    app = create_app(FleetConfig.from_root(tmp_path/'hub', dev_operator='test@local',
        session_repositories_enabled=True, runner_credentials={'mac':'secret'}))
    client = app.test_client()
    def post(cfg, path, events):
        response = client.post(path, json=events, headers={'X-Runner-Credential':'mac:secret'})
        assert response.status_code == 200
        assert not response.get_json().get('rejected')
        return response.status_code, response.get_json()
    cfg = RunnerConfig(hub='https://localhost', machine='mac', credential='secret',
        runner_id='mac', projects={}, agents={}, cache_dir=tmp_path/'cache')
    task = {'attempt_id':'turn1','task_id':'task1','agent_type':'codex','instruction':'first'}
    sv = Supervisor(tmp_path/'manifests', 'mac')
    from tools.session.runner_capture import session_prefix
    sid = session_prefix('mac') + 'turn1'
    script = tmp_path / 'native_fixture.py'
    script.write_text('print(\'{"type":"thread.started","thread_id":"same-native-id"}\')', encoding='utf-8')
    m = sv.launch(agent='codex', command=[sys.executable, str(script)],
        cwd=str(tmp_path),env_allowlist={}, session_id=sid,attempt_id='turn1')
    cap = RunnerCapture(cfg, task, post, process_group_id=m.process_group_id)
    sv.wait_session(sid, timeout_s=5, on_line=cap.line)
    cap.finish(0, resumable=sv.continuation_ready(sid))
    assert cap.record.exists() and not json.loads(cap.record.read_text()).get('pruned')
    monkeypatch.setattr(continuation_capture, 'post', post)
    seen = []
    def argv(family, exe, token, text):
        seen.append(token)
        return [sys.executable, '-c', 'import json; print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":'+repr('ACK '+text)+'}}))']
    monkeypatch.setattr(native_resume, '_native_resume_argv', argv)
    for text in ['followup one', 'followup two']:
        controller = Supervisor(tmp_path/'manifests', 'mac')
        capture = ContinuationCapture(cfg, controller)
        assert capture.send(sid,text) == 'appended'
        deadline = time.monotonic()+10
        while capture.active and time.monotonic()<deadline:
            time.sleep(.02)
        assert not capture.active
        assert controller.get(sid).reason == 'completed'
    events = client.get('/api/sessions/'+sid+'/events?limit=100').get_json()['events']
    messages = [e['payload']['text'] for e in events if e['kind'] in {'user_message','assistant_message'}]
    assert messages == ['first', 'followup one', 'ACK followup one', 'followup two', 'ACK followup two']
    assert seen == ['same-native-id', 'same-native-id']
    sequences = [e['sequence'] for e in events]
    assert len(sequences) == len(set(sequences))
    assert client.get('/api/sessions/'+sid).get_json()['session']['current_state']=='completed'
    assert client.get('/api/sessions/'+sid).get_json()['session']['status']=='closed'


def test_agent_provider_environment_is_explicit_and_filtered(tmp_path):
    import json
    from tools.runner_config import load_config
    from tools.adapters import create
    (tmp_path/'credential').write_text('runner-secret')
    (tmp_path/'provider.json').write_text(json.dumps({'ANTHROPIC_AUTH_TOKEN':'provider-key',
        'ANTHROPIC_BASE_URL':'https://provider.example', 'AGENT_FLEET_TOKEN':'must-not-pass'}))
    (tmp_path/'runner.yaml').write_text(f'''hub: https://example.com
machine: mac
credential_file: {tmp_path}/credential
projects:
  test:
    path: {tmp_path}
agents:
  claude_code:
    env_file: {tmp_path}/provider.json
''')
    cfg = load_config(tmp_path/'runner.yaml')
    adapter = create('claude_code',environment=cfg.agents['claude_code']['environment'])
    env = adapter.child_env()
    assert env['ANTHROPIC_AUTH_TOKEN']=='provider-key'
    assert env['ANTHROPIC_BASE_URL']=='https://provider.example'
    assert 'AGENT_FLEET_TOKEN' not in env


def test_desktop_control_uses_native_session_and_waits_for_completion(tmp_path):
    from types import SimpleNamespace
    from tools.supervisor.desktop_control import DesktopControl
    class Bridge:
        def send(self, sid, cid, text):
            assert (sid,cid,text)==('sess_native','cmd_test','hello')
            return {'status':'accepted','receipt':{'delivery':'queued'}}
        def receipt(self, cid):
            return {'status':'accepted','observation':{'operations':[{'status':'completed'}]}}
    cfg = SimpleNamespace(supervisor_manifest_dir=tmp_path, machine='mac', managed_enabled=True)
    control = DesktopControl(cfg,client=Bridge())
    control.register('runner_test','sess_native','native_test')
    assert control.send('runner_test','cmd_test','hello')==('accepted','desktop_queued')
    restored = DesktopControl(cfg,client=Bridge())
    assert list(restored.receipts())==[('cmd_test','succeeded','desktop_completed')]


def test_lifecycle_replay_cannot_close_a_new_turn(tmp_path):
    from hub.bootstrap import create_app
    from hub.config import FleetConfig
    from tests.test_session_query_api import _event
    app = create_app(FleetConfig.from_root(tmp_path/'hub', dev_operator='test@local',
        session_repositories_enabled=True, ingest_token='secret'))
    client = app.test_client()
    def ingest(seq, kind, payload):
        evt = _event(seq, kind=kind)
        evt['payload'] = payload
        r = client.post('/api/session-events', json=[evt],
            headers={'X-Agent-Fleet-Token':'secret'})
        assert r.status_code == 200 and not r.get_json()['rejected']
    def state():
        return client.get('/api/sessions/sess_q_1').get_json()['session']['status']
    ingest(1, 'session_start', {'agent_family':'codex'})
    ingest(2, 'session_close', {'exit_code':0})
    assert state() == 'closed'
    ingest(3, 'session_metadata', {'native_resume':True, 'resumed':True})
    assert state() == 'running'
    ingest(2, 'session_close', {'exit_code':0})
    assert state() == 'running'
    ingest(4, 'session_close', {'exit_code':0})
    assert state() == 'closed'


def test_foreign_supervisor_refreshes_completed_state(tmp_path):
    import sys
    from tools.supervisor.supervisor import Supervisor
    owner = Supervisor(tmp_path/'m', 'mac')
    m = owner.launch(agent='codex', command=[sys.executable, '-c', 'print("ok")'],
        cwd=str(tmp_path), env_allowlist={})
    observer = Supervisor(tmp_path/'m', 'mac')
    assert observer.get(m.session_id).state == 'running'
    owner.wait_session(m.session_id, timeout_s=5)
    assert observer.get(m.session_id).state == 'terminated'
    assert observer.get(m.session_id).reason == 'completed'


def test_continuation_preserves_explicit_provider_options(tmp_path):
    import sys
    from tools.supervisor.supervisor import Supervisor
    sv = Supervisor(tmp_path/'m', 'mac')
    m = sv.launch(agent='codex', command=[sys.executable, '-c',
        'print(\'{"type":"thread.started","thread_id":"configured-native"}\')',
        '--model', 'custom/model', '--config=model_provider="custom"'],
        cwd=str(tmp_path), env_allowlist={})
    sv.wait_session(m.session_id, timeout_s=5)
    restored = Supervisor(tmp_path/'m', 'mac')
    restored.get(m.session_id)
    prepared = restored._prepare_user_turn(m.session_id, 'followup', restored._entries[m.session_id])
    argv = prepared[2]
    assert '--model' in argv and 'custom/model' in argv
    assert '--config=model_provider="custom"' in argv


def test_desktop_binding_pagination_and_unacknowledged_receipts_retry(tmp_path):
    from types import SimpleNamespace
    from tools.supervisor.desktop_control import DesktopControl
    class Bridge:
        def request(self, path):
            native = 'sess_second' if '?' in path else 'sess_first'
            return {'sessions':[{'session':{'sessionId':native,'sourceServerId':'local'},
                                 'availableActions':['sendToSession']}],
                    'nextCursor':None if '?' in path else 'page/2'}
        def send(self, *args):
            return {'status':'accepted'}
        def receipt(self, cid):
            return {'status':'accepted','observation':{'operations':[{'status':'completed'}]}}
    cfg = SimpleNamespace(supervisor_manifest_dir=tmp_path, machine='mac', managed_enabled=True)
    control = DesktopControl(cfg,client=Bridge())
    assert set(control.bindings()) == {'sess_first','sess_second'}
    control.register('runner_test','sess_second','native_test')
    control.send('runner_test','cmd_test','hello')
    expected = [('cmd_test','succeeded','desktop_completed')]
    assert list(control.receipts()) == expected
    assert list(control.receipts()) == expected  # Hub response lost; replay receipt, never send.
    control.acknowledge('cmd_test')
    assert list(control.receipts()) == []


def test_desktop_without_rollout_has_control_and_original_messages(tmp_path):
    from tools.session.sync import sync_desktop, native_identity
    from tools.supervisor.desktop_control import DesktopControl
    from test_review_node_sync import _cfg, _client
    cfg, client = _cfg(tmp_path), _client(tmp_path)
    cfg.supervisor_enabled = True
    cfg.supervisor_manifest_dir = tmp_path/'manifests'
    class Bridge:
        def request(self, path):
            return {'messages':[{'id':'u1','role':'user','text':'hello'},
                                {'id':'a1','role':'assistant','text':'ACK'}], 'nextCursor':None}
    desktop = DesktopControl(cfg, client=Bridge())
    def send(cfg, path, events):
        r = client.post(path,json=events,headers={'X-Runner-Credential':'pxed:secret'})
        assert r.status_code == 200 and not r.get_json().get('rejected')
        return r.status_code,r.get_json()
    path = tmp_path/'model-io-sess_desktop.jsonl'
    row = {'session':{'sessionId':'sess_desktop'}, 'status':'idle'}
    sync_desktop(cfg, desktop, row, path, send)
    sync_desktop(cfg, desktop, row, path, send)
    from tools.session.runner_capture import session_prefix
    sid = session_prefix(cfg.machine)+native_identity('zcode',path)
    info = client.get('/api/sessions/'+sid).get_json()['session']
    assert info['managed'] and info['message_delivery']['mechanism']=='zcode_desktop_bridge'
    events = client.get('/api/sessions/'+sid+'/events').get_json()['events']
    assert [e['payload']['text'] for e in events if e['kind'] in {'user_message','assistant_message'}]==['hello','ACK']


def test_codex_continuation_keeps_original_execution_policy(tmp_path):
    import sys
    from tools.supervisor.supervisor import Supervisor
    sv = Supervisor(tmp_path/'m', 'mac')
    m = sv.launch(agent='codex', command=[sys.executable, '-c',
        'print(\'{"type":"thread.started","thread_id":"native-policy"}\')', '--approve-for-me'],
        cwd=str(tmp_path), env_allowlist={})
    sv.wait_session(m.session_id, timeout_s=5)
    entry = sv._entries[m.session_id]
    argv = sv._prepare_user_turn(m.session_id, 'followup', entry)[2]
    assert argv.index('--approve-for-me') < argv.index('resume')
