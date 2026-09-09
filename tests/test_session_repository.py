"""Tests for the independent hub SessionRepository (Task 5).

The session metadata repository owns session rows, the per-session stream
cursor, the event dedupe key, and capability state.  It is a separate durable
store (its own SQLite file) -- never the legacy observation JSONL, the
``events.jsonl`` log, the task SQLite, or ordinary task rows.

Coverage (from the Task 5 brief):

- the session repository root is separate from state/task paths;
- upsert validated session specifications and reject invalid ones without
  leaking exception text;
- stream cursor advances monotonically and dedupe is idempotent
  (re-ingesting the same event_id yields a duplicate, never a second row);
- capability state round-trips;
- a stray anomalous session row never poisons later queries;
- repository errors are classified with bounded codes that never embed
  paths, exception text, or raw input.
"""

import os
import tempfile
import unittest
from pathlib import Path

import pytest

from hub.config import FleetConfig
from hub.infrastructure.session_repository import (
    SCHEMA_VERSION,
    SessionError,
    SessionRepository,
)


def _spec(machine_id="host-t5-1", session_id=None, **extra):
    spec = {
        "machine_id": machine_id,
        "session_id": session_id or "sess_t5_a1b2",
        "managed": True,
        "capture_quality": "structured",
        "process_group_id": "grp_t5_001",
        "attempt_id": "att_t5_001",
    }
    spec.update(extra)
    return spec


class SessionRepositoryConstructionTests(unittest.TestCase):
    def test_session_repo_paths_are_separate_from_state_and_task_paths(self):
        root = Path(tempfile.mkdtemp())
        cfg = FleetConfig.from_root(root, session_repositories_enabled=True)
        self.assertTrue(cfg.session_repositories_enabled)
        # The session DB and transcript root must not live in the legacy
        # observation state directory, and must not collide with the task
        # DB / events log paths.
        self.assertIsNotNone(cfg.session_db)
        self.assertIsNotNone(cfg.session_transcript_root)
        self.assertNotEqual(cfg.session_db.parent, cfg.state_dir)
        self.assertNotEqual(cfg.session_db, cfg.task_db)
        self.assertNotEqual(cfg.session_db, cfg.event_log)
        self.assertNotEqual(cfg.session_transcript_root, cfg.state_dir)
        # The session DB lives outside the legacy state tree.
        self.assertNotIn(str(cfg.session_db), {str(cfg.task_db), str(cfg.event_log)})

    def test_session_repo_disabled_by_default(self):
        cfg = FleetConfig.from_root(Path(tempfile.mkdtemp()))
        self.assertFalse(cfg.session_repositories_enabled)
        self.assertIsNone(cfg.session_db)
        self.assertIsNone(cfg.session_transcript_root)

    def test_session_repo_supports_explicit_override(self):
        root = Path(tempfile.mkdtemp())
        db = root / "custom" / "meta.db"
        tr = root / "custom" / "transcripts"
        cfg = FleetConfig.from_root(
            root, session_repositories_enabled=True,
            session_db=db, session_transcript_root=tr,
        )
        self.assertEqual(cfg.session_db, db)
        self.assertEqual(cfg.session_transcript_root, tr)


class SessionRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db_path = self.tmp / "sessions.db"
        self.repo = SessionRepository(self.db_path)
        self.repo.init()

    def test_init_records_schema_version(self):
        self.assertEqual(SCHEMA_VERSION, 2)
        self.assertEqual(self.repo.schema_version(), 2)

    def test_upsert_and_get_session(self):
        spec = _spec()
        row = self.repo.upsert_session(spec)
        self.assertEqual(row["session_id"], spec["session_id"])
        self.assertEqual(row["machine_id"], spec["machine_id"])
        self.assertTrue(row["managed"])
        got = self.repo.get_session(spec["session_id"])
        self.assertIsNotNone(got)
        self.assertEqual(got["session_id"], spec["session_id"])
        self.assertEqual(got["capture_quality"], "structured")
        self.assertEqual(got["process_group_id"], "grp_t5_001")

    def test_upsert_rejects_invalid_spec_with_bounded_error(self):
        # managed without process_group_id must be rejected (bounded code).
        with self.assertRaises(SessionError) as ctx:
            self.repo.upsert_session(_spec(process_group_id=None))
        self.assertEqual(ctx.exception.code, "invalid_session_spec")
        self.assertNotIn(str(ctx.exception), "process_group_id")

    def test_list_sessions_filters_by_machine(self):
        self.repo.upsert_session(_spec(session_id="sess_a", machine_id="m1"))
        self.repo.upsert_session(_spec(session_id="sess_b", machine_id="m2"))
        rows = self.repo.list_sessions(machine_id="m1")
        self.assertEqual([r["session_id"] for r in rows], ["sess_a"])

    def test_stream_cursor_advances_monotonically(self):
        sid = "sess_cursor"
        self.repo.init_stream(sid)
        self.assertEqual(self.repo.stream_cursor(sid), 0)
        self.repo.note_event(sid, "evt_1", 1, "user_message")
        self.repo.note_event(sid, "evt_2", 2, "assistant_message")
        self.assertEqual(self.repo.stream_cursor(sid), 2)

    def test_note_event_concurrent_writes_never_regress_cursor(self):
        # Two writers racing on one session must leave the cursor at the
        # highest sequence seen, regardless of write ordering (the cursor
        # merges with MAX atomically inside the upsert statement).
        sid = "sess_race"
        import threading
        barrier = threading.Barrier(2)

        def worker(seq):
            barrier.wait()
            self.repo.note_event(sid, f"evt_{seq}", seq, "user_message")

        t1 = threading.Thread(target=worker, args=(3,))
        t2 = threading.Thread(target=worker, args=(4,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(self.repo.stream_cursor(sid), 4)

    def test_acknowledge_never_regresses(self):
        sid = "sess_ack_monotonic"
        self.repo.acknowledge(sid, 5)
        self.repo.acknowledge(sid, 2)  # late lower ack must not regress
        conn = self.repo._connect()
        try:
            row = conn.execute(
                "SELECT ack_sequence FROM stream_state WHERE session_id=?",
                (sid,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(int(row["ack_sequence"]), 5)

    def test_acknowledge_never_advances_last_sequence(self):
        sid = "sess_ack"
        self.repo.note_event(sid, "evt_1", 5, "step_start")
        self.repo.acknowledge(sid, 3)
        self.assertEqual(self.repo.stream_cursor(sid), 5)

    def test_event_dedupe_is_idempotent(self):
        sid = "sess_dedupe"
        self.assertTrue(self.repo.event_seen(sid, "evt_x"))
        self.assertFalse(self.repo.event_seen(sid, "evt_x"), )  # noqa: E712
        self.assertTrue(self.repo.event_seen(sid, "evt_y"))
        # A second, distinct event with a distinct id is a new event.
        self.assertTrue(self.repo.event_seen(sid, "evt_z"))

    def test_invalid_session_id_rejected(self):
        # path-looking session ids must be refused at the repository boundary.
        with self.assertRaises(SessionError):
            self.repo.get_session("../../etc/passwd")
        with self.assertRaises(SessionError):
            self.repo.note_event("../../etc/passwd", "e", 1, "step_start")

    def test_capabilities_round_trip(self):
        sid = "sess_caps"
        self.assertEqual(self.repo.get_capabilities(sid), [])
        self.repo.set_capabilities(sid, ["exact", "pending2"])
        self.repo.set_capabilities(sid, ["exact", "pending2", "process_group"])
        self.assertEqual(
            sorted(self.repo.get_capabilities(sid)),
            sorted(["process_group", "pending2", "exact"]),
        )

    def test_non_managed_session_is_not_treated_as_managed(self):
        row = self.repo.upsert_session(_spec(managed=False))
        self.assertFalse(row["managed"])

    def test_stray_session_row_does_not_poison_listing(self):
        # A row written directly with an unknown status must not break reads
        # of other, well-formed rows.
        row_ok = self.repo.upsert(_spec(session_id="sess_ok_b", machine_id="m_ok"))
        conn = self.repo._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO sessions (session_id, machine_id, managed,"
                " capture_quality, status, started_at, updated_at, process_group_id,"
                " attempt_id, finished_at)"
                " VALUES ('sess_bad', 'm_ok', 1, 'bogus', 'bogus-status',"
                " '2026-08-26T00:00:00Z', '2026-08-26T00:00:00Z',"
                " 'grp', 'att', NULL)"
            )
        finally:
            conn.close()
        rows = self.repo.list_sessions(machine_id="m_ok")
        self.assertIn("sess_ok_b", {r["session_id"] for r in rows})

    def test_error_str_is_bounded_code(self):
        err = SessionError("bounded_code_x")
        self.assertEqual(str(err), "bounded_code_x")


# --- Task 4: agent_family binding ---------------------------------------------

def test_agent_family_roundtrips(tmp_path):
    repo = SessionRepository(tmp_path / "s.db")
    repo.init()
    repo.upsert_session({
        "session_id": "s1", "machine_id": "m1", "managed": True,
        "process_group_id": "grp_1", "agent_family": "codex",
    })
    row = repo.get_session("s1")
    assert row["agent_family"] == "codex"


def test_agent_family_defaults_empty_when_absent(tmp_path):
    repo = SessionRepository(tmp_path / "s.db")
    repo.init()
    repo.upsert_session({"session_id": "s2", "machine_id": "m1"})
    row = repo.get_session("s2")
    assert row["agent_family"] == ""


def test_agent_family_rejects_path_shapes(tmp_path):
    repo = SessionRepository(tmp_path / "s.db")
    repo.init()
    repo.upsert_session({
        "session_id": "s3", "machine_id": "m1", "agent_family": "../../etc/passwd",
    })
    row = repo.get_session("s3")
    assert row["agent_family"] == ""


def test_agent_family_rejects_secret_markers(tmp_path):
    repo = SessionRepository(tmp_path / "s.db")
    repo.init()
    for i, bad in enumerate(("secret_x", "x_key", "MyToken", "pw_password")):
        sid = f"sid_bad_{i}"
        repo.upsert_session({
            "session_id": sid, "machine_id": "m1", "agent_family": bad,
        })
        assert repo.get_session(sid)["agent_family"] == ""


def test_legacy_db_migrates_without_losing_rows(tmp_path):
    import sqlite3
    db = tmp_path / "s.db"
    repo = SessionRepository(db)
    repo.init()
    repo.upsert_session({"session_id": "old", "machine_id": "m1"})
    conn = sqlite3.connect(str(db))
    conn.execute("ALTER TABLE sessions DROP COLUMN agent_family")
    conn.close()
    repo2 = SessionRepository(db)
    repo2.init()  # 幂等,不抛
    assert repo2.get_session("old")["session_id"] == "old"
    assert repo2.get_session("old")["agent_family"] == ""


def test_session_id_boundary_matches_transcript_repository(tmp_path):
    """Both repositories reject the same path/DB-shaped session ids so an
    id accepted on the ingest path can always be read back."""
    from hub.infrastructure.transcript_repository import (
        TranscriptError, TranscriptRepository,
    )

    for bad in ("../etc/passwd", "sess.db", "sess.json", "a/b", "a\\b",
                "secret_x"):
        sr = SessionRepository(tmp_path / "sr.db")
        sr.init()
        with pytest.raises(SessionError):
            sr.upsert_session({"session_id": bad, "machine_id": "m"})
        with pytest.raises(SessionError):
            sr.get_session(bad)
        tr = TranscriptRepository(tmp_path / "tr.db", key=b"\x00" * 32)
        tr.init()
        with pytest.raises(TranscriptError):
            tr.read_redacted(bad)


if __name__ == "__main__":
    unittest.main()