"""Shared bounded contracts for read-only service collectors."""
from __future__ import annotations

import math
import os
import subprocess
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Sequence


class CollectorError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True)
class HealthEvidence:
    dimension: str
    source: str
    state: str
    observed_at: float
    ttl_s: float
    detail: dict[str, Any]

    def as_dict(self, *, service_id: str) -> dict[str, Any]:
        data = asdict(self)
        data["service_id"] = service_id
        return data


def _bounded_timeout(timeout_s: float) -> float:
    try:
        value = float(timeout_s)
    except (TypeError, ValueError):
        raise CollectorError("invalid_timeout") from None
    if not math.isfinite(value) or value <= 0 or value > 300:
        raise CollectorError("invalid_timeout")
    return value


def _detail(**values: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in values.items():
        if isinstance(value, str):
            out[key] = value[:512]
        elif isinstance(value, (bool, int, float)) or value is None:
            out[key] = value
    return out


def run_fixed_command(
    argv: Sequence[str], *, timeout_s: float, runner: Callable[..., Any] | None = None,
    now: Callable[[], float] = time.time,
    path_prefix: Sequence[str | os.PathLike[str]] | None = None,
) -> tuple[str, str, int | None, float]:
    """Run an adapter-owned argv without a shell and return bounded metadata."""
    if not isinstance(argv, (list, tuple)) or not argv or any(
        not isinstance(arg, str) or not arg or len(arg) > 512 or "\x00" in arg
        for arg in argv
    ):
        raise CollectorError("invalid_command")
    started = float(now())
    execute = runner or subprocess.run
    normalized_prefix: list[str] = []
    for item in path_prefix or ():
        if isinstance(item, os.PathLike):
            item = os.fspath(item)
        if not isinstance(item, str) or not item or len(item) > 4096 or "\x00" in item:
            raise CollectorError("invalid_path_prefix")
        normalized_prefix.append(item)
    current_path = os.environ.get("PATH", "/usr/bin:/bin")
    path = os.pathsep.join((*normalized_prefix, current_path))
    try:
        result = execute(
            list(argv), capture_output=True, text=True, timeout=_bounded_timeout(timeout_s),
            check=False, shell=False, env={"PATH": path},
        )
    except subprocess.TimeoutExpired:
        return "timeout", "", None, max(0.0, float(now()) - started)
    except FileNotFoundError:
        return "unsupported", "", None, max(0.0, float(now()) - started)
    except PermissionError:
        return "unsupported", "permission_denied", None, max(0.0, float(now()) - started)
    except OSError:
        return "unknown", "exec_failed", None, max(0.0, float(now()) - started)
    stdout = str(getattr(result, "stdout", "") or "").strip()[:512]
    stderr = str(getattr(result, "stderr", "") or "").strip()[:512]
    return "ok", stdout or stderr, getattr(result, "returncode", None), max(0.0, float(now()) - started)


def command_evidence(
    *, service_id: str, source: str, alias: str, argv: Sequence[str],
    dimension: str = "process_state", timeout_s: float = 5.0, ttl_s: float = 180.0,
    runner: Callable[..., Any] | None = None, now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    status, output, returncode, duration_s = run_fixed_command(
        argv, timeout_s=timeout_s, runner=runner, now=now,
    )
    if status == "ok":
        state = "healthy" if returncode == 0 else "unhealthy"
    elif status == "unsupported":
        state = "unsupported"
    elif status == "timeout":
        state = "unknown"
    else:
        state = "unknown"
    detail = _detail(alias=alias, returncode=returncode, output=output, duration_s=round(duration_s, 3))
    if status not in {"ok", "unsupported"}:
        detail["collector_status"] = status
    return HealthEvidence(dimension, source, state, float(now()), float(ttl_s), detail).as_dict(service_id=service_id)
