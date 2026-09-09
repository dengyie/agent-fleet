"""HTTP adapter contract tests: unified error shape, opaque request_id, auth matrix.

Covers the Task 8 thin-adapter contract:
- ``ApplicationError(code, detail, status=400)`` and ``error_response(error,
  request_id)`` produce ``{ok:false,error,detail,request_id}`` and nothing else.
- ``detail`` is bounded and never carries exception reprs, absolute paths, SQL,
  or credentials.
- Errors surfaced through the API have the stable public shape and a per-request
  opaque id that is unique between requests.
- Auth domains stay mutually exclusive at the HTTP boundary.
"""
import tempfile
import unittest
from pathlib import Path

from hub import events
from hub import state as store
from hub import task_store
from hub.bootstrap import create_app


def make_test_client(dev_operator="op@example.com"):
    """Build a test app/client against a temporary sandbox."""
    temp_dir = Path(tempfile.mkdtemp())
    old_state = store.STATE_DIR
    old_events = events.EVENT_LOG
    old_db = task_store.DB_PATH
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
        dev_operator=dev_operator,
        runner_credentials={"mac-local": "test-only"},
        project_whitelist={"mac-local": ["agent-fleet"]},
    )
    client = app.test_client()
    client._fleet_test_cleanup = (old_state, old_events, old_db)
    return client


class HttpContractTestBase(unittest.TestCase):
    def setUp(self):
        self.client = make_test_client()

    def tearDown(self):
        old_state, old_events, old_db = self.client._fleet_test_cleanup
        store.STATE_DIR = old_state
        events.EVENT_LOG = old_events
        task_store.DB_PATH = old_db


class ErrorContractTests(HttpContractTestBase):
    def test_application_error_has_stable_public_shape(self):
        # 未认证 POST /api/tasks（关闭 DEV fallback）→ 401，统一错误外型
        self.client.application.config["DEV_OPERATOR"] = None
        response = self.client.post("/api/tasks", json={})
        self.assertEqual(response.status_code, 401)
        body = response.get_json()
        self.assertEqual(set(body), {"ok", "error", "detail", "request_id"})
        self.assertFalse(body["ok"])

    def test_validation_error_has_stable_public_shape(self):
        response = self.client.post("/api/tasks", json={
            "machine": "mac-local", "agent_type": "bash",
            "project": "agent-fleet", "instruction": "x"})
        self.assertEqual(response.status_code, 400)
        body = response.get_json()
        self.assertEqual(set(body), {"ok", "error", "detail", "request_id"})
        self.assertEqual(body["error"], "invalid_agent_type")
        self.assertIsInstance(body["request_id"], str)
        self.assertTrue(body["request_id"])

    def test_no_repr_no_path_no_sql_no_credential_in_errors(self):
        response = self.client.post("/api/tasks", json={
            "machine": "../../etc/passwd", "agent_type": "codex",
            "project": "agent-fleet", "instruction": "x"})
        self.assertEqual(response.status_code, 400)
        body = response.get_json()
        text = " ".join(str(v) for v in body.values())
        self.assertNotIn("/etc/passwd", text)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("sqlite", text.lower())
        self.assertNotIn("test-only", text)

    def test_request_id_opaque_and_unique(self):
        one = self.client.post("/api/tasks", json={
            "machine": "../x", "agent_type": "codex",
            "project": "agent-fleet", "instruction": "x"}).get_json()
        two = self.client.post("/api/tasks", json={
            "machine": "../y", "agent_type": "codex",
            "project": "agent-fleet", "instruction": "x"}).get_json()
        self.assertNotEqual(one["request_id"], two["request_id"])
        self.assertNotIn("/", one["request_id"])
        self.assertNotIn("..", two["request_id"])

    def test_ingest_and_observe_errors_are_bounded(self):
        resp = self.client.post("/api/ingest", json={"machine": "../escaped",
                                                     "agents": {}},
                                headers={"X-Agent-Fleet-Token": "test-only"})
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertEqual(body["error"], "invalid_machine")
        self.assertEqual(body["detail"], "machine 名不合法")
        text = " ".join(str(v) for v in body.values())
        self.assertNotIn("/", text)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("ObserveError", text)

    def test_malformed_ingest_json_uses_canonical_error_code(self):
        resp = self.client.post(
            "/api/ingest",
            data=b"{not-json",
            content_type="application/json",
            headers={"X-Agent-Fleet-Token": "test-only"},
        )
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertEqual(body["error"], "invalid_json")
        self.assertEqual(body["detail"], "")

    def test_non_object_ingest_json_uses_canonical_error_code(self):
        resp = self.client.post(
            "/api/ingest",
            json=["not", "an", "object"],
            headers={"X-Agent-Fleet-Token": "test-only"},
        )
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertEqual(body["error"], "invalid_json")
        self.assertEqual(body["detail"], "")

    def test_machine_endpoint_404_stays_unified(self):
        resp = self.client.get("/api/machines/ghost")
        self.assertEqual(resp.status_code, 404)
        body = resp.get_json()
        self.assertEqual(set(body), {"ok", "error", "detail", "request_id"})
        self.assertFalse(body["ok"])

    def test_pages_error_responses_are_plain_text(self):
        self.assertEqual(self.client.get("/machine/ghost").status_code, 404)
        self.assertEqual(self.client.get("/machine/ghost").get_data(as_text=True),
                         "machine not found")
        self.assertEqual(self.client.get("/task/ghost").status_code, 404)
        self.assertEqual(self.client.get("/task/ghost").get_data(as_text=True),
                         "task not found")


class ApiEdgeErrorContractTests(HttpContractTestBase):
    """Task 11: unknown /api/* 404/405 and unexpected API exceptions use bounded JSON.

    Page routes keep their compatibility plain-text behavior. Raw Flask HTML
    error pages must never leak for API clients.
    """

    def assert_bounded_api_error(self, resp, status, expected_error):
        self.assertEqual(resp.status_code, status)
        self.assertEqual(resp.content_type.split(";")[0], "application/json")
        body = resp.get_json()
        self.assertEqual(set(body), {"ok", "error", "detail", "request_id"})
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], expected_error)
        self.assertLessEqual(len(body["detail"]), 200)
        self.assertTrue(body["request_id"])
        # no raw repr / path / SQL / credential leak
        text = " ".join(str(v) for v in body.values())
        self.assertNotIn("Traceback", text)
        self.assertNotIn(".py", text)
        self.assertNotIn("sqlite", text.lower())
        self.assertNotIn("/etc", text)

    def test_unknown_api_route_404_is_bounded_json(self):
        resp = self.client.get("/api/definitely-unknown")
        self.assert_bounded_api_error(resp, 404, "not_found")

    def test_unknown_api_route_post_method_not_allowed_is_bounded_json(self):
        resp = self.client.post("/api/definitely-unknown", json={})
        self.assert_bounded_api_error(resp, 405, "method_not_allowed")

    def test_method_not_allowed_on_api_route_is_bounded_json(self):
        resp = self.client.delete("/api/tasks")
        self.assert_bounded_api_error(resp, 405, "method_not_allowed")

    def test_unexpected_exception_on_api_is_bounded_json(self):
        # Break the injected service the same way a programmer bug would.
        bad = type("BadObserve", (), {
            "status": lambda self: (_ for _ in ()).throw(RuntimeError("raw internal detail"))})()
        self.client.application.extensions["fleet"]["services"]["observe"] = bad
        resp = self.client.get("/api/status")
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.content_type.split(";")[0], "application/json")
        body = resp.get_json()
        self.assertEqual(set(body), {"ok", "error", "detail", "request_id"})
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "internal_error")
        text = " ".join(str(v) for v in body.values())
        self.assertNotIn("boom", text)
        self.assertNotIn("RuntimeError", text)
        self.assertNotIn("Traceback", text)
        self.assertTrue(body["request_id"])

    def test_unexpected_exception_on_page_route_is_plain_text(self):
        from unittest import mock
        with mock.patch("hub.http.pages.build_summary",
                        side_effect=RuntimeError("page boom")):
            resp = self.client.get("/")
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_data(as_text=True), "internal error")

    def test_unknown_page_404_remains_plain_text(self):
        resp = self.client.get("/no-such-page")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_data(as_text=True), "not found")

    def test_internal_error_response_never_exposes_app_secrets(self):
        # DEV_OPERATOR / credentials are not in any bounded 404 body.
        resp = self.client.get("/api/no-such-path")
        text = " ".join(str(v) for v in resp.get_json().values())
        self.assertNotIn("op@example.com", text)
        self.assertNotIn("test-only", text)


class SessionV1AliasAndAuthMatrixTests(unittest.TestCase):
    """Task 6: /api/v1/session-events adapter + matrix for the session surface.

    The session routes are optional (disabled by default).  Assert the v1
    adapter reuses the same service surface and that foreign-domain headers
    never grant access across the observe/operator boundary in either version.
    """

    def setUp(self):
        import tempfile
        from pathlib import Path as _P

        from hub import web
        from hub.config import FleetConfig

        self._tmp = _P(tempfile.mkdtemp())
        self._old_state = store.STATE_DIR
        self._old_events = events.EVENT_LOG
        self._old_db = task_store.DB_PATH
        store.STATE_DIR = self._tmp
        events.EVENT_LOG = self._tmp / "events.jsonl"
        task_store.DB_PATH = self._tmp / "fleet.db"
        task_store.init_db()
        store.save_snapshot("mac-local", {
            "machine": "mac-local", "source": "ingest", "reachable": True,
            "agents": {}, "system": {},
        })
        cfg = FleetConfig.from_root(
            self._tmp,
            ingest_token="test-only",
            dev_operator="op@example.com",
            runner_credentials={"mac-local": "test-only"},
            project_whitelist={"mac-local": ["agent-fleet"]},
            session_repositories_enabled=True,
            session_db=self._tmp / "var" / "sessions" / "meta.db",
            session_transcript_root=self._tmp / "var" / "sessions" / "tr",
            session_encryption_raw=b"\x01" * 32,
        )
        self.app = create_app(cfg)
        self.client = self.app.test_client()

    def tearDown(self):
        store.STATE_DIR = self._old_state
        events.EVENT_LOG = self._old_events
        task_store.DB_PATH = self._old_db

    def _evt(self, seq):
        return {
            "schema_version": 1,
            "event_id": f"evt_hc_{seq:03d}",
            "stream_id": "stream_hc_1",
            "machine_id": "mac-local",
            "session_id": "sess_hc_1",
            "attempt_id": "att_hc_1",
            "process_group_id": "grp_hc_1",
            "sequence": seq,
            "kind": "user_message",
            "capture_quality": "structured",
            "source": "bridge",
            "emitted_at": "2026-08-26T00:00:00Z",
            "payload": {"text": f"msg {seq}", "is_complete": True},
        }

    def test_v1_session_events_equivalent_to_old(self):
        old = self.client.post("/api/session-events", json=[self._evt(1)],
                              headers={"X-Agent-Fleet-Token": "test-only"})
        self.assertEqual(old.status_code, 200)
        old_body = old.get_json()
        new = self.client.post("/api/v1/session-events", json=[self._evt(2)],
                              headers={"X-Agent-Fleet-Token": "test-only"})
        self.assertEqual(new.status_code, 200)
        new_body = new.get_json()
        self.assertEqual(set(old_body) - {"request_id"},
                         set(new_body) - {"request_id"})
        self.assertEqual(old_body["accepted_through"], 1)
        self.assertEqual(new_body["accepted_through"], 2)

    def test_session_ingest_foreign_headers_denied(self):
        for headers in (
            {"X-Runner-Credential": "mac-local:test-only"},
            {"Cf-Access-Authenticated-User-Email": "op@example.com"},
        ):
            for url in ("/api/session-events", "/api/v1/session-events"):
                resp = self.client.post(url, json=[self._evt(1)], headers=headers)
                self.assertEqual(resp.status_code, 403, (url, headers))

    def test_session_query_foreign_headers_no_dev_fallback(self):
        for headers in (
            {"X-Runner-Credential": "mac-local:test-only"},
            {"X-Agent-Fleet-Token": "test-only"},
        ):
            resp = self.client.get("/api/sessions", headers=headers)
            self.assertEqual(resp.status_code, 401, headers)

    def test_no_supervisor_surface_in_v1(self):
        from hub import web as web_module
        rules = {r.rule for r in self.app.url_map.iter_rules()}
        self.assertNotIn("/api/v1/supervisor/poll", rules)
        self.assertNotIn("/api/v1/supervisor/receipts", rules)


class AuthMatrixTests(unittest.TestCase):
    def setUp(self):
        self._clients = []

    def tearDown(self):
        for client in self._clients:
            old_state, old_events, old_db = client._fleet_test_cleanup
            store.STATE_DIR = old_state
            events.EVENT_LOG = old_events
            task_store.DB_PATH = old_db

    def _make(self, dev_operator="op@example.com"):
        client = make_test_client(dev_operator)
        self._clients.append(client)
        return client

    def test_ingest_header_cannot_fallback_to_dev_operator(self):
        client = self._make(dev_operator="dev@example.com")
        response = client.post("/api/tasks", json={},
                               headers={"X-Agent-Fleet-Token": "test-only"})
        self.assertEqual(response.status_code, 401)

    def test_runner_header_cannot_call_operator_task_api(self):
        client = self._make(dev_operator=None)
        response = client.post("/api/tasks", json={},
                               headers={"X-Runner-Credential": "hk:test-only"})
        self.assertEqual(response.status_code, 401)

    def test_ingest_header_cannot_poll_commands_as_runner(self):
        client = self._make()
        response = client.post("/api/commands/poll", json={},
                               headers={"X-Agent-Fleet-Token": "test-only"})
        self.assertEqual(response.status_code, 403)

    def test_operator_header_cannot_poll_commands(self):
        client = self._make(dev_operator=None)
        response = client.post("/api/commands/poll", json={},
                               headers={"Cf-Access-Authenticated-User-Email": "x@y.z"})
        self.assertEqual(response.status_code, 403)


class VersionedApiTests(HttpContractTestBase):
    """Task 18: /api/v1 compat adapters reuse the same views/DTOs as /api/*.

    These tests assert only the versioned *surface*: the v1 routes must return
    the exact public shapes, stable error fields, and same auth-domain guards
    as the authoritative legacy routes. They deliberately do not re-test
    service behavior (already covered by the service suites).
    """

    def test_status_old_and_v1_use_same_public_shape(self):
        old = self.client.get("/api/status").get_json()
        new = self.client.get("/api/v1/status").get_json()
        self.assertEqual(set(old), set(new))
        self.assertTrue(old["machines"])
        self.assertEqual(set(old["machines"][0]), set(new["machines"][0]))

    def test_machine_detail_old_and_v1_use_same_public_shape(self):
        old = self.client.get("/api/machines/mac-local").get_json()
        new = self.client.get("/api/v1/machines/mac-local").get_json()
        self.assertEqual(set(old), set(new))
        self.assertEqual(old["machine"], new["machine"])
        self.assertEqual(set(old["current"]), set(new["current"]))

    def test_v1_machine_not_found_stays_unified(self):
        resp = self.client.get("/api/v1/machines/ghost")
        self.assertEqual(resp.status_code, 404)
        body = resp.get_json()
        self.assertEqual(set(body), {"ok", "error", "detail", "request_id"})
        self.assertFalse(body["ok"])

    def test_events_old_and_v1_use_same_public_shape(self):
        old = self.client.get("/api/events").get_json()
        new = self.client.get("/api/v1/events").get_json()
        self.assertEqual(set(old), set(new))
        self.assertEqual(old["events"], new["events"])

    def test_v1_stream_uses_sse_transport_headers(self):
        resp = self.client.get("/api/v1/stream", buffered=False)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/event-stream", resp.content_type)
        self.assertEqual(resp.headers.get("Cache-Control"), "no-cache")
        self.assertEqual(resp.headers.get("X-Accel-Buffering"), "no")
        body = b""
        for chunk in resp.response:
            body += chunk
            if b": connected" in body:
                break
        resp.close()
        self.assertIn(b": connected", body)

    def test_v1_task_error_preserves_stable_fields(self):
        response = self.client.post("/api/v1/tasks", json={})
        self.assertEqual(response.status_code, 400)
        body = response.get_json()
        self.assertFalse(body["ok"])
        self.assertIn("error", body)
        self.assertIn("detail", body)
        self.assertIn("request_id", body)

    def test_v1_task_validation_error_matches_old(self):
        body = {"machine": "../x", "agent_type": "bash",
                "project": "agent-fleet", "instruction": "x"}
        old = self.client.post("/api/tasks", json=body).get_json()
        new = self.client.post("/api/v1/tasks", json=body).get_json()
        self.assertEqual(old["error"], new["error"])
        self.assertEqual(old["detail"], new["detail"])
        self.assertEqual(new["error"], "invalid_machine")
        self.assertIn("request_id", new)

    def test_v1_task_crud_matches_old(self):
        payload = {"machine": "mac-local", "agent_type": "codex",
                   "project": "agent-fleet", "instruction": "hello"}
        resp = self.client.post("/api/v1/tasks", json=payload)
        self.assertEqual(resp.status_code, 201)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        tid = body["task"]["task_id"]
        # list contains it
        listed = self.client.get("/api/v1/tasks").get_json()["tasks"]
        self.assertIn(tid, [t["task_id"] for t in listed])
        # detail DTO matches legacy
        old = self.client.get(f"/api/tasks/{tid}").get_json()
        new = self.client.get(f"/api/v1/tasks/{tid}").get_json()
        self.assertEqual(set(old["task"]), set(new["task"]))
        self.assertEqual(new["task"]["state"], "queued")
        self.assertNotIn("attempt_id", new["task"])
        self.assertNotIn("client_token", new["task"])
        # cancel + retry on v1
        cancel = self.client.post(f"/api/v1/tasks/{tid}/cancel").get_json()
        self.assertTrue(cancel["changed"])
        self.assertEqual(cancel["task"]["state"], "cancelled")
        retry = self.client.post(f"/api/v1/tasks/{tid}/retry").get_json()
        self.assertTrue(retry["changed"])
        self.assertEqual(retry["task"]["state"], "queued")
        # not-found keeps unified shape
        nf = self.client.post("/api/v1/tasks/t-missing/cancel")
        self.assertEqual(nf.status_code, 404)
        self.assertFalse(nf.get_json()["ok"])

    def test_v1_task_requires_operator_identity(self):
        client = make_test_client(dev_operator=None)
        try:
            resp = client.post("/api/v1/tasks", json={})
            self.assertEqual(resp.status_code, 401)
            body = resp.get_json()
            self.assertEqual(set(body), {"ok", "error", "detail", "request_id"})
            self.assertFalse(body["ok"])
            # runner credential header cannot impersonate operator on v1
            resp = client.post("/api/v1/tasks", json={},
                               headers={"X-Runner-Credential": "mac-local:test-only"})
            self.assertEqual(resp.status_code, 401)
        finally:
            old_state, old_events, old_db = client._fleet_test_cleanup
            store.STATE_DIR = old_state
            events.EVENT_LOG = old_events
            task_store.DB_PATH = old_db

    def test_v1_foreign_identity_header_blocks_dev_operator(self):
        # DEV_OPERATOR is set; a foreign ingest token must still not grant
        # operator access on the task surface.
        resp = self.client.post("/api/v1/tasks", json={},
                                headers={"X-Agent-Fleet-Token": "test-only"})
        self.assertEqual(resp.status_code, 401)

    def test_v1_observe_surface_is_public(self):
        self.assertEqual(self.client.get("/api/v1/status").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/machines/mac-local").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/events").status_code, 200)

    def test_runner_v1_commands_remain_unregistered(self):
        # Old /api/commands/* stays authoritative; the /api/v1 runner surface
        # is intentionally not exposed by Task 18. Assert no v1 command URL
        # rule exists AND any request fails closed with a bounded JSON error.
        rules = {r.rule for r in self.client.application.url_map.iter_rules()}
        self.assertNotIn("/api/v1/commands/poll", rules)
        self.assertNotIn("/api/v1/commands/<attempt_id>/heartbeat", rules)
        self.assertNotIn("/api/v1/commands/<attempt_id>/result", rules)
        # a stray runner request fails closed (bounded non-ok JSON error)
        resp = self.client.post("/api/v1/commands/poll", json={})
        self.assertIn(resp.status_code, (404, 405))
        body = resp.get_json()
        self.assertFalse(body["ok"])
        self.assertIn("request_id", body)


class SameOriginProxyContractTests(HttpContractTestBase):
    """Task 20: same-origin routing config keeps the HTTP-layer contract.

    The Nginx example (same-origin frontend/backend split) must preserve the
    invariants the adapter already enforces: ``/api/stream`` is never proxied
    with buffering re-enabled, the release never introduces wildcard CORS, and
    the hub itself never emits an ``Access-Control-Allow-Origin`` header
    (same-origin: no CORS was ever needed).
    """

    def test_nginx_example_rejects_proxy_buffering_on(self):
        source = Path('deploy/nginx-frontend-backend.example.conf').read_text()
        self.assertIn('location /api/stream', source)
        self.assertIn('proxy_buffering off', source)
        self.assertNotIn('proxy_buffering on', source)

    def test_nginx_example_rejects_wildcard_cors(self):
        source = Path('deploy/nginx-frontend-backend.example.conf').read_text()
        self.assertNotIn('Access-Control-Allow-Origin', source)
        self.assertNotIn('access-control-allow-origin', source.lower())

    def test_nginx_example_keeps_stream_http_version_and_long_timeout(self):
        source = Path('deploy/nginx-frontend-backend.example.conf').read_text()
        start = source.index('location /api/stream')
        block = source[start:source.index('\n    }', start)]
        self.assertIn('proxy_http_version 1.1', block)
        self.assertIn('proxy_read_timeout', block)
        timeout = [tok for tok in ('3600s', '1800s', '600s')
                   if f'proxy_read_timeout {tok}' in block]
        self.assertTrue(timeout, 'SSE read timeout must be long')
        self.assertIn('proxy_set_header Connection', block)

    def test_nginx_example_routes_frontend_and_backend_same_origin(self):
        source = Path('deploy/nginx-frontend-backend.example.conf').read_text()
        self.assertIn('upstream fleet_frontend', source)
        self.assertIn('upstream fleet_backend', source)
        self.assertIn('location /assets/', source)
        self.assertIn('location /api/', source)

    def test_hub_responses_emit_no_cors_headers(self):
        # Same-origin contract: the API must never hand out ACAO anywhere.
        for url in ('/api/status', '/api/events', '/', '/machine/mac-local'):
            resp = self.client.get(url)
            self.assertNotIn('Access-Control-Allow-Origin', resp.headers, url)

    def test_legacy_stream_still_disables_buffering(self):
        resp = self.client.get('/api/stream', buffered=False)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get('X-Accel-Buffering'), 'no')
        self.assertEqual(resp.headers.get('Cache-Control'), 'no-cache')
        body = b""
        for chunk in resp.response:
            body += chunk
            if b": connected" in body:
                break
        resp.close()
        self.assertIn(b": connected", body)


if __name__ == "__main__":
    unittest.main()