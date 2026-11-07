"""Own one bounded gh login; parent EOF or explicit cancellation stops it."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import threading
from urllib.parse import urlsplit

from fleet_core.process.bounded_process import ProcessAborted, run_streamed
from fleet_core.locking import LockBusy, OwnedLock
from .login_store import ACTIVE, LoginStore


def run(store, login_id, executable, *, parent_stream=None, timeout_s=900, git_protocol='https'):
    job = store.get(login_id)
    parent_gone = threading.Event()

    def watch_parent():
        try:
            if parent_stream is not None:
                parent_stream.read()
            else:
                # Read the raw fd instead of sys.stdin.buffer: a daemon thread
                # blocked in BufferedReader.read() holds the buffer lock, and
                # interpreter shutdown then dies with "Fatal Python error:
                # _enter_buffered_busy" whenever the parent keeps its stdin
                # pipe open past our exit. os.read takes no Python IO lock.
                while os.read(0, 65536):
                    pass
        finally:
            parent_gone.set()

    threading.Thread(target=watch_parent, daemon=True, name='plugin-parent-watch').start()

    def aborted():
        current = store.get(login_id)
        return parent_gone.is_set() or current['cancel_requested'] or current['state'] not in ACTIVE

    def progress(line):
        line = re.sub(r'\x1b\[[0-9;]*m', '', line[:4096])
        current = store.get(login_id)
        values = {}
        code = re.search(r'one-time code:\s*([A-Z0-9]{4}-[A-Z0-9]{4})\b', line)
        if code:
            values['user_code'] = code.group(1)
        for raw in re.findall(r'https://[^\s<>]+', line):
            parsed = urlsplit(raw)
            if parsed.hostname == job['hostname'] and not parsed.username and not parsed.password:
                values['verification_uri'] = raw[:2048]
        if values:
            store.update(login_id, 'waiting_authorization', **values)
        elif current['state'] in ACTIVE:
            store.update(login_id, current['state'])

    context_lock = OwnedLock(Path(store.path).parent / ('context-' + job['context_id'] + '.lock'), purpose='github-login')
    try:
        # Keep ownership until gh is completely reaped. A new node process
        # may mark the old job unknown before this worker has finished cleanup.
        context_lock.acquire(timeout=5)
        if aborted():
            raise ProcessAborted()
        code = run_streamed(
            [executable, 'auth', 'login', '--hostname', job['hostname'], '--web',
             '--git-protocol', git_protocol, '--skip-ssh-key'],
            cwd=Path(store.path).parent, env=dict(os.environ), timeout_s=timeout_s,
            on_line=progress, should_abort=aborted)
        store.update(login_id, 'succeeded' if code == 0 else 'failed',
                     error_code=None if code == 0 else 'login_failed')
    except ProcessAborted:
        cancelled = store.get(login_id)['cancel_requested']
        store.update(login_id, 'cancelled' if cancelled else 'unknown',
                     error_code='login_cancelled' if cancelled else 'node_stopped')
    except subprocess.TimeoutExpired:
        store.update(login_id, 'expired', error_code='login_expired')
    except LockBusy:
        store.update(login_id, 'failed', error_code='context_busy')
    except Exception:
        store.update(login_id, 'failed', error_code='login_worker_failed')
    finally:
        context_lock.release()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', required=True)
    parser.add_argument('--login-id', required=True)
    parser.add_argument('--executable', required=True)
    parser.add_argument('--git-protocol', choices=('https', 'ssh'), default='https')
    args = parser.parse_args(argv)
    return run(LoginStore(args.store), args.login_id, args.executable, git_protocol=args.git_protocol)


if __name__ == '__main__':
    raise SystemExit(main())
