"""Bounded browser backend contract and opaque local session manager."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import secrets
import time
from typing import Any

from .browser_policy import BrowserPolicyError, validate_action
from hub.domain.browser_submit import opaque_browser_id
from .browser_transport import _submit_scope


@dataclass(frozen=True)
class BrowserSessionRef:
    session_id: str
    backend: str


class BrowserBackendError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class BrowserExecutionError(RuntimeError):
    """An after-dispatch driver failure whose side effect is indeterminate."""

    def __init__(self, code: str = "backend_interrupted"):
        self.code = code
        super().__init__(code)


class BrowserCleanupError(RuntimeError):
    """Bounded cleanup failure that does not expose driver exception details."""

    def __init__(self, failures):
        self.code = "backend_cleanup_failed"
        self.failures = tuple(failures)
        self.failure_count = len(self.failures)
        super().__init__(self.code)


class LocalBrowserBackend:
    """Capability-scoped adapter; driver injection avoids shell/CDP fallbacks."""

    _MAX_SESSIONS = 8
    _MAX_RESULT_BYTES = 16 * 1024
    _MAX_SCREENSHOT_BYTES = 256 * 1024
    _MAX_RESULT_DEPTH = 8
    _MAX_RESULT_ITEMS = 256
    _MAX_RESULT_NODES = 2048

    def __init__(self, driver_factory=None, *, clock=time.time, network_enabled=False,
                 allowed_origins=(), resolver=None, submit_enabled=False):
        self.driver_factory = driver_factory
        self.available = callable(driver_factory)
        self.clock = clock
        self.network_enabled = bool(network_enabled)
        self.allowed_origins = tuple(allowed_origins or ())
        self.resolver = resolver
        self.submit_enabled = bool(submit_enabled)
        self._sessions: dict[str, Any] = {}

    @classmethod
    def _validate_result(cls, value: Any, *, max_bytes: int, depth: int = 0,
                         budget: list[int] | None = None, allow_binary: bool = False) -> None:
        if budget is None:
            budget = [0, 0]

        def charge(amount: int) -> None:
            budget[0] += amount
            budget[1] += 1
            if budget[0] > max_bytes or budget[1] > cls._MAX_RESULT_NODES:
                raise BrowserBackendError("result_too_large")

        if depth > cls._MAX_RESULT_DEPTH:
            raise BrowserBackendError("result_too_large")
        if isinstance(value, str):
            if len(value) > max_bytes:
                raise BrowserBackendError("result_too_large")
            try:
                charge(len(json.dumps(value, ensure_ascii=False).encode("utf-8")))
            except UnicodeError as exc:
                raise BrowserBackendError("invalid_backend_receipt") from exc
            return
        if isinstance(value, bytes):
            if not allow_binary or depth != 0:
                raise BrowserBackendError("invalid_backend_receipt")
            if len(value) > max_bytes:
                raise BrowserBackendError("result_too_large")
            charge(len(value))
            return
        if value is None or isinstance(value, (bool, int, float)):
            if isinstance(value, int) and not -(2**53 - 1) <= value <= 2**53 - 1:
                raise BrowserBackendError("invalid_backend_receipt")
            if isinstance(value, float) and not math.isfinite(value):
                raise BrowserBackendError("invalid_backend_receipt")
            charge(len(json.dumps(value, allow_nan=False)))
            return
        if isinstance(value, dict):
            if len(value) > cls._MAX_RESULT_ITEMS:
                raise BrowserBackendError("result_too_large")
            charge(2 + max(0, len(value) - 1) + len(value))
            for key, item in value.items():
                if not isinstance(key, str):
                    raise BrowserBackendError("invalid_backend_receipt")
                if len(key) > 512:
                    raise BrowserBackendError("result_too_large")
                try:
                    key_bytes = len(key.encode("utf-8"))
                except UnicodeError as exc:
                    raise BrowserBackendError("invalid_backend_receipt") from exc
                if key_bytes > 512:
                    raise BrowserBackendError("result_too_large")
                cls._validate_result(key, max_bytes=max_bytes, depth=depth, budget=budget)
                cls._validate_result(item, max_bytes=max_bytes, depth=depth + 1, budget=budget)
            return
        if isinstance(value, (list, tuple)):
            if len(value) > cls._MAX_RESULT_ITEMS:
                raise BrowserBackendError("result_too_large")
            charge(2 + max(0, len(value) - 1))
            for item in value:
                cls._validate_result(item, max_bytes=max_bytes, depth=depth + 1, budget=budget)
            return
        raise BrowserBackendError("invalid_backend_receipt")

    @staticmethod
    def _driver_capabilities(driver: Any):
        try:
            supported_tools = getattr(driver, "supported_tools", None)
        except Exception as exc:
            raise BrowserBackendError("invalid_backend_capability") from exc
        if supported_tools is not None:
            if (not isinstance(supported_tools, (set, frozenset))
                    or any(type(item) is not str for item in supported_tools)):
                raise BrowserBackendError("invalid_backend_capability")
        return supported_tools

    @staticmethod
    def _driver_method(driver: Any, tool: str):
        supported_tools = LocalBrowserBackend._driver_capabilities(driver)
        if supported_tools is not None and tool not in supported_tools:
            raise BrowserBackendError("unsupported_tool")
        method_name = {
            "browser.open": "open",
            "browser.navigate": "navigate",
            "browser.snapshot": "snapshot",
            "browser.screenshot": "screenshot",
            "browser.click": "click",
            "browser.type": "type",
            "browser.scroll": "scroll",
            "browser.back": "back",
            "browser.submit": "submit",
            "browser.close": "close",
        }.get(tool)
        try:
            method = getattr(driver, method_name, None) if method_name is not None else None
        except Exception as exc:
            raise BrowserBackendError("invalid_backend_capability") from exc
        if not callable(method):
            raise BrowserBackendError("unsupported_tool")
        return method

    @staticmethod
    def _discard_driver(driver: Any):
        if driver is None:
            return None
        try:
            close = getattr(driver, "close", None)
            if callable(close):
                close()
        except Exception as exc:
            return exc
        return None

    @classmethod
    def _raise_open_failure(cls, driver: Any, cause: Exception) -> None:
        cleanup_error = cls._discard_driver(driver)
        if cleanup_error is not None:
            failure = BrowserCleanupError((cause, cleanup_error))
            raise BrowserBackendError("backend_failed") from failure
        raise BrowserBackendError("backend_failed") from cause

    def execute(self, tool: str, arguments: dict, *, run_id: str | None = None,
                approval_id: str | None = None) -> dict:
        try:
            args = validate_action(tool, arguments, network_enabled=self.network_enabled,
                                   allowed_origins=self.allowed_origins,
                                   resolver=self.resolver)
        except BrowserPolicyError as exc:
            raise BrowserBackendError(exc.code) from None
        session_id = args.pop("session_id", None)
        if tool == "browser.open":
            if not self.available:
                raise BrowserBackendError("backend_unavailable")
            if len(self._sessions) >= self._MAX_SESSIONS:
                raise BrowserBackendError("session_limit")
            session_id = secrets.token_urlsafe(24)
            driver = None
            try:
                driver = self.driver_factory()
                open_method = self._driver_method(driver, tool)
            except BrowserBackendError as exc:
                cleanup_error = self._discard_driver(driver)
                if cleanup_error is not None:
                    failure = BrowserCleanupError((exc, cleanup_error))
                    raise BrowserBackendError(exc.code) from failure
                raise
            except Exception as exc:
                self._raise_open_failure(driver, exc)
            try:
                open_method(args["url"])
            except Exception as exc:
                self._raise_open_failure(driver, exc)
            self._sessions[session_id] = {"driver": driver, "run_id": run_id}
            return {"session_id": session_id, "state": "open", "backend": "cdp_local"}
        entry = self._sessions.get(session_id) if isinstance(session_id, str) else None
        if entry is not None and entry["run_id"] != run_id:
            raise BrowserBackendError("session_not_found")
        driver = entry["driver"] if entry is not None else None
        if driver is None:
            raise BrowserBackendError("session_not_found")
        if tool == "browser.submit":
            if not self.submit_enabled:
                raise BrowserBackendError("submit_disabled")
            try:
                opaque_browser_id(approval_id, "approval")
            except ValueError as exc:
                raise BrowserBackendError("approval_required") from exc
        driver_method = self._driver_method(driver, tool)
        try:
            if tool == "browser.navigate": result = driver_method(args["url"])
            elif tool in {"browser.snapshot", "browser.screenshot", "browser.back"}:
                result = driver_method()
            elif tool in {"browser.click", "browser.submit"}:
                if tool == "browser.submit":
                    with _submit_scope():
                        result = driver_method(args["selector"])
                else:
                    result = driver_method(args["selector"])
            elif tool == "browser.type": result = driver_method(args["selector"], args["text"])
            elif tool == "browser.scroll": result = driver_method(args["delta_y"])
            elif tool == "browser.close":
                driver_method()
                self._sessions.pop(session_id, None)
                return {"session_id": session_id, "state": "closed"}
            else:
                raise BrowserBackendError("unknown_tool")
        except BrowserBackendError:
            raise
        except Exception as exc:
            raise BrowserExecutionError() from exc
        if tool == "browser.screenshot" and (not isinstance(result, bytes) or not result.startswith(b"\x89PNG\r\n\x1a\n")):
            raise BrowserBackendError("invalid_backend_receipt")
        self._validate_result(
            result,
            max_bytes=(self._MAX_SCREENSHOT_BYTES
                       if tool == "browser.screenshot" else self._MAX_RESULT_BYTES),
            allow_binary=tool == "browser.screenshot",
        )
        return {"session_id": session_id, "result": result, **({"approval_id": approval_id} if tool == "browser.submit" else {})}

    def close_session(self, session_id: str) -> None:
        entry = self._sessions.get(session_id)
        if entry is None:
            return
        try:
            entry["driver"].close()
        except Exception as exc:
            raise BrowserBackendError("backend_failed") from exc
        self._sessions.pop(session_id, None)

    def close_all(self) -> None:
        failures = []
        for session_id in list(self._sessions):
            try:
                self.close_session(session_id)
            except BrowserBackendError as exc:
                failures.append(exc)
        if failures:
            failure = BrowserCleanupError(failures)
            raise failure from failures[0]


__all__ = [
    "BrowserBackendError", "BrowserCleanupError", "BrowserExecutionError",
    "BrowserSessionRef", "LocalBrowserBackend",
]
