"""Hook capture adapter (Task 7).

Claude Code hooks (``--hooks``) deliver JSON payloads on stdin with
environment variables that must NEVER be forwarded into session events.  This
adapter:

- is enabled only when the capability manifest claims ``hooks``;
- maps the REAL hook shapes into bounded, schema-valid payload fields:

  - ``UserPromptSubmit`` -> ``user_message``: the ``prompt`` value becomes
    ``payload.text``;
  - ``PreToolUse`` -> ``tool_call``: ``tool_name`` / ``tool_use_id`` /
    ``tool_input`` map to ``tool_name`` / ``call_id`` / ``arguments``;
  - ``PostToolUse`` -> ``tool_result``: ``tool_name`` / ``tool_use_id`` /
    ``tool_response`` map to ``tool_name`` / ``call_id`` / ``result``;

- preserves meaningful tool content: the sanitized ``tool_input`` /
  ``tool_response`` (a bounded JSON serialization of the nested value, with
  env/path/credential-shaped keys stripped) IS kept, so a tool's real input /
  output is captured rather than silently dropped;
- NEVER copies the hook ``env`` / ``cwd`` / ``command`` / ``process_id`` /
  ``secret`` / ``token`` (top-level, or nested under a sensitive key) into the
  event; the hook process environment is never forwarded.

The output of :meth:`HookAdapter.normalize` is a validated candidate session
event.  The Bridge then redacts and sequences it.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone
from typing import Any, Mapping

from session_schema import (
    SCHEMA_VERSION,
    SessionValidationError,
    validate_event,
)

# Always-dropped raw fields even if present in the raw hook payload.  These
# would leak process environment, working dir, credentials, or PIDs.
_FORBIDDEN_RAW_FIELDS = frozenset({
    "env", "environment", "cwd", "pwd", "path", "proc", "pid", "process_id",
    "process_group_id", "token", "secret", "credential", "authorization",
    "api_key", "access_token", "password", "command", "argv", "args",
    "raw", "raw_output", "collector", "collector_output", "log",
    "permission_mode", "transcript_path", "session_id",
})

# Nested sensitive keys: when they appear INSIDE ``tool_input`` /
# ``tool_response`` they are dropped wholesale.  Domain content keys like
# ``command`` inside a Bash tool input are preserved (that IS the captured tool
# call); env/path/credential-shaped keys are not.
_FORBIDDEN_VALUE_KEYS = frozenset({
    "env", "environment", "cwd", "pwd", "path", "proc", "pid", "process_id",
    "token", "secret", "credential", "authorization", "api_key",
    "access_token", "password", "private_key", "raw", "raw_output",
    "collector", "collector_output", "gcp_service_account",
    "aws_credentials", "credential_file",
})

# Hook ``type`` -> session event kind.
_TYPE_TO_KIND = {
    "PreToolUse": "tool_call",
    "PostToolUse": "tool_result",
    "UserPromptSubmit": "user_message",
    "AssistantMessage": "assistant_message",
    "SessionStart": "session_start",
}

# Bounded per-kind source-text key hints.  ``tool_call``/``tool_result`` use
# their own field allowlist instead.
_TEXT_KEYS = {
    "user_message": ("prompt", "text", "content", "result"),
    "assistant_message": ("text", "content", "result"),
}

_MAX_FIELD = 262144               # mirror session_schema._MAX_TOOL_FIELD
_MAX_SERIALIZED = 48 << 10        # 48 KiB JSON serialization of tool bodies
_FIT_ATTEMPTS = 12                # bounded deterministic shrink loop


def _bound_text(value: Any, limit: int) -> str:
    """Bounded text; containers never leak as a stringified blob."""
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return str(value)[:limit]


def _first_text(value: Any, keys: tuple) -> str:
    if isinstance(value, str) and value:
        return value[:_MAX_FIELD]
    if isinstance(value, Mapping):
        for key in keys:
            v = value.get(key)
            if isinstance(v, str) and v:
                return v[:_MAX_FIELD]
    return ""


# Strict allowed character set for a hook-supplied opaque event id.  Only plain
# alphanumerics and separator-safe punctuation are accepted; JWT dots,
# base64/cookie-sensitive ``= + / %``, and secret-marker words are rejected so
# a hook-provided id can never carry a session-token/cookie/JWT value or shape
# onto a durable/public surface.
_HOOK_SAFE_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_:"
)
_HOOK_SAFE_ID_FORBIDDEN_MARKERS = (
    "token", "secret", "private", "key", "password", "bearer",
    "session", "cookie", "auth", "credential",
)


def _safe_event_id(value: Any, prefix: str) -> str:
    """A strict-allowlist bounded hook event id.

    A raw ``event_id`` is kept only when it is a bounded opaque shape
    (plain short id matching :func:`_is_hook_safe_id`); anything else —
    including JWT-, session-token-, cookie- or secret-shaped values — is
    replaced by a generated opaque id so it can never smuggle a token value
    into the durable record.
    """
    if _is_hook_safe_id(value):
        return value
    h = hashlib.sha256(secrets.token_bytes(16)).hexdigest()[:24]
    return f"{prefix}_{h}"


def _is_hook_safe_id(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if len(value) > 128 or value[0] in "._-" or value[-1] in "._-":
        return False
    if not all(c in _HOOK_SAFE_ID_CHARS for c in value):
        return False
    lowered = value.lower()
    return not any(m in lowered for m in _HOOK_SAFE_ID_FORBIDDEN_MARKERS)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _scrub(value: Any, _depth: int = 0) -> Any:
    """Recursively strip forbidden-shaped keys in nested tool values.

    A plain scalar is returned as-is; a Mapping has any key in
    ``_FORBIDDEN_VALUE_KEYS`` (case-insensitive) removed before recursing; a
    list/tuple is scrubbed element-wise.  Bounded work per call (depth cap and
    element cap) so one huge payload cannot inflate CPU/memory without bound.
    """
    if _depth > 6:
        return ""
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and k.lower() in _FORBIDDEN_VALUE_KEYS:
                continue
            out[str(k)[:256]] = _scrub(v, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_scrub(v, _depth + 1) for v in value[:64]]
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (str, int, float)):
        return value
    return ""


def _serialize(value: Any) -> str:
    """Deterministic bounded JSON serialization of a sanitized tool value."""
    try:
        text = json.dumps(
            _scrub(value), ensure_ascii=True, sort_keys=True,
            separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        return ""
    if len(text) > _MAX_SERIALIZED:
        text = text[:_MAX_SERIALIZED]
    return text


class HookAdapter:
    """Normalizes a raw hook JSON payload into a validated candidate event.

    The raw payload is filtered through ``_FORBIDDEN_RAW_FIELDS`` before any
    field is read, so the hook environment / cwd / secrets can never reach the
    event surface.
    """

    def __init__(self, manifest: Mapping[str, Any],
                 *, session_id: str = "",
                 machine_id: str = "",
                 stream_id: str = "",
                 agent_family: str = ""):
        self.enabled = bool(manifest.get("hooks"))
        self.session_id = session_id
        self.machine_id = machine_id
        self.stream_id = stream_id
        self.agent_family = agent_family or "claude"

    @property
    def adapter_name(self) -> str:
        return "hooks"

    def normalize(self, raw: Mapping[str, Any]) -> dict | None:
        """Return a validated candidate event (or ``None`` when disabled)."""
        if not self.enabled:
            return None
        if not isinstance(raw, Mapping):
            return None

        # STRIP all forbidden raw fields including the environment.  The
        # original ``env`` may be a dict with secrets; it must never survive.
        safe_raw = {
            str(k): v for k, v in raw.items()
            if str(k).lower() not in _FORBIDDEN_RAW_FIELDS
        }
        kind = _TYPE_TO_KIND.get(str(raw.get("type") or ""))
        if not kind:
            return None

        event: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "event_id": _safe_event_id(raw.get("event_id"), "hook"),
            "stream_id": self.stream_id or "hook_stream",
            "machine_id": self.machine_id or "local",
            "session_id": self.session_id or "session",
            "sequence": 1,  # the Bridge reassigns; a placeholder is fine
            "kind": kind,
            "capture_quality": "structured",
            "source": "hooks",
            "emitted_at": _iso_now(),
        }

        if kind == "session_start":
            payload: dict[str, Any] = {
                "agent_family": self.agent_family, "adapter": "hooks"}
        elif kind == "tool_call":
            payload = {
                "tool_name": _bound_text(safe_raw.get("tool_name"), 128),
                "call_id": _bound_text(
                    safe_raw.get("tool_use_id")
                    or safe_raw.get("call_id"), 128),
                "status": _bound_text(safe_raw.get("status"), 64),
                "arguments": _serialize_tool_body(
                    safe_raw.get("tool_input"),
                    safe_raw.get("input"),
                ),
                "result": "",
                "digest": "",
            }
        elif kind == "tool_result":
            payload = {
                "tool_name": _bound_text(safe_raw.get("tool_name"), 128),
                "call_id": _bound_text(
                    safe_raw.get("tool_use_id")
                    or safe_raw.get("call_id"), 128),
                "status": _bound_text(safe_raw.get("status"), 64),
                "arguments": "",
                "result": _serialize_tool_body(
                    safe_raw.get("tool_response"),
                    safe_raw.get("result"),
                ),
                "digest": "",
            }
        else:
            payload = {
                "text": _first_text(safe_raw, _TEXT_KEYS.get(kind, ("text",))),
                "is_complete": bool(raw.get("is_complete", True)),
            }

        event["payload"] = payload
        return self._fit(event, payload)

    def _fit(self, event: dict, payload: dict) -> dict | None:
        """Deterministically shrink payload text until the event fits.

        Returns ``None`` only when the candidate could not be made valid
        (bounded).  Never raises for a too-large event: the schema cap is
        enforced by truncation, not by an exception.
        """
        for _ in range(_FIT_ATTEMPTS):
            try:
                return validate_event(event)
            except SessionValidationError as exc:
                if exc.code != "event_too_large":
                    return None
            # shrink the LARGEST text field by half, most-impactful first
            largest = None
            largest_len = -1
            for key in ("result", "arguments", "text"):
                v = payload.get(key)
                if isinstance(v, str) and len(v) > largest_len:
                    largest_len = len(v)
                    largest = key
            if largest is None or largest_len <= 0:
                return None
            payload[largest] = payload[largest][: max(1, largest_len // 2)]
            event["payload"] = payload
        return None


def _serialize_tool_body(value: Any, fallback: Any) -> str:
    """Sanitize a nested ``tool_input``/``tool_response`` into bounded text."""
    if value is None:
        if fallback is None:
            return ""
        return _serialize(fallback)
    if isinstance(value, str):
        return value[:_MAX_SERIALIZED]
    return _serialize(value)


__all__ = ["HookAdapter"]