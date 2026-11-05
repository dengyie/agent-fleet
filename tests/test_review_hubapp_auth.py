"""评审修复 #2/#3/#13/#14：ingest 认证 fail-closed、机器绑定、存储故障 503、
显式机器认证标记。"""
import functools
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flask import Flask, jsonify

from hub import auth
from hub import events
from hub import state as store
from hub import task_store
from hub.bootstrap import create_app
from hub.config import FleetConfig
from tests.test_session_ingest_api import _event, _key

TOKEN = "ingest-secret"
RUNNER = {"mac-a": "secret-a", "mac-b": "secret-b"}


def _report(machine):
    return {"machine": machine, "agents": {}, "system": {}}


class _AppBase(unittest.TestCase):
    ingest_token = TOKEN

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.old = (store.STATE_DIR, events.EVENT_LOG, task_store.DB_PATH)
        store.STATE_DIR = self.temp_dir
        events.EVENT_LOG = self.temp_dir / "events.jsonl"
        task_store.DB_PATH = self.temp_dir / "fleet.db"
        task_store.init_db()
        config = FleetConfig.from_root(
            self.temp_dir, ingest_token=self.ingest_token,
            dev_operator="op@example.com", runner_credentials=dict(RUNNER),
            project_whitelist={"mac-a": ["agent-fleet"]},
            session_repositories_enabled=True,
            session_db=self.temp_dir / "var" / "sessions" / "meta.db",
            session_transcript_root=self.temp_dir / "var" / "sessions" / "t",
            session_encryption_raw=_key())
        with mock.patch.object(auth, "resolve_ingest_token", return_value=None):
            self.app = create_app(config)
        self.client = self.app.test_client()

    def tearDown(self):
        store.STATE_DIR, events.EVENT_LOG, task_store.DB_PATH = self.old

    def ingest(self, body, runner=None, token=TOKEN):
        headers = {"X-Agent-Fleet-Token": token}
        if runner:
            headers["X-Runner-Credential"] = runner
        return self.client.post("/api/ingest", json=body, headers=headers)


class IngestFailClosedTests(_AppBase):
    ingest_token = None

    def test_no_token_configured_is_503(self):
        with mock.patch.dict(os.environ, {auth.ALLOW_INSECURE_INGEST_ENV: ""}):
            for path, body in (("/api/ingest", _report("mac-a")),
                               ("/api/session-events", [_event(1)]),
                               ("/api/scan", None)):
                resp = self.client.post(path, json=body)
                self.assertEqual(resp.status_code, 503, path)
                self.assertEqual(resp.get_json()["error"],
                                 "ingest_token_not_configured")

    def test_env_dev_flag_opens_ingest(self):
        with mock.patch.dict(os.environ, {auth.ALLOW_INSECURE_INGEST_ENV: "1"}):
            resp = self.client.post("/api/ingest", json=_report("mac-a"))
        self.assertEqual(resp.status_code, 200)


class IngestMachineBindingTests(_AppBase):
    def observe(self):
        return self.app.extensions["fleet"]["services"]["observe"]

    def test_credential_must_match_payload_machine(self):
        resp = self.ingest(_report("mac-b"), runner="mac-a:secret-a")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json()["error"], "machine_mismatch")
        self.assertIsNone(store.read_current("mac-b"))

    def test_invalid_credential_is_403_even_with_valid_token(self):
        resp = self.ingest(_report("mac-a"), runner="mac-a:wrong")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json()["error"], "forbidden")

    def test_shared_token_cannot_overwrite_credential_bound_machine(self):
        self.assertEqual(
            self.ingest(_report("mac-a"), runner="mac-a:secret-a").status_code, 200)
        self.assertTrue(self.observe().is_credential_bound("mac-a"))
        resp = self.ingest(_report("mac-a"))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json()["error"], "machine_bound")
        # the bound node itself keeps reporting
        self.assertEqual(
            self.ingest(_report("mac-a"), runner="mac-a:secret-a").status_code, 200)

    def test_shared_token_stays_compatible_and_is_recorded(self):
        self.assertEqual(self.ingest(_report("mac-b")).status_code, 200)
        self.assertEqual(store.read_current("mac-b")["ingest_auth"], "shared_token")
        # never exposed on the public read model
        status = self.client.get("/api/status").get_json()
        self.assertNotIn("ingest_auth", json.dumps(status))

    def test_session_events_bound_to_credential_machine(self):
        headers = {"X-Agent-Fleet-Token": TOKEN,
                   "X-Runner-Credential": "mac-a:secret-a"}
        resp = self.client.post("/api/session-events",
                                json=[_event(1, machine="mac-b")], headers=headers)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json()["error"], "machine_mismatch")
        resp = self.client.post("/api/session-events",
                                json=[_event(1, machine="mac-a")], headers=headers)
        self.assertEqual(resp.status_code, 200)

    def test_shared_token_session_events_refused_for_bound_machine(self):
        self.ingest(_report("mac-a"), runner="mac-a:secret-a")
        resp = self.client.post("/api/session-events",
                                json=[_event(1, machine="mac-a")],
                                headers={"X-Agent-Fleet-Token": TOKEN})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json()["error"], "machine_bound")


class CredentialStoreFailureTests(unittest.TestCase):
    def _app(self):
        app = Flask(__name__)
        app.config["RUNNER_CREDENTIALS"] = None
        app.config["SUPERVISOR_CREDENTIALS"] = None

        @app.route("/r", methods=["POST"])
        @auth.require_runner
        def r():
            return jsonify(ok=True)

        @app.route("/s", methods=["POST"])
        @auth.require_supervisor
        def s():
            return jsonify(ok=True)
        return app

    def test_unreadable_runner_store_is_503_not_403(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "runner.json"
            bad.write_text("{not json")
            with mock.patch.object(auth, "RUNNER_CREDENTIALS_FILE", bad):
                resp = self._app().test_client().post(
                    "/r", headers={"X-Runner-Credential": "mac-a:x"})
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json()["error"], "credential_store_unavailable")

    def test_missing_runner_store_is_still_403(self):
        with mock.patch.object(auth, "RUNNER_CREDENTIALS_FILE",
                               Path("/nonexistent/runner.json")):
            resp = self._app().test_client().post(
                "/r", headers={"X-Runner-Credential": "mac-a:x"})
        self.assertEqual(resp.status_code, 403)

    def test_unreadable_supervisor_store_is_503(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "credentials").mkdir()
            (Path(tmp) / "credentials" / "supervisor-credentials.json").write_text("[")
            with mock.patch.object(auth, "FLEET_HOME", Path(tmp)), \
                    mock.patch.dict(os.environ, {}, clear=False) as env:
                env.pop("AGENT_FLEET_SUPERVISOR_CREDENTIALS", None)
                resp = self._app().test_client().post(
                    "/s", headers={"X-Supervisor-Credential": "mac-a:x"})
        self.assertEqual(resp.status_code, 503)

    def test_platform_store_error_is_503(self):
        from hub.infrastructure.platform_db import PlatformRepositoryError

        class _Broken:
            def authenticate_node_credential(self, node_id, secret):
                raise PlatformRepositoryError("platform_store")

        app = Flask(__name__)
        app.extensions["fleet"] = {"platform_repository": _Broken()}

        @app.route("/p", methods=["POST"])
        @auth.require_platform_node
        def p():
            return jsonify(ok=True)
        resp = app.test_client().post(
            "/p", headers={"X-Platform-Node-Credential": "node-1:secret"})
        self.assertEqual(resp.status_code, 503)


class MachineAuthMarkerTests(unittest.TestCase):
    def test_marker_does_not_leak_through_outer_wraps(self):
        @auth.require_runner
        def inner():
            return None

        def outer_decorator(view):
            @functools.wraps(view)
            def wrapper(*a, **kw):
                return view(*a, **kw)
            return wrapper

        outer = outer_decorator(inner)
        self.assertTrue(auth.is_machine_auth_view(inner))
        self.assertFalse(auth.is_machine_auth_view(outer))
        self.assertFalse(auth.is_machine_auth_view(None))

    def test_every_registered_machine_view_is_explicitly_marked(self):
        from hub import web
        app = web.make_app(ingest_token="t", dev_operator="op@example.com",
                           runner_credentials={"mac-a": "x"})
        marked = {name for name, view in app.view_functions.items()
                  if auth.is_machine_auth_view(view)}
        self.assertIn("observe.api_ingest", marked)
        self.assertIn("sessions.api_session_events", marked)
        self.assertNotIn("observe.api_status", marked)


if __name__ == "__main__":
    unittest.main()
