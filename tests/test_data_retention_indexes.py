"""Round-5 data-layer regressions: hot-path indexes and history retention."""

import sqlite3
import time

import pytest

from hub.infrastructure.command_repository import CommandRepository
from hub.infrastructure.execution_window_repository import ExecutionWindowRepository
from hub.infrastructure.platform_db import PlatformRepository
from hub.infrastructure.platform_retention import (
    DAY_S,
    HISTORY_RETENTION_DAYS,
    PlatformRetention,
    RetentionPolicy,
)
from hub.infrastructure.task_repository import SqliteTaskRepository as TaskRepository
from hub.infrastructure.transcript_repository import TranscriptRepository

NOW = 1_000 * DAY_S
OLD = NOW - 120 * DAY_S
RECENT = NOW - DAY_S


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _plan(conn, sql, params=()):
    return " | ".join(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params))


def _assert_uses(conn, sql, params, index, table):
    plan = _plan(conn, sql, params)
    accepted = (index,) if isinstance(index, str) else index
    assert any(name in plan for name in accepted), plan
    assert f"SCAN {table}" not in plan.replace(f"SCAN {table} USING", "")
    for line in plan.split(" | "):
        assert line.strip() != f"SCAN {table}", plan


@pytest.fixture()
def platform_db(tmp_path):
    db = tmp_path / "platform.db"
    PlatformRepository(db).init()
    CommandRepository(db).init()
    ExecutionWindowRepository(db).init()
    return db


@pytest.fixture()
def task_db(tmp_path):
    db = tmp_path / "fleet.db"
    TaskRepository(db).init()
    return db


@pytest.fixture()
def transcript_db(tmp_path):
    db = tmp_path / "transcript.db"
    TranscriptRepository(db, key=b"k" * 32).init()
    return db


# -- index plans ---------------------------------------------------------------


def test_claim_run_seeks_owner_state_index(platform_db):
    from hub.infrastructure import platform_db_runs

    source = open(platform_db_runs.__file__, encoding="utf-8").read()
    assert "r.state IN ('queued','cancelling','running','waiting_node')" in source
    conn = sqlite3.connect(platform_db)
    sql = (
        "SELECT r.* FROM runs r JOIN messages trigger ON trigger.message_id=r.trigger_message_id "
        "WHERE r.owner_id=? AND r.state IN ('queued','cancelling','running','waiting_node') "
        "AND ((r.state='waiting_node' AND r.runtime_resume_at<=?) OR (r.state='queued' AND"
        " (r.lease_id IS NULL OR r.lease_expires_at IS NULL OR r.lease_expires_at<=?)) OR"
        " (r.state='cancelling' AND (r.lease_expires_at IS NULL OR r.lease_expires_at<=?)) OR"
        " (r.state='running' AND r.lease_expires_at IS NOT NULL AND r.lease_expires_at<=?))"
        " ORDER BY r.created_at, trigger.turn_sequence, r.run_id LIMIT 1")
    _assert_uses(conn, sql, ("o", 1, 1, 1, 1), "idx_runs_owner_state_created", "r")
    conn.close()


def test_run_event_retention_selects_terminal_runs_by_state_index(platform_db):
    conn = sqlite3.connect(platform_db)
    sql = ("SELECT r.run_id FROM runs r WHERE r.state IN ('cancelled','failed','succeeded','unknown') "
           "AND r.updated_at<? AND EXISTS (SELECT 1 FROM run_events e WHERE e.run_id=r.run_id) LIMIT ?")
    _assert_uses(conn, sql, (1, 10), "idx_runs_state_updated", "r")
    conn.close()


@pytest.mark.parametrize("table,index", [
    ("platform_command_reconciliations", "idx_platform_reconcile_command"),
    ("platform_command_postchecks", "idx_platform_postcheck_command"),
    ("platform_service_postchecks", "idx_platform_service_postcheck_command"),
])
def test_command_child_tables_have_command_id_index(platform_db, table, index):
    conn = sqlite3.connect(platform_db)
    _assert_uses(conn, f"DELETE FROM {table} WHERE command_id IN (?,?)", ("a", "b"), index, table)
    conn.close()


def test_execution_window_indexes(platform_db):
    conn = sqlite3.connect(platform_db)
    _assert_uses(conn, "SELECT window_id FROM execution_window_frames WHERE artifact_id IN (?)",
                 ("a",), "idx_execution_window_frames_artifact_id", "execution_window_frames")
    _assert_uses(conn, "SELECT owner_id, window_id FROM execution_windows "
                       "WHERE state IN ('closed','expired') AND updated_at<? LIMIT ?",
                 (1, 10), "idx_execution_windows_state_updated", "execution_windows")
    conn.close()


@pytest.mark.parametrize("sql,params,index,table", [
    ("SELECT t.* FROM tasks t LEFT JOIN task_gates g ON g.task_id=t.task_id"
     " WHERE t.machine=? AND t.state='queued' AND +t.expires_at>?"
     " AND (g.task_id IS NULL OR g.state='confirmed') ORDER BY t.created_at LIMIT 1",
     ("m", "x"), "idx_tasks_machine_state_created", "t"),
    ("SELECT l.task_id FROM tasks t JOIN leases l ON l.attempt_id=t.attempt_id AND l.task_id=t.task_id"
     " WHERE t.state IN ('leased','running') AND l.expires_at<=?",
     # Both plans seek active tasks by state, then probe the current lease by
     # attempt_id. SQLite may prefer the narrower partial Workflow-era index.
     ("x",), ("idx_tasks_state_expires", "idx_tasks_live_lease"), "t"),
    ("SELECT task_id FROM tasks WHERE state='queued' AND expires_at<=?",
     ("x",), "idx_tasks_state_expires", "tasks"),
    ("SELECT id FROM audit WHERE ts<? LIMIT ?", ("x", 5), "idx_audit_ts", "audit"),
    ("SELECT 1 FROM audit a WHERE a.task_id=? AND a.ts>=?", ("t", "x"), "idx_audit_task", "a"),
    ("DELETE FROM results WHERE task_id IN (?)", ("t",), "idx_results_task", "results"),
    ("DELETE FROM handoff_receipts WHERE task_id IN (?)", ("t",),
     "idx_handoff_receipts_task", "handoff_receipts"),
])
def test_task_store_indexes(task_db, sql, params, index, table):
    conn = sqlite3.connect(task_db)
    _assert_uses(conn, sql, params, index, table)
    conn.close()


def test_lease_task_query_uses_machine_index_without_sort(task_db):
    from hub.infrastructure import task_repository

    source = open(task_repository.__file__, encoding="utf-8").read()
    assert "AND +t.expires_at>?" in source
    conn = sqlite3.connect(task_db)
    plan = _plan(conn, "SELECT t.* FROM tasks t LEFT JOIN task_gates g ON g.task_id=t.task_id"
                       " WHERE t.machine=? AND t.state='queued' AND +t.expires_at>?"
                       " AND (g.task_id IS NULL OR g.state='confirmed') ORDER BY t.created_at LIMIT 1",
                 ("m", "x"))
    assert "TEMP B-TREE" not in plan, plan
    conn.close()


def test_transcript_retention_indexes(transcript_db):
    conn = sqlite3.connect(transcript_db)
    _assert_uses(conn, "SELECT id FROM audit WHERE ts<? LIMIT ?", ("x", 5),
                 "idx_transcript_audit_ts", "audit")
    _assert_uses(conn, "SELECT rowid FROM policy_signals WHERE emitted_at<? LIMIT ?", ("x", 5),
                 "idx_policy_emitted", "policy_signals")
    plan = _plan(conn, "SELECT session_id FROM redacted_events GROUP BY session_id "
                       "HAVING MAX(julianday(upper(emitted_at)))<?", (1,))
    # A GROUP BY over every session is a full pass by nature; it must read the
    # narrow covering index, never the redacted_json table rows.
    assert "COVERING INDEX idx_redacted_session_emitted" in plan, plan
    conn.close()


# -- task retention ------------------------------------------------------------


def _finished_task(repo, machine, now, *, files=True):
    repo.create_task(machine=machine, agent_type="codex", project="p", instruction="i",
                     requested_by="u", now=now)
    lease = repo.lease_task(machine=machine, runner_id="r", now=now)
    repo.complete_task(attempt_id=lease["attempt_id"], nonce=lease["nonce"], exit_code=0,
                       log_summary="ok", diff_stat="", duration_s=1.0, now=now,
                       files=[{"path": "a.txt", "content": "x"}] if files else None)
    return lease["task_id"]


def _count(db, table, where="1=1", params=()):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]
    finally:
        conn.close()


def test_task_retention_removes_old_terminal_tasks_with_children(task_db):
    repo = TaskRepository(task_db)
    old = _finished_task(repo, "m1", OLD)
    recent = _finished_task(repo, "m1", RECENT)
    repo.create_task(machine="m2", agent_type="codex", project="p", instruction="live",
                     requested_by="u", now=OLD)  # queued, never leased: not terminal
    touched = _finished_task(repo, "m3", OLD)
    repo.audit_action("op", "read_file", touched, now=RECENT)  # recent activity keeps it

    deleted = PlatformRetention(None, task_db=task_db)(now=NOW)

    assert deleted["tasks"] == 1
    for table in ("tasks", "results", "result_files", "leases"):
        assert _count(task_db, table, "task_id=?", (old,)) == 0, table
        assert _count(task_db, table, "task_id=?", (recent,)) == 1, table
        assert _count(task_db, table, "task_id=?", (touched,)) == 1, table
    assert _count(task_db, "audit", "task_id=?", (old,)) == 0
    assert _count(task_db, "audit", "task_id=?", (recent,)) > 0
    assert _count(task_db, "tasks", "machine='m2'") == 1
    conn = sqlite3.connect(task_db)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_task_audit_rows_older_than_horizon_are_pruned(task_db):
    repo = TaskRepository(task_db)
    repo.audit_action("op", "old", None, now=OLD)
    repo.audit_action("op", "new", None, now=RECENT)
    deleted = PlatformRetention(None, task_db=task_db)(now=NOW)
    assert deleted == {"task_audit": 1}
    assert _count(task_db, "audit", "action='old'") == 0
    assert _count(task_db, "audit", "action='new'") == 1


def test_history_horizon_is_configurable(task_db):
    repo = TaskRepository(task_db)
    repo.audit_action("op", "a", None, now=NOW - 20 * DAY_S)
    assert PlatformRetention(None, task_db=task_db)(now=NOW) == {}
    policy = RetentionPolicy.with_history_days(10)
    assert PlatformRetention(None, policy, task_db=task_db)(now=NOW) == {"task_audit": 1}


def test_expire_leases_only_considers_current_attempts(task_db):
    repo = TaskRepository(task_db)
    for _ in range(3):
        _finished_task(repo, "m", NOW - 3600, files=False)  # stale lease history
    repo.create_task(machine="m", agent_type="codex", project="p", instruction="live",
                     requested_by="u", now=NOW)
    lease = repo.lease_task(machine="m", runner_id="r", lease_ttl_s=60, now=NOW)
    assert repo.expire_leases(now=NOW + 30) == []
    assert repo.expire_leases(now=NOW + 120) == [lease["task_id"]]
    assert repo.expire_leases(now=NOW + 240) == []


# -- execution windows -----------------------------------------------------------


def _window(conn, window_id, state, updated_at, expires_at):
    conn.execute(
        "INSERT INTO execution_windows(window_id,owner_id,run_id,state,created_at,expires_at,updated_at)"
        " VALUES(?,'o','r',?,?,?,?)", (window_id, state, updated_at, expires_at, updated_at))
    conn.execute(
        "INSERT INTO execution_window_events(window_id,owner_id,sequence,client_event_id,kind,payload,"
        "created_at) VALUES(?,'o',1,'c','k','{}',?)", (window_id, updated_at))
    conn.execute(
        "INSERT INTO execution_window_frames(window_id,owner_id,sequence,workspace_id,artifact_id,sha256,"
        "width,height,captured_at) VALUES(?,'o',1,'w',?,'s',1,1,'t')", (window_id, "a-" + window_id))
    conn.execute(
        "INSERT INTO execution_window_tickets(ticket_id,window_id,owner_id,token_hash,expires_at,created_at)"
        " VALUES(?,?,'o',?,?,?)", ("t-" + window_id, window_id, "h-" + window_id, expires_at, updated_at))
    conn.execute(
        "INSERT INTO execution_window_frame_reads(window_id,owner_id,last_read_at) VALUES(?,'o',?)",
        (window_id, updated_at))


def test_execution_window_age_purge(platform_db):
    conn = sqlite3.connect(platform_db)
    _window(conn, "closed-old", "closed", OLD, OLD + 3600)
    _window(conn, "closed-new", "closed", RECENT, RECENT + 3600)
    _window(conn, "abandoned", "attached", OLD, OLD + 3600)
    _window(conn, "live", "attached", OLD, NOW + 3600)
    conn.commit()
    conn.close()

    deleted = PlatformRetention(platform_db)(now=NOW)

    assert deleted["execution_windows"] == 2
    conn = sqlite3.connect(platform_db)
    assert {r[0] for r in conn.execute("SELECT window_id FROM execution_windows")} == {
        "closed-new", "live"}
    for child in ("execution_window_events", "execution_window_frames", "execution_window_tickets",
                  "execution_window_frame_reads"):
        assert {r[0] for r in conn.execute(f"SELECT window_id FROM {child}")} == {
            "closed-new", "live"}, child
    conn.execute("PRAGMA foreign_keys=ON")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_run_events_pruned_by_run_id_in_bounded_batches(platform_db):
    conn = sqlite3.connect(platform_db)
    conn.execute("PRAGMA foreign_keys=OFF")
    for run_id, state in (("done", "succeeded"), ("live", "running")):
        conn.execute(
            "INSERT INTO runs(run_id,conversation_id,owner_id,trigger_message_id,state,config_snapshot,"
            "created_at,updated_at) VALUES(?,'c','o','m',?,'{}',?,?)", (run_id, state, OLD, OLD))
        for seq in range(1, 8):
            conn.execute("INSERT INTO run_events(run_id,sequence,kind,payload,created_at)"
                         " VALUES(?,?,'k','{}',?)", (run_id, seq, OLD))
    conn.commit()
    conn.close()
    retention = PlatformRetention(platform_db, RetentionPolicy(batch_size=3, max_batches=1))
    assert retention(now=NOW) == {"run_events": 3}
    retention(now=NOW)
    retention(now=NOW)
    assert retention(now=NOW) == {}
    assert _count(platform_db, "run_events", "run_id='done'") == 0
    assert _count(platform_db, "run_events", "run_id='live'") == 7


# -- transcript store ------------------------------------------------------------


def _rfc(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def test_transcript_audit_and_policy_signals_age_out(transcript_db):
    conn = sqlite3.connect(transcript_db)
    for ts, tag in ((OLD, "old"), (RECENT, "new")):
        conn.execute("INSERT INTO audit(ts,actor,action) VALUES(?,?,?)", (_rfc(ts), "a", tag))
        conn.execute("INSERT INTO policy_signals(session_id,emitted_at,severity,reason)"
                     " VALUES('s',?,'low',?)", (_rfc(ts), tag))
    conn.commit()
    conn.close()
    deleted = PlatformRetention(None, transcript_db=transcript_db)(now=NOW)
    assert deleted == {"transcript_audit": 1, "policy_signals": 1}
    assert _count(transcript_db, "audit") == 1
    assert _count(transcript_db, "policy_signals", "reason='new'") == 1


def test_raw_purge_writes_one_aggregate_audit_row_per_batch(tmp_path):
    from tests.test_transcript_repository import _event, _key

    repo = TranscriptRepository(tmp_path / "t.db", key=_key())
    repo.init()
    for seq in range(1, 4):
        repo.ingest(_event(seq=seq, session_id="sess_agg", event_id=f"evt_agg_{seq}", text="x"))
    conn = sqlite3.connect(repo.db_path)
    conn.executemany("UPDATE raw_events SET retention_until=? WHERE event_id=?", [
        ("2000-01-01T00:00:00Z", "evt_agg_1"), ("2000-01-02T00:00:00Z", "evt_agg_2"),
        ("2000-01-03T00:00:00Z", "evt_agg_3")])
    conn.commit()
    conn.close()

    removed = repo.purge_expired_raw(now="2026-01-01T00:00:00Z")

    assert len(removed) == 3
    purge = [a for a in repo.read_audit() if a["action"] == "raw_purge"]
    assert len(purge) == 1
    assert purge[0]["removed"] == 3
    assert purge[0]["target"] == "2000-01-01T00:00:00Z..2000-01-03T00:00:00Z"
    assert not any("evt_agg" in str(a.get("target")) for a in purge)


class _Search:
    def __init__(self, fail=False):
        self.fail = fail
        self.purged = []
        self.deleted = []

    def purge_before(self, cutoff):
        if self.fail:
            raise RuntimeError("search down")
        self.purged.append(cutoff)
        return 2

    def delete_session(self, session_id):
        self.deleted.append(session_id)
        return 1


def _redacted(conn, session_id, event_id, ts):
    conn.execute(
        "INSERT INTO redacted_events(session_id,event_id,sequence,kind,capture_quality,emitted_at,"
        "redacted_json) VALUES(?,?,1,'k','structured',?,'{}')", (session_id, event_id, _rfc(ts)))


def test_session_search_follows_redacted_transcript_retention(transcript_db):
    conn = sqlite3.connect(transcript_db)
    _redacted(conn, "gone", "e1", OLD)
    _redacted(conn, "kept", "e2", RECENT)
    conn.commit()
    conn.close()
    search = _Search()
    deleted = PlatformRetention(None, transcript_db=transcript_db, session_search=search)(now=NOW)
    assert deleted["redacted_events"] == 1
    assert search.deleted == ["gone"]
    assert search.purged == [_rfc(NOW - 30 * DAY_S)]
    assert deleted["session_search_entries"] == 3


def test_session_search_failure_does_not_block_other_retention(task_db):
    TaskRepository(task_db).audit_action("op", "old", None, now=OLD)
    deleted = PlatformRetention(None, task_db=task_db, session_search=_Search(fail=True))(now=NOW)
    assert deleted == {"task_audit": 1}


def test_session_search_purge_against_real_index(tmp_path):
    from hub.application.session_search_service import SessionSearchService
    from hub.infrastructure.session_search_repository import SessionSearchRepository

    repo = SessionSearchRepository(tmp_path / "search.db")
    service = SessionSearchService(repo)
    service.initialize_optional()
    removed = PlatformRetention(None, session_search=service)(now=NOW)
    assert removed == {}


# -- configuration ---------------------------------------------------------------


def test_history_retention_config_is_clamped(tmp_path):
    from hub.config import FleetConfig

    assert FleetConfig.from_root(tmp_path).history_retention_days == HISTORY_RETENTION_DAYS
    assert FleetConfig.from_root(tmp_path, history_retention_days=1).history_retention_days == 7.0
    assert FleetConfig.from_root(tmp_path, history_retention_days=float("nan")).history_retention_days == 90.0
    assert FleetConfig.from_root(tmp_path, history_retention_days=10**9).history_retention_days == 3650.0


def test_history_retention_env_reaches_retention_policy(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from hub.bootstrap import _platform_retention
    from hub.web import _HISTORY_RETENTION_DAYS_ENV, _bounded_float, _optional_setting

    monkeypatch.setenv(_HISTORY_RETENTION_DAYS_ENV, "45")
    days = _bounded_float(_optional_setting(None, env=_HISTORY_RETENTION_DAYS_ENV),
                          default=90.0, lower=7.0, upper=3650.0)
    assert days == 45.0
    cfg = SimpleNamespace(platform_db=None, tasks_enabled=True, task_db=tmp_path / "fleet.db",
                          history_retention_days=days)
    retention = _platform_retention(cfg, platform_repository=None)
    assert retention.task_db == tmp_path / "fleet.db"
    assert retention.policy.task_days == retention.policy.execution_window_days == 45.0
    assert retention.policy.redacted_event_days == 30.0
