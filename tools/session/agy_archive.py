"""Publish read-only Agy history using existing RunnerCapture and atomic cursors."""
from __future__ import annotations

import json
from pathlib import Path

from tools.runner_source import MAX_RECORD_BYTES, atomic_private_json
from tools.session.agy_storage import TERMINAL, digest, read_page
from tools.session.runner_capture import RunnerCapture


def _checkpoint(path):
    try:
        with path.open('rb') as stream:
            raw = stream.read(MAX_RECORD_BYTES + 1)
    except FileNotFoundError:
        return {'version': 1, 'cursor': -1, 'metadata': '', 'model': '', 'active': '',
                'receipts': [], 'pending': None, 'revision': 0, 'batch': None,
                'metadata_revision': 0}
    try:
        row = json.loads(raw)
        if (len(raw) > MAX_RECORD_BYTES or not isinstance(row, dict)
                or type(row.get('version')) is not int or row['version'] != 1
                or type(row.get('cursor')) is not int
                or not -1 <= row['cursor'] < (1 << 63)
                or any(not isinstance(row.get(k), str) or (row[k] and (
                    len(row[k]) != 64 or any(c not in '0123456789abcdef' for c in row[k])))
                    for k in ('metadata', 'model', 'active'))):
            raise ValueError
        receipts, pending = row.get('receipts'), row.get('pending')
        if (not isinstance(receipts, list) or len(receipts) > 2048
                or any(not isinstance(x, str) or len(x) != 44 or not x.startswith('evt_')
                       or any(c not in '0123456789abcdef' for c in x[4:]) for x in receipts)
                or len(set(receipts)) != len(receipts)):
            raise ValueError
        if pending is not None and (not isinstance(pending, dict)
                or set(pending) != {'event_id', 'sequence'}
                or not isinstance(pending['event_id'], str) or len(pending['event_id']) != 44
                or not pending['event_id'].startswith('evt_')
                or any(c not in '0123456789abcdef' for c in pending['event_id'][4:])
                or type(pending['sequence']) is not int or not 1 <= pending['sequence'] < (1 << 63)):
            raise ValueError
        if any(type(row.get(k)) is not int or not 0 <= row[k] < (1 << 63)
               for k in ('revision', 'metadata_revision')):
            raise ValueError
        batch = row.get('batch')
        if batch is not None and (not isinstance(batch, str) or len(batch) != 64
                or any(c not in '0123456789abcdef' for c in batch)):
            raise ValueError
        return row
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError('agy_checkpoint_invalid') from None


def recover_receipt(capture):
    """Resolve the one uncertain append BEFORE replay may ACK/compact its frame.

    Called under the existing spool writer lock by both archive and replay.
    A quota gap occupies a sequence but is never a receipt for the source event.
    """
    checkpoint = capture.root / (capture.session_id + '.agy-cursor')
    state = _checkpoint(checkpoint)
    from tools.session.native_receipt import recover_append
    return recover_append(capture, state, lambda value: atomic_private_json(checkpoint, value),
                          error='agy_receipt_invalid')


def sync_agy(cfg, path, send, *, should_stop=lambda: False):
    """One finite SQLite page. Return true only at acknowledged terminal EOF."""
    from tools.session.sync import native_identity
    identity = native_identity('agy', path)
    # Verify source before constructing a new public session/recovery record.
    root = Path(cfg.cache_dir) / 'conversations'
    from tools.session.runner_capture import session_prefix
    checkpoint = root / (session_prefix(cfg.machine) + identity + '.agy-cursor')
    state = _checkpoint(checkpoint)
    metadata, rows, complete = read_page(path, state['cursor'])
    if should_stop():
        return False
    # The input is already an allowlisted projection. Explicit large visible
    # fields have stable source chunks; automatic raw wrapping would create
    # random source IDs on a recovered step and duplicate its source archive.
    capture = RunnerCapture(cfg, {'attempt_id': identity, 'agent_type': 'agy', 'task_id': ''},
                            send, preserve_source=False)
    try:
        # A second scanner may have waited for this writer lock after reading
        # the old source cursor. Reload under ownership before writing events.
        current = recover_receipt(capture)
        if current != state:
            state = current
            metadata, rows, complete = read_page(path, state['cursor'])
        spool = capture.bridge.spool
        def emit(event):
            if event['event_id'] in state['receipts']:
                return
            state['pending'] = {'event_id': event['event_id'],
                                'sequence': spool.status()['next_sequence']}
            atomic_private_json(checkpoint, state)
            if not capture.bridge.ingest_one(event, strict=True):
                raise RuntimeError('agy_event_not_persisted')
            state['receipts'].append(event['event_id'])
            state['pending'] = None
            atomic_private_json(checkpoint, state)
        if metadata and digest(metadata) != state['metadata']:
            # A -> B -> A renames are distinct observations. The revision is
            # committed with the digest, so an interrupted delivery reuses it.
            metadata_id = 'evt_' + digest([identity, 'metadata',
                state['metadata_revision'] + 1, metadata])[:40]
            emit({'kind': 'session_metadata', 'payload': metadata,
                  'event_id': metadata_id})
            state['metadata'] = digest(metadata)
            state['metadata_revision'] += 1
            state['receipts'].remove(metadata_id)
            atomic_private_json(checkpoint, state)
        for index, status, events in rows:
            if should_stop():
                return False
            active = digest([index, events])
            if status in TERMINAL or active != state['active']:
                if state['batch'] != active:
                    state['batch'] = active
                    state['revision'] += 1
                    state['receipts'] = []
                    atomic_private_json(checkpoint, state)
                for event in events:
                    if event['kind'] == 'session_metadata':
                        model = digest(event['payload'])
                        if model == state['model']:
                            continue
                    # A changed in-place snapshot may later return to equal
                    # text. Keep message identity, but give each observed
                    # revision its own delivery identity and Hub cursor.
                    event = dict(event, event_id='evt_' + digest(
                        [event['event_id'], state['revision']])[:40])
                    emit(event)
                    if event['kind'] == 'session_metadata':
                        state['model'] = model
                if status in TERMINAL:
                    state['cursor'], state['active'] = index, ''
                else:
                    state['active'] = active
                state['receipts'] = []
                state['batch'] = None
                atomic_private_json(checkpoint, state)
        capture.flush(should_stop=should_stop)
        status = capture.bridge.spool.status()
        if capture.bridge.uploader.last_error or status['ack_sequence'] < status['next_sequence'] - 1:
            raise RuntimeError('conversation_upload_pending')
        return complete
    finally:
        capture.bridge.close()
