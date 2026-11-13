"""Exact workflow usage projections, independent of ledger storage."""
from __future__ import annotations

from dataclasses import dataclass

from .workflow_control import exact_counter
from .workflow_errors import WorkflowValidationError


@dataclass(frozen=True, slots=True)
class WorkflowUsage:
    tokens: int
    requests: int
    complete: bool

    def __post_init__(self) -> None:
        exact_counter(self.tokens, minimum=0)
        exact_counter(self.requests, minimum=0)
        if type(self.complete) is not bool:
            raise WorkflowValidationError("invalid_usage_completeness")


@dataclass(frozen=True, slots=True)
class BudgetTick:
    examined: int
    changed: int
