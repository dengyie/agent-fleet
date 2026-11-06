"""Node-side web-agent executor over the shared contract in ``fleet_core``."""
from fleet_core.web_agent_contract import MuseBridgeError, _identity, validate_arguments


class WebAgentExecutor:
    def __init__(self, bridge, *, workspace_ids):
        self.bridge = bridge
        self.workspaces = frozenset(_identity(w, 'scope_invalid') for w in workspace_ids)
        if not self.workspaces:
            raise ValueError('Muse workspace allowlist required')

    def execute(self, tool, arguments, *, resource_id):
        if resource_id not in self.workspaces:
            raise MuseBridgeError('workspace_forbidden')
        args = validate_arguments(tool, arguments)
        if tool == 'web_agent.status':
            return self.bridge.status()
        if tool == 'web_agent.submit':
            return self.bridge.submit(args['task_id'], args['prompt'], scope=resource_id)
        return self.bridge.read(args['task_id'], scope=resource_id)
