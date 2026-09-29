"""Offline verifier for bounded, sanitized Komari JSON captures.

This module deliberately does not construct :class:`KomariClient` or import a
network requester.  It verifies a version-aware capture against the existing
Komari payload contract and exposes only normalized node snapshots.
"""
from __future__ import annotations

import errno
import json
import math
import os
import stat
import time
from pathlib import Path
from typing import Any, Mapping

from hub.integrations.komari import KomariClient, KomariError, normalize_nodes


CAPTURE_SCHEMA_VERSION = 1
MAX_CAPTURE_BYTES = KomariClient.MAX_BODY_BYTES
_ERROR_CODES = frozenset(
    {
        "invalid_capture",
        "unsupported_schema",
        "capture_too_large",
        "invalid_capture_path",
        "capture_read_failed",
    }
)


class KomariCaptureError(RuntimeError):
    """Stable, secret-free failure for offline capture verification."""

    def __init__(self, code: str):
        safe_code = code if isinstance(code, str) and code in _ERROR_CODES else "invalid_capture"
        self.code = safe_code
        super().__init__(safe_code)

    def __str__(self) -> str:
        return self.code


def _reject_json_constant(value: str) -> None:
    raise ValueError(value)


def _capture_timestamp(observed_at: float | None) -> float:
    value = time.time() if observed_at is None else observed_at
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError):
        raise KomariCaptureError("invalid_capture") from None
    if not math.isfinite(timestamp) or timestamp < 0:
        raise KomariCaptureError("invalid_capture")
    return timestamp


def _decode_capture(raw: bytes | bytearray) -> Any:
    if not isinstance(raw, (bytes, bytearray)):
        raise KomariCaptureError("invalid_capture")
    if len(raw) > MAX_CAPTURE_BYTES:
        raise KomariCaptureError("capture_too_large")
    try:
        return json.loads(
            bytes(raw).decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, TypeError, OverflowError, RecursionError):
        raise KomariCaptureError("invalid_capture") from None


def verify_komari_capture_bytes(
    raw: bytes | bytearray,
    *,
    observed_at: float | None = None,
) -> dict[str, Any]:
    """Verify one bounded versioned capture without opening a network socket."""
    payload = _decode_capture(raw)
    if not isinstance(payload, Mapping):
        raise KomariCaptureError("invalid_capture")
    if "schema_version" in payload:
        version = payload.get("schema_version")
    else:
        version = CAPTURE_SCHEMA_VERSION
    if type(version) is not int or version != CAPTURE_SCHEMA_VERSION:
        raise KomariCaptureError("unsupported_schema")

    timestamp = _capture_timestamp(observed_at)
    try:
        rows = normalize_nodes(payload, observed_at=timestamp)
    except KomariError as exc:
        code = "unsupported_schema" if exc.code == "unsupported_schema" else "invalid_capture"
        raise KomariCaptureError(code) from None
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise KomariCaptureError("invalid_capture") from None

    return {
        "ok": True,
        "schema_version": CAPTURE_SCHEMA_VERSION,
        "observed_at": timestamp,
        "node_count": len(rows),
        "nodes": [row.as_dict() for row in rows],
    }


def _open_capture_file(path: str | Path) -> bytes:
    if not isinstance(path, (str, Path)):
        raise KomariCaptureError("invalid_capture_path")
    try:
        capture_path = Path(path)
        path_stat = os.lstat(capture_path)
    except (FileNotFoundError, NotADirectoryError, ValueError):
        raise KomariCaptureError("invalid_capture_path") from None
    except OSError:
        raise KomariCaptureError("capture_read_failed") from None

    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise KomariCaptureError("invalid_capture_path")
    if path_stat.st_size > MAX_CAPTURE_BYTES:
        raise KomariCaptureError("capture_too_large")

    flags = os.O_RDONLY
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags |= nofollow
    try:
        descriptor = os.open(capture_path, flags)
    except (FileNotFoundError, NotADirectoryError, ValueError):
        raise KomariCaptureError("invalid_capture_path") from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise KomariCaptureError("invalid_capture_path") from None
        raise KomariCaptureError("capture_read_failed") from None

    try:
        with os.fdopen(descriptor, "rb") as handle:
            try:
                opened_stat = os.fstat(handle.fileno())
            except OSError:
                raise KomariCaptureError("capture_read_failed") from None
            if not stat.S_ISREG(opened_stat.st_mode):
                raise KomariCaptureError("invalid_capture_path")
            if opened_stat.st_size > MAX_CAPTURE_BYTES:
                raise KomariCaptureError("capture_too_large")
            try:
                raw = handle.read(MAX_CAPTURE_BYTES + 1)
            except (OSError, ValueError):
                raise KomariCaptureError("capture_read_failed") from None
    except KomariCaptureError:
        raise
    except (OSError, ValueError):
        raise KomariCaptureError("capture_read_failed") from None

    if len(raw) > MAX_CAPTURE_BYTES:
        raise KomariCaptureError("capture_too_large")
    return raw


def verify_komari_capture_file(
    path: str | Path,
    *,
    observed_at: float | None = None,
) -> dict[str, Any]:
    """Read and verify one non-symlink regular capture file."""
    return verify_komari_capture_bytes(
        _open_capture_file(path), observed_at=observed_at,
    )


__all__ = [
    "CAPTURE_SCHEMA_VERSION",
    "MAX_CAPTURE_BYTES",
    "KomariCaptureError",
    "verify_komari_capture_bytes",
    "verify_komari_capture_file",
]
