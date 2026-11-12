"""Preview or apply original Workflow workspace bindings: python -m hub.workflow_setup."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from hub.composition.workflow_registration import read_workflow_registration
from hub.infrastructure.platform_db import PlatformRepository, PlatformRepositoryError
from hub.infrastructure.workflow_setup import WorkflowSetupError, prepare_workflow_workspace
from platform_schema import validate_owner_id, validate_id


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--registration", required=True, type=Path)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not args.database.is_absolute():
            raise WorkflowSetupError("workflow_setup_requires_absolute_database")
        owner, workspace = validate_owner_id(args.owner), validate_id(args.workspace, "workspace_id")
        entries = read_workflow_registration(args.registration.expanduser())
        matches = [row for row in entries if type(row) is dict
            and row.get("owner_id") == owner and row.get("workspace_id") == workspace]
        if len(matches) != 1:
            raise WorkflowSetupError("workflow_setup_requires_exact_registration")
        row = matches[0]
        conversations = tuple(row[phase]["conversation_id"] for phase in ("develop", "review", "test"))
        report = prepare_workflow_workspace(PlatformRepository(args.database), owner_id=owner,
            workspace_id=workspace, root_path=args.root, conversation_ids=conversations, apply=args.apply)
    except WorkflowSetupError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    except (OSError, ValueError, TypeError, KeyError, PlatformRepositoryError):
        print(json.dumps({"ok": False, "error": "workflow_setup_invalid_configuration_or_store"}))
        return 2
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
