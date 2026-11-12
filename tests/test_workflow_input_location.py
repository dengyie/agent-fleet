"""Hub-local consumer registration cannot replace a Node receive receipt."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.domain.artifact_receive import RegisteredNodeInputs
from fleet_core.control import generate_ed25519_keypair
from hub.domain.workflow_contract import parse_definition
from hub.domain.workflow_contract_types import CatalogRef
from hub.domain.workflow_errors import WorkflowValidationError
from hub.domain.workflow_execution import HubInputTarget
from hub.domain.workflow_identity import canonical_json
from hub.domain.workflow_review import ReviewAdapterSpec, ReviewIdentity
from hub.infrastructure.workflow_repository import WorkflowRepository, WorkflowRepositoryError
from hub.infrastructure.workflow_run_executor import LegacyTaskTarget, RegisteredRunTarget, RunStepExecutor
from test_workflow_native_text_inputs import TextSetup, make_text
from test_workflow_repository import OWNER, WORKSPACE


def registration() -> HubInputTarget:
    return HubInputTarget(OWNER, WORKSPACE, "native-a", CatalogRef("native", 1), CatalogRef("safe", 1), "native_run")


@pytest.mark.parametrize(("field", "value"), [
    ("owner_id", None), ("workspace_id", ""), ("target_key", "x" * 129), ("target_key", "\ud800"),
    ("adapter_ref", None), ("adapter_ref", {"id": "native", "revision": 1}),
    ("adapter_ref", CatalogRef("native", True)), ("adapter_ref", CatalogRef("native", 0)),
    ("adapter_ref", CatalogRef("native", 9007199254740992)), ("adapter_ref", CatalogRef("", 1)),
    ("policy_ref", CatalogRef("safe", 1.0)), ("policy_ref", CatalogRef("safe", -1)),
    ("kind", None), ("kind", "external_agent"), ("kind", "run"),
])
def test_hub_input_registration_rejects_invalid_identity(field: str, value: Any) -> None:
    with pytest.raises(WorkflowValidationError):
        replace(registration(), **{field: value})


@pytest.mark.parametrize("damage", ["list", "element", "duplicate", "limit"])
def test_invalid_hub_input_index_keeps_previous_configuration(tmp_path: Path, damage: str) -> None:
    setup = make_text(tmp_path, admit=False)
    target = registration()
    setup.workflows.configure_hub_input_targets((target,))
    invalid = {"list": [target], "element": (None,), "duplicate": (target, target),
        "limit": tuple(replace(target, target_key=f"target-{index}") for index in range(257))}[damage]
    with pytest.raises(WorkflowRepositoryError):
        setup.workflows.configure_hub_input_targets(invalid)
    assert not setup.workflows.requires_node_inputs(setup.resolved, kind="native_run", target_key="native-a")


def test_hub_input_index_has_bounded_capacity_and_no_persistent_exemption(tmp_path: Path) -> None:
    setup = make_text(tmp_path, admit=False)
    targets = tuple(replace(registration(), target_key=f"target-{index}") for index in range(256))
    setup.workflows.configure_hub_input_targets(targets)
    assert not setup.workflows.requires_node_inputs(setup.resolved, kind="native_run", target_key="target-255")
    restored = WorkflowRepository(setup.workflows.db_path)
    restored.init()
    assert restored.requires_node_inputs(setup.resolved, kind="native_run", target_key="target-255")
    setup.workflows.configure_hub_input_targets(())
    assert setup.workflows.requires_node_inputs(setup.resolved, kind="native_run", target_key="target-255")


@pytest.mark.parametrize(("field", "value"), [
    ("owner_id", "other@example.test"), ("workspace_id", "other-workspace"), ("target_key", "other-target"),
    ("adapter_ref", CatalogRef("other-adapter", 1)), ("adapter_ref", CatalogRef("native", 2)),
    ("policy_ref", CatalogRef("other-policy", 1)), ("policy_ref", CatalogRef("safe", 2)), ("kind", "validation"),
])
def test_hub_input_identity_must_match_every_scope_revision_and_kind(tmp_path: Path, field: str, value: Any) -> None:
    setup = make_text(tmp_path, admit=False)
    setup.workflows.configure_hub_input_targets((replace(registration(), **{field: value}),))
    assert setup.workflows.requires_node_inputs(setup.resolved, kind="native_run", target_key="native-a")


@pytest.mark.parametrize("mode", ["missing", "mismatch", "exact", "pinned"])
def test_direct_admission_preserves_default_and_pinned_node_receipt_guards(tmp_path: Path, mode: str) -> None:
    setup = make_text(tmp_path, admit=False)
    _, public_key = generate_ed25519_keypair()
    setup.workflows.artifact_input_public_key = public_key
    workflow_id, claim = "workflow-a", setup.claim
    if mode == "pinned":
        workflow_id = "workflow-pinned"
        definition = parse_definition(setup.workflows.get(OWNER, "workflow-a").definition_snapshot)
        setup.workflows.create_or_get(OWNER, WORKSPACE, workflow_id, client_token=workflow_id, definition=definition, now=101)
        claim = setup.workflows.claim_due(OWNER, worker_id="pinned", now=102, limit=1, lease_seconds=60)[0]
        assert claim.workflow_id == workflow_id
        setup.workflows.freeze_inputs(claim, step_key="develop", generation=1, resolved=setup.resolved,
            expected_revision=1, now=102, input_target="native-a", inputs_pending=True)
    if mode != "missing":
        target = replace(registration(), adapter_ref=CatalogRef("native", 2)) if mode == "mismatch" else registration()
        setup.workflows.configure_hub_input_targets((target,))
    before = setup.workflows.get(OWNER, workflow_id)
    assert before is not None

    def admit() -> Any:
        return setup.workflows.admit_step(claim, step_key="develop", generation=1,
            expected_revision=before.revision, target_key="native-a", capacity=1,
            reserve_tokens=100, reserve_requests=2, now=103)

    if mode == "exact":
        assert admit().target_key == "native-a"
        return
    with pytest.raises(WorkflowRepositoryError, match="input_receipt_required"):
        admit()
    assert setup.workflows.get(OWNER, workflow_id) == before
    with pytest.raises(WorkflowRepositoryError, match="dispatch_not_found"):
        setup.workflows.get_dispatch(OWNER, workflow_id, "develop", 1)


def test_only_existing_complete_hub_consumers_derive_registration(tmp_path: Path) -> None:
    setup = make_text(tmp_path, admit=False)
    assert setup.target.hub_input_target() == registration()
    plain = replace(setup.target, inline_text_inputs=False)
    assert plain.hub_input_target() is None
    legacy = replace(plain, legacy_task=LegacyTaskTarget("mac-local", "codex", "agent-fleet"))
    assert legacy.hub_input_target() is None
    remote = replace(plain, config_snapshot=canonical_json({"execution_node_id": "node-a"}))
    assert remote.hub_input_target() is None
    review = RegisteredRunTarget(OWNER, WORKSPACE, "review", CatalogRef("review", 1), CatalogRef("safe", 1),
        "review-chat", b"{}", review_adapter=ReviewAdapterSpec(CatalogRef("review", 1),
            ReviewIdentity("review-service", "service"), "a" * 64))
    test = RegisteredRunTarget(OWNER, WORKSPACE, "test", CatalogRef("fixed-tests", 1), CatalogRef("safe", 1),
        "test-chat", b"{}", test_recipe=CatalogRef("coding-delivery-tests", 1))
    assert review.hub_input_target().kind == "coding_task"
    assert test.hub_input_target().kind == "validation"


def bootstrap(setup: TextSetup, tmp_path: Path, executors: tuple[Any, ...], *, node_inputs: tuple[Any, ...] = ()) -> Any:
    private_key, _ = generate_ed25519_keypair()
    config = FleetConfig.from_root(tmp_path, dev_operator=OWNER, platform_enabled=True,
        platform_db=setup.workflows.db_path, platform_artifact_root=setup.artifacts.root,
        platform_workflows_enabled=True, platform_workflow_dispatch_enabled=True,
        platform_artifact_transfer_enabled=True, platform_command_signing_raw=private_key, serve_frontend=False)
    return create_app(config, repositories={"workflows": setup.workflows, "workflow_clock": setup.clock,
        "workflow_executors": executors, "workflow_node_inputs": node_inputs})


@pytest.mark.parametrize("local", [False, True])
def test_bootstrap_derives_input_location_from_real_executor(tmp_path: Path, local: bool) -> None:
    setup = make_text(tmp_path, admit=False)
    executor = RunStepExecutor(setup.workflows, replace(setup.target, inline_text_inputs=local), clock=setup.clock)
    app = bootstrap(setup, tmp_path, ((OWNER, WORKSPACE, "native-a", executor),))
    repository = app.extensions["fleet"]["repositories"]["workflows"]
    assert repository.requires_node_inputs(setup.resolved, kind="native_run", target_key="native-a") is not local
    assert executor.text_inputs is not None


@pytest.mark.parametrize("field", ["owner", "workspace", "target"])
def test_bootstrap_rejects_executor_registration_identity_mismatch(tmp_path: Path, field: str) -> None:
    setup = make_text(tmp_path, admit=False)
    identity = {"owner": OWNER, "workspace": WORKSPACE, "target": "native-a"}
    identity[field] = "different"
    with pytest.raises(WorkflowRepositoryError, match="invalid_hub_input_target"):
        bootstrap(setup, tmp_path, ((identity["owner"], identity["workspace"], identity["target"], setup.executor()),))


def test_bootstrap_rejects_ambiguous_hub_and_node_input_location(tmp_path: Path) -> None:
    setup = make_text(tmp_path, admit=False)
    inputs = RegisteredNodeInputs(OWNER, WORKSPACE, "native-a", (("input-0", "source", ".txt"),))
    with pytest.raises(WorkflowRepositoryError, match="input_location_conflict"):
        bootstrap(setup, tmp_path, ((OWNER, WORKSPACE, "native-a", setup.executor()),), node_inputs=(inputs,))
    assert setup.workflows.requires_node_inputs(setup.resolved, kind="native_run", target_key="native-a")
