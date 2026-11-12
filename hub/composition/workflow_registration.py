"""Operator-owned registrations for the built-in coding delivery template."""
from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import stat

from hub.application.workflow_coordinator import RegisteredWorkflowPlan
from hub.application.workflow_templates import RegisteredWorkflowTemplate, WorkflowTemplateCatalog, coding_delivery_definition
from hub.domain.artifact_receive import RegisteredNodeInputs
from hub.domain.capability import CapabilityDefinition, NodePolicy, SelectionRequirements
from hub.domain.capability_execution import RegisteredCapabilityAction
from hub.domain.capability_routing import RegisteredCapabilityRoute
from hub.domain.remote_workflow_test import REMOTE_TEST_ACTION
from hub.domain.workflow_contract_types import CatalogRef
from hub.domain.workflow_identity import canonical_json
from hub.domain.workflow_json import decode_snapshot
from hub.domain.workflow_review import ReviewIdentity
from hub.infrastructure.workflow_run_executor import LegacyTaskTarget, RegisteredRunTarget, RunStepExecutor
from hub.platform.workflow_review import CommandReviewAdapter

_MAX_BYTES = 256 * 1024


def _object(value, fields: str):
    if type(value) is not dict or value.keys() != set(fields.split()):
        raise ValueError("invalid_workflow_registration_fields")
    return value


def _argv(value):
    if type(value) is not list or not value:
        raise ValueError("workflow_registration_requires_argv")
    return tuple(value)


def read_workflow_manifest(path: Path):
    try:
        if not path.is_absolute() or not stat.S_ISREG(path.stat().st_mode):
            raise ValueError("invalid_workflow_registration_file")
        with path.open("rb") as stream:
            value = decode_snapshot(stream.read(_MAX_BYTES + 1), max_bytes=_MAX_BYTES)
    except OSError as exc:
        raise ValueError("workflow_registration_file_unavailable") from exc
    if (type(value) is not dict or set(value) - {"schema_version", "coding_delivery", "native"}
            or "schema_version" not in value or not ("coding_delivery" in value or "native" in value)):
        raise ValueError("invalid_workflow_registration_manifest")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("invalid_workflow_registration_manifest")
    for key in ("coding_delivery", "native"):
        entries = value.get(key, [])
        if type(entries) is not list or len(entries) > 32:
            raise ValueError("invalid_workflow_registration_manifest")
    if not 1 <= len(value.get("coding_delivery", ())) + len(value.get("native", ())) <= 32:
        raise ValueError("invalid_workflow_registration_manifest")
    return value


def read_workflow_registration(path: Path):
    """Return coding entries for the existing workspace setup CLI contract."""
    return read_workflow_manifest(path).get("coding_delivery", [])


def load_workflow_registration(config, fleet, repository, *, clock):
    manifest = read_workflow_manifest(config.platform_workflow_registration_file)
    entries = manifest.get("coding_delivery", [])
    native_entries = manifest.get("native", [])
    if config.platform_workflow_dispatch_enabled and not (
            config.platform_worker_enabled and (config.platform_worker_scheduler_enabled or config.dev_operator)):
        raise ValueError("workflow_registration_requires_worker_scheduler")
    if entries and not config.tasks_enabled:
        raise ValueError("workflow_registration_requires_tasks")
    platform = fleet["platform_repository"]
    definition = coding_delivery_definition()
    policy_ref = CatalogRef(definition.template_id, definition.revision)
    plan_digest = hashlib.sha256(definition.snapshot).hexdigest()
    templates, routes, executors, inputs = [], [], [], []
    adapters, definitions, nodes, actions = {}, {}, {}, {}
    for value in entries:
        row = _object(value, "owner_id workspace_id develop review test")
        owner, workspace = row["owner_id"], row["workspace_id"]
        template = RegisteredWorkflowTemplate(owner, workspace, definition)
        if not (platform.get_workspace(owner, workspace) or {}).get("enabled"):
            raise ValueError("workflow_registration_workspace_unavailable")
        if config.platform_workflow_dispatch_enabled and not config.platform_worker_scheduler_enabled and owner != config.dev_operator:
            raise ValueError("workflow_registration_owner_not_scheduled")
        develop = _object(row["develop"], "machine agent_type project repository_id conversation_id")
        review = _object(row["review"], "adapter_id revision executor_id kind model_id argv launcher timeout_seconds conversation_id")
        test = row["test"]
        if type(test) is not dict or set(test) not in ({"conversation_id"}, {"conversation_id", "remote"}):
            raise ValueError("invalid_workflow_registration_fields")
        conversations = [item["conversation_id"] for item in (develop, review, test)]
        if len(set(conversations)) != 3:
            raise ValueError("workflow_registration_requires_distinct_conversations")
        for conversation in conversations:
            saved = platform.get_conversation_metadata(owner, conversation)
            if not saved or saved["workspace_id"] != workspace or saved["archived_at"] is not None:
                raise ValueError("workflow_registration_conversation_unavailable")
        legacy = LegacyTaskTarget(develop["machine"], develop["agent_type"], develop["project"])
        if config.platform_workflow_dispatch_enabled and not fleet["services"]["tasks"].hosts.project_allowed(legacy.machine, legacy.project):
            raise ValueError("workflow_registration_project_unavailable")
        adapter = CommandReviewAdapter(CatalogRef(review["adapter_id"], review["revision"]),
            ReviewIdentity(review["executor_id"], review["kind"], review["model_id"]),
            _argv(review["argv"]), launcher=_argv(review["launcher"]), timeout_seconds=review["timeout_seconds"])
        previous = adapters.setdefault(adapter.spec.adapter_ref, adapter)
        if previous.spec != adapter.spec:
            raise ValueError("workflow_registration_review_conflict")
        targets = [
            RegisteredRunTarget(owner, workspace, "coding-develop", CatalogRef("legacy-coding-runner", 1), policy_ref,
                conversations[0], b"{}", legacy_task=legacy, source_repository_id=develop["repository_id"]),
            RegisteredRunTarget(owner, workspace, "coding-review", adapter.spec.adapter_ref, policy_ref,
                conversations[1], b"{}", review_adapter=adapter.spec),
        ]
        test_adapter, test_recipe = CatalogRef("fixed-tests", 1), CatalogRef("coding-delivery-tests", 1)
        if "remote" not in test:
            if config.platform_workflow_dispatch_enabled and not config.platform_sandbox_launcher:
                raise ValueError("workflow_registration_requires_test_launcher")
            targets.append(RegisteredRunTarget(owner, workspace, "coding-test", test_adapter, policy_ref,
                conversations[2], b"{}", test_recipe=test_recipe))
        else:
            if not (config.platform_remote_execution_enabled and config.platform_artifact_transfer_enabled
                    and config.platform_capability_routing_enabled and fleet["services"]["platform_delivery"].signing_key):
                raise ValueError("workflow_registration_remote_gate_dependencies")
            remote = _object(test["remote"], "node_id capability architecture software residency_alias")
            node_id = remote["node_id"]
            if node_id in {"coding-develop", "coding-review", "coding-test"} or not (platform.get_node(owner, node_id) or {}).get("enabled"):
                raise ValueError("workflow_registration_node_unavailable")
            capability = CapabilityDefinition.from_snapshot(canonical_json(remote["capability"]))
            cap = CatalogRef(capability.capability_id, capability.revision)
            if (cap != CatalogRef("coding.test", 1) or capability.adapter_id != test_adapter.id
                    or capability.retry_class != "manual_only" or capability.risk != "workspace_read"
                    or "operator" not in capability.allowed_owner_roles
                    or capability.max_active_seconds < definition.steps[2].contract.budget.max_active_seconds
                    or capability.result_limit_bytes != 65536):
                raise ValueError("workflow_registration_invalid_fixed_test_capability")
            if definitions.setdefault(cap, capability) != capability:
                raise ValueError("workflow_registration_capability_conflict")
            residency = remote["residency_alias"]
            key = (owner, node_id)
            policy = NodePolicy(owner, node_id, (workspace,), (policy_ref,), (cap,), slot_limit=1,
                input_aliases=("source",), residency_aliases=(residency,), allowed_risks=("workspace_read",))
            previous_policy = nodes.get(key)
            if previous_policy is not None:
                if replace(previous_policy, workspaces=(workspace,)) != policy:
                    raise ValueError("workflow_registration_remote_node_policy_conflict")
                policy = replace(previous_policy, workspaces=tuple(sorted(set((*previous_policy.workspaces, workspace)))))
            nodes[key] = policy
            inputs.append(RegisteredNodeInputs(owner, workspace, node_id, (("source", "source", ".tar"),)))
            actions[cap] = RegisteredCapabilityAction(test_adapter.id, cap, REMOTE_TEST_ACTION)
            software = remote["software"]
            if type(software) is not dict or not software:
                raise ValueError("workflow_registration_requires_node_software")
            selection = SelectionRequirements(owner, workspace, policy_ref, "operator", test_adapter.id,
                definition.steps[2].contract.capability_requirements, "0" * 64, operating_systems=("linux",),
                architectures=(remote["architecture"],), software=tuple(sorted(software.items())),
                input_aliases=("source",), residency_aliases=(residency,))
            routes.append(RegisteredCapabilityRoute(plan_digest, "test", test_adapter, policy_ref, selection, 0, 0))
            targets.append(RegisteredRunTarget(owner, workspace, node_id, test_adapter, policy_ref, conversations[2],
                canonical_json({"execution_node_id": node_id}), test_recipe=test_recipe, remote_test=True))
        for step, target in zip(definition.steps, targets):
            executors.append((owner, workspace, target.target_key, RunStepExecutor(repository, target, clock=clock)))
            if not target.remote_test:
                routes.append(RegisteredWorkflowPlan(owner, workspace, plan_digest, step.step_key, target.adapter_ref,
                    policy_ref, target.target_key, 1, 0, 0 if step.step_key == "test" else 1))
        templates.append(template)
    if native_entries:
        from hub.composition.native_registration import load_native_registration

        native_templates, native_routes, native_executors = load_native_registration(
            native_entries, config, fleet, repository, clock=clock)
        templates.extend(native_templates)
        routes.extend(native_routes)
        executors.extend(native_executors)
    return {"workflow_templates": WorkflowTemplateCatalog(tuple(templates)), "workflow_steps": tuple(routes),
        "workflow_executors": tuple(executors), "workflow_review_adapters": tuple(adapters.values()),
        "workflow_node_inputs": tuple(inputs), "workflow_capability_definitions": tuple(definitions.values()),
        "workflow_capability_nodes": tuple(nodes.values()), "workflow_capability_actions": tuple(actions.values())}
