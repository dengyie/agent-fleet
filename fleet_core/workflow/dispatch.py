"""Immutable dispatch intent shared by capability and workflow contracts."""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DispatchIntent:
    token: str
    payload_hash: str
    target_key: str
    resolved_snapshot: bytes
