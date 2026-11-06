"""Shared helpers for the platform SQLite store (``hub.infrastructure.platform_db``)."""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from hub.domain.request_metadata import REQUEST_EVENT_KINDS, freeze_requests, project_requests

MAX_JSON_BYTES = 64 * 1024


class PlatformRepositoryError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


def _json(value: Any) -> str:
    try:
        result = json.dumps(
            value or {}, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        )
        encoded_size = len(result.encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise PlatformRepositoryError("invalid_value") from exc
    if encoded_size > MAX_JSON_BYTES:
        raise PlatformRepositoryError("value_too_large")
    return result


def _decode(value: str | None) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return dict(data) if isinstance(data, Mapping) else {}


def freeze_run_request_summary(conn, run_id: str, state: str) -> None:
    """Persist the bounded request projection on ``runs.request_summary``.

    Called inside the writer transaction of every terminal run transition so
    the per-request metadata survives run_events retention pruning.
    """
    rows = conn.execute(
        "SELECT kind,payload FROM run_events WHERE run_id=? AND kind IN (?,?) ORDER BY sequence",
        (run_id, *REQUEST_EVENT_KINDS)).fetchall()
    requests = project_requests([{"kind": row[0], "payload": _decode(row[1])} for row in rows], state)
    conn.execute("UPDATE runs SET request_summary=? WHERE run_id=?", (freeze_requests(requests), run_id))
