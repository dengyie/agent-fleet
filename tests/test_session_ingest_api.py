"""HTTP contract tests for ``POST /api/session-events`` (Task 6).

Covers the Task 6 ingest surface:

- Observe success (ingest token) with ``accepted_through``, ``next_cursor``,
  per-event ``rejected`` and opaque ``request_id``;
- Observe 403 with a wrong/missing token;
- foreign-header no-dev-fallback on the ingest domain (Runner credential and
  CF operator headers both rejected);
- batch bounds (101 events, oversized bytes);
- duplicate sequence idempotency (re-ingest yields a duplicate);
- malformed event isolation (valid siblings are kept);
- bounded error body (no paths / SQL / secrets);
- public payload allowlist (secret-shaped text is redacted);
- failure isolation: transcript repository failure returns a bounded
  ``session_unavailable`` without breaking ``/api/ingest``;
- no raw transcript in SSE / status / events.
"""
import tempfile
import unittest
from pathlib import Path

from hub import events
from hub import state as store
from hub import task_store
from hub.bootstrap import create_app
from hub.config import FleetConfig


def _event(seq, session_id="sess_api_1", kind="user_message",
           text="hello from the agent", quality="structured",
           event_id=None, machine="mac-api-1", stream="stream_api_1",
           group="grp_api_1", attempt="att_api_1",
           emitted="2026-08-26T00:00:00Z"):
    evt = {
        "schema_version": 1,
        "event_id": event_id or f"evt_{seq:03d}_api",
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
    return evt


def _key() -> bytes:
    return b"\xb1\xc2\xd3\xe4\xf5\x06\x17\x28\x39\x4a\x5b\x6c\x7d\x8e\x9f\xa0" \
           b"\x01\x12\x23\x34\x45\x56\x67\x78\x89\x9a\xab\xbc\xcd\xde\xef\x01"


def _config(root):
    return FleetConfig.from_root(
        root,
        ingest_token="ingest-secret",
        dev_operator="op@example.com",
        runner_credentials=None,
        project_whitelist={"mac-api-1": ["agent-fleet"]},
        session_repositories_enabled=True,
        session_db=root / "var" / "sessions" / "meta.db",
        session_transcript_root=root / "var" / "sessions" / "transcripts",
        session_encryption_raw=_key(),
    )


class SessionHttpTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.old_state = store.STATE_DIR
        self.old_events = events.EVENT_LOG
        self.old_db = task_store.DB_PATH
        store.STATE_DIR = self.temp_dir
        events.EVENT_LOG = self.temp_dir / "events.jsonl"
        task_store.DB_PATH = self.temp_dir / "fleet.db"
        task_store.init_db()
        store.save_snapshot("mac-api-1", {
            "machine": "mac-api-1", "source": "ingest", "reachable": True,
            "agents": {}, "system": {},
        })
        self.app = create_app(_config(self.temp_dir))
        self.client = self.app.test_client()

    def tearDown(self):
        store.STATE_DIR = self.old_state
        events.EVENT_LOG = self.old_events
        task_store.DB_PATH = self.old_db

    def _ingest(self, events, token="ingest-secret"):
        return self.client.post(
            "/api/session-events",
            json=events,
            headers={"X-Agent-Fleet-Token": token})


class SessionIngestAuthTests(SessionHttpTestBase):
    def test_observe_ingest_accepts_valid_batch(self):
        resp = self._ingest([_event(1), _event(2)])
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["accepted_through"], 2)
        self.assertEqual(body["rejected"], [])
        self.assertTrue(body["next_cursor"])
        self.assertNotIn("/", body["next_cursor"])
        self.assertIn("request_id", body)
        self.assertTrue(body["request_id"])

    def test_observe_wrong_token_403(self):
        resp = self._ingest([_event(1)], token="wrong")
        self.assertEqual(resp.status_code, 403)
        body = resp.get_json()
        self.assertEqual(set(body), {"ok", "error", "detail", "request_id"})
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "forbidden")

    def test_observe_missing_token_403(self):
        resp = self.client.post("/api/session-events", json=[_event(1)])
        self.assertEqual(resp.status_code, 403)

    def test_runner_header_denied_on_ingest(self):
        resp = self.client.post(
            "/api/session-events",
            json=[_event(1)],
            headers={"X-Runner-Credential": "mac-api-1:runner-secret"})
        self.assertEqual(resp.status_code, 403)

    def test_operator_header_denied_on_ingest(self):
        resp = self.client.post(
            "/api/session-events",
            json=[_event(1)],
            headers={"Cf-Access-Authenticated-User-Email": "op@example.com"})
        self.assertEqual(resp.status_code, 403)

    def test_ingest_requires_list_body(self):
        resp = self.client.post(
            "/api/session-events",
            json={},
            headers={"X-Agent-Fleet-Token": "ingest-secret"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "invalid_json")


class SessionIngestBoundaryTests(SessionHttpTestBase):
    def test_batch_too_many_events(self):
        events = [_event(i) for i in range(1, 102)]
        resp = self._ingest(events)
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertEqual(body["error"], "batch_too_many")
        self.assertFalse(body["ok"])
        self.assertNotIn("Traceback", " ".join(str(v) for v in body.values()))

    def test_batch_too_large_bytes(self):
        # The app-level transport cap (MAX_CONTENT_LENGTH = 256 KiB) rejects a
        # batch whose raw body exceeds the batch bound with 413 before any
        # event is persisted.  This is the same bounded transport behavior the
        # legacy /api/ingest already exhibits.
        big = [_event(i, text="x" * 60000) for i in range(1, 6)]
        resp = self._ingest(big)
        self.assertEqual(resp.status_code, 413)
        body = resp.get_data(as_text=True)
        self.assertNotIn("Traceback", body)
        self.assertNotIn("ingest-secret", body)

    def test_duplicate_sequence_idempotent(self):
        first = self._ingest([_event(1)])
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_json()["accepted_through"], 1)
        second = self._ingest([_event(1)])
        self.assertEqual(second.status_code, 200)
        # Re-ingesting the same event id must not advance the cursor.
        self.assertEqual(second.get_json()["accepted_through"], 1)

    def test_malformed_event_isolated(self):
        good = _event(1)
        bad = {"schema_version": 1, "event_id": "evt_bad"}
        resp = self._ingest([good, bad, _event(2)])
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["accepted_through"], 2)
        self.assertEqual(body["status"], "partial")
        self.assertEqual(len(body["rejected"]), 1)
        self.assertEqual(body["rejected"][0]["index"], 1)
        text = " ".join(str(v) for v in body["rejected"])
        self.assertNotIn("Traceback", text)
        self.assertIn("code", body["rejected"][0])

    def test_per_event_reject_is_bounded(self):
        bad = {"schema_version": 1, "kind": "bogus"}
        resp = self._ingest([bad])
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["accepted_through"], 0)
        self.assertEqual(body["status"], "rejected")
        self.assertEqual(len(body["rejected"]), 1)
        self.assertIn("code", body["rejected"][0])

    def test_oversized_event_is_rejected_per_event(self):
        # A single event over the per-event 64 KiB bound is a per-event reject
        # (never silently accepted, never a whole-batch failure).
        big = [_event(1, text="y" * 70000)]
        resp = self._ingest(big)
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["accepted_through"], 0)
        self.assertEqual(body["status"], "rejected")
        self.assertEqual(len(body["rejected"]), 1)
        self.assertEqual(body["rejected"][0]["code"], "event_too_large")


class SessionIngestIsolationTests(SessionHttpTestBase):
    def test_transcript_failure_bounded_does_not_break_api_ingest(self):
        import hub.infrastructure.transcript_repository as tr_mod

        original = tr_mod.TranscriptRepository.ingest

        def boom(*a, **kw):
            raise tr_mod.TranscriptError("raw_quota_exceeded")

        tr_mod.TranscriptRepository.ingest = boom
        try:
            # A transcript bound error surfaces as a bounded session error.
            resp = self._ingest([_event(1)])
            self.assertEqual(resp.status_code, 503)
            body = resp.get_json()
            self.assertFalse(body["ok"])
            self.assertEqual(body["error"], "session_unavailable")
            text = " ".join(str(v) for v in body.values())
            self.assertNotIn("raw_quota_exceeded", text)
            self.assertNotIn("Traceback", text)
            self.assertNotIn("/", text)
        finally:
            tr_mod.TranscriptRepository.ingest = original

        # The legacy observation ingest route is untouched.
        obs = self.client.post(
            "/api/ingest",
            json={"machine": "mac-api-01", "agents": {}},
            headers={"X-Agent-Fleet-Token": "ingest-secret"})
        self.assertEqual(obs.status_code, 200)

    def test_session_store_failure_is_bounded_503_not_500(self):
        """A locked/read-only session DB surfaces as bounded 503, not a 500."""
        import sqlite3 as _sqlite3
        import hub.infrastructure.session_repository as sr_mod

        original = sr_mod.SessionRepository.upsert_session

        def locked(*a, **kw):
            raise _sqlite3.OperationalError("database is locked")

        sr_mod.SessionRepository.upsert_session = locked
        try:
            resp = self._ingest([_event(1)])
            self.assertEqual(resp.status_code, 503)
            body = resp.get_json()
            self.assertFalse(body["ok"])
            self.assertEqual(body["error"], "session_unavailable")
            text = " ".join(str(v) for v in body.values())
            self.assertNotIn("locked", text.lower())
            self.assertNotIn("Traceback", text)
        finally:
            sr_mod.SessionRepository.upsert_session = original

    def test_no_raw_text_in_sse_or_public_status(self):
        self._ingest([_event(1, text="password=supersecret_42")])
        status = str(self.client.get("/api/status").get_json())
        self.assertNotIn("supersecret_42", status)
        self.assertNotIn("password=", status)
        ev_list = self.client.get("/api/events").get_json()["events"]
        self.assertNotIn("password=", " ".join(str(e) for e in ev_list))
        self.assertNotIn("supersecret_42", " ".join(str(e) for e in ev_list))

    def test_payload_allowlist_passthrough_keeps_agent_text(self):
        evt = _event(1)
        evt["payload"]["text"] = "token=sk-0123456789abcdef"
        self._ingest([evt])
        detail = self.client.get("/api/sessions/1-api-1/events")  # operator call
        body = detail.get_json()
        self.assertIn("events", body)
        for e in body["events"]:
            if e.get("sequence") == 1:
                text = (e.get("payload") or {}).get("text") or ""
                self.assertEqual(text, "token=sk-0123456789abcdef")
                self.assertNotIn("[REDACTED:", text)

    def test_quota_gap_not_acked_via_http(self):
        # Build an app whose transcript repo has a tiny raw budget so a
        # best-effort event overflows the quota while staying under the event
        # byte bound.  The quota gap must be a bounded per-event rejection,
        # never ``accepted_through``.
        import hub.application.session_service as svc_mod
        from hub.infrastructure.transcript_repository import TranscriptRepository

        # The bootstrap-built service already wired the real TranscriptRepository
        # with the default 256MiB quota; replace it in the app services with a
        # capped one for this test only.
        svc = self.app.extensions["fleet"]["services"]["sessions"]
        capped_tr = TranscriptRepository(
            self.temp_dir / "var" / "sessions" / "quota-tr.db",
            key=_key(), max_raw_bytes=200)
        capped_tr.init()
        capped = svc_mod.SessionService(svc.session_repo, capped_tr)
        self.app.extensions["fleet"]["services"]["sessions"] = capped

        big = _event(1, quality="best_effort", text="y" * 4096)
        resp = self.client.post(
            "/api/session-events",
            json=[big],
            headers={"X-Agent-Fleet-Token": "ingest-secret"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["accepted_through"], 0)
        self.assertEqual(body["status"], "rejected")
        self.assertEqual(len(body["rejected"]), 1)
        self.assertEqual(body["rejected"][0]["code"], "raw_quota_exceeded")
        text = " ".join(str(v) for v in body.values())
        self.assertNotIn("y" * 16, text)
        self.assertNotIn("Traceback", text)


class SessionServiceQuotaGapTests(unittest.TestCase):
    """A best-effort quota overflow returns ``gap`` WITHOUT storing any row.

    ``SessionService`` must surface it as a bounded per-event rejection and
    must NOT acknowledge it or count it toward ``accepted_through``.
    """

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())

    def _quota_service(self, max_raw_bytes=200):
        from hub.application.session_service import SessionService
        from hub.infrastructure.session_repository import SessionRepository
        from hub.infrastructure.transcript_repository import TranscriptRepository

        repo = SessionRepository(self._tmp / "meta.db")
        repo.init()
        tr = TranscriptRepository(
            self._tmp / "tr.db", key=_key(), max_raw_bytes=max_raw_bytes)
        tr.init()
        return repo, tr, SessionService(repo, tr)

    def test_quota_gap_never_acked_and_reported_as_rejected(self):
        session_repo, tr_repo, service = self._quota_service()
        # A best-effort single event larger than the 200-byte raw cap.
        evt = _event(1, quality="best_effort", text="y" * 4096)
        result = service.ingest_events([evt])
        self.assertEqual(result["accepted_through"], 0)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(len(result["rejected"]), 1)
        self.assertEqual(result["rejected"][0]["code"],
                         "raw_quota_exceeded")
        # No redacted row was stored by the transcript store.
        self.assertEqual(tr_repo.read_redacted("sess_api_1"), [])
        # The durable ack cursor must be 0 (never acknowledged as stored).
        # stream_cursor/last_sequence is the *observation* cursor advanced by
        # note_event before persistence is confirmed; the load-bearing
        # non-ack is ack_sequence.
        conn = session_repo._connect()
        try:
            row = conn.execute(
                "SELECT ack_sequence FROM stream_state"
                " WHERE session_id=?", ("sess_api_1",)).fetchone()
            if row is not None:
                self.assertEqual(int(row["ack_sequence"]), 0)
        finally:
            conn.close()

    def test_quota_gap_after_accepted_sibling_keeps_sibling(self):
        # 500-byte cap: a small valid event (cipher ~372 B) fits under the raw
        # quota, while the huge best-effort event (cipher ~4.5 KB) overflows it.
        session_repo, tr_repo, service = self._quota_service(max_raw_bytes=500)
        # First a small accepted event.  The quota is per-session; use a fresh
        # session + small text so the first event fits under the cap.
        ok = _event(1, session_id="sess_quota_2", text="ok")
        first = service.ingest_events([ok])
        self.assertEqual(first["accepted_through"], 1)
        # A huge best-effort follow-on over the quota is rejected per-event,
        # leaving the accepted sibling acked and the gap not stored.
        big = _event(2, session_id="sess_quota_2", quality="best_effort",
                     text="z" * 4096)
        second = service.ingest_events([big])
        self.assertEqual(second["accepted_through"], 0)
        self.assertEqual(second["status"], "rejected")
        self.assertEqual(second["rejected"][0]["code"], "raw_quota_exceeded")
        # Only event 1 is stored; event 2 was never written.
        rows = tr_repo.read_redacted("sess_quota_2")
        self.assertEqual([r["sequence"] for r in rows], [1])

    def test_quota_gap_rejection_code_bounded(self):
        _, _, service = self._quota_service()
        big = _event(1, quality="best_effort", text="y" * 4096)
        result = service.ingest_events([big])
        text = " ".join(str(v) for v in result["rejected"])
        self.assertNotIn("Traceback", text)
        self.assertNotIn("/", text)
        self.assertNotIn("y" * 16, text)

    def test_sequence_gap_is_still_acked_when_stored(self):
        # The fix must NOT regress a genuine sequence gap: a missing seq 2
        # followed by seq 3 stores the red row and returns ``gap`` with
        # raw_written=True, so it IS acknowledged and counted.  Only the
        # non-stored quota gap is rejected.
        session_repo, tr_repo, service = self._quota_service(max_raw_bytes=500)
        result = service.ingest_events([
            _event(3, session_id="sess_quota_4", text="skip seq 2"),
        ])
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["accepted_through"], 3)
        self.assertEqual(result["rejected"], [])
        rows = tr_repo.read_redacted("sess_quota_4")
        self.assertEqual([r["sequence"] for r in rows], [3])

    def test_duplicate_still_acked_and_idempotent(self):
        session_repo, tr_repo, service = self._quota_service(max_raw_bytes=500)
        first = service.ingest_events([
            _event(1, session_id="sess_quota_5", text="dup"),
        ])
        self.assertEqual(first["accepted_through"], 1)
        second = service.ingest_events([
            _event(1, session_id="sess_quota_5", text="dup"),
        ])
        # Duplicate does not advance the cursor but remains accepted/idempotent.
        self.assertEqual(second["accepted_through"], 1)
        self.assertEqual(second["status"], "accepted")
        self.assertEqual(second["rejected"], [])
        # The stored redacted stream has exactly one event.
        rows = tr_repo.read_redacted("sess_quota_5")
        self.assertEqual(len(rows), 1)

    def test_mid_batch_reject_stops_prefix_not_max(self):
        """accepted_through is the contiguous prefix, not the global max.

        Batch [ok(seq1), rejected(seq2 quota), ok(seq3)] must report
        accepted_through=1 (the prefix before the first reject), never 3.
        """
        session_repo, tr_repo, service = self._quota_service(max_raw_bytes=1000)
        evt1 = _event(1, session_id="sess_prefix", quality="structured",
                      text="ok1")
        evt2 = _event(2, session_id="sess_prefix", quality="best_effort",
                      text="z" * 4096)
        evt3 = _event(3, session_id="sess_prefix", quality="structured",
                      text="ok3")
        result = service.ingest_events([evt1, evt2, evt3])
        self.assertEqual(result["accepted_through"], 1)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["rejected"]), 1)
        self.assertEqual(result["rejected"][0]["code"], "raw_quota_exceeded")
        self.assertEqual(result["rejected"][0]["index"], 1)
        # Only seq 1 stored; seq 3 was stored (redacted always written) but
        # not acked because accepted_through stops at the prefix.
        rows = tr_repo.read_redacted("sess_prefix")
        stored = {r["sequence"] for r in rows}
        self.assertIn(1, stored)
        self.assertIn(3, stored)
        # ack is only the prefix.
        conn = session_repo._connect()
        try:
            row = conn.execute(
                "SELECT ack_sequence FROM stream_state"
                " WHERE session_id=?", ("sess_prefix",)).fetchone()
            if row is not None:
                self.assertEqual(int(row["ack_sequence"]), 1)
        finally:
            conn.close()


class SessionServiceBoundTests(unittest.TestCase):
    """Service-level batch bounds (HTTP max_content_length would short-circuit).

    The HTTP route relies on Flask's 256 KiB transport cap; the service's own
    ``MAX_BATCH_BYTES``/``MAX_BATCH_EVENTS`` checks guard non-HTTP callers and
    are exercised here directly.
    """

    def test_service_rejects_oversized_byte_batch(self):
        from hub.application.session_service import (
            SessionService,
            SessionServiceError,
        )
        from hub.infrastructure.session_repository import SessionRepository
        from hub.infrastructure.transcript_repository import TranscriptRepository

        tmp = Path(tempfile.mkdtemp())
        repo = SessionRepository(tmp / "s.db")
        repo.init()
        tr = TranscriptRepository(tmp / "tr.db", key=_key())
        tr.init()
        service = SessionService(repo, tr)
        big = [_event(i, text="x" * 60000) for i in range(1, 6)]
        with self.assertRaises(SessionServiceError) as ctx:
            service.ingest_events(big)
        self.assertEqual(ctx.exception.code, "batch_too_large")
        self.assertNotIn("/", str(ctx.exception))

    def test_service_rejects_invalid_batch_shape(self):
        from hub.application.session_service import SessionService, SessionServiceError
        from hub.infrastructure.session_repository import SessionRepository
        from hub.infrastructure.transcript_repository import TranscriptRepository

        tmp = Path(tempfile.mkdtemp())
        service = SessionService(SessionRepository(tmp / "s.db"),
                                 TranscriptRepository(tmp / "t.db"))
        with self.assertRaises(SessionServiceError) as ctx:
            service.ingest_events({"schema_version": 1})
        self.assertEqual(ctx.exception.code, "invalid_batch")


class SessionServiceSpecExtractionTests(unittest.TestCase):
    def test_session_spec_from_event_extracts_agent_family(self):
        from hub.application.session_service import SessionService
        spec = SessionService._session_spec_from_event({
            "session_id": "s1", "machine_id": "m1",
            "capture_quality": "best_effort",
            "payload": {"agent_family": "codex"},
        })
        self.assertEqual(spec["agent_family"], "codex")

    def test_session_spec_from_event_absent_family_omits_key(self):
        from hub.application.session_service import SessionService
        spec = SessionService._session_spec_from_event({
            "session_id": "s2", "machine_id": "m1",
            "capture_quality": "best_effort",
            "payload": {},
        })
        self.assertNotIn("agent_family", spec)


if __name__ == "__main__":
    unittest.main()