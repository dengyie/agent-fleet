"""Independent review inside the existing leased Run lifecycle."""
from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from hub.domain.workflow_contract_types import CodeRevisionReference
from hub.domain.workflow_delivery import DeliveryReceipt, DeliveryRunLease, DeliveryTarget
from hub.domain.workflow_errors import WorkflowPersistenceError, WorkflowValidationError
from hub.domain.workflow_review import ReviewAdapterSpec
from hub.platform.async_io import run_owned_io
from hub.platform.workflow_source import SourceExportError, extract_source_bundle
from hub.platform.workflow_validators import ReviewValidation, WorkflowValidatorError
from .workflow_coding_delivery import WorkflowCodingDeliveryError, WorkflowCodingDeliveryService


class ReviewExecutionStore(Protocol):
    def target_for_review(self, lease: DeliveryRunLease, *, now: float) -> tuple[DeliveryTarget, ReviewAdapterSpec]: ...

    def check_run(self, target: DeliveryTarget, *, now: float) -> bool: ...


class ReviewExecutionAdapter(Protocol):
    spec: ReviewAdapterSpec

    async def run(self, source: Path, revision: CodeRevisionReference, subject: str, *, developer_executor_id: str,
                  should_cancel: Callable[[], bool]) -> ReviewValidation: ...


class WorkflowReviewExecutionError(RuntimeError):
    def __init__(self, code: str, *, uncertain: bool = False) -> None:
        self.code = code
        self.uncertain = uncertain
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class WorkflowReviewResult:
    state: str
    verdict: str | None = None
    receipt: DeliveryReceipt | None = None
    report_hash: str | None = None


class WorkflowReviewExecutionService:
    def __init__(self, repository: ReviewExecutionStore, delivery: WorkflowCodingDeliveryService, *,
                 clock: Callable[[], float], adapters: tuple[ReviewExecutionAdapter, ...] = ()) -> None:
        if type(adapters) is not tuple or len(adapters) > 32 or any(type(item.spec) is not ReviewAdapterSpec for item in adapters):
            raise WorkflowReviewExecutionError("invalid_review_catalog")
        self.repository = repository
        self.delivery = delivery
        self.clock = clock
        self.adapters = {(item.spec.adapter_ref.id, item.spec.adapter_ref.revision): item for item in adapters}
        if len(self.adapters) != len(adapters):
            raise WorkflowReviewExecutionError("duplicate_review_registration")

    async def _target(self, lease: DeliveryRunLease) -> tuple[DeliveryTarget, ReviewAdapterSpec]:
        try:
            return await run_owned_io(self.repository.target_for_review, lease, now=self.clock())
        except (WorkflowPersistenceError, WorkflowValidationError) as exc:
            raise WorkflowReviewExecutionError(exc.code) from exc

    async def execute(self, lease: DeliveryRunLease, *, should_cancel: Callable[[], bool],
                      on_started: Callable[[], None]) -> WorkflowReviewResult:
        target, spec = await self._target(lease)
        adapter = self.adapters.get((spec.adapter_ref.id, spec.adapter_ref.revision))
        if adapter is None or adapter.spec != spec:
            raise WorkflowReviewExecutionError("review_adapter_unavailable")
        try:
            context, revision, developer = await self.delivery.source_context(target)
            if context.source_subject is None:
                raise WorkflowReviewExecutionError("source_unavailable")
            if spec.executor.executor_id == developer:
                raise WorkflowReviewExecutionError("independent_reviewer_required")

            def execution_cancelled() -> bool:
                if should_cancel():
                    return True
                try:
                    return self.repository.check_run(target, now=self.clock())
                except (WorkflowPersistenceError, WorkflowValidationError) as exc:
                    raise WorkflowReviewExecutionError(exc.code, uncertain=exc.code == "workflow_store") from exc

            if await run_owned_io(execution_cancelled):
                return WorkflowReviewResult("cancelled")
            with tempfile.TemporaryDirectory(prefix="fleet-workflow-review-") as temporary:
                checkout = Path(temporary) / "source"
                await run_owned_io(extract_source_bundle, self.delivery.artifacts, target.binding.owner_id,
                    target.binding.workspace_id, revision.source_bundle.artifact_id, checkout, expected_revision=revision)
                if await run_owned_io(execution_cancelled):
                    return WorkflowReviewResult("cancelled")
                await run_owned_io(on_started)
                result = await adapter.run(checkout, revision, context.source_subject,
                    developer_executor_id=developer, should_cancel=execution_cancelled)
                if await run_owned_io(execution_cancelled):
                    return WorkflowReviewResult("cancelled")
                receipt = await self._publish(target, result)
                return WorkflowReviewResult("succeeded", result.verdict, receipt, result.report_hash)
        except WorkflowValidatorError as exc:
            if exc.code == "review_cancelled":
                return WorkflowReviewResult("cancelled")
            raise WorkflowReviewExecutionError(exc.code, uncertain=exc.code == "review_stop_unconfirmed") from exc
        except (SourceExportError, WorkflowCodingDeliveryError) as exc:
            raise WorkflowReviewExecutionError(exc.code) from exc

    async def _publish(self, target: DeliveryTarget, result: ReviewValidation) -> DeliveryReceipt:
        lease = target.run_lease
        if lease is None:
            raise WorkflowReviewExecutionError("invalid_delivery_lease")
        failure: WorkflowCodingDeliveryError | WorkflowReviewExecutionError | None = None
        refresh = False
        for _ in range(3):
            try:
                if refresh:
                    target, spec = await self._target(lease)
                    if result.executor != spec.executor:
                        raise WorkflowReviewExecutionError("executor_identity_mismatch")
                    refresh = False
                return await self.delivery.record_review(target, result.raw_report, executor=result.executor)
            except (WorkflowCodingDeliveryError, WorkflowReviewExecutionError) as exc:
                if exc.code not in ("revision_conflict", "delivery_io_failed", "workflow_store"):
                    raise WorkflowReviewExecutionError(exc.code) from exc
                failure = exc
                if exc.code == "revision_conflict":
                    refresh = True
        raise WorkflowReviewExecutionError("review_publication_unconfirmed", uncertain=True) from failure


__all__ = ["ReviewExecutionAdapter", "WorkflowReviewExecutionError", "WorkflowReviewExecutionService", "WorkflowReviewResult"]
