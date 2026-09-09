"""Direct-execution smoke tests for cron/CLI entrypoints.

``hub/scan.py`` (scan cron) and ``tools/agent-self-report.py`` (probe loop)
are run as plain scripts in production, so ``__package__`` is empty and their
direct-run bootstrap shims (release-root sys.path anchor / file-location
module registration) execute. Loading them as modules in-process (as
``tests/test_regressions.py`` does) never exercises those branches — a
regression there passed the whole suite while production cron crashed
(2026-09-01: ``ModuleNotFoundError: No module named 'hub'``). These tests run
the real interpreter against the real paths.
"""

import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class DirectRunSmokeTests(unittest.TestCase):
    def test_scan_py_direct_run_reaches_argparse(self):
        # Direct run (like the cron does) must get past the bootstrap shim to
        # argparse: ``--help`` exits 0 with usage on stdout, not a traceback.
        proc = subprocess.run(
            [sys.executable, "hub/scan.py", "--help"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0,
                         f"stderr:\n{proc.stderr}")
        self.assertIn("usage:", proc.stdout.lower())
        self.assertNotIn("ModuleNotFoundError", proc.stderr)

    def test_agent_self_report_direct_run_reaches_argparse(self):
        # Same for the probe entrypoint; its bootstrap registers
        # report_schema/agent_profiles/tools/connectors before importing
        # tools.probe_collectors. Import-time failure would surface here.
        proc = subprocess.run(
            [sys.executable, "tools/agent-self-report.py", "--help"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0,
                         f"stderr:\n{proc.stderr}")
        self.assertIn("usage:", proc.stdout.lower())
        self.assertNotIn("ModuleNotFoundError", proc.stderr)

    def test_agent_runner_direct_run_reaches_argparse(self):
        # Same for the repo runner entrypoint. Its bootstrap registers the
        # repo-root ``agent_profiles`` module plus the ``tools`` namespace
        # before ``tools.runner_config`` (which imports yaml + agent_profiles)
        # loads. argparse processes --help before load_config, so this proves
        # the import chain, not the config. The deployment interpreter is
        # homebrew py3.14 (system /usr/bin/python3 3.9 lacks pyyaml — the
        # reason the plist must point at /opt/homebrew/bin/python3), so run
        # both: the test interpreter and, where present, the homebrew one.
        interpreters = [sys.executable]
        if Path("/opt/homebrew/bin/python3").exists():
            interpreters.append("/opt/homebrew/bin/python3")
        for py in dict.fromkeys(interpreters):
            proc = subprocess.run(
                [py, "tools/agent-runner.py", "--help"],
                cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(proc.returncode, 0,
                             f"stderr:\n{proc.stderr}")
            self.assertIn("usage:", proc.stdout.lower())
            self.assertNotIn("ModuleNotFoundError", proc.stderr)

    def test_control_client_cli_direct_run_reaches_argparse(self):
        # ControlClient has no __main__; the CLI wrapper is the LaunchAgent
        # entrypoint. Bootstrap must put the repo root on sys.path so
        # hub.domain.control / tools.runner_config import before argparse
        # handles --help.
        interpreters = [sys.executable]
        if Path("/opt/homebrew/bin/python3").exists():
            interpreters.append("/opt/homebrew/bin/python3")
        for py in dict.fromkeys(interpreters):
            proc = subprocess.run(
                [py, "tools/supervisor/control_client_cli.py", "--help"],
                cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(proc.returncode, 0,
                             f"stderr:\n{proc.stderr}")
            self.assertIn("usage:", proc.stdout.lower())
            self.assertNotIn("ModuleNotFoundError", proc.stderr)


if __name__ == "__main__":
    unittest.main()
