"""hub/domain/control.py — control-command domain protocol (Task 9).

Pure domain values — no HTTP, persistence, or process concerns.  Everything a
Hub command issuer and an Agent-side verifier/executor agree on lives here:

* the fixed ``CONTROL_ACTIONS`` / ``CONTROL_STATES`` enums and the forbidden
  action set (spec §12.4 / §13);
* canonical JSON serialization (sort_keys + separators) so a signature computed
  by the Hub over the canonical bytes verifies on the Agent regardless of dict
  key insertion order;
* Ed25519 signature primitives over those canonical bytes;
* ``ControlTarget`` / ``ControlCommand`` value objects and the wire
  ``command_to_dict`` / ``command_from_dict`` mapping;
* ``validate_command`` — the single gate both the Agent control client and the
  Hub-replay path use: signature, action-set, machine/session/attempt scope,
  TTL expiry, clock skew, and nonce uniqueness.

No secret value is ever printed or persisted here.
"""
from __future__ import annotations

import base64
import json
import re
import secrets
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

# --------------------------------------------------------------------------- #
# Fixed enums (verbatim from spec §13)
# --------------------------------------------------------------------------- #

#: The ONLY control actions a Supervisor may execute. Everything else is an
#: ``unsupported_action`` rejection.  ``adopt`` / ``detach`` are the operator
#: adoption (纳管) commands.  ``append_user_turn`` / ``apply_local_profile``
#: are Phase 4/5 seams: they may be signed only when their issuance gates are
#: on, carry a bounded ``payload`` (text / local profile_id), and are NEVER
#: ``inject_stdin`` / credential push.
CONTROL_ACTIONS = frozenset({
    "pause_session",
    "resume_session",
    "terminate_session",
    "quarantine_session",
    "cancel_attempt",
    "adopt",
    "detach",
    "append_user_turn",
    "apply_local_profile",
})

#: Operator source-control tokens that require a bounded signed payload.
PAYLOAD_ACTIONS = frozenset({
    "append_user_turn",
    "apply_local_profile",
})

_MAX_TURN_TEXT = 2000
_MAX_PROFILE_ID = 128
_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PROFILE_ID_REJECT = ("/", "\\", "token", "secret", "private", "password",
                      "api_key", "apikey", "credential")

#: The actions the spec explicitly forbids (kept so tests + audits can assert
#: the fixed surface never grows into arbitrary control).
FORBIDDEN_ACTIONS = frozenset({
    "raw_cmd", "exec_shell", "write_file", "inject_stdin", "tmux_command",
    "arbitrary_signal", "change_credential", "change_policy",
})

#: Control-queue state machine. ``queued`` on the Hub is NEVER execution —
#: the terminal truth is the Supervisor receipt.
CONTROL_STATES = frozenset({
    "queued", "delivered", "accepted", "rejected", "executing",
    "succeeded", "already_finished", "failed", "expired",
})

#: Receipt statuses the Agent may report back. ``accepted``/``executing`` are
#: intermediate progress; the terminal truth is ``succeeded`` /
#: ``already_finished`` / ``failed`` / ``rejected``.
RECEIPT_STATUSES = frozenset({
    "accepted", "executing", "succeeded", "already_finished", "failed",
    "rejected",
})

#: Bounded reason strings the Hub itself may attach when it expires/supersedes.
INTERNAL_REASONS = frozenset({"expired", "superseded"})

_MAX_OPAQUE = 128
_REASON_MAX = 32
_NONCE_BYTES = 16

# Order the envelope fields appear on the wire (spec §13).  The signature is
# computed over everything except ``signature``.
_WIRE_FIELDS = (
    "command_id", "action", "target", "issued_at", "expires_at",
    "nonce", "reason_code", "signature",
)


# ---------------------------------------------------------------------------
# canonical JSON
# ---------------------------------------------------------------------------

def canonical_json_bytes(value: Any) -> bytes:
    """Deterministic compact JSON (sorted keys, ASCII-safe)."""
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")


def canonical_bytes(value: Any) -> bytes:
    """Alias kept for read symmetry."""
    return canonical_json_bytes(value)


def payload_bytes(command: Mapping[str, Any]) -> bytes:
    """Canonical bytes of the signable payload (signature field excluded)."""
    body = {k: v for k, v in command.items() if k != "signature"}
    return canonical_json_bytes(body)


# ---------------------------------------------------------------------------
# Ed25519 primitives (raw 32-byte keys, detached signature)
# ---------------------------------------------------------------------------

def generate_ed25519_keypair() -> tuple[bytes, bytes]:
    """Generate a ``(private_bytes, public_bytes)`` 32-byte raw keypair."""
    from cryptography.hazmat.primitives import serialization as _ser
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )

    priv = Ed25519PrivateKey.generate()
    priv_bytes = priv.private_bytes(
        _ser.Encoding.Raw, _ser.PrivateFormat.Raw, _ser.NoEncryption())
    pub_bytes = priv.public_key().public_bytes(
        _ser.Encoding.Raw, _ser.PublicFormat.Raw)
    return priv_bytes, pub_bytes


def sign_payload(private_bytes: bytes, payload: bytes) -> str:
    """Detached Ed25519 signature (base64) over ``payload``."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )

    priv = Ed25519PrivateKey.from_private_bytes(private_bytes)
    return base64.b64encode(priv.sign(payload)).decode("ascii")


def verify_payload(public_bytes: bytes, payload: bytes, signature: str) -> bool:
    """Verify a base64 detached signature. Returns False on ANY failure —
    never raises, so an attacker-controlled command can only ever yield False.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PublicKey,
    )

    if not isinstance(signature, str) or not signature:
        return False
    try:
        raw = base64.b64decode(signature, validate=True)
        if len(raw) != 64:
            return False
        pub = Ed25519PublicKey.from_public_bytes(bytes(public_bytes))
        pub.verify(raw, payload)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# command value objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ControlTarget:
    """Scope fields every command carries (spec §13)."""

    machine_id: str
    session_id: str
    attempt_id: str | None = None

    def as_dict(self) -> dict:
        out = {
            "machine_id": self.machine_id,
            "session_id": self.session_id,
        }
        if self.attempt_id:
            out["attempt_id"] = self.attempt_id
        return out

    @classmethod
    def from_dict(cls, raw: Any) -> "ControlTarget":
        if not isinstance(raw, dict):
            raise ValueError("target must be an object")
        machine_id = str(raw.get("machine_id") or "").strip()
        session_id = str(raw.get("session_id") or "").strip()
        attempt_id = raw.get("attempt_id")
        if not machine_id or not session_id:
            raise ValueError("target requires machine_id and session_id")
        if len(machine_id) > _MAX_OPAQUE or len(session_id) > _MAX_OPAQUE:
            raise ValueError("target ids too long")
        attempt = None
        if attempt_id:
            attempt = str(attempt_id)
            if len(attempt) > _MAX_OPAQUE:
                raise ValueError("attempt_id too long")
        return cls(
            machine_id=machine_id,
            session_id=session_id,
            attempt_id=attempt,
        )


@dataclass(frozen=True)
class ControlCommand:
    """A fully populated command envelope (spec §13)."""

    command_id: str
    action: str
    target: ControlTarget
    issued_at: str
    expires_at: str
    nonce: str
    reason_code: str = "operator_requested"
    signature: str = ""
    payload: dict | None = None

    def to_dict(self) -> dict:
        out = {
            "command_id": self.command_id,
            "action": self.action,
            "target": self.target.as_dict(),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
            "reason_code": self.reason_code,
            "signature": self.signature,
        }
        if self.payload is not None:
            out["payload"] = dict(self.payload)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ControlCommand":
        cmd_id = str(data.get("command_id") or "")
        action = str(data.get("action") or "")
        issued_at = str(data.get("issued_at") or "")
        expires_at = str(data.get("expires_at") or "")
        nonce = str(data.get("nonce") or "")
        reason_code = str(data.get("reason_code") or "operator_requested")[:32]
        signature = str(data.get("signature") or "")
        if not cmd_id:
            raise ValueError("command_id required")
        if action not in CONTROL_ACTIONS:
            raise ValueError("unsupported action")
        if len(cmd_id) > 128 or len(nonce) > 256:
            raise ValueError("command_id/nonce too long")
        bounded_payload = None
        if action in PAYLOAD_ACTIONS:
            bounded_payload = validated_control_payload(
                action, data.get("payload"))
        elif data.get("payload") is not None:
            raise ValueError("invalid_payload")
        return cls(
            command_id=cmd_id,
            action=action,
            target=ControlTarget.from_dict(data.get("target")),
            issued_at=issued_at,
            expires_at=expires_at,
            nonce=nonce,
            reason_code=reason_code,
            signature=signature,
            payload=bounded_payload,
        )


def command_to_dict(command: ControlCommand) -> dict:
    return command.to_dict()


def command_from_dict(data: Mapping[str, Any]) -> ControlCommand:
    return ControlCommand.from_dict(dict(data))


def new_command_id() -> str:
    return f"cmd_{secrets.token_hex(16)}"


def new_nonce() -> str:
    return secrets.token_hex(_NONCE_BYTES)


def validated_control_payload(action: str, payload: Any) -> dict:
    """Return the bounded signed payload for a Phase 4/5 action.

    Exact allowlist: ``append_user_turn`` may carry only ``text``;
    ``apply_local_profile`` may carry only ``profile_id``.  Extra keys
    (cwd / family / api_key / argv / pid) are refused, never copied or
    stripped.  Raises ``ValueError`` with a bounded code.
    """
    if action not in PAYLOAD_ACTIONS:
        raise ValueError("unsupported_action")
    if not isinstance(payload, Mapping):
        raise ValueError("invalid_payload")
    keys = set(payload)
    if action == "append_user_turn":
        if keys != {"text"}:
            raise ValueError("invalid_payload")
        text = payload.get("text")
        if not isinstance(text, str) or not (1 <= len(text) <= _MAX_TURN_TEXT):
            raise ValueError("invalid_payload")
        return {"text": text}
    if keys != {"profile_id"}:
        raise ValueError("invalid_payload")
    profile_id = payload.get("profile_id")
    if not isinstance(profile_id, str) or not profile_id:
        raise ValueError("invalid_payload")
    if len(profile_id) > _MAX_PROFILE_ID:
        raise ValueError("invalid_payload")
    lowered = profile_id.lower()
    if any(marker in lowered for marker in _PROFILE_ID_REJECT):
        raise ValueError("invalid_payload")
    if _PROFILE_ID_RE.fullmatch(profile_id) is None:
        raise ValueError("invalid_payload")
    return {"profile_id": profile_id}


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def _parse_ts(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        from datetime import datetime
        return (
            datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            if value.endswith("Z")
            else datetime.fromisoformat(value).timestamp()
        )
    except (ValueError, TypeError, OverflowError):
        return None


def validate_command(
    command: Mapping[str, Any],
    *,
    public_key: bytes,
    expected_machine: str,
    now: float,
    max_clock_skew_s: float = 300.0,
    nonce_used: Callable[[str], bool],
) -> tuple[bool, str]:
    """The Agent-side command gate; returns ``(ok, bounded_code)``.

    Fail-closed: any missing/unknown/non-verifying signature, wrong machine,
    expired/far-future command, replayed nonce, or action outside the fixed
    set produces ``(False, <bounded code>)`` that can never be executed.
    """
    if not isinstance(command, dict):
        return False, "malformed_command"

    action = command.get("action")
    if isinstance(action, str):
        if action in FORBIDDEN_ACTIONS:
            return False, "unsupported_action"
        if action not in CONTROL_ACTIONS:
            return False, "unsupported_action"
    else:
        return False, "unsupported_action"

    target = command.get("target")
    if not isinstance(target, dict):
        return False, "scope_mismatch"
    machine_id = target.get("machine_id")
    if machine_id != expected_machine:
        return False, "scope_mismatch"

    sig = command.get("signature")
    if not isinstance(sig, str) or not sig:
        return False, "malformed_signature"

    # verify the signature before trusting any timestamp/nonce field
    try:
        pub_bytes = bytes(public_key)
    except (TypeError, ValueError):
        return False, "invalid_public_key"
    if not verify_payload(pub_bytes, payload_bytes(command), sig):
        return False, "invalid_signature"

    now = float(now)
    issued = _parse_ts(command.get("issued_at"))
    expires = _parse_ts(command.get("expires_at"))
    if issued is None or expires is None:
        return False, "malformed_timestamp"
    skew = abs(now - issued)
    if skew > max_clock_skew_s:
        return False, "clock_skew"
    if now > expires:
        return False, "expired"

    nonce = command.get("nonce")
    if not isinstance(nonce, str) or not nonce:
        return False, "malformed_nonce"
    if isinstance(nonce_used, Callable) and nonce_used(nonce):
        return False, "nonce_replay"
    if action in PAYLOAD_ACTIONS:
        try:
            validated_control_payload(action, command.get("payload"))
        except ValueError:
            return False, "invalid_payload"
    return True, "allowed"


def _signing_key_check(public_key: Any) -> bool:
    try:
        return isinstance(bytes(public_key), bytes) and len(bytes(public_key)) == 32
    except (TypeError, ValueError):
        return False


__all__ = [
    "CONTROL_ACTIONS",
    "CONTROL_STATES",
    "FORBIDDEN_ACTIONS",
    "PAYLOAD_ACTIONS",
    "RECEIPT_STATUSES",
    "ControlCommand",
    "ControlTarget",
    "canonical_bytes",
    "canonical_json_bytes",
    "command_from_dict",
    "command_to_dict",
    "generate_ed25519_keypair",
    "new_command_id",
    "new_nonce",
    "payload_bytes",
    "sign_payload",
    "validate_command",
    "validated_control_payload",
    "verify_payload",
]