"""Trusted immutable templates; no discovery from model or request content."""
from __future__ import annotations

from dataclasses import dataclass

from hub.domain.workflow_contract import parse_definition, rebind_contract
from hub.domain.workflow_contract_types import CodeRevisionReference, ResolvedContract, WorkflowDefinition
from hub.domain.workflow_errors import WorkflowValidationError
from hub.domain.workflow_identity import canonical_json, validate_reference
from hub.domain.workflow_json import decode_snapshot
from hub.domain.workflow_operator import WorkflowCreateRequest


@dataclass(frozen=True, slots=True)
class RegisteredWorkflowTemplate:
    owner_id: str
    workspace_id: str
    definition: WorkflowDefinition

    def __post_init__(self) -> None:
        validate_reference(self.owner_id)
        validate_reference(self.workspace_id)
        if type(self.definition) is not WorkflowDefinition or parse_definition(self.definition.snapshot) != self.definition:
            raise WorkflowValidationError("invalid_definition_snapshot")
        policies = {step.contract.policy_ref for step in self.definition.steps}
        if len(policies) != 1:
            raise WorkflowValidationError("inconsistent_template_policy")


class WorkflowTemplateCatalog:
    def __init__(self, templates: tuple[RegisteredWorkflowTemplate, ...] = ()) -> None:
        if type(templates) is not tuple or len(templates) > 100 or any(type(item) is not RegisteredWorkflowTemplate for item in templates):
            raise WorkflowValidationError("invalid_template_catalog")
        self.templates = {(item.owner_id, item.workspace_id, item.definition.template_id, item.definition.revision): item for item in templates}
        if len(self.templates) != len(templates):
            raise WorkflowValidationError("duplicate_template_registration")

    def available(self, owner: str) -> tuple[RegisteredWorkflowTemplate, ...]:
        return tuple(item for key, item in self.templates.items() if key[0] == owner)

    def instantiate(self, request: WorkflowCreateRequest) -> WorkflowDefinition:
        item = self.templates.get((request.owner_id, request.workspace_id, request.template_id, request.template_revision))
        if item is None:
            raise WorkflowValidationError("template_not_found")
        definition = item.definition
        if definition.steps[0].contract.policy_ref.revision != request.expected_policy_revision:
            raise WorkflowValidationError("policy_revision_conflict")
        root = rebind_contract(definition.steps[0].contract, request.input_refs)
        value = decode_snapshot(definition.snapshot)
        value["steps"][0]["contract"] = decode_snapshot(root.snapshot)
        return parse_definition(canonical_json(value))


def coding_delivery_definition() -> WorkflowDefinition:
    """Return the immutable first product template used by F3.

    The template deliberately has no user supplied command, validator, or
    capability. Those revisions are fixed catalog references; execution
    adapters decide whether the current deployment has them registered.
    """
    contract = {
        "schema_version": 1,
        "objective": "Produce a reviewed and tested code delivery",
        "input_refs": [],
        "output_specs": [{"output_key": "source", "kind": "code_revision", "required": True, "result_schema": {"type": "object"}},
                         {"output_key": "source_bundle", "kind": "artifact", "required": True, "max_bytes": 10485760}],
        "capability_requirements": [{"id": "coding.develop", "revisions": [1]}],
        "policy_ref": {"id": "coding-delivery", "revision": 1},
        "validator_refs": [],
        "context_refs": [],
        "retry_class": "manual_only",
        "budget": {"max_tokens": 200000, "max_model_requests": 50, "max_active_seconds": 3600, "max_wait_seconds": 7200},
    }
    review = {**contract, "objective": "Review the fixed source revision", "input_refs": [{"ref_key": "source", "kind": "prior_step_output", "step_key": "develop", "output_key": "source"}],
              "output_specs": [{"output_key": "review_report", "kind": "artifact", "required": True, "max_bytes": 65536}],
              "capability_requirements": [{"id": "coding.review", "revisions": [1]}],
              "validator_refs": [{"id": "review-report", "revision": 1}]}
    test = {**contract, "objective": "Run the fixed coding delivery test recipe", "input_refs": [{"ref_key": "source", "kind": "prior_step_output", "step_key": "develop", "output_key": "source"}],
            "output_specs": [{"output_key": "test_report", "kind": "artifact", "required": True, "max_bytes": 65536},
                             {"output_key": "junit", "kind": "artifact", "required": True, "max_bytes": 65536}],
            "capability_requirements": [{"id": "coding.test", "revisions": [1]}],
            "validator_refs": [{"id": "test-report", "revision": 1}]}
    return parse_definition(canonical_json({
        "schema_version": 1, "template_id": "coding-delivery", "revision": 1, "acceptance_required": True,
        "steps": [
            {"step_key": "develop", "kind": "coding_task", "requires": [], "contract": contract},
            {"step_key": "review", "kind": "coding_task", "requires": ["develop"], "contract": review},
            {"step_key": "test", "kind": "validation", "requires": ["review"], "contract": test},
        ],
    }))


def coding_delivery_template(owner_id: str, workspace_id: str) -> RegisteredWorkflowTemplate:
    return RegisteredWorkflowTemplate(owner_id, workspace_id, coding_delivery_definition())


def validate_coding_validation_contract(resolved: ResolvedContract, step_key: str) -> None:
    code = "invalid_" + step_key + "_contract"
    if step_key not in ("review", "test") or type(resolved) is not ResolvedContract or resolved.step_key != step_key:
        raise WorkflowValidationError(code)
    references = resolved.contract.input_refs
    if len(references) != 1 or references[0].ref_key != "source" or type(references[0].reference) is not CodeRevisionReference:
        raise WorkflowValidationError(code)
    expected = decode_snapshot(coding_delivery_definition().steps[1 if step_key == "review" else 2].contract.snapshot)
    actual = decode_snapshot(resolved.contract.snapshot)
    actual["input_refs"] = expected["input_refs"]
    if actual != expected:
        raise WorkflowValidationError(code)


def validate_coding_test_contract(resolved: ResolvedContract) -> None:
    validate_coding_validation_contract(resolved, "test")


def catalog_with_coding_delivery(owner_id: str, workspace_id: str) -> WorkflowTemplateCatalog:
    return WorkflowTemplateCatalog((coding_delivery_template(owner_id, workspace_id),))


__all__ = ["RegisteredWorkflowTemplate", "WorkflowTemplateCatalog", "catalog_with_coding_delivery", "coding_delivery_definition", "coding_delivery_template", "validate_coding_test_contract", "validate_coding_validation_contract"]
