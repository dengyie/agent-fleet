"""Fixed tool allowlist in front of execution backends."""
from __future__ import annotations

from hub.domain.workspace import WorkspaceError
from hub.application.task_service import ApplicationError
from .artifacts import ArtifactError
from .backends.base import ToolReceipt
from .browser_backend import BrowserBackendError
from .resource_lease import ResourceLeaseError
from .node_executor import BROWSER_TOOLS


class ToolBroker:
    CONVERSATION_TOOLS = frozenset({
        "conversation.list", "conversation.rename", "conversation.archive",
        "conversation.restore",
    })
    LOCAL_ONLY_TOOLS = CONVERSATION_TOOLS | frozenset({
        "platform.get_defaults", "platform.update_defaults", "service.request_action",
    })
    TOOLS = frozenset({
        "workspace.list", "workspace.read", "workspace.write", "workspace.exec",
        "workspace.artifact", "fleet.list_services", "service.get_health",
        "service.read_logs", "incident.get_evidence", "service.request_action",
        "platform.get_defaults", "platform.update_defaults",
    }) | BROWSER_TOOLS
    TOOLS = TOOLS | CONVERSATION_TOOLS
    _BASE_TOOL_DEFINITIONS = ({'name': 'workspace.list',
      'risk': 'read_only',
      'scope_kind': 'workspace',
      'description': 'List files in the selected workspace. Omit path to list the root.',
      'parameters': {'type': 'object',
                     'properties': {'path': {'type': 'string',
                                             'description': 'Path relative to the selected workspace; never an '
                                                            'absolute path.'}},
                     'required': [],
                     'additionalProperties': False}},
     {'name': 'workspace.read',
      'risk': 'read_only',
      'scope_kind': 'workspace',
      'description': 'Read a UTF-8 file in the selected workspace.',
      'parameters': {'type': 'object',
                     'properties': {'path': {'type': 'string',
                                             'description': 'Path relative to the selected workspace; never an '
                                                            'absolute path.'}},
                     'required': ['path'],
                     'additionalProperties': False}},
     {'name': 'workspace.write',
      'risk': 'write',
      'scope_kind': 'workspace',
      'description': 'Create or overwrite a UTF-8 file. Use path for its relative filename and content for its '
                     'complete text.',
      'parameters': {'type': 'object',
                     'properties': {'path': {'type': 'string',
                                             'description': 'Path relative to the selected workspace; never an '
                                                            'absolute path.'},
                                    'content': {'type': 'string', 'description': 'Full file text.'}},
                     'required': ['path', 'content'],
                     'additionalProperties': False}},
     {'name': 'workspace.exec',
      'risk': 'restricted_exec',
      'scope_kind': 'workspace',
      'description': 'Execute an allowed command in the workspace. Requires a configured execution sandbox.',
      'parameters': {'type': 'object',
                     'properties': {'argv': {'type': 'array',
                                             'items': {'type': 'string'},
                                             'minItems': 1,
                                             'description': 'Executable followed by arguments; not a shell '
                                                            'command string.'},
                                    'timeout_s': {'type': 'number', 'minimum': 0.1, 'maximum': 30}},
                     'required': ['argv'],
                     'additionalProperties': False}},
     {'name': 'workspace.artifact',
      'risk': 'write',
      'scope_kind': 'workspace',
      'description': 'Publish an existing workspace file as a downloadable artifact.',
      'parameters': {'type': 'object',
                     'properties': {'path': {'type': 'string',
                                             'description': 'Path relative to the selected workspace; never an '
                                                            'absolute path.'},
                                    'name': {'type': 'string', 'description': 'Display filename.'},
                                    'content_type': {'type': 'string', 'description': 'MIME type.'}},
                     'required': ['path'],
                     'additionalProperties': False}},
     {'name': 'fleet.list_services',
      'risk': 'read_only',
      'scope_kind': 'owner',
      'description': 'List the current operator service catalog and health summaries.',
      'parameters': {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}},
     {'name': 'service.get_health',
      'risk': 'read_only',
      'scope_kind': 'service',
      'description': 'Get health evidence for a registered service.',
      'parameters': {'type': 'object',
                     'properties': {'service_id': {'type': 'string',
                                                   'description': 'ID returned by fleet.list_services.'}},
                     'required': ['service_id'],
                     'additionalProperties': False}},
     {'name': 'service.read_logs',
      'risk': 'read_only',
      'scope_kind': 'service',
      'description': 'Read bounded logs for a registered service.',
      'parameters': {'type': 'object',
                     'properties': {'service_id': {'type': 'string',
                                                   'description': 'ID returned by fleet.list_services.'},
                                    'window_s': {'type': 'integer', 'minimum': 1},
                                    'max_bytes': {'type': 'integer', 'minimum': 1}},
                     'required': ['service_id'],
                     'additionalProperties': False}},
     {'name': 'incident.get_evidence',
      'risk': 'read_only',
      'scope_kind': 'incident',
      'description': 'Read evidence for an incident.',
      'parameters': {'type': 'object',
                     'properties': {'incident_id': {'type': 'string', 'description': 'Registered incident ID.'}},
                     'required': ['incident_id'],
                     'additionalProperties': False}},
     {'name': 'conversation.list', 'risk': 'read_only', 'scope_kind': 'owner',
      'description': 'List a bounded set of the current owner’s active or archived conversations.',
      'parameters': {'type': 'object', 'properties': {
          'archived': {'type': 'boolean'},
          'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20}},
          'required': [], 'additionalProperties': False}},
     {'name': 'conversation.rename', 'risk': 'write', 'scope_kind': 'conversation',
      'description': 'Rename one of the current owner’s conversations.',
      'parameters': {'type': 'object', 'properties': {
          'conversation_id': {'type': 'string'}, 'title': {'type': 'string', 'minLength': 1, 'maxLength': 120}},
          'required': ['conversation_id', 'title'], 'additionalProperties': False}},
     {'name': 'conversation.archive', 'risk': 'write', 'scope_kind': 'conversation',
      'description': 'Archive one of the current owner’s conversations.',
      'parameters': {'type': 'object', 'properties': {'conversation_id': {'type': 'string'}},
          'required': ['conversation_id'], 'additionalProperties': False}},
     {'name': 'conversation.restore', 'risk': 'write', 'scope_kind': 'conversation',
      'description': 'Restore one of the current owner’s archived conversations.',
      'parameters': {'type': 'object', 'properties': {'conversation_id': {'type': 'string'}},
          'required': ['conversation_id'], 'additionalProperties': False}},
     {'name': 'platform.get_defaults', 'risk': 'read_only', 'scope_kind': 'owner',
      'description': 'Read the current owner’s default model, workspace, execution node, and safe resource catalogs.',
      'parameters': {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}},
     {'name': 'platform.update_defaults', 'risk': 'write', 'scope_kind': 'owner',
      'description': 'Replace the current owner’s default model, workspace, and execution node using the revision returned by platform.get_defaults. Use null to clear a selection.',
      'parameters': {'type': 'object', 'properties': {
          'expected_revision': {'type': 'integer', 'minimum': 0},
          'model_profile_id': {'type': ['string', 'null']},
          'workspace_id': {'type': ['string', 'null']},
          'execution_node_id': {'type': ['string', 'null']}},
          'required': ['expected_revision', 'model_profile_id', 'workspace_id', 'execution_node_id'],
          'additionalProperties': False}},
     {'name': 'service.request_action', 'risk': 'write', 'scope_kind': 'service',
      'description': 'Request a fixed action allowed by a registered service. Restart always creates an owner approval grant.',
      'parameters': {'type': 'object', 'properties': {
          'service_id': {'type': 'string'},
          'action': {'type': 'string', 'enum': ['inspect', 'restart']}},
          'required': ['service_id', 'action'], 'additionalProperties': False}},)
    _BROWSER_TOOL_DEFINITIONS = (
        {'name': 'browser.open', 'risk': 'read_only', 'scope_kind': 'browser',
         'description': 'Open an allowed URL in an isolated browser session.',
         'parameters': {'type': 'object', 'properties': {'url': {'type': 'string'}},
                        'required': ['url'], 'additionalProperties': False}},
        {'name': 'browser.navigate', 'risk': 'read_only', 'scope_kind': 'browser',
         'description': 'Navigate an existing browser session to an allowed URL.',
         'parameters': {'type': 'object', 'properties': {
             'session_id': {'type': 'string'}, 'url': {'type': 'string'}},
                        'required': ['session_id', 'url'], 'additionalProperties': False}},
        {'name': 'browser.snapshot', 'risk': 'read_only', 'scope_kind': 'browser',
         'description': 'Read bounded visible text from a browser session.',
         'parameters': {'type': 'object', 'properties': {'session_id': {'type': 'string'}},
                        'required': ['session_id'], 'additionalProperties': False}},
        {'name': 'browser.screenshot', 'risk': 'read_only', 'scope_kind': 'browser',
         'description': 'Capture a PNG screenshot from a browser session.',
         'parameters': {'type': 'object', 'properties': {'session_id': {'type': 'string'}},
                        'required': ['session_id'], 'additionalProperties': False}},
        {'name': 'browser.click', 'risk': 'write', 'scope_kind': 'browser',
         'description': 'Click a bounded selector in a browser session.',
         'parameters': {'type': 'object', 'properties': {
             'session_id': {'type': 'string'}, 'selector': {'type': 'string'}},
                        'required': ['session_id', 'selector'], 'additionalProperties': False}},
        {'name': 'browser.type', 'risk': 'write', 'scope_kind': 'browser',
         'description': 'Type non-sensitive bounded text into a browser selector.',
         'parameters': {'type': 'object', 'properties': {
             'session_id': {'type': 'string'}, 'selector': {'type': 'string'},
             'text': {'type': 'string'}},
                        'required': ['session_id', 'selector', 'text'], 'additionalProperties': False}},
        {'name': 'browser.scroll', 'risk': 'read_only', 'scope_kind': 'browser',
         'description': 'Scroll a browser session by a bounded delta.',
         'parameters': {'type': 'object', 'properties': {
             'session_id': {'type': 'string'}, 'delta_y': {'type': 'integer'}},
                        'required': ['session_id', 'delta_y'], 'additionalProperties': False}},
        {'name': 'browser.back', 'risk': 'read_only', 'scope_kind': 'browser',
         'description': 'Navigate back in a browser session.',
         'parameters': {'type': 'object', 'properties': {'session_id': {'type': 'string'}},
                        'required': ['session_id'], 'additionalProperties': False}},
        {'name': 'browser.close', 'risk': 'write', 'scope_kind': 'browser',
         'description': 'Close a browser session.',
         'parameters': {'type': 'object', 'properties': {'session_id': {'type': 'string'}},
                        'required': ['session_id'], 'additionalProperties': False}},
    )
    _SUBMIT_TOOL_DEFINITION = {'name': 'browser.submit', 'risk': 'write', 'scope_kind': 'browser',
         'description': 'Submit the form enclosing an approved selector after owner approval.',
         'parameters': {'type': 'object', 'properties': {
             'session_id': {'type': 'string'}, 'selector': {'type': 'string'}},
                        'required': ['session_id', 'selector'], 'additionalProperties': False}}

    def __init__(self, backend, leases, *, resource_id: str, artifact_store=None,
                 diagnostics=None, lease_owner_id: str | None = None,
                 artifact_workspace_id: str | None = None, allowed_tools=None,
                 browser_backend=None, browser_enabled: bool = False,
                 browser_submit_enabled: bool = False, conversation_service=None,
                 service_actions=None, defaults_service=None):
        self.backend = backend
        self.leases = leases
        self.resource_id = resource_id
        self.artifact_workspace_id = artifact_workspace_id or resource_id
        self.artifact_store = artifact_store
        self.diagnostics = diagnostics
        self.lease_owner_id = lease_owner_id
        self.browser_backend = browser_backend
        self.browser_enabled = bool(browser_enabled)
        self.browser_submit_enabled = bool(browser_submit_enabled)
        self.conversation_service = conversation_service
        self.service_actions = service_actions
        self.defaults_service = defaults_service
        self.allowed_tools = self.TOOLS if allowed_tools is None else self.TOOLS.intersection(allowed_tools)

    @classmethod
    def tool_definitions(cls, allowed_tools=None, *, browser_enabled: bool = False,
                         browser_submit_enabled: bool = False,
                         service_actions_enabled: bool = False):
        definitions = [dict(item) for item in cls._BASE_TOOL_DEFINITIONS]
        if browser_enabled:
            definitions.extend(dict(item) for item in cls._BROWSER_TOOL_DEFINITIONS)
            if browser_submit_enabled:
                definitions.append(dict(cls._SUBMIT_TOOL_DEFINITION))
        if not service_actions_enabled:
            definitions = [item for item in definitions
                           if item["name"] != "service.request_action"]
        allowed = cls.TOOLS if allowed_tools is None else cls.TOOLS.intersection(allowed_tools)
        return [item for item in definitions if item["name"] in allowed]

    def execute(self, *, command_id: str, tool: str, arguments: dict, owner_id: str, epoch: int) -> ToolReceipt:
        if tool not in self.TOOLS:
            return ToolReceipt(command_id, "failed", {}, "unknown_tool")
        if tool not in self.allowed_tools:
            return ToolReceipt(command_id, "failed", {}, "tool_not_allowed")
        if not self.leases.validate(self.resource_id, self.lease_owner_id or owner_id, epoch):
            return ToolReceipt(command_id, "failed", {}, "lease_mismatch")
        if not isinstance(arguments, dict):
            return ToolReceipt(command_id, "failed", {}, "invalid_arguments")
        if tool in BROWSER_TOOLS:
            if not self.browser_enabled or self.browser_backend is None:
                return ToolReceipt(command_id, "failed", {}, "browser_disabled")
            if tool == "browser.submit":
                return ToolReceipt(command_id, "failed", {}, "submit_disabled")
            try:
                result = self.browser_backend.execute(tool, dict(arguments))
            except BrowserBackendError as exc:
                return ToolReceipt(command_id, "failed", {}, exc.code)
            except Exception:
                raise
            if not isinstance(result, dict):
                return ToolReceipt(command_id, "failed", {}, "invalid_backend_receipt")
            return ToolReceipt(command_id, "succeeded", result)
        def with_command_id(receipt):
            if receipt.command_id == command_id:
                return receipt
            return ToolReceipt(command_id, receipt.state, receipt.result, receipt.error_code, receipt.truncated)

        try:
            if tool == "platform.get_defaults":
                if self.defaults_service is None:
                    return ToolReceipt(command_id, "failed", {}, "platform_controls_unavailable")
                if arguments:
                    return ToolReceipt(command_id, "failed", {}, "invalid_arguments")
                snapshot = self.defaults_service.get_defaults(owner_id)
                defaults = snapshot["defaults"]
                return ToolReceipt(command_id, "succeeded", {
                    "defaults": {key: defaults.get(key) for key in (
                        "model_profile_id", "workspace_id", "execution_node_id", "revision")},
                    "models": [{key: row.get(key) for key in (
                        "profile_id", "provider", "model", "enabled")}
                        for row in snapshot["models"][:50]],
                    "workspaces": [{key: row.get(key) for key in (
                        "workspace_id", "name", "backend", "enabled")}
                        for row in snapshot["workspaces"][:50]],
                    "nodes": [{key: row.get(key) for key in (
                        "node_id", "label", "capabilities", "enabled", "status")}
                        for row in snapshot["nodes"][:50]],
                })
            if tool == "platform.update_defaults":
                if self.defaults_service is None:
                    return ToolReceipt(command_id, "failed", {}, "platform_controls_unavailable")
                if set(arguments) != {
                    "expected_revision", "model_profile_id", "workspace_id", "execution_node_id",
                }:
                    return ToolReceipt(command_id, "failed", {}, "invalid_arguments")
                revision = arguments.get("expected_revision")
                values = {key: arguments.get(key) for key in (
                    "model_profile_id", "workspace_id", "execution_node_id")}
                if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                    return ToolReceipt(command_id, "failed", {}, "invalid_arguments")
                updated = self.defaults_service.update_defaults(
                    owner_id, values, expected_revision=revision)["defaults"]
                updated.pop("owner_id", None)
                return ToolReceipt(command_id, "succeeded", {"defaults": updated})
            if tool in self.CONVERSATION_TOOLS:
                if self.conversation_service is None:
                    return ToolReceipt(command_id, "failed", {}, "conversation_controls_unavailable")
                valid_keys = {
                    "conversation.list": {"archived", "limit"},
                    "conversation.rename": {"conversation_id", "title"},
                    "conversation.archive": {"conversation_id"},
                    "conversation.restore": {"conversation_id"},
                }[tool]
                if set(arguments) - valid_keys:
                    return ToolReceipt(command_id, "failed", {}, "invalid_arguments")
                conversation_id = arguments.get("conversation_id")
                if tool == "conversation.list":
                    archived = arguments.get("archived", False)
                    limit = arguments.get("limit", 20)
                    if (not isinstance(archived, bool) or isinstance(limit, bool)
                            or not isinstance(limit, int) or not 1 <= limit <= 20):
                        return ToolReceipt(command_id, "failed", {}, "invalid_arguments")
                    result = self.conversation_service.list(
                        owner_id, limit=limit, archived=archived)
                    return ToolReceipt(command_id, "succeeded", result)
                if not isinstance(conversation_id, str) or not conversation_id:
                    return ToolReceipt(command_id, "failed", {}, "invalid_arguments")
                if tool == "conversation.rename":
                    result = self.conversation_service.rename(
                        owner_id, conversation_id, arguments.get("title"))
                    row = result["conversation"]
                    return ToolReceipt(command_id, "succeeded", {
                        "conversation_id": row["conversation_id"], "title": row["title"],
                        "archived_at": row["archived_at"],
                    })
                if tool == "conversation.archive":
                    result = self.conversation_service.set_archived(
                        owner_id, conversation_id, archived=True)
                else:
                    result = self.conversation_service.set_archived(
                        owner_id, conversation_id, archived=False)
                row = result["conversation"]
                return ToolReceipt(command_id, "succeeded", {
                    "conversation_id": row["conversation_id"],
                    "archived_at": row["archived_at"],
                    "archived": row["archived_at"] is not None,
                })
            if tool == "service.request_action":
                if self.service_actions is None:
                    return ToolReceipt(command_id, "failed", {}, "service_actions_disabled")
                if set(arguments) != {"service_id", "action"}:
                    return ToolReceipt(command_id, "failed", {}, "invalid_arguments")
                result = self.service_actions.request(
                    owner_id, arguments.get("service_id"), arguments.get("action"),
                    idempotency_key=command_id,
                )
                return ToolReceipt(command_id, "succeeded", result)
            if tool.startswith(("fleet.", "service.", "incident.")):
                if self.diagnostics is None:
                    return ToolReceipt(command_id, "failed", {}, "diagnostics_unavailable")
                if tool == "fleet.list_services":
                    return with_command_id(ToolReceipt(
                        command_id, "succeeded",
                        self.diagnostics.list_services(owner_id),
                    ))
                if tool == "service.get_health":
                    return with_command_id(ToolReceipt(
                        command_id, "succeeded",
                        self.diagnostics.get_health(owner_id, arguments.get("service_id")),
                    ))
                if tool == "service.read_logs":
                    return with_command_id(ToolReceipt(
                        command_id, "succeeded",
                        self.diagnostics.read_logs(
                            owner_id, arguments.get("service_id"),
                            window_s=arguments.get("window_s"),
                            max_bytes=arguments.get("max_bytes"),
                        ),
                    ))
                if tool == "incident.get_evidence":
                    return with_command_id(ToolReceipt(
                        command_id, "succeeded",
                        self.diagnostics.get_incident_evidence(
                            owner_id, arguments.get("incident_id"),
                        ),
                    ))
            if tool == "workspace.list":
                return with_command_id(self.backend.list(arguments.get("path", "")))
            if tool == "workspace.read":
                return with_command_id(self.backend.read(arguments.get("path", "")))
            if tool == "workspace.write":
                content = arguments.get("content")
                if not isinstance(content, str):
                    return ToolReceipt(command_id, "failed", {}, "invalid_content")
                return with_command_id(self.backend.write(arguments.get("path", ""), content.encode("utf-8")))
            if tool == "workspace.artifact":
                if self.artifact_store is None:
                    return ToolReceipt(command_id, "failed", {}, "artifact_unavailable")
                path = arguments.get("path")
                if not isinstance(path, str) or not path:
                    return ToolReceipt(command_id, "failed", {}, "invalid_arguments")
                resolver = getattr(self.backend, "_path", None)
                if not callable(resolver):
                    return ToolReceipt(command_id, "failed", {}, "artifact_unavailable")
                manifest = self.artifact_store.put_file(
                    owner_id, self.artifact_workspace_id, resolver(path),
                    name=arguments.get("name"), content_type=arguments.get("content_type"),
                )
                return ToolReceipt(command_id, "succeeded", {"artifact": manifest})
            argv = arguments.get("argv")
            return self.backend.execute(command_id, argv, timeout_s=arguments.get("timeout_s", 30))
        except ApplicationError as exc:
            return ToolReceipt(command_id, "failed", {}, exc.code)
        except (WorkspaceError, ResourceLeaseError, ArtifactError) as exc:
            return ToolReceipt(command_id, "failed", {}, exc.code)
        except (TypeError, ValueError):
            return ToolReceipt(command_id, "failed", {}, "invalid_arguments")
        except Exception:
            return ToolReceipt(command_id, "failed", {}, "tool_failed")


__all__ = ["ToolBroker"]
