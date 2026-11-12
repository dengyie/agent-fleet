"""Operator use cases for explicit summary publication and exact version reads."""
from __future__ import annotations

from typing import Any, Callable, Protocol

from hub.domain.workflow_summary import WorkflowSummaryVersion, summary_value


class SummaryStore(Protocol):
    def publish(self, owner: str, workspace: str, summary_id: str, *, title: str, content: str,
                client_token: str, expected_epoch: int, now: float) -> WorkflowSummaryVersion: ...
    def get(self, owner: str, workspace: str, summary_id: str, *, revision: int) -> WorkflowSummaryVersion: ...
    def list(self, owner: str, workspace: str, *, limit: int, cursor: str | None) -> dict[str, Any]: ...
    def set_enabled(self, owner: str, workspace: str, summary_id: str, *, enabled: bool, expected_epoch: int) -> dict[str, Any]: ...


class WorkflowSummaryService:
    def __init__(self, store: SummaryStore, *, clock: Callable[[], float]) -> None:
        self.store, self.clock = store, clock

    def publish(self, owner: str, values: dict[str, Any]) -> dict[str, Any]:
        item = self.store.publish(owner, values["workspace_id"], values["summary_id"],
            title=values["title"], content=values["content"], client_token=values["client_token"],
            expected_epoch=values["expected_epoch"], now=self.clock())
        return {**summary_value(item), "sha256": item.sha256}

    def get(self, owner: str, workspace: str, summary_id: str, *, revision: int) -> dict[str, Any]:
        item = self.store.get(owner, workspace, summary_id, revision=revision)
        return {**summary_value(item), "sha256": item.sha256}

    def head(self, owner: str, workspace: str, summary_id: str) -> dict[str, Any]:
        return self.store.head(owner, workspace, summary_id)

    def list(self, owner: str, workspace: str, *, limit: int, cursor: str | None) -> dict[str, Any]:
        return self.store.list(owner, workspace, limit=limit, cursor=cursor)

    def set_enabled(self, owner: str, values: dict[str, Any]) -> dict[str, Any]:
        return self.store.set_enabled(owner, values["workspace_id"], values["summary_id"],
            enabled=values["enabled"], expected_epoch=values["expected_epoch"])
