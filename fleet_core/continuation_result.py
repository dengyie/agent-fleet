"""Bounded continuation-result wire contract, shared by Hub and node."""
from __future__ import annotations

import json
import math
import re
from datetime import datetime
from collections.abc import Mapping

from fleet_core.result_files import redact_content, redact_patch
from fleet_core.task_result import MAX_DIFF_PATCH, bound_patch, normalize_test_summary

MAX_RESULT_BYTES = 150 * 1024
RESULT_FIELDS = frozenset({
    "command_id", "session_id", "task_id", "execution_state", "collection_state",
    "collection_reason", "exit_code", "duration_s", "finished_at", "diff_stat",
    "diff_patch", "diff_truncated", "stat_truncated", "redacted", "test_summary",
})
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WHEN = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z$")


def validate_result(raw: Mapping) -> dict:
    """Strict shape followed by repeated redaction; no opaque vendor fields."""
    if not isinstance(raw, Mapping) or set(raw) != RESULT_FIELDS:
        raise ValueError("invalid_continuation_result")
    value = dict(raw)
    if any(not isinstance(value[key], str) or not _ID.fullmatch(value[key])
           for key in ("command_id", "session_id", "task_id")):
        raise ValueError("invalid_continuation_result")
    if (not isinstance(value["execution_state"], str) or value["execution_state"] not in {"succeeded", "failed", "cancelled", "unknown"}
            or not isinstance(value["collection_state"], str) or value["collection_state"] not in {"complete", "partial", "unavailable"}
            or not isinstance(value["collection_reason"], str) or value["collection_reason"] not in {"", "result_unavailable", "result_too_large", "test_summary_unavailable"}
            or type(value["exit_code"]) is not int or not -(2**31) <= value["exit_code"] < 2**31
            or type(value["duration_s"]) not in (int, float)
            or not 0 <= value["duration_s"] <= 365 * 86400 or not math.isfinite(value["duration_s"])
            or not isinstance(value["finished_at"], str) or not _WHEN.fullmatch(value["finished_at"])
            or any(type(value[key]) is not bool for key in ("diff_truncated", "stat_truncated", "redacted"))):
        raise ValueError("invalid_continuation_result")
    if not isinstance(value["diff_stat"], str) or not isinstance(value["diff_patch"], str):
        raise ValueError("invalid_continuation_result")
    if re.search(r"(?m)^GIT binary patch\s*$", value["diff_patch"]):
        # Encoded binary hunks bypass text redaction and can reconstruct a
        # private file. Nodes publish only Git's "Binary files differ" notice.
        raise ValueError("invalid_continuation_result")
    try:
        datetime.fromisoformat(value["finished_at"].replace("Z", "+00:00"))
        if len(value["diff_stat"].encode("utf-8")) > 5120 or len(value["diff_patch"].encode("utf-8")) > MAX_DIFF_PATCH:
            raise ValueError("invalid_continuation_result")
        value["diff_stat"], report = redact_content(value["diff_stat"])
        value["diff_stat"], cut = bound_patch(value["diff_stat"], 5120)
        value["stat_truncated"] |= cut
        value["redacted"] |= report.replaced > 0
        value["diff_patch"], changed = redact_patch(value["diff_patch"])
        value["redacted"] |= changed
        summary = normalize_test_summary(value["test_summary"])
        if summary is not None:
            summary = dict(summary)
            if "failed_names" in summary:
                summary["failed_names"] = [redact_content(name)[0][:200] for name in summary["failed_names"]]
        value["test_summary"] = summary
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (UnicodeError, OverflowError, TypeError, ValueError, RecursionError) as exc:
        raise ValueError("invalid_continuation_result") from exc
    if len(encoded) > MAX_RESULT_BYTES:
        raise ValueError("invalid_continuation_result")
    if value["collection_state"] == "unavailable" and (value["diff_patch"] or value["diff_stat"] or value["test_summary"]):
        raise ValueError("invalid_continuation_result")
    return value


def metadata(value: dict) -> dict:
    """No patch bodies in the directory: one row cannot amplify list responses."""
    return {key: item for key, item in value.items() if key not in {"diff_patch", "test_summary"}}
