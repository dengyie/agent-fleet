"""Common backend protocol and bounded receipt values."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolReceipt:
    command_id: str
    state: str
    result: dict[str, Any]
    error_code: str | None = None
    truncated: bool = False


class ExecutionBackend(Protocol):
    def list(self, relative_path: str = "") -> ToolReceipt: ...
    def read(self, relative_path: str) -> ToolReceipt: ...
    def write(self, relative_path: str, content: bytes) -> ToolReceipt: ...
    def execute(self, command_id: str, argv: list[str], *, timeout_s: float = 30.0) -> ToolReceipt: ...
    def digest(self, relative_path: str) -> ToolReceipt: ...


__all__ = ["ExecutionBackend", "ToolReceipt"]
