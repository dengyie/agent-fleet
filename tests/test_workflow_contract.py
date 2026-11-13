"""Expanded workflow snapshots are strict, immutable and scoped before dispatch."""
from __future__ import annotations

import copy
import json
from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from hub.domain.workflow_contract import parse_definition, resolve_contract
from hub.domain.workflow_contract_types import (
    ArtifactReference, AuthorizedInputs, CatalogRef, InputBinding, PriorOutputReference,
)
from hub.domain.workflow_errors import WorkflowValidationError

SHA = "a" * 64
OWNER = "owner@example.test"


def artifact() -> dict[str, Any]:
    return {"artifact_id": "source-a", "sha256": SHA, "size": 12}


def contract() -> dict[str, Any]:
    return {
        "schema_version": 1, "objective": "实现并验证中文目标",
        "input_refs": [{"ref_key": "source", "kind": "artifact", **artifact()}],
        "output_specs": [{"output_key": "source", "kind": "artifact", "required": True, "max_bytes": 100}],
        "capability_requirements": [{"id": "coding.develop", "revisions": [2, 1]}],
        "policy_ref": {"id": "workspace-safe", "revision": 1},
        "validator_refs": [{"id": "source-check", "revision": 1}],
        "budget": {"max_tokens": 0, "max_model_requests": 0, "max_active_seconds": 30, "max_wait_seconds": 60},
        "retry_class": "reconcile_before_retry", "context_refs": [],
    }


def definition() -> dict[str, Any]:
    return {
        "schema_version": 1, "template_id": "coding-delivery", "revision": 1,
        "steps": [{"step_key": "develop", "kind": "coding_task", "requires": [], "contract": contract()}],
        "acceptance_required": True,
    }


def encode(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def inputs(*, owner: str = OWNER, workspace: str = "ws-a", step: str = "develop") -> AuthorizedInputs:
    return AuthorizedInputs(
        OWNER, "ws-a", step, CatalogRef("cli-codex", 1),
        (InputBinding("source", owner, workspace, ArtifactReference("source-a", SHA, 12)),),
    )


def test_valid_expanded_snapshot_is_immutable_and_round_trips() -> None:
    parsed = parse_definition(encode(definition()))
    assert parsed.template_id == "coding-delivery"
    assert parsed.steps[0].contract.budget.max_generations == 3
    assert parsed.steps[0].contract.capability_requirements[0].revisions == (1, 2)
    assert parse_definition(parsed.snapshot) == parsed
    assert type(parsed.steps) is tuple
    with pytest.raises(FrozenInstanceError):
        parsed.revision = 2


@pytest.mark.parametrize("where", ["root", "step", "contract", "budget", "input", "output", "policy", "capability", "validator"])
def test_every_contract_object_rejects_unknown_fields(where: str) -> None:
    data = definition()
    work = data["steps"][0]["contract"]
    target = {
        "root": data, "step": data["steps"][0], "contract": work, "budget": work["budget"],
        "input": work["input_refs"][0], "output": work["output_specs"][0],
        "policy": work["policy_ref"], "capability": work["capability_requirements"][0],
        "validator": work["validator_refs"][0],
    }[where]
    target["shell"] = "must-not-run"
    with pytest.raises(WorkflowValidationError):
        parse_definition(encode(data))


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, "1", None, 2**53])
def test_template_revision_is_an_exact_safe_positive_integer(value: object) -> None:
    data = definition()
    data["revision"] = value
    with pytest.raises(WorkflowValidationError):
        parse_definition(encode(data))


@pytest.mark.parametrize("field", ["max_tokens", "max_model_requests", "max_active_seconds", "max_wait_seconds", "max_generations"])
@pytest.mark.parametrize("value", [True, -1, "1", 1.25, None, 2**53])
def test_budget_numbers_are_never_coerced(field: str, value: object) -> None:
    data = definition()
    data["steps"][0]["contract"]["budget"][field] = value
    with pytest.raises(WorkflowValidationError):
        parse_definition(encode(data))


@pytest.mark.parametrize("field", ["max_active_seconds", "max_wait_seconds", "max_generations"])
def test_deadlines_and_generation_budget_cannot_be_zero(field: str) -> None:
    data = definition()
    data["steps"][0]["contract"]["budget"][field] = 0
    with pytest.raises(WorkflowValidationError):
        parse_definition(encode(data))


@pytest.mark.parametrize("value", [0, "true", [], None])
def test_acceptance_required_is_boolean(value: object) -> None:
    data = definition()
    data["acceptance_required"] = value
    with pytest.raises(WorkflowValidationError):
        parse_definition(encode(data))


@pytest.mark.parametrize("raw", [
    b'{"schema_version":1,"schema_version":1}', b'{"x":{"a":1,"a":2}}',
    b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1e999}', b'{"x":"\\ud800"}',
    b'\xff', b'\xef\xbb\xbf{}', b'{', b'[]', b'null',
])
def test_malformed_json_and_duplicate_keys_are_rejected(raw: bytes) -> None:
    with pytest.raises(WorkflowValidationError):
        parse_definition(raw)


def test_raw_size_and_nesting_are_checked_before_decode() -> None:
    for raw in (b" " * 65537, b"[" * 1000 + b"0" + b"]" * 1000):
        with pytest.raises(WorkflowValidationError):
            parse_definition(raw)
    with pytest.raises(WorkflowValidationError):
        parse_definition("{}")


def test_unicode_and_json_errors_retain_their_root_cause() -> None:
    with pytest.raises(WorkflowValidationError) as error:
        parse_definition(b"\xff")
    assert isinstance(error.value.__cause__, UnicodeDecodeError)
    with pytest.raises(WorkflowValidationError) as error:
        parse_definition(b"{")
    assert isinstance(error.value.__cause__, json.JSONDecodeError)


@pytest.mark.parametrize("field", ["input_refs", "output_specs", "capability_requirements", "validator_refs", "context_refs"])
def test_per_contract_collection_limits(field: str) -> None:
    data = definition()
    work = data["steps"][0]["contract"]
    item = work[field][0] if work[field] else {"ref_key": "context", "kind": "memory", "ref_id": "memory-a", "revision": 1}
    work[field] = [copy.deepcopy(item) for _ in range(17)]
    with pytest.raises(WorkflowValidationError):
        parse_definition(encode(data))


def test_objective_is_bounded_by_utf8_bytes() -> None:
    data = definition()
    work = data["steps"][0]["contract"]
    work["objective"] = "中" * 1365
    assert parse_definition(encode(data)).steps[0].contract.objective == work["objective"]
    for text in ("中" * 1366, " \n", "\x00", "\ud800"):
        work["objective"] = text
        with pytest.raises(WorkflowValidationError):
            parse_definition(encode(data))


def test_contract_byte_limit_is_independent_of_whole_definition_limit() -> None:
    data = definition()
    data["steps"][0]["contract"]["output_specs"] = [{
        "output_key": "report", "kind": "structured", "required": True,
        "result_schema": {"type": "object", "description": "x" * 16384},
    }]
    assert len(encode(data)) < 65536
    with pytest.raises(WorkflowValidationError):
        parse_definition(encode(data))


def test_only_linear_chains_and_existing_required_prior_outputs_are_allowed() -> None:
    data = definition()
    second = copy.deepcopy(data["steps"][0])
    second.update(step_key="review", requires=["develop"])
    second["contract"]["input_refs"] = [{
        "ref_key": "source", "kind": "prior_step_output", "step_key": "develop", "output_key": "source",
    }]
    data["steps"].append(second)
    assert len(parse_definition(encode(data)).steps) == 2
    for requires in ([], ["review"], ["absent"], ["develop", "develop"]):
        bad = copy.deepcopy(data)
        bad["steps"][1]["requires"] = requires
        with pytest.raises(WorkflowValidationError):
            parse_definition(encode(bad))
    second["contract"]["input_refs"][0]["output_key"] = "absent"
    with pytest.raises(WorkflowValidationError):
        parse_definition(encode(data))


def test_zero_nine_and_duplicate_steps_are_rejected() -> None:
    for count in (0, 9):
        data = definition()
        data["steps"] *= count
        with pytest.raises(WorkflowValidationError):
            parse_definition(encode(data))
    data = definition()
    data["steps"].append(copy.deepcopy(data["steps"][0]))
    with pytest.raises(WorkflowValidationError):
        parse_definition(encode(data))


@pytest.mark.parametrize("identifier", ["/tmp/repo", "https://user:pw@example.test/repo", "a..b", "../repo", "x" * 129, "中"])
def test_registry_ids_cannot_encode_paths_or_urls(identifier: str) -> None:
    data = definition()
    data["template_id"] = identifier
    with pytest.raises(WorkflowValidationError):
        parse_definition(encode(data))


def test_code_revision_is_fixed_and_hashes_are_normalized() -> None:
    data = definition()
    source = {
        "ref_key": "source", "kind": "code_revision", "repository_id": "repo-a",
        "base_commit": "A" * 40, "head_commit": "B" * 40, "tree_id": "C" * 40,
        "source_bundle": {**artifact(), "sha256": "D" * 64},
    }
    data["steps"][0]["contract"]["input_refs"] = [source]
    parsed = parse_definition(encode(data))
    ref = parsed.steps[0].contract.input_refs[0].reference
    assert ref.head_commit == "b" * 40
    assert ref.source_bundle.sha256 == "d" * 64
    for key, value in (("head_commit", "main"), ("tree_id", "c" * 64), ("repository_id", "/tmp/repo")):
        bad = copy.deepcopy(data)
        bad["steps"][0]["contract"]["input_refs"][0][key] = value
        with pytest.raises(WorkflowValidationError):
            parse_definition(encode(bad))


def test_context_is_revisioned_and_cannot_alias_an_input() -> None:
    data = definition()
    work = data["steps"][0]["contract"]
    work["context_refs"] = [{"ref_key": "memory", "kind": "memory", "ref_id": "memory-a", "revision": 1}]
    assert parse_definition(encode(data)).steps[0].contract.context_refs[0].reference.revision == 1
    work["context_refs"][0]["ref_key"] = "source"
    with pytest.raises(WorkflowValidationError):
        parse_definition(encode(data))


@pytest.mark.parametrize("key", ["$ref", "$dynamicRef", "$recursiveRef"])
def test_output_schema_never_fetches_external_references(key: str) -> None:
    data = definition()
    data["steps"][0]["contract"]["output_specs"] = [{
        "output_key": "report", "kind": "structured", "required": True,
        "result_schema": {"type": "object", "properties": {"nested": {key: "https://external.test/schema"}}},
    }]
    with pytest.raises(WorkflowValidationError):
        parse_definition(encode(data))


def test_resolution_freezes_scope_and_adapter_without_mutable_aliases() -> None:
    parsed = parse_definition(encode(definition()))
    first = resolve_contract(parsed, inputs())
    second = resolve_contract(parsed, inputs())
    assert first == second
    assert first.owner_id == OWNER
    assert type(first.snapshot) is bytes
    payload = json.loads(first.snapshot)
    payload["contract"]["objective"] = "changed"
    assert json.loads(first.snapshot)["contract"]["objective"] == "实现并验证中文目标"
    other = AuthorizedInputs(OWNER, "ws-a", "develop", CatalogRef("cli-codex", 2), inputs().bindings)
    assert resolve_contract(parsed, other).digest != first.digest


@pytest.mark.parametrize("owner,workspace", [("other@example.test", "ws-a"), (OWNER, "ws-b")])
def test_resolution_rejects_cross_scope_artifacts(owner: str, workspace: str) -> None:
    parsed = parse_definition(encode(definition()))
    with pytest.raises(WorkflowValidationError):
        resolve_contract(parsed, inputs(owner=owner, workspace=workspace))


def test_resolution_rejects_missing_extra_duplicate_and_changed_inputs() -> None:
    parsed = parse_definition(encode(definition()))
    binding = inputs().bindings[0]
    variants = (
        (), (binding, binding),
        (InputBinding("other", OWNER, "ws-a", binding.reference),),
        (InputBinding("source", OWNER, "ws-a", ArtifactReference("source-a", "b" * 64, 12)),),
    )
    for bindings in variants:
        with pytest.raises(WorkflowValidationError):
            resolve_contract(parsed, AuthorizedInputs(OWNER, "ws-a", "develop", CatalogRef("cli-codex", 1), bindings))
    with pytest.raises(WorkflowValidationError):
        resolve_contract(parsed, inputs(step="absent"))


def test_prior_output_resolution_requires_the_exact_producer_identity() -> None:
    data = definition()
    review = copy.deepcopy(data["steps"][0])
    review.update(step_key="review", requires=["develop"])
    review["contract"]["input_refs"] = [{
        "ref_key": "source", "kind": "prior_step_output", "step_key": "develop", "output_key": "source",
    }]
    data["steps"].append(review)
    parsed = parse_definition(encode(data))
    ref = ArtifactReference("source-a", SHA, 12)
    for source in (None, PriorOutputReference("other", "source")):
        bad = AuthorizedInputs(OWNER, "ws-a", "review", CatalogRef("cli-codex", 1), (
            InputBinding("source", OWNER, "ws-a", ref, source),
        ))
        with pytest.raises(WorkflowValidationError):
            resolve_contract(parsed, bad)
    good = AuthorizedInputs(OWNER, "ws-a", "review", CatalogRef("cli-codex", 1), (
        InputBinding("source", OWNER, "ws-a", ref, PriorOutputReference("develop", "source")),
    ))
    resolved = resolve_contract(parsed, good)
    assert resolved.contract.input_refs[0].reference == ref
    assert b'prior_step_output' not in resolved.contract.snapshot


def test_prior_output_resolution_preserves_declared_kind_and_artifact_limit() -> None:
    data = definition()
    review = copy.deepcopy(data["steps"][0])
    review.update(step_key="review", requires=["develop"])
    review["contract"]["input_refs"] = [{
        "ref_key": "source", "kind": "prior_step_output", "step_key": "develop", "output_key": "source",
    }]
    data["steps"].append(review)
    scope = AuthorizedInputs(OWNER, "ws-a", "review", CatalogRef("cli-codex", 1), (
        InputBinding("source", OWNER, "ws-a", ArtifactReference("source-a", SHA, 101),
                     PriorOutputReference("develop", "source")),
    ))
    with pytest.raises(WorkflowValidationError, match="prior_output_constraint"):
        resolve_contract(parse_definition(encode(data)), scope)
    data["steps"][0]["contract"]["output_specs"] = [{
        "output_key": "source", "kind": "code_revision", "required": True,
        "result_schema": {"type": "object"},
    }]
    with pytest.raises(WorkflowValidationError, match="prior_output_constraint"):
        resolve_contract(parse_definition(encode(data)), scope)


def test_eight_steps_can_consume_an_earlier_required_output() -> None:
    data = definition()
    for index in range(1, 8):
        step = copy.deepcopy(data["steps"][0])
        step.update(step_key=f"step-{index}", requires=[data["steps"][-1]["step_key"]])
        step["contract"]["input_refs"] = [{
            "ref_key": "source", "kind": "prior_step_output", "step_key": "develop", "output_key": "source",
        }]
        data["steps"].append(step)
    assert len(parse_definition(encode(data)).steps) == 8
    data["steps"][0]["contract"]["output_specs"][0]["required"] = False
    with pytest.raises(WorkflowValidationError, match="invalid_prior_output"):
        parse_definition(encode(data))


@pytest.mark.parametrize("revision", [1, 9007199254740991])
def test_validator_catalog_reference_fits_compact_evidence_identity(revision: int) -> None:
    data = definition()
    ref = data["steps"][0]["contract"]["validator_refs"][0]
    ref.update(id="v" * (127 - len(str(revision))), revision=revision)
    assert len(parse_definition(encode(data)).steps[0].contract.validator_refs) == 1
    ref["id"] += "v"
    with pytest.raises(WorkflowValidationError, match="invalid_identifier"):
        parse_definition(encode(data))
