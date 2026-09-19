"""Local execution context used by agent-side collectors.

The hub never executes commands on another machine. Every probe runs on the
machine where this context is created and only returns bounded stdout.
"""

import os
import subprocess
from pathlib import Path

FLEET_HOME = Path(__file__).resolve().parent.parent

# pythonw（无控制台）spawn 控制台子进程（bash/python3/ps）时会新建一个
# 可见 conhost 窗口——每轮 self-report 闪窗的根因。CREATE_NO_WINDOW 让
# 子进程仍有 console 可继承但窗口不显示；POSIX 下为 0（无此语义）。
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


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
                creationflags=_NO_WINDOW,
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
                creationflags=_NO_WINDOW,
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
