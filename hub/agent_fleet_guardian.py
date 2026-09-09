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
import signal
import subprocess
import time
from pathlib import Path

# Use gateway.run logger for visibility in gateway logs
logger = logging.getLogger("gateway.run")


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
                f"http://127.0.0.1:{self.web_port}/",
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
            # Step 1: Kill old process
            if self.web_pid_file.exists():
                try:
                    old_pid = int(self.web_pid_file.read_text().strip())
                    try:
                        os.kill(old_pid, 0)  # Check if process exists
                        logger.info(f"Killing old web process {old_pid}")
                        os.kill(old_pid, signal.SIGTERM)
                        await asyncio.sleep(2)

                        # Force kill if still alive
                        try:
                            os.kill(old_pid, 0)
                            logger.warning(f"Process {old_pid} still alive, sending SIGKILL")
                            os.kill(old_pid, signal.SIGKILL)
                            await asyncio.sleep(1)
                        except ProcessLookupError:
                            pass  # Process already dead

                        # Verify killed
                        try:
                            os.kill(old_pid, 0)
                            logger.error(f"Failed to kill process {old_pid}")
                            return False
                        except ProcessLookupError:
                            pass  # Success

                    except ProcessLookupError:
                        logger.info(f"Old process {old_pid} already dead")
                except (ValueError, OSError) as e:
                    logger.warning(f"Failed to read/kill old PID: {e}")

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
                        "--frontend-cutover",
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
                os.kill(new_pid, 0)  # Check if process still alive
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
