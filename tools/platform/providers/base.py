"""Provider-neutral model response values.

The runtime deliberately consumes a very small response contract.  Provider
adapters may retain richer wire metadata internally, but only bounded text,
one structured tool call, and normalized usage cross into the execution loop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol
from tools.platform.request_metadata import RequestMetadata, RequestObserver


@dataclass(frozen=True)
class ModelResponse:
    kind: str
    text: str = ""
    tool: str | None = None
    arguments: dict[str, Any] | None = None
    usage: dict[str, int] | None = None
    tool_call_id: str | None = None
    finish_reason: str | None = None
    metadata: RequestMetadata = field(default_factory=dict)


class ModelProvider(Protocol):
    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *,
                 request_observer: RequestObserver | None = None) -> ModelResponse: ...


__all__ = ["ModelProvider", "ModelResponse"]
