"""Remote ToolBroker adapter backed by durable Hub commands."""
from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any

from hub.domain.platform_command import PlatformCommand
from .backends.base import ToolReceipt
from .tool_broker import ToolBroker


class RemoteToolBroker:
    """Turn exactly one model tool call into exactly one Node command."""

    def __init__(self, delivery, *, node_id: str, resource_id: str, run_id: str,
                 event_sink=None, waiter=None, clock=time.time, receipt_timeout_s=30.0):
        self.delivery = delivery
        self.node_id = node_id
        self.resource_id = resource_id
        self.run_id = run_id
        self.event_sink = event_sink or (lambda kind, payload: None)
        self.waiter = waiter
        self.clock = clock
        self.receipt_timeout_s = max(0.1, min(float(receipt_timeout_s), 300.0))

    def _event(self, kind: str, command_id: str, tool: str, **extra):
        payload = {"command_id": command_id, "node_id": self.node_id, "tool": tool, **extra}
        self.event_sink(kind, payload)

    def execute(self, *, command_id: str, tool: str, arguments: dict, owner_id: str, epoch: int) -> ToolReceipt:
        if tool not in ToolBroker.TOOLS:
            return ToolReceipt(command_id, "failed", {}, "unknown_tool")
        if not isinstance(arguments, Mapping):
            return ToolReceipt(command_id, "failed", {}, "invalid_arguments")
        if not isinstance(command_id, str) or not command_id.startswith(self.run_id + ":step:"):
            return ToolReceipt(command_id, "failed", {}, "invalid_command_id")
        try:
            bounded = dict(arguments)
            if len(json.dumps(bounded, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 64 * 1024:
                return ToolReceipt(command_id, "failed", {}, "arguments_too_large")
            retry_class = "read_only" if tool in {"workspace.list", "workspace.read", "fleet.list_services", "service.get_health", "service.read_logs", "incident.get_evidence"} else "reconcile_before_retry"
            command = PlatformCommand.create(
                command_id=command_id, target_node=self.node_id, owner_id=owner_id,
                action=f"tool.{tool}", resource_id=self.resource_id,
                arguments=bounded, retry_class=retry_class,
                expires_at=float(self.clock()) + min(self.receipt_timeout_s + 60.0, 3600.0),
                run_id=self.run_id,
            )
            self.delivery.enqueue(command, idempotency_key=command_id)
            self._event("node_command_queued", command_id, tool)
            waiter = self.waiter or self.delivery
            row = waiter.wait_for_receipt(command_id, timeout_s=self.receipt_timeout_s)
            status = row.get("status") if isinstance(row, Mapping) else None
            result = row.get("result") if isinstance(row, Mapping) and isinstance(row.get("result"), dict) else {}
            # NodeToolExecutor returns a bounded wire envelope inside the
            # Hub receipt.  Expose only its backend result to the model while
            # retaining the outer envelope for status/error handling.
            if isinstance(result.get("state"), str) and isinstance(result.get("result"), dict):
                inner_result = result["result"]
                inner_error = result.get("error_code")
                result = inner_result
            else:
                inner_error = None
            self._event("node_receipt", command_id, tool, status=status)
            if status == "succeeded":
                return ToolReceipt(command_id, "succeeded", result)
            if status == "unknown":
                return ToolReceipt(command_id, "unknown", {}, "receipt_unknown")
            if status in {"failed", "expired"}:
                return ToolReceipt(command_id, "failed", result, inner_error or "remote_failed")
            return ToolReceipt(command_id, "unknown", {}, "receipt_unknown")
        except Exception:
            self._event("node_receipt", command_id, tool, status="unknown")
            return ToolReceipt(command_id, "unknown", {}, "receipt_unknown")


__all__ = ["RemoteToolBroker"]
