"""Independent wire fixtures for per-request observability (no provider serializers)."""
import json

import pytest

from hub.platform.providers.openai_compatible import OpenAICompatibleProvider, ProviderError, TransportResponse


def payload(**extra):
    return {'id': 'chatcmpl-fixture', 'model': 'actual-model',
            'choices': [{'message': {'content': 'done'}, 'finish_reason': 'stop'}], **extra}


class Transport:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


def response(value):
    return TransportResponse(200, {}, json.dumps(value).encode())


def provider(transport, **kwargs):
    return OpenAICompatibleProvider(model='selected-model', endpoint='http://fixture/v1/chat/completions',
        api_key='private-fixture-key', transport=transport, **kwargs)


def test_request_metadata_preserves_five_categories_and_provider_cost():
    events = []
    p = provider(Transport(response(payload(usage={'prompt_tokens': 147, 'completion_tokens': 24,
        'total_tokens': 171, 'prompt_tokens_details': {'cached_tokens': 100, 'cache_write_tokens': 0},
        'completion_tokens_details': {'reasoning_tokens': 10}, 'cost': 0.000131}))))
    p.complete([], [], request_observer=lambda kind, data: events.append((kind, data)))
    assert [e[0] for e in events] == ['provider_request_started', 'provider_request_finished']
    data = events[-1][1]
    assert data['model'] == 'actual-model'
    assert data['requested_model'] == 'selected-model'
    assert data['usage'] == {'input_tokens': 147, 'output_tokens': 24, 'total_tokens': 171,
        'cache_read_tokens': 100, 'cache_write_tokens': 0, 'reasoning_tokens': 10}
    assert data['cost'] == {'amount': '0.000131', 'currency': 'USD', 'source': 'provider'}
    assert data['duration_ms'] >= 0 and data['started_at'] > 0
    assert 'private-fixture-key' not in json.dumps(events)


def test_unknown_usage_is_not_zero_and_each_retry_is_recorded():
    events = []
    p = provider(Transport(TransportResponse(429, {}, b'{}'), response(payload())), max_retries=1, sleeper=lambda _: None)
    p.complete([], [], request_observer=lambda kind, data: events.append((kind, data)))
    finished = [data for kind, data in events if kind.endswith('finished')]
    assert len(finished) == 2
    assert [x['request_index'] for x in finished] == [1, 2]
    assert finished[0]['status'] == 'failed' and finished[0]['http_status'] == 429
    assert finished[1]['status'] == 'succeeded'
    assert finished[1]['usage']['input_tokens'] is None
    assert finished[1]['cost'] is None


@pytest.mark.parametrize('stream', [False, True])
def test_response_request_ids_distinguish_failed_and_successful_attempts(stream):
    events = []
    wire = (b'data: {"id":"reply-id","choices":[{"delta":{"content":"done"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
            if stream else json.dumps(payload(id='reply-id')).encode())
    transport = Transport(
        TransportResponse(503, {'X-Oneapi-Request-Id': 'gateway-failed',
            'X-Upstream-Request-Id': 'upstream-failed', 'Set-Cookie': 'private-cookie'}, b'{}'),
        TransportResponse(200, {'x-oneapi-request-id': 'gateway-success',
            'x-upstream-request-id': 'upstream-success', 'Authorization': 'private-header'}, wire))
    result = provider(transport, stream=stream, max_retries=1, sleeper=lambda _: None).complete(
        [], [], request_observer=lambda kind, data: events.append((kind, data)))
    finished = [data for kind, data in events if kind == 'provider_request_finished']
    assert [data.get('gateway_request_id') for data in finished] == ['gateway-failed', 'gateway-success']
    assert [data.get('upstream_request_id') for data in finished] == ['upstream-failed', 'upstream-success']
    assert [data['request_index'] for data in finished] == [1, 2]
    assert result.metadata['response_id'] == 'reply-id'
    assert result.metadata['gateway_request_id'] == 'gateway-success'
    assert result.metadata['upstream_request_id'] == 'upstream-success'
    assert not any(secret in json.dumps(events) for secret in ('private-cookie', 'private-header', 'private-fixture-key'))


def test_accepted_stream_failure_keeps_request_ids_without_replay():
    events, closed = [], []
    def chunks():
        try:
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            raise TimeoutError('private failure detail')
        finally:
            closed.append(True)
    transport = Transport(TransportResponse(200, {'X-Oneapi-Request-Id': 'gateway-interrupted',
        'X-Upstream-Request-Id': 'upstream-interrupted'}, chunks()))
    with pytest.raises(ProviderError) as caught:
        provider(transport, stream=True, max_retries=3).complete(
            [], [], request_observer=lambda kind, data: events.append((kind, data)))
    finished = events[-1][1]
    assert finished.get('gateway_request_id') == 'gateway-interrupted'
    assert finished.get('upstream_request_id') == 'upstream-interrupted'
    assert caught.value.diagnostic()['gateway_request_id'] == 'gateway-interrupted'
    assert finished['status'] == 'unknown'
    assert len(transport.calls) == 1 and closed == [True]
    assert 'private failure detail' not in json.dumps(events)


@pytest.mark.parametrize('bad', [None, True, 7, [], {}, '', 'x' * 129, 'with space',
    'line\nbreak', 'id,another-id', '<img src=x>', 'https://private.invalid/?key=secret', '非编号'])
def test_request_ids_reject_nonopaque_values_at_provider_and_projection(bad):
    from hub.domain.request_metadata import project_requests
    events = []
    transport = Transport(TransportResponse(503, {'X-Oneapi-Request-Id': bad,
        'X-Upstream-Request-Id': bad}, b'{}'))
    with pytest.raises(ProviderError) as caught:
        provider(transport, max_retries=0).complete(
            [], [], request_observer=lambda kind, data: events.append((kind, data)))
    projected = project_requests([{'payload': {'attempt': 1, 'step': 1, 'request_index': 1,
        'gateway_request_id': bad, 'upstream_request_id': bad, 'raw_headers': {'secret': 'hidden'}}}], 'unknown')[0]
    for record in (caught.value.diagnostic(), events[-1][1], projected):
        assert 'gateway_request_id' not in record and 'upstream_request_id' not in record
        assert 'raw_headers' not in record


def test_valid_request_ids_remain_distinct_from_local_and_response_ids():
    from hub.domain.request_metadata import freeze_requests, project_requests, thaw_request_summary
    gateway_id = 'G' * 128
    upstream_id = 'request_123.ABC-def:456'
    requests = project_requests([{'payload': {'attempt': 1, 'step': 2, 'request_index': 3,
        'gateway_request_id': gateway_id, 'upstream_request_id': upstream_id,
        'response_id': 'reply-id', 'status': 'failed'}}], 'unknown')
    assert requests[0]['request_id'] == '1:2:3'
    assert requests[0]['response_id'] == 'reply-id'
    assert requests[0]['gateway_request_id'] == gateway_id
    assert requests[0]['upstream_request_id'] == upstream_id
    assert thaw_request_summary(freeze_requests(requests))['requests'] == requests


@pytest.mark.parametrize('bad', [True, -1, 1.5, '7', float('nan'), float('inf'), 10_000_001])
def test_invalid_tokens_remain_unknown(bad):
    p = provider(Transport(response(payload(usage={'prompt_tokens': bad, 'completion_tokens': 0}))))
    result = p.complete([], [])
    assert result.metadata['usage']['input_tokens'] is None
    assert result.metadata['usage']['output_tokens'] == 0
    assert result.metadata['cost'] is None


def test_decimal_pricing_and_missing_detail_are_explicit():
    from fleet_core.request_metadata import normalized_tokens, request_cost, validate_pricing
    rates = {'currency': 'USD', 'input': '1', 'output': '6', 'cache_read': '.1', 'cache_write': '1.25'}
    usage = normalized_tokens({'input_tokens': 147, 'output_tokens': 14,
        'cache_read_tokens': 100, 'cache_write_tokens': 0, 'reasoning_tokens': 0})
    cost = request_cost(usage, pricing=rates)
    assert cost['amount'] == '0.000141'
    assert cost['source'] == 'configured'
    assert sum(float(x['amount']) for x in cost['items']) == pytest.approx(.000141)
    usage['cache_read_tokens'] = None
    assert request_cost(usage, pricing=rates) is None
    assert request_cost(usage, reported=0)['amount'] == '0'
    for bad in ('NaN', '-1', True, '1e999'):
        with pytest.raises(ValueError):
            validate_pricing({**rates, 'input': bad})


def test_sse_utf8_split_usage_tail_and_iterator_closed():
    events = []
    text = 'data: ' + json.dumps({'id':'stream-id','model':'stream-model', 'choices':[{'delta':{'content':'你好'},'finish_reason':'stop'}]}, ensure_ascii=False) + '\n\n'
    text += 'data: ' + json.dumps({'choices':[], 'usage':{'prompt_tokens':47,'completion_tokens':14,
        'prompt_tokens_details':{'cached_tokens':0, 'cache_write_tokens':0},
        'completion_tokens_details':{'reasoning_tokens':0}, 'cost':0.000131}}) + '\n\n'
    text += 'data: [DONE]\n\n'
    closed = []
    def chunks():
        try:
            for byte in text.encode():
                yield bytes([byte])
        finally:
            closed.append(True)
    transport = Transport(TransportResponse(200, {}, chunks()))
    result = provider(transport, stream=True).complete([], [], request_observer=lambda kind,data: events.append((kind,data)))
    assert result.text == '你好'
    assert result.metadata['usage']['input_tokens'] == 47
    assert result.metadata['model'] == 'stream-model'
    assert result.metadata['cost']['amount'] == '0.000131'
    assert json.loads(transport.calls[0]['body'])['stream_options'] == {'include_usage': True}
    assert closed == [True]


def test_stream_failure_is_recorded_closed_and_never_retried():
    events, closed = [], []
    def chunks():
        try:
            yield b'data: invalid-json\n\n'
        finally:
            closed.append(True)
    transport = Transport(TransportResponse(200, {}, chunks()))
    with pytest.raises(ProviderError, match='invalid_response'):
        provider(transport, stream=True, max_retries=3).complete([], [], request_observer=lambda kind,data: events.append((kind,data)))
    assert len(transport.calls) == 1
    assert events[-1][1]['provider_error'] == 'invalid_response'
    assert events[-1][1]['cost'] is None
    assert closed == [True]


@pytest.mark.parametrize('content', ['text', 'tool'])
@pytest.mark.parametrize('ending', ['done', 'eof', 'finish-then-done'])
def test_inband_stream_error_rejects_partial_response_without_replay(content, ending):
    events, closed = [], []
    delta = ({'content': 'partial public answer'} if content == 'text' else
        {'tool_calls': [{'index': 0, 'id': 'partial-call', 'function': {
            'name': 'workspace_list', 'arguments': '{}'}}]})
    def chunks():
        try:
            yield ('data: ' + json.dumps({'choices': [{'delta': delta}]}) + '\n\n').encode()
            if ending == 'finish-then-done':
                yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            yield b'data: {"error":{"code":"system_cpu_overloaded","message":"private-stream-error-sentinel"}}\n\n'
            if ending != 'eof':
                yield b'data: [DONE]\n\n'
        finally:
            closed.append(True)
    transport = Transport(TransportResponse(200, {'X-Oneapi-Request-Id': 'gateway-stream-error'}, chunks()))
    with pytest.raises(ProviderError) as caught:
        provider(transport, stream=True, max_retries=3).complete([], [{'name': 'workspace.list'}],
            request_observer=lambda k, d: events.append((k, d)))
    assert caught.value.code == 'provider_stream_error' and caught.value.retryable is False
    assert caught.value.diagnostic()['upstream_code'] == 'system_cpu_overloaded'
    finished = events[-1][1]
    assert finished['status'] == 'unknown' and finished['http_status'] == 200
    assert finished['provider_error'] == 'provider_stream_error'
    assert finished['gateway_request_id'] == 'gateway-stream-error'
    assert len(transport.calls) == 1 and closed == [True]
    if content == 'text':
        assert ''.join(d['text'] for k, d in events if k == 'assistant_text_delta') == 'partial public answer'
    assert 'private-stream-error-sentinel' not in json.dumps(events) + str(caught.value)


@pytest.mark.parametrize('error', ['private-code', {}, [], True,
    {'code': 'private-code'}, {'code': []}])
def test_inband_stream_error_does_not_publish_unrecognized_error_details(error):
    events = []
    body = ('data: ' + json.dumps({'error': error}) + '\n\ndata: [DONE]\n\n').encode()
    with pytest.raises(ProviderError) as caught:
        provider(Transport(TransportResponse(200, {}, body)), stream=True).complete([], [],
            request_observer=lambda k, d: events.append((k, d)))
    assert caught.value.code == 'provider_stream_error'
    assert 'upstream_code' not in caught.value.diagnostic()
    assert events[-1][1]['status'] == 'unknown'
    assert 'private-code' not in json.dumps(events) + str(caught.value)


def test_request_observer_failure_does_not_dispatch_or_replay():
    transport = Transport(response(payload()))
    def reject(kind, data):
        raise RuntimeError('event store unavailable')
    with pytest.raises(RuntimeError):
        provider(transport, max_retries=3).complete([], [], request_observer=reject)
    assert transport.calls == []


def test_request_timing_uses_monotonic_clock():
    from hub.platform.providers.observation import RequestObservation
    ticks = iter([10, 12.988])
    events = []
    trace = RequestObservation(lambda kind,data: events.append(data), model='m', provider='test', clock=lambda: 123,
                               monotonic=lambda: next(ticks))
    trace.finish()
    assert events[-1]['duration_ms'] == 2988
    assert events[-1]['started_at'] == 123


def test_sse_final_event_without_blank_line_is_not_dropped():
    raw = b'data: {"choices":[{"delta":{"content":"done"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":2}}'
    result = provider(Transport(TransportResponse(200, {}, [raw])), stream=True).complete([], [])
    assert result.text == 'done' and result.usage['output_tokens'] == 2


def test_sse_done_stops_consumption_and_closes_instead_of_waiting():
    closed = []
    def chunks():
        try:
            yield b'data: {"choices":[{"delta":{"content":"done"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
            raise AssertionError('transport must not read after DONE')
        finally:
            closed.append(True)
    result = provider(Transport(TransportResponse(200, {}, chunks())), stream=True).complete([], [])
    assert result.text == 'done' and closed == [True]


def test_sse_truncated_partial_answer_is_not_success():
    raw = b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
    with pytest.raises(ProviderError, match='invalid_response'):
        provider(Transport(TransportResponse(200, {}, [raw])), stream=True).complete([], [])


def test_equal_cache_rates_allow_cost_without_inventing_token_counts():
    from fleet_core.request_metadata import normalized_tokens, request_cost
    usage = normalized_tokens({'input_tokens':100, 'output_tokens':10, 'cache_read_tokens':50})
    result = request_cost(usage, pricing={'currency':'USD','input':'2','output':'10','cache_read':'.2','cache_write':'2'})
    assert result['amount'] == '0.00021'
    assert result['items'][0]['includes_unspecified_cache'] is True
    assert 'cache_write' not in [x['category'] for x in result['items']]
    assert usage['cache_write_tokens'] is None


def test_gateway_cache_creation_alias_and_explicit_duration_totals():
    for details, extras, expected in [
        ({'cached_tokens':20,'cached_creation_tokens':30},{},30),
        ({'cached_tokens':20},{'claude_cache_creation_5_m_tokens':0,'claude_cache_creation_1_h_tokens':0},0),
    ]:
        result = provider(Transport(response(payload(usage={'prompt_tokens':100,'completion_tokens':10,
            'prompt_tokens_details':details,**extras})))).complete([], [])
        assert result.metadata['usage']['cache_write_tokens'] == expected


def test_unsupported_cache_tier_does_not_get_uniform_estimate():
    result = provider(Transport(response(payload(usage={'prompt_tokens':100,'completion_tokens':10,
        'prompt_tokens_details':{'cached_tokens':0,'cached_creation_tokens':20},
        'claude_cache_creation_1_h_tokens':20}))),
        pricing={'currency':'USD','input':'5','output':'25','cache_read':'.5','cache_write':'6.25'}).complete([], [])
    assert result.metadata['cost'] is None
    assert result.metadata['cost_unavailable_reason'] == 'unsupported_cache_tier'


@pytest.mark.parametrize('failure', [TimeoutError, OSError])
def test_accepted_nonstream_read_failure_is_closed_and_never_retried(monkeypatch, failure):
    from hub.platform.providers import openai_compatible as adapter
    sent, closed, events = [], [], []
    class Accepted:
        status, headers = 200, {}
        def read1(self, size):
            raise failure('body interrupted after HTTP 200')
        def close(self):
            closed.append(True)
    def send(*args, **kwargs):
        sent.append(True)
        return Accepted()
    monkeypatch.setattr(adapter, 'open_http', send)
    p = adapter.OpenAICompatibleProvider(model='fixture', endpoint='http://fixture/v1/chat/completions',
        api_key='fixture', allow_network=True, max_retries=2, sleeper=lambda _: None)
    with pytest.raises(ProviderError):
        p.complete([], [], request_observer=lambda kind,data: events.append((kind,data)))
    assert len(sent) == 1
    assert closed == [True]
    assert events[-1][1]['status'] == 'unknown'
    assert events[-1][1]['http_status'] == 200
