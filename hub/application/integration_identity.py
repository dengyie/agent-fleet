"""Authentication I/O port and compatibility imports for Fleet integrations."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from hub.domain.integration_identity import IntegrationIdentity, IntegrationIdentityError


@runtime_checkable
class IntegrationIdentityStore(Protocol):
    def authenticate(self, token: str) -> IntegrationIdentity | None: ...


__all__ = ["IntegrationIdentity", "IntegrationIdentityError", "IntegrationIdentityStore"]
