"""Bounded Hermes display history; vendor storage is always read only.

Hermes copies message rows during compaction. A private SQLite identity ledger
keeps its native display-generation key and the first observed row ID, without
content. Delivery identity still uses that row ID and the Hub ingestion cursor;
equal text in different native messages is never a delivery receipt.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from datetime import datetime, timezone

from fleet_core.session.redact import redact_secrets_text
from tools.runner_source import atomic_private_json, MAX_RECORD_BYTES
from tools.session.native_projection import digest, tool_field as _tool_field
from tools.session.native_metadata import _safe_file_info
from tools.session.native_receipt import recover_append
from tools.session.native_storage import _database
from tools.session.runner_capture import RunnerCapture

PAGE_ROWS = 32
MAX_FIELD = 2 << 20
MAX_PAGE = 4 << 20
_TOKEN = re.compile(r'[A-Za-z0-9_.:-]{1,256}')


def _checkpoint(path):
    try:
        with path.open('rb') as stream:
            raw = stream.read(MAX_RECORD_BYTES + 1)
    except FileNotFoundError:
        return {'version': 1, 'cursor': -1, 'metadata': '', 'metadata_revision': 0,
                'receipts': [], 'pending': None}
    def event_id(value):
        return isinstance(value, str) and re.fullmatch(r'evt_[0-9a-f]{40}', value)
    try:
        state = json.loads(raw)
        if (len(raw) > MAX_RECORD_BYTES or not isinstance(state, dict)
                or set(state) != {'version', 'cursor', 'metadata', 'metadata_revision', 'receipts', 'pending'}
                or type(state['version']) is not int or state['version'] != 1
                or type(state['cursor']) is not int or not -1 <= state['cursor'] < (1 << 63)
                or type(state['metadata_revision']) is not int or not 0 <= state['metadata_revision'] < (1 << 63)
                or not isinstance(state['metadata'], str)
                or (state['metadata'] and not re.fullmatch('[0-9a-f]{64}', state['metadata']))
                or not isinstance(state['receipts'], list) or len(state['receipts']) > 2048
                or any(not event_id(value) for value in state['receipts'])
                or len(set(state['receipts'])) != len(state['receipts'])):
            raise ValueError
        pending = state['pending']
        if pending is not None and (not isinstance(pending, dict) or set(pending) != {'event_id', 'sequence'}
                or not event_id(pending['event_id']) or type(pending['sequence']) is not int
                or not 1 <= pending['sequence'] < (1 << 63)):
            raise ValueError
        return state
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError('hermes_checkpoint_invalid') from None


def session_paths(root):
    """Indexed keyset pages, never a full database/session-content scan."""
    database = Path(root) / 'state.db'
    if not database.is_file():
        return
    _safe_file_info(database)
    cursor = ''
    while True:
        with _database(database) as db:
            schema = db.execute('PRAGMA table_info(sessions)').fetchmany(129)
            if not any(r[1] == 'id' and r[5] == 1 for r in schema) or len(schema) > 128:
                raise ValueError('hermes_schema_unsupported')
            rows = db.execute('SELECT id FROM sessions WHERE id>? ORDER BY id LIMIT 32', (cursor,)).fetchall()
        if not rows:
            return
        for (token,) in rows:
            if not isinstance(token, str) or not _TOKEN.fullmatch(token) or token in {'.', '..'}:
                raise ValueError('hermes_identity_invalid')
            yield database / token  # private virtual identity, never opened as a file
        cursor = rows[-1][0]


def _stamp(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    except (ValueError, OverflowError, OSError):
        return None


def _event(kind, **payload):
    return {'kind': kind, 'payload': payload}


def _project(role, content, calls, call_id, tool_name):
    if content.startswith('\x00json:'):
        value = json.loads(content[6:])
        content = '\n'.join(x['text'] for x in value if isinstance(x, dict)
                            and x.get('type') == 'text' and isinstance(x.get('text'), str)) if isinstance(value, list) else ''
    content = redact_secrets_text(content)[0]
    events = []
    if role in {'user', 'assistant'}:
        for start in range(0, len(content), 4000):
            events.append(_event('user_message' if role == 'user' else 'assistant_message',
                text=content[start:start + 4000], content_mode='snapshot', is_complete=True))
    if role == 'tool':
        result, chunks = _tool_field(content)
        events.extend(chunks)
        events.append(_event('tool_result', call_id=call_id, tool_name=tool_name, result=result))
    if role == 'assistant' and calls:
        values = json.loads(calls)
        if not isinstance(values, list) or len(values) > 128:
            raise ValueError('hermes_tool_invalid')
        for call in values:
            if not isinstance(call, dict) or not isinstance(call.get('function'), dict):
                raise ValueError('hermes_tool_invalid')
            function = call['function']
            arguments = function.get('arguments') or ''
            if not isinstance(arguments, str):
                raise ValueError('hermes_tool_invalid')
            arguments, chunks = _tool_field(arguments)
            events.extend(chunks)
            events.append(_event('tool_call', call_id=call.get('id') or '',
                tool_name=function.get('name') or '', arguments=arguments))
    return events


def read_page(path, cursor):
    path = Path(path)
    if (not _TOKEN.fullmatch(path.name) or path.name in {'.', '..'} or path.parent.name != 'state.db'
            or type(cursor) is not int or not -1 <= cursor < (1 << 63)):
        raise ValueError('hermes_identity_invalid')
    database, token = path.parent, path.name
    _safe_file_info(database)
    with _database(database) as db:
        db.execute('BEGIN')
        schema = db.execute('PRAGMA table_info(messages)').fetchmany(129)
        columns = {r[1] for r in schema}
        if len(schema) > 128 or not any(r[1] == 'id' and r[2].upper() == 'INTEGER' and r[5] == 1 for r in schema):
            raise ValueError('hermes_schema_unsupported')
        required = {'session_id', 'role', 'content', 'tool_calls', 'tool_call_id', 'tool_name', 'timestamp'}
        if not required <= columns:
            raise ValueError('hermes_schema_unsupported')
        plan = db.execute('EXPLAIN QUERY PLAN SELECT id FROM messages WHERE session_id=? AND id>? ORDER BY id LIMIT 33', (token, cursor)).fetchall()
        if (not any('session_id=?' in r[3] and ('id>?' in r[3] or 'rowid>?' in r[3]) for r in plan)
                or any('TEMP B-TREE' in r[3] for r in plan)):
            raise ValueError('hermes_index_unsupported')
        session_columns = {r[1] for r in db.execute('PRAGMA table_info(sessions)').fetchmany(129)}
        meta_fields = [f'substr({key},1,161)' if key in session_columns else 'NULL'
                       for key in ('title', 'model')]
        meta_fields.append('started_at' if 'started_at' in session_columns else 'NULL')
        meta = db.execute(f'SELECT {",".join(meta_fields)} FROM sessions WHERE id=? LIMIT 2', (token,)).fetchall()
        if len(meta) != 1:
            raise ValueError('hermes_identity_invalid')
        last = db.execute('SELECT id FROM messages WHERE session_id=? ORDER BY id DESC LIMIT 1', (token,)).fetchone()
        if cursor >= 0 and (last is None or last[0] < cursor):
            raise ValueError('hermes_history_rewound')
        optional = [key if key in columns else default for key, default in
                    (('active', '1'), ('compacted', '0'), ('_compressed_summary', '0'), ('display_kind', 'NULL'))]
        # Cast TEXT to BLOB before substr: SQLite TEXT substr truncates at the
        # NUL in Hermes' documented structured-content prefix.
        selected = ['id', 'role'] + [f'substr(CAST({key} AS BLOB),1,{MAX_FIELD + 1})' for key in
                    ('content', 'tool_calls', 'tool_call_id', 'tool_name')] + ['timestamp', *optional]
        query = db.execute(f'SELECT {",".join(selected)} FROM messages WHERE session_id=? AND id>? ORDER BY id LIMIT 33', (token, cursor))
        rows, total, more = [], 0, False
        for row in query:
            size = sum(len(v) for v in row if isinstance(v, bytes))
            if len(rows) == PAGE_ROWS or (rows and total + size > MAX_PAGE):
                more = True
                break
            if size > MAX_PAGE or any(isinstance(v, bytes) and len(v) > MAX_FIELD for v in row):
                raise ValueError('hermes_row_oversized')
            rows.append(row)
            total += size
    title, model, started = meta[0]
    metadata = {k: v[:160] for k, v in (('title', title), ('model', model)) if isinstance(v, str) and v.strip()}
    if _stamp(started):
        metadata['native_started_at'] = _stamp(started)
    result = []
    for idx, role, content, calls, call_id, tool_name, stamp, active, compacted, summary, display in rows:
        content, calls, call_id, tool_name = [(v or b'').decode('utf-8') for v in (content, calls, call_id, tool_name)]
        if not _stamp(stamp):
            raise ValueError('hermes_timestamp_invalid')
        # This is the vendor's exact display-generation key, not a Hub event
        # dedup key. It distinguishes a second identical turn by native time,
        # while collapsing native compaction copies of that same record.
        key = hashlib.sha256(repr((role, content, stamp, call_id or None, calls or None,
                                   tool_name or None)).encode('utf-8')).hexdigest()
        visible = (active == 1 or compacted == 1) and summary != 1 and display is None
        events = _project(role, content, calls, call_id, tool_name) if visible and role in {'user', 'assistant', 'tool'} else []
        result.append((idx, key, stamp, events))
    return metadata, result, not more


@contextmanager
def ledger(path):
    """Atomic v0-empty -> v1 initialization; unknown/corrupt stores fail closed.

    Only hashes, first native row IDs and publication flags are persisted. The
    ledger is vendor-specific generation reconciliation, not a second queue.
    """
    path = Path(path)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(fd)
    _safe_file_info(path)
    if path.stat().st_size > 32 << 20:
        raise ValueError('hermes_ledger_limit')
    db = sqlite3.connect(path, timeout=.5)
    try:
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version == 0:
            if db.execute('SELECT name FROM sqlite_master LIMIT 1').fetchone():
                raise ValueError('hermes_ledger_version')
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE identities(digest TEXT PRIMARY KEY,first_id INTEGER NOT NULL,published INTEGER NOT NULL CHECK(published IN (0,1))) WITHOUT ROWID')
            db.execute('PRAGMA user_version=1')
            db.commit()
        elif version != 1:
            raise ValueError('hermes_ledger_version')
        schema = db.execute('PRAGMA table_info(identities)').fetchall()
        if [r[1] for r in schema] != ['digest', 'first_id', 'published']:
            raise ValueError('hermes_ledger_schema')
        yield db
    finally:
        db.close()


def recover_receipt(capture):
    checkpoint = capture.root / (capture.session_id + '.hermes-cursor')
    state = _checkpoint(checkpoint)
    return recover_append(capture, state, lambda value: atomic_private_json(checkpoint, value),
                          error='hermes_receipt_invalid')


def sync_hermes(cfg, path, send, *, should_stop=lambda: False):
    from tools.session.sync import native_identity
    identity = native_identity('hermes', path)
    # Verify source before creating a public record or node-private ledger.
    read_page(path, -1)
    if should_stop():
        return False
    capture = RunnerCapture(cfg, {'attempt_id': identity, 'agent_type': 'hermes', 'task_id': ''},
                            send, preserve_source=False)
    checkpoint = capture.root / (capture.session_id + '.hermes-cursor')
    try:
        state = recover_receipt(capture)
        ledger_path = capture.root / (capture.session_id + '.hermes-identities.sqlite')
        if state['cursor'] >= 0 and not ledger_path.is_file():
            raise ValueError('hermes_ledger_missing')
        metadata, rows, done = read_page(path, state['cursor'])
        def save():
            atomic_private_json(checkpoint, state)
        def emit(event):
            if event['event_id'] in state['receipts']:
                return
            state['pending'] = {'event_id': event['event_id'], 'sequence': capture.bridge.spool.status()['next_sequence']}
            save()
            if not capture.bridge.ingest_one(event, strict=True):
                raise RuntimeError('hermes_event_not_persisted')
            state['receipts'].append(event['event_id'])
            state['pending'] = None
            save()
        if metadata and digest(metadata) != state['metadata']:
            emit(dict(_event('session_metadata', **metadata), event_id='evt_' + digest(
                [identity, 'metadata', state['metadata_revision'] + 1, metadata])[:40]))
            state['metadata'], state['metadata_revision'] = digest(metadata), state['metadata_revision'] + 1
            state['receipts'] = []
            save()
        with ledger(ledger_path) as db:
            for idx, key, stamp, events in rows:
                if should_stop():
                    return False
                if not events:
                    # Invisible/rewound originals are not receipts for a later
                    # visible clone of this native compaction generation.
                    state['cursor'], state['receipts'] = idx, []
                    save()
                    continue
                row = db.execute('SELECT first_id,published FROM identities WHERE digest=?', (key,)).fetchone()
                if row is None:
                    db.execute('INSERT INTO identities VALUES (?,?,0)', (key, idx))
                    db.commit()
                    row = idx, 0
                first, published = row
                if type(first) is not int or not 0 <= first <= idx or published not in (0, 1):
                    raise ValueError('hermes_ledger_invalid')
                if not published:
                    for offset, event in enumerate(events):
                        event = dict(event, event_id='evt_' + digest([identity, first, offset, event])[:40], emitted_at=_stamp(stamp))
                        if event['kind'] in {'user_message', 'assistant_message'}:
                            event['payload']['message_id'] = 'hermes_' + digest([identity, first, offset])[:40]
                        emit(event)
                    db.execute('UPDATE identities SET published=1 WHERE digest=?', (key,))
                    db.commit()
                state['cursor'], state['receipts'] = idx, []
                save()
        capture.flush(should_stop=should_stop)
        info = capture.bridge.spool.status()
        if capture.bridge.uploader.last_error or info['ack_sequence'] < info['next_sequence'] - 1:
            raise RuntimeError('conversation_upload_pending')
        return done
    finally:
        capture.bridge.close()
