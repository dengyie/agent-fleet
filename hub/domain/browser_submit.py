"""Pure request/response contracts for owner-issued browser approvals."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import re
from typing import Any, Protocol


SENSITIVE_MARKERS = ('password', 'passwd', 'token', 'secret', 'credential', 'cookie', 'authorization', 'api_key')
APPROVAL_TTL_S = 300.0


class SubmitApprovalError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def submit_selector(value: object) -> str:
    if not isinstance(value, str) or not value or re.search(r'[\x00-\x1f\x7f]', value):
        raise SubmitApprovalError('invalid_selector')
    try:
        size = len(value.encode('utf-8'))
    except UnicodeError as exc:
        raise SubmitApprovalError('invalid_selector') from exc
    if size > 512:
        raise SubmitApprovalError('invalid_selector')
    if any(marker in value.lower() for marker in SENSITIVE_MARKERS):
        raise SubmitApprovalError('sensitive_field_forbidden')
    return value


def opaque_browser_id(value: object, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", value):
        raise SubmitApprovalError("invalid_" + field)
    return value


def utc_timestamp(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SubmitApprovalError('invalid_timestamp')
    return float(value)


@dataclass(frozen=True)
class SubmitApprovalRequest:
    session_id: str
    selector: str

    @classmethod
    def parse(cls, body: object) -> SubmitApprovalRequest:
        if not isinstance(body, Mapping) or set(body) != {'session_id', 'selector'}:
            raise SubmitApprovalError('invalid_arguments')
        try:
            session = opaque_browser_id(body['session_id'], 'session')
        except ValueError as exc:
            raise SubmitApprovalError('invalid_session') from exc
        return cls(session, submit_selector(body['selector']))


@dataclass(frozen=True)
class SubmitApprovalResponse:
    approval_id: str
    run_id: str
    session_id: str
    state: str
    granted_at: float
    expires_at: float
    consumed_at: float | None
    consumed_command_id: str | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> SubmitApprovalResponse:
        return cls(**{name: row[name] for name in cls.__dataclass_fields__})

    def public(self) -> dict[str, Any]:
        result = {name: getattr(self, name) for name in self.__dataclass_fields__}
        for name in ('granted_at', 'expires_at', 'consumed_at'):
            value = result[name]
            result[name] = datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z') if value is not None else None
        return result


class SubmitApprovalStore(Protocol):
    """Persistence port; transaction handles are owned by the storage adapter."""

    def grant_submit_approval(self, owner_id: str, *, workspace_id: str, run_id: str,
                              node_id: str, session_id: str, selector: str,
                              idempotency_key: str | None = None,
                              now: float | None = None) -> dict[str, Any]: ...

    def consume_submit_approval(self, owner_id: str, run_id: str, *, session_id: str,
                                selector: str, command_id: str, now: float | None = None,
                                workspace_id: str | None = None, node_id: str | None = None,
                                connection: Any = None) -> str: ...


    def get_session(self, owner_id: str, session_id: str) -> dict[str, Any] | None: ...

    def revoke_submit_approval(self, owner_id: str, approval_id: str,
                               *, run_id: str | None = None) -> dict[str, Any]: ...

    def get_submit_approval(self, owner_id: str, approval_id: str) -> dict[str, Any] | None: ...


class RunLookup(Protocol):
    def get_run(self, owner_id: str, run_id: str) -> dict[str, Any] | None: ...
