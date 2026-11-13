"""Bounded JSON decoding before workflow schema validation; no I/O."""
from __future__ import annotations

import json
import math
from typing import Any

from .errors import WorkflowValidationError
from .identity import MAX_CANONICAL_DEPTH, canonical_json


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WorkflowValidationError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise WorkflowValidationError("invalid_number")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise WorkflowValidationError("invalid_number")
    return number


def _check_depth(raw: bytes) -> None:
    depth = 0
    quoted = escaped = False
    for char in raw:
        if quoted:
            if escaped:
                escaped = False
            elif char == 92:
                escaped = True
            elif char == 34:
                quoted = False
            continue
        if char == 34:
            quoted = True
        elif char in (91, 123):
            depth += 1
        elif char in (93, 125):
            depth -= 1
        if depth > MAX_CANONICAL_DEPTH + 1:
            raise WorkflowValidationError("json_too_deep")


def decode_snapshot(raw: bytes, *, max_bytes: int = 65536) -> dict[str, Any]:
    if type(raw) is not bytes:
        raise WorkflowValidationError("invalid_json_bytes")
    if len(raw) > max_bytes:
        raise WorkflowValidationError("json_too_large")
    _check_depth(raw)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkflowValidationError("invalid_unicode") from exc
    try:
        value = json.loads(
            text, object_pairs_hook=_unique_object,
            parse_constant=_reject_constant, parse_float=_finite_float,
        )
    except WorkflowValidationError:
        raise
    except (ValueError, RecursionError) as exc:
        raise WorkflowValidationError("invalid_json") from exc
    if type(value) is not dict:
        raise WorkflowValidationError("invalid_json_object")
    canonical_json(value)
    return value
