"""Bounded workflow coordination; lifecycle and in-flight work have one owner."""
from __future__ import annotations

import asyncio
import hashlib
import math
import threading
from dataclasses import dataclass
from typing import Callable, Protocol
from hub.platform.async_io import run_owned_io, wait_owned

from hub.domain.workflow_contract import parse_contract, parse_definition, parse_resolved
from hub.domain.workflow_contract_types import CatalogRef, PriorOutputReference, ResolvedContract
from hub.domain.workflow_errors import WorkflowPersistenceError, WorkflowValidationError
from hub.domain.workflow_control import WorkflowReconciliationRequest, WorkflowStopRequest
from hub.domain.workflow_execution import DispatchRecord, ExecutionObservation
from hub.domain.workflow_external import RegisteredExternalTarget
from hub.domain.workflow_identity import validate_reference
from hub.domain.workflow_json import decode_snapshot
from hub.domain.workflow_projection import TERMINAL_STATES, project_workflow
from hub.domain.workflow_records import CoordinatorClaim, DispatchIntent, WorkflowRecord, WorkflowSnapshot, WorkflowStepRecord
from hub.domain.capability_routing import RegisteredCapabilityRoute
from .workflow_dispatch import DispatchStore, StepExecutor, WorkflowDispatchDeferred, WorkflowDispatchService
from .workflow_budget import WorkflowBudgetError, WorkflowBudgetService


class WorkflowCoordinatorError(RuntimeError):
    def __init__(self, code: str, *, workflow_id: str | None = None) -> None:
        self.code = code
        self.workflow_id = workflow_id
        super().__init__(code)


def _number(value: float, *, minimum: float, maximum: float) -> float:
    if type(value) not in (int, float) or not minimum <= value <= maximum or not math.isfinite(value):
        raise WorkflowCoordinatorError("invalid_coordinator_configuration")
    return float(value)


@dataclass(frozen=True, slots=True)
class RegisteredWorkflowStep:
    """Trusted fixed registration, never constructed from an operator body."""

    definition_digest: str
    resolved: ResolvedContract
    target_key: str
    capacity: int
    reserve_tokens: int
    reserve_requests: int
    external_target: RegisteredExternalTarget | None = None

    def __post_init__(self) -> None:
        if type(self.resolved) is not ResolvedContract or parse_resolved(self.resolved.snapshot) != self.resolved:
            raise WorkflowValidationError("invalid_resolved_snapshot")
        validate_reference(self.target_key)
        if self.external_target is not None:
            if type(self.external_target) is not RegisteredExternalTarget or self.external_target.target_key != self.target_key:
                raise WorkflowValidationError("external_registration_conflict")
            self.external_target.validate_resolved(self.resolved)
        if type(self.definition_digest) is not str or len(self.definition_digest) != 64 or any(c not in "0123456789abcdef" for c in self.definition_digest):
            raise WorkflowValidationError("invalid_definition_digest")
        for value, minimum, maximum in ((self.capacity, 1, 1024), (self.reserve_tokens, 0, 9007199254740991), (self.reserve_requests, 0, 9007199254740991)):
            if type(value) is not int or not minimum <= value <= maximum:
                raise WorkflowValidationError("invalid_admission_configuration")


@dataclass(frozen=True, slots=True)
class RegisteredWorkflowPlan:
    """Trusted execution policy; concrete predecessor outputs are resolved later."""
    owner_id: str
    workspace_id: str
    definition_digest: str
    step_key: str
    adapter_ref: CatalogRef
    policy_ref: CatalogRef
    target_key: str
    capacity: int
    reserve_tokens: int
    reserve_requests: int
    external_target: RegisteredExternalTarget | None = None

    def __post_init__(self) -> None:
        for value in (self.owner_id, self.workspace_id, self.step_key, self.target_key):
            validate_reference(value)
        if self.external_target is not None and (type(self.external_target) is not RegisteredExternalTarget or (
                self.external_target.owner_id, self.external_target.workspace_id, self.external_target.target_key,
                self.external_target.adapter_ref, self.external_target.policy_ref) != (
                self.owner_id, self.workspace_id, self.target_key, self.adapter_ref, self.policy_ref)):
            raise WorkflowValidationError("external_registration_conflict")
        if type(self.definition_digest) is not str or len(self.definition_digest) != 64 or any(char not in "0123456789abcdef" for char in self.definition_digest):
            raise WorkflowValidationError("invalid_definition_digest")
        for reference in (self.adapter_ref, self.policy_ref):
            if type(reference) is not CatalogRef:
                raise WorkflowValidationError("invalid_catalog_reference")
            validate_reference(reference.id)
            if type(reference.revision) is not int or not 1 <= reference.revision <= 9007199254740991:
                raise WorkflowValidationError("invalid_catalog_reference")
        for value, minimum, maximum in ((self.capacity, 1, 1024), (self.reserve_tokens, 0, 9007199254740991), (self.reserve_requests, 0, 9007199254740991)):
            if type(value) is not int or not minimum <= value <= maximum:
                raise WorkflowValidationError("invalid_admission_configuration")


class WorkflowInputResolver(Protocol):
    async def resolve(self, snapshot: WorkflowSnapshot, step_key: str, adapter_ref: CatalogRef) -> ResolvedContract: ...


class WorkflowArtifactInputs(Protocol):
    def prepare(self, claim: CoordinatorClaim, *, expected_revision: int, resolved: ResolvedContract,
                generation: int, target_key: str) -> bool | None: ...


class WorkflowCapabilityAdmission(Protocol):
    def advance(self, claim: CoordinatorClaim, *, expected_revision: int, generation: int,
                resolved: ResolvedContract, registration: RegisteredCapabilityRoute, now: float) -> None: ...


def _registration_key(item: RegisteredWorkflowStep | RegisteredWorkflowPlan | RegisteredCapabilityRoute) -> tuple[str, str, str, str]:
    if type(item) is RegisteredWorkflowStep:
        return item.resolved.owner_id, item.resolved.workspace_id, item.definition_digest, item.resolved.step_key
    if type(item) in (RegisteredWorkflowPlan, RegisteredCapabilityRoute):
        return item.owner_id, item.workspace_id, item.definition_digest, item.step_key
    raise WorkflowValidationError("invalid_step_registration")


class CoordinatorStore(DispatchStore, Protocol):
    """SQLite boundary, including atomic projection and resource ownership."""

    def claim_fair_due(self, *, worker_id: str, now: float, limit: int, lease_seconds: float) -> tuple[CoordinatorClaim, ...]: ...

    def snapshots(self, claims: tuple[CoordinatorClaim, ...], *, now: float) -> tuple[WorkflowSnapshot, ...]: ...

    def requires_node_inputs(self, resolved: ResolvedContract, *, kind: str, target_key: str) -> bool: ...

    def freeze_inputs(self, claim: CoordinatorClaim, *, step_key: str, generation: int, resolved: ResolvedContract,
                      expected_revision: int, now: float, input_target: str | None = None) -> WorkflowRecord: ...

    def admit_step(self, claim: CoordinatorClaim, *, step_key: str, generation: int, expected_revision: int, target_key: str, capacity: int, reserve_tokens: int, reserve_requests: int, now: float,
                   external_target: RegisteredExternalTarget | None = None) -> DispatchIntent: ...

    def refresh_projection(self, claim: CoordinatorClaim, *, expected_revision: int, now: float) -> WorkflowRecord: ...

    def defer_admission(self, claim: CoordinatorClaim, *, expected_revision: int, reason_code: str, now: float, step_key: str | None = None) -> WorkflowRecord: ...

    def get_dispatch(self, owner: str, workflow_id: str, step_key: str, generation: int) -> DispatchRecord: ...

    def apply_observation(self, claim: CoordinatorClaim, record: DispatchRecord, observation: ExecutionObservation, *, now: float,
                          reconciled: bool = False) -> WorkflowRecord: ...

    def ensure_stop(self, claim: CoordinatorClaim, *, step_key: str, generation: int, expected_revision: int, now: float) -> WorkflowStopRequest | None: ...

    def apply_stop_observation(self, claim: CoordinatorClaim, record: DispatchRecord, observation: ExecutionObservation | None, *, now: float, deadline: float) -> WorkflowRecord: ...

    def reconciliation_request(self, claim: CoordinatorClaim, *, now: float, expected_revision: int) -> WorkflowReconciliationRequest: ...

    def reconciliation_inconclusive(self, claim: CoordinatorClaim, record: DispatchRecord, *, now: float, reason_code: str) -> None: ...

    def release_claims(self, claims: tuple[CoordinatorClaim, ...], *, now: float, next_poll_at: float) -> tuple[bool, ...]: ...

    def expire_waiting(self, claim: CoordinatorClaim, *, expected_revision: int, now: float) -> bool: ...


@dataclass(frozen=True, slots=True)
class TickSummary:
    claimed: int
    examined: int
    action_budget_used: int
    deferred: int
    failures: tuple[WorkflowCoordinatorError, ...] = ()


class WorkflowCoordinator:
    def __init__(
        self, repository: CoordinatorStore, *, worker_id: str, clock: Callable[[], float],
        registrations: tuple[RegisteredWorkflowStep | RegisteredWorkflowPlan | RegisteredCapabilityRoute, ...] = (),
        executors: tuple[tuple[str, str, str, StepExecutor], ...] = (), dispatch_enabled: bool = False,
        workflow_limit: int = 32, action_limit: int = 64, lease_seconds: float = 60,
        interval_seconds: float = 1, dispatch_timeout_seconds: float = 30,
        budget_service: WorkflowBudgetService | None = None,
        input_resolver: WorkflowInputResolver | None = None,
        artifact_input_service: WorkflowArtifactInputs | None = None,
        capability_admission: WorkflowCapabilityAdmission | None = None,
        external_agents_enabled: bool = False,
    ) -> None:
        validate_reference(worker_id)
        if type(dispatch_enabled) is not bool or type(external_agents_enabled) is not bool or type(workflow_limit) is not int or not 1 <= workflow_limit <= 32 or type(action_limit) is not int or not 1 <= action_limit <= 64:
            raise WorkflowCoordinatorError("invalid_coordinator_configuration")
        if budget_service is not None and action_limit < 2:
            raise WorkflowCoordinatorError("invalid_coordinator_configuration")
        self.repository = repository
        self.worker_id = worker_id
        self.clock = clock
        self.dispatch_enabled = dispatch_enabled
        self.external_agents_enabled = external_agents_enabled
        self.budget_service = budget_service
        self.input_resolver = input_resolver
        self.artifact_input_service = artifact_input_service
        self.capability_admission = capability_admission
        self.workflow_limit = workflow_limit
        self.action_limit = action_limit
        self.lease_seconds = _number(lease_seconds, minimum=1, maximum=3600)
        self.interval_seconds = _number(interval_seconds, minimum=0.1, maximum=60)
        self.registrations = {_registration_key(item): item for item in registrations}
        self.dispatchers = {(owner, workspace, target): WorkflowDispatchService(repository, executor, clock=clock, timeout_seconds=dispatch_timeout_seconds)
                            for owner, workspace, target, executor in executors}
        if len(self.registrations) != len(registrations) or len(self.dispatchers) != len(executors):
            raise WorkflowCoordinatorError("duplicate_workflow_registration")

    async def tick(self, now: float) -> TickSummary:
        now = _number(now, minimum=0, maximum=253402300799)
        claims: tuple[CoordinatorClaim, ...] = ()
        try:
            budget_actions = 0
            if self.budget_service is not None and self.action_limit >= 2:
                budget = await self.budget_service.tick(limit=min(8, self.action_limit // 2))
                budget_actions = budget.examined
            claims = await asyncio.to_thread(self.repository.claim_fair_due, worker_id=self.worker_id, now=now,
                                             limit=self.workflow_limit, lease_seconds=self.lease_seconds)
            snapshots = await asyncio.to_thread(self.repository.snapshots, claims, now=self.clock())
            by_id = {item.workflow.workflow_id: item for item in snapshots}
            existing = tuple(claim for claim in claims if self._active(by_id[claim.workflow_id]) is not None)
            new = tuple(claim for claim in claims if self._active(by_id[claim.workflow_id]) is None)
            actions = budget_actions
            pending: list[tuple[CoordinatorClaim, WorkflowSnapshot]] = []
            for claim in existing + new:
                snapshot = by_id[claim.workflow_id]
                cost = self._cost(snapshot, now)
                if actions + cost > self.action_limit:
                    continue
                actions += cost
                pending.append((claim, snapshot))
            failures: list[WorkflowCoordinatorError] = []
            for active_phase in (True, False):
                batch = tuple((claim, snapshot) for claim, snapshot in pending if (self._active(snapshot) is not None) == active_phase)
                outcomes = await asyncio.gather(*(self._advance(claim, snapshot) for claim, snapshot in batch), return_exceptions=True)
                for (claim, _), result in zip(batch, outcomes):
                    if isinstance(result, BaseException) and not isinstance(result, Exception):
                        raise result
                    if isinstance(result, Exception):
                        failures.append(self._error(result, claim.workflow_id))
            return TickSummary(len(claims), len(pending), actions, len(claims) - len(pending), tuple(failures))
        except (WorkflowPersistenceError, WorkflowValidationError, WorkflowBudgetError) as exc:
            raise self._error(exc) from exc
        finally:
            # asyncio cancellation cannot interrupt a SQLite transaction already in a thread.
            if claims:
                await asyncio.shield(self._release(claims))

    @staticmethod
    def _error(cause: BaseException, workflow_id: str | None = None) -> WorkflowCoordinatorError:
        error = WorkflowCoordinatorError(getattr(cause, "code", "workflow_coordination_failed"), workflow_id=workflow_id)
        error.__cause__ = cause
        return error

    @staticmethod
    def _active(snapshot: WorkflowSnapshot) -> WorkflowStepRecord | None:
        if snapshot.workflow.reason_code == "reconciliation_requested":
            return next((step for step in snapshot.steps if step.state == "unknown"), None)
        return next((step for step in snapshot.steps if step.dispatch_stage is not None and step.state not in TERMINAL_STATES), None)

    def _cost(self, snapshot: WorkflowSnapshot, now: float) -> int:
        if snapshot.workflow.reason_code == "reconciliation_requested":
            return 2
        active = self._active(snapshot)
        if active is not None:
            if snapshot.workflow.cancel_requested or active.state == "cancelling":
                return 3
            if any(deadline is not None and deadline <= now for deadline in (active.active_deadline, active.wait_deadline)):
                return 3
            return 3 if active.state == "dispatching" else 1
        return 2 if any(step.state in ("ready", "waiting_node") and step.resolved_snapshot is not None for step in snapshot.steps) else 1

    async def _advance(self, claim: CoordinatorClaim, snapshot: WorkflowSnapshot) -> None:
        definition = parse_definition(snapshot.workflow.definition_snapshot)
        if hashlib.sha256(definition.snapshot).hexdigest() != snapshot.workflow.definition_digest:
            raise WorkflowValidationError("invalid_definition_digest")
        if snapshot.workflow.reason_code == "reconciliation_requested":
            await self._reconcile(claim, snapshot)
            return
        if await asyncio.to_thread(self.repository.expire_waiting, claim, expected_revision=snapshot.workflow.revision, now=self.clock()):
            return
        active = self._active(snapshot)
        if active is not None:
            await self._inspect(claim, snapshot, active.step_key, active.generation, active.target_key,
                                external=active.kind == "external_agent")
            return
        projection = project_workflow(definition, snapshot)
        if projection.eligible_step is None or not self.dispatch_enabled or snapshot.admission_mode != "open":
            await asyncio.to_thread(self.repository.refresh_projection, claim, expected_revision=snapshot.workflow.revision, now=self.clock())
            return
        step = {item.step_key: item for item in snapshot.steps}[projection.eligible_step]
        registration = self.registrations.get((claim.owner_id, claim.workspace_id, snapshot.workflow.definition_digest, step.step_key))
        template = {item.step_key: item for item in definition.steps}[step.step_key]
        reason = None
        if registration is None:
            reason = "step_registration_missing"
        elif template.kind == "external_agent" and not self.external_agents_enabled:
            reason = "external_agents_disabled"
        elif template.kind == "external_agent" and (type(registration) is RegisteredCapabilityRoute or registration.external_target is None):
            reason = "external_registration_required"
        elif type(registration) is RegisteredWorkflowStep and any(type(item.reference) is PriorOutputReference for item in template.contract.input_refs):
            reason = "input_resolution_required"
        elif type(registration) is RegisteredCapabilityRoute and self.capability_admission is None:
            reason = "capability_unavailable"
        elif type(registration) is not RegisteredCapabilityRoute and (claim.owner_id, claim.workspace_id, registration.target_key) not in self.dispatchers:
            reason = "executor_unavailable"
        elif type(registration) is RegisteredWorkflowStep and step.contract_snapshot is not None and parse_contract(step.contract_snapshot).snapshot != registration.resolved.contract.snapshot:
            reason = "registration_conflict"
        elif type(registration) in (RegisteredWorkflowPlan, RegisteredCapabilityRoute) and parse_contract(step.contract_snapshot).policy_ref != registration.policy_ref:
            reason = "registration_conflict"
        elif type(registration) in (RegisteredWorkflowPlan, RegisteredCapabilityRoute) and self.input_resolver is None:
            reason = "input_resolution_required"
        if reason is not None:
            await asyncio.to_thread(self.repository.defer_admission, claim, expected_revision=snapshot.workflow.revision, reason_code=reason, now=self.clock())
            return
        if type(registration) is RegisteredWorkflowStep:
            resolved = registration.resolved
        elif step.resolved_snapshot is not None:
            resolved = parse_resolved(step.resolved_snapshot)
            adapter = decode_snapshot(resolved.snapshot)["adapter_ref"]
            if adapter != {"id": registration.adapter_ref.id, "revision": registration.adapter_ref.revision}:
                raise WorkflowValidationError("registration_conflict")
        else:
            try:
                resolved = await self.input_resolver.resolve(snapshot, step.step_key, registration.adapter_ref)
            except WorkflowPersistenceError as exc:
                if exc.code not in ("output_unavailable", "reference_forbidden", "context_resolution_required", "source_unavailable"):
                    raise
                await asyncio.to_thread(self.repository.defer_admission, claim, expected_revision=snapshot.workflow.revision,
                                       reason_code=exc.code, now=self.clock())
                return
        if type(registration) is RegisteredCapabilityRoute:
            await self._admit_capability(claim, snapshot, step, resolved, registration)
            return
        needs_node_inputs = template.kind != "external_agent" and (step.target_key is not None or
            self.repository.requires_node_inputs(resolved, kind=template.kind, target_key=registration.target_key))
        node_inputs = self.artifact_input_service if needs_node_inputs else None
        if step.resolved_snapshot is None:
            if node_inputs is not None:
                try:
                    prepared = await asyncio.to_thread(node_inputs.prepare, claim,
                        expected_revision=snapshot.workflow.revision, resolved=resolved,
                        generation=step.generation, target_key=registration.target_key)
                except WorkflowPersistenceError as exc:
                    if exc.code not in {"input_alias_unregistered", "input_receipt_required"}:
                        raise
                    await asyncio.to_thread(self.repository.defer_admission, claim,
                        expected_revision=snapshot.workflow.revision, reason_code=exc.code,
                        step_key=step.step_key, now=self.clock())
                    return
                if prepared is False:
                    return
            try:
                await asyncio.to_thread(self.repository.freeze_inputs, claim, step_key=step.step_key, generation=step.generation,
                    resolved=resolved, expected_revision=snapshot.workflow.revision, now=self.clock(),
                    input_target=registration.target_key if node_inputs is not None else None)
            except WorkflowPersistenceError as exc:
                if exc.code != "input_receipt_required":
                    raise
                await asyncio.to_thread(self.repository.defer_admission, claim, expected_revision=snapshot.workflow.revision,
                    reason_code=exc.code, now=self.clock())
            return
        if (step.resolved_digest, step.resolved_snapshot) != (resolved.digest, resolved.snapshot):
            await asyncio.to_thread(self.repository.defer_admission, claim, expected_revision=snapshot.workflow.revision,
                                   reason_code="registration_conflict", now=self.clock())
            return
        if node_inputs is not None:
            try:
                prepared = await asyncio.to_thread(node_inputs.prepare, claim,
                    expected_revision=snapshot.workflow.revision, resolved=resolved,
                    generation=step.generation, target_key=registration.target_key)
            except WorkflowPersistenceError as exc:
                if exc.code not in {"input_alias_unregistered", "input_receipt_required"}:
                    raise
                await asyncio.to_thread(self.repository.defer_admission, claim,
                    expected_revision=snapshot.workflow.revision, reason_code=exc.code,
                    step_key=step.step_key, now=self.clock())
                return
            if prepared is False:
                return
        try:
            await asyncio.to_thread(self.repository.admit_step, claim, step_key=step.step_key, generation=step.generation,
                                   expected_revision=snapshot.workflow.revision, target_key=registration.target_key, capacity=registration.capacity,
                                   reserve_tokens=registration.reserve_tokens, reserve_requests=registration.reserve_requests, now=self.clock(),
                                   external_target=registration.external_target)
        except WorkflowPersistenceError as exc:
            if exc.code not in ("capacity_exhausted", "budget_exhausted"):
                raise
            await asyncio.to_thread(self.repository.defer_admission, claim, expected_revision=snapshot.workflow.revision,
                                   reason_code=exc.code, step_key=step.step_key if exc.code == "capacity_exhausted" else None, now=self.clock())

    async def _admit_capability(self, claim: CoordinatorClaim, snapshot: WorkflowSnapshot, step: WorkflowStepRecord,
                                resolved: ResolvedContract, registration: RegisteredCapabilityRoute) -> None:
        try:
            await asyncio.to_thread(self.capability_admission.advance, claim, expected_revision=snapshot.workflow.revision,
                generation=step.generation, resolved=resolved, registration=registration, now=self.clock())
        except WorkflowPersistenceError as exc:
            if exc.code not in {"capability_unavailable", "executor_unavailable", "input_alias_unregistered",
                    "input_receipt_required", "registration_conflict", "capacity_exhausted", "budget_exhausted",
                    "unsupported_capability_target"}:
                raise
            await asyncio.to_thread(self.repository.defer_admission, claim, expected_revision=snapshot.workflow.revision,
                reason_code=exc.code, step_key=step.step_key if exc.code in {"capability_unavailable", "capacity_exhausted",
                    "input_alias_unregistered", "input_receipt_required"} else None, now=self.clock())

    async def _inspect(self, claim: CoordinatorClaim, snapshot: WorkflowSnapshot, step_key: str, generation: int, target: str | None,
                       *, external: bool = False) -> None:
        stop = await asyncio.to_thread(self.repository.ensure_stop, claim, step_key=step_key, generation=generation,
                                       expected_revision=snapshot.workflow.revision, now=self.clock())
        dispatcher = self.dispatchers.get((claim.owner_id, claim.workspace_id, target))
        if stop is not None:
            await self._stop(claim, stop, dispatcher)
            return
        if dispatcher is None:
            await asyncio.to_thread(self.repository.defer_admission, claim, expected_revision=snapshot.workflow.revision,
                                   reason_code="executor_unavailable", now=self.clock())
            return
        record = await asyncio.to_thread(self.repository.get_dispatch, claim.owner_id, claim.workflow_id, step_key, generation)
        allow_create = self.dispatch_enabled and (not external or self.external_agents_enabled) and snapshot.admission_mode == "open" and not snapshot.workflow.cancel_requested and not snapshot.workflow.pause_requested
        if record.stage == "intent" and not allow_create:
            await asyncio.to_thread(self.repository.refresh_projection, claim, expected_revision=record.revision, now=self.clock())
            return
        try:
            observation = await dispatcher.dispatch(claim, step_key=step_key, generation=generation,
                                                    expected_revision=record.revision, allow_create=allow_create)
        except WorkflowDispatchDeferred:
            return
        current = await asyncio.to_thread(self.repository.get_dispatch, claim.owner_id, claim.workflow_id, step_key, generation)
        await asyncio.to_thread(self.repository.apply_observation, claim, current, observation, now=self.clock())

    async def _stop(self, claim: CoordinatorClaim, stop: WorkflowStopRequest, dispatcher: WorkflowDispatchService | None) -> None:
        record = await asyncio.to_thread(self.repository.get_dispatch, claim.owner_id, claim.workflow_id, stop.step_key, stop.generation)
        observation = None
        if dispatcher is not None:
            async def inspect_stop() -> ExecutionObservation | None:
                binding = record.binding
                if binding is None and dispatcher.executor.capabilities.lookup:
                    binding = await dispatcher.executor.lookup(record.token, record.payload_hash, record.resolved)
                if binding is None:
                    return None
                dispatcher._validate_binding(record, binding)
                if record.binding is None:
                    await run_owned_io(self.repository.bind_dispatch, claim, record, binding, now=self.clock())
                if dispatcher.executor.capabilities.cancel:
                    return await dispatcher.executor.request_cancel(binding)
                return await dispatcher.executor.inspect(binding)

            try:
                observation = await wait_owned(inspect_stop(), timeout=dispatcher.timeout_seconds)
            except Exception as exc:
                current = await asyncio.to_thread(self.repository.get_dispatch, claim.owner_id, claim.workflow_id, stop.step_key, stop.generation)
                await asyncio.to_thread(self.repository.apply_stop_observation, claim, current, None, now=self.clock(), deadline=stop.deadline)
                raise self._error(exc, claim.workflow_id) from exc
        current = await asyncio.to_thread(self.repository.get_dispatch, claim.owner_id, claim.workflow_id, stop.step_key, stop.generation)
        await asyncio.to_thread(self.repository.apply_stop_observation, claim, current, observation, now=self.clock(), deadline=stop.deadline)

    async def _reconcile(self, claim: CoordinatorClaim, snapshot: WorkflowSnapshot) -> None:
        request = await asyncio.to_thread(self.repository.reconciliation_request, claim, now=self.clock(), expected_revision=snapshot.workflow.revision)
        record = await asyncio.to_thread(self.repository.get_dispatch, claim.owner_id, claim.workflow_id, request.step_key, request.generation)
        dispatcher = self.dispatchers.get((claim.owner_id, claim.workspace_id, record.target_key))
        observation = None
        if dispatcher is not None:
            async def inspect_existing() -> ExecutionObservation | None:
                binding = record.binding
                if binding is None and dispatcher.executor.capabilities.lookup:
                    binding = await dispatcher.executor.lookup(record.token, record.payload_hash, record.resolved)
                if binding is None:
                    return None
                dispatcher._validate_binding(record, binding)
                if request.mode == "cancel" and dispatcher.executor.capabilities.cancel:
                    return await dispatcher.executor.request_cancel(binding)
                return await dispatcher.executor.inspect(binding)

            try:
                observation = await wait_owned(inspect_existing(), timeout=dispatcher.timeout_seconds)
            except Exception as exc:
                await asyncio.to_thread(self.repository.reconciliation_inconclusive, claim, record, now=self.clock(), reason_code="inspection_failed")
                raise self._error(exc, claim.workflow_id) from exc
        if observation is None or observation.state not in ("succeeded", "failed", "cancelled"):
            if request.mode == "cancel" and request.deadline is not None and self.clock() < request.deadline:
                return
            await asyncio.to_thread(self.repository.reconciliation_inconclusive, claim, record, now=self.clock(), reason_code="inspection_inconclusive")
            return
        await asyncio.to_thread(self.repository.apply_observation, claim, record, observation, now=self.clock(), reconciled=True)

    async def _release(self, claims: tuple[CoordinatorClaim, ...]) -> None:
        try:
            now = self.clock()
            await run_owned_io(self.repository.release_claims, claims, now=now, next_poll_at=now + self.interval_seconds)
        except Exception as exc:
            raise self._error(exc) from exc

    def start(self, *, on_error: Callable[[WorkflowCoordinatorError], None]) -> CoordinatorStopHandle:
        handle = CoordinatorStopHandle(self, on_error)
        handle.thread.start()
        return handle


class CoordinatorStopHandle:
    """Owns lifecycle state; stop is idempotent, join reports unfinished cleanup."""

    def __init__(self, coordinator: WorkflowCoordinator, on_error: Callable[[WorkflowCoordinatorError], None]) -> None:
        self.coordinator = coordinator
        self.on_error = on_error
        self.stopped = threading.Event()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._failure: BaseException | None = None
        self.thread = threading.Thread(target=self._run, name="workflow-coordinator", daemon=True)

    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except BaseException as exc:
            self._failure = exc

    async def _serve(self) -> None:
        wake = asyncio.Event()
        with self._lock:
            self._loop, self._wake = asyncio.get_running_loop(), wake
        try:
            while not self.stopped.is_set():
                await self._tick_or_stop(wake)
                try:
                    await asyncio.wait_for(wake.wait(), timeout=self.coordinator.interval_seconds)
                except asyncio.TimeoutError:
                    continue
        finally:
            with self._lock:
                self._loop, self._wake = None, None

    async def _tick_or_stop(self, wake: asyncio.Event) -> None:
        tick = asyncio.create_task(self.coordinator.tick(self.coordinator.clock()))
        stopping = asyncio.create_task(wake.wait())
        try:
            done, _ = await asyncio.wait((tick, stopping), return_when=asyncio.FIRST_COMPLETED)
            if stopping in done:
                tick.cancel()
            try:
                summary = await tick
                for failure in summary.failures:
                    self.on_error(failure)
            except asyncio.CancelledError:
                if not self.stopped.is_set():
                    raise
            except WorkflowCoordinatorError as exc:
                self.on_error(exc)
        finally:
            stopping.cancel()
            await asyncio.gather(stopping, return_exceptions=True)

    def stop(self) -> None:
        self.stopped.set()
        with self._lock:
            if self._loop is not None and self._wake is not None:
                self._loop.call_soon_threadsafe(self._wake.set)

    def join(self, timeout: float = 30) -> None:
        timeout = _number(timeout, minimum=0, maximum=60)
        self.thread.join(timeout=timeout)
        if self.thread.is_alive():
            raise WorkflowCoordinatorError("workflow_worker_stop_timeout")
        if self._failure is not None:
            raise WorkflowCoordinatorError("workflow_worker_failed") from self._failure

    def __call__(self) -> None:
        self.stop()
        self.join()
