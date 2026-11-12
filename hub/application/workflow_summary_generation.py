"""Create/observe a queued summary Run and explicitly publish its result."""
from __future__ import annotations

from typing import Any, Callable

from hub.domain.workflow_summary import summary_value


class WorkflowSummaryGenerationService:
    def __init__(self, generations, summaries, *, clock: Callable[[], float]) -> None:
        self.generations, self.summaries, self.clock = generations, summaries, clock

    def create(self, owner: str, values: dict[str, Any]) -> dict[str, Any]:
        return self.generations.create(owner, values, now=self.clock())

    def get(self, owner: str, run_id: str) -> dict[str, Any]:
        return self.generations.get(owner, run_id)

    def find(self, owner: str, client_token: str) -> dict[str, Any]:
        return self.generations.find(owner, client_token)

    def publish(self, owner: str, run_id: str) -> dict[str, Any]:
        item = self.summaries.publish_generated(owner, run_id, now=self.clock())
        return {**summary_value(item), "sha256": item.sha256}
