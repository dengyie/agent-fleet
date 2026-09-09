"""Transport-layer auth identity extraction matrix tests.

Covers the Task 8 contract for the transport-only identity helpers:
``extract_operator_identity(request) -> str | None``,
``extract_runner_identity(request) -> tuple[str, str] | None``, and
``extract_ingest_token(request) -> str``.

The three auth domains stay mutually exclusive at the HTTP boundary: carrying a
foreign-domain credential header must never cause a DEV_OPERATOR fallback, and
runner identity must only be minted from a valid runner credential.
"""
import tempfile
import unittest
from pathlib import Path

from flask import g, request
from hub import auth
from hub import events
from hub import state as store
from hub import task_store


def _make_app():
    temp_dir = Path(tempfile.mkdtemp())
    old = (store.STATE_DIR, events.EVENT_LOG, task_store.DB_PATH)
    store.STATE_DIR = temp_dir
    events.EVENT_LOG = temp_dir / "events.jsonl"
    task_store.DB_PATH = temp_dir / "fleet.db"
    task_store.init_db()
    store.save_snapshot("mac-local", {
        "machine": "mac-local", "source": "ingest", "reachable": True,
        "agents": {}, "system": {},
    })
    from hub import web
    app = web.make_app(
        ingest_token="test-only",
        dev_operator="dev@example.com",
        runner_credentials={"mac-local": "test-only"},
        project_whitelist={"mac-local": ["agent-fleet"]},
    )
    app._fleet_test_cleanup = old
    return app


class ExtractIdentityTests(unittest.TestCase):
    def setUp(self):
        self._apps = []

    def tearDown(self):
        for app in self._apps:
            store.STATE_DIR, events.EVENT_LOG, task_store.DB_PATH = \
                app._fleet_test_cleanup

    def _app(self):
        app = _make_app()
        self._apps.append(app)
        return app

    def test_extract_operator_from_cf_header(self):
        app = self._app()
        with app.test_request_context(
                "/api/tasks",
                headers={"Cf-Access-Authenticated-User-Email": "op@example.com"}):
            self.assertEqual(auth.extract_operator_identity(request), "op@example.com")

    def test_extract_operator_dev_fallback_and_blank(self):
        app = self._app()
        with app.test_request_context("/api/tasks"):
            # 无任何身份头 → DEV_OPERATOR 兜底
            self.assertEqual(auth.extract_operator_identity(), "dev@example.com")
        app.config["DEV_OPERATOR"] = None
        with app.test_request_context("/api/tasks"):
            self.assertIsNone(auth.extract_operator_identity())

    def test_foreign_ingest_token_blocks_dev_fallback(self):
        app = self._app()
        with app.test_request_context("/api/tasks",
                                      headers={"X-Agent-Fleet-Token": "test-only"}):
            self.assertIsNone(auth.extract_operator_identity())

    def test_foreign_runner_header_blocks_dev_fallback(self):
        app = self._app()
        with app.test_request_context("/api/tasks",
                                      headers={"X-Runner-Credential": "mac-local:test-only"}):
            self.assertIsNone(auth.extract_operator_identity())

    def test_extract_runner_identity_valid(self):
        app = self._app()
        with app.test_request_context("/api/commands/poll",
                                      headers={"X-Runner-Credential": "mac-local:test-only"}):
            self.assertEqual(auth.extract_runner_identity(), ("mac-local", "test-only"))
            self.assertEqual(g.runner_machine, "mac-local")

    def test_extract_runner_identity_invalid_returns_none(self):
        app = self._app()
        with app.test_request_context("/api/commands/poll",
                                      headers={"X-Runner-Credential": "boom"}):
            self.assertIsNone(auth.extract_runner_identity())

    def test_extract_ingest_token(self):
        app = self._app()
        with app.test_request_context("/api/ingest",
                                      headers={"X-Agent-Fleet-Token": "test-only"}):
            self.assertEqual(auth.extract_ingest_token(), "test-only")
        with app.test_request_context("/api/ingest"):
            self.assertEqual(auth.extract_ingest_token(), "")


class SupervisorIdentityBoundaryTests(unittest.TestCase):
    """Task 9 supervisor boundary — auth-domains stay mutually exclusive.

    ``hub.web.make_app`` has no supervisor surface by scope; these tests
    exercise the extraction primitives directly (the primitives read
    ``app.config["SUPERVISOR_CREDENTIALS"]``), so the supervisor credential
    works exactly like the runner credential at the transport boundary and can
    never be minted from — nor mint — a foreign domain identity.
    """

    CREDENTIALS = {"mac-local": "sup-secret"}

    def setUp(self):
        self._apps = []
        self.app = _make_app()
        self.app.config["SUPERVISOR_CREDENTIALS"] = dict(self.CREDENTIALS)
        self._apps.append(self.app)

    def tearDown(self):
        for app in self._apps:
            store.STATE_DIR, events.EVENT_LOG, task_store.DB_PATH = \
                app._fleet_test_cleanup

    def test_valid_supervisor_credential_extracts_identity(self):
        with self.app.test_request_context(
                "/api/supervisor/poll",
                headers={"X-Supervisor-Credential": "mac-local:sup-secret"}):
            self.assertEqual(auth.extract_supervisor_identity(),
                             ("mac-local", "sup-secret"))
            self.assertEqual(g.supervisor_machine, "mac-local")

    def test_invalid_supervisor_credential_returns_none(self):
        for bad in ("mac-local:wrong", "no-separator", "", "a:b:c",
                    "mac local:sup-secret"):
            with self.app.test_request_context(
                    "/api/supervisor/poll",
                    headers={"X-Supervisor-Credential": bad}):
                self.assertIsNone(auth.extract_supervisor_identity())

    def test_runner_credential_cannot_extract_supervisor_identity(self):
        with self.app.test_request_context(
                "/api/supervisor/poll",
                headers={"X-Runner-Credential": "mac-local:test-only"}):
            self.assertIsNone(auth.extract_supervisor_identity())

    def test_ingest_token_cannot_extract_supervisor_identity(self):
        with self.app.test_request_context(
                "/api/supervisor/poll",
                headers={"X-Agent-Fleet-Token": "test-only"}):
            self.assertIsNone(auth.extract_supervisor_identity())

    def test_operator_header_cannot_extract_supervisor_identity(self):
        with self.app.test_request_context(
                "/api/supervisor/poll",
                headers={"Cf-Access-Authenticated-User-Email": "o@example.com"}):
            self.assertIsNone(auth.extract_supervisor_identity())

    def test_supervisor_header_blocks_dev_operator_fallback(self):
        with self.app.test_request_context(
                "/api/tasks",
                headers={"X-Supervisor-Credential": "mac-local:sup-secret"}):
            self.assertIsNone(auth.extract_operator_identity())

    def test_supervisor_header_cannot_extract_runner_identity(self):
        with self.app.test_request_context(
                "/api/commands/poll",
                headers={"X-Supervisor-Credential": "mac-local:sup-secret"}):
            self.assertIsNone(auth.extract_runner_identity())

    def test_supervisor_credential_holds_no_ingest_meaning(self):
        with self.app.test_request_context(
                "/api/ingest",
                headers={"X-Supervisor-Credential": "mac-local:sup-secret"}):
            self.assertEqual(auth.extract_ingest_token(), "")


if __name__ == "__main__":
    unittest.main()