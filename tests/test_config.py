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

    def test_komari_sync_defaults_off_and_interval_is_bounded(self):
        root = Path(tempfile.mkdtemp())
        cfg = FleetConfig.from_root(root)
        self.assertFalse(cfg.komari_sync_enabled)
        self.assertEqual(cfg.komari_sync_interval_s, 60.0)
        self.assertEqual(
            FleetConfig.from_root(root, komari_sync_interval_s=1).komari_sync_interval_s,
            5.0,
        )
        self.assertEqual(
            FleetConfig.from_root(root, komari_sync_interval_s=99999).komari_sync_interval_s,
            3600.0,
        )
        self.assertEqual(
            FleetConfig.from_root(root, komari_sync_interval_s="invalid").komari_sync_interval_s,
            60.0,
        )
        self.assertFalse(FleetConfig.from_root(
            root, komari_enabled=True, komari_network_enabled=True,
        ).komari_network_enabled)
        self.assertTrue(FleetConfig.from_root(
            root, platform_enabled=True, komari_enabled=True,
            komari_network_enabled=True,
        ).komari_network_enabled)

    def test_http_probe_gates_are_separate_and_interval_is_bounded(self):
        root = Path(tempfile.mkdtemp())
        cfg = FleetConfig.from_root(root)
        self.assertFalse(cfg.http_probe_enabled)
        self.assertFalse(cfg.http_probe_network_enabled)
        self.assertFalse(cfg.http_probe_sync_enabled)
        cfg = FleetConfig.from_root(
            root,
            platform_enabled=True,
            service_monitoring_enabled=True,
            http_probe_enabled=True,
            http_probe_network_enabled=True,
            http_probe_sync_enabled=True,
            http_probe_sync_interval_s=1,
            http_probe_allowed_origins=("https://probe.example.test",),
        )
        self.assertTrue(cfg.http_probe_enabled)
        self.assertTrue(cfg.http_probe_network_enabled)
        self.assertTrue(cfg.http_probe_sync_enabled)
        self.assertEqual(cfg.http_probe_sync_interval_s, 5.0)
        self.assertEqual(
            FleetConfig.from_root(
                root,
                platform_enabled=True,
                service_monitoring_enabled=True,
                http_probe_enabled=True,
                http_probe_network_enabled=True,
                http_probe_sync_enabled=True,
                http_probe_sync_interval_s=99999,
            ).http_probe_sync_interval_s,
            3600.0,
        )
        self.assertFalse(FleetConfig.from_root(
            root,
            platform_enabled=True,
            service_monitoring_enabled=True,
            http_probe_enabled=True,
            http_probe_sync_enabled=True,
        ).http_probe_sync_enabled)

    def test_platform_worker_is_off_and_bounded(self):
        root = Path(tempfile.mkdtemp())
        cfg = FleetConfig.from_root(root, platform_enabled=True)
        self.assertFalse(cfg.platform_worker_enabled)
        self.assertEqual(cfg.platform_worker_interval_s, 1.0)
        self.assertEqual(cfg.platform_worker_lease_s, 60.0)
        cfg = FleetConfig.from_root(
            root, platform_enabled=True, platform_worker_enabled=True,
            platform_worker_interval_s=0, platform_worker_lease_s=99999,
        )
        self.assertTrue(cfg.platform_worker_enabled)
        self.assertEqual(cfg.platform_worker_interval_s, 1.0)
        self.assertEqual(cfg.platform_worker_lease_s, 3600.0)
        self.assertFalse(FleetConfig.from_root(
            root, platform_worker_enabled=True).platform_worker_enabled)

    def test_platform_memory_is_default_off_and_requires_platform(self):
        root = Path(tempfile.mkdtemp())
        self.assertFalse(FleetConfig.from_root(root).platform_memory_enabled)
        self.assertFalse(FleetConfig.from_root(
            root, platform_memory_enabled=True,
        ).platform_memory_enabled)
        self.assertTrue(FleetConfig.from_root(
            root, platform_enabled=True, platform_memory_enabled=True,
        ).platform_memory_enabled)

    def test_multi_owner_scheduler_is_separate_default_off_gate(self):
        root = Path(tempfile.mkdtemp())
        cfg = FleetConfig.from_root(
            root, platform_enabled=True, platform_worker_enabled=True,
            platform_worker_scheduler_enabled=True,
            platform_worker_max_concurrency=8,
            platform_worker_max_workspace_concurrency=3,
        )
        self.assertTrue(cfg.platform_worker_scheduler_enabled)
        self.assertEqual(cfg.platform_worker_max_concurrency, 8)
        self.assertEqual(cfg.platform_worker_max_workspace_concurrency, 3)
        self.assertFalse(FleetConfig.from_root(
            root, platform_enabled=True,
            platform_worker_scheduler_enabled=True).platform_worker_scheduler_enabled)
        self.assertEqual(FleetConfig.from_root(
            root, platform_worker_enabled=True, platform_worker_scheduler_enabled=True,
            platform_worker_max_concurrency=999,
        ).platform_worker_max_concurrency, 64)

    def test_memory_context_gate_requires_platform_and_memory_gates(self):
        root = Path(tempfile.mkdtemp())
        self.assertFalse(FleetConfig.from_root(
            root, platform_enabled=True, platform_memory_context_enabled=True,
        ).platform_memory_context_enabled)
        self.assertFalse(FleetConfig.from_root(
            root, platform_enabled=True, platform_memory_enabled=True,
        ).platform_memory_context_enabled)
        self.assertTrue(FleetConfig.from_root(
            root, platform_enabled=True, platform_memory_enabled=True,
            platform_memory_context_enabled=True,
        ).platform_memory_context_enabled)

    def test_provider_network_requires_platform_gate(self):
        root = Path(tempfile.mkdtemp())
        self.assertFalse(FleetConfig.from_root(
            root, platform_provider_network_enabled=True,
        ).platform_provider_network_enabled)
        self.assertTrue(FleetConfig.from_root(
            root, platform_enabled=True, platform_provider_network_enabled=True,
        ).platform_provider_network_enabled)

    def test_remote_execution_requires_platform_gate_and_defaults_off(self):
        root = Path(tempfile.mkdtemp())
        self.assertFalse(FleetConfig.from_root(
            root, platform_remote_execution_enabled=True,
        ).platform_remote_execution_enabled)
        self.assertTrue(FleetConfig.from_root(
            root, platform_enabled=True, platform_remote_execution_enabled=True,
        ).platform_remote_execution_enabled)


if __name__ == "__main__":
    unittest.main()
