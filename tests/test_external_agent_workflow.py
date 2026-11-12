"""Original Workflow dispatch over real SQLite and a controlled Muse journal."""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from hub.application.workflow_dispatch import WorkflowDispatchDeferred, WorkflowDispatchError, WorkflowDispatchService
from hub.application.workflow_external_execution import ExternalStepExecutor
from hub.domain.external_agent import CancelReceipt, ExternalAgentCapabilities, ExternalAgentError, ExternalAgentStatus, ExternalTaskRef
from hub.domain.workflow_contract import parse_definition, resolve_contract
from hub.domain.workflow_contract_types import AuthorizedInputs, CatalogRef, ResolvedContract
from hub.domain.workflow_execution import ExecutionBinding
from hub.domain.workflow_external import RegisteredExternalTarget, parse_external_target
from hub.domain.workflow_records import CoordinatorClaim
from hub.infrastructure.workflow_external_repository import ExternalWorkflowRepository
from hub.infrastructure.workflow_repository import WorkflowRepository, WorkflowRepositoryError
from tools.browser_bridge.muse import MuseBridge
from connectors.external_agents.muse import MuseExternalAgentAdapter
from test_muse_browser_bridge import Transport, message
from test_workflow_dispatch import Clock, SimulatedCrash
from test_workflow_repository import OWNER, WORKSPACE, prepare_database, registered


@dataclass
class ExternalSetup:
    repository: WorkflowRepository
    store: ExternalWorkflowRepository
    clock: Clock
    claim: CoordinatorClaim
    resolved: ResolvedContract
    target: RegisteredExternalTarget
    adapter: MuseExternalAgentAdapter
    transport: Transport

    def admit(self, target: RegisteredExternalTarget | None = None) -> None:
        self.repository.admit_step(self.claim, step_key="develop", generation=1,
            expected_revision=self.repository.get(OWNER, "workflow-a").revision,
            target_key=self.target.target_key, capacity=1, reserve_tokens=100, reserve_requests=2,
            now=102, external_target=target)

    def executor(self, *, enabled: bool = True, adapter: Any = None, target: RegisteredExternalTarget | None = None) -> ExternalStepExecutor:
        return ExternalStepExecutor(self.store, target or self.target, adapter or self.adapter, clock=self.clock, enabled=enabled)

    def dispatch(self, executor: Any = None):
        service = WorkflowDispatchService(self.repository, executor or self.executor(), clock=self.clock)
        return asyncio.run(service.dispatch(self.claim, step_key="develop", generation=1,
            expected_revision=self.repository.get(OWNER, "workflow-a").revision))

    def recover(self) -> None:
        self.clock.now = 130
        self.claim = self.repository.claim_due(OWNER, worker_id="recovered", now=130, limit=1, lease_seconds=60)[0]

    def apply(self, observed):
        record = self.repository.get_dispatch(OWNER, "workflow-a", "develop", 1)
        return self.repository.apply_observation(self.claim, record, observed, now=self.clock())


@pytest.fixture
def external(tmp_path: Path) -> ExternalSetup:
    return make_external(tmp_path)


def make_external(tmp_path: Path, *, admit: bool = True,
                  capabilities: ExternalAgentCapabilities | None = None,
                  contract_changes: dict[str, Any] | None = None,
                  following_steps: tuple[dict[str, Any], ...] = ()) -> ExternalSetup:
    repository = prepare_database(tmp_path / "platform.db")
    store = ExternalWorkflowRepository(repository)
    store.init()
    transport = Transport()
    adapter = MuseExternalAgentAdapter(MuseBridge(tmp_path / "muse.db", transport=transport),
        workspace_id=WORKSPACE, account_ref="account-a", transport_ref="fixture-v1")
    adapter.bridge.initialize()
    if capabilities is not None:
        adapter.capabilities = capabilities
    target = RegisteredExternalTarget(OWNER, WORKSPACE, "muse-account-a", CatalogRef("muse", 1), CatalogRef("safe", 1),
        adapter.agent_id, adapter.configuration_digest, adapter.capabilities)
    raw = json.loads(registered().snapshot)
    raw["steps"][0]["kind"] = "external_agent"
    raw["steps"][0]["contract"]["output_specs"] = []
    raw["steps"][0]["contract"].update(contract_changes or {})
    raw["steps"].extend(following_steps)
    definition = parse_definition(json.dumps(raw).encode())
    repository.create_or_get(OWNER, WORKSPACE, "workflow-a", client_token="external-a", definition=definition, now=100)
    claim = repository.claim_due(OWNER, worker_id="worker", now=100, limit=1, lease_seconds=20)[0]
    resolved = resolve_contract(definition, AuthorizedInputs(OWNER, WORKSPACE, "develop", CatalogRef("muse", 1), ()))
    ready = repository.freeze_inputs(claim, step_key="develop", generation=1, resolved=resolved, expected_revision=1, now=101)
    setup = ExternalSetup(repository, store, Clock(), claim, resolved, target, adapter, transport)
    if admit:
        setup.admit(target)
    return setup


def held(external: ExternalSetup) -> str:
    with external.repository.transaction(write=False) as connection:
        return connection.execute("SELECT state FROM capability_reservations").fetchone()[0]


def test_external_target_roundtrip_is_strict_and_nonsecret(external: ExternalSetup) -> None:
    assert parse_external_target(external.target.snapshot) == external.target
    assert hashlib.sha256(external.target.snapshot).hexdigest() == external.target.digest
    assert b"muse.db" not in external.target.snapshot and b"account_ref" not in external.target.snapshot
    raw = json.loads(external.target.snapshot)
    raw["extra"] = "forbidden"
    with pytest.raises(ValueError):
        parse_external_target(json.dumps(raw).encode())


def test_external_capability_requirements_are_pinned_to_registered_adapter(tmp_path: Path) -> None:
    requirement = CatalogRef("external.tool", 2)
    capabilities = ExternalAgentCapabilities(True, True, True, False, False, False, (requirement,))
    setup = make_external(tmp_path, capabilities=capabilities,
        contract_changes={"capability_requirements": [{"id": requirement.id, "revisions": [requirement.revision]}]})
    observed = setup.dispatch()
    assert observed.state == "waiting_external"
    assert len(setup.transport.sends) == 1


@pytest.mark.parametrize('phase', ['snapshot', 'dispatch'])
def test_external_capability_order_survives_snapshot_and_dispatch_recovery(tmp_path: Path, phase: str) -> None:
    references = (CatalogRef('z.tool', 2), CatalogRef('a.tool', 1))
    capabilities = ExternalAgentCapabilities(True, True, True, False, False, False, references)
    setup = make_external(tmp_path, capabilities=capabilities,
        contract_changes={'capability_requirements': [{'id': 'z.tool', 'revisions': [2]}]})
    if phase == 'snapshot':
        assert parse_external_target(setup.target.snapshot) == setup.target
        return
    first = setup.dispatch()
    assert first.state == 'waiting_external'
    setup.apply(first)
    setup.recover()
    # A restart can enumerate the same capability set in another order.
    setup.adapter.capabilities = replace(capabilities, supported_capabilities=tuple(reversed(references)))
    target = replace(setup.target, capabilities=setup.adapter.capabilities)
    assert target.digest == setup.target.digest
    recovered = setup.dispatch(setup.executor(target=target))
    assert recovered.binding == first.binding
    assert len(setup.transport.sends) == 1


def test_external_capability_requirement_does_not_fall_back_to_unadvertised_adapter(tmp_path: Path) -> None:
    setup = make_external(tmp_path,
        admit=False,
        contract_changes={"capability_requirements": [{"id": "external.tool", "revisions": [2]}]})
    with pytest.raises(WorkflowRepositoryError, match="external_capability_unavailable"):
        setup.admit(setup.target)
    assert setup.transport.sends == []


def test_external_admission_pins_original_config_and_creates_no_run(external: ExternalSetup) -> None:
    with external.repository.transaction(write=False) as connection:
        row = connection.execute("SELECT target_snapshot,target_digest,send_started_at,task_snapshot FROM workflow_external_bindings").fetchone()
        assert (bytes(row[0]), row[1], row[2], row[3]) == (external.target.snapshot, external.target.digest, None, None)
        assert connection.execute("SELECT minimum_reader_version FROM workflow_schema_meta").fetchone()[0] == 6
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    assert external.transport.sends == []


def test_external_submit_and_restart_reuse_original_binding(external: ExternalSetup) -> None:
    first = external.dispatch()
    assert first.binding.kind == "external" and first.binding.run_id is None
    assert first.state == "waiting_external" and first.evidence_source == "accepted"
    assert external.resolved.snapshot.decode() in external.transport.sends[0]["prompt"]
    external.recover()
    restarted = MuseExternalAgentAdapter(MuseBridge(external.adapter.bridge.path, transport=external.transport),
        workspace_id=WORKSPACE, account_ref="account-a", transport_ref="fixture-v1")
    assert external.dispatch(external.executor(adapter=restarted)).binding == first.binding
    assert len(external.transport.sends) == 1 and held(external) == "held"


@pytest.mark.parametrize("window", ["after_send", "after_task_pin", "after_binding"])
def test_external_crash_windows_only_recover_original_task(external: ExternalSetup, monkeypatch, window: str) -> None:
    executor = external.executor()
    if window == "after_send":
        original = external.store.bind_task

        def crash(*args, **kwargs):
            raise SimulatedCrash()

        monkeypatch.setattr(external.store, "bind_task", crash)
    elif window == "after_task_pin":
        class Crash(ExternalStepExecutor):
            async def create_or_lookup(self, *args, **kwargs):
                await super().create_or_lookup(*args, **kwargs)
                raise SimulatedCrash()

        executor = Crash(external.store, external.target, external.adapter, clock=external.clock, enabled=True)
    else:
        class Crash(ExternalStepExecutor):
            async def inspect(self, binding):
                raise SimulatedCrash()

        executor = Crash(external.store, external.target, external.adapter, clock=external.clock, enabled=True)
    with pytest.raises(SimulatedCrash):
        external.dispatch(executor)
    if window == "after_send":
        monkeypatch.setattr(external.store, "bind_task", original)
    external.recover()
    observed = external.dispatch()
    assert observed.state == "waiting_external"
    assert len(external.transport.sends) == 1


@pytest.mark.parametrize("change", ["account", "scope", "journal", "revision", "capabilities"])
def test_external_config_drift_rejects_before_any_external_io(external: ExternalSetup, change: str) -> None:
    external.dispatch()
    if change == "account":
        external.adapter.account_ref = "different"
    elif change == "scope":
        external.adapter.workspace_id = "other"
    elif change == "journal":
        external.adapter.bridge.path = external.adapter.bridge.path.with_name("different.db")
    elif change == "capabilities":
        external.adapter.capabilities = replace(external.adapter.capabilities, cancel_supported=True)
    target = replace(external.target, adapter_ref=CatalogRef("muse", 2)) if change == "revision" else external.target

    def forbidden(*args, **kwargs):
        raise AssertionError("changed adapter performed external I/O")

    external.adapter.inspect = forbidden
    with pytest.raises(WorkflowDispatchError):
        external.dispatch(external.executor(target=target))
    assert len(external.transport.sends) == 1 and held(external) == "held"


def test_external_lookup_error_is_not_absence_and_never_replays(external: ExternalSetup, monkeypatch) -> None:
    executor = external.executor()
    original = external.store.bind_task
    monkeypatch.setattr(external.store, "bind_task", lambda *a, **kw: (_ for _ in ()).throw(SimulatedCrash()))
    with pytest.raises(SimulatedCrash):
        external.dispatch(executor)
    monkeypatch.setattr(external.store, "bind_task", original)
    external.recover()

    def unavailable(*args, **kwargs):
        raise ExternalAgentError("bridge_unavailable", uncertain=True, cause=OSError("private detail"))

    external.adapter.lookup = unavailable
    with pytest.raises(WorkflowDispatchError) as error:
        external.dispatch()
    assert error.value.__cause__ is not None
    assert len(external.transport.sends) == 1 and held(external) == "held"
    assert external.repository.get(OWNER, "workflow-a").execution_state == "unknown"
    assert b"private detail" not in b"".join(event.payload for event in external.repository.events(OWNER, "workflow-a").events)


def test_external_gate_off_defers_send_but_preserves_existing_facts(external: ExternalSetup) -> None:
    with pytest.raises(WorkflowDispatchDeferred):
        external.dispatch(external.executor(enabled=False))
    assert external.transport.sends == []
    observed = external.dispatch()
    with external.repository.transaction() as connection:
        connection.execute("UPDATE workspaces SET enabled=0 WHERE owner_id=?", (OWNER,))
    assert external.dispatch(external.executor(enabled=False)).binding == observed.binding
    assert len(external.transport.sends) == 1


def test_agent_reported_terminal_does_not_create_validation_output_or_acceptance(external: ExternalSetup) -> None:
    original = external.dispatch()
    external.apply(original)
    journal = external.adapter.bridge.lookup(original.binding.token, external.resolved.snapshot.decode(), scope=WORKSPACE)
    external.transport.messages.append(message("done", "assistant", "Done\n" + journal["completion_marker"]))
    observed = external.dispatch()
    workflow = external.apply(observed)
    assert observed.state == "succeeded" and observed.evidence_source == "agent_reported"
    assert workflow.acceptance_state == "pending" and workflow.validation_state == "not_requested"
    with external.repository.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM workflow_evidence").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM workflow_artifact_refs").fetchone()[0] == 0
        payload = json.loads(connection.execute("SELECT observation_snapshot FROM workflow_dispatches").fetchone()[0])
        assert payload["evidence_source"] == "agent_reported" and "text" not in payload
    assert held(external) == "released"


def test_external_input_wait_does_not_fabricate_fleet_approval(external: ExternalSetup) -> None:
    original = external.dispatch()
    journal = external.adapter.bridge.lookup(original.binding.token, external.resolved.snapshot.decode(), scope=WORKSPACE)
    external.transport.messages.append(message("input", "assistant", "Need input\n" + journal["input_marker"]))
    observed = external.dispatch()
    assert observed.state == "waiting_external" and observed.reason_code == "external_input_required"
    assert external.apply(observed).acceptance_state == "pending"


def test_muse_unsupported_cancel_cannot_claim_stopped(external: ExternalSetup) -> None:
    observed = external.dispatch()
    cancelled = asyncio.run(external.executor().request_cancel(observed.binding))
    assert cancelled.state == "waiting_external"
    assert not external.executor().capabilities.cancel
    assert len(external.transport.sends) == 1 and held(external) == "held"


@pytest.mark.parametrize("guard", ["workspace", "cancel", "pause", "frozen", "lease", "deadline", "reservation"])
def test_external_preflight_rechecks_original_admission(external: ExternalSetup, guard: str) -> None:
    statements = {
        "workspace": "UPDATE workspaces SET enabled=0",
        "cancel": "UPDATE workflow_runs SET cancel_requested=1",
        "pause": "UPDATE workflow_runs SET pause_requested=1",
        "frozen": "UPDATE workflow_schema_meta SET admission_mode='frozen'",
        "lease": "UPDATE workflow_runs SET lease_expires_at=103",
        "deadline": "UPDATE workflow_steps SET active_deadline=103",
        "reservation": "UPDATE capability_reservations SET state='released'",
    }
    with external.repository.transaction() as connection:
        connection.execute(statements[guard])
    with pytest.raises(WorkflowDispatchError):
        external.dispatch()
    assert external.transport.sends == []


def test_owned_external_send_is_joined_before_cancellation_returns(external: ExternalSetup) -> None:
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = external.adapter.submit

    def blocked(*args, **kwargs):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test release missing")
        result = original(*args, **kwargs)
        finished.set()
        return result

    external.adapter.submit = blocked

    async def scenario() -> None:
        service = WorkflowDispatchService(external.repository, external.executor(), clock=external.clock)
        task = asyncio.create_task(service.dispatch(external.claim, step_key="develop", generation=1,
            expected_revision=external.repository.get(OWNER, "workflow-a").revision))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()

    asyncio.run(scenario())
    external.recover()
    assert external.dispatch().state == "waiting_external"
    assert len(external.transport.sends) == 1


@pytest.mark.parametrize("idempotent,lookup", [(False, False), (True, False), (False, True)])
def test_uncertain_external_receiver_without_replay_proof_is_never_resubmitted(tmp_path: Path, monkeypatch, idempotent: bool, lookup: bool) -> None:
    capabilities = replace(MuseExternalAgentAdapter.capabilities, submit_idempotent=idempotent, lookup_by_token=lookup)
    setup = make_external(tmp_path, capabilities=capabilities)
    original = setup.store.bind_task

    def crash(*args, **kwargs):
        raise SimulatedCrash()

    monkeypatch.setattr(setup.store, "bind_task", crash)
    with pytest.raises(SimulatedCrash):
        setup.dispatch()
    monkeypatch.setattr(setup.store, "bind_task", original)
    setup.adapter.lookup = lambda *args: None
    setup.recover()
    with pytest.raises(WorkflowDispatchError):
        setup.dispatch()
    assert len(setup.transport.sends) == 1 and held(setup) == "held"


def test_crash_before_receiver_send_can_only_resume_same_idempotent_token(external: ExternalSetup, monkeypatch) -> None:
    original = external.adapter.submit

    def crash(*args, **kwargs):
        raise SimulatedCrash()

    monkeypatch.setattr(external.adapter, "submit", crash)
    with pytest.raises(SimulatedCrash):
        external.dispatch()
    assert not external.transport.sends
    monkeypatch.setattr(external.adapter, "submit", original)
    external.recover()
    assert external.dispatch().state == "waiting_external"
    assert len(external.transport.sends) == 1


def test_external_lookup_cannot_send_after_workspace_revocation_during_query(external: ExternalSetup, monkeypatch) -> None:
    original = external.adapter.submit
    def crash(*args, **kwargs):
        raise SimulatedCrash()

    monkeypatch.setattr(external.adapter, "submit", crash)
    with pytest.raises(SimulatedCrash):
        external.dispatch()
    monkeypatch.setattr(external.adapter, "submit", original)
    external.recover()
    queries = 0

    def lookup(*args):
        nonlocal queries
        queries += 1
        if queries == 2:
            with external.repository.transaction() as connection:
                connection.execute("UPDATE workspaces SET enabled=0 WHERE owner_id=?", (OWNER,))
        return None

    monkeypatch.setattr(external.adapter, "lookup", lookup)
    with pytest.raises(WorkflowDispatchError):
        external.dispatch()
    assert queries == 2
    assert not external.transport.sends and held(external) == "held"


@pytest.mark.parametrize("replacement", ["absent", "new_journal", "identity_missing"])
def test_lost_or_replaced_muse_journal_cannot_authorize_replay(external: ExternalSetup, monkeypatch, replacement: str) -> None:
    original = external.store.bind_task
    def crash(*args, **kwargs):
        raise SimulatedCrash()

    monkeypatch.setattr(external.store, "bind_task", crash)
    with pytest.raises(SimulatedCrash):
        external.dispatch()
    monkeypatch.setattr(external.store, "bind_task", original)
    external.adapter.bridge.path.rename(external.adapter.bridge.path.with_suffix(".original"))
    if replacement == "new_journal":
        external.adapter.bridge.initialize()
    elif replacement == "identity_missing":
        external.adapter.bridge.path.touch()
    external.recover()
    with pytest.raises(WorkflowDispatchError):
        external.dispatch()
    assert len(external.transport.sends) == 1 and held(external) == "held"


@pytest.mark.parametrize("confirmable,confirmed", [(False, False), (False, True), (True, False), (True, True)])
def test_cancel_requires_declared_confirmation_for_exact_original_task(tmp_path: Path, confirmable: bool, confirmed: bool) -> None:
    capabilities = replace(MuseExternalAgentAdapter.capabilities, cancel_supported=True, cancel_confirmable=confirmable)
    setup = make_external(tmp_path, capabilities=capabilities)
    binding = setup.dispatch().binding
    setup.adapter.request_cancel = lambda task: CancelReceipt("cancelled" if confirmed else "cancelling", True, confirmed, task.external_id)
    observed = asyncio.run(setup.executor().request_cancel(binding))
    expected = "cancelled" if confirmable and confirmed else ("unknown" if confirmed else "cancelling")
    assert observed.state == expected
    assert observed.evidence_source == ("confirmed" if expected == "cancelled" else ("unknown" if confirmed else "transport"))


@pytest.mark.parametrize("reported", [None, "foreign-session", "original-session"])
@pytest.mark.parametrize("external_id", [None, "original-task"])
def test_external_status_requires_exact_original_task_and_session(tmp_path: Path, reported: str | None, external_id: str | None) -> None:
    setup = make_external(tmp_path)
    setup.adapter.submit = lambda token, contract: ExternalTaskRef("muse", token, external_id, "original-session")
    setup.adapter.inspect = lambda task: ExternalAgentStatus("succeeded", "agent_reported", external_id, session_id=reported)
    if reported != "original-session":
        with pytest.raises(WorkflowDispatchError, match="dispatch_unknown") as error:
            setup.dispatch()
        assert isinstance(error.value.__cause__, WorkflowDispatchError)
        assert error.value.__cause__.code == "external_task_mismatch"
        assert held(setup) == "held"
        return
    observed = setup.dispatch()
    assert observed.state == "succeeded" and observed.evidence_source == "agent_reported"


@pytest.mark.parametrize("reported", [None, "foreign-session", "original-session"])
@pytest.mark.parametrize("external_id", [None, "original-task"])
def test_external_cancel_requires_exact_original_task_and_session(tmp_path: Path, reported: str | None, external_id: str | None) -> None:
    capabilities = replace(MuseExternalAgentAdapter.capabilities, cancel_supported=True, cancel_confirmable=True)
    setup = make_external(tmp_path, capabilities=capabilities)
    setup.adapter.submit = lambda token, contract: ExternalTaskRef("muse", token, external_id, "original-session")
    setup.adapter.inspect = lambda task: ExternalAgentStatus("waiting_external", "accepted", external_id, session_id="original-session")
    binding = setup.dispatch().binding
    setup.adapter.request_cancel = lambda task: CancelReceipt("cancelled", True, True, external_id, session_id=reported)
    if reported != "original-session":
        with pytest.raises(WorkflowDispatchError, match="external_task_mismatch"):
            asyncio.run(setup.executor().request_cancel(binding))
        assert held(setup) == "held"
        return
    observed = asyncio.run(setup.executor().request_cancel(binding))
    assert observed.state == "cancelled" and observed.evidence_source == "confirmed"


def test_foreign_cancel_receipt_and_unsubstantiated_success_fail_closed(tmp_path: Path) -> None:
    capabilities = replace(MuseExternalAgentAdapter.capabilities, cancel_supported=True, cancel_confirmable=True)
    setup = make_external(tmp_path, capabilities=capabilities)
    binding = setup.dispatch().binding
    setup.adapter.request_cancel = lambda task: CancelReceipt("cancelled", True, True, "foreign")
    with pytest.raises(WorkflowDispatchError, match="external_task_mismatch"):
        asyncio.run(setup.executor().request_cancel(binding))
    setup.adapter.inspect = lambda task: ExternalAgentStatus("succeeded", "accepted", task.external_id)
    observed = asyncio.run(setup.executor().inspect(binding))
    assert observed.state == "unknown" and held(setup) == "held"


def test_accepted_offline_task_waits_under_original_deadline(external: ExternalSetup) -> None:
    external.apply(external.dispatch())
    external.adapter.inspect = lambda task: ExternalAgentStatus("unknown", "unknown", task.external_id, error_code="observation_unavailable")
    observed = external.dispatch()
    assert observed.state == "waiting_external" and observed.reason_code == "external_observation_unavailable"
    external.apply(observed)
    assert held(external) == "held" and len(external.transport.sends) == 1


def test_agent_self_report_keeps_required_independent_validation_pending(tmp_path: Path) -> None:
    setup = make_external(tmp_path, contract_changes={"validator_refs": [{"id": "independent-review", "revision": 1}]})
    original = setup.dispatch()
    setup.apply(original)
    journal = setup.adapter.bridge.lookup(original.binding.token, setup.resolved.snapshot.decode(), scope=WORKSPACE)
    setup.transport.messages.append(message("done", "assistant", "Done\n" + journal["completion_marker"]))
    workflow = setup.apply(setup.dispatch())
    assert workflow.execution_state == "succeeded" and workflow.validation_state == "pending"
    assert workflow.acceptance_state == "pending"


def test_external_same_token_changed_payload_cannot_rebind(external: ExternalSetup) -> None:
    from hub.domain.workflow_contract import parse_resolved

    original = external.dispatch()
    raw = json.loads(external.resolved.snapshot)
    raw["contract"]["objective"] = "A different task"
    changed = parse_resolved(json.dumps(raw).encode())
    with pytest.raises(WorkflowDispatchError, match="dispatch_conflict"):
        asyncio.run(external.executor().create_or_lookup(original.binding.token, changed.digest, changed))
    assert len(external.transport.sends) == 1


@pytest.mark.parametrize("field,value", [("configuration_digest", None), ("configuration_digest", "A" * 64),
    ("adapter_ref", CatalogRef("muse", True)), ("adapter_ref", CatalogRef("muse", 9007199254740992)),
    ("capabilities", {}), ("agent_id", "not an identity")])
def test_external_registration_rejects_inexact_authority(external: ExternalSetup, field: str, value: Any) -> None:
    with pytest.raises(ValueError):
        replace(external.target, **{field: value})
