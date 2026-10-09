"""
Agent Fleet Guardian - Health monitoring and auto-recovery for agent-fleet web service.

This module integrates with Hermes Gateway's background task supervision system
to monitor the agent-fleet web service and automatically restart it on failure.

Usage:
    Enable via environment variable:
        AGENT_FLEET_GUARDIAN_ENABLED=true

    Configuration:
        AGENT_FLEET_REPO_ROOT=$HOME/agent-fleet
        AGENT_FLEET_WEB_PORT=8790
        AGENT_FLEET_WEB_HOST=0.0.0.0
        AGENT_FLEET_HEALTH_CHECK_INTERVAL=20
        AGENT_FLEET_FAILURE_THRESHOLD=3
        AGENT_FLEET_MAX_RESTART_FAILURES=5
"""

import asyncio
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

# Use gateway.run logger for visibility in gateway logs
logger = logging.getLogger("gateway.run")
_RELEASE_ROOT = Path(__file__).resolve().parents[1]


def _proc_identity(pid: int) -> tuple[str, str, int] | None:
    """Read bounded identity fields before touching a PID-file process."""
    proc = Path("/proc") / str(pid)
    try:
        cmdline = " ".join(
            part.decode("utf-8", "replace")
            for part in (proc / "cmdline").read_bytes().split(b"\0")
            if part
        )
        cwd = os.path.realpath(proc / "cwd")
        uid_line = next(
            line for line in (proc / "status").read_text().splitlines()
            if line.startswith("Uid:")
        )
        uid = int(uid_line.split()[1])
    except (OSError, StopIteration, ValueError, UnicodeError):
        return None
    return cmdline, cwd, uid


class AgentFleetGuardian:
    """Health monitoring and auto-recovery for agent-fleet web service."""

    def __init__(
        self,
        repo_root: str,
        web_port: int = 8790,
        web_host: str = "0.0.0.0",
        health_check_interval: int = 20,
        failure_threshold: int = 3,
        max_restart_failures: int = 5,
    ):
        self.repo_root = Path(repo_root)
        self.web_port = web_port
        self.web_host = web_host
        self.health_check_interval = health_check_interval
        self.failure_threshold = failure_threshold
        self.max_restart_failures = max_restart_failures

        home = Path(os.environ.get("HOME") or Path.home())
        hermes = home / ".hermes"
        self.web_pid_file = Path(
            os.environ.get("AGENT_FLEET_WEB_PID_FILE", str(hermes / "agent-fleet-web.pid"))
        )
        self.web_log = Path(
            os.environ.get("AGENT_FLEET_WEB_LOG", str(hermes / "logs" / "agent-fleet-web.log"))
        )
        self.web_error_log = Path(
            os.environ.get(
                "AGENT_FLEET_WEB_ERROR_LOG",
                str(hermes / "logs" / "agent-fleet-web-errors.log"),
            )
        )

        self.failure_count = 0
        self.restart_failure_count = 0

        logger.info(
            f"Guardian initialized: repo_root={repo_root}, port={web_port}, "
            f"health_interval={health_check_interval}s, "
            f"failure_threshold={failure_threshold}, "
            f"max_restart_failures={max_restart_failures}"
        )

    def process_matches(self, pid: int, *, require_api_only: bool = True) -> bool:
        """Ensure a PID identifies this user's bounded web identity."""
        identity = _proc_identity(pid)
        if identity is None:
            return False
        cmdline, cwd, uid = identity
        try:
            expected_cwd = str(self.repo_root.resolve())
            current_uid = os.getuid()
        except (OSError, RuntimeError):
            return False
        return (
            uid == current_uid
            and cwd == expected_cwd
            and "hub/web.py" in cmdline
            and (not require_api_only or "--no-serve-frontend" in cmdline)
        )

    def _managed_hub_cwd(self, pid: int) -> str | None:
        identity = _proc_identity(pid)
        if identity is None:
            return None
        cmdline, cwd, uid = identity
        if uid != os.getuid() or "hub/web.py" not in cmdline:
            return None
        try:
            actual = Path(cwd).resolve(strict=True)
            configured = self.repo_root.resolve(strict=True)
            module_root = _RELEASE_ROOT.resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        if actual in {configured, module_root}:
            return str(actual)
        # Symlink-based releases keep older Hub cwd values in sibling release
        # directories while LIVE already resolves to the candidate release.
        if actual.parent == module_root.parent and (actual / "hub" / "web.py").is_file():
            return str(actual)
        return None

    async def _stop_process_with_pidfd(self, pid: int, expected_cwd: str) -> str:
        helper = _RELEASE_ROOT / "deploy" / "hk-web-process-control.py"
        if not helper.is_file():
            logger.error("Process-control helper is missing: %s", helper)
            return "failed"
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(helper),
                str(pid),
                expected_cwd,
                str(os.getuid()),
                "web",
                "allow_existing",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            communication = asyncio.create_task(process.communicate())
            try:
                stdout, stderr = await asyncio.shield(communication)
            except asyncio.CancelledError:
                await asyncio.shield(communication)
                raise
        except (OSError, RuntimeError) as error:
            logger.error("Unable to run pidfd process control: %s", error)
            return "failed"

        result = stdout.decode("utf-8", "replace").strip()
        if process.returncode == 0 and result in {"stopped", "absent"}:
            return result
        if process.returncode == 3 and result == "mismatch":
            return result
        logger.error(
            "Pidfd process control failed for PID %s (exit=%s): %s",
            pid,
            process.returncode,
            stderr.decode("utf-8", "replace").strip(),
        )
        return "failed"

    async def check_health(self) -> int:
        """
        Check agent-fleet web service health.

        Returns:
            HTTP status code (200 = healthy, other = unhealthy)
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "curl",
                "-s",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "--max-time",
                "5",
                f"http://127.0.0.1:{self.web_port}/healthz",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            status = stdout.decode().strip()
            return int(status) if status.isdigit() else 0
        except Exception as e:
            logger.warning(f"Health check exception: {e}")
            return 0

    async def restart_service(self) -> bool:
        """
        Restart agent-fleet web service.

        Returns:
            True if restart successful, False otherwise
        """
        logger.warning("Restarting agent-fleet web service")

        try:
            # Stop the old process through a pidfd so PID reuse cannot redirect
            # TERM or KILL after the identity check.
            if self.web_pid_file.exists():
                try:
                    old_pid = int(self.web_pid_file.read_text().strip())
                    if old_pid <= 0:
                        raise ValueError("PID must be positive")
                except (ValueError, OSError) as error:
                    logger.warning("Ignoring invalid or unreadable web PID: %s", error)
                else:
                    expected_cwd = self._managed_hub_cwd(old_pid)
                    if expected_cwd is None:
                        identity = _proc_identity(old_pid)
                        if identity is not None:
                            logger.warning("Ignoring stale or unmanaged web PID %s", old_pid)
                            stop_result = "mismatch"
                        else:
                            expected_cwd = str(self.repo_root.resolve())
                            stop_result = await self._stop_process_with_pidfd(old_pid, expected_cwd)
                    else:
                        stop_result = await self._stop_process_with_pidfd(old_pid, expected_cwd)
                    if stop_result == "failed":
                        return False
                    if stop_result == "mismatch":
                        logger.warning("Ignoring stale or mismatched web PID %s", old_pid)
                    else:
                        logger.info("Old web process %s: %s", old_pid, stop_result)

            # Step 2: Start new process
            if not self.repo_root.exists():
                logger.error(f"Repo root does not exist: {self.repo_root}")
                return False

            # Ensure log directory exists
            self.web_log.parent.mkdir(parents=True, exist_ok=True)

            # Start new web service
            # Use venv python if available, fallback to system python3
            venv_python = self.repo_root / ".venv" / "bin" / "python3"
            python_cmd = str(venv_python) if venv_python.exists() else "python3"

            with open(self.web_log, "a") as log_out, open(self.web_error_log, "a") as log_err:
                proc = subprocess.Popen(
                    [
                        python_cmd,
                        "hub/web.py",
                        "--host",
                        self.web_host,
                        "--port",
                        str(self.web_port),
                        "--no-serve-frontend",
                    ],
                    cwd=str(self.repo_root),
                    stdout=log_out,
                    stderr=log_err,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
                new_pid = proc.pid

            # Write PID file
            self.web_pid_file.write_text(str(new_pid))
            logger.info(f"Web service started: PID {new_pid}")

            # Step 3: Verify started
            await asyncio.sleep(2)
            try:
                if proc.poll() is not None or not self.process_matches(new_pid):
                    logger.error("New web process identity check failed: PID %s", new_pid)
                    self.web_pid_file.unlink(missing_ok=True)
                    return False
                logger.info(f"Web service verified running: PID {new_pid}")
                return True
            except ProcessLookupError:
                logger.error(f"Web process {new_pid} died immediately after start")
                return False

        except Exception as e:
            logger.error(f"Restart failed with exception: {e}", exc_info=True)
            return False


async def agent_fleet_guardian_watcher(gateway_runner) -> None:
    """
    Background watcher for agent-fleet health monitoring.

    This function runs as a supervised background task in Hermes Gateway,
    monitoring the agent-fleet web service and automatically restarting it
    on health check failures.

    Args:
        gateway_runner: The GatewayRunner instance (unused but required by supervisor)
    """
    # Load configuration from environment
    home = Path(os.environ.get("HOME") or Path.home())
    repo_root = os.environ.get("AGENT_FLEET_REPO_ROOT", str(home / "agent-fleet"))
    web_port = int(os.environ.get("AGENT_FLEET_WEB_PORT", "8790"))
    web_host = os.environ.get("AGENT_FLEET_WEB_HOST", "0.0.0.0")
    health_check_interval = int(os.environ.get("AGENT_FLEET_HEALTH_CHECK_INTERVAL", "20"))
    failure_threshold = int(os.environ.get("AGENT_FLEET_FAILURE_THRESHOLD", "3"))
    max_restart_failures = int(os.environ.get("AGENT_FLEET_MAX_RESTART_FAILURES", "5"))

    guardian = AgentFleetGuardian(
        repo_root=repo_root,
        web_port=web_port,
        web_host=web_host,
        health_check_interval=health_check_interval,
        failure_threshold=failure_threshold,
        max_restart_failures=max_restart_failures,
    )

    logger.info(f"Agent-fleet Guardian watcher started (PID {os.getpid()})")

    while True:
        # Health check
        status = await guardian.check_health()

        if status == 200:
            if guardian.failure_count > 0:
                logger.info("Service recovered, health check passed")
            guardian.failure_count = 0
        else:
            guardian.failure_count += 1
            logger.warning(
                f"Health check failed: status={status} "
                f"(failure_count={guardian.failure_count}/{guardian.failure_threshold})"
            )

            if guardian.failure_count >= guardian.failure_threshold:
                logger.error(
                    f"Health check failed {guardian.failure_count} times, triggering restart"
                )

                if await guardian.restart_service():
                    logger.info("Restart successful")
                    guardian.failure_count = 0
                    guardian.restart_failure_count = 0
                    await asyncio.sleep(10)  # Grace period after successful restart
                else:
                    guardian.restart_failure_count += 1
                    logger.error(
                        f"Restart failed "
                        f"(restart_failure_count={guardian.restart_failure_count}/"
                        f"{guardian.max_restart_failures})"
                    )

                    if guardian.restart_failure_count >= guardian.max_restart_failures:
                        logger.error(
                            f"FATAL: Exceeded max restart failures ({guardian.max_restart_failures}), "
                            f"stopping Guardian watcher"
                        )
                        raise RuntimeError(
                            f"Guardian exceeded max restart failures: {guardian.max_restart_failures}"
                        )

                    # Reset health check failure count to retry after next interval
                    guardian.failure_count = 0
                    await asyncio.sleep(30)  # Longer wait after restart failure

        # Sleep until next check
        await asyncio.sleep(guardian.health_check_interval)
