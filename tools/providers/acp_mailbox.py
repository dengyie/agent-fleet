"""Node-private, cross-process ACP request mailbox. No public paths or raw RPC."""
from __future__ import annotations

import json
import math
import os
import secrets
import stat
import time
from pathlib import Path

from fleet_core.locking import OwnedLock, LockBusy
from fleet_core.process.process_group import process_start_identity

_MAX = 8192


def _atomic(path, value):
    raw = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(raw) > _MAX: raise ValueError('mailbox_record_too_large')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name('.' + path.name + '.' + secrets.token_hex(6) + '.tmp')
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        try: tmp.unlink()
        except OSError: pass


class AcpMailbox:
    @staticmethod
    def session_root(manifest_dir, session_id): return (Path(manifest_dir) / 'acp' / session_id).resolve()

    @classmethod
    def begin(cls, root, *, session_id, pid=None):
        root = Path(root).resolve(); root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not isinstance(session_id, str) or not session_id or len(session_id) > 128:
            raise ValueError('invalid_session_identity')
        owner_pid = os.getpid() if pid is None else pid
        if type(owner_pid) is not int or owner_pid <= 0:
            raise ValueError('invalid_owner_identity')
        lock = OwnedLock(root / '.owner.lock', purpose='acp-owner')
        try:
            lock.acquire(timeout=1)
        except LockBusy as exc:
            raise ValueError('mailbox_in_use') from exc
        try:
            previous = cls._read(root / 'owner.json')
            if previous and previous.get('state') == 'active':
                old_pid, old_start = previous.get('pid'), previous.get('start_identity')
                if (type(old_pid) is int and old_pid > 0 and isinstance(old_start, str)
                        and process_start_identity(old_pid) == old_start):
                    raise ValueError('mailbox_in_use')
            _atomic(root / 'owner.json', {'session_id': session_id, 'incarnation': secrets.token_hex(16),
                'pid': owner_pid, 'start_identity': process_start_identity(owner_pid),
                'started': time.time(), 'state': 'active'})
        finally:
            lock.release()
        return root

    @classmethod
    def live_owner(cls, root):
        owner = cls._read(Path(root) / 'owner.json')
        if (not owner or owner.get('state') != 'active'
                or type(owner.get('pid')) is not int or owner['pid'] <= 0
                or not isinstance(owner.get('start_identity'), str) or not owner['start_identity']
                or not isinstance(owner.get('incarnation'), str) or len(owner['incarnation']) != 32): return None
        return owner if process_start_identity(owner.get('pid')) == owner['start_identity'] else None

    @classmethod
    def cancel(cls, root):
        owner = cls.live_owner(root)
        if not owner: return False
        _atomic(Path(root) / 'cancel.json', {'incarnation': owner['incarnation']})
        return True

    @classmethod
    def cancelled(cls, root):
        owner = cls._read(Path(root) / 'owner.json')
        cancel = cls._read(Path(root) / 'cancel.json')
        return bool(owner and cancel and owner.get('incarnation') == cancel.get('incarnation'))

    @classmethod
    def publish(cls, root, request_id, options, *, ttl=300):
        lock = OwnedLock(Path(root) / '.answer.lock', purpose='acp-answer')
        try: lock.acquire(timeout=.1)
        except LockBusy: return False
        try: return cls._publish(root, request_id, options, ttl=ttl)
        finally: lock.release()

    @classmethod
    def _publish(cls, root, request_id, options, *, ttl=300):
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128: return False
        owner = cls._read(Path(root) / 'owner.json')
        if not owner or owner.get('state') != 'active': return False
        if not isinstance(options, list) or not 1 <= len(options) <= 16: return False
        clean = [{'id': x['id'], 'label': x['label']} for x in options
                 if isinstance(x, dict) and isinstance(x.get('id'), str) and 0 < len(x['id']) <= 128
                 and isinstance(x.get('label'), str) and 0 < len(x['label']) <= 200]
        if not clean: return False
        pending_path = Path(root) / 'pending.json'
        previous = cls._read(pending_path)
        if (previous and previous.get('incarnation') == owner['incarnation']
                and (previous.get('state') in {'dispatching', 'execution_unknown'}
                     or previous.get('state') == 'pending' and previous['expires'] > time.time())): return False
        answer_path = Path(root) / 'answer.json'
        try: answer_path.unlink()
        except OSError: pass
        _atomic(pending_path, {'incarnation': owner['incarnation'], 'request_id': request_id,
            'options': clean, 'expires': time.time() + min(300, max(1, ttl)), 'state': 'pending'})
        return True

    @classmethod
    def wait(cls, root, request_id, *, timeout=.1):
        deadline = time.monotonic() + max(0, timeout); root = Path(root)
        while time.monotonic() < deadline:
            command, pending, owner = (cls._read(root / name) for name in ('answer.json', 'pending.json', 'owner.json'))
            if not owner or not pending or owner.get('state') != 'active' or pending.get('expires', 0) <= time.time(): return None
            if (command and pending.get('state') == 'dispatching' and pending.get('request_id') == request_id
                    and command.get('incarnation') == owner.get('incarnation') == pending.get('incarnation')
                    and command.get('request_id') == request_id):
                return command.get('selected')
            time.sleep(min(.02, max(0, deadline-time.monotonic())))
        return None

    @classmethod
    def ack(cls, root, request_id):
        root = Path(root)
        lock = OwnedLock(root / '.answer.lock', purpose='acp-answer')
        try: lock.acquire(timeout=.1)
        except LockBusy: return False
        try:
            pending, owner = cls._read(root / 'pending.json'), cls.live_owner(root)
            if (pending and owner and pending.get('state') == 'dispatching'
                    and pending.get('request_id') == request_id
                    and pending.get('incarnation') == owner.get('incarnation')):
                _atomic(root / 'pending.json', {**pending, 'state': 'delivered'}); return True
            return False
        finally: lock.release()

    @classmethod
    def answer(cls, root, request_id, selected, *, timeout=2.0):
        root = Path(root).resolve()
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128: return 'invalid_payload'
        if isinstance(selected, list) and len(selected) == 1: selected = selected[0]
        if not isinstance(selected, str) or not selected or len(selected) > 128: return 'invalid_payload'
        lock = OwnedLock(root / '.answer.lock', purpose='acp-answer')
        try: lock.acquire(timeout=0)
        except LockBusy: return 'interaction_unavailable'
        try:
            owner, pending = cls.live_owner(root), cls._read(root / 'pending.json')
            if not owner or owner.get('state') != 'active' or not pending: return 'interaction_expired'
            if pending.get('expires', 0) <= time.time() or pending.get('request_id') != request_id: return 'interaction_expired'
            if pending.get('incarnation') != owner.get('incarnation'): return 'execution_unknown'
            if pending.get('state') in {'dispatching', 'execution_unknown'}: return 'execution_unknown'
            if pending.get('state') != 'pending': return 'interaction_expired'
            if not any(isinstance(x, dict) and x.get('id') == selected for x in pending.get('options', [])): return 'invalid_payload'
            _atomic(root / 'pending.json', {**pending, 'state': 'dispatching'})
            _atomic(root / 'answer.json', {'incarnation': owner['incarnation'], 'request_id': request_id, 'selected': selected, 'written': time.time()})
        finally: lock.release()
        deadline = time.monotonic() + max(.05, timeout)
        while time.monotonic() < deadline:
            status = cls._read(root / 'pending.json')
            if (status and status.get('request_id') == request_id
                    and status.get('incarnation') == owner['incarnation']):
                if status.get('state') == 'delivered': return 'answered'
                if status.get('state') == 'execution_unknown': return 'execution_unknown'
            time.sleep(.025)
        # Serialize expiry against the transport's post-flush ack. A late ack
        # after this outcome remains uncertain and must never be replayed.
        lock = OwnedLock(root / '.answer.lock', purpose='acp-answer')
        try: lock.acquire(timeout=.1)
        except LockBusy: return 'execution_unknown'
        try:
            status = cls._read(root / 'pending.json')
            if (status and status.get('request_id') == request_id
                    and status.get('incarnation') == owner['incarnation']
                    and status.get('state') == 'delivered'):
                return 'answered'
            if (status and status.get('request_id') == request_id
                    and status.get('incarnation') == owner['incarnation']
                    and status.get('state') == 'dispatching'):
                _atomic(root / 'pending.json', {**status, 'state': 'execution_unknown'})
            return 'execution_unknown'
        finally: lock.release()

    @staticmethod
    def _read(path):
        try:
            path = Path(path)
            entry = path.lstat()
            if (not stat.S_ISREG(entry.st_mode)
                    or getattr(entry, 'st_file_attributes', 0) & 0x400): return None
            flags = (os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
                     | getattr(os, 'O_NONBLOCK', 0))
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (entry.st_dev, entry.st_ino): return None
                raw = stream.read(_MAX + 1)
            if len(raw) > _MAX: return None
            value = json.loads(raw.decode('utf-8'))
            if isinstance(value, dict) and path.name == 'pending.json':
                expires = value.get('expires')
                if type(expires) not in (int, float) or not math.isfinite(expires): return None
                if not isinstance(value.get('options'), list) or len(value['options']) > 16: return None
            return value if isinstance(value, dict) else None
        except (OSError, ValueError, UnicodeDecodeError): return None
