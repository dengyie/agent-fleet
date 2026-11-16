"""The administrator's main assistant uses real Hub management services."""
import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.application.errors import ApplicationError
from tests.test_platform_run_worker import OWNER, _catalog


@pytest.fixture
def app(tmp_path):
    app = create_app(FleetConfig.from_root(tmp_path, dev_operator=OWNER,
        platform_enabled=True, platform_worker_enabled=True))
    _catalog(app, tmp_path)
    return app


def invoke(app, tool, **arguments):
    return app.extensions['fleet']['services']['assistant_admin'].execute(
        tool=tool, arguments=arguments, owner_id=OWNER, command_id='run:step:1')


def test_capabilities_and_system_info_are_truthful(app):
    info = invoke(app, 'platform.get_system_info')
    assert info['catalog_counts']['models'] > 0
    assert info['cloud_vm_lifecycle'] == 'not_configured'
    ops = invoke(app, 'platform.admin_inspect', operation='capabilities')['operations']
    assert 'settings.save' in ops and 'nodes.remove' in ops
    assert 'memory.list' not in ops and 'workflows.create' not in ops
    assert 'expected_digest' in ops['settings.save']['parameters']['properties']


def test_settings_are_shared_with_real_settings_page(app):
    catalog = invoke(app, 'platform.admin_inspect', operation='settings.catalog',
                     payload={'kind': 'nodes'})
    assert catalog['items'] == []
    node = {'node_id': 'vm-new', 'label': '新 VM', 'capabilities': {}, 'enabled': True}
    invoke(app, 'platform.admin_mutate', operation='settings.save', payload={
        'kind': 'nodes', 'identifier': 'vm-new', 'value': node, 'expected_digest': ''})
    repository = app.extensions['fleet']['platform_repository']
    assert repository.get_node(OWNER, 'vm-new')['label'] == '新 VM'
    with pytest.raises(ApplicationError) as conflict:
        invoke(app, 'platform.admin_mutate', operation='settings.save', payload={
            'kind': 'nodes', 'identifier': 'vm-new', 'value': node, 'expected_digest': ''})
    assert conflict.value.code == 'revision_conflict'


def test_runtime_write_uses_existing_backup_and_revision(app):
    current = invoke(app, 'platform.admin_inspect', operation='settings.runtime')
    result = invoke(app, 'platform.admin_mutate', operation='settings.save_runtime', payload={
        'digest': current['digest'], 'values': {'AGENT_FLEET_PLATFORM_BROWSER_ENABLED': True}})
    assert result['restart_required'] is True
    saved = {row['key']: row for row in result['settings']}
    assert saved['AGENT_FLEET_PLATFORM_BROWSER_ENABLED']['saved'] == '1'
    assert saved['AGENT_FLEET_PLATFORM_BROWSER_ENABLED']['effective'] is False
    with pytest.raises(ApplicationError) as conflict:
        invoke(app, 'platform.admin_mutate', operation='settings.save_runtime', payload={
            'digest': current['digest'], 'values': {}})
    assert conflict.value.code == 'revision_conflict'


def test_admin_rechecks_authorization_and_rejects_cross_owner_or_unknown_operations(app):
    service = app.extensions['fleet']['services']['assistant_admin']
    assert service.available_tools(OWNER)
    service.authorize = lambda _: False
    assert not service.available_tools(OWNER)
    with pytest.raises(ApplicationError) as forbidden:
        invoke(app, 'platform.get_system_info')
    assert forbidden.value.code == 'forbidden'
    service.authorize = None
    for tool, args in [('platform.admin_mutate', {'operation': 'settings.catalog'}),
                       ('platform.admin_inspect', {'operation': '__dict__'}),
                       ('platform.admin_inspect', {'operation': 'settings.runtime', 'owner_id': 'other'}),
                       ('platform.admin_inspect', {'operation': 'memory.list'})]:
        with pytest.raises(ApplicationError):
            invoke(app, tool, **args)


def test_node_removal_requires_disabled_unreferenced_node_and_preserves_history(app):
    repo = app.extensions['fleet']['platform_repository']
    repo.upsert_node(OWNER, {'node_id': 'remove-me', 'label': '测试'})
    repo.provision_node_credential(OWNER, 'remove-me', secret='x' * 32)
    row = invoke(app, 'platform.admin_inspect', operation='settings.catalog', payload={'kind': 'nodes'})['items'][0]
    args = {'node_id': row['node_id'], 'expected_digest': row['digest']}
    with pytest.raises(ApplicationError, match='node_must_be_disabled'):
        invoke(app, 'platform.admin_mutate', operation='nodes.remove', payload=args)
    repo.upsert_node(OWNER, {**row, 'enabled': False})
    row = invoke(app, 'platform.admin_inspect', operation='settings.catalog', payload={'kind': 'nodes'})['items'][0]
    args['expected_digest'] = row['digest']
    repo.upsert_workspace(OWNER, {'workspace_id': 'bound', 'name': 'bound', 'root_path': '/tmp/test',
                                'default_node_id': 'remove-me'})
    with pytest.raises(ApplicationError, match='node_in_use'):
        invoke(app, 'platform.admin_mutate', operation='nodes.remove', payload=args)
    repo.upsert_workspace(OWNER, {'workspace_id': 'bound', 'name': 'bound', 'root_path': '/tmp/test'})
    result = invoke(app, 'platform.admin_mutate', operation='nodes.remove', payload=args)
    assert result['removed'] is True and result['cloud_vm_destroyed'] is False
    assert repo.get_node(OWNER, 'remove-me') is None
    assert repo.authenticate_node_credential('remove-me', 'x' * 32) is None


def test_admin_tools_route_to_hub_from_remote_worker(app):
    from hub.platform.providers.base import ModelResponse
    from tests.test_platform_run_worker import _queued
    repo = app.extensions['fleet']['platform_repository']
    repo.upsert_node(OWNER, {'node_id': 'remote', 'capabilities': {'workspace.list': True}})
    repo.update_defaults(OWNER, {'model_profile_id': 'local', 'workspace_id': 'home', 'execution_node_id': 'remote'}, 1)
    run = _queued(app)
    worker = app.extensions['fleet']['services']['platform_worker']
    worker.remote_execution_enabled = True
    worker.remote_delivery = object()  # Misrouting to Node fails instead of managing the Hub.
    calls = []

    class Provider:
        def complete(self, messages, tools, **kwargs):
            calls.append(messages)
            assert {'platform.get_system_info', 'platform.admin_mutate'} <= {t['name'] for t in tools}
            if len(calls) == 1:
                return ModelResponse(kind='tool_call', tool='platform.admin_mutate', arguments={
                    'operation': 'settings.save', 'payload': {'kind': 'nodes', 'identifier': 'new',
                    'expected_digest': '', 'value': {'label': 'new'}}})
            assert messages[-1]['result']['value']['node_id'] == 'new'
            return ModelResponse(kind='final', text='节点已登记，尚未安装。')

    worker.provider_factory = lambda _: Provider()
    assert worker.run_once(OWNER)['state'] == 'succeeded'
    assert repo.get_node(OWNER, 'new')['enabled'] is True
    assert repo.get_run(OWNER, run['run_id'])['config_snapshot']['execution_node_id'] == 'remote'


def test_acceptance_cannot_gain_admin_tools(app):
    from hub.platform.providers.base import ModelResponse
    client = app.test_client()
    conv = client.post('/api/platform/v1/conversations', json={}).json['conversation']
    client.post('/api/platform/v1/conversations/' + conv['conversation_id'] + '/acceptance-turns',
                json={'text': 'read', 'client_token': 'admin-accept'})
    worker = app.extensions['fleet']['services']['platform_worker']

    class Provider:
        def complete(self, messages, tools, **kwargs):
            assert {t['name'] for t in tools} == {'workspace.list'}
            return ModelResponse(kind='tool_call', tool='platform.admin_mutate',
                                 arguments={'operation': 'nodes.remove', 'payload': {}})

    worker.provider_factory = lambda _: Provider()
    assert worker.run_once(OWNER)['state'] == 'failed'


def test_real_account_demotion_revokes_admin_tool_execution(tmp_path):
    from hub.platform.resource_lease import ResourceLeaseManager
    from hub.platform.tool_broker import ToolBroker
    from tests.test_accounts import PASSWORD, register
    app = create_app(FleetConfig.from_root(tmp_path, platform_enabled=True, platform_worker_enabled=True,
        accounts={'enabled': True, 'origin': 'https://fleet.example.test'}))
    accounts = app.extensions['accounts']
    accounts.bootstrap_admin('admin@example.test', PASSWORD)
    _, admin = accounts.login('admin@example.test', PASSWORD)
    register(accounts)
    _, user = accounts.login('alice@example.test', PASSWORD)
    service = app.extensions['fleet']['services']['assistant_admin']
    assert not service.available_tools(user['id'])
    accounts.manage(admin['id'], user['id'], changes={'role': 'admin'}, expected_revision=user['revision'])
    assert service.available_tools(user['id'])
    leases = ResourceLeaseManager()
    lease = leases.acquire('test', user['id'])
    broker = ToolBroker(None, leases, resource_id='test', assistant_admin=service)
    accounts.manage(admin['id'], user['id'], changes={'role': 'user'}, expected_revision=user['revision'] + 1)
    receipt = broker.execute(command_id='step', tool='platform.get_system_info', arguments={},
                             owner_id=user['id'], epoch=lease['epoch'])
    assert receipt.state == 'failed' and receipt.error_code == 'forbidden'


def test_memory_and_schedules_use_real_scoped_stores_and_stable_creation_ids(tmp_path):
    app = create_app(FleetConfig.from_root(tmp_path, dev_operator=OWNER, platform_enabled=True,
        platform_worker_enabled=True, platform_memory_enabled=True, platform_schedules_enabled=True))
    created = invoke(app, 'platform.admin_mutate', operation='memory.create',
                     payload={'value': {'kind': 'note', 'title': '任务', 'content': '真实记忆'}})['memory']
    memory_id = created['memory_id']
    assert invoke(app, 'platform.admin_inspect', operation='memory.get', payload={'memory_id': memory_id})['memory']['content'] == '真实记忆'
    with pytest.raises(ApplicationError):
        invoke(app, 'platform.admin_mutate', operation='memory.create',
               payload={'value': {'kind': 'note', 'title': '任务', 'content': '真实记忆'}})
    assert len(invoke(app, 'platform.admin_inspect', operation='memory.list')['memories']) == 1
    invoke(app, 'platform.admin_mutate', operation='memory.update',
           payload={'memory_id': memory_id, 'value': {'content': '已修改'}, 'expected_revision': created['revision']})
    with pytest.raises(ApplicationError):
        invoke(app, 'platform.admin_mutate', operation='memory.delete',
               payload={'memory_id': memory_id, 'expected_revision': created['revision']})
    schedule = invoke(app, 'platform.admin_mutate', operation='schedules.create', payload={
        'value': {'action': 'service_health', 'interval_s': 60, 'target': {'service_id': 'test'}}})
    sid = schedule['schedule_id']
    assert invoke(app, 'platform.admin_inspect', operation='schedules.get', payload={'schedule_id': sid})['action'] == 'service_health'
    with pytest.raises(ApplicationError):
        invoke(app, 'platform.admin_mutate', operation='schedules.create', payload={
            'value': {'action': 'service_health', 'interval_s': 60, 'target': {'service_id': 'test'}}})
    assert len(invoke(app, 'platform.admin_inspect', operation='schedules.list')['items']) == 1


def test_node_enrollment_duplicate_cannot_rotate_credentials(tmp_path):
    from fleet_core.control import generate_ed25519_keypair
    private, _ = generate_ed25519_keypair()
    app = create_app(FleetConfig.from_root(tmp_path, dev_operator=OWNER, platform_enabled=True,
        platform_worker_enabled=True, platform_remote_execution_enabled=True, platform_command_signing_raw=private))
    result = invoke(app, 'platform.admin_mutate', operation='nodes.enroll', payload={'node_id': 'new'})
    repo = app.extensions['fleet']['platform_repository']
    secret = result['credential'].partition(':')[2]
    assert repo.authenticate_node_credential('new', secret)['owner_id'] == OWNER
    with pytest.raises(ApplicationError):
        invoke(app, 'platform.admin_mutate', operation='nodes.enroll', payload={'node_id': 'new'})
    assert repo.authenticate_node_credential('new', secret) is not None
    repo.upsert_node(OWNER, {'node_id': 'new', 'capabilities': {'workspace.exec': True}})
    repo.upsert_workspace(OWNER, {'workspace_id': 'work', 'root_path': str(tmp_path / 'work'), 'default_node_id': 'new'})
    enrollment = invoke(app, 'platform.admin_mutate', operation='nodes.workspace_enrollment', payload={
        'node_id': 'new', 'hub_url': 'https://fleet.example.test', 'execution_mode': 'vm_direct', 'sandbox_launcher': []})['enrollment']
    assert enrollment['workspace']['roots'] == {'work': str(tmp_path / 'work')}
    assert repo.authenticate_node_credential('new', secret) is None
    assert repo.authenticate_node_credential('new', enrollment['credential'].partition(':')[2]) is not None


def test_workflow_admin_uses_original_idempotent_admission_and_revision_controls(tmp_path):
    from tests.test_workflow_http import http_setup, body
    from tests.test_workflow_repository import OWNER as WORKFLOW_OWNER
    from hub.application.assistant_admin_service import AssistantAdminService
    app, _, _ = http_setup(tmp_path)
    service = AssistantAdminService(app.extensions['fleet'])

    def call(operation, payload=None, write=False, key='step'):
        return service.execute(tool='platform.admin_mutate' if write else 'platform.admin_inspect',
            arguments={'operation': operation, 'payload': payload or {}}, owner_id=WORKFLOW_OWNER, command_id=key)

    value = body()
    value.pop('client_token')
    value['template_revision'] = value.pop('revision')
    assert call('workflows.templates')['templates'][0]['template_id'] == 'fixture'
    first = call('workflows.create', {'value': value}, True)
    assert call('workflows.create', {'value': value}, True) == first
    assert call('workflows.detail', {'workflow_id': first['workflow_id']})['workflow_id'] == first['workflow_id']
    assert call('workflows.events', {'workflow_id': first['workflow_id']})['events']
    paused = call('workflows.control', {'value': {'workflow_id': first['workflow_id'],
        'action': 'pause', 'expected_revision': first['revision']}}, True, 'pause-step')
    assert paused['execution_state'] == 'queued'  # Receipt is a request, not coordinator completion.
    assert paused['action'] == 'pause'
    with pytest.raises(ApplicationError):
        call('workflows.control', {'value': {'workflow_id': first['workflow_id'],
            'action': 'resume', 'expected_revision': first['revision']}}, True, 'resume-step')


def test_admin_activity_never_echoes_secret_arguments_or_enrollment_receipts():
    from hub.domain.tool_activity import tool_activity
    from fleet_core.backends.base import ToolReceipt
    activity = tool_activity('platform.admin_mutate', {'operation': 'nodes.enroll', 'payload': {'api_key': 'private'}},
        ToolReceipt('step', 'succeeded', {'credential': 'another-private'}))
    assert 'nodes.enroll' in activity['title']
    assert 'private' not in str(activity)


def test_admin_declarations_preserve_mcp_and_hide_unwired_tools():
    from hub.platform.tool_broker import ToolBroker
    from hub.domain.assistant_admin import ADMIN_TOOLS
    class Catalog:
        def tool_definitions(self, **kwargs):
            return [{'name': 'mcp.fixture.read', 'parameters': {'type': 'object'}}]
    baseline = {t['name'] for t in ToolBroker.tool_definitions(mcp_enabled=True, mcp_catalog=Catalog())}
    extended = {t['name'] for t in ToolBroker.tool_definitions(mcp_enabled=True, mcp_catalog=Catalog(), admin_tools=ADMIN_TOOLS)}
    assert not baseline & ADMIN_TOOLS
    assert extended == baseline | ADMIN_TOOLS


def test_settings_catalog_pages_without_exposing_credentials(app):
    repo = app.extensions['fleet']['platform_repository']
    for index in range(3):
        repo.upsert_node(OWNER, {'node_id': 'page-' + str(index)})
    first = invoke(app, 'platform.admin_inspect', operation='settings.catalog', payload={'kind': 'nodes', 'limit': 2})
    second = invoke(app, 'platform.admin_inspect', operation='settings.catalog',
                    payload={'kind': 'nodes', 'limit': 2, 'offset': first['next_offset']})
    assert [r['node_id'] for r in first['items'] + second['items']] == ['page-0', 'page-1', 'page-2']
    assert second['next_offset'] is None and second['total'] == 3
    assert all('owner_id' not in r for r in first['items'])
