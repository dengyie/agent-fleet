"""Fixed web-agent tool contract shared by Hub and local Node."""
from collections.abc import Mapping

from .muse import MAX_PROMPT, MuseBridgeError, _identity

WEB_AGENT_TOOLS = frozenset({'web_agent.status', 'web_agent.submit', 'web_agent.read'})
WEB_AGENT_DEFINITIONS = (
    {'name': 'web_agent.status', 'risk': 'read_only', 'scope_kind': 'workspace',
     'description': 'Check the explicitly connected local Muse agent without reading chat history.',
     'parameters': {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}},
    {'name': 'web_agent.submit', 'risk': 'write', 'scope_kind': 'workspace',
     'description': 'Delegate a user-authorized task to the connected Muse account. Use a unique task_id and preserve it for retries. Returns dispatch status, not task completion. Read with web_agent.read. Unknown outcomes must not be resubmitted with a new ID. Muse keeps its own approvals.',
     'parameters': {'type': 'object', 'properties': {
         'task_id': {'type': 'string', 'minLength': 1, 'maxLength': 128},
         'prompt': {'type': 'string', 'minLength': 1, 'maxLength': MAX_PROMPT}},
         'required': ['task_id', 'prompt'], 'additionalProperties': False}},
    {'name': 'web_agent.read', 'risk': 'read_only', 'scope_kind': 'workspace',
     'description': 'Read one delegated Muse task. completed/failed are agent-reported outcomes, not independent verification. running/unknown/waiting_input are unfinished. Ask the user to open Muse for required input; never approve on their behalf.',
     'parameters': {'type': 'object', 'properties': {'task_id': {'type': 'string', 'minLength': 1, 'maxLength': 128}},
                    'required': ['task_id'], 'additionalProperties': False}},
)


def validate_arguments(tool, arguments):
    required = {'web_agent.status': set(), 'web_agent.submit': {'task_id', 'prompt'},
                'web_agent.read': {'task_id'}}.get(tool)
    if required is None or not isinstance(arguments, Mapping) or set(arguments) != required:
        raise MuseBridgeError('invalid_arguments')
    if 'task_id' in required:
        _identity(arguments['task_id'])
    if 'prompt' in required:
        prompt = arguments['prompt']
        if (not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT
                or any(ord(c) < 32 and c not in '\r\n\t' for c in prompt)):
            raise MuseBridgeError('prompt_invalid')
    return dict(arguments)


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
