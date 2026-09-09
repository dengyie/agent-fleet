"""HTTP contract tests for the session Operator query API (Task 6).

Covers:
- GET /api/sessions (list), GET /api/sessions/<id>, GET
  /api/sessions/<id>/events, GET /api/sessions/<id>/policy-signals
- Operator 401 without identity; foreign-header (ingest/runner) no-dev-fallback;
- Runner denial on session query routes;
- bounded error body (404 not_found, invalid_session_id);
- public DTO allowlist (managed/unmanaged, capture_quality, control_capability);
- no raw transcript appears in redacted event responses;
- event stream ordering and limit;
- v1 adapter equivalence (no supervisor route).
"""
import tempfile
import unittest
from pathlib import Path

from hub import events
from hub import state as store
from hub import task_store
from hub.bootstrap import create_app
from hub.config import FleetConfig


def _event(seq, session_id="sess_q_1", kind="user_message",
           text="hello", event_id=None, machine="mac-q-1",
           stream="stream_q_1", group="grp_q_1", attempt="att_q_1",
           emitted="2026-08-26T00:00:00Z", quality="structured"):
    evt = {
        "schema_version": 1,
        "event_id": event_id or f"evt_q_{seq:03d}",
        "stream_id": stream,
        "machine_id": machine,
        "session_id": session_id,
        "attempt_id": attempt,
        "process_group_id": group,
        "sequence": seq,
        "kind": kind,
        "capture_quality": quality,
        "source": "bridge",
        "emitted_at": emitted,
    }
    if kind in ("user_message", "assistant_message"):
        evt["payload"] = {"text": text, "is_complete": True}
    elif kind == "session_start":
        evt["payload"] = {"agent_family": "codex", "adapter": "codex_stream"}
    elif kind == "tool_call":
        evt["payload"] = {"tool_name": "bash", "call_id": "call_1",
                          "arguments": "ls -la", "result": "",
                          "status": "ok", "digest": "d1"}
    return evt


def _key():
    return b"\xb1\xc2\xd3\xe4\xf5\x06\x17\x28\x39\x4a\x5b\x6c\x7d\x8e\x9f\xa0" \
           b"\x01\x12\x23\x34\x45\x56\x67\x78\x89\x9a\xab\xbc\xcd\xde\xef\x01"


def _config(root, dev_operator="op@example.com"):
    return FleetConfig.from_root(
        root,
        ingest_token="ingest-secret",
        dev_operator=dev_operator,
        runner_credentials=None,
        project_whitelist={"mac-q-1": ["agent-fleet"]},
        session_repositories_enabled=True,
        session_db=root / "var" / "sessions" / "meta.db",
        session_transcript_root=root / "var" / "sessions" / "transcripts",
        session_encryption_raw=_key(),
    )


class SessionQueryTestBase(unittest.TestCase):
    dev_operator = "op@example.com"

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.old_state = store.STATE_DIR
        self.old_events = events.EVENT_LOG
        self.old_db = task_store.DB_PATH
        store.STATE_DIR = self.temp_dir
        events.EVENT_LOG = self.temp_dir / "events.jsonl"
        task_store.DB_PATH = self.temp_dir / "fleet.db"
        task_store.init_db()
        store.save_snapshot("mac-q-1", {
            "machine": "mac-q-1", "source": "ingest", "reachable": True,
            "agents": {}, "system": {},
        })
        self.app = create_app(_config(self.temp_dir, self.dev_operator))
        self.client = self.app.test_client()
        self._seed()

    def _seed(self):
        # Seeding uses the ingest surface (Observe domain) - no operator
        # identity needed.  ``sess_q_1`` gets a bounded event stream; ``sess_q_2``
        # carries a policy signal index.
        for evt in (_event(1), _event(2, text="second message"),
                    _event(3, kind="session_start"),
                    _event(4, kind="tool_call"),
                    _event(1, session_id="sess_q_2", kind="session_start")):
            resp = self.client.post(
                "/api/session-events", json=[evt],
                headers={"X-Agent-Fleet-Token": "ingest-secret"})
            assert resp.status_code == 200, (evt.get("event_id"), resp.get_json())
        from hub.infrastructure.transcript_repository import TranscriptRepository
        tr = TranscriptRepository(
            self.temp_dir / "var" / "sessions" / "transcripts", key=_key())
        tr.append_policy_signal("sess_q_2", "warn", "llm_uncertain")
        tr.append_policy_signal("sess_q_2", "info", "supervisor_orphan")

    def tearDown(self):
        store.STATE_DIR = self.old_state
        events.EVENT_LOG = self.old_events
        task_store.DB_PATH = self.old_db


class SessionQueryAuthTests(SessionQueryTestBase):
    dev_operator = None  # no DEV fallback: all session queries require identity

    def test_list_sessions_requires_operator(self):
        resp = self.client.get("/api/sessions")
        self.assertEqual(resp.status_code, 401)

    def test_foreign_ingest_header_no_dev_fallback(self):
        resp = self.client.get(
            "/api/sessions",
            headers={"X-Agent-Fleet-Token": "ingest-secret"})
        self.assertEqual(resp.status_code, 401)

    def test_runner_header_no_dev_fallback(self):
        resp = self.client.get(
            "/api/sessions",
            headers={"X-Runner-Credential": "mac-q-1:runner-secret"})
        self.assertEqual(resp.status_code, 401)

    def test_events_endpoint_requires_operator(self):
        resp = self.client.get("/api/sessions/sess_q_1/events")
        self.assertEqual(resp.status_code, 401)

    def test_policy_signals_requires_operator(self):
        resp = self.client.get("/api/sessions/sess_q_2/policy-signals")
        self.assertEqual(resp.status_code, 401)

    def test_operator_header_grants_access(self):
        resp = self.client.get(
            "/api/sessions",
            headers={"Cf-Access-Authenticated-User-Email": "op@example.com"})
        self.assertEqual(resp.status_code, 200)


class SessionQueryDtoTests(SessionQueryTestBase):
    def test_list_sessions_succeeds_and_maps_dto(self):
        resp = self.client.get("/api/sessions")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIn("sessions", body)
        ids = {s.get("session_id") for s in body["sessions"]}
        self.assertIn("sess_q_1", ids)
        self.assertIn("sess_q_2", ids)

    def test_list_public_dto_allowlist(self):
        body = self.client.get("/api/sessions").get_json()
        session = next(s for s in body["sessions"]
                       if s.get("session_id") == "sess_q_1")
        self.assertNotIn("pid", session)
        self.assertNotIn("command", session)
        self.assertNotIn("token", session)
        self.assertNotIn("secret", session)
        self.assertNotIn("/", session.get("session_id", ""))
        self.assertNotIn("/", session.get("machine_id", ""))
        self.assertEqual(session["capture_quality"], "structured")
        self.assertEqual(session["control_capability"], "available")

    def test_detail_maps_public_dto(self):
        resp = self.client.get("/api/sessions/sess_q_1")
        self.assertEqual(resp.status_code, 200)
        session = resp.get_json()["session"]
        self.assertEqual(session["session_id"], "sess_q_1")
        self.assertEqual(session["machine_id"], "mac-q-1")
        self.assertTrue(session["managed"])
        self.assertEqual(session["control_capability"], "available")

    def test_detail_404_bounded(self):
        resp = self.client.get("/api/sessions/no-such-session")
        self.assertEqual(resp.status_code, 404)
        body = resp.get_json()
        self.assertEqual(set(body), {"ok", "error", "detail", "request_id"})
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "session_not_found")
        text = " ".join(str(v) for v in body.values())
        self.assertNotIn("Traceback", text)
        self.assertNotIn("/", text)

    def test_events_endpoint_maps_bounded_dto(self):
        resp = self.client.get("/api/sessions/sess_q_1/events")
        self.assertEqual(resp.status_code, 200)
        events_ = resp.get_json()["events"]
        self.assertEqual(len(events_), 4)
        self.assertEqual([e["sequence"] for e in events_], [1, 2, 3, 4])
        self.assertEqual([e["kind"] for e in events_],
                         ["user_message", "user_message", "session_start",
                          "tool_call"])
        for ev in events_:
            self.assertIn("event_id", ev)
            self.assertIn("session_id", ev)
            self.assertIn("emitted_at", ev)
            self.assertNotIn("pid", ev)
            self.assertNotIn("command", ev)
            if "payload" in ev:
                for key in ev["payload"]:
                    self.assertNotIn(key, ("pid", "command", "token", "secret",
                                           "raw_output", "env", "cwd"))

    def test_events_limit_bounded(self):
        resp = self.client.get("/api/sessions/sess_q_1/events?limit=2")
        events_ = resp.get_json()["events"]
        self.assertLessEqual(len(events_), 2)

    def test_policy_signals_bounded(self):
        resp = self.client.get("/api/sessions/sess_q_2/policy-signals")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIn("signals", body)
        reasons = [s.get("reason") for s in body["signals"]]
        self.assertIn("llm_uncertain", reasons)
        self.assertNotIn("password", str(body))
        self.assertNotIn("secret", str(body))

    def test_session_event_payload_is_passthrough(self):
        # Session query DTO keeps agent text; observation status stays metadata-only.
        evt = {"schema_version": 1, "event_id": "e_red_1",
               "stream_id": "stream_q_1", "machine_id": "mac-q-1",
               "session_id": "sess_q_3", "attempt_id": "att_q_1",
               "process_group_id": "grp_q_1", "sequence": 1,
               "kind": "user_message", "capture_quality": "structured",
               "source": "bridge", "emitted_at": "2026-08-26T00:00:00Z",
               "payload": {"text": "token=sk-ABCDEF", "is_complete": True}}
        self.client.post("/api/session-events", json=[evt],
                         headers={"X-Agent-Fleet-Token": "ingest-secret"})
        body = self.client.get("/api/sessions/sess_q_3/events").get_json()
        self.assertIn("sk-ABCDEF", str(body))
        self.assertNotIn("[REDACTED:", str(body))

    def test_unmanaged_session_dto_best_effort(self):
        evt = _event(1, session_id="sess_unmanaged", group=None, attempt=None,
                     quality="best_effort", event_id="evt_unmanaged_1")
        evt = {k: v for k, v in evt.items() if k not in ("process_group_id",
                                                          "attempt_id")}
        self.client.post("/api/session-events", json=[evt],
                         headers={"X-Agent-Fleet-Token": "ingest-secret"})
        dto = self.client.get("/api/sessions/sess_unmanaged").get_json()["session"]
        self.assertFalse(dto["managed"])
        self.assertEqual(dto["capture_quality"], "best_effort")
        self.assertEqual(dto["control_capability"], "unavailable")


class SessionQueryV1Tests(SessionQueryTestBase):
    def test_v1_list_matches_old_shape(self):
        old = self.client.get("/api/sessions").get_json()
        new = self.client.get("/api/v1/sessions").get_json()
        self.assertEqual(set(old), set(new))
        self.assertEqual(
            [s.get("session_id") for s in old["sessions"]],
            [s.get("session_id") for s in new["sessions"]])

    def test_v1_detail_matches_old(self):
        old = self.client.get("/api/sessions/sess_q_1").get_json()
        new = self.client.get("/api/v1/sessions/sess_q_1").get_json()
        self.assertEqual(set(old), set(new))
        self.assertEqual(old["session"], new["session"])

    def test_v1_events_matches_old(self):
        old = self.client.get("/api/sessions/sess_q_1/events").get_json()
        new = self.client.get("/api/v1/sessions/sess_q_1/events").get_json()
        self.assertEqual(set(old), set(new))
        self.assertEqual(old["events"], new["events"])

    def test_v1_policy_signals_equivalent(self):
        old = self.client.get("/api/sessions/sess_q_2/policy-signals").get_json()
        new = self.client.get("/api/v1/sessions/sess_q_2/policy-signals").get_json()
        self.assertEqual(set(old), set(new))
        self.assertEqual(old["signals"], new["signals"])

    def test_v1_supervisor_route_absent(self):
        rules = {r.rule for r in self.app.url_map.iter_rules()}
        self.assertNotIn("/api/v1/supervisor/poll", rules)
        self.assertNotIn("/api/supervisor/poll", rules)
        self.assertNotIn("/api/v1/sessions/<session_id>/control", rules)
        self.assertNotIn("/api/sessions/<session_id>/control", rules)

    def test_v1_foreign_header_blocks_dev_fallback(self):
        client = self.app.test_client()
        resp = client.get(
            "/api/v1/sessions",
            headers={"X-Agent-Fleet-Token": "ingest-secret"})
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()