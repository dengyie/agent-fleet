"""Public projections and reviewed revocation over the original Fleet authority."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Literal, Protocol

from hub.domain.integration_administration import (
    IntegrationIdentityDetail, IntegrationIdentityKey, IntegrationIdentityPage, IntegrationIdentityRecord,
    IntegrationIdentitySummary, IntegrationRevocation, IntegrationRevokeCommand, identity_page,
)
from hub.domain.integration_identity import IntegrationIdentity, IntegrationIdentityError


class IntegrationIdentityAdminStore(Protocol):
    def list_page(self, after: IntegrationIdentityKey | None = None, limit: int = 8) -> tuple[tuple[IntegrationIdentityRecord, ...], IntegrationIdentityKey | None]: ...
    def get_record(self, owner: str, integration_id: str) -> IntegrationIdentityRecord: ...
    def revoke(self, owner: str, integration_id: str, *, expected_digest: str, now: float,
               expected_state: Literal["active"] | None = None) -> IntegrationIdentity: ...


def _utc(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _summary(record: IntegrationIdentityRecord) -> IntegrationIdentitySummary:
    identity = record.identity
    return IntegrationIdentitySummary(identity.owner_id, identity.integration_id, tuple(sorted(identity.scopes)),
        len(identity.template_ids), len(identity.workspace_ids), identity.max_artifacts, identity.max_budget_tokens,
        identity.max_budget_requests, record.state, _utc(record.created_at),
        _utc(record.revoked_at) if record.revoked_at is not None else None)


class IntegrationIdentityAdminService:
    def __init__(self, store: IntegrationIdentityAdminStore, *, clock: Callable[[], float]) -> None:
        self.store = store
        self.clock = clock

    def list_page(self, after: IntegrationIdentityKey | None = None, limit: int = 8) -> IntegrationIdentityPage:
        identity_page(after, limit)
        records, cursor = self.store.list_page(after, limit)
        return IntegrationIdentityPage(tuple(_summary(record) for record in records), cursor)

    def detail(self, owner: str, integration_id: str) -> IntegrationIdentityDetail:
        IntegrationIdentityKey(owner, integration_id)
        record = self.store.get_record(owner, integration_id)
        return IntegrationIdentityDetail(_summary(record), tuple(sorted(record.identity.template_ids)),
            tuple(sorted(record.identity.workspace_ids)))

    def revoke(self, owner: str, integration_id: str, command: IntegrationRevokeCommand) -> IntegrationRevocation:
        IntegrationIdentityKey(owner, integration_id)
        if type(command) is not IntegrationRevokeCommand:
            raise IntegrationIdentityError("invalid_identity_command")
        command = IntegrationRevokeCommand.parse({"expected_state": command.expected_state})
        record = self.store.get_record(owner, integration_id)
        identity = self.store.revoke(owner, integration_id, expected_digest=record.identity.definition_digest,
            expected_state=command.expected_state, now=self.clock())
        return IntegrationRevocation(identity.owner_id, identity.integration_id)
