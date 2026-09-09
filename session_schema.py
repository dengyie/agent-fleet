"""Shared session-event wire contract for the agent-fleet supervision surface.

This module is intentionally dependency-light: it imports only the standard
library so both the Hub (``hub/domain/session.py``) and standalone ``tools/``
scripts can use the same envelope allowlist and size bounds without importing
Flask or Hub application state.  It is a pure data contract - no persistence,
no network, no filesystem access, no CLI packages, no credentials.

The envelope, identity fields, quality model, and size bounds mirror the frozen
agent-session-supervision design (v5):

- exactly fifteen ``EVENT_KINDS``;
- three ``CAPTURE_QUALITIES`` (``exact > structured > best_effort``);
- single-event and batch size limits;
- ``validate_event`` / ``validate_batch`` that remove unknown top-level fields,
  reject unknown kinds, invalid/missing identity fields, invalid quality, and
  oversized payloads;
- ``partition_batch`` / ``try_validate_event`` that reject malformed events
  without losing the valid ones (per-event classification for later ingest);
- ``public_session_dto`` builds a bounded public session row that never leaks a
  raw path, PID, token, or raw collector output.

Design rules enforced here:

- Public identifiers must never be derived from raw PIDs, filesystem paths,
  tokens, or secrets; the DTO sanitizes path/secret-shaped identifiers.
- Raw collector output must never be passed through: every event kind has an
  explicit payload allowlist, and kinds without one (process_spawn,
  session_metadata, policy_signal, ...) drop every key outside the allowlist.
- ``emitted_at`` must be an RFC3339 timestamp.
- A ``capture_quality_changed`` event carries an explicit old/new quality pair
  and is the only legal downward quality-switch signal.
- ``best_effort`` sources may never upgrade to ``exact``/``structured``.
- A ``capture_gap`` always represents degradation: its ``payload.quality`` must
  be strictly lower than the event's own ``capture_quality`` (never equal or
  elevated), and it must carry ``start_sequence``, ``end_sequence``, and the
  degraded ``quality``.
"""

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

EVENT_KINDS = frozenset({
    'session_start',
    'session_metadata',
    'user_message',
    'assistant_message',
    'tool_call',
    'tool_result',
    'process_spawn',
    'process_exit',
    'step_start',
    'step_end',
    'session_close',
    'capture_quality_changed',
    'capture_gap',
    'policy_signal',
    'supervisor_state_changed',
})

CAPTURE_QUALITIES = ('exact', 'structured', 'best_effort')

SCHEMA_VERSION = 1

MAX_EVENT_BYTES = 65536          # 64 KiB per event (post-redaction)
MAX_BATCH_EVENTS = 100
MAX_BATCH_BYTES = 262144         # 256 KiB per batch

_MAX_OPAQUE_ID = 256
_MAX_TEXT = 262144
_MAX_NOTE = 200
_MAX_TOOL_FIELD = 262144

# Every event must carry these identity/envelope fields.  ``attempt_id`` and
# ``process_group_id`` are optional on the envelope (standalone sessions may
# omit ``attempt_id``); managed-session binding is a session-level concern
# enforced by ``hub/domain/session.py``.
_REQUIRED_FIELDS = (
    'schema_version',
    'event_id',
    'stream_id',
    'machine_id',
    'session_id',
    'sequence',
    'kind',
    'capture_quality',
    'emitted_at',
)

_TEXT_PAYLOAD_KINDS = frozenset({'user_message', 'assistant_message'})
_TOOL_PAYLOAD_KINDS = frozenset({'tool_call', 'tool_result'})
_QUALITY_CHANGE_KIND = 'capture_quality_changed'
_CAPTURE_GAP_KIND = 'capture_gap'

# Payload keys that must never survive validation for any event kind.  These
# are defense in depth: every kind also has its own explicit allowlist, so a
# value arriving under one of these names can never reach the public envelope.
_FORBIDDEN_PAYLOAD_KEYS = frozenset({
    'pid', 'process_id', 'path', 'proc_path', 'cwd', 'raw', 'raw_output',
    'collector_output', 'collector', 'command', 'argv', 'command_line',
    'args', 'env', 'environment', 'token', 'secret', 'private_key',
    'credential', 'password', 'api_key', 'access_token', 'bearer',
})

# Explicit payload allowlists per generic event kind.  Fields are deliberately
# whitelisted, so a process_spawn may carry ``process_family``/``owner`` and a
# ``process_group_id`` binding but never ``cmd``/``argv``/``env``/``cwd``/``pid``.
_GENERIC_PAYLOAD_FIELDS: dict[str, tuple[str, ...]] = {
    'session_start': ('agent_family', 'adapter', 'cli_version'),
    'session_metadata': ('agent_family', 'adapter', 'task_id', 'agent_ref'),
    'process_spawn': ('process_family', 'owner', 'group_binding',
                      'process_group_id'),
    'process_exit': ('exit_code', 'duration_s', 'reason'),
    'step_start': ('step_id', 'step_name'),
    'step_end': ('step_id', 'step_name', 'status', 'duration_s'),
    'session_close': ('reason', 'status', 'exit_code'),
    'policy_signal': ('severity', 'policy', 'reason', 'detail'),
    'supervisor_state_changed': ('old_state', 'new_state', 'reason'),
}

# Opaque identifiers must never encode a path separator or a secret marker.
# ``key`` is restored so 'api_key', 'private', etc. never shape an opaque ID.
_FORBIDDEN_ID_MARKERS = ('/', '\\', 'token', 'key', 'secret', 'private',
                         'password')

_RFC3339_RE = re.compile(
    r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$'
)


class SessionValidationError(ValueError):
    """Stable, bounded validation error with a machine-readable code.

    ``code`` is a short stable token (for example ``invalid_event_kind``) that
    adapters can match on; ``detail`` is a bounded human-readable explanation
    that never embeds raw input values, secrets, paths, or tracebacks.
    ``str(error)`` is always equal to ``code``, so a plain-text consumer can
    never extract raw input from the exception.
    """

    def __init__(self, code: str, detail: str = ''):
        self.code = code
        self.detail = detail[:200]
        super().__init__(code)


# ---------------------------------------------------------------------------
# Quality transitions
# ---------------------------------------------------------------------------

def validate_quality_transition(old_quality: str, new_quality: str) -> str:
    """Validate a capture-quality transition.

    Quality is ordered ``exact > structured > best_effort``.  A transition may
    only stay equal or degrade; ``best_effort`` may never upgrade.  Returns the
    new quality on success and raises :class:`SessionValidationError`
    otherwise, with a stable ``invalid_capture_quality`` /
    ``illegal_quality_upgrade`` code.
    """
    if old_quality not in CAPTURE_QUALITIES:
        raise SessionValidationError('invalid_capture_quality',
                                     'unknown capture quality')
    if new_quality not in CAPTURE_QUALITIES:
        raise SessionValidationError('invalid_capture_quality',
                                     'unknown capture quality')
    old_index = CAPTURE_QUALITIES.index(old_quality)
    new_index = CAPTURE_QUALITIES.index(new_quality)
    if new_index < old_index:
        raise SessionValidationError('illegal_quality_upgrade',
                                     'illegal capture-quality upgrade')
    return new_quality


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _is_opaque_shape(value: str) -> bool:
    lowered = value.lower()
    return not any(marker in lowered for marker in _FORBIDDEN_ID_MARKERS)


def _opaque(value: Any, name: str) -> str:
    """Validate an opaque identifier: string, bounded, not a path/secret."""
    if not isinstance(value, str) or not value:
        raise SessionValidationError('invalid_opaque_id',
                                     f'{name} must be a non-empty opaque id')
    if len(value) > _MAX_OPAQUE_ID:
        raise SessionValidationError('invalid_opaque_id',
                                     f'{name} is too long')
    if not _is_opaque_shape(value):
        raise SessionValidationError('invalid_opaque_id',
                                     f'{name} must not encode a path or secret')
    return value


def _public_id(value: Any) -> str:
    """Return a safe opaque id for a public DTO, or '' if it is not one.

    Path separators and secret markers mean the value cannot be an opaque id
    and must not reach a public surface; redact it to an empty string instead
    of leaking the raw text.
    """
    if not isinstance(value, str) or not value:
        return ''
    if len(value) > _MAX_OPAQUE_ID:
        return ''
    if not _is_opaque_shape(value):
        return ''
    return value


def is_valid_session_id(value: Any) -> bool:
    """Single session-id boundary rule shared by every repository.

    A session id must be a non-empty, bounded opaque string: no path
    separators, no path/DB-shaped fragments (``..``, ``.json``, ``.db``),
    and no secret markers.  Both :mod:`hub.infrastructure.session_repository`
    and :mod:`hub.infrastructure.transcript_repository` gate ingestion AND
    queries through this predicate so an id accepted on the write path can
    never be rejected on the read path (and vice versa).
    """
    if not isinstance(value, str) or not value:
        return False
    if len(value) > _MAX_OPAQUE_ID:
        return False
    if not _is_opaque_shape(value):
        return False
    lowered = value.lower()
    return not any(f in lowered for f in ('..', '.json', '.db'))


def _is_rfc3339(value: str) -> bool:
    """True when ``value`` is a valid RFC3339 timestamp."""
    if not isinstance(value, str) or not _RFC3339_RE.fullmatch(value):
        return False
    try:
        datetime.fromisoformat(value.replace('Z', '+00:00'))
        return True
    except ValueError:
        return False


def _json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=True))


def _bounded_text(value: Any, limit: int) -> str:
    if value is None:
        return ''
    return str(value)[:limit]


def _payload_text(value: Any, name: str, limit: int) -> str:
    """Bounded text for a payload/event field; rejects nested containers.

    A dict/list/tuple/set value in a payload field would otherwise be
    stringified by ``str()`` and could retain a nested token, path, credential,
    or raw collector output directly inside the public event.  Instead the
    event is rejected with a stable ``invalid_payload_type`` code so an ingest
    pipeline can classify the failure without ever exposing the raw value.
    """
    if isinstance(value, (dict, list, tuple, set)):
        raise SessionValidationError('invalid_payload_type',
                                     f'{name} must be a primitive value')
    if value is None:
        return ''
    return str(value)[:limit]


def normalize_managed(value: Any) -> bool:
    """Strict ``managed`` normalization for session rows and specs.

    Only an actual boolean ``True`` — or the integer ``1`` that JSON
    transports naturally when a spec explicitly writes ``"managed": 1`` — is
    managed.  Every other value, including the strings ``'false'``, ``'0'``,
    ``'no'``, ``'False'``, and ``'true'``, renders the session unmanaged: the
    DTO then reports ``capture_quality='best_effort'`` with
    ``control_capability='unavailable'`` and ``validate_session_spec`` treats
    it as an unmanaged best-effort session.
    """
    if value is True:
        return True
    if isinstance(value, int) and not isinstance(value, bool) and value == 1:
        return True
    return False


def _payload(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = event.get('payload')
    if payload is None:
        return {}
    if not isinstance(payload, Mapping):
        raise SessionValidationError('invalid_payload_type',
                                     'payload must be an object')
    return dict(payload)


def _tool_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a tool_call/tool_result payload to the allowlist."""
    return {
        'tool_name': _payload_text(payload.get('tool_name'), 'tool_name', 128),
        'call_id': _payload_text(payload.get('call_id'), 'call_id', 128),
        'status': _payload_text(payload.get('status'), 'status', 64),
        'arguments': _payload_text(payload.get('arguments'), 'arguments',
                                   _MAX_TOOL_FIELD),
        'result': _payload_text(payload.get('result'), 'result',
                                _MAX_TOOL_FIELD),
        'digest': _payload_text(payload.get('digest'), 'digest', 128),
    }


def _sane_redaction(redaction: Mapping[str, Any]) -> dict[str, Any]:
    """Allowlist the redaction metadata to bounded public values."""
    state = redaction.get('state')
    if not isinstance(state, str) or state not in ('none', 'redacted', 'partial'):
        state = 'none'
    clean: dict[str, Any] = {
        'state': state,
        'uncertain': bool(redaction.get('uncertain', False)),
    }
    rules = redaction.get('rules')
    if isinstance(rules, list):
        rule_values: list[str] = []
        for rule in rules[:64]:
            if isinstance(rule, (dict, list, tuple, set)):
                rule_values.append('')
            else:
                rule_values.append(str(rule)[:64])
        clean['rules'] = rule_values
    else:
        clean['rules'] = []
    return clean


def _generic_payload(payload: Mapping[str, Any], kind: str) -> dict[str, Any]:
    """Keep only the allowlisted public fields for a generic event kind.

    The per-kind allowlist already restricts the output; as defense in depth,
    any key that names a PID, command/argv/env/cwd, raw collector output,
    credential, or filesystem path is dropped even if it were ever allowlisted.
    Numeric allowlisted fields are preserved; other values are bounded.
    """
    allowed = _GENERIC_PAYLOAD_FIELDS.get(kind, ())
    clean: dict[str, Any] = {}
    for field in allowed:
        if field in _FORBIDDEN_PAYLOAD_KEYS:
            continue
        if field in payload:
            value = payload[field]
            if isinstance(value, (bool, int, float)):
                clean[field] = value
            else:
                clean[field] = _payload_text(value, field, 200)
    return clean


# ---------------------------------------------------------------------------
# Event validation
# ---------------------------------------------------------------------------

def validate_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one normalized session event.

    Returns the sanitized event as a plain ``dict`` with unknown top-level
    fields dropped.  Raises :class:`ValueError` for unknown event kinds,
    invalid/missing identity fields, invalid capture quality, non-positive or
    non-integer sequences, non-RFC3339 ``emitted_at``, payloads that exceed the
    single-event byte cap, or impossible capture-quality transitions.
    """
    if not isinstance(event, Mapping):
        raise SessionValidationError('invalid_event', 'event must be an object')

    schema_version = event.get('schema_version')
    if schema_version != SCHEMA_VERSION:
        raise SessionValidationError('unsupported_schema_version',
                                     'unsupported schema version')

    for field in _REQUIRED_FIELDS:
        if field not in event:
            raise SessionValidationError('missing_field',
                                         f'missing required field: {field}')

    kind = event['kind']
    if kind not in EVENT_KINDS:
        raise SessionValidationError('invalid_event_kind',
                                     'unknown event kind')

    quality = event['capture_quality']
    if quality not in CAPTURE_QUALITIES:
        raise SessionValidationError('invalid_capture_quality',
                                     'unknown capture quality')

    _opaque(event['event_id'], 'event_id')
    _opaque(event['stream_id'], 'stream_id')
    _opaque(event['machine_id'], 'machine_id')
    _opaque(event['session_id'], 'session_id')

    attempt_id = event.get('attempt_id')
    if attempt_id is not None:
        clean_attempt = _opaque(attempt_id, 'attempt_id')
    else:
        clean_attempt = None

    process_group_id = event.get('process_group_id')
    if process_group_id is not None:
        clean_group = _opaque(process_group_id, 'process_group_id')
    else:
        clean_group = None

    sequence = event['sequence']
    if not isinstance(sequence, int) or isinstance(sequence, bool):
        raise SessionValidationError('invalid_sequence',
                                     'sequence must be an integer')
    if sequence < 1:
        raise SessionValidationError('invalid_sequence',
                                     'sequence must be positive')

    emitted_at = event['emitted_at']
    if not _is_rfc3339(emitted_at):
        raise SessionValidationError('invalid_emitted_at',
                                     'emitted_at must be an RFC3339 timestamp')

    source = event.get('source')
    if source is not None and (not isinstance(source, str) or not source):
        raise SessionValidationError('invalid_source',
                                     'source must be a non-empty string')

    plain_payload = _payload(event)

    # ---- kind-specific payload normalization ------------------------------
    if kind in _TEXT_PAYLOAD_KINDS:
        if 'text' not in plain_payload:
            raise SessionValidationError('missing_payload_text',
                                         'event requires payload.text')
        clean_payload: dict[str, Any] = {
            'text': _payload_text(plain_payload.get('text'), 'text',
                                  _MAX_TEXT),
            'is_complete': bool(plain_payload.get('is_complete', True)),
        }
    elif kind in _TOOL_PAYLOAD_KINDS:
        clean_payload = _tool_payload(plain_payload)
    elif kind == _QUALITY_CHANGE_KIND:
        if 'old_quality' not in plain_payload or 'new_quality' not in plain_payload:
            raise SessionValidationError(
                'missing_quality_fields',
                'capture_quality_changed requires old/new quality')
        old_quality_raw = plain_payload.get('old_quality')
        new_quality_raw = plain_payload.get('new_quality')
        if not isinstance(old_quality_raw, str) or not isinstance(new_quality_raw, str):
            raise SessionValidationError('invalid_payload_type',
                                         'quality fields must be strings')
        validate_quality_transition(old_quality_raw, new_quality_raw)
        clean_payload = {
            'old_quality': old_quality_raw,
            'new_quality': new_quality_raw,
            'reason': _payload_text(plain_payload.get('reason'), 'reason',
                                    _MAX_NOTE),
        }
    elif kind == _CAPTURE_GAP_KIND:
        for field in ('start_sequence', 'end_sequence', 'quality'):
            if field not in plain_payload:
                raise SessionValidationError('missing_gap_field',
                                         f'capture_gap requires payload.{field}')
        start = plain_payload['start_sequence']
        end = plain_payload['end_sequence']
        if not (isinstance(start, int) and isinstance(end, int)
                and not isinstance(start, bool) and not isinstance(end, bool)):
            raise SessionValidationError('invalid_gap_sequence',
                                     'capture_gap sequences must be integers')
        if end < start:
            raise SessionValidationError('invalid_gap_sequence',
                                     'capture_gap end before start')
        gap_quality = plain_payload['quality']
        if not isinstance(gap_quality, str):
            raise SessionValidationError('invalid_payload_type',
                                     'gap quality must be a string')
        if gap_quality not in CAPTURE_QUALITIES:
            raise SessionValidationError('invalid_capture_quality',
                                     'unknown capture quality')
        gap_index = CAPTURE_QUALITIES.index(gap_quality)
        event_index = CAPTURE_QUALITIES.index(quality)
        if gap_index <= event_index:
            raise SessionValidationError(
                'gap_must_degrade',
                'capture_gap must represent quality degradation')
        clean_payload = {
            'start_sequence': start,
            'end_sequence': end,
            'reason': _payload_text(plain_payload.get('reason'), 'reason',
                                    _MAX_NOTE),
            'quality': gap_quality,
        }
    else:
        # Generic kinds (session_start, session_metadata, process_spawn,
        # process_exit, step_start, step_end, session_close, policy_signal,
        # supervisor_state_changed) only keep their allowlisted payload fields.
        # Non-allowlisted fields - PIDs, paths, commands, credentials, raw
        # collector output - are dropped here.
        clean_payload = _generic_payload(plain_payload, kind)

    # ---- single-event size bound (after redaction) ----------------------
    probe_event = dict(event)
    if 'payload' in event:
        probe_event['payload'] = clean_payload
    if _json_bytes(probe_event) > MAX_EVENT_BYTES:
        raise SessionValidationError('event_too_large',
                                     'event exceeds max bytes')

    redaction = event.get('redaction')
    if redaction is not None and not isinstance(redaction, Mapping):
        raise SessionValidationError('invalid_redaction',
                                     'redaction must be an object')

    clean: dict[str, Any] = {
        'schema_version': SCHEMA_VERSION,
        'event_id': event['event_id'],
        'stream_id': event['stream_id'],
        'machine_id': event['machine_id'],
        'session_id': event['session_id'],
        'sequence': sequence,
        'kind': kind,
        'capture_quality': quality,
        'emitted_at': emitted_at,
    }
    if clean_attempt is not None:
        clean['attempt_id'] = clean_attempt
    if clean_group is not None:
        clean['process_group_id'] = clean_group
    if source is not None:
        clean['source'] = _bounded_text(source, 64)
    if clean_payload:
        clean['payload'] = clean_payload
    if redaction is not None:
        clean['redaction'] = _sane_redaction(redaction)
    return clean


def validate_batch(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Validate an event batch against the fixed upper limits.

    Enforces ``MAX_BATCH_EVENTS`` (count) and ``MAX_BATCH_BYTES`` (serialized
    bytes) across the whole validated batch.  Raises :class:`ValueError` on
    any violation so an adapter can reject the request before persisting
    anything.
    """
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        raise SessionValidationError('invalid_batch', 'batch must be a list')
    if len(events) > MAX_BATCH_EVENTS:
        raise SessionValidationError('batch_too_many',
                                     'batch exceeds max event count')

    validated = [validate_event(event) for event in events]
    total = _json_bytes(validated)
    if total > MAX_BATCH_BYTES:
        raise SessionValidationError('batch_too_large',
                                     'batch exceeds max bytes')
    return validated


def try_validate_event(event: Mapping[str, Any]) -> tuple[bool, Any]:
    """Validate a single event without raising.

    Returns ``(True, clean_event)`` on success or ``(False, error_code)`` on
    failure.  ``error_code`` is always a stable, bounded non-secret code such
    as ``invalid_event_kind`` - never ``str(exc)``, never a raw input value,
    path, credential, or traceback.  This is the per-event primitive used by
    :func:`partition_batch` so an ingest pipeline can keep valid events even
    when other events in the batch are malformed.
    """
    try:
        clean = validate_event(event)
    except SessionValidationError as exc:
        return False, exc.code
    except (ValueError, TypeError):
        return False, 'invalid_event'
    return True, clean


def partition_batch(
    events: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Partition an event batch into valid and rejected events.

    Returns ``(valid, rejected)``.  ``valid`` preserves the original order of
    events that passed validation; ``rejected`` is a list of ``{"index": int,
    "code": str}`` entries for malformed events, where ``code`` is a stable,
    bounded, non-secret token (never raw exception text or raw input values).
    This is the preserve-valid-events path for later ingest: a malformed event
    is rejected without dropping the legitimate ones around it.

    Raises :class:`ValueError` only when the whole argument is not a list of
    events at all.
    """
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        raise SessionValidationError('invalid_batch', 'batch must be a list')

    valid: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        ok, result = try_validate_event(event)
        if ok:
            valid.append(result)
        else:
            rejected.append({'index': index, 'code': result})
    return valid, rejected


# ---------------------------------------------------------------------------
# Public session DTO
# ---------------------------------------------------------------------------

def public_session_dto(session: Mapping[str, Any]) -> dict[str, Any]:
    """Build a bounded public session row for operator/UI display.

    Only allowlisted, non-secret fields are returned.  Raw filesystem paths,
    process PIDs, raw collector output, tokens, and secrets are never
    included.  Opaque identifiers (machine_id, session_id, attempt_id,
    process_group_id) plus surface strings (agent_family, status,
    capabilities) are checked for path/secret shapes and redacted/omitted when
    unsafe.  ``capture_quality`` is clamped to the permitted set, and
    unmanaged sessions always emit ``capture_quality='best_effort'`` even when
    the stored value claims ``exact``/``structured``.  ``started_at`` and
    ``updated_at`` are emitted only when they are valid RFC3339 timestamps;
    otherwise they are omitted.
    """
    if not isinstance(session, Mapping):
        return {}

    managed = normalize_managed(session.get('managed', False))
    quality = str(session.get('capture_quality', ''))
    if quality not in CAPTURE_QUALITIES or not managed:
        quality = 'best_effort'

    dto: dict[str, Any] = {
        'machine_id': _public_id(session.get('machine_id')),
        'session_id': _public_id(session.get('session_id')),
        'managed': managed,
        'capture_quality': quality,
        'control_capability': 'available' if managed else 'unavailable',
    }
    if managed:
        attempt = _public_id(session.get('attempt_id'))
        if attempt:
            dto['attempt_id'] = attempt
    group = _public_id(session.get('process_group_id'))
    if group:
        dto['process_group_id'] = group

    for field in ('agent_family', 'status'):
        value = _public_id(session.get(field))
        if value:
            dto[field] = value[:64]
    for field in ('started_at', 'updated_at'):
        value = session.get(field)
        if _is_rfc3339(value):
            dto[field] = value

    capabilities = session.get('capabilities')
    if isinstance(capabilities, list):
        surfaced = []
        for capability in capabilities[:64]:
            safe = _public_id(capability)
            if safe:
                surfaced.append(safe[:64])
        if surfaced:
            dto['capabilities'] = surfaced
    return dto


__all__ = [
    'CAPTURE_QUALITIES',
    'EVENT_KINDS',
    'SCHEMA_VERSION',
    'SessionValidationError',
    'MAX_BATCH_BYTES',
    'MAX_BATCH_EVENTS',
    'MAX_EVENT_BYTES',
    'is_valid_session_id',
    'normalize_managed',
    'partition_batch',
    'public_session_dto',
    'try_validate_event',
    'validate_batch',
    'validate_event',
    'validate_quality_transition',
]