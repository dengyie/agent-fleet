"""评审修复 #8/#9：SSE 订阅生命周期与并发上限、/api/scan 单飞、
web.make_app 不覆盖 bootstrap 的实际生效门控。"""
import threading
import tempfile
import unittest
from pathlib import Path

from support.hub_env import HubStores

TOKEN = "ingest-secret"


class _Base(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.hub = HubStores(self.temp_dir)

    def app(self, **kw):
        kw.setdefault("ingest_token", TOKEN)
        kw.setdefault("dev_operator", "op@example.com")
        return self.hub.make_app(**kw)


class SseLifecycleTests(_Base):
    def publisher(self, app):
        return app.extensions["fleet"]["publisher"]

    def test_unconsumed_stream_leaves_no_subscription_and_returns_slot(self):
        app = self.app()
        app.config["SSE_MAX_STREAMS"] = 2
        client = app.test_client()
        before = len(self.publisher(app)._sse_pairs)
        for _ in range(5):  # more than the cap: every slot must come back
            resp = client.get("/api/stream", buffered=False)
            self.assertEqual(resp.status_code, 200)
            resp.close()  # client went away without reading a byte
        self.assertEqual(len(self.publisher(app)._sse_pairs), before)
        self.assertEqual(app.extensions["observe_http"]["sse"].active, 0)

    def test_consumed_stream_unsubscribes(self):
        app = self.app()
        client = app.test_client()
        before = len(self.publisher(app)._sse_pairs)
        resp = client.get("/api/stream", buffered=False)
        first = next(resp.response)
        self.assertIn(b"connected", first if isinstance(first, bytes) else first.encode())
        self.assertEqual(len(self.publisher(app)._sse_pairs), before + 1)
        resp.close()
        self.assertEqual(len(self.publisher(app)._sse_pairs), before)

    def test_stream_cap_returns_503(self):
        app = self.app()
        app.config["SSE_MAX_STREAMS"] = 1
        client = app.test_client()
        held = client.get("/api/stream", buffered=False)
        try:
            resp = client.get("/api/stream")
            self.assertEqual(resp.status_code, 503)
            self.assertEqual(resp.get_json()["error"], "stream_capacity")
        finally:
            held.close()
        again = client.get("/api/stream", buffered=False)
        self.assertEqual(again.status_code, 200)
        again.close()


class ScanSingleFlightTests(_Base):
    def test_concurrent_scans_run_one_reconcile(self):
        app = self.app()
        service = app.extensions["fleet"]["services"]["observe"]
        entered, release = threading.Event(), threading.Event()
        calls = []

        def slow_reconcile():
            calls.append(1)
            entered.set()
            release.wait(5)
            return []

        service.reconcile = slow_reconcile
        client = app.test_client()
        headers = {"X-Agent-Fleet-Token": TOKEN}
        first = client.post("/api/scan", headers=headers).get_json()
        self.assertTrue(entered.wait(5))
        second = client.post("/api/scan", headers=headers).get_json()
        self.assertNotIn("already_running", first)
        self.assertTrue(second["already_running"])
        release.set()
        lock = app.extensions["observe_http"]["scan_lock"]
        for _ in range(100):
            if not lock.locked():
                break
            threading.Event().wait(0.02)
        self.assertFalse(lock.locked())
        self.assertNotIn("already_running",
                         client.post("/api/scan", headers=headers).get_json())
        release.set()
        self.assertGreaterEqual(len(calls), 1)


class EffectiveGateTests(_Base):
    def test_make_app_keeps_bootstrap_effective_gates(self):
        # sync requested but network gate off: bootstrap never starts a
        # scheduler, so the effective gate must stay False.
        app = self.app(platform_enabled=True,
                       platform_db=self.temp_dir / "platform.db",
                       http_probe_enabled=True, http_probe_sync_enabled=True,
                       service_actions_enabled=True)
        self.assertFalse(app.config["HTTP_PROBE_SYNC_ENABLED"])
        # service actions need service monitoring; without it no service.
        self.assertFalse(app.config["SERVICE_ACTIONS_ENABLED"])
        # memory requested without the platform: no service, gate stays off.
        app = self.app(platform_memory_enabled=True,
                       platform_memory_context_enabled=True)
        self.assertFalse(app.config["PLATFORM_MEMORY_ENABLED"])
        self.assertFalse(app.config["PLATFORM_MEMORY_CONTEXT_ENABLED"])


if __name__ == "__main__":
    unittest.main()
