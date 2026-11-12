"""Preview trusted registrations or explicitly discover one MCP connector."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from hub.application.connector_catalog import ConnectorCatalog
from hub.composition.mcp_registration import (
    read_mcp_registration,
    validate_mcp_workspaces,
)
from hub.domain.mcp_catalog import MCPCatalogError
from hub.infrastructure.mcp_catalog_repository import MCPCatalogRepository
from hub.infrastructure.platform_db import PlatformRepository, PlatformRepositoryError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registration", required=True, type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--connector")
    parser.add_argument("--discover", action="store_true")
    args = parser.parse_args(argv)
    try:
        connectors = read_mcp_registration(args.registration.expanduser())
        selected = tuple(item for item in connectors if args.connector is None or item.config.connector_id == args.connector)
        if not selected:
            raise MCPCatalogError("mcp_setup_connector_not_configured")
        if not args.discover:
            report = {"ok": True, "mode": "preview", "connector_ids": [item.config.connector_id for item in selected]}
        else:
            if not args.connector or args.database is None or not args.database.is_absolute() or not args.database.is_file():
                raise MCPCatalogError("mcp_setup_requires_existing_database_and_connector")
            platform = PlatformRepository(args.database)
            validate_mcp_workspaces(selected, platform)
            store = MCPCatalogRepository(args.database)
            store.init()
            catalog = ConnectorCatalog(enabled=True, store=store)
            catalog.register(selected[0])
            try:
                discovery = catalog.discover(args.connector)
                state, revision = "active", discovery.revision
            except MCPCatalogError as exc:
                if exc.code != "connector_revision_pending":
                    raise
                record = store.get_many((args.connector,))[0]
                state, revision = "pending", record.last_revision
            report = {"ok": True, "mode": "discover", "connector_id": args.connector, "state": state, "revision": revision}
    except MCPCatalogError as exc:
        print(json.dumps({"ok": False, "error": exc.code}))
        return 2
    except (OSError, ValueError, TypeError, PlatformRepositoryError):
        print(json.dumps({"ok": False, "error": "mcp_setup_invalid_configuration_or_store"}))
        return 2
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
