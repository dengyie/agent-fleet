"""Automatic routes use the original input, admission, Run and Command facts."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import hashlib
import json
import multiprocessing
from pathlib import Path
from typing import Any

import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.domain.artifact_receive import RegisteredNodeInputs
from hub.domain.capability import NodePolicy
from hub.domain.capability_execution import RegisteredCapabilityAction
from hub.domain.capability_routing import RegisteredCapabilityRoute
from fleet_core.control import generate_ed25519_keypair
from hub.domain.workflow_contract import parse_definition
from hub.domain.workflow_contract_types import CatalogRef
from hub.infrastructure.capability_repository import CapabilityRepository
from hub.infrastructure.platform_db import PlatformRepository
from hub.infrastructure.workflow_run_executor import RegisteredRunTarget, RunStepExecutor
from hub.infrastructure.capability_workflow import CapabilityWorkflowAdmission
from hub.infrastructure.workflow_repository import WorkflowRepository
from hub.platform.artifacts import ArtifactStore
from test_capability_router import BOOT, OWNER, WORKSPACE, definition, offer_payload, requirements
from test_workflow_repository import prepare_database, registered


CAP = CatalogRef("workspace.files", 1)


@dataclass
class Setup:
    app: Any
    workflows: Any
    capabilities: CapabilityRepository
    platform: PlatformRepository
    route: RegisteredCapabilityRoute
    clock: list[float]
    credentials: dict[str, str]
    artifact: dict[str, Any] | None

    def tick(self) -> Any:
        coordinator = self.app.extensions["fleet"]["services"]["workflow_coordinator"]
        result = asyncio.run(coordinator.tick(self.clock[0]))
        self.clock[0] += 1
        assert not result.failures, [(error.code, error.__cause__) for error in result.failures]
        return result

    def count(self, table: str) -> int:
        with self.workflows.transaction(write=False) as connection:
            return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def setup_route(tmp_path: Path, *, inputs: bool = False, capability_enabled: bool = True,
                transfer_enabled: bool = True, executors: bool = True, signing_enabled: bool = True,
                prior_output: bool = False) -> Setup:
    workflows = prepare_database(tmp_path / "platform.db")
    platform = PlatformRepository(workflows.db_path)
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = None
    if inputs:
        source = tmp_path / "source.tar"
        source.write_bytes(b"fixed input bytes\n")
        artifact = store.put_file(OWNER, WORKSPACE, source)
    raw = json.loads(registered(source={key: artifact[key] for key in ("artifact_id", "sha256", "size")} if artifact else None).snapshot)
    raw["steps"][0]["kind"] = "native_run"
    raw["steps"][0]["contract"]["capability_requirements"] = [{"id": CAP.id, "revisions": [1]}]
    if prior_output:
        producer = raw["steps"][0]
        consumer = json.loads(json.dumps(producer))
        consumer.update(step_key="review", requires=["develop"])
        consumer["contract"]["input_refs"] = [{"ref_key": "source", "kind": "prior_step_output", "step_key": "develop", "output_key": "report"}]
        producer["contract"]["input_refs"] = []
        raw["steps"].append(consumer)
    workflow = parse_definition(json.dumps(raw).encode())
    workflows.create_or_get(OWNER, WORKSPACE, "workflow-a", client_token="workflow-a", definition=workflow, now=100)
    if prior_output:
        with workflows.transaction() as connection:
            connection.execute("UPDATE workflow_steps SET state='succeeded' WHERE step_key='develop'")
            connection.execute("INSERT INTO workflow_artifact_refs VALUES(?,?,?,?,?,?,?,?,?)",
                (OWNER, WORKSPACE, "workflow-a", "develop", 1, "output:report", artifact["artifact_id"], artifact["sha256"], artifact["size"]))
    platform.upsert_model(OWNER, {"profile_id": "fixture", "provider": "deterministic", "model": "fixture"})
    capabilities = CapabilityRepository(workflows, boot_id=BOOT)
    capabilities.init()
    capabilities.register_definition(definition(capability_id=CAP.id, retry_class="read_only"), now=100)
    targets, node_inputs, credentials = [], [], {}
    for node in ("node-a", "node-b"):
        platform.upsert_node(OWNER, {"node_id": node})
        secret = node[-1] * 32
        credentials[node] = platform.provision_node_credential(OWNER, node, secret=secret)["credential"]
        fingerprint = platform.authenticate_node_credential(node, secret)["credential_fingerprint"]
        capabilities.register_node(NodePolicy(OWNER, node, (WORKSPACE,), (CatalogRef("fixture", 1),), (CAP,),
            slot_limit=1, input_aliases=("source",), residency_aliases=("private",), allowed_risks=("workspace_read",)), now=100)
        capabilities.record_offer(OWNER, node, json.dumps(offer_payload(slots=1,
            capabilities=[{"id": CAP.id, "revision": 1}], local_allowed_capabilities=[{"id": CAP.id, "revision": 1}])).encode(),
            credential_fingerprint=fingerprint, now=100)
        platform.create_conversation(OWNER, "chat-" + node, title=node, workspace_id=WORKSPACE)
        target = RegisteredRunTarget(OWNER, WORKSPACE, node, CatalogRef("cli", 1), CatalogRef("safe", 1), "chat-" + node,
            json.dumps({"execution_node_id": node, "model_profile_id": "fixture", "provider_snapshot": {
                "provider": "deterministic", "model": "fixture", "provider_config": {}, "capabilities": {}}}).encode())
        targets.append((OWNER, WORKSPACE, node, RunStepExecutor(workflows, target, clock=lambda: clock[0])))
        node_inputs.append(RegisteredNodeInputs(OWNER, WORKSPACE, node, (("source", "source", ".tar"),)))
    route = RegisteredCapabilityRoute(hashlib.sha256(workflow.snapshot).hexdigest(), workflow.steps[-1].step_key, CatalogRef("cli", 1),
        CatalogRef("safe", 1), requirements(capabilities=workflow.steps[0].contract.capability_requirements), 100, 2)
    clock = [100.0]
    private, _ = generate_ed25519_keypair()
    app = create_app(FleetConfig.from_root(tmp_path, platform_enabled=True, platform_workflows_enabled=True,
        platform_workflow_dispatch_enabled=True, platform_capability_routing_enabled=capability_enabled,
        platform_artifact_transfer_enabled=transfer_enabled, platform_command_signing_raw=private if signing_enabled else None,
        platform_db=workflows.db_path, platform_artifact_root=store.root, dev_operator=OWNER, serve_frontend=False),
        repositories={"workflows": workflows, "capabilities": capabilities, "workflow_clock": lambda: clock[0],
            "workflow_steps": (route,), "workflow_executors": tuple(targets) if executors else (),
            "workflow_node_inputs": tuple(node_inputs),
            "workflow_capability_actions": (RegisteredCapabilityAction("cli", CAP, "tool.workspace.read"),)})
    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    delivery.clock = delivery.repository.clock = lambda: clock[0]
    return Setup(app, workflows, capabilities, platform, route, clock, credentials, artifact)


def test_coordinator_selects_and_creates_original_bound_run_without_manual_admission(tmp_path: Path) -> None:
    setup = setup_route(tmp_path)
    setup.tick()
    assert setup.workflows.current_steps(OWNER, "workflow-a")[0].state == "ready"
    setup.tick()
    step = setup.workflows.current_steps(OWNER, "workflow-a")[0]
    assert step.target_key == "node-a" and step.state == "dispatching"
    for table in ("workflow_dispatches", "workflow_budget_reservations", "capability_reservations", "capability_step_bindings"):
        assert setup.count(table) == 1
    setup.tick()
    with setup.workflows.transaction(write=False) as connection:
        row = connection.execute("SELECT config_snapshot FROM runs").fetchone()
    config = json.loads(row[0])
    assert config["execution_node_id"] == "node-a"
    assert config["workflow_capability"]["binding_digest"]
    setup.tick()
    assert setup.count("runs") == 1


@pytest.mark.parametrize("setting,reason", [("capability_enabled", "capability_unavailable"),
    ("executors", "executor_unavailable"), ("transfer_enabled", "input_alias_unregistered")])
def test_missing_routing_dependencies_defer_without_static_fallback(tmp_path: Path, setting: str, reason: str) -> None:
    setup = setup_route(tmp_path, inputs=True, **{setting: False})
    setup.tick()
    assert setup.workflows.get(OWNER, "workflow-a").reason_code == reason
    assert setup.count("runs") == setup.count("platform_commands") == setup.count("workflow_dispatches") == 0


def test_input_selection_and_receive_command_share_one_fixed_target_transaction(tmp_path: Path) -> None:
    setup = setup_route(tmp_path, inputs=True)
    setup.tick()
    step = setup.workflows.current_steps(OWNER, "workflow-a")[0]
    assert (step.state, step.reason_code, step.target_key) == ("waiting_node", "inputs_receiving", "node-a")
    assert setup.count("platform_commands") == 1
    assert setup.count("workflow_dispatches") == setup.count("capability_step_bindings") == 0
    with setup.workflows.transaction(write=False) as connection:
        row = connection.execute("SELECT target_node,arguments FROM platform_commands").fetchone()
    assert row[0] == "node-a" and json.loads(row[1])["input_digest"] == step.resolved_digest
    setup.platform.upsert_node(OWNER, {"node_id": "node-a", "enabled": False})
    setup.tick()
    assert setup.workflows.current_steps(OWNER, "workflow-a")[0].target_key == "node-a"
    assert setup.count("platform_commands") == 1
    assert setup.count("workflow_dispatches") == 0


def test_input_command_failure_rolls_back_selected_target_and_frozen_contract(tmp_path: Path) -> None:
    setup = setup_route(tmp_path, inputs=True)
    with setup.workflows.transaction() as connection:
        connection.execute("CREATE TRIGGER fail_receive BEFORE INSERT ON platform_commands BEGIN SELECT RAISE(ABORT,'receive write failed'); END")
    coordinator = setup.app.extensions["fleet"]["services"]["workflow_coordinator"]
    result = asyncio.run(coordinator.tick(setup.clock[0]))
    assert len(result.failures) == 1
    step = setup.workflows.current_steps(OWNER, "workflow-a")[0]
    assert step.target_key is None and step.resolved_snapshot is None
    assert setup.count("platform_commands") == setup.count("workflow_dispatches") == 0


@pytest.mark.parametrize("change", [{"reserve_tokens": True}, {"reserve_requests": -1},
    {"definition_digest": "bad"}, {"adapter_ref": CatalogRef("other", 1)}])
def test_route_registration_rejects_wrong_identity_or_noninteger_budget(tmp_path: Path, change: dict[str, Any]) -> None:
    setup = setup_route(tmp_path)
    with pytest.raises(ValueError):
        replace(setup.route, **change)


def test_real_http_input_receipt_precedes_automatic_binding_run_and_command(tmp_path: Path) -> None:
    from fleet_core.platform_command import PlatformCommand
    from hub.domain.capability_execution import CapabilityExecutionContext
    from tools.platform.artifact_transfer import InputDirectory
    from tools.platform.node_runtime import NodeRuntime, NodeRuntimeConfig
    from tools.transport import Transport
    from test_workflow_two_nodes import server
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    setup = setup_route(tmp_path, inputs=True)
    setup.tick()
    services = setup.app.extensions["fleet"]["services"]
    delivery = services["platform_delivery"]
    public = Ed25519PrivateKey.from_private_bytes(delivery.signing_key).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    destination = tmp_path / "inputs"
    destination.mkdir(mode=0o700)
    with server(setup.app) as url:
        node = NodeRuntime(NodeRuntimeConfig(workspace_id=WORKSPACE, node_id="node-a", credential=setup.credentials["node-a"],
            workspace_root=tmp_path / "node-workspace", journal_path=tmp_path / "node.db", hub_url=url,
            public_key=public, worker_id="receiver", capabilities=frozenset({"artifact.receive"}),
            artifact_inputs=(("source", InputDirectory(destination, frozenset({".tar"}))),)),
            clock=lambda: setup.clock[0], transport=Transport(mode="direct", attempts=1))
        try:
            assert node.poll_once() == {"ok": True, "commands": 1, "receipts": 1}
            assert len(list(destination.glob("*.tar"))) == 1
            assert next(destination.glob("*.tar")).read_bytes() == b"fixed input bytes\n"
            setup.tick()
            assert setup.workflows.current_steps(OWNER, "workflow-a")[0].state == "ready"
            assert setup.count("workflow_dispatches") == 0
            setup.tick()
            setup.tick()
            run = setup.platform.claim_run(OWNER, worker_id="run-worker", now=setup.clock[0], lease_s=50)
            assert run is not None
            command = PlatformCommand.create(command_id=run["run_id"] + ":read", owner_id=OWNER,
                target_node="node-a", resource_id=WORKSPACE, run_id=run["run_id"], action="tool.workspace.read",
                arguments={"path": "report.txt"}, retry_class="read_only", expires_at=setup.clock[0] + 40)
            prepared = delivery.enqueue(command, idempotency_key=command.command_id)
            assert CapabilityExecutionContext.from_command(prepared).binding.node_id == "node-a"
            assert setup.count("runs") == setup.count("capability_step_bindings") == 1
        finally:
            node.close_all()


@pytest.mark.parametrize("changed", ["stale", "priority", "software"])
def test_input_target_never_moves_to_another_node_in_the_same_generation(tmp_path: Path, changed: str) -> None:
    setup = setup_route(tmp_path, inputs=True)
    setup.tick()
    if changed == "priority":
        with setup.workflows.transaction(write=False) as connection:
            row = connection.execute("SELECT snapshot FROM capability_node_policies WHERE node_id='node-b'").fetchone()
        setup.capabilities.register_node(replace(NodePolicy.from_snapshot(bytes(row[0])), priority=-1), now=setup.clock[0])
    elif changed == "stale":
        with setup.workflows.transaction() as connection:
            connection.execute("UPDATE capability_node_offers SET received_at=0 WHERE node_id='node-a'")
        setup.clock[0] = 121
    else:
        fingerprint = setup.platform.authenticate_node_credential("node-a", "a" * 32)["credential_fingerprint"]
        setup.capabilities.record_offer(OWNER, "node-a", json.dumps(offer_payload(offer_revision=2, slots=1,
            software={"python": "changed"}, capabilities=[{"id": CAP.id, "revision": 1}],
            local_allowed_capabilities=[{"id": CAP.id, "revision": 1}])).encode(),
            credential_fingerprint=fingerprint, now=setup.clock[0])
    setup.tick()
    assert setup.workflows.current_steps(OWNER, "workflow-a")[0].target_key == "node-a"
    with setup.workflows.transaction(write=False) as connection:
        assert {row[0] for row in connection.execute("SELECT target_node FROM platform_commands")} == {"node-a"}
    assert setup.count("workflow_dispatches") == 0


def test_admission_binding_failure_rolls_back_original_intent_budget_and_slot(tmp_path: Path) -> None:
    setup = setup_route(tmp_path)
    setup.tick()
    with setup.workflows.transaction() as connection:
        connection.execute("CREATE TRIGGER fail_binding BEFORE INSERT ON capability_step_bindings BEGIN SELECT RAISE(ABORT,'binding failed'); END")
    result = asyncio.run(setup.app.extensions["fleet"]["services"]["workflow_coordinator"].tick(setup.clock[0]))
    assert len(result.failures) == 1
    for table in ("workflow_dispatches", "workflow_budget_reservations", "capability_reservations", "capability_step_bindings"):
        assert setup.count(table) == 0
    step = setup.workflows.current_steps(OWNER, "workflow-a")[0]
    assert step.state == "ready" and step.target_key is None


@pytest.mark.parametrize("change", ["pause", "cancel", "drain", "dispatch_off"])
def test_routing_control_gate_blocks_new_input_and_execution(tmp_path: Path, change: str) -> None:
    setup = setup_route(tmp_path, inputs=True)
    if change == "dispatch_off":
        setup.app.extensions["fleet"]["services"]["workflow_coordinator"].dispatch_enabled = False
    else:
        with setup.workflows.transaction() as connection:
            if change == "drain":
                connection.execute("UPDATE workflow_schema_meta SET admission_mode='drain'")
            else:
                field = "pause_requested" if change == "pause" else "cancel_requested"
                connection.execute(f"UPDATE workflow_runs SET {field}=1")
    setup.tick()
    assert setup.count("platform_commands") == setup.count("workflow_dispatches") == setup.count("runs") == 0


def _race_route(path: str, claim: Any, route: RegisteredCapabilityRoute, target: RegisteredRunTarget,
                ready: Any, proceed: Any, queue: Any) -> None:
    workflows = WorkflowRepository(Path(path))
    capabilities = CapabilityRepository(workflows, boot_id="race-boot")
    capabilities.init()
    admission = CapabilityWorkflowAdmission(capabilities, targets=(target,))
    ready.put(True)
    assert proceed.wait(timeout=15)
    snapshot = workflows.snapshots((claim,), now=102)[0]
    from hub.domain.workflow_contract import parse_resolved
    admission.advance(claim, expected_revision=snapshot.workflow.revision, generation=1,
        resolved=parse_resolved(snapshot.steps[0].resolved_snapshot), registration=route, now=102)
    queue.put(True)


def test_two_process_routing_admissions_cannot_oversell_last_node_slot(tmp_path: Path) -> None:
    from hub.domain.workflow_contract import resolve_contract
    from hub.domain.workflow_contract_types import AuthorizedInputs
    setup = setup_route(tmp_path)
    first = setup.workflows.get(OWNER, "workflow-a")
    setup.workflows.create_or_get(OWNER, WORKSPACE, "workflow-b", client_token="workflow-b",
        definition=parse_definition(first.definition_snapshot), now=100)
    claims = setup.workflows.claim_due(OWNER, worker_id="race-worker", now=100, limit=2, lease_seconds=60)
    assert len(claims) == 2
    resolved = resolve_contract(parse_definition(first.definition_snapshot), AuthorizedInputs(OWNER, WORKSPACE, "develop", CatalogRef("cli", 1), ()))
    for claim in claims:
        setup.workflows.freeze_inputs(claim, step_key="develop", generation=1, resolved=resolved, expected_revision=1, now=101)
    coordinator = setup.app.extensions["fleet"]["services"]["workflow_coordinator"]
    target = coordinator.dispatchers[(OWNER, WORKSPACE, "node-a")].executor.target
    context = multiprocessing.get_context("spawn")
    ready, queue, proceed = context.Queue(), context.Queue(), context.Event()
    processes = [context.Process(target=_race_route, args=(str(setup.workflows.db_path), claim, setup.route,
        target, ready, proceed, queue)) for claim in claims]
    for process in processes:
        process.start()
    for _ in processes:
        assert ready.get(timeout=15)
    fingerprint = setup.platform.authenticate_node_credential("node-a", "a" * 32)["credential_fingerprint"]
    setup.capabilities.record_offer(OWNER, "node-a", json.dumps(offer_payload(slots=1,
        capabilities=[{"id": CAP.id, "revision": 1}], local_allowed_capabilities=[{"id": CAP.id, "revision": 1}])).encode(),
        credential_fingerprint=fingerprint, now=102)
    proceed.set()
    for _ in processes:
        assert queue.get(timeout=15)
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    with setup.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM capability_reservations WHERE target_key='node-a' AND state='held'").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM capability_step_bindings").fetchone()[0] == 1


def test_all_candidates_unavailable_waits_without_freezing_or_transferring(tmp_path: Path) -> None:
    setup = setup_route(tmp_path, inputs=True)
    for node in ("node-a", "node-b"):
        setup.platform.upsert_node(OWNER, {"node_id": node, "enabled": False})
    setup.tick()
    step = setup.workflows.current_steps(OWNER, "workflow-a")[0]
    assert step.state == "waiting_node" and step.reason_code == "capability_unavailable"
    assert step.target_key is None and step.resolved_snapshot is None
    assert setup.count("platform_commands") == setup.count("workflow_dispatches") == 0


def test_input_candidate_requires_exact_registered_ref_key_and_allowed_alias(tmp_path: Path) -> None:
    setup = setup_route(tmp_path, inputs=True)
    inputs = setup.app.extensions["fleet"]["services"]["artifact_inputs"]
    inputs.targets.pop((OWNER, WORKSPACE, "node-a"))
    setup.tick()
    assert setup.workflows.current_steps(OWNER, "workflow-a")[0].target_key == "node-b"
    with setup.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT target_node FROM platform_commands").fetchone()[0] == "node-b"


def test_restarted_router_waits_for_a_new_authenticated_report(tmp_path: Path) -> None:
    setup = setup_route(tmp_path)
    setup.tick()
    services = setup.app.extensions["fleet"]["services"]
    capabilities = CapabilityRepository(setup.workflows, boot_id="restarted-router")
    capabilities.init()
    target = services["workflow_coordinator"].dispatchers[(OWNER, WORKSPACE, "node-a")].executor.target
    services["workflow_coordinator"].capability_admission = CapabilityWorkflowAdmission(capabilities, targets=(target,))
    setup.tick()
    assert setup.count("capability_step_bindings") == setup.count("workflow_dispatches") == 0
    fingerprint = setup.platform.authenticate_node_credential("node-a", "a" * 32)["credential_fingerprint"]
    capabilities.record_offer(OWNER, "node-a", json.dumps(offer_payload(slots=1,
        capabilities=[{"id": CAP.id, "revision": 1}], local_allowed_capabilities=[{"id": CAP.id, "revision": 1}])).encode(),
        credential_fingerprint=fingerprint, now=setup.clock[0])
    setup.tick()
    assert setup.count("capability_step_bindings") == setup.count("workflow_dispatches") == 1


def test_existing_dispatch_uses_original_target_after_route_registration_is_removed(tmp_path: Path) -> None:
    setup = setup_route(tmp_path)
    for _ in range(3):
        setup.tick()
    coordinator = setup.app.extensions["fleet"]["services"]["workflow_coordinator"]
    coordinator.registrations.clear()
    coordinator.capability_admission = None
    setup.tick()
    assert setup.count("runs") == 1
    assert setup.workflows.get_dispatch(OWNER, "workflow-a", "develop", 1).target_key == "node-a"


def test_select_and_input_pin_cannot_observe_revocation_outside_their_transaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    setup = setup_route(tmp_path, inputs=True)
    original = setup.capabilities._select
    connections = []

    def disable_before_select(connection: Any, *args: Any, **kwargs: Any) -> Any:
        assert connection.in_transaction
        connections.append(connection)
        connection.execute("UPDATE node_credentials SET enabled=0")
        return original(connection, *args, **kwargs)

    monkeypatch.setattr(setup.capabilities, "_select", disable_before_select)
    setup.tick()
    assert len(connections) == 1
    assert setup.workflows.current_steps(OWNER, "workflow-a")[0].target_key is None
    assert setup.count("platform_commands") == 0


def test_foreign_outer_transaction_rejected_before_any_admission_write(tmp_path: Path) -> None:
    setup = setup_route(tmp_path)
    setup.tick()
    claim = setup.workflows.claim_due(OWNER, worker_id="caller", now=setup.clock[0], limit=1, lease_seconds=60)[0]
    snapshot = setup.workflows.snapshots((claim,), now=setup.clock[0])[0]
    from hub.domain.capability import CapabilityStoreError
    from hub.domain.workflow_contract import parse_resolved
    resolved = parse_resolved(snapshot.steps[0].resolved_snapshot)
    req = setup.route.requirements(parse_definition(snapshot.workflow.definition_snapshot), resolved)
    foreign = prepare_database(tmp_path / "foreign.db")
    with foreign.transaction() as connection, pytest.raises(CapabilityStoreError, match="invalid_transaction"):
        setup.capabilities.admit(claim, step_key="develop", generation=1, expected_revision=snapshot.workflow.revision,
            requirements=req, reserve_tokens=100, reserve_requests=2, now=setup.clock[0], connection=connection)
    assert setup.count("capability_step_bindings") == setup.count("workflow_dispatches") == 0


def test_unsigned_configuration_never_assembles_automatic_admission(tmp_path: Path) -> None:
    setup = setup_route(tmp_path, transfer_enabled=False, signing_enabled=False)
    setup.tick()
    assert setup.app.extensions["fleet"]["services"]["workflow_coordinator"].capability_admission is None
    assert setup.workflows.get(OWNER, "workflow-a").reason_code == "capability_unavailable"
    assert setup.count("workflow_dispatches") == setup.count("runs") == 0


def test_route_resolves_current_predecessor_output_before_pinning_input(tmp_path: Path) -> None:
    setup = setup_route(tmp_path, inputs=True, prior_output=True)
    setup.tick()
    steps = setup.workflows.current_steps(OWNER, "workflow-a")
    assert steps[0].state == "succeeded"
    assert (steps[1].step_key, steps[1].state, steps[1].target_key) == ("review", "waiting_node", "node-a")
    from hub.domain.workflow_contract import parse_resolved
    resolved = parse_resolved(steps[1].resolved_snapshot)
    reference = resolved.contract.input_refs[0].reference
    assert (reference.artifact_id, reference.sha256, reference.size) == tuple(setup.artifact[key] for key in ("artifact_id", "sha256", "size"))
    with setup.workflows.transaction(write=False) as connection:
        arguments = json.loads(connection.execute("SELECT arguments FROM platform_commands").fetchone()[0])
    assert arguments["step_key"] == "review" and arguments["input_digest"] == resolved.digest
    assert arguments["artifact_id"] == setup.artifact["artifact_id"]
