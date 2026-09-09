"""Best-effort PTY capture adapter (Task 7).

A PTY byte stream (e.g. an interactive CLI's terminal output captured with
``script`` or ``tmux``) is inherently lossy and unstructured.  This adapter is
explicitly best-effort:

- it is enabled only when the capability manifest claims ``pty``;
- ``CAPTURE_QUALITY`` is ``best_effort`` — the only legal quality for PTY
  output (matches the frozen model: non-managed capture is best-effort and
  the only legal non-managed quality);
- ``PtyAdapter.normalize`` converts raw bytes/text into a validated
  ``assistant_message``-shaped event with a bounded, control-stripped payload;
- the whole SERIALIZED event (envelope + payload) stays at or below the shared
  ``MAX_EVENT_BYTES`` cap; ``normalize`` never raises for a chunk at its
  declared bound — it deterministically shrinks the payload text until the
  event fits, and returns ``None`` only when nothing can fit;
- it never runs a subprocess, never opens inbound connections, and NEVER
  consults the caller's environment or working directory.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Any, Mapping

from session_schema import (
    MAX_EVENT_BYTES,
    SCHEMA_VERSION,
    SessionValidationError,
    validate_event,
)

CAPTURE_QUALITY = "best_effort"
# A single PTY frame's raw byte budget.  The SCHEMA cap governs the whole
# serialized event (envelope + payload); ``_fit`` deterministically shrinks the
# payload until the serialized event fits, so a chunk this size can never
# produce a too-large event and never makes ``normalize`` raise.
MAX_PTY_FRAME = 48 << 10        # 48 KiB raw text per call (fits under 64 KiB)
_STRIP_LIMIT = 1 << 20          # sanitized text bound before fit
_FIT_ATTEMPTS = 12              # bounded deterministic shrink iterations


class PtyAdapter:
    """Normalizes raw PTY chunks into bounded best-effort events."""

    def __init__(self, manifest: Mapping[str, Any],
                 *, session_id: str = "",
                 machine_id: str = "",
                 stream_id: str = "",
                 agent_family: str = ""):
        self.enabled = bool(manifest.get("pty"))
        self.session_id = session_id
        self.machine_id = machine_id
        self.stream_id = stream_id
        self.agent_family = agent_family or "claude"

    @property
    def adapter_name(self) -> str:
        return "pty"

    @property
    def quality(self) -> str:
        return CAPTURE_QUALITY

    def normalize(self, chunk) -> dict | None:
        """Return a validated best-effort event or ``None`` when disabled.

        A chunk at ``MAX_PTY_FRAME`` never raises: the sanitized text is
        capped at ``_STRIP_LIMIT``, then the event is run through ``_fit``
        which deterministically shrinks the payload until the whole serialized
        event fits ``MAX_EVENT_BYTES``.  ``None`` is returned only for a
        disabled adapter, an empty/whitespace/control-only chunk, or a
        pathological case where even a one-char payload cannot be validated.
        """
        if not self.enabled:
            return None
        if chunk is None:
            return None
        if isinstance(chunk, (bytes, bytearray)):
            data = bytes(chunk)[:MAX_PTY_FRAME]
            text = data.decode("utf-8", errors="replace")
        else:
            text = str(chunk)[:MAX_PTY_FRAME]
        text = _strip_control(text).strip()[:_STRIP_LIMIT]
        if not text:
            return None

        event: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "event_id": _opaque_event_id("pty"),
            "stream_id": self.stream_id or "pty_stream",
            "machine_id": self.machine_id or "local",
            "session_id": self.session_id or "session",
            "sequence": 1,  # the Bridge reassigns; a placeholder is fine
            "kind": "assistant_message",  # PTY text is rendered output
            "capture_quality": CAPTURE_QUALITY,
            "source": "pty",
            "emitted_at": _iso_now(),
            "payload": {"text": text, "is_complete": True},
        }
        return self._fit(event)

    def _fit(self, event: dict) -> dict | None:
        """Deterministically shrink payload text until the event fits."""
        for _ in range(_FIT_ATTEMPTS):
            try:
                return validate_event(event)
            except SessionValidationError as exc:
                if exc.code != "event_too_large":
                    return None
            text = event["payload"].get("text")
            if not isinstance(text, str) or len(text) <= 1:
                return None
            event["payload"]["text"] = text[: len(text) // 2]
        return None


def _strip_control(text: str) -> str:
    """Drop ANSI escape sequences and other control bytes (best-effort)."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        o = ord(text[i])
        if o == 27:  # ESC
            # skip a plausible escape sequence but stay bounded
            i += 1
            if i < n and text[i] in "()[]":
                i += 1
            j = i
            while j < n and j < i + 32:
                if _is_letter(ord(text[j])):
                    i = j + 1
                    break
                j += 1
            else:
                i = j
            continue
        if o < 32 or o == 127:  # control chars
            i += 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _is_letter(o: int) -> bool:
    return (65 <= o <= 90) or (97 <= o <= 122)


def _opaque_event_id(prefix: str) -> str:
    h = hashlib.sha256(secrets.token_bytes(16)).hexdigest()[:24]
    return f"{prefix}_{h}"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# test ergonomics alias
PtyCapture = PtyAdapter

__all__ = ["CAPTURE_QUALITY", "MAX_EVENT_BYTES", "MAX_PTY_FRAME",
           "PtyAdapter", "PtyCapture"]