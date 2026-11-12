"""Parse versioned contracts and resolve already-authorized references."""
from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any, Callable, TypeVar

from .contract_types import (
    ArtifactReference, AuthorizedInputs, CapabilityRequirement, CatalogRef, CodeRevisionReference,
    ContextReference, InputBinding, NamedReference, OutputSpec, PriorOutputReference,
    ResolvedContract, WorkContract, WorkflowBudget, WorkflowDefinition, WorkflowStepDefinition,
)
from .errors import WorkflowValidationError
from .identity import canonical_digest, canonical_json, validate_reference
from .json import decode_snapshot

MAX_SAFE_INTEGER = 9007199254740991
MAX_CONTRACT_BYTES = 16384
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_OPAQUE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_HEX = re.compile(r"[0-9a-fA-F]+")
_KINDS = frozenset({"native_run", "coding_task", "fixed_tool", "external_agent", "validation"})
_RETRIES = frozenset({"read_only", "reconcile_before_retry", "manual_only"})
_CONTRACT_FIELDS = frozenset({
    "schema_version", "objective", "input_refs", "output_specs", "capability_requirements",
    "policy_ref", "validator_refs", "budget", "retry_class", "context_refs",
})
_T = TypeVar("_T")


def _object(value: object, fields: set[str] | frozenset[str], *, optional: frozenset[str] = frozenset()) -> dict[str, Any]:
    if type(value) is not dict or set(value) - fields - optional or fields - set(value):
        raise WorkflowValidationError("invalid_object_fields")
    return value


def _integer(value: object, *, minimum: int = 1) -> int:
    if type(value) is not int or not minimum <= value <= MAX_SAFE_INTEGER:
        raise WorkflowValidationError("invalid_integer")
    return value


def _bool(value: object) -> bool:
    if type(value) is not bool:
        raise WorkflowValidationError("invalid_boolean")
    return value


def _id(value: object) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None or ".." in value:
        raise WorkflowValidationError("invalid_registry_id")
    return value


def _hash(value: object, lengths: frozenset[int] = frozenset({64})) -> str:
    if type(value) is not str or len(value) not in lengths or _HEX.fullmatch(value) is None:
        raise WorkflowValidationError("invalid_hash")
    return value.lower()


def _items(value: object, *, maximum: int = 16) -> list[Any]:
    if type(value) is not list or len(value) > maximum:
        raise WorkflowValidationError("invalid_collection")
    return value


def _unique(values: tuple[_T, ...], key: Callable[[_T], object]) -> tuple[_T, ...]:
    if len({key(value) for value in values}) != len(values):
        raise WorkflowValidationError("duplicate_reference")
    return values


def _catalog(value: object) -> CatalogRef:
    raw = _object(value, {"id", "revision"})
    return CatalogRef(_id(raw["id"]), _integer(raw["revision"]))


def _validator(value: object) -> CatalogRef:
    reference = _catalog(value)
    validate_reference(f"{reference.id}@{reference.revision}")
    return reference


def _artifact(value: object) -> ArtifactReference:
    raw = _object(value, {"artifact_id", "sha256", "size"})
    artifact_id = raw["artifact_id"]
    if type(artifact_id) is not str or _OPAQUE.fullmatch(artifact_id) is None:
        raise WorkflowValidationError("invalid_artifact_id")
    return ArtifactReference(artifact_id, _hash(raw["sha256"]), _integer(raw["size"], minimum=0))


def _reference(value: object, *, context: bool = False, schema_version: int = 1) -> NamedReference:
    if type(value) is not dict:
        raise WorkflowValidationError("invalid_reference")
    ref_key = _id(value.get("ref_key"))
    kind = value.get("kind")
    if context:
        return NamedReference(ref_key, _context(value, schema_version=schema_version))
    if kind == "artifact":
        raw = _object(value, {"ref_key", "kind", "artifact_id", "sha256", "size"})
        return NamedReference(ref_key, _artifact({key: raw[key] for key in ("artifact_id", "sha256", "size")}))
    if kind == "prior_step_output":
        raw = _object(value, {"ref_key", "kind", "step_key", "output_key"})
        return NamedReference(ref_key, PriorOutputReference(_id(raw["step_key"]), _id(raw["output_key"])))
    if kind != "code_revision":
        raise WorkflowValidationError("invalid_reference_kind")
    raw = _object(value, {"ref_key", "kind", "repository_id", "base_commit", "head_commit", "tree_id", "source_bundle"})
    commits = tuple(_hash(raw[key], frozenset({40, 64})) for key in ("base_commit", "head_commit", "tree_id"))
    if len({len(commit) for commit in commits}) != 1:
        raise WorkflowValidationError("inconsistent_git_hashes")
    return NamedReference(ref_key, CodeRevisionReference(_id(raw["repository_id"]), *commits, _artifact(raw["source_bundle"])))


def _context(value: dict[str, Any], *, schema_version: int = 1) -> ContextReference:
    kind = value.get("kind")
    if kind not in ("memory", "summary"):
        raise WorkflowValidationError("invalid_context_kind")
    fields = {"ref_key", "kind", "ref_id", "revision"}
    raw = _object(value, fields | ({"sha256"} if kind == "summary" else set()))
    digest = _hash(raw["sha256"]) if kind == "summary" else None
    minimum = 0 if schema_version == 2 and kind == "memory" else 1
    return ContextReference(kind, _id(raw["ref_id"]), _integer(raw["revision"], minimum=minimum), digest)


def _schema(value: object) -> bytes:
    if type(value) is not dict:
        raise WorkflowValidationError("invalid_result_schema")
    stack: list[object] = [value]
    while stack:
        item = stack.pop()
        if type(item) is list:
            stack.extend(item)
            continue
        if type(item) is not dict:
            continue
        for key, child in item.items():
            if key in ("$ref", "$dynamicRef", "$recursiveRef") and (type(child) is not str or not child.startswith("#")):
                raise WorkflowValidationError("external_schema_reference")
            stack.append(child)
    return canonical_json(value)


def _output(value: object) -> OutputSpec:
    if type(value) is not dict:
        raise WorkflowValidationError("invalid_output")
    kind = value.get("kind")
    if kind not in ("artifact", "code_revision", "structured"):
        raise WorkflowValidationError("invalid_output_kind")
    constraint = "max_bytes" if kind == "artifact" else "result_schema"
    raw = _object(value, {"output_key", "kind", "required", constraint})
    size = _integer(raw["max_bytes"], minimum=0) if kind == "artifact" else None
    schema = _schema(raw["result_schema"]) if kind != "artifact" else None
    return OutputSpec(_id(raw["output_key"]), kind, _bool(raw["required"]), size, schema)


def _capability(value: object) -> CapabilityRequirement:
    raw = _object(value, {"id", "revisions"})
    revisions = tuple(_integer(item) for item in _items(raw["revisions"]))
    if not revisions or len(set(revisions)) != len(revisions):
        raise WorkflowValidationError("invalid_capability_revisions")
    return CapabilityRequirement(_id(raw["id"]), tuple(sorted(revisions)))


def _budget(value: object) -> WorkflowBudget:
    raw = _object(value, {"max_tokens", "max_model_requests", "max_active_seconds", "max_wait_seconds"}, optional=frozenset({"max_generations"}))
    return WorkflowBudget(
        _integer(raw["max_tokens"], minimum=0), _integer(raw["max_model_requests"], minimum=0),
        _integer(raw["max_active_seconds"]), _integer(raw["max_wait_seconds"]),
        _integer(raw.get("max_generations", 3)),
    )


def _contract(value: object) -> WorkContract:
    raw = _object(value, _CONTRACT_FIELDS)
    if type(raw["schema_version"]) is not int or raw["schema_version"] not in (1, 2):
        raise WorkflowValidationError("unsupported_contract_schema")
    objective = raw["objective"]
    if type(objective) is not str or not objective.strip() or "\x00" in objective or len(objective.encode("utf-8")) > 4096:
        raise WorkflowValidationError("invalid_objective")
    retry = raw["retry_class"]
    if type(retry) is not str or retry not in _RETRIES:
        raise WorkflowValidationError("invalid_retry_class")
    inputs = tuple(_reference(item) for item in _items(raw["input_refs"]))
    contexts = tuple(_reference(item, context=True, schema_version=raw["schema_version"]) for item in _items(raw["context_refs"]))
    _unique(inputs + contexts, lambda item: item.ref_key)
    outputs = _unique(tuple(_output(item) for item in _items(raw["output_specs"])), lambda item: item.output_key)
    capabilities = _unique(tuple(_capability(item) for item in _items(raw["capability_requirements"])), lambda item: item.id)
    validators = _unique(tuple(_validator(item) for item in _items(raw["validator_refs"])), lambda item: item.id)
    contract = WorkContract(objective, inputs, outputs, capabilities, _catalog(raw["policy_ref"]), validators, _budget(raw["budget"]), retry, contexts, b"", raw["schema_version"])
    snapshot = canonical_json(_contract_dict(contract))
    if len(snapshot) > MAX_CONTRACT_BYTES:
        raise WorkflowValidationError("contract_too_large")
    return replace(contract, snapshot=snapshot)


def _reference_dict(named: NamedReference) -> dict[str, Any]:
    ref = named.reference
    common = {"ref_key": named.ref_key}
    if type(ref) is ArtifactReference:
        return {**common, "kind": "artifact", **_artifact_dict(ref)}
    if type(ref) is CodeRevisionReference:
        return {
            **common, "kind": "code_revision", "repository_id": ref.repository_id,
            "base_commit": ref.base_commit, "head_commit": ref.head_commit, "tree_id": ref.tree_id,
            "source_bundle": _artifact_dict(ref.source_bundle),
        }
    if type(ref) is PriorOutputReference:
        return {**common, "kind": "prior_step_output", "step_key": ref.step_key, "output_key": ref.output_key}
    if type(ref) is not ContextReference:
        raise WorkflowValidationError("invalid_reference")
    data = {**common, "kind": ref.kind, "ref_id": ref.ref_id, "revision": ref.revision}
    if ref.kind == "summary":
        data["sha256"] = ref.sha256
    return data


def _artifact_dict(ref: ArtifactReference) -> dict[str, Any]:
    if type(ref) is not ArtifactReference:
        raise WorkflowValidationError("invalid_artifact_reference")
    return {"artifact_id": ref.artifact_id, "sha256": ref.sha256, "size": ref.size}


def _catalog_dict(ref: CatalogRef) -> dict[str, Any]:
    if type(ref) is not CatalogRef:
        raise WorkflowValidationError("invalid_catalog_reference")
    return {"id": ref.id, "revision": ref.revision}


def _output_dict(spec: OutputSpec) -> dict[str, Any]:
    raw = {"output_key": spec.output_key, "kind": spec.kind, "required": spec.required}
    if spec.kind == "artifact":
        return {**raw, "max_bytes": spec.max_bytes}
    return {**raw, "result_schema": json.loads(spec.result_schema)}


def _contract_dict(contract: WorkContract) -> dict[str, Any]:
    budget = contract.budget
    return {
        "schema_version": contract.schema_version, "objective": contract.objective,
        "input_refs": [_reference_dict(item) for item in contract.input_refs],
        "output_specs": [_output_dict(item) for item in contract.output_specs],
        "capability_requirements": [{"id": item.id, "revisions": list(item.revisions)} for item in contract.capability_requirements],
        "policy_ref": _catalog_dict(contract.policy_ref),
        "validator_refs": [_catalog_dict(item) for item in contract.validator_refs],
        "budget": {
            "max_tokens": budget.max_tokens, "max_model_requests": budget.max_model_requests,
            "max_active_seconds": budget.max_active_seconds, "max_wait_seconds": budget.max_wait_seconds,
            "max_generations": budget.max_generations,
        },
        "retry_class": contract.retry_class,
        "context_refs": [_reference_dict(item) for item in contract.context_refs],
    }


def parse_contract(raw_json: bytes) -> WorkContract:
    return _contract(decode_snapshot(raw_json))


def parse_input_references(values: object) -> tuple[NamedReference, ...]:
    references = tuple(_reference(item) for item in _items(values))
    return _unique(references, lambda item: item.ref_key)


def reference_values(references: tuple[NamedReference, ...]) -> list[dict[str, Any]]:
    if type(references) is not tuple or len(references) > 16 or any(type(item) is not NamedReference for item in references):
        raise WorkflowValidationError("invalid_input_references")
    values = [_reference_dict(item) for item in references]
    normalized = tuple(_reference(item) for item in values)
    if references != _unique(normalized, lambda item: item.ref_key):
        raise WorkflowValidationError("invalid_input_references")
    if len(canonical_json(values)) > MAX_CONTRACT_BYTES:
        raise WorkflowValidationError("contract_too_large")
    return values


def rebind_contract(template: WorkContract, references: tuple[NamedReference, ...]) -> WorkContract:
    """Change only existing input slots and the same code repository identity."""
    values = reference_values(references)
    original = {item.ref_key: item.reference for item in template.input_refs}
    if {item.ref_key for item in references} != set(original):
        raise WorkflowValidationError("input_binding_mismatch")
    by_key = {item.ref_key: item.reference for item in references}
    for key, previous in original.items():
        current = by_key[key]
        if type(current) is not type(previous):
            raise WorkflowValidationError("input_reference_conflict")
        if type(previous) is PriorOutputReference and current != previous:
            raise WorkflowValidationError("prior_output_conflict")
        if type(previous) is CodeRevisionReference and current.repository_id != previous.repository_id:
            raise WorkflowValidationError("input_reference_conflict")
    raw = _contract_dict(template)
    raw["input_refs"] = values
    return _contract(raw)


def parse_definition(raw_json: bytes) -> WorkflowDefinition:
    """Accept an expanded registration snapshot, never a live template lookup."""
    raw = _object(decode_snapshot(raw_json), {"schema_version", "template_id", "revision", "steps", "acceptance_required"})
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise WorkflowValidationError("unsupported_workflow_schema")
    template_id, revision = _id(raw["template_id"]), _integer(raw["revision"])
    acceptance_required = _bool(raw["acceptance_required"])
    steps: list[WorkflowStepDefinition] = []
    previous: dict[str, dict[str, OutputSpec]] = {}
    for value in _items(raw["steps"], maximum=8):
        step = _object(value, {"step_key", "kind", "requires", "contract"})
        key = _id(step["step_key"])
        kind = step["kind"]
        if key in previous or type(kind) is not str or kind not in _KINDS:
            raise WorkflowValidationError("invalid_step")
        requires = tuple(_id(item) for item in _items(step["requires"], maximum=1))
        expected = (steps[-1].step_key,) if steps else ()
        if requires != expected:
            raise WorkflowValidationError("nonlinear_workflow")
        contract = _contract(step["contract"])
        _check_prior_outputs(contract, previous)
        previous[key] = {item.output_key: item for item in contract.output_specs}
        steps.append(WorkflowStepDefinition(key, kind, requires, contract))
    if not steps:
        raise WorkflowValidationError("empty_workflow")
    snapshot = canonical_json({
        "schema_version": 1, "template_id": template_id, "revision": revision,
        "acceptance_required": acceptance_required,
        "steps": [{"step_key": item.step_key, "kind": item.kind, "requires": list(item.requires), "contract": json.loads(item.contract.snapshot)} for item in steps],
    })
    return WorkflowDefinition(template_id, revision, tuple(steps), acceptance_required, snapshot)


def _check_prior_outputs(contract: WorkContract, previous: dict[str, dict[str, OutputSpec]]) -> None:
    for named in contract.input_refs:
        ref = named.reference
        if type(ref) is not PriorOutputReference:
            continue
        output = previous.get(ref.step_key, {}).get(ref.output_key)
        if output is None or not output.required:
            raise WorkflowValidationError("invalid_prior_output")


def _binding_map(inputs: AuthorizedInputs) -> dict[str, InputBinding]:
    if type(inputs.bindings) is not tuple or len(inputs.bindings) > 32:
        raise WorkflowValidationError("invalid_input_bindings")
    result: dict[str, InputBinding] = {}
    for binding in inputs.bindings:
        if type(binding) is not InputBinding:
            raise WorkflowValidationError("invalid_input_binding")
        key = _id(binding.ref_key)
        if key in result or binding.owner_id != inputs.owner_id or binding.workspace_id != inputs.workspace_id:
            raise WorkflowValidationError("input_scope_conflict")
        result[key] = binding
    return result


def _resolve(
    named: NamedReference, bindings: dict[str, InputBinding],
    outputs: dict[tuple[str, str], OutputSpec], *, context: bool = False, schema_version: int = 1,
) -> NamedReference:
    binding = bindings.get(named.ref_key)
    if binding is None:
        raise WorkflowValidationError("missing_input_binding")
    if type(named.reference) is PriorOutputReference:
        if type(binding.source) is not PriorOutputReference or binding.source != named.reference:
            raise WorkflowValidationError("prior_output_conflict")
        if type(binding.reference) not in (ArtifactReference, CodeRevisionReference):
            raise WorkflowValidationError("invalid_prior_output_type")
    elif binding.source is not None or binding.reference != named.reference:
        raise WorkflowValidationError("input_reference_conflict")
    concrete = NamedReference(named.ref_key, binding.reference)
    concrete = _reference(_reference_dict(concrete), context=context, schema_version=schema_version)
    if type(named.reference) is PriorOutputReference:
        ref = named.reference
        spec = outputs[(ref.step_key, ref.output_key)]
        expected_type = CodeRevisionReference if spec.kind == "code_revision" else ArtifactReference
        if type(concrete.reference) is not expected_type:
            raise WorkflowValidationError("prior_output_constraint")
        if spec.kind == "artifact" and concrete.reference.size > spec.max_bytes:
            raise WorkflowValidationError("prior_output_constraint")
    return concrete


def resolve_contract(definition: WorkflowDefinition, authorized_inputs: AuthorizedInputs, *, contract_override: WorkContract | None = None) -> ResolvedContract:
    """Freeze trusted scoped bindings; this does not grant policy or tool access."""
    if type(definition) is not WorkflowDefinition or type(authorized_inputs) is not AuthorizedInputs:
        raise WorkflowValidationError("invalid_resolution")
    scope = authorized_inputs
    validate_reference(scope.owner_id)
    _id(scope.workspace_id)
    _id(scope.step_key)
    adapter = _catalog(_catalog_dict(scope.adapter_ref))
    steps = {step.step_key: step for step in definition.steps}
    outputs = {
        (item.step_key, output.output_key): output
        for item in definition.steps for output in item.contract.output_specs
    }
    step = steps.get(scope.step_key)
    if step is None:
        raise WorkflowValidationError("unknown_step")
    bindings = _binding_map(scope)
    contract = step.contract
    if contract_override is not None:
        if type(contract_override) is not WorkContract or rebind_contract(contract, contract_override.input_refs) != contract_override:
            raise WorkflowValidationError("invalid_contract_override")
        contract = contract_override
    expected = {item.ref_key for item in contract.input_refs + contract.context_refs}
    if set(bindings) != expected:
        raise WorkflowValidationError("input_binding_mismatch")
    resolved = replace(
        contract, input_refs=tuple(_resolve(item, bindings, outputs) for item in contract.input_refs),
        context_refs=tuple(_resolve(item, bindings, outputs, context=True, schema_version=contract.schema_version) for item in contract.context_refs), snapshot=b"",
    )
    resolved = _contract(_contract_dict(resolved))
    payload = {
        "schema_version": 1, "owner_id": scope.owner_id, "workspace_id": scope.workspace_id,
        "template_id": definition.template_id, "template_revision": definition.revision,
        "step_key": step.step_key, "adapter_ref": _catalog_dict(adapter),
        "contract": json.loads(resolved.snapshot),
    }
    snapshot = canonical_json(payload)
    return ResolvedContract(scope.owner_id, scope.workspace_id, step.step_key, resolved, snapshot, canonical_digest(payload))


def parse_resolved(raw_json: bytes) -> ResolvedContract:
    """Rebuild a persisted snapshot's shape; callers still verify authorization."""
    raw = _object(decode_snapshot(raw_json), {
        "schema_version", "owner_id", "workspace_id", "template_id", "template_revision",
        "step_key", "adapter_ref", "contract",
    })
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise WorkflowValidationError("unsupported_resolved_schema")
    owner = validate_reference(raw["owner_id"])
    workspace, step = _id(raw["workspace_id"]), _id(raw["step_key"])
    _id(raw["template_id"])
    _integer(raw["template_revision"])
    adapter = _catalog(raw["adapter_ref"])
    contract = _contract(raw["contract"])
    if any(type(item.reference) is PriorOutputReference for item in contract.input_refs):
        raise WorkflowValidationError("unresolved_input")
    payload = {**raw, "adapter_ref": _catalog_dict(adapter), "contract": _contract_dict(contract)}
    snapshot = canonical_json(payload)
    return ResolvedContract(owner, workspace, step, contract, snapshot, canonical_digest(payload))
