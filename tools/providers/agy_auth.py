"""Google personal AGY setup over the signed supervisor channel.

Short-lived Google responses exist only in memory. Private files contain bounded
status, process ownership and retryable reports, never authorization responses.
"""
from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
from collections.abc import Mapping
from pathlib import Path

from fleet_core.locking import LockBusy, OwnedLock
from fleet_core.process.local_process import popen as popen_local
from fleet_core.process.process_group import (
    posix_group_alive, posix_group_only_zombies, process_start_identity, signal_group,
)
from fleet_core.provider_accounts import account_id, authorization_url, control_payload, login_response
from tools.providers.acp_transport import AcpTransport, AcpTransportError
from tools.providers.agy_accounts import AgyAccounts, _write

_MAX_LINE = 8192
_MAX_OUTPUT = 1024 * 1024
_FLOW_TTL = 300.0
_ERROR_CODES = frozenset({
    'provider_account_store', 'provider_account_unavailable', 'provider_account_busy',
    'provider_account_owner_mismatch', 'provider_account_stale_operation',
    'provider_account_operation_conflict', 'provider_account_state_conflict',
    'provider_account_not_ready', 'login_expired', 'login_failed', 'login_interrupted',
    'provider_cleanup_failed', 'provider_authentication_failed', 'invalid_authorization_url',
    'invalid_login_response', 'provider_account_logout_unsupported',
})


class _Child:
    """One owned process tree; Windows starts suspended inside a kill-on-close Job."""
    def __init__(self, command, *, cwd, env):
        self.job = None
        options = {'start_new_session': True}
        if os.name == 'nt':
            from fleet_core.process.windows_job import WindowsJob
            self.job = WindowsJob()
            options = {'creationflags': 0x00000004 | 0x00000200}
        try:
            self.proc = popen_local(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, close_fds=True, **options)
            if self.job is not None:
                self.job.start(self.proc._handle)
        except BaseException:
            if hasattr(self, 'proc'):
                self.proc.kill()
                self.proc.wait(timeout=2)
            if self.job is not None:
                self.job.close()
            raise

    def close(self):
        proc = self.proc
        try:
            if self.job is not None:
                self.job.close()
            else:
                signal_group(proc.pid, getattr(signal, 'SIGTERM', 15))
                until = time.monotonic() + .3
                while proc.poll() is None and time.monotonic() < until:
                    time.sleep(.02)
                signal_group(proc.pid, getattr(signal, 'SIGKILL', 9))
            proc.wait(timeout=2)
            if self.job is None:
                until = time.monotonic() + 2
                while posix_group_alive(proc.pid) and not posix_group_only_zombies(proc.pid):
                    if time.monotonic() >= until:
                        raise ValueError('provider_cleanup_failed')
                    time.sleep(.02)
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError('provider_cleanup_failed') from None
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try: stream.close()
                except (OSError, ValueError): pass


class _Flow:
    def __init__(self, identifier, command_id, revision, owner_digest, expires):
        self.account_id, self.command_id = identifier, command_id
        self.report_command_id, self.revision = command_id, revision
        self.owner_digest, self.expires = owner_digest, expires
        self.cancelled, self.answer, self.done = threading.Event(), threading.Event(), threading.Event()
        self.response, self.url, self.child, self.thread = None, None, None, None
        self.generation = None
        self.auth_lock = None


class AgyAccountControl:
    """ControlClient invokes execute after signature and durable nonce validation."""
    def __init__(self, cfg, supervisor=None):
        root = getattr(cfg, 'supervisor_manifest_dir', None)
        if root is None:
            raise ValueError('provider_account_unavailable')
        self.cfg, self.supervisor = cfg, supervisor
        self.accounts = AgyAccounts(Path(root) / 'provider-accounts')
        self._lock = threading.RLock()
        self._flows: dict[str, _Flow] = {}
        self._scan = None
        self._scan_lock = threading.Lock()
        self.login_timeout = _FLOW_TTL

    @staticmethod
    def _post(transport, path, body, headers):
        try: return transport(path, body, headers)
        except Exception: return 0, {}

    def _operation_path(self, identifier):
        return self.accounts._directory(identifier) / 'operation.json'

    def _report_path(self, identifier):
        return self.accounts._directory(identifier) / 'report.json'

    @staticmethod
    def _read(path):
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
                return None
            raw = path.read_bytes()
            if len(raw) > 16384: return None
            value = json.loads(raw.decode('utf-8'))
            return value if isinstance(value, dict) else None
        except (OSError, UnicodeError, ValueError, RecursionError): return None

    def _save_operation(self, identifier, command_id, payload, **fields):
        _write(self._operation_path(identifier), {'command_id': command_id, **payload, **fields})

    def _report(self, command_id, identifier, revision, state, transport, headers, **fields):
        body = {'command_id': command_id, 'account_id': identifier,
                'revision': revision, 'state': state, **fields}
        with self._lock:
            current = self.accounts.read(identifier)
            if current['revision'] != revision: return
            _write(self._report_path(identifier), body)
        self._send_report(identifier, body, transport, headers)

    def _send_report(self, identifier, body, transport, headers):
        status, result = self._post(transport, '/api/supervisor/provider-accounts/report', body, headers)
        if status == 200 and isinstance(result, Mapping) and result.get('ok') is True:
            # accepted:false is stale and must not retry forever either.
            with self._lock:
                path = self._report_path(identifier)
                if self._read(path) == body:
                    try: path.unlink()
                    except FileNotFoundError: pass

    def _recover(self, identifier, transport, headers):
        """Recover one owned account, including before a new command for it."""
        lock = OwnedLock(self.accounts._directory(identifier) / 'auth.lock', purpose='agy-account-recovery')
        try: lock.acquire(timeout=0)
        except LockBusy: return
        try:
            self._recover_locked(identifier, transport, headers)
        finally:
            lock.release()

    def _recover_locked(self, identifier, transport, headers):
        with self._lock:
            if identifier in self._flows: return
            operation = self._read(self._operation_path(identifier))
            if not operation: return
            state = self.accounts.read(identifier)
            if (state['state'] not in {'starting', 'waiting', 'verifying'}
                    or state['revision'] != operation.get('revision')):
                return
            pid, start = operation.get('pid'), operation.get('start_identity')
            if type(pid) is int and isinstance(start, str) and os.name != 'nt':
                # POSIX process identity proves this is our persisted tree.
                # Windows auth Jobs are kill-on-close and die with their owner.
                if process_start_identity(pid) == start:
                    signal_group(pid, getattr(signal, 'SIGTERM', 15))
                    signal_group(pid, getattr(signal, 'SIGKILL', 9))
                elif posix_group_alive(pid) and not posix_group_only_zombies(pid):
                    # Never signal a reused PID, and never admit a new login
                    # while a surviving tree's ownership cannot be proved.
                    raise ValueError('provider_cleanup_failed')
                deadline = time.monotonic() + 2
                while posix_group_alive(pid) and not posix_group_only_zombies(pid):
                    if time.monotonic() >= deadline:
                        raise ValueError('provider_cleanup_failed')
                    time.sleep(.02)
            self.accounts.set_state(identifier, state['revision'], 'failed', logged_in=False)
            self._report(operation['command_id'], identifier, state['revision'], 'failed',
                         transport, headers, error='login_interrupted')

    def _directories(self):
        # Keep the scandir continuation between ticks: memory and each tick's IO
        # are bounded while every account is eventually visited, including >201.
        with self._scan_lock:
            if self._scan is None: self._scan = os.scandir(self.accounts.root)
            entries = []
            for _ in range(64):
                try: entry = next(self._scan)
                except StopIteration:
                    self._scan.close()
                    self._scan = None
                    break
                if not entry.is_symlink() and entry.is_dir(follow_symlinks=False):
                    entries.append(Path(entry.path))
            return entries

    def tick(self, transport, headers):
        """Recover interrupted login and retry only current-revision durable reports."""
        for directory in self._directories():
            try:
                operation = self._read(directory / 'operation.json')
                if not operation: continue
                identifier = account_id(operation['account_id'])
                if self.accounts._directory(identifier) != directory: continue
                self._recover(identifier, transport, headers)
                body = self._read(directory / 'report.json')
                if not body or body.get('account_id') != identifier: continue
                if self.accounts.read(identifier)['revision'] != body['revision']:
                    (directory / 'report.json').unlink()
                    continue
                self._send_report(identifier, body, transport, headers)
            except (KeyError, ValueError, OSError): continue

    def _resolve(self, command_id, transport, headers):
        status, body = self._post(transport, '/api/supervisor/provider-accounts/resolve',
                                  {'command_id': command_id}, headers)
        if status != 200 or not isinstance(body, Mapping) or not isinstance(body.get('configuration'), Mapping):
            raise ValueError('provider_account_unavailable')
        return dict(body['configuration'])

    def execute(self, command, transport, headers):
        """Return (receipt status, bounded reason); login runs asynchronously."""
        try:
            if not isinstance(command, Mapping) or command.get('action') != 'manage_provider_account':
                raise ValueError('provider_account_unavailable')
            payload = control_payload(command.get('payload'))
            target, identifier = command.get('target'), payload['account_id']
            if (not isinstance(target, Mapping) or target.get('session_id') != identifier
                    or target.get('attempt_id') is not None):
                raise ValueError('provider_account_unavailable')
            cid = command.get('command_id')
            if not isinstance(cid, str) or not cid.startswith('cmd_'):
                raise ValueError('provider_account_unavailable')
            config = self._resolve(cid, transport, headers)
            if any(config.get(key) != value for key, value in payload.items()):
                raise ValueError('provider_account_unavailable')
            self._recover(identifier, transport, headers)
            return self._execute(identifier, cid, payload, config, transport, headers)
        except Exception as exc:
            reason = str(exc) if str(exc) in _ERROR_CODES else 'provider_account_failed'
            return 'failed', reason

    def _execute(self, identifier, cid, payload, config, transport, headers):
        operation, revision = payload['operation'], payload['revision']
        owner = payload['owner_digest']
        if operation == 'login':
            expires = config.get('expires_at')
            if type(expires) not in (int, float) or not time.time() < expires <= time.time() + _FLOW_TTL:
                raise ValueError('login_expired')
        if operation in {'submit_login', 'cancel_login'}:
            with self._lock:
                flow = self._flows.get(identifier)
                if (flow is None or flow.command_id != config.get('login_command_id')
                        or flow.owner_digest != owner or flow.expires <= time.time()
                        or flow.cancelled.is_set()):
                    raise ValueError('login_expired')
                if operation == 'submit_login':
                    if not flow.url: raise ValueError('login_expired')
                    response = login_response(flow.url, config.get('response'))
                    state = self.accounts.apply(identifier, operation, revision, owner_digest=owner)
                    # One signed submission delivers one line, including local retries.
                    if flow.answer.is_set():
                        if flow.report_command_id != cid: raise ValueError('login_expired')
                        return 'succeeded', 'submitted'
                    flow.report_command_id, flow.revision = cid, revision
                    self._save_operation(identifier, cid, payload,
                        login_command_id=flow.command_id, expires_at=flow.expires,
                        pid=flow.child.proc.pid if flow.child else None,
                        start_identity=process_start_identity(flow.child.proc.pid) if flow.child else None)
                    self._report(cid, identifier, revision, 'verifying', transport, headers)
                    flow.response = response
                    flow.answer.set()
                    return 'succeeded', 'submitted'
                self.accounts.apply(identifier, operation, revision, owner_digest=owner)
                self._save_operation(identifier, cid, payload)
                flow.cancelled.set()
            if not flow.done.wait(5):
                raise ValueError('provider_cleanup_failed')
            self._report(cid, identifier, revision, 'signed_out', transport, headers,
                         error='login_cancelled')
            return 'succeeded', 'cancelled'

        if operation == 'login':
            return self._begin_login(identifier, cid, payload, config, transport, headers)

        with self._lock:
            previous = None
            try: previous = self.accounts.read(identifier)
            except ValueError as exc:
                if str(exc) != 'provider_account_unavailable': raise
            state = self.accounts.apply(identifier, operation, revision, owner_digest=owner)
            # A repeated operation is a status read, never a second side effect.
            if previous and previous['revision'] == revision:
                saved = self._read(self._operation_path(identifier))
                completed = (saved and saved.get('completed') is True and saved.get('revision') == revision
                             and saved.get('operation') == operation and saved.get('command_id') == cid)
                if operation not in {'disable', 'logout'} or completed:
                    self._report(cid, identifier, revision, state['state'], transport, headers)
                    return 'succeeded', 'already_applied'
            self._save_operation(identifier, cid, payload, completed=operation in {'create', 'enable'})
            if operation == 'create':
                self._report(cid, identifier, revision, 'signed_out', transport, headers)
                return 'succeeded', 'created'

        if operation == 'enable':
            self._report(cid, identifier, revision, state['state'], transport, headers)
            return 'succeeded', 'enabled'
        if operation in {'disable', 'logout'}:
            try:
                self._stop(identifier)
                lock = OwnedLock(self.accounts._directory(identifier) / 'auth.lock', purpose='agy-account-auth')
                try: lock.acquire(timeout=5)
                except LockBusy: raise ValueError('provider_account_busy') from None
                try:
                    # Native logout is the only credential deletion mechanism;
                    # profile histories, user files and shared executable remain.
                    if operation == 'logout':
                        self._native_logout(identifier)
                    final = 'disabled' if operation == 'disable' else 'signed_out'
                    self.accounts.set_state(identifier, revision, final,
                        logged_in=bool(previous and previous['logged_in']) if operation == 'disable' else False)
                finally:
                    lock.release()
                self._save_operation(identifier, cid, payload, completed=True)
                self._report(cid, identifier, revision, final, transport, headers)
                return 'succeeded', 'disabled' if operation == 'disable' else 'signed_out'
            except Exception as exc:
                error = str(exc) if str(exc) in _ERROR_CODES else 'provider_cleanup_failed'
                self.accounts.set_state(identifier, revision, 'failed',
                                        logged_in=bool(previous and previous['logged_in']))
                self._report(cid, identifier, revision, 'failed', transport, headers, error=error)
                return 'failed', error
        raise ValueError('provider_account_unavailable')

    def _begin_login(self, identifier, cid, payload, config, transport, headers):
        with self._lock:
            try:
                current = self.accounts.read(identifier)
            except ValueError as exc:
                if str(exc) != 'provider_account_unavailable':
                    raise
                current = None
            if current is not None and current['revision'] == payload['revision']:
                self.accounts.apply(identifier, 'login', payload['revision'], owner_digest=payload['owner_digest'])
                flow = self._flows.get(identifier)
                if not flow and current['state'] in {'starting', 'waiting', 'verifying'}:
                    # Recovery skipped a live peer's auth lock, or local durable
                    # operation metadata was lost: never launch a second flow.
                    lock = OwnedLock(self.accounts._directory(identifier) / 'auth.lock', purpose='agy-account-recovery')
                    try: lock.acquire(timeout=0)
                    except LockBusy: raise ValueError('provider_account_busy') from None
                    try:
                        self.accounts.set_state(identifier, current['revision'], 'failed', logged_in=False)
                        self._report(cid, identifier, current['revision'], 'failed', transport, headers, error='login_interrupted')
                    finally:
                        lock.release()
                    return 'failed', 'login_interrupted'
                fields = {'authorization_url': flow.url, 'expires_at': flow.expires} if flow and flow.url else {}
                self._report(cid, identifier, current['revision'], current['state'], transport, headers, **fields)
                return 'succeeded', 'already_applied'
        # Lock before publishing starting: another live ControlClient must not
        # mistake the prelaunch window for a crashed owner's interrupted flow.
        lock = OwnedLock(self.accounts._directory(identifier) / 'auth.lock', purpose='agy-account-auth')
        try: lock.acquire(timeout=0)
        except LockBusy: raise ValueError('provider_account_busy') from None
        transferred = False
        try:
            with self._lock:
                state = self.accounts.apply(identifier, 'login', payload['revision'], owner_digest=payload['owner_digest'])
                if identifier in self._flows: return 'succeeded', 'already_applied'
                flow = _Flow(identifier, cid, payload['revision'], payload['owner_digest'], config['expires_at'])
                flow.generation, flow.auth_lock = state['generation'], lock
                self._save_operation(identifier, cid, payload, login_command_id=cid, expires_at=flow.expires)
                self._flows[identifier] = flow
                flow.thread = threading.Thread(target=self._login,
                    args=(flow, payload, transport, dict(headers)),
                    name='agy-login-' + identifier[-8:], daemon=True)
                flow.thread.start()
                transferred = True
                return 'succeeded', 'login_started'
        finally:
            if not transferred: lock.release()

    def _stop(self, identifier, *, exclude_flow=None):
        with self._lock:
            flow = self._flows.get(identifier)
            if flow is not None and flow is not exclude_flow:
                flow.cancelled.set()
        if flow is not None and flow is not exclude_flow and not flow.done.wait(5):
            raise ValueError('provider_cleanup_failed')
        if self.supervisor is not None and self.supervisor.stop_provider_account(identifier) is not True:
            raise ValueError('provider_cleanup_failed')
        if not self.accounts.quiesce(identifier, timeout=5):
            raise ValueError('provider_account_busy')

    def _launch(self, identifier, suffix=()):
        from tools.adapters.agy import AgyAdapter
        config = self.cfg.agents.get('agy', {})
        source = {**os.environ, **(config.get('environment') or {})}
        # Use the existing launcher validation/normalization; no shell parsing.
        adapter = AgyAdapter(command=config.get('command'), environment=config.get('environment'))
        argv = adapter.build_argv('account-auth-check')
        vendor = json.loads(argv[argv.index('--vendor-command') + 1])
        env = self.accounts.environment(identifier, source)
        workspace = self.accounts._directory(identifier) / 'auth-workspace'
        from tools.providers.agy_accounts import _directory
        _directory(workspace)
        return _Child([*vendor, *suffix], cwd=str(workspace), env=env)

    @staticmethod
    def _drain(stream, rows, failed, *, stderr=False):
        """Bound retained bytes and total output; drain status prefixes and stderr."""
        count = 0
        try:
            while True:
                raw = stream.read(4096) if stderr else stream.readline(_MAX_LINE + 3)
                if not raw: return
                count += len(raw)
                if count > _MAX_OUTPUT or not stderr and len(raw.rstrip(b'\r\n')) > _MAX_LINE:
                    failed.set()
                    return
                if not stderr:
                    text = raw.decode('utf-8', 'strict').strip()
                    if text.startswith('https://'):
                        try: rows.put_nowait(text)
                        except queue.Full:
                            failed.set()
                            return
        except (OSError, UnicodeError, ValueError): failed.set()

    def _flow_cancelled(self, flow):
        if flow.cancelled.is_set() or flow.expires <= time.time(): return True
        try:
            state = self.accounts.read(flow.account_id)
            return state['generation'] != flow.generation or state['state'] not in {'starting', 'waiting', 'verifying'}
        except ValueError: return True

    def _login(self, flow, payload, transport, headers):
        identifier, cid = flow.account_id, flow.command_id
        lock = flow.auth_lock
        child, readers = None, []
        error = None
        try:
            self._stop(identifier, exclude_flow=flow)
            if self._flow_cancelled(flow): raise ValueError('login_expired')
            self._report(cid, identifier, flow.revision, 'starting', transport, headers)
            with self._lock:
                if self._flow_cancelled(flow): raise ValueError('login_expired')
                child = self._launch(identifier, ('--login',))
                flow.child = child
                self._save_operation(identifier, cid, payload, login_command_id=cid,
                    expires_at=flow.expires, pid=child.proc.pid,
                    start_identity=process_start_identity(child.proc.pid))
            rows, failed = queue.Queue(maxsize=4), threading.Event()
            for stream, stderr in ((child.proc.stdout, False), (child.proc.stderr, True)):
                thread = threading.Thread(target=self._drain, args=(stream, rows, failed),
                                          kwargs={'stderr': stderr}, daemon=True)
                thread.start()
                readers.append(thread)
            end = time.monotonic() + min(self.login_timeout, flow.expires - time.time())
            while time.monotonic() < end and not self._flow_cancelled(flow):
                if failed.is_set(): raise ValueError('login_failed')
                try: raw_url = rows.get(timeout=.05)
                except queue.Empty:
                    if child.proc.poll() is not None: raise ValueError('login_failed')
                    continue
                flow.url = authorization_url(raw_url)
                break
            if not flow.url: raise ValueError('login_expired')
            with self._lock:
                if self._flow_cancelled(flow): raise ValueError('login_expired')
                self.accounts.set_state(identifier, flow.revision, 'waiting')
                self._report(cid, identifier, flow.revision, 'waiting', transport, headers,
                             authorization_url=flow.url, expires_at=flow.expires)
            while time.monotonic() < end and not self._flow_cancelled(flow):
                if failed.is_set(): raise ValueError('login_failed')
                if flow.answer.wait(.05): break
                if child.proc.poll() is not None: raise ValueError('login_failed')
            if self._flow_cancelled(flow) or time.monotonic() >= end or flow.response is None:
                raise ValueError('login_expired')
            response, flow.response = flow.response, None
            write_done, write_failed = threading.Event(), threading.Event()
            def write_response():
                try:
                    child.proc.stdin.write((response + '\n').encode('utf-8'))
                    child.proc.stdin.flush()
                    child.proc.stdin.close()
                except (OSError, ValueError): write_failed.set()
                finally: write_done.set()
            writer = threading.Thread(target=write_response, daemon=True)
            writer.start()
            readers.append(writer)
            while not write_done.wait(.05):
                if self._flow_cancelled(flow) or time.monotonic() >= end: raise ValueError('login_expired')
            if write_failed.is_set(): raise ValueError('login_failed')
            while child.proc.poll() is None:
                if self._flow_cancelled(flow) or time.monotonic() >= end: raise ValueError('login_expired')
                if failed.wait(.05): raise ValueError('login_failed')
            if child.proc.returncode != 0 or failed.is_set(): raise ValueError('login_failed')
            child.close()
            child, flow.child = None, None
            if self._flow_cancelled(flow): raise ValueError('login_expired')
            self._probe(identifier, logout=False, should_cancel=lambda: self._flow_cancelled(flow))
            if self._flow_cancelled(flow): raise ValueError('login_expired')
            with self._lock:
                self.accounts.set_state(identifier, flow.revision, 'ready', logged_in=True)
                self._report(flow.report_command_id, identifier, flow.revision, 'ready', transport, headers)
        except Exception as exc:
            error = str(exc) if str(exc) in _ERROR_CODES else 'login_failed'
        finally:
            if child is not None:
                try: child.close()
                except ValueError: error = 'provider_cleanup_failed'
            for thread in readers: thread.join(timeout=1)
            with self._lock:
                if error:
                    try:
                        state = self.accounts.read(identifier)
                        if state['revision'] == flow.revision and state['state'] in {'starting', 'waiting', 'verifying'}:
                            self.accounts.set_state(identifier, flow.revision, 'failed', logged_in=False)
                            self._report(flow.report_command_id, identifier, flow.revision, 'failed',
                                         transport, headers, error=error)
                    except (ValueError, OSError): pass
                if self._flows.get(identifier) is flow: self._flows.pop(identifier, None)
                flow.response = None
            lock.release()
            flow.done.set()

    def _probe(self, identifier, *, logout, should_cancel=None):
        child = self._launch(identifier)
        process = child.proc
        transport = AcpTransport(process, should_abort=should_cancel)
        try:
            init = transport.request('initialize', {
                'protocolVersion': 1,
                'clientInfo': {'name': 'agent-fleet-accounts', 'title': 'AGY accounts', 'version': '1'},
                'clientCapabilities': {'fs': {'readTextFile': False, 'writeTextFile': False}, 'terminal': False},
            }, timeout=15)
            if type(init.get('protocolVersion')) is not int or init['protocolVersion'] != 1:
                raise ValueError('provider_authentication_failed')
            if logout:
                caps = init.get('agentCapabilities') if isinstance(init.get('agentCapabilities'), Mapping) else {}
                auth = caps.get('auth') if isinstance(caps.get('auth'), Mapping) else {}
                if auth.get('logout') is not True:
                    raise ValueError('provider_account_logout_unsupported')
                transport.request('logout', {}, timeout=30)
            else:
                transport.request('authenticate', {'methodId': 'oauth-personal'}, timeout=20)
                setup = transport.request('session/new', {'cwd': str(self.accounts._directory(identifier) / 'auth-workspace'),
                                                        'mcpServers': []}, timeout=20)
                if not isinstance(setup.get('sessionId'), str) or not setup['sessionId']:
                    raise ValueError('provider_authentication_failed')
        except AcpTransportError:
            raise ValueError('provider_authentication_failed') from None
        finally:
            transport.close()
            child.close()

    def _native_logout(self, identifier):
        self._probe(identifier, logout=True)

    def close(self):
        with self._lock:
            flows = list(self._flows.values())
            for flow in flows: flow.cancelled.set()
        for flow in flows:
            if not flow.done.wait(5): raise ValueError('provider_cleanup_failed')
        with self._scan_lock:
            if self._scan is not None:
                self._scan.close()
                self._scan = None
