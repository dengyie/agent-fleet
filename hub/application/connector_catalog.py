"""Administrator-pinned MCP catalog with an optional durable authority port."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
from threading import RLock
import time
from typing import Any, Callable, Protocol

from hub.domain.mcp_catalog import MAX_CONNECTORS, MCPCatalogRecord
from hub.domain.mcp_tool_pins import MCPToolPin
from hub.domain.workflow_identity import canonical_digest
from hub.platform.connectors.mcp import MCPCallResult, MCPConnector, MCPConnectorError, MCPDiscovery, MCPTool
from hub.platform.connectors.snapshots import decode_config, decode_discovery, encode_config, encode_discovery

MAX_EXPOSED_TOOLS = 20


class MCPCatalogStore(Protocol):
    def register(self, config_snapshot: bytes, *, now: float) -> MCPCatalogRecord: ...
    def get_many(self, connector_ids: tuple[str, ...]) -> tuple[MCPCatalogRecord, ...]: ...
    def list_page(self, after: str | None = None, limit: int = 8) -> tuple[tuple[MCPCatalogRecord, ...], str | None]: ...
    def publish(self, connector_id: str, *, expected_epoch: int, discovery_snapshot: bytes, now: float) -> MCPCatalogRecord: ...
    def admit(self, connector_id: str, *, expected_revision: int, now: float, expected_epoch: int | None = None) -> MCPCatalogRecord: ...
    def revoke(self, connector_id: str, *, expected_digest: str, now: float, expected_epoch: int | None = None) -> MCPCatalogRecord: ...


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    public_name: str
    connector_id: str
    tool_name: str
    owner_id: str
    workspace_id: str
    revision: int
    risk: str
    retry_class: str
    definition: dict[str, Any]
    pin: MCPToolPin


def _tool_pin(public_name: str, connector: MCPConnector, discovery: MCPDiscovery, tool: MCPTool) -> MCPToolPin:
    registration = asdict(connector.config)
    registration["selected_tools"] = sorted(connector.config.selected_tools)
    registration["timeout_s"] = float(connector.config.timeout_s)
    digest = canonical_digest({
        "registration": registration,
        "protocol_version": discovery.protocol_version,
        "server_name": discovery.server_name,
        "server_version": discovery.server_version,
        "tool": tool.name,
        "input_schema_digest": tool.schema_digest,
        "output_schema_digest": tool.output_schema_digest,
        "description_digest": hashlib.sha256(tool.description.encode("utf-8")).hexdigest(),
    })
    return MCPToolPin(public_name, discovery.revision, digest)


def _entries(connector: MCPConnector, discovery: MCPDiscovery) -> dict[str, CatalogEntry]:
    config = connector.config
    result: dict[str, CatalogEntry] = {}
    for tool in discovery.tools:
        name = f"mcp.{config.connector_id}.{tool.name}"
        result[name] = CatalogEntry(name, config.connector_id, tool.name, config.owner_id, config.workspace_id,
            discovery.revision, config.risk, config.retry_class,
            {"name": name, "risk": config.risk, "scope_kind": "workspace", "description": tool.description,
             "parameters": deepcopy(tool.input_schema)}, _tool_pin(name, connector, discovery, tool))
    return result


def _validate_pins(pins: tuple[MCPToolPin, ...]) -> None:
    if (type(pins) is not tuple or len(pins) > MAX_EXPOSED_TOOLS
            or any(type(pin) is not MCPToolPin for pin in pins)
            or len({pin.public_name for pin in pins}) != len(pins)):
        raise MCPConnectorError("invalid_mcp_tool_pins")


class ConnectorCatalog:
    def __init__(self, *, enabled: bool = False, store: MCPCatalogStore | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.enabled = bool(enabled)
        self.store, self.clock = store, clock
        self._connectors: dict[str, MCPConnector] = {}
        self._entries: dict[str, CatalogEntry] = {}
        self._revisions: dict[str, int] = {}
        self._admitted: dict[str, MCPDiscovery] = {}
        self._pending: dict[str, MCPDiscovery] = {}
        self._by_name: dict[str, str] = {}
        self._scope_ids: dict[tuple[str, str], set[str]] = {}
        self._lock = RLock()

    def _enabled(self) -> None:
        if not self.enabled:
            raise MCPConnectorError("mcp_connectors_disabled")

    def attached_ids(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._connectors)

    def register(self, connector: MCPConnector) -> None:
        self._enabled()
        if type(connector) is not MCPConnector:
            raise MCPConnectorError("invalid_connector")
        raw = encode_config(connector.config)
        config = decode_config(raw)
        if self.store is not None:
            self.store.register(raw, now=self.clock())
        with self._lock:
            key = config.connector_id
            if self.store is None and key in self._connectors:
                raise MCPConnectorError("connector_already_registered")
            if key not in self._connectors and len(self._connectors) >= MAX_CONNECTORS:
                raise MCPConnectorError("too_many_connectors")
            names = tuple(f"mcp.{key}.{name}" for name in config.selected_tools)
            if any(name in self._by_name and self._by_name[name] != key for name in names):
                raise MCPConnectorError("connector_tool_name_conflict")
            self._connectors[key] = connector
            self._by_name.update((name, key) for name in names)
            self._scope_ids.setdefault((config.owner_id, config.workspace_id), set()).add(key)

    def discover(self, connector_id: str) -> MCPDiscovery:
        return self.refresh(connector_id)

    def _admit(self, connector_id: str, discovery: MCPDiscovery) -> None:
        connector = self._connectors[connector_id]
        self._admitted[connector_id] = deepcopy(discovery)
        connector.discovery, connector.revision = deepcopy(discovery), discovery.revision
        for name in tuple(self._entries):
            if self._entries[name].connector_id == connector_id:
                del self._entries[name]
        self._entries.update(_entries(connector, discovery))
        self._revisions[connector_id] = discovery.revision

    def _durable(self, connector_ids: tuple[str, ...]) -> tuple[dict[str, CatalogEntry], dict[str, MCPConnector]]:
        """Read one authority snapshot, then construct independent I/O adapters."""
        with self._lock:
            attached = {key: self._connectors[key] for key in connector_ids if key in self._connectors}
        records = self.store.get_many(tuple(attached))
        if len(records) != len(attached):
            raise MCPConnectorError("connector_not_found")
        entries: dict[str, CatalogEntry] = {}
        pinned: dict[str, MCPConnector] = {}
        for record in records:
            adapter = attached[record.connector_id]
            if record.config_snapshot != encode_config(adapter.config):
                raise MCPConnectorError("connector_registration_conflict")
            if record.disabled or record.active_snapshot is None:
                continue
            connector = MCPConnector(decode_config(record.config_snapshot), transport=adapter.transport, secret_broker=adapter.secret_broker)
            connector.discovery = decode_discovery(record.active_snapshot, connector.config)
            connector.revision = connector.discovery.revision
            pinned[record.connector_id] = connector
            entries.update(_entries(connector, connector.discovery))
        self._enabled()
        return entries, pinned

    def _selected_entries(self, owner_id: str, workspace_id: str,
                          pins: tuple[MCPToolPin, ...] | None = None) -> dict[str, CatalogEntry]:
        if pins is not None:
            _validate_pins(pins)
        with self._lock:
            if self.store is None:
                return dict(self._entries)
            if pins is not None and any(pin.public_name not in self._by_name for pin in pins):
                raise MCPConnectorError("mcp_disabled")
            ids = (self._scope_ids.get((owner_id, workspace_id), set()) if pins is None else
                   {self._by_name[pin.public_name] for pin in pins})
        return self._durable(tuple(sorted(ids)))[0]

    def admit_revision(self, connector_id: str, expected_revision: int) -> MCPDiscovery:
        self._enabled()
        if self.store is not None:
            record = self.store.admit(connector_id, expected_revision=expected_revision, now=self.clock())
            return decode_discovery(record.active_snapshot, decode_config(record.config_snapshot))
        with self._lock:
            self._enabled()
            discovery = self._pending.get(connector_id)
            if type(expected_revision) is not int or discovery is None or discovery.revision != expected_revision:
                raise MCPConnectorError("connector_revision_changed")
            self._admit(connector_id, discovery)
            del self._pending[connector_id]
            return deepcopy(discovery)

    def revoke(self, connector_id: str, *, expected_digest: str) -> MCPCatalogRecord:
        self._enabled()
        if self.store is None:
            raise MCPConnectorError("mcp_catalog_persistence_required")
        return self.store.revoke(connector_id, expected_digest=expected_digest, now=self.clock())

    def revision_pins(self, *, workspace_id: str) -> dict[str, int]:
        if not self.enabled:
            return {}
        with self._lock:
            ids = tuple(key for key, connector in self._connectors.items() if connector.config.workspace_id == workspace_id)
            entries = dict(self._entries)
        if self.store is not None:
            entries = self._durable(ids)[0]
        return {name: entry.revision for name, entry in entries.items() if entry.workspace_id == workspace_id}

    def run_snapshot(self, *, owner_id: str, workspace_id: str) -> dict[str, dict[str, str | int]]:
        if not self.enabled:
            return {}
        entries = self._selected_entries(owner_id, workspace_id)
        result: dict[str, dict[str, str | int]] = {}
        for name, entry in entries.items():
            if (entry.owner_id, entry.workspace_id) != (owner_id, workspace_id):
                continue
            result[name] = entry.pin.snapshot()
            if len(result) == MAX_EXPOSED_TOOLS:
                break
        return result

    def tool_definitions(self, *, owner_id: str | None = None,
                         workspace_id: str | None = None, limit: int = MAX_EXPOSED_TOOLS,
                         expected_pins: tuple[MCPToolPin, ...] | None = None) -> list[dict[str, Any]]:
        if not self.enabled:
            if expected_pins:
                raise MCPConnectorError("mcp_connectors_disabled")
            return []
        if type(limit) is not int or not 0 <= limit <= MAX_EXPOSED_TOOLS:
            raise MCPConnectorError("invalid_tool_limit")
        if owner_id is None or workspace_id is None:
            return []
        entries = self._selected_entries(owner_id, workspace_id, expected_pins)
        if expected_pins is not None:
            selected = self._pinned_entries(expected_pins, owner_id, workspace_id, entries)
            return [deepcopy(entry.definition) for entry in selected[:limit]]
        selected = (entry for entry in entries.values() if (entry.owner_id, entry.workspace_id) == (owner_id, workspace_id))
        result: list[dict[str, Any]] = []
        for entry in selected:
            if len(result) == limit:
                break
            result.append(deepcopy(entry.definition))
        return result

    @staticmethod
    def _pinned_entries(pins: tuple[MCPToolPin, ...], owner_id: str, workspace_id: str,
                        entries: Mapping[str, CatalogEntry]) -> list[CatalogEntry]:
        _validate_pins(pins)
        result: list[CatalogEntry] = []
        for pin in pins:
            entry = entries.get(pin.public_name)
            if entry is None or (entry.owner_id, entry.workspace_id, entry.pin) != (owner_id, workspace_id, pin):
                raise MCPConnectorError("mcp_tool_pin_changed")
            result.append(entry)
        return result

    def call(self, public_name: str, arguments: Mapping[str, Any], *, owner_id: str,
             workspace_id: str, expected_revision: int | None = None,
             expected_pin: MCPToolPin | None = None) -> MCPCallResult:
        self._enabled()
        with self._lock:
            key = self._by_name.get(public_name)
            entries = dict(self._entries) if self.store is None else {}
            pinned = None
            if key is not None and self.store is None:
                adapter = self._connectors[key]
                pinned = MCPConnector(adapter.config, transport=adapter.transport, secret_broker=adapter.secret_broker)
                pinned.discovery = deepcopy(self._admitted.get(key))
                pinned.revision = pinned.discovery.revision if pinned.discovery else 0
        if key is not None and self.store is not None:
            entries, connectors = self._durable((key,))
            pinned = connectors.get(key)
        entry = entries.get(public_name)
        if entry is None or (entry.owner_id, entry.workspace_id) != (owner_id, workspace_id):
            raise MCPConnectorError("tool_not_allowed")
        if expected_pin is not None and (type(expected_pin) is not MCPToolPin or expected_pin != entry.pin):
            raise MCPConnectorError("mcp_tool_pin_changed")
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision != entry.revision):
            raise MCPConnectorError("connector_revision_changed")
        if pinned is None or pinned.discovery is None or pinned.revision != entry.revision:
            raise MCPConnectorError("connector_revision_changed")
        self._enabled()
        return pinned.call(entry.tool_name, arguments)

    def refresh(self, connector_id: str) -> MCPDiscovery:
        self._enabled()
        with self._lock:
            connector = self._connectors.get(connector_id)
            if connector is None:
                raise MCPConnectorError("connector_not_found")
            prior, pending = self._admitted.get(connector_id), self._pending.get(connector_id)
        record = None
        if self.store is not None:
            records = self.store.get_many((connector_id,))
            if not records:
                raise MCPConnectorError("connector_not_found")
            record = records[0]
            if record.disabled:
                raise MCPConnectorError("connector_revoked")
            if record.config_snapshot != encode_config(connector.config):
                raise MCPConnectorError("connector_registration_conflict")
            prior = decode_discovery(record.active_snapshot, connector.config) if record.active_snapshot else None
            pending = decode_discovery(record.pending_snapshot, connector.config) if record.pending_snapshot else None
        probe = MCPConnector(connector.config, transport=connector.transport, secret_broker=connector.secret_broker)
        probe.discovery = deepcopy(pending or prior)
        probe.revision = probe.discovery.revision if probe.discovery is not None else 0
        discovery = probe.discover()
        self._enabled()
        if record is not None:
            saved = self.store.publish(connector_id, expected_epoch=record.epoch,
                discovery_snapshot=encode_discovery(discovery), now=self.clock())
            if saved.pending_snapshot is not None:
                raise MCPConnectorError("connector_revision_pending")
            return decode_discovery(saved.active_snapshot, decode_config(saved.config_snapshot))
        with self._lock:
            self._enabled()
            if self._admitted.get(connector_id) is not prior or self._pending.get(connector_id) is not pending:
                raise MCPConnectorError("connector_revision_changed")
            if prior is not None and discovery.revision != self._revisions.get(connector_id):
                self._pending[connector_id] = deepcopy(discovery)
                raise MCPConnectorError("connector_revision_pending")
            if prior is None:
                self._admit(connector_id, discovery)
            return deepcopy(discovery)


__all__ = ["CatalogEntry", "ConnectorCatalog", "MCPCatalogStore"]
