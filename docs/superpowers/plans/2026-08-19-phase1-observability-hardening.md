# Phase 1 观测加固 Implementation Plan

> **Status (2026-09-08): historical / implemented.** Open checkboxes below are the original plan and were not kept in sync after merge. Do not re-implement from unchecked boxes. Remaining work is recorded in `docs/HANDOFF.md` §六 as `done` or `deferred-with-condition`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把单体 hub/web.py 拆成蓝图模块、给 JSONL 状态存储加轮转、修复 scan filter 语义、前端抽离为三视图模板 + 静态资源 + SSE 实时推送，全程不破坏现有观测链路。

**Architecture:** Flask blueprint 按认证域拆分（Phase 1 只有 observe 域）；JSONL 观测存储保留但加 >5MB 轮转；前端从 `render_template_string(PAGE)` 迁到 `hub/templates/` + `hub/static/`；SSE 复用现有 `hub/events.py` 订阅总线，断线自动降级 10s 轮询。

**Tech Stack:** Python 3 标准库 + Flask + PyYAML（现有依赖，无新增）；原生 JS（EventSource），无构建链；unittest。

**Spec:** docs/superpowers/specs/2026-08-19-v4-optimization-design.md（本计划实现其 §4.1 observe 部分、§4.4、§5.1、§5.3、§6、§7.3/7.5 的观测侧）

## Global Constraints

- push-only：hub 不反向连接机器；不新增任何 SSH/子进程远程执行。
- token 不进入 git、文档或日志；所有凭据经 `credentials/`（gitignored）或环境变量。
- probe 出站与 hub 公共 API 双重应用 `report_schema.py` 字段白名单。
- ingest machine 名必须匹配 `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`。
- state current JSON 原子替换（NamedTemporaryFile + fsync + os.replace）。
- 不引入新依赖（requirements.txt 不变）；前端无 Node 构建链。
- 测试运行：`.venv/bin/python -m unittest discover -s tests -v`；语法检查：`python3 -m compileall -q connectors hub tools tests`。
- 现有测试（tests/test_regressions.py、tests/test_push_only.py）必须保持全绿——它们 patch `store.STATE_DIR`、`events.EVENT_LOG`、`web.STATE_DIR`，因此 `hub/web.py` 必须保留 `STATE_DIR` 模块属性（可为未使用的兼容别名），`hub/state.py` / `hub/events.py` 的目录属性名不变。

## 文件结构

| 文件 | 动作 | 职责 |
|---|---|---|
| `hub/state.py` | 修改 | 加 `rotate_if_needed()`，`save_snapshot()` 追加后调用 |
| `hub/scan.py` | 修改 | `reconcile_ingest(now=None, machine=None)` 支持单机范围；`scan_all(filter_host=...)` 传透 |
| `hub/auth.py` | 新建 | `MACHINE_RE`、`resolve_ingest_token()`、`require_ingest_token` 装饰器（Phase 2 在此追加 operator/runner） |
| `hub/routes_observe.py` | 新建 | observe 蓝图：`/api/ingest`、`/api/scan`、`/api/status`、`/api/machines/<name>`、`/api/events`、`/api/stream`（SSE）；`build_summary()` 迁到这里 |
| `hub/events.py` | 修改 | 加 `sse_subscribe()` / `sse_unsubscribe()`（queue 桥） |
| `hub/web.py` | 修改 | 瘦身为 app 工厂 + 页面路由 + 启动入口；挂载 observe 蓝图；保留 `STATE_DIR` 兼容属性 |
| `hub/templates/base.html` | 新建 | 布局骨架 + SSE 连接状态点 |
| `hub/templates/fleet.html` | 新建 | 总览：健康条 + 机器网格 + 事件流侧栏 |
| `hub/templates/machine.html` | 新建 | 机器详情：系统指标 + agent 表 + 24h 时间线 |
| `hub/static/style.css` | 新建 | 全部样式（深色主题、响应式） |
| `hub/static/app.js` | 新建 | SSE 客户端 + 轮询降级 + fetch 封装 |
| `hub/static/components.js` | 新建 | 卡片/时间线/事件渲染纯函数（`FleetApp` 全局） |
| `tests/test_observability.py` | 新建 | 轮转、scan filter、机器详情 API、SSE 事件测试 |

---

### Task 1: state.py JSONL 轮转

**Files:**
- Modify: `hub/state.py`（在 `save_snapshot` 后加轮转）
- Test: `tests/test_observability.py`

**Interfaces:**
- Consumes: 现有 `state.save_snapshot(machine, snapshot)`、`_mach_file`、`_LOCKS`。
- Produces: `state.rotate_if_needed(machine) -> bool`；模块常量 `state.MAX_JSONL_BYTES = 5 * 1024 * 1024`、`state.ROTATE_KEEP_LINES = 2000`。`save_snapshot` 行为对调用方不变。

- [ ] **Step 1: 写失败测试**

在新建的 `tests/test_observability.py` 中：

```python
import json
import tempfile
import unittest
from pathlib import Path

from hub import state as store


class JsonlRotationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.old_state_dir = store.STATE_DIR
        self.old_max = store.MAX_JSONL_BYTES
        store.STATE_DIR = self.temp_dir
        store.MAX_JSONL_BYTES = 4096  # 缩小阈值避免写 5MB

    def tearDown(self):
        store.STATE_DIR = self.old_state_dir
        store.MAX_JSONL_BYTES = self.old_max

    def test_oversized_jsonl_rotates_and_keeps_tail(self):
        for i in range(200):
            store.save_snapshot("hk", {"machine": "hk", "seq": i, "pad": "x" * 100})
        path = self.temp_dir / "hk.jsonl"
        self.assertLessEqual(path.stat().st_size, 4096 * 2)
        lines = path.read_text().splitlines()
        self.assertLessEqual(len(lines), 200)
        # 最新快照必须保留，且每行可解析
        self.assertEqual(json.loads(lines[-1])["seq"], 199)
        for line in lines:
            json.loads(line)

    def test_small_jsonl_is_untouched(self):
        store.save_snapshot("hk", {"machine": "hk", "seq": 0})
        store.save_snapshot("hk", {"machine": "hk", "seq": 1})
        self.assertEqual(len((self.temp_dir / "hk.jsonl").read_text().splitlines()), 2)
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_observability.JsonlRotationTests -v`
Expected: FAIL — `AttributeError: module 'hub.state' has no attribute 'MAX_JSONL_BYTES'`

- [ ] **Step 3: 实现轮转**

在 `hub/state.py` 顶部常量区（`_LOCKS` 之后）加：

```python
MAX_JSONL_BYTES = 5 * 1024 * 1024
ROTATE_KEEP_LINES = 2000


def rotate_if_needed(machine):
    """JSONL 超过 MAX_JSONL_BYTES 时截断为最近 ROTATE_KEEP_LINES 行（原子替换）。"""
    f = _mach_file(machine)
    try:
        if not f.exists() or f.stat().st_size <= MAX_JSONL_BYTES:
            return False
    except OSError:
        return False
    with _LOCKS[machine]:
        try:
            lines = f.read_text().splitlines()
        except OSError:
            return False
        keep = lines[-ROTATE_KEEP_LINES:]
        tmp = tempfile.NamedTemporaryFile(
            mode="w", dir=STATE_DIR, prefix="rotate-", suffix=".tmp", delete=False
        )
        tmp_name = tmp.name
        try:
            with tmp:
                tmp.write("\n".join(keep) + "\n")
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, f)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
    return True
```

在 `save_snapshot` 中 JSONL 追加之后、写 current JSON 之前插入一行：

```python
        rotate_if_needed(machine)
```

（`_LOCKS` 是 RLock，`save_snapshot` 持锁时调用安全。）

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/python -m unittest tests.test_observability.JsonlRotationTests -v`
Expected: PASS（2 个测试）

- [ ] **Step 5: 回归 + 提交**

Run: `.venv/bin/python -m unittest discover -s tests -v`
Expected: 全绿（既有 20 行 JSONL 并发测试不受影响，因为轮转阈值远大于测试数据量）

```bash
git add hub/state.py tests/test_observability.py
git commit -m "feat: 观测 JSONL 存储轮转 — >5MB 截断保留最近 2000 行"
```

---

### Task 2: scan.py 单机范围 reconcile（修 filter 语义）

**Files:**
- Modify: `hub/scan.py:70-104`
- Test: `tests/test_observability.py`（追加）

**Interfaces:**
- Consumes: 现有 `reconcile_ingest(now=None)`、`_state_machines()`。
- Produces: `reconcile_ingest(now=None, machine=None) -> list`；`scan_all(filter_host=None)` 语义变为「只 reconcile 指定机器」，返回值结构不变。

- [ ] **Step 1: 写失败测试**

在 `tests/test_observability.py` 追加：

```python
import time
from hub import scan, events


class ScanFilterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.old_state_dir = store.STATE_DIR
        self.old_event_log = events.EVENT_LOG
        store.STATE_DIR = self.temp_dir
        events.EVENT_LOG = self.temp_dir / "events.jsonl"

    def tearDown(self):
        store.STATE_DIR = self.old_state_dir
        events.EVENT_LOG = self.old_event_log

    def _write_stale(self, machine):
        store.save_snapshot(machine, {
            "machine": machine, "source": "ingest", "reachable": True, "agents": {},
        })
        current = store.read_current(machine)
        current["_ts"] = time.time() - 301
        (self.temp_dir / f"{machine}.json").write_text(json.dumps(current))

    def test_machine_scoped_reconcile_leaves_other_machines_alone(self):
        self._write_stale("hk")
        self._write_stale("tebi")
        results = scan.reconcile_ingest(now=time.time(), machine="hk")
        self.assertEqual([r["machine"] for r in results], ["hk"])
        self.assertFalse(store.read_current("hk")["reachable"])
        self.assertTrue(store.read_current("tebi")["reachable"])

    def test_scan_all_filter_host_only_reconciles_that_host(self):
        self._write_stale("hk")
        self._write_stale("tebi")
        results = scan.scan_all(filter_host="tebi")
        self.assertEqual([r["machine"] for r in results], ["tebi"])
        self.assertTrue(store.read_current("hk")["reachable"])
        self.assertFalse(store.read_current("tebi")["reachable"])
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_observability.ScanFilterTests -v`
Expected: FAIL — `TypeError: reconcile_ingest() got an unexpected keyword argument 'machine'`

- [ ] **Step 3: 实现**

把 `hub/scan.py` 的 `reconcile_ingest` 与 `scan_all` 改为：

```python
def reconcile_ingest(now=None, machine=None):
    """Mark expired push reports offline without executing any process.

    machine: 只 reconcile 指定机器（None = 全部）。
    """
    now = time.time() if now is None else now
    ttls = _configured_ttls()
    results = []
    machines = [machine] if machine else sorted(_state_machines())
    for name in machines:
        old = store.read_current(name)
        if not old or old.get("source") != "ingest":
            continue
        if not old.get("reachable", True):
            continue
        ttl = ttls.get(name, DEFAULT_STALE_AFTER_S)
        last_seen = float(old.get("_ts", 0))
        if now - last_seen <= ttl:
            continue
        snapshot = dict(old)
        snapshot.update({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "reachable": False,
            "remote_error": f"ingest 超过 {ttl}s 未上报",
            "source": "ingest",
        })
        store.save_snapshot(name, snapshot)
        changes = _diff(old, snapshot)
        if changes:
            events.emit("state_changed", machine=name, changes=changes, snapshot=snapshot)
        results.append({"machine": name, "changed": changes, "stale": True})
    return results


def scan_all(filter_host=None):
    return reconcile_ingest(machine=filter_host)
```

（函数体其余逻辑不变，仅循环变量 `machine` 改名 `name` 避免与参数冲突。）

- [ ] **Step 4: 运行确认通过 + 回归**

Run: `.venv/bin/python -m unittest discover -s tests -v`
Expected: 全绿（既有 `reconcile_ingest(now=...)` 调用兼容）

- [ ] **Step 5: 提交**

```bash
git add hub/scan.py tests/test_observability.py
git commit -m "fix: scan filter 语义 — 单机 reconcile 不再先全量扫描"
```

---

### Task 3: hub/auth.py（ingest token 认证域）

**Files:**
- Create: `hub/auth.py`
- Test: `tests/test_observability.py`（追加）

**Interfaces:**
- Consumes: Flask `request`、`jsonify`；`credentials/ingest-token`。
- Produces:
  - `auth.MACHINE_RE`（re.compile，`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`）
  - `auth.resolve_ingest_token(cli_token=None) -> str | None`（CLI > env `AGENT_FLEET_INGEST_TOKEN` > credentials 文件）
  - `auth.require_ingest_token(view)` — 装饰器；token 从 `flask.current_app.config["INGEST_TOKEN"]` 读取，未配置则放行（与现状一致），配置了则校验 `X-Agent-Fleet-Token` 头（hmac.compare_digest），失败返回 403 `{"ok": false, "error": "forbidden"}`。

- [ ] **Step 1: 写失败测试**

```python
class AuthIngestTokenTests(unittest.TestCase):
    def _make_app(self, token):
        from flask import Flask, jsonify
        from hub import auth
        app = Flask(__name__)
        app.config["INGEST_TOKEN"] = token

        @app.route("/probe", methods=["POST"])
        @auth.require_ingest_token
        def probe():
            return jsonify({"ok": True})
        return app

    def test_valid_token_passes(self):
        client = self._make_app("secret").test_client()
        resp = client.post("/probe", headers={"X-Agent-Fleet-Token": "secret"})
        self.assertEqual(resp.status_code, 200)

    def test_wrong_or_missing_token_403(self):
        client = self._make_app("secret").test_client()
        self.assertEqual(client.post("/probe").status_code, 403)
        self.assertEqual(
            client.post("/probe", headers={"X-Agent-Fleet-Token": "nope"}).status_code, 403)

    def test_unconfigured_token_allows(self):
        client = self._make_app(None).test_client()
        self.assertEqual(client.post("/probe").status_code, 200)

    def test_machine_re(self):
        from hub import auth
        self.assertTrue(auth.MACHINE_RE.fullmatch("mac-local.1"))
        self.assertFalse(auth.MACHINE_RE.fullmatch("../etc"))
        self.assertFalse(auth.MACHINE_RE.fullmatch("-lead-dash"))
        self.assertFalse(auth.MACHINE_RE.fullmatch("x" * 65))
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_observability.AuthIngestTokenTests -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hub.auth'`

- [ ] **Step 3: 实现 hub/auth.py**

```python
"""hub/auth.py — 认证域装饰器与凭据解析

三个认证域（蓝图边界 = 认证边界）：
  observe  — ingest token（机器上报）
  tasks    — CF Access operator（Phase 2 实现 require_operator）
  commands — runner credential（Phase 2 实现 require_runner）
"""
import functools
import hmac
import os
import re
from pathlib import Path

from flask import current_app, jsonify, request

FLEET_HOME = Path(__file__).resolve().parent.parent
INGEST_TOKEN_FILE = FLEET_HOME / "credentials" / "ingest-token"
INGEST_TOKEN_ENV = "AGENT_FLEET_INGEST_TOKEN"
MACHINE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def resolve_ingest_token(cli_token=None):
    """ingest 令牌解析优先级：CLI > env > credentials/ingest-token 文件。"""
    if cli_token:
        return cli_token
    env = os.environ.get(INGEST_TOKEN_ENV)
    if env:
        return env
    try:
        if INGEST_TOKEN_FILE.exists():
            return INGEST_TOKEN_FILE.read_text().strip() or None
    except Exception:
        pass
    return None


def _forbidden():
    return jsonify({"ok": False, "error": "forbidden"}), 403


def require_ingest_token(view):
    """observe 域：X-Agent-Fleet-Token 头与 app.config['INGEST_TOKEN'] 比对。"""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        token = current_app.config.get("INGEST_TOKEN")
        if token:
            got = request.headers.get("X-Agent-Fleet-Token", "")
            if not hmac.compare_digest(got, token):
                return _forbidden()
        return view(*args, **kwargs)
    return wrapper
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/python -m unittest tests.test_observability.AuthIngestTokenTests -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add hub/auth.py tests/test_observability.py
git commit -m "feat: hub/auth.py 认证模块 — ingest token 域"
```

---

### Task 4: routes_observe.py 蓝图（从 web.py 抽取）

**Files:**
- Create: `hub/routes_observe.py`
- Modify: `hub/web.py`（本任务先并存：web.py 改为挂载蓝图并删除内联路由；页面路由暂时保留旧 PAGE，Task 7 再换模板）
- Test: `tests/test_observability.py`（追加）；既有 `tests/test_regressions.py` 必须不动且通过

**Interfaces:**
- Consumes: `auth.require_ingest_token`、`auth.MACHINE_RE`、`report_schema.sanitize_agents/sanitize_system`、`hub.scan._diff`、`hub.events.emit/read_recent`、`hub.state.read_current/save_snapshot/STATE_DIR`、`hub.web.load_hosts`（hosts.yaml 读取逻辑暂留 web.py，蓝图 import）。
- Produces:
  - `routes_observe.bp`（Flask Blueprint，name="observe"）
  - `routes_observe.build_summary() -> list[dict]`（从 web.py 迁入，签名不变，供 web.py 页面路由 import）
  - 路由：`POST /api/ingest`、`POST /api/scan`、`GET /api/status`、`GET /api/machines/<name>`、`GET /api/events?limit=`
  - `/api/machines/<name>` 返回 `{"ok": True, "machine": name, "current": <sanitize 后快照>, "history": [{"ts": epoch, "reachable": bool}, ...]}`；未知机器 404 `{"ok": false, "error": "not_found"}`，非法名 400。

关键约束：`build_summary` 内所有文件访问必须**动态**引用 `store.STATE_DIR`（每次调用时读取模块属性），不能用模块级拷贝——既有测试靠 patch `store.STATE_DIR` 隔离。

- [ ] **Step 1: 写失败测试**

```python
class ObserveBlueprintTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.old_state_dir = store.STATE_DIR
        self.old_event_log = events.EVENT_LOG
        store.STATE_DIR = self.temp_dir
        events.EVENT_LOG = self.temp_dir / "events.jsonl"
        from hub import web
        self.client = web.make_app(ingest_token="secret").test_client()

    def tearDown(self):
        store.STATE_DIR = self.old_state_dir
        events.EVENT_LOG = self.old_event_log

    def _ingest(self, machine="mac-local"):
        return self.client.post("/api/ingest", json={
            "machine": machine,
            "agents": {"hermes": {"installed": True, "gateway_state": "running",
                                  "sessions": [{"session_id": "x"}]}},
            "system": {"platform": "darwin", "load": "1.0"},
        }, headers={"X-Agent-Fleet-Token": "secret"})

    def test_ingest_and_status_via_blueprint(self):
        self.assertEqual(self._ingest().status_code, 200)
        payload = self.client.get("/api/status").get_json()
        machine = next(m for m in payload["machines"] if m["machine"] == "mac-local")
        self.assertEqual(machine["agents"]["hermes"]["session_count"], 1)
        self.assertNotIn("sessions", machine["agents"]["hermes"])

    def test_machine_detail_api(self):
        self._ingest()
        resp = self.client.get("/api/machines/mac-local")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["current"]["reachable"])
        self.assertEqual(data["current"]["agents"]["hermes"]["session_count"], 1)
        self.assertIsInstance(data["history"], list)
        self.assertEqual(self.client.get("/api/machines/ghost").status_code, 404)
        self.assertEqual(self.client.get("/api/machines/..%2Fetc").status_code, 400)

    def test_events_api_lists_recent_events(self):
        self._ingest()
        payload = self.client.get("/api/events").get_json()
        self.assertTrue(any(e["machine"] == "mac-local" for e in payload["events"]))
        # 事件流不携带完整快照（SSE/列表只给摘要）
        for e in payload["events"]:
            self.assertNotIn("snapshot", e.get("extra", {}))
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_observability.ObserveBlueprintTests -v`
Expected: FAIL — `assertEqual(resp.status_code, 200)` 处 404（`/api/machines/...` 尚不存在）

- [ ] **Step 3: 实现 hub/routes_observe.py**

```python
"""hub/routes_observe.py — 观测链路蓝图（ingest token 认证域）

路由：
  POST /api/ingest        机器自报告（token 认证）
  POST /api/scan          触发 stale reconciliation（token 认证，后台线程）
  GET  /api/status        全部机器摘要（公共，已白名单脱敏）
  GET  /api/machines/<n>  单机详情 + 历史时间线（公共，脱敏）
  GET  /api/events        最近事件摘要（公共，不含快照体）
  GET  /api/stream        SSE 实时事件流（Task 5 实现）
"""
import threading
import time

from flask import Blueprint, jsonify, request

from hub import events as ev
from hub import scan
from hub import state as store
from hub.auth import MACHINE_RE, require_ingest_token
from report_schema import sanitize_agents, sanitize_system

bp = Blueprint("observe", __name__)


def build_summary():
    """机器摘要列表（供 /api/status 与 Fleet 页面 SSR）。从 web.py 迁入，逻辑不变。"""
    from hub.web import load_hosts  # hosts.yaml 解析暂留 web.py，避免循环 import
    hosts = load_hosts()
    known = {h["name"] for h in hosts}
    rows = []
    try:
        for f in sorted(store.STATE_DIR.glob("*.json")):
            name = f.stem
            if name in known:
                continue
            current = store.read_current(name)
            if current and current.get("source") == "ingest" and current.get("machine"):
                hosts.append({"name": name, "desc": "自报告 (ingest)", "transport": "ingest"})
                known.add(name)
    except Exception:
        pass

    for h in hosts:
        st = store.read_current(h["name"])
        if not st:
            rows.append({"machine": h["name"], "desc": h["desc"], "online": False,
                         "error": "no data"})
            continue
        if not st.get("reachable", True):
            rows.append({"machine": h["name"], "desc": h["desc"], "online": False,
                         "error": st.get("remote_error", "unreachable")})
            continue
        agents = sanitize_agents(st.get("agents", {}))
        hermes = agents.get("hermes") if isinstance(agents, dict) else None
        is_hermes = bool(isinstance(hermes, dict) and hermes.get("installed"))
        agent_summaries = []
        for atype, astate in agents.items():
            if isinstance(astate, dict) and astate.get("error"):
                agent_summaries.append({"type": atype, "status": "error", "detail": astate["error"]})
            elif isinstance(astate, dict) and astate.get("installed") is False:
                agent_summaries.append({"type": atype, "status": "absent"})
            elif isinstance(astate, dict):
                agent_summaries.append({"type": atype, "status": "ok"})
        rows.append({
            "machine": h["name"],
            "desc": h["desc"],
            "online": True,
            "timestamp": st.get("timestamp"),
            "system": sanitize_system(st.get("system", {})),
            "agents": agents,
            "agent_summaries": agent_summaries,
            "agent_count": len(agent_summaries),
            "has_hermes": is_hermes,
            "hermes_state": (hermes or {}).get("gateway_state", "n/a") if is_hermes else None,
        })
    return rows


@bp.route("/api/ingest", methods=["POST"])
@require_ingest_token
def api_ingest():
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "invalid json"}), 400
    machine = data.get("machine")
    if not machine:
        return jsonify({"ok": False, "error": "machine required"}), 400
    if not isinstance(machine, str) or not MACHINE_RE.fullmatch(machine):
        return jsonify({"ok": False, "error": "invalid machine name"}), 400

    old = store.read_current(machine)
    payload = {
        "machine": machine,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "ingest",
        "agents": sanitize_agents(data.get("agent") or data.get("agents") or {}),
        "system": sanitize_system(data.get("system", {})),
        "reachable": True,
        "remote_error": data.get("error"),
    }
    if old and isinstance(old, dict):
        changes = scan._diff(old, payload)
    else:
        changes = ["initial"]
    if changes:
        try:
            ev.emit("state_changed", machine=machine, changes=changes, snapshot=payload)
        except Exception:
            pass
    store.save_snapshot(machine, payload)
    return jsonify({"ok": True, "changes": changes})


@bp.route("/api/scan", methods=["POST"])
@require_ingest_token
def api_scan():
    threading.Thread(target=scan.scan_all, daemon=True).start()
    return jsonify({"ok": True, "message": "scan triggered (async)"})


@bp.route("/api/status")
def api_status():
    return jsonify({"updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "machines": build_summary()})


@bp.route("/api/machines/<name>")
def api_machine(name):
    if not MACHINE_RE.fullmatch(name):
        return jsonify({"ok": False, "error": "invalid machine name"}), 400
    current = store.read_current(name)
    if not current:
        return jsonify({"ok": False, "error": "not_found"}), 404
    history = [{
        "ts": h.get("_ts"),
        "reachable": bool(h.get("reachable", True)),
    } for h in store.read_history(name, limit=720)]  # 120s 间隔 × 720 ≈ 24h
    return jsonify({
        "ok": True,
        "machine": name,
        "current": {
            "timestamp": current.get("timestamp"),
            "reachable": bool(current.get("reachable", True)),
            "remote_error": current.get("remote_error"),
            "agents": sanitize_agents(current.get("agents", {})),
            "system": sanitize_system(current.get("system", {})),
        },
        "history": history,
    })


@bp.route("/api/events")
def api_events():
    try:
        limit = min(200, max(1, int(request.args.get("limit", 50))))
    except (TypeError, ValueError):
        limit = 50
    items = []
    for e in ev.read_recent(limit):
        items.append({
            "event": e.get("event"),
            "machine": e.get("machine"),
            "ts": e.get("ts"),
            "changes": e.get("changes", []),
        })
    return jsonify({"events": items})
```

- [ ] **Step 4: web.py 挂载蓝图并删除内联 API 路由**

`hub/web.py` 修改：
1. 删除 `api_status`、`api_ingest`、`api_scan` 三个内联路由函数；删除文件顶部不再使用的 `hmac`、`sanitize_agents`、`sanitize_system` import（保留 `load_hosts`、`read_state`、`collect_all`、`start_reconciliation`、`build_summary` 中尚未迁出的部分——注意 `build_summary` 整体删除，页面路由改为 `from hub.routes_observe import build_summary`）。
2. 保留模块属性 `STATE_DIR = FLEET_HOME / "state"`（既有测试 patch 它）、`HOSTS_FILE`、`_HOSTS`、`load_hosts`、`collect_all`、`start_reconciliation`、`MACHINE_RE`（兼容 re-export：`from hub.auth import MACHINE_RE`）。
3. `make_app` 改为：

```python
def make_app(ingest_token=None, require_token=True):
    from hub import auth
    from hub import routes_observe

    resolved_token = auth.resolve_ingest_token(ingest_token)
    if require_token and not resolved_token:
        raise RuntimeError(
            "AGENT_FLEET_INGEST_TOKEN or credentials/ingest-token is required"
        )
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 256 * 1024
    app.config["INGEST_TOKEN"] = resolved_token
    app.register_blueprint(routes_observe.bp)

    @app.route("/")
    def index():
        from hub.routes_observe import build_summary
        rows = build_summary()
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        return render_template_string(PAGE, rows=rows, now=now, scan_enabled=False)

    return app
```

（`resolve_ingest_token` 在 web.py 留一个 `from hub.auth import resolve_ingest_token` 的 re-export，避免外部调用方断裂。页面模板 Task 7 再替换，本任务保持 `render_template_string(PAGE)`。）

- [ ] **Step 5: 运行全部测试确认通过**

Run: `.venv/bin/python -m unittest discover -s tests -v`
Expected: 全绿——包括既有 `IngestApiSecurityTests`（`web.make_app(ingest_token="secret")` 路径、`/api/ingest` 400/403 行为、`/api/status` 白名单断言全部不变）

- [ ] **Step 6: 提交**

```bash
git add hub/auth.py hub/routes_observe.py hub/web.py tests/test_observability.py
git commit -m "refactor: web.py 拆分 observe 蓝图 — ingest/scan/status/machines/events API"
```

---

### Task 5: SSE 实时事件流

**Files:**
- Modify: `hub/events.py`（加 SSE 订阅桥）
- Modify: `hub/routes_observe.py`（加 `/api/stream`）
- Test: `tests/test_observability.py`（追加）

**Interfaces:**
- Consumes: `events.subscribe(cb)`（现有）、`routes_observe.bp`（Task 4）。
- Produces:
  - `events.sse_subscribe(maxsize=200) -> queue.Queue`（注册桥接回调，返回队列；队列满时丢事件不阻塞 emit）
  - `events.sse_unsubscribe(q) -> None`
  - `GET /api/stream`：`Content-Type: text/event-stream`，事件映射 `state_changed → machine_update`，其余 → `fleet_event`；25s 无事件发 `: keepalive`；`?since=<epoch>` 先补发 missed 事件。
  - SSE data 载荷（JSON）：`machine_update` → `{"machine", "changes", "online", "ts", "agents", "system"}`（agents/system 过白名单）；`fleet_event` → `{"event", "machine", "changes", "ts"}`。

- [ ] **Step 1: 写失败测试**

```python
class SseStreamTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.old_state_dir = store.STATE_DIR
        self.old_event_log = events.EVENT_LOG
        store.STATE_DIR = self.temp_dir
        events.EVENT_LOG = self.temp_dir / "events.jsonl"
        from hub import web
        self.app = web.make_app(ingest_token="secret")

    def tearDown(self):
        store.STATE_DIR = self.old_state_dir
        events.EVENT_LOG = self.old_event_log

    def test_sse_bridge_receives_emitted_events(self):
        q = events.sse_subscribe()
        try:
            events.emit("state_changed", machine="hk", changes=["agents"],
                        snapshot={"reachable": True, "agents": {}, "system": {}})
            got = q.get(timeout=2)
            self.assertEqual(got["event"], "state_changed")
            self.assertEqual(got["machine"], "hk")
        finally:
            events.sse_unsubscribe(q)

    def test_stream_endpoint_delivers_machine_update(self):
        q_client = self.app.test_client()
        resp = q_client.get("/api/stream", buffered=False)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/event-stream", resp.content_type)
        events.emit("state_changed", machine="hk", changes=["agents"],
                    snapshot={"reachable": True, "agents": {"codex": {"installed": True}},
                              "system": {"load": "1.0"}})
        body = b""
        for chunk in resp.response:
            body += chunk
            if b"machine_update" in body:
                break
        resp.close()
        text = body.decode()
        self.assertIn("event: machine_update", text)
        self.assertIn('"machine": "hk"', text)
        self.assertNotIn("session", text)
```

注意：测试用 `resp.response` 迭代器逐块读，读到目标即 `resp.close()`，避免无限阻塞。`sse_subscribe`/`sse_unsubscribe` 的 queue 在测试间不泄漏（finally 中退订）。

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_observability.SseStreamTests -v`
Expected: FAIL — `AttributeError: module 'hub.events' has no attribute 'sse_subscribe'`

- [ ] **Step 3: 实现 events.py SSE 桥**

在 `hub/events.py` 顶部加 `import queue`，文件尾部加：

```python
# --- SSE 桥：把总线事件转发给 /api/stream 的队列订阅者 ---
_sse_queues = []


def sse_subscribe(maxsize=200):
    """注册一个 SSE 队列订阅者。队列满时丢事件（实时流允许丢，不允许阻塞 emit）。"""
    q = queue.Queue(maxsize=maxsize)

    def _cb(ev):
        try:
            q.put_nowait(ev)
        except queue.Full:
            pass

    with _lock:
        _sse_queues.append((q, _cb))
    subscribe(_cb)
    return q


def sse_unsubscribe(q):
    with _lock:
        pair = next((p for p in _sse_queues if p[0] is q), None)
        if pair:
            _sse_queues.remove(pair)
        if pair and pair[1] in _subscribers:
            _subscribers.remove(pair[1])
```

- [ ] **Step 4: routes_observe.py 加 /api/stream**

文件顶部 import 加 `import json`、`import queue`、`from flask import Response, stream_with_context`。蓝图尾部加：

```python
_SSE_EVENT_NAMES = {"state_changed": "machine_update"}


def _sse_payload(e):
    if e.get("event") == "state_changed" and isinstance(e.get("extra", {}).get("snapshot"), dict):
        snap = e["extra"]["snapshot"]
        return {
            "machine": e.get("machine"),
            "changes": e.get("changes", []),
            "online": bool(snap.get("reachable", True)),
            "ts": e.get("ts"),
            "agents": sanitize_agents(snap.get("agents", {})),
            "system": sanitize_system(snap.get("system", {})),
        }
    return {
        "event": e.get("event"),
        "machine": e.get("machine"),
        "changes": e.get("changes", []),
        "ts": e.get("ts"),
    }


@bp.route("/api/stream")
def api_stream():
    try:
        since = float(request.args.get("since", 0) or 0)
    except (TypeError, ValueError):
        since = 0.0
    q = ev.sse_subscribe()

    def gen():
        try:
            yield ": connected\n\n"
            if since:  # 断线重连补发
                for e in ev.read_recent(200):
                    if float(e.get("ts") or 0) > since:
                        name = _SSE_EVENT_NAMES.get(e.get("event"), "fleet_event")
                        yield f"event: {name}\ndata: {json.dumps(_sse_payload(e), ensure_ascii=False)}\n\n"
            while True:
                try:
                    e = q.get(timeout=25)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                name = _SSE_EVENT_NAMES.get(e.get("event"), "fleet_event")
                yield f"event: {name}\ndata: {json.dumps(_sse_payload(e), ensure_ascii=False)}\n\n"
        finally:
            ev.sse_unsubscribe(q)

    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
```

- [ ] **Step 5: 运行确认通过 + 回归**

Run: `.venv/bin/python -m unittest discover -s tests -v`
Expected: 全绿

- [ ] **Step 6: 提交**

```bash
git add hub/events.py hub/routes_observe.py tests/test_observability.py
git commit -m "feat: SSE 实时事件流 — events 队列桥 + /api/stream + since 补发"
```

---

### Task 6: 前端静态资源（style.css + components.js + app.js）

**Files:**
- Create: `hub/static/style.css`、`hub/static/components.js`、`hub/static/app.js`
- Test: 本任务为纯静态资源，无单元测试；验收在 Task 8 手动冒烟。

**Interfaces:**
- Produces:
  - `components.js` 暴露全局 `FleetApp`：`esc(s)`、`fmtTime(ts)`、`machineCard(r)`（返回 HTML 字符串）、`agentRow(type, a)`、`eventItem(e)`、`timeline(container, history)`、`flash(el)`、`logAppend(el, line, maxLines=500)`。所有动态文本经 `esc()`，不拼接未转义值（防日志注入，spec §7.4）。
  - `app.js`：SSE 连接管理（`EventSource('/api/stream')`），`machine_update` → 局部更新卡片 + flash；`fleet_event` → prepend 事件流（上限 50 条）；`onerror` → 关闭 SSE、启动 10s 轮询 `refreshAll()`；`onopen` → 停轮询。轮询 `refreshAll()`：GET `/api/status` 重渲网格（仅 fleet 页）。重连用 `?since=<lastTs>` 补发。

- [ ] **Step 1: 写 hub/static/style.css**

完整样式（深色主题 `#0f172a`，三级灰 `#f8fafc/#94a3b8/#64748b`，状态色 绿 `#22c55e` / 红 `#ef4444` / 黄 `#f59e0b` / 紫 `#a855f7`）：

```css
:root {
  --bg: #0f172a; --panel: #1e293b; --border: #334155;
  --text: #f8fafc; --text-2: #94a3b8; --text-3: #64748b;
  --ok: #22c55e; --bad: #ef4444; --warn: #f59e0b; --hermes: #a855f7; --link: #60a5fa;
}
* { box-sizing: border-box; }
body { font-family: -apple-system, "PingFang SC", sans-serif; background: var(--bg); color: var(--text); margin: 0; }
a { color: var(--link); text-decoration: none; }
.topbar { display: flex; align-items: center; gap: 12px; padding: 16px 24px; border-bottom: 1px solid var(--border); }
.topbar .brand { font-size: 18px; font-weight: 600; color: var(--text); }
.topbar nav { margin-left: auto; display: flex; gap: 16px; font-size: 13px; color: var(--text-2); }
.sse-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-3); display: inline-block; }
.sse-dot.live { background: var(--ok); box-shadow: 0 0 6px var(--ok); }
main { padding: 20px 24px; }

/* Fleet 布局：≥1440 三列网格+右栏；768-1440 两列；<768 单列 */
.fleet-layout { display: grid; grid-template-columns: 1fr 320px; gap: 20px; }
.healthbar { display: flex; gap: 24px; background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 12px 16px; margin-bottom: 16px; font-size: 13px; color: var(--text-2); }
.healthbar b { color: var(--text); font-size: 16px; margin-left: 4px; }
.healthbar .hb-ok b { color: var(--ok); } .healthbar .hb-bad b { color: var(--bad); } .healthbar .hb-warn b { color: var(--warn); }
.grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
.card { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 16px; transition: border-color .2s; }
.card h2 { margin: 0 0 8px; font-size: 16px; display: flex; align-items: center; gap: 8px; }
.card h2 a { color: var(--text); }
.dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; flex: none; }
.dot.on { background: var(--ok); } .dot.off { background: var(--bad); } .dot.warn { background: var(--warn); }
.badge-hermes { font-size: 11px; background: #3b0764; color: #e879f9; padding: 2px 6px; border-radius: 4px; }
.meta { color: var(--text-2); font-size: 12px; margin-bottom: 10px; }
.agent-row { background: var(--bg); border-radius: 8px; padding: 4px 8px; margin: 4px 0; font-size: 13px; display: flex; gap: 8px; }
.agent-row .k { color: var(--link); font-size: 11px; line-height: 18px; }
.st-ok { color: var(--ok); } .st-absent { color: var(--text-3); } .st-err { color: #f87171; }
.stat { display: flex; gap: 12px; flex-wrap: wrap; font-size: 12px; color: #cbd5e1; margin-top: 8px; }
.stat b { color: var(--text); }
.bar { height: 6px; background: var(--border); border-radius: 3px; margin-top: 6px; overflow: hidden; }
.bar i { display: block; height: 100%; background: var(--ok); }
.bar i.warn { background: var(--warn); } .bar i.danger { background: var(--bad); }
.err { color: #f87171; font-size: 13px; }

/* 事件流侧栏 */
.event-stream { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 12px; height: fit-content; max-height: 80vh; overflow-y: auto; }
.event-stream h3 { margin: 0 0 8px; font-size: 13px; color: var(--text-2); }
.event-item { font-size: 12px; padding: 6px 8px; border-left: 2px solid var(--border); margin: 6px 0; color: var(--text-2); }
.event-item .t { color: var(--text-3); font-family: monospace; font-size: 11px; }
.event-item.warn { border-left-color: var(--warn); } .event-item.bad { border-left-color: var(--bad); } .event-item.ok { border-left-color: var(--ok); }

/* 机器详情 */
.detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }
.panel { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 16px; }
.panel h3 { margin: 0 0 12px; font-size: 14px; color: var(--text-2); }
.metric { display: flex; justify-content: space-between; font-size: 13px; padding: 6px 0; border-bottom: 1px solid var(--bg); }
.metric:last-child { border-bottom: none; }
.metric b { color: var(--text); }
table.agent-table { width: 100%; border-collapse: collapse; font-size: 13px; }
table.agent-table th { text-align: left; color: var(--text-3); font-weight: 500; padding: 6px 8px; border-bottom: 1px solid var(--border); }
table.agent-table td { padding: 6px 8px; border-bottom: 1px solid var(--bg); }
.timeline { display: flex; gap: 1px; height: 28px; align-items: flex-end; }
.timeline i { flex: 1; min-width: 2px; background: var(--ok); border-radius: 1px; height: 100%; opacity: .85; }
.timeline i.down { background: var(--bad); }
.timeline-legend { display: flex; justify-content: space-between; font-size: 11px; color: var(--text-3); margin-top: 4px; }

/* 高亮闪烁（SSE 到达） */
@keyframes flash { 0% { border-color: var(--warn); } 100% { border-color: var(--border); } }
.flash { animation: flash .2s ease-in-out 3; }

@media (max-width: 1439px) { .grid { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 1023px) { .fleet-layout { grid-template-columns: 1fr; } .event-stream { max-height: 320px; } }
@media (max-width: 767px) { .grid { grid-template-columns: 1fr; } .detail-grid { grid-template-columns: 1fr; } main { padding: 12px; } }
```

- [ ] **Step 2: 写 hub/static/components.js**

```javascript
/* components.js — 渲染纯函数。所有动态文本必须经 esc()（textContent 等价物）。 */
window.FleetApp = window.FleetApp || {};
(function (F) {
  F.esc = function (s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  };

  F.fmtTime = function (ts) {
    if (!ts) return '?';
    const d = new Date(typeof ts === 'number' ? ts * 1000 : ts);
    return isNaN(d) ? '?' : d.toLocaleString('zh-CN', { hour12: false });
  };

  function diskPct(system) {
    const raw = (system && system.disk_used_pct) || '0';
    return parseInt(String(raw).replace('%', ''), 10) || 0;
  }

  F.agentRow = function (type, a) {
    let status;
    if (a && a.error) status = '<span class="st-err">● ' + F.esc(a.error) + '</span>';
    else if (a && a.installed === false) status = '<span class="st-absent">○ 未部署</span>';
    else status = '<span class="st-ok">● ok</span>';
    return '<div class="agent-row"><span class="k">' + F.esc(type) + '</span>' + status + '</div>';
  };

  F.machineCard = function (r) {
    const dot = r.online ? 'on' : 'off';
    let inner = '<h2><span class="dot ' + dot + '"></span><a href="/machine/' +
      encodeURIComponent(r.machine) + '">' + F.esc(r.machine) + '</a>';
    if (r.has_hermes) inner += ' <span class="badge-hermes">Hermes ' + F.esc(r.hermes_state || '') + '</span>';
    inner += '</h2><div class="meta">' + F.esc(r.desc || '') + '</div>';
    if (!r.online) {
      inner += '<div class="err">⚠️ ' + F.esc(r.error || 'offline') + '</div>';
    } else {
      if (r.agent_summaries && r.agent_summaries.length) {
        inner += '<div class="meta">📦 agent 连接器</div>';
        r.agent_summaries.forEach(function (a) {
          let s;
          if (a.status === 'ok') s = '<span class="st-ok">● ok</span>';
          else if (a.status === 'absent') s = '<span class="st-absent">○ 未部署</span>';
          else s = '<span class="st-err">● ' + F.esc(a.detail || 'error') + '</span>';
          inner += '<div class="agent-row"><span class="k">' + F.esc(a.type) + '</span>' + s + '</div>';
        });
      }
      const sys = r.system || {};
      const pct = diskPct(sys);
      const barCls = pct > 90 ? 'danger' : pct > 70 ? 'warn' : '';
      inner += '<div class="stat">' +
        '<span>📦 agent: <b>' + F.esc(r.agent_count || 0) + '</b></span>' +
        '<span>📈 负载: <b>' + F.esc(sys.load || 0) + '</b></span>' +
        '<span>💾 磁盘: <b>' + pct + '%</b></span>' +
        '<span>⏱️ 运行: <b>' + F.esc(sys.uptime || '?') + '</b></span></div>' +
        '<div class="bar"><i class="' + barCls + '" style="width:' + pct + '%"></i></div>';
    }
    return '<div class="card" data-machine="' + F.esc(r.machine) + '">' + inner + '</div>';
  };

  F.eventItem = function (e) {
    let cls = '';
    if (e.changes && e.changes.indexOf('reachable') >= 0) cls = e.online === false ? 'bad' : 'ok';
    const label = e.machine || e.event || 'event';
    const detail = (e.changes && e.changes.length) ? ' · ' + e.changes.join(',') : '';
    return '<div class="event-item ' + cls + '"><div>' + F.esc(label) + F.esc(detail) +
      '</div><div class="t">' + F.fmtTime(e.ts) + '</div></div>';
  };

  F.timeline = function (container, history) {
    if (!container) return;
    if (!history || !history.length) {
      container.innerHTML = '<div class="meta">暂无历史数据</div>';
      return;
    }
    let html = '<div class="timeline">';
    history.forEach(function (h) {
      html += '<i class="' + (h.reachable ? '' : 'down') + '" title="' +
        F.esc(F.fmtTime(h.ts)) + '"></i>';
    });
    html += '</div><div class="timeline-legend"><span>' +
      F.esc(F.fmtTime(history[0].ts)) + '</span><span>现在</span></div>';
    container.innerHTML = html;
  };

  F.flash = function (el) {
    if (!el) return;
    el.classList.remove('flash');
    void el.offsetWidth; /* 重启动画 */
    el.classList.add('flash');
  };

  F.logAppend = function (el, line, maxLines) {
    if (!el) return;
    const div = document.createElement('div');
    div.textContent = line; /* textContent：防日志注入 */
    el.appendChild(div);
    const cap = maxLines || 500;
    while (el.children.length > cap) el.removeChild(el.firstChild);
  };
})(window.FleetApp);
```

- [ ] **Step 3: 写 hub/static/app.js**

```javascript
/* app.js — SSE 客户端 + 轮询降级。事件：machine_update / fleet_event / task_update / task_log。 */
(function (F) {
  const page = document.body.getAttribute('data-page') || '';
  const dot = document.getElementById('sse-dot');
  let es = null;
  let pollTimer = null;
  let lastTs = 0;

  function setLive(on) { if (dot) dot.classList.toggle('live', !!on); }

  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(function () { refreshAll().catch(function () {}); }, 10000);
  }
  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  function refreshAll() {
    if (page === 'fleet') {
      return fetch('/api/status', { cache: 'no-store' }).then(function (r) { return r.json(); })
        .then(function (data) { renderFleet(data.machines || []); });
    }
    if (page === 'machine') {
      const name = document.body.getAttribute('data-machine');
      return fetch('/api/machines/' + encodeURIComponent(name), { cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(function (d) { if (d.ok) renderMachineDetail(d); });
    }
    return Promise.resolve();
  }

  function renderFleet(machines) {
    const grid = document.getElementById('machine-grid');
    if (grid) grid.innerHTML = machines.map(F.machineCard).join('');
    const online = machines.filter(function (m) { return m.online; }).length;
    const alerts = machines.filter(function (m) { return !m.online; }).length;
    const hbOn = document.getElementById('hb-online');
    const hbAlert = document.getElementById('hb-alerts');
    const hbTotal = document.getElementById('hb-total');
    if (hbOn) hbOn.textContent = online;
    if (hbAlert) hbAlert.textContent = alerts;
    if (hbTotal) hbTotal.textContent = machines.length;
  }

  function renderMachineDetail(d) {
    const c = d.current || {};
    const reach = document.getElementById('machine-reach');
    if (reach) {
      reach.textContent = c.reachable ? '在线' : ('离线 — ' + (c.remote_error || ''));
      reach.className = c.reachable ? 'st-ok' : 'st-err';
    }
    F.timeline(document.getElementById('uptime-timeline'), d.history);
  }

  function pushEvent(e) {
    const stream = document.getElementById('event-stream');
    if (!stream) return;
    const div = document.createElement('div');
    div.innerHTML = F.eventItem(e);
    const item = div.firstChild;
    stream.insertBefore(item, stream.children[1] || null);
    while (stream.children.length > 51) stream.removeChild(stream.lastChild);
  }

  function connect() {
    if (es) es.close();
    const url = '/api/stream' + (lastTs ? '?since=' + lastTs : '');
    es = new EventSource(url);
    es.onopen = function () { setLive(true); stopPolling(); };
    es.onerror = function () { setLive(false); startPolling(); };
    es.addEventListener('machine_update', function (e) {
      const d = JSON.parse(e.data);
      lastTs = Math.max(lastTs, d.ts || 0);
      if (page === 'fleet') {
        refreshAll().then(function () {
          F.flash(document.querySelector('[data-machine="' + CSS.escape(d.machine) + '"]'));
        });
        pushEvent({ machine: d.machine, changes: d.changes, ts: d.ts, online: d.online });
      } else if (page === 'machine' && d.machine === document.body.getAttribute('data-machine')) {
        refreshAll();
      }
    });
    es.addEventListener('fleet_event', function (e) {
      const d = JSON.parse(e.data);
      lastTs = Math.max(lastTs, d.ts || 0);
      pushEvent(d);
    });
    /* task_update / task_log 由 Phase 2/3 页面消费；此处预注册不报错 */
  }

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && (!es || es.readyState === 2)) connect();
  });
  connect();
  F.refreshAll = refreshAll; /* 供调试与降级轮询复用 */
})(window.FleetApp);
```

- [ ] **Step 4: 提交**

```bash
git add hub/static/style.css hub/static/components.js hub/static/app.js
git commit -m "feat: 前端静态资源 — 深色主题样式 + 渲染纯函数 + SSE 客户端"
```

---

### Task 7: 模板三视图 + web.py 页面路由切换

**Files:**
- Create: `hub/templates/base.html`、`hub/templates/fleet.html`、`hub/templates/machine.html`
- Modify: `hub/web.py`（删 PAGE 常量与 `render_template_string`，页面改用 `render_template`，加 `/machine/<name>`）
- Test: `tests/test_observability.py`（追加页面冒烟测试）

**Interfaces:**
- Consumes: `routes_observe.build_summary()`、`store.read_current/read_history`、Task 6 静态资源。
- Produces:
  - `GET /` → `fleet.html`（SSR 机器网格 + 健康条 + 空事件流容器，JS 接管）
  - `GET /machine/<name>` → `machine.html`（SSR 系统指标 + agent 表 + 时间线容器）；未知机器 404 文本页；非法名 400。
  - 两个页面 `<body data-page="fleet|machine">`，machine 页带 `data-machine="<name>"`。

- [ ] **Step 1: 写失败测试**

```python
class PageViewTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.old_state_dir = store.STATE_DIR
        self.old_event_log = events.EVENT_LOG
        store.STATE_DIR = self.temp_dir
        events.EVENT_LOG = self.temp_dir / "events.jsonl"
        from hub import web
        self.client = web.make_app(ingest_token="secret").test_client()

    def tearDown(self):
        store.STATE_DIR = self.old_state_dir
        events.EVENT_LOG = self.old_event_log

    def test_fleet_page_uses_template_and_static_assets(self):
        store.save_snapshot("hk", {"machine": "hk", "source": "ingest",
                                   "reachable": True, "agents": {}, "system": {}})
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('data-page="fleet"', html)
        self.assertIn('static/style.css', html)
        self.assertIn('static/app.js', html)
        self.assertIn('data-machine="hk"', html)
        self.assertNotIn("render_template_string", html)

    def test_machine_page(self):
        store.save_snapshot("hk", {"machine": "hk", "source": "ingest",
                                   "reachable": True,
                                   "agents": {"codex": {"installed": True, "active_count": 1}},
                                   "system": {"platform": "linux", "load": "0.5"}})
        resp = self.client.get("/machine/hk")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('data-page="machine"', html)
        self.assertIn('data-machine="hk"', html)
        self.assertIn("codex", html)
        self.assertEqual(self.client.get("/machine/ghost").status_code, 404)
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_observability.PageViewTests -v`
Expected: FAIL — `data-page="fleet"` 不在旧内联模板里

- [ ] **Step 3: 写 hub/templates/base.html**

```html
<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{% block title %}agent-fleet{% endblock %}</title>
<link rel="stylesheet" href="{{ url_for('static', filename='style.css') }}">
</head>
<body data-page="{% block page_id %}{% endblock %}"{% block body_attrs %}{% endblock %}>
<header class="topbar">
  <a class="brand" href="/">🛰️ agent-fleet</a>
  <span class="sse-dot" id="sse-dot" title="实时连接状态"></span>
  <nav><a href="/api/status">API</a></nav>
</header>
<main>{% block content %}{% endblock %}</main>
<script src="{{ url_for('static', filename='components.js') }}"></script>
<script src="{{ url_for('static', filename='app.js') }}"></script>
{% block scripts %}{% endblock %}
</body>
</html>
```

- [ ] **Step 4: 写 hub/templates/fleet.html**

SSR 直接复用 `components.js` 同款结构（Jinja 渲染首屏，JS 接管增量更新）：

```html
{% extends "base.html" %}
{% block title %}agent-fleet 总览{% endblock %}
{% block page_id %}fleet{% endblock %}
{% block content %}
<div class="fleet-layout">
  <section>
    <div class="healthbar">
      <span class="hb-ok">在线<b id="hb-online">{{ rows | selectattr('online') | list | length }}</b></span>
      <span class="hb-bad">告警<b id="hb-alerts">{{ rows | rejectattr('online') | list | length }}</b></span>
      <span>机器<b id="hb-total">{{ rows | length }}</b></span>
      <span>任务<b id="hb-tasks">0</b></span>
    </div>
    <div class="grid" id="machine-grid">
      {% for r in rows %}
      <div class="card" data-machine="{{ r.machine }}">
        <h2>
          <span class="dot {{ 'on' if r.online else 'off' }}"></span>
          <a href="/machine/{{ r.machine }}">{{ r.machine }}</a>
          {% if r.has_hermes %}<span class="badge-hermes">Hermes {{ r.hermes_state }}</span>{% endif %}
        </h2>
        <div class="meta">{{ r.desc }}</div>
        {% if not r.online %}
          <div class="err">⚠️ {{ r.error }}</div>
        {% else %}
          {% if r.agent_summaries %}
            <div class="meta">📦 agent 连接器</div>
            {% for a in r.agent_summaries %}
              <div class="agent-row"><span class="k">{{ a.type }}</span>
                {% if a.status == 'ok' %}<span class="st-ok">● ok</span>
                {% elif a.status == 'absent' %}<span class="st-absent">○ 未部署</span>
                {% else %}<span class="st-err">● {{ a.detail }}</span>{% endif %}
              </div>
            {% endfor %}
          {% endif %}
          <div class="stat">
            <span>📦 agent: <b>{{ r.agent_count }}</b></span>
            <span>📈 负载: <b>{{ r.system.get('load', 0) }}</b></span>
            <span>💾 磁盘: <b>{{ r.system.get('disk_used_pct', 0) }}</b></span>
            <span>⏱️ 运行: <b>{{ r.system.get('uptime', '?') }}</b></span>
          </div>
        {% endif %}
      </div>
      {% endfor %}
    </div>
  </section>
  <aside class="event-stream" id="event-stream">
    <h3>实时事件</h3>
    {% for e in events %}
    <div class="event-item"><div>{{ e.machine or e.event }}</div><div class="t">{{ e.ts }}</div></div>
    {% endfor %}
  </aside>
</div>
{% endblock %}
```

- [ ] **Step 5: 写 hub/templates/machine.html**

```html
{% extends "base.html" %}
{% block title %}{{ name }} · agent-fleet{% endblock %}
{% block page_id %}machine{% endblock %}
{% block body_attrs %} data-machine="{{ name }}"{% endblock %}
{% block content %}
<h1 style="font-size:20px;margin:0 0 4px;">
  <span class="dot {{ 'on' if reachable else 'off' }}"></span> {{ name }}
</h1>
<div class="meta">{{ desc }} · <span id="machine-reach" class="{{ 'st-ok' if reachable else 'st-err' }}">{{ '在线' if reachable else '离线 — ' ~ (error or '') }}</span></div>
<div class="detail-grid">
  <div class="panel">
    <h3>系统指标</h3>
    <div class="metric"><span>平台</span><b>{{ system.get('platform', '?') }}</b></div>
    <div class="metric"><span>负载</span><b>{{ system.get('load', '?') }}</b></div>
    <div class="metric"><span>内存</span><b>{{ system.get('mem_used_mb', '?') }} / {{ system.get('mem_total_mb', '?') }} MB</b></div>
    <div class="metric"><span>磁盘</span><b>{{ system.get('disk_used_pct', '?') }}</b></div>
    <div class="metric"><span>运行时间</span><b>{{ system.get('uptime', '?') }}</b></div>
  </div>
  <div class="panel">
    <h3>Agent 状态</h3>
    <table class="agent-table">
      <tr><th>类型</th><th>状态</th><th>会话</th><th>进程</th></tr>
      {% for atype, a in agents.items() %}
      <tr>
        <td>{{ atype }}</td>
        <td>{% if a.error %}<span class="st-err">{{ a.error }}</span>
            {% elif a.installed is sameas false %}<span class="st-absent">未部署</span>
            {% elif a.gateway_state %}{{ a.gateway_state }}{% else %}<span class="st-ok">ok</span>{% endif %}</td>
        <td>{{ a.session_count if a.session_count is defined else '—' }}</td>
        <td>{{ a.process_count if a.process_count is defined else '—' }}</td>
      </tr>
      {% endfor %}
    </table>
  </div>
</div>
<div class="panel">
  <h3>24h 在线时间线</h3>
  <div id="uptime-timeline"></div>
</div>
{% endblock %}
{% block scripts %}
<script>
fetch('/api/machines/' + encodeURIComponent(document.body.getAttribute('data-machine')))
  .then(function (r) { return r.json(); })
  .then(function (d) { if (d.ok) FleetApp.timeline(document.getElementById('uptime-timeline'), d.history); });
</script>
{% endblock %}
```

- [ ] **Step 6: web.py 页面路由切换**

`hub/web.py`：
1. 删除整个 `PAGE = """..."""` 常量；import 中 `render_template_string` 改为 `from flask import Flask, jsonify, render_template, request`（`jsonify/request` 若不再用则一并清理）。
2. `make_app` 的页面路由改为：

```python
    @app.route("/")
    def index():
        from hub.routes_observe import build_summary
        from hub import events as ev
        rows = build_summary()
        recent = [{"machine": e.get("machine"), "event": e.get("event"),
                   "ts": time.strftime("%H:%M:%S", time.localtime(e.get("ts") or 0))}
                  for e in ev.read_recent(20)]
        return render_template("fleet.html", rows=rows, events=recent)

    @app.route("/machine/<name>")
    def machine_view(name):
        from hub import auth as _auth
        from hub import state as st
        from report_schema import sanitize_agents, sanitize_system
        if not _auth.MACHINE_RE.fullmatch(name):
            return "invalid machine name", 400
        current = st.read_current(name)
        if not current:
            return "machine not found", 404
        desc = next((h["desc"] for h in load_hosts() if h["name"] == name), "自报告 (ingest)")
        return render_template(
            "machine.html",
            name=name,
            desc=desc,
            reachable=bool(current.get("reachable", True)),
            error=current.get("remote_error"),
            agents=sanitize_agents(current.get("agents", {})),
            system=sanitize_system(current.get("system", {})),
        )
```

3. 文件头 docstring 路由清单更新（`GET /machine/<name>`、蓝图 API 列表）。

- [ ] **Step 7: 运行全部测试 + 语法检查**

Run: `.venv/bin/python -m unittest discover -s tests -v && python3 -m compileall -q connectors hub tools tests`
Expected: 全绿 + 无编译错误

- [ ] **Step 8: 提交**

```bash
git add hub/templates/ hub/web.py tests/test_observability.py
git commit -m "feat: 前端三视图模板化 — base/fleet/machine + 静态资源接管实时更新"
```

---

### Task 8: Phase 1 手动冒烟 + 文档收尾

**Files:**
- Modify: `README.md`（Web API 小节加新路由）、`docs/HANDOFF.md`（架构部件表更新）

- [ ] **Step 1: 本地冒烟**

```bash
AGENT_FLEET_INGEST_TOKEN='dev-smoke-token' .venv/bin/python hub/web.py --port 8791 &
sleep 1
curl -s -X POST http://127.0.0.1:8791/api/ingest \
  -H 'X-Agent-Fleet-Token: dev-smoke-token' -H 'Content-Type: application/json' \
  -d '{"machine":"smoke-test","agents":{"codex":{"installed":true,"active_count":1}},"system":{"platform":"darwin","load":"1.0","disk_used_pct":"40%"}}'
# Expected: {"ok": true, ...}
curl -s http://127.0.0.1:8791/api/status | python3 -m json.tool | head -30
curl -s http://127.0.0.1:8791/api/machines/smoke-test | python3 -m json.tool | head -20
curl -sN --max-time 3 http://127.0.0.1:8791/api/stream | head -5
# Expected: ": connected" 行；再次 POST ingest 时能看到 event: machine_update
open http://127.0.0.1:8791/          # 总览页：健康条 + 卡片 + 事件流
open http://127.0.0.1:8791/machine/smoke-test
kill %1
```

逐条核对预期；浏览器开发者工具 Network 面板确认 `/api/stream` 保持连接、EventSource 无报错。

- [ ] **Step 2: 更新 README.md Web API 小节**

把「Web API」列表替换为：

```markdown
- `GET /`：Fleet 总览（机器网格 + 实时事件流）
- `GET /machine/<name>`：机器详情（系统指标 + agent 表 + 24h 在线时间线）
- `GET /api/status`：当前机器状态
- `GET /api/machines/<name>`：单机详情 + 历史
- `GET /api/events`：最近事件摘要
- `GET /api/stream`：SSE 实时事件流（断线自动重连并补发）
- `POST /api/ingest`：机器主动上报，必须带 `X-Agent-Fleet-Token`
- `POST /api/scan`：仅执行 stale reconciliation，必须带 token；不执行机器采集
```

- [ ] **Step 3: 更新 docs/HANDOFF.md 架构部件表**

把「当前架构」表替换为：

```markdown
| 部件 | 位置 | 职责 |
|---|---|---|
| hub/web.py | HK 容器 | app 工厂、页面路由（fleet/machine 视图）、启动入口 |
| hub/routes_observe.py | HK 容器 | `/api/ingest`、`/api/status`、`/api/machines/<name>`、`/api/events`、SSE `/api/stream` |
| hub/auth.py | HK 容器 | 认证域装饰器（ingest token） |
| hub/state.py | HK 容器 | JSONL 历史（>5MB 轮转）+ 原子 current 快照 |
| hub/events.py | HK 容器 | 事件总线 + SSE 队列桥 |
| agent-self-report.py | 每台 agent 机器 | 本地采集并主动 POST |
| probe_collectors.py | 每台 agent 机器 | Hermes/Claude/Codex/generic 本地采集 |
| state/ | HK hub | JSONL 历史 + 原子 current 快照 + events.jsonl |
| hosts.yaml | HK hub | 展示描述和 stale TTL，不是机器注册门槛 |
```

「未实现能力」小节中「WebSocket：未实现，当前页面 fetch 轮询」改为「WebSocket：未实现；实时性由 SSE（`/api/stream`）提供，断线降级 10s 轮询」。

- [ ] **Step 4: 最终回归 + 提交**

Run: `.venv/bin/python -m unittest discover -s tests -v && python3 -m compileall -q connectors hub tools tests`
Expected: 全绿

```bash
git add README.md docs/HANDOFF.md
git commit -m "docs: Phase 1 观测加固收尾 — 新路由与 SSE 文档"
```

---

## Self-Review 记录

- **Spec 覆盖**：§4.1 observe 蓝图 ✅（Task 4）、SSE ✅（Task 5，spec §5.3 的 task_* 事件类型在 Phase 2 计划扩展映射）、§4.4/§6 前端三视图 ✅（Task 6-7；task 视图属 Phase 2）、JSONL 轮转 ✅（Task 1，spec §7.3）、scan filter ✅（Task 2）、SSE 断线降级轮询 ✅（Task 6，spec §7.5）。`?since=` 补发 ✅（Task 5，spec §6.3）。
- **兼容性**：既有测试 patch 的 `store.STATE_DIR`/`events.EVENT_LOG`/`web.STATE_DIR` 均保留语义 ✅；`web.make_app(ingest_token=...)` 签名不变 ✅；`/api/ingest`/`/api/scan`/`/api/status` 行为逐字保持 ✅。
- **安全**：公共路由（status/machines/events/stream）全部经白名单脱敏；SSE 载荷二次 sanitize ✅；前端全部 `esc()`/`textContent` ✅；机器名正则双端校验 ✅。
