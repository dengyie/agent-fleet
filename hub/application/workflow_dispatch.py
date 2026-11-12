"""One bounded dispatch/recovery operation; execution I/O stays outside SQLite."""
from __future__ import annotations

import asyncio
import math
from typing import Callable, Protocol
from hub.platform.async_io import run_owned_io, wait_owned

from hub.domain.workflow_contract_types import ResolvedContract
from hub.domain.workflow_errors import WorkflowPersistenceError
from hub.domain.workflow_execution import (
    DispatchRecord, ExecutionBinding, ExecutionObservation, ExecutorCapabilities,
)
from hub.domain.workflow_records import CoordinatorClaim


class WorkflowDispatchError(RuntimeError):
    """Stable application error; uncertain failures never authorize a replay."""

    def __init__(self, code: str, *, uncertain: bool = True) -> None:
        self.code = code
        self.uncertain = uncertain
        super().__init__(code)


class WorkflowDispatchDeferred(WorkflowDispatchError):
    """Admission closed before a new side effect; keep the durable intent."""


class StepExecutor(Protocol):
    """External execution port: cancellation does not imply execution stopped."""

    capabilities: ExecutorCapabilities

    async def create_or_lookup(self, token: str, payload_hash: str, resolved: ResolvedContract) -> ExecutionBinding: ...

    async def lookup(self, token: str, payload_hash: str, resolved: ResolvedContract) -> ExecutionBinding | None: ...

    async def inspect(self, binding: ExecutionBinding) -> ExecutionObservation: ...

    async def request_cancel(self, binding: ExecutionBinding) -> ExecutionObservation: ...


class DispatchStore(Protocol):
    """Short persistence operations; no executor may run inside these calls."""

    def begin_dispatch(self, claim: CoordinatorClaim, *, step_key: str, generation: int,
                       expected_revision: int, now: float) -> DispatchRecord: ...

    def authorize_dispatch(self, claim: CoordinatorClaim, record: DispatchRecord, *, now: float) -> None: ...

    def bind_dispatch(self, claim: CoordinatorClaim, record: DispatchRecord, binding: ExecutionBinding,
                      *, now: float) -> DispatchRecord: ...

    def fail_dispatch(self, claim: CoordinatorClaim, record: DispatchRecord, *, reason_code: str,
                      uncertain: bool, now: float) -> None: ...


class WorkflowDispatchService:
    def __init__(self, repository: DispatchStore, executor: StepExecutor, *, clock: Callable[[], float],
                 timeout_seconds: float = 30.0) -> None:
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 300 or not math.isfinite(timeout_seconds):
            raise WorkflowDispatchError("invalid_dispatch_timeout", uncertain=False)
        if type(executor.capabilities) is not ExecutorCapabilities:
            raise WorkflowDispatchError("invalid_executor_capabilities", uncertain=False)
        self.repository = repository
        self.executor = executor
        self.clock = clock
        self.timeout_seconds = float(timeout_seconds)

    async def dispatch(self, claim: CoordinatorClaim, *, step_key: str, generation: int,
                       expected_revision: int, allow_create: bool = True) -> ExecutionObservation:
        if type(allow_create) is not bool:
            raise WorkflowDispatchError("invalid_dispatch_permission", uncertain=False)
        try:
            record = await run_owned_io(
                self.repository.begin_dispatch, claim, step_key=step_key, generation=generation,
                expected_revision=expected_revision, now=self.clock(),
            )
        except WorkflowPersistenceError as exc:
            raise WorkflowDispatchError(exc.code, uncertain=False) from exc
        deadline = asyncio.get_running_loop().time() + self.timeout_seconds
        try:
            if record.binding is None:
                remaining = self._remaining(deadline)
                binding = await wait_owned(self._acquire_binding(claim, record, allow_create=allow_create), timeout=remaining)
                self._validate_binding(record, binding)
                record = await run_owned_io(self.repository.bind_dispatch, claim, record, binding, now=self.clock())
            remaining = self._remaining(deadline)
            observation = await wait_owned(self.executor.inspect(record.binding), timeout=remaining)
            if type(observation) is not ExecutionObservation or observation.binding != record.binding:
                raise WorkflowDispatchError("invalid_execution_observation")
            return observation
        except WorkflowDispatchDeferred:
            raise
        except WorkflowPersistenceError as exc:
            raise WorkflowDispatchError(exc.code) from exc
        except Exception as exc:
            uncertain = record.recovered or record.binding is not None or not isinstance(exc, WorkflowDispatchError) or exc.uncertain
            reason = exc.code if isinstance(exc, WorkflowDispatchError) else "executor_unavailable"
            try:
                await run_owned_io(self.repository.fail_dispatch, claim, record, reason_code=reason,
                                        uncertain=uncertain, now=self.clock())
            except WorkflowPersistenceError as store_error:
                raise WorkflowDispatchError(store_error.code) from store_error
            if isinstance(exc, WorkflowDispatchError) and not uncertain:
                raise
            raise WorkflowDispatchError("dispatch_unknown") from exc

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError()
        return remaining

    async def _acquire_binding(self, claim: CoordinatorClaim, record: DispatchRecord, *, allow_create: bool) -> ExecutionBinding:
        if record.recovered:
            if not self.executor.capabilities.lookup:
                raise WorkflowDispatchError("executor_lookup_unsupported")
            binding = await self.executor.lookup(record.token, record.payload_hash, record.resolved)
            if binding is not None:
                return binding
            if not self.executor.capabilities.idempotent:
                raise WorkflowDispatchError("executor_not_idempotent")
        if not allow_create:
            raise WorkflowDispatchDeferred("admission_closed", uncertain=False)
        try:
            await run_owned_io(self.repository.authorize_dispatch, claim, record, now=self.clock())
        except WorkflowPersistenceError as exc:
            if exc.code == "admission_closed":
                raise WorkflowDispatchDeferred(exc.code, uncertain=False) from exc
            raise
        return await self.executor.create_or_lookup(record.token, record.payload_hash, record.resolved)

    @staticmethod
    def _validate_binding(record: DispatchRecord, binding: ExecutionBinding) -> None:
        if type(binding) is not ExecutionBinding or (
            binding.owner_id, binding.workspace_id, binding.target_key, binding.token, binding.payload_hash,
        ) != (record.resolved.owner_id, record.resolved.workspace_id, record.target_key, record.token, record.payload_hash):
            raise WorkflowDispatchError("invalid_execution_binding")
