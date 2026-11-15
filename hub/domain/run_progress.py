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
            if payload.get('truncated') is True:
                result['truncated'] = True
            text = payload.get('text')
            retained = _text(text)
            if isinstance(text, str) and retained != text:
                result['truncated'] = True
            segment.update(text=retained, complete=True,
                           final=payload.get('response_kind') == 'final')
        elif not segment.get('complete'):
            offset = payload.get('offset')
            if type(offset) is int and offset == len(segment['text']):
                text = payload.get('text')
                delta = _text(text, 4096)
                combined = segment['text'] + delta
                retained = _text(combined)
                if retained != combined or (isinstance(text, str) and delta != text):
                    result['truncated'] = True
                segment['text'] = retained
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
        if kind == 'tool_result' and payload.get('error_code'):
            segment['error_code'] = _text(payload.get('error_code'), 128)
        else:
            segment.pop('error_code', None)
        for field, maximum in (('title', 512), ('input', 4608), ('output', 3584)):
            if isinstance(payload.get(field), str):
                segment[field] = _text(payload[field], maximum)
    if kind == 'provider_retry_scheduled':
        result['retry'] = {key: payload[key] for key in
            ('step', 'retry_index', 'max_retries', 'retry_in_ms', 'provider_status')
            if type(payload.get(key)) is int}
    elif kind in {'assistant_response', 'run_finished', 'run_unknown', 'run_failed'}:
        result.pop('retry', None)
    if len(segments) > 128:
        result['truncated'] = True
    result.update(segments=segments[-128:], sequence=sequence)
    # Trim oldest narration first; tools remain ordered and the newest response
    # remains useful even for a long run. JSON escaping is included in the bound.
    while len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > MAX_PROGRESS_BYTES:
        if len(result['segments']) > 1:
            result['segments'].pop(0)
            result['truncated'] = True
        else:
            text = result['segments'][0].get('text', '')
            # JSON can expand one control character to six bytes. Always
            # shrink instead of repeatedly applying the same fixed cutoff.
            result['segments'][0]['text'] = _text(text, len(text.encode('utf-8')) // 2)
            result['truncated'] = True
    return result
