"""One external StepExecutor; original Workflow owns admission and recovery."""
from __future__ import annotations

from typing import Callable, Protocol

from hub.domain.external_agent import CancelReceipt, ExternalAgentAdapter, ExternalAgentStatus, ExternalTaskRef
from hub.domain.external_request import ExternalAgentRequest, build_external_request
from hub.domain.workflow_contract_types import ResolvedContract
from hub.domain.workflow_errors import WorkflowPersistenceError, WorkflowValidationError
from hub.domain.workflow_execution import ExecutionBinding, ExecutionObservation, ExecutorCapabilities
from hub.domain.workflow_external import ExternalDispatchPin, RegisteredExternalTarget, task_snapshot
from hub.domain.workflow_json import decode_snapshot
from hub.platform.async_io import run_owned_io
from .workflow_dispatch import WorkflowDispatchDeferred, WorkflowDispatchError
from .workflow_external_inputs import WorkflowExternalInputs
from .workflow_external_outputs import WorkflowExternalOutputs, WorkflowExternalOutputsError


class ExternalExecutionStore(Protocol):
    def lookup(self, token: str, payload_hash: str, resolved: ResolvedContract, target: RegisteredExternalTarget) -> ExternalDispatchPin: ...

    def read_binding(self, binding: ExecutionBinding, target: RegisteredExternalTarget) -> ExternalDispatchPin: ...

    def prepare_send(self, token: str, payload_hash: str, resolved: ResolvedContract,
                     target: RegisteredExternalTarget, *, now: float, request: ExternalAgentRequest | None = None) -> bool: ...

    def bind_task(self, token: str, payload_hash: str, resolved: ResolvedContract,
                  target: RegisteredExternalTarget, task: ExternalTaskRef, *, now: float) -> ExternalTaskRef: ...

    def authorize_outputs(self, binding: ExecutionBinding, target: RegisteredExternalTarget, *, now: float) -> None: ...


class ExternalStepExecutor:
    def __init__(self, repository: ExternalExecutionStore, target: RegisteredExternalTarget,
                 adapter: ExternalAgentAdapter, *, clock: Callable[[], float], enabled: bool = False,
                 inputs: WorkflowExternalInputs | None = None, outputs: WorkflowExternalOutputs | None = None) -> None:
        if type(target) is not RegisteredExternalTarget or type(enabled) is not bool:
            raise WorkflowValidationError("invalid_external_executor")
        self.repository = repository
        self.target = target
        self.adapter = adapter
        self.clock = clock
        self.enabled = enabled
        self.inputs = inputs
        self.outputs = outputs
        capabilities = target.capabilities
        self.capabilities = ExecutorCapabilities(capabilities.submit_idempotent, capabilities.lookup_by_token, capabilities.cancel_supported)

    async def create_or_lookup(self, token: str, payload_hash: str, resolved: ResolvedContract) -> ExecutionBinding:
        return await run_owned_io(self._create, token, payload_hash, resolved)

    async def lookup(self, token: str, payload_hash: str, resolved: ResolvedContract) -> ExecutionBinding | None:
        return await run_owned_io(self._lookup, token, payload_hash, resolved)

    async def inspect(self, binding: ExecutionBinding) -> ExecutionObservation:
        return await run_owned_io(self._inspect, binding)

    async def request_cancel(self, binding: ExecutionBinding) -> ExecutionObservation:
        return await run_owned_io(self._cancel, binding)

    def _binding(self, token: str, payload_hash: str) -> ExecutionBinding:
        return ExecutionBinding(self.target.owner_id, self.target.workspace_id, self.target.target_key,
                                token, payload_hash, "external", "wf-external-" + token)

    def _guard_adapter(self) -> None:
        try:
            matches = (self.adapter.agent_id, self.adapter.capabilities, self.adapter.configuration_digest) == (
                self.target.agent_id, self.target.capabilities, self.target.configuration_digest,
            )
        except Exception as exc:
            raise WorkflowDispatchError("external_configuration_unavailable") from exc
        if not matches:
            raise WorkflowDispatchError("external_configuration_changed", uncertain=False)

    def _pin(self, token: str, payload_hash: str, resolved: ResolvedContract) -> ExternalDispatchPin:
        try:
            self.target.validate_resolved(resolved)
            self._binding(token, payload_hash)
            pin = self.repository.lookup(token, payload_hash, resolved, self.target)
        except (WorkflowPersistenceError, WorkflowValidationError) as exc:
            raise WorkflowDispatchError(exc.code, uncertain=False) from exc
        if pin.observation is None:
            self._guard_adapter()
        return pin

    def _request(self, pin: ExternalDispatchPin) -> ExternalAgentRequest:
        if pin.request is not None:
            return pin.request
        if not pin.resolved.contract.input_refs and not pin.resolved.contract.context_refs:
            return build_external_request(pin.resolved)
        if self.inputs is None:
            raise WorkflowDispatchError("external_input_unavailable", uncertain=False)
        return self.inputs.collect(pin.resolved)

    def _bind(self, token: str, payload_hash: str, resolved: ResolvedContract, task: ExternalTaskRef) -> ExecutionBinding:
        try:
            task_snapshot(task, self.target, token)
            self.repository.bind_task(token, payload_hash, resolved, self.target, task, now=self.clock())
        except (WorkflowPersistenceError, WorkflowValidationError) as exc:
            raise WorkflowDispatchError("external_task_binding_failed") from exc
        return self._binding(token, payload_hash)

    def _lookup_task(self, token: str, payload_hash: str, resolved: ResolvedContract,
                     request: ExternalAgentRequest) -> ExecutionBinding | None:
        if not self.target.capabilities.lookup_by_token:
            raise WorkflowDispatchError("executor_lookup_unsupported")
        self._guard_adapter()
        try:
            task = self.adapter.lookup(token, {"prompt": request.prompt})
        except Exception as exc:
            raise WorkflowDispatchError("external_lookup_unavailable") from exc
        if task is None:
            return None
        return self._bind(token, payload_hash, resolved, task)

    def _lookup(self, token: str, payload_hash: str, resolved: ResolvedContract) -> ExecutionBinding | None:
        pin = self._pin(token, payload_hash, resolved)
        if pin.task is not None:
            return self._binding(token, payload_hash)
        if pin.send_started_at is None:
            return None
        return self._lookup_task(token, payload_hash, resolved, self._request(pin))

    def _create(self, token: str, payload_hash: str, resolved: ResolvedContract) -> ExecutionBinding:
        pin = self._pin(token, payload_hash, resolved)
        if pin.task is not None:
            return self._binding(token, payload_hash)
        if not self.enabled:
            raise WorkflowDispatchDeferred("external_agents_disabled", uncertain=False)
        request = self._request(pin)
        try:
            first = self.repository.prepare_send(token, payload_hash, resolved, self.target, now=self.clock(), request=request)
        except WorkflowPersistenceError as exc:
            if exc.code == "admission_closed":
                raise WorkflowDispatchDeferred(exc.code, uncertain=False) from exc
            raise WorkflowDispatchError(exc.code, uncertain=pin.send_started_at is not None) from exc
        if not first:
            original = self._lookup_task(token, payload_hash, resolved, request)
            if original is not None:
                return original
            if not self.target.capabilities.submit_idempotent:
                raise WorkflowDispatchError("executor_not_idempotent")
        # Recheck after short store I/O; no transaction spans the adapter call.
        self._guard_adapter()
        if not self.enabled:
            raise WorkflowDispatchDeferred("external_agents_disabled", uncertain=False)
        if resolved.contract.input_refs or resolved.contract.context_refs:
            if self.inputs is None:
                raise WorkflowDispatchError("external_input_unavailable", uncertain=True)
            try:
                self.inputs.authorize(resolved, request)
            except WorkflowDispatchError as exc:
                raise WorkflowDispatchError(exc.code, uncertain=True) from exc
        try:
            self.repository.prepare_send(token, payload_hash, resolved, self.target, now=self.clock(), request=request)
        except WorkflowPersistenceError as exc:
            if exc.code == "admission_closed":
                raise WorkflowDispatchDeferred(exc.code, uncertain=False) from exc
            raise WorkflowDispatchError(exc.code) from exc
        self._guard_adapter()
        try:
            task = self.adapter.submit(token, {"prompt": request.prompt})
        except Exception as exc:
            raise WorkflowDispatchError("external_submit_unknown") from exc
        return self._bind(token, payload_hash, resolved, task)

    def _read_binding(self, binding: ExecutionBinding) -> ExternalDispatchPin:
        if type(binding) is not ExecutionBinding or binding != self._binding(binding.token, binding.payload_hash):
            raise WorkflowDispatchError("dispatch_conflict", uncertain=False)
        try:
            pin = self.repository.read_binding(binding, self.target)
        except WorkflowPersistenceError as exc:
            raise WorkflowDispatchError(exc.code) from exc
        if pin.task is None:
            raise WorkflowDispatchError("external_task_missing")
        if pin.observation is None:
            self._guard_adapter()
        return pin

    @staticmethod
    def _unknown(binding: ExecutionBinding, *, reason: str = "external_status_unconfirmed") -> ExecutionObservation:
        return ExecutionObservation(binding, "unknown", reason_code=reason, evidence_source="unknown")

    def _observe(self, binding: ExecutionBinding, pin: ExternalDispatchPin, status: ExternalAgentStatus) -> ExecutionObservation:
        if type(status) is not ExternalAgentStatus or (status.external_id, status.session_id) != (pin.task.external_id, pin.task.session_id):
            raise WorkflowDispatchError("external_task_mismatch")
        if status.state == "unknown" and pin.accepted:
            return ExecutionObservation(binding, "waiting_external", reason_code="external_observation_unavailable", evidence_source="accepted")
        if status.state in {"succeeded", "failed"} and status.evidence in {"agent_reported", "confirmed"}:
            proof = None
            if status.state == "succeeded" and pin.resolved.contract.output_specs:
                if self.outputs is None:
                    raise WorkflowDispatchError("external_output_unavailable")
                try:
                    self.repository.authorize_outputs(binding, self.target, now=self.clock())
                    self._guard_adapter()
                    specs = tuple(decode_snapshot(pin.resolved.contract.snapshot)["output_specs"])
                    exported = self.adapter.export_outputs(pin.task, specs)
                    self._guard_adapter()
                    proof = self.outputs.verify(pin.resolved, pin.task, exported)
                    self._guard_adapter()
                except Exception as exc:
                    code = exc.code if isinstance(exc, (WorkflowPersistenceError, WorkflowValidationError, WorkflowExternalOutputsError)) else "external_output_unavailable"
                    raise WorkflowDispatchError(code) from exc
            return ExecutionObservation(binding, status.state, reason_code="external_" + status.evidence,
                                        evidence_source=status.evidence, external_outputs=proof)
        if status.state == "cancelled" and status.evidence == "confirmed" and self.target.capabilities.cancel_confirmable:
            return ExecutionObservation(binding, "cancelled", reason_code="external_stop_confirmed", evidence_source="confirmed")
        if status.state in {"waiting_external", "running", "waiting_input"} and status.evidence in {"accepted", "confirmed"}:
            reason = "external_input_required" if status.state == "waiting_input" else None
            return ExecutionObservation(binding, "waiting_external", reason_code=reason, evidence_source=status.evidence)
        if status.state == "cancelling" and status.evidence in {"transport", "accepted", "confirmed"}:
            return ExecutionObservation(binding, "cancelling", reason_code="external_cancel_requested", evidence_source=status.evidence)
        return self._unknown(binding)

    def _inspect(self, binding: ExecutionBinding) -> ExecutionObservation:
        pin = self._read_binding(binding)
        if pin.observation is not None:
            return pin.observation
        if not self.target.capabilities.status_evidence:
            return self._unknown(binding, reason="external_status_unsupported")
        try:
            status = self.adapter.inspect(pin.task)
        except Exception as exc:
            raise WorkflowDispatchError("external_observation_unavailable") from exc
        return self._observe(binding, pin, status)

    def _cancel(self, binding: ExecutionBinding) -> ExecutionObservation:
        observed = self._inspect(binding)
        if observed.state in {"succeeded", "failed", "cancelled"} or not self.target.capabilities.cancel_supported:
            return observed
        pin = self._read_binding(binding)
        try:
            receipt = self.adapter.request_cancel(pin.task)
        except Exception as exc:
            raise WorkflowDispatchError("external_cancel_unknown") from exc
        if type(receipt) is not CancelReceipt or (receipt.external_id, receipt.session_id) != (pin.task.external_id, pin.task.session_id):
            raise WorkflowDispatchError("external_task_mismatch")
        if receipt.confirmed and self.target.capabilities.cancel_confirmable:
            return ExecutionObservation(binding, "cancelled", reason_code="external_stop_confirmed", evidence_source="confirmed")
        if receipt.requested and not receipt.confirmed:
            return ExecutionObservation(binding, "cancelling", reason_code="external_cancel_requested", evidence_source="transport")
        return self._unknown(binding, reason="external_stop_unconfirmed")
