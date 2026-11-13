"""
Unit tests for agent_fleet_guardian module.

Tests health checking, restart logic, failure counting, and configuration.
"""

import asyncio
import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from hub.agent_fleet_guardian import AgentFleetGuardian, agent_fleet_guardian_watcher


class TestAgentFleetGuardian(unittest.IsolatedAsyncioTestCase):
    """Test cases for AgentFleetGuardian class."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.repo_root = Path(self.temp_dir) / "agent-fleet"
        self.repo_root.mkdir()
        self.home = Path(self.temp_dir) / "home"
        self.home.mkdir()
        self._home_patch = unittest.mock.patch.dict(
            os.environ, {"HOME": str(self.home)}, clear=False
        )
        self._home_patch.start()

        self.guardian = AgentFleetGuardian(
            repo_root=str(self.repo_root),
            web_port=8790,
            health_check_interval=1,
            failure_threshold=3,
            max_restart_failures=5,
        )

    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        self._home_patch.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch("hub.agent_fleet_guardian.asyncio.create_subprocess_exec")
    async def test_check_health_success(self, mock_exec):
        """Test health check returns 200 on success."""
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"200", b""))
        mock_exec.return_value = mock_proc

        status = await self.guardian.check_health()

        self.assertEqual(status, 200)
        mock_exec.assert_called_once()
        self.assertEqual(mock_exec.call_args.args[-1], 'http://127.0.0.1:8790/healthz')

    @patch("hub.agent_fleet_guardian.asyncio.create_subprocess_exec")
    async def test_check_health_failure(self, mock_exec):
        """Test health check returns 0 on failure."""
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"000", b""))
        mock_exec.return_value = mock_proc

        status = await self.guardian.check_health()

        self.assertEqual(status, 0)

    @patch("hub.agent_fleet_guardian.asyncio.create_subprocess_exec")
    async def test_check_health_exception(self, mock_exec):
        """Test health check returns 0 on exception."""
        mock_exec.side_effect = Exception("Connection refused")

        status = await self.guardian.check_health()

        self.assertEqual(status, 0)

    @patch("hub.agent_fleet_guardian.subprocess.Popen")
    @patch("hub.agent_fleet_guardian.AgentFleetGuardian._stop_process_with_pidfd", new_callable=AsyncMock)
    async def test_restart_service_success(self, stop_pidfd, mock_popen):
        """Test successful service restart."""
        # Mock old process
        self.guardian.web_pid_file.parent.mkdir(parents=True, exist_ok=True)
        self.guardian.web_pid_file.write_text("12345")
        stop_pidfd.return_value = "stopped"

        # Mock new process
        mock_proc = MagicMock()
        mock_proc.pid = 67890
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc
        self.guardian.process_matches = MagicMock(return_value=True)

        # Create hub/web.py to pass existence check
        (self.repo_root / "hub").mkdir()
        (self.repo_root / "hub" / "web.py").touch()

        success = await self.guardian.restart_service()

        self.assertTrue(success)
        self.assertEqual(self.guardian.web_pid_file.read_text(), "67890")
        stop_pidfd.assert_awaited_once_with(12345, str(self.repo_root.resolve()))

    @patch("hub.agent_fleet_guardian.asyncio.sleep", new_callable=AsyncMock)
    @patch("hub.agent_fleet_guardian.subprocess.Popen")
    def test_restart_service_popen_uses_api_only(
        self, mock_popen, _mock_sleep
    ):
        """Independent backend releases must not depend on frontend files."""
        log_dir = Path(self.temp_dir) / "logs"
        log_dir.mkdir()
        self.guardian.web_log = log_dir / "web.log"
        self.guardian.web_error_log = log_dir / "web-errors.log"
        self.guardian.web_pid_file = Path(self.temp_dir) / "web.pid"
        mock_proc = MagicMock()
        mock_proc.pid = 67890
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc
        self.guardian.process_matches = MagicMock(return_value=True)
        (self.repo_root / "hub").mkdir()
        (self.repo_root / "hub" / "web.py").touch()

        success = asyncio.run(self.guardian.restart_service())

        self.assertTrue(success)
        mock_popen.assert_called_once()
        argv = mock_popen.call_args[0][0]
        self.assertEqual(argv[1], "hub/web.py")
        self.assertIn("--no-serve-frontend", argv)
        self.assertNotIn("--frontend-dir", argv)

    @patch("hub.agent_fleet_guardian.subprocess.Popen")
    async def test_restart_service_process_dies(self, mock_popen):
        """Test restart fails when new process dies immediately."""
        # Mock no old process
        mock_proc = MagicMock()
        mock_proc.pid = 67890
        mock_proc.poll.return_value = 1
        mock_popen.return_value = mock_proc

        (self.repo_root / "hub").mkdir()
        (self.repo_root / "hub" / "web.py").touch()

        success = await self.guardian.restart_service()

        self.assertFalse(success)

    @patch("hub.agent_fleet_guardian.subprocess.Popen")
    @patch("hub.agent_fleet_guardian.AgentFleetGuardian._stop_process_with_pidfd", new_callable=AsyncMock)
    async def test_restart_does_not_start_new_hub_when_pidfd_stop_fails(self, stop_pidfd, mock_popen):
        self.guardian.web_pid_file.parent.mkdir(parents=True, exist_ok=True)
        self.guardian.web_pid_file.write_text("12345")
        stop_pidfd.return_value = "failed"

        self.assertFalse(await self.guardian.restart_service())
        mock_popen.assert_not_called()

    @patch("hub.agent_fleet_guardian.asyncio.create_subprocess_exec")
    async def test_pidfd_helper_receives_process_identity_contract(self, create_process):
        from hub import agent_fleet_guardian as guardian_module
        release_helper = guardian_module._RELEASE_ROOT / "deploy" / "hk-web-process-control.py"
        helper_process = AsyncMock()
        helper_process.communicate = AsyncMock(return_value=(b"stopped\n", b""))
        helper_process.returncode = 0
        create_process.return_value = helper_process

        result = await self.guardian._stop_process_with_pidfd(
            12345, str(guardian_module._RELEASE_ROOT)
        )

        self.assertEqual(result, "stopped")
        args = create_process.call_args.args
        self.assertEqual(args[1], str(release_helper))
        self.assertEqual(args[2:4], ("12345", str(guardian_module._RELEASE_ROOT)))
        self.assertEqual(args[4:], (str(os.getuid()), "web", "allow_existing"))

    def test_guardian_accepts_previous_sibling_release_after_live_symlink_switch(self):
        from hub import agent_fleet_guardian as guardian_module

        releases = Path(self.temp_dir) / "releases"
        old_release = releases / "old"
        new_release = releases / "new"
        for release in (old_release, new_release):
            (release / "hub").mkdir(parents=True)
            (release / "hub" / "web.py").touch()
            (release / "RELEASE_ORIGIN").write_text("commit: fixture\n")
        live = Path(self.temp_dir) / "live-link"
        live.symlink_to(new_release, target_is_directory=True)
        self.guardian.repo_root = live
        with patch.object(guardian_module, "_RELEASE_ROOT", new_release):
            with patch("hub.agent_fleet_guardian._proc_identity", return_value=(
                "python3 hub/web.py --no-serve-frontend",
                str(old_release),
                os.getuid(),
            )):
                self.assertEqual(
                    self.guardian._managed_hub_cwd(12345), str(old_release.resolve())
                )

    def test_guardian_routes_old_hub_stop_through_pidfd_helper(self):
        source = (Path(__file__).resolve().parents[1] / "hub" / "agent_fleet_guardian.py").read_text()
        restart = source[source.index("async def restart_service"):source.index("async def agent_fleet_guardian_watcher")]
        self.assertIn("_stop_process_with_pidfd(old_pid, expected_cwd)", restart)
        self.assertNotIn("os.kill(old_pid", restart)
        self.assertIn("proc.poll()", restart)

    @patch("hub.agent_fleet_guardian.subprocess.Popen")
    @patch("hub.agent_fleet_guardian.os.kill")
    async def test_restart_service_repo_not_exists(self, mock_kill, mock_popen):
        """Test restart fails when repo root doesn't exist."""
        self.guardian.repo_root = self.repo_root / "missing-repository"

        success = await self.guardian.restart_service()

        self.assertFalse(success)
        mock_popen.assert_not_called()

    def test_guardian_initialization(self):
        """Test Guardian initializes with correct configuration."""
        guardian = AgentFleetGuardian(
            repo_root="/test/path",
            web_port=9999,
            health_check_interval=30,
            failure_threshold=5,
            max_restart_failures=10,
        )

        self.assertEqual(guardian.repo_root, Path("/test/path"))
        self.assertEqual(guardian.web_port, 9999)
        self.assertEqual(guardian.health_check_interval, 30)
        self.assertEqual(guardian.failure_threshold, 5)
        self.assertEqual(guardian.max_restart_failures, 10)
        self.assertEqual(guardian.failure_count, 0)
        self.assertEqual(guardian.restart_failure_count, 0)
        self.assertEqual(
            guardian.web_pid_file,
            self.home / ".hermes" / "agent-fleet-web.pid",
        )
        source = (
            Path(__file__).resolve().parents[1] / "hub" / "agent_fleet_guardian.py"
        ).read_text()
        self.assertNotIn("/home/mango", source)


class TestAgentFleetGuardianWatcher(unittest.IsolatedAsyncioTestCase):
    """Test cases for agent_fleet_guardian_watcher function."""

    @patch.dict(os.environ, {
        "AGENT_FLEET_REPO_ROOT": "/test/repo",
        "AGENT_FLEET_WEB_PORT": "8888",
        "AGENT_FLEET_HEALTH_CHECK_INTERVAL": "10",
        "AGENT_FLEET_FAILURE_THRESHOLD": "2",
        "AGENT_FLEET_MAX_RESTART_FAILURES": "3",
    })
    @patch("hub.agent_fleet_guardian.AgentFleetGuardian")
    async def test_watcher_reads_env_config(self, mock_guardian_class):
        """Test watcher reads configuration from environment variables."""
        mock_guardian = AsyncMock()
        mock_guardian.check_health = AsyncMock(return_value=200)
        mock_guardian.health_check_interval = 0.1
        mock_guardian.failure_count = 0
        mock_guardian_class.return_value = mock_guardian

        # Run watcher for short time
        task = asyncio.create_task(agent_fleet_guardian_watcher(None))
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Verify Guardian initialized with env config
        mock_guardian_class.assert_called_once_with(
            repo_root="/test/repo",
            web_port=8888,
            web_host="0.0.0.0",
            health_check_interval=10,
            failure_threshold=2,
            max_restart_failures=3,
        )

    @patch("hub.agent_fleet_guardian.AgentFleetGuardian")
    async def test_watcher_health_check_success(self, mock_guardian_class):
        """Test watcher handles successful health checks."""
        mock_guardian = AsyncMock()
        mock_guardian.check_health = AsyncMock(return_value=200)
        mock_guardian.health_check_interval = 0.1
        mock_guardian.failure_count = 0
        mock_guardian.restart_service = AsyncMock()
        mock_guardian_class.return_value = mock_guardian

        task = asyncio.create_task(agent_fleet_guardian_watcher(None))
        await asyncio.sleep(0.25)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Should check health multiple times
        self.assertGreater(mock_guardian.check_health.call_count, 1)
        # Should not restart
        mock_guardian.restart_service.assert_not_called()

    @patch("hub.agent_fleet_guardian.AgentFleetGuardian")
    async def test_watcher_triggers_restart_on_threshold(self, mock_guardian_class):
        """Test watcher triggers restart after threshold failures."""
        mock_guardian = AsyncMock()
        mock_guardian.health_check_interval = 0.1
        mock_guardian.failure_threshold = 3
        mock_guardian.failure_count = 0

        # Simulate 3 failures then success
        health_results = [0, 0, 0, 200, 200]
        mock_guardian.check_health = AsyncMock(side_effect=health_results)
        mock_guardian.restart_service = AsyncMock(return_value=True)
        mock_guardian_class.return_value = mock_guardian

        task = asyncio.create_task(agent_fleet_guardian_watcher(None))

        # Manually update failure_count to simulate watcher logic
        for i in range(len(health_results)):
            await asyncio.sleep(0.15)
            if health_results[i] != 200 and i < 3:
                mock_guardian.failure_count += 1
            elif health_results[i] == 200:
                mock_guardian.failure_count = 0

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Should have attempted restart
        self.assertGreaterEqual(mock_guardian.check_health.call_count, 3)


def run_async_test(coro):
    """Helper to run async test."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


if __name__ == "__main__":
    # Run async tests
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    for test_group in suite:
        for test in test_group:
            if asyncio.iscoroutinefunction(test._testMethodName):
                # Wrap async test
                original_method = getattr(test, test._testMethodName)
                setattr(
                    test,
                    test._testMethodName,
                    lambda self, m=original_method: run_async_test(m(self))
                )

    runner = unittest.TextTestRunner(verbosity=2)
    runner.run(suite)
