"""Bounded projection of original request charges, never a second ledger."""
from __future__ import annotations

import asyncio
from typing import Callable, Protocol

from hub.domain.workflow_budget import BudgetTick
from hub.domain.workflow_errors import WorkflowPersistenceError


class WorkflowBudgetError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class WorkflowBudgetStore(Protocol):
    def project_due(self, *, now: float, limit: int) -> BudgetTick: ...


class WorkflowBudgetService:
    def __init__(self, repository: WorkflowBudgetStore, *, clock: Callable[[], float]) -> None:
        self.repository = repository
        self.clock = clock

    async def tick(self, *, limit: int = 32) -> BudgetTick:
        try:
            return await asyncio.to_thread(self.repository.project_due, now=self.clock(), limit=limit)
        except WorkflowPersistenceError as exc:
            raise WorkflowBudgetError(exc.code) from exc
