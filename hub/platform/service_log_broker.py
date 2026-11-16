"""Read registered-service logs through existing signed Node commands."""
from __future__ import annotations

from fleet_core.backends.base import ToolReceipt
from fleet_core.command_timing import CommandTimingPolicy
from fleet_core.node_action_contract import SERVICE_LOG_ACTION
from fleet_core.platform_command import PlatformCommand
from hub.application.errors import ApplicationError
from hub.platform.remote_tool_broker import RemoteToolBroker


class ServiceLogBroker:
    def __init__(self, diagnostics, repository, delivery, *, run_id, clock,
                 timing_policy: CommandTimingPolicy, should_cancel=None, event_sink=None):
        self.diagnostics = diagnostics
        self.repository = repository
        self.delivery = delivery
        self.run_id = run_id
        self.clock = clock
        self.timing_policy = timing_policy
        self.should_cancel = should_cancel
        self.event_sink = event_sink or (lambda kind, payload: None)

    def _node_available(self, owner_id, service):
        node = self.repository.get_node(owner_id, service['node_id'])
        return bool(service.get('enabled', True) and service['adapter'] in {'systemd', 'supervisor', 'docker'}
                    and node and node.get('enabled')
                    and node.get('capabilities', {}).get(SERVICE_LOG_ACTION) is True)

    def available(self, owner_id):
        return any(self._node_available(owner_id, service)
                   for service in self.diagnostics.service_repository.list_services(owner_id))

    def execute(self, *, command_id, tool, arguments, owner_id, epoch):
        if tool != 'service.read_logs':
            return ToolReceipt(command_id, 'failed', {}, 'tool_not_allowed')
        if (not isinstance(arguments, dict) or 'service_id' not in arguments
                or set(arguments) - {'service_id', 'window_s', 'max_bytes'}):
            return ToolReceipt(command_id, 'failed', {}, 'invalid_arguments')
        if not command_id.startswith(self.run_id + ':step:'):
            return ToolReceipt(command_id, 'failed', {}, 'invalid_command_id')
        if self.should_cancel is not None and self.should_cancel():
            return ToolReceipt(command_id, 'failed', {}, 'execution_cancelled')
        try:
            service, window, byte_limit = self.diagnostics.prepare_log_query(owner_id, **arguments)
            if not self._node_available(owner_id, service):
                return ToolReceipt(command_id, 'failed', {}, 'service_logs_unavailable')
            timing = self.timing_policy.start(float(self.clock()))
            command = PlatformCommand.create(
                command_id=command_id, owner_id=owner_id, run_id=self.run_id,
                target_node=service['node_id'], resource_id=service['service_id'],
                action=SERVICE_LOG_ACTION, retry_class='read_only', expires_at=timing.hard_deadline,
                arguments={'adapter': service['adapter'], 'target_alias': service['target_alias'],
                           'service_version': service['version'], 'action': 'logs',
                           'window_s': window, 'max_bytes': byte_limit},
            )
            self.delivery.enqueue(command, idempotency_key=command_id, timing=timing)
            self.event_sink('node_command_queued', {
                'command_id': command_id, 'node_id': service['node_id'], 'tool': tool,
            })
            return RemoteToolBroker(self.delivery, node_id=service['node_id'],
                resource_id=service['service_id'], run_id=self.run_id,
                event_sink=self.event_sink, should_cancel=self.should_cancel).observe(command_id=command_id, tool=tool)
        except ApplicationError as error:
            return ToolReceipt(command_id, 'failed', {}, error.code)
