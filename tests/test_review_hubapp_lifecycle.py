"""评审修复 #17：create_app 不派生线程；Muse 自动审批 worker 由
start_background_jobs 启动并可停止。"""
import threading
import tempfile
import unittest
from pathlib import Path

from hub.bootstrap import create_app, start_background_jobs
from hub.config import FleetConfig


class CreateAppSpawnsNoThreadsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def config(self):
        return FleetConfig.from_root(
            self.tmp, ingest_token="i", dev_operator="op@example.com",
            platform_enabled=True)

    def test_existing_muse_store_starts_only_with_background_jobs(self):
        # 预置 Muse 持久化文件：旧实现会在 create_app 内直接起 muse-approvals 线程。
        app = create_app(self.config())
        muse = app.extensions["fleet"]["services"]["muse"]
        muse.repository.path.parent.mkdir(parents=True, exist_ok=True)
        muse.repository.path.touch()

        before = {t.ident for t in threading.enumerate()}
        app = create_app(self.config())
        muse = app.extensions["fleet"]["services"]["muse"]
        self.assertTrue(muse.repository.path.exists())
        spawned = [t.name for t in threading.enumerate() if t.ident not in before]
        self.assertEqual(spawned, [])
        self.assertIsNone(muse.worker)

        jobs = start_background_jobs(app)
        try:
            self.assertIn("muse_approvals", jobs)
            self.assertIsNotNone(muse.worker)
            self.assertTrue(muse.worker.is_alive())
        finally:
            for stop in jobs.values():
                stop.set()
            muse.close()
        self.assertIsNone(muse.worker)


if __name__ == "__main__":
    unittest.main()
