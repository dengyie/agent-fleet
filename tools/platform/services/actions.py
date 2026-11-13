"""Fixed-argv executor for explicitly authorized service actions."""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Callable, Sequence

from .base import CollectorError, run_fixed_command


class ServiceActionError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


# These argv templates are owned by the adapter.  No command name or extra
# argument can be supplied by the model, operator, or command payload.
_ADAPTER_ACTIONS = {
    "systemd": {
        "inspect": lambda alias: ("systemctl", "is-active", "--", alias),
        "restart": lambda alias: ("systemctl", "restart", "--", alias),
    },
    "supervisor": {
        "inspect": lambda alias: ("supervisorctl", "status", alias),
        "restart": lambda alias: ("supervisorctl", "restart", alias),
    },
    "docker": {
        "inspect": lambda alias: ("docker", "inspect", "--format", "{{.State.Status}}", "--", alias),
        "restart": lambda alias: ("docker", "restart", "--", alias),
    },
}
_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@%+=-]{0,255}$")


def fixed_action_argv(adapter: Any, action: Any, alias: Any) -> tuple[str, ...]:
    if not isinstance(adapter, str) or not isinstance(action, str) or not isinstance(alias, str):
        raise ServiceActionError("invalid_action")
    factory = _ADAPTER_ACTIONS.get(adapter, {}).get(action)
    if factory is None:
        raise ServiceActionError("unsupported_action")
    if not _ALIAS_RE.fullmatch(alias):
        raise ServiceActionError("invalid_target_alias")
    return tuple(factory(alias))


def action_argv_from_command(command: Mapping[str, Any]) -> tuple[str, ...]:
    if not isinstance(command, Mapping):
        raise ServiceActionError("invalid_command")
    action_name = command.get("action")
    if action_name not in {"service.inspect", "service.restart", "reconcile.service.inspect"}:
        raise ServiceActionError("unsupported_action")
    arguments = command.get("arguments")
    if not isinstance(arguments, Mapping):
        raise ServiceActionError("invalid_arguments")
    # The Hub emits this exact shape. Reject extra keys so a forged or
    # accidentally widened command cannot smuggle argv-like values.
    if set(arguments) != {"adapter", "target_alias", "service_version", "action"}:
        raise ServiceActionError("invalid_arguments")
    action = "inspect" if action_name == "reconcile.service.inspect" else action_name.split(".", 1)[1]
    if arguments.get("action") != action or command.get("resource_id") is None:
        raise ServiceActionError("invalid_arguments")
    try:
        int(arguments.get("service_version"))
    except (TypeError, ValueError):
        raise ServiceActionError("invalid_arguments") from None
    return fixed_action_argv(arguments.get("adapter"), action, arguments.get("target_alias"))


def execute_service_action(command: Mapping[str, Any], *, runner: Callable[..., Any] | None = None,
                           timeout_s: float = 30.0, now=None,
                           path_prefix: Sequence[str | Any] | None = None) -> dict[str, Any]:
    """Execute a command using ``shell=False`` and bounded output.

    Any failed, unsupported, timed out, or interrupted process is surfaced as
    an exception so the existing NodeClient journal records the outcome as
    ``unknown`` until health evidence reconciles the service.
    """
    try:
        argv = action_argv_from_command(command)
        status, output, returncode, duration_s = run_fixed_command(
            argv, timeout_s=timeout_s, runner=runner, now=now or __import__("time").time,
            path_prefix=path_prefix,
        )
    except (CollectorError, ServiceActionError) as exc:
        raise ServiceActionError(getattr(exc, "code", "action_failed")) from None
    is_postcheck = command.get("action") == "reconcile.service.inspect"
    inspect = argv[1] in {"is-active", "status", "inspect"}
    if status != "ok":
        if is_postcheck and inspect:
            health_state = "unsupported" if status == "unsupported" else "unknown"
            return {"action": command["action"], "resource_id": command["resource_id"],
                    "state": health_state, "error_code": status,
                    "duration_s": round(float(duration_s), 3)}
        raise ServiceActionError("action_failed")
    if returncode != 0 and not (is_postcheck and inspect):
        raise ServiceActionError("action_failed")
    health_state = "healthy" if returncode == 0 else "unhealthy"
    return {
        "action": command["action"],
        "resource_id": command["resource_id"],
        "state": health_state if inspect else "succeeded",
        "health_state": health_state if inspect else "succeeded",
        "returncode": returncode,
        "duration_s": round(float(duration_s), 3),
        "output": str(output or "")[:256] if command["action"] == "service.inspect" else "",
    }


class ServiceActionExecutor:
    """Callable adapter for ``NodeClient(executor=...)``."""

    def __init__(self, *, runner=None, timeout_s=30.0, now=None, path_prefix=None):
        self.runner = runner
        self.timeout_s = timeout_s
        self.now = now
        self.path_prefix = path_prefix

    def __call__(self, command):
        return execute_service_action(
            command, runner=self.runner, timeout_s=self.timeout_s, now=self.now,
            path_prefix=self.path_prefix,
        )


__all__ = [
    "ServiceActionError", "ServiceActionExecutor", "action_argv_from_command",
    "execute_service_action", "fixed_action_argv",
]
