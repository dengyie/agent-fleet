"""Real SQLite admission, signed Commands, receive journals and fixed pytest reports.

The fixture launcher is plumbing only; these tests do not prove Linux confinement.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from hub.application.artifact_input_service import ArtifactInputService
from hub.application.command_delivery_service import CommandDeliveryService
from hub.application.remote_workflow_test_execution import RemoteWorkflowTestExecutionService
from hub.application.run_event_service import RunEventService
from hub.application.run_worker_service import LocalRunWorkerService
from hub.application.workflow_dispatch import WorkflowDispatchService
from hub.application.workflow_coding_delivery import WorkflowCodingDeliveryError
from hub.domain.artifact_receive import RegisteredNodeInputs
from hub.domain.capability import NodePolicy
from hub.domain.capability_execution import RegisteredCapabilityAction
from hub.domain.capability_routing import RegisteredCapabilityRoute
from fleet_core.control import generate_ed25519_keypair
from hub.domain.remote_workflow_test import REMOTE_TEST_ACTION, remote_test_command_id
from hub.domain.workflow_contract import resolve_contract
from hub.domain.workflow_contract_types import AuthorizedInputs, CatalogRef, InputBinding
from hub.domain.workflow_execution import ExecutionObservation
from hub.infrastructure.artifact_input_repository import ArtifactInputRepository
from hub.infrastructure.capability_dispatch import CapabilityDispatchRepository
from hub.infrastructure.capability_repository import CapabilityRepository
from hub.infrastructure.capability_workflow import CapabilityWorkflowAdmission
from hub.infrastructure.command_repository import CommandRepository
from hub.infrastructure.workflow_run_executor import RegisteredRunTarget, RunStepExecutor
from tools.platform.artifact_transfer import ArtifactReceiver, InputDirectory
from tools.platform.journal import NodeJournal
from tools.platform.node_client import NodeClient
from tools.platform.node_executor import NodeToolExecutor
from test_artifact_receive import ContentTransport
from test_capability_router import BOOT, definition, offer_payload, requirements
from test_remote_workflow_test_node import FixtureExecutor, fixture_test_launcher
from test_workflow_repository import OWNER, WORKSPACE
from test_workflow_test_execution import ExecutionFixture
from test_workflow_two_nodes import _private_node_directory


CAP = CatalogRef("coding.test", 1)


class RemoteExecutionFixture(ExecutionFixture):
    def __init__(self, path: Path, *, content: str | None = None) -> None:
        super().__init__(path, content=content)
        self.private, self.public = generate_ed25519_keypair()
        self.capabilities = CapabilityRepository(self.repository, boot_id=BOOT)
        self.capabilities.init()
        self.capabilities.register_definition(definition(capability_id=CAP.id, adapter_id="fixed-tests",
            retry_class="manual_only", max_active_seconds=3600), now=self.now)
        self.platform.upsert_node(OWNER, {"node_id": "node-a"})
        self.credential = self.platform.provision_node_credential(OWNER, "node-a", secret="n" * 32)["credential"]
        self.fingerprint = self.platform.authenticate_node_credential("node-a", "n" * 32)["credential_fingerprint"]
        self.capabilities.register_node(NodePolicy(OWNER, "node-a", (WORKSPACE,), (CatalogRef("coding-delivery", 1),),
            (CAP,), slot_limit=1, input_aliases=("source",), residency_aliases=("private",),
            allowed_risks=("workspace_read",)), now=self.now)
        self.capabilities.record_offer(OWNER, "node-a", json.dumps(offer_payload(slots=1,
            capabilities=[{"id": CAP.id, "revision": 1}], local_allowed_capabilities=[{"id": CAP.id, "revision": 1}])).encode(),
            credential_fingerprint=self.fingerprint, now=self.now)
        self.target = RegisteredRunTarget(OWNER, WORKSPACE, "node-a", CatalogRef("fixed-tests", 1),
            CatalogRef("coding-delivery", 1), "test-execution", b'{"execution_node_id":"node-a"}',
            test_recipe=CatalogRef("coding-delivery-tests", 1), remote_test=True)
        self.guard = CapabilityDispatchRepository(self.capabilities,
            actions=(RegisteredCapabilityAction("fixed-tests", CAP, REMOTE_TEST_ACTION),))
        self.commands = CommandRepository(self.repository.db_path, clock=self.clock)
        self.commands.init()
        self.commands.capability_guard = self.guard.guard_command
        input_repository = ArtifactInputRepository(self.repository, self.commands, signing_key=self.private, public_key=self.public)
        self.commands.artifact_receive_guard = input_repository.receipt_guard
        self.inputs = ArtifactInputService(input_repository, self.store, clock=self.clock,
            targets=(RegisteredNodeInputs(OWNER, WORKSPACE, "node-a", (("source", "source", ".tar"),)),))
        self.admission = CapabilityWorkflowAdmission(self.capabilities, targets=(self.target,), inputs=self.inputs)
        self.route = RegisteredCapabilityRoute(hashlib.sha256(self.definition.snapshot).hexdigest(), "test",
            self.target.adapter_ref, self.target.policy_ref, requirements(template=CatalogRef("coding-delivery", 1),
                adapter_id="fixed-tests", capabilities=self.definition.steps[2].contract.capability_requirements), 0, 0)
        self.executor = RunStepExecutor(self.repository, self.target, clock=self.clock, capability_dispatch=self.guard)
        input_root = _private_node_directory(path / "received")
        receive_state = _private_node_directory(path / "receive-state")
        self.receiver = ArtifactReceiver(node_id="node-a", credential=self.credential, worker_id="receiver",
            hub_url="https://hub.test", public_key=self.public, inputs={"source": InputDirectory(input_root)},
            journal_path=receive_state / "receives.db", clock=self.clock,
            transport=ContentTransport(self.store.read(OWNER, WORKSPACE, self.exported.revision.source_bundle.artifact_id)))
        self.node_executor = FixtureExecutor(self.receiver, launcher=fixture_test_launcher(), clock=self.clock, should_cancel=lambda: False)
        journal = NodeJournal(path / "node.db")
        journal.init()
        self.node = NodeClient(journal, executor=NodeToolExecutor(None, workflow_test_executor=self.node_executor,
            capability_prepare=lambda command: dict(command)), clock=self.clock, node_id="node-a", public_key=self.public)
        self.command_delivery = CommandDeliveryService(self.commands, signing_key=self.private, clock=self.clock,
            capability_dispatch=self.guard, sleep=self.deliver_test)
        self.executions = 0

    def resolved(self):
        step = self.definition.steps[2]
        return resolve_contract(self.definition, AuthorizedInputs(OWNER, WORKSPACE, "test", self.target.adapter_ref,
            (InputBinding("source", OWNER, WORKSPACE, self.exported.revision, step.contract.input_refs[0].reference),)))

    def advance(self) -> None:
        self.admission.advance(self.claim, expected_revision=self.repository.get(OWNER, "workflow-a").revision,
            generation=1, resolved=self.resolved(), registration=self.route, now=self.now)

    def receive(self) -> None:
        commands = self.command_delivery.poll("node-a", "receiver", owner_id=OWNER, credential_fingerprint=self.fingerprint)
        assert len(commands) == 1 and commands[0]["action"] == "artifact.receive"
        receipt = self.receiver.receive(commands[0])
        self.command_delivery.receipt(commands[0]["command_id"], "succeeded", result=receipt, worker_id="receiver",
            owner_id=OWNER, node_id="node-a", credential_fingerprint=self.fingerprint)

    def ready(self) -> None:
        self.advance()
        self.receive()
        self.advance()
        self.advance()

    def dispatch(self) -> Any:
        self.ready()
        return asyncio.run(WorkflowDispatchService(self.repository, self.executor, clock=self.clock).dispatch(
            self.claim, step_key="test", generation=1, expected_revision=self.repository.get(OWNER, "workflow-a").revision))

    def deliver_test(self, _: float = 0) -> None:
        commands = self.command_delivery.poll("node-a", "node-worker", owner_id=OWNER, credential_fingerprint=self.fingerprint)
        assert len(commands) == 1 and commands[0]["action"] == REMOTE_TEST_ACTION
        receipt = self.node.handle(commands[0])
        self.executions += 1
        self.command_delivery.receipt(receipt["command_id"], receipt["status"], result=receipt["result"], worker_id="node-worker",
            owner_id=OWNER, node_id="node-a", credential_fingerprint=self.fingerprint)

    def worker(self, *, enabled: bool = True, remote_enabled: bool = True) -> LocalRunWorkerService:
        service = RemoteWorkflowTestExecutionService(self.delivery, self.service, commands=self.command_delivery, clock=self.clock)
        return LocalRunWorkerService(self.platform, RunEventService(self.platform, clock=self.clock), worker_id="test-worker",
            clock=self.clock, artifact_store=self.store, remote_execution_enabled=remote_enabled,
            workflow_remote_test_executor=service if enabled else None,
            provider_factory=lambda profile: pytest.fail("remote fixed tests cannot call the model"))

    def test_command(self) -> dict[str, Any]:
        record = self.repository.get_dispatch(OWNER, "workflow-a", "test", 1)
        return self.commands.get(remote_test_command_id(record.token))

    def reservation(self) -> str:
        with self.repository.transaction(write=False) as connection:
            return connection.execute("SELECT state FROM capability_reservations WHERE step_key='test'").fetchone()[0]


@pytest.mark.parametrize("passed", [True, False])
def test_remote_report_uses_original_received_revision_and_hub_publication(tmp_path: Path, passed: bool) -> None:
    fixture = RemoteExecutionFixture(tmp_path, content=f"def test_delivery():\n    assert {passed}\n")
    observation = fixture.dispatch()
    assert fixture.worker().run_once(OWNER)["state"] == "succeeded"
    assert fixture.executions == 1 and fixture.test_command()["status"] == "succeeded"
    assert fixture.test_command()["retry_class"] == "manual_only"
    assert fixture.worker().run_once(OWNER) is None
    assert asyncio.run(fixture.executor.inspect(observation.binding)).state == "succeeded"
    assert set(fixture.outputs()) == {"test_report", "junit"}
    assert fixture.test_evidence()[0]["result"] == ("passed" if passed else "failed")
    assert fixture.test_evidence()[0]["subject_digest"] == fixture.source_receipt.subject_digest


def test_publication_conflict_retries_only_hub_transaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = RemoteExecutionFixture(tmp_path)
    fixture.dispatch()
    commit = fixture.delivery.commit
    calls = []

    def conflict_once(*args: Any, **kwargs: Any):
        calls.append(args[1])
        if len(calls) == 1:
            with fixture.repository.transaction() as connection:
                connection.execute("UPDATE workflow_runs SET revision=revision+1 WHERE workflow_id='workflow-a'")
        return commit(*args, **kwargs)

    monkeypatch.setattr(fixture.delivery, "commit", conflict_once)
    assert fixture.worker().run_once(OWNER)["state"] == "succeeded"
    assert calls == ["test", "test"] and fixture.executions == 1 and len(fixture.test_evidence()) == 1


@pytest.mark.parametrize("setting,reason", [("enabled", "workflow_test_unavailable"), ("remote_enabled", "remote_execution_disabled")])
def test_worker_gates_fail_closed_without_model_or_command(tmp_path: Path, setting: str, reason: str) -> None:
    fixture = RemoteExecutionFixture(tmp_path)
    fixture.dispatch()
    result = fixture.worker(**{setting: False}).run_once(OWNER)
    assert result["state"] == "failed" and result["result_text"] == reason
    assert fixture.test_command() is None and fixture.test_evidence() == []


@pytest.mark.parametrize("failure", ["unknown", "envelope", "report", "publication", "started"])
def test_unconfirmed_remote_execution_preserves_unknown_reservation(tmp_path: Path, failure: str, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = RemoteExecutionFixture(tmp_path)
    observation = fixture.dispatch()
    original_wait = fixture.command_delivery.wait_for_receipt

    def wait(command_id: str, **kwargs: Any):
        if failure == "unknown":
            return fixture.commands.mark_unknown(command_id, reason="receipt_timeout")
        row = original_wait(command_id, **kwargs)
        if failure == "envelope":
            return row | {"result": row["result"] | {"command_id": "another-command"}}
        if failure == "report":
            return row | {"result": row["result"] | {"result": row["result"]["result"] | {"report_hash": "0" * 64}}}
        return row

    async def reject_publication(*args: Any, **kwargs: Any):
        raise WorkflowCodingDeliveryError("subject_conflict") from ValueError("stale source")

    monkeypatch.setattr(fixture.command_delivery, "wait_for_receipt", wait)
    if failure == "publication":
        monkeypatch.setattr(fixture.service, "record_test", reject_publication)
    worker = fixture.worker()
    if failure == "started":
        event = worker._event

        def failed_start(claim: dict[str, Any], kind: str, payload: dict[str, Any]):
            if kind == "run_started":
                raise RuntimeError("event store unavailable")
            return event(claim, kind, payload)

        monkeypatch.setattr(worker, "_event", failed_start)
    result = worker.run_once(OWNER)
    assert result["state"] == "unknown"
    assert fixture.test_evidence() == [] and fixture.outputs() == {}
    record = fixture.repository.get_dispatch(OWNER, "workflow-a", "test", 1)
    fixture.repository.apply_observation(fixture.claim, record, ExecutionObservation(observation.binding, "unknown"), now=fixture.now)
    assert fixture.reservation() in {"held", "isolated"}
    assert fixture.worker().run_once(OWNER) is None


def test_terminal_command_receipt_is_recovered_without_replaying_node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = RemoteExecutionFixture(tmp_path)
    fixture.dispatch()
    claim = fixture.platform.claim_run(OWNER, worker_id="test-worker", now=fixture.now)
    lease = fixture.lease(claim)
    target = fixture.delivery.target_for_remote_test(lease, now=fixture.now)
    context, revision, _ = asyncio.run(fixture.service.source_context(target))
    request = fixture.delivery.remote_test_request(target, source_revision=revision,
        subject_digest=context.source_subject, now=fixture.now)
    from fleet_core.platform_command import PlatformCommand

    command = PlatformCommand.create(command_id=remote_test_command_id(target.binding.token), owner_id=OWNER,
        resource_id=WORKSPACE, target_node="node-a", action=REMOTE_TEST_ACTION, retry_class="manual_only",
        arguments=request.payload(), expires_at=fixture.now + 90, run_id=lease.run_id)
    fixture.command_delivery.enqueue(command, idempotency_key=command.command_id)
    fixture.deliver_test()
    monkeypatch.setattr(fixture.node_executor.runner, "run", lambda *args, **kwargs: pytest.fail("no replay"))
    service = RemoteWorkflowTestExecutionService(fixture.delivery, fixture.service, commands=fixture.command_delivery, clock=fixture.clock)
    result = asyncio.run(service.execute(lease, should_cancel=lambda: False, on_started=lambda: None))
    assert result.state == "succeeded" and fixture.executions == 1 and len(fixture.test_evidence()) == 1


def test_cancel_while_waiting_fences_unclaimed_command_without_redelivery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = RemoteExecutionFixture(tmp_path)
    fixture.dispatch()
    monkeypatch.setattr(fixture.command_delivery, "sleep", lambda _: fixture.platform.cancel_run(
        OWNER, fixture.repository.get_dispatch(OWNER, "workflow-a", "test", 1).binding.run_id, now=fixture.now))
    result = fixture.worker().run_once(OWNER)
    assert result["state"] == "unknown" and fixture.test_command()["status"] == "expired"
    assert fixture.test_command()["result"] == {"reason": "execution_cancelled"}
    assert fixture.executions == 0 and fixture.outputs() == {} and fixture.reservation() == "held"


@pytest.mark.parametrize("lost_response", [False, True])
def test_committed_remote_evidence_survives_late_cancel(tmp_path: Path, lost_response: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = RemoteExecutionFixture(tmp_path)
    observation = fixture.dispatch()
    publish = fixture.service.record_test
    calls = []

    async def late_cancel(*args: Any, **kwargs: Any):
        receipt = await publish(*args, **kwargs)
        calls.append(receipt)
        fixture.platform.cancel_run(OWNER, observation.binding.run_id, now=fixture.now)
        if lost_response and len(calls) == 1:
            raise WorkflowCodingDeliveryError("delivery_io_failed") from OSError("receipt lost")
        return receipt

    monkeypatch.setattr(fixture.service, "record_test", late_cancel)
    result = fixture.worker().run_once(OWNER)
    assert result["state"] == "succeeded"
    assert fixture.executions == 1 and len(fixture.test_evidence()) == 1
    assert len(calls) == (2 if lost_response else 1)


def test_lease_loss_during_wait_never_requeues_remote_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = RemoteExecutionFixture(tmp_path)
    fixture.dispatch()

    def lose_lease(_: float):
        with fixture.repository.transaction() as connection:
            connection.execute("UPDATE runs SET lease_owner='another-worker' WHERE run_id LIKE 'wf-run-%'")

    monkeypatch.setattr(fixture.command_delivery, "sleep", lose_lease)
    assert fixture.worker().run_once(OWNER)["state"] == "lease_lost"
    assert fixture.test_command()["status"] != "succeeded" and fixture.executions == 0
    assert fixture.outputs() == {} and fixture.reservation() == "held"


@pytest.mark.parametrize("gate", [None, "remote", "capability", "transfer", "signing"])
def test_bootstrap_requires_all_remote_dependencies(tmp_path: Path, gate: str | None) -> None:
    from hub.bootstrap import create_app
    from hub.config import FleetConfig

    fixture = RemoteExecutionFixture(tmp_path)
    config = FleetConfig.from_root(tmp_path, platform_enabled=True, platform_workflows_enabled=True,
        platform_workflow_dispatch_enabled=True, platform_worker_enabled=True, platform_capability_routing_enabled=gate != "capability",
        platform_remote_execution_enabled=gate != "remote", platform_artifact_transfer_enabled=gate != "transfer",
        platform_command_signing_raw=fixture.private if gate != "signing" else None,
        platform_db=fixture.repository.db_path, platform_artifact_root=fixture.store.root, dev_operator=OWNER, serve_frontend=False)
    dependencies = {"workflows": fixture.repository, "capabilities": fixture.capabilities,
        "workflow_clock": fixture.clock, "workflow_steps": (fixture.route,),
        "workflow_executors": ((OWNER, WORKSPACE, "node-a", fixture.executor),),
        "workflow_node_inputs": tuple(fixture.inputs.targets.values()),
        "workflow_capability_actions": (RegisteredCapabilityAction("fixed-tests", CAP, REMOTE_TEST_ACTION),)}
    if gate == "signing":
        with pytest.raises(ValueError, match="artifact transfer requires command signing"):
            create_app(config, repositories=dependencies)
        return
    app = create_app(config, repositories=dependencies)
    services = app.extensions["fleet"]["services"]
    remote = services.get("workflow_remote_test_executor")
    assert (remote is not None) == (gate is None)
    assert services["platform_worker"].workflow_remote_test_executor is remote
    assert services["platform_worker"].workflow_test_executor is services["workflow_test_executor"]
