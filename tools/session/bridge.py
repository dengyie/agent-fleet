"""Session Bridge — verified transcript sources into the encrypted spool (Task 7).

``SessionBridge.open(manifest, session_config) -> SessionBridge`` wires the
capability-manifest-driven source adapters on the Agent side and, for every
bounded batch, runs:

    source read -> normalize -> redact -> append -> upload

Design rules enforced here (see the Task 7 brief):

- Capability manifests are the SOLE source of truth for which adapter path is
  allowed.  CLI names, versions, or arbitrary remote commands never select or
  construct an adapter.  Hermes has no structured/spawn claim, so no Hermes
  structured spawn is ever assumed.
- Managed/unmanaged semantics are authoritative: an unmanaged session reports
  ``managed=False``, ``capture_quality='best_effort'``,
  ``control_capability='unavailable'`` and may only ever capture best-effort.
- Redaction (:mod:`tools.session.redact`) runs BEFORE the spool append.  The
  spool is encrypted, append-only, quota-bounded (Task 4).
- Uploads go through the injected :class:`SessionUploader` / ``post_json``
  callable and honor the Task 6 response contract
  (``accepted_through`` / ``next_cursor`` / bounded ``rejected``).  The bridge
  never invents a second HTTP protocol.
- Every source failure is isolated into a bounded ``capture_gap`` /
  ``capture_quality_changed`` event.  Raw exception text, paths, secrets, and
  unbounded source output never appear in the events, spool, or diagnostics.
- Malformed records affect only themselves (a parse failure never drops the
  next valid record).
- No subprocess / inbound connections / reverse connections are introduced.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import session_schema
from session_schema import (
    EVENT_KINDS,
    SCHEMA_VERSION,
    normalize_managed,
    validate_event,
    validate_quality_transition,
)
from tools.session.capture import pty as _pty
from tools.session.capture.native_jsonl import tailer as _tailer
from tools.session.capture.structured_stream import iter_json as _iter_json
from tools.session.redact import Redactor
from tools.session.spool import LocalSpool, SpoolError
from tools.session.uploader import SessionUploader

DEFAULT_STREAM = "stream"
DEFAULT_MACHINE = "host"
DEFAULT_SESSION = "session"

# Adapter capability flag -> source name.  ONLY these manifest flags grant a
# source; nothing else (CLI names, versions, help text) does.
_SELECTOR = {
    "structured_stream": "structured_stream",
    "native_transcript": "native_transcript",
    "hooks": "hooks",
    "pty": "pty",
}

# Allowlisted bounded reason tokens.  Anything else maps to
# ``source_unavailable`` — raw exception text/paths never enter events.
_BOUNDED_REASONS = frozenset({
    "source_unavailable", "source_overflow", "source_disconnected",
    "structured_unavailable", "native_ended", "hook_stopped",
    "timeout", "shutdown", "spool_quota",
})

_TYPE_TO_KIND = {
    "PreToolUse": "tool_call",
    "PostToolUse": "tool_result",
    "UserPromptSubmit": "user_message",
    "AssistantMessage": "assistant_message",
    "SessionStart": "session_start",
}

# Control/quality events legitimately carry a higher ``capture_quality`` (the
# graded state before the drop is recorded).  They are NOT captured content, so
# the unmanaged best-effort clamp never applies to them.
_CLAMP_EXEMPT = frozenset({"capture_gap", "capture_quality_changed"})


def _native_message_text(content: Any) -> str:
    """Flatten one native transcript ``message.content`` to bounded text.

    String content passes through bounded; a block list keeps only the
    ``text`` blocks (thinking blocks and tool payloads are separate concerns
    mapped below, never flattened into a message body).
    """
    if isinstance(content, str):
        return content[:_MAX_TEXT]
    if isinstance(content, list):
        parts = []
        for block in content:
            if (isinstance(block, Mapping) and block.get("type") == "text"
                    and isinstance(block.get("text"), str) and block["text"]):
                parts.append(block["text"])
        return "\n".join(parts)[:_MAX_TEXT]
    return ""


_MAX_TEXT = 262144
_MAX_TOOL_FIELD = 262144


def _shape_native_row(raw: Mapping) -> list[tuple[str, Mapping]]:
    """Map a native transcript JSONL row to a list of ``(kind, payload_source)``.

    claude_code transcripts carry lowercase ``user``/``assistant`` rows whose
    ``message.content`` is a string (user prompt) or a block list
    (``text`` / ``thinking`` / ``tool_use`` / ``tool_result``).  ``thinking``
    blocks are dropped (internal reasoning, not an observable surface); the
    message payload keeps only text blocks.  Tool blocks map onto the bounded
    ``tool_call``/``tool_result`` payload allowlist — the nested JSON of a
    tool's input/result IS preserved (bounded) so real tool activity is
    captured, matching the hook adapter's behavior.  Unknown row types
    (attachment, file-history-snapshot, queue-operation, ...) return ``[]``.

    A row with several mapped blocks yields one event per block in order
    (a text-only claude row stays a single message event; claude rarely
    interleaves text and tool_use in one row, but when it does every block
    is now captured rather than only the first tool block).
    """
    rtype = str(raw.get("type") or "")
    if rtype in ("event_msg", "response_item"):
        # codex rollout rows (type/payload wrapper) — see _shape_codex_row.
        return _shape_codex_row(raw)
    if rtype == "message":
        # pi transcript rows (role/message envelope) — see _shape_pi_row.
        return _shape_pi_row(raw)
    if rtype not in ("user", "assistant"):
        return []
    message = raw.get("message")
    if not isinstance(message, Mapping):
        return []
    content = message.get("content")
    kind = "user_message" if rtype == "user" else "assistant_message"
    shaped: list[tuple[str, Mapping]] = []
    # tool_use / tool_result blocks live in the content block list; a plain
    # string or text-only list is a message body.
    if isinstance(content, list):
        pending_text: list[str] = []
        for block in content:
            if not isinstance(block, Mapping):
                continue
            if block.get("type") == "tool_use":
                if pending_text:
                    shaped.append((kind, {"text": "\n".join(pending_text)
                                          [:_MAX_TEXT], "is_complete": True}))
                    pending_text = []
                shaped.append(("tool_call", {
                    "tool_name": str(block.get("name") or "")[:128],
                    "call_id": str(block.get("id") or "")[:128],
                    "arguments": _bounded_json(block.get("input")),
                    "result": "",
                    "digest": "",
                }))
            elif block.get("type") == "tool_result":
                if pending_text:
                    shaped.append((kind, {"text": "\n".join(pending_text)
                                          [:_MAX_TEXT], "is_complete": True}))
                    pending_text = []
                shaped.append(("tool_result", {
                    "tool_name": "",
                    "call_id": str(block.get("tool_use_id") or "")[:128],
                    "arguments": "",
                    # Keep bounded TEXT only: a non-text block list (e.g.
                    # image payloads) would otherwise dump base64 blobs into
                    # the durable record — map it to "" instead (same
                    # discipline as the pi toolResult mapper).
                    "result": _native_message_text(block.get("content")),
                    "digest": "",
                }))
            elif (block.get("type") == "text"
                    and isinstance(block.get("text"), str) and block["text"]):
                pending_text.append(block["text"])
        if pending_text:
            shaped.append((kind, {"text": "\n".join(pending_text)[:_MAX_TEXT],
                                  "is_complete": True}))
        return shaped
    text = _native_message_text(content)
    if not text:
        return []
    return [(kind, {"text": text, "is_complete": True})]


def _shape_pi_row(raw: Mapping) -> list[tuple[str, Mapping]]:
    """Map a pi transcript JSONL row to a list of ``(kind, payload_source)``.

    Pi wraps each turn as ``{"type": "message", "message": {...}}`` where the
    inner role is ``user`` / ``assistant`` / ``toolResult`` — tool results are
    their own role (with top-level ``toolName``/``toolCallId``/``isError``)
    rather than content blocks, and a call is a ``toolCall`` block whose
    ``arguments`` is already a dict.  Non-``message`` envelopes (``session``,
    ``model_change``, ``compaction``, ...) return ``[]``; ``thinking`` blocks
    are internal surfaces and never reach a message body.

    One row maps onto MULTIPLE events when the real transcript interleaves
    them inside a single message: real pi sessions frequently emit
    ``text`` + ``toolCall`` in one assistant row and several ``toolCall``
    blocks per row, so every toolCall block maps to its own ``tool_call``
    and any surrounding text maps to its own message event (order
    preserved; text is emitted before the first toolCall that follows it).
    An assistant row with no text and no toolCall yields ``[]`` (observed
    for tool-only turns).
    """
    message = raw.get("message")
    if not isinstance(message, Mapping):
        return []
    role = str(message.get("role") or "")
    content = message.get("content")
    if role == "toolResult":
        text = _native_message_text(content)
        body = {
            "tool_name": str(message.get("toolName") or "")[:128],
            "call_id": str(message.get("toolCallId") or "")[:128],
            "status": "error" if message.get("isError") else "ok",
            "arguments": "",
            # Keep bounded TEXT for the common case; a non-text block list
            # (e.g. image payloads) would otherwise dump base64 blobs into
            # the durable record — map it to "" instead.
            "result": text if text else "",
            "digest": "",
        }
        return [("tool_result", body)]
    if role not in ("user", "assistant"):
        return []
    shaped: list[tuple[str, Mapping]] = []
    message_kind = "user_message" if role == "user" else "assistant_message"
    if isinstance(content, list):
        pending_text: list[str] = []
        for block in content:
            if not (isinstance(block, Mapping)):
                continue
            if (block.get("type") == "text" and isinstance(block.get("text"), str)
                    and block["text"]):
                pending_text.append(block["text"])
                continue
            if block.get("type") == "toolCall":
                if pending_text:
                    text = "\n".join(pending_text)[:_MAX_TEXT]
                    shaped.append((message_kind,
                                   {"text": text, "is_complete": True}))
                    pending_text = []
                shaped.append(("tool_call", {
                    "tool_name": str(block.get("name") or "")[:128],
                    "call_id": str(block.get("id") or "")[:128],
                    "arguments": _bounded_json(block.get("arguments")),
                    "result": "",
                    "digest": "",
                }))
        if pending_text:
            shaped.append((message_kind,
                           {"text": "\n".join(pending_text)[:_MAX_TEXT],
                            "is_complete": True}))
    else:
        text = _native_message_text(content)
        if text:
            shaped.append((message_kind, {"text": text, "is_complete": True}))
    return shaped


def _bounded_json(value: Any) -> str:
    """Bounded JSON serialization for nested tool input/result values."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value[:_MAX_TOOL_FIELD]
    try:
        return json.dumps(value, ensure_ascii=False)[:_MAX_TOOL_FIELD]
    except (TypeError, ValueError):
        return str(value)[:_MAX_TOOL_FIELD]


def _shape_codex_row(raw: Mapping) -> list[tuple[str, Mapping]]:
    """Map a codex rollout JSONL row to a list of ``(kind, payload_source)``.

    Codex wraps everything as ``{"type": "event_msg"|"response_item", ...,
    "payload": {...}}``.  Only observable surfaces map: user/assistant
    messages, tool calls/outputs.  ``reasoning`` and ``token_count`` are
    internal surfaces (never emitted); ``response_item/message`` is dropped —
    the parallel ``event_msg`` stream already carries the same content and
    emitting both would double-count every message.  Each codex payload maps
    to at most one event, returned as a single-element list.
    """
    rtype = str(raw.get("type") or "")
    payload = raw.get("payload")
    if not isinstance(payload, Mapping):
        return []
    ptype = str(payload.get("type") or "")
    if rtype == "event_msg":
        if ptype == "user_message":
            text = payload.get("message")
            if isinstance(text, str) and text:
                return [("user_message", {"text": text[:_MAX_TEXT],
                                          "is_complete": True})]
            return []
        if ptype == "agent_message":
            text = payload.get("message")
            if isinstance(text, str) and text:
                return [("assistant_message", {"text": text[:_MAX_TEXT],
                                               "is_complete": True})]
            return []
        if ptype == "mcp_tool_call_end":
            invocation = payload.get("invocation")
            result = payload.get("result")
            ok = isinstance(result, Mapping) and "Ok" in result
            return [("tool_result", {
                "tool_name": str((invocation or {}).get("tool")
                                 if isinstance(invocation, Mapping)
                                 else "")[:128],
                "call_id": str(payload.get("call_id") or "")[:128],
                "status": "ok" if ok else "error",
                "arguments": _bounded_json(
                    (invocation or {}).get("arguments")
                    if isinstance(invocation, Mapping) else None),
                "result": _bounded_json(
                    result.get("Ok") if isinstance(result, Mapping) else None),
                "digest": "",
            })]
        return []
    if rtype == "response_item":
        if ptype == "function_call":
            return [("tool_call", {
                "tool_name": str(payload.get("name") or "")[:128],
                "call_id": str(payload.get("call_id") or "")[:128],
                "arguments": _bounded_json(payload.get("arguments")),
                "result": "",
                "digest": "",
            })]
        if ptype == "custom_tool_call":
            return [("tool_call", {
                "tool_name": str(payload.get("name") or "")[:128],
                "call_id": str(payload.get("call_id") or "")[:128],
                "arguments": _bounded_json(payload.get("input")),
                "result": "",
                "digest": "",
            })]
        if ptype == "function_call_output":
            return [("tool_result", {
                "tool_name": "",
                "call_id": str(payload.get("call_id") or "")[:128],
                "arguments": "",
                "result": _bounded_json(payload.get("output")),
                "digest": "",
            })]
        if ptype == "custom_tool_call_output":
            return [("tool_result", {
                "tool_name": "",
                "call_id": str(payload.get("call_id") or "")[:128],
                "arguments": "",
                "result": _bounded_json(payload.get("output")),
                "digest": "",
            })]
        # message rows are duplicated by the event_msg stream; reasoning is
        # internal — both dropped here.
        return []
    return []


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _opaque_event_id(prefix: str) -> str:
    h = hashlib.sha256(secrets.token_bytes(16)).hexdigest()[:24]
    return f"{prefix}_{h}"


def _safe_reason(reason: Any) -> str:
    """Allowlist-only reason token: never raw text, paths, or exceptions."""
    text = str(reason or "").strip().replace("\n", " ")[:64]
    if text in _BOUNDED_REASONS:
        return text
    return "source_unavailable"


def _required_opaque(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty opaque id")
    if len(value) > 256:
        raise ValueError(f"{name} is too long")
    lowered = value.lower()
    for marker in ("/", "\\", "token", "secret", "private", "key"):
        if marker in lowered:
            raise ValueError(f"{name} must not encode a path or secret")
    return value


def _opaque_opt(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return _required_opaque(value, "opaque id")
    except ValueError:
        return None


# Strict allowed character set for a source-supplied opaque event id.  Only
# plain alphanumerics and separator-safe punctuation are accepted; JWT dots,
# base64/url-sensitive ``= + / %``, cookie/session shapes, and secret-marker
# words are all rejected so an untrusted source id can never carry a token
# value or shape onto the durable/public surface.
_SAFE_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_:"
)

# Marker substrings that classify an id as secret-shaped regardless of charset.
_SAFE_ID_FORBIDDEN_MARKERS = ("token", "secret", "private", "key",
                              "password", "bearer", "session", "cookie",
                              "auth", "credential")


def _is_safe_opaque_id(value: Any) -> bool:
    """Strict allowlist check for a trustable opaque source id."""
    if not isinstance(value, str) or not value:
        return False
    if len(value) > 128:  # bounded, not 256 (opaque ids are short)
        return False
    if value[0] in "._-" or value[-1] in "._-":  # JWT/session edge separators
        return False
    if not all(c in _SAFE_ID_CHARS for c in value):
        return False
    lowered = value.lower()
    return not any(m in lowered for m in _SAFE_ID_FORBIDDEN_MARKERS)


def _safe_source_id(value: Any, prefix: str) -> str:
    """Keep a strict-allowlist opaque source event_id or replace it.

    A raw source ``event_id`` is trusted only when it passes
    :func:`_is_safe_opaque_id` (plain short opaque shape, no JWT/base64 cookie
    characters, no secret markers).  Anything else — including JWT-,
    session-token- or cookie-shaped values — is replaced by a generated opaque
    id so it can never smuggle a token value or shape onto the durable record.
    The whole record is never dropped for a bad id.
    """
    if _is_safe_opaque_id(value):
        return value
    return _opaque_event_id(prefix)


def _bounded(value: Any, limit: int) -> str:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return str(value)[:limit]


def _first_text(value: Any, keys: tuple) -> str:
    if isinstance(value, str) and value:
        return value[:_MAX_TEXT]
    if isinstance(value, Mapping):
        for key in keys:
            v = value.get(key)
            if isinstance(v, str) and v:
                return v[:_MAX_TEXT]
    return ""


def select_sources(manifest: Mapping[str, Any]) -> list[str]:
    """Return adapter source names enabled by ``manifest`` flags (sole
    adapter-selection function)."""
    return [name for flag, name in _SELECTOR.items()
            if bool(manifest.get(flag))]


def build_capture_gap_event(
    base: Mapping[str, Any],
    *,
    start_sequence: int,
    end_sequence: int,
    reason: Any,
) -> dict:
    """Build a validated ``capture_gap`` event that always degrades."""
    event: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event_id": _opaque_event_id("gap"),
        "stream_id": base.get("stream_id") or DEFAULT_STREAM,
        "machine_id": base.get("machine_id") or DEFAULT_MACHINE,
        "session_id": base.get("session_id") or DEFAULT_SESSION,
        "sequence": max(int(end_sequence), 1) + 1,
        "kind": "capture_gap",
        "capture_quality": base.get("capture_quality") or "structured",
        "source": base.get("source") or "bridge",
        "emitted_at": _iso_now(),
        "payload": {
            "start_sequence": int(start_sequence),
            "end_sequence": int(end_sequence),
            "quality": "best_effort",
            "reason": _safe_reason(reason),
        },
    }
    return validate_event(event)


def build_capture_quality_event(
    base: Mapping[str, Any],
    *,
    old_quality: str,
    new_quality: str,
    reason: Any,
) -> dict:
    """Build a validated ``capture_quality_changed`` event (degrade-only)."""
    if not old_quality:
        old_quality = new_quality
    validate_quality_transition(old_quality, new_quality)
    event: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event_id": _opaque_event_id("quality"),
        "stream_id": base.get("stream_id") or DEFAULT_STREAM,
        "machine_id": base.get("machine_id") or DEFAULT_MACHINE,
        "session_id": base.get("session_id") or DEFAULT_SESSION,
        "sequence": 1,  # spool owns the durable assignment
        "kind": "capture_quality_changed",
        "capture_quality": new_quality,
        "source": base.get("source") or "bridge",
        "emitted_at": _iso_now(),
        "payload": {
            "old_quality": old_quality,
            "new_quality": new_quality,
            "reason": _safe_reason(reason),
        },
    }
    return validate_event(event)


class SessionBridge:
    """Orchestrate verified transcript sources into the encrypted spool.

    ``manifest`` — a capability manifest mapping (``tools.session.probe.as_dict``
    output or equivalent).  ONLY its capability flags are consulted; no CLI
    name/version ever selects an adapter.

    ``session_config`` — a Mapping with the bounded session-binding fields:

      - ``session_id``, ``machine_id`` (required opaque ids)
      - ``stream_id`` (optional)
      - ``process_group_id``, ``attempt_id`` (optional)
      - ``managed`` (bool; unmanaged -> best-effort, no control capability)
      - ``best_effort`` (bool, optional; when True on the *managed* path the
        bridge forces ``capture_quality="best_effort"`` while still reporting
        ``managed=True`` and ``control_capability="available"`` — the adopt
        (纳管) default so an attached native tail is retained/redacted/uploaded
        at bounded best-effort until an operator explicitly upgrades)
      - ``spool``: a ``LocalSpool`` instance (recommended) OR
      - ``spool_root`` + ``spool_key`` (32 bytes) to build one
      - ``post_json``: the documented POST session-events transport (optional)
      - ``uploader``: a ``SessionUploader`` (optional; auto-built from
        ``post_json`` when present).
    """

    def __init__(self, manifest: Mapping[str, Any],
                 session_config: Mapping[str, Any] | None = None,
                 **kwargs):
        config: dict[str, Any] = {}
        if session_config is not None:
            if not isinstance(session_config, Mapping):
                raise TypeError("session_config must be a Mapping")
            config.update(dict(session_config))
        config.update(kwargs)

        self._manifest = dict(manifest or {})
        self._session_id = _required_opaque(config.get("session_id"),
                                            "session_id")
        self._machine_id = _required_opaque(config.get("machine_id"),
                                            "machine_id")
        self._stream_id = _opaque_opt(config.get("stream_id")) or "stream"
        self._agent_family = _opaque_opt(config.get("agent_family")) or str(
            self._manifest.get("agent") or "claude")

        self._process_group_id = _opaque_opt(config.get("process_group_id"))
        self._attempt_id = _opaque_opt(config.get("attempt_id"))
        want_managed = normalize_managed(config.get("managed"))
        # A managed session requires a process_group_id; otherwise it degrades
        # to unmanaged best-effort (never claims control).  ``best_effort`` is
        # the adopt clamp: a managed session forced to best-effort capture
        # (``control_capability`` still ``available``; the tail is retained and
        # redacted/uploaded at bounded quality until an operator upgrades).
        self.managed = bool(want_managed and self._process_group_id)
        if not self.managed or bool(config.get("best_effort")):
            self._forced_quality = "best_effort"
        else:
            self._forced_quality = ""

        # ---- spool ----------------------------------------------------
        self._spool = config.get("spool")
        if self._spool is None:
            spool_root = config.get("spool_root")
            spool_key = config.get("spool_key")
            if spool_root is None:
                raise ValueError("session_config requires a spool or spool_root")
            if spool_key is None:
                raise ValueError("session_config requires a 32-byte spool_key")
            self._spool = LocalSpool(
                Path(spool_root), self._machine_id, self._session_id,
                key=bytes(spool_key))
        if self._spool is None:
            raise ValueError("session_config requires a spool")

        # ---- uploader --------------------------------------------------
        self._uploader = config.get("uploader")
        post_json = config.get("post_json")
        if self._uploader is None and post_json is not None:
            # The Hub re-serializes each event after validation and attaches
            # redaction metadata, so a batch at the Hub's raw 256 KiB cap can
            # exceed ``MAX_BATCH_BYTES`` server-side and 413.  Ship at 75% of
            # the cap to absorb the envelope growth (still far above the
            # useful batch size).
            self._uploader = SessionUploader(
                post_json, spool=self._spool,
                batch_bytes=(session_schema.MAX_BATCH_BYTES * 3) // 4)

        # ---- native transcript ------------------------------------------
        # ``native_file_path`` comes in through the session config (the
        # ControlClient adopt config already carries it); subsequent
        # ``ingest_native()`` calls with no path resume from here.  A local
        # filesystem path is NOT an opaque id (it legitimately contains
        # ``/``), so it is bounded as a plain string, not via _opaque_opt.
        native_path = config.get("native_file_path")
        self._native_path = (str(native_path)[:_MAX_TEXT]
                             if isinstance(native_path, str) and native_path
                             else None)

        # ---- adapters --------------------------------------------------
        self._redactor = Redactor()
        self._hook_adapter = None
        self._pty_adapter = None
        self._tailer = None
        self._last_error: str | None = None
        self._last_reject: str | None = None
        self._started = False
        self._reported_quality: str | None = None
        self.sources = select_sources(self._manifest)
        self.source = self.sources[0] if self.sources else "metadata"
        self._init_adapters()

    # -- lifecycle -------------------------------------------------------

    @classmethod
    def open(cls, manifest: Mapping[str, Any],
             session_config: Mapping[str, Any]) -> "SessionBridge":
        """Open a bridge handle for one session (the Task 7 producer API)."""
        return cls(manifest, session_config)

    def start(self) -> dict:
        """Emit a ``session_start`` metadata event (idempotent)."""
        if self._started:
            return {}
        start = self._build("session_start", payload={
            "agent_family": self._agent_family,
            "adapter": self.source or "metadata",
        })
        self._emit_direct(start)
        self._started = True
        return {}

    # -- accessors --------------------------------------------------------

    @property
    def spool(self) -> LocalSpool:
        return self._spool

    @property
    def uploader(self):
        return self._uploader

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def last_reject(self) -> str | None:
        return self._last_reject

    @property
    def control_capability(self) -> str:
        return "available" if self.managed else "unavailable"

    @property
    def capture_quality(self) -> str:
        if not self.managed or self._forced_quality:
            return "best_effort"
        if self.source == "pty":
            return _pty.CAPTURE_QUALITY
        return "structured"

    @property
    def session_id(self) -> str:
        return self._session_id

    # -- ingestion --------------------------------------------------------

    def ingest(self, raw, *, source: str | None = None) -> bool:
        """Process one or more raw source items (list or single)."""
        source = source or self.source
        if isinstance(raw, list):
            ok = True
            for item in raw:
                if not self._handle_raw(source, item):
                    ok = False
            return ok
        return self._handle_raw(source, raw)

    def ingest_structured(self, raw_lines) -> None:
        """Ingest lines from a CLI structured stream (noise is rejected)."""
        for cleaned in _iter_json(raw_lines):
            self._handle_raw("structured_stream", cleaned)

    def ingest_native(self, path: str | None = None,
                      checkpoint_path: str | None = None) -> int:
        """Tail a native transcript JSONL by byte offset and ingest."""
        if path is None:
            path = self._native_path
        if path is None:
            raise ValueError("native path not configured")
        if self._tailer is None or checkpoint_path is not None:
            self._tailer = _tailer(path, checkpoint_path=checkpoint_path)
        count = 0
        try:
            for obj in self._tailer.read():
                if self._handle_raw("native_transcript", obj):
                    count += 1
        except (OSError, ValueError):
            self._emit_disconnect_gap("native_transcript", "source_unavailable")
            return count
        if self._tailer.disconnected:
            self._emit_disconnect_gap("native_transcript", "source_disconnected")
        elif self._tailer.last_error:
            self._emit_disconnect_gap("native_transcript", "source_unavailable")
        if checkpoint_path is not None and self._tailer is not None:
            self._tailer.checkpoint_to(checkpoint_path)
        return count

    def ingest_one(self, raw, *, source: str | None = None) -> bool:
        return self._handle_raw(source or self.source, raw)

    def flush(self) -> int:
        """Upload all pending unacked events (Task 6 ack/replay)."""
        if self._uploader is None:
            return 0
        return self._uploader.flush_once()

    def replay(self) -> int:
        if self._uploader is None:
            return 0
        return self._uploader.replay_from_ack()

    def close(self) -> None:
        try:
            if self._spool is not None:
                self._spool.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- internals ---------------------------------------------------------

    def _init_adapters(self):
        from tools.session.capture.hooks import HookAdapter

        if "hooks" in self.sources:
            self._hook_adapter = HookAdapter(
                self._manifest, session_id=self._session_id,
                machine_id=self._machine_id, stream_id=self._stream_id,
                agent_family=self._agent_family)
        if "pty" in self.sources:
            self._pty_adapter = _pty.PtyAdapter(
                self._manifest, session_id=self._session_id,
                machine_id=self._machine_id, stream_id=self._stream_id,
                agent_family=self._agent_family)
        self._tailer = None

    def _build(self, kind: str, *, payload: Mapping[str, Any]) -> dict:
        event = {
            "schema_version": SCHEMA_VERSION,
            "event_id": _opaque_event_id("evt"),
            "stream_id": self._stream_id,
            "machine_id": self._machine_id,
            "session_id": self._session_id,
            "sequence": 1,  # placeholder; spool owns durable assignment
            "kind": kind,
            "capture_quality": self._event_quality(kind),
            "source": self.source or "metadata",
            "emitted_at": _iso_now(),
            "payload": dict(payload),
        }
        # The envelope is the ONLY channel that binds a session to its
        # supervisor process group: the Hub derives ``managed`` from
        # ``process_group_id`` (hub/application/session_service.py
        # ``_session_spec_from_event``), so an adopt bridge that omits it
        # degrades the whole session to unmanaged best-effort server-side
        # even though the local manifest is managed.  Optional per the
        # shared schema (standalone sessions may omit it).
        if self._process_group_id:
            event["process_group_id"] = self._process_group_id
        if self._attempt_id:
            event["attempt_id"] = self._attempt_id
        return event

    def _event_quality(self, kind: str) -> str:
        if not self.managed or self._forced_quality:
            return "best_effort"
        if self.source == "pty":
            return _pty.CAPTURE_QUALITY
        return "structured"

    def _handle_struct(self, raw) -> bool:
        return self._handle_raw("structured_stream", raw)

    def _handle_raw(self, source: str, raw) -> bool:
        if source == "structured_stream":
            return self._handle_structured(raw)
        if source == "native_transcript":
            return self._handle_native(raw)
        if source == "hooks":
            return self._handle_hook(raw)
        if source == "pty":
            return self._handle_pty(raw)
        return False

    def _handle_structured(self, raw) -> bool:
        return self._emit_all(self._normalize(raw))

    def _handle_native(self, raw) -> bool:
        return self._emit_all(self._normalize(raw))

    def _emit_all(self, events) -> bool:
        """Emit every shaped event in order; True if ANY was accepted.

        ``events`` is a list (multi-event shaping) or a single dict (full-
        envelope / coerce path) — both normalize to the same loop.
        """
        if isinstance(events, dict):
            events = [events]
        accepted = False
        for event in (events or []):
            if self._emit_event(event):
                accepted = True
        return accepted

    def _handle_hook(self, raw) -> bool:
        if self._hook_adapter is None:
            return False
        try:
            event = self._hook_adapter.normalize(raw)
        except Exception:
            return False
        return self._emit_event(event)

    def _handle_pty(self, raw) -> bool:
        if self._pty_adapter is None:
            return False
        try:
            event = self._pty_adapter.normalize(raw)
        except Exception:
            return False
        return self._emit_event(event)

    def _normalize(self, raw: Mapping) -> dict | None:
        if not isinstance(raw, Mapping):
            return None
        if isinstance(raw.get("kind"), str) and raw["kind"] in EVENT_KINDS:
            return self._coerce_full(raw)
        return self._shape_cli_events(raw)

    def _coerce_full(self, e: Mapping) -> dict | None:
        candidate = dict(e)
        candidate.setdefault("schema_version", SCHEMA_VERSION)
        candidate["event_id"] = _safe_source_id(e.get("event_id"), "evt")
        candidate.setdefault("stream_id", self._stream_id)
        candidate.setdefault("machine_id", self._machine_id)
        candidate.setdefault("session_id", self._session_id)
        candidate.setdefault("sequence", 1)  # spool reassigns; not durable
        candidate.setdefault("emitted_at", _iso_now())
        candidate.setdefault("capture_quality", self._event_quality("?"))
        candidate.setdefault("source", self.source or "metadata")
        payload = candidate.get("payload")
        kind = candidate.get("kind")
        # For a minimal source record with a text/tool body (no envelope),
        # shape a legal payload rather than rejecting the whole record.
        if kind in ("user_message", "assistant_message"):
            candidate["payload"] = {
                "text": _first_text(payload if payload is not None
                                    else e, ("text", "content", "result")),
                "is_complete": True,
            }
        elif kind in ("tool_call", "tool_result"):
            source_body = payload if isinstance(payload, Mapping) else e
            candidate["payload"] = {
                "tool_name": _first_text(source_body, ("tool_name",)),
                "call_id": _first_text(source_body, ("call_id", "id")),
                "status": _bounded(source_body.get("status"), 64),
                "arguments": _first_text(source_body, ("arguments", "input")),
                "result": _first_text(source_body, ("result", "output")),
                "digest": "",
            }
        try:
            return validate_event(candidate)
        except (ValueError, TypeError):
            return None

    def _shape_cli_events(self, raw: Mapping) -> list[dict]:
        """Shape one raw source row into a list of validated events.

        Hook-adapter shapes first (PreToolUse / UserPromptSubmit / ...);
        otherwise native transcript JSONL shapes (claude_code
        "user"/"assistant" rows, codex rollout wrappers, pi "message" rows).
        A single row can legitimately map onto MULTIPLE events (e.g. a pi
        assistant row carrying text + several toolCall blocks) — the caller
        emits each in order.
        """
        rtype = _TYPE_TO_KIND.get(str(raw.get("type") or ""))
        if rtype is not None:
            event = self._shape_typed(rtype, raw)
            return [event] if event is not None else []
        events = []
        for kind, payload in _shape_native_row(raw):
            event = self._shape_typed(kind, payload)
            if event is not None:
                events.append(event)
        return events

    def _shape_typed(self, rtype: str, raw: Mapping) -> dict | None:
        ev = self._build(rtype, payload={})
        ev["capture_quality"] = self._event_quality(rtype)
        if rtype in ("user_message", "assistant_message"):
            ev["payload"] = {
                "text": _first_text(raw, ("text", "content", "result")),
                "is_complete": True,
            }
        elif rtype in ("tool_call", "tool_result"):
            ev["payload"] = {
                "tool_name": _first_text(raw, ("tool_name",)),
                "call_id": _first_text(raw, ("call_id", "id")),
                "status": _bounded(raw.get("status"), 64),
                "arguments": _first_text(raw, ("arguments", "input")),
                "result": _first_text(raw, ("result", "output")),
                "digest": "",
            }
        elif rtype == "session_start":
            ev["payload"] = {"agent_family": self._agent_family,
                             "adapter": self.source}
        try:
            return validate_event(ev)
        except (ValueError, TypeError):
            return None

    def _clamp_event_quality(self, event: dict | None) -> dict | None:
        """Enforce the unmanaged best-effort boundary on every emitted event.

        A non-managed session must never durably emit a structured/exact
        capture event.  This is the FINAL gate on the event path: whatever the
        source adapter produced, the persisted record is clamped to
        ``best_effort`` before redaction and append.

        Source-injected control/quality events (``capture_gap`` /
        ``capture_quality_changed``) are REJECTED on an unmanaged session: a
        raw source event can carry a top-level ``capture_quality`` of
        ``exact``/``structured``, and exempting it would let that grading reach
        the durable record from an unmanaged source.  The bridge's OWN bounded
        control events (quality drop + gap) are persisted via
        ``_emit_downgrade`` / ``_emit_disconnect_gap`` which call the spool
        directly and never pass through this gate, so their graded-state
        semantics are preserved.
        """
        if event is None:
            return None
        if self.managed:
            return event
        kind = event.get("kind")
        if kind in _CLAMP_EXEMPT:
            # source-injected control event on an unmanaged session: reject
            return None
        quality = event.get("capture_quality")
        if quality not in ("best_effort",):
            event = dict(event)
            event["capture_quality"] = "best_effort"
        return event

    def _emit_disconnect_gap(self, source: str, reason: Any) -> None:
        """Emit a bounded, validated quality drop when a source disconnects.

        Only when the session is at a capture quality ABOVE ``best_effort``
        (managed structured/exact) is the degradation meaningful: the schema
        forbids a ``capture_gap``/quality change from ``best_effort`` (there is
        nothing lower to degrade to).  An unmanaged best-effort source break is
        therefore represented by nothing in the quality stream — the source
        break is not a quality change.  The source name never leaks raw text;
        the reason stays a bounded token.
        """
        current = self.capture_quality or "structured"
        if current not in ("exact", "structured"):
            return
        try:
            base = {
                "schema_version": SCHEMA_VERSION,
                "event_id": _opaque_event_id("gap"),
                "stream_id": self._stream_id,
                "machine_id": self._machine_id,
                "session_id": self._session_id,
                "sequence": 1,  # spool owns durable assignment
                "kind": "capture_quality_changed",
                "capture_quality": current,
                "source": self.source or "bridge",
                "emitted_at": _iso_now(),
                "payload": {},
            }
            self._spool.append(build_capture_quality_event(
                base, old_quality=current, new_quality="best_effort",
                reason=reason))
            last_seq = self._spool.status().get("last_sequence", 0)
            self._spool.append(build_capture_gap_event(
                base, start_sequence=last_seq, end_sequence=last_seq,
                reason=reason))
        except (ValueError, TypeError, SpoolError):
            pass

    def _bind_envelope(self, event: dict) -> dict:
        """Bind the supervisor identity onto every emitted event.

        The Hub derives a session's ``managed`` state from the event's
        ``process_group_id``; source adapters that build full envelopes
        (structured stream, hooks, pty) never see the bridge config, so the
        binding is applied centrally here.  Unmanaged bridges (no group id)
        leave the envelope untouched — the shared schema treats the field as
        optional.
        """
        if self._process_group_id and "process_group_id" not in event:
            event["process_group_id"] = self._process_group_id
        if self._attempt_id and "attempt_id" not in event:
            event["attempt_id"] = self._attempt_id
        return event

    def _emit_event(self, event: dict | None) -> bool:
        if event is None:
            return False
        event = self._clamp_event_quality(self._bind_envelope(event))
        if event is None:
            # the unmanaged clamp rejected the event (e.g. a source-injected
            # control event); do not redact or persist a rejected event.
            return False
        # (1) redact
        redacted, _ = self._redactor.redact_event(event)
        try:
            result = self._spool.append(redacted)
        except (ValueError, TypeError, SpoolError) as exc:
            # SpoolError (quota/setup/checkpoint) degrades to a bounded
            # per-event reject — it never escapes ingest into the source loop.
            self._last_reject = getattr(exc, "code", None) or "invalid_event"
            return False
        if result.capture_blocked:
            self._emit_downgrade(event, "spool_quota")
        return result.accepted or result.gap_sequence is not None

    def _emit_direct(self, event: dict) -> None:
        event = self._clamp_event_quality(self._bind_envelope(event))
        if event is None:
            return
        redacted, _ = self._redactor.redact_event(event)
        try:
            self._spool.append(redacted)
        except (ValueError, TypeError, SpoolError):
            pass

    def _emit_downgrade(self, base: Mapping, reason: Any) -> None:
        old_q = base.get("capture_quality") or "structured"
        new_q = "best_effort"
        try:
            quality = build_capture_quality_event(
                base, old_quality=old_q, new_quality=new_q, reason=reason)
            self._spool.append(quality)
        except (ValueError, TypeError, SpoolError):
            pass
        try:
            gap = build_capture_gap_event(
                base,
                start_sequence=self._spool.status().get("last_sequence", 0),
                end_sequence=self._spool.status().get("last_sequence", 0),
                reason=reason)
            self._spool.append(gap)
        except (ValueError, TypeError, SpoolError):
            pass
        self._forced_quality = "best_effort"
        self._reported_quality = "best_effort"


Bridge = SessionBridge  # ergonomic alias

__all__ = [
    "Bridge",
    "SessionBridge",
    "build_capture_gap_event",
    "build_capture_quality_event",
    "select_sources",
]