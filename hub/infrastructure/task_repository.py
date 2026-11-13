"""Explicit-path SQLite repository for task and lease state.

The repository owns SQLite connection, transaction, schema, fencing, and
result persistence semantics. It does not depend on Flask or module globals.
"""


import contextlib
import json
import logging
import math
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any
from hub.domain.task import (
    LEASE_HOLDING_STATES, TASK_STATES, DomainError, TaskStopObservation, can_cancel, can_continue, can_pause, can_retry,
    holds_lease,
)
from hub.infrastructure.sqlite import connect as sqlite_connect
from fleet_core.task_result import (
    bound_patch,
    normalize_test_summary as _normalize_test_summary,
)

from hub.domain.handoff import HandoffConflict


LEASE_TTL_S = 300
TASK_TTL_S = 24 * 3600
SQLITE_CONNECT_TIMEOUT_S = 10.0

_log = logging.getLogger(__name__)


def _sql_in(states) -> str:
    """Render a domain state tuple as a literal SQL ``IN (...)`` list."""
    return "(" + ",".join("'" + s + "'" for s in states) + ")"


_TASK_STATES_SQL = _sql_in(TASK_STATES)
_LEASE_HOLDING_STATES_SQL = _sql_in(LEASE_HOLDING_STATES)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY,
  client_token TEXT UNIQUE,
  machine TEXT NOT NULL,
  agent_type TEXT NOT NULL,
  project TEXT NOT NULL,
  instruction TEXT NOT NULL CHECK(length(instruction) <= 2000),
  requested_by TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN """ + _TASK_STATES_SQL + """),
  attempt_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leases (
  attempt_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  runner_id TEXT NOT NULL,
  nonce TEXT NOT NULL,
  leased_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS handoff_receipts (
  client_token TEXT PRIMARY KEY,
  request_digest TEXT NOT NULL,
  source_session_id TEXT NOT NULL,
  handoff_id TEXT NOT NULL UNIQUE,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  attempt_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lease_task ON leases(task_id);
CREATE TABLE IF NOT EXISTS legacy_task_origins (
  task_id TEXT PRIMARY KEY REFERENCES tasks(task_id), owner_id TEXT NOT NULL, run_id TEXT NOT NULL,
  repository_id TEXT, UNIQUE(owner_id,run_id)
);
CREATE TABLE IF NOT EXISTS task_source_receipts (
  task_id TEXT NOT NULL REFERENCES tasks(task_id), attempt_id TEXT PRIMARY KEY,
  artifact_id TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
  recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_submission_cancellations (
  client_token TEXT PRIMARY KEY,
  requested_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS results (
  attempt_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  exit_code INTEGER,
  log_summary TEXT CHECK(length(log_summary) <= 10240),
  diff_stat TEXT CHECK(length(diff_stat) <= 5120),
  diff_patch TEXT,
  test_summary TEXT,
  duration_s REAL,
  finished_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_gates (
  task_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('confirm_before_dispatch')),
  state TEXT NOT NULL CHECK(state IN ('pending','confirmed','rejected')),
  requested_by TEXT NOT NULL,
  decided_by TEXT,
  decided_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS result_files (
  attempt_id TEXT NOT NULL,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  path TEXT NOT NULL,
  content TEXT NOT NULL CHECK(length(content) <= 16384),
  truncated INTEGER NOT NULL,
  redacted INTEGER NOT NULL,
  bytes INTEGER NOT NULL,
  PRIMARY KEY (attempt_id, path)
);
CREATE INDEX IF NOT EXISTS idx_result_files_task ON result_files(task_id);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  task_id TEXT,
  detail TEXT
);
"""

def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))

@contextlib.contextmanager
def _tx(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")

def _audit(conn, actor, action, task_id=None, detail=None, now=None):
    conn.execute(
        "INSERT INTO audit (ts, actor, action, task_id, detail) VALUES (?,?,?,?,?)",
        (_iso(now if now is not None else time.time()), actor, action, task_id,
         json.dumps(detail, ensure_ascii=False) if detail is not None else None),
    )

class SqliteTaskRepository:
    """Task persistence bound to one explicitly supplied database path."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        # WAL negotiation can fail immediately despite busy_timeout; the shared
        # helper retries only the idempotent setup until the deadline.
        return sqlite_connect(self.db_path, timeout=SQLITE_CONNECT_TIMEOUT_S)


    def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if self.db_path.exists():
            try:
                with contextlib.closing(self._connect()) as conn:
                    ok = conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            except sqlite3.DatabaseError as exc:
                code = getattr(exc, "sqlite_errorcode", None)
                # SQLite primary codes CORRUPT=11 and NOTADB=26 are stable;
                # Python 3.10 exposes only their exact SQLite error messages.
                corrupt = (code & 0xff) in (11, 26) if code is not None else str(exc) in (
                    "database disk image is malformed", "file is not a database",
                )
                if not corrupt:
                    raise
                ok = False
            if not ok:
                self._quarantine_corrupt_database()
        conn = self._connect()
        try:
            # Disable FK enforcement only on this initialization connection;
            # table replacement is checked before committing and it then closes.
            conn.execute("PRAGMA foreign_keys=OFF")
            with conn:
                # executescript commits any preceding transaction, so BEGIN
                # belongs in the script, before the first schema change.
                conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
                self._migrate_schema(conn)
        finally:
            conn.close()

    def _quarantine_corrupt_database(self) -> None:
        """Move the corrupt db and its -wal/-shm sidecars aside together.

        Leaving a stale ``-wal`` next to the rebuilt database would let SQLite
        replay old frames into the fresh file. Every quarantine gets a unique
        timestamped suffix so a second corruption never overwrites evidence.
        """
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        suffix = ".corrupt-" + stamp
        n = 0
        while any(
            self.db_path.with_name(self.db_path.name + sidecar + suffix).exists()
            for sidecar in ("", "-wal", "-shm")
        ):
            n += 1
            suffix = f".corrupt-{stamp}-{n}"
        moved = []
        for sidecar in ("", "-wal", "-shm"):
            source = self.db_path.with_name(self.db_path.name + sidecar)
            target = source.with_name(source.name + suffix)
            try:
                os.replace(source, target)
            except FileNotFoundError:
                continue
            moved.append(target.name)
        _log.error("task database failed integrity check; quarantined as %s and rebuilt",
                   ", ".join(moved) or "(nothing)")

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        """Apply v4 changes inside init's serialized schema transaction.

        CREATE TABLE IF NOT EXISTS does not alter old ``results`` / ``tasks``
        check constraints. Rebuild ``tasks`` when the state CHECK lacks
        ``paused`` so pause/continue can persist. New result columns are
        nullable; column inspection runs under the same writer transaction.
        """
        cols = {
            row[1] for row in conn.execute("PRAGMA table_info(results)").fetchall()
        }
        if "diff_patch" not in cols:
            conn.execute("ALTER TABLE results ADD COLUMN diff_patch TEXT")
        if "test_summary" not in cols:
            conn.execute("ALTER TABLE results ADD COLUMN test_summary TEXT")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS task_gates ("
            " task_id TEXT PRIMARY KEY,"
            " kind TEXT NOT NULL CHECK(kind IN ('confirm_before_dispatch')),"
            " state TEXT NOT NULL CHECK(state IN ('pending','confirmed','rejected')),"
            " requested_by TEXT NOT NULL,"
            " decided_by TEXT,"
            " decided_at TEXT,"
            " created_at TEXT NOT NULL)"
        )
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()
        if sql and sql[0] and "'paused'" not in sql[0]:
            conn.execute(
                "CREATE TABLE tasks_v4 ("
                " task_id TEXT PRIMARY KEY,"
                " client_token TEXT UNIQUE,"
                " machine TEXT NOT NULL,"
                " agent_type TEXT NOT NULL,"
                " project TEXT NOT NULL,"
                " instruction TEXT NOT NULL CHECK(length(instruction) <= 2000),"
                " requested_by TEXT NOT NULL,"
                " state TEXT NOT NULL CHECK(state IN " + _TASK_STATES_SQL + "),"
                " attempt_id TEXT NOT NULL,"
                " created_at TEXT NOT NULL,"
                " expires_at TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO tasks_v4 (task_id, client_token, machine, agent_type,"
                " project, instruction, requested_by, state, attempt_id,"
                " created_at, expires_at) SELECT task_id, client_token, machine,"
                " agent_type, project, instruction, requested_by, state,"
                " attempt_id, created_at, expires_at FROM tasks"
            )
            conn.execute("DROP TABLE tasks")
            conn.execute("ALTER TABLE tasks_v4 RENAME TO tasks")
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise sqlite3.IntegrityError("task schema migration violates foreign keys")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_attempt ON tasks(attempt_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_live_lease ON tasks(state,attempt_id) WHERE state IN ('leased','running')")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_task_source_receipts_task ON task_source_receipts(task_id)")
        # 附加式索引（回滚安全）：lease_task 按机器取最早 queued；expire_* 与保留
        # 任务按 state+expires_at 定位；保留清理按 task_id/ts 删除子表与审计。
        for ddl in (
            "CREATE INDEX IF NOT EXISTS idx_tasks_machine_state_created"
            " ON tasks(machine, state, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_tasks_state_expires ON tasks(state, expires_at)",
            "CREATE INDEX IF NOT EXISTS idx_results_task ON results(task_id, finished_at)",
            "CREATE INDEX IF NOT EXISTS idx_handoff_receipts_task ON handoff_receipts(task_id)",
            "CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts)",
            "CREATE INDEX IF NOT EXISTS idx_audit_task ON audit(task_id, ts)",
        ):
            conn.execute(ddl)

    def create_task(self, *, machine, agent_type, project, instruction, requested_by,
                    client_token=None, ttl_s=TASK_TTL_S, now=None, confirm=False,
                    handoff=None, legacy_origin=None):
        """创建任务；(client_token 命中, False) 幂等返回已有任务。

        超长 instruction 服务端截断至 2000 字符后再 INSERT（不依赖 SQLite CHECK
        抛错），符合全局约束「超限服务端截断」。
        """
        instruction = instruction[:2000]
        now = time.time() if now is None else now
        task_id = "t-" + uuid.uuid4().hex[:12]
        conn = self._connect()
        try:
            with _tx(conn):
                if client_token:
                    existing = conn.execute(
                        "SELECT * FROM tasks WHERE client_token=?", (client_token,)).fetchone()
                    if existing:
                        if legacy_origin is not None:
                            origin = conn.execute("SELECT owner_id,run_id,repository_id FROM legacy_task_origins WHERE task_id=?",
                                                  (existing["task_id"],)).fetchone()
                            if origin is None or dict(origin) != legacy_origin:
                                raise DomainError("legacy_task_conflict", "legacy_task_conflict")
                        if handoff is not None:
                            receipt = conn.execute(
                                "SELECT request_digest FROM handoff_receipts WHERE client_token=?",
                                (client_token,)).fetchone()
                            if not receipt or receipt["request_digest"] != handoff["request_digest"]:
                                raise HandoffConflict()
                        return dict(existing), False
                    cancelled = conn.execute("SELECT requested_by FROM task_submission_cancellations WHERE client_token=?", (client_token,)).fetchone()
                    if cancelled is not None:
                        raise DomainError("task_submission_cancelled", "task_submission_cancelled")
                attempt_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO tasks (task_id, client_token, machine, agent_type, project,"
                    " instruction, requested_by, state, attempt_id, created_at, expires_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (task_id, client_token, machine, agent_type, project, instruction,
                     requested_by, "queued", attempt_id, _iso(now), _iso(now + ttl_s)))
                if handoff is not None:
                    conn.execute(
                        "INSERT INTO handoff_receipts (client_token, request_digest, source_session_id,"
                        " handoff_id, task_id, attempt_id) VALUES (?,?,?,?,?,?)",
                        (client_token, handoff["request_digest"], handoff["source_session_id"],
                         "h-" + uuid.uuid4().hex, task_id, attempt_id))
                if legacy_origin is not None:
                    if set(legacy_origin) != {"owner_id", "run_id", "repository_id"} or requested_by != legacy_origin["owner_id"]:
                        raise DomainError("invalid_legacy_origin", "invalid_legacy_origin")
                    conn.execute("INSERT INTO legacy_task_origins VALUES(?,?,?,?)",
                                 (task_id, legacy_origin["owner_id"], legacy_origin["run_id"], legacy_origin["repository_id"]))
                if confirm:
                    conn.execute(
                        "INSERT INTO task_gates (task_id, kind, state, requested_by,"
                        " created_at) VALUES (?,?,?,?,?)",
                        (task_id, "confirm_before_dispatch", "pending", requested_by,
                         _iso(now)))
                _audit(conn, requested_by, "create_task", task_id,
                       {"machine": machine, "agent_type": agent_type, "project": project,
                        "instruction_len": len(instruction),
                        "confirm": bool(confirm)}, now)
                row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            return dict(row), True
        finally:
            conn.close()

    def _decode_result(self, row):
        if not row:
            return None
        out = dict(row)
        raw = out.get("test_summary")
        if isinstance(raw, str) and raw:
            out["test_summary"] = _normalize_test_summary(raw)
        elif raw in ("", None):
            out["test_summary"] = None
        return out

    def get_handoff_receipt(self, client_token: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT request_digest, handoff_id, task_id, attempt_id"
                " FROM handoff_receipts WHERE client_token=?", (client_token,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_task_for_attempt(self, attempt_id: str) -> dict | None:
        """Indexed source binding; retain the result of this exact attempt."""
        conn = self._connect()
        try:
            binding = conn.execute(
                "SELECT task_id FROM tasks WHERE attempt_id=? UNION ALL"
                " SELECT task_id FROM leases WHERE attempt_id=? UNION ALL"
                " SELECT task_id FROM results WHERE attempt_id=? LIMIT 1",
                (attempt_id, attempt_id, attempt_id)).fetchone()
            if not binding:
                return None
            row = conn.execute(
                "SELECT task_id, project, machine, instruction FROM tasks WHERE task_id=?",
                (binding["task_id"],)).fetchone()
            result = conn.execute(
                "SELECT exit_code, log_summary, test_summary FROM results WHERE attempt_id=?",
                (attempt_id,)).fetchone()
            return {**dict(row), "result": self._decode_result(result)} if row else None
        finally:
            conn.close()

    def get_session_search_metadata(self, attempt_id: str, machine_id: str) -> dict | None:
        """Resolve an existing session attempt without exposing task content."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT project,agent_type FROM tasks WHERE attempt_id=? AND machine=? LIMIT 1",
                (attempt_id, machine_id),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def get_task(self, task_id):
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                return None
            out = dict(row)
            res = conn.execute(
                "SELECT * FROM results WHERE task_id=? ORDER BY finished_at DESC LIMIT 1",
                (task_id,)).fetchone()
            out["result"] = self._decode_result(res)
            gate = conn.execute(
                "SELECT * FROM task_gates WHERE task_id=?", (task_id,)).fetchone()
            out["gate"] = dict(gate) if gate else None
            return out
        finally:
            conn.close()

    def list_tasks(self, machine=None, state=None, limit=50):
        sql = "SELECT * FROM tasks"
        conds, params = [], []
        if machine:
            conds.append("machine=?")
            params.append(machine)
        if state:
            conds.append("state=?")
            params.append(state)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        conn = self._connect()
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def lease_task(self, *, machine, runner_id, lease_ttl_s=LEASE_TTL_S, now=None):
        """原子领取：BEGIN IMMEDIATE 内 选任务→改状态→作废旧 lease→写新 lease。"""
        now = time.time() if now is None else now
        now_iso = _iso(now)
        conn = self._connect()
        try:
            with _tx(conn):
                row = conn.execute(
                    "SELECT t.*,o.repository_id AS source_repository_id FROM tasks t"
                    " LEFT JOIN task_gates g ON g.task_id=t.task_id"
                    " LEFT JOIN legacy_task_origins o ON o.task_id=t.task_id"
                    # ``+`` keeps expires_at off the index choice so the planner seeks
                    # idx_tasks_machine_state_created (machine, state) in created_at order.
                    " WHERE t.machine=? AND t.state='queued' AND +t.expires_at>?"
                    " AND (g.task_id IS NULL OR g.state='confirmed')"
                    " ORDER BY t.created_at LIMIT 1", (machine, now_iso)).fetchone()
                if row is None:
                    return None
                attempt_id = uuid.uuid4().hex
                nonce = uuid.uuid4().hex
                expires = _iso(now + lease_ttl_s)
                conn.execute("UPDATE tasks SET state='leased', attempt_id=? WHERE task_id=?",
                             (attempt_id, row["task_id"]))
                conn.execute("UPDATE leases SET expires_at=? WHERE task_id=? AND expires_at>?",
                             (now_iso, row["task_id"], now_iso))
                conn.execute(
                    "INSERT INTO leases (attempt_id, task_id, runner_id, nonce, leased_at, expires_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (attempt_id, row["task_id"], runner_id, nonce, now_iso, expires))
                _audit(conn, runner_id, "lease_task", row["task_id"],
                       {"attempt_id": attempt_id}, now)
            return {
                "task_id": row["task_id"], "machine": row["machine"],
                "agent_type": row["agent_type"], "project": row["project"],
                "instruction": row["instruction"], "attempt_id": attempt_id,
                "nonce": nonce, "lease_ttl_s": lease_ttl_s, "lease_expires_at": expires,
                "source_capture": row["source_repository_id"] is not None,
                "source_repository_id": row["source_repository_id"],
            }
        finally:
            conn.close()

    @contextlib.contextmanager
    def source_fence(self, machine: str, attempt_id: str, nonce: str, *, now: float, recovered: bool = False):
        """Serialize only the final source write against Task cancellation/release."""
        conn = self._connect()
        try:
            with _tx(conn):
                row = conn.execute("SELECT t.task_id,t.state,t.attempt_id,t.machine,t.requested_by,t.client_token,"
                    "t.project,t.agent_type,t.instruction,l.nonce,l.expires_at,o.owner_id,o.run_id,o.repository_id "
                    "FROM leases l JOIN tasks t USING(task_id) JOIN legacy_task_origins o USING(task_id) "
                    "WHERE l.attempt_id=?", (attempt_id,)).fetchone()
                if row is None or (row["machine"], row["attempt_id"], row["nonce"]) != (machine, attempt_id, nonce):
                    raise DomainError("lease_mismatch", "lease_mismatch")
                if not recovered and (row["state"] not in ("leased", "running") or row["expires_at"] <= _iso(now)):
                    raise DomainError("lease_mismatch", "lease_mismatch")
                origin = conn.execute("SELECT owner_id,run_id,repository_id FROM legacy_task_origins WHERE task_id=?", (row["task_id"],)).fetchone()
                receipt = conn.execute("SELECT artifact_id,sha256,size FROM task_source_receipts WHERE task_id=? AND attempt_id=?",
                                       (row["task_id"], attempt_id)).fetchone()
                row = {**dict(row), "origin_owner_id": origin["owner_id"] if origin else None,
                       "origin_run_id": origin["run_id"] if origin else None,
                       "repository_id": origin["repository_id"] if origin else None,
                       "source_receipt": dict(receipt) if receipt else None}
                yield {**row, "_connection": conn}
        finally:
            conn.close()


    def source_origin(self, machine: str, attempt_id: str, nonce: str) -> dict[str, Any] | None:
        with contextlib.closing(self._connect()) as conn:
            row = conn.execute("SELECT t.task_id,t.state,t.attempt_id,t.machine,t.requested_by,t.client_token,"
                "t.project,t.agent_type,t.instruction,l.expires_at,o.owner_id,o.run_id,o.repository_id "
                "FROM leases l JOIN tasks t USING(task_id) JOIN legacy_task_origins o USING(task_id) "
                "WHERE l.attempt_id=? AND l.nonce=? AND t.machine=?", (attempt_id, nonce, machine)).fetchone()
            return dict(row) if row is not None else None


    def source_receipt(self, machine: str, attempt_id: str, nonce: str) -> dict[str, Any] | None:
        with contextlib.closing(self._connect()) as conn:
            row = conn.execute("SELECT r.artifact_id,r.sha256,r.size FROM task_source_receipts r "
                "JOIN leases l USING(attempt_id) JOIN tasks t USING(task_id) "
                "WHERE r.attempt_id=? AND l.nonce=? AND t.machine=?", (attempt_id, nonce, machine)).fetchone()
            return dict(row) if row is not None else None


    def record_source_receipt(self, *, task_id: str, attempt_id: str, artifact_id: str, sha256: str, size: int,
                              now: float, connection: sqlite3.Connection | None = None, recovered: bool = False) -> None:
        owned = connection is None
        conn = self._connect() if owned else connection
        try:
            if owned:
                transaction = _tx(conn)
            else:
                if not conn.in_transaction:
                    raise DomainError("invalid_source_transaction", "invalid_source_transaction")
                transaction = contextlib.nullcontext()
            with transaction:
                lease = conn.execute("SELECT l.task_id,l.expires_at,t.state,t.attempt_id FROM leases l JOIN tasks t USING(task_id) "
                                     "WHERE l.attempt_id=?", (attempt_id,)).fetchone()
                if lease is None or lease["task_id"] != task_id or lease["attempt_id"] != attempt_id:
                    raise DomainError("lease_mismatch", "lease_mismatch")
                if not recovered and (lease["state"] not in ("leased", "running") or lease["expires_at"] <= _iso(now)):
                    raise DomainError("lease_mismatch", "lease_mismatch")
                prior = conn.execute("SELECT artifact_id,sha256,size FROM task_source_receipts WHERE attempt_id=?", (attempt_id,)).fetchone()
                if prior is not None:
                    if tuple(prior) != (artifact_id, sha256, size):
                        raise DomainError("source_upload_conflict", "source_upload_conflict")
                    return
                conn.execute("INSERT INTO task_source_receipts VALUES(?,?,?,?,?,?)",
                             (task_id, attempt_id, artifact_id, sha256, int(size), _iso(now)))
        finally:
            if owned:
                conn.close()


    def heartbeat(self, *, attempt_id, nonce, machine=None,
                  extend_s=LEASE_TTL_S, now=None):
        """续租；返回 {"task_id", "lease_expires_at"}，lease 无效/过期返回 None。

        仅接受「当前 attempt + 任务仍 leased/running」的续租；租约存在但 attempt 已不是任务
        最新 attempt、或任务已被取消/完成/过期（非 leased/running）时返回 None 且不扫延。
        """
        now = time.time() if now is None else now
        conn = self._connect()
        try:
            with _tx(conn):
                lease = conn.execute("SELECT * FROM leases WHERE attempt_id=?",
                                     (attempt_id,)).fetchone()
                # 栅栏是 attempt_id + nonce + 任务仍 leased/running，而不是墙钟：
                # TTL 已过但 expire_leases 尚未回收（任务未回 queued、未被重派）时，
                # 当前 attempt 仍是唯一持有者，续租必须成功，否则 sweep 间隙内的
                # 慢心跳会被误判为租约丢失。
                if not lease or lease["nonce"] != nonce:
                    return None
                # 仅接受当前 attempt + 任务仍 leased/running：取消后旧 lease 的心跳必须失败，
                # 否则 cancel 的 lease 失效语义被心跳击穿（lease 行会被顺延）。
                task = conn.execute("SELECT * FROM tasks WHERE task_id=?",
                                    (lease["task_id"],)).fetchone()
                if not task or task["attempt_id"] != attempt_id:
                    return None
                if machine is not None and task["machine"] != machine:
                    return None
                if not holds_lease(task["state"]):
                    return None
                new_exp = _iso(now + extend_s)
                conn.execute("UPDATE leases SET expires_at=? WHERE attempt_id=?",
                             (new_exp, attempt_id))
                conn.execute("UPDATE tasks SET state='running' WHERE task_id=? AND state='leased'",
                             (lease["task_id"],))
            return {"task_id": lease["task_id"], "lease_expires_at": new_exp}
        finally:
            conn.close()

    def complete_task(self, *, attempt_id, nonce, exit_code, log_summary, diff_stat, duration_s,
                      now=None, files=None, diff_patch=None, test_summary=None,
                      machine=None):
        """回传结果；attempt_id PK 撞 即幂等。返回 {"task_id","state","stored"} 或 None。

        log_summary/diff_stat 服务端截断后再 INSERT（不依赖 SQLite CHECK 抛错），
        符合全局约束「超限服务端截断」。
        ``files`` 是 runner 附带的有界快照（调用方已 sanitize）；幂等
        重交不替换已落库快照。

        仅接受「当前 attempt + nonce + 任务仍 leased/running」的完成：lease TTL
        已过但 expire_leases 尚未把任务打回 queued 时仍接受（无人可能接手）；
        一旦被回收/重派，attempt 或状态不再匹配即拒绝。
        同一 attempt 重复提交直接返回已落库状态（幂等），不被过期/陈旧 attempt 覆写。
        """
        log_summary = (log_summary or "")[:10240]
        diff_stat = (diff_stat or "")[:5120]
        if diff_patch is None:
            patch_value = None
        else:
            patch_value = bound_patch(diff_patch)[0]
        summary_obj = _normalize_test_summary(test_summary)
        summary_json = (
            json.dumps(summary_obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            if summary_obj is not None else None
        )
        now = time.time() if now is None else now
        conn = self._connect()
        try:
            with _tx(conn):
                lease = conn.execute("SELECT * FROM leases WHERE attempt_id=?",
                                     (attempt_id,)).fetchone()
                if not lease or lease["nonce"] != nonce:
                    return None
                if machine is not None:
                    bound = conn.execute(
                        "SELECT machine FROM tasks WHERE task_id=?", (lease["task_id"],)
                    ).fetchone()
                    if not bound or bound["machine"] != machine:
                        return None
                # 幂等：同一 attempt 已有结果，返回已持久化的状态（不按新 exit_code 重算）
                existing = conn.execute(
                    "SELECT task_id, exit_code FROM results WHERE attempt_id=?",
                    (attempt_id,)).fetchone()
                if existing:
                    persisted = "succeeded" if int(existing["exit_code"]) == 0 else "failed"
                    return {"task_id": existing["task_id"], "state": persisted, "stored": False}
                # 仅接受当前 attempt：任务 attempt_id==此次、状态 leased/running
                task = conn.execute("SELECT * FROM tasks WHERE task_id=?",
                                    (lease["task_id"],)).fetchone()
                if not task or task["attempt_id"] != attempt_id:
                    return None
                if not holds_lease(task["state"]):
                    return None
                origin = conn.execute("SELECT 1 FROM legacy_task_origins WHERE task_id=? AND repository_id IS NOT NULL", (lease["task_id"],)).fetchone()
                source = conn.execute("SELECT 1 FROM task_source_receipts WHERE task_id=? AND attempt_id=?",
                                      (lease["task_id"], attempt_id)).fetchone()
                if origin is not None and int(exit_code) == 0 and source is None:
                    return None
                state = "succeeded" if int(exit_code) == 0 else "failed"
                conn.execute(
                    "INSERT INTO results (attempt_id, task_id, exit_code, log_summary,"
                    " diff_stat, diff_patch, test_summary, duration_s, finished_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (attempt_id, lease["task_id"], int(exit_code), log_summary,
                     diff_stat, patch_value, summary_json, duration_s, _iso(now)))
                for item in files or ():
                    if not isinstance(item, dict):
                        continue
                    path = str(item.get("path") or "")[:256]
                    content = str(item.get("content") or "")[:16384]
                    if not path:
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO result_files (attempt_id, task_id, path,"
                        " content, truncated, redacted, bytes) VALUES (?,?,?,?,?,?,?)",
                        (attempt_id, lease["task_id"], path, content,
                         1 if item.get("truncated") else 0,
                         1 if item.get("redacted") else 0,
                         int(item.get("bytes") or len(content.encode("utf-8")))))
                conn.execute("UPDATE tasks SET state=? WHERE task_id=?",
                             (state, lease["task_id"]))
                conn.execute("UPDATE leases SET expires_at=? WHERE attempt_id=?",
                             (_iso(now), attempt_id))
                _audit(conn, lease["runner_id"], "complete_task", lease["task_id"],
                       {"exit_code": int(exit_code),
                        "file_count": len(files or ())}, now)
            return {"task_id": lease["task_id"], "state": state, "stored": True}
        finally:
            conn.close()

    def list_result_files(self, task_id):
        """Hub-local snapshot metadata for the task's current attempt."""
        conn = self._connect()
        try:
            task = conn.execute(
                "SELECT attempt_id FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if not task:
                return []
            rows = conn.execute(
                "SELECT path, bytes, truncated, redacted FROM result_files "
                "WHERE task_id=? AND attempt_id=? ORDER BY path",
                (task_id, task["attempt_id"])).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_result_file(self, task_id, path):
        """Hub-local snapshot body for the current attempt, or None."""
        conn = self._connect()
        try:
            task = conn.execute(
                "SELECT attempt_id FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if not task:
                return None
            row = conn.execute(
                "SELECT path, content, bytes, truncated, redacted FROM result_files "
                "WHERE task_id=? AND attempt_id=? AND path=?",
                (task_id, task["attempt_id"], path)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def audit_action(self, actor, action, task_id, detail=None, now=None):
        """Append one bounded audit row (operator file reads, etc.)."""
        conn = self._connect()
        try:
            with _tx(conn):
                _audit(conn, actor, action, task_id, detail, now)
        finally:
            conn.close()

    def expire_leases(self, now=None):
        """过期 lease → 对应 leased/running 任务回 queued。返回重派的 task_id 列表。"""
        now = time.time() if now is None else now
        now_iso = _iso(now)
        conn = self._connect()
        try:
            with _tx(conn):
                # 从活跃任务出发（idx_tasks_state_expires 定位 leased/running），按
                # attempt_id 主键探测其当前 lease：历史 lease 行不再被逐行扫描。
                # 仅处理「当前 attempt」：lease 的 attempt_id 必须仍是任务的最新
                # attempt_id，否则说明该 lease 已被重派/完成取代，不能把新运行打回 queued。
                rows = conn.execute(
                    "SELECT l.task_id, l.attempt_id, t.state,o.task_id AS workflow_task FROM tasks t"
                    " JOIN leases l ON l.attempt_id=t.attempt_id AND l.task_id=t.task_id"
                    " LEFT JOIN legacy_task_origins o ON o.task_id=t.task_id"
                    f" WHERE t.state IN {_LEASE_HOLDING_STATES_SQL} AND l.expires_at<=?"
                    " ORDER BY l.expires_at, l.task_id",
                    (now_iso,)).fetchall()
                requeued = []
                for lease in rows:
                    if holds_lease(lease["state"]):
                        state = "expired" if lease["workflow_task"] else "queued"
                        conn.execute("UPDATE tasks SET state=? WHERE task_id=?", (state, lease["task_id"]))
                        _audit(conn, "hub", "lease_expired", lease["task_id"],
                               {"attempt_id": lease["attempt_id"], "state": state}, now)
                        if state == "queued":
                            requeued.append(lease["task_id"])
            return requeued
        finally:
            conn.close()

    def expire_tasks(self, now=None):
        """queued 超过任务 TTL → expired。返回过期的 task_id 列表。"""
        now = time.time() if now is None else now
        now_iso = _iso(now)
        conn = self._connect()
        try:
            with _tx(conn):
                rows = conn.execute(
                    "SELECT task_id FROM tasks WHERE state='queued' AND expires_at<=?",
                    (now_iso,)).fetchall()
                for r in rows:
                    conn.execute("UPDATE tasks SET state='expired' WHERE task_id=?",
                                 (r["task_id"],))
                    _audit(conn, "hub", "task_expired", r["task_id"], None, now)
            return [r["task_id"] for r in rows]
        finally:
            conn.close()

    @staticmethod
    def _submission_identity(client_token: str, actor: str) -> None:
        if type(client_token) is not str or not 1 <= len(client_token) <= 128 or type(actor) is not str or not 1 <= len(actor) <= 128:
            raise DomainError("invalid_submission_identity", "invalid_submission_identity")


    def fence_submission(self, client_token: str, actor: str, *, now: float) -> dict[str, Any] | None:
        """Durably block a late cross-database create using the original token."""
        self._submission_identity(client_token, actor)
        if type(now) not in (int, float) or not 0 <= now <= 253402300799 or not math.isfinite(now):
            raise DomainError("invalid_submission_clock", "invalid_submission_clock")
        with contextlib.closing(self._connect()) as connection, _tx(connection):
            task = connection.execute("SELECT * FROM tasks WHERE client_token=?", (client_token,)).fetchone()
            prior = connection.execute("SELECT requested_by FROM task_submission_cancellations WHERE client_token=?", (client_token,)).fetchone()
            if (task is not None and task["requested_by"] != actor) or (prior is not None and prior[0] != actor):
                raise DomainError("submission_owner_conflict", "submission_owner_conflict")
            connection.execute("INSERT OR IGNORE INTO task_submission_cancellations VALUES(?,?,?)", (client_token, actor, _iso(now)))
            return dict(task) if task is not None else None


    def submission_stop(self, client_token: str, actor: str) -> TaskStopObservation | None:
        self._submission_identity(client_token, actor)
        with contextlib.closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT c.requested_by,t.task_id,t.attempt_id,t.state,"
                "EXISTS(SELECT 1 FROM leases l WHERE l.task_id=t.task_id) AS ever_leased "
                "FROM task_submission_cancellations c LEFT JOIN tasks t USING(client_token) WHERE c.client_token=?", (client_token,),
            ).fetchone()
            if row is None:
                return None
            if row["requested_by"] != actor:
                raise DomainError("submission_owner_conflict", "submission_owner_conflict")
            state = row["state"]
            stopped = row["task_id"] is None or state in ("succeeded", "failed") or (state == "cancelled" and not row["ever_leased"])
            return TaskStopObservation(row["task_id"], row["attempt_id"], state or "cancelled", stopped)


    def cancel_task(self, task_id, actor, now=None):
        """非终态（queued/leased/running）任务 → cancelled；终态返回 (row, False)，不存在 (None, False)。

        置为 cancelled 即栅栏住旧 attempt：complete_task/heartbeat/expire_leases 均不再
        作用于该任务（任务已非 leased/running），从而失效任何活跃 lease。
        """
        conn = self._connect()
        try:
            with _tx(conn):
                row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                if not row:
                    return None, False
                if not can_cancel(row["state"]):
                    return dict(row), False
                conn.execute("UPDATE tasks SET state='cancelled' WHERE task_id=?", (task_id,))
                _audit(conn, actor, "cancel_task", task_id, {"from": row["state"]}, now)
            out = dict(row)
            out["state"] = "cancelled"
            return out, True
        finally:
            conn.close()

    def retry_task(self, task_id, actor, now=None):
        """终态（succeeded/failed/cancelled/expired）→ queued，新 attempt_id、expires_at 顺延一个 TASK_TTL_S。

        保留 client_token（不重写），idempotency 语义不变；非终态返回 (row, False)。
        """
        now = time.time() if now is None else now
        conn = self._connect()
        try:
            with _tx(conn):
                row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                if not row:
                    return None, False
                self._require_independent_task(conn, task_id)
                if not can_retry(row["state"]):
                    return dict(row), False
                if row["client_token"] and conn.execute("SELECT 1 FROM task_submission_cancellations WHERE client_token=?", (row["client_token"],)).fetchone():
                    raise DomainError("task_submission_cancelled", "task_submission_cancelled")
                conn.execute(
                    "UPDATE tasks SET state='queued', attempt_id=?, expires_at=? WHERE task_id=?",
                    (uuid.uuid4().hex, _iso(now + TASK_TTL_S), task_id))
                # 被拒绝的派发闸门在重试时重新进入 pending：否则 lease_task 永远跳过
                # 该任务（gate=rejected），confirm 也因非 pending 失败，任务卡死在 queued。
                conn.execute(
                    "UPDATE task_gates SET state='pending', decided_by=NULL, decided_at=NULL"
                    " WHERE task_id=? AND state='rejected'", (task_id,))
                _audit(conn, actor, "retry_task", task_id, {"from": row["state"]}, now)
            out = dict(row)
            out["state"] = "queued"
            return out, True
        finally:
            conn.close()

    def get_task_gate(self, task_id):
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM task_gates WHERE task_id=?", (task_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def pause_task(self, task_id, actor, now=None):
        """queued/leased/running → paused. Terminal → (row, False)."""
        now = time.time() if now is None else now
        conn = self._connect()
        try:
            with _tx(conn):
                row = conn.execute(
                    "SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                if not row:
                    return None, False
                self._require_independent_task(conn, task_id)
                if not can_pause(row["state"]):
                    return dict(row), False
                conn.execute(
                    "UPDATE tasks SET state='paused' WHERE task_id=?", (task_id,))
                _audit(conn, actor, "pause_task", task_id, {"from": row["state"]}, now)
            out = dict(row)
            out["state"] = "paused"
            return out, True
        finally:
            conn.close()

    def continue_task(self, task_id, actor, now=None):
        """paused → queued with a fresh attempt_id. Other states → (row, False)."""
        now = time.time() if now is None else now
        conn = self._connect()
        try:
            with _tx(conn):
                row = conn.execute(
                    "SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                if not row:
                    return None, False
                self._require_independent_task(conn, task_id)
                if not can_continue(row["state"]):
                    return dict(row), False
                attempt_id = uuid.uuid4().hex
                conn.execute(
                    "UPDATE tasks SET state='queued', attempt_id=?, expires_at=?"
                    " WHERE task_id=?",
                    (attempt_id, _iso(now + TASK_TTL_S), task_id))
                _audit(conn, actor, "continue_task", task_id,
                       {"from": "paused", "attempt_id": attempt_id}, now)
            out = dict(row)
            out["state"] = "queued"
            out["attempt_id"] = attempt_id
            return out, True
        finally:
            conn.close()

    @staticmethod
    def _require_independent_task(connection: sqlite3.Connection, task_id: str) -> None:
        if connection.execute("SELECT 1 FROM legacy_task_origins WHERE task_id=?", (task_id,)).fetchone() is not None:
            raise DomainError("workflow_control_required", "请通过工作流控制该步骤")


    def confirm_task(self, task_id, actor, now=None):
        """pending gate → confirmed so poll can see the queued task."""
        now = time.time() if now is None else now
        conn = self._connect()
        try:
            with _tx(conn):
                row = conn.execute(
                    "SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                if not row:
                    return None, False
                gate = conn.execute(
                    "SELECT * FROM task_gates WHERE task_id=?", (task_id,)).fetchone()
                if not gate:
                    return dict(row), False
                if gate["state"] != "pending":
                    return dict(row), False
                conn.execute(
                    "UPDATE task_gates SET state='confirmed', decided_by=?,"
                    " decided_at=? WHERE task_id=?",
                    (actor, _iso(now), task_id))
                _audit(conn, actor, "confirm_task", task_id, {"kind": gate["kind"]}, now)
            return dict(row), True
        finally:
            conn.close()

    def reject_task(self, task_id, actor, now=None):
        """pending gate → rejected and task cancelled."""
        now = time.time() if now is None else now
        conn = self._connect()
        try:
            with _tx(conn):
                row = conn.execute(
                    "SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                if not row:
                    return None, False
                gate = conn.execute(
                    "SELECT * FROM task_gates WHERE task_id=?", (task_id,)).fetchone()
                if not gate or gate["state"] != "pending":
                    return dict(row) if row else None, False
                conn.execute(
                    "UPDATE task_gates SET state='rejected', decided_by=?,"
                    " decided_at=? WHERE task_id=?",
                    (actor, _iso(now), task_id))
                conn.execute(
                    "UPDATE tasks SET state='cancelled' WHERE task_id=?", (task_id,))
                _audit(conn, actor, "reject_task", task_id, {"kind": gate["kind"]}, now)
            out = dict(row)
            out["state"] = "cancelled"
            return out, True
        finally:
            conn.close()
