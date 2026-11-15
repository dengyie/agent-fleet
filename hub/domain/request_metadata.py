"""Allowlisted public projection of durable provider request lifecycle events."""
import json
import math
import re
from decimal import Decimal, localcontext
from typing import Any, Iterable, Mapping, cast

from fleet_core.request_metadata import RequestMetadata, normalized_tokens


REQUEST_EVENT_KINDS = ('provider_request_started', 'provider_request_finished')
TERMINAL_STATES = {'succeeded', 'failed', 'unknown', 'cancelled'}
MAX_JSON_DATE_EPOCH_SECONDS = 8_640_000_000_000
# Frozen ``runs.request_summary`` bounds. run_events of old terminal runs are
# pruned by retention; the frozen copy keeps the per-request metadata. At most
# MAX_FROZEN_REQUESTS entries and MAX_FROZEN_REQUEST_BYTES of UTF-8 JSON are
# kept; oldest details are dropped while full totals and the omitted count
# survive. These are presentation/storage bounds, never execution limits.
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


def request_totals(requests: list[RequestMetadata]) -> dict:
    """Account for every request before bounding individual detail retention."""
    keys = tuple(normalized_tokens(None))
    totals = {'count': len(requests), 'complete': True,
              'usage': dict.fromkeys(keys, 0), 'known': dict.fromkeys(keys, 0),
              'display_usage': dict.fromkeys(keys, 0),
              'duration_ms': 0, 'duration_known': 0, 'cost_known': 0,
              'states': {}, 'models': [], 'model_count': 0, 'cost': None}
    models, costs = set(), []
    for request in requests:
        usage = normalized_tokens(request.get('usage'))
        displayed = dict(usage)
        if all(usage[key] is not None for key in ('input_tokens', 'cache_read_tokens', 'cache_write_tokens')):
            displayed['input_tokens'] = max(0, usage['input_tokens'] - usage['cache_read_tokens'] - usage['cache_write_tokens'])
        if usage['output_tokens'] is not None and usage['reasoning_tokens'] is not None:
            displayed['output_tokens'] = max(0, usage['output_tokens'] - usage['reasoning_tokens'])
        for key in keys:
            if usage[key] is not None:
                totals['usage'][key] += usage[key]
                totals['known'][key] += 1
                totals['display_usage'][key] += displayed[key]
        duration = request.get('duration_ms')
        if type(duration) in (int, float) and math.isfinite(duration) and duration >= 0:
            totals['duration_ms'] += duration
            totals['duration_known'] += 1
        state = request.get('status', 'unknown')
        state = state if state in {'running', 'succeeded', 'failed', 'unknown', 'cancelled'} else 'unknown'
        totals['states'][state] = totals['states'].get(state, 0) + 1
        model = request.get('model') or request.get('requested_model')
        models.add(model[:160] if isinstance(model, str) else '未提供')
        cost = request.get('cost')
        if isinstance(cost, dict) and cost.get('currency') == 'USD':
            amount = cost.get('amount')
            if isinstance(amount, str) and len(amount) <= 128 and re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', amount):
                costs.append((Decimal(amount), cost.get('source')))
    totals['models'] = sorted(models)[:8]
    totals['model_count'] = len(models)
    if costs:
        # Keep decimal billing exact even when adding many sub-cent amounts.
        with localcontext() as context:
            context.prec = 256
            totals['cost'] = {'amount': format(sum((item[0] for item in costs), Decimal(0)), 'f'),
                              'currency': 'USD',
                              'source': 'configured' if any(item[1] == 'configured' for item in costs) else 'provider'}
        totals['cost_known'] = len(costs)
    return totals


def freeze_requests(requests: list[RequestMetadata]) -> str:
    """Keep full totals and a bounded tail of individual request details."""
    totals = request_totals(requests)
    kept = list(requests)[-MAX_FROZEN_REQUESTS:]
    budget = MAX_FROZEN_REQUEST_BYTES - len(_dump(totals).encode('utf-8')) - 128
    total = 0
    start = len(kept)
    while start > 0:
        size = len(_dump(kept[start - 1]).encode('utf-8')) + 1
        if total + size > budget:
            break
        total += size
        start -= 1
    kept = kept[start:]
    return _dump({'requests': kept, 'omitted': len(requests) - len(kept), 'totals': totals})


def thaw_request_summary(value: str | None) -> dict:
    try:
        data = json.loads(value or '{}')
    except (TypeError, ValueError):
        data = {}
    items = data.get('requests') if isinstance(data, dict) else None
    requests = [cast(RequestMetadata, item) for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    omitted = data.get('omitted', 0) if isinstance(data, dict) else 0
    omitted = omitted if type(omitted) is int and omitted >= 0 else 0
    totals = data.get('totals') if isinstance(data, dict) else None
    if not isinstance(totals, dict):
        totals = request_totals(requests)
        totals.update(count=len(requests) + omitted, complete=not omitted)
    return {'requests': requests, 'omitted': omitted, 'totals': totals}
