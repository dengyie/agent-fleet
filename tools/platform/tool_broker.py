"""Fixed tool allowlist in front of execution backends."""
from __future__ import annotations

from hub.domain.workspace import WorkspaceError
from hub.application.task_service import ApplicationError
from .artifacts import ArtifactError
from .backends.base import ToolReceipt
from .resource_lease import ResourceLeaseError


class ToolBroker:
    TOOLS = frozenset({
        "workspace.list", "workspace.read", "workspace.write", "workspace.exec",
        "workspace.artifact", "fleet.list_services", "service.get_health",
        "service.read_logs", "incident.get_evidence",
    })
    TOOL_DEFINITIONS = (
        {"name": "workspace.list", "risk": "read_only", "scope_kind": "workspace"},
        {"name": "workspace.read", "risk": "read_only", "scope_kind": "workspace"},
        {"name": "workspace.write", "risk": "write", "scope_kind": "workspace"},
        {"name": "workspace.exec", "risk": "restricted_exec", "scope_kind": "workspace"},
        {"name": "workspace.artifact", "risk": "write", "scope_kind": "workspace"},
        {"name": "fleet.list_services", "risk": "read_only", "scope_kind": "owner"},
        {"name": "service.get_health", "risk": "read_only", "scope_kind": "service"},
        {"name": "service.read_logs", "risk": "read_only", "scope_kind": "service"},
        {"name": "incident.get_evidence", "risk": "read_only", "scope_kind": "incident"},
    )

    def __init__(self, backend, leases, *, resource_id: str, artifact_store=None,
                 diagnostics=None, lease_owner_id: str | None = None,
                 artifact_workspace_id: str | None = None):
        self.backend = backend
        self.leases = leases
        self.resource_id = resource_id
        self.artifact_workspace_id = artifact_workspace_id or resource_id
        self.artifact_store = artifact_store
        self.diagnostics = diagnostics
        self.lease_owner_id = lease_owner_id

    @classmethod
    def tool_definitions(cls):
        return [dict(item) for item in cls.TOOL_DEFINITIONS]

    def execute(self, *, command_id: str, tool: str, arguments: dict, owner_id: str, epoch: int) -> ToolReceipt:
        if tool not in self.TOOLS:
            return ToolReceipt(command_id, "failed", {}, "unknown_tool")
        if not self.leases.validate(self.resource_id, self.lease_owner_id or owner_id, epoch):
            return ToolReceipt(command_id, "failed", {}, "lease_mismatch")
        if not isinstance(arguments, dict):
            return ToolReceipt(command_id, "failed", {}, "invalid_arguments")
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
