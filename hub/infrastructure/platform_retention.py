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
  a timestamp SQLite cannot parse never counts as old (fail safe).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from fleet_core.platform_command import TERMINAL_COMMAND_STATES
from hub.domain.run import TERMINAL_RUN_STATES
from hub.infrastructure.platform_db import freeze_run_request_summary
from hub.infrastructure.platform_schedule_repository import TERMINAL_TRIGGER_STATES
from hub.infrastructure.sqlite import connect as sqlite_connect

logger = logging.getLogger(__name__)

DAY_S = 86400.0
#: julianday('1970-01-01T00:00:00Z')：unix 秒 → SQLite julian day 的换算偏移
UNIX_EPOCH_JULIAN_DAY = 2440587.5
SQLITE_CONNECT_TIMEOUT_S = 10.0
COMMAND_CHILD_TABLES = (
    "platform_command_outbox",
    "platform_command_reconciliations",
    "platform_command_postchecks",
    "platform_service_postchecks",
)


@dataclass(frozen=True)
class RetentionPolicy:
    schedule_trigger_days: float = 30.0
    usage_event_days: float = 90.0
    command_days: float = 30.0
    run_event_days: float = 30.0
    redacted_event_days: float = 30.0
    batch_size: int = 500
    max_batches: int = 20


def _placeholders(values) -> tuple[str, tuple]:
    items = tuple(sorted(values))
    return ",".join("?" for _ in items), items


class PlatformRetention:
    """Callable retention job: ``retention(now=...) -> {table: deleted}``."""

    def __init__(self, db_path, policy: RetentionPolicy | None = None, *, clock=time.time,
                 transcript_db=None):
        self.db_path = Path(db_path) if db_path is not None else None
        self.transcript_db = Path(transcript_db) if transcript_db is not None else None
        self.policy = policy or RetentionPolicy()
        self.clock = clock

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
            )))
        if self.transcript_db is not None and self.transcript_db.exists():
            deleted.update(self._run_jobs(self.transcript_db, (
                ("redacted_events", self._prune_redacted_events,
                 now - policy.redacted_event_days * DAY_S),
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
            for table, prune, cutoff in jobs:
                if table not in tables:
                    continue
                total = 0
                for _ in range(max(1, int(policy.max_batches))):
                    count = self._batch(conn, prune, cutoff, tables)
                    total += count
                    if count < policy.batch_size:
                        break
                if total:
                    deleted[table] = total
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
        return conn.execute(
            "DELETE FROM run_events WHERE rowid IN ("
            "SELECT e.rowid FROM run_events e JOIN runs r ON r.run_id=e.run_id "
            f"WHERE r.state IN ({marks}) AND r.updated_at<?{frozen} LIMIT ?)",
            (*states, cutoff, self.policy.batch_size),
        ).rowcount

    def _prune_redacted_events(self, conn, cutoff, tables) -> int:
        # emitted_at 是 RFC3339 文本（可带小数秒/时区偏移），用 julianday 统一换算；
        # 按会话判定：会话内最新一条都早于 cutoff 才整会话清理，避免截断活跃长会话。
        # julianday 解析失败为 NULL → MAX 忽略/为 NULL → 永不视为过期（fail safe）。
        cutoff_jd = cutoff / DAY_S + UNIX_EPOCH_JULIAN_DAY
        return conn.execute(
            "DELETE FROM redacted_events WHERE rowid IN ("
            "SELECT rowid FROM redacted_events WHERE session_id IN ("
            "SELECT session_id FROM redacted_events GROUP BY session_id "
            "HAVING MAX(julianday(upper(emitted_at)))<?) LIMIT ?)",
            (cutoff_jd, self.policy.batch_size),
        ).rowcount


__all__ = ["PlatformRetention", "RetentionPolicy"]
