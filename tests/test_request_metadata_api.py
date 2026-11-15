"""Real worker, SQLite and private API request history; no browser-derived totals."""
import json

import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.platform.providers.openai_compatible import OpenAICompatibleProvider, TransportResponse

OWNER = 'metadata@example.test'
_MISSING = object()


def setup(tmp_path):
    app = create_app(FleetConfig.from_root(tmp_path, dev_operator=OWNER, platform_enabled=True, platform_worker_enabled=True))
    repo = app.extensions['fleet']['platform_repository']
    repo.upsert_model(OWNER, {'profile_id': 'model', 'provider':'openai_compatible', 'model':'frozen-model',
        'secret_ref':'env://FIXTURE', 'provider_config': {'endpoint':'https://fixture.invalid'}})
    repo.upsert_workspace(OWNER, {'workspace_id':'home', 'root_path':str(tmp_path / 'work')})
    repo.update_defaults(OWNER, {'model_profile_id':'model', 'workspace_id':'home'}, 0)
    client = app.test_client()
    conversation = client.post('/api/platform/v1/conversations', json={}).get_json()['conversation']
    run = client.post(f"/api/platform/v1/conversations/{conversation['conversation_id']}/turns",
        json={'text':'inspect', 'client_token':'one'}).get_json()['run']
    return app, repo, client, conversation, run


def test_requests_survive_restart_and_are_owner_scoped(tmp_path):
    app, repo, client, conversation, run = setup(tmp_path)
    class Transport:
        def request(self, **kwargs):
            return TransportResponse(200, {}, json.dumps({'model':'returned-model','id':'reply-one',
                'choices':[{'message':{'content':'done'}, 'finish_reason':'stop'}],
                'usage':{'prompt_tokens':47,'completion_tokens':14,'cost':.000131}}).encode())
    worker = app.extensions['fleet']['services']['platform_worker']
    worker.provider_factory = lambda profile: OpenAICompatibleProvider(model=profile['model'], endpoint='http://fixture',api_key='never-expose',transport=Transport())
    assert worker.run_once(OWNER)['state'] == 'succeeded'
    repo.upsert_model(OWNER, {'profile_id':'model','model':'changed-model','provider':'deterministic'})
    url = f"/api/platform/v1/runs/{run['run_id']}"
    value = client.get(url).get_json()
    assert value['config']['model'] == 'frozen-model'
    assert value['requests'][0]['model'] == 'returned-model'
    assert value['requests'][0]['requested_model'] == 'frozen-model'
    assert value['requests'][0]['cost']['amount'] == '0.000131'
    assert value['requests'][0]['request_id'] == '1:1:1'
    restarted = create_app(FleetConfig.from_root(tmp_path, dev_operator=OWNER, platform_enabled=True))
    assert restarted.test_client().get(url).get_json()['requests'] == value['requests']
    other = create_app(FleetConfig.from_root(tmp_path, dev_operator='other@example.test', platform_enabled=True))
    assert other.test_client().get(url).status_code == 404
    assert other.test_client().get(f"/api/platform/v1/conversations/{conversation['conversation_id']}").status_code == 404
    history = client.get(f"/api/platform/v1/conversations/{conversation['conversation_id']}").get_json()['conversation']
    assert history['runs'][0]['requests'] == value['requests']
    assert history['runs'][0]['trigger_message_id'] == history['messages'][0]['message_id']
    assert 'never-expose' not in json.dumps(history)


@pytest.mark.parametrize('succeeds', [True, False])
def test_gateway_request_ids_survive_worker_retry_retention_and_restart(tmp_path, succeeds):
    from hub.infrastructure.platform_retention import DAY_S, PlatformRetention
    app, _, client, conversation, run = setup(tmp_path)
    class Transport:
        calls = 0
        def request(self, **kwargs):
            self.calls += 1
            headers = {'X-Oneapi-Request-Id': f'gateway-{self.calls}',
                       'X-Upstream-Request-Id': f'upstream-{self.calls}'}
            return (TransportResponse(503, headers, b'{}') if self.calls == 1 or not succeeds else
                TransportResponse(200, headers, b'{"id":"reply-id","choices":[{"message":{"content":"done"}}]}'))
    worker = app.extensions['fleet']['services']['platform_worker']
    transport = Transport()
    worker.provider_factory = lambda profile: OpenAICompatibleProvider(model=profile['model'],
        endpoint='http://fixture', api_key='never-expose', transport=transport,
        max_retries=1, sleeper=lambda _: None)
    assert worker.run_once(OWNER)['state'] == ('succeeded' if succeeds else 'unknown')
    url = f"/api/platform/v1/runs/{run['run_id']}"
    before = client.get(url).get_json()
    requests = before['requests']
    assert [item.get('gateway_request_id') for item in requests] == ['gateway-1', 'gateway-2']
    assert [item.get('upstream_request_id') for item in requests] == ['upstream-1', 'upstream-2']
    assert [item['request_id'] for item in requests] == ['1:1:1', '1:1:2']
    assert requests[-1].get('response_id') == ('reply-id' if succeeds else None)
    assert before['request_totals']['count'] == 2 and transport.calls == 2
    assert PlatformRetention(next(tmp_path.rglob('platform.db')))(now=50_000 * DAY_S)['run_events'] > 0
    restarted = create_app(FleetConfig.from_root(tmp_path, dev_operator=OWNER, platform_enabled=True))
    after = restarted.test_client().get(url).get_json()
    assert after['requests'] == requests and after['request_totals'] == before['request_totals']
    history = restarted.test_client().get(
        f"/api/platform/v1/conversations/{conversation['conversation_id']}").get_json()['conversation']
    assert history['runs'][0]['requests'] == requests


@pytest.mark.parametrize('succeeds', [True, False])
def test_http_error_body_failure_keeps_worker_retry_history(tmp_path, monkeypatch, succeeds):
    from hub.infrastructure.platform_retention import DAY_S, PlatformRetention
    from hub.platform.providers import openai_compatible as adapter
    app, _, client, conversation, run = setup(tmp_path)
    calls, closed, waits = [], [], []
    class Response:
        def __init__(self, index):
            self.index, self.sent = index, False
            self.status = 200 if index == 2 and succeeds else 503
            self.headers = {'X-Oneapi-Request-Id': f'gateway-{index}'}
        def read(self, size):
            raise TimeoutError('private-error-body')
        def read1(self, size):
            if self.sent:
                return b''
            self.sent = True
            return b'{"choices":[{"message":{"content":"recovered"},"finish_reason":"stop"}]}'
        def close(self):
            closed.append(self.index)
    def send(*args, **kwargs):
        calls.append(True)
        return Response(len(calls))
    monkeypatch.setattr(adapter, 'open_http', send)
    worker = app.extensions['fleet']['services']['platform_worker']
    worker.provider_factory = lambda profile: OpenAICompatibleProvider(model=profile['model'],
        endpoint='http://fixture', api_key='never-expose', allow_network=True,
        max_retries=1, sleeper=waits.append)
    assert worker.run_once(OWNER)['state'] == ('succeeded' if succeeds else 'unknown')
    url = f"/api/platform/v1/runs/{run['run_id']}"
    before = client.get(url).get_json()
    assert [r['http_status'] for r in before['requests']] == [503, 200 if succeeds else 503]
    assert [r['gateway_request_id'] for r in before['requests']] == ['gateway-1', 'gateway-2']
    assert before['requests'][0]['provider_error'] == 'transient_http'
    assert before['request_totals']['count'] == 2 and closed == [1, 2] and waits == [.1]
    assert PlatformRetention(next(tmp_path.rglob('platform.db')))(now=50_000 * DAY_S)['run_events'] > 0
    restarted = create_app(FleetConfig.from_root(tmp_path, dev_operator=OWNER, platform_enabled=True))
    after = restarted.test_client().get(url).get_json()
    assert after['requests'] == before['requests'] and after['request_totals'] == before['request_totals']
    history = restarted.test_client().get(
        f"/api/platform/v1/conversations/{conversation['conversation_id']}").get_json()['conversation']
    assert history['runs'][0]['requests'] == before['requests']
    assert 'private-error-body' not in json.dumps(history)


@pytest.mark.parametrize('with_tool', [False, True], ids=['text', 'tool'])
def test_stream_error_preserves_partial_history_without_dispatching_tool(tmp_path, with_tool):
    from hub.infrastructure.platform_retention import DAY_S, PlatformRetention
    app, repo, client, conversation, run = setup(tmp_path)
    calls, closed = [], []
    delta = {'content': '已收到的公开说明'}
    if with_tool:
        delta['tool_calls'] = [{'index': 0, 'id': 'not-committed', 'function': {
            'name': 'workspace_write', 'arguments': '{"path":"must-not-write.txt","content":"no"}'}}]
    def chunks():
        try:
            yield ('data: ' + json.dumps({'choices': [{'delta': delta}]}) + '\n\n').encode()
            yield b'data: {"error":{"message":"private-stream-error","code":"system_cpu_overloaded"}}\n\n'
            yield b'data: [DONE]\n\n'
        finally:
            closed.append(True)
    class Transport:
        def request(self, **kwargs):
            calls.append(True)
            return TransportResponse(200, {'X-Oneapi-Request-Id': 'gateway-stream-error'}, chunks())
    worker = app.extensions['fleet']['services']['platform_worker']
    worker.provider_factory = lambda profile: OpenAICompatibleProvider(model=profile['model'],
        endpoint='http://fixture', api_key='never-expose', stream=True, transport=Transport(),
        max_retries=3, sleeper=lambda _: None)
    assert worker.run_once(OWNER)['state'] == 'unknown'
    url = f"/api/platform/v1/runs/{run['run_id']}"
    before = client.get(url).get_json()
    assert 'provider_stream_error' in before['result_text']
    assert before['progress']['segments'][0]['text'] == '已收到的公开说明'
    assert before['requests'][0]['provider_error'] == 'provider_stream_error'
    assert before['requests'][0]['gateway_request_id'] == 'gateway-stream-error'
    assert before['requests'][0]['status'] == 'unknown' and before['requests'][0]['http_status'] == 200
    assert len(calls) == 1 and closed == [True]
    events = repo.list_run_events(OWNER, run['run_id'])['events']
    assert not any(e['kind'] in {'tool_call', 'tool_result', 'assistant_response'} for e in events)
    assert not (tmp_path / 'work' / 'must-not-write.txt').exists()
    assert 'private-stream-error' not in json.dumps(events) + json.dumps(before)
    assert PlatformRetention(next(tmp_path.rglob('platform.db')))(now=50_000 * DAY_S)['run_events'] > 0
    restarted = create_app(FleetConfig.from_root(tmp_path, dev_operator=OWNER, platform_enabled=True))
    history = restarted.test_client().get(
        f"/api/platform/v1/conversations/{conversation['conversation_id']}").get_json()['conversation']
    assert history['runs'][0]['requests'] == before['requests']
    assert history['runs'][0]['progress'] == before['progress']


def test_pending_terminal_unknown_and_projection_queries_are_batched(tmp_path, monkeypatch):
    app, repo, client, conversation, run = setup(tmp_path)
    import time
    for step in range(1, 21):
        repo.append_run_event(OWNER, run['run_id'], 'provider_request_started',
            {'step':step,'attempt':1,'request_index':1,'status':'running','started_at':time.time(),
             'model':'frozen-model','raw_body':'private body'}, now=time.time())
    active = client.get(f"/api/platform/v1/runs/{run['run_id']}").get_json()['requests']
    assert len(active) == 20 and all(x['elapsed_ms'] >= 0 for x in active)
    repo.set_run_state(OWNER, run['run_id'], 'unknown', now=time.time())
    statements = []
    connect = repo._connect
    def traced():
        conn = connect(); conn.set_trace_callback(statements.append); return conn
    monkeypatch.setattr(repo, '_connect', traced)
    history = repo.get_conversation(OWNER, conversation['conversation_id'])
    requests = history['runs'][0]['requests']
    assert all(x['status'] == 'unknown' and x.get('duration_ms') is None for x in requests)
    assert all('raw_body' not in x for x in requests)
    assert len([q for q in statements if 'FROM run_events e' in q]) == 1


@pytest.mark.parametrize("started_at", [
    pytest.param(_MISSING, id="missing"), None, "not-a-timestamp", True, [], 10**1000,
])
def test_active_request_with_invalid_start_time_does_not_break_run_projection(
    tmp_path, started_at,
):
    _, repo, client, _, run = setup(tmp_path)
    payload = {'attempt': 1, 'step': 1, 'request_index': 1, 'status': 'running'}
    if started_at is not _MISSING:
        payload['started_at'] = started_at
    repo.append_run_event(OWNER, run['run_id'], 'provider_request_started', payload, now=1.0)

    response = client.get(f"/api/platform/v1/runs/{run['run_id']}")

    assert response.status_code == 200
    request = response.get_json()['requests'][0]
    assert request['status'] == 'running'
    assert 'started_at' not in request
    assert 'elapsed_ms' not in request


def test_model_pricing_is_validated_and_frozen(tmp_path):
    app, repo, client, _, _ = setup(tmp_path)
    rates = {'currency':'USD','input':'1','output':'6','cache_read':'.1','cache_write':'1.25'}
    model = {'profile_id':'priced','provider':'openai_compatible','model':'price-model', 'provider_config':{'pricing':rates}}
    repo.upsert_model(OWNER, model)
    import pytest
    with pytest.raises(Exception, match='invalid_model'):
        repo.upsert_model(OWNER, {**model,'provider_config':{'pricing':{**rates,'input':'NaN'}}})
    # The same resolver that freezes endpoint/model also freezes pricing.
    conversation = client.post('/api/platform/v1/conversations', json={}).get_json()['conversation']
    run = client.post(f"/api/platform/v1/conversations/{conversation['conversation_id']}/turns",
        json={'text':'inspect','client_token':'priced-turn','overrides':{'model_profile_id':'priced'}}).get_json()['run']
    snapshot = repo.get_run(OWNER, run['run_id'])['config_snapshot']['provider_snapshot']['provider_config']['pricing']
    repo.upsert_model(OWNER, {**model,'provider_config':{'pricing':{**rates,'input':'20'}}})
    assert snapshot['input'] == '1'
    assert repo.get_run(OWNER, run['run_id'])['config_snapshot']['provider_snapshot']['provider_config']['pricing'] == snapshot
