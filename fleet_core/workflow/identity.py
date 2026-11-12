"""Deterministic, bounded workflow hashes without I/O or permission decisions."""
from __future__ import annotations

import hashlib
import json
import math
import re

from .errors import WorkflowValidationError

MAX_CANONICAL_BYTES = 65536
MAX_CANONICAL_DEPTH = 32
MAX_REFERENCE_BYTES = 128
_ENCODER = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def validate_reference(value: str) -> str:
    """Validate an internal reference, preserving its exact UTF-8 identity."""
    if type(value) is not str or not value or "\x00" in value or len(value) > MAX_REFERENCE_BYTES:
        raise WorkflowValidationError("invalid_identifier")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise WorkflowValidationError("invalid_identifier") from exc
    if size > MAX_REFERENCE_BYTES:
        raise WorkflowValidationError("invalid_identifier")
    return value


def _json_size(value: object, depth: int = 0, budget: int = MAX_CANONICAL_BYTES) -> int:
    """Bound validation and allocations before the canonical encoder runs."""
    if depth > MAX_CANONICAL_DEPTH:
        raise WorkflowValidationError("json_too_deep")
    if budget < 1:
        raise WorkflowValidationError("json_too_large")
    kind = type(value)
    if kind in (dict, list):
        return _container_size(value, depth, budget)
    if kind not in (str, int, bool, float) and value is not None:
        raise WorkflowValidationError("not_json")
    if kind is str and len(value) + 2 > budget:
        raise WorkflowValidationError("json_too_large")
    if kind is int and value.bit_length() > budget * 4:
        raise WorkflowValidationError("json_too_large")
    if kind is float and not math.isfinite(value):
        raise WorkflowValidationError("invalid_number")
    try:
        size = len(_ENCODER.encode(value).encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise WorkflowValidationError("invalid_unicode") from exc
    except (ValueError, TypeError) as exc:
        raise WorkflowValidationError("invalid_json") from exc
    if size > budget:
        raise WorkflowValidationError("json_too_large")
    return size


def _container_size(value: dict[str, object] | list[object], depth: int, budget: int) -> int:
    size = 2 + max(0, len(value) - 1)
    if size + len(value) > budget:
        raise WorkflowValidationError("json_too_large")
    if type(value) is list:
        for item in value:
            size += _json_size(item, depth + 1, budget - size)
        return size
    for key, item in value.items():
        if type(key) is not str:
            raise WorkflowValidationError("non_string_key")
        size += _json_size(key, depth + 1, budget - size) + 1
        size += _json_size(item, depth + 1, budget - size)
    return size


def canonical_digest(value: object) -> str:
    """Hash sorted, compact UTF-8 JSON up to 64 KiB, excluding NaN and surrogates."""
    _json_size(value)
    digest = hashlib.sha256()
    for chunk in _ENCODER.iterencode(value):
        digest.update(chunk.encode("utf-8"))
    return digest.hexdigest()


def canonical_json(value: object) -> bytes:
    """Freeze an already decoded JSON snapshot within the same canonical bounds."""
    _json_size(value)
    return _ENCODER.encode(value).encode("utf-8")


def dispatch_token(owner_id: str, workflow_id: str, step_key: str, generation: int) -> str:
    """One logical dispatch per generation; coordinator recovery adds no identity."""
    if type(generation) is not int or generation < 1:
        raise WorkflowValidationError("invalid_generation")
    try:
        decimal_generation = str(generation)
    except ValueError as exc:
        raise WorkflowValidationError("invalid_generation") from exc
    parts = (
        "workflow-v1", validate_reference(owner_id), validate_reference(workflow_id),
        validate_reference(step_key), decimal_generation,
    )
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def digest_value(value: str) -> str:
    """Validate the shared delivery/review digest without execution dependencies."""
    if type(value) is not str or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise WorkflowValidationError("invalid_delivery_digest")
    return value
