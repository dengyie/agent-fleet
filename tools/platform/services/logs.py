"""Adapter-owned, read-only service log readers."""
from __future__ import annotations

import math
import os
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any


class ServiceLogReaderError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _alias(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or "\x00" in value:
        raise ServiceLogReaderError("invalid_target_alias")
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in value):
        raise ServiceLogReaderError("invalid_target_alias")
    return value


def _window(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ServiceLogReaderError("invalid_window_s") from None
    if not math.isfinite(parsed) or parsed <= 0 or parsed > 3600:
        raise ServiceLogReaderError("invalid_window_s")
    return parsed


def _bytes(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ServiceLogReaderError("invalid_max_bytes") from None
    if parsed <= 0 or parsed > 64 * 1024:
        raise ServiceLogReaderError("invalid_max_bytes")
    return parsed


def fixed_log_argv(adapter: Any, alias: Any, window_s: Any) -> tuple[str, ...]:
    """Return the only argv allowed for a registered service adapter."""
    target = _alias(alias)
    window = _window(window_s)
    window_arg = str(int(window)) if window.is_integer() else f"{window:.3f}".rstrip("0").rstrip(".")
    if adapter == "systemd":
        return ("journalctl", "--no-pager", "--output=short-iso", f"--since=-{window_arg}s", "--unit", target)
    if adapter == "supervisor":
        return ("supervisorctl", "tail", "-100", target, "stdout")
    if adapter == "docker":
        return ("docker", "logs", "--since", f"{window_arg}s", "--tail", "500", target)
    raise ServiceLogReaderError("unsupported_adapter")


def _bounded_text(value: Any, max_bytes: int) -> tuple[str, bool]:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        value = str(value or "")
    encoded = value.encode("utf-8", errors="replace")
    truncated = len(encoded) > max_bytes
    if truncated:
        value = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return value, truncated


class FixedServiceLogReader:
    """Callable reader for an explicitly enabled Node log adapter."""

    def __init__(self, *, runner: Callable[..., Any] | None = None,
                 timeout_s: float = 30.0, clock: Callable[[], float] = time.time,
                 path_prefix: Sequence[str | os.PathLike[str]] | None = None):
        try:
            timeout = float(timeout_s)
        except (TypeError, ValueError):
            raise ServiceLogReaderError("invalid_timeout") from None
        if not math.isfinite(timeout) or timeout <= 0 or timeout > 300:
            raise ServiceLogReaderError("invalid_timeout")
        self.runner = runner or subprocess.run
        self.timeout_s = timeout
        self.clock = clock
        normalized_prefix: list[str] = []
        for item in path_prefix or ():
            if isinstance(item, os.PathLike):
                item = os.fspath(item)
            if (not isinstance(item, str) or not item or len(item) > 4096
                    or "\x00" in item):
                raise ServiceLogReaderError("invalid_path_prefix")
            normalized_prefix.append(item)
        self.path_prefix = tuple(normalized_prefix)

    def __call__(self, command: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(command, Mapping):
            raise ServiceLogReaderError("invalid_command")
        arguments = command.get("arguments")
        required = {"adapter", "target_alias", "service_version", "action", "window_s", "max_bytes"}
        if not isinstance(arguments, Mapping) or set(arguments) != required or arguments.get("action") != "logs":
            raise ServiceLogReaderError("invalid_arguments")
        max_bytes = _bytes(arguments.get("max_bytes"))
        argv = fixed_log_argv(arguments.get("adapter"), arguments.get("target_alias"), arguments.get("window_s"))
        started = float(self.clock())
        try:
            current_path = os.environ.get("PATH", "/usr/bin:/bin")
            path = os.pathsep.join((*self.path_prefix, current_path))
            result = self.runner(
                list(argv), capture_output=True, text=True, timeout=self.timeout_s,
                check=False, shell=False, env={"PATH": path},
            )
        except subprocess.TimeoutExpired:
            raise ServiceLogReaderError("timeout") from None
        except FileNotFoundError:
            raise ServiceLogReaderError("unsupported") from None
        except PermissionError:
            raise ServiceLogReaderError("permission_denied") from None
        except OSError:
            raise ServiceLogReaderError("reader_failed") from None
        text, truncated = _bounded_text(getattr(result, "stdout", ""), max_bytes)
        return {"text": text, "truncated": truncated,
                "observed_at": float(self.clock()),
                "duration_s": round(max(0.0, float(self.clock()) - started), 3)}


__all__ = ["FixedServiceLogReader", "ServiceLogReaderError", "fixed_log_argv"]
