"""Independent literal ACP stdio peer; never imports the product runtime."""
import json
import os
import sys
import threading
import time
from pathlib import Path

scenario, record = sys.argv[1:3]
SESSION = 'agy_native_fixture'
lock = threading.Lock()


def emit(value):
    with lock:
        sys.stdout.buffer.write((json.dumps(value, ensure_ascii=False) + '\n').encode('utf-8'))
        sys.stdout.buffer.flush()


def response(request, result=None, error=None):
    emit({'jsonrpc': '2.0', 'id': request['id'],
          **({'error': error} if error else {'result': result or {}})})


def update(session_update, **fields):
    emit({'jsonrpc': '2.0', 'method': 'session/update', 'params': {
        'sessionId': SESSION, 'update': {'sessionUpdate': session_update, **fields}}})


def trace(value):
    with open(record, 'a', encoding='utf-8') as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + '\n')


pending_prompt = None
for line in sys.stdin.buffer:
    request = json.loads(line.decode('utf-8'))
    assert request.get('jsonrpc') == '2.0'
    trace(request)
    method, params = request.get('method'), request.get('params', {})
    if method == 'initialize':
        assert params['protocolVersion'] == 1
        assert params['clientInfo']['name'] == 'agent-fleet'
        assert params['clientCapabilities'] == {'fs': {'readTextFile': False, 'writeTextFile': False}, 'terminal': False}
        if scenario == 'startup_timeout':
            continue
        response(request, {'protocolVersion': 2 if scenario == 'wrong_version' else 1,
            'agentInfo': {'name': 'Antigravity', 'version': '1.5.14'},
            'agentCapabilities': {'sessionCapabilities': {'resume': False if scenario == 'invalid_resume' else {}}},
            'authMethods': [{'id': 'oauth-personal', 'name': 'Google'}]})
    elif method == 'authenticate':
        assert params == {'methodId': 'oauth-personal'}
        response(request, error={'code': -32000, 'message': 'SECRET_AUTH_TEXT'} if scenario == 'auth_fail' else None)
    elif method in {'session/new', 'session/resume'}:
        assert Path(params['cwd']).is_absolute() and params['mcpServers'] == []
        if method == 'session/resume':
            assert params['sessionId'] == SESSION
        if scenario == 'resume_fail':
            response(request, error={'code': -32000, 'message': 'SECRET_NATIVE_ID'})
        else:
            if scenario == 'early_metadata':
                update('session_info_update', title='Native fixture title')
            response(request, {'sessionId': SESSION,
                **({'configOptions': [
                    {'id': 'model', 'name': 'Model', 'category': 'model', 'type': 'select',
                     'currentValue': 'model-fixture', 'options': [
                         {'value': 'model-fixture', 'name': 'Fixture'}, {'value': 'model-next', 'name': 'Next'}]},
                    {'id': 'mode', 'name': 'Mode', 'category': 'mode', 'type': 'select',
                     'currentValue': 'default', 'options': [
                         {'value': 'default', 'name': 'Default'}, {'value': 'plan', 'name': 'Plan'}]},
                ]} if scenario == 'configure' else {}),
                'modes': {'currentModeId': 'default', 'availableModes': [{'id': 'default', 'name': 'Default'}]},
                'models': {'currentModelId': 'model-fixture', 'availableModels': [{'modelId': 'model-fixture', 'name': 'Fixture'}]}})
    elif method == 'session/set_config_option':
        assert scenario == 'configure' and params['sessionId'] == SESSION
        assert (params['configId'], params['value']) in {('model', 'model-next'), ('mode', 'plan')}
        response(request, {})
    elif method == 'session/prompt':
        assert params['sessionId'] == SESSION
        assert params['prompt'] and params['prompt'][0]['type'] == 'text'
        pending_prompt = request
        if scenario == 'eof':
            sys.exit(0)
        if scenario == 'oversized':
            sys.stdout.buffer.write(b'{' + b' ' * (1024 * 1024 + 1))
            sys.stdout.buffer.flush()
            continue
        if scenario == 'invalid_utf8':
            sys.stdout.buffer.write(b'\xff\n'); sys.stdout.buffer.flush()
            continue
        if scenario == 'malformed_update':
            update([])
            continue
        if scenario == 'stderr':
            sys.stderr.buffer.write(b'SECRET_STDERR ' * 200000)
            sys.stderr.buffer.flush()
        if scenario == 'wrong_session':
            emit({'jsonrpc': '2.0', 'method': 'session/update', 'params': {
                'sessionId': 'sibling', 'update': {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'WRONG'}}}})
            continue
        if scenario == 'unknown_client_request':
            emit({'jsonrpc': '2.0', 'id': 'fs-request', 'method': 'fs/read_text_file',
                  'params': {'sessionId': SESSION, 'path': '/private/secret'}})
            continue
        if scenario in {'permission', 'permission_cancel', 'permission_early'}:
            emit({'jsonrpc': '2.0', 'id': 'native_permission_1', 'method': 'session/request_permission', 'params': {
                'sessionId': SESSION, 'toolCall': {'toolCallId': 'run_1', 'title': 'Run fixture tests?',
                  'kind': 'execute', 'status': 'pending', 'rawInput': {'CommandLine': 'echo test'}},
                'options': [{'optionId': 'yes', 'name': 'Allow once', 'kind': 'allow_once'},
                            {'optionId': 'no', 'name': 'Reject once', 'kind': 'reject_once'}]}})
            if scenario == 'permission_early':
                response(request, {'stopReason': 'end_turn'})
                pending_prompt = None
            continue
        if scenario == 'cancel':
            continue
        if scenario == 'large_text':
            update('agent_message_chunk', content={'type': 'text', 'text': '🌲' * 30000})
            response(request, {'stopReason': 'end_turn'})
            continue
        if scenario == 'large_tool':
            update('tool_call', toolCallId='large', title='Read output', kind='read', status='in_progress',
                   rawInput={'query': 'x' * 10000 + 'INPUT_TAIL'})
            update('tool_call_update', toolCallId='large', status='completed', rawOutput='🌲' * 10000 + 'OUTPUT_TAIL')
            response(request, {'stopReason': 'end_turn'})
            continue
        if scenario == 'echo':
            update('agent_message_chunk', content={'type': 'text', 'text': 'ACK ' + params['prompt'][0]['text']})
            response(request, {'stopReason': 'end_turn'})
            continue
        update('agent_message_chunk', content={'type': 'text', 'text': 'hello '})
        update('agent_thought_chunk', content={'type': 'text', 'text': 'PRIVATE_THOUGHT'})
        update('agent_message_chunk', content={'type': 'text', 'text': '🌲 world'})
        update('tool_call', toolCallId='run_1', title='Run tests', kind='execute', status='in_progress', rawInput={'CommandLine': 'echo test'})
        if scenario == 'background':
            response(request, {'stopReason': 'end_turn'})
            def finish_background():
                time.sleep(.15)
                update('tool_call_update', toolCallId='run_1', status='completed', rawOutput='passed')
                update('agent_message_chunk', content={'type': 'text', 'text': 'background done'})
            threading.Thread(target=finish_background, daemon=True).start()
        else:
            update('tool_call_update', toolCallId='run_1', status='completed', rawOutput='passed')
            response(request, {'stopReason': 'end_turn'})
    elif method == 'session/cancel':
        assert params == {'sessionId': SESSION}
        if pending_prompt is not None:
            response(pending_prompt, {'stopReason': 'cancelled'})
            pending_prompt = None
    elif 'method' not in request:
        if request['id'] == 'native_permission_1':
            outcome = request['result']['outcome']
            assert outcome in [{'outcome': 'selected', 'optionId': 'yes'}, {'outcome': 'cancelled'}]
            if pending_prompt is not None:
                update('agent_message_chunk', content={'type': 'text', 'text': 'permission resolved'})
                response(pending_prompt, {'stopReason': 'end_turn' if outcome['outcome'] == 'selected' else 'cancelled'})
                pending_prompt = None
        elif request['id'] == 'fs-request':
            assert request['error']['code'] == -32601
            response(pending_prompt, {'stopReason': 'end_turn'})
    else:
        raise AssertionError('Unexpected method: ' + str(method))
