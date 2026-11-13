"""Regression coverage for process identity safety in the shell guardian."""

from pathlib import Path
import importlib.util
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = REPO_ROOT / "deploy" / "hk-web-process-control.py"
SPEC = importlib.util.spec_from_file_location("hk_web_process_control", HELPER_PATH)
CONTROL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTROL)


class GuardianProcessControlTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "linux", "requires Linux pidfd and /proc")
    def test_linux_pidfd_stops_only_sleep_child_of_expected_guardian(self):
        with tempfile.TemporaryDirectory(prefix="fleet-pidfd-") as cwd:
            child = subprocess.Popen(["/bin/sleep", "30"], cwd=cwd)
            try:
                result = CONTROL.stop_web_process(
                    child.pid,
                    cwd,
                    expected_uid=os.geteuid(),
                    process_kind="sleep",
                    expected_parent_pid=os.getpid(),
                )
                self.assertEqual(result, "stopped")
                self.assertIsNotNone(child.poll())
            finally:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=2)

    @unittest.skipUnless(sys.platform == "linux", "requires Linux pidfd and /proc")
    def test_linux_pidfd_refuses_sleep_child_with_different_parent_identity(self):
        with tempfile.TemporaryDirectory(prefix="fleet-pidfd-") as cwd:
            child = subprocess.Popen(["/bin/sleep", "30"], cwd=cwd)
            try:
                result = CONTROL.stop_web_process(
                    child.pid,
                    cwd,
                    expected_uid=os.geteuid(),
                    process_kind="sleep",
                    expected_parent_pid=os.getpid() + 1,
                )
                self.assertEqual(result, "mismatch")
                self.assertIsNone(child.poll())
            finally:
                child.terminate()
                child.wait(timeout=2)

    @unittest.skipUnless(sys.platform == "linux", "requires Linux pidfd and /proc")
    def test_linux_pidfd_escalates_to_kill_for_term_ignoring_child(self):
        with tempfile.TemporaryDirectory(prefix="fleet-pidfd-") as cwd:
            child = subprocess.Popen(
                ["/bin/sh", "-c", "trap '' TERM; exec /bin/sleep 30"], cwd=cwd
            )
            try:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    try:
                        identity = CONTROL._read_process_identity(child.pid)
                    except OSError:
                        identity = None
                    if identity and identity[2] == (b"/bin/sleep", b"30"):
                        break
                    time.sleep(0.01)
                result = CONTROL.stop_web_process(
                    child.pid,
                    cwd,
                    expected_uid=os.geteuid(),
                    process_kind="sleep",
                    expected_parent_pid=os.getpid(),
                    term_timeout=0.05,
                    kill_timeout=2,
                )
                self.assertEqual(result, "stopped")
                self.assertIsNotNone(child.poll())
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=2)

    def test_shell_guardian_does_not_signal_a_revalidated_numeric_pid(self):
        source = (REPO_ROOT / "deploy" / "hk-self-report-loop.sh").read_text()
        start = source.index("stop_web_process()")
        end = source.index("# Main loop", start)
        restart = source[start:end]

        self.assertNotRegex(
            restart,
            re.compile(r"\bkill\s+(?:-9\s+)?[\"']?\$old_pid[\"']?\b"),
        )
        self.assertIn("hk-web-process-control.py", restart)
        cleanup_start = source.index("cleanup_probe_state() {")
        cleanup_end = source.index("\n}\n\nacquire_probe_lock", cleanup_start)
        cleanup = source[cleanup_start:cleanup_end]
        self.assertNotRegex(cleanup, re.compile(r"\bkill\s+\"?\$sleep_pid"))
        self.assertIn('sleep "child:$$"', cleanup)
        self.assertNotIn('kill -0 "$new_pid"', restart)

    @mock.patch.object(CONTROL, "_read_process_identity")
    @mock.patch.object(CONTROL.os, "close")
    @mock.patch.object(CONTROL.signal, "pidfd_send_signal", create=True)
    @mock.patch.object(CONTROL.os, "pidfd_open", create=True)
    def test_pid_reuse_after_pidfd_open_never_signals_the_replacement(
        self, pidfd_open, send_signal, close_pidfd, read_identity
    ):
        pidfd_open.return_value = 73
        read_identity.return_value = (
            CONTROL.os.geteuid(),
            "/srv/agent-fleet",
            (b"python3", b"hub/web.py", b"--no-serve-frontend"),
            77,
        )
        send_signal.side_effect = ProcessLookupError

        with mock.patch.object(CONTROL.sys, "platform", "linux"):
            result = CONTROL.stop_web_process(1234, "/srv/agent-fleet")

        self.assertEqual(result, "absent")
        send_signal.assert_called_once_with(73, 0, None, 0)
        close_pidfd.assert_called_once_with(73)

    @mock.patch.object(CONTROL, "_read_process_identity")
    @mock.patch.object(CONTROL.os, "close")
    @mock.patch.object(CONTROL.select, "poll")
    @mock.patch.object(CONTROL.signal, "pidfd_send_signal", create=True)
    @mock.patch.object(CONTROL.os, "pidfd_open", create=True)
    def test_forced_stop_uses_the_same_pidfd_for_both_signals(
        self, pidfd_open, send_signal, poll, close_pidfd, read_identity
    ):
        pidfd_open.return_value = 73
        send_signal.return_value = None
        read_identity.return_value = (
            CONTROL.os.geteuid(),
            "/srv/agent-fleet",
            (b"python3", b"hub/web.py", b"--no-serve-frontend"),
            77,
        )
        poll.return_value.poll.side_effect = [[], [(73, CONTROL.select.POLLIN)]]

        with mock.patch.object(CONTROL.sys, "platform", "linux"):
            result = CONTROL.stop_web_process(1234, "/srv/agent-fleet")

        self.assertEqual(result, "stopped")
        self.assertEqual(
            [call.args for call in send_signal.call_args_list],
            [(73, 0, None, 0), (73, signal.SIGTERM, None, 0), (73, signal.SIGKILL, None, 0)],
        )
        close_pidfd.assert_called_once_with(73)

    @mock.patch.object(CONTROL.os, "pidfd_open", create=True)
    def test_pidfd_support_absence_fails_closed_without_numeric_kill(self, pidfd_open):
        with mock.patch.object(CONTROL.sys, "platform", "linux"), mock.patch.object(
            CONTROL.signal, "pidfd_send_signal", None, create=True
        ):
            with self.assertRaises(CONTROL.ProcessControlError):
                CONTROL.stop_web_process(1234, "/srv/agent-fleet")
        pidfd_open.assert_not_called()

    def test_sleep_child_identity_is_bound_to_expected_guardian_parent(self):
        identity = (
            CONTROL.os.geteuid(),
            "/srv/agent-fleet",
            (b"/usr/bin/sleep", b"30"),
            900,
        )

        self.assertTrue(
            CONTROL._matches_process(
                identity, "/srv/agent-fleet", CONTROL.os.geteuid(),
                "sleep", False, 900
            )
        )
        self.assertFalse(
            CONTROL._matches_process(
                identity, "/srv/agent-fleet", CONTROL.os.geteuid(),
                "sleep", False, 901
            )
        )


if __name__ == "__main__":
    unittest.main()
