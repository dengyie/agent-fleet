# Phase 2 任务存储和 API Implementation Plan

> **Status (2026-09-08): historical / implemented.** Open checkboxes below are the original plan and were not kept in sync after merge. Do not re-implement from unchecked boxes. Remaining work is recorded in `docs/HANDOFF.md` §六 as `done` or `deferred-with-condition`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地 v4 控制面的 hub 侧：SQLite 任务/租约/结果/审计存储，三个认证域（ingest/operator/runner），任务 API 与 runner 命令 API，任务详情页面前端。

**Architecture:** `hub/task_store.py` 用 SQLite（WAL + BEGIN IMMEDIATE）保证 lease 原子领取和幂等提交；`hub/auth.py` 追加 operator（CF Access 头）与 runner（每机独立 credential）装饰器；两个新蓝图 `routes_tasks.py`（operator 域）、`routes_commands.py`（runner 域）挂到现有 app 工厂；前端加任务视图。观测链路（Phase 1）不受影响：SQLite 不可用时任务 API 503、观测继续。

**Tech Stack:** Python 3 标准库（sqlite3）+ Flask；原生 JS；unittest。

**Spec:** docs/superpowers/specs/2026-08-19-v4-optimization-design.md（本计划实现其 §4.1 tasks/commands/auth/task_store、§4.3 数据模型、§5.2 任务链路、§5.3 task_* 事件、§7.1/7.4/7.5 对应行）

**前置：** Phase 1 计划（`2026-08-19-phase1-observability-hardening.md`）已完成——`hub/auth.py`、`hub/routes_observe.py`（含 `_sse_payload`）、瘦身的 `hub/web.py` 已存在。

## Global Constraints

- 沿用 Phase 1 全部约束（push-only、token 不进 git、白名单、unittest 命令、无新依赖）。
- 统一错误格式：`{"ok": false, "error": "<code>", "detail": "<msg>"}`。
- 三个认证域不串：ingest token 不能调任务 API；runner credential 不能调 operator API；CF Access 头缺失时仅 `--dev-operator` 开发模式兜底（生产拒绝）。
- instruction ≤2000 字符；log_summary ≤10240；diff_stat ≤5120；超限**服务端截断**（不报错，防 CHECK 约束炸 500）。
- lease TTL 300s；heartbeat 续期 300s；任务排队 TTL 24h。
- 时间一律 ISO UTC 字符串（`time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))`），字典序即时间序。
- 测试 patch `task_store.DB_PATH` 指向临时文件；每个测试新建临时 DB。SQLite 连接**不跨调用缓存**（每次 `_connect()` 新建），保证 patch 即生效。
- v4 设计文档中的 `GET /api/tasks/<id>/files/<path>`（按需读文件）**本计划不实现**：它需要 runner 侧文件回传通道，spec §4.3 数据模型无对应表。Phase 3 计划末尾评估追加。

## 文件结构

| 文件 | 动作 | 职责 |
|---|---|---|
| `hub/task_store.py` | 新建 | SQLite schema + 全部任务/租约/结果/审计操作 |
| `hub/auth.py` | 修改 | 追加 `require_operator`、`require_runner`、`require_task_store`、`load_runner_credentials` |
| `hub/routes_tasks.py` | 新建 | operator 域任务 API |
| `hub/routes_commands.py` | 新建 | runner 域命令 API |
| `hub/routes_observe.py` | 修改 | SSE 事件映射扩展 task_update/task_log |
| `hub/web.py` | 修改 | make_app 挂载新蓝图 + init_db + 降级 + lease reconciler + `/task/<id>` 页 |
| `hub/templates/task.html` | 新建 | 任务详情视图 |
| `hub/templates/machine.html` | 修改 | 任务列表 + 新建任务模态框 |
| `hub/static/app.js` | 修改 | task_update/task_log 处理 + 任务操作 fetch |
| `hub/static/style.css` | 修改 | 任务视图样式 |
| `tests/test_task_store.py` | 新建 | 存储层全部单测 |
| `tests/test_task_api.py` | 新建 | API 集成测试（认证隔离、全链路、lease 竞争） |

---

### Task 1: task_store.py — schema + 创建/查询

**Files:**
- Create: `hub/task_store.py`
- Test: `tests/test_task_store.py`

**Interfaces:**
- Consumes: 无（纯 SQLite）。
- Produces（后续任务与 routes 依赖的确切签名）:
  - `task_store.DB_PATH`（模块属性，测试 patch 目标）、`LEASE_TTL_S = 300`、`TASK_TTL_S = 86400`、`AGENT_TYPES = ("codex", "claude_code", "hermes")`、`TASK_STATES = ("queued","leased","running","succeeded","failed","cancelled","expired")`
  - `init_db() -> None`（建目录、integrity_check、损坏文件改名 `.corrupt` 重建、executescript SCHEMA）
  - `create_task(*, machine, agent_type, project, instruction, requested_by, client_token=None, ttl_s=TASK_TTL_S, now=None) -> (dict, bool)` — 返回 (task_row, created)；client_token 命中已有任务时返回 (已有行, False)（幂等）
  - `get_task(task_id) -> dict | None` — task 字段 + `result`（最新 results 行 dict 或 None）
  - `list_tasks(machine=None, state=None, limit=50) -> list[dict]`

- [ ] **Step 1: 写失败测试**

`tests/test_task_store.py`:

```python
import tempfile
import time
import unittest
from pathlib import Path

from hub import task_store


class TaskStoreTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.old_db = task_store.DB_PATH
        task_store.DB_PATH = self.temp_dir / "fleet.db"
        task_store.init_db()

    def tearDown(self):
        task_store.DB_PATH = self.old_db

    def _create(self, **kw):
        params = dict(machine="mac-local", agent_type="codex", project="agent-fleet",
                      instruction="修复测试", requested_by="op@example.com")
        params.update(kw)
        return task_store.create_task(**params)


class TaskCreateTests(TaskStoreTestBase):
    def test_create_task_returns_queued_row(self):
        task, created = self._create()
        self.assertTrue(created)
        self.assertEqual(task["state"], "queued")
        self.assertEqual(task["machine"], "mac-local")
        self.assertTrue(task["task_id"].startswith("t-"))
        self.assertTrue(task["attempt_id"])

    def test_client_token_makes_create_idempotent(self):
        first, created1 = self._create(client_token="web-abc")
        second, created2 = self._create(client_token="web-abc")
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(first["task_id"], second["task_id"])

    def test_get_task_and_list_tasks(self):
        task, _ = self._create()
        got = task_store.get_task(task["task_id"])
        self.assertEqual(got["instruction"], "修复测试")
        self.assertIsNone(got["result"])
        self.assertIsNone(task_store.get_task("t-missing"))
        listing = task_store.list_tasks(machine="mac-local")
        self.assertEqual([t["task_id"] for t in listing], [task["task_id"]])
        self.assertEqual(task_store.list_tasks(machine="other"), [])
        self.assertEqual(task_store.list_tasks(state="failed"), [])

    def test_corrupt_db_is_quarantined_and_rebuilt(self):
        task_store.DB_PATH.write_bytes(b"not a sqlite database at all")
        task_store.init_db()  # 不抛异常
        task, created = self._create()
        self.assertTrue(created)
        self.assertTrue((self.temp_dir / "fleet.db.corrupt").exists())
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_task_store -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hub.task_store'`

- [ ] **Step 3: 实现 hub/task_store.py（第一段）**

```python
"""hub/task_store.py — 任务/租约/结果/审计的 SQLite 存储（state/fleet.db）

并发模型：WAL + BEGIN IMMEDIATE。连接不缓存（每次 _connect 新建），
测试 patch DB_PATH 即生效。时间一律 ISO UTC 字符串（字典序 = 时间序）。
"""
import contextlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

FLEET_HOME = Path(__file__).resolve().parent.parent
DB_PATH = FLEET_HOME / "state" / "fleet.db"
LEASE_TTL_S = 300
TASK_TTL_S = 24 * 3600
AGENT_TYPES = ("codex", "claude_code", "hermes")
TASK_STATES = ("queued", "leased", "running", "succeeded", "failed", "cancelled", "expired")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY,
  client_token TEXT UNIQUE,
  machine TEXT NOT NULL,
  agent_type TEXT NOT NULL,
  project TEXT NOT NULL,
  instruction TEXT NOT NULL CHECK(length(instruction) <= 2000),
  requested_by TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('queued','leased','running','succeeded','failed','cancelled','expired')),
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
  duration_s REAL,
  finished_at TEXT NOT NULL
);
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


def _connect():
    conn = sqlite3.connect(str(DB_PATH), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


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


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        try:
            conn = _connect()
            ok = conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            conn.close()
        except sqlite3.DatabaseError:
            ok = False
        if not ok:
            os.replace(DB_PATH, DB_PATH.with_name(DB_PATH.name + ".corrupt"))
    conn = _connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def _audit(conn, actor, action, task_id=None, detail=None, now=None):
    conn.execute(
        "INSERT INTO audit (ts, actor, action, task_id, detail) VALUES (?,?,?,?,?)",
        (_iso(now if now is not None else time.time()), actor, action, task_id,
         json.dumps(detail, ensure_ascii=False) if detail is not None else None),
    )


def create_task(*, machine, agent_type, project, instruction, requested_by,
                client_token=None, ttl_s=TASK_TTL_S, now=None):
    """创建任务；(client_token 命中, False) 幂等返回已有任务。"""
    now = time.time() if now is None else now
    task_id = "t-" + uuid.uuid4().hex[:12]
    conn = _connect()
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
            _audit(conn, requested_by, "create_task", task_id,
                   {"machine": machine, "agent_type": agent_type, "project": project,
                    "instruction_len": len(instruction)}, now)
            row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return dict(row), True
    finally:
        conn.close()


def get_task(task_id):
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            return None
        out = dict(row)
        res = conn.execute(
            "SELECT * FROM results WHERE task_id=? ORDER BY finished_at DESC LIMIT 1",
            (task_id,)).fetchone()
        out["result"] = dict(res) if res else None
        return out
    finally:
        conn.close()


def list_tasks(machine=None, state=None, limit=50):
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
    conn = _connect()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/python -m unittest tests.test_task_store -v`
Expected: PASS（4 个测试）

- [ ] **Step 5: 提交**

```bash
git add hub/task_store.py tests/test_task_store.py
git commit -m "feat: task_store SQLite 存储 — schema/init/创建幂等/查询"
```

---

### Task 2: lease 状态机（领取/心跳/完成/过期）

**Files:**
- Modify: `hub/task_store.py`（追加）
- Test: `tests/test_task_store.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `_connect`/`_tx`/`_audit`/`_iso`、`create_task`。
- Produces:
  - `lease_task(*, machine, runner_id, lease_ttl_s=LEASE_TTL_S, now=None) -> dict | None` — 原子领取最老 queued 任务；返回 `{"task_id","machine","agent_type","project","instruction","attempt_id","nonce","lease_ttl_s","lease_expires_at"}`；无可领任务返回 None
  - `heartbeat(*, attempt_id, nonce, extend_s=LEASE_TTL_S, now=None) -> dict | None` — 续租成功返回 `{"task_id","lease_expires_at"}`；lease 不存在/nonce 不符/已过期返回 None；首次心跳把 leased→running
  - `complete_task(*, attempt_id, nonce, exit_code, log_summary, diff_stat, duration_s, now=None) -> dict | None` — 返回 `{"task_id","state","stored"}`；nonce/attempt 不符返回 None；重复提交返回 `stored=False`（幂等成功）
  - `expire_leases(now=None) -> list[str]` — 过期 lease 的 leased/running 任务回 queued，返回重派 task_id 列表
  - `expire_tasks(now=None) -> list[str]` — queued 超过任务 TTL → expired

- [ ] **Step 1: 写失败测试**

```python
import threading


class LeaseTests(TaskStoreTestBase):
    def test_lease_task_moves_queued_to_leased(self):
        task, _ = self._create()
        lease = task_store.lease_task(machine="mac-local", runner_id="runner-1")
        self.assertEqual(lease["task_id"], task["task_id"])
        self.assertEqual(lease["instruction"], "修复测试")
        self.assertTrue(lease["nonce"])
        self.assertEqual(task_store.get_task(task["task_id"])["state"], "leased")
        # 已被领走，再次领取为 None
        self.assertIsNone(task_store.lease_task(machine="mac-local", runner_id="r2"))

    def test_lease_is_scoped_to_machine(self):
        self._create()
        self.assertIsNone(task_store.lease_task(machine="other-machine", runner_id="r1"))

    def test_concurrent_lease_only_one_wins(self):
        task, _ = self._create()
        winners = []

        def grab(rid):
            lease = task_store.lease_task(machine="mac-local", runner_id=rid)
            if lease:
                winners.append(rid)

        threads = [threading.Thread(target=grab, args=(f"r{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(winners), 1)

    def test_heartbeat_extends_and_marks_running(self):
        self._create()
        lease = task_store.lease_task(machine="mac-local", runner_id="r1")
        t0 = time.time()
        out = task_store.heartbeat(attempt_id=lease["attempt_id"], nonce=lease["nonce"], now=t0)
        self.assertEqual(out["task_id"], lease["task_id"])
        self.assertEqual(task_store.get_task(lease["task_id"])["state"], "running")
        # 错误 nonce 拒绝
        self.assertIsNone(task_store.heartbeat(attempt_id=lease["attempt_id"], nonce="wrong"))

    def test_expired_lease_requeues_task(self):
        task, _ = self._create()
        lease = task_store.lease_task(machine="mac-local", runner_id="r1")
        t_future = time.time() + 301
        requeued = task_store.expire_leases(now=t_future)
        self.assertEqual(requeued, [task["task_id"]])
        self.assertEqual(task_store.get_task(task["task_id"])["state"], "queued")
        # 过期后心跳失败
        self.assertIsNone(task_store.heartbeat(
            attempt_id=lease["attempt_id"], nonce=lease["nonce"], now=t_future))
        # 重派后可被再次领取
        lease2 = task_store.lease_task(machine="mac-local", runner_id="r2", now=t_future + 1)
        self.assertEqual(lease2["task_id"], task["task_id"])
        self.assertNotEqual(lease2["attempt_id"], lease["attempt_id"])

    def test_complete_task_stores_result_and_is_idempotent(self):
        task, _ = self._create()
        lease = task_store.lease_task(machine="mac-local", runner_id="r1")
        out = task_store.complete_task(
            attempt_id=lease["attempt_id"], nonce=lease["nonce"], exit_code=0,
            log_summary="all good", diff_stat="1 file changed", duration_s=12.5)
        self.assertEqual(out["state"], "succeeded")
        self.assertTrue(out["stored"])
        again = task_store.complete_task(
            attempt_id=lease["attempt_id"], nonce=lease["nonce"], exit_code=0,
            log_summary="all good", diff_stat="1 file changed", duration_s=12.5)
        self.assertFalse(again["stored"])  # 幂等
        got = task_store.get_task(task["task_id"])
        self.assertEqual(got["state"], "succeeded")
        self.assertEqual(got["result"]["log_summary"], "all good")
        # 错误 nonce 拒绝
        self.assertIsNone(task_store.complete_task(
            attempt_id=lease["attempt_id"], nonce="wrong", exit_code=1,
            log_summary="", diff_stat="", duration_s=0))

    def test_failed_exit_code_marks_failed(self):
        task, _ = self._create()
        lease = task_store.lease_task(machine="mac-local", runner_id="r1")
        out = task_store.complete_task(
            attempt_id=lease["attempt_id"], nonce=lease["nonce"], exit_code=3,
            log_summary="boom", diff_stat="", duration_s=1.0)
        self.assertEqual(out["state"], "failed")

    def test_expire_tasks_marks_old_queued_expired(self):
        task, _ = self._create(now=time.time() - 90000)
        expired = task_store.expire_tasks(now=time.time())
        self.assertEqual(expired, [task["task_id"]])
        self.assertEqual(task_store.get_task(task["task_id"])["state"], "expired")
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_task_store.LeaseTests -v`
Expected: FAIL — `AttributeError: ... no attribute 'lease_task'`

- [ ] **Step 3: 实现（追加到 hub/task_store.py）**

```python
def lease_task(*, machine, runner_id, lease_ttl_s=LEASE_TTL_S, now=None):
    """原子领取：BEGIN IMMEDIATE 内 选任务→改状态→作废旧 lease→写新 lease。"""
    now = time.time() if now is None else now
    now_iso = _iso(now)
    conn = _connect()
    try:
        with _tx(conn):
            row = conn.execute(
                "SELECT * FROM tasks WHERE machine=? AND state='queued' AND expires_at>?"
                " ORDER BY created_at LIMIT 1", (machine, now_iso)).fetchone()
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


def heartbeat(*, attempt_id, nonce, extend_s=LEASE_TTL_S, now=None):
    """续租；返回 {"task_id", "lease_expires_at"}，lease 无效/过期返回 None。"""
    now = time.time() if now is None else now
    now_iso = _iso(now)
    conn = _connect()
    try:
        with _tx(conn):
            lease = conn.execute("SELECT * FROM leases WHERE attempt_id=?",
                                 (attempt_id,)).fetchone()
            if not lease or lease["nonce"] != nonce or lease["expires_at"] <= now_iso:
                return None
            new_exp = _iso(now + extend_s)
            conn.execute("UPDATE leases SET expires_at=? WHERE attempt_id=?",
                         (new_exp, attempt_id))
            conn.execute("UPDATE tasks SET state='running' WHERE task_id=? AND state='leased'",
                         (lease["task_id"],))
        return {"task_id": lease["task_id"], "lease_expires_at": new_exp}
    finally:
        conn.close()


def complete_task(*, attempt_id, nonce, exit_code, log_summary, diff_stat, duration_s, now=None):
    """回传结果；attempt_id PK 冲突即幂等成功。返回 {"task_id","state","stored"} 或 None。"""
    now = time.time() if now is None else now
    conn = _connect()
    try:
        with _tx(conn):
            lease = conn.execute("SELECT * FROM leases WHERE attempt_id=?",
                                 (attempt_id,)).fetchone()
            if not lease or lease["nonce"] != nonce:
                return None
            state = "succeeded" if int(exit_code) == 0 else "failed"
            existing = conn.execute("SELECT 1 FROM results WHERE attempt_id=?",
                                    (attempt_id,)).fetchone()
            if existing:
                return {"task_id": lease["task_id"], "state": state, "stored": False}
            conn.execute(
                "INSERT INTO results (attempt_id, task_id, exit_code, log_summary,"
                " diff_stat, duration_s, finished_at) VALUES (?,?,?,?,?,?,?)",
                (attempt_id, lease["task_id"], int(exit_code), log_summary or "",
                 diff_stat or "", duration_s, _iso(now)))
            conn.execute("UPDATE tasks SET state=? WHERE task_id=?",
                         (state, lease["task_id"]))
            conn.execute("UPDATE leases SET expires_at=? WHERE attempt_id=?",
                         (_iso(now), attempt_id))
            _audit(conn, lease["runner_id"], "complete_task", lease["task_id"],
                   {"exit_code": int(exit_code)}, now)
        return {"task_id": lease["task_id"], "state": state, "stored": True}
    finally:
        conn.close()


def expire_leases(now=None):
    """过期 lease → 对应 leased/running 任务回 queued。返回重派的 task_id 列表。"""
    now = time.time() if now is None else now
    now_iso = _iso(now)
    conn = _connect()
    try:
        with _tx(conn):
            rows = conn.execute("SELECT * FROM leases WHERE expires_at<=?",
                                (now_iso,)).fetchall()
            requeued = []
            for lease in rows:
                task = conn.execute("SELECT state FROM tasks WHERE task_id=?",
                                    (lease["task_id"],)).fetchone()
                if task and task["state"] in ("leased", "running"):
                    conn.execute("UPDATE tasks SET state='queued' WHERE task_id=?",
                                 (lease["task_id"],))
                    _audit(conn, "hub", "lease_expired", lease["task_id"],
                           {"attempt_id": lease["attempt_id"]}, now)
                    requeued.append(lease["task_id"])
        return requeued
    finally:
        conn.close()


def expire_tasks(now=None):
    """queued 超过任务 TTL → expired。返回过期的 task_id 列表。"""
    now = time.time() if now is None else now
    now_iso = _iso(now)
    conn = _connect()
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
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/python -m unittest tests.test_task_store -v`
Expected: PASS（含 8 线程 lease 竞争测试——BEGIN IMMEDIATE 串行化保证只有一个赢家）

- [ ] **Step 5: 提交**

```bash
git add hub/task_store.py tests/test_task_store.py
git commit -m "feat: lease 状态机 — 原子领取/心跳续租/幂等结果/过期重派"
```

---

### Task 3: cancel / retry

**Files:**
- Modify: `hub/task_store.py`（追加）
- Test: `tests/test_task_store.py`（追加）

**Interfaces:**
- Produces:
  - `cancel_task(task_id, actor, now=None) -> (dict | None, bool)` — queued/leased/running → cancelled；终态返回 (row, False)；不存在 (None, False)
  - `retry_task(task_id, actor, now=None) -> (dict | None, bool)` — 终态 → queued（新 attempt_id、expires_at 顺延一个 TASK_TTL_S）；非终态 (row, False)

- [ ] **Step 1: 写失败测试**

```python
class CancelRetryTests(TaskStoreTestBase):
    def test_cancel_queued_task(self):
        task, _ = self._create()
        row, changed = task_store.cancel_task(task["task_id"], "op@example.com")
        self.assertTrue(changed)
        self.assertEqual(row["state"], "cancelled")
        # 已取消不可再领
        self.assertIsNone(task_store.lease_task(machine="mac-local", runner_id="r1"))

    def test_cancel_terminal_is_noop(self):
        task, _ = self._create()
        lease = task_store.lease_task(machine="mac-local", runner_id="r1")
        task_store.complete_task(attempt_id=lease["attempt_id"], nonce=lease["nonce"],
                                 exit_code=0, log_summary="", diff_stat="", duration_s=1)
        row, changed = task_store.cancel_task(task["task_id"], "op@example.com")
        self.assertFalse(changed)
        self.assertEqual(row["state"], "succeeded")

    def test_cancel_missing_returns_none(self):
        row, changed = task_store.cancel_task("t-missing", "op@example.com")
        self.assertIsNone(row)
        self.assertFalse(changed)

    def test_retry_failed_task_requeues_with_new_attempt(self):
        task, _ = self._create()
        lease = task_store.lease_task(machine="mac-local", runner_id="r1")
        task_store.complete_task(attempt_id=lease["attempt_id"], nonce=lease["nonce"],
                                 exit_code=1, log_summary="", diff_stat="", duration_s=1)
        row, changed = task_store.retry_task(task["task_id"], "op@example.com")
        self.assertTrue(changed)
        self.assertEqual(row["state"], "queued")
        lease2 = task_store.lease_task(machine="mac-local", runner_id="r2")
        self.assertEqual(lease2["task_id"], task["task_id"])

    def test_retry_active_task_rejected(self):
        task, _ = self._create()
        row, changed = task_store.retry_task(task["task_id"], "op@example.com")
        self.assertFalse(changed)
        self.assertEqual(row["state"], "queued")
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_task_store.CancelRetryTests -v`
Expected: FAIL — `AttributeError: ... no attribute 'cancel_task'`

- [ ] **Step 3: 实现（追加到 hub/task_store.py）**

```python
def cancel_task(task_id, actor, now=None):
    conn = _connect()
    try:
        with _tx(conn):
            row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                return None, False
            if row["state"] not in ("queued", "leased", "running"):
                return dict(row), False
            conn.execute("UPDATE tasks SET state='cancelled' WHERE task_id=?", (task_id,))
            _audit(conn, actor, "cancel_task", task_id, {"from": row["state"]}, now)
        out = dict(row)
        out["state"] = "cancelled"
        return out, True
    finally:
        conn.close()


def retry_task(task_id, actor, now=None):
    now = time.time() if now is None else now
    conn = _connect()
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
```

- [ ] **Step 4: 运行确认通过 + 提交**

Run: `.venv/bin/python -m unittest tests.test_task_store -v`
Expected: PASS

```bash
git add hub/task_store.py tests/test_task_store.py
git commit -m "feat: 任务取消与重试 — 状态迁移审计"
```

---

### Task 4: auth.py — operator / runner / task_store 守卫

**Files:**
- Modify: `hub/auth.py`（追加；Phase 1 已有 `require_ingest_token`、`_forbidden`、`MACHINE_RE`、`FLEET_HOME`）
- Test: `tests/test_task_api.py`（新建）

**Interfaces:**
- Produces:
  - `require_operator(view)` — 读 `Cf-Access-Authenticated-User-Email` 头；缺失时回退 `app.config["DEV_OPERATOR"]`；都没有 → 401 `{"ok":false,"error":"unauthorized"}`。成功设 `flask.g.operator`。
  - `require_runner(view)` — 读 `X-Runner-Credential: <machine>:<secret>` 头；凭据来源 `app.config["RUNNER_CREDENTIALS"]`（dict，`{machine: secret}`；值为 None 时调 `load_runner_credentials()` 读文件）；比对失败/格式错 → 403。成功设 `flask.g.runner_machine`。
  - `load_runner_credentials() -> dict` — 读 `credentials/runner-credentials.json`（`{"<machine>": "<secret>"}`），异常返回 `{}`。
  - `require_task_store(view)` — `app.config["TASKS_ENABLED"]` 为假 → 503 `{"ok":false,"error":"tasks_unavailable","detail":"任务存储不可用，观测链路不受影响"}`。

- [ ] **Step 1: 写失败测试**

`tests/test_task_api.py`:

```python
import json
import tempfile
import unittest
from pathlib import Path

from hub import events
from hub import state as store
from hub import task_store


class TaskApiTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.old_state_dir = store.STATE_DIR
        self.old_event_log = events.EVENT_LOG
        self.old_db = task_store.DB_PATH
        store.STATE_DIR = self.temp_dir
        events.EVENT_LOG = self.temp_dir / "events.jsonl"
        task_store.DB_PATH = self.temp_dir / "fleet.db"
        task_store.init_db()
        # 让 mac-local 在线
        store.save_snapshot("mac-local", {
            "machine": "mac-local", "source": "ingest", "reachable": True,
            "agents": {}, "system": {},
        })
        from hub import web
        self.app = web.make_app(
            ingest_token="ingest-secret",
            dev_operator="op@example.com",
            runner_credentials={"mac-local": "runner-secret"},
            project_whitelist={"mac-local": ["agent-fleet"]},
        )
        self.client = self.app.test_client()

    def tearDown(self):
        store.STATE_DIR = self.old_state_dir
        events.EVENT_LOG = self.old_event_log
        task_store.DB_PATH = self.old_db


class AuthIsolationTests(TaskApiTestBase):
    def test_ingest_token_cannot_create_task(self):
        resp = self.client.post("/api/tasks", json={
            "machine": "mac-local", "agent_type": "codex",
            "project": "agent-fleet", "instruction": "x",
        }, headers={"X-Agent-Fleet-Token": "ingest-secret"})
        self.assertEqual(resp.status_code, 401)

    def test_runner_credential_cannot_create_task(self):
        resp = self.client.post("/api/tasks", json={
            "machine": "mac-local", "agent_type": "codex",
            "project": "agent-fleet", "instruction": "x",
        }, headers={"X-Runner-Credential": "mac-local:runner-secret"})
        self.assertEqual(resp.status_code, 401)

    def test_operator_cannot_poll_commands(self):
        resp = self.client.post("/api/commands/poll", json={},
                                headers={"Cf-Access-Authenticated-User-Email": "op@example.com"})
        self.assertEqual(resp.status_code, 403)

    def test_ingest_token_cannot_poll_commands(self):
        resp = self.client.post("/api/commands/poll", json={},
                                headers={"X-Agent-Fleet-Token": "ingest-secret"})
        self.assertEqual(resp.status_code, 403)

    def test_runner_credential_machine_mismatch_rejected(self):
        resp = self.client.post("/api/commands/poll", json={},
                                headers={"X-Runner-Credential": "mac-local:wrong"})
        self.assertEqual(resp.status_code, 403)

    def test_operator_without_any_identity_401(self):
        app = self.app
        app.config["DEV_OPERATOR"] = None  # 关闭开发兜底
        resp = self.client.post("/api/tasks", json={
            "machine": "mac-local", "agent_type": "codex",
            "project": "agent-fleet", "instruction": "x"})
        self.assertEqual(resp.status_code, 401)
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_task_api -v`
Expected: FAIL — `web.make_app() got an unexpected keyword argument 'dev_operator'`（Task 7 才改 make_app；本任务先让 auth 函数存在）

注意：本任务只加 auth 函数，make_app 参数在 Task 7 接入。因此 Step 1 的测试在 Task 7 完成后才会全绿——**本任务的提交点只验证 auth 单元级行为**。为了让本任务可独立提交，先写只针对装饰器的测试：

```python
class AuthDecoratorTests(unittest.TestCase):
    def _app(self, **config):
        from flask import Flask, jsonify, g
        from hub import auth
        app = Flask(__name__)
        app.config.update(config)

        @app.route("/op", methods=["POST"])
        @auth.require_operator
        def op():
            return jsonify({"ok": True, "operator": g.operator})

        @app.route("/run", methods=["POST"])
        @auth.require_runner
        def run():
            return jsonify({"ok": True, "machine": g.runner_machine})

        @app.route("/guarded")
        @auth.require_task_store
        def guarded():
            return jsonify({"ok": True})
        return app

    def test_operator_cf_header(self):
        client = self._app(DEV_OPERATOR=None).test_client()
        resp = client.post("/op", headers={
            "Cf-Access-Authenticated-User-Email": "op@example.com"})
        self.assertEqual(resp.get_json()["operator"], "op@example.com")

    def test_operator_dev_fallback_and_reject(self):
        self.assertEqual(self._app(DEV_OPERATOR="dev@local").test_client()
                         .post("/op").status_code, 200)
        self.assertEqual(self._app(DEV_OPERATOR=None).test_client()
                         .post("/op").status_code, 401)

    def test_runner_credential(self):
        app = self._app(RUNNER_CREDENTIALS={"mac-local": "s3"})
        client = app.test_client()
        self.assertEqual(client.post("/run", headers={
            "X-Runner-Credential": "mac-local:s3"}).get_json()["machine"], "mac-local")
        self.assertEqual(client.post("/run", headers={
            "X-Runner-Credential": "mac-local:bad"}).status_code, 403)
        self.assertEqual(client.post("/run").status_code, 403)

    def test_task_store_guard(self):
        self.assertEqual(self._app(TASKS_ENABLED=False).test_client()
                         .get("/guarded").status_code, 503)
        self.assertEqual(self._app(TASKS_ENABLED=True).test_client()
                         .get("/guarded").status_code, 200)
```

把 `AuthIsolationTests` 完整类也写进文件（它们现在 fail，是 Task 5-7 的目标测试；文件内保持，逐步转绿）。

- [ ] **Step 3: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_task_api.AuthDecoratorTests -v`
Expected: FAIL — `AttributeError: module 'hub.auth' has no attribute 'require_operator'`

- [ ] **Step 4: 实现（追加到 hub/auth.py，顶部 import 加 `import json`、`from flask import g`）**

```python
RUNNER_CREDENTIALS_FILE = FLEET_HOME / "credentials" / "runner-credentials.json"


def load_runner_credentials():
    """每机独立 runner 凭据：credentials/runner-credentials.json {"<machine>": "<secret>"}。

    文件须 0600、不进 git；单台机器吊销 = 删对应键。
    """
    try:
        if RUNNER_CREDENTIALS_FILE.exists():
            data = json.loads(RUNNER_CREDENTIALS_FILE.read_text())
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def require_operator(view):
    """tasks 域：CF Access 认证。生产必须带 Cf-Access-Authenticated-User-Email 头；
    仅开发模式允许 DEV_OPERATOR 兜底。"""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        email = request.headers.get("Cf-Access-Authenticated-User-Email", "").strip()
        if not email:
            email = current_app.config.get("DEV_OPERATOR") or ""
        if not email:
            return jsonify({"ok": False, "error": "unauthorized",
                            "detail": "operator identity required"}), 401
        g.operator = email
        return view(*args, **kwargs)
    return wrapper


def require_runner(view):
    """commands 域：X-Runner-Credential: <machine>:<secret>，credential 与机器绑定。"""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        header = request.headers.get("X-Runner-Credential", "")
        machine, _, secret = header.partition(":")
        if not machine or not secret or not MACHINE_RE.fullmatch(machine):
            return _forbidden()
        creds = current_app.config.get("RUNNER_CREDENTIALS")
        if creds is None:
            creds = load_runner_credentials()
        expected = creds.get(machine)
        if not expected or not hmac.compare_digest(secret, expected):
            return _forbidden()
        g.runner_machine = machine
        return view(*args, **kwargs)
    return wrapper


def require_task_store(view):
    """SQLite 不可用降级：任务 API 503，观测链路不受影响。"""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not current_app.config.get("TASKS_ENABLED"):
            return jsonify({"ok": False, "error": "tasks_unavailable",
                            "detail": "任务存储不可用，观测链路不受影响"}), 503
        return view(*args, **kwargs)
    return wrapper
```

- [ ] **Step 5: 运行确认通过**

Run: `.venv/bin/python -m unittest tests.test_task_api.AuthDecoratorTests -v`
Expected: PASS（AuthIsolationTests 仍红，属后续任务）

- [ ] **Step 6: 提交**

```bash
git add hub/auth.py tests/test_task_api.py
git commit -m "feat: operator/runner 认证域 + 任务存储降级守卫"
```

---

### Task 5: routes_tasks.py — operator 任务 API

**Files:**
- Create: `hub/routes_tasks.py`
- Modify: `hub/web.py`（make_app 挂载 + 新配置项——即 Task 7 的挂载部分提前到本任务，保证测试可跑）
- Test: `tests/test_task_api.py`（追加）

**Interfaces:**
- Consumes: `auth.require_operator/require_task_store/MACHINE_RE`、`task_store.*`（Task 1-3）、`store.read_current`（在线校验）、`events.emit`。
- Produces: `routes_tasks.bp`；路由：
  - `POST /api/tasks` body `{machine, agent_type, project, instruction, client_token?}` → 201 `{"ok":true,"created":true,"task":{...}}`；幂等命中 200 `created:false`。校验失败 400（error code：`invalid_machine`/`invalid_agent_type`/`invalid_project`/`invalid_instruction`/`invalid_client_token`/`machine_offline`/`project_not_allowed`）
  - `GET /api/tasks?machine=&state=&limit=` → `{"ok":true,"tasks":[...]}`
  - `GET /api/tasks/<id>` → `{"ok":true,"task":{...,"instruction":...,"result":{...}|null}}`；404 `not_found`
  - `POST /api/tasks/<id>/cancel` → `{"ok":true,"changed":bool,"task":{...}}`；404
  - `POST /api/tasks/<id>/retry` → 同上
  - 项目白名单来源：`app.config["PROJECT_WHITELIST"]`（dict，`None` 时读 hosts.yaml 中该 host 的 `projects` 列表；host 无 projects 键 → 拒绝全部）

- [ ] **Step 1: 写失败测试**

```python
class TaskCrudApiTests(TaskApiTestBase):
    def _create(self, **kw):
        body = {"machine": "mac-local", "agent_type": "codex",
                "project": "agent-fleet", "instruction": "修复登录页样式"}
        body.update(kw)
        return self.client.post("/api/tasks", json=body)

    def test_create_and_get_task(self):
        resp = self._create()
        self.assertEqual(resp.status_code, 201)
        task = resp.get_json()["task"]
        self.assertEqual(task["state"], "queued")
        got = self.client.get(f"/api/tasks/{task['task_id']}").get_json()
        self.assertEqual(got["task"]["instruction"], "修复登录页样式")
        self.assertEqual(got["task"]["requested_by"], "op@example.com")
        self.assertIsNone(got["task"]["result"])

    def test_create_idempotent_with_client_token(self):
        first = self._create(client_token="web-1")
        second = self._create(client_token="web-1")
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.get_json()["created"])
        self.assertEqual(first.get_json()["task"]["task_id"],
                         second.get_json()["task"]["task_id"])

    def test_validation_errors(self):
        self.assertEqual(self._create(machine="../x").status_code, 400)
        self.assertEqual(self._create(agent_type="bash").status_code, 400)
        self.assertEqual(self._create(project="bad proj!").status_code, 400)
        self.assertEqual(self._create(instruction="").status_code, 400)
        self.assertEqual(self._create(instruction="x" * 2001).status_code, 400)
        self.assertEqual(self._create(project="not-registered").status_code, 400)
        resp = self._create(machine="ghost-machine")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "machine_offline")

    def test_list_and_filter(self):
        self._create(instruction="任务一")
        self._create(instruction="任务二")
        payload = self.client.get("/api/tasks?machine=mac-local").get_json()
        self.assertEqual(len(payload["tasks"]), 2)
        self.assertEqual(self.client.get("/api/tasks?machine=other").get_json()["tasks"], [])

    def test_cancel_and_retry(self):
        task = self._create().get_json()["task"]
        tid = task["task_id"]
        resp = self.client.post(f"/api/tasks/{tid}/cancel")
        self.assertTrue(resp.get_json()["changed"])
        self.assertEqual(resp.get_json()["task"]["state"], "cancelled")
        resp = self.client.post(f"/api/tasks/{tid}/retry")
        self.assertTrue(resp.get_json()["changed"])
        self.assertEqual(resp.get_json()["task"]["state"], "queued")
        self.assertEqual(self.client.post("/api/tasks/t-missing/cancel").status_code, 404)
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_task_api.TaskCrudApiTests -v`
Expected: FAIL — 404（蓝图未挂载）

- [ ] **Step 3: 实现 hub/routes_tasks.py**

```python
"""hub/routes_tasks.py — 任务 API 蓝图（CF Access operator 认证域）"""
import re

from flask import Blueprint, current_app, g, jsonify, request

from hub import events as ev
from hub import state as store
from hub import task_store
from hub.auth import MACHINE_RE, require_operator, require_task_store

bp = Blueprint("tasks", __name__)
PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _err(code, detail="", status=400):
    return jsonify({"ok": False, "error": code, "detail": detail}), status


def _public_task(t, with_result=False):
    out = {
        "task_id": t["task_id"], "machine": t["machine"], "agent_type": t["agent_type"],
        "project": t["project"], "state": t["state"], "requested_by": t["requested_by"],
        "created_at": t["created_at"], "expires_at": t["expires_at"],
        "instruction": t["instruction"],
    }
    if with_result:
        out["result"] = t.get("result")
    return out


def _project_allowed(machine, project):
    whitelist = current_app.config.get("PROJECT_WHITELIST")
    if whitelist is not None:
        return project in whitelist.get(machine, [])
    from hub import scan
    for h in scan.load_config().get("hosts", []):
        if h.get("name") == machine:
            return project in (h.get("projects") or [])
    return False


def _machine_online(machine):
    current = store.read_current(machine)
    return bool(current and current.get("reachable", True))


def _emit_task(event, machine, task_id, state):
    try:
        ev.emit(event, machine=machine, task_id=task_id, state=state)
    except Exception:
        pass


@bp.route("/api/tasks", methods=["POST"])
@require_operator
@require_task_store
def create_task():
    data = request.get_json(silent=True) or {}
    machine = data.get("machine", "")
    agent_type = data.get("agent_type", "")
    project = data.get("project", "")
    instruction = data.get("instruction", "")
    client_token = data.get("client_token") or None
    if not isinstance(machine, str) or not MACHINE_RE.fullmatch(machine):
        return _err("invalid_machine", "machine 名不合法")
    if agent_type not in task_store.AGENT_TYPES:
        return _err("invalid_agent_type",
                    "agent_type 必须是 " + "/".join(task_store.AGENT_TYPES))
    if not isinstance(project, str) or not PROJECT_RE.fullmatch(project):
        return _err("invalid_project", "project 名不合法")
    if not isinstance(instruction, str) or not (1 <= len(instruction) <= 2000):
        return _err("invalid_instruction", "instruction 长度须为 1-2000 字符")
    if client_token is not None and not (1 <= len(str(client_token)) <= 128):
        return _err("invalid_client_token", "client_token 长度须为 1-128")
    if not _machine_online(machine):
        return _err("machine_offline", f"{machine} 不在线")
    if not _project_allowed(machine, project):
        return _err("project_not_allowed", f"{machine} 未注册项目 {project}")
    task, created = task_store.create_task(
        machine=machine, agent_type=agent_type, project=project,
        instruction=instruction, requested_by=g.operator,
        client_token=str(client_token) if client_token else None)
    if created:
        _emit_task("task_queued", task["machine"], task["task_id"], "queued")
    return jsonify({"ok": True, "created": created,
                    "task": _public_task(task)}), (201 if created else 200)


@bp.route("/api/tasks")
@require_operator
@require_task_store
def list_tasks():
    machine = request.args.get("machine") or None
    state = request.args.get("state") or None
    if state and state not in task_store.TASK_STATES:
        return _err("invalid_state", "state 不合法")
    try:
        limit = min(200, max(1, int(request.args.get("limit", 50))))
    except (TypeError, ValueError):
        limit = 50
    tasks = task_store.list_tasks(machine=machine, state=state, limit=limit)
    return jsonify({"ok": True, "tasks": [_public_task(t) for t in tasks]})


@bp.route("/api/tasks/<task_id>")
@require_operator
@require_task_store
def get_task(task_id):
    task = task_store.get_task(task_id)
    if not task:
        return _err("not_found", "任务不存在", 404)
    return jsonify({"ok": True, "task": _public_task(task, with_result=True)})


@bp.route("/api/tasks/<task_id>/cancel", methods=["POST"])
@require_operator
@require_task_store
def cancel_task(task_id):
    row, changed = task_store.cancel_task(task_id, g.operator)
    if row is None:
        return _err("not_found", "任务不存在", 404)
    if changed:
        _emit_task("task_cancelled", row["machine"], task_id, "cancelled")
    return jsonify({"ok": True, "changed": changed, "task": _public_task(row)})


@bp.route("/api/tasks/<task_id>/retry", methods=["POST"])
@require_operator
@require_task_store
def retry_task(task_id):
    row, changed = task_store.retry_task(task_id, g.operator)
    if row is None:
        return _err("not_found", "任务不存在", 404)
    if changed:
        _emit_task("task_queued", row["machine"], task_id, "queued")
    return jsonify({"ok": True, "changed": changed, "task": _public_task(row)})
```

- [ ] **Step 4: web.py 挂载（make_app 扩展）**

`hub/web.py` 的 `make_app` 改为：

```python
def make_app(ingest_token=None, require_token=True, dev_operator=None,
             runner_credentials=None, project_whitelist=None):
    from hub import auth
    from hub import routes_observe, routes_tasks, routes_commands
    from hub import task_store

    resolved_token = auth.resolve_ingest_token(ingest_token)
    if require_token and not resolved_token:
        raise RuntimeError(
            "AGENT_FLEET_INGEST_TOKEN or credentials/ingest-token is required"
        )
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 256 * 1024
    app.config["INGEST_TOKEN"] = resolved_token
    app.config["DEV_OPERATOR"] = dev_operator
    app.config["RUNNER_CREDENTIALS"] = runner_credentials  # None → 请求时读 credentials 文件
    app.config["PROJECT_WHITELIST"] = project_whitelist    # None → 请求时读 hosts.yaml
    try:
        task_store.init_db()
        app.config["TASKS_ENABLED"] = True
    except Exception:
        app.config["TASKS_ENABLED"] = False  # 降级：任务 API 503，观测继续
    app.register_blueprint(routes_observe.bp)
    app.register_blueprint(routes_tasks.bp)
    app.register_blueprint(routes_commands.bp)

    # …页面路由不变（/、/machine/<name>；/task/<id> 在 Task 8 加）
    return app
```

（`routes_commands` 尚不存在——先建只含空蓝图的占位文件 `hub/routes_commands.py`，Task 6 填实现：

```python
"""hub/routes_commands.py — runner API 蓝图（runner credential 认证域）。Task 6 实现。"""
from flask import Blueprint

bp = Blueprint("commands", __name__)
```
）

- [ ] **Step 5: 运行确认通过**

Run: `.venv/bin/python -m unittest tests.test_task_api.TaskCrudApiTests -v`
Expected: PASS；同时 `AuthIsolationTests` 中 operator/runner 隔离用例开始转绿

- [ ] **Step 6: 提交**

```bash
git add hub/routes_tasks.py hub/routes_commands.py hub/web.py tests/test_task_api.py
git commit -m "feat: 任务 API — 创建幂等/校验/列表/详情/取消/重试"
```

---

### Task 6: routes_commands.py — runner 命令 API + SSE task 事件

**Files:**
- Modify: `hub/routes_commands.py`（填实现）
- Modify: `hub/routes_observe.py`（SSE 映射扩展）
- Test: `tests/test_task_api.py`（追加）

**Interfaces:**
- Consumes: `auth.require_runner/require_task_store`、`task_store.*`、`events.emit`。
- Produces: 路由：
  - `POST /api/commands/poll` body `{runner_id?}` → `{"ok":true,"task": null | {task_id, machine, agent_type, project, instruction, attempt_id, nonce, lease_ttl_s, lease_expires_at}}`
  - `POST /api/commands/<attempt_id>/heartbeat` body `{nonce, log_lines?}` → 200 `{"ok":true,"task_id","lease_expires_at"}`；409 `{"ok":false,"error":"lease_expired"}`。`log_lines` ≤50 条、每条截断 500 字符，逐条 `emit("task_log", ...)`。
  - `POST /api/commands/<attempt_id>/result` body `{nonce, exit_code, log_summary?, diff_stat?, duration_s?}` → 200 `{"ok":true,"task_id","state","stored"}`；409 `lease_mismatch`；400 `invalid_exit_code`。log/diff 服务端截断到 10240/5120。
- routes_observe SSE 映射更新（Phase 1 的 `_SSE_EVENT_NAMES` dict 与 `_sse_payload` 函数替换为）：
  - `_sse_event_name(e)`：`state_changed → machine_update`；`task_log → task_log`；其余 `task_* → task_update`；否则 `fleet_event`
  - `_sse_payload(e)`：`machine_update` 同 Phase 1；`task_log` → `{"task_id","line","ts"}`；`task_update` → `{"task_id","machine","state","event","ts"}`（task_id/state 从 `e["extra"]` 取）；`fleet_event` 同 Phase 1

- [ ] **Step 1: 写失败测试**

```python
class CommandFlowApiTests(TaskApiTestBase):
    RUNNER = {"X-Runner-Credential": "mac-local:runner-secret"}

    def _create_task(self, instruction="跑测试"):
        return self.client.post("/api/tasks", json={
            "machine": "mac-local", "agent_type": "codex",
            "project": "agent-fleet", "instruction": instruction}).get_json()["task"]

    def test_full_task_flow(self):
        task = self._create_task()
        # poll 领取
        resp = self.client.post("/api/commands/poll", json={"runner_id": "r1"},
                                headers=self.RUNNER)
        leased = resp.get_json()["task"]
        self.assertEqual(leased["task_id"], task["task_id"])
        self.assertEqual(leased["lease_ttl_s"], 300)
        self.assertTrue(leased["nonce"])
        # 队列已空
        resp = self.client.post("/api/commands/poll", json={}, headers=self.RUNNER)
        self.assertIsNone(resp.get_json()["task"])
        # heartbeat（带日志行）
        resp = self.client.post(f"/api/commands/{leased['attempt_id']}/heartbeat",
                                json={"nonce": leased["nonce"],
                                      "log_lines": ["start", "running tests"]},
                                headers=self.RUNNER)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["task_id"], task["task_id"])
        # result
        resp = self.client.post(f"/api/commands/{leased['attempt_id']}/result",
                                json={"nonce": leased["nonce"], "exit_code": 0,
                                      "log_summary": "ok", "diff_stat": "1 file",
                                      "duration_s": 3.2},
                                headers=self.RUNNER)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["state"], "succeeded")
        # 任务详情反映终态与结果
        got = self.client.get(f"/api/tasks/{task['task_id']}").get_json()["task"]
        self.assertEqual(got["state"], "succeeded")
        self.assertEqual(got["result"]["log_summary"], "ok")

    def test_result_with_wrong_nonce_409(self):
        self._create_task()
        leased = self.client.post("/api/commands/poll", json={},
                                  headers=self.RUNNER).get_json()["task"]
        resp = self.client.post(f"/api/commands/{leased['attempt_id']}/result",
                                json={"nonce": "wrong", "exit_code": 0},
                                headers=self.RUNNER)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["error"], "lease_mismatch")

    def test_expired_lease_heartbeat_409_then_requeue(self):
        task = self._create_task()
        leased = self.client.post("/api/commands/poll", json={},
                                  headers=self.RUNNER).get_json()["task"]
        task_store.expire_leases(now=__import__("time").time() + 301)
        resp = self.client.post(f"/api/commands/{leased['attempt_id']}/heartbeat",
                                json={"nonce": leased["nonce"]}, headers=self.RUNNER)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["error"], "lease_expired")
        # 任务已回 queued，可被重新领取
        leased2 = self.client.post("/api/commands/poll", json={},
                                   headers=self.RUNNER).get_json()["task"]
        self.assertEqual(leased2["task_id"], task["task_id"])
        self.assertNotEqual(leased2["attempt_id"], leased["attempt_id"])

    def test_oversized_result_is_truncated(self):
        self._create_task()
        leased = self.client.post("/api/commands/poll", json={},
                                  headers=self.RUNNER).get_json()["task"]
        resp = self.client.post(f"/api/commands/{leased['attempt_id']}/result",
                                json={"nonce": leased["nonce"], "exit_code": 0,
                                      "log_summary": "x" * 20000,
                                      "diff_stat": "y" * 8000, "duration_s": 1},
                                headers=self.RUNNER)
        self.assertEqual(resp.status_code, 200)
        got = self.client.get(
            f"/api/tasks/{leased['task_id']}").get_json()["task"]["result"]
        self.assertEqual(len(got["log_summary"]), 10240)
        self.assertEqual(len(got["diff_stat"]), 5120)

    def test_commands_require_runner_auth(self):
        self.assertEqual(self.client.post("/api/commands/poll", json={}).status_code, 403)
        self.assertEqual(self.client.post(
            "/api/commands/abc/heartbeat", json={"nonce": "x"}).status_code, 403)
```

SSE 映射测试（追加到 `tests/test_observability.py` 的 `SseStreamTests`）：

```python
    def test_stream_maps_task_events(self):
        from hub import routes_observe
        e = {"event": "task_queued", "machine": "mac-local", "ts": 1.0,
             "changes": [], "extra": {"task_id": "t-1", "state": "queued"}}
        self.assertEqual(routes_observe._sse_event_name(e), "task_update")
        self.assertEqual(routes_observe._sse_payload(e)["task_id"], "t-1")
        log_e = {"event": "task_log", "machine": "mac-local", "ts": 2.0,
                 "changes": [], "extra": {"task_id": "t-1", "line": "hello"}}
        self.assertEqual(routes_observe._sse_event_name(log_e), "task_log")
        self.assertEqual(routes_observe._sse_payload(log_e)["line"], "hello")
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_task_api.CommandFlowApiTests -v`
Expected: FAIL — poll 404（空蓝图）

- [ ] **Step 3: 实现 hub/routes_commands.py（整文件替换占位）**

```python
"""hub/routes_commands.py — runner API 蓝图（runner credential 认证域）

runner 通过 HTTPS 主动 poll；hub 不反向连接机器。
lease TTL 300s，heartbeat 续期；结果按 attempt_id 幂等。
"""
from flask import Blueprint, g, jsonify, request

from hub import events as ev
from hub import task_store
from hub.auth import require_runner, require_task_store

bp = Blueprint("commands", __name__)
MAX_LOG_SUMMARY = 10240
MAX_DIFF_STAT = 5120
MAX_LOG_LINES = 50
MAX_LOG_LINE = 500


def _err(code, detail="", status=400):
    return jsonify({"ok": False, "error": code, "detail": detail}), status


def _emit(event, machine, task_id, **extra):
    try:
        ev.emit(event, machine=machine, task_id=task_id, **extra)
    except Exception:
        pass


@bp.route("/api/commands/poll", methods=["POST"])
@require_runner
@require_task_store
def poll():
    data = request.get_json(silent=True) or {}
    runner_id = str(data.get("runner_id") or g.runner_machine)[:64]
    task = task_store.lease_task(machine=g.runner_machine, runner_id=runner_id)
    if task:
        _emit("task_leased", task["machine"], task["task_id"], state="leased")
    return jsonify({"ok": True, "task": task})


@bp.route("/api/commands/<attempt_id>/heartbeat", methods=["POST"])
@require_runner
@require_task_store
def heartbeat(attempt_id):
    data = request.get_json(silent=True) or {}
    result = task_store.heartbeat(attempt_id=attempt_id, nonce=str(data.get("nonce", "")))
    if result is None:
        return _err("lease_expired", "lease 不存在或已过期，runner 应放弃任务", 409)
    lines = data.get("log_lines") or []
    if isinstance(lines, list):
        for line in lines[:MAX_LOG_LINES]:
            _emit("task_log", g.runner_machine, result["task_id"],
                  line=str(line)[:MAX_LOG_LINE])
    return jsonify({"ok": True, **result})


@bp.route("/api/commands/<attempt_id>/result", methods=["POST"])
@require_runner
@require_task_store
def result(attempt_id):
    data = request.get_json(silent=True) or {}
    try:
        exit_code = int(data.get("exit_code"))
    except (TypeError, ValueError):
        return _err("invalid_exit_code", "exit_code 必须是整数")
    duration = data.get("duration_s")
    if not isinstance(duration, (int, float)):
        duration = None
    out = task_store.complete_task(
        attempt_id=attempt_id, nonce=str(data.get("nonce", "")), exit_code=exit_code,
        log_summary=str(data.get("log_summary", ""))[:MAX_LOG_SUMMARY],
        diff_stat=str(data.get("diff_stat", ""))[:MAX_DIFF_STAT],
        duration_s=duration)
    if out is None:
        return _err("lease_mismatch", "attempt_id/nonce 不匹配", 409)
    _emit("task_finished", g.runner_machine, out["task_id"], state=out["state"])
    return jsonify({"ok": True, **out})
```

- [ ] **Step 4: routes_observe.py SSE 映射更新**

把 Phase 1 的 `_SSE_EVENT_NAMES` dict 与 `_sse_payload` 替换为：

```python
def _sse_event_name(e):
    t = e.get("event", "")
    if t == "state_changed":
        return "machine_update"
    if t == "task_log":
        return "task_log"
    if t.startswith("task_"):
        return "task_update"
    return "fleet_event"


def _sse_payload(e):
    name = _sse_event_name(e)
    extra = e.get("extra", {}) if isinstance(e.get("extra"), dict) else {}
    if name == "machine_update" and isinstance(extra.get("snapshot"), dict):
        snap = extra["snapshot"]
        return {
            "machine": e.get("machine"),
            "changes": e.get("changes", []),
            "online": bool(snap.get("reachable", True)),
            "ts": e.get("ts"),
            "agents": sanitize_agents(snap.get("agents", {})),
            "system": sanitize_system(snap.get("system", {})),
        }
    if name == "task_log":
        return {"task_id": extra.get("task_id"), "line": extra.get("line"),
                "ts": e.get("ts")}
    if name == "task_update":
        return {"task_id": extra.get("task_id"), "machine": e.get("machine"),
                "state": extra.get("state"), "event": e.get("event"), "ts": e.get("ts")}
    return {
        "event": e.get("event"),
        "machine": e.get("machine"),
        "changes": e.get("changes", []),
        "ts": e.get("ts"),
    }
```

`api_stream` 内两处 `_SSE_EVENT_NAMES.get(e.get("event"), "fleet_event")` 改为 `_sse_event_name(e)`。

- [ ] **Step 5: 运行全部测试**

Run: `.venv/bin/python -m unittest discover -s tests -v`
Expected: 全绿（含 AuthIsolationTests、SSE 新旧用例、Phase 1 全部回归）

- [ ] **Step 6: 提交**

```bash
git add hub/routes_commands.py hub/routes_observe.py tests/test_task_api.py tests/test_observability.py
git commit -m "feat: runner 命令 API — poll/heartbeat/result + SSE task 事件映射"
```

---

### Task 7: lease reconciler + 启动入口参数

**Files:**
- Modify: `hub/web.py`（`start_lease_reconciler` + `__main__` 参数）
- Test: `tests/test_task_api.py`（追加）

**Interfaces:**
- Produces:
  - `web.start_lease_reconciler(interval_s=60) -> threading.Event`（stop 句柄；每轮 `task_store.expire_leases()` + `expire_tasks()`，重派任务各 emit 一次 `task_update`）
  - CLI 新参数：`--dev-operator <email>`（开发模式 operator 兜底）

- [ ] **Step 1: 写失败测试**

```python
class LeaseReconcilerTests(TaskApiTestBase):
    def test_reconcile_once_requeues_and_emits(self):
        import time as _time
        from hub import web
        self.client.post("/api/tasks", json={
            "machine": "mac-local", "agent_type": "codex",
            "project": "agent-fleet", "instruction": "x"})
        leased = self.client.post("/api/commands/poll", json={},
                                  headers={"X-Runner-Credential": "mac-local:runner-secret"}
                                  ).get_json()["task"]
        # 人为过期
        conn = task_store._connect()
        conn.execute("UPDATE leases SET expires_at=? WHERE attempt_id=?",
                     ("2000-01-01T00:00:00Z", leased["attempt_id"]))
        conn.close()
        requeued = web.reconcile_leases_once(now=_time.time())
        self.assertEqual(requeued, [leased["task_id"]])
        task = self.client.get(f"/api/tasks/{leased['task_id']}").get_json()["task"]
        self.assertEqual(task["state"], "queued")
        # 审计可查
        conn = task_store._connect()
        actions = [r[0] for r in conn.execute(
            "SELECT action FROM audit WHERE task_id=?", (leased["task_id"],)).fetchall()]
        conn.close()
        self.assertIn("lease_expired", actions)
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_task_api.LeaseReconcilerTests -v`
Expected: FAIL — `AttributeError: module 'hub.web' has no attribute 'reconcile_leases_once'`

- [ ] **Step 3: 实现（hub/web.py 追加）**

```python
def reconcile_leases_once(now=None):
    """单轮 lease/任务过期处理；返回重派的 task_id 列表。"""
    from hub import task_store
    from hub import events as ev
    requeued = task_store.expire_leases(now=now)
    for task_id in requeued:
        try:
            ev.emit("task_update", task_id=task_id, state="queued")
        except Exception:
            pass
    task_store.expire_tasks(now=now)
    return requeued


def start_lease_reconciler(interval_s=60):
    """daemon 线程：周期性回收过期 lease。返回 stop 句柄。"""
    interval_s = max(10, int(interval_s))
    stop = threading.Event()

    def loop():
        while not stop.wait(interval_s):
            try:
                reconcile_leases_once()
            except Exception:
                pass

    thread = threading.Thread(target=loop, name="lease-reconciler", daemon=True)
    thread.start()
    return stop
```

`__main__`：parser 加 `--dev-operator`（default None，help="开发模式 operator 兜底身份（生产勿用）"）；启动序列加 `start_lease_reconciler()`；`make_app(..., dev_operator=args.dev_operator)`。

- [ ] **Step 4: 运行确认通过 + 回归 + 提交**

Run: `.venv/bin/python -m unittest discover -s tests -v`
Expected: 全绿

```bash
git add hub/web.py tests/test_task_api.py
git commit -m "feat: lease reconciler daemon + --dev-operator 开发模式"
```

---

### Task 8: 前端任务视图（task.html + 机器页任务区 + 创建模态框）

**Files:**
- Create: `hub/templates/task.html`
- Modify: `hub/templates/machine.html`、`hub/static/app.js`、`hub/static/style.css`、`hub/web.py`（`/task/<task_id>` 路由）
- Test: `tests/test_task_api.py`（追加页面冒烟）

**Interfaces:**
- Consumes: Phase 1 `FleetApp`/SSE；Task 5/6 API。
- Produces:
  - `GET /task/<task_id>` → task.html（SSR 状态机 + instruction + result；JS 接 SSE task_log）
  - machine.html 加「任务」panel（该机器最近 10 条任务）+「＋ 新建任务」按钮与模态框（agent_type 下拉 codex/claude_code/hermes、project 文本框、instruction textarea、提交后跳 `/task/<id>`；`client_token` 用 `crypto.randomUUID()` 防重复提交）
  - app.js 加：`task_update` → fleet 页刷新健康条任务计数、machine 页刷新任务列表、task 页更新状态；`task_log` → task 页 append（`FleetApp.logAppend`，500 行上限）；`createTask()`/`cancelTask()`/`retryTask()` fetch 封装

- [ ] **Step 1: 写失败测试**

```python
class TaskPageTests(TaskApiTestBase):
    def test_task_page_renders(self):
        task = self.client.post("/api/tasks", json={
            "machine": "mac-local", "agent_type": "codex",
            "project": "agent-fleet", "instruction": "做个页面"}).get_json()["task"]
        resp = self.client.get(f"/task/{task['task_id']}")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('data-page="task"', html)
        self.assertIn('data-task-id="' + task["task_id"], html)
        self.assertIn("做个页面", html)
        self.assertEqual(self.client.get("/task/t-missing").status_code, 404)

    def test_machine_page_has_task_section_and_create_button(self):
        self.client.post("/api/tasks", json={
            "machine": "mac-local", "agent_type": "codex",
            "project": "agent-fleet", "instruction": "任务甲"})
        html = self.client.get("/machine/mac-local").get_data(as_text=True)
        self.assertIn("task-list", html)
        self.assertIn("任务甲", html)
        self.assertIn("新建任务", html)
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_task_api.TaskPageTests -v`
Expected: FAIL — `/task/...` 404

- [ ] **Step 3: 写 hub/templates/task.html**

```html
{% extends "base.html" %}
{% block title %}{{ task.task_id }} · agent-fleet{% endblock %}
{% block page_id %}task{% endblock %}
{% block body_attrs %} data-task-id="{{ task.task_id }}"{% endblock %}
{% block content %}
<h1 style="font-size:20px;margin:0 0 4px;">任务 {{ task.task_id }}</h1>
<div class="meta">
  <a href="/machine/{{ task.machine }}">{{ task.machine }}</a> · {{ task.agent_type }} ·
  {{ task.project }} · {{ task.requested_by }} · {{ task.created_at }}
</div>
<div class="panel" style="margin-bottom:16px;">
  <div class="stateflow" id="stateflow">
    {% for s in ['queued', 'leased', 'running', 'succeeded'] %}
    <span class="state-node{% if task.state == s %} current{% endif %}
      {% if task.state in ['failed', 'cancelled', 'expired'] and loop.index == 4 %} {% endif %}"
      data-state="{{ s }}">{{ s }}</span>{% if not loop.last %}<span class="state-arrow">→</span>{% endif %}
    {% endfor %}
    {% if task.state in ['failed', 'cancelled', 'expired'] %}
    <span class="state-node terminal-{{ task.state }} current">{{ task.state }}</span>
    {% endif %}
  </div>
</div>
<div class="detail-grid">
  <div class="panel">
    <h3>指令</h3>
    <pre class="instruction">{{ task.instruction }}</pre>
    <div class="actions">
      {% if task.state in ['queued', 'leased', 'running'] %}
      <button onclick="FleetApp.cancelTask('{{ task.task_id }}')">取消</button>
      {% endif %}
      {% if task.state in ['succeeded', 'failed', 'cancelled', 'expired'] %}
      <button onclick="FleetApp.retryTask('{{ task.task_id }}')">重试</button>
      {% endif %}
    </div>
  </div>
  <div class="panel">
    <h3>结果</h3>
    {% if task.result %}
    <div class="metric"><span>退出码</span><b>{{ task.result.exit_code }}</b></div>
    <div class="metric"><span>耗时</span><b>{{ '%.1f' % task.result.duration_s }}s</b></div>
    <h3 style="margin-top:12px;">diff 摘要</h3>
    <pre class="diffstat">{{ task.result.diff_stat or '（无）' }}</pre>
    {% else %}
    <div class="meta" id="result-pending">任务尚未完成</div>
    {% endif %}
  </div>
</div>
<div class="panel">
  <h3>实时日志</h3>
  <div class="log-area" id="task-log">{% if task.result %}{{ task.result.log_summary }}{% endif %}</div>
</div>
{% endblock %}
```

- [ ] **Step 4: machine.html 追加任务区（在 24h 时间线 panel 之前插入）**

```html
<div class="panel" style="margin-bottom:16px;">
  <h3>任务 <button style="float:right" onclick="document.getElementById('task-modal').classList.add('open')">＋ 新建任务</button></h3>
  <div id="task-list">
    {% for t in tasks %}
    <div class="task-row" data-task-id="{{ t.task_id }}">
      <a href="/task/{{ t.task_id }}">{{ t.task_id }}</a>
      <span class="task-state st-{{ t.state }}">{{ t.state }}</span>
      <span class="meta">{{ t.agent_type }} · {{ t.project }} · {{ t.instruction[:60] }} · {{ t.created_at }}</span>
    </div>
    {% else %}
    <div class="meta">暂无任务</div>
    {% endfor %}
  </div>
</div>
<div class="modal" id="task-modal">
  <div class="modal-box">
    <h3>新建任务 · {{ name }}</h3>
    <label>agent 类型
      <select id="nt-agent">
        <option value="codex">codex</option>
        <option value="claude_code">claude_code</option>
        <option value="hermes">hermes</option>
      </select></label>
    <label>项目（须在白名单）<input id="nt-project" placeholder="agent-fleet"></label>
    <label>指令（≤2000 字符）<textarea id="nt-instruction" rows="5" maxlength="2000"></textarea></label>
    <div class="actions">
      <button onclick="FleetApp.createTask('{{ name }}')">创建</button>
      <button onclick="document.getElementById('task-modal').classList.remove('open')">取消</button>
    </div>
    <div class="err" id="nt-error"></div>
  </div>
</div>
```

web.py `machine_view` 增加（在 render_template 前）：

```python
        tasks = []
        if app.config.get("TASKS_ENABLED"):
            try:
                from hub import task_store
                tasks = task_store.list_tasks(machine=name, limit=10)
            except Exception:
                tasks = []
```
并把 `tasks=tasks` 传入 render_template。

web.py 加任务页路由：

```python
    @app.route("/task/<task_id>")
    def task_view(task_id):
        if not app.config.get("TASKS_ENABLED"):
            return "tasks unavailable", 503
        from hub import task_store
        task = task_store.get_task(task_id)
        if not task:
            return "task not found", 404
        return render_template("task.html", task=task)
```

注意：`/task/<id>` 与 `/api/tasks/<id>` 一样是 operator 资源。模板页在 CF Access 之后（部署层保护，Phase 4 文档）；本路由不加 API 认证装饰器（浏览器 session 由 CF 边缘认证），与 `/`、`/machine/<name>` 同级。在 Phase 4 部署文档中明确 CF Access 覆盖路径含 `/task/*`。

- [ ] **Step 5: app.js 追加（文件尾部，IIFE 内 `connect();` 之前插入以下函数与事件注册，并把两个 addEventListener 加进 connect()）**

```javascript
  /* ---- 任务操作 ---- */
  F.createTask = function (machine) {
    const errEl = document.getElementById('nt-error');
    const body = {
      machine: machine,
      agent_type: document.getElementById('nt-agent').value,
      project: document.getElementById('nt-project').value.trim(),
      instruction: document.getElementById('nt-instruction').value,
      client_token: (crypto.randomUUID ? crypto.randomUUID() : String(Date.now())),
    };
    fetch('/api/tasks', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(function (r) { return r.json().then(function (d) { return { status: r.status, d: d }; }); })
      .then(function (res) {
        if (res.d.ok) { window.location.href = '/task/' + res.d.task.task_id; }
        else if (errEl) { errEl.textContent = res.d.detail || res.d.error || '创建失败'; }
      })
      .catch(function () { if (errEl) errEl.textContent = '网络错误'; });
  };

  F.cancelTask = function (taskId) {
    fetch('/api/tasks/' + encodeURIComponent(taskId) + '/cancel', { method: 'POST' })
      .then(function () { location.reload(); });
  };

  F.retryTask = function (taskId) {
    fetch('/api/tasks/' + encodeURIComponent(taskId) + '/retry', { method: 'POST' })
      .then(function () { location.reload(); });
  };
```

connect() 内追加：

```javascript
    es.addEventListener('task_update', function (e) {
      const d = JSON.parse(e.data);
      lastTs = Math.max(lastTs, d.ts || 0);
      if (page === 'task' && d.task_id === document.body.getAttribute('data-task-id')) {
        if (d.state === 'succeeded' || d.state === 'failed' ||
            d.state === 'cancelled' || d.state === 'expired') {
          location.reload(); /* 终态：重载拿结果 */
        } else {
          const flow = document.getElementById('stateflow');
          if (flow) {
            flow.querySelectorAll('.state-node').forEach(function (n) {
              n.classList.toggle('current', n.getAttribute('data-state') === d.state);
            });
          }
        }
      } else if (page === 'machine' || page === 'fleet') {
        refreshAll();
      }
    });
    es.addEventListener('task_log', function (e) {
      const d = JSON.parse(e.data);
      lastTs = Math.max(lastTs, d.ts || 0);
      if (page === 'task' && d.task_id === document.body.getAttribute('data-task-id')) {
        const pending = document.getElementById('result-pending');
        if (pending) pending.textContent = '任务进行中…';
        F.logAppend(document.getElementById('task-log'), d.line, 500);
        F.flash(document.getElementById('task-log'));
      }
    });
```

machine 页 `refreshAll` 的分支里补任务列表刷新（renderMachineDetail 中追加）：

```javascript
    fetch('/api/tasks?machine=' + encodeURIComponent(document.body.getAttribute('data-machine')) + '&limit=10')
      .then(function (r) { return r.json(); })
      .then(function (d) {
        const list = document.getElementById('task-list');
        if (!list || !d.ok) return;
        list.innerHTML = d.tasks.map(function (t) {
          return '<div class="task-row" data-task-id="' + F.esc(t.task_id) + '">' +
            '<a href="/task/' + encodeURIComponent(t.task_id) + '">' + F.esc(t.task_id) + '</a>' +
            '<span class="task-state st-' + F.esc(t.state) + '">' + F.esc(t.state) + '</span>' +
            '<span class="meta">' + F.esc(t.agent_type) + ' · ' + F.esc(t.project) + ' · ' +
            F.esc((t.instruction || '').slice(0, 60)) + ' · ' + F.esc(t.created_at) + '</span></div>';
        }).join('') || '<div class="meta">暂无任务</div>';
      });
```

- [ ] **Step 6: style.css 追加**

```css
/* 任务视图 */
.stateflow { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.state-node { padding: 4px 12px; border-radius: 16px; border: 1px solid var(--border); color: var(--text-3); font-size: 12px; }
.state-node.current { border-color: var(--warn); color: var(--warn); }
.state-node.current[data-state="succeeded"] { border-color: var(--ok); color: var(--ok); }
.state-node.terminal-failed.current, .state-node.terminal-cancelled.current, .state-node.terminal-expired.current { border-color: var(--bad); color: var(--bad); }
.state-arrow { color: var(--text-3); }
.instruction, .diffstat { white-space: pre-wrap; word-break: break-word; font-size: 13px; color: var(--text); background: var(--bg); border-radius: 8px; padding: 10px; max-height: 240px; overflow-y: auto; }
.log-area { font-family: monospace; font-size: 12px; color: var(--text-2); background: var(--bg); border-radius: 8px; padding: 10px; min-height: 120px; max-height: 400px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; }
.task-row { display: flex; gap: 10px; align-items: baseline; padding: 6px 0; border-bottom: 1px solid var(--bg); font-size: 13px; }
.task-state { font-size: 11px; padding: 1px 8px; border-radius: 10px; border: 1px solid var(--border); color: var(--text-2); }
.st-succeeded { border-color: var(--ok); color: var(--ok); }
.st-failed, .st-cancelled, .st-expired { border-color: var(--bad); color: var(--bad); }
.st-running, .st-leased { border-color: var(--warn); color: var(--warn); }
.actions { margin-top: 12px; display: flex; gap: 8px; }
button { background: var(--border); color: var(--text); border: none; border-radius: 6px; padding: 6px 12px; cursor: pointer; font-size: 13px; }
button:hover { background: #475569; }
.modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.6); z-index: 10; }
.modal.open { display: flex; align-items: center; justify-content: center; }
.modal-box { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 20px; width: min(560px, 92vw); }
.modal-box label { display: block; font-size: 13px; color: var(--text-2); margin: 10px 0; }
.modal-box input, .modal-box select, .modal-box textarea { display: block; width: 100%; margin-top: 4px; background: var(--bg); color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 8px; font-size: 13px; font-family: inherit; }
```

- [ ] **Step 7: 运行全部测试**

Run: `.venv/bin/python -m unittest discover -s tests -v && python3 -m compileall -q connectors hub tools tests`
Expected: 全绿

- [ ] **Step 8: 提交**

```bash
git add hub/templates/task.html hub/templates/machine.html hub/static/app.js hub/static/style.css hub/web.py tests/test_task_api.py
git commit -m "feat: 任务详情页 + 机器页任务区 + 新建任务模态框"
```

---

### Task 9: Phase 2 冒烟 + 文档

**Files:**
- Modify: `README.md`、`docs/HANDOFF.md`

- [ ] **Step 1: 本地全链路冒烟**

```bash
mkdir -p /tmp/fleet-smoke/credentials
AGENT_FLEET_INGEST_TOKEN='smoke-ingest' .venv/bin/python hub/web.py --port 8792 \
  --dev-operator 'op@example.com' &
sleep 1
# 上报让机器在线
curl -s -X POST http://127.0.0.1:8792/api/ingest -H 'X-Agent-Fleet-Token: smoke-ingest' \
  -H 'Content-Type: application/json' -d '{"machine":"smoke","agents":{},"system":{}}'
# 临时项目白名单：dev 模式下 PROJECT_WHITELIST=None → 读 hosts.yaml。
# hosts.yaml 给 smoke 加 projects: [demo]（或先用已注册机器名）
curl -s -X POST http://127.0.0.1:8792/api/tasks -H 'Content-Type: application/json' \
  -d '{"machine":"smoke","agent_type":"codex","project":"demo","instruction":"echo hi"}'
# runner 流程（credential 来自 credentials/runner-credentials.json：{"smoke": "rs"}）
curl -s -X POST http://127.0.0.1:8792/api/commands/poll -H 'X-Runner-Credential: smoke:rs' -d '{}'
# 用返回的 attempt_id/nonce 依次 heartbeat、result；浏览器打开 /task/<id> 看实时日志与终态
kill %1
```

逐条核对：401/403 隔离、lease 领取、结果幂等（重复 POST result 返回 `"stored": false`）、页面状态流转。

- [ ] **Step 2: 更新 README.md**

「Web API」列表追加：

```markdown
- `GET /task/<id>`：任务详情页（状态机 + 实时日志 + 结果）
- `POST /api/tasks`：创建任务（operator 认证：CF Access 头；开发模式 `--dev-operator`）
- `GET /api/tasks` / `GET /api/tasks/<id>`：任务查询
- `POST /api/tasks/<id>/cancel` / `retry`：取消 / 重试
- `POST /api/commands/poll`：runner 领取任务（`X-Runner-Credential: <machine>:<secret>`）
- `POST /api/commands/<attempt_id>/heartbeat`：lease 续期（可携带 ≤50 行日志）
- `POST /api/commands/<attempt_id>/result`：回传结果（attempt_id 幂等）

项目白名单：hosts.yaml 中 host 加 `projects: [name, ...]`；runner 凭据：credentials/runner-credentials.json `{"<machine>": "<secret>"}`（0600，不进 git）。
```

「当前限制」小节改为：中心控制链路已实现（任务队列 + runner pull），runner 端执行器见 Phase 3。

- [ ] **Step 3: 更新 docs/HANDOFF.md**

架构部件表追加：

```markdown
| hub/task_store.py | HK 容器 | SQLite 任务/租约/结果/审计（state/fleet.db，WAL） |
| hub/routes_tasks.py | HK 容器 | operator 任务 API（CF Access） |
| hub/routes_commands.py | HK 容器 | runner 命令 API（每机 credential） |
```

「未实现能力」改为：runner 执行器（tools/agent-runner.py + adapters）尚未实现——任务可创建并入队，Phase 3 前不会被消费。

- [ ] **Step 4: 最终回归 + 提交**

Run: `.venv/bin/python -m unittest discover -s tests -v && python3 -m compileall -q connectors hub tools tests`
Expected: 全绿

```bash
git add README.md docs/HANDOFF.md
git commit -m "docs: Phase 2 任务 API 文档"
```

---

## Self-Review 记录

- **Spec 覆盖**：§4.3 四表 schema 逐字段一致 ✅；§5.2 创建/领取/心跳/回传/过期五条流 ✅（lease_task 事务含旧 lease 作废 ✅）；§5.3 task_update/task_log SSE ✅；§7.1 错误表（400/401/403/404/409/500）✅；§7.4 长度上限 ✅；§7.5 SQLite 降级 503 ✅、`--dev-operator` ✅。
- **类型一致性**：`lease_task` 返回键与 routes_commands poll 透传一致 ✅；`heartbeat` 返回 `{"task_id","lease_expires_at"}` 与 heartbeat 路由解包一致 ✅；`complete_task` 返回 `{"task_id","state","stored"}` 一致 ✅；SSE payload 键（task_id/line/state）与 app.js 消费一致 ✅。
- **有意偏离 spec 处**：`/api/tasks/<id>/files/<path>` 未实现（spec 数据模型无支撑，见 Global Constraints 末条，Phase 3 末评估）。
