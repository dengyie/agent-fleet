"""Fixed administration contracts; no user-selected methods or HTTP routes."""
from hub.domain.task import DEFAULT_AGENT_TYPES, MAX_INSTRUCTION, TASK_STATES

ADMIN_TOOLS = frozenset({'platform.get_system_info', 'platform.admin_inspect', 'platform.admin_mutate'})
# Only errors proven to reject before a business mutation commits. Storage,
# authorization and uncertain remote outcomes deliberately have no entry.
CORRECTABLE_MUTATION_ERRORS = {
    'settings.save': frozenset({'revision_conflict', 'reference_forbidden'}),
    'settings.save_runtime': frozenset({'revision_conflict'}),
    'settings.save_hosts': frozenset({'revision_conflict'}),
    'nodes.remove': frozenset({'revision_conflict', 'node_in_use', 'node_must_be_disabled', 'node_not_found'}),
    'nodes.enroll': frozenset({'plugin_node_exists'}),
    'plugins.configure': frozenset({'revision_conflict'}),
    'plugins.set_target': frozenset({'node_not_found'}),
    'memory.update': frozenset({'revision_conflict'}),
    'memory.delete': frozenset({'revision_conflict'}),
    'schedules.update': frozenset({'revision_conflict'}),
    'workflows.control': frozenset({'revision_conflict'}),
    'tasks.create': frozenset({'machine_offline', 'project_not_allowed', 'agent_unavailable',
                              'invalid_machine', 'invalid_project', 'invalid_instruction'}),
}
TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 2048}
IDENTIFIER = {'type': 'string', 'minLength': 1, 'maxLength': 128}
OBJECT = {'type': 'object'}
BOOL = {'type': 'boolean'}
REVISION = {'type': 'integer', 'minimum': 0}
LIMIT = {'type': 'integer', 'minimum': 1, 'maximum': 100}
OFFSET = {'type': 'integer', 'minimum': 0}
DIGEST = {'type': 'string', 'maxLength': 64}


def operation(service, write=False, *, required=(), **properties):
    return {'service': service, 'write': write, 'parameters': {'type': 'object',
        'properties': properties, 'required': list(required), 'additionalProperties': False}}


OPERATIONS = {
    'capabilities': operation(None),
    'settings.catalog': operation('settings', required=('kind',),
        kind={'enum': ['models', 'nodes', 'workspaces']}, offset=OFFSET, limit=LIMIT),
    'settings.save': operation('settings', True, required=('kind', 'identifier', 'expected_digest', 'value'),
        kind={'enum': ['models', 'nodes', 'workspaces']}, identifier=IDENTIFIER,
        expected_digest=DIGEST, value={'type': 'object', 'description':
            'Complete editable value only. models: provider,model,provider_config,capabilities,enabled; '
            'workspaces: name,backend,root_path,default_node_id,enabled; nodes: label,capabilities,enabled. '
            'ID is supplied by identifier; omit digest,owner_id,status,last_seen_at,secret_configured. '
            'provider_config.base_url sets API URL; pass a replacement API key separately only when required.'}, api_key={'type': 'string', 'maxLength': 8192}),
    'settings.runtime': operation('runtime_settings'),
    'settings.save_runtime': operation('runtime_settings', True, required=('digest', 'values'), digest=DIGEST, values=OBJECT),
    'settings.hosts': operation('runtime_settings'),
    'settings.save_hosts': operation('runtime_settings', True, required=('digest', 'content'), digest=DIGEST,
        content={'type': 'string', 'maxLength': 262144}),
    'nodes.enroll': operation('plugins', True, required=('node_id',), node_id=IDENTIFIER, label=TEXT),
    'nodes.workspace_enrollment': operation('settings', True, required=('node_id', 'hub_url', 'execution_mode', 'sandbox_launcher'),
        node_id=IDENTIFIER, hub_url=TEXT, execution_mode={'enum': ['vm_direct', 'sandbox']},
        sandbox_launcher={'type': 'array', 'items': TEXT}),
    'nodes.remove': operation('settings', True, required=('node_id', 'expected_digest'), node_id=IDENTIFIER, expected_digest=DIGEST),
    'plugins.catalog': operation('plugins'),
    'plugins.targets': operation('plugins', required=('plugin_id',), plugin_id=IDENTIFIER),
    'plugins.configure': operation('plugins', True, required=('plugin_id', 'enabled', 'revision'),
        plugin_id=IDENTIFIER, enabled=BOOL, revision=REVISION),
    'plugins.set_target': operation('plugins', True, required=('plugin_id', 'node_id', 'enabled'),
        plugin_id=IDENTIFIER, node_id=IDENTIFIER, enabled=BOOL),
    'plugins.submit': operation('plugins', True, required=('plugin_id', 'node_id', 'action', 'arguments'),
        plugin_id=IDENTIFIER, node_id=IDENTIFIER, action=IDENTIFIER, arguments=OBJECT),
    'plugins.operation': operation('plugins', required=('plugin_id', 'operation_id'), plugin_id=IDENTIFIER, operation_id=IDENTIFIER),
    'services.register': operation('service_health', True, required=('value',), value=OBJECT),
    'usage.summary': operation('usage_summary', params=OBJECT),
    'commands.get': operation('command_inspection', required=('command_id',), command_id=IDENTIFIER),
    'commands.unknown': operation('command_inspection', limit=LIMIT),
    'memory.list': operation('platform_memory', limit=LIMIT),
    'memory.search': operation('platform_memory', required=('query',), query=TEXT, limit=LIMIT),
    'memory.get': operation('platform_memory', required=('memory_id',), memory_id=IDENTIFIER),
    'memory.create': operation('platform_memory', True, required=('value',), value=OBJECT),
    'memory.update': operation('platform_memory', True, required=('memory_id', 'value', 'expected_revision'),
        memory_id=IDENTIFIER, value=OBJECT, expected_revision=REVISION),
    'memory.delete': operation('platform_memory', True, required=('memory_id', 'expected_revision'),
        memory_id=IDENTIFIER, expected_revision=REVISION),
    'schedules.list': operation('platform_schedules', limit=LIMIT),
    'schedules.get': operation('platform_schedules', required=('schedule_id',), schedule_id=IDENTIFIER),
    'schedules.create': operation('platform_schedules', True, required=('value',), value=OBJECT),
    'schedules.update': operation('platform_schedules', True, required=('schedule_id', 'value', 'expected_revision'),
        schedule_id=IDENTIFIER, value=OBJECT, expected_revision=REVISION),
    'schedules.delete': operation('platform_schedules', True, required=('schedule_id',), schedule_id=IDENTIFIER),
    'adoption.candidates': operation('adoptions', required=('machine_id',), machine_id=IDENTIFIER),
    'adoption.adopt': operation('adoptions', True, required=('machine_id', 'pid', 'started_at'),
        machine_id=IDENTIFIER, pid={'type': 'integer', 'minimum': 1}, started_at=TEXT),
    'adoption.revoke': operation('adoptions', True, required=('session_id',), session_id=IDENTIFIER),
    'tasks.targets': operation('tasks', required=('machine',), machine=IDENTIFIER),
    'tasks.create': operation('tasks', True, required=('machine', 'project', 'agent_type', 'instruction'),
        machine=IDENTIFIER, project=IDENTIFIER, agent_type={'enum': list(DEFAULT_AGENT_TYPES)},
        instruction={'type': 'string', 'minLength': 1, 'maxLength': MAX_INSTRUCTION}),
    'tasks.list': operation('tasks', machine=IDENTIFIER, state={'enum': list(TASK_STATES)}, limit=LIMIT),
    'tasks.get': operation('tasks', required=('task_id',), task_id=IDENTIFIER),
    'tasks.cancel': operation('tasks', True, required=('task_id',), task_id=IDENTIFIER),
    'workflows.templates': operation('workflows'),
    'workflows.list': operation('workflows', limit=LIMIT, cursor=TEXT),
    'workflows.detail': operation('workflows', required=('workflow_id',), workflow_id=IDENTIFIER),
    'workflows.events': operation('workflows', required=('workflow_id',), workflow_id=IDENTIFIER, after=REVISION, limit=LIMIT),
    'workflows.create': operation('workflows', True, required=('value',), value=OBJECT),
    'workflows.control': operation('workflows', True, required=('value',), value=OBJECT),
    'mcp.list': operation('mcp_catalog_admin', after=IDENTIFIER, limit={'type': 'integer', 'minimum': 1, 'maximum': 8}),
    'mcp.detail': operation('mcp_catalog_admin', required=('connector_id',), connector_id=IDENTIFIER),
    'mcp.discover': operation('mcp_catalog_admin', True, required=('connector_id', 'expected_epoch'),
        connector_id=IDENTIFIER, expected_epoch=REVISION),
    'mcp.register': operation('mcp_catalog_admin', True, required=('value',), value=OBJECT),
    'mcp.manage': operation('mcp_catalog_admin', True, required=('connector_id', 'action', 'value'),
        connector_id=IDENTIFIER, action={'enum': ['admit', 'revoke']}, value=OBJECT),
}

# User-facing nested value contracts. Infrastructure validators remain final.
OPERATIONS['memory.create']['parameters']['properties']['value'] = {
    'type': 'object', 'properties': {'memory_id': IDENTIFIER,
        'kind': {'enum': ['fact', 'preference', 'decision', 'note']},
        'title': TEXT, 'content': {'type': 'string', 'maxLength': 16384},
        'tags': {'type': 'array', 'items': TEXT}, 'source': TEXT, 'enabled': BOOL},
    'required': ['kind', 'title', 'content'], 'additionalProperties': False}
OPERATIONS['schedules.create']['parameters']['properties']['value'] = {
    'type': 'object', 'properties': {'schedule_id': IDENTIFIER, 'name': TEXT,
        'action': {'enum': ['service_health', 'http_probe']},
        'interval_s': {'type': 'number', 'minimum': 5, 'maximum': 86400},
        'target': {'type': 'object', 'properties': {'service_id': IDENTIFIER}, 'additionalProperties': False},
        'timezone': TEXT, 'missed_policy': {'enum': ['skip', 'catch_up']},
        'overlap_policy': {'enum': ['skip', 'coalesce']},
        'next_run_at': {'type': 'number', 'minimum': 0}, 'enabled': BOOL},
    'required': ['action', 'interval_s'], 'additionalProperties': False}
OPERATIONS['workflows.create']['parameters']['properties']['value'] = {
    'type': 'object', 'properties': {'workspace_id': IDENTIFIER, 'template_id': IDENTIFIER,
        'template_revision': {'type': 'integer', 'minimum': 1},
        'expected_policy_revision': {'type': 'integer', 'minimum': 1},
        'input_refs': {'type': 'array', 'items': OBJECT}},
    'required': ['workspace_id', 'template_id', 'template_revision', 'expected_policy_revision'],
    'additionalProperties': False}
OPERATIONS['workflows.control']['parameters']['properties']['value'] = {
    'type': 'object', 'properties': {'workflow_id': IDENTIFIER,
        'action': {'enum': ['pause', 'resume', 'cancel', 'retry', 'invalidate', 'reconcile']},
        'expected_revision': {'type': 'integer', 'minimum': 1}, 'step_key': IDENTIFIER,
        'expected_generation': {'type': 'integer', 'minimum': 1}, 'reason': TEXT,
        'input_refs': {'type': 'array', 'items': OBJECT}},
    'required': ['workflow_id', 'action', 'expected_revision'], 'additionalProperties': False}
