"""Pure session-domain validation, managed/unmanaged rules, and public DTOs.

This module deliberately contains no HTTP, persistence, or process concerns.
It imports the dependency-light shared contract from ``session_schema.py``
(repository root) so the Hub and standalone ``tools/`` share one envelope
allowlist without importing Flask.

Hub-only session policy layers on top of the shared schema:

- managed sessions must carry ``session_id`` plus ``process_group_id``;
- standalone (task-less) sessions still require ``session_id`` and
  ``process_group_id``;
- an unmanaged session is only ever renderable as best-effort with
  ``control_capability == "unavailable"``;
- quality transitions degrade only (``exact > structured > best_effort``), and
  a ``best_effort`` source may never upgrade.
"""

from collections.abc import Mapping
from typing import Any

from session_schema import (
    CAPTURE_QUALITIES,
    normalize_managed as _schema_normalize_managed,
    partition_batch as _schema_partition_batch,
    public_session_dto as _schema_public_session_dto,
    try_validate_event as _schema_try_validate_event,
    validate_batch as _schema_validate_batch,
    validate_event as _schema_validate_event,
    validate_quality_transition as _schema_validate_quality_transition,
)

def _opaque(value: Any, name: str) -> str:
    """Validate a non-empty, bounded opaque session id."""
    if not isinstance(value, str) or not value:
        raise ValueError(f'{name} must be a non-empty opaque id')
    if len(value) > 256:
        raise ValueError(f'{name} is too long')
    lowered = value.lower()
    for marker in ('/', '\\', 'token', 'secret', 'private', 'key'):
        if marker in lowered:
            raise ValueError(f'{name} must not encode a path or secret')
    return value


def validate_quality_transition(old_quality: str, new_quality: str) -> str:
    """Validate a capture-quality transition (exact > structured > best_effort).

    Degrades only; a ``best_effort`` source may never upgrade.  Raises
    :class:`ValueError` on an illegal or unknown quality.
    """
    return _schema_validate_quality_transition(old_quality, new_quality)


def normalize_managed(value: Any) -> bool:
    """Strict ``managed`` normalization shared with the public DTO."""
    return _schema_normalize_managed(value)


def validate_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one session event against the shared contract."""
    return _schema_validate_event(event)


def validate_batch(events) -> list[dict[str, Any]]:
    """Validate an event batch against the shared contract."""
    return _schema_validate_batch(events)


def try_validate_event(event: Mapping[str, Any]) -> tuple[bool, Any]:
    """Validate one event without raising.

    Returns ``(True, clean_event)`` on success or ``(False, bounded_error)``
    on failure so an ingest path can keep valid events even when others in a
    batch are malformed.
    """
    return _schema_try_validate_event(event)


def partition_batch(
    events,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a batch into ``(valid, rejected)`` preserving valid order."""
    return _schema_partition_batch(events)


def validate_session_spec(
    spec: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate and normalize a session specification.

    A managed session must carry ``session_id`` plus ``process_group_id``;
    an unmanaged session is allowed but may only be best-effort and must not
    claim control.  Returns the accepted non-secret spec fields.
    """
    if not isinstance(spec, Mapping):
        raise ValueError('session spec must be an object')

    session_id = _opaque(spec.get('session_id'), 'session_id')
    machine_id = _opaque(spec.get('machine_id'), 'machine_id')

    process_group_id = spec.get('process_group_id')
    if process_group_id is not None:
        process_group_id = _opaque(process_group_id, 'process_group_id')

    managed = normalize_managed(spec.get('managed', False))
    if managed and process_group_id is None:
        raise ValueError('managed session requires process_group_id')

    quality_raw = spec.get('capture_quality', 'best_effort')
    if not isinstance(quality_raw, str):
        raise ValueError('capture_quality must be a string')
    quality = quality_raw
    if quality not in CAPTURE_QUALITIES:
        raise ValueError('invalid capture quality')
    if not managed and quality != 'best_effort':
        raise ValueError(
            'unmanaged session can only be best_effort capture quality')

    clean: dict[str, Any] = {
        'machine_id': machine_id,
        'session_id': session_id,
        'managed': managed,
    }
    if process_group_id is not None:
        clean['process_group_id'] = process_group_id
    if spec.get('attempt_id') is not None and managed:
        clean['attempt_id'] = _opaque(spec['attempt_id'], 'attempt_id')
    if quality in CAPTURE_QUALITIES:
        clean['capture_quality'] = quality
    return clean


def public_session_dto(session: Mapping[str, Any]) -> dict[str, Any]:
    """Return a bounded, credential-free public session row.

    Only allowlisted non-secret fields are returned; raw paths, PIDs, tokens,
    and raw collector output are never included.  Unmanaged sessions are always
    rendered as best-effort and without control capability.
    """
    return _schema_public_session_dto(session)


__all__ = [
    'normalize_managed',
    'partition_batch',
    'public_session_dto',
    'try_validate_event',
    'validate_batch',
    'validate_event',
    'validate_quality_transition',
    'validate_session_spec',
]