"""Local execution context used by agent-side collectors.

The hub never executes commands on another machine. Every probe runs on the
machine where this context is created and only returns bounded stdout.
"""

import subprocess
from pathlib import Path

FLEET_HOME = Path(__file__).resolve().parent.parent


class ProbeContext:
    """Bounded local command execution for one probe cycle."""

    def __init__(self, machine, timeout=10):
        self.machine = machine
        self.local = True
        self.timeout = timeout
        self.inner_reachable = True

    def run(self, cmd):
        try:
            result = subprocess.run(
                ["bash", "-c", cmd],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=str(FLEET_HOME),
            )
            return result.stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            return ""

    def run_python(self, script):
        try:
            result = subprocess.run(
                ["python3", "-"],
                input=script,
                capture_output=True,
                text=True,
                timeout=self.timeout + 5,
                cwd=str(FLEET_HOME),
            )
            return result.stdout
        except (subprocess.TimeoutExpired, OSError):
            return "{}"

    def run_batch(self, cmds):
        script = "; ".join(f"echo __CMD__={i}; {cmd}" for i, cmd in enumerate(cmds))
        return self.run(script)

    def kill_gateway(self):
        return {"ok": False, "message": "push-only 模式不提供远程进程控制"}

    def restart_process(self, pattern):
        return {"ok": False, "message": "push-only 模式不提供远程进程控制"}

    def tmux_send(self, session, cmd):
        return {"ok": False, "message": "push-only 模式不提供远程进程控制"}
