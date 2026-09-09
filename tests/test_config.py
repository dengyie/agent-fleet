from pathlib import Path
import sys
import tempfile
import unittest

from hub.config import FleetConfig


class FleetConfigTests(unittest.TestCase):
    def test_paths_are_derived_once_from_root(self):
        root = Path(tempfile.mkdtemp())
        cfg = FleetConfig.from_root(root)
        self.assertEqual(cfg.root, root)
        self.assertEqual(cfg.state_dir, root / "state")
        self.assertEqual(cfg.hosts_file, root / "hosts.yaml")
        self.assertEqual(cfg.event_log, root / "state" / "events.jsonl")
        self.assertEqual(cfg.task_db, root / "state" / "fleet.db")

    def test_config_accepts_explicit_test_paths(self):
        root = Path(tempfile.mkdtemp())
        cfg = FleetConfig.from_root(root, state_dir=root / "tmp-state")
        self.assertEqual(cfg.state_dir, root / "tmp-state")

    def test_config_keeps_auth_and_task_options(self):
        root = Path(tempfile.mkdtemp())
        cfg = FleetConfig.from_root(
            root,
            ingest_token="test-only-token",
            dev_operator="dev@example.test",
            runner_credentials={"hk": "test-only-runner"},
            project_whitelist={"hk": ["agent-fleet"]},
            tasks_enabled=False,
        )
        self.assertEqual(cfg.ingest_token, "test-only-token")
        self.assertEqual(cfg.dev_operator, "dev@example.test")
        self.assertEqual(cfg.runner_credentials, {"hk": "test-only-runner"})
        self.assertEqual(cfg.project_whitelist, {"hk": ["agent-fleet"]})
        self.assertFalse(cfg.tasks_enabled)

    def test_bootstrap_import_does_not_mutate_sys_path(self):
        before = list(sys.path)
        import hub.bootstrap  # noqa: F401
        self.assertEqual(sys.path, before)


if __name__ == "__main__":
    unittest.main()
