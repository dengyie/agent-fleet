"""Shared pure registration parsing and offline default adapter construction."""
from __future__ import annotations

import re
from dataclasses import asdict

from hub.domain.mcp_catalog import MCPCatalogError
from hub.platform.providers.openai_compatible import EnvironmentSecretBroker
from .mcp import MCPConnector, MCPConnectorConfig, UrllibMCPTransport
from .snapshots import decode_config, encode_config


def parse_mcp_registration(row: object) -> MCPConnectorConfig:
    fields = set(MCPConnectorConfig.__dataclass_fields__)
    if (type(row) is not dict or set(row) not in (fields, fields - {"max_tools"})
            or type(row["selected_tools"]) is not list):
        raise MCPCatalogError("invalid_mcp_registration_fields")
    try:
        config = MCPConnectorConfig(**{**row, "selected_tools": tuple(row["selected_tools"])})
        canonical = decode_config(encode_config(config))
    except MCPCatalogError as exc:
        if exc.code == "connector_snapshot_invalid":
            raise MCPCatalogError("invalid_mcp_registration_fields", cause=exc) from exc
        raise
    except (TypeError, ValueError) as exc:
        raise MCPCatalogError("invalid_mcp_registration_fields", cause=exc) from exc
    if not re.fullmatch(r"env://[A-Z0-9_]{1,128}", canonical.secret_ref):
        raise MCPCatalogError("mcp_registration_requires_environment_secret_ref")
    return config


def connector_from_config(config: MCPConnectorConfig) -> MCPConnector:
    row = asdict(config)
    row["selected_tools"] = list(row["selected_tools"])
    canonical = parse_mcp_registration(row)
    return MCPConnector(canonical, transport=UrllibMCPTransport(), secret_broker=EnvironmentSecretBroker())
