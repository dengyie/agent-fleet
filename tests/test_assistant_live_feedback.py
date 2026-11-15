import json

import pytest

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


def test_reasoning_phase_is_visible_before_text_without_publishing_raw_reasoning():
    observed = []
    private = 'raw-reasoning-sentinel'
    def body():
        for _ in range(5):
            yield ('data: ' + json.dumps({'choices': [{'delta': {
                'reasoning_content': private}}]}) + '\n\n').encode()
        activities = [p for k, p in observed if k == 'assistant_activity']
        assert activities == [{'phase': 'thinking', 'request_index': 1}]
        yield _frame('公开回复')
        assert [p['phase'] for k, p in observed if k == 'assistant_activity'] == ['thinking', 'responding']
        yield b'data: [DONE]\n\n'
    provider = OpenAICompatibleProvider(model='test', endpoint='http://localhost', api_key='x',
        stream=True, transport=FakeTransport([TransportResponse(200, {}, body())]))
    result = provider.complete([], [], request_observer=lambda k, p: observed.append((k, p)))
    assert result.text == '公开回复'
    assert private not in json.dumps(observed, ensure_ascii=False) + json.dumps(result.__dict__, ensure_ascii=False)


def test_tool_phase_is_visible_before_arguments_finish():
    observed = []
    def body():
        yield b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"workspace_list","arguments":"{"}}]}}]}\n\n'
        assert [p['phase'] for k, p in observed if k == 'assistant_activity'] == ['tool_call']
        yield b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"}"}}]},"finish_reason":"tool_calls"}]}\n\n'
        yield b'data: [DONE]\n\n'
    provider = OpenAICompatibleProvider(model='test', endpoint='http://localhost', api_key='x',
        stream=True, transport=FakeTransport([TransportResponse(200, {}, body())]))
    result = provider.complete([], [{'name': 'workspace.list'}],
        request_observer=lambda k, p: observed.append((k, p)))
    assert result.kind == 'tool_call' and result.tool == 'workspace.list'


def test_activity_projection_tracks_request_retry_and_terminal_lifecycle():
    from hub.domain.run_progress import project_progress
    progress = project_progress({}, 'provider_request_started', {'step': 1, 'request_index': 1}, 1)
    assert progress['activity'] == {'phase': 'waiting', 'step': 1, 'request_index': 1}
    progress = project_progress(progress, 'assistant_activity', {
        'step': 1, 'request_index': 1, 'phase': 'thinking', 'reasoning_content': 'private'}, 2)
    assert progress['activity']['phase'] == 'thinking'
    assert 'private' not in json.dumps(progress)
    progress = project_progress(progress, 'assistant_activity', {'step': 1, 'phase': 'arbitrary-private'}, 3)
    assert progress['activity']['phase'] == 'thinking'
    progress = project_progress(progress, 'provider_request_finished', {'step': 1, 'status': 'failed'}, 4)
    assert 'activity' not in progress
    progress = project_progress(progress, 'provider_retry_scheduled', {'step': 1, 'retry_index': 1}, 5)
    progress = project_progress(progress, 'provider_request_started', {'step': 1, 'request_index': 2}, 6)
    assert progress['activity'] == {'phase': 'waiting', 'step': 1, 'request_index': 2}
    assert progress['retry']['retry_index'] == 1
    progress = project_progress(progress, 'assistant_text_delta', {'step': 1, 'offset': 0, 'text': '回复'}, 7)
    assert progress['activity']['phase'] == 'responding'
    assert 'retry' not in progress
    progress = project_progress(progress, 'run_finished', {'state': 'cancelled'}, 8)
    assert 'activity' not in progress
    assert progress['segments'][0]['text'] == '回复'


def test_thinking_heartbeat_is_throttled_and_phase_changes_are_immediate(monkeypatch):
    from types import SimpleNamespace
    from hub.platform.providers import observation
    now, observed = [0.0], []
    monkeypatch.setattr(observation, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    stream = observation.TextObservation(lambda k, p: observed.append((k, p)), 2)
    stream.activity('thinking')
    for _ in range(1000):
        stream.activity('thinking')
    assert len(observed) == 1
    now[0] = 1.1
    stream.activity('thinking')
    stream.activity('tool_call')
    assert [p['phase'] for k, p in observed] == ['thinking', 'thinking', 'tool_call']


def test_cancel_during_thinking_closes_stream_without_waiting_for_text(tmp_path):
    from hub.application.run_worker_service import LocalRunWorkerService
    from tests.test_platform_run_worker import OWNER, _app, _catalog, _queued
    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    run_id = _queued(app)['run_id']
    closed = []
    def body():
        try:
            repo.cancel_run(OWNER, run_id, now=100)
            yield b'data: {"choices":[{"delta":{"reasoning_content":"raw-reasoning-cancel-sentinel"}}]}\n\n'
            raise AssertionError('cancelled reasoning must not wait for a public text delta')
        finally:
            closed.append(True)
    transport = FakeTransport([TransportResponse(200, {}, body())])
    provider = OpenAICompatibleProvider(model='test', endpoint='http://localhost', api_key='x',
        stream=True, max_retries=3, transport=transport)
    LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'],
        provider_factory=lambda _: provider).run_once(OWNER)
    public = app.test_client().get(f'/api/platform/v1/runs/{run_id}').get_json()
    assert public['state'] == 'cancelled' and 'activity' not in public['progress']
    assert 'raw-reasoning-cancel-sentinel' not in json.dumps(repo.list_run_events(OWNER, run_id)) + json.dumps(public)
    assert closed == [True] and len(transport.calls) == 1


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


def test_short_long_run_progress_discloses_segment_retention():
    from hub.domain.run_progress import project_progress
    progress = {}
    for step in range(1, 151):
        progress = project_progress(progress, 'assistant_response', {
            'step': step, 'text': f'step {step}', 'response_kind': 'tool_call'}, step)
    assert len(progress['segments']) == 128
    assert progress['segments'][0]['step'] == 23
    assert progress['segments'][-1]['step'] == 150
    assert progress['truncated'] is True


def test_single_long_response_discloses_text_retention():
    from hub.domain.run_progress import project_progress
    text = '中' * 9000
    progress = project_progress({}, 'assistant_response', {
        'step': 1, 'text': text, 'response_kind': 'tool_call'}, 1)
    assert len(progress['segments']) == 1
    assert progress['segments'][0]['text'] != text
    assert progress['truncated'] is True

    streaming = {}
    for offset in range(0, len(text), 1000):
        streaming = project_progress(streaming, 'assistant_text_delta', {
            'step': 1, 'offset': offset, 'text': text[offset:offset + 1000]}, offset + 1)
    assert streaming['segments'][0]['text'] == progress['segments'][0]['text']
    assert streaming['truncated'] is True


@pytest.mark.parametrize('text', ['x' * 25000, '中' * 9000, '\x00' * 9000],
                         ids=['ascii-byte-bound', 'utf8-byte-bound', 'json-escape-bound'])
@pytest.mark.parametrize('restart', [False, True])
def test_worker_discloses_response_clipping_through_public_history(tmp_path, monkeypatch, text, restart):
    from hub.application.run_worker_service import LocalRunWorkerService
    from hub.platform.providers.base import ModelResponse
    from hub.platform.providers.openai_compatible import ProviderError
    from tests.test_native_run_checkpoints import Crash
    from tests.test_platform_run_worker import OWNER, _app, _catalog, _queued

    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    run_id = _queued(app)['run_id']
    calls = []

    class Provider:
        def complete(self, messages, tools, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                return ModelResponse(kind='tool_call', text=text, tool='workspace.list', arguments={})
            raise ProviderError('transient_http', status=503)

    def worker(name):
        return LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'],
            worker_id=name, provider_factory=lambda _: Provider())

    if restart:
        original = repo.save_runtime_checkpoint

        def crash_after_response(*args, **kwargs):
            original(*args, **kwargs)
            if args[2]['phase'] == 'response':
                raise Crash()

        monkeypatch.setattr(repo, 'save_runtime_checkpoint', crash_after_response)
        with pytest.raises(Crash):
            worker('before').run_once(OWNER)
        monkeypatch.setattr(repo, 'save_runtime_checkpoint', original)
        with repo._connect() as conn:
            conn.execute('UPDATE runs SET lease_expires_at=0 WHERE run_id=?', (run_id,))

    worker('after').run_once(OWNER)
    public = app.test_client().get(f'/api/platform/v1/runs/{run_id}').get_json()
    assert public['state'] == 'unknown'
    assert len(calls) == 2  # Recovery reuses the full committed response.
    assert public['progress']['segments'][0]['text'] != text
    assert public['progress'].get('truncated') is True
    response_event = next(event for event in repo.list_run_events(OWNER, run_id)['events']
                          if event['kind'] == 'assistant_response')
    assert response_event['payload']['truncated'] is True
    assert len(json.dumps(response_event['payload'], ensure_ascii=False).encode()) < 64 * 1024
    # Event retention and conversation hydration keep the same public disclosure.
    with repo._connect() as conn:
        conn.execute('DELETE FROM run_events WHERE run_id=?', (run_id,))
    history = app.test_client().get('/api/platform/v1/conversations/' + public['conversation_id']).get_json()
    assert history['conversation']['runs'][0]['progress'] == public['progress']


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


@pytest.mark.parametrize('text', ['x' * 25000, '中' * 9000], ids=['ascii', 'utf8'])
@pytest.mark.parametrize('failure', ['disconnect', 'error-frame'])
def test_interrupted_stream_discloses_provider_text_clipping_in_history(tmp_path, text, failure):
    from hub.bootstrap import create_app
    from hub.config import FleetConfig
    from hub.infrastructure.platform_retention import DAY_S, PlatformRetention
    from tests.test_request_metadata_api import setup, OWNER
    app, repo, client, conversation, run = setup(tmp_path)
    closed = []
    def body():
        try:
            yield _frame(text)
            if failure == 'disconnect':
                raise OSError('private-interruption')
            yield b'data: {"error":{"message":"private-interruption"}}\n\ndata: [DONE]\n\n'
        finally:
            closed.append(True)
    transport = FakeTransport([TransportResponse(200, {}, body())])
    worker = app.extensions['fleet']['services']['platform_worker']
    worker.provider_factory = lambda _: OpenAICompatibleProvider(model='fixture', endpoint='http://fixture',
        api_key='fixture', stream=True, max_retries=3, transport=transport)
    assert worker.run_once(OWNER)['state'] == 'unknown'
    before = client.get(f"/api/platform/v1/runs/{run['run_id']}").get_json()
    progress = before['progress']
    assert progress.get('truncated') is True
    assert progress['segments'][0]['text'] == text.encode('utf-8')[:24576].decode('utf-8', errors='ignore')
    assert len(transport.calls) == 1 and closed == [True]
    events = repo.list_run_events(OWNER, run['run_id'])['events']
    assert any(e['kind'] == 'assistant_text_delta' and e['payload'].get('truncated') for e in events)
    assert 'private-interruption' not in json.dumps(before) + json.dumps(events)
    assert PlatformRetention(next(tmp_path.rglob('platform.db')))(now=50_000 * DAY_S)['run_events'] > 0
    restarted = create_app(FleetConfig.from_root(tmp_path, dev_operator=OWNER, platform_enabled=True))
    history = restarted.test_client().get(
        f"/api/platform/v1/conversations/{conversation['conversation_id']}").get_json()['conversation']
    assert history['runs'][0]['progress'] == progress


def test_cancel_after_public_text_limit_still_checks_stream_activity(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from hub.platform.providers import observation
    from tests.test_request_metadata_api import setup, OWNER
    now, closed = [0.0], []
    monkeypatch.setattr(observation, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    app, repo, client, _, run = setup(tmp_path)
    def body():
        try:
            yield _frame('x' * 24577)
            repo.cancel_run(OWNER, run['run_id'], now=100)
            now[0] = 1.1
            yield _frame('text beyond the presentation limit')
            raise AssertionError('cancelled stream must not keep reading after the text limit')
        finally:
            closed.append(True)
    transport = FakeTransport([TransportResponse(200, {}, body())])
    worker = app.extensions['fleet']['services']['platform_worker']
    worker.provider_factory = lambda _: OpenAICompatibleProvider(model='fixture', endpoint='http://fixture',
        api_key='fixture', stream=True, max_retries=3, transport=transport)
    assert worker.run_once(OWNER)['state'] == 'cancelled'
    public = client.get(f"/api/platform/v1/runs/{run['run_id']}").get_json()
    assert public['progress']['segments'][0]['text'] == 'x' * 24576
    assert len(transport.calls) == 1 and closed == [True]


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


def test_tool_progress_explains_operation_and_result_and_survives_retention(tmp_path):
    from hub.application.run_worker_service import LocalRunWorkerService
    from hub.platform.providers.base import ModelResponse
    from tests.test_platform_run_worker import OWNER, _app, _catalog, _queued
    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    run_id = _queued(app)['run_id']
    (tmp_path / 'workspace').mkdir()
    (tmp_path / 'workspace' / 'report.txt').write_text('readable result')
    calls = []
    class Provider:
        def complete(self, messages, tools, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                return ModelResponse(kind='tool_call', tool='workspace.read', arguments={'path':'report.txt','api_key':'hidden'})
            return ModelResponse(kind='final', text='done')
    LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'],provider_factory=lambda _:Provider()).run_once(OWNER)
    segment = repo.get_run(OWNER,run_id)['progress']['segments'][1]
    assert segment['title'] == '读取文件 · report.txt'
    assert segment['input'] == 'report.txt' and segment['output'] == 'readable result'
    assert 'hidden' not in json.dumps(segment)
    with repo._connect() as conn:
        conn.execute('DELETE FROM run_events WHERE run_id=?',(run_id,))
    assert repo.get_run(OWNER,run_id)['progress']['segments'][1] == segment


def test_exec_activity_is_available_before_dispatch_and_result_is_bounded():
    from hub.domain.tool_activity import tool_activity
    from hub.domain.run_progress import project_progress
    from fleet_core.backends.base import ToolReceipt
    args = {'argv':['sh','-c','python3 -V && pwd'], 'unrelated_secret':'never-public'}
    call = tool_activity('workspace.exec', args)
    assert call['title'].startswith('执行命令 · sh -c')
    assert 'python3 -V && pwd' in call['input'] and 'never-public' not in str(call)
    receipt = ToolReceipt('cmd','failed',{'returncode':7,'output':'中'*10000},'process_failed',True)
    result = tool_activity('workspace.exec',args,receipt)
    assert '退出码：7' in result['output'] and '截断' in result['output']
    assert len(result['output'].encode()) < 3584
    public = project_progress({},'tool_call',{'command_id':'cmd','tool':'workspace.exec',**call},1)
    public = project_progress(public,'tool_result',{'command_id':'cmd','tool':'workspace.exec','state':'failed','error_code':'process_failed',**result},2)
    assert public['segments'][0]['input']==call['input']
    assert public['segments'][0]['output']==result['output']
