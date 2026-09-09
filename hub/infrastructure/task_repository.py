"""Explicit-path SQLite repository for task and lease state.

The repository owns SQLite connection, transaction, schema, fencing, and
result persistence semantics. It does not depend on Flask or module globals.
"""


import contextlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path


LEASE_TTL_S = 300
TASK_TTL_S = 24 * 3600
AGENT_TYPES = ("codex", "claude_code", "hermes")
TASK_STATES = (
    "queued", "leased", "running", "paused",
    "succeeded", "failed", "cancelled", "expired",
)
MAX_DIFF_PATCH = 102400
MAX_TEST_SUMMARY = 20480
TEST_SUMMARY_KEYS = (
    "framework", "passed", "failed", "skipped", "errors", "duration_s", "failed_names",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY,
  client_token TEXT UNIQUE,
  machine TEXT NOT NULL,
  agent_type TEXT NOT NULL,
  project TEXT NOT NULL,
  instruction TEXT NOT NULL CHECK(length(instruction) <= 2000),
  requested_by TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('queued','leased','running','paused','succeeded','failed','cancelled','expired')),
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
CREATE INDEX IF NOT EXISTS idx_lease_task ON leases(task_id);
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

def _normalize_test_summary(raw):
    """Allowlisted test summary; unknown keys dropped. None if empty."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return None
    if not isinstance(raw, dict):
        return None
    out = {}
    for key in TEST_SUMMARY_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if key == "framework":
            text = str(value)[:32]
            if text in ("pytest", "unittest", "unknown"):
                out[key] = text
            else:
                out[key] = "unknown"
        elif key in ("passed", "failed", "skipped", "errors"):
            try:
                out[key] = max(0, int(value))
            except (TypeError, ValueError):
                continue
        elif key == "duration_s":
            try:
                out[key] = float(value)
            except (TypeError, ValueError):
                continue
        elif key == "failed_names" and isinstance(value, (list, tuple)):
            names = []
            for item in value[:20]:
                text = str(item)[:200]
                if text:
                    names.append(text)
            out[key] = names
    return out or None


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

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn


    def init(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if self.db_path.exists():
            try:
                conn = self._connect()
                ok = conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                conn.close()
            except sqlite3.DatabaseError:
                ok = False
            if not ok:
                os.replace(self.db_path, self.db_path.with_name(self.db_path.name + ".corrupt"))
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            self._migrate_schema(conn)
        finally:
            conn.close()

    def _migrate_schema(self, conn):
        """Add v4-initial columns/tables on existing LIVE databases.

        CREATE TABLE IF NOT EXISTS does not alter old ``results`` / ``tasks``
        check constraints. Rebuild ``tasks`` when the state CHECK lacks
        ``paused`` so pause/continue can persist. New result columns are
        nullable; duplicate-column ALTER is ignored.
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
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute(
                "CREATE TABLE tasks_v4 ("
                " task_id TEXT PRIMARY KEY,"
                " client_token TEXT UNIQUE,"
                " machine TEXT NOT NULL,"
                " agent_type TEXT NOT NULL,"
                " project TEXT NOT NULL,"
                " instruction TEXT NOT NULL CHECK(length(instruction) <= 2000),"
                " requested_by TEXT NOT NULL,"
                " state TEXT NOT NULL CHECK(state IN "
                "('queued','leased','running','paused','succeeded','failed','cancelled','expired')),"
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
            conn.execute("PRAGMA foreign_keys=ON")

    def create_task(self, *, machine, agent_type, project, instruction, requested_by,
                    client_token=None, ttl_s=TASK_TTL_S, now=None, confirm=False):
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
                        return dict(existing), False
                conn.execute(
                    "INSERT INTO tasks (task_id, client_token, machine, agent_type, project,"
                    " instruction, requested_by, state, attempt_id, created_at, expires_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (task_id, client_token, machine, agent_type, project, instruction,
                     requested_by, "queued", uuid.uuid4().hex, _iso(now), _iso(now + ttl_s)))
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
                    "SELECT t.* FROM tasks t"
                    " LEFT JOIN task_gates g ON g.task_id=t.task_id"
                    " WHERE t.machine=? AND t.state='queued' AND t.expires_at>?"
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
            }
        finally:
            conn.close()

    def heartbeat(self, *, attempt_id, nonce, extend_s=LEASE_TTL_S, now=None):
        """续租；返回 {"task_id", "lease_expires_at"}，lease 无效/过期返回 None。

        仅接受「当前 attempt + 任务仍 leased/running」的续租；租约存在但 attempt 已不是任务
        最新 attempt、或任务已被取消/完成/过期（非 leased/running）时返回 None 且不扫延。
        """
        now = time.time() if now is None else now
        now_iso = _iso(now)
        conn = self._connect()
        try:
            with _tx(conn):
                lease = conn.execute("SELECT * FROM leases WHERE attempt_id=?",
                                     (attempt_id,)).fetchone()
                if not lease or lease["nonce"] != nonce or lease["expires_at"] <= now_iso:
                    return None
                # 仅接受当前 attempt + 任务仍 leased/running：取消后旧 lease 的心跳必须失败，
                # 否则 cancel 的 lease 失效语义被心跳击穿（lease 行会被顺延）。
                task = conn.execute("SELECT * FROM tasks WHERE task_id=?",
                                    (lease["task_id"],)).fetchone()
                if not task or task["attempt_id"] != attempt_id:
                    return None
                if task["state"] not in ("leased", "running"):
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
                      now=None, files=None, diff_patch=None, test_summary=None):
        """回传结果；attempt_id PK 撞 即幂等。返回 {"task_id","state","stored"} 或 None。

        log_summary/diff_stat 服务端截断后再 INSERT（不依赖 SQLite CHECK 抛错），
        符合全局约束「超限服务端截断」。
        ``files`` 是 runner 附带的有界快照（调用方已 sanitize）；幂等
        重交不替换已落库快照。

        仅接受「当前 attempt + 任务仍 leased/running + lease 未过期」的完成；
        同一 attempt 重复提交直接返回已落库状态（幂等），不被过期/陈旧 attempt 覆写。
        """
        log_summary = (log_summary or "")[:10240]
        diff_stat = (diff_stat or "")[:5120]
        if diff_patch is None:
            patch_value = None
        else:
            patch_value = str(diff_patch)[:MAX_DIFF_PATCH]
        summary_obj = _normalize_test_summary(test_summary)
        summary_json = (
            json.dumps(summary_obj, ensure_ascii=False)[:MAX_TEST_SUMMARY]
            if summary_obj is not None else None
        )
        now = time.time() if now is None else now
        now_iso = _iso(now)
        conn = self._connect()
        try:
            with _tx(conn):
                lease = conn.execute("SELECT * FROM leases WHERE attempt_id=?",
                                     (attempt_id,)).fetchone()
                if not lease or lease["nonce"] != nonce:
                    return None
                # 幂等：同一 attempt 已有结果，返回已持久化的状态（不按新 exit_code 重算）
                existing = conn.execute(
                    "SELECT task_id, exit_code FROM results WHERE attempt_id=?",
                    (attempt_id,)).fetchone()
                if existing:
                    persisted = "succeeded" if int(existing["exit_code"]) == 0 else "failed"
                    return {"task_id": existing["task_id"], "state": persisted, "stored": False}
                # 仅接受当前 attempt：任务 attempt_id==此次、状态 leased/running、lease 未过期
                task = conn.execute("SELECT * FROM tasks WHERE task_id=?",
                                    (lease["task_id"],)).fetchone()
                if not task or task["attempt_id"] != attempt_id:
                    return None
                if task["state"] not in ("leased", "running"):
                    return None
                if lease["expires_at"] <= now_iso:
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
                rows = conn.execute("SELECT * FROM leases WHERE expires_at<=?",
                                    (now_iso,)).fetchall()
                requeued = []
                for lease in rows:
                    # 仅处理「当前 attempt」：过期 lease 的 attempt_id 必须仍是任务的最新
                    # attempt_id，否则说明该 lease 已被重派/完成取代，不能把新运行打回 queued。
                    task = conn.execute(
                        "SELECT state FROM tasks WHERE task_id=? AND attempt_id=?",
                        (lease["task_id"], lease["attempt_id"])).fetchone()
                    if task and task["state"] in ("leased", "running"):
                        conn.execute("UPDATE tasks SET state='queued' WHERE task_id=?",
                                     (lease["task_id"],))
                        _audit(conn, "hub", "lease_expired", lease["task_id"],
                               {"attempt_id": lease["attempt_id"]}, now)
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
                if row["state"] not in ("queued", "leased", "running", "paused"):
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
                if row["state"] not in ("succeeded", "failed", "cancelled", "expired"):
                    return dict(row), False
                conn.execute(
                    "UPDATE tasks SET state='queued', attempt_id=?, expires_at=? WHERE task_id=?",
                    (uuid.uuid4().hex, _iso(now + TASK_TTL_S), task_id))
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
                if row["state"] not in ("queued", "leased", "running"):
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
                if row["state"] != "paused":
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
