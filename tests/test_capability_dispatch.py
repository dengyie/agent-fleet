"""Original admission, Run, approval and Command transactions share one binding."""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from hub.application.command_delivery_service import CommandDeliveryService
from hub.application.command_postcheck_service import CommandPostcheckService
from hub.application.task_service import ApplicationError
from hub.application.workflow_dispatch import WorkflowDispatchError, WorkflowDispatchService
from hub.domain.capability import CapabilityStoreError, NodePolicy, SelectionRequirements
from hub.domain.capability_execution import CAPABILITY_ARGUMENT, CapabilityExecutionContext, RegisteredCapabilityAction
from fleet_core.control import generate_ed25519_keypair
from fleet_core.platform_command import PlatformCommand, verify_command
from hub.domain.workflow_contract import parse_definition, parse_resolved, resolve_contract
from hub.domain.workflow_contract_types import AuthorizedInputs, CapabilityRequirement, CatalogRef
from hub.infrastructure.capability_dispatch import CapabilityDispatchRepository
from hub.infrastructure.capability_repository import CapabilityRepository
from hub.infrastructure.command_repository import CommandRepository
from hub.infrastructure.platform_db import PlatformRepository
from hub.infrastructure.workflow_run_executor import RegisteredRunTarget, RunStepExecutor
from hub.platform.remote_tool_broker import remote_retry_class
from test_capability_router import BOOT, OWNER, WORKSPACE, definition, offer_payload, requirements
from test_workflow_repository import prepare_database, registered


CAP = CatalogRef("workspace.files", 1)
DIGEST_CAP = CatalogRef("workspace.digest", 1)
CAPABILITY_REFS = (CAP, DIGEST_CAP)


@dataclass
class Fixture:
    repository: CapabilityRepository
    platform: PlatformRepository
    guard: CapabilityDispatchRepository
    admission: Any
    claim: Any
    target: RegisteredRunTarget
    private: bytes
    public: bytes
    clock: list[float]
    fingerprint: str
    credential: str
    action: str

    def run(self) -> str:
        executor = RunStepExecutor(self.repository.workflows, self.target, clock=lambda: self.clock[0],
                                   capability_dispatch=self.guard)
        service = WorkflowDispatchService(self.repository.workflows, executor, clock=lambda: self.clock[0])
        observation = asyncio.run(service.dispatch(self.claim, step_key="develop", generation=1,
            expected_revision=self.repository.workflows.get(OWNER, self.claim.workflow_id).revision))
        run_id = observation.binding.run_id
        assert self.platform.claim_run(OWNER, worker_id="run-worker", now=self.clock[0], lease_s=50)["run_id"] == run_id
        return run_id

    def delivery(self, *, enabled: bool = True) -> CommandDeliveryService:
        commands = CommandRepository(self.repository.workflows.db_path, clock=lambda: self.clock[0])
        commands.init()
        if enabled:
            commands.capability_guard = self.guard.guard_command
        return CommandDeliveryService(commands, signing_key=self.private, clock=lambda: self.clock[0],
                                      capability_dispatch=self.guard if enabled else None)

    def command(self, run_id: str, *, suffix: str = "one") -> PlatformCommand:
        arguments = {"session_id": "session-aaaaaaaaaa", "selector": "#go"} if self.action == "tool.browser.submit" else {"path": "report.txt", "content": "fixed output"}
        if self.action == "tool.workspace.read":
            arguments = {"path": "report.txt"}
        retry = remote_retry_class(self.action[5:])
        return PlatformCommand.create(command_id=run_id + ":step:" + suffix, owner_id=OWNER, target_node="node-a",
            resource_id=WORKSPACE, run_id=run_id, action=self.action, arguments=arguments,
            retry_class=retry, expires_at=150)

    def isolate_write(self, delivery: CommandDeliveryService, command_id: str, *, reservation: str = "isolated") -> None:
        delivery.repository.mark_unknown(command_id, reason="receipt_timeout")
        with self.repository.workflows.transaction() as connection:
            connection.execute("UPDATE workflow_dispatches SET stage='unknown'")
            connection.execute("UPDATE workflow_steps SET state='unknown'")
            connection.execute("UPDATE workflow_runs SET execution_state='unknown'")
            connection.execute("UPDATE workflow_budget_reservations SET settlement='unknown'")
            connection.execute("UPDATE capability_reservations SET state=?", (reservation,))
            connection.execute("UPDATE runs SET state='unknown',lease_expires_at=NULL")


def setup_dispatch(tmp_path: Path, *, action: str = "tool.workspace.write", digest_helper: bool = False) -> Fixture:
    refs = CAPABILITY_REFS if digest_helper else (CAP,)
    requested = tuple(CapabilityRequirement(item.id, (1,)) for item in refs)
    workflows = prepare_database(tmp_path / "platform.db")
    platform = PlatformRepository(workflows.db_path)
    platform.upsert_node(OWNER, {"node_id": "node-a", "capabilities": {"legacy": True}})
    credential = platform.provision_node_credential(OWNER, "node-a", secret="n" * 32)["credential"]
    fingerprint = platform.authenticate_node_credential("node-a", "n" * 32)["credential_fingerprint"]
    platform.create_conversation(OWNER, "step-chat", title="Step", workspace_id=WORKSPACE)
    platform.upsert_model(OWNER, {"profile_id": "fixture", "provider": "deterministic", "model": "fixture"})
    capabilities = CapabilityRepository(workflows, boot_id=BOOT)
    capabilities.init()
    retry = remote_retry_class(action[5:])
    capabilities.register_definition(definition(capability_id=CAP.id, retry_class=retry), now=100)
    if digest_helper:
        capabilities.register_definition(definition(capability_id=DIGEST_CAP.id, risk="workspace_read", retry_class="read_only"), now=100)
    capabilities.register_node(NodePolicy(OWNER, "node-a", (WORKSPACE,), (CatalogRef("fixture", 1),), refs,
        slot_limit=1, input_aliases=("source",), residency_aliases=("private",), allowed_risks=("workspace_read",)), now=100)
    capabilities.record_offer(OWNER, "node-a", json.dumps(offer_payload(slots=1,
        capabilities=[{"id": item.id, "revision": item.revision} for item in refs],
        local_allowed_capabilities=[{"id": item.id, "revision": item.revision} for item in refs])).encode(),
        credential_fingerprint=fingerprint, now=100)
    source = json.loads(registered().snapshot)
    source["steps"][0]["kind"] = "native_run"
    source["steps"][0]["contract"]["capability_requirements"] = [{"id": item.id, "revisions": [1]} for item in refs]
    workflow_definition = parse_definition(json.dumps(source).encode())
    resolved = resolve_contract(workflow_definition, AuthorizedInputs(OWNER, WORKSPACE, "develop", CatalogRef("cli", 1), ()))
    workflows.create_or_get(OWNER, WORKSPACE, "workflow-a", client_token="workflow-a", definition=workflow_definition, now=100)
    claim = workflows.claim_due(OWNER, worker_id="coordinator", now=100, limit=1, lease_seconds=60)[0]
    frozen = workflows.freeze_inputs(claim, step_key="develop", generation=1, resolved=resolved, expected_revision=1, now=101)
    req = requirements(capabilities=requested, input_digest=resolved.digest)
    admitted = capabilities.admit(claim, step_key="develop", generation=1, expected_revision=frozen.revision,
        requirements=req, reserve_tokens=100, reserve_requests=2, now=102)
    actions = (RegisteredCapabilityAction("cli", CAP, action),)
    if digest_helper:
        actions += (RegisteredCapabilityAction("cli", DIGEST_CAP, "reconcile.workspace.digest"),)
    guard = CapabilityDispatchRepository(capabilities, actions=actions)
    config = {"model_profile_id": "fixture", "execution_node_id": "node-a", "provider_snapshot": {
        "provider": "deterministic", "model": "fixture", "provider_config": {}, "capabilities": {}}}
    target = RegisteredRunTarget(OWNER, WORKSPACE, "node-a", CatalogRef("cli", 1), CatalogRef("safe", 1),
        "step-chat", json.dumps(config).encode())
    private, public = generate_ed25519_keypair()
    return Fixture(capabilities, platform, guard, admitted, claim, target, private, public, [104.0], fingerprint, credential, action)


def test_revalidation_reuses_own_last_slot_and_original_resource_reservation(tmp_path: Path) -> None:
    fixture = setup_dispatch(tmp_path)
    assert fixture.repository.select(requirements(input_digest=fixture.admission.intent.payload_hash,
        capabilities=(CapabilityRequirement(CAP.id, (1,)),)), now=104).binding is None
    with fixture.repository.workflows.transaction() as connection:
        assert fixture.guard.validate_dispatch(connection, fixture.admission.intent.token, now=104) == fixture.admission.selection.binding


@pytest.mark.parametrize("change", ["policy", "offer", "resources", "old_report", "budget", "slot", "generation", "digest"])
def test_revalidation_rejects_changed_or_incomplete_original_binding(tmp_path: Path, change: str) -> None:
    fixture = setup_dispatch(tmp_path)
    with fixture.repository.workflows.transaction() as connection:
        if change == "policy":
            row = connection.execute("SELECT snapshot FROM capability_node_policies").fetchone()
            policy = NodePolicy.from_snapshot(bytes(row[0]))
            changed = replace(policy, priority=1).snapshot
            connection.execute("UPDATE capability_node_policies SET snapshot=?,digest=?", (changed, hashlib.sha256(changed).hexdigest()))
        elif change in {"offer", "resources", "old_report"}:
            value = json.loads(connection.execute("SELECT snapshot FROM capability_node_offers").fetchone()[0])
            value["health"] = "unhealthy"
            if change == "resources":
                value["health"], value["memory_bytes"] = "healthy", 99
            if change == "old_report":
                value["health"] = "healthy"
                connection.execute("UPDATE capability_node_offers SET received_at=0")
            fixture.clock[0] = 121
            connection.execute("UPDATE capability_node_offers SET snapshot=?", (json.dumps(value).encode(),))
        elif change == "budget":
            connection.execute("UPDATE workflow_budget_reservations SET settlement='settled'")
        elif change == "slot":
            connection.execute("UPDATE capability_reservations SET state='released'")
        elif change == "generation":
            connection.execute("UPDATE workflow_steps SET is_current=0")
        else:
            connection.execute("UPDATE capability_step_bindings SET digest=?", ("0" * 64,))
    with fixture.repository.workflows.transaction() as connection, pytest.raises(CapabilityStoreError):
        fixture.guard.validate_dispatch(connection, fixture.admission.intent.token, now=121 if change == "old_report" else 104)


def test_requirements_roundtrip_rejects_noncanonical_and_hostile_types() -> None:
    req = requirements()
    assert SelectionRequirements.from_snapshot(req.snapshot) == req
    value = json.loads(req.snapshot)
    for field, hostile in (("memory_bytes", True), ("capabilities", {}), ("template", []), ("software", []), ("extra", 1)):
        with pytest.raises(ValueError):
            SelectionRequirements.from_snapshot(json.dumps(value | {field: hostile}).encode())


def test_run_command_is_prepared_signed_and_replayed_with_same_persistent_binding(tmp_path: Path) -> None:
    fixture = setup_dispatch(tmp_path)
    run_id = fixture.run()
    delivery = fixture.delivery()
    command = fixture.command(run_id)
    saved = delivery.enqueue(command, idempotency_key=command.command_id)
    assert verify_command(saved, fixture.public)
    assert CapabilityExecutionContext.from_command(saved).binding == fixture.admission.selection.binding
    assert delivery.enqueue(command, idempotency_key=command.command_id) == saved
    leased = delivery.poll("node-a", "node-worker", owner_id=OWNER)
    assert len(leased) == 1 and leased[0]["signature"] == saved["signature"]
    delivery.receipt(command.command_id, "succeeded", result={"state": "succeeded", "result": {}},
                     worker_id="node-worker", owner_id=OWNER, node_id="node-a")
    fixture.platform.upsert_node(OWNER, {"node_id": "node-a", "enabled": False})
    assert delivery.enqueue(command, idempotency_key=command.command_id)["status"] == "succeeded"
    assert delivery.poll("node-a", "new-worker", owner_id=OWNER) == []


def test_model_reserved_context_is_rejected_even_if_pre_signed(tmp_path: Path) -> None:
    fixture = setup_dispatch(tmp_path)
    run_id = fixture.run()
    command = CapabilityExecutionContext(CAP.id, "cli", fixture.admission.selection.binding).bind(fixture.command(run_id)).signed(fixture.private)
    with pytest.raises(ApplicationError) as failure:
        fixture.delivery().enqueue(command)
    assert failure.value.code == "invalid_capability_command"
    assert failure.value.__cause__ is not None


def test_gate_off_does_not_enqueue_unbound_fallback_for_capability_run(tmp_path: Path) -> None:
    fixture = setup_dispatch(tmp_path)
    run_id = fixture.run()
    delivery = fixture.delivery(enabled=False)
    with pytest.raises(ApplicationError) as failure:
        delivery.enqueue(fixture.command(run_id))
    assert failure.value.code == "capability_unavailable"
    assert failure.value.__cause__ is not None
    assert delivery.repository.get(fixture.command(run_id).command_id) is None


def test_unknown_isolated_capability_write_gets_only_descriptor_backed_digest_postcheck(tmp_path: Path) -> None:
    fixture = setup_dispatch(tmp_path, digest_helper=True)
    run_id = fixture.run()
    delivery = fixture.delivery()
    original = fixture.command(run_id, suffix="unknown-write")
    saved = delivery.enqueue(original, idempotency_key=original.command_id)
    delivery.repository.mark_unknown(original.command_id, reason="receipt_timeout")
    with fixture.repository.workflows.transaction() as connection:
        connection.execute("UPDATE workflow_dispatches SET stage='unknown'")
        connection.execute("UPDATE workflow_steps SET state='unknown'")
        connection.execute("UPDATE workflow_runs SET execution_state='unknown'")
        connection.execute("UPDATE workflow_budget_reservations SET settlement='unknown'")
        connection.execute("UPDATE capability_reservations SET state='isolated'")
    with fixture.repository.workflows.transaction() as connection:
        connection.execute("UPDATE runs SET state='unknown',lease_expires_at=NULL WHERE run_id=?", (run_id,))

    service = CommandPostcheckService(delivery.repository, delivery, clock=lambda: fixture.clock[0])
    response = service.request(OWNER, original.command_id)
    check_id = response["postcheck"]["check_command_id"]
    check = delivery.repository.get(check_id)

    assert response["postcheck"]["path"] == "report.txt"
    assert "expected_sha256" not in str(response)
    assert check["action"] == "reconcile.workspace.digest"
    assert check["retry_class"] == "read_only"
    assert check["arguments"]["path"] == "report.txt"
    assert set(check["arguments"]) == {"path", CAPABILITY_ARGUMENT}
    assert check["target_node"] == saved["target_node"] == "node-a"
    assert check["resource_id"] == saved["resource_id"] == WORKSPACE
    assert check["run_id"] == saved["run_id"] == run_id
    check_context = CapabilityExecutionContext.from_command(check)
    assert check_context.binding == fixture.admission.selection.binding
    assert check_context.capability_id == DIGEST_CAP.id
    assert delivery.repository.get(original.command_id)["status"] == "unknown"
    with fixture.repository.workflows.transaction(write=False) as connection:
        state = connection.execute(
            "SELECT w.execution_state,s.state,d.stage,r.state AS reservation_state,b.settlement "
            "FROM workflow_runs w JOIN workflow_steps s USING(owner_id,workspace_id,workflow_id) "
            "JOIN workflow_dispatches d USING(owner_id,workspace_id,workflow_id,step_key,generation) "
            "JOIN capability_reservations r USING(owner_id,workspace_id,workflow_id,step_key,generation) "
            "JOIN workflow_budget_reservations b USING(owner_id,workspace_id,workflow_id,step_key,generation)"
        ).fetchone()
    assert tuple(state) == ("unknown", "unknown", "unknown", "isolated", "unknown")


@pytest.mark.parametrize("reservation", ["held", "isolated"])
def test_capability_postcheck_reuses_only_persisted_descriptor_without_releasing_binding(tmp_path: Path, reservation: str) -> None:
    fixture = setup_dispatch(tmp_path, digest_helper=True)
    run_id = fixture.run()
    delivery = fixture.delivery()
    original = fixture.command(run_id, suffix="unknown-" + reservation)
    saved = delivery.enqueue(original, idempotency_key=original.command_id)
    delivery.repository.mark_unknown(original.command_id, reason="receipt_timeout")
    with fixture.repository.workflows.transaction() as connection:
        connection.execute("UPDATE workflow_dispatches SET stage='unknown'")
        connection.execute("UPDATE workflow_steps SET state='unknown'")
        connection.execute("UPDATE workflow_runs SET execution_state='unknown'")
        connection.execute("UPDATE workflow_budget_reservations SET settlement='unknown'")
        connection.execute("UPDATE capability_reservations SET state=?", (reservation,))
        connection.execute("UPDATE runs SET state='unknown',lease_expires_at=NULL WHERE run_id=?", (run_id,))
    service = CommandPostcheckService(delivery.repository, delivery, clock=lambda: fixture.clock[0])
    response = service.request(OWNER, original.command_id)
    check_id = response["postcheck"]["check_command_id"]
    check = delivery.repository.get(check_id)
    assert check["action"] == "reconcile.workspace.digest"
    assert check["target_node"] == saved["target_node"] and check["run_id"] == run_id
    assert CapabilityExecutionContext.from_command(check).capability_id == DIGEST_CAP.id
    assert delivery.repository.get(original.command_id)["status"] == "unknown"
    with fixture.repository.workflows.transaction(write=False) as connection:
        state = connection.execute("SELECT s.state,w.execution_state,r.state,b.settlement FROM workflow_steps s "
            "JOIN workflow_runs w USING(owner_id,workspace_id,workflow_id) "
            "JOIN capability_reservations r USING(owner_id,workspace_id,workflow_id,step_key,generation) "
            "JOIN workflow_budget_reservations b USING(owner_id,workspace_id,workflow_id,step_key,generation)").fetchone()
    assert tuple(state) == ("unknown", "unknown", reservation, "unknown")


@pytest.mark.parametrize("tamper", ["path", "node", "run", "check_id", "missing_descriptor", "released", "stale_offer"])
def test_capability_postcheck_rejects_mismatched_descriptor_or_stale_authority(tmp_path: Path, tamper: str) -> None:
    fixture = setup_dispatch(tmp_path, digest_helper=True)
    run_id = fixture.run()
    delivery = fixture.delivery()
    original = fixture.command(run_id, suffix="bad-postcheck-" + tamper)
    delivery.enqueue(original, idempotency_key=original.command_id)
    delivery.repository.mark_unknown(original.command_id, reason="receipt_timeout")
    with fixture.repository.workflows.transaction() as connection:
        connection.execute("UPDATE workflow_dispatches SET stage='unknown'")
        connection.execute("UPDATE workflow_steps SET state='unknown'")
        connection.execute("UPDATE workflow_runs SET execution_state='unknown'")
        connection.execute("UPDATE workflow_budget_reservations SET settlement='unknown'")
        connection.execute("UPDATE capability_reservations SET state='isolated'")
        connection.execute("UPDATE runs SET state='unknown',lease_expires_at=NULL WHERE run_id=?", (run_id,))
    response = CommandPostcheckService(delivery.repository, delivery, clock=lambda: fixture.clock[0]).request(OWNER, original.command_id)
    check_id = response["postcheck"]["check_command_id"]
    with fixture.repository.workflows.transaction() as connection:
        if tamper == "path":
            connection.execute("UPDATE platform_command_postchecks SET path='other.txt' WHERE check_command_id=?", (check_id,))
        elif tamper == "node":
            connection.execute("UPDATE platform_commands SET target_node='node-b' WHERE command_id=?", (check_id,))
        elif tamper == "run":
            connection.execute("UPDATE platform_commands SET run_id='other-run' WHERE command_id=?", (check_id,))
        elif tamper == "check_id":
            connection.execute("UPDATE platform_command_postchecks SET check_command_id='other-check' WHERE check_command_id=?", (check_id,))
        elif tamper == "missing_descriptor":
            connection.execute("DELETE FROM platform_command_postchecks WHERE check_command_id=?", (check_id,))
        elif tamper == "released":
            connection.execute("UPDATE capability_reservations SET state='released'")
        else:
            connection.execute("UPDATE capability_node_offers SET received_at=0")
            fixture.clock[0] = 121
    with fixture.repository.workflows.transaction() as connection, pytest.raises(CapabilityStoreError):
        row = connection.execute("SELECT * FROM platform_commands WHERE command_id=?", (check_id,)).fetchone()
        command_row = delivery.repository._decode(row)
        fixture.guard.guard_command(connection, command_row, fixture.clock[0])
    assert delivery.repository.get(original.command_id)["status"] == "unknown"


@pytest.mark.parametrize("missing", ["token", "binding"])
def test_capability_postcheck_corrupt_original_run_cannot_fall_back_to_unbound_command(tmp_path: Path, missing: str) -> None:
    fixture = setup_dispatch(tmp_path, digest_helper=True)
    run_id = fixture.run()
    delivery = fixture.delivery()
    original = fixture.command(run_id)
    delivery.enqueue(original)
    fixture.isolate_write(delivery, original.command_id)
    with fixture.repository.workflows.transaction() as connection:
        config = json.loads(connection.execute("SELECT config_snapshot FROM runs WHERE run_id=?", (run_id,)).fetchone()[0])
        config.pop("workflow_capability")
        config.pop("workflow_dispatch")
        connection.execute("UPDATE runs SET config_snapshot=? WHERE run_id=?", (json.dumps(config), run_id))
        if missing == "token":
            connection.execute("UPDATE workflow_dispatches SET binding_run_id=NULL")
        else:
            connection.execute("DELETE FROM capability_step_bindings")
    service = CommandPostcheckService(delivery.repository, delivery, clock=lambda: fixture.clock[0])
    with pytest.raises(ApplicationError):
        service.request(OWNER, original.command_id)
    assert delivery.repository.get(service._check_command_id(original.command_id)) is None


@pytest.mark.parametrize("registration", ["undeclared", "missing", "ambiguous", "write_capability", "wrong_adapter"])
def test_capability_postcheck_requires_an_independent_predeclared_registered_helper(tmp_path: Path, registration: str) -> None:
    fixture = setup_dispatch(tmp_path, digest_helper=registration != "undeclared")
    run_id = fixture.run()
    delivery = fixture.delivery()
    original = fixture.command(run_id)
    delivery.enqueue(original)
    fixture.isolate_write(delivery, original.command_id)
    actions = [RegisteredCapabilityAction("cli", CAP, original.action)]
    if registration in {"undeclared", "ambiguous"}:
        actions.append(RegisteredCapabilityAction("cli", DIGEST_CAP, "reconcile.workspace.digest"))
    if registration in {"ambiguous", "write_capability"}:
        actions.append(RegisteredCapabilityAction("cli", CAP, "reconcile.workspace.digest"))
    if registration == "wrong_adapter":
        actions.append(RegisteredCapabilityAction("other", DIGEST_CAP, "reconcile.workspace.digest"))
    guard = CapabilityDispatchRepository(fixture.repository, actions=tuple(actions))
    delivery.capability_dispatch = guard
    delivery.repository.capability_guard = guard.guard_command
    service = CommandPostcheckService(delivery.repository, delivery, clock=lambda: fixture.clock[0])
    with pytest.raises(ApplicationError):
        service.request(OWNER, original.command_id)
    assert delivery.repository.get(service._check_command_id(original.command_id)) is None
    assert delivery.repository.get(original.command_id)["status"] == "unknown"


@pytest.mark.parametrize("phase", ["enqueue", "lease"])
@pytest.mark.parametrize("tamper", ["released", "stale_report", "definition", "payload", "context", "generation",
    "owner", "resource", "cancel", "run_cancel", "run_state", "workflow_state", "step_state", "stage", "budget", "deadline"])
def test_capability_postcheck_revalidates_original_authority_before_enqueue_and_lease(tmp_path: Path, phase: str, tamper: str) -> None:
    fixture = setup_dispatch(tmp_path, digest_helper=True)
    run_id = fixture.run()
    delivery = fixture.delivery()
    original = fixture.command(run_id)
    delivery.enqueue(original)
    fixture.isolate_write(delivery, original.command_id)
    service = CommandPostcheckService(delivery.repository, delivery, clock=lambda: fixture.clock[0])
    check_id = service._check_command_id(original.command_id)
    if phase == "lease":
        service.request(OWNER, original.command_id)
    with fixture.repository.workflows.transaction() as connection:
        if tamper == "released":
            connection.execute("UPDATE capability_reservations SET state='released'")
        elif tamper == "stale_report":
            connection.execute("UPDATE capability_node_offers SET received_at=0")
            fixture.clock[0] = 121
        elif tamper == "definition":
            changed = definition(capability_id=DIGEST_CAP.id, retry_class="reconcile_before_retry")
            connection.execute("UPDATE capability_definitions SET snapshot=?,digest=? WHERE capability_id=?",
                (changed.snapshot, changed.digest, DIGEST_CAP.id))
        elif tamper in {"payload", "context"}:
            args = json.loads(connection.execute("SELECT arguments FROM platform_commands WHERE command_id=?", (original.command_id,)).fetchone()[0])
            if tamper == "payload":
                args["content"] = "changed content"
            else:
                args[CAPABILITY_ARGUMENT]["adapter_id"] = "foreign"
            connection.execute("UPDATE platform_commands SET arguments=? WHERE command_id=?", (json.dumps(args), original.command_id))
        elif tamper == "generation":
            connection.execute("UPDATE workflow_steps SET is_current=0")
        elif tamper in {"owner", "resource"}:
            field = "owner_id" if tamper == "owner" else "resource_id"
            connection.execute("UPDATE platform_commands SET " + field + "='foreign' WHERE command_id=?", (original.command_id,))
        elif tamper == "cancel":
            connection.execute("UPDATE workflow_runs SET cancel_requested=1")
        elif tamper == "run_cancel":
            connection.execute("UPDATE runs SET cancel_requested=1 WHERE run_id=?", (run_id,))
        elif tamper == "run_state":
            connection.execute("UPDATE runs SET state='failed' WHERE run_id=?", (run_id,))
        elif tamper == "workflow_state":
            connection.execute("UPDATE workflow_runs SET execution_state='failed'")
        elif tamper == "step_state":
            connection.execute("UPDATE workflow_steps SET state='failed'")
        elif tamper == "stage":
            connection.execute("UPDATE workflow_dispatches SET stage='bound'")
        elif tamper == "budget":
            connection.execute("UPDATE workflow_budget_reservations SET settlement='settled'")
        else:
            connection.execute("UPDATE workflow_steps SET active_deadline=104")
    if phase == "enqueue":
        with pytest.raises(ApplicationError):
            service.request(OWNER, original.command_id)
        assert delivery.repository.get(check_id) is None
    else:
        assert delivery.poll("node-a", "node-worker", owner_id=OWNER) == []
        check = delivery.repository.get(check_id)
        assert check["status"] == "failed" and check["attempt"] == 0
    assert delivery.repository.get(original.command_id)["status"] == "unknown"


@pytest.mark.parametrize("mode", ["pause", "drain", "frozen", "gate_off"])
def test_capability_postcheck_control_blocks_enqueue_and_defers_original_queued_check(tmp_path: Path, mode: str) -> None:
    fixture = setup_dispatch(tmp_path, digest_helper=True)
    run_id = fixture.run()
    delivery = fixture.delivery()
    original = fixture.command(run_id)
    delivery.enqueue(original)
    fixture.isolate_write(delivery, original.command_id)
    service = CommandPostcheckService(delivery.repository, delivery, clock=lambda: fixture.clock[0])
    response = service.request(OWNER, original.command_id)
    check_id = response["postcheck"]["check_command_id"]
    signed = delivery.repository.get(check_id)
    with fixture.repository.workflows.transaction() as connection:
        if mode == "pause":
            connection.execute("UPDATE workflow_runs SET pause_requested=1")
        elif mode != "gate_off":
            connection.execute("UPDATE workflow_schema_meta SET admission_mode=?", (mode,))
    blocked_delivery = fixture.delivery(enabled=False) if mode == "gate_off" else delivery
    assert blocked_delivery.poll("node-a", "node-worker", owner_id=OWNER) == []
    assert blocked_delivery.repository.get(check_id) == signed
    # A separate deterministic candidate uses the same persisted descriptor,
    # so preparation must still reject a new command while controls block it.
    pending = PlatformCommand.create(command_id=check_id, owner_id=OWNER, target_node="node-a", resource_id=WORKSPACE,
        run_id=run_id, action="reconcile.workspace.digest", arguments={"path": "report.txt"}, retry_class="read_only", expires_at=150)
    with fixture.repository.workflows.transaction() as connection, pytest.raises(CapabilityStoreError):
        if mode == "gate_off":
            from hub.infrastructure.capability_dispatch import reject_disabled_command
            reject_disabled_command(connection, pending.as_dict())
        else:
            fixture.guard.prepare_command(connection, pending, now=fixture.clock[0])
    with fixture.repository.workflows.transaction() as connection:
        connection.execute("UPDATE workflow_runs SET pause_requested=0")
        connection.execute("UPDATE workflow_schema_meta SET admission_mode='open'")
    assert delivery.poll("node-a", "node-worker", owner_id=OWNER)[0]["signature"] == signed["signature"]
    assert delivery.repository.get(original.command_id)["status"] == "unknown"


def test_capability_postcheck_cannot_be_prepared_without_descriptor_or_with_foreign_run(tmp_path: Path) -> None:
    fixture = setup_dispatch(tmp_path, digest_helper=True)
    run_id = fixture.run()
    delivery = fixture.delivery()
    original = fixture.command(run_id)
    delivery.enqueue(original)
    fixture.isolate_write(delivery, original.command_id)
    service = CommandPostcheckService(delivery.repository, delivery, clock=lambda: fixture.clock[0])
    check_id = service._check_command_id(original.command_id)
    pending = PlatformCommand.create(command_id=check_id, owner_id=OWNER, target_node="node-a", resource_id=WORKSPACE,
        run_id=run_id, action="reconcile.workspace.digest", arguments={"path": "report.txt"}, retry_class="read_only", expires_at=150)
    with pytest.raises(ApplicationError):
        delivery.enqueue(pending)
    service.request(OWNER, original.command_id)
    with fixture.repository.workflows.transaction() as connection, pytest.raises(CapabilityStoreError):
        fixture.guard.prepare_command(connection, replace(pending, run_id="foreign-run"), now=fixture.clock[0])


def test_offer_changed_after_enqueue_is_rejected_before_node_lease(tmp_path: Path) -> None:
    fixture = setup_dispatch(tmp_path)
    run_id = fixture.run()
    delivery = fixture.delivery()
    command = fixture.command(run_id)
    delivery.enqueue(command, idempotency_key=command.command_id)
    fixture.platform.upsert_node(OWNER, {"node_id": "node-a", "enabled": False})
    assert delivery.poll("node-a", "node-worker", owner_id=OWNER) == []
    row = delivery.repository.get(command.command_id)
    assert row["status"] == "failed" and row["result"]["error_code"] == "capability_changed"
    assert row["attempt"] == 0
    with fixture.repository.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT state FROM capability_reservations").fetchone()[0] == "held"


@pytest.mark.parametrize("mode", ["pause", "drain", "frozen"])
def test_workflow_control_defers_unleased_commands_until_resumed(tmp_path: Path, mode: str) -> None:
    fixture = setup_dispatch(tmp_path)
    run_id = fixture.run()
    delivery = fixture.delivery()
    command = fixture.command(run_id)
    delivery.enqueue(command, idempotency_key=command.command_id)
    with fixture.repository.workflows.transaction() as connection:
        if mode == "pause":
            connection.execute("UPDATE workflow_runs SET pause_requested=1")
        else:
            connection.execute("UPDATE workflow_schema_meta SET admission_mode=?", (mode,))
    assert delivery.poll("node-a", "node-worker", owner_id=OWNER) == []
    assert delivery.repository.get(command.command_id)["status"] == "queued"
    with fixture.repository.workflows.transaction() as connection:
        connection.execute("UPDATE workflow_runs SET pause_requested=0")
        connection.execute("UPDATE workflow_schema_meta SET admission_mode='open'")
    assert len(delivery.poll("node-a", "node-worker", owner_id=OWNER)) == 1


@pytest.mark.parametrize("enabled", [True, False])
def test_corrupt_run_markers_cannot_downgrade_persistent_binding(tmp_path: Path, enabled: bool) -> None:
    fixture = setup_dispatch(tmp_path)
    run_id = fixture.run()
    with fixture.repository.workflows.transaction() as connection:
        config = json.loads(connection.execute("SELECT config_snapshot FROM runs WHERE run_id=?", (run_id,)).fetchone()[0])
        config.pop("workflow_dispatch")
        config.pop("workflow_capability")
        connection.execute("UPDATE runs SET config_snapshot=? WHERE run_id=?", (json.dumps(config), run_id))
    delivery = fixture.delivery(enabled=enabled)
    with pytest.raises(ApplicationError) as failure:
        delivery.enqueue(fixture.command(run_id))
    assert failure.value.code in {"capability_unavailable", "capability_changed", "capability_snapshot_corrupt"}
    assert delivery.repository.get(fixture.command(run_id).command_id) is None


def test_corrupt_run_configuration_digest_cannot_create_new_commands(tmp_path: Path) -> None:
    fixture = setup_dispatch(tmp_path)
    run_id = fixture.run()
    with fixture.repository.workflows.transaction() as connection:
        config = json.loads(connection.execute("SELECT config_snapshot FROM runs WHERE run_id=?", (run_id,)).fetchone()[0])
        config["workflow_dispatch"]["config_digest"] = "0" * 64
        connection.execute("UPDATE runs SET config_snapshot=? WHERE run_id=?", (json.dumps(config), run_id))
    with pytest.raises(ApplicationError) as failure:
        fixture.delivery().enqueue(fixture.command(run_id))
    assert failure.value.code == "capability_changed"


def test_model_reserved_parameter_is_rejected_before_remote_enqueue(tmp_path: Path) -> None:
    from hub.platform.remote_tool_broker import RemoteToolBroker

    fixture = setup_dispatch(tmp_path)
    run_id = fixture.run()
    delivery = fixture.delivery()
    command = fixture.command(run_id)
    broker = RemoteToolBroker(delivery, node_id="node-a", resource_id=WORKSPACE, run_id=run_id)
    receipt = broker.execute(command_id=command.command_id, tool="workspace.write", owner_id=OWNER, epoch=1,
        arguments={**command.arguments, CAPABILITY_ARGUMENT: {}})
    assert receipt.state == "failed" and receipt.error_code == "invalid_capability_command"
    assert delivery.repository.get(command.command_id) is None


def test_browser_approval_and_capability_command_commit_once_and_roll_back_together(tmp_path: Path) -> None:
    from hub.infrastructure.browser_repository import BrowserRepository

    fixture = setup_dispatch(tmp_path, action="tool.browser.submit")
    run_id = fixture.run()
    browser = BrowserRepository(fixture.repository.workflows.db_path, clock=lambda: fixture.clock[0])
    browser.init()
    browser.create_session(OWNER, workspace_id=WORKSPACE, run_id=run_id, node_id="node-a",
                           profile_id="profile-a", session_id="session-aaaaaaaaaa")
    approval = browser.grant_submit_approval(OWNER, workspace_id=WORKSPACE, run_id=run_id, node_id="node-a",
                                           session_id="session-aaaaaaaaaa", selector="#go")
    delivery = fixture.delivery()
    command = fixture.command(run_id)
    with fixture.repository.workflows.transaction() as connection:
        connection.execute("CREATE TRIGGER reject_outbox BEFORE INSERT ON platform_command_outbox BEGIN SELECT RAISE(ABORT,'test failure'); END")
    with pytest.raises(ApplicationError) as failure:
        delivery.enqueue_submit(command, approvals=browser)
    assert failure.value.__cause__ is not None
    assert browser.get_submit_approval(OWNER, approval["approval_id"])["state"] == "active"
    assert delivery.repository.get(command.command_id) is None
    with fixture.repository.workflows.transaction() as connection:
        connection.execute("DROP TRIGGER reject_outbox")
    saved = delivery.enqueue_submit(command, approvals=browser)
    assert verify_command(saved, fixture.public)
    assert saved["arguments"]["approval_id"] == approval["approval_id"]
    assert CapabilityExecutionContext.from_command(saved).binding == fixture.admission.selection.binding
    assert browser.get_submit_approval(OWNER, approval["approval_id"])["state"] == "consumed"
    fixture.platform.upsert_node(OWNER, {"node_id": "node-a", "enabled": False})
    assert delivery.enqueue_submit(command, approvals=browser) == saved


@pytest.mark.parametrize("change", ["cancel", "run_lease", "generation", "digest"])
def test_invalid_execution_scope_never_reaches_first_command_lease(tmp_path: Path, change: str) -> None:
    fixture = setup_dispatch(tmp_path)
    run_id = fixture.run()
    delivery = fixture.delivery()
    command = fixture.command(run_id)
    delivery.enqueue(command)
    with fixture.repository.workflows.transaction() as connection:
        if change == "cancel":
            connection.execute("UPDATE workflow_runs SET cancel_requested=1")
        elif change == "run_lease":
            connection.execute("UPDATE runs SET lease_expires_at=104")
        elif change == "generation":
            connection.execute("UPDATE workflow_steps SET is_current=0")
        else:
            connection.execute("UPDATE capability_step_bindings SET digest=?", ("0" * 64,))
    assert delivery.poll("node-a", "node-worker", owner_id=OWNER) == []
    row = delivery.repository.get(command.command_id)
    assert row["attempt"] == 0 and row["status"] == "failed"


def test_capability_change_cannot_retract_inflight_or_completed_command_fact(tmp_path: Path) -> None:
    fixture = setup_dispatch(tmp_path)
    run_id = fixture.run()
    delivery = fixture.delivery()
    command = fixture.command(run_id)
    delivery.enqueue(command, idempotency_key=command.command_id)
    delivery.poll("node-a", "node-worker", owner_id=OWNER)
    fixture.platform.upsert_node(OWNER, {"node_id": "node-a", "enabled": False})
    result = {"state": "succeeded", "result": {"bytes_written": 12}}
    receipt = delivery.receipt(command.command_id, "succeeded", result=result,
                              worker_id="node-worker", owner_id=OWNER, node_id="node-a")
    assert receipt["result"] == result and receipt["status"] == "succeeded"
    assert delivery.enqueue(command, idempotency_key=command.command_id) == receipt


@pytest.mark.parametrize("change", ["disable", "wrong_node", "missing_guard"])
def test_run_creation_refuses_changed_node_or_missing_guard_without_child_run(tmp_path: Path, change: str) -> None:
    fixture = setup_dispatch(tmp_path)
    target = fixture.target
    if change == "disable":
        fixture.platform.upsert_node(OWNER, {"node_id": "node-a", "enabled": False})
    if change == "wrong_node":
        config = json.loads(target.config_snapshot)
        config["execution_node_id"] = "node-b"
        target = replace(target, config_snapshot=json.dumps(config).encode())
    executor = RunStepExecutor(fixture.repository.workflows, target, clock=lambda: fixture.clock[0],
                              capability_dispatch=None if change == "missing_guard" else fixture.guard)
    service = WorkflowDispatchService(fixture.repository.workflows, executor, clock=lambda: fixture.clock[0])
    with pytest.raises(WorkflowDispatchError):
        asyncio.run(service.dispatch(fixture.claim, step_key="develop", generation=1,
            expected_revision=fixture.repository.workflows.get(OWNER, fixture.claim.workflow_id).revision))
    with fixture.repository.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def test_generic_enqueue_cannot_sign_unconsumed_browser_approval(tmp_path: Path) -> None:
    fixture = setup_dispatch(tmp_path, action="tool.browser.submit")
    run_id = fixture.run()
    command = fixture.command(run_id)
    from fleet_core.platform_command import args_hash
    arguments = {**command.arguments, "approval_id": "approval-aaaaaaaaaa"}
    command = replace(command, arguments=arguments, args_digest=args_hash(arguments))
    delivery = fixture.delivery()
    with pytest.raises(ApplicationError) as failure:
        delivery.enqueue(command)
    assert failure.value.code == "approval_required"
    assert delivery.repository.get(command.command_id) is None


def test_injected_context_must_fit_original_argument_limit(tmp_path: Path) -> None:
    fixture = setup_dispatch(tmp_path)
    run_id = fixture.run()
    command = fixture.command(run_id)
    from fleet_core.platform_command import args_hash
    arguments = {"path": "report.txt", "content": "x" * 65000}
    command = replace(command, arguments=arguments, args_digest=args_hash(arguments))
    delivery = fixture.delivery()
    with pytest.raises(ApplicationError) as failure:
        delivery.enqueue(command)
    assert failure.value.code == "arguments_too_large"
    assert delivery.repository.get(command.command_id) is None


def test_capability_command_expiry_is_bounded_by_original_active_deadline(tmp_path: Path) -> None:
    fixture = setup_dispatch(tmp_path)
    run_id = fixture.run()
    command = replace(fixture.command(run_id), expires_at=1000)
    saved = fixture.delivery().enqueue(command)
    assert saved["expires_at"] == 162
    assert verify_command(saved, fixture.public)


def test_removed_action_registration_fails_queued_command_without_delivery(tmp_path: Path) -> None:
    fixture = setup_dispatch(tmp_path)
    run_id = fixture.run()
    delivery = fixture.delivery()
    command = fixture.command(run_id)
    delivery.enqueue(command)
    changed = CapabilityDispatchRepository(fixture.repository)
    delivery.repository.capability_guard = changed.guard_command
    assert delivery.poll("node-a", "node-worker", owner_id=OWNER) == []
    row = delivery.repository.get(command.command_id)
    assert row["status"] == "failed" and row["attempt"] == 0


def test_gate_off_queued_command_waits_then_uses_original_signature_when_enabled(tmp_path: Path) -> None:
    fixture = setup_dispatch(tmp_path)
    run_id = fixture.run()
    delivery = fixture.delivery()
    command = fixture.command(run_id)
    saved = delivery.enqueue(command, idempotency_key=command.command_id)
    delivery.repository.capability_guard = None
    assert delivery.poll("node-a", "node-worker", owner_id=OWNER) == []
    assert delivery.repository.get(command.command_id)["status"] == "queued"
    delivery.repository.capability_guard = fixture.guard.guard_command
    leased = delivery.poll("node-a", "node-worker", owner_id=OWNER)
    assert len(leased) == 1 and leased[0]["signature"] == saved["signature"]


@pytest.mark.parametrize("column", ["active_deadline", "lease_expires_at"])
def test_corrupt_lifecycle_timestamps_use_typed_rejection_with_cause(tmp_path: Path, column: str) -> None:
    fixture = setup_dispatch(tmp_path)
    run_id = fixture.run()
    delivery = fixture.delivery()
    command = fixture.command(run_id)
    with fixture.repository.workflows.transaction() as connection:
        if column == "active_deadline":
            connection.execute("UPDATE workflow_steps SET active_deadline='invalid'")
        else:
            connection.execute("UPDATE runs SET lease_expires_at='invalid'")
    with pytest.raises(ApplicationError) as failure:
        delivery.enqueue(command)
    assert failure.value.code == "capability_snapshot_corrupt"
    assert failure.value.__cause__ is not None


def test_other_isolated_slot_cannot_be_subtracted_as_own_reservation(tmp_path: Path) -> None:
    fixture = setup_dispatch(tmp_path)
    workflows = fixture.repository.workflows
    with workflows.transaction(write=False) as connection:
        original = connection.execute("SELECT definition_snapshot FROM workflow_runs").fetchone()[0]
    workflow_definition = parse_definition(bytes(original))
    workflows.create_or_get(OWNER, WORKSPACE, "workflow-b", client_token="workflow-b", definition=workflow_definition, now=104)
    claim = workflows.claim_due(OWNER, worker_id="coordinator-b", now=104, limit=1, lease_seconds=60)[0]
    resolved = parse_resolved(fixture.admission.intent.resolved_snapshot)
    frozen = workflows.freeze_inputs(claim, step_key="develop", generation=1, resolved=resolved, expected_revision=1, now=104)
    workflows.admit_step(claim, step_key="develop", generation=1, expected_revision=frozen.revision, target_key="node-a",
                         capacity=2, reserve_tokens=100, reserve_requests=2, now=104)
    with workflows.transaction() as connection:
        connection.execute("UPDATE capability_reservations SET state='isolated' WHERE workflow_id='workflow-b'")
    with workflows.transaction() as connection, pytest.raises(CapabilityStoreError) as failure:
        fixture.guard.validate_dispatch(connection, fixture.admission.intent.token, now=104)
    assert failure.value.code == "capability_changed"


def test_lost_run_response_looks_up_original_run_after_report_changes(tmp_path: Path) -> None:
    fixture = setup_dispatch(tmp_path)
    workflows = fixture.repository.workflows
    record = workflows.begin_dispatch(fixture.claim, step_key="develop", generation=1,
        expected_revision=workflows.get(OWNER, fixture.claim.workflow_id).revision, now=104)
    executor = RunStepExecutor(workflows, fixture.target, clock=lambda: fixture.clock[0], capability_dispatch=fixture.guard)
    first = asyncio.run(executor.create_or_lookup(record.token, record.payload_hash, record.resolved))
    fixture.platform.upsert_node(OWNER, {"node_id": "node-a", "enabled": False})
    assert asyncio.run(executor.create_or_lookup(record.token, record.payload_hash, record.resolved)) == first
    assert asyncio.run(executor.lookup(record.token, record.payload_hash, record.resolved)) == first
    with workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 1


@pytest.mark.parametrize("change", ["retry", "owner", "workspace", "node"])
def test_new_commands_cannot_widen_fixed_execution_scope(tmp_path: Path, change: str) -> None:
    fixture = setup_dispatch(tmp_path)
    run_id = fixture.run()
    command = fixture.command(run_id)
    overrides = {"retry": {"retry_class": "read_only"}, "owner": {"owner_id": "other@example.test"},
                 "workspace": {"resource_id": "other"}, "node": {"target_node": "node-b"}}
    delivery = fixture.delivery()
    with pytest.raises(ApplicationError) as failure:
        delivery.enqueue(replace(command, **overrides[change]))
    assert failure.value.code == "capability_changed"
    assert delivery.repository.get(command.command_id) is None


def test_duplicate_changed_retry_class_cannot_reuse_original_signed_command(tmp_path: Path) -> None:
    fixture = setup_dispatch(tmp_path)
    run_id = fixture.run()
    delivery = fixture.delivery()
    command = fixture.command(run_id)
    saved = delivery.enqueue(command, idempotency_key=command.command_id)
    with pytest.raises(ApplicationError) as failure:
        delivery.enqueue(replace(command, retry_class="read_only"), idempotency_key=command.command_id)
    assert failure.value.code == "idempotency_conflict"
    assert delivery.repository.get(command.command_id) == saved


def test_direct_repository_cannot_bypass_context_preparation(tmp_path: Path) -> None:
    from hub.infrastructure.command_repository import CommandRepositoryError

    fixture = setup_dispatch(tmp_path)
    run_id = fixture.run()
    commands = fixture.delivery().repository
    command = fixture.command(run_id).signed(fixture.private)
    with pytest.raises(CommandRepositoryError) as failure:
        commands.enqueue(command)
    assert failure.value.code == "invalid_capability_command"
    assert commands.get(command.command_id) is None


def test_capability_commands_require_signing_even_if_legacy_signature_gate_is_off(tmp_path: Path) -> None:
    fixture = setup_dispatch(tmp_path)
    run_id = fixture.run()
    delivery = fixture.delivery()
    delivery.signing_key = None
    with pytest.raises(ApplicationError) as failure:
        delivery.enqueue(fixture.command(run_id))
    assert failure.value.code == "command_signature_required"


def test_unknown_command_does_not_redeliver_after_capability_changes(tmp_path: Path) -> None:
    fixture = setup_dispatch(tmp_path)
    run_id = fixture.run()
    delivery = fixture.delivery()
    command = fixture.command(run_id)
    delivery.enqueue(command, idempotency_key=command.command_id)
    delivery.poll("node-a", "node-worker", owner_id=OWNER)
    delivery.repository.mark_unknown(command.command_id, reason="receipt_timeout")
    fixture.platform.upsert_node(OWNER, {"node_id": "node-a", "enabled": False})
    row = delivery.enqueue(command, idempotency_key=command.command_id)
    assert row["status"] == "unknown"
    assert delivery.poll("node-a", "node-worker", owner_id=OWNER) == []
    with fixture.repository.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT state FROM capability_reservations").fetchone()[0] == "held"


def test_restart_requires_fresh_report_before_new_lease_and_retains_original_binding(tmp_path: Path) -> None:
    fixture = setup_dispatch(tmp_path)
    run_id = fixture.run()
    delivery = fixture.delivery()
    command = fixture.command(run_id)
    saved = delivery.enqueue(command, idempotency_key=command.command_id)
    restarted = CapabilityRepository(fixture.repository.workflows, boot_id="new-boot")
    restarted.init()
    guard = CapabilityDispatchRepository(restarted, actions=(RegisteredCapabilityAction("cli", CAP, fixture.action),
        RegisteredCapabilityAction("cli", DIGEST_CAP, "reconcile.workspace.digest")))
    delivery.repository.capability_guard = guard.guard_command
    assert delivery.poll("node-a", "node-worker", owner_id=OWNER) == []
    row = delivery.repository.get(command.command_id)
    assert row["status"] == "failed" and row["attempt"] == 0
    assert row["signature"] == saved["signature"]


def test_registration_must_be_explicit_supported_and_unambiguous(tmp_path: Path) -> None:
    from hub.domain.capability import CapabilityError

    fixture = setup_dispatch(tmp_path)
    for action in ("artifact.receive", "tool.service.request_action", "tool.nonexistent", "reconcile.service.inspect", "reconcile.service.logs"):
        with pytest.raises(CapabilityError):
            registration = RegisteredCapabilityAction("cli", CAP, action)
            CapabilityDispatchRepository(fixture.repository, actions=(registration,))
    registration = RegisteredCapabilityAction("cli", CAP, fixture.action)
    with pytest.raises(CapabilityError, match="duplicate_capability_action"):
        CapabilityDispatchRepository(fixture.repository, actions=(registration, registration))


def test_run_creator_rejects_local_review_or_legacy_target_for_node_binding(tmp_path: Path) -> None:
    from hub.infrastructure.workflow_run_executor import LegacyTaskTarget

    fixture = setup_dispatch(tmp_path)
    workflows = fixture.repository.workflows
    target = replace(fixture.target, legacy_task=LegacyTaskTarget("mac-local", "codex", "agent-fleet"))
    executor = RunStepExecutor(workflows, target, clock=lambda: fixture.clock[0], capability_dispatch=fixture.guard)
    resolved = parse_resolved(fixture.admission.intent.resolved_snapshot)
    record = workflows.begin_dispatch(fixture.claim, step_key="develop", generation=1,
        expected_revision=workflows.get(OWNER, fixture.claim.workflow_id).revision, now=104)
    with pytest.raises(WorkflowDispatchError, match="unsupported_capability_target"):
        asyncio.run(executor.create_or_lookup(record.token, record.payload_hash, resolved))
    with workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def test_application_assembly_wires_existing_native_executor_and_delivery_guard(tmp_path: Path) -> None:
    from hub.bootstrap import create_app
    from hub.config import FleetConfig

    fixture = setup_dispatch(tmp_path)
    executor = RunStepExecutor(fixture.repository.workflows, fixture.target, clock=lambda: fixture.clock[0])
    config = FleetConfig.from_root(tmp_path, platform_enabled=True, platform_workflows_enabled=True,
        platform_workflow_dispatch_enabled=True, platform_capability_routing_enabled=True,
        platform_db=fixture.repository.workflows.db_path, dev_operator=OWNER, serve_frontend=False)
    app = create_app(replace(config, platform_command_signing_raw=fixture.private), repositories={
        "workflows": fixture.repository.workflows, "capabilities": fixture.repository,
        "workflow_clock": lambda: fixture.clock[0],
        "workflow_capability_actions": (RegisteredCapabilityAction("cli", CAP, fixture.action),
            RegisteredCapabilityAction("cli", DIGEST_CAP, "reconcile.workspace.digest")),
        "workflow_executors": ((OWNER, WORKSPACE, "node-a", executor),)})
    services = app.extensions["fleet"]["services"]
    assert executor.capability_dispatch is services["capability_dispatch"]
    assert services["platform_delivery"].capability_dispatch is services["capability_dispatch"]
    assert services["platform_delivery"].repository.capability_guard is not None
    fixture.repository.workflows.release_claim(fixture.claim, now=104, next_poll_at=104)
    summary = asyncio.run(services["workflow_coordinator"].tick(104))
    assert not summary.failures
    with fixture.repository.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT stage FROM workflow_dispatches").fetchone()[0] == "bound"
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
