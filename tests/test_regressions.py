import importlib.util
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hub import events
from hub import scan
from hub import state as store
from hub import web


def load_self_report_module():
    path = Path(__file__).resolve().parents[1] / "tools" / "agent-self-report.py"
    spec = importlib.util.spec_from_file_location("agent_self_report", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SelfReportTests(unittest.TestCase):
    def test_collect_system_supports_macos(self):
        from tools import probe_collectors as module

        def fake_run(args, **kwargs):
            if args[:3] == ["sysctl", "-n", "hw.memsize"]:
                return SimpleNamespace(stdout=str(16 * 1024**3))
            if args == ["vm_stat"]:
                return SimpleNamespace(stdout=(
                    "Mach Virtual Memory Statistics: (page size of 4096 bytes)\n"
                    "Pages free: 100000.\n"
                    "Pages inactive: 200000.\n"
                    "Pages speculative: 50000.\n"
                ))
            raise AssertionError(f"unexpected command: {args}")

        disk = SimpleNamespace(total=1000, used=250, free=750)
        with mock.patch.object(module.platform, "system", return_value="Darwin"), \
             mock.patch.object(module.os, "getloadavg", return_value=(1.25, 1.0, 0.5)), \
             mock.patch.object(module.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(module.shutil, "disk_usage", return_value=disk):
            result = module.collect_system()

        self.assertEqual(result["platform"], "darwin")
        self.assertEqual(result["load"], "1.25")
        self.assertEqual(result["mem_total_mb"], "16384")
        self.assertGreater(int(result["mem_used_mb"]), 0)
        self.assertEqual(result["disk_used_pct"], "25%")


class IngestScanTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.old_state_dir = store.STATE_DIR
        self.old_event_log = events.EVENT_LOG
        store.STATE_DIR = self.temp_dir
        events.EVENT_LOG = self.temp_dir / "events.jsonl"

    def tearDown(self):
        store.STATE_DIR = self.old_state_dir
        events.EVENT_LOG = self.old_event_log

    def test_fresh_ingest_snapshot_is_not_overwritten(self):
        store.save_snapshot("katabump", {
            "machine": "katabump",
            "source": "ingest",
            "reachable": True,
            "agents": {"codex": {"installed": True}},
        })
        changes = scan.reconcile_ingest(now=time.time())
        snapshot = store.read_current("katabump")

        self.assertTrue(snapshot["reachable"])
        self.assertEqual(snapshot["agents"], {"codex": {"installed": True}})
        self.assertEqual(changes, [])

    def test_stale_ingest_snapshot_becomes_offline(self):
        store.save_snapshot("katabump", {
            "machine": "katabump",
            "source": "ingest",
            "reachable": True,
            "agents": {},
        })
        current = store.read_current("katabump")
        current["_ts"] = time.time() - 301
        (self.temp_dir / "katabump.json").write_text(__import__("json").dumps(current))
        changes = scan.reconcile_ingest(now=time.time())
        snapshot = store.read_current("katabump")

        self.assertFalse(snapshot["reachable"])
        self.assertEqual(changes[0]["machine"], "katabump")
        self.assertIn("reachable", changes[0]["changed"])

    def test_concurrent_snapshot_writes_keep_current_json_valid(self):
        def write(i):
            store.save_snapshot("katabump", {"machine": "katabump", "seq": i})

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(write, range(20)))

        current = store.read_current("katabump")
        self.assertIn(current["seq"], range(20))
        lines = (self.temp_dir / "katabump.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 20)
        for line in lines:
            json.loads(line)


class IngestApiSecurityTests(unittest.TestCase):
    def test_machine_name_cannot_escape_state_directory(self):
        root = Path(tempfile.mkdtemp())
        state_dir = root / "state"
        state_dir.mkdir()
        old_store_dir = store.STATE_DIR
        old_web_dir = web.STATE_DIR
        old_event_log = events.EVENT_LOG
        store.STATE_DIR = state_dir
        web.STATE_DIR = state_dir
        events.EVENT_LOG = state_dir / "events.jsonl"
        try:
            app = web.make_app(ingest_token="secret")
            client = app.test_client()
            response = client.post(
                "/api/ingest",
                json={"machine": "../escaped", "agents": {}},
                headers={"X-Agent-Fleet-Token": "secret"},
            )
            self.assertEqual(response.status_code, 400)
            self.assertFalse((root / "escaped.json").exists())
        finally:
            store.STATE_DIR = old_store_dir
            web.STATE_DIR = old_web_dir
            events.EVENT_LOG = old_event_log

    def test_scan_endpoint_requires_post_and_token(self):
        app = web.make_app(ingest_token="secret")
        client = app.test_client()

        # Flask 路由匹配后按方法校验：GET /api/scan 无 GET 处理器 → 404
        # （bounded not_found JSON）；无 token POST 仍必须 403。
        self.assertEqual(client.get("/api/scan").status_code, 404)
        self.assertEqual(client.post("/api/scan").status_code, 403)

    def test_public_status_drops_sensitive_ingest_fields(self):
        root = Path(tempfile.mkdtemp())
        state_dir = root / "state"
        state_dir.mkdir()
        old_store_dir = store.STATE_DIR
        old_web_dir = web.STATE_DIR
        old_event_log = events.EVENT_LOG
        store.STATE_DIR = state_dir
        web.STATE_DIR = state_dir
        events.EVENT_LOG = state_dir / "events.jsonl"
        try:
            app = web.make_app(ingest_token="secret")
            client = app.test_client()
            response = client.post(
                "/api/ingest",
                json={
                    "machine": "mac-local",
                    "agents": {
                        "hermes": {
                            "installed": True,
                            "gateway_state": "running",
                            "session_count": 1,
                            "sessions": [{
                                "session_id": "secret-session",
                                "display_name": "private-user",
                            }],
                        }
                    },
                },
                headers={"X-Agent-Fleet-Token": "secret"},
            )
            self.assertEqual(response.status_code, 200)

            payload = client.get("/api/status").get_json()
            machine = next(
                item for item in payload["machines"] if item["machine"] == "mac-local"
            )
            self.assertEqual(machine["agents"]["hermes"], {
                "installed": True,
                "gateway_state": "running",
                "session_count": 1,
            })
            self.assertNotIn("sessions", machine)
        finally:
            store.STATE_DIR = old_store_dir
            web.STATE_DIR = old_web_dir
            events.EVENT_LOG = old_event_log


if __name__ == "__main__":
    unittest.main()
