"""Explicit idempotent control use cases; no client-selected terminal state."""
from __future__ import annotations

import asyncio
from typing import Callable, Protocol

from hub.domain.workflow_control import AuthorizedInputRevision, WorkflowControlReceipt, WorkflowControlRequest
from hub.domain.workflow_errors import WorkflowPersistenceError


class WorkflowControlError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class WorkflowControlStore(Protocol):
    def control(self, request: WorkflowControlRequest, *, now: float, cancel_seconds: float,
                authorized_inputs: AuthorizedInputRevision | None = None) -> WorkflowControlReceipt: ...


class WorkflowInputAuthorizer(Protocol):
    async def authorize(self, request: WorkflowControlRequest) -> AuthorizedInputRevision: ...


class WorkflowControlService:
    def __init__(self, repository: WorkflowControlStore, *, clock: Callable[[], float], cancel_seconds: float = 30,
                 input_authorizer: WorkflowInputAuthorizer | None = None) -> None:
        if type(cancel_seconds) not in (int, float) or not 0 < cancel_seconds <= 300:
            raise WorkflowControlError("invalid_cancel_timeout")
        self.repository = repository
        self.clock = clock
        self.cancel_seconds = float(cancel_seconds)
        self.input_authorizer = input_authorizer

    async def execute(self, request: WorkflowControlRequest) -> WorkflowControlReceipt:
        try:
            authorized = None
            if request.input_refs is not None:
                if self.input_authorizer is None:
                    raise WorkflowControlError("reference_forbidden")
                authorized = await self.input_authorizer.authorize(request)
            return await asyncio.to_thread(self.repository.control, request, now=self.clock(), cancel_seconds=self.cancel_seconds,
                                           authorized_inputs=authorized)
        except WorkflowPersistenceError as exc:
            raise WorkflowControlError(exc.code) from exc
