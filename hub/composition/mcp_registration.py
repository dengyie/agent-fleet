"""Trusted, offline MCP adapter assembly for normal Hub startup and setup CLI."""
from __future__ import annotations

import re
import stat
from pathlib import Path

from hub.domain.mcp_catalog import MAX_CONNECTORS, MCPCatalogError
from hub.domain.workflow_errors import WorkflowValidationError
from hub.domain.workflow_json import decode_snapshot
from hub.platform.connectors.mcp import (
    MCPConnector,
    MCPConnectorConfig,
    UrllibMCPTransport,
)
from hub.platform.connectors.snapshots import encode_config
from hub.platform.providers.openai_compatible import EnvironmentSecretBroker

MAX_REGISTRATION_BYTES = 256 * 1024


def read_mcp_registration(path: Path) -> tuple[MCPConnector, ...]:
    """Validate the entire file before returning adapters; never resolve a secret."""
    try:
        if not path.is_absolute() or not stat.S_ISREG(path.stat().st_mode):
            raise MCPCatalogError("invalid_mcp_registration_file")
        with path.open("rb") as stream:
            value = decode_snapshot(stream.read(MAX_REGISTRATION_BYTES + 1), max_bytes=MAX_REGISTRATION_BYTES)
    except OSError as exc:
        raise MCPCatalogError("mcp_registration_file_unavailable", cause=exc) from exc
    except WorkflowValidationError as exc:
        raise MCPCatalogError("invalid_mcp_registration_json", cause=exc) from exc
    if (set(value) not in ({"schema_version", "connectors"}, {"schema_version", "connectors", "private_aliases"})
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or type(value["connectors"]) is not list or not 1 <= len(value["connectors"]) <= MAX_CONNECTORS):
        raise MCPCatalogError("invalid_mcp_registration_manifest")
    fields = set(MCPConnectorConfig.__dataclass_fields__)
    connectors, ids = [], set()
    broker = EnvironmentSecretBroker()
    aliases = value.get("private_aliases", {})
    if type(aliases) is not dict or len(aliases) > MAX_CONNECTORS:
        raise MCPCatalogError("invalid_mcp_private_aliases")
    transport = UrllibMCPTransport(private_aliases=aliases)
    for row in value["connectors"]:
        if (type(row) is not dict or set(row) not in (fields, fields - {"max_tools"})
                or type(row["selected_tools"]) is not list):
            raise MCPCatalogError("invalid_mcp_registration_fields")
        config = MCPConnectorConfig(**{**row, "selected_tools": tuple(row["selected_tools"])})
        encode_config(config)
        if not re.fullmatch(r"env://[A-Z0-9_]{1,128}", config.secret_ref):
            raise MCPCatalogError("mcp_registration_requires_environment_secret_ref")
        if config.connector_id in ids:
            raise MCPCatalogError("duplicate_mcp_registration")
        ids.add(config.connector_id)
        connectors.append(MCPConnector(config, transport=transport, secret_broker=broker))
    if set(aliases) - {item.config.endpoint for item in connectors}:
        raise MCPCatalogError("unused_mcp_private_alias")
    return tuple(connectors)


def validate_mcp_workspaces(connectors, platform) -> None:
    for connector in connectors:
        config = connector.config
        if not (platform.get_workspace(config.owner_id, config.workspace_id) or {}).get("enabled"):
            raise MCPCatalogError("mcp_registration_workspace_unavailable")
