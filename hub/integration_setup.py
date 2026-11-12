"""Preview or register one immutable Fleet MCP identity in an existing Hub store."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from hub.composition.integration_registration import read_integration_registration
from hub.domain.integration_identity import IntegrationIdentityError
from hub.infrastructure.integration_identity_repository import (
    IntegrationIdentityRepository,
)
from hub.infrastructure.platform_db import PlatformRepository, PlatformRepositoryError
from hub.infrastructure.workflow_repository import (
    WorkflowRepository,
    WorkflowRepositoryError,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registration", required=True, type=Path)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--integration", required=True)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        identities = read_integration_registration(args.registration.expanduser())
        matches = [item for item in identities if (item.owner_id, item.integration_id) == (args.owner, args.integration)]
        if len(matches) != 1:
            raise IntegrationIdentityError("integration_setup_requires_exact_registration")
        identity = matches[0]
        report = {"ok": True, "mode": "apply" if args.apply else "preview",
                  "owner_id": identity.owner_id, "integration_id": identity.integration_id}
        if args.apply:
            if args.database is None or not args.database.is_absolute() or not args.database.is_file():
                raise IntegrationIdentityError("integration_setup_requires_existing_database")
            platform = PlatformRepository(args.database)
            if any(not (platform.get_workspace(identity.owner_id, workspace) or {}).get("enabled")
                   for workspace in identity.workspace_ids):
                raise IntegrationIdentityError("integration_setup_workspace_unavailable")
            store = IntegrationIdentityRepository(WorkflowRepository(args.database))
            store.init()
            store.register(identity, now=time.time())
            record = store.get_record(identity.owner_id, identity.integration_id)
            report["state"] = "restore_disabled" if record.restore_disabled else "revoked" if record.identity.revoked else "active"
    except IntegrationIdentityError as exc:
        print(json.dumps({"ok": False, "error": exc.code}))
        return 2
    except (OSError, ValueError, TypeError, PlatformRepositoryError, WorkflowRepositoryError):
        print(json.dumps({"ok": False, "error": "integration_setup_invalid_configuration_or_store"}))
        return 2
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
