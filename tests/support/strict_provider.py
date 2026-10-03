"""OpenAI wire-contract fixture. Only the external model is simulated.

Does not import production serializers or tool definitions: malformed client
requests must fail here even when the same bug exists on both sides of Hub.
"""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

from jsonschema import Draft202012Validator, ValidationError, SchemaError

REPORT = 'Full-flow artifact: 中文内容\nverified bytes\n'
NAME = re.compile(r'[A-Za-z0-9_-]{1,64}')


def validate_request(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('model'), str):
        raise ValueError('invalid_request')
    definitions = {}
    for tool in payload['tools']:
        function = tool['function']
        name = function['name']
        if tool['type'] != 'function' or not NAME.fullmatch(name) or name in definitions:
            raise ValueError('invalid_tool_name')
        Draft202012Validator.check_schema(function['parameters'])
        if not function.get('description'):
            raise ValueError('missing_tool_description')
        definitions[name] = function
    pending = {}
    for message in payload['messages']:
        if message['role'] == 'assistant':
            for call in message.get('tool_calls', []):
                function = call['function']
                if function['name'] not in definitions or call['id'] in pending:
                    raise ValueError('invalid_tool_history')
                Draft202012Validator(definitions[function['name']]['parameters']).validate(
                    json.loads(function['arguments']))
                pending[call['id']] = function['name']
        elif message['role'] == 'tool':
            if message['tool_call_id'] not in pending:
                raise ValueError('unmatched_tool_result')
            del pending[message['tool_call_id']]
            json.loads(message['content'])
    if pending:
        raise ValueError('missing_tool_result')
    return definitions


class StrictProvider:
    def __init__(self):
        self.requests = []
        self.errors = []
        self.entered = Event()
        self.release = Event()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def send(self, status, payload, *, raw=False):
                body = payload if raw else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # Expected only in the transport-timeout scenario.

            def do_POST(self):
                if self.path != '/v1/chat/completions':
                    return self.send(404, {'error': {'code': 'wrong_path'}})
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 1024 * 1024:
                    return self.send(413, {'error': {'code': 'oversized_request'}})
                try:
                    payload = json.loads(self.rfile.read(size))
                    definitions = validate_request(payload)
                    if self.headers.get('Authorization') != 'Bearer full-flow-provider-secret':
                        raise ValueError('invalid_authentication')
                except (ValueError, KeyError, TypeError, ValidationError, SchemaError):
                    fixture.errors.append('invalid_contract')
                    return self.send(400, {'error': {'code': 'invalid_contract'}})
                fixture.requests.append(payload)
                last_user = max(i for i, m in enumerate(payload['messages']) if m['role'] == 'user')
                transcript = payload['messages'][last_user:]
                prompt = transcript[0]['content']
                if 'slow' in prompt or 'timeout' in prompt:
                    fixture.entered.set()
                    if not fixture.release.wait(15):
                        return self.send(504, {'error': {'code': 'fixture_timeout'}})
                if 'http-' in prompt or payload['model'] == 'broken':
                    match = re.search(r'http-(\d{3})', prompt)
                    return self.send(int(match[1]) if match else 502,
                                     {'error': {'code': 'do_request_failed', 'message': 'must-not-leak'}})
                if 'bad-json' in prompt:
                    return self.send(200, b'{broken', raw=True)
                step = sum(m['role'] == 'tool' for m in transcript)
                if 'invalid-tool' in prompt:
                    name, arguments = 'not_advertised', {}
                else:
                    operations = [('workspace_list', {})]
                    if payload['model'] != 'read-only':
                        operations += [
                            ('workspace_write', {'path': '../escape.txt' if 'unsafe-path' in prompt else 'flow.txt', 'content': REPORT}),
                            ('workspace_read', {'path': 'flow.txt'}),
                            ('workspace_artifact', {'path': 'flow.txt', 'name': 'flow.txt', 'content_type': 'text/plain'}),
                        ]
                    if step >= len(operations):
                        return self.send(200, {'choices': [{'message': {'content': 'Full flow completed: 中文回复'}, 'finish_reason': 'stop'}],
                                               'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}})
                    name, arguments = operations[step]
                    if name not in definitions:
                        return self.send(400, {'error': {'code': 'missing_required_tool'}})
                self.send(200, {'choices': [{'message': {'role': 'assistant', 'tool_calls': [{
                    'id': f'flow-call-{step}', 'type': 'function',
                    'function': {'name': name, 'arguments': json.dumps(arguments)},
                }]}, 'finish_reason': 'tool_calls'}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}})

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.url = f'http://127.0.0.1:{self.server.server_port}/v1/chat/completions'

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        assert not self.thread.is_alive()
