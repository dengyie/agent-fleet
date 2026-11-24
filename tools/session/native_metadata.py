"""Bounded read-only metadata snapshots, separate from native content cursors.

Only explicit vendor display fields are read. Retained sessions use their
existing private continuation binding; there is no CLI, newest-file guess,
command dispatch, conversation replay or vendor database write here.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
from types import SimpleNamespace

from tools.session.normalization import _native_metadata

HEAD_BYTES = 256 << 10
TAIL_BYTES = 2 << 20
MAX_ROWS = 4096
MAX_LINE_BYTES = 256 << 10
MAX_BINDING_BYTES = 1 << 20

_ROW_TYPES = {
    'codex': {'session_meta', 'turn_context', 'custom-title', 'event_msg', 'response_item',
              'item.started', 'item.updated', 'item.completed'},
    'claude_code': {'user', 'assistant', 'custom-title', 'system', 'stream_event'},
    'pi': {'session', 'session_info', 'model_change', 'message', 'message_end'},
    'zcode': {'model_io'},
}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _rows(data, *, tail=False, midline=False):
    # Split with a fixed maximum before decoding JSON, even for millions of
    # tiny/empty lines. The last fragment is never a complete source row.
    if tail:
        pieces = data.rsplit(b'\n', MAX_ROWS + 1)
        if midline or len(pieces) > MAX_ROWS + 1:
            pieces = pieces[1:]
        lines = pieces[:-1][-MAX_ROWS:]
    else:
        lines = data.split(b'\n', MAX_ROWS)
        lines = lines[:-1][:MAX_ROWS]
    result = []
    for line in lines:
        if not line or len(line.rstrip(b'\r')) >= MAX_LINE_BYTES:
            continue
        try:
            row = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(row, dict):
            result.append(row)
    return result


def _same_cwd(observed, expected):
    return (isinstance(observed, str) and observed
            and os.path.normcase(os.path.realpath(observed)) == os.path.normcase(os.path.realpath(expected)))


def _bound(family, path, prefix, rows, token, cwd):
    if token is None and cwd is None:
        return True  # archive identity is already the discovered source path
    if not isinstance(token, str) or not token or not isinstance(cwd, str) or not cwd:
        return False
    proof = False
    prefix_ids = {id(row) for row in prefix}
    for row in rows:
        kind = row.get('type')
        payload = row.get('payload') if isinstance(row.get('payload'), dict) else {}
        native_id = None
        if family == 'codex' and kind == 'session_meta':
            native_id = payload.get('id')
        elif family == 'claude_code':
            native_id = row.get('sessionId')
        elif family == 'pi' and kind == 'session':
            native_id = row.get('id')
        if native_id is None:
            continue
        expected = token
        if family == 'pi' and ('/' in token or '\\' in token):
            if (os.path.normcase(os.path.realpath(token)) != os.path.normcase(os.path.realpath(path))
                    or not isinstance(native_id, str) or not native_id
                    or not (path.stem == native_id or path.stem.endswith('_' + native_id))):
                return False
            expected = native_id
        if native_id != expected:
            return False
        observed_cwd = payload.get('cwd') if family == 'codex' else row.get('cwd')
        if id(row) in prefix_ids and _same_cwd(observed_cwd, cwd):
            proof = True
    return proof


def _project(family, rows):
    result = {}
    for row in rows:
        if row.get('type') in _ROW_TYPES[family]:
            result.update(_native_metadata(row))
    return result


def _safe_file_info(path):
    # Refuse special files and symlinks/reparse points before reading. The
    # descriptor checks below also reject a replaced final component.
    for component in (path, *path.parents):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('native_metadata_unsafe')
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('native_metadata_unsafe')
    return info


def metadata_store_path(family, path):
    """Known sibling store only; used by archive scan fingerprints too."""
    path = Path(path)
    if family == 'zcode':
        return path.parent.parent / 'db/db.sqlite'
    if family == 'codex':
        root = next((parent.parent for parent in path.parents
                     if parent.name in {'sessions', 'archived_sessions'}), None)
        return root / 'state_5.sqlite' if root is not None else None
    return None


def _codex_store_metadata(path, prefix, *, token=None, cwd=None):
    header = next((row.get('payload') for row in prefix
        if row.get('type') == 'session_meta' and isinstance(row.get('payload'), dict)), None)
    if not header or not isinstance(header.get('id'), str):
        return {}
    native_id, native_cwd = header['id'], header.get('cwd')
    if token is not None and token != native_id:
        return {}
    if cwd is not None and not _same_cwd(native_cwd, cwd):
        return {}
    if not isinstance(native_cwd, str) or not native_cwd:
        return {}
    # Only the observed v5 store is supported. Do not select a newest vendor
    # schema from state_*.sqlite or scan unrelated databases.
    database = metadata_store_path('codex', path)
    if database is None:
        return {}
    if not database.is_file():
        return {}
    _safe_file_info(database)
    from tools.session.native_storage import read_metadata
    return read_metadata('codex', database, native_id, cwd=native_cwd, source_path=path)


def read_jsonl_metadata(family, path, *, token=None, cwd=None):
    """Return a fingerprint and bounded prefix/current-tail metadata events.

    A prefix separated from EOF by an unread gap can only fill missing fields;
    facts explicitly present in the EOF tail are current source observations.
    No message bytes or source cursor are returned or persisted.
    """
    if family not in _ROW_TYPES:
        raise ValueError('native_family_invalid')
    path = Path(path).expanduser().absolute()
    original = _safe_file_info(path)
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or (before.st_dev, before.st_ino) != (original.st_dev, original.st_ino):
            raise ValueError('native_metadata_unsafe')
        head = stream.read(min(HEAD_BYTES, before.st_size))
        start = max(0, before.st_size - TAIL_BYTES)
        stream.seek(start)
        tail = stream.read(min(TAIL_BYTES, before.st_size))
        after = os.fstat(stream.fileno())
    stamp = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    if stamp(before) != stamp(after) or stamp(after) != stamp(path.stat()):
        raise ValueError('native_metadata_changed')
    prefix = _rows(head)
    latest = _rows(tail, tail=True, midline=start > 0)
    if not _bound(family, path, prefix, prefix + latest, token, cwd):
        raise ValueError('native_metadata_unbound')
    events = []
    baseline, current = _project(family, prefix), _project(family, latest)
    if baseline:
        missing = {key: value for key, value in baseline.items() if key not in current}
        if missing:
            events.append(dict(missing, fill_missing=True))
    if current:
        events.append(current)
    if family == 'codex':
        metadata = _codex_store_metadata(path, prefix, token=token, cwd=cwd)
        if metadata:
            events.append(metadata)
    if family == 'zcode' and path.stem.startswith('model-io-'):
        native_id = path.stem.removeprefix('model-io-')
        identities = [row.get('sessionId') for row in prefix + latest if row.get('type') == 'model_io']
        if identities and all(value == native_id for value in identities):
            from tools.session.native_storage import read_metadata
            database = metadata_store_path('zcode', path)
            if database.is_file():
                _safe_file_info(database)
                metadata = read_metadata('zcode', database, native_id)
                if metadata:
                    events.append(metadata)
    return _digest((family, str(path), stamp(after), events)), events


def _retained_jsonl_path(entry):
    if isinstance(entry.native_file_path, str) and entry.native_file_path:
        return Path(entry.native_file_path)
    token = entry.resume_token
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', token):
        return None
    family, env = entry.manifest.agent, entry.env
    home = Path(env.get('HOME') or env.get('USERPROFILE') or Path.home())
    if family == 'codex':
        root = Path(env.get('CODEX_HOME') or home / '.codex')
        roots = [root / directory for directory in ('sessions', 'archived_sessions')]
        names, maximum_depth = (token + '.jsonl', 'rollout-*-' + token + '.jsonl'), 3
    elif family == 'claude_code':
        root = Path(env.get('CLAUDE_CONFIG_DIR') or home / '.claude')
        roots, names, maximum_depth = [root / 'projects'], (token + '.jsonl',), 2
    elif family == 'pi':
        root = Path(env.get('PI_CODING_AGENT_DIR') or home / '.pi/agent') / 'sessions'
        for index, option in enumerate(entry.resume_options):
            if option == '--session-dir' and index + 1 < len(entry.resume_options):
                root = Path(entry.resume_options[index + 1])
            elif option.startswith('--session-dir='):
                root = Path(option.split('=', 1)[1])
        if not root.is_absolute():
            root = Path(entry.cwd) / root
        roots, names, maximum_depth = [root], (token + '.jsonl', '*_' + token + '.jsonl'), 2
    else:
        return None
    # Fixed native directory layouts and exact token patterns; count matches
    # jointly across active/archived stores so ambiguity never picks a file.
    paths = []
    for root in roots:
        for depth in range(maximum_depth + 1):
            for name in names:
                for path in root.glob('/'.join(['*'] * depth + [name])):
                    if path not in paths:
                        paths.append(path)
                        if len(paths) > 1:
                            return None
    return paths[0] if len(paths) == 1 else None


def read_retained_metadata(cfg, session_id, family):
    """Reuse the persisted continuation's exact id/machine/family/cwd binding."""
    if cfg.supervisor_manifest_dir is None or family not in {*_ROW_TYPES, 'hermes'}:
        return None
    from tools.supervisor.continuation import ContinuationStore
    store = ContinuationStore(cfg.supervisor_manifest_dir)
    path = store.path(session_id)
    if not path.exists():
        return None
    if _safe_file_info(path).st_size > MAX_BINDING_BYTES:
        raise ValueError('native_metadata_binding')
    entry = SimpleNamespace(manifest=SimpleNamespace(session_id=session_id,
        machine_id=cfg.machine, agent=family))
    if not store.restore(entry):
        return None
    if family in {'zcode', 'hermes'}:
        from tools.session.native_storage import database_path, read_metadata
        database = database_path(family, entry.env).expanduser().absolute()
        _safe_file_info(database)
        metadata = read_metadata(family, database, entry.resume_token, cwd=entry.cwd)
        return _digest((family, session_id, metadata)), [metadata] if metadata else []
    source = _retained_jsonl_path(entry)
    return read_jsonl_metadata(family, source, token=entry.resume_token, cwd=entry.cwd) if source else None
