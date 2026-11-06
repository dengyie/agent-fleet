"""Bounded retention for append-only platform tables.

Several platform tables only ever grow: one ``platform_schedule_triggers`` row
per schedule occurrence (a 5 s schedule writes ~17k rows/day), one
``platform_usage_events`` row per provider request, system commands without an
owner, and ``run_events`` for every run.  This module prunes rows that are both
old and no longer referenced by live state, in small batches so a single sweep
never holds the write lock for long.

``redacted_events`` lives in the transcript repository's own SQLite file
(``session_transcript_root``); it is pruned here only when that path is passed
as ``transcript_db``.  Raw transcript rows keep their own, shorter
``retention_until`` purge in the transcript repository.

Rules (every table is optional; a missing table is skipped):

* schedule triggers: terminal state, ``updated_at`` older than the cutoff and
  strictly before the schedule's ``next_run_at`` (so the occurrence can never
  be claimed again);
* usage events: ``occurred_at`` older than the cutoff (90 days by default,
  well beyond ``MAX_WINDOW_S`` of the usage limiter);
* commands: ``owner_id IS NULL``, terminal status, ``updated_at`` older than
  the cutoff; child rows (outbox / reconciliations / postchecks) go first in
  the same transaction to keep foreign keys intact;
* run events: only for terminal runs whose ``updated_at`` is older than the
  cutoff; a run terminated before ``runs.request_summary`` existed gets its
  request projection frozen first, so run history keeps per-request metadata;
* redacted transcript events (30 days by default): whole sessions whose
  NEWEST ``emitted_at`` (RFC3339 text, compared via ``julianday``) is older
  than the cutoff — an active long session is never partially truncated, and
  a timestamp SQLite cannot parse never counts as old (fail safe);
* session search index (same 30-day horizon): ``purge_before`` drops entries
  older than the cutoff and ``delete_session`` drops every entry of a session
  whose redacted transcript was fully pruned (optional ``session_search``);
* execution windows (history horizon, 90 days by default): terminal windows
  whose ``updated_at`` is older than the cutoff, and abandoned non-terminal
  windows whose fixed ``expires_at`` is older than the cutoff, together with
  every child row;
* transcript ``audit`` and ``policy_signals`` (history horizon): rows whose
  ``_now_iso`` timestamp is older than the cutoff;
* task store (``task_db``, history horizon): terminal tasks whose queue TTL
  and every audit row are older than the cutoff, with their leases, results,
  result files, gates and handoff receipts; plus task audit rows older than
  the cutoff.

The history horizon is ``AGENT_FLEET_HISTORY_RETENTION_DAYS`` (see
``FleetConfig.history_retention_days``).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fleet_core.platform_command import TERMINAL_COMMAND_STATES
from hub.domain.execution_window import TERMINAL_WINDOW_STATES, WINDOW_STATES
from hub.domain.run import TERMINAL_RUN_STATES
from hub.domain.task import TERMINAL_STATES as TERMINAL_TASK_STATES
from hub.infrastructure.execution_window_repository import EXECUTION_WINDOW_CHILD_TABLES
from hub.infrastructure.platform_db import freeze_run_request_summary
from hub.infrastructure.platform_schedule_repository import TERMINAL_TRIGGER_STATES
from hub.infrastructure.sqlite import connect as sqlite_connect

logger = logging.getLogger(__name__)

DAY_S = 86400.0
#: Redacted transcripts (and the search index derived from them) are kept for
#: 30 days of session inactivity.
REDACTED_EVENT_RETENTION_DAYS = 30.0
#: Default horizon for operational history (tasks, audit, windows, signals).
HISTORY_RETENTION_DAYS = 90.0
MIN_HISTORY_RETENTION_DAYS = 7.0
MAX_HISTORY_RETENTION_DAYS = 3650.0
#: julianday('1970-01-01T00:00:00Z')：unix 秒 → SQLite julian day 的换算偏移
UNIX_EPOCH_JULIAN_DAY = 2440587.5
SQLITE_CONNECT_TIMEOUT_S = 10.0
COMMAND_CHILD_TABLES = (
    "platform_command_outbox",
    "platform_command_reconciliations",
    "platform_command_postchecks",
    "platform_service_postchecks",
)
TASK_CHILD_TABLES = (
    "result_files",
    "results",
    "leases",
    "task_gates",
    "handoff_receipts",
    "audit",
)


@dataclass(frozen=True)
class RetentionPolicy:
    schedule_trigger_days: float = 30.0
    usage_event_days: float = 90.0
    command_days: float = 30.0
    run_event_days: float = 30.0
    redacted_event_days: float = REDACTED_EVENT_RETENTION_DAYS
    execution_window_days: float = HISTORY_RETENTION_DAYS
    transcript_audit_days: float = HISTORY_RETENTION_DAYS
    policy_signal_days: float = HISTORY_RETENTION_DAYS
    task_days: float = HISTORY_RETENTION_DAYS
    task_audit_days: float = HISTORY_RETENTION_DAYS
    batch_size: int = 500
    max_batches: int = 20

    @classmethod
    def with_history_days(cls, days: float) -> "RetentionPolicy":
        """Policy whose operational-history horizons all equal ``days``."""
        return cls(execution_window_days=days, transcript_audit_days=days,
                   policy_signal_days=days, task_days=days, task_audit_days=days)


def _placeholders(values) -> tuple[str, tuple]:
    items = tuple(sorted(values))
    return ",".join("?" for _ in items), items


def _iso_seconds(ts: float) -> str:
    """Second-resolution UTC text matching the task store's ``_iso``."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _rfc3339(ts: float) -> str:
    """Microsecond UTC text matching the transcript/search ``_now_iso`` form."""
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


class PlatformRetention:
    """Callable retention job: ``retention(now=...) -> {table: deleted}``."""

    def __init__(self, db_path, policy: RetentionPolicy | None = None, *, clock=time.time,
                 transcript_db=None, task_db=None, session_search=None):
        self.db_path = Path(db_path) if db_path is not None else None
        self.transcript_db = Path(transcript_db) if transcript_db is not None else None
        self.task_db = Path(task_db) if task_db is not None else None
        # Duck-typed ``purge_before(rfc3339) -> int`` / ``delete_session(id) -> int``.
        self.session_search = session_search
        self.policy = policy or RetentionPolicy()
        self.clock = clock
        self._pruned_sessions: set[str] = set()

    def __call__(self, *, now: float | None = None) -> dict[str, int]:
        return self.run(now=now)

    def run(self, *, now: float | None = None) -> dict[str, int]:
        now = float(self.clock() if now is None else now)
        policy = self.policy
        deleted: dict[str, int] = {}
        if self.db_path is not None and self.db_path.exists():
            deleted.update(self._run_jobs(self.db_path, (
                ("platform_schedule_triggers", self._prune_triggers,
                 now - policy.schedule_trigger_days * DAY_S),
                ("platform_usage_events", self._prune_usage,
                 now - policy.usage_event_days * DAY_S),
                ("platform_commands", self._prune_commands,
                 now - policy.command_days * DAY_S),
                ("run_events", self._prune_run_events,
                 now - policy.run_event_days * DAY_S),
                ("execution_windows", self._prune_execution_windows,
                 now - policy.execution_window_days * DAY_S),
            )))
        self._pruned_sessions = set()
        if self.transcript_db is not None and self.transcript_db.exists():
            deleted.update(self._run_jobs(self.transcript_db, (
                ("redacted_events", self._prune_redacted_events,
                 now - policy.redacted_event_days * DAY_S),
                ("audit", self._prune_transcript_audit,
                 now - policy.transcript_audit_days * DAY_S, "transcript_audit"),
                ("policy_signals", self._prune_policy_signals,
                 now - policy.policy_signal_days * DAY_S),
            )))
        if self.session_search is not None:
            removed = self._prune_session_search(now - policy.redacted_event_days * DAY_S)
            if removed:
                deleted["session_search_entries"] = removed
        if self.task_db is not None and self.task_db.exists():
            deleted.update(self._run_jobs(self.task_db, (
                ("tasks", self._prune_tasks, now - policy.task_days * DAY_S),
                ("audit", self._prune_task_audit,
                 now - policy.task_audit_days * DAY_S, "task_audit"),
            )))
        if deleted:
            logger.info("platform retention pruned %s", deleted)
        return deleted

    def _run_jobs(self, db_path: Path, jobs) -> dict[str, int]:
        policy = self.policy
        conn = sqlite_connect(db_path, timeout=SQLITE_CONNECT_TIMEOUT_S)
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            deleted: dict[str, int] = {}
            for table, prune, cutoff, *label in jobs:
                if table not in tables:
                    continue
                total = 0
                for _ in range(max(1, int(policy.max_batches))):
                    count = self._batch(conn, prune, cutoff, tables)
                    total += count
                    if count < policy.batch_size:
                        break
                if total:
                    deleted[label[0] if label else table] = total
            return deleted
        finally:
            conn.close()

    @staticmethod
    def _batch(conn, prune, cutoff, tables) -> int:
        conn.execute("BEGIN IMMEDIATE")
        try:
            count = prune(conn, cutoff, tables)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        return count

    def _prune_triggers(self, conn, cutoff, tables) -> int:
        marks, states = _placeholders(TERMINAL_TRIGGER_STATES)
        return conn.execute(
            "DELETE FROM platform_schedule_triggers WHERE rowid IN ("
            "SELECT t.rowid FROM platform_schedule_triggers t "
            "JOIN platform_schedules s ON s.owner_id=t.owner_id AND s.schedule_id=t.schedule_id "
            f"WHERE t.state IN ({marks}) AND t.updated_at<? AND t.scheduled_at<s.next_run_at "
            "LIMIT ?)",
            (*states, cutoff, self.policy.batch_size),
        ).rowcount

    def _prune_usage(self, conn, cutoff, tables) -> int:
        return conn.execute(
            "DELETE FROM platform_usage_events WHERE rowid IN ("
            "SELECT rowid FROM platform_usage_events WHERE occurred_at<? LIMIT ?)",
            (cutoff, self.policy.batch_size),
        ).rowcount

    def _prune_commands(self, conn, cutoff, tables) -> int:
        marks, states = _placeholders(TERMINAL_COMMAND_STATES)
        ids = [row[0] for row in conn.execute(
            "SELECT command_id FROM platform_commands "
            f"WHERE owner_id IS NULL AND status IN ({marks}) AND updated_at<? LIMIT ?",
            (*states, cutoff, self.policy.batch_size),
        )]
        if not ids:
            return 0
        id_marks = ",".join("?" for _ in ids)
        for child in COMMAND_CHILD_TABLES:
            if child in tables:
                conn.execute(f"DELETE FROM {child} WHERE command_id IN ({id_marks})", ids)
        return conn.execute(
            f"DELETE FROM platform_commands WHERE command_id IN ({id_marks})", ids,
        ).rowcount

    def _prune_run_events(self, conn, cutoff, tables) -> int:
        if "runs" not in tables:
            return 0
        marks, states = _placeholders(TERMINAL_RUN_STATES)
        frozen = ""
        if "request_summary" in {row[1] for row in conn.execute("PRAGMA table_info(runs)")}:
            # Freeze before deleting, and only delete events of frozen runs.
            unfrozen = conn.execute(
                "SELECT r.run_id,r.state FROM runs r "
                f"WHERE r.state IN ({marks}) AND r.updated_at<? AND r.request_summary IS NULL "
                "AND EXISTS (SELECT 1 FROM run_events e WHERE e.run_id=r.run_id) LIMIT ?",
                (*states, cutoff, self.policy.batch_size),
            ).fetchall()
            for run_id, state in unfrozen:
                freeze_run_request_summary(conn, run_id, state)
            frozen = " AND r.request_summary IS NOT NULL"
        # Pick terminal runs through idx_runs_state_updated first (each EXISTS
        # is a primary-key probe), then delete their events by run_id; the
        # old join walked every run_events row on each batch.
        run_ids = [row[0] for row in conn.execute(
            "SELECT r.run_id FROM runs r "
            f"WHERE r.state IN ({marks}) AND r.updated_at<?{frozen} "
            "AND EXISTS (SELECT 1 FROM run_events e WHERE e.run_id=r.run_id) LIMIT ?",
            (*states, cutoff, self.policy.batch_size),
        )]
        if not run_ids:
            return 0
        id_marks = ",".join("?" for _ in run_ids)
        return conn.execute(
            "DELETE FROM run_events WHERE rowid IN ("
            f"SELECT rowid FROM run_events WHERE run_id IN ({id_marks}) LIMIT ?)",
            (*run_ids, self.policy.batch_size),
        ).rowcount

    def _prune_redacted_events(self, conn, cutoff, tables) -> int:
        # emitted_at 是 RFC3339 文本（可带小数秒/时区偏移），用 julianday 统一换算；
        # 按会话判定：会话内最新一条都早于 cutoff 才整会话清理，避免截断活跃长会话。
        # julianday 解析失败为 NULL → MAX 忽略/为 NULL → 永不视为过期（fail safe）。
        # 分组走覆盖索引 idx_redacted_session_emitted(session_id, emitted_at)，
        # 不再读取含 redacted_json 的整表行。
        cutoff_jd = cutoff / DAY_S + UNIX_EPOCH_JULIAN_DAY
        rows = conn.execute(
            "SELECT rowid, session_id FROM redacted_events WHERE session_id IN ("
            "SELECT session_id FROM redacted_events GROUP BY session_id "
            "HAVING MAX(julianday(upper(emitted_at)))<?) LIMIT ?",
            (cutoff_jd, self.policy.batch_size),
        ).fetchall()
        if not rows:
            return 0
        self._pruned_sessions.update(row[1] for row in rows)
        id_marks = ",".join("?" for _ in rows)
        return conn.execute(
            f"DELETE FROM redacted_events WHERE rowid IN ({id_marks})",
            tuple(row[0] for row in rows),
        ).rowcount

    def _prune_session_search(self, cutoff: float) -> int:
        """Mirror transcript retention into the session search index.

        Search failures never block the other retention jobs; they are logged
        and retried on the next hourly run (``purge_before`` is idempotent).
        """
        removed = 0
        sessions, self._pruned_sessions = self._pruned_sessions, set()
        if sessions and self.transcript_db is not None:
            conn = sqlite_connect(self.transcript_db, timeout=SQLITE_CONNECT_TIMEOUT_S)
            try:
                gone = [sid for sid in sorted(sessions) if conn.execute(
                    "SELECT 1 FROM redacted_events WHERE session_id=? LIMIT 1", (sid,),
                ).fetchone() is None]
            finally:
                conn.close()
            for session_id in gone:
                try:
                    removed += int(self.session_search.delete_session(session_id) or 0)
                except Exception:
                    logger.warning("session search delete_session failed", exc_info=True)
        try:
            removed += int(self.session_search.purge_before(_rfc3339(cutoff)) or 0)
        except Exception:
            logger.warning("session search purge_before failed", exc_info=True)
        return removed

    def _prune_execution_windows(self, conn, cutoff, tables) -> int:
        terminal_marks, terminal = _placeholders(TERMINAL_WINDOW_STATES)
        live_marks, live = _placeholders(WINDOW_STATES - TERMINAL_WINDOW_STATES)
        limit = self.policy.batch_size
        rows = conn.execute(
            "SELECT owner_id, window_id FROM execution_windows "
            f"WHERE state IN ({terminal_marks}) AND updated_at<? LIMIT ?",
            (*terminal, cutoff, limit),
        ).fetchall()
        if len(rows) < limit:
            # ``expires_at`` is fixed at creation (TTL <= 24h), so a live-state
            # window past it by the whole horizon was abandoned unread.
            rows += conn.execute(
                "SELECT owner_id, window_id FROM execution_windows "
                f"WHERE state IN ({live_marks}) AND expires_at<? LIMIT ?",
                (*live, cutoff, limit - len(rows)),
            ).fetchall()
        for owner_id, window_id in rows:
            for child in EXECUTION_WINDOW_CHILD_TABLES:
                if child in tables:
                    conn.execute(f"DELETE FROM {child} WHERE owner_id=? AND window_id=?",
                                 (owner_id, window_id))
            conn.execute("DELETE FROM execution_windows WHERE owner_id=? AND window_id=?",
                         (owner_id, window_id))
        return len(rows)

    def _prune_transcript_audit(self, conn, cutoff, tables) -> int:
        return conn.execute(
            "DELETE FROM audit WHERE id IN (SELECT id FROM audit WHERE ts<? LIMIT ?)",
            (_rfc3339(cutoff), self.policy.batch_size),
        ).rowcount

    def _prune_policy_signals(self, conn, cutoff, tables) -> int:
        return conn.execute(
            "DELETE FROM policy_signals WHERE rowid IN ("
            "SELECT rowid FROM policy_signals WHERE emitted_at<? LIMIT ?)",
            (_rfc3339(cutoff), self.policy.batch_size),
        ).rowcount

    def _prune_tasks(self, conn, cutoff, tables) -> int:
        marks, states = _placeholders(TERMINAL_TASK_STATES)
        cutoff_iso = _iso_seconds(cutoff)
        audit_guard = ""
        if "audit" in tables:
            # Every transition (complete/cancel/expire/retry) is audited, so a
            # recent audit row means recent activity regardless of the TTL.
            audit_guard = " AND NOT EXISTS (SELECT 1 FROM audit a WHERE a.task_id=t.task_id AND a.ts>=?)"
        params = (*states, cutoff_iso) + ((cutoff_iso,) if audit_guard else ())
        ids = [row[0] for row in conn.execute(
            "SELECT t.task_id FROM tasks t "
            f"WHERE t.state IN ({marks}) AND t.expires_at<?{audit_guard} LIMIT ?",
            (*params, self.policy.batch_size),
        )]
        if not ids:
            return 0
        id_marks = ",".join("?" for _ in ids)
        for child in TASK_CHILD_TABLES:
            if child in tables:
                conn.execute(f"DELETE FROM {child} WHERE task_id IN ({id_marks})", ids)
        return conn.execute(f"DELETE FROM tasks WHERE task_id IN ({id_marks})", ids).rowcount

    def _prune_task_audit(self, conn, cutoff, tables) -> int:
        return conn.execute(
            "DELETE FROM audit WHERE id IN (SELECT id FROM audit WHERE ts<? LIMIT ?)",
            (_iso_seconds(cutoff), self.policy.batch_size),
        ).rowcount


__all__ = [
    "HISTORY_RETENTION_DAYS",
    "PlatformRetention",
    "REDACTED_EVENT_RETENTION_DAYS",
    "RetentionPolicy",
]
