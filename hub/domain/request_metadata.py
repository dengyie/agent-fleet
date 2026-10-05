"""Allowlisted public projection of durable provider request lifecycle events."""
from typing import Any, Iterable, Mapping, cast

from tools.platform.request_metadata import RequestMetadata, normalized_tokens


REQUEST_EVENT_KINDS = ('provider_request_started', 'provider_request_finished')
TERMINAL_STATES = {'succeeded', 'failed', 'unknown', 'cancelled'}


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
        if record.get('status') == 'running' and state in TERMINAL_STATES:
            record['status'] = 'unknown'
        records[key] = record
    return [records[key] for key in sorted(records)]
