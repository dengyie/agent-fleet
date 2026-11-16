"""Retry incomplete model generations without replaying committed tools."""
import json
from threading import Event

import pytest

from fleet_core.backends.base import ToolReceipt
from hub.domain.run_progress import project_progress
from hub.platform.runtime.native import NativeAssistantRuntime
from tests.test_provider_transport_boundaries import QuietHandler, provider, serve


@pytest.mark.parametrize('partial_tool', [False, True])
def test_real_http_incomplete_generation_retries_with_live_output_and_one_tool(partial_tool):
    calls, executed, events, bodies = [], [], [], []
    retry_seen = Event()
    progress = {}

    def observe(kind, payload):
        nonlocal progress
        events.append((kind, payload))
        progress = project_progress(progress, kind, payload, len(events))
        if kind == 'provider_retry_scheduled':
            retry_seen.set()
        if kind == 'assistant_text_delta' and payload['text'] == 'recovered':
            segment = next(s for s in progress['segments'] if s.get('kind') == 'text')
            assert segment['text'] == 'recovered'

    class Peer(QuietHandler):
        def do_POST(self):
            bodies.append(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
            calls.append(True)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('X-Oneapi-Request-Id', 'body-attempt-' + str(len(calls)))
            self.end_headers()
            if len(calls) == 1:
                delta = {'content': 'abandoned'}
                if partial_tool:
                    delta['tool_calls'] = [{'index': 0, 'id': 'uncommitted', 'function': {
                        'name': 'workspace_write', 'arguments': json.dumps({'path': 'wrong.txt', 'content': 'uncommitted'})}}]
                self.wfile.write(('data: ' + json.dumps({'choices': [{'delta': delta}]}) + '\n\n').encode())
                self.wfile.flush()
                retry_seen.wait(3)
                return
            if len(calls) == 2:
                delta = {'content': 'recovered', 'tool_calls': [{'index': 0, 'id': 'committed', 'function': {
                    'name': 'workspace_write', 'arguments': json.dumps({'path': 'once.txt', 'content': 'done'})}}]}
                chunks = [{'choices': [{'delta': delta, 'finish_reason': 'tool_calls'}]}]
            else:
                chunks = [{'choices': [{'delta': {'content': 'finished'}, 'finish_reason': 'stop'}]}]
            self.wfile.write((''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks) + 'data: [DONE]\n\n').encode())
            self.wfile.flush()

    class Broker:
        def execute(self, **kwargs):
            executed.append(kwargs)
            return ToolReceipt(kwargs['command_id'], 'succeeded', {'size': 4})

    with serve(Peer) as server:
        p = provider(server, stream=True, max_retries=2, sleeper=lambda _: None)
        result = NativeAssistantRuntime(p, Broker()).run(run_id='run-body', owner_id='owner', epoch=1,
            messages=[{'role': 'user', 'content': 'write once'}], tools=[{'name': 'workspace.write'}], event_sink=observe)
    assert result.state == 'succeeded' and result.text == 'finished'
    assert len(calls) == 3 and len(executed) == 1
    assert executed[0]['arguments'] == {'path': 'once.txt', 'content': 'done'}
    assert bodies[0] == bodies[1] and b'uncommitted' not in bodies[2]
    attempts = [p for k, p in events if k == 'provider_request_finished']
    assert [p['status'] for p in attempts] == ['unknown', 'succeeded', 'succeeded']
    assert [p['gateway_request_id'] for p in attempts] == ['body-attempt-1', 'body-attempt-2', 'body-attempt-3']
    assert attempts[0]['provider_error'] == 'timeout' and attempts[0]['retryable'] is True


def test_request_start_for_retry_replaces_only_its_incomplete_draft():
    progress = project_progress({}, 'assistant_text_delta', {'step': 1, 'text': 'earlier', 'offset': 0}, 1)
    progress = project_progress(progress, 'assistant_response', {'step': 1, 'text': 'earlier', 'response_kind': 'tool_call'}, 2)
    progress = project_progress(progress, 'assistant_text_delta', {'step': 2, 'text': 'abandoned', 'offset': 0, 'request_index': 1}, 3)
    progress = project_progress(progress, 'provider_retry_scheduled', {'step': 2, 'retry_index': 1}, 4)
    assert progress['segments'][-1]['text'] == 'abandoned'
    progress = project_progress(progress, 'provider_request_started', {'step': 2, 'request_index': 2}, 5)
    progress = project_progress(progress, 'assistant_text_delta', {'step': 2, 'request_index': 2, 'offset': 0, 'text': 'recovered'}, 6)
    assert [s['text'] for s in progress['segments']] == ['earlier', 'recovered']


@pytest.mark.parametrize('failure', ['timeout', 'network', 'eof', 'transient-frame'])
def test_exhausted_incomplete_generations_close_each_body_and_never_execute_tool(failure):
    from hub.platform.providers.openai_compatible import OpenAICompatibleProvider, TransportResponse
    from hub.platform.runtime.native import ProviderBoundaryUnknown
    from tests.test_platform_provider import FakeTransport

    closed, events, executed = [], [], []

    def body(index):
        try:
            yield b'data: {"choices":[{"delta":{"content":"partial","tool_calls":[{"index":0,"function":{"name":"workspace_write","arguments":"{}"}}]}}]}\n\n'
            if failure == 'timeout':
                raise TimeoutError('private-error')
            if failure == 'network':
                raise OSError('private-error')
            if failure == 'transient-frame':
                yield b'data: {"error":{"code":"system_cpu_overloaded","message":"private-error"}}\n\n'
        finally:
            closed.append(index)

    transport = FakeTransport([TransportResponse(200, {}, body(index)) for index in range(2)])

    def sleep(_):
        assert closed == [0]

    p = OpenAICompatibleProvider(model='test', endpoint='http://fixture', api_key='private',
        transport=transport, stream=True, max_retries=1, sleeper=sleep)

    class Broker:
        def execute(self, **kwargs):
            executed.append(kwargs)
            raise AssertionError('Uncommitted generation must never execute a tool')

    with pytest.raises(ProviderBoundaryUnknown):
        NativeAssistantRuntime(p, Broker()).run(run_id='run-exhausted', owner_id='owner', epoch=1,
            messages=[], tools=[{'name': 'workspace.write'}], event_sink=lambda k, d: events.append((k, d)))
    assert len(transport.calls) == 2 and not executed and closed == [0, 1]
    assert [d['status'] for k, d in events if k == 'provider_request_finished'] == ['unknown', 'unknown']
    assert len([k for k, _ in events if k == 'provider_retry_scheduled']) == 1
    assert 'private-error' not in json.dumps(events)


def test_cancellation_after_partial_generation_closes_body_and_prevents_retry():
    from hub.platform.providers.openai_compatible import OpenAICompatibleProvider, TransportResponse
    from tests.test_platform_provider import FakeTransport

    cancelled, closed = [False], []

    def body():
        try:
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            raise TimeoutError()
        finally:
            closed.append(True)

    transport = FakeTransport([TransportResponse(200, {}, body())])
    p = OpenAICompatibleProvider(model='test', endpoint='http://fixture', api_key='private',
        transport=transport, stream=True, max_retries=3, sleeper=lambda _: pytest.fail('Cancelled retry must not sleep'))

    def observe(kind, payload):
        if kind == 'provider_retry_scheduled':
            cancelled[0] = True

    result = NativeAssistantRuntime(p, None).run(run_id='run-cancelled', owner_id='owner', epoch=1,
        messages=[], tools=[], event_sink=observe, should_cancel=lambda: cancelled[0])
    assert result.state == 'cancelled' and len(transport.calls) == 1 and closed == [True]
