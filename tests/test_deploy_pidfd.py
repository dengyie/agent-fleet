"""Deployment process stops must not signal a reusable numeric PID."""

from pathlib import Path
import re
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]


class DeploymentPidfdSourceTests(unittest.TestCase):
    def test_auto_deployer_does_not_signal_a_pid_after_snapshot_comparison(self):
        source = (REPO_ROOT / "deploy" / "auto_deploy_container.py").read_text()
        start = source.index("def stop(root):")
        end = source.index("\n\ndef probe", start)
        stop = source[start:end]

        self.assertNotRegex(stop, re.compile(r"\bos\.kill\(pid,"))
        self.assertIn("_stop_pidfd(pidfd)", stop)
        self.assertIn("_send_pidfd_signal(pidfd, signum)", source)
        guardian = (REPO_ROOT / "hub" / "agent_fleet_guardian.py").read_text()
        self.assertNotIn("os.kill(old_pid", guardian)
        self.assertIn("_stop_process_with_pidfd(old_pid, expected_cwd)", guardian)
        self.assertIn("proc.poll()", guardian)

    def test_container_installer_does_not_signal_reused_hub_or_probe_pids(self):
        source = (REPO_ROOT / "deploy" / "hk-container-install.sh").read_text()
        probe_start = source.index("stop_probe_loop()")
        probe_end = source.index("\nstart_probe_loop()", probe_start)
        probe_stop = source[probe_start:probe_end]
        hub_start = source.index("stop_probe_loop \"$probe_cwd\"")
        hub_end = source.index('\nstart_hub "$live"', hub_start)
        hub_stop = source[hub_start:hub_end]

        self.assertNotRegex(probe_stop, re.compile(r'\bkill\s+(?:-TERM|-KILL)?\s*"\$pid"'))
        self.assertNotRegex(hub_stop, re.compile(r'\bkill\s+(?:-TERM|-KILL)?\s*"\$old_pid"'))
        self.assertIn("hk-web-process-control.py", probe_stop)
        self.assertIn("hk-web-process-control.py", hub_stop)
        self.assertIn('"$new_pid" "$live" "$rollback_uid" web allow_existing', source)
        self.assertIn('expected_uid file_pid=""', probe_stop)
        rollback = source[source.index("rollback() {"):source.index("trap rollback ERR")]
        self.assertNotRegex(rollback, re.compile(r'\bkill\s+-?(?:TERM|KILL|0)\b'))
        self.assertIn('hub_pid_for_cwd "$live" allow_existing', rollback)
        self.assertIn('probe_process_matches "$pid" "$expected_cwd"', probe_stop)

    def test_probe_stop_leaves_pidfile_cleanup_to_next_start(self):
        source = (REPO_ROOT / "deploy" / "hk-container-install.sh").read_text()
        start = source.index("stop_probe_loop()")
        end = source.index("\nstart_probe_loop()", start)
        probe_stop = source[start:end]
        start_stop = source.index("start_probe_loop()")
        start_end = source.index("\ncleanup_token_source()", start_stop)
        probe_start = source[start_stop:start_end]

        self.assertNotIn('rm -f "$probe_pid_file"', probe_stop)
        self.assertEqual(probe_start.count('rm -f "$probe_pid_file"'), 1)
        self.assertLess(
            probe_start.index('rm -f "$probe_pid_file"'),
            probe_start.index('su -s /bin/bash -c'),
        )

    def test_installer_uses_target_release_helper_after_cutover(self):
        source = (REPO_ROOT / "deploy" / "hk-container-install.sh").read_text()
        self.assertIn('python3 \"$release/deploy/hk-web-process-control.py\"', source)
        self.assertIn('old_uid=$(id -u \"$FLEET_USER\")', source)


class AutoDeployPidfdBehaviorTests(unittest.TestCase):
    @mock.patch("deploy.auto_deploy_container.process_identity")
    @mock.patch("deploy.auto_deploy_container._send_pidfd_signal", return_value=False)
    @mock.patch("deploy.auto_deploy_container.os.close")
    @mock.patch("deploy.auto_deploy_container.os.pidfd_open", create=True, return_value=61)
    @mock.patch("deploy.auto_deploy_container.signal.pidfd_send_signal", create=True)
    def test_reused_numeric_pid_is_discarded_when_bound_pidfd_is_already_dead(
        self, pidfd_sender, pidfd_open, close_pidfd, send_signal, identity
    ):
        from pathlib import Path
        from deploy import auto_deploy_container as control

        identity.return_value = (1234, "hub", "start")
        with mock.patch.object(control.sys, "platform", "linux"):
            captured = control._capture_pidfd(Path("/srv/agent-fleet"), 1234)

        self.assertIsNone(captured)
        pidfd_open.assert_called_once_with(1234, 0)
        send_signal.assert_called_once_with(61, 0)
        close_pidfd.assert_called_once_with(61)

    @mock.patch("deploy.auto_deploy_container.os.pidfd_open", create=True)
    def test_pidfd_unavailable_fails_closed_before_process_discovery(self, pidfd_open):
        from pathlib import Path
        from deploy import auto_deploy_container as control

        with mock.patch.object(control.sys, "platform", "darwin"):
            with self.assertRaisesRegex(RuntimeError, "pidfd_unavailable"):
                control._capture_pidfd(Path("/srv/agent-fleet"), 1234)
        pidfd_open.assert_not_called()

    @mock.patch("deploy.auto_deploy_container._wait_pidfd", side_effect=[False, True])
    @mock.patch("deploy.auto_deploy_container._send_pidfd_signal")
    def test_stop_escalates_on_same_descriptor_then_waits_for_exit(self, send_signal, wait):
        from deploy import auto_deploy_container as control

        control._stop_pidfd(61, term_timeout=0.01, kill_timeout=0.01)

        self.assertEqual(
            [call.args for call in send_signal.call_args_list],
            [(61, control.signal.SIGTERM), (61, control.signal.SIGKILL)],
        )
        self.assertEqual([call.args for call in wait.call_args_list], [(61, 0.01), (61, 0.01)])

    @mock.patch("deploy.auto_deploy_container._wait_pidfd", return_value=True)
    @mock.patch("deploy.auto_deploy_container._send_pidfd_signal")
    def test_stop_does_not_escalate_after_term_exit(self, send_signal, wait):
        from deploy import auto_deploy_container as control

        control._stop_pidfd(61, term_timeout=0.01, kill_timeout=0.01)

        send_signal.assert_called_once_with(61, control.signal.SIGTERM)
        wait.assert_called_once_with(61, 0.01)


if __name__ == "__main__":
    unittest.main()
