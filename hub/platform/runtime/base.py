"""Runtime limits and worker result values."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RuntimeLimits:
    # Ordinary conversations run until completion or cancellation. A caller
    # with an explicit contract (for example a summary) can supply a budget.
    max_steps: int | None = None
    max_tokens: int | None = None


@dataclass(frozen=True)
class UsageSummary:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    provider_requests: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": max(0, int(self.input_tokens)),
            "output_tokens": max(0, int(self.output_tokens)),
            "total_tokens": max(0, int(self.total_tokens)),
            "provider_requests": max(0, int(self.provider_requests)),
        }


@dataclass(frozen=True)
class WorkerResult:
    state: str
    text: str
    steps: int
    estimated_tokens: int
    usage: UsageSummary = field(default_factory=UsageSummary)


__all__ = ["RuntimeLimits", "UsageSummary", "WorkerResult"]
