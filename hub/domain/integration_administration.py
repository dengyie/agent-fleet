"""Deterministic Fleet identity administration contracts, without credentials in DTOs."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from platform_schema import validate_owner_id
from .integration_identity import IntegrationIdentity, IntegrationIdentityError, parse_integration_identity
from .workflow_errors import WorkflowValidationError
from .workflow_identity import canonical_json, validate_reference

IntegrationIdentityState = Literal["active", "revoked", "restore_disabled"]


@dataclass(frozen=True, slots=True)
class IntegrationIdentityKey:
    owner_id: str
    integration_id: str

    def __post_init__(self) -> None:
        try:
            validate_reference(self.owner_id)
            validate_reference(self.integration_id)
        except WorkflowValidationError as exc:
            raise IntegrationIdentityError("invalid_identity_reference") from exc


def identity_page(after: IntegrationIdentityKey | None, limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= 8 or (after is not None and type(after) is not IntegrationIdentityKey):
        raise IntegrationIdentityError("invalid_identity_page")


@dataclass(frozen=True, slots=True)
class IntegrationRevokeCommand:
    expected_state: Literal["active"]

    @classmethod
    def parse(cls, payload: object) -> IntegrationRevokeCommand:
        if type(payload) is not dict or payload.keys() - {"expected_state"}:
            raise IntegrationIdentityError("invalid_identity_command")
        if "expected_state" not in payload:
            raise IntegrationIdentityError("precondition_required")
        if type(payload["expected_state"]) is not str or payload["expected_state"] != "active":
            raise IntegrationIdentityError("invalid_identity_command")
        return cls("active")


@dataclass(frozen=True, slots=True)
class IntegrationRegisterCommand:
    identity: IntegrationIdentity = field(repr=False)

    @classmethod
    def parse(cls, payload: object) -> IntegrationRegisterCommand:
        try:
            if type(payload) is not dict or set(payload) != {"identity"}:
                raise ValueError("invalid registration command")
            identity = parse_integration_identity(canonical_json(payload["identity"]))
            if validate_owner_id(identity.owner_id) != identity.owner_id:
                raise ValueError("invalid registration owner")
            return cls(identity)
        except (ValueError, TypeError) as exc:
            raise IntegrationIdentityError("invalid_identity_registration") from exc


@dataclass(frozen=True, slots=True)
class IntegrationIdentityRecord:
    identity: IntegrationIdentity = field(repr=False)
    created_at: float
    revoked_at: float | None
    restore_disabled: bool

    @property
    def state(self) -> IntegrationIdentityState:
        if self.restore_disabled:
            return "restore_disabled"
        return "revoked" if self.revoked_at is not None else "active"


@dataclass(frozen=True, slots=True)
class IntegrationIdentitySummary:
    owner_id: str
    integration_id: str
    scopes: tuple[str, ...]
    template_count: int
    workspace_count: int
    max_artifacts: int
    max_budget_tokens: int
    max_budget_requests: int
    state: IntegrationIdentityState
    created_at: str
    revoked_at: str | None


@dataclass(frozen=True, slots=True)
class IntegrationIdentityDetail:
    summary: IntegrationIdentitySummary
    template_ids: tuple[str, ...]
    workspace_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IntegrationIdentityPage:
    identities: tuple[IntegrationIdentitySummary, ...]
    next_cursor: IntegrationIdentityKey | None


@dataclass(frozen=True, slots=True)
class IntegrationRevocation:
    owner_id: str
    integration_id: str
    state: Literal["revoked"] = "revoked"
