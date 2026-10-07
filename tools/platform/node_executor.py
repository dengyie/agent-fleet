"""Fixed, bounded executor for commands received by an execution Node."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .backends.base import ToolReceipt
from .browser_backend import BrowserBackendError, BrowserExecutionError


BROWSER_TOOLS = frozenset({
    "browser.open", "browser.navigate", "browser.snapshot", "browser.screenshot",
    "browser.click", "browser.type", "browser.scroll", "browser.back", "browser.close",
    "browser.submit",
})
BROWSER_SESSION_CAPABILITY = "browser.session"
NODE_TOOLS = frozenset({
    "workspace.list", "workspace.read", "workspace.write",
    "workspace.exec",
}) | BROWSER_TOOLS
POSTCHECK_ACTION = "reconcile.workspace.digest"
SERVICE_POSTCHECK_ACTION = "reconcile.service.inspect"
SERVICE_LOGS_POSTCHECK_ACTION = "reconcile.service.logs"
POSTCHECK_ACTIONS = frozenset({
    POSTCHECK_ACTION,
    SERVICE_POSTCHECK_ACTION,
    SERVICE_LOGS_POSTCHECK_ACTION,
})


class NodeToolExecutor:
    """Translate the signed wire action into an existing backend call.

    The command is treated as untrusted data.  In particular, ``argv`` is
    passed only to the backend's existing fixed executable policy and is never
    assembled into a shell command here.
    """

    def __init__(self, backend, *, allowed_tools: frozenset[str] | set[str] | None = None,
                 service_executor=None, log_reader=None, browser_backend=None,
                 browser_enabled: bool = False,
                 allowed_postcheck_actions: frozenset[str] | set[str] | None = None):
        self.backend = backend
        self.browser_backend = browser_backend
        self.browser_enabled = bool(browser_enabled)
        self.allowed_tools = frozenset(NODE_TOOLS if allowed_tools is None else allowed_tools)
        self.service_executor = service_executor
        self.log_reader = log_reader
        self.allowed_postcheck_actions = frozenset(
            POSTCHECK_ACTIONS if allowed_postcheck_actions is None
            else allowed_postcheck_actions
        )

    @staticmethod
    def _wire(command_id: str, receipt: ToolReceipt) -> dict[str, Any]:
        return {
            "state": receipt.state if receipt.state in {"succeeded", "failed"} else "failed",
            "result": dict(receipt.result or {}),
            "error_code": receipt.error_code,
            "truncated": bool(receipt.truncated),
            "command_id": command_id,
        }

    def __call__(self, command: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(command, Mapping):
            return {"state": "failed", "result": {}, "error_code": "invalid_command"}
        command_id = command.get("command_id")
        action = command.get("action")
        if not isinstance(command_id, str) or not command_id or not isinstance(action, str):
            return {"state": "failed", "result": {}, "error_code": "invalid_command"}
        if action in POSTCHECK_ACTIONS and action not in self.allowed_postcheck_actions:
            return {"state": "failed", "result": {},
                    "error_code": "capability_unavailable", "command_id": command_id}
        if action == POSTCHECK_ACTION:
            arguments = command.get("arguments")
            if not isinstance(arguments, Mapping) or set(arguments) != {"path"} or not isinstance(arguments.get("path"), str):
                return {"state": "failed", "result": {}, "error_code": "invalid_arguments", "command_id": command_id}
            try:
                receipt = self.backend.digest(arguments["path"])
            except Exception:
                raise
            if not isinstance(receipt, ToolReceipt):
                return {"state": "failed", "result": {}, "error_code": "invalid_backend_receipt", "command_id": command_id}
            return self._wire(command_id, receipt)
        if action == SERVICE_POSTCHECK_ACTION:
            arguments = command.get("arguments")
            required = {"adapter", "target_alias", "service_version", "action"}
            if (self.service_executor is None or not isinstance(arguments, Mapping)
                    or set(arguments) != required or arguments.get("action") != "inspect"
                    or command.get("resource_id") is None):
                return {"state": "failed", "result": {}, "error_code": "invalid_arguments", "command_id": command_id}
            try:
                version = int(arguments.get("service_version"))
            except (TypeError, ValueError):
                return {"state": "failed", "result": {}, "error_code": "invalid_arguments", "command_id": command_id}
            if version < 1:
                return {"state": "failed", "result": {}, "error_code": "invalid_arguments", "command_id": command_id}
            try:
                result = self.service_executor(command)
            except Exception:
                raise
            if not isinstance(result, Mapping):
                return {"state": "failed", "result": {}, "error_code": "invalid_backend_receipt", "command_id": command_id}
            state = result.get("state")
            if state in {"healthy", "unhealthy", "degraded", "unsupported"}:
                wire_state = "succeeded"
            elif state == "succeeded":
                wire_state = "succeeded"
            else:
                wire_state = "failed"
            safe = {key: result[key] for key in ("state", "returncode", "duration_s")
                    if key in result and isinstance(result[key], (str, int, float, bool))}
            if isinstance(result.get("error_code"), str):
                safe["error_code"] = result["error_code"][:80]
            return {"state": wire_state,
                    "result": safe, "error_code": result.get("error_code"),
                    "command_id": command_id}
        if action == SERVICE_LOGS_POSTCHECK_ACTION:
            arguments = command.get("arguments")
            required = {"adapter", "target_alias", "service_version", "action", "window_s", "max_bytes"}
            if (self.log_reader is None or not isinstance(arguments, Mapping)
                    or set(arguments) != required or arguments.get("action") != "logs"
                    or command.get("resource_id") is None):
                return {"state": "failed", "result": {}, "error_code": "invalid_arguments", "command_id": command_id}
            try:
                version = int(arguments.get("service_version"))
                window_s = float(arguments.get("window_s"))
                max_bytes = int(arguments.get("max_bytes"))
            except (TypeError, ValueError):
                return {"state": "failed", "result": {}, "error_code": "invalid_arguments", "command_id": command_id}
            if version < 1 or not (0 < window_s <= 3600) or not (0 < max_bytes <= 64 * 1024):
                return {"state": "failed", "result": {}, "error_code": "invalid_arguments", "command_id": command_id}
            try:
                raw = self.log_reader(command)
                if isinstance(raw, Mapping):
                    text, truncated, observed_at = raw.get("text", ""), bool(raw.get("truncated", False)), raw.get("observed_at")
                else:
                    text, truncated, observed_at = raw, False, None
                if isinstance(text, bytes):
                    text = text.decode("utf-8", errors="replace")
                if not isinstance(text, str):
                    return {"state": "failed", "result": {}, "error_code": "invalid_log_result", "command_id": command_id}
                from tools.session.redact import Redactor
                sanitized, report = Redactor(passthrough=False).redact_event({"payload": {"text": text}})
                safe_text = str(((sanitized.get("payload") or {}).get("text")) or "")
                encoded = safe_text.encode("utf-8", errors="replace")
                if len(encoded) > max_bytes:
                    safe_text = encoded[:max_bytes].decode("utf-8", errors="ignore")
                    truncated = True
                safe = {"text": safe_text, "truncated": bool(truncated),
                        "redaction": {"replaced": int(report.replaced),
                                      "categories": list(report.categories)[:32],
                                      "uncertain": bool(report.uncertain)}}
                if isinstance(observed_at, (int, float)):
                    safe["observed_at"] = float(observed_at)
                return {"state": "succeeded", "result": safe,
                        "command_id": command_id}
            except Exception:
                raise
        if not action.startswith("tool."):
            return {"state": "failed", "result": {}, "error_code": "unknown_tool", "command_id": command_id}
        tool = action[5:]
        if tool in BROWSER_TOOLS:
            if not self.browser_enabled:
                return {"state": "failed", "result": {}, "error_code": "browser_disabled", "command_id": command_id}
            if self.browser_backend is None or not getattr(self.browser_backend, "available", True):
                return {"state": "failed", "result": {}, "error_code": "backend_unavailable", "command_id": command_id}
            if tool not in self.allowed_tools:
                return {"state": "failed", "result": {}, "error_code": "capability_unavailable", "command_id": command_id}
            arguments = command.get("arguments")
            run_id = command.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                return {"state": "failed", "result": {}, "error_code": "invalid_arguments", "command_id": command_id}
            if not isinstance(arguments, Mapping):
                return {"state": "failed", "result": {}, "error_code": "invalid_arguments", "command_id": command_id}
            bounded = dict(arguments)
            approval_options = {}
            if tool == "browser.submit":
                approval_options["approval_id"] = bounded.pop("approval_id", None)
            try:
                receipt = self.browser_backend.execute(
                    tool, bounded, run_id=run_id, **approval_options,
                )
            except BrowserExecutionError:
                raise
            except BrowserBackendError as exc:
                return {"state": "failed", "result": {},
                        "error_code": exc.code, "command_id": command_id}
            if not isinstance(receipt, Mapping):
                return {"state": "failed", "result": {}, "error_code": "invalid_backend_receipt", "command_id": command_id}
            return {"state": "succeeded", "result": dict(receipt), "command_id": command_id}
        if tool not in self.allowed_tools:
            return {"state": "failed", "result": {}, "error_code": "unknown_tool", "command_id": command_id}
        arguments = command.get("arguments")
        if not isinstance(arguments, Mapping):
            return {"state": "failed", "result": {}, "error_code": "invalid_arguments", "command_id": command_id}
        try:
            if tool == "workspace.list":
                if set(arguments) - {"path"} or not isinstance(arguments.get("path", ""), str):
                    return {"state": "failed", "result": {}, "error_code": "invalid_arguments", "command_id": command_id}
                receipt = self.backend.list(arguments.get("path", ""))
            elif tool == "workspace.read":
                if set(arguments) - {"path"} or not isinstance(arguments.get("path"), str):
                    return {"state": "failed", "result": {}, "error_code": "invalid_arguments", "command_id": command_id}
                receipt = self.backend.read(arguments.get("path"))
            elif tool == "workspace.write":
                if set(arguments) - {"path", "content"} or not isinstance(arguments.get("path"), str) or not isinstance(arguments.get("content"), str):
                    return {"state": "failed", "result": {}, "error_code": "invalid_arguments", "command_id": command_id}
                if len(arguments["content"].encode("utf-8")) > 64 * 1024:
                    return {"state": "failed", "result": {}, "error_code": "content_too_large", "command_id": command_id}
                receipt = self.backend.write(arguments["path"], arguments["content"].encode("utf-8"))
            elif tool == "workspace.exec":
                if set(arguments) - {"argv", "timeout_s"} or not isinstance(arguments.get("argv"), list):
                    return {"state": "failed", "result": {}, "error_code": "invalid_arguments", "command_id": command_id}
                receipt = self.backend.execute(command_id, arguments["argv"], timeout_s=arguments.get("timeout_s", 30))
            else:
                return {"state": "failed", "result": {}, "error_code": "unknown_tool", "command_id": command_id}
            if not isinstance(receipt, ToolReceipt):
                return {"state": "failed", "result": {}, "error_code": "invalid_backend_receipt", "command_id": command_id}
            return self._wire(command_id, receipt)
        except Exception:
            # An executor/backend interruption does not prove that no side
            # effect occurred.  Let NodeClient journal it as ``unknown`` so a
            # later reconcile step, rather than an automatic retry, decides.
            raise


__all__ = [
    "BROWSER_TOOLS", "BROWSER_SESSION_CAPABILITY", "NODE_TOOLS", "POSTCHECK_ACTION", "SERVICE_POSTCHECK_ACTION",
    "SERVICE_LOGS_POSTCHECK_ACTION", "POSTCHECK_ACTIONS", "NodeToolExecutor",
]
