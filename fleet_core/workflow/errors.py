"""Stable domain failures; transport layers choose their own status mapping."""
from __future__ import annotations


class WorkflowValidationError(ValueError):
    """Invalid workflow input with a bounded, non-secret reason code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class WorkflowPersistenceError(RuntimeError):
    """Persistence port failure; concrete stores retain their original cause."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)
