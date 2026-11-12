"""One remote fixed command; only its original receipt and publication may retry."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from fleet_core.platform_command import PlatformCommand
from hub.domain.remote_workflow_test import (
    REMOTE_TEST_ACTION, RemoteTestError, RemoteTestRequest, remote_test_command_id,
)
from hub.domain.workflow_contract_types import CodeRevisionReference
from hub.domain.workflow_delivery import DeliveryRunLease, DeliveryTarget
from hub.domain.workflow_errors import WorkflowPersistenceError, WorkflowValidationError
from fleet_core.async_io import run_owned_io
from fleet_core.workflow.remote_test_codec import decode_test_result
from .task_service import ApplicationError
from .workflow_coding_delivery import WorkflowCodingDeliveryError, WorkflowCodingDeliveryService
from .workflow_test_execution import (
    TestExecutionStore, WorkflowTestExecutionError, WorkflowTestExecutionService, WorkflowTestResult,
)


class RemoteTestStore(TestExecutionStore, Protocol):
    def target_for_remote_test(self, lease: DeliveryRunLease, *, now: float) -> DeliveryTarget: ...

    def remote_test_request(self, target: DeliveryTarget, *, source_revision: CodeRevisionReference,
                            subject_digest: str, now: float) -> RemoteTestRequest: ...


class RemoteTestCommands(Protocol):
    def enqueue(self, command: PlatformCommand, *, idempotency_key: str) -> dict[str, Any]: ...

    def wait_for_receipt(self, command_id: str, *, timeout_s: float,
                         should_cancel: Callable[[], bool]) -> dict[str, Any]: ...


class RemoteWorkflowTestExecutionService(WorkflowTestExecutionService):
    def __init__(self, repository: RemoteTestStore, delivery: WorkflowCodingDeliveryService, *,
                 commands: RemoteTestCommands, clock: Callable[[], float]) -> None:
        super().__init__(repository, delivery, clock=clock)
        self.commands = commands

    async def _target(self, lease: DeliveryRunLease) -> DeliveryTarget:
        try:
            return await run_owned_io(self.repository.target_for_remote_test, lease, now=self.clock())
        except (WorkflowPersistenceError, WorkflowValidationError) as exc:
            raise WorkflowTestExecutionError(exc.code) from exc

    async def execute(self, lease: DeliveryRunLease, *, should_cancel: Callable[[], bool],
                      on_started: Callable[[], None]) -> WorkflowTestResult:
        target = await self._target(lease)
        enqueued = False
        try:
            context, revision, _ = await self.delivery.source_context(target)
            if context.source_subject is None or context.active_deadline is None:
                raise WorkflowTestExecutionError("source_unavailable")
            request = await run_owned_io(self.repository.remote_test_request, target, source_revision=revision,
                subject_digest=context.source_subject, now=self.clock())

            def cancelled() -> bool:
                return should_cancel() or self.repository.check_run(target, now=self.clock())

            if await run_owned_io(cancelled):
                return WorkflowTestResult("cancelled")
            command = PlatformCommand.create(command_id=remote_test_command_id(target.binding.token),
                owner_id=target.binding.owner_id, target_node=target.binding.target_key, resource_id=target.binding.workspace_id,
                action=REMOTE_TEST_ACTION, retry_class="manual_only", run_id=lease.run_id, arguments=request.payload(),
                expires_at=min(context.active_deadline, self.clock() + 90))
            # Enqueue's durable idempotency check returns the original command without another dispatch.
            await run_owned_io(self.commands.enqueue, command, idempotency_key=command.command_id)
            enqueued = True
            await run_owned_io(on_started)
            row = await run_owned_io(self.commands.wait_for_receipt, command.command_id,
                timeout_s=max(0, min(90, context.active_deadline - self.clock())), should_cancel=cancelled)
            if row.get("status") == "unknown":
                raise WorkflowTestExecutionError("remote_test_unknown", uncertain=True)
            if row.get("status") != "succeeded":
                result = row.get("result")
                error = result.get("error_code") if type(result) is dict else None
                raise WorkflowTestExecutionError(error if type(error) is str else "remote_test_failed")
            envelope = row.get("result")
            if (type(envelope) is not dict or set(envelope) != {"state", "result", "command_id"}
                    or envelope["state"] != "succeeded" or envelope["command_id"] != command.command_id):
                raise WorkflowTestExecutionError("invalid_remote_test_result")
            result = decode_test_result(envelope["result"], revision=revision, subject_digest=context.source_subject,
                node_id=target.binding.target_key)
            if await run_owned_io(cancelled):
                return WorkflowTestResult("cancelled")
            receipt = await self._publish(target, result)
            return WorkflowTestResult("succeeded", result.passed, receipt, result.report_hash)
        except WorkflowTestExecutionError as exc:
            raise WorkflowTestExecutionError(exc.code, uncertain=enqueued or exc.uncertain) from exc
        except (RemoteTestError, WorkflowCodingDeliveryError, WorkflowPersistenceError, WorkflowValidationError) as exc:
            raise WorkflowTestExecutionError(exc.code, uncertain=enqueued or getattr(exc, "uncertain", False)) from exc
        except ApplicationError as exc:
            raise WorkflowTestExecutionError(exc.code, uncertain=enqueued or exc.code == "command_store") from exc
        except (OSError, RuntimeError) as exc:
            raise WorkflowTestExecutionError("remote_test_execution_unconfirmed", uncertain=enqueued) from exc
