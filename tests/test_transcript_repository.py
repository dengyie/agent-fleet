"""Tests for the independent hub TranscriptRepository (Task 5).

The transcript repository owns the Hub's bounded redacted event stream plus
the encrypted restricted raw stream, with metadata/raw retention, quota
rotation, and an audit index for restricted reads and control receipts.

Coverage (from the Task 5 brief):

- ``ingest(event) -> IngestResult`` with accepted/duplicate/rejected/gap
  states;
- the store is an additive durable structure (never writes transcript
  payloads into legacy observation/event/task stores);
- redacted data is queryable without a special key;
- raw data requires the restricted AEAD key (fail closed: a repo without a
  key cannot read raw; a wrong key cannot decrypt);
- retention purges only eligible RAW rows (never redacted rows, never
  ineligible raw rows) and audits the purge WITHOUT deleted content;
- raw-quota overflow signals an explicit capture-gap (best-effort) or a
  bounded rejection (exact/structured) -- never a silent drop;
- a malformed record is rejected per-event and never poisons later records;
- a raw-storage write failure is isolated: the redacted stream stays
  available and legacy observation repository calls remain successful;
- restricted raw reads are audited without logging the content;
- repository errors carry bounded codes and never encode paths or exception
  text.
"""

import json  # noqa: F401 (kept for parity with sibling test files)
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hub.infrastructure.state_repository import JsonlObservationRepository
from hub.infrastructure.transcript_repository import (
    SCHEMA_VERSION,
    IngestResult,
    TranscriptError,
    TranscriptRepository,
)


def _key() -> bytes:
    return b"\xb1\xc2\xd3\xe4\xf5\x06\x17\x28\x39\x4a\x5b\x6c\x7d\x8e\x9f\xa0" \
           b"\x01\x12\x23\x34\x45\x56\x67\x78\x89\x9a\xab\xbc\xcd\xde\xef\x01"


def _event(seq=1, session_id=None, kind="user_message", quality="structured",
           text=None, event_id=None):
    evt = {
        "schema_version": 1,
        "event_id": event_id or f"evt_{seq:03d}",
        "stream_id": "stream_t5_1",
        "machine_id": "host-t5-1",
        "session_id": session_id or "sess_t5_tr1",
        "attempt_id": "att_t5_1",
        "process_group_id": "grp_t5_1",
        "sequence": seq,
        "kind": kind,
        "capture_quality": quality,
        "source": "bridge",
        "emitted_at": "2026-08-26T00:00:00Z",
    }
    if text is not None:
        payload = {"text": text, "is_complete": True}
        if kind == "tool_call":
            payload = {"tool_name": "bash", "call_id": "call_1",
                       "arguments": text, "result": "", "status": "ok"}
        evt["payload"] = payload
    return evt


class TranscriptRepositoryBasicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "transcripts.db"
        self.repo = TranscriptRepository(self.db, key=_key())
        self.repo.init()

    def test_schema_version_recorded(self):
        self.assertEqual(SCHEMA_VERSION, 1)
        self.assertEqual(self.repo.schema_version(), 1)

    def test_ingest_accepts_valid_event(self):
        result = self.repo.ingest(_event(seq=1))
        self.assertEqual(result.status, "accepted")
        self.assertIsNone(result.reason)
        self.assertEqual(result.event_id, "evt_001")
        self.assertTrue(result.raw_written)
        rows = self.repo.read_redacted("sess_t5_tr1")
        self.assertEqual(len(rows), 1)
        self.assertIn("payload", rows[0])
        self.assertEqual(rows[0]["sequence"], 1)

    def test_ingest_duplicate_is_idempotent(self):
        first = self.repo.ingest(_event(seq=1))
        self.assertEqual(first.status, "accepted")
        second = self.repo.ingest(_event(seq=1))
        self.assertEqual(second.status, "duplicate")
        self.assertEqual(second.event_id, "evt_001")
        self.assertFalse(second.raw_written)
        self.assertEqual(len(self.repo.read_redacted("sess_t5_tr1")), 1)

    def test_same_event_id_across_sessions_is_not_a_duplicate(self):
        # Dedupe is per-session: two sessions may share an event_id without
        # the second being silently dropped.
        a = self.repo.ingest(_event(seq=1, session_id="sess_a",
                                    event_id="evt_shared"))
        b = self.repo.ingest(_event(seq=1, session_id="sess_b",
                                    event_id="evt_shared"))
        self.assertEqual(a.status, "accepted")
        self.assertEqual(b.status, "accepted")
        self.assertEqual(len(self.repo.read_redacted("sess_a")), 1)
        self.assertEqual(len(self.repo.read_redacted("sess_b")), 1)

    def test_ingest_rejects_malformed_without_poisoning(self):
        bad = {"schema_version": 1, "event_id": "evt_bad"}
        result = self.repo.ingest(bad)
        self.assertEqual(result.status, "rejected")
        self.assertTrue(result.reason)
        # a later valid event still ingests; nothing is poisoned
        good = self.repo.ingest(_event(seq=1))
        self.assertEqual(good.status, "accepted")
        self.assertEqual(len(self.repo.read_redacted("sess_t5_tr1")), 1)

    def test_ingest_returns_gap_for_sequence_hole(self):
        self.repo.ingest(_event(seq=1))
        result = self.repo.ingest(_event(seq=3, event_id="evt_003"))
        self.assertEqual(result.status, "gap")
        rows = self.repo.read_redacted("sess_t5_tr1")
        self.assertEqual([r["sequence"] for r in rows], [1, 3])

    def test_ingest_result_properties(self):
        self.assertTrue(IngestResult("accepted", "e", None).accepted)
        self.assertEqual(IngestResult("gap", "e", None).status, "gap")
        self.assertFalse(IngestResult("duplicate", "e", None).accepted)


class TranscriptRepositoryRedactedStreamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = TranscriptRepository(self.tmp / "tr.db", key=_key())
        self.repo.init()

    def test_redacted_is_queryable_without_a_key(self):
        secret = "token=sk-0123456789abcdef0123456789abcdef"
        self.repo.ingest(_event(seq=1, session_id="sess_r", text=secret,
                                event_id="evt_r"))
        no_key = TranscriptRepository(self.tmp / "tr.db", key=None)
        rows = no_key.read_redacted("sess_r")
        self.assertEqual(len(rows), 1)
        # Live path is passthrough: operator query keeps agent text.
        self.assertEqual(rows[0]["payload"]["text"], secret)
        self.assertNotIn("[REDACTED:", rows[0]["payload"]["text"])

    def test_redacted_stream_is_bounded(self):
        for seq in range(1, 11):
            self.repo.ingest(_event(seq=seq, session_id="sess_b",
                                    text=f"message {seq}",
                                    event_id=f"evt_b_{seq}"))
        rows = self.repo.read_redacted("sess_b", limit=5)
        self.assertEqual(len(rows), 5)


class TranscriptRawStreamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "tr.db"
        self.repo = TranscriptRepository(self.db, key=_key())
        self.repo.init()

    def test_raw_read_round_trip_with_correct_key(self):
        secret_text = "password=correct horse battery staple"
        self.repo.ingest(_event(seq=1, text=secret_text,
                                event_id="evt_raw_1"))
        raw = self.repo.read_raw("sess_t5_tr1", "evt_raw_1", actor="operator-1")
        self.assertEqual(raw["payload"]["text"], secret_text)

    def test_raw_read_requires_restricted_key(self):
        self.repo.ingest(_event(seq=1, text="password=secret-value",
                                event_id="evt_raw_2"))
        no_key = TranscriptRepository(self.db, key=None)
        with self.assertRaises(TranscriptError) as ctx:
            no_key.read_raw("sess_t5_tr1", "evt_raw_2", actor="operator-1")
        self.assertEqual(ctx.exception.code, "raw_key_missing")

        wrong_key = TranscriptRepository(self.db, key=b"\x00" * 32)
        with self.assertRaises(TranscriptError) as ctx2:
            wrong_key.read_raw("sess_t5_tr1", "evt_raw_2", actor="operator-1")
        self.assertEqual(ctx2.exception.code, "raw_decrypt_failed")

    def test_raw_writes_require_a_key_fail_closed(self):
        keyless = TranscriptRepository(self.tmp / "keyless.db", key=None)
        keyless.init()
        result = keyless.ingest(_event(seq=1, event_id="evt_kl", text="x"))
        self.assertEqual(result.status, "accepted")   # redacted still stored
        self.assertFalse(result.raw_written)          # raw never written
        with self.assertRaises(TranscriptError):
            keyless.read_raw("sess_t5_tr1", "evt_kl", actor="operator-1")

    def test_raw_reads_are_audited_without_content(self):
        self.repo.ingest(_event(seq=1, event_id="evt_aud",
                                text="password=top_secret_value"))
        self.repo.read_raw("sess_t5_tr1", "evt_aud", actor="operator-1")
        audit = self.repo.read_audit()
        reads = [a for a in audit if a["action"] == "raw_read"]
        self.assertTrue(reads)
        for entry in reads:
            self.assertNotIn("top_secret_value", entry.get("detail") or "")
            self.assertNotIn("password=", entry.get("detail") or "")


class TranscriptRetentionAndQuotaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "retention.db"
        self.repo = TranscriptRepository(self.db, key=_key())
        self.repo.init()

    @staticmethod
    def _backdate_raw(repo, event_id, iso):
        conn = repo._connect()
        try:
            conn.execute(
                "UPDATE raw_events SET retention_until=? WHERE event_id=?",
                (iso, event_id))
        finally:
            conn.close()

    def test_retention_purges_only_eligible_raw(self):
        session = "sess_ret"
        self.repo.ingest(_event(seq=1, session_id=session,
                                event_id="evt_old", text="old"))
        self.repo.ingest(_event(seq=2, session_id=session,
                                event_id="evt_new", text="new"))
        self._backdate_raw(self.repo, "evt_old", "2000-01-01T00:00:00Z")

        removed = self.repo.purge_expired_raw(now="2026-01-01T00:00:00Z")
        self.assertEqual(removed, ["evt_old"])

        # Redacted rows are untouched (metadata outlives raw retention).
        redacted = self.repo.read_redacted(session)
        self.assertEqual({r["event_id"] for r in redacted},
                         {"evt_old", "evt_new"})

        # The stale raw is gone; the eligible raw is still readable.
        with self.assertRaises(TranscriptError):
            self.repo.read_raw("sess_ret", "evt_old", actor="op")
        self.assertEqual(
            self.repo.read_raw("sess_ret", "evt_new", actor="op")["sequence"], 2)

    def test_retention_audit_carries_counts_not_content(self):
        self.repo.ingest(_event(seq=1, event_id="evt_ret_aud",
                                text="supersecret_value_42"))
        self._backdate_raw(self.repo, "evt_ret_aud", "2000-01-01T00:00:00Z")
        self.repo.purge_expired_raw(now="2026-01-01T00:00:00Z")
        audit = self.repo.read_audit()
        purge = [a for a in audit if a["action"] == "raw_purge"]
        self.assertTrue(purge)
        self.assertEqual(purge[0]["removed"], 1)
        self.assertNotIn("supersecret_value_42",
                         purge[0].get("detail") or "")

    def test_quota_best_effort_overflow_signals_gap(self):
        repo = TranscriptRepository(
            self.tmp / "quota.db", key=_key(), max_raw_bytes=200)
        repo.init()
        result = repo.ingest(_event(seq=1, quality="best_effort",
                                    text="y" * 4096, event_id="evt_q_b"))
        self.assertEqual(result.status, "gap")
        self.assertEqual(result.reason, "raw_quota_exceeded")

    def test_quota_exact_overflow_is_rejected_not_dropped(self):
        repo = TranscriptRepository(
            self.tmp / "quota2.db", key=_key(), max_raw_bytes=200)
        repo.init()
        result = repo.ingest(_event(seq=1, quality="exact",
                                    text="y" * 4096, event_id="evt_q_x"))
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.reason, "raw_quota_exceeded")


class FailureIsolationTests(unittest.TestCase):
    def test_raw_write_failure_does_not_break_redacted_or_observation(self):
        tmp = Path(tempfile.mkdtemp())
        repo = TranscriptRepository(tmp / "iso.db", key=_key())
        repo.init()
        with mock.patch.object(
            repo, "_write_raw_encrypted",
            side_effect=RuntimeError("injected raw write failure")):
            result = repo.ingest(_event(seq=1, text="still surveyable",
                                        event_id="evt_iso"))
            self.assertEqual(result.status, "accepted")
            self.assertEqual(result.reason, "raw_write_failed")
            self.assertFalse(result.raw_written)
            # Redacted stream is still durably available.
            self.assertEqual(len(repo.read_redacted("sess_t5_tr1")), 1)

        # The legacy observation repository still works after the failure.
        state_dir = tmp / "state"
        obs = JsonlObservationRepository(state_dir)
        obs.save_snapshot("host-t5-1",
                          {"hermes": {"status": "ok"}, "agents": {}})
        current = obs.read_current("host-t5-1")
        self.assertEqual(current["hermes"], {"status": "ok"})


class TranscriptErrorSurfaceTests(unittest.TestCase):
    def test_error_carries_bounded_code_not_paths_or_exception_text(self):
        err = TranscriptError("raw_quota_exceeded")
        self.assertEqual(str(err), "raw_quota_exceeded")
        self.assertEqual(err.code, "raw_quota_exceeded")
        self.assertNotIn("/", str(err))
        self.assertNotIn(".py", str(err))
        self.assertNotIn("Traceback", str(err))

    def test_bad_session_identity_rejected_bounded(self):
        repo = TranscriptRepository(Path(tempfile.mkdtemp()) / "e.db",
                                    key=_key())
        repo.init()
        with self.assertRaises(TranscriptError):
            repo.read_redacted("../../etc/passwd")


class PolicySignalAndAuditTests(unittest.TestCase):
    def test_audit_append_read_for_control_receipts(self):
        repo = TranscriptRepository(Path(tempfile.mkdtemp()) / "a.db",
                                    key=_key())
        repo.init()
        repo.append_audit("supervisor", "pause_session", target="sess_ctrl",
                          detail={"reason_code": "op_requested"})
        repo.append_audit("supervisor", "resume_session", target="sess_ctrl",
                          detail={"reason": "drained"})
        audit = repo.read_audit()
        self.assertEqual([a["action"] for a in audit],
                         ["pause_session", "resume_session"])
        self.assertEqual({a["target"] for a in audit}, {"sess_ctrl"})

    def test_policy_signals_round_trip(self):
        repo = TranscriptRepository(Path(tempfile.mkdtemp()) / "p.db",
                                    key=_key())
        repo.init()
        repo.append_policy_signal("sess_pol", "warn", "llm_uncertain")
        signals = repo.read_policy_signals("sess_pol")
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["severity"], "warn")


class LegacyStoreIsolationTests(unittest.TestCase):
    def test_ingest_does_not_touch_legacy_event_log(self):
        tmp = Path(tempfile.mkdtemp())
        repo = TranscriptRepository(tmp / "tr.db", key=_key())
        repo.init()
        repo.ingest(_event(seq=1, text="payload-that-must-not-leak"))
        # Nothing transcript-shaped under the legacy state dir.
        self.assertFalse((tmp / "events.jsonl").exists())
        self.assertFalse((tmp / "state").exists())
        self.assertTrue((tmp / "tr.db").exists())


if __name__ == "__main__":
    unittest.main()