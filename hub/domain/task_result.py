"""Pure byte contract shared by task-result producers and consumers."""
from __future__ import annotations

import json
import math
from typing import Any

MAX_DIFF_PATCH = 102400
MAX_TEST_SUMMARY = 20480
PATCH_TRUNCATION_SUFFIX = "\n…[truncated]"


class PatchError(ValueError):
    """A patch is not representable by the bounded UTF-8 contract."""


class TestSummaryError(ValueError):
    """A present adapter report cannot be accepted as bounded evidence."""


def _reject_json_constant(_value: str) -> None:
    raise ValueError("invalid_test_summary")


def _validate_json_numbers(value: Any) -> None:
    pending = [value]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if isinstance(current, float):
            if not math.isfinite(current):
                raise TestSummaryError("invalid_test_summary")
            continue
        if isinstance(current, (dict, list, tuple)):
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            children = current.values() if isinstance(current, dict) else current
            pending.extend(children)


def bound_patch(text: str, max_bytes: int = MAX_DIFF_PATCH) -> tuple[str, bool]:
    if not isinstance(text, str):
        raise PatchError("invalid_diff_patch")
    if type(max_bytes) is not int or max_bytes < 0:
        raise PatchError("invalid_patch_limit")
    try:
        encoded = text[:max_bytes + 1].encode("utf-8")
    except UnicodeError as exc:
        raise PatchError("invalid_diff_patch") from exc
    if len(text) <= max_bytes and len(encoded) <= max_bytes:
        return text, text.endswith(PATCH_TRUNCATION_SUFFIX)
    marker = PATCH_TRUNCATION_SUFFIX.encode("utf-8")
    if len(marker) > max_bytes:
        marker = b""
    # The source was encoded strictly; only an incomplete final code point
    # can be dropped by this decode at the byte boundary.
    prefix = encoded[:max_bytes - len(marker)].decode("utf-8", errors="ignore")
    return prefix + marker.decode("utf-8"), True


def normalize_test_summary(raw: Any) -> dict[str, Any] | None:
    """Project documented scalar fields without corrupting JSON or precision."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        if len(raw) > MAX_TEST_SUMMARY:
            raise TestSummaryError("invalid_test_summary")
        try:
            if len(raw.encode("utf-8")) > MAX_TEST_SUMMARY:
                raise TestSummaryError("invalid_test_summary")
            raw = json.loads(raw, parse_constant=_reject_json_constant)
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise TestSummaryError("invalid_test_summary") from exc
    if not isinstance(raw, dict):
        raise TestSummaryError("invalid_test_summary")
    _validate_json_numbers(raw)
    result: dict[str, Any] = {}
    if "framework" in raw:
        framework = raw["framework"]
        if not isinstance(framework, str):
            raise TestSummaryError("invalid_test_summary")
        result["framework"] = framework if framework in ("pytest", "unittest", "unknown") else "unknown"
    for key in ("passed", "failed", "skipped", "errors"):
        if key not in raw:
            continue
        value = raw[key]
        if type(value) is not int or not 0 <= value <= 2**53 - 1:
            raise TestSummaryError("invalid_test_summary")
        result[key] = value
    if "duration_s" in raw:
        value = raw["duration_s"]
        if type(value) not in (int, float):
            raise TestSummaryError("invalid_test_summary")
        try:
            duration = float(value)
        except OverflowError as exc:
            raise TestSummaryError("invalid_test_summary") from exc
        if not math.isfinite(duration) or duration < 0:
            raise TestSummaryError("invalid_test_summary")
        result["duration_s"] = duration
    if "failed_names" not in raw:
        return result or None
    names = raw["failed_names"]
    if not isinstance(names, (list, tuple)):
        raise TestSummaryError("invalid_test_summary")
    result["failed_names"] = []
    used = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    for name in names[:20]:
        if not isinstance(name, str):
            raise TestSummaryError("invalid_test_summary")
        name = name[:200]
        if not name:
            continue
        try:
            size = len(json.dumps(name, ensure_ascii=False).encode("utf-8"))
        except UnicodeError as exc:
            raise TestSummaryError("invalid_test_summary") from exc
        size += bool(result["failed_names"])
        if used + size > MAX_TEST_SUMMARY:
            break
        result["failed_names"].append(name)
        used += size
    return result
