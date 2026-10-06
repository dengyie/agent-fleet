"""Remote ToolBroker adapter backed by durable Hub commands."""
from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any

from hub.domain.platform_command import PlatformCommand
from .backends.base import ToolReceipt
from .browser_policy import BrowserPolicyError, validate_action
from .node_executor import BROWSER_TOOLS
from .tool_broker import ToolBroker


class RemoteToolBroker:
    """Turn exactly one model tool call into exactly one Node command."""

    def __init__(self, delivery, *, node_id: str, resource_id: str, run_id: str,
                 event_sink=None, waiter=None, clock=time.time, receipt_timeout_s=30.0,
                 allowed_tools=None,
                 browser_enabled: bool = False, browser_network_enabled: bool = False,
                 browser_allowed_origins=(), browser_resolver=None,
                 browser_submit_enabled: bool = False, submit_approvals=None):
        self.delivery = delivery
        self.node_id = node_id
        self.resource_id = resource_id
        self.run_id = run_id
        self.event_sink = event_sink or (lambda kind, payload: None)
        self.waiter = waiter
        self.clock = clock
        self.receipt_timeout_s = max(0.1, min(float(receipt_timeout_s), 300.0))
        self.allowed_tools = ToolBroker.TOOLS if allowed_tools is None else ToolBroker.TOOLS.intersection(allowed_tools)
        self.browser_enabled = bool(browser_enabled)
        self.browser_network_enabled = bool(browser_network_enabled)
        self.browser_allowed_origins = tuple(browser_allowed_origins or ())
        self.browser_resolver = browser_resolver
        self.browser_submit_enabled = bool(browser_submit_enabled)
        self.submit_approvals = submit_approvals

    def _event(self, kind: str, command_id: str, tool: str, **extra):
        payload = {"command_id": command_id, "node_id": self.node_id, "tool": tool, **extra}
        self.event_sink(kind, payload)

    def execute(self, *, command_id: str, tool: str, arguments: dict, owner_id: str, epoch: int) -> ToolReceipt:
        if tool not in ToolBroker.TOOLS:
            return ToolReceipt(command_id, "failed", {}, "unknown_tool")
        if tool not in self.allowed_tools:
            return ToolReceipt(command_id, "failed", {}, "tool_not_allowed")
        if tool in BROWSER_TOOLS and not self.browser_enabled:
            return ToolReceipt(command_id, "failed", {}, "browser_disabled")
        if tool == "browser.submit" and (not self.browser_submit_enabled or self.submit_approvals is None):
            return ToolReceipt(command_id, "failed", {}, "submit_disabled")
        if not isinstance(arguments, Mapping):
            return ToolReceipt(command_id, "failed", {}, "invalid_arguments")
        if not isinstance(command_id, str) or not command_id.startswith(self.run_id + ":step:"):
            return ToolReceipt(command_id, "failed", {}, "invalid_command_id")
        try:
            bounded = dict(arguments)
            if tool in BROWSER_TOOLS:
                bounded = validate_action(
                    tool, bounded,
                    network_enabled=self.browser_network_enabled,
                    allowed_origins=self.browser_allowed_origins,
                    resolver=self.browser_resolver,
                )
            if tool == "browser.submit":
                # Approval consumption is atomic on the durable row and
                # precedes command creation; a later undelivered command
                # leaves the approval consumed (the fail-safe direction).
                try:
                    bounded["approval_id"] = self.submit_approvals.consume_submit_approval(
                        owner_id, self.run_id,
                        session_id=bounded["session_id"],
                        selector=bounded["selector"],
                        command_id=command_id, now=float(self.clock()),
                    )
                except Exception as exc:
                    code = getattr(exc, "code", None)
                    if isinstance(code, str) and code:
                        return ToolReceipt(command_id, "failed", {}, code)
                    raise
            if len(json.dumps(bounded, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 64 * 1024:
                return ToolReceipt(command_id, "failed", {}, "arguments_too_large")
            retry_class = "read_only" if tool in {"workspace.list", "workspace.read", "fleet.list_services", "service.get_health", "service.read_logs", "incident.get_evidence"} else "reconcile_before_retry"
            if tool in BROWSER_TOOLS:
                retry_class = "manual_only"
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
        except BrowserPolicyError as exc:
            return ToolReceipt(command_id, "failed", {}, exc.code)
        except Exception as exc:
            # A non-active approval can still be present when the stored row is
            # consumed/revoked under another selector match window; surface its
            # stable code instead of generic unknown so the retry stays manual.
            approval_codes = {"approval_expired", "approval_consumed", "approval_revoked"}
            code = getattr(exc, "code", None)
            if isinstance(code, str) and code in approval_codes:
                return ToolReceipt(command_id, "failed", {}, code)
            self._event("node_receipt", command_id, tool, status="unknown")
            return ToolReceipt(command_id, "unknown", {}, "receipt_unknown")


__all__ = ["RemoteToolBroker"]
