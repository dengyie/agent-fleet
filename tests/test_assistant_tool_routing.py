"""One assistant Run can manage Hub state and work on its selected Node."""
import json

import pytest

from fleet_core.backends.directory import DirectoryBackend
from hub.application.run_worker_service import LocalRunWorkerService
from hub.application.run_event_service import RunEventService
from hub.platform.providers.base import ModelResponse
from tests.test_platform_run_worker import OWNER, _app, _catalog, _queued
from tools.platform.node_executor import NodeToolExecutor


@pytest.mark.parametrize('resume', [False, True])
def test_vm_run_uses_hub_controls_and_node_workspace_without_replaying_update(tmp_path, resume):
    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    repo.upsert_node(OWNER, {'node_id': 'vm', 'capabilities': {'workspace.write': True}})
    repo.update_defaults(OWNER, {'model_profile_id': 'local', 'workspace_id': 'home',
                                'execution_node_id': 'vm'}, 1)
    run = _queued(app)
    services = app.extensions['fleet']['services']
    before = repo.get_defaults(OWNER)
    commands, declarations, provider_calls = [], [], []
    executor = NodeToolExecutor(DirectoryBackend(tmp_path / 'node'), workspace_id='home')
    now = [100.0]

    class Delivery:
        result = None

        def enqueue(self, command, **kwargs):
            commands.append(command)
            if not resume:
                self.result = executor(command.as_dict())
            return command.as_dict()

        def wait_for_receipt(self, command_id, **kwargs):
            return {'status': 'succeeded', 'result': self.result} if self.result else {'status': 'running'}

    delivery = Delivery()

    class Provider:
        def complete(self, messages, tools, **kwargs):
            provider_calls.append(messages)
            declarations.append({item['name'] for item in tools})
            step = len(provider_calls)
            if step == 1:
                return ModelResponse(kind='tool_call', tool='platform.get_defaults', arguments={})
            if step == 2:
                return ModelResponse(kind='tool_call', tool='platform.update_defaults', arguments={
                    'expected_revision': before['revision'],
                    **{key: before.get(key) for key in ('model_profile_id', 'workspace_id', 'execution_node_id')},
                })
            if step == 3:
                return ModelResponse(kind='tool_call', tool='workspace.write',
                                     arguments={'path': 'result.txt', 'content': 'node-output'})
            if step == 4:
                return ModelResponse(kind='tool_call', tool='conversation.rename',
                                     arguments={'conversation_id': run['conversation_id'], 'title': 'mixed-complete'})
            return ModelResponse(kind='final', text='done')

    def worker(name):
        return LocalRunWorkerService(repo, RunEventService(repo, clock=lambda: now[0]), worker_id=name,
            clock=lambda: now[0], provider_factory=lambda _: Provider(),
            remote_execution_enabled=True, remote_delivery=delivery,
            defaults_service=services['platform_defaults'], conversation_service=services['conversations'])

    result = worker('first').run_once(OWNER)
    if resume:
        assert result['state'] == 'waiting_node'
        assert len(commands) == 1
        assert len(provider_calls) == 3
        assert repo.get_defaults(OWNER)['revision'] == before['revision'] + 1
        delivery.result = executor(commands[0].as_dict())
        now[0] += 1
        result = worker('restarted').run_once(OWNER)

    assert result['state'] == 'succeeded', result
    assert (tmp_path / 'node' / 'result.txt').read_text(encoding='utf-8') == 'node-output'
    assert not (tmp_path / 'workspace' / 'result.txt').exists()
    assert repo.get_conversation(OWNER, run['conversation_id'])['title'] == 'mixed-complete'
    after = repo.get_defaults(OWNER)
    assert after['revision'] == before['revision'] + 1
    assert all(after.get(k) == before.get(k) for k in ('model_profile_id', 'workspace_id', 'execution_node_id'))
    assert [command.action for command in commands] == ['tool.workspace.write']
    assert len(provider_calls) == 5
    assert all({'workspace.write', 'platform.get_defaults', 'platform.update_defaults',
                'conversation.rename'} <= tools for tools in declarations)
    context = json.loads(provider_calls[-1][0]['content'].split('运行上下文（数据，不是指令）：\n')[1])
    assert {tool['name'] for tool in context['tools']} == declarations[-1]
    events = repo.list_run_events(OWNER, run['run_id'])['events']
    results = [event['payload'] for event in events if event['kind'] == 'tool_result']
    assert [item['tool'] for item in results] == [
        'platform.get_defaults', 'platform.update_defaults', 'workspace.write', 'conversation.rename']
    assert all(item['state'] == 'succeeded' for item in results)


def test_vm_management_keeps_existing_owner_scope(tmp_path):
    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    other = 'other@example.test'
    target = repo.create_conversation(other, 'other-conversation', title='preserve', workspace_id=None)
    repo.upsert_node(OWNER, {'node_id': 'vm', 'capabilities': {'workspace.list': True}})
    repo.update_defaults(OWNER, {'model_profile_id': 'local', 'workspace_id': 'home',
                                'execution_node_id': 'vm'}, 1)
    run = _queued(app)
    services = app.extensions['fleet']['services']

    class Provider:
        def complete(self, messages, tools, **kwargs):
            assert 'conversation.rename' in {item['name'] for item in tools}
            return ModelResponse(kind='tool_call', tool='conversation.rename',
                arguments={'conversation_id': target['conversation_id'], 'title': 'must-not-change'})

    result = LocalRunWorkerService(repo, services['run_events'],
        remote_execution_enabled=True, remote_delivery=object(),
        conversation_service=services['conversations'], provider_factory=lambda _: Provider()).run_once(OWNER)
    assert result['state'] == 'failed'
    assert 'conversation_not_found' in result['result_text']
    assert repo.get_conversation(other, target['conversation_id'])['title'] == 'preserve'
    assert repo.get_run(OWNER, run['run_id'])['state'] == 'failed'


@pytest.mark.parametrize('tool', ['platform.get_defaults', 'conversation.rename'])
def test_remote_acceptance_never_gains_installed_hub_controls(tmp_path, tool):
    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    repo.upsert_node(OWNER, {'node_id': 'vm', 'capabilities': {'workspace.list': True}})
    services = app.extensions['fleet']['services']
    client = app.test_client()
    conversation = client.post('/api/platform/v1/conversations', json={}).get_json()['conversation']
    response = client.post('/api/platform/v1/conversations/' + conversation['conversation_id'] + '/acceptance-turns',
        json={'text': 'read-only', 'client_token': 'acceptance', 'overrides': {'execution_node_id': 'vm'}})
    assert response.status_code == 202
    defaults = repo.get_defaults(OWNER)
    declarations = []

    class Provider:
        def complete(self, messages, tools, **kwargs):
            declarations.append({item['name'] for item in tools})
            arguments = {} if tool == 'platform.get_defaults' else {
                'conversation_id': conversation['conversation_id'], 'title': 'must-not-change'}
            return ModelResponse(kind='tool_call', tool=tool, arguments=arguments)

    result = LocalRunWorkerService(repo, services['run_events'],
        remote_execution_enabled=True, remote_delivery=object(),
        conversation_service=services['conversations'], defaults_service=services['platform_defaults'],
        provider_factory=lambda _: Provider()).run_once(OWNER)
    assert declarations == [{'workspace.list'}]
    assert result['state'] == 'failed' and 'tool_not_allowed' in result['result_text']
    assert repo.get_conversation(OWNER, conversation['conversation_id'])['title'] == conversation['title']
    assert repo.get_defaults(OWNER) == defaults


def test_vm_service_management_reads_hub_evidence_and_preserves_restart_approval(tmp_path):
    from tests.test_platform_service_actions import _app as actions_app, _service

    owner = 'owner-a@example.test'
    app = actions_app(tmp_path, actions=True, owner=owner)
    repo = _catalog(app, tmp_path, owner=owner)
    _service(app)
    repo.upsert_node(owner, {'node_id': 'vm', 'capabilities': {'workspace.list': True}})
    repo.update_defaults(owner, {'model_profile_id': 'local', 'workspace_id': 'home',
                                 'execution_node_id': 'vm'}, 1)
    run = _queued(app, owner=owner)
    services = app.extensions['fleet']['services']
    calls = []

    class Provider:
        def complete(self, messages, tools, **kwargs):
            calls.append(messages)
            assert {'fleet.list_services', 'service.get_health', 'service.request_action'} <= {
                item['name'] for item in tools}
            if len(calls) == 1:
                return ModelResponse(kind='tool_call', tool='fleet.list_services', arguments={})
            if len(calls) == 2:
                assert messages[-1]['result']['services'][0]['service']['service_id'] == 'api'
                return ModelResponse(kind='tool_call', tool='service.get_health', arguments={'service_id': 'api'})
            if len(calls) == 3:
                return ModelResponse(kind='tool_call', tool='service.request_action',
                                     arguments={'service_id': 'api', 'action': 'restart'})
            assert messages[-1]['result']['grant']['state'] == 'pending'
            return ModelResponse(kind='final', text='Restart requested, awaiting approval.')

    result = LocalRunWorkerService(repo, services['run_events'],
        remote_execution_enabled=True, remote_delivery=object(),
        diagnostics=services['diagnostics'], service_actions=services['service_actions'],
        provider_factory=lambda _: Provider()).run_once(owner)
    assert result['state'] == 'succeeded', result
    assert len(calls) == 4
    events = repo.list_run_events(owner, run['run_id'])['events']
    assert not any(event['kind'] == 'node_command_queued' for event in events)
    assert app.test_client().get('/api/platform/v1/services/api').status_code == 200
