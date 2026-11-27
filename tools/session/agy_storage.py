"""Read-only, bounded projection of observed Antigravity CLI SQLite history.

Field numbers follow gemini_coder.Step and cortex_pb/codeium_common_pb in the
installed CLI schema. Never recursively decode arbitrary embedded objects:
reasoning, signatures, system/context, credentials and generator/executor data
are deliberately outside this allowlist. Native IDs/paths stay node-private.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from fleet_core.session.redact import redact_secrets_text
from tools.session.native_metadata import _safe_file_info
from tools.session.native_storage import _database
from tools.session.native_projection import digest, tool_field as _tool_field

MAX_BLOB = 2 << 20
MAX_PAGE_BYTES = 4 << 20
PAGE_ROWS = 32
_UUID = re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}')
TERMINAL = frozenset({3, 4, 5, 6, 7, 12})
_STATUSES = TERMINAL | {1, 2, 8, 9, 11}


def _varint(data, pos):
    value = 0
    for shift in range(0, 70, 7):
        if pos >= len(data):
            raise ValueError('agy_proto_invalid')
        byte = data[pos]
        pos += 1
        if shift == 63 and byte > 1:
            raise ValueError('agy_proto_invalid')
        value |= (byte & 127) << shift
        if byte < 128:
            return value, pos
    raise ValueError('agy_proto_invalid')


def fields(data):
    """One bounded protobuf message; unknown wire fields are never interpreted."""
    if not isinstance(data, bytes) or len(data) > MAX_BLOB:
        raise ValueError('agy_blob_invalid')
    pos, result, count = 0, {}, 0
    while pos < len(data):
        tag, pos = _varint(data, pos)
        number, wire = tag >> 3, tag & 7
        count += 1
        if not 1 <= number < (1 << 29) or count > 65536:
            raise ValueError('agy_proto_invalid')
        if wire == 0:
            value, pos = _varint(data, pos)
        elif wire in {1, 2, 5}:
            size, pos = _varint(data, pos) if wire == 2 else (8 if wire == 1 else 4, pos)
            if size > len(data) - pos:
                raise ValueError('agy_proto_invalid')
            value, pos = data[pos:pos + size], pos + size
        else:
            raise ValueError('agy_proto_invalid')
        result.setdefault((number, wire), []).append(value)
    return result


def _one(message, number, wire=2, default=b''):
    values = message.get((number, wire), [])
    if len(values) > 1:
        raise ValueError('agy_proto_ambiguous')
    return values[0] if values else default


def _text(message, number):
    return _one(message, number).decode('utf-8')


def _message(message, number):
    return fields(_one(message, number))


def _timestamp(metadata):
    stamp = _message(metadata, 1)
    seconds, nanos = _one(stamp, 1, 0, 0), _one(stamp, 2, 0, 0)
    if not seconds:
        return None
    if not 0 <= nanos < 1_000_000_000:
        raise ValueError('agy_timestamp_invalid')
    try:
        return datetime.fromtimestamp(seconds + nanos / 1e9, timezone.utc).isoformat(
            timespec='milliseconds').replace('+00:00', 'Z')
    except (ValueError, OSError, OverflowError):
        raise ValueError('agy_timestamp_invalid') from None


def _id(token, index, suffix):
    return 'agy_' + hashlib.sha256(f'{token}\0{index}\0{suffix}'.encode()).hexdigest()[:40]


def _event(kind, **payload):
    return {'kind': kind, 'payload': payload}


def _tool_result(kind, step):
    """Explicit visible result fields only; never stringify the protobuf tree."""
    if kind == 132:
        return _text(_message(_message(step, 140), 2), 1)
    field = {5: 10, 7: 13, 8: 14, 9: 15, 21: 28, 33: 42}[kind]
    body = _message(step, field)
    if kind == 8:
        return _text(body, 4)
    if kind == 7:
        return _text(body, 3)
    if kind == 9:
        return '\n'.join(_text(fields(row), 1) for row in body.get((3, 2), []))
    if kind == 21:
        return (_text(_message(body, 26), 1) or _text(_message(body, 21), 1)
                or '\n'.join(filter(None, (_text(body, 4), _text(body, 5)))))
    if kind == 33:
        return _text(body, 5)
    # Code-action description is visible; internal patch/application metadata is
    # not a tool output. Native call arguments still contain the requested edit.
    return _text(body, 26)


def project_step(token, index, kind, status, payload, metadata):
    step, meta = fields(payload), fields(metadata or b'')
    if (_one(step, 1, 0, 0) != kind or _one(step, 4, 0, 0) != status
            or status not in _STATUSES):
        raise ValueError('agy_step_invalid')
    emitted = _timestamp(meta)
    events = []
    model = _text(_message(meta, 24), 8) or _text(_message(meta, 24), 20)
    if model:
        events.append(_event('session_metadata', model=model[:160]))
    if kind in {14, 15}:
        body = _message(step, 19 if kind == 14 else 20)
        if kind == 14:
            # user_response and repeated text items are alternate representations.
            text = _text(body, 2) or _text(body, 1) or '\n'.join(
                _text(fields(item), 1) for item in body.get((3, 2), []))
        else:
            text = _text(body, 8) or _text(body, 1)
        text = redact_secrets_text(text)[0]
        for block, start in enumerate(range(0, len(text), 4000)):
            events.append(_event('user_message' if kind == 14 else 'assistant_message',
                text=text[start:start + 4000], message_id=_id(token, index, f'message:{block}'),
                content_mode='snapshot', is_complete=status in TERMINAL))
    elif kind in {5, 7, 8, 9, 21, 33, 132}:
        call = _message(meta, 4)
        name = _text(call, 2) or {5: 'code_action', 7: 'grep_search', 8: 'view_file',
            9: 'list_directory', 21: 'run_command', 33: 'search_web', 132: 'tool'}[kind]
        arguments, archive = _tool_field(_text(call, 3))
        events.extend(archive)
        binding = {'call_id': _id(token, index, 'tool'), 'tool_name': name[:128]}
        events.append(_event('tool_call', **binding, arguments=arguments,
                             status='running' if status not in TERMINAL else 'completed'))
        if status in TERMINAL:
            result, archive = _tool_field(_tool_result(kind, step))
            events.extend(archive)
            events.append(_event('tool_result', **binding, result=result,
                                 status='ok' if status == 3 else 'error'))
    elif kind == 17:
        body = _message(step, 24)
        if _one(body, 5, 0, 0):
            text = _text(_message(body, 3), 1)
            if text:
                events.append(_event('assistant_message', text=redact_secrets_text(text)[0][:4000],
                    message_id=_id(token, index, 'error'), content_mode='snapshot', is_complete=True))
    elif kind not in {1, 2, 23, 98, 101}:
        raise ValueError('agy_step_unsupported')
    for offset, event in enumerate(events):
        if emitted:
            event['emitted_at'] = emitted
        event['event_id'] = 'evt_' + digest([token, index, offset, event])[:40]
    return events


def _summary(path):
    database = path.parent.parent / 'conversation_summaries.db'
    if not database.exists():
        return {}
    _safe_file_info(database)
    with _database(database) as db:
        rows = db.execute('SELECT substr(title,1,161),substr(preview,1,161) '
                          'FROM conversation_summaries WHERE conversation_id=? LIMIT 2',
                          (path.stem,)).fetchall()
    if len(rows) != 1:
        return {}
    title = rows[0][0] or rows[0][1]
    return {'title': title[:160]} if isinstance(title, str) and title.strip() else {}


def read_page(path, cursor, *, limit=PAGE_ROWS):
    """Return a bounded step snapshot, metadata and EOF flag; active row stops it.

    ``idx`` starts at zero. Cursor -1 is only the initial position. A row updated
    in place stays ahead of the cursor until terminal. Unsupported/corrupt data
    fails closed, preserving the previous durable cursor for repair/retry.
    """
    path = Path(path).absolute()
    if not _UUID.fullmatch(path.stem) or path.suffix != '.db':
        raise ValueError('agy_identity_invalid')
    if type(cursor) is not int or not -1 <= cursor < (1 << 63):
        raise ValueError('agy_cursor_invalid')
    if type(limit) is not int or not 1 <= limit <= PAGE_ROWS:
        raise ValueError('agy_limit_invalid')
    _safe_file_info(path)
    with _database(path) as db:
        db.execute('BEGIN')
        identity = db.execute('SELECT cascade_id FROM trajectory_meta LIMIT 2').fetchall()
        if len(identity) != 1 or identity[0][0] != path.stem:
            raise ValueError('agy_identity_invalid')
        schema = db.execute('PRAGMA table_info(steps)').fetchmany(129)
        if len(schema) > 128 or not any(row[1] == 'idx' and row[2].upper() == 'INTEGER'
                                       and row[5] == 1 for row in schema):
            raise ValueError('agy_schema_unsupported')
        first = db.execute('SELECT idx,substr(metadata,1,?) FROM steps ORDER BY idx LIMIT 1',
                           (MAX_BLOB + 1,)).fetchone()
        last = db.execute('SELECT idx FROM steps ORDER BY idx DESC LIMIT 1').fetchone()
        if (first and (type(first[0]) is not int or first[0] < 0)):
            raise ValueError('agy_step_invalid')
        if cursor >= 0 and (last is None or last[0] < cursor):
            raise ValueError('agy_history_rewound')
        query = db.execute('SELECT idx,step_type,status,substr(step_payload,1,?),'
            'substr(metadata,1,?),step_format FROM steps WHERE idx>? ORDER BY idx LIMIT ?',
            (MAX_BLOB + 1, MAX_BLOB + 1, cursor, limit + 1))
        rows, total, more = [], 0, False
        for row in query:
            size = len(row[3] or b'') + len(row[4] or b'')
            if len(rows) == limit or (rows and total + size > MAX_PAGE_BYTES):
                more = True
                break
            rows.append(row)
            total += size
            if row[2] not in TERMINAL:
                more = True
                break
    metadata = _summary(path)
    if first:
        started = _timestamp(fields(first[1] or b''))
        if started:
            metadata['native_started_at'] = started
    result = []
    for index, kind, status, payload, meta, format_ in rows:
        if type(index) is not int or index < 0 or format_ != 0:
            raise ValueError('agy_step_invalid')
        events = project_step(path.stem, index, kind, status, payload, meta)
        result.append((index, status, events))
        if status not in TERMINAL:
            return metadata, result, False
    return metadata, result, not more
