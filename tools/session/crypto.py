"""AEAD session crypto primitives for encrypted transcript-at-rest (Task 3).

Only :mod:`cryptography` primitives are used - AES-GCM (256-bit) with
explicit supplied associated data - no manual crypto.  ``AeadBox`` encrypts
with a fresh random 12-byte nonce and binds the nonce in the ciphertext so
``decrypt`` accepts exactly what ``encrypt`` produced for the same
associated data.

Security rules enforced here:

- ``AeadBox.decrypt`` raises :class:`cryptography.exceptions.InvalidTag` when
  the associated data is wrong or the ciphertext has been tampered with /
  truncated.  Errors are never swallowed.
- ``load_encryption_key`` reads ONLY the explicitly configured local key
  source.  When no key is configured (or the source is absent/wrong type) it
  returns ``None`` - a call that needs encryption without a key must fail
  closed, never fall back to plaintext.  No implicit file/network read.
- Key bytes never enter ``repr`` / ``str`` / exception text / logs or any
  report: the only exceptions surfaced from this module are
  ``cryptography.exceptions.InvalidTag`` and bounded ``ValueError``/
  ``TypeError`` whose messages never include key bytes.
"""

from __future__ import annotations

import base64
import secrets
from collections.abc import Mapping
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_LENGTH = 12
KEY_LENGTH = 32

# Explicit configuration fields this loader is allowed to source a key from.
# ``None`` means the caller's environment does not configure a raw encryption
# key -> fail closed.
_KEY_ATTRS = (
    "session_encryption_raw",      # raw 32-byte bytes or base64 of 32 bytes
    "session_encryption_key_src",   # dict {'raw': bytes|'b64': str} source
)


def _normalise_key(value: Any) -> bytes | None:
    """Return the 32 key bytes from a configured source, or ``None``."""
    if isinstance(value, bytes):
        return value if len(value) == KEY_LENGTH else None
    if isinstance(value, str) and value:
        try:
            decoded = base64.b64decode(value, validate=True)
        except (ValueError, TypeError):
            return None
        return decoded if len(decoded) == KEY_LENGTH else None
    if isinstance(value, dict):
        raw = value.get("raw")
        if isinstance(raw, bytes) and len(raw) == KEY_LENGTH:
            return raw
        b64 = value.get("b64")
        if isinstance(b64, str) and b64:
            try:
                decoded = base64.b64decode(b64, validate=True)
            except (ValueError, TypeError):
                return None
            if len(decoded) == KEY_LENGTH:
                return decoded
        return None
    return None


class AeadBox:
    """AES-256-GCM authenticated encryption box with explicit associated data.

    The key is stored only as a private instance attribute and never appears
    in ``repr``/``str`` (both descriptions are static).  The ciphertext layout
    is ``nonce (12 bytes) || ciphertext+tag (AESGCM)``.
    """

    def __init__(self, key: bytes):
        if not isinstance(key, bytes) or len(key) != KEY_LENGTH:
            raise ValueError("AeadBox key must be exactly 32 bytes")
        self._key = key

    def encrypt(self, plaintext: bytes, associated_data: bytes = b"") -> bytes:
        """Encrypt ``plaintext`` with a fresh random nonce.

        Returns ``nonce || ciphertext`` (AESGCM binds the tag).  The plaintext
        is never embedded in the output.
        """
        if not isinstance(plaintext, bytes):
            raise TypeError("plaintext must be bytes")
        if not isinstance(associated_data, bytes):
            raise TypeError("associated data must be bytes")
        nonce = secrets.token_bytes(NONCE_LENGTH)
        ciphertext = AESGCM(self._key).encrypt(nonce, plaintext,
                                               associated_data)
        return nonce + ciphertext

    def decrypt(self, ciphertext: bytes, associated_data: bytes = b"") -> bytes:
        """Decrypt ``nonce || ciphertext`` for the given associated data.

        Raises :class:`cryptography.exceptions.InvalidTag` (never swallowed)
        when the data was tampered with, truncated, or the wrong associated
        data is supplied.
        """
        if not isinstance(ciphertext, bytes):
            raise TypeError("ciphertext must be bytes")
        if not isinstance(associated_data, bytes):
            raise TypeError("associated data must be bytes")
        nonce = ciphertext[:NONCE_LENGTH]
        body = ciphertext[NONCE_LENGTH:]
        return AESGCM(self._key).decrypt(nonce, body, associated_data)

    def __repr__(self) -> str:
        return "<AeadBox: aes256-gcm>"

    def __str__(self) -> str:
        return "<AeadBox>"

    @classmethod
    def load_encryption_key(cls, config: Any) -> bytes | None:
        """Read the session encryption key from an explicitly configured
        local source (classmethod form of the module-level function).

        Returns ``None`` (fail closed) when no explicit key is configured or
        the configured value is not a recognised 32-byte source.
        """
        return load_encryption_key(config)


def load_encryption_key(config: Any) -> bytes | None:
    """Read the session encryption key from an explicitly configured local
    source.

    Returns ``None`` (fail closed) when no explicit key is configured, when
    the configured value is not a source this module recognises, or when the
    value does not decode to 32 key bytes.  No file/env/network source is read
    implicitly and no key bytes ever leave this function into logs.

    ``config`` may be an object with attributes (``FleetConfig``) or a plain
    ``Mapping``; the same explicit source names are consulted in both cases.
    """
    candidates = []
    if isinstance(config, Mapping):
        for attr in _KEY_ATTRS:
            candidates.append(config.get(attr))
    else:
        for attr in _KEY_ATTRS:
            if hasattr(config, attr):
                candidates.append(getattr(config, attr))
    for value in candidates:
        key = _normalise_key(value)
        if key is not None:
            return key
    return None


def encrypt_bounded(key: bytes | None, plaintext: bytes,
                    associated_data: bytes = b"") -> bytes | None:
    """Fail-closed encryption helper.

    Returns ``None`` immediately when no key is available - it never falls
    back to plaintext.  With a valid key it behaves exactly like
    :meth:`AeadBox.encrypt`.
    """
    if key is None or not isinstance(key, bytes) or len(key) != KEY_LENGTH:
        return None
    return AeadBox(key).encrypt(plaintext, associated_data)


__all__ = [
    "AeadBox",
    "KEY_LENGTH",
    "NONCE_LENGTH",
    "encrypt_bounded",
    "load_encryption_key",
]