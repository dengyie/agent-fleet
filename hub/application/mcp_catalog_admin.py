"""MCP management projection and exact durable mutations; no discovery I/O."""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit

from hub.domain.mcp_administration import (
    MCPAdminCommand, MCPAdminDiscovery, MCPAdminState, MCPAdminTool, MCPConnectorDetail,
    MCPConnectorPage, MCPConnectorSummary, catalog_page, connector_identifier,
)
from hub.domain.mcp_catalog import MAX_CATALOG_INTEGER, MCPCatalogError, MCPCatalogRecord
from hub.platform.connectors.mcp import MCPDiscovery
from hub.platform.connectors.snapshots import decode_config, decode_discovery, discovery_digest
from .connector_catalog import ConnectorCatalog, MCPCatalogStore
from .mcp_registration import MCPRegistrationCommand, register_mcp_connection


def _utc(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _discovery(value: MCPDiscovery | None) -> MCPAdminDiscovery | None:
    if value is None:
        return None
    return MCPAdminDiscovery(value.revision, value.protocol_version, value.server_name, value.server_version,
        value.capabilities, tuple(MCPAdminTool(tool.name, tool.description, tool.input_schema,
            tool.output_schema, tool.schema_digest, tool.output_schema_digest, tool.server_annotations)
            for tool in value.tools), discovery_digest(value))


def _project(record: MCPCatalogRecord, attached: frozenset[str]) -> tuple[MCPConnectorSummary, MCPDiscovery | None, MCPDiscovery | None]:
    config = decode_config(record.config_snapshot)
    active = decode_discovery(record.active_snapshot, config) if record.active_snapshot else None
    pending = decode_discovery(record.pending_snapshot, config) if record.pending_snapshot else None
    state: MCPAdminState = "registered"
    if record.restore_disabled:
        state = "restore_disabled"
    elif record.revoked_at is not None:
        state = "revoked"
    elif pending is not None:
        state = "pending"
    elif active is not None:
        state = "active"
    endpoint = urlsplit(config.endpoint)
    origin = f"{endpoint.scheme}://{endpoint.netloc}"
    present = record.connector_id in attached
    summary = MCPConnectorSummary(config.connector_id, config.owner_id, config.workspace_id, origin,
        config.selected_tools, config.risk, config.retry_class, config.timeout_s, config.result_limit_bytes,
        record.epoch, active.revision if active else None, pending.revision if pending else None,
        state, present, present and active is not None and not record.disabled,
        _utc(record.created_at), _utc(record.updated_at), _utc(record.revoked_at) if record.revoked_at is not None else None)
    return summary, active, pending


def _detail(record: MCPCatalogRecord, attached: frozenset[str]) -> MCPConnectorDetail:
    summary, active, pending = _project(record, attached)
    return MCPConnectorDetail(summary, _discovery(active), _discovery(pending))


class MCPCatalogAdminService:
    def __init__(self, catalog: ConnectorCatalog, *, registration_enabled: bool = False) -> None:
        if catalog.store is None:
            raise MCPCatalogError("mcp_catalog_persistence_required")
        self.catalog = catalog
        self.store: MCPCatalogStore = catalog.store
        self.registration_enabled = bool(registration_enabled)

    def _registration(self) -> None:
        self._enabled()
        if not self.registration_enabled:
            raise MCPCatalogError("mcp_registration_disabled")

    def register(self, command: MCPRegistrationCommand) -> MCPConnectorDetail:
        self._registration()
        record = register_mcp_connection(self.catalog, command)
        return _detail(record, self.catalog.attached_ids())

    def discover(self, connector_id: str, expected_epoch: int) -> MCPConnectorDetail:
        self._registration()
        connector_id = connector_identifier(connector_id)
        if type(expected_epoch) is not int or not 1 <= expected_epoch <= MAX_CATALOG_INTEGER:
            raise MCPCatalogError("invalid_catalog_command")
        try:
            self.catalog.refresh(connector_id, expected_epoch=expected_epoch)
        except MCPCatalogError as exc:
            if exc.code != "connector_revision_pending":
                raise
        return self.detail(connector_id)

    def _enabled(self) -> None:
        if not self.catalog.enabled:
            raise MCPCatalogError("mcp_connectors_disabled")

    def list_page(self, after: str | None = None, limit: int = 8) -> MCPConnectorPage:
        self._enabled()
        after, limit = catalog_page(after, limit)
        records, cursor = self.store.list_page(after, limit)
        attached = self.catalog.attached_ids()
        return MCPConnectorPage(tuple(_project(record, attached)[0] for record in records), cursor)

    def detail(self, connector_id: str) -> MCPConnectorDetail:
        self._enabled()
        records = self.store.get_many((connector_identifier(connector_id),))
        if not records:
            raise MCPCatalogError("connector_not_found")
        return _detail(records[0], self.catalog.attached_ids())

    def manage(self, connector_id: str, command: MCPAdminCommand) -> MCPConnectorDetail:
        self._enabled()
        connector_id = connector_identifier(connector_id)
        if type(command) is not MCPAdminCommand:
            raise MCPCatalogError("invalid_catalog_command")
        # Revalidate direct application calls as well as parsed HTTP requests.
        payload: dict[str, object] = {"expected_epoch": command.expected_epoch}
        if command.action == "admit":
            payload["pending_revision"] = command.pending_revision
        command = MCPAdminCommand.parse(command.action, payload)
        if command.action == "admit":
            if command.pending_revision is None:
                raise MCPCatalogError("precondition_required")
            record = self.store.admit(connector_id, expected_revision=command.pending_revision,
                expected_epoch=command.expected_epoch, now=self.catalog.clock())
        else:
            records = self.store.get_many((connector_id,))
            if not records:
                raise MCPCatalogError("connector_not_found")
            record = self.store.revoke(connector_id, expected_digest=records[0].config_digest,
                expected_epoch=command.expected_epoch, now=self.catalog.clock())
        return _detail(record, self.catalog.attached_ids())
