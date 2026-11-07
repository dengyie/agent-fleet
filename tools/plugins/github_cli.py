"""GitHub CLI host adapter; never changes the execution user's identity."""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import getpass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

from fleet_core.plugins import OFFICIAL_PLUGINS, PluginError, identifier
from fleet_core.locking import LockBusy, OwnedLock
from fleet_core.process.bounded_process import run_bounded
from fleet_core.process.local_process import popen
from .login_store import LoginStore

TOKEN_VARIABLES = ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN')
MAX_OUTPUT = 128 * 1024


def default_config_dir():
    env = os.environ
    if env.get('GH_CONFIG_DIR'):
        return env['GH_CONFIG_DIR']
    if env.get('XDG_CONFIG_HOME'):
        return str(Path(env['XDG_CONFIG_HOME']) / 'gh')
    if os.name == 'nt' and env.get('APPDATA'):
        return str(Path(env['APPDATA']) / 'GitHub CLI')
    return str(Path.home() / '.config' / 'gh')


def os_user():
    if os.name == 'posix':
        import pwd
        return pwd.getpwuid(os.geteuid()).pw_name
    return getpass.getuser()


@dataclass(frozen=True)
class GitHubContext:
    context_id: str
    label: str
    config_dir: str

    def environment(self, hostname):
        return {**os.environ, 'GH_CONFIG_DIR': self.config_dir, 'GH_HOST': hostname,
                'NO_COLOR': '1', 'CLICOLOR': '0', 'GH_PROMPT_DISABLED': '1'}

    def public(self):
        return {'context_id': self.context_id, 'label': self.label,
                'config_dir': self.config_dir, 'os_user': os_user(),
                'environment_sources': [k for k in TOKEN_VARIABLES if os.environ.get(k)],
                'env_override': any(os.environ.get(k) for k in TOKEN_VARIABLES)}


class GitHubCliPlugin:
    definition = OFFICIAL_PLUGINS.get('github-cli')

    def __init__(self, state_directory, config=None):
        self.state_directory = Path(state_directory)
        self.state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.config = dict(config or {})
        if set(self.config) - {'executable', 'contexts'}:
            raise PluginError('invalid_plugin_config')
        self.executable = self.config.get('executable', 'gh')
        if not isinstance(self.executable, str) or not self.executable or '\0' in self.executable:
            raise PluginError('invalid_plugin_config')
        contexts = self.config.get('contexts', [{'context_id': 'default'}])
        if not isinstance(contexts, list) or not 1 <= len(contexts) <= 16:
            raise PluginError('invalid_plugin_contexts')
        self.contexts = {}
        directories = set()
        for value in contexts:
            if not isinstance(value, dict) or set(value) - {'context_id', 'label', 'config_dir'}:
                raise PluginError('invalid_plugin_contexts')
            cid = identifier(value.get('context_id'), 'invalid_context')
            label = value.get('label', cid)
            raw_directory = value.get('config_dir', default_config_dir())
            if not isinstance(raw_directory, str) or not raw_directory or '\0' in raw_directory:
                raise PluginError('invalid_plugin_contexts')
            directory = Path(raw_directory).expanduser()
            if (not directory.is_absolute() or cid in self.contexts
                    or not isinstance(label, str) or len(label) > 128):
                raise PluginError('invalid_plugin_contexts')
            directory = str(directory.resolve())
            if directory in directories:
                raise PluginError('duplicate_plugin_context')
            directories.add(directory)
            self.contexts[cid] = GitHubContext(cid, label, directory)
        self.store = LoginStore(self.state_directory / 'logins.db')
        self.store.recover()
        self._workers = {}
        self._lock = threading.Lock()

    @staticmethod
    def _override(hostname):
        keys = TOKEN_VARIABLES[:2] if hostname == 'github.com' or hostname.endswith('.ghe.com') else TOKEN_VARIABLES[2:]
        return next((key for key in keys if os.environ.get(key)), None)

    def _run(self, context, hostname, argv, timeout=20):
        executable = shutil.which(self.executable)
        if not executable:
            raise PluginError('dependency_missing')
        try:
            result = run_bounded([executable, *argv], cwd=self.state_directory,
                                 env=context.environment(hostname), timeout_s=timeout,
                                 stdout_limit=MAX_OUTPUT + 1, stderr_limit=8193)
        except subprocess.TimeoutExpired:
            raise PluginError('network_timeout') from None
        except OSError:
            raise PluginError('cli_unavailable') from None
        if len(result.stdout) > MAX_OUTPUT or len(result.stderr) > 8192:
            raise PluginError('cli_output_too_large')
        return result

    def manifest(self):
        available, version = False, ''
        try:
            result = self._run(next(iter(self.contexts.values())), 'github.com', ['--version'], timeout=5)
            available = result.returncode == 0
            version = result.stdout.decode('utf-8', errors='replace').splitlines()[0][:160]
        except (PluginError, IndexError):
            pass
        return {'plugin_id': self.definition.plugin_id, 'version': self.definition.version,
                'api_version': self.definition.api_version, 'available': available,
                'dependency_version': version, 'contexts': [c.public() for c in self.contexts.values()]}

    def _status(self, context, host):
        result = self._run(context, host, ['auth', 'status', '--hostname', host, '--json', 'hosts'])
        try:
            payload = json.loads(result.stdout)
        except (ValueError, TypeError, KeyError):
            if result.stdout.strip():
                raise PluginError('invalid_cli_response') from None
            error = result.stderr.decode('utf-8', errors='replace').lower()
            if 'not logged' in error:
                payload = {'hosts': {}}
            elif 'unknown flag' in error or 'unknown shorthand' in error:
                raise PluginError('cli_upgrade_required') from None
            elif any(s in error for s in ('timeout', 'connection', 'dial tcp', 'network', 'no such host')):
                raise PluginError('network_unreachable') from None
            else:
                raise PluginError('auth_check_failed') from None
        if not isinstance(payload, dict) or not isinstance(payload.get('hosts'), dict):
            raise PluginError('invalid_cli_response')
        rows = payload['hosts'].get(host, [])
        if not isinstance(rows, list) or len(rows) > 64:
            raise PluginError('invalid_cli_response')
        accounts, network_error = [], False
        for row in rows[:16]:
            if not isinstance(row, dict) or not isinstance(row.get('login'), str):
                raise PluginError('invalid_cli_response')
            failure = str(row.get('error', '')).lower()
            network_failure = row.get('state') != 'success' and any(s in failure for s in (
                'timeout', 'connection', 'dial tcp', 'network', 'no such host'))
            network_error |= network_failure
            accounts.append({'login': row['login'][:128], 'active': row.get('active') is True,
                             'state': 'success' if row.get('state') == 'success' else 'error',
                             'error_code': ('network_unreachable' if network_failure else 'authentication_failed')
                                           if row.get('state') != 'success' else None,
                             'token_source': str(row.get('tokenSource', 'unknown'))[:128],
                             'scopes': str(row.get('scopes', ''))[:256],
                             'git_protocol': str(row.get('gitProtocol', ''))[:16]})
        active = next((a for a in accounts if a['active'] and a['state'] == 'success'), None)
        if not active and network_error:
            raise PluginError('network_unreachable')
        state = ('authenticated' if active and all(a['state'] == 'success' for a in accounts)
                 else 'partial' if active else 'authentication_failed' if accounts else 'not_logged_in')
        return {'state': state, 'active_account': active['login'] if active else None,
                'environment_source': self._override(host), 'accounts': accounts,
                'accounts_truncated': len(rows) > 16}

    @contextmanager
    def _mutation(self, context, action):
        if action not in {'login_start', 'switch', 'logout'}:
            yield
            return
        lock = OwnedLock(self.state_directory / ('context-' + context.context_id + '.lock'), purpose='github-auth')
        try:
            lock.acquire()
        except LockBusy:
            raise PluginError('context_busy') from None
        try:
            yield
        finally:
            lock.release()

    def execute(self, action, arguments, *, command_id):
        args = self.definition.validate(action, arguments)
        context = self.contexts.get(args['context_id'])
        if context is None:
            raise PluginError('context_unavailable')
        host = args['hostname']
        with self._lock, self._mutation(context, action):
            for login_id, worker in list(self._workers.items()):
                if worker.poll() is not None:
                    if worker.stdin:
                        worker.stdin.close()
                    worker.wait()
                    self.store.update(login_id, 'unknown', error_code='login_worker_stopped')
                    self._workers.pop(login_id)
            if action in {'login_status', 'login_cancel'}:
                job = self.store.get(args['login_id'])
                if job['context_id'] != context.context_id or job['hostname'] != host:
                    raise PluginError('login_not_found')
                return self.store.cancel(args['login_id']) if action == 'login_cancel' else job
            if action in {'login_start', 'switch', 'logout'}:
                if self._override(host):
                    raise PluginError('environment_override')
                if self.store.active(context.context_id):
                    raise PluginError('context_busy')
            if action == 'login_start':
                return self._login(context, host, command_id)
            if action == 'repo_check':
                result = self._run(context, host, ['api', '--hostname', host, 'repos/' + args['repository']])
                if result.returncode:
                    error = result.stderr.decode('utf-8', errors='replace').lower()
                    if any(s in error for s in ('timeout', 'connection', 'network', 'dial tcp')):
                        raise PluginError('network_unreachable')
                    raise PluginError('repository_inaccessible')
                try:
                    repo = json.loads(result.stdout)
                    if not isinstance(repo, dict):
                        raise ValueError()
                    perms = repo.get('permissions') or {}
                    if not isinstance(perms, dict):
                        raise ValueError()
                    return {'repository': {'name': str(repo['full_name'])[:256],
                            'private': repo.get('private') is True,
                            'permissions': {k: perms[k] for k in ('pull', 'push', 'admin', 'maintain', 'triage')
                                            if type(perms.get(k)) is bool}}, 'context': context.public()}
                except (TypeError, ValueError, KeyError):
                    raise PluginError('invalid_cli_response') from None
            if action in {'switch', 'logout'}:
                result = self._run(context, host, ['auth', action, '--hostname', host, '--user', args['username']])
                if result.returncode:
                    raise PluginError('account_action_failed')
            return {'auth': self._status(context, host), 'context': context.public()}

    def _login(self, context, host, command_id):
        executable = shutil.which(self.executable)
        if not executable:
            raise PluginError('dependency_missing')
        configured = self._run(context, host, ['config', 'get', 'git_protocol', '--host', host])
        protocol = configured.stdout.decode('utf-8', errors='replace').strip()
        if configured.returncode or protocol not in {'https', 'ssh'}:
            raise PluginError('git_protocol_unavailable')
        job, created = self.store.create(command_id, context.context_id, host)
        if not created:
            return job
        try:
            worker = popen([sys.executable, '-m', 'tools.plugins.login_worker',
                           '--store', str(self.store.path.resolve()), '--login-id', command_id,
                           '--executable', executable, '--git-protocol', protocol], cwd=Path(__file__).resolve().parents[2],
                          env=context.environment(host), stdin=subprocess.PIPE,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._workers[command_id] = worker
        except OSError:
            self.store.update(command_id, 'failed', error_code='login_worker_failed')
            raise PluginError('login_worker_failed') from None
        return job

    def close(self):
        with self._lock:
            workers, self._workers = self._workers, {}
            for worker in workers.values():
                if worker.stdin:
                    worker.stdin.close()
            for worker in workers.values():
                try:
                    worker.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    # The worker owns gh's process group and enforces its own
                    # deadline. Do not kill its cleanup supervisor.
                    pass
