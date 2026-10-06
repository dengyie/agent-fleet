"""Bounded browser backend contract and opaque local session manager."""
from __future__ import annotations

from dataclasses import dataclass
import secrets
import time
from typing import Any

from .browser_policy import BrowserPolicyError, validate_action


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
                 allowed_origins=(), submit_enabled=False):
        self.driver_factory = driver_factory
        self.available = callable(driver_factory)
        self.clock = clock
        self.network_enabled = bool(network_enabled)
        self.allowed_origins = tuple(allowed_origins or ())
        self.submit_enabled = bool(submit_enabled)
        self._sessions: dict[str, Any] = {}

    @classmethod
    def _validate_result(cls, value: Any, *, max_bytes: int, depth: int = 0,
                         budget: list[int] | None = None) -> None:
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
            size = len(value.encode("utf-8"))
            if size > max_bytes:
                raise BrowserBackendError("result_too_large")
            charge(size)
            return
        if isinstance(value, bytes):
            if len(value) > max_bytes:
                raise BrowserBackendError("result_too_large")
            charge(len(value))
            return
        if value is None or isinstance(value, (bool, int, float)):
            charge(8)
            return
        if isinstance(value, dict):
            if len(value) > cls._MAX_RESULT_ITEMS:
                raise BrowserBackendError("result_too_large")
            charge(2)
            for key, item in value.items():
                if not isinstance(key, str) or len(key.encode("utf-8")) > 512:
                    raise BrowserBackendError("result_too_large")
                charge(len(key.encode("utf-8")) + 4)
                cls._validate_result(item, max_bytes=max_bytes, depth=depth + 1, budget=budget)
            return
        if isinstance(value, (list, tuple)):
            if len(value) > cls._MAX_RESULT_ITEMS:
                raise BrowserBackendError("result_too_large")
            charge(2)
            for item in value:
                cls._validate_result(item, max_bytes=max_bytes, depth=depth + 1, budget=budget)
            return
        raise BrowserBackendError("invalid_backend_receipt")

    def execute(self, tool: str, arguments: dict, *, run_id: str | None = None) -> dict:
        try:
            args = validate_action(tool, arguments, network_enabled=self.network_enabled,
                                   allowed_origins=self.allowed_origins)
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
                driver.open(args["url"])
            except Exception as exc:
                close = getattr(driver, "close", None) if driver is not None else None
                if callable(close):
                    try:
                        close()
                    except Exception as cleanup_exc:
                        failure = BrowserCleanupError((exc, cleanup_exc))
                        raise BrowserBackendError("backend_failed") from failure
                raise BrowserBackendError("backend_failed") from exc
            self._sessions[session_id] = {"driver": driver, "run_id": run_id}
            return {"session_id": session_id, "state": "open", "backend": "cdp_local"}
        entry = self._sessions.get(session_id) if isinstance(session_id, str) else None
        if entry is not None and entry["run_id"] != run_id:
            raise BrowserBackendError("session_not_found")
        driver = entry["driver"] if entry is not None else None
        if driver is None:
            raise BrowserBackendError("session_not_found")
        if tool == "browser.submit" and not self.submit_enabled:
            raise BrowserBackendError("submit_disabled")
        try:
            if tool == "browser.navigate": result = driver.navigate(args["url"])
            elif tool == "browser.snapshot": result = driver.snapshot()
            elif tool == "browser.screenshot": result = driver.screenshot()
            elif tool == "browser.click": result = driver.click(args["selector"])
            elif tool == "browser.type": result = driver.type(args["selector"], args["text"])
            elif tool == "browser.scroll": result = driver.scroll(args["delta_y"])
            elif tool == "browser.back": result = driver.back()
            elif tool == "browser.submit": result = driver.submit(args["selector"])
            elif tool == "browser.close":
                driver.close()
                self._sessions.pop(session_id, None)
                return {"session_id": session_id, "state": "closed"}
            else:
                raise BrowserBackendError("unknown_tool")
        except BrowserBackendError:
            raise
        except Exception as exc:
            raise BrowserExecutionError() from exc
        self._validate_result(
            result,
            max_bytes=(self._MAX_SCREENSHOT_BYTES
                       if tool == "browser.screenshot" else self._MAX_RESULT_BYTES),
        )
        payload = {"session_id": session_id, "result": result}
        if tool == "browser.submit" and "approval_id" in args:
            payload["approval_id"] = args["approval_id"]
        return payload

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
