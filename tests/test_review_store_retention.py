"""Review regression: append-only platform tables are pruned in bounded batches."""

import sqlite3

from hub.application.reconciliation_service import ReconciliationService
from hub.infrastructure.command_repository import CommandRepository
from hub.infrastructure.platform_db import PlatformRepository
from hub.infrastructure.platform_retention import DAY_S, PlatformRetention, RetentionPolicy
from hub.infrastructure.platform_schedule_repository import PlatformScheduleRepository
from hub.infrastructure.usage_repository import UsageRepository

NOW = 1_000 * DAY_S
OLD = NOW - 120 * DAY_S
RECENT = NOW - DAY_S


def _db(tmp_path):
    db = tmp_path / "platform.db"
    PlatformRepository(db).init()
    PlatformScheduleRepository(db).init()
    CommandRepository(db).init()
    UsageRepository(db).init()
    return db


def _seed(db, old_count):
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        "INSERT INTO platform_schedules(owner_id,schedule_id,name,action,target,interval_s,timezone,"
        "missed_policy,overlap_policy,next_run_at,updated_at) "
        "VALUES('o','s','n','service_health','{}',5,'UTC','skip','skip',?,?)", (NOW, NOW))
    for i in range(old_count):
        conn.execute(
            "INSERT INTO platform_schedule_triggers(owner_id,schedule_id,scheduled_at,state,updated_at) "
            "VALUES('o','s',?,'succeeded',?)", (OLD + i, OLD + i))
        conn.execute(
            "INSERT INTO platform_usage_events(event_id,reservation_id,owner_id,model_key,run_id,attempt,"
            "step,outcome,occurred_at) VALUES(?,?,'o','m','r',1,?,'ok',?)",
            (f"e{i}", f"res{i}", i, OLD))
        conn.execute(
            "INSERT INTO platform_commands(command_id,owner_id,target_node,action,resource_id,arguments,"
            "retry_class,expires_at,args_hash,status,created_at,updated_at) "
            "VALUES(?,NULL,'n','a','x','{}','read_only',0,'h','succeeded',?,?)", (f"c{i}", OLD, OLD))
        conn.execute(
            "INSERT INTO platform_command_outbox(command_id,event_type,payload,created_at,updated_at) "
            "VALUES(?,'command_queued','{}',?,?)", (f"c{i}", OLD, OLD))
        conn.execute(
            "INSERT INTO run_events(run_id,sequence,kind,payload,created_at) VALUES('old-run',?,'k','{}',?)",
            (i + 1, OLD))
    # Rows that must survive: recent, non-terminal, owned, or not yet past next_run_at.
    conn.execute("INSERT INTO platform_schedule_triggers(owner_id,schedule_id,scheduled_at,state,updated_at) "
                 "VALUES('o','s',?,'succeeded',?)", (RECENT, RECENT))
    conn.execute("INSERT INTO platform_schedule_triggers(owner_id,schedule_id,scheduled_at,state,updated_at) "
                 "VALUES('o','s',?,'running',?)", (OLD - 1, OLD))
    conn.execute("INSERT INTO platform_schedule_triggers(owner_id,schedule_id,scheduled_at,state,updated_at) "
                 "VALUES('o','s',?,'failed',?)", (NOW, OLD))
    conn.execute("INSERT INTO platform_usage_events(event_id,reservation_id,owner_id,model_key,run_id,attempt,"
                 "step,outcome,occurred_at) VALUES('keep','keep','o','m','r',2,1,'ok',?)", (RECENT,))
    for cid, owner, status in (("c-owned", "o", "succeeded"), ("c-live", None, "queued")):
        conn.execute(
            "INSERT INTO platform_commands(command_id,owner_id,target_node,action,resource_id,arguments,"
            "retry_class,expires_at,args_hash,status,created_at,updated_at) "
            "VALUES(?,?,'n','a','x','{}','read_only',0,'h',?,?,?)", (cid, owner, status, OLD, OLD))
    for run_id, state in (("old-run", "succeeded"), ("live-run", "running")):
        conn.execute(
            "INSERT INTO runs(run_id,conversation_id,owner_id,trigger_message_id,state,config_snapshot,"
            "created_at,updated_at) VALUES(?,'c','o','m',?,'{}',?,?)", (run_id, state, OLD, OLD))
    conn.execute("INSERT INTO run_events(run_id,sequence,kind,payload,created_at) "
                 "VALUES('live-run',1,'k','{}',?)", (OLD,))
    conn.commit()
    conn.close()


def _counts(db):
    conn = sqlite3.connect(db)
    try:
        return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in (
            "platform_schedule_triggers", "platform_usage_events", "platform_commands",
            "platform_command_outbox", "run_events")}
    finally:
        conn.close()


def test_retention_prunes_old_rows_in_bounded_batches(tmp_path):
    db = _db(tmp_path)
    _seed(db, 25)
    retention = PlatformRetention(db, RetentionPolicy(batch_size=10, max_batches=2))

    first = retention(now=NOW)
    assert first == {"platform_schedule_triggers": 20, "platform_usage_events": 20,
                     "platform_commands": 20, "run_events": 20}

    retention(now=NOW)
    assert retention(now=NOW) == {}
    assert _counts(db) == {
        "platform_schedule_triggers": 3,
        "platform_usage_events": 1,
        "platform_commands": 2,
        "platform_command_outbox": 0,
        "run_events": 1,
    }
    conn = sqlite3.connect(db)
    for child in ("platform_command_outbox", "platform_command_reconciliations"):
        assert conn.execute(f"PRAGMA foreign_key_check({child})").fetchall() == []
    conn.close()


def test_row_count_stays_bounded_when_driven_by_reconciliation(tmp_path):
    db = _db(tmp_path)
    _seed(db, 50)

    class _Repo:
        def expire_leases(self, *, now):
            return []

        def expire_tasks(self, *, now):
            return []

    clock = [NOW]
    svc = ReconciliationService(None, _Repo(), None, clock=lambda: clock[0],
                                retention=PlatformRetention(db))
    svc.reconcile_leases()
    assert _counts(db)["platform_schedule_triggers"] == 3
    assert _counts(db)["run_events"] == 1


def test_missing_database_is_a_noop(tmp_path):
    assert PlatformRetention(tmp_path / "absent.db")(now=NOW) == {}
    assert not (tmp_path / "absent.db").exists()


def _iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _transcript_db(tmp_path):
    from hub.infrastructure.transcript_repository import TranscriptRepository
    db = tmp_path / "transcripts.db"
    TranscriptRepository(db).init()
    return db


def _seed_redacted(db, session_id, stamps):
    conn = sqlite3.connect(db)
    for seq, stamp in enumerate(stamps):
        conn.execute(
            "INSERT INTO redacted_events(session_id,event_id,stream_id,machine_id,sequence,kind,"
            "capture_quality,emitted_at,redacted_json) VALUES(?,?,NULL,'m',?,'assistant_message',"
            "'full',?,'{}')", (session_id, f"{session_id}-{seq}", seq, stamp))
    conn.commit()
    conn.close()


def _redacted_sessions(db):
    conn = sqlite3.connect(db)
    try:
        return {row[0]: row[1] for row in conn.execute(
            "SELECT session_id, COUNT(*) FROM redacted_events GROUP BY session_id")}
    finally:
        conn.close()


def test_redacted_events_pruned_per_idle_session_after_30_days(tmp_path):
    db = _transcript_db(tmp_path)
    old = NOW - 31 * DAY_S
    # 整会话都早于 30 天 → 清理（含小数秒与时区偏移的 RFC3339 写法）
    _seed_redacted(db, "sess_idle", [_iso(old + i) for i in range(12)]
                   + ["1970-01-31T00:00:00.123456+08:00"])
    # 旧事件 + 新事件的活跃长会话 → 整会话保留，不截断
    _seed_redacted(db, "sess_active", [_iso(old), _iso(old + 1), _iso(RECENT)])
    # 29 天内 → 保留
    _seed_redacted(db, "sess_recent", [_iso(NOW - 29 * DAY_S)])
    retention = PlatformRetention(None, RetentionPolicy(batch_size=5, max_batches=2),
                                  transcript_db=db)

    assert retention(now=NOW) == {"redacted_events": 10}
    assert retention(now=NOW) == {"redacted_events": 3}
    assert retention(now=NOW) == {}
    assert _redacted_sessions(db) == {"sess_active": 3, "sess_recent": 1}


def test_redacted_retention_unparseable_timestamp_is_never_old(tmp_path):
    db = _transcript_db(tmp_path)
    _seed_redacted(db, "sess_bad", ["not-a-timestamp"])
    assert PlatformRetention(None, transcript_db=db)(now=NOW) == {}
    assert _redacted_sessions(db) == {"sess_bad": 1}


def test_redacted_retention_runs_alongside_platform_tables(tmp_path):
    platform = _db(tmp_path)
    _seed(platform, 3)
    transcripts = _transcript_db(tmp_path)
    _seed_redacted(transcripts, "sess_idle", [_iso(NOW - 40 * DAY_S)])
    pruned = PlatformRetention(platform, transcript_db=transcripts)(now=NOW)
    assert pruned["redacted_events"] == 1
    assert pruned["platform_usage_events"] == 3
    # 缺失的 transcript 库是 no-op，不会被创建
    assert PlatformRetention(None, transcript_db=tmp_path / "absent.db")(now=NOW) == {}
    assert not (tmp_path / "absent.db").exists()


def test_bootstrap_wires_transcript_db_into_retention(tmp_path):
    from types import SimpleNamespace

    from hub.bootstrap import _platform_retention

    cfg = SimpleNamespace(platform_db=tmp_path / "p.db",
                          session_repositories_enabled=True,
                          adoption_repositories_enabled=False,
                          session_transcript_root=tmp_path / "t.db")
    retention = _platform_retention(cfg, platform_repository=None)
    assert retention.db_path is None
    assert retention.transcript_db == tmp_path / "t.db"
    retention = _platform_retention(cfg, platform_repository=object())
    assert retention.db_path == tmp_path / "p.db"
    cfg.session_repositories_enabled = False
    assert _platform_retention(cfg, platform_repository=None) is None
