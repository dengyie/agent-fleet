"""Explicit immutable MCP registration using the original durable authority."""
from __future__ import annotations

from dataclasses import asdict, dataclass

from hub.domain.mcp_catalog import MAX_CONNECTORS, MCPCatalogError, MCPCatalogRecord
from hub.platform.connectors.registration import connector_from_config, parse_mcp_registration
from hub.platform.connectors.snapshots import decode_config, encode_config
from .connector_catalog import ConnectorCatalog


@dataclass(frozen=True, slots=True)
class MCPRegistrationCommand:
    config_snapshot: bytes

    @classmethod
    def parse(cls, payload: object) -> MCPRegistrationCommand:
        if type(payload) is not dict or payload.keys() != {"connector"}:
            raise MCPCatalogError("invalid_mcp_registration_command")
        return cls(encode_config(parse_mcp_registration(payload["connector"])))


def register_mcp_connection(catalog: ConnectorCatalog, command: MCPRegistrationCommand) -> MCPCatalogRecord:
    if not catalog.enabled:
        raise MCPCatalogError("mcp_connectors_disabled")
    if type(command) is not MCPRegistrationCommand or catalog.store is None:
        raise MCPCatalogError("invalid_mcp_registration_command")
    config = decode_config(command.config_snapshot)
    row = asdict(config)
    row["selected_tools"] = list(row["selected_tools"])
    config = parse_mcp_registration(row)
    record = catalog.store.register(encode_config(config), now=catalog.clock(), require_available_workspace=True)
    if not record.disabled and config.connector_id not in catalog.attached_ids():
        catalog.register(connector_from_config(config))
    return record


def attach_durable_mcp_connections(catalog: ConnectorCatalog) -> None:
    if catalog.store is None or catalog.registration_factory is None:
        return
    after, count = None, 0
    while True:
        records, after = catalog.store.list_page(after, 8)
        count += len(records)
        if count > MAX_CONNECTORS:
            raise MCPCatalogError("too_many_connectors")
        catalog.attach_persisted(records)
        if after is None:
            return
