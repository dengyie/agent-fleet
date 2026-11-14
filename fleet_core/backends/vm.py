"""Direct execution on an explicitly configured execution VM/account."""
import math
import os
import subprocess

from .directory import DirectoryBackend, MAX_OUTPUT_BYTES
from fleet_core.process.bounded_process import (
    ProcessCancelled, ProcessStopUnconfirmed, decode_prefix, run_bounded,
)


class VMBackend(DirectoryBackend):
    """The VM/account is the boundary; commands run directly with bounded life.

    File tools retain workspace path checks. Explicit commands can use the
    account filesystem and network, including shell/interpreter argv.
    """

    def execute(self, command_id, argv, *, timeout_s=30.0, should_cancel=None):
        if (type(argv) is not list or not 1 <= len(argv) <= 256
                or any(type(arg) is not str or len(arg) > 4096 or '\x00' in arg for arg in argv)
                or not argv[0]):
            return self._receipt(command_id, 'failed', error_code='invalid_command')
        try:
            if sum(len(arg.encode('utf-8')) for arg in argv) > 65536:
                raise ValueError('argv too large')
        except (UnicodeError, ValueError):
            return self._receipt(command_id, 'failed', error_code='invalid_command')
        if (type(timeout_s) not in (int, float) or not 0 < timeout_s <= 600
                or not math.isfinite(timeout_s)):
            return self._receipt(command_id, 'failed', error_code='invalid_timeout')
        try:
            home, temporary = self.root / '.fleet-home', self.root / '.fleet-tmp'
            home.mkdir(exist_ok=True)
            temporary.mkdir(exist_ok=True)
            env = {'PATH': os.environ.get('PATH', os.defpath), 'HOME': str(home),
                   'TMPDIR': str(temporary), 'TMP': str(temporary), 'TEMP': str(temporary),
                   'LANG': 'C.UTF-8', 'PYTHONIOENCODING': 'utf-8'}
            if os.name == 'nt':
                for name in ('SystemRoot', 'WINDIR', 'COMSPEC', 'PATHEXT'):
                    if name in os.environ:
                        env[name] = os.environ[name]
            proc = run_bounded(argv, cwd=self.root, timeout_s=min(float(timeout_s), 30),
                stdout_limit=MAX_OUTPUT_BYTES + 1, env=env, should_cancel=should_cancel)
            output, truncated = decode_prefix(proc.stdout, MAX_OUTPUT_BYTES)
            return self._receipt(command_id, 'succeeded' if proc.returncode == 0 else 'failed',
                {'returncode': proc.returncode, 'output': output},
                error_code=None if proc.returncode == 0 else 'process_failed', truncated=truncated)
        except subprocess.TimeoutExpired:
            return self._receipt(command_id, 'failed', error_code='timeout')
        except ProcessCancelled:
            return self._receipt(command_id, 'cancelled', error_code='cancelled')
        except ProcessStopUnconfirmed:
            return self._receipt(command_id, 'unknown', error_code='process_stop_unconfirmed')
        except OSError:
            return self._receipt(command_id, 'failed', error_code='exec_failed')
