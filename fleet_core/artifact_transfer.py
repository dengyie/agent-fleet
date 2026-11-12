"""Pure validation and state values for the V2 artifact transfer protocol."""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Callable, Final


class ArtifactTransferError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


TRANSFER_STATES: Final = frozenset({
    "reserved", "uploading", "verifying", "publishing", "ready",
    "failed", "quarantined", "unknown",
})
TERMINAL_STATES: Final = frozenset({"ready", "failed", "quarantined", "unknown"})
EXPORT_COMMAND_ACTIONS: Final = frozenset({"artifact.export"})
HARD_MAX_BYTES: Final = 256 * 1024 * 1024
CHUNK_BYTES: Final = 1024 * 1024
SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
ID_RE: Final = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
NAME_RE: Final = re.compile(r"^[^/\\\x00-\x1f\x7f]{1,256}$")


@dataclass(frozen=True, slots=True)
class TransferLimits:
    default_size: int = 64 * 1024 * 1024
    max_size: int = 256 * 1024 * 1024
    chunk_size: int = 1024 * 1024
    max_parts: int = 256
    per_node_concurrency: int = 2
    global_concurrency: int = 4
    workspace_quota: int = 1024 * 1024 * 1024
    workflow_budget: int = 256 * 1024 * 1024
    reservation_ttl: float = 30 * 60
    absolute_ttl: float = 2 * 60 * 60

    def __post_init__(self) -> None:
        integer_fields = ("default_size", "max_size", "chunk_size", "max_parts", "per_node_concurrency", "global_concurrency", "workspace_quota", "workflow_budget")
        for field in integer_fields:
            value = getattr(self, field)
            if type(value) is not int or value <= 0:
                raise ArtifactTransferError("invalid_transfer_limits")
        if (self.default_size > self.max_size or self.max_size > HARD_MAX_BYTES
                or self.chunk_size != CHUNK_BYTES or self.max_parts != 256):
            raise ArtifactTransferError("invalid_transfer_limits")
        if self.max_parts * self.chunk_size < self.max_size:
            raise ArtifactTransferError("invalid_transfer_limits")
        for field in ("reservation_ttl", "absolute_ttl"):
            value = getattr(self, field)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ArtifactTransferError("invalid_transfer_limits")
        if self.absolute_ttl < self.reservation_ttl:
            raise ArtifactTransferError("invalid_transfer_limits")
        if self.absolute_ttl > 2 * 60 * 60:
            raise ArtifactTransferError("invalid_transfer_limits")


@dataclass(frozen=True, slots=True)
class TransferScope:
    command_id: str
    owner_id: str
    node_id: str
    workspace_id: str
    workflow_id: str
    step_key: str
    generation: int
    output_key: str

    def __post_init__(self) -> None:
        for value in (self.command_id, self.owner_id, self.node_id, self.workspace_id, self.workflow_id, self.step_key, self.output_key):
            if (not isinstance(value, str) or not value or len(value) > 128
                    or any(ord(char) < 0x20 or ord(char) == 0x7f for char in value)):
                raise ArtifactTransferError("invalid_transfer_scope")
        if type(self.generation) is not int or self.generation < 1:
            raise ArtifactTransferError("invalid_transfer_scope")


@dataclass(frozen=True, slots=True)
class TransferManifest:
    transfer_id: str
    artifact_id: str
    scope: TransferScope
    name: str
    size: int
    sha256: str
    state: str
    chunk_size: int
    expires_at: float

    def __post_init__(self) -> None:
        if not ID_RE.fullmatch(self.transfer_id or "") or not ID_RE.fullmatch(self.artifact_id or ""):
            raise ArtifactTransferError("invalid_transfer_id")
        if not isinstance(self.name, str) or not NAME_RE.fullmatch(self.name) or self.name in {".", ".."}:
            raise ArtifactTransferError("invalid_artifact_name")
        if type(self.size) is not int or self.size < 0:
            raise ArtifactTransferError("invalid_size")
        if not isinstance(self.sha256, str) or not SHA256_RE.fullmatch(self.sha256):
            raise ArtifactTransferError("invalid_hash")
        if self.state not in TRANSFER_STATES or type(self.chunk_size) is not int or self.chunk_size <= 0:
            raise ArtifactTransferError("invalid_transfer_state")
        if type(self.expires_at) not in (int, float) or not math.isfinite(self.expires_at) or self.expires_at < 0:
            raise ArtifactTransferError("invalid_transfer_state")


def validate_reservation(*, size: int, sha256: str, name: str, limits: TransferLimits) -> None:
    if type(size) is not int or size < 0:
        raise ArtifactTransferError("invalid_size")
    if size > limits.default_size:
        raise ArtifactTransferError("artifact_too_large")
    if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
        raise ArtifactTransferError("invalid_hash")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name) or name in {".", ".."}:
        raise ArtifactTransferError("invalid_artifact_name")


def validate_part_metadata(*, offset: int, length: int, total_size: int, chunk_size: int, max_parts: int) -> None:
    if type(offset) is not int or offset < 0 or offset % chunk_size:
        raise ArtifactTransferError("invalid_offset")
    if type(length) is not int:
        raise ArtifactTransferError("invalid_content_length")
    if length <= 0:
        raise ArtifactTransferError("invalid_part_size")
    if offset + length > total_size:
        raise ArtifactTransferError("part_out_of_bounds")
    if total_size > 0 and offset >= total_size:
        raise ArtifactTransferError("part_out_of_bounds")
    if length > chunk_size or (offset + length < total_size and length != chunk_size):
        raise ArtifactTransferError("invalid_part_size")
    if offset // chunk_size >= max_parts:
        raise ArtifactTransferError("too_many_parts")


def validate_part(*, offset: int, length: int, payload: bytes, declared_hash: str, total_size: int, chunk_size: int, max_parts: int, existing_parts: int) -> str:
    validate_part_metadata(offset=offset, length=length, total_size=total_size, chunk_size=chunk_size, max_parts=max_parts)
    if length != len(payload):
        raise ArtifactTransferError("invalid_content_length")
    if not isinstance(declared_hash, str) or not SHA256_RE.fullmatch(declared_hash):
        raise ArtifactTransferError("invalid_hash")
    actual = hashlib.sha256(payload).hexdigest()
    if actual != declared_hash:
        raise ArtifactTransferError("part_hash_mismatch")
    return actual


def validate_complete_parts(parts: list[tuple[int, int, str]], *, total_size: int, expected_hash: str, chunk_size: int, max_parts: int, read_part: Callable[[int], bytes]) -> None:
    if len(parts) > max_parts:
        raise ArtifactTransferError("too_many_parts")
    if total_size == 0:
        if parts:
            raise ArtifactTransferError("unexpected_parts")
        digest = hashlib.sha256(b"").hexdigest()
        if digest != expected_hash:
            raise ArtifactTransferError("artifact_hash_mismatch")
        return
    ordered = sorted(parts)
    cursor = 0
    digest = hashlib.sha256()
    for offset, length, part_hash in ordered:
        if offset != cursor or offset % chunk_size or length <= 0 or offset + length > total_size:
            raise ArtifactTransferError("missing_or_overlapping_part")
        payload = read_part(offset)
        if len(payload) != length or hashlib.sha256(payload).hexdigest() != part_hash:
            raise ArtifactTransferError("part_hash_mismatch")
        digest.update(payload)
        cursor += length
    if cursor != total_size:
        raise ArtifactTransferError("missing_or_overlapping_part")
    if digest.hexdigest() != expected_hash:
        raise ArtifactTransferError("artifact_hash_mismatch")


__all__ = ["ArtifactTransferError", "EXPORT_COMMAND_ACTIONS", "TERMINAL_STATES", "TRANSFER_STATES", "TransferLimits", "TransferManifest", "TransferScope", "validate_complete_parts", "validate_part", "validate_reservation"]
