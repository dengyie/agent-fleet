"""Strict private MCP snapshots using the original transport/schema validators."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from typing import Any

from hub.domain.mcp_catalog import MAX_CATALOG_INTEGER
from .mcp import (
    MAX_EXPOSED_TOOLS, MAX_TOTAL_SCHEMA_BYTES, MCPConnectorConfig, MCPConnectorError,
    MCPDiscovery, MCPTool, PROTOCOL_VERSION, _IDENTIFIER, _canonical, _validate_schema,
)

MAX_CONFIG_BYTES = 8192
MAX_DISCOVERY_BYTES = 512 * 1024
_CONFIG_FIELDS = frozenset(MCPConnectorConfig.__dataclass_fields__)
_DISCOVERY_FIELDS = frozenset(MCPDiscovery.__dataclass_fields__)
_TOOL_FIELDS = frozenset(MCPTool.__dataclass_fields__)


def _bounded_json(raw: bytes, maximum: int) -> dict[str, Any]:
    if type(raw) is not bytes or not 1 <= len(raw) <= maximum:
        raise MCPConnectorError("connector_snapshot_invalid")
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise MCPConnectorError("connector_snapshot_invalid")
            result[key] = value
        return result
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
        if type(value) is not dict or _canonical(value) != raw:
            raise MCPConnectorError("connector_snapshot_invalid")
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise MCPConnectorError("connector_snapshot_invalid", cause=exc) from exc


def encode_config(config: MCPConnectorConfig) -> bytes:
    if type(config) is not MCPConnectorConfig:
        raise MCPConnectorError("invalid_connector")
    # Revalidate even a forged or externally replaced frozen dataclass.
    config = MCPConnectorConfig(**asdict(config))
    value = asdict(config)
    value["selected_tools"] = sorted(config.selected_tools)
    value["timeout_s"] = float(config.timeout_s)
    raw = _canonical(value)
    if len(raw) > MAX_CONFIG_BYTES:
        raise MCPConnectorError("connector_snapshot_invalid")
    return raw


def decode_config(raw: bytes) -> MCPConnectorConfig:
    value = _bounded_json(raw, MAX_CONFIG_BYTES)
    if value.keys() != _CONFIG_FIELDS or type(value["selected_tools"]) is not list:
        raise MCPConnectorError("connector_snapshot_invalid")
    try:
        value["selected_tools"] = tuple(value["selected_tools"])
        config = MCPConnectorConfig(**value)
        if encode_config(config) != raw:
            raise MCPConnectorError("connector_snapshot_invalid")
        return config
    except (ValueError, TypeError) as exc:
        raise MCPConnectorError("connector_snapshot_invalid", cause=exc) from exc


def encode_discovery(discovery: MCPDiscovery) -> bytes:
    if type(discovery) is not MCPDiscovery:
        raise MCPConnectorError("connector_snapshot_invalid")
    value = asdict(discovery)
    value["tools"] = sorted(value["tools"], key=lambda tool: tool["name"])
    raw = _canonical(value)
    if len(raw) > MAX_DISCOVERY_BYTES:
        raise MCPConnectorError("connector_snapshot_invalid")
    return raw


def discovery_digest(discovery: MCPDiscovery) -> str:
    # Revision is a catalog fact, never part of the external content identity.
    return hashlib.sha256(encode_discovery(replace(discovery, revision=1))).hexdigest()


def _text(value: object, maximum: int) -> str:
    if type(value) is not str or len(value) > maximum:
        raise MCPConnectorError("connector_snapshot_invalid")
    return value


def _tool(value: object) -> MCPTool:
    if type(value) is not dict or value.keys() != _TOOL_FIELDS:
        raise MCPConnectorError("connector_snapshot_invalid")
    if type(value["name"]) is not str or not _IDENTIFIER.fullmatch(value["name"]):
        raise MCPConnectorError("connector_snapshot_invalid")
    _text(value["description"], 8192)
    if type(value["server_annotations"]) is not dict:
        raise MCPConnectorError("connector_snapshot_invalid")
    input_schema = _validate_schema(value["input_schema"])
    output_schema = value["output_schema"]
    if output_schema is not None:
        _validate_schema(output_schema)
    output_digest = hashlib.sha256(_canonical(output_schema)).hexdigest() if output_schema is not None else None
    if (value["schema_digest"] != hashlib.sha256(_canonical(input_schema)).hexdigest()
            or value["output_schema_digest"] != output_digest):
        raise MCPConnectorError("connector_snapshot_invalid")
    return MCPTool(**value)


def decode_discovery(raw: bytes, config: MCPConnectorConfig) -> MCPDiscovery:
    value = _bounded_json(raw, MAX_DISCOVERY_BYTES)
    if (value.keys() != _DISCOVERY_FIELDS or value["protocol_version"] != PROTOCOL_VERSION
            or type(value["revision"]) is not int or not 1 <= value["revision"] <= MAX_CATALOG_INTEGER
            or type(value["capabilities"]) is not dict or type(value["tools"]) is not list
            or not 1 <= len(value["tools"]) <= min(MAX_EXPOSED_TOOLS, config.max_tools)):
        raise MCPConnectorError("connector_snapshot_invalid")
    _text(value["server_name"], 256)
    _text(value["server_version"], 128)
    tools = tuple(_tool(tool) for tool in value["tools"])
    if len({tool.name for tool in tools}) != len(tools) or {tool.name for tool in tools} != set(config.selected_tools):
        raise MCPConnectorError("connector_snapshot_invalid")
    schema_bytes = sum(len(_canonical(tool.input_schema)) + (
        len(_canonical(tool.output_schema)) if tool.output_schema is not None else 0) for tool in tools)
    if schema_bytes > MAX_TOTAL_SCHEMA_BYTES:
        raise MCPConnectorError("schema_total_too_large")
    discovery = MCPDiscovery(**{**value, "tools": tools})
    if encode_discovery(discovery) != raw:
        raise MCPConnectorError("connector_snapshot_invalid")
    return discovery
