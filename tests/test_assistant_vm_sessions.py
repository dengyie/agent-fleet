"""Main assistant controls existing VM sessions through the signed Hub queue."""
import json

import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.platform.providers.base import ModelResponse
from hub.platform.tool_broker import ToolBroker
from tests.test_platform_run_worker import OWNER, _catalog, _queued
from tests.test_session_interactions import event
from tests.test_adoption_service import instance_row, DEFAULT_PID, DEFAULT_STARTED_AT


@pytest.fixture
def app(tmp_path):
    app = create_app(FleetConfig.from_root(
        tmp_path, dev_operator=OWNER, ingest_token='test',
        platform_enabled=True, platform_worker_enabled=True,
        session_repositories_enabled=True, adoption_repositories_enabled=True,
        supervisor_enabled=True, supervisor_signing_raw=bytes(range(32)),
        append_user_turn_enabled=True,
    ))
    services = app.extensions['fleet']['services']
    services['observe'].ingest({'machine': 'm1', 'instances': [instance_row(native_file_path='/sessions/native.jsonl')],
                                'agents': {}, 'reachable': True})
    services['sessions'].ingest_events([
        event(1, 'session_start', {'agent_family': 'claude', 'adapter': 'hooks'}),
        event(2, 'assistant_message', {'text': 'remote output', 'is_complete': True}),
    ])
    return app


def execute(app, tool, arguments=None, command_id='run-1:step:1', owner=OWNER):
    return app.extensions['fleet']['services']['agent_sessions'].execute(
        tool=tool, arguments=arguments or {}, owner_id=owner, command_id=command_id)


def test_discovery_uses_machine_identity_and_redacted_session_stream(app):
    machines = execute(app, 'fleet.list_machines')
    assert machines['machines'][0]['machine_id'] == 'm1'
    rows = execute(app, 'session.list', {'machine_id': 'm1'})['sessions']
    assert rows[0]['session_id'] == 's1' and rows[0]['managed'] is True
    detail = execute(app, 'session.get', {'session_id': 's1'})['session']
    assert detail['machine_id'] == 'm1'
    assert 'append_user_turn' in detail['allowed_actions']
    first = execute(app, 'session.read_events', {'session_id': 's1', 'limit': 1})
    assert len(first['events']) == 1 and first['next_cursor'] == '1'
    second = execute(app, 'session.read_events', {
        'session_id': 's1', 'after_cursor': first['next_cursor'], 'limit': 1})
    assert second['events'][0]['payload']['text'] == 'remote output'
    assert not any(key in str(machines) for key in ('exe_path', 'cmdline', 'signature'))


@pytest.mark.parametrize('tool,args,action', [
    ('session.send_message', {'text': 'Please continue'}, 'append_user_turn'),
    ('session.control', {'action': 'pause_session'}, 'pause_session'),
    ('session.control', {'action': 'resume_session'}, 'resume_session'),
    ('session.control', {'action': 'terminate_session'}, 'terminate_session'),
    ('session.control', {'action': 'quarantine_session'}, 'quarantine_session'),
])
def test_command_is_durable_idempotent_and_waits_for_original_receipt(app, tool, args, action):
    services = app.extensions['fleet']['services']
    first = execute(app, tool, {'session_id': 's1', **args})
    assert first['status'] == 'pending'
    assert execute(app, tool, {'session_id': 's1', **args}) == first
    supervisor = services['supervisor']
    commands = supervisor.poll('m1')
    assert len(commands) == 1 and commands[0]['action'] == action
    assert commands[0]['target']['machine_id'] == 'm1'
    assert 'signature' not in str(first)
    query = {'session_id': 's1', 'command_id': first['command_id']}
    assert execute(app, 'session.get_command', query)['receipt']['status'] == 'delivered'
    supervisor.receipt(first['command_id'], 'succeeded', 'applied', machine='m1')
    assert execute(app, 'session.get_command', query)['receipt']['status'] == 'succeeded'
    assert supervisor.poll('m1') == []


@pytest.mark.parametrize('tool,args', [
    ('session.control', {'session_id': 's1', 'action': 'exec_shell'}),
    ('session.control', {'session_id': 's1', 'action': 'pause_session', 'machine_id': 'other'}),
    ('session.read_events', {'session_id': 's1', 'after_cursor': '-1'}),
    ('session.list', {'limit': True}),
    ('session.send_message', {'session_id': 's1', 'text': 'x' * 2001}),
])
def test_schema_validation_prevents_ambiguous_or_arbitrary_control(app, tool, args):
    from hub.application.errors import ApplicationError
    with pytest.raises(ApplicationError, match='invalid_arguments'):
        execute(app, tool, args)
    assert app.extensions['fleet']['services']['supervisor'].poll('m1') == []


def test_unmanaged_session_and_cross_session_receipt_are_rejected(app):
    from hub.application.errors import ApplicationError
    services = app.extensions['fleet']['services']
    row = event(3, 'session_start', {'agent_family': 'claude'}, managed=False)
    row.update(session_id='unmanaged', stream_id='other-stream')
    services['sessions'].ingest_events([row])
    with pytest.raises(ApplicationError, match='session_not_managed'):
        execute(app, 'session.send_message', {'session_id': 'unmanaged', 'text': 'continue'})
    command = execute(app, 'session.control', {'session_id': 's1', 'action': 'pause_session'})
    with pytest.raises(ApplicationError, match='command_not_found'):
        execute(app, 'session.get_command', {'session_id': 'unmanaged', 'command_id': command['command_id']})


def test_adopted_sessions_keep_pending_revoked_and_hermes_guards(app):
    from hub.application.errors import ApplicationError
    services = app.extensions['fleet']['services']
    adoption = services['adoptions'].adopt('m1', DEFAULT_PID, DEFAULT_STARTED_AT, OWNER)
    args = {'session_id': adoption.session_id, 'text': 'continue'}
    with pytest.raises(ApplicationError, match='invalid_status_transition'):
        execute(app, 'session.send_message', args)
    services['adoptions'].adoption_repo.update_status(adoption.session_id, 'adopted')
    first = execute(app, 'session.send_message', args)
    assert first['status'] == 'pending'
    services['adoptions'].revoke(adoption.session_id, OWNER)
    with pytest.raises(ApplicationError, match='adoption_revoked'):
        execute(app, 'session.send_message', args, command_id='new:step:2')
    # Querying the original command stays possible after revocation.
    assert execute(app, 'session.get_command', {
        'session_id': adoption.session_id, 'command_id': first['command_id']})['receipt']['command_id'] == first['command_id']


def test_missing_signing_key_hides_mutations_but_keeps_reads(app):
    services = app.extensions['fleet']['services']
    supervisor = services['supervisor']
    # The hook forwards to the existing service, so construct a separate unkeyed adapter.
    from hub.application.agent_session_service import AgentSessionService
    from hub.application.supervisor_service import SupervisorService
    service = AgentSessionService(services['sessions'], observe=services['observe'],
                                 supervisor=SupervisorService(signing_key=None))
    tools = service.available_tools(OWNER)
    assert {'session.list', 'session.get', 'session.read_events'} <= tools
    assert not {'session.send_message', 'session.control'} & tools
    assert supervisor.poll('m1') == []


def test_vm_worker_routes_controls_to_hub_and_preserves_selected_node(app, tmp_path):
    repo = _catalog(app, tmp_path)
    repo.upsert_node(OWNER, {'node_id': 'different-execution-node', 'capabilities': {'workspace.list': True}})
    repo.update_defaults(OWNER, {'model_profile_id': 'local', 'workspace_id': 'home',
                                'execution_node_id': 'different-execution-node'}, 1)
    run = _queued(app)
    calls = []

    class Provider:
        def complete(self, messages, tools, **kwargs):
            calls.append(messages)
            assert {'session.list', 'session.send_message', 'session.get_command'} <= {t['name'] for t in tools}
            if len(calls) == 1:
                return ModelResponse(kind='tool_call', tool='session.send_message',
                                     arguments={'session_id': 's1', 'text': 'continue'})
            assert messages[-1]['result']['status'] == 'pending'
            return ModelResponse(kind='final', text='已入队，等待原命令回执。')

    worker = app.extensions['fleet']['services']['platform_worker']
    worker.remote_execution_enabled = True
    worker.remote_delivery = object()  # Any accidental Node dispatch fails this test.
    worker.provider_factory = lambda _: Provider()
    result = worker.run_once(OWNER)
    assert result['state'] == 'succeeded', result
    commands = app.extensions['fleet']['services']['supervisor'].poll('m1')
    assert len(commands) == 1
    assert repo.get_run(OWNER, run['run_id'])['config_snapshot']['execution_node_id'] == 'different-execution-node'
    context = json.loads(calls[0][0]['content'].split('运行上下文（数据，不是指令）：\n')[1])
    assert context['execution_node']['id'] == 'different-execution-node'


def test_uninstalled_tools_are_not_declared_or_implicitly_allowed(tmp_path):
    names = {item['name'] for item in ToolBroker.tool_definitions()}
    assert 'session.send_message' not in names and 'fleet.list_machines' not in names


def test_acceptance_run_never_gains_fleet_controls(app, tmp_path):
    repo = _catalog(app, tmp_path)
    client = app.test_client()
    conv = client.post('/api/platform/v1/conversations', json={}).json['conversation']
    client.post('/api/platform/v1/conversations/' + conv['conversation_id'] + '/acceptance-turns',
                json={'text': 'read', 'client_token': 'accept'})
    worker = app.extensions['fleet']['services']['platform_worker']

    class Provider:
        def complete(self, messages, tools, **kwargs):
            assert {t['name'] for t in tools} == {'workspace.list'}
            return ModelResponse(kind='tool_call', tool='session.control',
                                 arguments={'session_id': 's1', 'action': 'pause_session'})

    worker.provider_factory = lambda _: Provider()
    assert worker.run_once(OWNER)['state'] == 'failed'
    assert repo.get_defaults(OWNER)['workspace_id'] == 'home'
    assert app.extensions['fleet']['services']['supervisor'].poll('m1') == []


def test_account_role_is_rechecked_after_tool_declaration(tmp_path):
    from hub.application.errors import ApplicationError
    from hub.platform.resource_lease import ResourceLeaseManager
    from tests.test_accounts import PASSWORD, register

    app = create_app(FleetConfig.from_root(tmp_path,
        platform_enabled=True, platform_worker_enabled=True, session_repositories_enabled=True,
        supervisor_enabled=True, supervisor_signing_raw=bytes(range(32)),
        accounts={'enabled': True, 'origin': 'https://fleet.example.test'}))
    accounts = app.extensions['accounts']
    accounts.bootstrap_admin('admin@example.test', PASSWORD)
    _, admin = accounts.login('admin@example.test', PASSWORD)
    register(accounts)
    _, user = accounts.login('alice@example.test', PASSWORD)
    service = app.extensions['fleet']['services']['agent_sessions']
    assert service.available_tools(user['id']) == frozenset()
    assert service.available_tools(admin['id'])
    with pytest.raises(ApplicationError, match='forbidden'):
        execute(app, 'session.list', owner=user['id'])
    accounts.manage(admin['id'], user['id'], changes={'role': 'admin'}, expected_revision=user['revision'])
    assert 'session.control' in service.available_tools(user['id'])
    leases = ResourceLeaseManager()
    lease = leases.acquire('test', user['id'])
    broker = ToolBroker(None, leases, resource_id='test', agent_sessions=service)
    accounts.manage(admin['id'], user['id'], changes={'role': 'user'}, expected_revision=user['revision'] + 1)
    receipt = broker.execute(command_id='test-step', tool='session.list', arguments={},
                             owner_id=user['id'], epoch=lease['epoch'])
    assert receipt.state == 'failed' and receipt.error_code == 'forbidden'
    accounts.manage(admin['id'], user['id'], changes={'active': False}, expected_revision=user['revision'] + 2)
    assert not accounts.is_active_admin(user['id'])


def test_hermes_adoption_cannot_receive_follow_up(app):
    from hub.application.errors import ApplicationError
    services = app.extensions['fleet']['services']
    services['observe'].ingest({'machine': 'm1', 'instances': [instance_row(agent_family='hermes')], 'agents': {}})
    row = services['adoptions'].adopt('m1', DEFAULT_PID, DEFAULT_STARTED_AT, OWNER)
    services['adoptions'].adoption_repo.update_status(row.session_id, 'adopted')
    services['supervisor'].poll('m1')  # Only the existing adoption command.
    assert 'append_user_turn' not in execute(app, 'session.get', {'session_id': row.session_id})['session']['allowed_actions']
    with pytest.raises(ApplicationError, match='unsupported_action'):
        execute(app, 'session.send_message', {'session_id': row.session_id, 'text': 'continue'})
    assert services['supervisor'].poll('m1') == []


def test_new_fleet_tools_do_not_displace_configured_mcp_tools():
    from hub.domain.agent_session import SESSION_TOOLS

    class Catalog:
        def tool_definitions(self, **kwargs):
            return [{'name': 'mcp.example.read', 'parameters': {'type': 'object'}}]

    baseline = ToolBroker.tool_definitions(mcp_enabled=True, mcp_catalog=Catalog(), service_actions_enabled=True)
    extended = ToolBroker.tool_definitions(mcp_enabled=True, mcp_catalog=Catalog(), service_actions_enabled=True,
                                         session_tools=SESSION_TOOLS)
    assert 'mcp.example.read' in {item['name'] for item in baseline}
    assert {item['name'] for item in extended} == {item['name'] for item in baseline} | SESSION_TOOLS


def test_receipt_previews_distinguish_queueing_from_remote_completion():
    from fleet_core.backends.base import ToolReceipt
    from hub.domain.tool_activity import tool_activity

    args = {'session_id': 'remote', 'text': '修复项目测试'}
    queued = tool_activity('session.send_message', args,
        ToolReceipt('step', 'succeeded', {'status': 'pending', 'command_id': 'control-1'}))
    assert '向远端 Agent 发送任务' in queued['title']
    assert args['text'] in queued['input']
    assert '等待节点回执' in queued['output'] and 'control-1' in queued['output']
    applied = tool_activity('session.get_command', {'session_id': 'remote'},
        ToolReceipt('step', 'succeeded', {'receipt': {'status': 'succeeded', 'command_id': 'control-1'}}))
    assert '节点确认动作成功' in applied['output']


def test_event_byte_budget_keeps_cursor_before_unreturned_events(app):
    services = app.extensions['fleet']['services']
    for seq in range(3, 9):
        result = services['sessions'].ingest_events([
            event(seq, 'assistant_message', {'text': 'x' * 40000, 'is_complete': True})
        ])
        assert result['status'] == 'accepted' and result['rejected'] == []
        assert result['accepted_through'] == seq
    first = execute(app, 'session.read_events', {'session_id': 's1', 'limit': 100})
    assert len(json.dumps(first).encode()) < 132 * 1024
    assert first['has_more'] is True
    second = execute(app, 'session.read_events', {'session_id': 's1', 'after_cursor': first['next_cursor']})
    assert [e['sequence'] for e in first['events'] + second['events']] == list(range(1, 9))


def test_idempotency_key_cannot_be_reused_for_a_different_task(app):
    from hub.application.errors import ApplicationError
    execute(app, 'session.send_message', {'session_id': 's1', 'text': 'task one'})
    with pytest.raises(ApplicationError, match='idempotency_conflict'):
        execute(app, 'session.send_message', {'session_id': 's1', 'text': 'task two'})
    assert len(app.extensions['fleet']['services']['supervisor'].poll('m1')) == 1


def test_committed_control_is_reconciled_when_post_enqueue_audit_fails(app, monkeypatch):
    services = app.extensions['fleet']['services']
    row = services['adoptions'].adopt('m1', DEFAULT_PID, DEFAULT_STARTED_AT, OWNER)
    services['adoptions'].adoption_repo.update_status(row.session_id, 'adopted')
    services['supervisor'].poll('m1')

    def unavailable(*args, **kwargs):
        raise OSError('audit unavailable')

    monkeypatch.setattr(services['adoptions'].transcripts, 'append_audit', unavailable)
    result = execute(app, 'session.send_message', {'session_id': row.session_id, 'text': 'continue'})
    assert result['status'] == 'pending'
    commands = services['supervisor'].poll('m1')
    assert len(commands) == 1 and result['command_id'] == commands[0]['command_id']
    assert execute(app, 'session.send_message', {'session_id': row.session_id, 'text': 'continue'}) == result


@pytest.mark.parametrize('enabled,append,expected', [
    (False, False, {'session.list', 'session.get', 'session.read_events', 'fleet.list_machines', 'session.list_profiles'}),
    (True, False, {'session.list', 'session.get', 'session.read_events', 'fleet.list_machines', 'session.control', 'session.get_command', 'session.list_profiles', 'session.read_logs'}),
])
def test_supervisor_and_follow_up_gates_control_actual_tool_visibility(tmp_path, enabled, append, expected):
    app = create_app(FleetConfig.from_root(tmp_path, dev_operator=OWNER,
        platform_enabled=True, platform_worker_enabled=True, session_repositories_enabled=True,
        supervisor_enabled=enabled, supervisor_signing_raw=bytes(range(32)), append_user_turn_enabled=append))
    assert app.extensions['fleet']['services']['agent_sessions'].available_tools(OWNER) == expected


def test_remote_wait_recovery_does_not_resend_session_message(app, tmp_path):
    from hub.application.run_event_service import RunEventService
    from hub.application.run_worker_service import LocalRunWorkerService
    repo = _catalog(app, tmp_path)
    repo.upsert_node(OWNER, {'node_id': 'execution-vm', 'capabilities': {'workspace.list': True}})
    repo.update_defaults(OWNER, {'model_profile_id': 'local', 'workspace_id': 'home', 'execution_node_id': 'execution-vm'}, 1)
    _queued(app)
    services = app.extensions['fleet']['services']
    calls, node_commands = [], []
    now = [100.0]

    class Delivery:
        ready = False

        def enqueue(self, command, **kwargs):
            node_commands.append(command)
            return command.as_dict()

        def wait_for_receipt(self, *args, **kwargs):
            return {'status': 'succeeded', 'result': {'entries': []}} if self.ready else {'status': 'running'}

    delivery = Delivery()

    class Provider:
        def complete(self, messages, tools, **kwargs):
            calls.append(messages)
            if len(calls) == 1:
                return ModelResponse(kind='tool_call', tool='session.send_message', arguments={'session_id': 's1', 'text': 'continue'})
            if len(calls) == 2:
                return ModelResponse(kind='tool_call', tool='workspace.list', arguments={})
            return ModelResponse(kind='final', text='done')

    def worker(name):
        return LocalRunWorkerService(repo, RunEventService(repo, clock=lambda: now[0]),
            worker_id=name, clock=lambda: now[0], provider_factory=lambda _: Provider(),
            remote_execution_enabled=True, remote_delivery=delivery, agent_sessions=services['agent_sessions'])

    assert worker('first').run_once(OWNER)['state'] == 'waiting_node'
    delivery.ready = True
    now[0] += 1
    assert worker('restart').run_once(OWNER)['state'] == 'succeeded'
    assert len(calls) == 3 and len(node_commands) == 1
    assert len(services['supervisor'].poll('m1')) == 1


@pytest.mark.parametrize('drift', [False, True])
def test_assistant_control_reaches_real_control_client_and_returns_original_receipt(tmp_path, drift):
    from hub.application.agent_session_service import AgentSessionService
    from tests.test_adoption_e2e import _build_app, _E2E, OPERATOR

    app = _build_app(tmp_path, adoption_enabled=True)
    driver = _E2E(app, tmp_path)
    row = driver.adopted_session()
    services = app.extensions['fleet']['services']
    adapter = AgentSessionService(services['sessions'],
                                  supervisor=services['supervisor'])
    if drift:
        driver.process.started_at = '2026-07-31T09:15:00Z'
    pending = adapter.execute(tool='session.control', arguments={
        'session_id': row.session_id, 'action': 'pause_session'}, owner_id=OPERATOR, command_id='step-1')
    assert pending['status'] == 'pending'
    assert driver.process.signals == []  # Queue admission alone does not control the process.
    driver.probe_poll_and_receipt()
    receipt = adapter.execute(tool='session.get_command', arguments={
        'session_id': row.session_id, 'command_id': pending['command_id']},
        owner_id=OPERATOR, command_id='step-2')['receipt']
    assert receipt['command_id'] == pending['command_id']
    if drift:
        assert receipt['status'] == 'rejected'
        assert driver.repo.get(row.session_id).status == 'revoked'
        assert driver.process.signals == []
    else:
        assert receipt['status'] == 'succeeded' and receipt['reason'] == 'paused'
        assert driver.sup.get(row.session_id).state == 'paused'
        assert driver.process.signals
