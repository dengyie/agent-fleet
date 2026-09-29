"""Fail-closed local execution backend with explicit sandbox boundaries."""
from __future__ import annotations

import os
import signal
import subprocess
import math
from dataclasses import dataclass
from pathlib import Path

from .directory import DirectoryBackend, ALLOWED_EXECUTABLES, MAX_OUTPUT_BYTES


@dataclass(frozen=True)
class SandboxPolicy:
    cpu_seconds: int = 30
    memory_bytes: int = 512 * 1024 * 1024
    process_count: int = 32
    timeout_seconds: float = 30.0
    network_enabled: bool = False


class SandboxBackend(DirectoryBackend):
    """Execute fixed commands with resource limits and an explicit launcher.

    ``launcher`` is an administrator-provided argv prefix such as a tested
    bubblewrap profile.  A missing launcher is never silently replaced by a
    plain host process. Confined file operations reuse DirectoryBackend's
    path checks and disk quota; only process execution requires a launcher.
    """

    def __init__(self, root: Path, *, policy: SandboxPolicy | None = None,
                 launcher: list[str] | tuple[str, ...] | None = None,
                 quota_bytes: int = 1024 * 1024 * 1024):
        super().__init__(root, quota_bytes=quota_bytes)
        self.policy = policy or SandboxPolicy()
        self.launcher = tuple(launcher or ())
        if any(not isinstance(item, str) or not item or len(item) > 4096 or "\x00" in item
               for item in self.launcher):
            raise ValueError("invalid sandbox launcher")
        if (self.policy.cpu_seconds < 1 or self.policy.memory_bytes < 1
                or self.policy.process_count < 1
                or not math.isfinite(float(self.policy.timeout_seconds))
                or self.policy.timeout_seconds <= 0):
            raise ValueError("invalid sandbox policy")
        # A resource-limited host process is not a sandbox. Every execution
        # therefore requires an administrator-configured launcher.
        self._unavailable = not bool(self.launcher)

    def _preexec(self):
        os.setsid()
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_CPU, (self.policy.cpu_seconds, self.policy.cpu_seconds))
            resource.setrlimit(resource.RLIMIT_AS, (self.policy.memory_bytes, self.policy.memory_bytes))
            resource.setrlimit(resource.RLIMIT_NPROC, (self.policy.process_count, self.policy.process_count))
        except (ImportError, OSError, ValueError):
            # The launcher remains the isolation authority on platforms that
            # do not expose POSIX resource limits.
            pass

    def execute(self, command_id: str, argv: list[str], *, timeout_s: float = 30.0):
        if self._unavailable:
            return self._receipt(command_id, "failed", error_code="sandbox_unavailable")
        if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) or len(arg) > 4096 for arg in argv):
            return self._receipt(command_id, "failed", error_code="invalid_command")
        try:
            timeout_s = float(timeout_s)
        except (TypeError, ValueError):
            return self._receipt(command_id, "failed", error_code="invalid_timeout")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            return self._receipt(command_id, "failed", error_code="invalid_timeout")
        executable = Path(argv[0]).name
        if executable not in ALLOWED_EXECUTABLES or any(arg in {"-c", "--command"} for arg in argv[1:]):
            return self._receipt(command_id, "failed", error_code="command_not_allowed")
        command = [*self.launcher, *argv]
        home = self.root / ".sandbox-home"
        tmp = self.root / ".sandbox-tmp"
        home.mkdir(exist_ok=True)
        tmp.mkdir(exist_ok=True)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "TMPDIR": str(tmp)}
        try:
            proc = subprocess.Popen(command, cwd=self.root, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, preexec_fn=self._preexec)
            try:
                raw, _ = proc.communicate(timeout=max(0.1, min(timeout_s, self.policy.timeout_seconds)))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    proc.kill()
                proc.wait(timeout=2)
                return self._receipt(command_id, "failed", error_code="timeout")
            raw = raw or b""
            truncated = len(raw) > MAX_OUTPUT_BYTES
            output = raw[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
            return self._receipt(command_id, "succeeded" if proc.returncode == 0 else "failed",
                                 {"returncode": proc.returncode, "output": output},
                                 error_code=None if proc.returncode == 0 else "process_failed",
                                 truncated=truncated)
        except OSError:
            return self._receipt(command_id, "failed", error_code="sandbox_exec_failed")


__all__ = ["SandboxBackend", "SandboxPolicy"]
