"""Bounded command envelope values for the platform node protocol."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from platform_schema import bounded_text, validate_id

COMMAND_STATES = frozenset({"queued", "leased", "accepted", "running", "succeeded", "failed", "unknown", "expired"})
TERMINAL_COMMAND_STATES = frozenset({"succeeded", "failed", "unknown", "expired"})
RECEIPT_STATES = frozenset({"accepted", "running", "succeeded", "failed", "unknown"})
RETRY_CLASSES = frozenset({"read_only", "idempotent_with_key", "reconcile_before_retry", "manual_only"})


class CommandValidationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def args_hash(arguments: Mapping[str, Any] | None) -> str:
    try:
        encoded = json.dumps(arguments or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        raise CommandValidationError("invalid_arguments") from None
    return hashlib.sha256(encoded).hexdigest()


def canonical_command_bytes(command: Mapping[str, Any]) -> bytes:
    # owner_id is Hub-local routing metadata and is deliberately redacted
    # from node responses. Keep it outside the signed wire envelope so a node
    # verifies exactly the bytes it received without learning tenant scope.
    # Only these fields are part of the node wire envelope. Repository
    # bookkeeping columns (status, lease, timestamps, attempt and result) may
    # be present in a decoded row but must never invalidate a valid signature.
    wire_fields = {
        "command_id", "target_node", "action", "resource_id", "arguments",
        "retry_class", "expires_at", "args_hash", "run_id", "grant_id",
    }
    body = {key: value for key, value in command.items() if key in wire_fields}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def sign_command(command: Mapping[str, Any], private_key: bytes) -> str:
    from hub.domain.control import sign_payload
    return sign_payload(private_key, canonical_command_bytes(command))


def verify_command(command: Mapping[str, Any], public_key: bytes) -> bool:
    from hub.domain.control import verify_payload
    signature = command.get("signature")
    if not isinstance(signature, str) or not signature:
        return False
    return bool(verify_payload(public_key, canonical_command_bytes(command), signature))


@dataclass(frozen=True)
class PlatformCommand:
    command_id: str
    target_node: str
    action: str
    resource_id: str
    arguments: dict[str, Any]
    retry_class: str
    expires_at: float
    args_digest: str
    run_id: str | None = None
    grant_id: str | None = None
    signature: str | None = None
    owner_id: str | None = None

    def __post_init__(self):
        try:
            validate_id(self.command_id, "command_id")
            validate_id(self.target_node, "target_node")
            validate_id(self.resource_id, "resource_id")
            bounded_text(self.action, "action", 120)
        except ValueError as exc:
            if isinstance(exc, CommandValidationError):
                raise
            raise CommandValidationError("invalid_command") from None
        if self.retry_class not in RETRY_CLASSES:
            raise CommandValidationError("invalid_retry_class")
        if not isinstance(self.expires_at, (int, float)) or self.expires_at <= 0:
            raise CommandValidationError("invalid_expiry")
        if args_hash(self.arguments) != self.args_digest:
            raise CommandValidationError("args_hash_mismatch")

    @classmethod
    def create(cls, *, command_id: str, target_node: str, action: str, resource_id: str, arguments: Mapping[str, Any] | None = None, retry_class: str = "read_only", expires_at: float, run_id: str | None = None, grant_id: str | None = None, signature: str | None = None, owner_id: str | None = None):
        args = dict(arguments or {})
        return cls(command_id=command_id, target_node=target_node, action=action, resource_id=resource_id, arguments=args, retry_class=retry_class, expires_at=float(expires_at), args_digest=args_hash(args), run_id=run_id, grant_id=grant_id, signature=signature, owner_id=owner_id)

    def as_dict(self) -> dict[str, Any]:
        return {"command_id": self.command_id, "target_node": self.target_node, "action": self.action, "resource_id": self.resource_id, "arguments": dict(self.arguments), "retry_class": self.retry_class, "expires_at": self.expires_at, "args_hash": self.args_digest, "run_id": self.run_id, "grant_id": self.grant_id, "signature": self.signature, "owner_id": self.owner_id}

    def signed(self, private_key: bytes) -> "PlatformCommand":
        """Return an immutable copy with an Ed25519 signature."""
        payload = self.as_dict()
        payload["signature"] = None
        signature = sign_command(payload, private_key)
        return PlatformCommand(
            command_id=self.command_id, target_node=self.target_node,
            action=self.action, resource_id=self.resource_id,
            arguments=dict(self.arguments), retry_class=self.retry_class,
            expires_at=self.expires_at, args_digest=self.args_digest,
            run_id=self.run_id, grant_id=self.grant_id, signature=signature,
            owner_id=self.owner_id,
        )


__all__ = ["COMMAND_STATES", "CommandValidationError", "PlatformCommand", "RECEIPT_STATES", "RETRY_CLASSES", "TERMINAL_COMMAND_STATES", "args_hash", "canonical_command_bytes", "sign_command", "verify_command"]
