"""Application-owned continuation result repository port."""
from typing import Protocol


class ContinuationResultStoreError(RuntimeError):
    def __init__(self, code="continuation_result_unavailable"):
        self.code = code
        super().__init__(code)


class ContinuationResultStore(Protocol):
    def put(self, machine: str, value: dict) -> tuple[dict, bool]: ...
    def page(self, session_id: str, *, limit: int, before: int | None) -> dict: ...
    def get(self, session_id: str, command_id: str) -> dict | None: ...
