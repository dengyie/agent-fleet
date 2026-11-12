"""Preview or apply original Workflow workspace bindings: python -m hub.workflow_setup."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from hub.composition.native_registration import parse_native_registration
from hub.composition.workflow_registration import read_workflow_manifest
from hub.infrastructure.platform_db import PlatformRepository, PlatformRepositoryError
from hub.infrastructure.workflow_setup import WorkflowSetupError, prepare_workflow_conversations, prepare_workflow_workspace
from platform_schema import validate_owner_id, validate_id


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--registration", required=True, type=Path)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--template", help="Exact Native template ID or coding-delivery; mixed manifests default to coding")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not args.database.is_absolute():
            raise WorkflowSetupError("workflow_setup_requires_absolute_database")
        owner, workspace = validate_owner_id(args.owner), validate_id(args.workspace, "workspace_id")
        manifest = read_workflow_manifest(args.registration.expanduser())
        coding = [row for row in manifest.get("coding_delivery", []) if type(row) is dict
            and row.get("owner_id") == owner and row.get("workspace_id") == workspace]
        native = [row for row in manifest.get("native", []) if type(row) is dict
            and row.get("owner_id") == owner and row.get("workspace_id") == workspace]
        if args.template is None and coding:
            kind, matches = "coding", coding
        elif args.template is not None:
            candidates = [("coding", row) for row in coding if args.template == "coding-delivery"]
            candidates.extend(("native", row) for row in native if type(row.get("definition")) is dict
                and row["definition"].get("template_id") == args.template)
            if len(candidates) != 1:
                raise WorkflowSetupError("workflow_setup_requires_exact_registration")
            kind, row = candidates[0]
            matches = [row]
        else:
            kind, matches = "native", native
        if len(matches) != 1:
            raise WorkflowSetupError("workflow_setup_requires_exact_registration")
        row = matches[0]
        if kind == "coding":
            conversations = tuple(row[phase]["conversation_id"] for phase in ("develop", "review", "test"))
            report = prepare_workflow_workspace(PlatformRepository(args.database), owner_id=owner,
                workspace_id=workspace, root_path=args.root, conversation_ids=conversations, apply=args.apply)
        else:
            definition, bindings = parse_native_registration(row)
            conversations = tuple((step.step_key, bindings[step.step_key]["conversation_id"]) for step in definition.steps)
            report = prepare_workflow_conversations(PlatformRepository(args.database), owner_id=owner,
                workspace_id=workspace, root_path=args.root, conversations=conversations, apply=args.apply)
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
