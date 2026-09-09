"""Pure task-domain validation, state rules, and public DTOs.

This module deliberately contains no HTTP, persistence, or process concerns.
The dictionaries accepted and returned here are ordinary Python values so the
same rules can be used by Flask adapters, application services, and static API
contracts without importing a web framework.
"""

import re
from dataclasses import dataclass
from typing import Any, Mapping

from agent_profiles import EXECUTABLE_AGENT_TYPES
from session_schema import is_valid_session_id


MACHINE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DEFAULT_AGENT_TYPES = EXECUTABLE_AGENT_TYPES

TASK_FIELDS = (
    "task_id",
    "machine",
    "agent_type",
    "project",
    "state",
    "requested_by",
    "created_at",
    "expires_at",
    "instruction",
)
RESULT_FIELDS = (
    "exit_code", "log_summary", "diff_stat", "diff_patch", "test_summary",
    "duration_s", "finished_at",
)
GATE_FIELDS = ("kind", "state", "requested_by", "decided_by", "decided_at", "created_at")
MAX_INSTRUCTION = 2000
MAX_CLIENT_TOKEN = 128
MAX_LOG_SUMMARY = 10240
MAX_DIFF_STAT = 5120
MAX_DIFF_PATCH = 102400
MAX_TEST_SUMMARY = 20480

_TERMINAL_STATES = frozenset(("succeeded", "failed", "cancelled", "expired"))
_CANCELLABLE_STATES = frozenset(("queued", "leased", "running", "paused"))
_PAUSABLE_STATES = frozenset(("queued", "leased", "running"))


@dataclass(frozen=True)
class DomainError(ValueError):
    """Stable, bounded validation error for an application/HTTP adapter."""

    code: str
    detail: str

    def __post_init__(self):
        object.__setattr__(self, "detail", str(self.detail)[:200])
        super().__init__(self.detail)


def _invalid(code: str, detail: str) -> DomainError:
    return DomainError(code, detail)


def validate_task_input(
    data: Mapping[str, Any], *, agent_types=DEFAULT_AGENT_TYPES
) -> dict[str, Any]:
    """Validate and normalize the operator task fields.

    Only fields consumed by task creation are returned.  Project whitelist and
    machine reachability are deployment policy and remain application-service
    concerns; syntax and size limits live here so every adapter agrees.
    """
    if not isinstance(data, Mapping):
        raise _invalid("invalid_json", "请求体必须是 JSON 对象")

    machine = data.get("machine", "")
    if not isinstance(machine, str) or not MACHINE_RE.fullmatch(machine):
        raise _invalid("invalid_machine", "machine 名不合法")

    agent_type = data.get("agent_type", "")
    if agent_type not in tuple(agent_types):
        raise _invalid(
            "invalid_agent_type",
            "agent_type 必须是 " + "/".join(str(item) for item in agent_types),
        )

    project = data.get("project", "")
    if not isinstance(project, str) or not PROJECT_RE.fullmatch(project):
        raise _invalid("invalid_project", "project 名不合法")

    instruction = data.get("instruction", "")
    if not isinstance(instruction, str) or not (1 <= len(instruction) <= MAX_INSTRUCTION):
        raise _invalid("invalid_instruction", "instruction 长度须为 1-2000 字符")

    raw_token = data.get("client_token") or None
    if raw_token is not None and not (1 <= len(str(raw_token)) <= MAX_CLIENT_TOKEN):
        raise _invalid("invalid_client_token", "client_token 长度须为 1-128")

    result = {
        "machine": machine,
        "agent_type": agent_type,
        "project": project,
        "instruction": instruction,
    }
    if raw_token is not None:
        result["client_token"] = str(raw_token)
    confirm = data.get("confirm")
    if confirm is True or confirm == "true" or confirm == 1:
        result["confirm"] = True
    elif confirm in (None, False, "false", 0, ""):
        pass
    else:
        raise _invalid("invalid_confirm", "confirm 必须是布尔值")
    return result


def _bounded_text(value: Any, limit: int):
    if value is None:
        return value
    if isinstance(value, str):
        return value[:limit]
    return str(value)[:limit]


def public_result(result: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return only user-readable result fields, with defense-in-depth bounds."""
    if not result:
        return None
    out = {}
    for key in RESULT_FIELDS:
        if key not in result:
            continue
        value = result[key]
        if key == "log_summary":
            value = _bounded_text(value, MAX_LOG_SUMMARY)
        elif key == "diff_stat":
            value = _bounded_text(value, MAX_DIFF_STAT)
        elif key == "diff_patch":
            if not value:
                continue
            value = _bounded_text(value, MAX_DIFF_PATCH)
        elif key == "test_summary":
            if not isinstance(value, dict) or not value:
                continue
            value = {
                k: value[k]
                for k in ("framework", "passed", "failed", "skipped",
                          "errors", "duration_s", "failed_names")
                if k in value
            }
            if not value:
                continue
        out[key] = value
    return out


def public_task(task: Mapping[str, Any], with_result: bool = False) -> dict[str, Any]:
    """Return the legacy public task shape without lease/idempotency secrets.

    ``session_id`` is optional Phase 3 deep-link projection: included only when
    the caller already attached a valid opaque id. Absent or invalid values are
    omitted so older clients keep working.
    """
    if not isinstance(task, Mapping):
        return {}
    out = {key: task[key] for key in TASK_FIELDS if key in task}
    session_id = task.get("session_id")
    if isinstance(session_id, str) and is_valid_session_id(session_id):
        out["session_id"] = session_id
    if with_result:
        out["result"] = public_result(task.get("result"))
    gate = task.get("gate")
    if isinstance(gate, Mapping) and gate.get("state") in ("pending", "confirmed", "rejected"):
        out["gate"] = {key: gate[key] for key in GATE_FIELDS if key in gate}
    return out


def is_terminal_state(state: str) -> bool:
    return state in _TERMINAL_STATES


def can_cancel(state: str) -> bool:
    return state in _CANCELLABLE_STATES


def can_retry(state: str) -> bool:
    return is_terminal_state(state)


def can_pause(state: str) -> bool:
    return state in _PAUSABLE_STATES


def can_continue(state: str) -> bool:
    return state == "paused"
