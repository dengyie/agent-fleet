"""Main-assistant delegation uses the existing durable machine runner queue."""
import pytest

from hub.application.errors import ApplicationError
from hub.bootstrap import create_app
from hub.config import FleetConfig
from tests.test_platform_run_worker import OWNER, _catalog, _queued


@pytest.fixture
def app(tmp_path):
    app = create_app(FleetConfig.from_root(tmp_path, dev_operator=OWNER,
        platform_enabled=True, platform_worker_enabled=True,
        session_repositories_enabled=True,
        project_whitelist={'win-local': ['agent-fleet'], 'other': ['other-project']}))
    _catalog(app, tmp_path)
    app.extensions['fleet']['services']['observe'].ingest({
        'machine': 'win-local', 'reachable': True,
        'agents': {'codex': {'installed': True}, 'claude_code': {'installed': True},
                   'zcode': {'installed': False}}})
    return app


def call(app, operation, payload=None, *, command='run:step:1', write=False):
    return app.extensions['fleet']['services']['assistant_admin'].execute(
        tool='platform.admin_mutate' if write else 'platform.admin_inspect',
        arguments={'operation': operation, 'payload': payload or {}},
        owner_id=OWNER, command_id=command)


def create(app, family='codex', **changes):
    return call(app, 'tasks.create', {'machine': 'win-local', 'project': 'agent-fleet',
        'agent_type': family, 'instruction': 'Reply with the acceptance marker.', **changes}, write=True)


def test_targets_discover_registered_projects_and_reported_agents(app):
    ops = call(app, 'capabilities')['operations']
    assert {'tasks.targets', 'tasks.create', 'tasks.list', 'tasks.get', 'tasks.cancel'} <= set(ops)
    target = call(app, 'tasks.targets', {'machine': 'win-local'})
    assert target['machine'] == 'win-local'
    assert target['projects'] == ['agent-fleet']
    assert target['agents']['codex']['installed'] is True
    assert target['agents']['claude_code']['installed'] is True
    assert target['agents']['zcode']['installed'] is False
    assert target['agents']['pi']['installed'] is False
    assert 'exe_path' not in str(target) and 'credential' not in str(target)
    assert call(app, 'tasks.targets', {'machine': 'unregistered'})['projects'] == []
    assert call(app, 'tasks.targets', {'machine': 'unregistered'})['agents']['codex']['installed'] is None


def test_target_discovery_preserves_unknown_probe_evidence_and_rejects_bad_machine(app):
    services = app.extensions['fleet']['services']
    services['observe'].ingest({'machine': 'win-local', 'reachable': True,
                               'agents': {'codex': {'error': 'probe_failed'}}})
    assert call(app, 'tasks.targets', {'machine': 'win-local'})['agents']['codex']['installed'] is None
    with pytest.raises(ApplicationError) as error:
        call(app, 'tasks.targets', {'machine': '../outside'})
    assert error.value.code == 'invalid_machine'


@pytest.mark.parametrize('family', ['codex', 'claude_code'])
def test_new_task_is_durable_idempotent_and_linked_to_its_native_session(app, family):
    first = create(app, family)
    duplicate = create(app, family)
    assert first['created'] is True and duplicate['created'] is False
    task = first['task']
    assert duplicate['task']['task_id'] == task['task_id']
    assert task['machine'] == 'win-local' and task['requested_by'] == OWNER
    assert task['state'] == 'queued' and 'session_id' not in task
    assert 'client_token' not in task and 'nonce' not in task
    services = app.extensions['fleet']['services']
    leased = services['runner'].poll('win-local', 'windows-runner')['task']
    assert leased['task_id'] == task['task_id'] and leased['agent_type'] == family
    from tests.test_session_interactions import event
    start = event(1, 'session_start', {'agent_family': family, 'adapter': 'hooks'})
    start.update(machine_id='win-local', session_id='windows_native', attempt_id=leased['attempt_id'])
    services['sessions'].ingest_events([start])
    services['runner'].result('win-local', leased['attempt_id'], leased['nonce'], 0, log_summary='real result')
    detail = call(app, 'tasks.get', {'task_id': task['task_id']})['task']
    assert detail['state'] == 'succeeded' and detail['session_id'] == 'windows_native'
    assert detail['result']['log_summary'] == 'real result'
    assert call(app, 'tasks.list', {'machine': 'win-local'})['tasks'][0]['task_id'] == task['task_id']
    assert services['runner'].poll('win-local', 'windows-runner')['task'] is None


@pytest.mark.parametrize('changes,code', [
    ({'project': 'unregistered'}, 'project_not_allowed'),
    ({'machine': 'offline'}, 'machine_offline'),
    ({'agent_type': 'zcode'}, 'agent_unavailable'),
    ({'machine': '../windows'}, 'invalid_machine'),
    ({'actor': 'other'}, 'invalid_arguments'),
    ({'client_token': 'forged'}, 'invalid_arguments'),
])
def test_dispatch_preserves_task_policy_and_runtime_identity(app, changes, code):
    with pytest.raises(ApplicationError) as error:
        create(app, **changes)
    assert error.value.code == code
    assert app.extensions['fleet']['services']['tasks'].list()['tasks'] == []


def test_cancel_queued_task_prevents_runner_execution(app):
    task = create(app)['task']
    result = call(app, 'tasks.cancel', {'task_id': task['task_id']}, command='run:step:2', write=True)
    assert result['task']['state'] == 'cancelled'
    assert app.extensions['fleet']['services']['runner'].poll('win-local', 'runner')['task'] is None


def test_wait_observes_original_task_progress_without_another_dispatch(app, monkeypatch):
    from hub.application import task_service
    task = create(app)['task']
    def progress(_):
        app.extensions['fleet']['services']['runner'].poll('win-local', 'runner')
    monkeypatch.setattr(task_service.time, 'sleep', progress)
    observed = call(app, 'tasks.get', {'task_id': task['task_id'], 'wait_seconds': 30})['task']
    assert observed['state'] == 'leased' and observed['task_id'] == task['task_id']
    assert len(call(app, 'tasks.list')['tasks']) == 1


def test_task_binding_is_not_lost_behind_a_large_native_archive(app):
    services = app.extensions['fleet']['services']
    task = create(app)['task']
    leased = services['runner'].poll('win-local', 'runner')['task']
    from tests.test_session_interactions import event
    events = []
    for i in range(1002):
        row = event(1, 'session_start', {'agent_family': 'codex', 'adapter': 'hooks'})
        row.update(session_id=f'archive_{i}', stream_id=f'stream_{i}', event_id=f'event_{i}',
                   machine_id='win-local', attempt_id=leased['attempt_id'] if i == 0 else f'archive_{i}',
                   emitted_at='2020-01-01T00:00:00Z' if i == 0 else '2026-01-01T00:00:00Z')
        events.append(row)
    for row in events:
        services['sessions'].ingest_events([row])
    assert call(app, 'tasks.get', {'task_id': task['task_id']})['task']['session_id'] == 'archive_0'


def test_disabled_task_feature_hides_operations_and_rejects_execution(tmp_path):
    app = create_app(FleetConfig.from_root(tmp_path, dev_operator=OWNER,
        platform_enabled=True, platform_worker_enabled=True, tasks_enabled=False))
    assert not any(name.startswith('tasks.') for name in call(app, 'capabilities')['operations'])
    with pytest.raises(ApplicationError) as error:
        create(app)
    assert error.value.code == 'operation_unavailable'


def test_current_admin_authorization_is_required_for_dispatch(app):
    app.extensions['fleet']['services']['assistant_admin'].authorize = lambda _: False
    with pytest.raises(ApplicationError) as error:
        create(app)
    assert error.value.code == 'forbidden'
    assert app.extensions['fleet']['services']['tasks'].list()['tasks'] == []


def test_main_assistant_can_dispatch_windows_agent_from_another_workspace(app):
    from hub.platform.providers.base import ModelResponse
    repo = app.extensions['fleet']['platform_repository']
    repo.upsert_node(OWNER, {'node_id': 'other-node', 'capabilities': {'workspace.list': True}})
    repo.update_defaults(OWNER, {'model_profile_id': 'local', 'workspace_id': 'home',
                                'execution_node_id': 'other-node'}, 1)
    run = _queued(app)
    worker = app.extensions['fleet']['services']['platform_worker']
    worker.remote_execution_enabled = True
    worker.remote_delivery = object()  # Dispatch must go through Hub, not this workspace Node.
    calls = []

    class Provider:
        def complete(self, messages, tools, **kwargs):
            calls.append(messages)
            if len(calls) == 1:
                return ModelResponse(kind='tool_call', tool='platform.admin_mutate', arguments={
                    'operation': 'tasks.create', 'payload': {'machine': 'win-local',
                    'project': 'agent-fleet', 'agent_type': 'codex', 'instruction': 'Read the project.'}})
            assert messages[-1]['result']['task']['machine'] == 'win-local'
            return ModelResponse(kind='final', text='Windows Agent 任务已入队。')

    worker.provider_factory = lambda _: Provider()
    assert worker.run_once(OWNER)['state'] == 'succeeded'
    task = app.extensions['fleet']['services']['tasks'].list()['tasks'][0]
    assert task['machine'] == 'win-local' and task['agent_type'] == 'codex'
    assert repo.get_run(OWNER, run['run_id'])['config_snapshot']['workspace_id'] == 'home'
    assert repo.get_run(OWNER, run['run_id'])['config_snapshot']['execution_node_id'] == 'other-node'


def test_activity_distinguishes_admission_from_real_task_completion():
    from types import SimpleNamespace
    from hub.domain.tool_activity import tool_activity
    args = {'operation': 'tasks.create', 'payload': {'machine': 'win-local',
            'project': 'agent-fleet', 'agent_type': 'codex', 'instruction': 'do work'}}
    receipt = SimpleNamespace(state='succeeded', truncated=False,
                              result={'task': {'task_id': 't-one', 'state': 'queued'}})
    activity = tool_activity('platform.admin_mutate', args, receipt)
    assert '启动远端 Agent 任务' in activity['title'] and 'win-local' in activity['title']
    assert '等待节点领取' in activity['output'] and '已完成' not in activity['output']
    receipt.result['task'].update(state='succeeded', session_id='native-one')
    activity = tool_activity('platform.admin_inspect', {'operation': 'tasks.get'}, receipt)
    assert '任务已完成' in activity['output'] and 'native-one' in activity['output']


@pytest.mark.parametrize('operation', [None, [], {}])
def test_malformed_operation_can_be_rendered_before_tool_validation(operation):
    from hub.domain.tool_activity import tool_activity
    result = tool_activity('platform.admin_mutate', {'operation': operation})
    assert 'title' in result
