"""评审修复 #11：共享请求体助手 hub/http/_common.py 与 /api 400/413 JSON 契约。"""
import tempfile
import unittest
from pathlib import Path

from flask import Flask
from werkzeug.exceptions import BadRequest

from hub import events
from hub import state as store
from hub import task_store
from hub import web
from hub.http import _common
from hub.http import (
    adoption_routes, conversation_routes, memory_routes, node_routes,
    platform_routes, schedule_routes, service_action_routes, service_routes,
    supervisor_routes,
)
from hub.http.errors import ApplicationError

TOKEN = "ingest-secret"


class CommonHelperTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)

    def ctx(self, data=None, content_type="application/json"):
        return self.app.test_request_context(
            "/api/x", method="POST", data=data, content_type=content_type)

    def test_json_object_requires_object(self):
        with self.ctx('{"a": 1}'):
            self.assertEqual(_common.json_object(), {"a": 1})
        for raw in (None, "[1]", "not json"):
            with self.ctx(raw):
                with self.assertRaises(ApplicationError) as cm:
                    _common.json_object()
                self.assertEqual(cm.exception.code, "invalid_json")

    def test_json_object_or_empty(self):
        with self.ctx(None):
            self.assertEqual(_common.json_object_or_empty(), {})
        with self.ctx("[1]"):
            with self.assertRaises(ApplicationError):
                _common.json_object_or_empty()

    def test_json_object_or_error_tuple(self):
        with self.ctx('{"k": "v"}'):
            self.assertEqual(_common.json_object_or_error(), ({"k": "v"}, None))
        with self.ctx("[1]"):
            body, err = _common.json_object_or_error()
            self.assertIsNone(body)
            self.assertEqual(err[1], 400)
            self.assertEqual(err[0].get_json()["error"], "invalid_json")

    def test_route_modules_share_helpers(self):
        for mod in (conversation_routes, memory_routes, schedule_routes, service_routes):
            self.assertIs(mod._body, _common.json_object)
        self.assertIs(platform_routes._json_object, _common.json_object)
        for mod in (node_routes, service_action_routes):
            self.assertIs(mod._body, _common.json_object_or_empty)
        for mod in (adoption_routes, supervisor_routes):
            self.assertIs(mod._json_object, _common.json_object_or_error)


class ApiErrorContractTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.old = (store.STATE_DIR, events.EVENT_LOG, task_store.DB_PATH)
        store.STATE_DIR = self.temp_dir
        events.EVENT_LOG = self.temp_dir / "events.jsonl"
        task_store.DB_PATH = self.temp_dir / "fleet.db"
        task_store.init_db()
        self.app = web.make_app(ingest_token=TOKEN, dev_operator="op@example.com")

    def tearDown(self):
        store.STATE_DIR, events.EVENT_LOG, task_store.DB_PATH = self.old

    def test_oversized_api_body_is_json_413(self):
        resp = self.app.test_client().post(
            "/api/ingest", data=b"x" * (self.app.config["MAX_CONTENT_LENGTH"] + 10),
            content_type="application/json",
            headers={"X-Agent-Fleet-Token": TOKEN})
        self.assertEqual(resp.status_code, 413)
        body = resp.get_json()
        self.assertEqual(body["error"], "payload_too_large")
        self.assertFalse(body["ok"])
        self.assertTrue(body["request_id"])

    def test_bad_request_on_api_is_json_400_without_parser_detail(self):
        def boom():
            raise BadRequest("Failed to decode JSON object: secret-ish detail")
        self.app.add_url_rule("/api/_review_bad", "review_bad", boom, methods=["POST"])
        self.app.add_url_rule("/_review_bad_page", "review_bad_page", boom, methods=["POST"])
        client = self.app.test_client()
        resp = client.post("/api/_review_bad")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "invalid_json")
        self.assertNotIn("secret-ish", resp.get_data(as_text=True))
        page = client.post("/_review_bad_page")
        self.assertEqual(page.status_code, 400)
        self.assertIsNone(page.get_json(silent=True))


if __name__ == "__main__":
    unittest.main()
