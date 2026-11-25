"""Node-private AGY personal-account profiles and process-generation fences.

Only Google login material written by AGY inside a profile is authoritative;
this module never imports the operator's ambient Gemini/Google credentials.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shlex
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from fleet_core.locking import LockBusy, OwnedLock
from fleet_core.provider_accounts import account_id

_SCHEMA = 1
_STATES = frozenset({'signed_out', 'ready', 'disabled', 'starting', 'waiting', 'verifying', 'failed'})
_TRANSITIONS = {
    'signed_out': {'starting', 'disabled', 'failed'},
    'ready': {'starting', 'disabled', 'signed_out', 'failed'},
    'disabled': {'ready', 'signed_out', 'starting', 'failed'},
    'starting': {'waiting', 'verifying', 'ready', 'failed', 'signed_out', 'disabled'},
    'waiting': {'verifying', 'failed', 'signed_out', 'disabled'},
    'verifying': {'ready', 'failed', 'signed_out', 'disabled'},
    'failed': {'starting', 'signed_out', 'disabled'},
}
_SAFE_ENV = frozenset({
    'HOME', 'USERPROFILE', 'PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ', 'TERM',
    'TMP', 'TEMP', 'TMPDIR', 'SYSTEMROOT', 'WINDIR', 'ComSpec', 'PATHEXT',
    'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY', 'http_proxy',
    'https_proxy', 'all_proxy', 'no_proxy', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
    'NODE_EXTRA_CA_CERTS', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME',
})


def _fail(code):
    raise ValueError(code)


def _directory(path: Path) -> None:
    try:
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            _fail('provider_account_store')
        if os.name == 'nt':
            # Pin parents and reuse the existing NTFS owner/reparse checks.
            # Set an explicit inheritable private DACL so vendor-created login
            # files do not inherit a broad parent directory read permission.
            import ctypes as C
            from tools.platform.artifact_windows import _native, root_handle, HANDLE, U32
            native = _native()
            with root_handle(path, create_private=True):
                handle = native.kernel.CreateFileW(str(path), 0x40000 | 0x20000 | 0x80, 3, None, 3, 0x02200000, None)
                if handle in {None, C.c_void_p(-1).value}:
                    raise OSError('private_acl_failed')
                try:
                    native.info(handle)
                    native.validate_acl(handle)
                    with native.private_security(inherit=True) as security:
                        present, defaulted, acl = U32(), U32(), HANDLE()
                        get_acl = native.security.GetSecurityDescriptorDacl
                        get_acl.argtypes = [HANDLE, C.POINTER(U32), C.POINTER(HANDLE), C.POINTER(U32)]
                        get_acl.restype = U32
                        native.checked(get_acl(security, C.byref(present), C.byref(acl), C.byref(defaulted)))
                        set_acl = native.security.SetSecurityInfo
                        set_acl.argtypes = [HANDLE, U32, U32, HANDLE, HANDLE, HANDLE, HANDLE]
                        set_acl.restype = U32
                        result = set_acl(handle, 1, 4 | 0x80000000, None, None, acl, None)
                        if result:
                            raise OSError('private_acl_failed')
                finally:
                    native.close(handle)
            return
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink() or path.stat().st_uid != os.getuid():
            _fail('provider_account_store')
        os.chmod(path, 0o700)
        if path.stat().st_mode & 0o077:
            _fail('provider_account_store')
    except ValueError:
        raise
    except Exception:
        _fail('provider_account_store')


def _write(path: Path, value: dict) -> None:
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(raw) > 16384:
        _fail('provider_account_store')
    _directory(path.parent)
    tmp = path.with_name('.' + path.name + '.' + secrets.token_hex(8) + '.tmp')
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(6):
            try:
                os.replace(tmp, path)
                break
            except OSError as exc:
                if getattr(exc, 'winerror', None) not in {5, 32, 33} or attempt == 5:
                    raise
                time.sleep(.01 * (attempt + 1))
        if hasattr(os, 'O_DIRECTORY'):
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    except (OSError, ValueError, TypeError):
        _fail('provider_account_store')
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


class AgyRuntimeLease:
    def __init__(self, accounts: 'AgyAccounts', identifier: str, owner: str | None, generation: int, lock: OwnedLock):
        self._accounts, self.account_id, self.owner_digest = accounts, identifier, owner
        self.generation, self._lock = generation, lock

    def cancelled(self) -> bool:
        try:
            state = self._accounts.read(self.account_id)
            return (state['generation'] != self.generation or state['state'] != 'ready'
                    or self.owner_digest is not None and state['owner_digest'] != self.owner_digest)
        except (OSError, ValueError, KeyError):
            return True

    def close(self):
        self._lock.release()
        try:
            self._lock.path.unlink()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
        return False


class AgyAccounts:
    """Small node-local store; IDs become SHA-256 directory names, never paths."""
    def __init__(self, root):
        self.root = Path(root).expanduser().absolute()
        _directory(self.root)

    def _directory(self, identifier):
        account_id(identifier)
        path = self.root / hashlib.sha256(identifier.encode('ascii')).hexdigest()
        _directory(path)
        return path

    def profile(self, identifier):
        path = self._directory(identifier) / 'profile'
        _directory(path)
        return path

    def _state_path(self, identifier):
        return self._directory(identifier) / 'state.json'

    def _admission_lock(self, identifier):
        return OwnedLock(self._directory(identifier) / 'admission.lock', purpose='agy-account-admission')

    def read(self, identifier):
        path = self._state_path(identifier)
        try:
            if not path.exists():
                _fail('provider_account_unavailable')
            if path.is_symlink() or not path.is_file() or (os.name != 'nt' and path.stat().st_uid != os.getuid()):
                _fail('provider_account_store')
            if (os.name != 'nt' and path.stat().st_mode & 0o077) or path.stat().st_size > 8192:
                _fail('provider_account_store')
            value = json.loads(path.read_text(encoding='utf-8'))
            if (not isinstance(value, dict) or set(value) != {
                    'schema', 'account_id', 'owner_digest', 'revision', 'generation',
                    'state', 'logged_in', 'last_operation', 'last_revision'}
                    or value['schema'] != _SCHEMA or value['account_id'] != identifier
                    or type(value['revision']) is not int or value['revision'] < 1
                    or type(value['generation']) is not int or value['generation'] < 1
                    or value['state'] not in _STATES or type(value['logged_in']) is not bool
                    or not isinstance(value['owner_digest'], str) or len(value['owner_digest']) != 64
                    or any(c not in '0123456789abcdef' for c in value['owner_digest'])
                    or value['last_operation'] not in {'create', 'login', 'submit_login', 'cancel_login', 'enable', 'disable', 'logout'}
                    or type(value['last_revision']) is not int or value['last_revision'] != value['revision']):
                _fail('provider_account_store')
            return value
        except ValueError as exc:
            if str(exc) in {'provider_account_store', 'provider_account_unavailable'}:
                raise
            _fail('provider_account_store')
        except (OSError, TypeError, json.JSONDecodeError):
            _fail('provider_account_store')

    def environment(self, identifier, source=None):
        """Build a clean child env with a case-safe private GEMINI_HOME."""
        profile = self.profile(identifier)
        source = os.environ if source is None else source
        env = {key: str(source[key]) for key in _SAFE_ENV if key in source and isinstance(source[key], str)}
        # Keys are case-insensitive on Windows; remove ambient auth from any spelling.
        for key in list(env):
            if key.upper() in {'GEMINI_HOME', 'GEMINI_API_KEY', 'GOOGLE_API_KEY',
                               'GOOGLE_APPLICATION_CREDENTIALS', 'GOOGLE_CLOUD_PROJECT',
                               'GOOGLE_CLOUD_LOCATION', 'GOOGLE_CLOUD_QUOTA_PROJECT',
                               'GOOGLE_CLOUD_USE_VERTEXAI', 'BROWSER', 'SSH_CONNECTION',
                               'AGY_ACP_FORCE_FILE_STORAGE', 'AGY_CLI_DISABLE_AUTO_UPDATE'}:
                env.pop(key, None)
        env.update({
            'GEMINI_HOME': str(profile),
            'SSH_CONNECTION': '127.0.0.1 0 127.0.0.1 0',
            'AGY_ACP_FORCE_FILE_STORAGE': '1',
            'AGY_CLI_DISABLE_AUTO_UPDATE': 'true',
            'BROWSER': shlex.quote(sys.executable.replace('\\', '/')) + ' -c ' + shlex.quote('raise SystemExit(0)') + ' %s',
            'ELECTRON_RUN_AS_NODE': '1',
            'PYTHONUNBUFFERED': '1',
        })
        acp = profile / 'antigravity-acp'
        _directory(acp)
        settings = acp / 'settings.json'
        if not settings.exists():
            _write(settings, {'auth': {'type': 'oauth-personal'}})
        temp = profile / 'tmp'
        _directory(temp)
        if os.name == 'nt':
            env['TEMP'] = env['TMP'] = str(temp)
        else:
            env['TMPDIR'] = str(temp)
        env.setdefault('PATH', os.defpath)
        env.setdefault('HOME', str(Path.home()))
        if os.name == 'nt':
            env.setdefault('SYSTEMROOT', os.environ.get('SYSTEMROOT', r'C:\Windows'))
        return env

    def _default(self, identifier, owner):
        return {'schema': _SCHEMA, 'account_id': identifier, 'owner_digest': owner,
                'revision': 0, 'generation': 1, 'state': 'signed_out', 'logged_in': False,
                'last_operation': '', 'last_revision': 0}

    def apply(self, identifier, operation, revision, *, owner_digest):
        """Apply a signed, monotonic operation; duplicate same revision is safe."""
        account_id(identifier)
        if (operation not in {'create', 'login', 'submit_login', 'cancel_login', 'enable', 'disable', 'logout'}
                or type(revision) is not int or revision < 1 or revision > 2**53
                or not isinstance(owner_digest, str) or len(owner_digest) != 64
                or any(c not in '0123456789abcdef' for c in owner_digest)):
            _fail('provider_account_operation_invalid')
        lock = self._admission_lock(identifier)
        try:
            lock.acquire(timeout=10)
        except LockBusy:
            _fail('provider_account_busy')
        try:
            path = self._state_path(identifier)
            if path.exists():
                state = self.read(identifier)
                if state['owner_digest'] != owner_digest:
                    _fail('provider_account_owner_mismatch')
                if revision < state['revision']:
                    _fail('provider_account_stale_operation')
                if revision == state['revision']:
                    if operation == state['last_operation'] and revision == state['last_revision']:
                        return dict(state)
                    _fail('provider_account_operation_conflict')
            else:
                state = self._default(identifier, owner_digest)
                # A newer signed operation can supersede create before the
                # offline node ever receives it. Initialize empty metadata for
                # that owner; never infer or import any credentials/readiness.
                if (operation == 'create' and revision != 1
                        or operation in {'submit_login', 'cancel_login'}):
                    _fail('provider_account_unavailable')
            before = state['state']
            logged_in = state['logged_in']
            generation = state['generation']
            if operation == 'create':
                if state['revision']:
                    _fail('provider_account_operation_conflict')
                after, logged_in = 'signed_out', False
            elif operation == 'login':
                if before not in {'signed_out', 'failed', 'ready', 'disabled'}:
                    _fail('provider_account_state_conflict')
                after, logged_in = 'starting', False
                generation += 1
            elif operation == 'submit_login':
                if before != 'waiting':
                    _fail('provider_account_state_conflict')
                after = 'verifying'
            elif operation == 'cancel_login':
                if before not in {'starting', 'waiting', 'verifying'}:
                    _fail('provider_account_state_conflict')
                after, logged_in = 'signed_out', False
                generation += 1
            elif operation == 'enable':
                if before not in {'disabled', 'signed_out', 'failed'}:
                    _fail('provider_account_state_conflict')
                after = 'ready' if logged_in and before != 'failed' else 'signed_out'
                if before == 'failed': logged_in = False
                generation += 1
            elif operation == 'disable':
                after = 'disabled'
                generation += 1
            else:  # logout
                # A crash after the fence must never let enable resurrect the
                # unconfirmed logout's credentials. Native cleanup still runs
                # regardless of this flag and preserves the profile/history.
                logged_in = False
                if before == 'signed_out' and not logged_in:
                    after = 'signed_out'
                else:
                    after = 'signed_out'
                    generation += 1
            if after != before and after not in _TRANSITIONS[before]:
                _fail('provider_account_state_conflict')
            state.update(revision=revision, generation=generation, state=after,
                         logged_in=logged_in, last_operation=operation, last_revision=revision)
            _write(path, state)
            return dict(state)
        finally:
            lock.release()

    def set_state(self, identifier, revision, state, *, logged_in=None):
        """Accept a monotonic local/report transition for the active revision."""
        if state not in _STATES:
            _fail('provider_account_state_invalid')
        lock = self._admission_lock(identifier)
        try:
            lock.acquire(timeout=10)
        except LockBusy:
            _fail('provider_account_busy')
        try:
            current = self.read(identifier)
            if revision != current['revision']:
                _fail('provider_account_stale_operation')
            before = current['state']
            if state != before and state not in _TRANSITIONS[before]:
                _fail('provider_account_state_conflict')
            current['state'] = state
            if logged_in is not None:
                current['logged_in'] = bool(logged_in)
            elif state == 'ready':
                current['logged_in'] = True
            _write(self._state_path(identifier), current)
            return dict(current)
        finally:
            lock.release()

    @contextmanager
    def runtime(self, identifier, owner_digest=None):
        """Register a process lease atomically with admission; cancellation is generation-based."""
        account_id(identifier)
        directory = self._directory(identifier) / 'leases'
        _directory(directory)
        admission = self._admission_lock(identifier)
        try:
            admission.acquire(timeout=10)
        except LockBusy:
            _fail('provider_account_busy')
        lease_lock = None
        try:
            state = self.read(identifier)
            if owner_digest is not None and state['owner_digest'] != owner_digest:
                _fail('provider_account_owner_mismatch')
            if state['state'] != 'ready' or not state['logged_in']:
                _fail('provider_account_not_ready')
            name = f'{os.getpid()}-{secrets.token_hex(12)}.lease'
            lease_lock = OwnedLock(directory / name, purpose='agy-account-runtime')
            lease_lock.acquire(timeout=0)
            lease = AgyRuntimeLease(self, identifier, owner_digest, state['generation'], lease_lock)
        finally:
            admission.release()
        try:
            yield lease
        finally:
            lease.close()

    def quiesce(self, identifier, *, timeout=30.0):
        """Wait for pre-fence managed executions to release their account leases."""
        account_id(identifier)
        directory = self._directory(identifier) / 'leases'
        deadline = time.monotonic() + max(0.0, min(float(timeout), 60.0))
        while True:
            busy = False
            try:
                entries = list(directory.iterdir()) if directory.exists() else []
            except OSError:
                _fail('provider_account_store')
            for entry in entries:
                if entry.name.endswith('.lease.owner.json'):
                    continue
                if entry.is_symlink() or not entry.name.endswith('.lease'):
                    _fail('provider_account_store')
                lock = OwnedLock(entry, purpose='agy-account-quiesce')
                try:
                    lock.acquire(timeout=0)
                except LockBusy:
                    busy = True
                else:
                    lock.release()
                    try:
                        entry.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        _fail('provider_account_store')
            if not busy:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(.05)
