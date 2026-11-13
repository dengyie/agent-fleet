"""Local execution context used by agent-side collectors.

The hub never executes commands on another machine. Every probe runs on the
machine where this context is created and only returns bounded stdout.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from fleet_core.process.bounded_process import run_bounded
from fleet_core.process.local_process import resolve_bash

FLEET_HOME = Path(__file__).resolve().parent.parent

#: Retained stdout per probe command; excess output is drained and dropped.
MAX_PROBE_OUTPUT = 8 << 20

# ``python -`` semantics (``sys.path[0] == ''`` with cwd FLEET_HOME) for a
# script read from a file: run_bounded has no stdin feed.
_RUN_SCRIPT = ("import sys;p=sys.argv[1];sys.argv=['-'];"
               "exec(compile(open(p,encoding='utf-8').read(),'<stdin>','exec'),"
               "{'__name__':'__main__','__builtins__':__builtins__})")


def run_local(argv, *, timeout, cwd):
    """Bounded local spawn: one deadline covering pipe drain and group reap.

    Returns a text ``CompletedProcess``; stderr is drained and discarded.
    """
    result = run_bounded(list(argv), cwd=cwd, timeout_s=timeout,
                         stdout_limit=MAX_PROBE_OUTPUT, stderr_limit=0)
    return subprocess.CompletedProcess(
        result.args, result.returncode,
        (result.stdout or b"").decode("utf-8", errors="replace"), "")


class ProbeContext:
    """Bounded local command execution for one probe cycle."""

    def __init__(self, machine, timeout=10):
        self.machine = machine
        self.local = True
        self.timeout = timeout

    def run(self, cmd):
        shell = resolve_bash()
        if shell is None:
            # No *real* POSIX shell on this host (e.g. Windows without Git
            # Bash): return empty output rather than spawn the WSL shim and
            # lose the whole cycle to its failure notice.
            return ""
        try:
            result = run_local([shell, "-c", cmd], timeout=self.timeout,
                               cwd=str(FLEET_HOME))
            return (result.stdout or "").strip()
        except (subprocess.TimeoutExpired, OSError,
                subprocess.SubprocessError, ValueError):
            return ""

    def run_python(self, script):
        path = None
        try:
            with tempfile.NamedTemporaryFile(
                    "w", encoding="utf-8", suffix=".py", prefix="fleet-probe-",
                    delete=False) as handle:
                path = handle.name
                handle.write(script)
            result = run_local([sys.executable, "-c", _RUN_SCRIPT, path],
                               timeout=self.timeout + 5, cwd=str(FLEET_HOME))
            return result.stdout or "{}"
        except (subprocess.TimeoutExpired, OSError,
                subprocess.SubprocessError, ValueError):
            return "{}"
        finally:
            if path is not None:
                try:
                    os.unlink(path)
                except OSError:
                    pass
