import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hub import state as store
import time
from hub import scan, events


class JsonlRotationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.old_state_dir = store.STATE_DIR
        self.old_max = store.MAX_JSONL_BYTES
        self.old_keep = store.ROTATE_KEEP_LINES
        store.STATE_DIR = self.temp_dir
        store.MAX_JSONL_BYTES = 4096  # 缩小阈值避免写 5MB
        store.ROTATE_KEEP_LINES = 40

    def tearDown(self):
        store.STATE_DIR = self.old_state_dir
        store.MAX_JSONL_BYTES = self.old_max
        store.ROTATE_KEEP_LINES = self.old_keep

    def test_oversized_jsonl_rotates_and_keeps_tail(self):
        for i in range(200):
            store.save_snapshot("hk", {"machine": "hk", "seq": i, "pad": "x" * 100})
        path = self.temp_dir / "hk.jsonl"
        self.assertLessEqual(path.stat().st_size, 4096 * 2)
        lines = path.read_text().splitlines()
        self.assertLessEqual(len(lines), 200)
        # 最新快照必须保留，且每行可解析
        self.assertEqual(json.loads(lines[-1])["seq"], 199)
        for line in lines:
            json.loads(line)

    def test_small_jsonl_is_untouched(self):
        store.save_snapshot("hk", {"machine": "hk", "seq": 0})
        store.save_snapshot("hk", {"machine": "hk", "seq": 1})
        self.assertEqual(len((self.temp_dir / "hk.jsonl").read_text().splitlines()), 2)


class ScanFilterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.old_state_dir = store.STATE_DIR
        self.old_event_log = events.EVENT_LOG
        store.STATE_DIR = self.temp_dir
        events.EVENT_LOG = self.temp_dir / "events.jsonl"

    def tearDown(self):
        store.STATE_DIR = self.old_state_dir
        events.EVENT_LOG = self.old_event_log

    def _write_stale(self, machine):
        store.save_snapshot(machine, {
            "machine": machine, "source": "ingest", "reachable": True, "agents": {},
        })
        current = store.read_current(machine)
        current["_ts"] = time.time() - 301
        (self.temp_dir / f"{machine}.json").write_text(json.dumps(current))

    def test_machine_scoped_reconcile_leaves_other_machines_alone(self):
        self._write_stale("hk")
        self._write_stale("tebi")
        results = scan.reconcile_ingest(now=time.time(), machine="hk")
        self.assertEqual([r["machine"] for r in results], ["hk"])
        self.assertFalse(store.read_current("hk")["reachable"])
        self.assertTrue(store.read_current("tebi")["reachable"])

    def test_scan_all_filter_host_only_reconciles_that_host(self):
        self._write_stale("hk")
        self._write_stale("tebi")
        results = scan.scan_all(filter_host="tebi")
        self.assertEqual([r["machine"] for r in results], ["tebi"])
        self.assertTrue(store.read_current("hk")["reachable"])
        self.assertFalse(store.read_current("tebi")["reachable"])


class AuthIngestTokenTests(unittest.TestCase):
    def _make_app(self, token):
        from flask import Flask, jsonify
        from hub import auth
        app = Flask(__name__)
        app.config["INGEST_TOKEN"] = token

        @app.route("/probe", methods=["POST"])
        @auth.require_ingest_token
        def probe():
            return jsonify({"ok": True})
        return app

    def test_valid_token_passes(self):
        client = self._make_app("secret").test_client()
        resp = client.post("/probe", headers={"X-Agent-Fleet-Token": "secret"})
        self.assertEqual(resp.status_code, 200)

    def test_wrong_or_missing_token_403(self):
        client = self._make_app("secret").test_client()
        self.assertEqual(client.post("/probe").status_code, 403)
        self.assertEqual(
            client.post("/probe", headers={"X-Agent-Fleet-Token": "nope"}).status_code, 403)

    def test_unconfigured_token_allows(self):
        client = self._make_app(None).test_client()
        self.assertEqual(client.post("/probe").status_code, 200)

    def test_machine_re(self):
        from hub import auth
        self.assertTrue(auth.MACHINE_RE.fullmatch("mac-local.1"))
        self.assertFalse(auth.MACHINE_RE.fullmatch("../etc"))
        self.assertFalse(auth.MACHINE_RE.fullmatch("-lead-dash"))
        self.assertFalse(auth.MACHINE_RE.fullmatch("x" * 65))


class ObserveBlueprintTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.old_state_dir = store.STATE_DIR
        self.old_event_log = events.EVENT_LOG
        store.STATE_DIR = self.temp_dir
        events.EVENT_LOG = self.temp_dir / "events.jsonl"
        from hub import web
        self.client = web.make_app(ingest_token="secret").test_client()

    def tearDown(self):
        store.STATE_DIR = self.old_state_dir
        events.EVENT_LOG = self.old_event_log

    def _ingest(self, machine="mac-local"):
        return self.client.post("/api/ingest", json={
            "machine": machine,
            "agents": {"hermes": {"installed": True, "gateway_state": "running",
                                  "session_count": 1,
                                  "sessions": [{"session_id": "x"}]}},
            "system": {"platform": "darwin", "load": "1.0"},
        }, headers={"X-Agent-Fleet-Token": "secret"})

    def test_ingest_and_status_via_blueprint(self):
        self.assertEqual(self._ingest().status_code, 200)
        payload = self.client.get("/api/status").get_json()
        machine = next(m for m in payload["machines"] if m["machine"] == "mac-local")
        self.assertEqual(machine["agents"]["hermes"]["session_count"], 1)
        self.assertNotIn("sessions", machine["agents"]["hermes"])

    def test_machine_detail_api(self):
        self._ingest()
        resp = self.client.get("/api/machines/mac-local")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["current"]["reachable"])
        self.assertEqual(data["current"]["agents"]["hermes"]["session_count"], 1)
        self.assertIsInstance(data["history"], list)
        self.assertEqual(self.client.get("/api/machines/ghost").status_code, 404)
        self.assertEqual(self.client.get("/api/machines/-lead-dash").status_code, 400)

    def test_events_api_lists_recent_events(self):
        self._ingest()
        payload = self.client.get("/api/events").get_json()
        self.assertTrue(any(e["machine"] == "mac-local" for e in payload["events"]))
        # 事件流不携带完整快照（SSE/列表只给摘要）
        for e in payload["events"]:
            self.assertNotIn("snapshot", e.get("extra", {}))

    def test_resolve_ingest_token_priority(self):
        from hub import auth
        self.assertEqual(auth.resolve_ingest_token("cli"), "cli")
        with mock.patch.dict(os.environ, {"AGENT_FLEET_INGEST_TOKEN": "envval"}, clear=True):
            self.assertEqual(auth.resolve_ingest_token(None), "envval")
            self.assertEqual(auth.resolve_ingest_token("cli2"), "cli2")


class SseStreamTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.old_state_dir = store.STATE_DIR
        self.old_event_log = events.EVENT_LOG
        store.STATE_DIR = self.temp_dir
        events.EVENT_LOG = self.temp_dir / "events.jsonl"
        from hub import web
        self.app = web.make_app(ingest_token="secret")

    def tearDown(self):
        store.STATE_DIR = self.old_state_dir
        events.EVENT_LOG = self.old_event_log

    def test_sse_bridge_receives_emitted_events(self):
        q = events.sse_subscribe()
        try:
            events.emit("state_changed", machine="hk", changes=["agents"],
                        snapshot={"reachable": True, "agents": {}, "system": {}})
            got = q.get(timeout=2)
            self.assertEqual(got["event"], "state_changed")
            self.assertEqual(got["machine"], "hk")
        finally:
            events.sse_unsubscribe(q)

    def test_stream_endpoint_delivers_machine_update(self):
        q_client = self.app.test_client()
        resp = q_client.get("/api/stream", buffered=False)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/event-stream", resp.content_type)
        events.emit("state_changed", machine="hk", changes=["agents"],
                    snapshot={"reachable": True, "agents": {"codex": {"installed": True}},
                              "system": {"load": "1.0"}})
        body = b""
        for chunk in resp.response:
            body += chunk
            if b"machine_update" in body:
                break
        resp.close()
        text = body.decode()
        self.assertIn("event: machine_update", text)
        self.assertIn('"machine": "hk"', text)
        self.assertNotIn("session", text)

    def test_stream_maps_task_events(self):
        from hub import routes_observe
        e = {"event": "task_queued", "machine": "mac-local", "ts": 1.0,
             "changes": [], "extra": {"task_id": "t-1", "state": "queued"}}
        self.assertEqual(routes_observe._sse_event_name(e), "task_update")
        self.assertEqual(routes_observe._sse_payload(e)["task_id"], "t-1")
        log_e = {"event": "task_log", "machine": "mac-local", "ts": 2.0,
                 "changes": [], "extra": {"task_id": "t-1", "line": "hello"}}
        self.assertEqual(routes_observe._sse_event_name(log_e), "task_log")
        self.assertEqual(routes_observe._sse_payload(log_e)["line"], "hello")

    def test_stream_endpoint_delivers_task_events(self):
        q_client = self.app.test_client()
        resp = q_client.get("/api/stream", buffered=False)
        self.assertEqual(resp.status_code, 200)
        events.emit("task_queued", machine="mac-local", task_id="t-1", state="queued")
        events.emit("task_log", machine="mac-local", task_id="t-1",
                    line="echo 中文日志")
        events.emit("task_finished", machine="mac-local", task_id="t-1", state="succeeded")
        events.emit("state_changed", machine="hk", changes=["agents"],
                    snapshot={"reachable": True, "agents": {}, "system": {}})
        body = b""
        for chunk in resp.response:
            body += chunk
            if b'"t-1"' in body and b"machine_update" in body:
                break
        resp.close()
        text = body.decode()
        self.assertIn("event: task_update", text)
        self.assertIn('"task_id": "t-1"', text)
        self.assertIn('"state": "succeeded"', text)
        self.assertIn("event: task_log", text)
        self.assertIn("echo 中文日志", text)
        self.assertIn("event: machine_update", text)


class EventPersistFailureIsolationTests(unittest.TestCase):
    """Task 11: event persistence is best effort; live subscribers remain fed."""

    def _app_with_failing_event_repo(self):
        from hub.application.event_publisher import EventPublisher
        from hub.config import FleetConfig
        from hub.infrastructure.state_repository import JsonlObservationRepository

        temp_dir = Path(tempfile.mkdtemp())
        old_state = store.STATE_DIR
        old_events = events.EVENT_LOG
        store.STATE_DIR = temp_dir
        events.EVENT_LOG = temp_dir / "events.jsonl"
        root = Path(__file__).resolve().parents[1]

        class FailingRepo:
            def __init__(self):
                self.appended = []

            def append(self, ev):
                raise OSError("event disk full")

            def read_recent(self, limit=50):
                return list(self.appended[-limit:])

            def read_since(self, ts, limit=200):
                return []

        firm = FailingRepo()
        publisher = EventPublisher(firm)
        config = FleetConfig.from_root(
            root,
            state_dir=temp_dir,
            hosts_file=root / "hosts.yaml",
            event_log=temp_dir / "events.jsonl",
            task_db=temp_dir / "fleet.db",
            ingest_token="secret",
        )
        from hub.bootstrap import create_app
        app = create_app(config, publisher=publisher)
        app._event_repo_cleanup = (old_state, old_events)
        return app, publisher, firm

    def tearDown(self):
        store.STATE_DIR, events.EVENT_LOG = self._cleanup

    def test_emit_to_failing_repo_still_reaches_subscriber(self):
        app, publisher, firm = self._app_with_failing_event_repo()
        self._cleanup = (store.STATE_DIR, events.EVENT_LOG)
        seen = []
        unsub = publisher.subscribe(seen.append)
        try:
            publisher.emit("state_changed", machine="hk", changes=["agents"])
        finally:
            unsub()
        self.assertEqual(seen[0]["machine"], "hk")
        self.assertEqual(seen[0]["event"], "state_changed")

    def test_event_persistence_failure_does_not_fail_ingest(self):
        app, publisher, firm = self._app_with_failing_event_repo()
        self._cleanup = (store.STATE_DIR, events.EVENT_LOG)
        from hub.bootstrap import create_app

        FailingObservationRepo = None  # not needed; default observation repo used

        resp = app.test_client().post(
            "/api/ingest",
            json={"machine": "hk", "agents": {"codex": {"installed": True}}},
            headers={"X-Agent-Fleet-Token": "secret"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])

    def test_sse_subscriber_receives_event_despite_persist_failure(self):
        app, publisher, firm = self._app_with_failing_event_repo()
        self._cleanup = (store.STATE_DIR, events.EVENT_LOG)
        q_client = app.test_client()
        resp = q_client.get("/api/stream", buffered=False)
        self.assertEqual(resp.status_code, 200)
        events.emit("state_changed", machine="hk", changes=["agents"],
                    snapshot={"reachable": True, "agents": {}, "system": {}})
        body = b""
        for chunk in resp.response:
            body += chunk
            if b"machine_update" in body:
                break
        resp.close()
        text = body.decode()
        self.assertIn("event: machine_update", text)
        self.assertIn('"machine": "hk"', text)


class EventReadFailureIsolationTests(unittest.TestCase):
    """Event repository reads degrade without breaking API or live delivery."""

    def setUp(self):
        self._cleanup = None

    def tearDown(self):
        if self._cleanup is not None:
            store.STATE_DIR, events.EVENT_LOG = self._cleanup

    def _build_app(self, repository):
        from hub.application.event_publisher import EventPublisher
        from hub.bootstrap import create_app
        from hub.config import FleetConfig

        temp_dir = Path(tempfile.mkdtemp())
        self._cleanup = (store.STATE_DIR, events.EVENT_LOG)
        store.STATE_DIR = temp_dir
        events.EVENT_LOG = temp_dir / "events.jsonl"
        root = Path(__file__).resolve().parents[1]
        config = FleetConfig.from_root(
            root,
            state_dir=temp_dir,
            hosts_file=root / "hosts.yaml",
            event_log=temp_dir / "events.jsonl",
            task_db=temp_dir / "fleet.db",
            ingest_token="secret",
        )
        return create_app(config, publisher=EventPublisher(repository))

    @staticmethod
    def _read_failing_repository():
        class ReadFailingRepository:
            def append(self, event):
                return None

            def read_recent(self, limit=50):
                raise OSError("event log unreadable")

            def read_since(self, ts, limit=200):
                raise OSError("event log unreadable")

        return ReadFailingRepository()

    def test_events_api_degrades_to_empty_when_recent_read_fails(self):
        app = self._build_app(self._read_failing_repository())
        response = app.test_client().get("/api/events")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"events": []})

    def test_sse_replay_read_failure_keeps_live_delivery(self):
        app = self._build_app(self._read_failing_repository())
        response = app.test_client().get(
            "/api/stream?since=999999", buffered=False)
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/event-stream", response.content_type)
        events.emit(
            "state_changed",
            machine="hk",
            changes=["agents"],
            snapshot={"reachable": True, "agents": {}, "system": {}},
        )
        body = b""
        for chunk in response.response:
            body += chunk
            if b"machine_update" in body:
                break
        response.close()
        text = body.decode()
        self.assertIn("event: machine_update", text)
        self.assertIn('"machine": "hk"', text)


class PageViewTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.old_state_dir = store.STATE_DIR
        self.old_event_log = events.EVENT_LOG
        store.STATE_DIR = self.temp_dir
        events.EVENT_LOG = self.temp_dir / "events.jsonl"
        from hub import web
        self.client = web.make_app(ingest_token="secret").test_client()

    def tearDown(self):
        store.STATE_DIR = self.old_state_dir
        events.EVENT_LOG = self.old_event_log

    def test_fleet_page_uses_template_and_static_assets(self):
        store.save_snapshot("hk", {"machine": "hk", "source": "ingest",
                                   "reachable": True, "agents": {}, "system": {}})
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('data-page="fleet"', html)
        self.assertIn('static/style.css', html)
        self.assertIn('static/app.js', html)
        self.assertIn('data-machine="hk"', html)
        self.assertNotIn("render_template_string", html)

    def test_machine_page(self):
        store.save_snapshot("hk", {"machine": "hk", "source": "ingest",
                                   "reachable": True,
                                   "agents": {"codex": {"installed": True, "active_count": 1}},
                                   "system": {"platform": "linux", "load": "0.5"}})
        resp = self.client.get("/machine/hk")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('data-page="machine"', html)
        self.assertIn('data-machine="hk"', html)
        self.assertIn("codex", html)
        self.assertEqual(self.client.get("/machine/ghost").status_code, 404)


class MachineDetailDisclosureTests(unittest.TestCase):
    """Fix I1: the machine-detail ``instances[]`` rows are identity-gated.

    An anonymous caller (no CF-Access email and no DEV_OPERATOR fallback) sees
    only the bounded metadata projection — NEVER raw pid/pgid/started_at/
    exe_path/cmdline/native_file_path.  An operator identity (CF header, or
    DEV_OPERATOR without a foreign-domain credential header) sees the full
    sanitized rows because adopting a candidate needs pid + started_at.
    """

    BANNED_KEYS = ("pid", "pgid", "started_at", "exe_path", "cmdline",
                   "native_file_path")
    INSTANCE = {
        "pid": 4242, "pgid": 4242,
        "exe_path": "/usr/local/bin/codex",
        "cmdline": "codex session --tour",
        "agent_family": "codex",
        "native_file_path": None,
        "started_at": "2026-07-30T09:15:00Z",
        "attachable": True,
    }

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.old_state_dir = store.STATE_DIR
        self.old_event_log = events.EVENT_LOG
        store.STATE_DIR = self.temp_dir
        events.EVENT_LOG = self.temp_dir / "events.jsonl"
        store.save_snapshot("mac-local", {
            "machine": "mac-local", "source": "ingest", "reachable": True,
            "agents": {"codex": {"installed": True, "active_count": 1}},
            "system": {"platform": "darwin"},
            "instances": [dict(self.INSTANCE)],
        })

    def tearDown(self):
        store.STATE_DIR = self.old_state_dir
        events.EVENT_LOG = self.old_event_log

    def _client(self, **kw):
        from hub import web
        return web.make_app(ingest_token="secret", **kw).test_client()

    def test_anonymous_machine_detail_reduces_instances_to_metadata(self):
        client = self._client(dev_operator=None)
        resp = client.get("/api/machines/mac-local")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        rows = data["current"]["instances"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        for banned in self.BANNED_KEYS:
            self.assertNotIn(banned, row)
        self.assertEqual(set(row), {"agent_family", "attachable"})
        # every OTHER detail field stays unchanged (agents/system/current/history)
        self.assertEqual(data["current"]["agents"]["codex"]["installed"], True)
        self.assertEqual(data["current"]["system"]["platform"], "darwin")
        self.assertEqual(data["machine"], "mac-local")
        self.assertIsInstance(data["history"], list)

    def test_anonymous_foreign_credential_header_never_gains_identity(self):
        # DEV_OPERATOR configured, but a foreign-domain credential header must
        # still block the fallback -> the anonymous projection is served.
        client = self._client(dev_operator="op@example.com")
        resp = client.get("/api/machines/mac-local",
                          headers={"X-Agent-Fleet-Token": "secret"})
        row = resp.get_json()["current"]["instances"][0]
        for banned in self.BANNED_KEYS:
            self.assertNotIn(banned, row)

    def test_operator_header_sees_full_sanitized_instance_rows(self):
        client = self._client(dev_operator=None)
        resp = client.get(
            "/api/machines/mac-local",
            headers={"Cf-Access-Authenticated-User-Email": "op@example.com"})
        rows = resp.get_json()["current"]["instances"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["pid"], 4242)
        self.assertEqual(row["started_at"], "2026-07-30T09:15:00Z")
        self.assertEqual(row["exe_path"], "/usr/local/bin/codex")
        self.assertEqual(row["cmdline"], "codex session --tour")
        self.assertEqual(row["agent_family"], "codex")
        self.assertTrue(row["attachable"])

    def test_dev_operator_fallback_sees_full_sanitized_instance_rows(self):
        client = self._client(dev_operator="op@example.com")
        row = client.get("/api/machines/mac-local").get_json()["current"]["instances"][0]
        self.assertEqual(row["pid"], 4242)
        self.assertIn("cmdline", row)
