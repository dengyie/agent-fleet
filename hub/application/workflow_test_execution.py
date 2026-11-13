"""Fixed test execution inside the existing leased Run worker lifecycle."""
from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from hub.domain.workflow_delivery import DeliveryReceipt, DeliveryRunLease, DeliveryTarget
from hub.domain.workflow_errors import WorkflowPersistenceError, WorkflowValidationError
from hub.platform.async_io import run_owned_io
from hub.platform.workflow_source import SourceExportError, extract_source_bundle
from hub.platform.workflow_validators import CODING_DELIVERY_TEST_RECIPE, FixedTestRunner, TestValidation, WorkflowValidatorError
from .workflow_coding_delivery import WorkflowCodingDeliveryError, WorkflowCodingDeliveryService


class TestExecutionStore(Protocol):
    def target_for_run(self, lease: DeliveryRunLease, *, operation: str, now: float) -> DeliveryTarget: ...

    def check_run(self, target: DeliveryTarget, *, now: float) -> bool: ...


class WorkflowTestExecutionError(RuntimeError):
    def __init__(self, code: str, *, uncertain: bool = False) -> None:
        self.code = code
        self.uncertain = uncertain
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class WorkflowTestResult:
    state: str
    passed: bool | None = None
    receipt: DeliveryReceipt | None = None
    report_hash: str | None = None


class WorkflowTestExecutionService:
    def __init__(self, repository: TestExecutionStore, delivery: WorkflowCodingDeliveryService, *,
                 clock: Callable[[], float], runner: FixedTestRunner | None = None) -> None:
        self.repository = repository
        self.delivery = delivery
        self.clock = clock
        self.runner = runner or FixedTestRunner()
        if self.runner.recipes.get(CODING_DELIVERY_TEST_RECIPE.recipe_id) != CODING_DELIVERY_TEST_RECIPE:
            raise WorkflowTestExecutionError("invalid_test_recipe")

    async def _target(self, lease: DeliveryRunLease) -> DeliveryTarget:
        try:
            return await run_owned_io(self.repository.target_for_run, lease, operation="test", now=self.clock())
        except (WorkflowPersistenceError, WorkflowValidationError) as exc:
            raise WorkflowTestExecutionError(exc.code) from exc

    async def execute(self, lease: DeliveryRunLease, *, should_cancel: Callable[[], bool],
                      on_started: Callable[[], None]) -> WorkflowTestResult:
        target = await self._target(lease)
        try:
            context, revision, _ = await self.delivery.source_context(target)
            if context.source_subject is None:
                raise WorkflowTestExecutionError("source_unavailable")

            def execution_cancelled() -> bool:
                if should_cancel():
                    return True
                try:
                    return self.repository.check_run(target, now=self.clock())
                except (WorkflowPersistenceError, WorkflowValidationError) as exc:
                    raise WorkflowTestExecutionError(exc.code, uncertain=exc.code == "workflow_store") from exc

            if await run_owned_io(execution_cancelled):
                return WorkflowTestResult("cancelled")
            with tempfile.TemporaryDirectory(prefix="fleet-workflow-test-") as temporary:
                checkout = Path(temporary) / "source"
                await run_owned_io(extract_source_bundle, self.delivery.artifacts, target.binding.owner_id,
                    target.binding.workspace_id, revision.source_bundle.artifact_id, checkout, expected_revision=revision)
                if await run_owned_io(execution_cancelled):
                    return WorkflowTestResult("cancelled")
                await run_owned_io(on_started)
                result = await self.runner.run(revision, context.source_subject, checkout,
                    recipe_id=CODING_DELIVERY_TEST_RECIPE.recipe_id, executor_id="local-fixed-tests", should_cancel=execution_cancelled)
                if await run_owned_io(execution_cancelled):
                    return WorkflowTestResult("cancelled")
                receipt = await self._publish(target, result)
                return WorkflowTestResult("succeeded", result.passed, receipt, result.report_hash)
        except WorkflowValidatorError as exc:
            if exc.code == "test_cancelled":
                return WorkflowTestResult("cancelled")
            raise WorkflowTestExecutionError(exc.code, uncertain=exc.code in {"test_execution_failed", "test_stop_unconfirmed"}) from exc
        except (SourceExportError, WorkflowCodingDeliveryError) as exc:
            raise WorkflowTestExecutionError(exc.code) from exc

    async def _publish(self, target: DeliveryTarget, result: TestValidation) -> DeliveryReceipt:
        # Only the publication is retried. The process is never rerun here.
        lease = target.run_lease
        if lease is None:
            raise WorkflowTestExecutionError("invalid_delivery_lease")
        failure: WorkflowCodingDeliveryError | WorkflowTestExecutionError | None = None
        refresh = False
        for _ in range(3):
            try:
                if refresh:
                    target = await self._target(lease)
                    refresh = False
                return await self.delivery.record_test(target, result)
            except (WorkflowCodingDeliveryError, WorkflowTestExecutionError) as exc:
                if exc.code not in ("revision_conflict", "delivery_io_failed", "workflow_store"):
                    raise WorkflowTestExecutionError(exc.code) from exc
                failure = exc
                if exc.code == "revision_conflict":
                    refresh = True
        raise WorkflowTestExecutionError("test_publication_unconfirmed", uncertain=True) from failure


__all__ = ["WorkflowTestExecutionError", "WorkflowTestExecutionService", "WorkflowTestResult"]
