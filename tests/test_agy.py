"""Antigravity protocol fixtures; never execute the installed vendor binary."""
import json
import sys

from agent_profiles import PROFILES, default_command
from tools.adapters import create
from tools.probe.discovery import classify_family
from tools.supervisor.native_resume import build_resume_argv, continuation_options
from tools.supervisor.supervisor import Supervisor

CONVERSATION = '055a398f-db14-4c5f-abbb-1bf03f8120a7'


def init():
    return {'event': 'init', 'conversation_id': CONVERSATION,
            'init': {'cwd': '/private/workspace', 'tools': ['run_command']}}


def step(index, kind, state='DONE', **fields):
    return {'event': 'step_update', 'step_update': {
        'conversation_id': CONVERSATION, 'step_index': index,
        'step_type': kind, 'state': state, **fields}}


def test_agy_registered_native_command_and_exact_discovery():
    assert default_command('agy') == ('agy', 'acp')
    assert PROFILES['agy'].runner_resume_only
    assert classify_family('/opt/bin/agy', 'agy --print prompt') == 'agy'
    assert classify_family(r'C:\bin\agy.exe', 'agy.exe --print prompt') == 'agy'
    assert classify_family('/opt/bin/antigravity', 'antigravity') is None
    argv = create('agy').build_argv('literal $(touch nope)')
    assert argv[-1] == '--instruction=literal $(touch nope)'
    assert json.loads(argv[argv.index('--vendor-command') + 1])[-1] == 'acp'


def test_agy_exact_resume_uses_owned_acp_without_print_fallback():
    command = ['agy', '--output-format', 'stream-json', '--project', 'my-project',
               '--model', 'observed-model', '--effort', 'high', '--mode', 'plan',
               '--settings', '{"theme":"dark"}', '--print', 'FIRST']
    options = continuation_options('agy', command)
    argv = build_resume_argv('agy', 'agy', CONVERSATION, 'FOLLOW', options)
    assert argv == [], 'pre-ACP identities must fail closed instead of print fallback'
    owned = create('agy').build_argv('FIRST')
    options = continuation_options('agy', owned)
    argv = build_resume_argv('agy', owned[0], CONVERSATION, '--literal', options)
    assert argv[-3:] == ['--resume-session', CONVERSATION, '--instruction=--literal']
    assert not any(flag in argv for flag in ('--continue', '--print', '--conversation'))
    assert build_resume_argv('agy', 'agy', CONVERSATION, '--unsafe') == []


def test_agy_configured_native_model_and_mode_use_rpc_not_cli_flags():
    argv = create('agy', command=['agy', 'acp', '--model', 'native-model', '--mode=plan']).build_argv('hi')
    assert json.loads(argv[argv.index('--vendor-command') + 1])[-1] == 'acp'
    assert argv[argv.index('--requested-model') + 1] == 'native-model'
    assert argv[argv.index('--requested-mode') + 1] == 'plan'


def test_agy_rejects_custom_print_selector_and_missing_acp():
    import pytest
    from tools.spawn_policy import UnsafeInstruction
    for command in [['agy'], ['agy', '--print=hidden'], ['agy', 'acp', '--continue=latest'],
                    ['agy', 'acp', 'prompt={instruction}']]:
        with pytest.raises(UnsafeInstruction):
            create('agy', command=command).build_argv('hi')


def test_agy_stream_retains_text_tools_errors_and_distinct_turns():
    from tools.session.agy import AgyStream
    stream = AgyStream()
    assert stream.normalize(init()) == []
    first = stream.normalize(step(3, 'agent_response', 'ACTIVE', text_delta='hello '))
    last = stream.normalize(step(3, 'agent_response', text_delta='world', thinking='PRIVATE'))
    assert [first[0][1]['text'], last[0][1]['text']] == ['hello ', 'world']
    assert first[0][1]['message_id'] == last[0][1]['message_id']
    assert first[0][1]['is_complete'] is False and last[0][1]['is_complete'] is True
    tools = stream.normalize(step(4, 'tool', tool_name='run_command', tool_info={
        'name': 'run_command', 'parameters': {'CommandLine': 'echo hello'},
        'output': 'hello', 'error': {'type': 'exit', 'message': 'failed'}}))
    assert [kind for kind, _ in tools] == ['tool_call', 'tool_result']
    assert json.loads(tools[0][1]['arguments']) == {'CommandLine': 'echo hello'}
    assert tools[1][1]['status'] == 'error' and 'failed' in tools[1][1]['result']
    result = {'event': 'result', 'result': {'conversation_id': CONVERSATION,
              'status': 'SUCCESS', 'response': 'hello world'}}
    assert not any(kind == 'assistant_message' for kind, _ in stream.normalize(result))
    stream.normalize(init())
    again = stream.normalize(step(3, 'agent_response', text_delta='next turn'))
    assert again[0][1]['message_id'] != first[0][1]['message_id']
    assert 'PRIVATE' not in json.dumps(first + last + tools)


def test_agy_result_fallback_malformed_records_and_bounded_state():
    from tools.session.agy import AgyStream
    stream = AgyStream()
    stream.normalize(init())
    result = stream.normalize({'event': 'result', 'result': {
        'conversation_id': CONVERSATION, 'status': 'SUCCESS', 'response': 'complete only'}})
    assert [p['text'] for k, p in result if k == 'assistant_message'] == ['complete only']
    for raw in [None, [], {'event': {}}, {'event': 'step_update', 'step_update': None},
                step(-1, 'agent_response', text_delta='bad'),
                step(True, 'agent_response', text_delta='bad'),
                step(1, 'reasoning', text_delta='PRIVATE'),
                {**step(1, 'agent_response', text_delta='bad'), 'step_update': {
                    'conversation_id': 'other', 'step_index': 1, 'state': 'DONE',
                    'step_type': 'agent_response', 'text_delta': 'bad'}}]:
        assert stream.normalize(raw) == []


def test_agy_rejects_unbound_identity_and_does_not_claim_arbitrary_adoption(tmp_path):
    from tests.test_agent_capability_layers import project
    from hub.domain.session_access import AdoptionAccess
    from tools.session.agy import AgyStream
    assert AgyStream().normalize(step(1, 'agent_response', text_delta='unbound')) == []
    stream = AgyStream()
    stream.normalize(init())
    assert stream.normalize(step(1, 'agent_response', state=[], text_delta='bad')) == []
    program = 'import json; print(json.dumps(' + repr(init()) + '))'
    supervisor = Supervisor(tmp_path / 'manifests', 'fixture-node')
    manifest = supervisor.launch(agent='agy', command=[sys.executable, '-c', program],
                                 cwd=str(tmp_path), env_allowlist={})
    supervisor.wait_session(manifest.session_id, timeout_s=5)
    assert not supervisor.continuation_ready(manifest.session_id), 'init cwd must match the owned task'
    adopted = project('agy', adoption=AdoptionAccess('m1', 'agy', 'adopted', '/private/source'), session_id='adopt_agy')
    assert adopted['message_delivery']['reason'] == 'native_resume_unsupported'
    assert 'configure_model' not in project('agy')['allowed_actions']


def test_pre_acp_agy_identity_never_advertises_unsupported_resume(tmp_path):
    rows = [init(), {'event': 'result', 'result': {
        'conversation_id': CONVERSATION, 'status': 'SUCCESS', 'response': 'ok'}}]
    rows[0]['init']['cwd'] = str(tmp_path)
    program = 'import json; rows=' + repr(rows) + ';[print(json.dumps(r)) for r in rows]'
    first = Supervisor(tmp_path / 'manifests', 'fixture-node')
    m = first.launch(agent='agy', command=[sys.executable, '-c', program],
                     cwd=str(tmp_path), env_allowlist={})
    assert first.wait_session(m.session_id, timeout_s=5).exit_code == 0
    second = Supervisor(tmp_path / 'manifests', 'fixture-node')
    assert not second.continuation_ready(m.session_id)
    assert CONVERSATION not in (tmp_path / 'manifests' / (m.session_id + '.json')).read_text(encoding='utf-8')


def test_persisted_pre_acp_identity_is_retained_without_resume_authority(tmp_path):
    first = Supervisor(tmp_path / 'manifests', 'fixture-node')
    m = first.launch(agent='agy', command=[sys.executable, '-c', 'print("legacy")'],
                     cwd=str(tmp_path), env_allowlist={})
    first.wait_session(m.session_id, timeout_s=5)
    entry = first._entries[m.session_id]
    entry.resume_token = CONVERSATION
    assert first._continuations.save(entry)
    entry.manifest.capability_manifest = {'resume': True}
    first._persist(entry.manifest)
    second = Supervisor(tmp_path / 'manifests', 'fixture-node')
    assert not second.continuation_ready(m.session_id)
    assert second._entries[m.session_id].resume_token == CONVERSATION
    assert second._entries[m.session_id].manifest.capability_manifest['resume'] is False
    assert second.continuation_retained(m.session_id), 'legacy identity and workspace stay recoverable'


def test_agy_runner_capture_real_sqlite_http_round_trip(tmp_path):
    from tools.session.runner_capture import RunnerCapture
    from tests.test_review_node_sync import _cfg, _client
    cfg, client = _cfg(tmp_path), _client(tmp_path)

    def send(cfg, path, events):
        response = client.post(path, json=events, headers={'X-Runner-Credential': 'pxed:secret'})
        return response.status_code, response.get_json()

    capture = RunnerCapture(cfg, {'attempt_id': 'agy_attempt', 'agent_type': 'agy',
        'task_id': 't-agy', 'instruction': 'read file'}, send, process_group_id='grp_fixture')
    try:
        rows = [init(), step(1, 'agent_response', 'ACTIVE', text_delta='visible '),
                step(1, 'agent_response', text_delta='answer'),
                step(2, 'tool', tool_name='run_command', tool_info={
                    'parameters': {'CommandLine': 'echo fixture'}, 'output': 'fixture'}),
                {'event': 'result', 'result': {'conversation_id': CONVERSATION,
                 'status': 'SUCCESS', 'response': 'visible answer'}}]
        for row in rows:
            assert capture.line(json.dumps(row), strict=True)
        capture.flush()
        response = client.get('/api/sessions/' + capture.session_id + '/events?include_source=0')
        assert response.status_code == 200
        events = response.get_json()['events']
        answers = [e['payload']['text'] for e in events if e['kind'] == 'assistant_message']
        assert answers == ['visible ', 'answer']
        assert len([e for e in events if e['kind'] == 'tool_result']) == 1
        assert not any(e['kind'] == 'source_record' for e in events)
    finally:
        capture.bridge.close()
