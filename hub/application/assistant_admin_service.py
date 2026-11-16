"""Administrator main-assistant access to existing application services.

This is an explicit operation allowlist, not an HTTP proxy or arbitrary method
executor. Caller identity comes from the Run and is revalidated on every call.
"""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import asdict, is_dataclass

from jsonschema import Draft202012Validator

from hub.application.errors import ApplicationError
from hub.application.platform_readiness_service import check_platform_readiness
from hub.domain.assistant_admin import ADMIN_TOOLS, OPERATIONS


def _public(value):
    if is_dataclass(value):
        return _public(asdict(value))
    if isinstance(value, (list, tuple)):
        return [_public(item) for item in value]
    if isinstance(value, dict):
        return {key: _public(item) for key, item in value.items()}
    return value


class AssistantAdminService:
    def __init__(self, fleet, *, authorize=None):
        self.fleet = fleet
        self.services = fleet['services']
        self.authorize = authorize

    def _authorized(self, owner):
        return bool(owner and (self.authorize is None or self.authorize(owner)))

    def available_tools(self, owner):
        return ADMIN_TOOLS if self._authorized(owner) else frozenset()

    def _operations(self):
        return {key: spec for key, spec in OPERATIONS.items()
                if (spec['service'] is None or self.services.get(spec['service']) is not None)
                and (key not in {'mcp.register', 'mcp.discover'}
                     or self.services['mcp_catalog_admin'].registration_enabled)}

    def execute(self, *, tool, arguments, owner_id, command_id):
        if not self._authorized(owner_id):
            raise ApplicationError('forbidden', '管理系统需要当前有效的管理员身份', 403)
        if tool not in ADMIN_TOOLS or not isinstance(arguments, dict):
            raise ApplicationError('invalid_arguments', '管理工具或参数无效', 400)
        if tool == 'platform.get_system_info':
            if arguments:
                raise ApplicationError('invalid_arguments', '总览无需参数', 400)
            repo = self.fleet['platform_repository']
            return {'catalog_counts': {kind: len(getattr(repo, 'list_' + kind)(owner_id))
                    for kind in ('models', 'nodes', 'workspaces')},
                'defaults': repo.get_defaults(owner_id),
                'services': sorted(key for key, value in self.services.items() if value is not None),
                'configuration': check_platform_readiness(self.fleet, owner_id),
                'runtime': self.services['runtime_settings'].read(),
                'cloud_vm_lifecycle': 'not_configured',
                'management_scope': 'current_administrator_catalog_and_fleet_sessions',
                'operation_count': len(self._operations()),
                'fleet': self._fleet_snapshot(owner_id)}
        name = arguments.get('operation')
        if not isinstance(name, str):
            raise ApplicationError('invalid_arguments', '需要管理操作名称', 400)
        spec = self._operations().get(name)
        if (set(arguments) - {'operation', 'payload'} or spec is None
                or spec['write'] != (tool == 'platform.admin_mutate')):
            raise ApplicationError('operation_unavailable', '管理操作不存在、未启用或读写工具不匹配', 400)
        payload = arguments.get('payload', {})
        if not Draft202012Validator(spec['parameters']).is_valid(payload):
            raise ApplicationError('invalid_arguments', '参数不符合 capabilities 返回的操作合同', 400)
        if name == 'capabilities':
            return {'operations': {key: {'tool': 'platform.admin_mutate' if item['write'] else 'platform.admin_inspect',
                    'parameters': item['parameters']} for key, item in self._operations().items()},
                'notes': [
                    'Node enrollment returns one-time credentials; never echo secrets in chat.',
                    'Settings writes use the same full value contract and digest as the settings page. Read before replacing.',
                    'Runtime/hosts writes keep backups and require Hub restart. Saved flags are not effective flags.',
                    'Node removal requires disabled/unreferenced status; it does not destroy a VM.',
                    'Operations use current administrator identity, never a supplied owner_id or actor.',
                    'Pending operation/workflow/adoption receipts require original receipt queries; never replay unknown effects.',
                    'Plugin action names/parameters come from plugins.catalog and targets, not guessed shell commands.',
                    'Workflow input schemas and revisions come from workflows.templates; value excludes owner_id/client_token.',
                    'Memory/schedule creation gets a stable generated ID when omitted; retain it for subsequent reads.',
                ]}
        # Stable per-Run-step tokens for existing durable admission APIs.
        key = 'assistant_' + hashlib.sha256((owner_id + '\0' + command_id).encode()).hexdigest()[:40]
        try:
            result = self._dispatch(name, payload, owner_id, key)
            result = _public(result)
            return result if isinstance(result, dict) else {'items': result}
        except ApplicationError:
            raise
        except Exception as exc:
            # Existing domain services expose their stable error codes. Preserve
            # those codes, never exception text (which can contain a secret).
            from hub.application.adoption_service import AdoptionServiceError
            from hub.application.platform_schedule_service import PlatformScheduleError
            from hub.application.workflow_operator import WorkflowOperatorError
            from hub.domain.workflow_errors import WorkflowValidationError
            from hub.domain.mcp_catalog import MCPCatalogError
            from hub.infrastructure.platform_db_base import PlatformRepositoryError
            from platform_schema import PlatformValidationError
            from fleet_core.plugins import PluginError
            if isinstance(exc, (AdoptionServiceError, PlatformScheduleError, WorkflowOperatorError,
                                WorkflowValidationError, MCPCatalogError, PlatformRepositoryError,
                                PlatformValidationError, PluginError)):
                raise ApplicationError(exc.code, exc.code, 400) from exc
            raise

    def _fleet_snapshot(self, owner):
        """Independent bounded sections; one failed source remains explicit."""
        sections = {}
        sources = {
            'machines': ('observe', lambda service: [
                {key: row.get(key) for key in ('machine', 'desc', 'online', 'timestamp')}
                for row in service.status()['machines']]),
            'sessions': ('sessions', lambda service: service.list_sessions(active=True, limit=25)),
            'services': ('service_health', lambda service: service.list(owner)),
        }
        for label, (name, read) in sources.items():
            service = self.services.get(name)
            if service is None:
                sections[label] = {'available': False}
                continue
            try:
                result = read(service)
                if label == 'machines':
                    result = {'items': result[:25], 'total': len(result), 'more_via': 'fleet.list_machines'}
                elif label == 'services':
                    rows = result.get('services', [])
                    result = {'items': rows[:25], 'total': len(rows), 'more_via': 'fleet.list_services'}
                else:
                    result['possibly_more'] = len(result.get('sessions', [])) == 25
                sections[label] = {'available': True, **result}
            except Exception:
                # An overview can report partial source failure without losing
                # usable sections. Detailed reads retain their original errors.
                sections[label] = {'available': True, 'error': 'source_unavailable'}
        return sections

    def _catalog(self, owner, p):
        service = self.services['settings']
        rows = getattr(service.repository, 'list_' + p['kind'])(owner)
        offset, limit = p.get('offset', 0), p.get('limit', 25)
        return {'items': [service.public(p['kind'], row) for row in rows[offset:offset + limit]],
                'total': len(rows), 'next_offset': offset + limit if offset + limit < len(rows) else None}

    def _dispatch(self, name, p, owner, key):
        s = self.services
        if name == 'settings.catalog':
            return self._catalog(owner, p)
        if name == 'settings.save':
            return s['settings'].save(owner, p['kind'], p['identifier'],
                {field: value for field, value in p.items() if field not in {'kind', 'identifier'}})
        if name == 'settings.runtime':
            return s['runtime_settings'].read()
        if name == 'settings.hosts':
            return s['runtime_settings'].hosts()
        if name == 'settings.save_runtime':
            return s['runtime_settings'].save(p)
        if name == 'settings.save_hosts':
            return s['runtime_settings'].save_hosts(p)
        if name == 'nodes.enroll':
            return s['plugins'].enroll(owner, p['node_id'], p.get('label', ''))
        if name == 'nodes.workspace_enrollment':
            from hub.application.workspace_node_setup import WorkspaceNodeSetupService
            return WorkspaceNodeSetupService(s['settings'].repository, s.get('platform_delivery'),
                remote_enabled=self.fleet['config'].platform_remote_execution_enabled).enroll(
                    owner, p['node_id'], {k: v for k, v in p.items() if k != 'node_id'})
        if name == 'nodes.remove':
            return s['settings'].remove_node(owner, p['node_id'], p['expected_digest'])
        if name.startswith('plugins.'):
            service = s['plugins']
            if name == 'plugins.catalog':
                return service.catalog(owner)
            if name == 'plugins.targets':
                return service.targets(owner, p['plugin_id'])
            if name == 'plugins.configure':
                return service.configure(owner, p['plugin_id'], p['enabled'], p['revision'])
            if name == 'plugins.set_target':
                return service.set_target(owner, p['plugin_id'], p['node_id'], p['enabled'])
            if name == 'plugins.submit':
                return service.submit(owner, p['plugin_id'], p['node_id'], p['action'], p['arguments'], key)
            if name == 'plugins.operation':
                return service.operation(owner, p['plugin_id'], p['operation_id'])
        if name == 'services.register':
            return s['service_health'].register(owner, p['value'])
        if name == 'usage.summary':
            return s['usage_summary'].summary(owner, p.get('params', {}))
        if name == 'commands.get':
            return s['command_inspection'].get(owner, p['command_id'])
        if name == 'commands.unknown':
            return s['command_inspection'].list_unknown(owner, limit=p.get('limit', 50))
        if name.startswith('memory.'):
            service = s['platform_memory']
            if name == 'memory.list':
                return service.list(owner, limit=p.get('limit', 50))
            if name == 'memory.search':
                return service.search(owner, p['query'], limit=p.get('limit', 20))
            if name == 'memory.get':
                return service.get(owner, p['memory_id'])
            if name == 'memory.create':
                return service.create(owner, {'memory_id': key, **p['value']})
            if name == 'memory.update':
                return service.update(owner, p['memory_id'], p['value'], expected_revision=p['expected_revision'])
            if name == 'memory.delete':
                return service.delete(owner, p['memory_id'], expected_revision=p['expected_revision'])
        if name.startswith('schedules.'):
            service = s['platform_schedules']
            if name == 'schedules.list':
                return service.list(owner, limit=p.get('limit', 100))
            if name == 'schedules.get':
                return service.get(owner, p['schedule_id'])
            if name == 'schedules.create':
                return service.create(owner, {'schedule_id': key, **p['value']})
            if name == 'schedules.update':
                return service.update(owner, p['schedule_id'], p['value'], expected_revision=p['expected_revision'])
            if name == 'schedules.delete':
                return service.delete(owner, p['schedule_id'])
        if name == 'adoption.candidates':
            return s['adoptions'].list_candidates(p['machine_id'])
        if name == 'adoption.adopt':
            return s['adoptions'].adopt(p['machine_id'], p['pid'], p['started_at'], owner)
        if name == 'adoption.revoke':
            s['adoptions'].revoke(p['session_id'], owner)
            return {'revoked': True, 'session_id': p['session_id']}
        if name.startswith('workflows.'):
            return self._workflow(name, p, owner, key)
        if name.startswith('mcp.'):
            return self._mcp(name, p)
        raise ApplicationError('operation_unavailable', '管理操作不可用', 400)

    def _workflow(self, name, p, owner, key):
        from hub.domain.workflow_operator import WorkflowCreateRequest
        from hub.domain.workflow_control import WorkflowControlRequest
        from hub.domain.workflow_contract import parse_input_references
        service = self.services['workflows']
        if name == 'workflows.templates':
            return service.templates(owner)
        if name == 'workflows.list':
            return asyncio.run(service.list(owner, limit=p.get('limit', 25), cursor=p.get('cursor')))
        if name == 'workflows.detail':
            return asyncio.run(service.detail(owner, p['workflow_id']))
        if name == 'workflows.events':
            return asyncio.run(service.events(owner, p['workflow_id'], after=p.get('after', 0), limit=p.get('limit', 50)))
        value = dict(p['value'])
        if 'owner_id' in value or 'client_token' in value:
            raise ApplicationError('invalid_arguments', '工作流身份与幂等标识由运行时提供', 400)
        if 'input_refs' in value:
            value['input_refs'] = parse_input_references(value['input_refs'])
        if name == 'workflows.create':
            value.setdefault('input_refs', ())
            return asyncio.run(service.create(WorkflowCreateRequest(owner_id=owner, client_token=key, **value)))
        return asyncio.run(service.control(WorkflowControlRequest(owner_id=owner, client_token=key, **value)))

    def _mcp(self, name, p):
        from hub.application.mcp_registration import MCPRegistrationCommand
        from hub.domain.mcp_administration import MCPAdminCommand
        service = self.services['mcp_catalog_admin']
        if name == 'mcp.list':
            return service.list_page(p.get('after'), p.get('limit', 8))
        if name == 'mcp.detail':
            return service.detail(p['connector_id'])
        if name == 'mcp.discover':
            return service.discover(p['connector_id'], p['expected_epoch'])
        if name == 'mcp.register':
            return service.register(MCPRegistrationCommand.parse(p['value']))
        return service.manage(p['connector_id'], MCPAdminCommand.parse(p['action'], p['value']))
