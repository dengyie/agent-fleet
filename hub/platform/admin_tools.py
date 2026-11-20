"""Discoverable Hub administrator tools for normal main-assistant Runs."""
from hub.domain.assistant_admin import ADMIN_TOOLS, OPERATIONS

__all__ = ['ADMIN_TOOLS', 'ADMIN_TOOL_DEFINITIONS']
ADMIN_TOOL_DEFINITIONS = (
    {'name': 'platform.get_system_info', 'risk': 'read_only', 'scope_kind': 'administrator',
     'description': 'Read project-wide gates, available services, current owner catalog counts and configuration readiness. '
                    'Not proof of model/VM connectivity. Cloud VM provisioning is not configured.',
     'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    *({'name': name, 'risk': 'write' if write else 'read_only', 'scope_kind': 'administrator',
       'description': description,
       'parameters': {'type': 'object', 'properties': {
           'operation': {'type': 'string', 'enum': [key for key, spec in OPERATIONS.items() if spec['write'] == write]},
           'payload': {'type': 'object', 'description': 'Exact operation parameters from capabilities. No owner_id or actor overrides.'}},
           'required': ['operation'], 'additionalProperties': False}}
      for name, write, description in (
          ('platform.admin_inspect', False,
           'Inspect project management resources. First call operation=capabilities (payload={}) for available operation schemas. '
           'Then use settings catalog/runtime/hosts, plugins, usage, commands, adoption, tasks, memory, schedules, workflows or MCP reads. '
           'tasks.targets discovers registered projects/Agent types on a machine; tasks.get follows the original task and its session_id. '
           'Only enabled services are callable; settings.catalog is paginated with next_offset.'),
          ('platform.admin_mutate', True,
           'Administer the project using an enabled operation from platform.admin_inspect capabilities. '
           'Read current values/schema/revision before writes. Reuse settings page services for models, URL, keys, workspaces and nodes; '
           'runtime/hosts changes require Hub restart and retain backups. nodes.enroll creates a NEW Node with one-time credentials; '
           'duplicate IDs fail without rotation. nodes.remove requires a disabled unreferenced Node; never destroys a cloud VM. '
           'Plugin operations and workflows return their own receipts, not completed remote work. Never repeat uncertain writes. '
           'tasks.create starts a new managed native Agent task on a registered machine/project, independent of the current workspace; '
           'tasks.cancel cancels the original task. For existing conversations use session.send_message instead of creating another task. '
           'Do not echo credentials or enrollment secrets in chat.'))),
)
