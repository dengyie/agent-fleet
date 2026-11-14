"""Regression for production workspace.exec/sandbox_unavailable failures."""
import pytest

from fleet_core.backends.base import ToolReceipt
from fleet_core.backends.sandbox import SandboxBackend
from hub.application.run_worker_service import LocalRunWorkerService
from hub.platform.providers.base import ModelResponse
from hub.platform.runtime.native import NativeAssistantRuntime
from tests.test_platform_run_worker import OWNER, _app, _catalog, _queued


@pytest.mark.parametrize('launcher,remote,expected_exec', [
    (None, False, False), (('fixture-launcher',), False, True),
    (None, True, True),
])
def test_worker_advertises_exec_only_when_local_sandbox_is_configured(
        tmp_path, launcher, remote, expected_exec):
    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    if remote:
        repo.upsert_node(OWNER, {'node_id': 'remote'})
        repo.update_defaults(OWNER, {'model_profile_id': 'local', 'workspace_id': 'home',
                                    'execution_node_id': 'remote'}, 1)
    run = _queued(app)
    declarations = []

    class Provider:
        def complete(self, messages, tools, **kwargs):
            declarations.append({item['name'] for item in tools})
            return ModelResponse(kind='final', text='环境已检查。')

    worker = LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'],
        sandbox_launcher=launcher, remote_execution_enabled=True, remote_delivery=object(),
        provider_factory=lambda _: Provider())
    result = worker.run_once(OWNER)
    assert result['state'] == 'succeeded'
    assert ('workspace.exec' in declarations[0]) is expected_exec
    assert {'workspace.list', 'workspace.read', 'workspace.write'} <= declarations[0]
    assert repo.get_run(OWNER, run['run_id'])['result_text'] == '环境已检查。'


def test_failed_tool_reason_survives_public_run_and_conversation_reload(tmp_path, monkeypatch):
    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    run = _queued(app)
    monkeypatch.setattr(SandboxBackend, 'execute', lambda self, command_id, *a, **kw:
                        ToolReceipt(command_id, 'failed', {}, 'sandbox_unavailable'))

    class Provider:
        def complete(self, messages, tools, **kwargs):
            return ModelResponse(kind='tool_call', tool='workspace.exec',
                                 arguments={'argv': ['pwd']})

    worker = LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'],
        sandbox_launcher=('fixture-launcher',), provider_factory=lambda _: Provider())
    result = worker.run_once(OWNER)
    assert result['state'] == 'failed'
    assert 'workspace.exec' in result['result_text']
    assert 'sandbox_unavailable' in result['result_text']
    assert '沙盒' in result['result_text']
    client = app.test_client()
    public = client.get('/api/platform/v1/runs/' + run['run_id']).get_json()
    tool = next(s for s in public['progress']['segments'] if s['kind'] == 'tool')
    assert tool['error_code'] == 'sandbox_unavailable'
    conversation = client.get('/api/platform/v1/conversations/' + run['conversation_id']).get_json()
    assert conversation['conversation']['runs'][0]['progress'] == public['progress']


def test_environment_inspection_finishes_without_attempting_unavailable_exec(tmp_path):
    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    run = _queued(app)
    calls = []

    class Provider:
        def complete(self, messages, tools, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                return ModelResponse(kind='tool_call', tool='workspace.list', arguments={})
            if any(tool['name'] == 'workspace.exec' for tool in tools):
                return ModelResponse(kind='tool_call', tool='workspace.exec',
                                     arguments={'argv': ['pwd']})
            return ModelResponse(kind='final', text='已检查目录，命令执行当前不可用。')

    worker = LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'],
                                  provider_factory=lambda _: Provider())
    result = worker.run_once(OWNER)
    assert result['state'] == 'succeeded'
    assert len(calls) == 2
    events = repo.list_run_events(OWNER, run['run_id'])['events']
    assert [e['payload']['tool'] for e in events if e['kind'] == 'tool_call'] == ['workspace.list']


def test_receipt_checkpoint_reports_failure_without_replaying_tool():
    checkpoint = {'version': 1, 'phase': 'receipt', 'step': 7, 'transcript': [],
        'usage': {}, 'response': {'kind': 'tool_call', 'tool': 'workspace.exec'},
        'receipt': {'command_id': 'run-1:step:7', 'state': 'failed', 'result': {},
                    'error_code': 'sandbox_unavailable'}}
    result = NativeAssistantRuntime(None, None, checkpoint=checkpoint).run(
        run_id='run-1', owner_id=OWNER, epoch=0, messages=[], tools=[])
    assert result.state == 'failed'
    assert 'workspace.exec' in result.text and 'sandbox_unavailable' in result.text


def test_progress_keeps_bounded_error_code_without_private_receipt_data():
    from hub.domain.run_progress import project_progress
    value = project_progress({}, 'tool_result', {'step': 7, 'command_id': 'run:7',
        'tool': 'workspace.exec', 'state': 'failed', 'error_code': 'sandbox_unavailable',
        'result': {'output': 'private process output'}}, 1)
    assert value['segments'][0]['error_code'] == 'sandbox_unavailable'
    assert 'private' not in str(value)
    value = project_progress(value, 'tool_result', {'step': 7, 'command_id': 'run:7',
        'tool': 'workspace.exec', 'state': 'succeeded', 'error_code': None}, 2)
    assert not value['segments'][0].get('error_code')
