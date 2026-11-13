import json

from hub.platform.providers.openai_compatible import OpenAICompatibleProvider, TransportResponse
from tests.test_platform_provider import FakeTransport, _response


def _frame(text):
    return ('data: ' + json.dumps({'choices': [{'delta': {'content': text}}]}) + '\n\n').encode()


def test_text_delta_is_observed_before_upstream_finishes():
    observed = []
    def body():
        yield _frame('正在检查')
        assert any(k == 'assistant_text_delta' and p['text'] == '正在检查' for k, p in observed)
        yield _frame('工作区')
        yield b'data: [DONE]\n\n'
    provider = OpenAICompatibleProvider(model='test', endpoint='http://localhost', api_key='x',
        stream=True, transport=FakeTransport([TransportResponse(200, {}, body())]))
    result = provider.complete([], [], request_observer=lambda k, p: observed.append((k, p)))
    assert result.text == '正在检查工作区'
    assert ''.join(p['text'] for k, p in observed if k == 'assistant_text_delta') == result.text


def test_transient_retries_are_visible_before_wait_and_keep_one_payload():
    observed, waits = [], []
    def sleep(delay):
        assert observed[-1][0] == 'provider_retry_scheduled'
        waits.append(delay)
    transport = FakeTransport([_response({}, status=503), _response({}, status=503),
        _response({'choices': [{'message': {'content': 'recovered'}}]})])
    provider = OpenAICompatibleProvider(model='test', endpoint='http://localhost', api_key='x',
        max_retries=3, transport=transport, sleeper=sleep)
    result = provider.complete([], [], request_observer=lambda k, p: observed.append((k, p)))
    assert result.text == 'recovered'
    retries = [p for k, p in observed if k == 'provider_retry_scheduled']
    assert [p['retry_index'] for p in retries] == [1, 2]
    assert all(p['max_retries'] == 3 and p['provider_status'] == 503 for p in retries)
    assert len(waits) == 2 and waits[1] > waits[0]
    assert len({call['body'] for call in transport.calls}) == 1


def test_narration_is_persisted_before_tool_and_survives_later_provider_failure(tmp_path):
    from hub.application.run_worker_service import LocalRunWorkerService
    from hub.platform.providers.base import ModelResponse
    from tests.test_platform_run_worker import OWNER, _app, _catalog, _queued
    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    run_id = _queued(app)['run_id']
    calls = []
    class Provider:
        def complete(self, messages, tools, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                return ModelResponse(kind='tool_call', text='我来查看工作区', tool='workspace.list', arguments={})
            from hub.platform.providers.openai_compatible import ProviderError
            raise ProviderError('transient_http', status=503)
    worker = LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'],
        provider_factory=lambda _: Provider())
    worker.run_once(OWNER)
    public = app.test_client().get(f'/api/platform/v1/runs/{run_id}').get_json()
    assert public['state'] == 'unknown'
    segments = public['progress']['segments']
    assert segments[0]['text'] == '我来查看工作区'
    assert segments[1]['tool'] == 'workspace.list' and segments[1]['state'] == 'succeeded'
    events = repo.list_run_events(OWNER, run_id)['events']
    kinds = [e['kind'] for e in events]
    assert kinds.index('assistant_response') < kinds.index('tool_call')
    # Projection survives event retention, independently of private checkpoints.
    with repo._connect() as conn:
        conn.execute('DELETE FROM run_events WHERE run_id=?', (run_id,))
    assert repo.get_run(OWNER, run_id)['progress']['segments'] == segments
    conversation = app.test_client().get('/api/platform/v1/conversations/' + public['conversation_id']).get_json()
    assert conversation['conversation']['runs'][0]['progress']['segments'] == segments


def test_public_progress_is_bounded_and_idempotent():
    from hub.domain.run_progress import project_progress, MAX_PROGRESS_BYTES
    progress = {}
    for step in range(30):
        progress = project_progress(progress, 'assistant_response', {
            'step': step, 'text': '中' * 16000, 'response_kind': 'tool_call', 'api_key': 'private'}, step)
        assert len(json.dumps(progress, ensure_ascii=False).encode()) <= MAX_PROGRESS_BYTES
    completed = progress['segments'][-1].copy()
    progress = project_progress(progress, 'assistant_text_delta', {'step':29, 'offset':0, 'text':'duplicate'},31)
    assert progress['segments'][-1] == completed
    assert 'private' not in json.dumps(progress)
    assert progress['truncated'] is True


def test_run_stream_is_owner_scoped_and_releases_capacity(tmp_path):
    from tests.test_platform_run_worker import OWNER, _app, _catalog, _queued
    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    run_id = _queued(app)['run_id']
    repo.append_run_event(OWNER, run_id, 'assistant_text_delta',
        {'step':1, 'offset':0, 'text':'实时文字'}, now=1)
    client = app.test_client()
    assert client.get(f'/api/platform/v1/runs/{run_id}/stream?after=-1').status_code == 400
    response = client.get(f'/api/platform/v1/runs/{run_id}/stream', buffered=False)
    assert response.status_code == 200
    assert 'text/event-stream' in response.content_type
    first = next(response.response).decode()
    assert '实时文字' in first and 'runtime_checkpoint' not in first
    assert app.extensions['observe_http']['sse'].active == 1
    response.close()
    assert app.extensions['observe_http']['sse'].active == 0
    repo.set_run_state(OWNER, run_id, 'succeeded', now=2, result_text='完成')
    done = client.get(f'/api/platform/v1/runs/{run_id}/stream', buffered=True)
    assert '完成' in done.get_data(as_text=True)
    assert app.extensions['observe_http']['sse'].active == 0
    repo.create_conversation('other@example.test', 'other-conv', title='', workspace_id=None)
    repo.append_turn('other@example.test', 'other-conv', 'other-message', 'other-run',
        text='private', client_token='private', config_snapshot={}, now=3)
    assert client.get('/api/platform/v1/runs/other-run/stream').status_code == 404


def test_partial_stream_failure_flushes_pending_text_without_replaying():
    import pytest
    from hub.platform.providers.openai_compatible import ProviderError
    observed = []
    def body():
        yield _frame('第一段')
        yield _frame('未完成')
        raise OSError('lost upstream')
    transport = FakeTransport([TransportResponse(200, {}, body())])
    provider = OpenAICompatibleProvider(model='test', endpoint='http://localhost', api_key='x',
        stream=True, max_retries=3, transport=transport)
    with pytest.raises(ProviderError):
        provider.complete([], [], request_observer=lambda k, p: observed.append((k,p)))
    assert ''.join(p['text'] for k,p in observed if k == 'assistant_text_delta') == '第一段未完成'
    assert len(transport.calls) == 1

def test_cancel_during_retry_does_not_dispatch_again(tmp_path):
    from hub.application.run_worker_service import LocalRunWorkerService
    from tests.test_platform_run_worker import OWNER, _app, _catalog, _queued
    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    run_id = _queued(app)['run_id']
    transport = FakeTransport([_response({}, status=503), _response({'choices':[{'message':{'content':'should not run'}}]})])
    provider = OpenAICompatibleProvider(model='test', endpoint='http://localhost', api_key='x',
        max_retries=3, transport=transport, sleeper=lambda _:repo.cancel_run(OWNER,run_id,now=100))
    worker = LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'], provider_factory=lambda _:provider)
    worker.run_once(OWNER)
    assert repo.get_run(OWNER,run_id)['state'] == 'cancelled'
    assert len(transport.calls) == 1


def test_cancel_during_stream_preserves_text_and_closes_body(tmp_path):
    from hub.application.run_worker_service import LocalRunWorkerService
    from tests.test_platform_run_worker import OWNER, _app, _catalog, _queued
    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    run_id = _queued(app)['run_id']
    closed = []
    def body():
        try:
            repo.cancel_run(OWNER,run_id,now=100)
            yield _frame('保留已收到的文字')
            raise AssertionError('cancelled stream must stop reading')
        finally:
            closed.append(True)
    transport = FakeTransport([TransportResponse(200,{},body())])
    provider = OpenAICompatibleProvider(model='test', endpoint='http://localhost', api_key='x',
        stream=True, max_retries=3, transport=transport)
    worker = LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'], provider_factory=lambda _:provider)
    worker.run_once(OWNER)
    run = repo.get_run(OWNER,run_id)
    assert run['state'] == 'cancelled'
    assert run['progress']['segments'][0]['text'] == '保留已收到的文字'
    assert len(transport.calls) == 1 and closed == [True]

def test_run_stream_delivers_updates_after_initial_snapshot(tmp_path):
    from tests.test_platform_run_worker import OWNER, _app, _catalog, _queued
    app = _app(tmp_path)
    repo = _catalog(app,tmp_path)
    run_id = _queued(app)['run_id']
    repo.append_run_event(OWNER,run_id,'assistant_text_delta',{'step':1,'offset':0,'text':'第一段'},now=1)
    with app.test_client().get(f'/api/platform/v1/runs/{run_id}/stream',buffered=False) as response:
        assert '第一段' in next(response.response).decode()
        repo.append_run_event(OWNER,run_id,'assistant_text_delta',{'step':1,'offset':3,'text':'继续'},now=2)
        assert '第一段继续' in next(response.response).decode()
    assert app.extensions['observe_http']['sse'].active == 0

def test_retry_notice_remains_visible_during_the_retry_request():
    from hub.domain.run_progress import project_progress
    progress=project_progress({},'provider_retry_scheduled',{'step':2,'retry_index':1,'max_retries':3,'provider_status':503},1)
    progress=project_progress(progress,'provider_request_started',{'step':2,'request_index':2},2)
    assert progress['retry']['retry_index']==1
    progress=project_progress(progress,'assistant_response',{'step':2,'text':'已恢复','response_kind':'final'},3)
    assert 'retry' not in progress

def test_progress_bound_handles_json_escaping_without_hanging():
    import subprocess
    import sys
    code = ('import json; from hub.domain.run_progress import project_progress, MAX_PROGRESS_BYTES; '
        'p=project_progress({},"assistant_response",{"step":1,"text":chr(0)*16000},1); '
        'assert len(json.dumps(p,ensure_ascii=False).encode()) <= MAX_PROGRESS_BYTES; '
        'assert p["truncated"] is True')
    result = subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,timeout=3)
    assert result.returncode == 0, result.stderr
