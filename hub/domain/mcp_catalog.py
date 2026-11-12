"""Immutable catalog persistence facts; no transport, framework or clock."""
from __future__ import annotations

from dataclasses import dataclass

MAX_CONNECTORS = 128
MAX_CATALOG_INTEGER = 9007199254740991


class MCPCatalogError(RuntimeError):
    def __init__(self, code: str, *, cause: BaseException | None = None) -> None:
        self.code = code
        super().__init__(code)
        if cause is not None:
            self.__cause__ = cause


@dataclass(frozen=True, slots=True)
class MCPCatalogRecord:
    connector_id: str
    config_snapshot: bytes
    config_digest: str
    active_snapshot: bytes | None
    pending_snapshot: bytes | None
    last_revision: int
    epoch: int
    created_at: float
    updated_at: float
    revoked_at: float | None
    restore_disabled: bool

    @property
    def disabled(self) -> bool:
        return self.revoked_at is not None or self.restore_disabled
