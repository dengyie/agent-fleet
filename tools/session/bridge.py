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
- Normalized previews and opted-in source_record archives keep agent text
  and paths, but secret/credential patterns are redacted on the node before
  append (secrets never leave the machine). The spool is encrypted,
  append-only and quota-bounded.
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
from tools.session.normalization import (
    _MAX_TEXT, content_block_message_id, normalize_native_row as _shape_native_row,
)
from session_schema import (
    AGENT_STATE_CONFIDENCES,
    AGENT_STATE_SOURCES,
    AGENT_STATES,
    EVENT_KINDS,
    SCHEMA_VERSION,
    normalize_managed,
    validate_event,
    validate_quality_transition,
)
from tools.session.capture import pty as _pty
from tools.session.capture.native_jsonl import tailer as _tailer
from tools.session.capture.structured_stream import iter_json as _iter_json
from fleet_core.session.redact import Redactor, redact_event_secrets, redact_secrets_text
from tools.session.spool import OWNER_WAIT_S, LocalSpool, SpoolError
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
    "session_metadata": "session_metadata",
    "interaction_request": "interaction_request",
    "interaction_response": "interaction_response",
    "agent_state_changed": "agent_state_changed",
}

# Control/quality events legitimately carry a higher ``capture_quality`` (the
# graded state before the drop is recorded).  They are NOT captured content, so
# the unmanaged best-effort clamp never applies to them.
_CLAMP_EXEMPT = frozenset({"capture_gap", "capture_quality_changed"})


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

        self._preserve_source = bool(config.get("preserve_source", False))
        self._manifest = dict(manifest or {})
        self._session_id = _required_opaque(config.get("session_id"),
                                            "session_id")
        self._machine_id = _required_opaque(config.get("machine_id"),
                                            "machine_id")
        self._stream_id = _opaque_opt(config.get("stream_id")) or "stream"
        self._agent_family = _opaque_opt(config.get("agent_family")) or str(
            self._manifest.get("agent") or "claude")
        from tools.session.agy import AgyStream
        self._agy_stream = AgyStream() if self._agent_family == 'agy' else None

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
            quota_root = config.get("spool_quota_root")
            self._spool = LocalSpool(
                Path(spool_root), self._machine_id, self._session_id,
                key=bytes(spool_key),
                quota_root=Path(quota_root) if quota_root is not None else None,
                max_machine_bytes=int(config.get('spool_machine_bytes', 134217728)),
                owner_lock=bool(config.get("spool_owner_lock", False)),
                owner_timeout=float(config.get("spool_owner_timeout",
                                               OWNER_WAIT_S)))
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
        self._checkpoint_path = config.get("checkpoint_path")
        self._tailer = None
        self._last_error: str | None = None
        self._last_reject: str | None = None
        self._expected_user_echo: str | None = None
        # A family name does not prove a live native permission channel. Only
        # persisted metadata from a managed ACP worker can enable this path.
        self._native_questions_enabled = False
        self._started = False
        self._active_native_message_id: str | None = None
        self._active_native_text_blocks: set[int] = set()
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
            "client_version": _bounded(self._manifest.get("version"), 96),
            "interactive_questions": self._manifest.get("interactive_questions") is True
                and self.source in ("hooks", "structured_stream", "native_transcript"),
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
        if checkpoint_path is not None:
            self._checkpoint_path = checkpoint_path
        if self._tailer is None:
            options = {"max_line_bytes": 16 << 20} if self._preserve_source else {}
            self._tailer = _tailer(path, checkpoint_path=self._checkpoint_path, **options)
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
        if self._checkpoint_path is not None and self._tailer is not None:
            self._tailer.checkpoint_to(self._checkpoint_path)
        return count

    def ingest_one(self, raw, *, source: str | None = None) -> bool:
        return self._handle_raw(source or self.source, raw)

    def record_user_turn(self, text: str) -> None:
        """Record a Runner-delivered turn and expect at most one native echo.

        Raw echo records remain archived. Each explicit delivery resets the
        match, so legitimate identical instructions in later turns survive.
        """
        self._expected_user_echo = None
        if self.ingest_one({'kind': 'user_message', 'payload': {'text': text}}):
            self._expected_user_echo = text[:_MAX_TEXT]

    def flush(self, guard=None) -> int:
        """Upload all pending unacked events (Task 6 ack/replay).

        ``guard`` (a context manager, e.g. the capture lock) is held only
        around spool access, never across the network POST.
        """
        if self._uploader is None:
            return 0
        return self._uploader.flush_once(guard=guard)

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
        if source not in self.sources:
            return False
        if self._preserve_source:
            self.record_source(raw)
        if source == "structured_stream":
            return self._handle_structured(raw)
        if source == "native_transcript":
            return self._handle_native(raw)
        if source == "hooks":
            return self._handle_hook(raw)
        if source == "pty":
            return self._handle_pty(raw)
        return False

    def record_source(self, raw) -> None:
        """Retain observable source records, chunking instead of truncating.

        4000 codepoints fit the wire cap even for escaped non-BMP Unicode.
        Consecutive chunks end with is_complete=true and retain source order.
        """
        text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        # 先对整行做 secret 脱敏再分块：密钥可能跨 4000 码点边界被拆开。
        text = redact_secrets_text(text)[0]
        parts = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [""]
        for index, part in enumerate(parts):
            event = self._build("source_record", payload={
                "text": part, "is_complete": index == len(parts) - 1})
            if not self._emit_event(event):
                raise RuntimeError("source_record_not_persisted")

    def _handle_structured(self, raw) -> bool:
        events = self._normalize(raw)
        state = self._state_event_from_raw("structured_stream", raw)
        if isinstance(events, dict):
            events = [events]
        else:
            events = list(events or [])
        if state is not None and not any(event.get('kind') == 'agent_state_changed' for event in events):
            events.append(state)
        elif self.managed:
            events.extend(self._states_from_events("structured_stream", events))
        return self._emit_all(events, source="structured_stream")

    def _handle_native(self, raw) -> bool:
        events = self._normalize(raw)
        state = self._state_event_from_raw("native_transcript", raw)
        if isinstance(events, dict):
            events = [events]
        else:
            events = list(events or [])
        if state is not None and not any(event.get('kind') == 'agent_state_changed' for event in events):
            events.append(state)
        elif self.managed:
            events.extend(self._states_from_events("native_transcript", events))
        return self._emit_all(events, source="native_transcript")

    def _emit_all(self, events, source: str | None = None) -> bool:
        """Emit every shaped event in order; True if ANY was accepted.

        ``events`` is a list (multi-event shaping) or a single dict (full-
        envelope / coerce path) — both normalize to the same loop.
        """
        if isinstance(events, dict):
            events = [events]
        accepted = False
        for event in (events or []):
            kind = event.get('kind') if event else None
            if kind in {'user_message', 'assistant_message', 'tool_call', 'tool_result'}:
                expected = self._expected_user_echo
                self._expected_user_echo = None
                if kind == 'user_message' and expected is not None and event['payload'].get('text') == expected:
                    # record_source() ran before shaping, preserving the exact
                    # observable native echo without a duplicate chat bubble.
                    accepted = True
                    continue
            if source and event:
                event = dict(event, source=source)
            if self._emit_event(event):
                accepted = True
        return accepted

    def _states_from_events(self, source: str, events: list[dict]) -> list[dict]:
        """Known structural records, never text heuristics, describe active work."""
        for event in reversed(events):
            kind = event.get("kind")
            mapping = {"session_start": ("running", "session_started"),
                "user_message": ("running", "user_prompt_submitted"),
                "tool_call": ("running", "tool_started"), "tool_result": ("running", "tool_finished"),
                "session_close": ("completed", "session_ended")}
            if kind not in mapping:
                continue
            state, reason = mapping[kind]
            return [self._build("agent_state_changed", payload={"state": state,
                "source": source, "confidence": "medium", "reason": reason})]
        return []

    def _handle_hook(self, raw) -> bool:
        if self._hook_adapter is None:
            return False
        try:
            event = self._hook_adapter.normalize(raw)
            state = self._hook_adapter.state_event(raw)
        except Exception:
            return False
        accepted = self._emit_event(event)
        if state is not None:
            accepted = self._emit_event(state) or accepted
        question = self._hook_adapter.interaction_event(raw)
        if question is not None:
            accepted = self._emit_event(question) or accepted
            waiting = self._hook_adapter.state_event({"type": "AskUserQuestion",
                "event_id": question["event_id"]})
            self._emit_event(waiting)
        return accepted

    def _state_event_from_raw(self, source: str, raw: Mapping) -> dict | None:
        """Build a lifecycle event only from explicit structured state data."""
        if not isinstance(raw, Mapping):
            return None
        value = raw.get("agent_state", raw.get("state"))
        if value not in AGENT_STATES:
            return None
        confidence = raw.get("state_confidence", "medium")
        if confidence not in AGENT_STATE_CONFIDENCES:
            confidence = "medium"
        event = self._build("agent_state_changed", payload={
            "state": value,
            "source": source,
            "confidence": confidence,
            "reason": "explicit_state",
        })
        event["source"] = source
        try:
            return validate_event(event)
        except (ValueError, TypeError):
            return None

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
        raw_type = str(raw.get("type") or "")
        stream_event = raw.get("event") if isinstance(raw.get("event"), Mapping) else {}
        stream_type = stream_event.get("type")
        if raw_type == "stream_event" and stream_type == "message_start":
            message = stream_event.get("message")
            candidate_id = message.get("id") if isinstance(message, Mapping) else None
            self._active_native_message_id = (
                candidate_id if isinstance(candidate_id, str) and candidate_id else None)
            self._active_native_text_blocks.clear()

        native_time = self._native_source_time(raw)
        events = []
        shaped = self._agy_stream.normalize(raw) if self._agy_stream is not None else _shape_native_row(raw)
        for kind, payload in shaped:
            payload = dict(payload)
            if (kind == "assistant_message" and not payload.get("message_id")
                    and self._active_native_message_id):
                index = stream_event.get("index", 0)
                if type(index) is not int or not 0 <= index < 1024:
                    continue
                payload["message_id"] = content_block_message_id(self._active_native_message_id, index)
                self._active_native_text_blocks.add(index)
            event = self._shape_typed(kind, payload)
            if event is not None:
                if native_time and kind != "session_metadata":
                    event = validate_event(dict(event, emitted_at=native_time))
                events.append(event)

        if (raw_type == "stream_event" and isinstance(stream_type, str)
                and stream_type in {"content_block_stop", "message_stop"}):
            stop_index = stream_event.get("index", 0)
            indices = set(self._active_native_text_blocks) if stream_type == "message_stop" else (
                {stop_index} & self._active_native_text_blocks if type(stop_index) is int else set())
            for index in sorted(indices):
                completed = self._shape_typed("assistant_message", {
                    "text": "", "is_complete": True,
                    "message_id": content_block_message_id(self._active_native_message_id, index),
                    "content_mode": "delta",
                })
                if completed is not None:
                    if native_time:
                        completed = validate_event(dict(completed, emitted_at=native_time))
                    events.append(completed)
            self._active_native_text_blocks.difference_update(indices)
            if stream_type == "message_stop":
                self._active_native_message_id = None
        return events

    @staticmethod
    def _native_source_time(raw: Mapping) -> str | None:
        candidates = [raw.get("timestamp"), raw.get("created_at")]
        payload = raw.get("payload")
        message = raw.get("message")
        stream = raw.get("event")
        if isinstance(payload, Mapping):
            candidates.append(payload.get("timestamp"))
        if isinstance(message, Mapping):
            candidates.append(message.get("timestamp"))
        if isinstance(stream, Mapping):
            candidates.append(stream.get("timestamp"))
            stream_message = stream.get("message")
            if isinstance(stream_message, Mapping):
                candidates.append(stream_message.get("created_at"))
        for value in candidates:
            if not isinstance(value, str) or len(value) > 40:
                continue
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is not None:
                return value
        return None

    def _shape_typed(self, rtype: str, raw: Mapping) -> dict | None:
        ev = self._build(rtype, payload={})
        ev["capture_quality"] = self._event_quality(rtype)
        if rtype in ("user_message", "assistant_message"):
            ev["payload"] = {
                "text": _first_text(raw, ("text", "content", "result")),
                "is_complete": True,
            }
            message_id = raw.get("message_id")
            content_mode = raw.get("content_mode")
            if isinstance(message_id, str) and message_id:
                ev["payload"]["message_id"] = message_id
            if content_mode in ("delta", "snapshot"):
                ev["payload"]["content_mode"] = content_mode
            if raw.get("is_complete") is False:
                ev["payload"]["is_complete"] = False
        elif rtype in ("tool_call", "tool_result"):
            ev["payload"] = {
                "tool_name": _first_text(raw, ("tool_name",)),
                "call_id": _first_text(raw, ("call_id", "id")),
                "status": _bounded(raw.get("status"), 64),
                "arguments": _first_text(raw, ("arguments", "input")),
                "result": _first_text(raw, ("result", "output")),
                "digest": "",
            }
        elif rtype in ("session_start", "session_metadata"):
            allowed = ("agent_family", "adapter", "task_id", "agent_ref",
                       "title", "model", "provider", "client_version",
                       "native_started_at", "interactive_questions", "runtime_protocol",
                       "native_resume", "resumed", "desktop_bridge",
                       "model_takeover")
            ev["payload"] = {key: raw[key] for key in allowed if key in raw}
            if rtype == "session_start":
                ev["payload"].setdefault("agent_family", self._agent_family)
                ev["payload"].setdefault("adapter", self.source)
        elif rtype == "interaction_request":
            allowed = ("request_id", "question_type", "prompt", "options",
                       "allow_custom", "tool_name", "delivery")
            ev["payload"] = {key: raw[key] for key in allowed if key in raw}
        elif rtype == "interaction_response":
            allowed = ("request_id", "selected", "text")
            ev["payload"] = {key: raw[key] for key in allowed if key in raw}
        elif rtype == "agent_state_changed":
            allowed = ("state", "source", "confidence", "reason")
            ev["payload"] = {key: raw[key] for key in allowed if key in raw}
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
        if event.get("kind") in ("session_start", "session_metadata"):
            payload = event["payload"]
            native_channel = (self.managed and self._agent_family == 'agy'
                and event.get('source') == 'structured_stream'
                and payload.get('runtime_protocol') == 'acp'
                and payload.get('interactive_questions') is True)
            if ('runtime_protocol' in payload and payload['runtime_protocol'] != 'acp'
                    and self._native_questions_enabled):
                event = dict(event, payload=dict(payload, interactive_questions=False))
            elif (payload.get('interactive_questions') is True
                    and not self._manifest.get('interactive_questions') and not native_channel):
                event = dict(event, payload=dict(payload, interactive_questions=False))
        if event.get("kind") == "agent_state_changed":
            source = event.get("source", self.source)
            source = "hook" if source == "hooks" else source
            if source not in AGENT_STATE_SOURCES:
                return None
            payload = dict(event["payload"], source=source)
            if not self.managed:
                payload.update(state="unknown", confidence="low", reason="unmanaged_source")
            if source == "pty":
                payload.update(state="unknown", confidence="low", reason="unstructured_source")
            event = dict(event, payload=payload)
        if event.get("kind") in ("interaction_request", "interaction_response"):
            if ((self._manifest.get("interactive_questions") is not True and not self._native_questions_enabled)
                    or event.get("source") not in ("hook", "hooks", "structured_stream", "native_transcript")):
                return None
            if (event['payload'].get('delivery') == 'native_permission'
                    and not self._native_questions_enabled):
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
        state_source = "hook" if source == "hooks" else source
        if state_source in AGENT_STATE_SOURCES:
            self._emit_event(self._build("agent_state_changed", payload={
                "state": "unknown", "source": state_source,
                "confidence": "low", "reason": "source_disconnected"}))
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
        # Source records are the operator's complete conversation archive:
        # paths/prose are kept, but secret/credential patterns are always
        # replaced on the node so they never leave this machine.
        redacted = redact_event_secrets(event)
        if event["kind"] != "source_record":
            redacted = self._redactor.redact_event(redacted)[0]
        try:
            result = self._spool.append(redacted)
        except (ValueError, TypeError, SpoolError) as exc:
            # SpoolError (quota/setup/checkpoint) degrades to a bounded
            # per-event reject — it never escapes ingest into the source loop.
            self._last_reject = getattr(exc, "code", None) or "invalid_event"
            return False
        if result.capture_blocked:
            self._emit_downgrade(event, "spool_quota")
        if result.accepted and event['kind'] in ('session_start', 'session_metadata'):
            payload = event['payload']
            if 'runtime_protocol' in payload and payload['runtime_protocol'] != 'acp':
                self._native_questions_enabled = False
            if 'interactive_questions' in payload:
                self._native_questions_enabled = bool(self.managed and self._agent_family == 'agy'
                    and event.get('source') == 'structured_stream'
                    and payload.get('runtime_protocol') == 'acp'
                    and payload['interactive_questions'] is True)
        if event["kind"] == "source_record":
            return result.accepted
        return result.accepted or result.gap_sequence is not None

    def _emit_direct(self, event: dict) -> None:
        event = self._clamp_event_quality(self._bind_envelope(event))
        if event is None:
            return
        redacted, _ = self._redactor.redact_event(redact_event_secrets(event))
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
