"""Explicit, secret-free MCP management DTOs and deterministic commands."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from .mcp_catalog import MAX_CATALOG_INTEGER, MCPCatalogError

MCPAdminState = Literal["registered", "active", "pending", "revoked", "restore_disabled"]
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


def connector_identifier(value: object) -> str:
    if type(value) is not str or not _IDENTIFIER.fullmatch(value):
        raise MCPCatalogError("invalid_connector_id")
    return value


def catalog_page(after: object, limit: object) -> tuple[str | None, int]:
    if type(limit) is not int or not 1 <= limit <= 8:
        raise MCPCatalogError("invalid_catalog_page")
    return (None if after is None else connector_identifier(after)), limit


@dataclass(frozen=True, slots=True)
class MCPAdminCommand:
    action: Literal["admit", "revoke"]
    expected_epoch: int
    pending_revision: int | None = None

    @classmethod
    def parse(cls, action: str, payload: object) -> MCPAdminCommand:
        if action not in ("admit", "revoke") or type(payload) is not dict:
            raise MCPCatalogError("invalid_catalog_command")
        fields = {"expected_epoch", "pending_revision"} if action == "admit" else {"expected_epoch"}
        if payload.keys() - fields:
            raise MCPCatalogError("invalid_catalog_command")
        if fields - payload.keys():
            raise MCPCatalogError("precondition_required")
        if any(type(value) is not int or not 1 <= value <= MAX_CATALOG_INTEGER for value in payload.values()):
            raise MCPCatalogError("invalid_catalog_command")
        operation: Literal["admit", "revoke"] = "admit" if action == "admit" else "revoke"
        return cls(operation, payload["expected_epoch"], payload.get("pending_revision"))


@dataclass(frozen=True, slots=True)
class MCPConnectorSummary:
    connector_id: str
    owner_id: str
    workspace_id: str
    endpoint_origin: str
    selected_tools: tuple[str, ...]
    risk: Literal["read_only"]
    retry_class: Literal["read_only"]
    timeout_s: float
    result_limit_bytes: int
    epoch: int
    active_revision: int | None
    pending_revision: int | None
    state: MCPAdminState
    adapter_attached: bool
    runtime_ready: bool
    created_at: str
    updated_at: str
    revoked_at: str | None


@dataclass(frozen=True, slots=True)
class MCPAdminTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    schema_digest: str
    output_schema_digest: str | None
    server_annotations: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MCPAdminDiscovery:
    revision: int
    protocol_version: str
    server_name: str
    server_version: str
    capabilities: dict[str, Any]
    tools: tuple[MCPAdminTool, ...]
    content_digest: str


@dataclass(frozen=True, slots=True)
class MCPConnectorDetail:
    summary: MCPConnectorSummary
    active: MCPAdminDiscovery | None
    pending: MCPAdminDiscovery | None


@dataclass(frozen=True, slots=True)
class MCPConnectorPage:
    connectors: tuple[MCPConnectorSummary, ...]
    next_cursor: str | None
