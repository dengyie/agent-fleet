"""Trusted local Muse registrations; startup never initializes or queries a browser."""
from __future__ import annotations

import hashlib
import re
import stat
from pathlib import Path

from connectors.external_agents.muse import MuseExternalAgentAdapter, open_muse_bridge
from hub.application.workflow_coordinator import RegisteredWorkflowPlan
from hub.application.workflow_templates import (
    RegisteredWorkflowTemplate,
    WorkflowTemplateCatalog,
)
from hub.domain.workflow_contract import parse_definition
from hub.domain.workflow_contract_types import CatalogRef
from hub.domain.workflow_external import RegisteredExternalTarget
from hub.domain.workflow_identity import canonical_json
from hub.domain.workflow_json import decode_snapshot

MAX_REGISTRATION_BYTES = 256 * 1024


def load_muse_registration(path: Path, platform):
    if not path.is_absolute() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("invalid_muse_registration_file")
    with path.open("rb") as stream:
        value = decode_snapshot(stream.read(MAX_REGISTRATION_BYTES + 1), max_bytes=MAX_REGISTRATION_BYTES)
    if (set(value) != {"schema_version", "muse"} or type(value["schema_version"]) is not int
            or value["schema_version"] != 1 or type(value["muse"]) is not list or not 1 <= len(value["muse"]) <= 32):
        raise ValueError("invalid_muse_registration_manifest")
    templates, routes, agents, targets = [], [], [], set()
    for row in value["muse"]:
        if type(row) is not dict or set(row) != {"owner_id", "workspace_id", "target_key", "account_ref",
                                                "journal_path", "journal_identity", "definition"}:
            raise ValueError("invalid_muse_registration_fields")
        owner, workspace = row["owner_id"], row["workspace_id"]
        definition = parse_definition(canonical_json(row["definition"]))
        template = RegisteredWorkflowTemplate(owner, workspace, definition)
        if not (platform.get_workspace(owner, workspace) or {}).get("enabled"):
            raise ValueError("muse_registration_workspace_unavailable")
        if any(step.kind != "external_agent" or step.contract.output_specs or step.contract.validator_refs
               or step.contract.capability_requirements for step in definition.steps):
            raise ValueError("unsupported_muse_template")
        if (type(row["journal_path"]) is not str or type(row["journal_identity"]) is not str
                or re.fullmatch(r"[0-9a-f]{64}", row["journal_identity"]) is None):
            raise ValueError("invalid_muse_journal")
        journal = Path(row["journal_path"]).expanduser()
        if not journal.is_absolute() or not journal.is_file():
            raise ValueError("muse_registration_requires_initialized_journal")
        bridge = open_muse_bridge(journal)
        if bridge.journal_identity() != row["journal_identity"]:
            raise ValueError("muse_registration_journal_changed")
        adapter = MuseExternalAgentAdapter(bridge, workspace_id=workspace, account_ref=row["account_ref"])
        configuration_digest = adapter.configuration_digest
        if bridge.journal_identity() != row["journal_identity"]:
            raise ValueError("muse_registration_journal_changed")
        adapter_ref, policy_ref = CatalogRef("muse", 1), definition.steps[0].contract.policy_ref
        target = RegisteredExternalTarget(owner, workspace, row["target_key"], adapter_ref, policy_ref,
            adapter.agent_id, configuration_digest, adapter.capabilities)
        key = (owner, workspace, target.target_key)
        if key in targets:
            raise ValueError("duplicate_muse_target")
        targets.add(key)
        digest = hashlib.sha256(definition.snapshot).hexdigest()
        routes.extend(RegisteredWorkflowPlan(owner, workspace, digest, step.step_key, adapter_ref, policy_ref,
            target.target_key, 1, step.contract.budget.max_tokens, step.contract.budget.max_model_requests,
            external_target=target) for step in definition.steps)
        agents.append((target, adapter))
        templates.append(template)
    return {"workflow_templates": WorkflowTemplateCatalog(tuple(templates)),
            "workflow_steps": tuple(routes), "workflow_external_agents": tuple(agents)}


def merge_muse_registration(dependencies, loaded):
    if dependencies.get("workflow_external_agents"):
        raise ValueError("muse_registration_conflicts_with_injected_adapters")
    catalog = dependencies.get("workflow_templates", WorkflowTemplateCatalog())
    if type(catalog) is not WorkflowTemplateCatalog:
        raise ValueError("invalid_workflow_template_catalog")
    return {**dependencies, **loaded,
        "workflow_templates": WorkflowTemplateCatalog(tuple(catalog.templates.values()) + tuple(loaded["workflow_templates"].templates.values())),
        "workflow_steps": tuple(dependencies.get("workflow_steps", ())) + loaded["workflow_steps"]}
