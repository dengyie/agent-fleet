"""Known read failures may be explained or corrected without losing the Run."""
import json

import pytest

from hub.application.run_worker_service import LocalRunWorkerService
from hub.platform.providers.base import ModelResponse
from hub.platform.runtime.native import NativeAssistantRuntime
from tests.test_native_run_checkpoints import Crash
from tests.test_platform_run_worker import OWNER, _app, _catalog, _queued


@pytest.mark.parametrize('tool', ['workspace.read', 'workspace.list'])
def test_missing_workspace_path_returns_to_model_and_preserves_failed_progress(tmp_path, tool):
    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    run = _queued(app)
    calls = []

    class Provider:
        def complete(self, messages, tools, **kwargs):
            calls.append(json.loads(json.dumps(messages)))
            if len(calls) == 1:
                return ModelResponse(kind='tool_call', tool=tool,
                    arguments={'path': 'missing'}, tool_call_id='read-missing',
                    usage={'total_tokens': 3})
            if len(calls) == 2:
                return ModelResponse(kind='tool_call', tool='workspace.list', arguments={},
                                     usage={'total_tokens': 4})
            return ModelResponse(kind='final', text='指定路径不存在，已检查根目录。',
                                 usage={'total_tokens': 5})

    worker = LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'],
                                  provider_factory=lambda _: Provider())
    result = worker.run_once(OWNER)
    assert result['state'] == 'succeeded'
    assert result['usage']['provider_requests'] == 3
    assert result['usage']['total_tokens'] == 12
    assert calls[1][-1] == {'role': 'tool', 'tool_call_id': 'read-missing', 'tool': tool,
                           'result': {'state': 'failed', 'error_code': 'not_found'}}
    assert calls[1][-2]['tool_calls'][0]['id'] == 'read-missing'
    events = repo.list_run_events(OWNER, run['run_id'])['events']
    receipts = [e['payload'] for e in events if e['kind'] == 'tool_result']
    assert [item['state'] for item in receipts] == ['failed', 'succeeded']
    assert receipts[0]['error_code'] == 'not_found'
    public = app.test_client().get('/api/platform/v1/runs/' + run['run_id']).get_json()
    failed = next(s for s in public['progress']['segments'] if s['kind'] == 'tool')
    assert failed['state'] == 'failed' and failed['error_code'] == 'not_found'


@pytest.mark.parametrize('phase', ['receipt', 'ready'])
def test_recovered_read_failure_resumes_without_reexecuting_tool(tmp_path, monkeypatch, phase):
    from hub.platform.tool_broker import ToolBroker

    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    run = _queued(app)
    model_calls, tool_calls = [], []
    execute = ToolBroker.execute

    def counted_execute(self, **kwargs):
        tool_calls.append(kwargs['command_id'])
        return execute(self, **kwargs)

    monkeypatch.setattr(ToolBroker, 'execute', counted_execute)

    class Provider:
        def complete(self, messages, tools, **kwargs):
            model_calls.append(json.loads(json.dumps(messages)))
            if len(model_calls) == 1:
                return ModelResponse(kind='tool_call', tool='workspace.read',
                    arguments={'path': 'missing'}, usage={'total_tokens': 3})
            return ModelResponse(kind='final', text='文件不存在。', usage={'total_tokens': 4})

    def worker(name):
        return LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'],
            worker_id=name, provider_factory=lambda _: Provider())

    save = repo.save_runtime_checkpoint

    def crash_after_save(*args, **kwargs):
        value = save(*args, **kwargs)
        if args[2]['phase'] == phase:
            raise Crash()
        return value

    monkeypatch.setattr(repo, 'save_runtime_checkpoint', crash_after_save)
    with pytest.raises(Crash):
        worker('before').run_once(OWNER)
    monkeypatch.setattr(repo, 'save_runtime_checkpoint', save)
    # If the saved read is replayed it would now succeed, changing its meaning.
    (tmp_path / 'workspace' / 'missing').write_text('created after crash', encoding='utf-8')
    with repo._connect() as conn:
        conn.execute('UPDATE runs SET lease_expires_at=0 WHERE run_id=?', (run['run_id'],))
    result = worker('after').run_once(OWNER)
    assert result['state'] == 'succeeded'
    assert len(model_calls) == 2 and len(tool_calls) == 1
    assert model_calls[-1][-1]['result'] == {'state': 'failed', 'error_code': 'not_found'}
    assert result['usage']['provider_requests'] == 2
    assert result['usage']['total_tokens'] == 7


@pytest.mark.parametrize('tool,state,code,expected', [
    ('workspace.read', 'failed', 'read_failed', 'succeeded'),
    ('workspace.list', 'failed', 'read_failed', 'failed'),
    ('workspace.read', 'unknown', 'not_found', 'unknown'),
    ('workspace.read', 'cancelled', 'not_found', 'failed'),
    ('workspace.read', 'failed', 'lease_mismatch', 'failed'),
    ('workspace.read', 'failed', 'path_escape', 'failed'),
    ('workspace.read', 'failed', None, 'failed'),
    ('workspace.write', 'failed', 'not_found', 'failed'),
    ('workspace.exec', 'failed', 'process_failed', 'succeeded'),
    ('workspace.exec', 'unknown', 'process_failed', 'unknown'),
    ('workspace.exec', 'failed', 'timeout', 'failed'),
    ('mcp.files.read', 'failed', 'not_found', 'failed'),
])
def test_only_confirmed_workspace_read_failures_can_continue(tool, state, code, expected):
    calls = []

    class Provider:
        def complete(self, messages, tools, **kwargs):
            calls.append(messages)
            assert messages[-1]['result'] == {'state': 'failed', 'error_code': code,
                **({'returncode': None, 'output': '', 'truncated': False} if tool == 'workspace.exec' else {})}
            return ModelResponse(kind='final', text='读取失败，请检查文件。')

    checkpoint = _receipt_checkpoint(tool=tool, state=state, code=code)
    result = NativeAssistantRuntime(Provider(), None, checkpoint=checkpoint).run(
        run_id='run', owner_id=OWNER, epoch=0, messages=[], tools=[])
    assert result.state == expected
    assert len(calls) == (1 if expected == 'succeeded' else 0)


def _receipt_checkpoint(*, tool='workspace.read', state='failed', code='not_found'):
    return {'version': 1, 'phase': 'receipt', 'step': 1, 'transcript': [],
        'usage': {}, 'response': {'kind': 'tool_call', 'tool': tool, 'arguments': {'path': 'missing'}},
        'receipt': {'command_id': 'run:step:1', 'state': state,
                    'result': {'private': 'discarded failure output'}, 'error_code': code}}


def test_read_failure_does_not_override_cancellation_or_step_limit():
    class NoProvider:
        def complete(self, *args, **kwargs):
            raise AssertionError('must not dispatch a model request')

    runtime = NativeAssistantRuntime(NoProvider(), None, checkpoint=_receipt_checkpoint())
    assert runtime.run(run_id='run', owner_id=OWNER, epoch=0, messages=[], tools=[],
                       should_cancel=lambda: True).state == 'cancelled'
    checkpoint = _receipt_checkpoint()
    checkpoint['limits'] = {'max_steps': 1, 'max_tokens': 100}
    runtime = NativeAssistantRuntime(NoProvider(), None, checkpoint=checkpoint)
    result = runtime.run(run_id='run', owner_id=OWNER, epoch=0, messages=[], tools=[])
    assert result.state == 'failed' and '上限' in result.text


def test_read_failure_reaches_openai_wire_with_matching_tool_call_id(tmp_path):
    from tests.test_platform_provider import Broker, FakeTransport, _response

    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    repo.upsert_model(OWNER, {'profile_id': 'local', 'provider': 'openai_compatible',
        'model': 'fixture', 'secret_ref': 'env://KEY',
        'provider_config': {'endpoint': 'https://fixture.example.test'}})
    _queued(app)
    transport = FakeTransport([
        _response({'choices': [{'message': {'role': 'assistant', 'content': '先检查文件。',
            'tool_calls': [{'id': 'call-missing', 'type': 'function', 'function': {
                'name': 'workspace_read', 'arguments': '{"path":"missing"}'}}]}}]}),
        _response({'choices': [{'message': {'role': 'assistant', 'content': '文件不存在。'}}]}),
    ])
    worker = LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'],
        secret_broker=Broker({'env://KEY': 'fixture'}), provider_transport=transport)
    assert worker.run_once(OWNER)['state'] == 'succeeded'
    assert len(transport.calls) == 2
    messages = json.loads(transport.calls[1]['body'])['messages']
    assert messages[-2]['tool_calls'][0]['function']['name'] == 'workspace_read'
    assert messages[-2]['tool_calls'][0]['id'] == messages[-1]['tool_call_id'] == 'call-missing'
    assert json.loads(messages[-1]['content']) == {'state': 'failed', 'error_code': 'not_found'}


def test_remote_missing_read_continues_from_receipt_without_new_command(tmp_path):
    from fleet_core.backends.directory import DirectoryBackend
    from hub.application.command_delivery_service import CommandDeliveryService
    from hub.infrastructure.command_repository import CommandRepository
    from tools.platform.node_executor import NodeToolExecutor

    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    repo.upsert_node(OWNER, {'node_id': 'remote'})
    repo.update_defaults(OWNER, {'model_profile_id': 'local', 'workspace_id': 'home',
                                'execution_node_id': 'remote'}, 1)
    run = _queued(app)
    commands = CommandRepository(repo.db_path)
    commands.init()
    calls = []

    class Provider:
        def complete(self, messages, tools, **kwargs):
            calls.append(json.loads(json.dumps(messages)))
            if len(calls) == 1:
                return ModelResponse(kind='tool_call', tool='workspace.read', arguments={'path': 'missing'})
            return ModelResponse(kind='final', text='远程文件不存在。')

    def worker(name):
        return LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'],
            worker_id=name, provider_factory=lambda _: Provider(), remote_execution_enabled=True,
            remote_delivery=CommandDeliveryService(commands))

    assert worker('before').run_once(OWNER)['state'] == 'waiting_node'
    claimed = commands.claim_for_node('remote', 'node-worker', owner_id=OWNER)
    assert len(claimed) == 1
    backend = DirectoryBackend(tmp_path / 'remote')
    receipt = NodeToolExecutor(backend, workspace_id='home')(claimed[0])
    assert receipt['state'] == 'failed' and receipt['error_code'] == 'not_found'
    commands.record_receipt(claimed[0]['command_id'], 'failed', result=receipt, worker_id='node-worker')
    with repo._connect() as conn:
        conn.execute('UPDATE runs SET runtime_resume_at=0 WHERE run_id=?', (run['run_id'],))
    assert worker('after').run_once(OWNER)['state'] == 'succeeded'
    assert len(calls) == 2
    assert calls[-1][-1]['result'] == {'state': 'failed', 'error_code': 'not_found'}
    assert commands.get(claimed[0]['command_id'])['attempt'] == 1
    assert commands.claim_for_node('remote', 'node-worker', owner_id=OWNER) == []


def test_recovered_read_failure_cancellation_needs_no_provider_or_workspace(tmp_path):
    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    run = _queued(app)
    claim = repo.claim_run(OWNER, worker_id='before', now=1, lease_s=5)
    checkpoint = _receipt_checkpoint()
    checkpoint['usage'] = dict(input_tokens=0, output_tokens=0, total_tokens=3, provider_requests=1)
    repo.save_runtime_checkpoint(OWNER, run['run_id'], checkpoint,
                                 lease_id=claim['lease_id'], worker_id='before', now=2)
    with repo._connect() as conn:
        conn.execute("UPDATE runs SET state='cancelling',cancel_requested=1,lease_expires_at=0 WHERE run_id=?",
                     (run['run_id'],))
    repo.upsert_workspace(OWNER, {'workspace_id': 'home', 'backend': 'directory',
        'root_path': str(tmp_path / 'workspace'), 'enabled': False})

    def no_provider(_):
        raise AssertionError('cancelled receipt must not resolve a provider')

    worker = LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'],
        worker_id='after', provider_factory=no_provider)
    result = worker.run_once(OWNER)
    assert result['state'] == 'cancelled'
    assert result['usage']['provider_requests'] == 1
    assert result['usage']['total_tokens'] == 3


def test_repeated_missing_reads_stop_at_frozen_step_budget(tmp_path):
    from hub.platform.runtime.base import RuntimeLimits

    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    _queued(app)
    calls = []

    class Provider:
        def complete(self, messages, tools, **kwargs):
            calls.append(1)
            return ModelResponse(kind='tool_call', tool='workspace.read', arguments={'path': 'missing'})

    worker = LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'],
        provider_factory=lambda _: Provider(), limits=RuntimeLimits(max_steps=3))
    result = worker.run_once(OWNER)
    assert result['state'] == 'failed' and '上限' in result['result_text']
    assert len(calls) == 3
