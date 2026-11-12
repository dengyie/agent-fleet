"""Trusted Native Workflow targets loaded from the normal Hub manifest."""
from __future__ import annotations

import hashlib

from hub.application.workflow_coordinator import RegisteredWorkflowPlan
from hub.application.workflow_templates import RegisteredWorkflowTemplate
from hub.domain.workflow_contract import parse_definition
from hub.domain.workflow_contract_types import CatalogRef
from hub.domain.workflow_identity import canonical_json
from hub.infrastructure.workflow_run_executor import (
    RegisteredRunTarget,
    RunStepExecutor,
    _valid_provider_snapshot,
)

_FIELDS = {"owner_id", "workspace_id", "definition", "bindings"}
_BINDING_FIELDS = {
    "step_key", "target_key", "conversation_id", "model_profile_id", "provider_snapshot", "input_mode",
}
_MODES = {"text": (True, False, False), "code": (False, True, False),
          "text_code": (True, True, False), "media": (False, False, True)}
_LOCAL_NODES = (None, "", "local", "hub", "host")


def load_native_registration(entries, config, fleet, repository, *, clock):
    platform = fleet["platform_repository"]
    defaults = fleet["services"].get("platform_defaults")
    if defaults is None:
        raise ValueError("native_registration_requires_platform_defaults")
    templates, plans, executors, target_keys = [], [], [], set()
    for row in entries:
        if type(row) is not dict or set(row) != _FIELDS:
            raise ValueError("invalid_native_registration_fields")
        owner, workspace = row["owner_id"], row["workspace_id"]
        if config.platform_workflow_dispatch_enabled and not config.platform_worker_scheduler_enabled and owner != config.dev_operator:
            raise ValueError("workflow_registration_owner_not_scheduled")
        workspace_row = platform.get_workspace(owner, workspace)
        if not workspace_row or not workspace_row.get("enabled"):
            raise ValueError("native_registration_workspace_unavailable")
        definition = parse_definition(canonical_json(row["definition"]))
        if (not definition.steps or any(step.kind != "native_run" or step.contract.output_specs
                or step.contract.validator_refs or step.contract.capability_requirements for step in definition.steps)):
            raise ValueError("unsupported_native_template")
        template = RegisteredWorkflowTemplate(owner, workspace, definition)
        bindings = row["bindings"]
        if type(bindings) is not list or len(bindings) != len(definition.steps):
            raise ValueError("invalid_native_bindings")
        by_step = {}
        for binding in bindings:
            if type(binding) is not dict or set(binding) != _BINDING_FIELDS:
                raise ValueError("invalid_native_binding_fields")
            if any(type(binding[key]) is not str or not binding[key]
                   for key in ("step_key", "target_key", "conversation_id", "model_profile_id", "input_mode")):
                raise ValueError("invalid_native_binding_fields")
            if not _valid_provider_snapshot(binding["provider_snapshot"]):
                raise ValueError("invalid_native_provider_snapshot")
            if binding["step_key"] in by_step:
                raise ValueError("duplicate_native_binding")
            if binding["input_mode"] not in _MODES:
                raise ValueError("invalid_native_input_mode")
            by_step[binding["step_key"]] = binding
        if set(by_step) != {step.step_key for step in definition.steps}:
            raise ValueError("native_bindings_do_not_match_steps")

        policy_ref = definition.steps[0].contract.policy_ref
        definition_digest = hashlib.sha256(definition.snapshot).hexdigest()
        adapter_ref = CatalogRef("native", 1)
        for step in definition.steps:
            binding = by_step[step.step_key]
            target_key = binding["target_key"]
            key = (owner, workspace, target_key)
            if key in target_keys:
                raise ValueError("duplicate_native_target")
            target_keys.add(key)
            conversation = platform.get_conversation_metadata(owner, binding["conversation_id"])
            if not conversation or conversation.get("workspace_id") != workspace or conversation.get("archived_at") is not None:
                raise ValueError("native_registration_conversation_unavailable")
            if conversation.get("overrides", {}).get("execution_node_id") not in _LOCAL_NODES:
                raise ValueError("native_registration_requires_local_conversation")
            profile = platform.get_model(owner, binding["model_profile_id"])
            if not profile or not profile.get("enabled"):
                raise ValueError("native_registration_model_unavailable")
            resolved = defaults.resolve_run_config(owner, conversation=conversation.get("overrides", {}), overrides={
                    "workspace_id": workspace, "model_profile_id": binding["model_profile_id"],
                })
            if resolved.get("execution_node_id") not in _LOCAL_NODES:
                raise ValueError("native_registration_requires_local_target")
            # The model profile is still checked as an enabled catalog entry,
            # but execution uses the immutable provider snapshot from the
            # manifest. This keeps queued runs stable across restarts and
            # operator edits to the live model record.
            resolved["provider_snapshot"] = binding["provider_snapshot"]
            text, code, media = _MODES[binding["input_mode"]]
            target = RegisteredRunTarget(owner, workspace, target_key, adapter_ref, policy_ref,
                binding["conversation_id"], canonical_json(resolved), inline_text_inputs=text,
                inline_code_inputs=code, inline_media_inputs=media)
            executors.append((owner, workspace, target_key, RunStepExecutor(repository, target, clock=clock)))
            plans.append(RegisteredWorkflowPlan(owner, workspace, definition_digest, step.step_key,
                adapter_ref, policy_ref, target_key, 1, step.contract.budget.max_tokens,
                step.contract.budget.max_model_requests))
        templates.append(template)
    return tuple(templates), tuple(plans), tuple(executors)


__all__ = ["load_native_registration"]
