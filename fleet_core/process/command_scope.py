"""Linux cgroup v2 ownership for one direct VM command and all descendants."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import time
import uuid


class CommandScopeUnavailable(OSError):
    """No delegated command scope is available; execution has not started."""


class CommandScopeStopUnconfirmed(RuntimeError):
    """The owned cgroup could not be confirmed empty."""


def command_scope_root():
    try:
        relative = next(line[3:] for line in Path('/proc/self/cgroup').read_text().splitlines()
                        if line.startswith('0::'))
        current = Path('/sys/fs/cgroup') / relative.lstrip('/')
    except (OSError, StopIteration):
        raise CommandScopeUnavailable('command_scope_unavailable') from None
    configured = os.environ.get('FLEET_COMMAND_CGROUP_ROOT')
    root = Path(configured) if configured else current
    if (not root.is_absolute() or not (root / 'cgroup.procs').is_file()
            or not os.access(root, os.W_OK)
            or not current.resolve().is_relative_to(root.resolve())):
        raise CommandScopeUnavailable('command_scope_unavailable')
    return root


class CommandScope:
    def __init__(self):
        self.path = command_scope_root() / ('fleet-command-' + uuid.uuid4().hex)
        try:
            self.path.mkdir()
            if not os.access(self.path / 'cgroup.kill', os.W_OK):
                self.path.rmdir()
                raise CommandScopeUnavailable('command_scope_unavailable')
        except OSError as exc:
            raise CommandScopeUnavailable('command_scope_unavailable') from exc

    def wrap(self, argv):
        # A fresh interpreter enters the cgroup before executing user argv.
        # preexec_fn is deliberately avoided in the multithreaded Node.
        helper = Path(__file__).with_name('command_scope_exec.py')
        return [sys.executable, '-I', str(helper), str(self.path), str(os.getpid()), *argv]

    def close(self):
        try:
            (self.path / 'cgroup.kill').write_text('1', encoding='ascii')
            deadline = time.monotonic() + 2
            while 'populated 1' in (self.path / 'cgroup.events').read_text(encoding='ascii'):
                if time.monotonic() >= deadline:
                    raise CommandScopeStopUnconfirmed('process_stop_unconfirmed')
                time.sleep(.01)
            self.path.rmdir()
        except OSError as exc:
            raise CommandScopeStopUnconfirmed('process_stop_unconfirmed') from exc

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
