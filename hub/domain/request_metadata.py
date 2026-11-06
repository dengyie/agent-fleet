"""Allowlisted public projection of durable provider request lifecycle events."""
import json
import math
from typing import Any, Iterable, Mapping, cast

from fleet_core.request_metadata import RequestMetadata, normalized_tokens


REQUEST_EVENT_KINDS = ('provider_request_started', 'provider_request_finished')
TERMINAL_STATES = {'succeeded', 'failed', 'unknown', 'cancelled'}
MAX_JSON_DATE_EPOCH_SECONDS = 8_640_000_000_000
# Frozen ``runs.request_summary`` bounds. run_events of old terminal runs are
# pruned by retention; the frozen copy keeps the per-request metadata. At most
# MAX_FROZEN_REQUESTS entries and MAX_FROZEN_REQUEST_BYTES of UTF-8 JSON are
# kept; when over either cap the OLDEST requests are dropped (the latest
# attempt/steps stay visible) and ``omitted`` records how many were dropped.
MAX_FROZEN_REQUESTS = 200
MAX_FROZEN_REQUEST_BYTES = 48 * 1024


def project_requests(events: Iterable[Mapping[str, Any]], state: str) -> list[RequestMetadata]:
    records: dict[tuple[int, ...], RequestMetadata] = {}
    for event in events:
        data = event.get('payload') or {}
        key = tuple(data.get(k) for k in ('attempt', 'step', 'request_index'))
        if not all(type(x) is int and 1 <= x <= 1_000_000 for x in key):
            continue
        record = cast(RequestMetadata, {k: data[k] for k in (
            'attempt', 'step', 'request_index', 'model', 'requested_model', 'provider',
            'started_at', 'duration_ms', 'status', 'parameters', 'response_id',
            'response_model', 'finish_reason', 'http_status', 'provider_error', 'retryable', 'upstream_code',
            'usage', 'cost', 'cost_unavailable_reason') if k in data})
        record['request_id'] = ':'.join(str(x) for x in key)
        record['usage'] = normalized_tokens(data.get('usage'))
        if 'started_at' in record:
            started_at = record['started_at']
            try:
                timestamp = float(started_at) if type(started_at) in (int, float) else None
            except (OverflowError, ValueError):
                timestamp = None
            if (timestamp is None or not math.isfinite(timestamp)
                    or abs(timestamp) > MAX_JSON_DATE_EPOCH_SECONDS):
                record.pop('started_at')
            else:
                record['started_at'] = timestamp
        if record.get('status') == 'running' and state in TERMINAL_STATES:
            record['status'] = 'unknown'
        records[key] = record
    return [records[key] for key in sorted(records)]


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def freeze_requests(requests: list[RequestMetadata]) -> str:
    """Bounded JSON envelope ``{"requests": [...], "omitted": n}``."""
    kept = list(requests)[-MAX_FROZEN_REQUESTS:]
    budget = MAX_FROZEN_REQUEST_BYTES - 64  # envelope + omitted counter
    total = 0
    start = len(kept)
    while start > 0:
        size = len(_dump(kept[start - 1]).encode('utf-8')) + 1
        if total + size > budget:
            break
        total += size
        start -= 1
    kept = kept[start:]
    return _dump({'requests': kept, 'omitted': len(requests) - len(kept)})


def thaw_requests(value: str | None) -> list[RequestMetadata]:
    try:
        data = json.loads(value or '{}')
    except (TypeError, ValueError):
        return []
    items = data.get('requests') if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [cast(RequestMetadata, item) for item in items if isinstance(item, dict)]
