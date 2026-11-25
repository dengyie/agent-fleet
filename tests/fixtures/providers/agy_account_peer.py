"""Independent Google-account/ACP peer. No vendor code, network or model."""
import json
import os
from pathlib import Path
import sys

profile = Path(os.environ['GEMINI_HOME'])
credential = profile / 'fixture-credential.json'
trace = profile / 'fixture-trace.jsonl'
assert os.environ['SSH_CONNECTION'] == '127.0.0.1 0 127.0.0.1 0'
assert os.environ['AGY_ACP_FORCE_FILE_STORAGE'] == '1'
assert not any(k.upper() in {'GOOGLE_API_KEY', 'GEMINI_API_KEY', 'GOOGLE_APPLICATION_CREDENTIALS',
    'AGENT_FLEET_TOKEN', 'DISPLAY'} for k in os.environ)


def record(method):
    with trace.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'method': method}) + '\n')


def emit(value):
    sys.stdout.buffer.write((json.dumps(value) + '\n').encode('utf-8'))
    sys.stdout.buffer.flush()


if '--login' in sys.argv:
    record('login')
    print('Waiting for personal Google authorization', flush=True)
    sys.stderr.buffer.write(b'fixture diagnostic\n' * 5000)
    sys.stderr.buffer.flush()
    print('https://accounts.google.com/o/oauth2/auth?response_type=code&state=account-fixture&redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback', flush=True)
    response = sys.stdin.buffer.readline().decode('utf-8').strip()
    if response != 'fixture-code':
        raise SystemExit(4)
    credential.write_text('fixture-private-credential', encoding='utf-8')
    raise SystemExit(0)

pending = None
for line in sys.stdin.buffer:
    request = json.loads(line.decode('utf-8'))
    method = request.get('method')
    record(method)
    result = {}
    if method == 'initialize':
        result = {'protocolVersion': 1,
            'agentInfo': {'name': 'Fixture AGY', 'version': '1.5'},
            'authMethods': [{'id': 'oauth-personal', 'name': 'Google'}],
            'agentCapabilities': {'auth': {'logout': True}, 'sessionCapabilities': {'resume': {}}}}
    elif method == 'authenticate':
        if not credential.exists():
            emit({'jsonrpc': '2.0', 'id': request['id'], 'error': {'code': -32000, 'message': 'private failure'}})
            continue
    elif method in {'session/new', 'session/resume'}:
        result = {'sessionId': 'account-fixture-session',
            'models': {'currentModelId': 'fixture-model', 'availableModels': [
                {'modelId': 'fixture-model', 'name': 'Fixture'}]}}
    elif method == 'session/prompt':
        pending = request
        emit({'jsonrpc': '2.0', 'method': 'session/update', 'params': {
            'sessionId': 'account-fixture-session', 'update': {'sessionUpdate': 'agent_message_chunk',
                'content': {'type': 'text', 'text': 'Fixture account admitted'}}}})
        if request['params']['prompt'][0]['text'] == 'hold':
            continue
        result = {'stopReason': 'end_turn'}
    elif method == 'session/cancel':
        if pending:
            emit({'jsonrpc': '2.0', 'id': pending['id'], 'result': {'stopReason': 'cancelled'}})
            pending = None
        continue
    elif method == 'logout':
        credential.unlink(missing_ok=True)
    else:
        raise SystemExit(5)
    emit({'jsonrpc': '2.0', 'id': request['id'], 'result': result})
