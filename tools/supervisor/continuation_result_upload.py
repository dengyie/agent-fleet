"""Durable bounded result upload; retries only completed data, never execution."""
from __future__ import annotations

import itertools
import json
import logging
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from fleet_core.continuation_result import MAX_RESULT_BYTES, validate_result


class ContinuationResultUpload:
    def __init__(self, cfg, post):
        self.cfg, self.post = cfg, post
        self.root = Path(cfg.cache_dir) / 'continuation-results'
        self._pending_scan = None
        self._draft_scan = None

    @staticmethod
    @contextmanager
    def _directory(path: Path):
        if os.name == 'nt':
            from tools.platform.artifact_windows import root_handle

            # The configured cache container can inherit a broad ACL. The
            # Fleet-owned child is created with a protected, inheritable ACL.
            with root_handle(path.absolute(), create_private=True):
                yield
            return
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
            raise ValueError('unsafe_result_store')
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('unsafe_result_store')
        yield

    def _atomic(self, path: Path, value: dict):
        self.root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._directory(self.root), self._directory(path.parent):
            descriptor, name = tempfile.mkstemp(prefix='.result-', dir=path.parent)
            try:
                with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                    json.dump(value, stream, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, path)
            finally:
                # Only this call's own known temporary file can be cleaned up.
                if os.path.exists(name):
                    os.unlink(name)

    def save(self, value):
        value = validate_result(value)
        if self.retained(value['command_id']):
            return
        self._atomic(self.root / (value['command_id'] + '.json'), {'result': value, 'acknowledged': False})

    def retained(self, command_id):
        return any((directory / (command_id + '.json')).exists() for directory in
                   (self.root, self.root / 'acknowledged', self.root / 'rejected'))

    def save_draft(self, value):
        self._atomic(self.root / 'drafts' / (value['command_id'] + '.json'), value)
        # This direct session fence must be durable before append is allowed.
        # A crash between the two writes leaves only a prepared, unspawned
        # draft; a dispatched draft always retains the direct lookup.
        self._atomic(self.root / 'draft-sessions' / (value['session_id'] + '.json'),
                     {'command_id': value['command_id'], 'session_id': value['session_id']})

    def finish_draft(self, command_id):
        source = self.root / 'drafts' / (command_id + '.json')
        if source.exists():
            target = self.root / 'drafts-finished'
            self._atomic(source, {'command_id': command_id, 'finished': True})
            self._move(source, target)

    def drafts(self, limit=32, *, session_id=None):
        if session_id is not None:
            if not isinstance(session_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', session_id):
                raise ValueError('invalid_result_draft')
            fence = self.root / 'draft-sessions' / (session_id + '.json')
            if not fence.exists():
                return []
            binding = self._read(fence)
            cid = binding.get('command_id') if isinstance(binding, dict) else None
            if (not isinstance(cid, str) or not re.fullmatch(r'cmd_[A-Za-z0-9._-]{1,124}', cid)
                    or binding.get('session_id') != session_id):
                raise ValueError('invalid_result_draft')
            if self.retained(cid) or (self.root / 'drafts-finished' / (cid + '.json')).exists():
                return []
            value = self._read(self.root / 'drafts' / (cid + '.json'))
            if not isinstance(value, dict) or value.get('command_id') != cid:
                raise ValueError('invalid_result_draft')
            if value.get('finished') is True:
                self.finish_draft(cid)
                return []
            if value.get('session_id') != session_id:
                raise ValueError('invalid_result_draft')
            return [value]
        directory = self.root / 'drafts'
        if not directory.exists():
            return []
        with self._directory(self.root), self._directory(directory):
            if self._draft_scan is None:
                self._draft_scan = os.scandir(directory)
            batch = list(itertools.islice(self._draft_scan, limit))
        if len(batch) < limit:
            self._draft_scan.close()
            self._draft_scan = None
        paths = [directory / row.name for row in batch
            if row.name.startswith('cmd_') and row.name.endswith('.json')]
        found = []
        for path in paths:
            try:
                value = self._read(path)
                if not isinstance(value, dict) or value.get('command_id') != path.stem:
                    raise ValueError('invalid_result_draft')
                if value.get('finished') is True:
                    self.finish_draft(path.stem)
                    continue
                found.append(value)
            except (OSError, ValueError, TypeError, RuntimeError):
                self.reject_draft(path.stem)
        return found

    def reject_draft(self, command_id):
        source = self.root / 'drafts' / (command_id + '.json')
        target = self.root / 'invalid-drafts'
        self._move(source, target)

    def _archive(self, path, *, acknowledged, rejected=False):
        target = self.root / ('acknowledged' if acknowledged else 'rejected' if rejected else 'invalid')
        self._move(path, target)

    def _move(self, source, target):
        with self._directory(self.root), self._directory(source.parent), self._directory(target):
            os.replace(source, target / source.name)

    def _read(self, path):
        if os.name == 'nt':
            from tools.platform.artifact_windows import source_handle
            with self._directory(self.root), source_handle(path.parent.absolute(), path.name) as (stream, _identity):
                raw = stream.read(MAX_RESULT_BYTES + 1025)
        else:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise ValueError('unsafe_result_store')
                raw = stream.read(MAX_RESULT_BYTES + 1025)
        if len(raw) > MAX_RESULT_BYTES + 1024:
            raise ValueError('invalid_result_store')
        return json.loads(raw)

    def replay(self, limit=32):
        """One bounded pass. Acknowledged files never starve pending uploads."""
        if not self.root.exists():
            return
        try:
            # Scandir is consumed lazily; no list of an unbounded spool exists.
            with self._directory(self.root):
                if self._pending_scan is None:
                    self._pending_scan = os.scandir(self.root)
                # Keep the iterator so failures cannot hide later results.
                batch = list(itertools.islice(self._pending_scan, limit))
            if len(batch) < limit:
                self._pending_scan.close()
                self._pending_scan = None
            candidates = [self.root / row.name for row in batch
                if row.name.startswith('cmd_') and row.name.endswith('.json')]
            for path in candidates:
                try:
                    data = self._read(path)
                    if not isinstance(data, dict):
                        raise ValueError('invalid_result_store')
                    if data.get('acknowledged') is True and data.get('command_id') == path.stem:
                        self._archive(path, acknowledged=True)
                        continue
                    value = validate_result(data.get('result'))
                    if path.name != value['command_id'] + '.json':
                        raise ValueError('invalid_result_store')
                except (OSError, ValueError, TypeError, RuntimeError):
                    # Preserve damaged evidence outside the pending queue. It
                    # must not permanently block all later completed turns.
                    self._archive(path, acknowledged=False)
                    continue
                try:
                    code, body = self.post(self.cfg, '/api/runner-continuation-results', value)
                    if code == 200 and isinstance(body, dict) and body.get('command_id') == value['command_id']:
                        self._atomic(path, {'command_id': value['command_id'], 'acknowledged': True})
                        self._archive(path, acknowledged=True)
                    elif code in {400, 403, 404, 409, 413, 422}:
                        # Preserve a definitive rejection for diagnosis instead
                        # of retrying it ahead of every later valid result.
                        self._archive(path, acknowledged=False, rejected=True)
                except Exception:
                    logging.getLogger(__name__).warning('Continuation result upload pending')
        except (OSError, ValueError, RuntimeError):
            if self._pending_scan is not None:
                self._pending_scan.close()
                self._pending_scan = None
            logging.getLogger(__name__).warning('Continuation result store unavailable')
