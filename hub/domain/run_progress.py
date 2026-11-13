"""Bounded public assistant progress, independent of private runtime checkpoints."""
from __future__ import annotations

import json

PROGRESS_KINDS = frozenset({
    'assistant_text_delta', 'assistant_response', 'tool_call', 'tool_result',
    'provider_retry_scheduled', 'provider_request_started', 'provider_request_finished',
    'run_started', 'run_waiting_node', 'run_finished',
    'run_unknown', 'run_failed',
})
MAX_PROGRESS_BYTES = 48 * 1024


def _text(value, limit=24576):
    return value.encode('utf-8')[:limit].decode('utf-8', errors='ignore') if isinstance(value, str) else ''


def project_progress(previous, kind, payload, sequence):
    """Reduce only explicitly public fields; retain partial text at all outcomes."""
    if kind not in PROGRESS_KINDS:
        return previous or {}
    result = dict(previous or {})
    segments = [dict(item) for item in result.get('segments', [])]
    step = payload.get('step', 0)
    step = step if type(step) is int and 0 <= step <= 10000 else 0
    if kind in {'assistant_text_delta', 'assistant_response'}:
        key = f'text:{step}'
        segment = next((item for item in segments if item.get('id') == key), None)
        if segment is None:
            segment = {'id': key, 'kind': 'text', 'step': step, 'text': ''}
            segments.append(segment)
        if kind == 'assistant_response':
            segment.update(text=_text(payload.get('text')), complete=True,
                           final=payload.get('response_kind') == 'final')
        elif not segment.get('complete'):
            offset = payload.get('offset')
            if type(offset) is int and offset == len(segment['text']):
                segment['text'] = _text(segment['text'] + _text(payload.get('text'), 4096))
    elif kind in {'tool_call', 'tool_result'}:
        key = _text(payload.get('command_id'), 256)
        segment = next((item for item in segments if item.get('id') == key), None)
        if segment is None:
            segment = {'id': key, 'kind': 'tool', 'step': step}
            segments.append(segment)
        state = payload.get('state')
        segment.update(tool=_text(payload.get('tool'), 160),
                       state='running' if kind == 'tool_call' else
                       state if state in {'succeeded', 'failed', 'unknown', 'cancelled'} else 'failed')
    if kind == 'provider_retry_scheduled':
        result['retry'] = {key: payload[key] for key in
            ('step', 'retry_index', 'max_retries', 'retry_in_ms', 'provider_status')
            if type(payload.get(key)) is int}
    elif kind in {'assistant_response', 'run_finished', 'run_unknown', 'run_failed'}:
        result.pop('retry', None)
    result.update(segments=segments[-128:], sequence=sequence)
    # Trim oldest narration first; tools remain ordered and the newest response
    # remains useful even for a long run. JSON escaping is included in the bound.
    while len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > MAX_PROGRESS_BYTES:
        if len(result['segments']) > 1:
            result['segments'].pop(0)
            result['truncated'] = True
        else:
            result['segments'][0]['text'] = _text(result['segments'][0].get('text'), 8192)
    return result
