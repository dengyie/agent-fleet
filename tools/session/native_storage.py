"""Bounded read-only projections of exact ZCode and Hermes native sessions.

No most-recent-session guesses and no writes to vendor storage. Only public
conversation text and tool evidence are projected; reasoning/system data stays
out of the capture stream.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

MAX_ROWS = 4096
MAX_BYTES = 4 << 20
MAX_FIELD = 262144


def database_path(family, environment):
    home = Path(environment.get('HOME') or environment.get('USERPROFILE') or Path.home())
    if family == 'zcode':
        return Path(environment.get('ZCODE_DATA_BASE_DIR') or home) / '.zcode/cli/db/db.sqlite'
    return Path(environment.get('HERMES_HOME') or home / '.hermes') / 'state.db'


@contextmanager
def _database(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=.5)
    try:
        db.execute('PRAGMA query_only=ON')
        deadline = time.monotonic() + 2
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        yield db
    finally:
        db.close()


def verify_identity(family, path, token, cwd):
    if not isinstance(token, str) or not token or len(token) > 256:
        return False
    table, column = ('session', 'directory') if family == 'zcode' else ('sessions', 'cwd')
    try:
        with _database(path) as db:
            row = db.execute(f'SELECT {column} FROM {table} WHERE id=?', (token,)).fetchone()
        return bool(row and row[0] and os.path.normcase(os.path.realpath(row[0])) == os.path.normcase(os.path.realpath(cwd)))
    except (OSError, ValueError, sqlite3.Error, TypeError):
        return False


def read_metadata(family, path, token, *, cwd=None, source_path=None):
    """Read one exact native session's optional display fields, never content.

    Older stores may lack display columns. A missing/ambiguous identity or a
    different cwd produces no facts. The query-only connection and bounded
    column reads keep system prompts, model config and credentials out.
    """
    if family not in {'codex', 'zcode', 'hermes'}:
        raise ValueError('native_family_invalid')
    if not isinstance(token, str) or not token or len(token) > 256:
        return {}
    table, directory = ({'codex': ('threads', 'cwd'), 'zcode': ('session', 'directory'),
                         'hermes': ('sessions', 'cwd')})[family]
    fields = {
        'codex': {'title': ('title', 160), 'name': ('title', 160), 'model': ('model', 160),
                  'model_provider': ('provider', 96), 'cli_version': ('client_version', 96)},
        'zcode': {'title': ('title', 160), 'version': ('client_version', 96)},
        'hermes': {'title': ('title', 160), 'model': ('model', 160)},
    }[family]
    time_field = {'codex': 'created_at_ms', 'zcode': 'time_created', 'hermes': 'started_at'}[family]
    with _database(path) as db:
        columns = set()
        for index, row in enumerate(db.execute(f'PRAGMA table_info({table})')):
            if index >= 128:
                raise ValueError('native_metadata_schema')
            columns.add(row[1])
        if not {'id', directory} <= columns:
            return {}
        if family == 'codex' and (source_path is None or 'rollout_path' not in columns):
            return {}
        if family == 'codex' and time_field not in columns:
            time_field = 'created_at'
        selected = [column for column in fields if column in columns]
        # Preserve SQLite value types: numeric storage versions must never
        # become a string client_version merely through CAST.
        expressions = [f'substr({directory},1,32769)'] + [
            f'CASE WHEN typeof({column})=\'text\' THEN substr({column},1,{fields[column][1] + 1}) END'
            for column in selected]
        if time_field in columns:
            expressions.append(f'CASE WHEN typeof({time_field}) IN (\'integer\',\'real\') THEN {time_field} END')
        if family == 'codex':
            expressions.append('substr(rollout_path,1,32769)')
        rows = db.execute(f'SELECT {",".join(expressions)} FROM {table} WHERE id=? LIMIT 2',
                          (token,)).fetchall()
    if len(rows) != 1:
        return {}
    row = rows[0]
    if family == 'codex':
        if (not isinstance(row[-1], str) or len(row[-1]) > 32768
                or os.path.normcase(os.path.realpath(row[-1])) != os.path.normcase(os.path.realpath(source_path))):
            return {}
        row = row[:-1]
    if (cwd is not None and (not isinstance(row[0], str) or len(row[0]) > 32768
            or os.path.normcase(os.path.realpath(row[0])) != os.path.normcase(os.path.realpath(cwd)))):
        return {}
    result = {}
    for column, value in zip(selected, row[1:]):
        target, maximum = fields[column]
        if (isinstance(value, str) and value.strip()
                and not any(ord(char) < 32 or ord(char) == 127 for char in value)):
            result[target] = value.strip()[:maximum]
    if time_field in columns:
        value = row[-1]
        if type(value) in (int, float) and math.isfinite(value) and value >= 0:
            try:
                seconds = value / 1000 if time_field in {'time_created', 'created_at_ms'} else value
                result['native_started_at'] = datetime.fromtimestamp(seconds, timezone.utc).isoformat(
                    timespec='milliseconds').replace('+00:00', 'Z')
            except (ValueError, OverflowError, OSError):
                pass
    return result


def _json(text, default=None):
    try:
        return json.loads(text) if isinstance(text, str) else default
    except ValueError:
        return default


def _text(value):
    return value[:MAX_FIELD] if isinstance(value, str) else ''


def _event(kind, **payload):
    return {'kind': kind, 'payload': payload}


def _native_event_id(family, token, row_id, index, event):
    body = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    digest = hashlib.sha256(
        f'{family}\0{token}\0{row_id}\0{index}\0{body}'.encode()).hexdigest()[:40]
    return 'evt_' + digest


def _zcode(message, part):
    if not isinstance(message, dict) or not isinstance(part, dict):
        return []
    semantics = message.get('semantics') or {}
    if (message.get('synthetic') or part.get('synthetic') or message.get('visibility') == 'model-only'
            or semantics.get('transcriptVisibility') == 'hidden' or message.get('role') != 'assistant'):
        return []
    if part.get('type') == 'text' and part.get('text'):
        return [_event('assistant_message', text=_text(part['text']), is_complete=True)]
    if part.get('type') != 'tool':
        return []
    state = part.get('state')
    if not isinstance(state, dict) or state.get('status') not in {'completed', 'error'}:
        return []
    call = dict(tool_name=_text(part.get('tool')), call_id=_text(part.get('callID')))
    return [_event('tool_call', **call, arguments=json.dumps(state.get('input'), ensure_ascii=False)[:MAX_FIELD]),
            _event('tool_result', **call, result=_text(state.get('output') or state.get('error')),
                   status='error' if state['status'] == 'error' else 'ok')]


def _zcode_pending_tool(message, part):
    """Whether this exact visible tool part can still change in place."""
    if not isinstance(message, dict) or not isinstance(part, dict):
        return False
    semantics = message.get('semantics') or {}
    if (message.get('synthetic') or part.get('synthetic')
            or message.get('visibility') == 'model-only'
            or semantics.get('transcriptVisibility') == 'hidden'
            or message.get('role') != 'assistant' or part.get('type') != 'tool'):
        return False
    state = part.get('state')
    return isinstance(state, dict) and state.get('status') in {'pending', 'running'}


def _hermes(role, content, calls, call_id, tool_name):
    if role == 'tool':
        return [_event('tool_result', tool_name=_text(tool_name), call_id=_text(call_id), result=_text(content))]
    if role != 'assistant':
        return []
    result = [_event('assistant_message', text=_text(content), is_complete=True)] if content else []
    calls = _json(calls, [])
    for call in calls if isinstance(calls, list) else []:
        if not isinstance(call, dict) or not isinstance(call.get('function'), dict):
            continue
        function = call['function']
        result.append(_event('tool_call', call_id=_text(call.get('id')), tool_name=_text(function.get('name')),
                             arguments=_text(function.get('arguments'))))
    return result


def read_events(family, path, token, cursor, *, limit=MAX_ROWS):
    """Return ordered native evidence and an exclusive durable row cursor.

    Read one bounded snapshot; active ZCode tool parts stop the cursor at the
    first visible pending row because the client updates that row in place.
    A malformed/oversized snapshot raises, leaving the old cursor available.
    Callers may request smaller row pages; a page ends before exceeding
    ``MAX_BYTES`` so the caller can resume from the returned row cursor.
    """
    if type(cursor) is not int or cursor < 0:
        raise ValueError('native_cursor_invalid')
    if type(limit) is not int or not 1 <= limit <= MAX_ROWS:
        raise ValueError('native_limit_invalid')
    with _database(path) as db:
        if family == 'zcode':
            sql = ('SELECT p.rowid, substr(m.data,1,262145), substr(p.data,1,262145) FROM part p '
                   'JOIN message m ON m.id=p.message_id AND m.session_id=p.session_id '
                   'WHERE p.session_id=? AND p.rowid>? ORDER BY p.rowid LIMIT ?')
        elif family == 'hermes':
            sql = ('SELECT id,role,substr(content,1,262145),substr(tool_calls,1,262145),tool_call_id,tool_name '
                   'FROM messages WHERE session_id=? AND id>? ORDER BY id LIMIT ?')
        else:
            raise ValueError('native_family_invalid')
        rows = db.execute(sql, (token, cursor, limit + 1))
        result, total, latest = [], 0, cursor
        for count, row in enumerate(rows, start=1):
            if count > limit:
                if limit == MAX_ROWS:
                    raise ValueError('native_capture_overflow')
                break
            row_bytes = sum(len(value.encode('utf-8')) for value in row if isinstance(value, str))
            if any(isinstance(value, str) and len(value) > MAX_FIELD for value in row):
                raise ValueError('native_capture_overflow')
            if total + row_bytes > MAX_BYTES:
                if limit == MAX_ROWS or latest == cursor:
                    raise ValueError('native_capture_overflow')
                break
            total += row_bytes
            message, part = (_json(row[1]), _json(row[2])) if family == 'zcode' else (None, None)
            # ZCode updates a part row in place. Do not acknowledge the first
            # visible nonterminal tool or rows after it: its completion would
            # otherwise fall behind the durable rowid checkpoint forever.
            if family == 'zcode' and _zcode_pending_tool(message, part):
                break
            latest = row[0]
            shaped = (_zcode(message, part) if family == 'zcode'
                      else _hermes(*row[1:]))
            for index, event in enumerate(shaped):
                event['event_id'] = _native_event_id(family, token, latest, index, event)
                result.append(event)
        return result, latest
