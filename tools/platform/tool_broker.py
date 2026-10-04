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
    TOOLS = frozenset({
        "workspace.list", "workspace.read", "workspace.write", "workspace.exec",
        "workspace.artifact", "fleet.list_services", "service.get_health",
        "service.read_logs", "incident.get_evidence",
    }) | BROWSER_TOOLS
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
                     'additionalProperties': False}},)
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

    def __init__(self, backend, leases, *, resource_id: str, artifact_store=None,
                 diagnostics=None, lease_owner_id: str | None = None,
                 artifact_workspace_id: str | None = None, allowed_tools=None,
                 browser_backend=None, browser_enabled: bool = False):
        self.backend = backend
        self.leases = leases
        self.resource_id = resource_id
        self.artifact_workspace_id = artifact_workspace_id or resource_id
        self.artifact_store = artifact_store
        self.diagnostics = diagnostics
        self.lease_owner_id = lease_owner_id
        self.browser_backend = browser_backend
        self.browser_enabled = bool(browser_enabled)
        self.allowed_tools = self.TOOLS if allowed_tools is None else self.TOOLS.intersection(allowed_tools)

    @classmethod
    def tool_definitions(cls, allowed_tools=None, *, browser_enabled: bool = False):
        definitions = [dict(item) for item in cls._BASE_TOOL_DEFINITIONS]
        if browser_enabled:
            definitions.extend(dict(item) for item in cls._BROWSER_TOOL_DEFINITIONS)
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
