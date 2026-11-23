"""Bounded Antigravity headless NDJSON projection; no engine/storage access.

Protocol: https://antigravity.google/docs/cli/headless . An invocation owns
its step indices. Final aggregate text is a fallback only; it cannot duplicate
the text already emitted by step updates. Native conversation IDs stay private.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Mapping

_UUID = re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}')
_MAX_TEXT = 262144


def _text(value):
    return value[:_MAX_TEXT] if isinstance(value, str) else ''


class AgyStream:
    def __init__(self):
        self.conversation = None
        self.turn = secrets.token_hex(12)
        self.had_text = False
        self.finished = False

    def _id(self, step):
        return 'agy_' + hashlib.sha256(f'{self.turn}:{step}'.encode()).hexdigest()[:32]

    def normalize(self, row):
        if not isinstance(row, Mapping):
            return []
        event = row.get('event')
        if not isinstance(event, str):
            return []
        if event == 'init':
            cid = row.get('conversation_id')
            if not isinstance(cid, str) or not _UUID.fullmatch(cid):
                return []
            self.conversation, self.turn = cid, secrets.token_hex(12)
            self.had_text, self.finished = False, False
            return []
        if not self.conversation or self.finished:
            return []
        payload = row.get(event) if event in {'step_update', 'result'} else None
        if not isinstance(payload, Mapping) or payload.get('conversation_id') != self.conversation:
            return []
        if event == 'result':
            self.finished = True
            text = _text(payload.get('response'))
            return [('assistant_message', {'text': text, 'message_id': self._id('result'),
                'content_mode': 'snapshot', 'is_complete': True})] if text and not self.had_text else []
        index, state, kind = payload.get('step_index'), payload.get('state'), payload.get('step_type')
        if (type(index) is not int or not 0 <= index <= 1_000_000
                or not isinstance(state, str) or state not in {'ACTIVE', 'DONE'}):
            return []
        complete = state == 'DONE'
        if kind == 'agent_response':
            text = _text(payload.get('text_delta'))
            if text:
                self.had_text = True
            return [('assistant_message', {'text': text, 'message_id': self._id(index),
                'content_mode': 'delta', 'is_complete': complete})] if text or complete else []
        if kind == 'tool':
            info = payload.get('tool_info')
            if not isinstance(info, Mapping):
                return []
            name = _text(info.get('name') or payload.get('tool_name'))[:128]
            call = {'tool_name': name, 'call_id': self._id(index)}
            parameters = info.get('parameters')
            arguments = json.dumps(parameters, ensure_ascii=False)[:_MAX_TEXT] if parameters is not None else ''
            events = [('tool_call', {**call, 'arguments': arguments, 'status': 'running'})]
            if complete:
                error = info.get('error')
                output = _text(info.get('output'))
                if isinstance(error, Mapping):
                    output = '\n'.join(filter(None, (output, _text(error.get('message')))))[:_MAX_TEXT]
                events.append(('tool_result', {**call, 'result': output,
                    'status': 'error' if error else 'ok'}))
            return events
        return []
