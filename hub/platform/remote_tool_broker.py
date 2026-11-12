"""Remote ToolBroker adapter backed by durable Hub commands."""
from __future__ import annotations

import json
import time
from collections.abc import Mapping

from fleet_core.backends.base import ToolReceipt
from fleet_core.browser_policy import BROWSER_TOOLS, BrowserPolicyError, validate_action
from fleet_core.platform_command import PlatformCommand
from fleet_core.command_timing import CommandTimingPolicy
from hub.domain.capability_execution import CAPABILITY_ARGUMENT
from fleet_core.web_agent_contract import WEB_AGENT_TOOLS, MuseBridgeError, validate_arguments
from .tool_broker import ToolBroker


def remote_retry_class(tool: str) -> str:
    """Classify either a tool name or its wire action without executing it."""
    tool = tool.removeprefix("tool.")
    if tool == "workflow.test" or tool in BROWSER_TOOLS or tool in WEB_AGENT_TOOLS:
        return "manual_only"
    if tool in {"workspace.list", "workspace.read", "fleet.list_services", "service.get_health", "service.read_logs", "incident.get_evidence"}:
        return "read_only"
    return "reconcile_before_retry"


class RemoteToolBroker:
    """Turn exactly one model tool call into exactly one Node command."""

    def __init__(self, delivery, *, node_id: str, resource_id: str, run_id: str,
                 event_sink=None, waiter=None, clock=time.time, receipt_timeout_s=None,
                 timing_policy: CommandTimingPolicy | None = None,
                 allowed_tools=None,
                 browser_enabled: bool = False, browser_network_enabled: bool = False,
                 browser_allowed_origins=(), browser_resolver=None,
                 browser_submit_enabled: bool = False, submit_approvals=None,
                 web_agent_enabled: bool = False, should_cancel=None, defer_wait=False):
        self.web_agent_enabled = bool(web_agent_enabled)
        self.delivery = delivery
        self.node_id = node_id
        self.resource_id = resource_id
        self.run_id = run_id
        self.event_sink = event_sink or (lambda kind, payload: None)
        self.waiter = waiter
        self.clock = clock
        self.should_cancel = should_cancel
        self.defer_wait = bool(defer_wait)
        if timing_policy is not None and receipt_timeout_s is not None:
            raise ValueError("ambiguous_command_timing")
        self.timing_policy = timing_policy or CommandTimingPolicy(
            **({"receipt_timeout_s": receipt_timeout_s} if receipt_timeout_s is not None else {}))
        self.allowed_tools = ToolBroker.TOOLS if allowed_tools is None else ToolBroker.TOOLS.intersection(allowed_tools)
        self.browser_enabled = bool(browser_enabled)
        self.browser_network_enabled = bool(browser_network_enabled)
        self.browser_allowed_origins = tuple(browser_allowed_origins or ())
        self.browser_resolver = browser_resolver
        self.browser_submit_enabled = bool(browser_submit_enabled and browser_enabled and browser_network_enabled)
        self.submit_approvals = submit_approvals

    def _event(self, kind: str, command_id: str, tool: str, **extra):
        payload = {"command_id": command_id, "node_id": self.node_id, "tool": tool, **extra}
        self.event_sink(kind, payload)

    def execute(self, *, command_id: str, tool: str, arguments: dict, owner_id: str, epoch: int) -> ToolReceipt:
        if tool not in ToolBroker.TOOLS:
            return ToolReceipt(command_id, "failed", {}, "unknown_tool")
        if tool not in self.allowed_tools:
            return ToolReceipt(command_id, "failed", {}, "tool_not_allowed")
        if tool in WEB_AGENT_TOOLS and not self.web_agent_enabled:
            return ToolReceipt(command_id, "failed", {}, "capability_unavailable")
        if tool in BROWSER_TOOLS and not self.browser_enabled:
            return ToolReceipt(command_id, "failed", {}, "browser_disabled")
        if tool == "browser.submit" and (not self.browser_submit_enabled or self.submit_approvals is None):
            return ToolReceipt(command_id, "failed", {}, "submit_disabled")
        if not isinstance(arguments, Mapping):
            return ToolReceipt(command_id, "failed", {}, "invalid_arguments")
        if CAPABILITY_ARGUMENT in arguments:
            return ToolReceipt(command_id, "failed", {}, "invalid_capability_command")
        if not isinstance(command_id, str) or not command_id.startswith(self.run_id + ":step:"):
            return ToolReceipt(command_id, "failed", {}, "invalid_command_id")
        attribution = {}
        try:
            if self.should_cancel is not None and self.should_cancel():
                return ToolReceipt(command_id, "failed", {}, "execution_cancelled")
            bounded = dict(arguments)
            if tool in WEB_AGENT_TOOLS:
                bounded = validate_arguments(tool, bounded)
            if tool in BROWSER_TOOLS:
                bounded = validate_action(
                    tool, bounded,
                    network_enabled=self.browser_network_enabled,
                    allowed_origins=self.browser_allowed_origins,
                    resolver=self.browser_resolver,
                )
            if len(json.dumps(bounded, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 64 * 1024:
                return ToolReceipt(command_id, "failed", {}, "arguments_too_large")
            retry_class = remote_retry_class(tool)
            timing = self.timing_policy.start(float(self.clock()))
            command = PlatformCommand.create(
                command_id=command_id, target_node=self.node_id, owner_id=owner_id,
                action=f"tool.{tool}", resource_id=self.resource_id,
                arguments=bounded, retry_class=retry_class,
                expires_at=timing.hard_deadline,
                run_id=self.run_id,
            )
            if tool == "browser.submit":
                try:
                    admitted = self.delivery.enqueue_submit(command, approvals=self.submit_approvals, idempotency_key=command_id, timing=timing)
                except Exception as exc:
                    code = getattr(exc, "code", "command_store")
                    allowed = {"approval_required", "approval_expired", "approval_consumed", "approval_revoked", "approval_limit",
                               "session_not_found", "invalid_selector", "sensitive_field_forbidden", "command_signature_required",
                               "idempotency_conflict", "command_conflict", "command_store", "browser_store",
                               "writer_lease_active"}
                    return ToolReceipt(command_id, "failed", {}, code if code in allowed else "command_store")
                attribution = {"approval_id": admitted["arguments"]["approval_id"]}
            else:
                try:
                    admitted = self.delivery.enqueue(command, idempotency_key=command_id, timing=timing)
                except Exception as exc:
                    if getattr(exc, "code", None) == "writer_lease_active":
                        return ToolReceipt(command_id, "failed", {}, "writer_lease_active")
                    raise
            self._event("node_command_queued", command_id, tool, **attribution)
            waiter = self.waiter or self.delivery
            # Idempotency may return an older command. Its original persisted
            # deadline, never a retry's newly calculated budget, governs wait.
            wait_options = {"timeout_s": 0 if self.defer_wait else max(0.0, admitted["expires_at"] - float(self.clock()))}
            if self.should_cancel is not None:
                wait_options["should_cancel"] = self.should_cancel
            row = waiter.wait_for_receipt(command_id, **wait_options)
            return self._receipt(row, command_id=command_id, tool=tool,
                                 attribution=attribution, allow_pending=self.defer_wait)
        except (BrowserPolicyError, MuseBridgeError) as exc:
            return ToolReceipt(command_id, "failed", {}, exc.code)
        except Exception as exc:
            if getattr(exc, 'code', None) == 'execution_cancelled':
                return ToolReceipt(command_id, 'failed', {}, 'execution_cancelled')
            self._event("node_receipt", command_id, tool, status="unknown", **attribution)
            return ToolReceipt(command_id, "unknown", attribution, "receipt_unknown")

    def observe(self, *, command_id, tool):
        """Observe an existing command; never enqueue or change its deadline."""
        try:
            row = self.delivery.wait_for_receipt(command_id, timeout_s=0, should_cancel=self.should_cancel)
        except Exception:
            self._event('node_receipt', command_id, tool, status='unknown')
            return ToolReceipt(command_id, 'unknown', {}, 'receipt_unknown')
        return self._receipt(row, command_id=command_id, tool=tool, allow_pending=True)

    def _receipt(self, row, *, command_id, tool, attribution=None, allow_pending=False):
        attribution = dict(attribution or {})
        status = row.get('status') if isinstance(row, Mapping) else None
        arguments = row.get('arguments') if isinstance(row, Mapping) else None
        if tool == 'browser.submit' and isinstance(arguments, Mapping) and arguments.get('approval_id'):
            attribution['approval_id'] = arguments['approval_id']
        payload = row.get('result') if isinstance(row, Mapping) else None
        payload = payload if isinstance(payload, dict) else {}
        if isinstance(payload.get('state'), str) and isinstance(payload.get('result'), dict):
            result, error = payload['result'], payload.get('error_code')
        else:
            result, error = payload, None
        if allow_pending and status in {'queued', 'leased', 'accepted', 'running'}:
            return ToolReceipt(command_id, 'waiting_node', attribution)
        self._event('node_receipt', command_id, tool, status=status, **attribution)
        if status == 'succeeded':
            return ToolReceipt(command_id, 'succeeded', {**result, **attribution})
        if status in {'failed', 'expired'}:
            return ToolReceipt(command_id, 'failed', {**result, **attribution}, error or 'remote_failed')
        return ToolReceipt(command_id, 'unknown', attribution, 'receipt_unknown')


__all__ = ["RemoteToolBroker", "remote_retry_class"]
