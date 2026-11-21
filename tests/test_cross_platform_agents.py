"""Cross-platform adapter contracts derived from installed native CLI output."""
import json
import sys

import pytest

from agent_profiles import default_command
from tools.session.bridge import _shape_native_row
from tools.supervisor.native_resume import _native_resume_argv, continuation_options
from tools.supervisor.supervisor import Supervisor
from tools.probe.discovery import classify_family


def test_pi_node_package_discovery_is_precise_on_both_platforms():
    assert classify_family('/opt/node', 'node /opt/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js') == 'pi'
    assert classify_family(r'C:\Program Files\nodejs\node.exe',
        r'"C:\Program Files\nodejs\node.exe" "C:\npm\node_modules\@mariozechner\pi-coding-agent\dist\cli.js"') == 'pi'
    assert classify_family('/bin/node', 'node /unrelated/cli.js pi-coding-agent') is None
    assert classify_family('/bin/node', 'node /tmp/pi-coding-agent/dist/cli.js') is None


def test_capability_probe_accepts_native_windows_exe_but_rejects_batch(tmp_path):
    from tools.session.probe import _validate_command, _env_for
    binary = tmp_path / 'codex.exe'
    binary.touch()
    assert _validate_command('codex', [str(binary)])[0] == str(binary.resolve())
    batch = tmp_path / 'codex.cmd'
    batch.touch()
    assert _validate_command('codex', [str(batch)])[0] is None
    env = _env_for(str(binary), [str(tmp_path)])
    assert str(tmp_path) in env['PATH'].split(__import__('os').pathsep)


def test_pi_default_and_followup_are_structured_noninteractive():
    assert list(default_command('pi')) == ['pi', '--print', '--mode', 'json', '{instruction}']
    assert _native_resume_argv('pi', 'pi', 'native-id', 'followup') == [
        'pi', '--print', '--mode', 'json', '--session', 'native-id', 'followup']
    assert _native_resume_argv('pi', 'pi', 'native-id', '--literal prompt')[-2:] == ['--', '--literal prompt']


def test_pi_complete_stream_events_are_mapped_once_without_thinking():
    message = {'role': 'assistant', 'content': [
        {'type': 'thinking', 'thinking': 'private internal reasoning'},
        {'type': 'text', 'text': '读取完成 🌲'},
        {'type': 'toolCall', 'name': 'read', 'id': 'read1', 'arguments': {'path': 'probe.txt'}}]}
    events = _shape_native_row({'type': 'message_end', 'message': message})
    assert [kind for kind, _ in events] == ['assistant_message', 'tool_call']
    assert events[0][1]['text'] == '读取完成 🌲'
    assert events[1][1]['call_id'] == 'read1'
    # Pi emits tool_execution_end AND the complete toolResult message. The
    # transcript-compatible complete message is the sole result authority.
    assert _shape_native_row({'type': 'tool_execution_end', 'toolName': 'read',
        'toolCallId': 'read1', 'result': {'content': [{'type': 'text', 'text': 'actual contents'}]}}) == []
    result = _shape_native_row({'type': 'message_end', 'message': {'role': 'toolResult',
        'toolName': 'read', 'toolCallId': 'read1',
        'content': [{'type': 'text', 'text': 'actual contents'}], 'isError': False}})
    assert result[0][0] == 'tool_result' and result[0][1]['result'] == 'actual contents'
    assert result[0][1]['call_id'] == 'read1'
    for kind in ['message_start', 'message_update', 'turn_end', 'agent_end']:
        assert _shape_native_row({'type': kind, 'message': message}) == []


def test_node_script_and_pi_options_survive_continuation():
    command = [r'C:\Program Files\nodejs\node.exe', r'C:\npm\pi\cli.js',
        '--print', '--mode', 'json', '--provider', 'private-provider', '--model', 'model-id',
        '--session-dir', 'D:/sessions', '--thinking', 'high', '--offline', '--no-extensions', 'FIRST']
    assert continuation_options('pi', command) == [command[1], '--provider', 'private-provider',
        '--model', 'model-id', '--session-dir', 'D:/sessions', '--thinking', 'high', '--offline', '--no-extensions']


def test_pi_session_identity_survives_supervisor_restart(tmp_path):
    root = tmp_path / 'manifests'
    first = Supervisor(root, 'fixture-node')
    row = json.dumps({'type': 'session', 'id': 'pi-native-id', 'version': 3, 'cwd': str(tmp_path)})
    m = first.launch(agent='pi', command=[sys.executable, '-c', f'print({row!r})'],
        cwd=str(tmp_path), env_allowlist={})
    assert first.wait_session(m.session_id, timeout_s=5).exit_code == 0
    second = Supervisor(root, 'fixture-node')
    assert second.get(m.session_id).reason == 'completed'
    assert second.continuation_ready(m.session_id)
    assert second._entries[m.session_id].resume_token == 'pi-native-id'
    assert 'pi-native-id' not in (root / (m.session_id + '.json')).read_text(encoding='utf-8')


def test_ephemeral_pi_does_not_claim_native_resume(tmp_path):
    sv = Supervisor(tmp_path / 'manifests', 'fixture-node')
    m = sv.launch(agent='pi', command=[sys.executable, '-c',
        'print(\'{"type":"session","id":"not-persisted"}\')', '--no-session'],
        cwd=str(tmp_path), env_allowlist={})
    sv.wait_session(m.session_id, timeout_s=5)
    assert not sv.continuation_ready(m.session_id)


def test_zcode_pretty_json_identity_restores_script_and_original_session(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    native = tmp_path / '.zcode/cli/db/db.sqlite'
    native.parent.mkdir(parents=True)
    with sqlite3.connect(native) as db:
        db.execute('create table session(id text, directory text)')
        db.execute('insert into session values (?,?)', ('sess_zcode_native', str(tmp_path)))
    sv = Supervisor(tmp_path / 'm', 'fixture-node')
    output = json.dumps({'sessionId': 'sess_zcode_native', 'response': 'actual reply'}, indent=2)
    m = sv.launch(agent='zcode', command=[sys.executable, '-c', f'print({output!r})'],
        cwd=str(tmp_path), env_allowlist={'HOME': str(tmp_path)})
    assert sv.wait_session(m.session_id, timeout_s=5).exit_code == 0
    assert sv.continuation_ready(m.session_id)
    assert sv._entries[m.session_id].resume_token == 'sess_zcode_native'
    assert _native_resume_argv('zcode', 'node', 'sess_zcode_native', 'FOLLOW') == [
        'node', '--json', '--resume', 'sess_zcode_native', '-p', 'FOLLOW']


def test_zcode_rollout_identity_never_uses_the_filename_prefix_as_token(tmp_path):
    from tools.supervisor.native_resume import _resume_token_from_native_path
    path=tmp_path/'model-io-sess_native.jsonl'
    path.write_text(json.dumps({'type':'model_io','sessionId':'sess_native'})+'\n',encoding='utf-8')
    assert _resume_token_from_native_path(str(path))=='sess_native'
    path.write_text(json.dumps({'type':'model_io','sessionId':'sess_other'})+'\n',encoding='utf-8')
    assert _resume_token_from_native_path(str(path)) is None


def test_arbitrary_hermes_transcript_cannot_claim_attached_resume(tmp_path):
    from tests.test_supervisor_attach import _FakeOps
    path = tmp_path / 'unverified.json'
    path.write_text('{}', encoding='utf-8')
    sv = Supervisor(tmp_path / 'manifests', 'fixture-node', ops=_FakeOps())
    assert sv.attach_to_existing(123, 'now', 'hermes', str(path), 'hermes',
        session_id='adopt_12345678', probe=lambda _: ('now', 'hermes')) == 'adopted'
    assert not sv.get('adopt_12345678').capability_manifest.get('resume', False)


def test_hermes_stderr_session_id_requires_matching_native_cwd(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    native = tmp_path / '.hermes/state.db'
    native.parent.mkdir(parents=True)
    with sqlite3.connect(native) as db:
        db.execute('create table sessions(id text, cwd text)')
        db.execute('insert into sessions values (?,?)', ('20261121_123456_abcd', str(tmp_path)))
    sv = Supervisor(tmp_path / 'm', 'fixture-node')
    m = sv.launch(agent='hermes', command=[sys.executable, '-c',
        'import sys;print("session_id: 20261121_123456_abcd",file=sys.stderr)'],
        cwd=str(tmp_path), env_allowlist={'HOME': str(tmp_path)})
    assert sv.wait_session(m.session_id, timeout_s=5).exit_code == 0
    assert sv.continuation_ready(m.session_id)
    assert _native_resume_argv('hermes', 'hermes', '20261121_123456_abcd', 'FOLLOW') == [
        'hermes', 'chat', '--quiet', '--resume', '20261121_123456_abcd', '--query', 'FOLLOW']


@pytest.mark.parametrize('family', ['pi', 'zcode', 'hermes'])
def test_owned_native_process_projects_real_tool_evidence_to_hub(tmp_path, family):
    import sqlite3
    from tools.session.runner_capture import RunnerCapture
    from tools.session.native_storage import database_path
    from tests.test_review_node_sync import _cfg, _client
    env = {'HOME': str(tmp_path), 'USERPROFILE': str(tmp_path)}
    token = 'sess_fixture_native'
    if family == 'pi':
        rows = [{'type': 'session', 'id': token},
            {'type': 'message_end', 'message': {'role': 'assistant', 'content': [
                {'type': 'toolCall', 'id': 'c1', 'name': 'read', 'arguments': {'path': 'probe.txt'}}]}},
            {'type': 'tool_execution_end', 'toolName': 'read', 'toolCallId': 'c1', 'result': {'content': [{'type': 'text', 'text': 'actual fixture bytes 🌲'}]}},
            {'type': 'message_end', 'message': {'role': 'toolResult', 'toolCallId': 'c1', 'toolName': 'read', 'content': [{'type': 'text', 'text': 'actual fixture bytes 🌲'}]}}]
        program = 'import json; rows=' + repr(rows) + '; [print(json.dumps(r)) for r in rows]'
    else:
        path = database_path(family, env)
        path.parent.mkdir(parents=True)
        with sqlite3.connect(path) as db:
            if family == 'zcode':
                db.execute('create table session(id text,directory text)')
                db.execute('insert into session values (?,?)', (token, str(tmp_path)))
                db.execute('create table message(id text,session_id text,data text)')
                db.execute('create table part(message_id text,session_id text,data text)')
                db.execute('insert into message values (?,?,?)', ('m1', token, '{"role":"assistant"}'))
                db.execute('insert into part values (?,?,?)', ('m1', token, json.dumps({'type': 'tool', 'tool': 'read', 'callID': 'c1', 'state': {'status': 'completed', 'input': {'path': 'probe.txt'}, 'output': 'actual fixture bytes 🌲'}})))
                program = 'print(' + repr(json.dumps({'sessionId': token}, indent=2)) + ')'
            else:
                db.execute('create table sessions(id text,cwd text)')
                db.execute('insert into sessions values (?,?)', (token, str(tmp_path)))
                db.execute('create table messages(id integer primary key,session_id text,role text,content text,tool_calls text,tool_call_id text,tool_name text)')
                db.execute('insert into messages values (1,?,?,?,?,?,?)', (token, 'assistant', '', json.dumps([{'id': 'c1', 'function': {'name': 'read', 'arguments': '{"path":"probe.txt"}'}}]), None, None))
                db.execute('insert into messages values (2,?,?,?,?,?,?)', (token, 'tool', 'actual fixture bytes 🌲', None, 'c1', 'read'))
                program = 'import sys; print("session_id: ' + token + '",file=sys.stderr)'
    cfg, client = _cfg(tmp_path), _client(tmp_path)
    def send(cfg, path, events):
        result = client.post(path, json=events, headers={'X-Runner-Credential': 'pxed:secret'})
        return result.status_code, result.get_json()
    capture = RunnerCapture(cfg, {'attempt_id': 'a1', 'agent_type': family, 'task_id': 't1'}, send, process_group_id='grp_fixture')
    try:
        sv = Supervisor(tmp_path / 'manifests', cfg.machine)
        session = sv.launch(agent=family, command=[sys.executable, '-c', program], cwd=str(tmp_path), env_allowlist=env)
        assert sv.wait_session(session.session_id, timeout_s=5, on_line=capture.line).exit_code == 0
        assert sv.continuation_ready(session.session_id)
        assert capture.failed_lines == 0
        capture.flush()
        events = client.get('/api/sessions/' + capture.session_id + '/events').get_json()['events']
        results = [e for e in events if e['kind'] == 'tool_result']
        assert len(results) == 1
        assert results[0]['payload']['result'] == 'actual fixture bytes 🌲'
        assert any(e['kind'] == 'tool_call' for e in events)
    finally:
        capture.bridge.close()
