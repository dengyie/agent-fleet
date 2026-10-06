"""Installed deployment helper, run as the Hub user inside its container.

Inputs arrive over stdin, never as credential-bearing command arguments.
This file deliberately has no imports from the release being deployed.
"""
import http.client
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time


def processes(root):
    found = []
    for path in Path('/proc').iterdir():
        if not path.name.isdecimal():
            continue
        try:
            if path.stat().st_uid != os.getuid() or path.joinpath('cwd').resolve() != root.resolve():
                continue
            argv = path.joinpath('cmdline').read_bytes().decode().split('\0')
            kind = next((kind for kind, suffix in [('hub', 'hub/web.py'), ('guardian', 'deploy/hk-self-report-loop.sh')]
                         if any(arg == suffix or arg.endswith('/' + suffix) for arg in argv)), None)
            if kind:
                started = path.joinpath('stat').read_text().split(') ', 1)[1].split()[19]
                found.append((int(path.name), kind, started))
        except (OSError, UnicodeError, IndexError):
            continue
    return found


def assert_idle(root):
    queries = [('var/platform/platform.db', 'runs', ('succeeded', 'failed', 'unknown', 'cancelled')),
               ('state/fleet.db', 'tasks', ('succeeded', 'failed', 'cancelled', 'expired'))]
    for relative, table, terminal in queries:
        with sqlite3.connect((root / relative).as_uri() + '?mode=ro', uri=True, timeout=3) as db:
            count = db.execute(f"SELECT count(*) FROM {table} WHERE state NOT IN ({','.join('?' for _ in terminal)})", terminal).fetchone()[0]
            if count:
                raise RuntimeError('active_tasks_prevent_deployment')


def stop(root):
    assert_idle(root)
    # Guardian first, so it cannot restart the Hub during the transition.
    for kind in ('guardian', 'hub'):
        original = {row for row in processes(root) if row[1] == kind}
        for pid, _, _ in original & set(processes(root)):
            os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 8
        while original & set(processes(root)) and time.monotonic() < deadline:
            time.sleep(.1)
        for pid, _, _ in original & set(processes(root)):
            os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 3
        while original & set(processes(root)) and time.monotonic() < deadline:
            time.sleep(.1)
    if processes(root):
        raise RuntimeError('process_stop_failed')


def probe(config, credentials=None):
    cookie = None

    def request(path, body=None, expected=200):
        nonlocal cookie
        connection = http.client.HTTPConnection('127.0.0.1', config['port'], timeout=5)
        headers = {'Origin': config['public_origin'], 'Content-Type': 'application/json'}
        if cookie:
            headers['Cookie'] = cookie
        try:
            connection.request('GET' if body is None else 'POST', path,
                               None if body is None else json.dumps(body), headers)
            response = connection.getresponse()
            raw = response.read(2 * 1024 * 1024)
            if response.status != expected:
                raise RuntimeError('probe_http_status')
            if response.getheader('Set-Cookie'):
                cookie = response.getheader('Set-Cookie').split(';', 1)[0]
            return json.loads(raw)
        finally:
            connection.close()

    request('/healthz')
    request('/api/operator/session', expected=401)
    if credentials is not None:
        logged_in = False
        try:
            request('/api/accounts/login', {
                'email': credentials.get('username') or credentials['email'],
                'password': credentials['password'],
            })
            logged_in = bool(cookie)
            if not logged_in:
                raise RuntimeError('probe_login_cookie_missing')
            request('/api/operator/session')
            if request('/api/platform/v1/readiness').get('configuration_ready') is not True:
                raise RuntimeError('probe_not_ready')
        finally:
            if logged_in:
                previous = cookie
                request('/api/accounts/logout', {})
                cookie = previous
                request('/api/operator/session', expected=401)


def main():
    data = json.load(sys.stdin)
    root = Path(data['live'])
    mode = data['mode']
    if mode == 'preflight':
        assert_idle(root)
        observed = processes(root)
        if [row[1] for row in observed].count('hub') != 1 or [row[1] for row in observed].count('guardian') != 1:
            raise RuntimeError('unexpected_process_topology')
        probe(data, data.get('credentials'))
    elif mode == 'idle':
        assert_idle(root)
    elif mode == 'stop':
        stop(root)
    elif mode == 'stage':
        subprocess.run([sys.executable, '-c', 'import flask,yaml; from hub import web; from hub.bootstrap import create_app'],
                       cwd=data['stage'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    elif mode == 'verify':
        origin = dict(line.split(': ', 1) for line in (root / 'RELEASE_ORIGIN').read_text().splitlines())
        if origin.get('commit') != data['sha']:
            raise RuntimeError('backend_revision_mismatch')
        deadline = time.monotonic() + 30
        while True:
            try:
                probe(data)
                break
            except (OSError, RuntimeError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.5)
        probe(data, data['credentials'])
        if sorted(row[1] for row in processes(root)) != ['guardian', 'hub']:
            raise RuntimeError('unexpected_process_topology')
    else:
        raise RuntimeError('invalid_helper_mode')
    print(json.dumps({'status': 'ok', 'mode': mode}))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # No traceback, request data, credential or provider response in CI logs.
        print(json.dumps({'status': 'failed', 'error': type(exc).__name__}))
        raise SystemExit(1)
