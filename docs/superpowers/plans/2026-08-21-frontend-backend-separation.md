# agent-fleet 前后端分离与架构隔离实施计划

> **Status (2026-09-08): historical / implemented.** Open checkboxes below are the original plan and were not kept in sync after merge. Do not re-implement from unchecked boxes. Remaining work is recorded in `docs/HANDOFF.md` §六 as `done` or `deferred-with-condition`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 agent-fleet 迁移为可独立发布的静态前端和 backend API，同时保持同源反向代理、旧 `/api/*`、push-only、三认证域、观测链路和 runner E2E 兼容。

**Architecture:** 采用四阶段渐进迁移。第一阶段建立显式配置、正式 package、repository 和 app-instance event publisher；第二阶段把 observe、task、runner 业务逻辑移入不依赖 Flask 的 application service；第三阶段建立不依赖 Flask 模板的原生 JavaScript frontend；第四阶段增加 `/api/v1` adapters、静态/backend release 产物、部署路由和回滚验证。旧 HTTP paths、JSONL/SQLite 格式、probe 和 runner 协议在迁移期间继续由 compatibility adapters 支持。

**Tech Stack:** Python 3 标准库、Flask、PyYAML、sqlite3、unittest、原生 JavaScript ES modules、EventSource、Nginx/Cloudflare Access 文档；不引入 Node/npm/webpack/Vite 或新的 Python 运行时依赖。

**Spec:** `docs/superpowers/specs/2026-08-21-frontend-backend-separation-design.md`

## Global Constraints

- 第一阶段生产入口使用同源反向代理：`/` 和 `/assets/*` 指向静态 frontend release，`/api/*` 指向 backend API release。
- 不启用宽泛 CORS；不得使用 `Access-Control-Allow-Origin: *` 与 credentials 组合。
- agent probe 主动 POST observation；runner 主动 poll、heartbeat、result；hub 不通过 SSH、反向隧道或中央私钥连接 agent；浏览器不直接连接 agent。
- observe、operator、runner 三个认证域互不复用：ingest 使用 `X-Agent-Fleet-Token`，operator 使用 Cloudflare Access identity，runner 使用 machine-bound `X-Runner-Credential`。
- token、密码、runner credential、私钥、Access header、session 内容、prompt、命令行、文件路径和原始 collector output 不进入 frontend release、测试 fixture、文档或 git。
- 保持现有 adapter allowlist、project whitelist、attempt fencing、nonce、lease TTL、结果幂等和有界日志/diff 约束。
- 保持现有兼容路径：`/api/status`、`/api/machines/<name>`、`/api/events`、`/api/stream`、`/api/ingest`、`/api/scan`、`/api/tasks*`、`/api/commands/*`。
- 旧路径和未来 `/api/v1` 必须调用同一 application service；不得复制业务规则或在两个 route 中分别实现状态迁移。
- observation storage 故障或 task storage 故障必须隔离：任务不可用时观测继续；事件持久化失败不阻塞 ingest 或 task completion。
- repository 使用显式构造参数接收路径；测试不得依赖仓库真实 `state/`，不得用模块全局路径 patch 作为新代码的主要配置机制。
- `/api/stream` 必须保持 `text/event-stream`、`Cache-Control: no-cache`、`X-Accel-Buffering: no`、keepalive 和 `since` replay 语义；SSE 队列有界且队列满时丢弃实时事件、不阻塞主流程。
- frontend 使用原生 JavaScript；视图不得直接调用 `fetch` 或 `EventSource`；动态文本优先使用 `textContent`/DOM API，不能引入第二套业务渲染实现。
- 每个任务结束后运行 focused tests；每个 phase 结束至少运行：

```bash
.venv/bin/python -m unittest discover -s tests -v
python3 -m compileall -q connectors hub tools tests
bash deploy/e2e-smoke.sh
```

- `.claude/` 和 `.playwright-mcp/` 是预先存在的无关目录，不删除、不修改、不提交。
- 不执行外部发布、Cloudflare 变更、Nginx 变更或生产部署；部署任务只修改仓库代码和文档，并提供本地验收命令。

## 文件结构

| 文件 | 动作 | 职责 |
|---|---|---|
| `hub/__init__.py` | Create | 正式 package marker 和公开最小导入面 |
| `hub/config.py` | Create | 路径、hosts、认证和运行配置的显式对象 |
| `hub/bootstrap.py` | Create | repository、publisher、service 和 Flask app 组装 |
| `hub/repositories.py` | Create | repository Protocol 和公共 DTO/错误边界 |
| `hub/infrastructure/state_repository.py` | Create | 现有 JSONL/current snapshot 的构造注入实现 |
| `hub/infrastructure/event_repository.py` | Create | 事件 JSONL append/recent/replay 的实现 |
| `hub/infrastructure/task_repository.py` | Create | 现有 SQLite task_store 的 repository facade |
| `hub/domain/events.py` | Create | domain event 类型和 public event 映射规则 |
| `hub/domain/task.py` | Create | task state、边界值和状态迁移规则 |
| `hub/application/observe_service.py` | Create | observation ingest、summary、detail 和 reconciliation 用例 |
| `hub/application/task_service.py` | Create | task create/list/get/cancel/retry 用例 |
| `hub/application/runner_service.py` | Create | runner poll/heartbeat/result 用例 |
| `hub/application/event_publisher.py` | Create | app-instance publisher、bounded subscribers 和 replay |
| `hub/http/errors.py` | Create | application error 到统一 JSON error 的转换 |
| `hub/http/observe_routes.py` | Create/Modify | observe HTTP adapter 和 SSE transport |
| `hub/http/task_routes.py` | Create/Modify | operator task HTTP adapter |
| `hub/http/command_routes.py` | Create/Modify | runner HTTP adapter |
| `hub/http/pages.py` | Create/Modify | 迁移期 compatibility page shell |
| `hub/auth.py` | Modify | header identity extraction 与 domain authorization 边界 |
| `hub/web.py` | Modify | 兼容启动入口，委托 `bootstrap.create_app()` |
| `hub/routes_observe.py` | Modify/Delete after migration | 旧 observe blueprint compatibility adapter |
| `hub/routes_tasks.py` | Modify/Delete after migration | 旧 task blueprint compatibility adapter |
| `hub/routes_commands.py` | Modify/Delete after migration | 旧 runner blueprint compatibility adapter |
| `hub/state.py` | Modify | 保留旧 API 的 repository-backed compatibility facade |
| `hub/events.py` | Modify | 保留旧 emit/subscribe API 的 compatibility facade |
| `hub/task_store.py` | Modify | 保留旧函数签名，委托 `TaskRepository` implementation |
| `frontend/index.html` | Create | 静态入口和无秘密 runtime config 引用 |
| `frontend/config.js` | Create | `apiBaseUrl`/`pageBaseUrl` 非秘密运行时配置 |
| `frontend/api/contracts.js` | Create | public DTO 和 SSE event 校验 |
| `frontend/api/client.js` | Create | 唯一 fetch/API client 和 `ApiError` |
| `frontend/realtime/sse.js` | Create | 唯一 EventSource 生命周期和 reconnect |
| `frontend/state/store.js` | Create | 页面模型、事件游标、连接状态和通知 |
| `frontend/views/fleet.js` | Create | Fleet view 和 machine card 安全渲染 |
| `frontend/views/machine.js` | Create | machine detail、task list 和 create flow |
| `frontend/views/task.js` | Create | task detail、终态刷新和日志渲染 |
| `frontend/styles/app.css` | Create | 从现有样式迁移的静态 frontend CSS |
| `frontend/routes.js` | Create | page/API URL 构造和 path segment 编码 |
| `tests/test_config.py` | Create | 配置和路径注入测试 |
| `tests/test_repositories.py` | Create | repository adapter、rotation、WAL、隔离测试 |
| `tests/test_services.py` | Create | 不创建 Flask context 的 application service 测试 |
| `tests/test_http_contracts.py` | Create | 兼容路径、错误和 `/api/v1` adapter 测试 |
| `tests/test_auth_matrix.py` | Create | 三认证域互斥测试 |
| `tests/test_event_publisher.py` | Create | publisher、SSE lifecycle、replay 和失败隔离测试 |
| `tests/test_frontend_contracts.py` | Create | frontend source/runtime contract tests |
| `tests/test_release_layout.py` | Create | 静态 release、无秘密、backend 独立运行测试 |
| `deploy/package-frontend-release.sh` | Create | 无构建链静态 frontend release 打包 |
| `deploy/frontend-release-layout.md` | Create | release layout、缓存、回滚和同源路由说明 |
| `deploy/nginx-expose.md` | Modify | 静态 frontend/backend 路由和 SSE 代理配置 |
| `deploy/cloudflare-access.md` | Modify | 分离后的路径/Access/BYPASS 矩阵 |
| `README.md` | Modify | 新部署拓扑和兼容 API 说明 |
| `docs/HANDOFF.md` | Modify | 迁移阶段、回滚和生产操作边界 |

## 依赖关系

- Task 1 是 Phase 1 的基础；Task 2、3、4、5 依赖 Task 1。
- Task 6 依赖 Task 2 和 Task 3；Task 7 依赖 Task 4；Task 8 依赖 Task 5、6、7。
- Phase 2 的 Task 9、10、11 依赖 Phase 1 完成；Task 12 依赖 Task 9、10、11。
- Phase 3 的 Task 13 依赖 Task 12 的 public DTO 和稳定旧 API；Task 14、15、16 依赖 Task 13；Task 17 依赖 Task 14、15、16。
- Phase 4 的 Task 18 依赖 Task 12 和 Task 17；Task 19 依赖 Task 18；Task 20 依赖 Task 19；Task 21 是全分支验收。

---

# Phase 1：边界基础

### Task 1: 正式 package、配置对象和 bootstrap 骨架

**Files:**
- Create: `hub/__init__.py`, `hub/config.py`, `hub/bootstrap.py`
- Modify: `hub/web.py`, `tools/agent-self-report.py`, `tools/agent-runner.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces `FleetConfig.from_root(root: Path, *, ingest_token=None, dev_operator=None, runner_credentials=None, project_whitelist=None, tasks_enabled=True) -> FleetConfig`。
- Produces `FleetConfig.root`, `.state_dir`, `.hosts_file`, `.event_log`, `.task_db`, `.ingest_token`, `.dev_operator`, `.runner_credentials`, `.project_whitelist`。
- Produces `create_app(config: FleetConfig, *, repositories=None, publisher=None) -> Flask`。
- Preserves `web.make_app(...)` as a compatibility wrapper that constructs `FleetConfig` and calls `create_app`。

- [ ] **Step 1: Write failing config/import tests**

```python
from pathlib import Path
import tempfile
import unittest

from hub.config import FleetConfig


class FleetConfigTests(unittest.TestCase):
    def test_paths_are_derived_once_from_root(self):
        root = Path(tempfile.mkdtemp())
        cfg = FleetConfig.from_root(root)
        self.assertEqual(cfg.state_dir, root / "state")
        self.assertEqual(cfg.hosts_file, root / "hosts.yaml")
        self.assertEqual(cfg.event_log, root / "state" / "events.jsonl")
        self.assertEqual(cfg.task_db, root / "state" / "fleet.db")

    def test_config_accepts_explicit_test_paths(self):
        root = Path(tempfile.mkdtemp())
        cfg = FleetConfig.from_root(root, state_dir=root / "tmp-state")
        self.assertEqual(cfg.state_dir, root / "tmp-state")

    def test_bootstrap_import_does_not_mutate_sys_path(self):
        import sys
        before = list(sys.path)
        import hub.bootstrap
        self.assertEqual(sys.path, before)
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `.venv/bin/python -m unittest tests.test_config -v`  
Expected: FAIL because `hub.config` and `FleetConfig` do not exist.

- [ ] **Step 3: Implement explicit configuration and compatibility bootstrap**

Use a dataclass with `root: Path` and optional path overrides. `from_root()` must resolve paths, but must not read credentials or create directories until a repository/bootstrap needs them. Move `FLEET_HOME`, `STATE_DIR`, `HOSTS_FILE`, `EVENT_LOG`, and `DB_PATH` resolution to the configuration/bootstrap boundary. Keep direct script entrypoints working without `sys.path.insert`; invoke them as package/module imports from repository root or use a single documented launcher path, and leave no new path mutation in `hub` modules.

`web.make_app` must preserve its current keyword arguments and public production behavior: `require_token=True` without a resolved ingest token still raises `RuntimeError`; `TASKS_ENABLED=False` still leaves observation routes usable. `create_app` must attach the config and service objects to `app.extensions["fleet"]` for route adapters and tests.

- [ ] **Step 4: Run focused and compatibility tests**

Run: `.venv/bin/python -m unittest tests.test_config tests.test_push_only.LocalProbeTests tests.test_push_only.ReconciliationTests tests.test_push_only.SelfReportContractTests -v`  
Expected: PASS; no test may require a repository credential or real network.

- [ ] **Step 5: Commit**

```bash
git add hub/__init__.py hub/config.py hub/bootstrap.py hub/web.py tools/agent-self-report.py tools/agent-runner.py tests/test_config.py
git commit -m "refactor: establish explicit hub configuration boundary"
```

### Task 2: Repository protocols and observation repository adapter

**Files:**
- Create: `hub/repositories.py`, `hub/infrastructure/__init__.py`, `hub/infrastructure/state_repository.py`
- Modify: `hub/state.py`, `hub/scan.py`
- Test: `tests/test_repositories.py`

**Interfaces:**
- Produces `ObservationRepository` Protocol with `save_snapshot(machine: str, snapshot: dict) -> None`, `read_current(machine: str) -> dict | None`, `read_history(machine: str, limit: int = 50) -> list[dict]`, `machines() -> set[str]`.
- Produces `JsonlObservationRepository(state_dir: Path, *, max_jsonl_bytes=5*1024*1024, keep_lines=2000)`.
- Produces `LegacyStateStoreAdapter(repository: ObservationRepository)` only if the old module facade needs an explicit adapter; old `state.save_snapshot/read_current/read_history` signatures remain functional.

- [ ] **Step 1: Add repository contract tests**

```python
class ObservationRepositoryTests(unittest.TestCase):
    def test_repository_uses_injected_directory(self):
        root = Path(tempfile.mkdtemp())
        repo = JsonlObservationRepository(root)
        repo.save_snapshot("hk", {"machine": "hk", "reachable": True})
        self.assertTrue((root / "hk.jsonl").exists())
        self.assertTrue((root / "hk.json").exists())

    def test_current_snapshot_is_atomic_and_history_is_bounded(self):
        repo = JsonlObservationRepository(Path(tempfile.mkdtemp()), max_jsonl_bytes=512,
                                           keep_lines=3)
        for seq in range(20):
            repo.save_snapshot("hk", {"machine": "hk", "seq": seq})
        self.assertEqual(repo.read_current("hk")["seq"], 19)
        self.assertLessEqual(len(repo.read_history("hk", 100)), 20)
        for row in repo.read_history("hk", 100):
            self.assertIsInstance(row, dict)
```

- [ ] **Step 2: Run tests and verify failure**

Run: `.venv/bin/python -m unittest tests.test_repositories.ObservationRepositoryTests -v`  
Expected: FAIL because the repository interface and implementation do not exist.

- [ ] **Step 3: Extract the existing JSONL behavior without changing its wire semantics**

Move the current atomic current write, per-machine lock, JSONL rotation and history parsing into `JsonlObservationRepository`. The implementation must preserve `MAX_JSONL_BYTES=5*1024*1024`, `ROTATE_KEEP_LINES=2000`, `NamedTemporaryFile + flush + fsync + os.replace`, machine-name path safety and malformed-line skipping. `state.py` becomes a compatibility facade whose default repository is constructed from the legacy `STATE_DIR`; new services use the injected repository directly.

- [ ] **Step 4: Run observation regression tests**

Run: `.venv/bin/python -m unittest tests.test_repositories tests.test_observability.JsonlRotationTests tests.test_observability.ScanFilterTests tests.test_regressions.IngestScanTests -v`  
Expected: PASS, including concurrent current JSON validity and stale reconciliation.

- [ ] **Step 5: Commit**

```bash
git add hub/repositories.py hub/infrastructure/__init__.py hub/infrastructure/state_repository.py hub/state.py hub/scan.py tests/test_repositories.py
git commit -m "refactor: isolate observation repository"
```

### Task 3: Event repository and app-instance publisher

**Files:**
- Create: `hub/domain/__init__.py`, `hub/domain/events.py`, `hub/infrastructure/event_repository.py`, `hub/application/__init__.py`, `hub/application/event_publisher.py`
- Modify: `hub/events.py`, `hub/notifier.py`, `hub/bootstrap.py`
- Test: `tests/test_event_publisher.py`

**Interfaces:**
- Produces `EventRepository.append(event: dict) -> None`, `read_recent(limit: int = 50) -> list[dict]`, `read_since(ts: float, limit: int = 200) -> list[dict]`.
- Produces `JsonlEventRepository(path: Path, *, max_bytes=1_000_000, keep_lines=1000)`.
- Produces `EventPublisher(event_repository, *, queue_size=200)` with `emit(event_type, machine=None, changes=None, snapshot=None, **extra) -> dict`, `subscribe(callback) -> Callable[[], None]`, `subscribe_sse() -> tuple[queue.Queue, Callable[[], None]]`, `read_recent(limit)`, and `read_since(ts, limit)`.
- Preserves old `events.emit`, `events.subscribe`, `events.sse_subscribe`, `events.sse_unsubscribe`, `events.read_recent` through a compatibility publisher configured by bootstrap.

- [ ] **Step 1: Write lifecycle and failure-isolation tests**

```python
class EventPublisherTests(unittest.TestCase):
    def test_unsubscribe_stops_future_delivery(self):
        publisher = EventPublisher(FakeEventRepository())
        seen = []
        unsubscribe = publisher.subscribe(seen.append)
        publisher.emit("fleet_event", machine="hk")
        unsubscribe()
        publisher.emit("fleet_event", machine="tebi")
        self.assertEqual([e["machine"] for e in seen], ["hk"])

    def test_full_sse_queue_does_not_block_emit(self):
        publisher = EventPublisher(FakeEventRepository(), queue_size=1)
        q, unsubscribe = publisher.subscribe_sse()
        try:
            publisher.emit("one")
            publisher.emit("two")
            self.assertEqual(q.get(timeout=1)["event"], "one")
        finally:
            unsubscribe()

    def test_repository_failure_does_not_drop_live_delivery(self):
        repo = FakeEventRepository(append_error=OSError("disk unavailable"))
        publisher = EventPublisher(repo)
        seen = []
        unsubscribe = publisher.subscribe(seen.append)
        try:
            publisher.emit("state_changed", machine="hk")
        finally:
            unsubscribe()
        self.assertEqual(seen[0]["machine"], "hk")
```

- [ ] **Step 2: Run tests and verify failure**

Run: `.venv/bin/python -m unittest tests.test_event_publisher -v`  
Expected: FAIL because `EventPublisher` and `JsonlEventRepository` do not exist.

- [ ] **Step 3: Split persistence, publisher, and subscriber lifecycle**

Move event JSONL append/recent/rotation into `JsonlEventRepository`. `EventPublisher` must create events with the existing `event`, `machine`, `ts`, `changes`, and `extra` shape, persist best-effort, then distribute to snapshot copies of subscribers. `subscribe()` returns an idempotent unsubscribe closure. SSE registration stores queue/callback as one lifecycle object; queue overflow catches `queue.Full` and returns. Publisher instances must not share subscriber lists.

- [ ] **Step 4: Wire compatibility facade and notifier**

Make `hub/events.py` delegate to the publisher selected by bootstrap while retaining test patch compatibility for `EVENT_LOG`. Update notifier registration to subscribe to an explicit publisher, with a compatibility default only for the CLI path. Ensure event persistence errors are logged to stderr/logger without raising into ingest/task service.

- [ ] **Step 5: Run event and SSE regressions**

Run: `.venv/bin/python -m unittest tests.test_event_publisher tests.test_observability.SseStreamTests tests.test_observability.ObserveBlueprintTests -v`  
Expected: PASS; SSE receives events, replay remains cursor-based, and public payload contains no snapshot body.

- [ ] **Step 6: Commit**

```bash
git add hub/domain hub/infrastructure/event_repository.py hub/application/event_publisher.py hub/events.py hub/notifier.py hub/bootstrap.py tests/test_event_publisher.py
git commit -m "refactor: isolate app event publisher and persistence"
```

### Task 4: Task repository facade with explicit DB ownership

**Files:**
- Create: `hub/infrastructure/task_repository.py`
- Modify: `hub/repositories.py`, `hub/task_store.py`
- Test: `tests/test_repositories.py`, `tests/test_task_store.py`

**Interfaces:**
- Produces `SqliteTaskRepository(db_path: Path)` with the existing task operations: `init()`, `create_task(...)`, `get_task(task_id)`, `list_tasks(...)`, `lease_task(...)`, `heartbeat(...)`, `complete_task(...)`, `expire_leases(...)`, `expire_tasks(...)`, `cancel_task(...)`, `retry_task(...)`.
- Preserves old `hub.task_store.DB_PATH` and function signatures as a compatibility facade.

- [ ] **Step 1: Add explicit-path and isolation tests**

```python
class TaskRepositoryIsolationTests(unittest.TestCase):
    def test_two_repositories_use_different_databases(self):
        first = SqliteTaskRepository(Path(tempfile.mkdtemp()) / "one.db")
        second = SqliteTaskRepository(Path(tempfile.mkdtemp()) / "two.db")
        first.init()
        second.init()
        first.create_task(machine="hk", agent_type="codex", project="agent-fleet",
                           instruction="one", requested_by="op")
        self.assertEqual(second.list_tasks(), [])
```

- [ ] **Step 2: Run tests and verify failure**

Run: `.venv/bin/python -m unittest tests.test_repositories.TaskRepositoryIsolationTests -v`  
Expected: FAIL because `SqliteTaskRepository` does not exist.

- [ ] **Step 3: Wrap the existing SQLite implementation without altering semantics**

Move connection, WAL, transaction, schema, corruption quarantine, task TTL, lease fencing, result truncation and audit operations behind `SqliteTaskRepository(db_path)`. Every public operation must open and close its own connection. Keep exact constants and result shapes used by runner and task API. `task_store.py` delegates to a lazily configured default repository so existing tests that patch `DB_PATH` continue to pass during migration.

- [ ] **Step 4: Run the complete storage suite**

Run: `.venv/bin/python -m unittest tests.test_repositories tests.test_task_store -v`  
Expected: PASS, including concurrency, stale attempts, duplicate completion, cancellation and retry.

- [ ] **Step 5: Commit**

```bash
git add hub/infrastructure/task_repository.py hub/repositories.py hub/task_store.py tests/test_repositories.py tests/test_task_store.py
git commit -m "refactor: isolate sqlite task repository"
```

### Task 5: Domain task and public event models

**Files:**
- Create: `hub/domain/task.py`, `hub/domain/machine.py`
- Modify: `hub/report_schema.py` only if public DTO helpers must move; preserve import compatibility
- Test: `tests/test_services.py`

**Interfaces:**
- Produces immutable/public model helpers: `validate_task_input(...)`, `public_task(task, with_result=False)`, `public_result(result)`, `public_machine_summary(snapshot, host)`, `public_machine_detail(snapshot, history)`.
- Produces state predicates: `is_terminal_state(state)`, `can_cancel(state)`, `can_retry(state)`.
- Domain functions accept plain Python values and return plain dicts or raise defined domain errors; they do not import Flask, repositories, `request`, `g`, or `current_app`.

- [ ] **Step 1: Write domain tests**

```python
class TaskDomainTests(unittest.TestCase):
    def test_public_task_omits_internal_fields(self):
        task = {"task_id": "t-1", "attempt_id": "a", "client_token": "secret",
                "machine": "hk", "state": "queued", "instruction": "run"}
        public = public_task(task)
        self.assertNotIn("attempt_id", public)
        self.assertNotIn("client_token", public)

    def test_state_predicates_match_existing_api(self):
        self.assertTrue(can_cancel("running"))
        self.assertFalse(can_cancel("succeeded"))
        self.assertTrue(can_retry("failed"))
        self.assertFalse(can_retry("queued"))
```

- [ ] **Step 2: Run tests and verify failure**

Run: `.venv/bin/python -m unittest tests.test_services.TaskDomainTests -v`  
Expected: FAIL because domain model functions do not exist.

- [ ] **Step 3: Implement pure domain rules**

Move `_public_task`, `_public_result`, task-state predicates and public observation serialization rules out of Flask route modules. Preserve the current allowlists, task field names, result field names, truncation limits and sanitized machine/system fields. Domain errors must contain stable error codes and bounded human-readable details.

- [ ] **Step 4: Run domain and security regressions**

Run: `.venv/bin/python -m unittest tests.test_services.TaskDomainTests tests.test_frontend_xss tests.test_observability.ObserveBlueprintTests -v`  
Expected: PASS; no internal task or sensitive observation field appears in public output.

- [ ] **Step 5: Commit**

```bash
git add hub/domain/task.py hub/domain/machine.py hub/report_schema.py tests/test_services.py
git commit -m "refactor: define pure task and machine domain boundaries"
```

### Task 6: Observe application service

**Files:**
- Create: `hub/application/observe_service.py`
- Modify: `hub/scan.py`, `hub/routes_observe.py`, `hub/bootstrap.py`
- Test: `tests/test_services.py`

**Interfaces:**
- Produces `ObserveService(observation_repo, event_publisher, host_config, *, clock=time.time)`.
- Produces `ingest(payload: dict) -> dict`, `status() -> dict`, `machine_detail(name: str) -> dict`, `events(limit: int) -> dict`, `reconcile(machine: str | None = None) -> list[dict]`.
- Service methods do not create Flask responses or read request headers.

- [ ] **Step 1: Add service tests with fakes**

```python
class ObserveServiceTests(unittest.TestCase):
    def test_ingest_sanitizes_persists_and_emits_change(self):
        repo = FakeObservationRepository()
        publisher = FakePublisher()
        service = ObserveService(repo, publisher, FakeHostConfig())
        result = service.ingest({"machine": "hk", "agents": {"hermes": {
            "installed": True, "sessions": [{"session_id": "hidden"}]}}})
        self.assertTrue(result["ok"])
        self.assertNotIn("sessions", repo.current["agents"]["hermes"])
        self.assertEqual(publisher.events[0]["event"], "state_changed")

    def test_status_uses_repository_and_public_fields_only(self):
        repo = FakeObservationRepository(current={"hk": {
            "machine": "hk", "reachable": True, "agents": {}, "system": {}}})
        result = ObserveService(repo, FakePublisher(), FakeHostConfig()).status()
        self.assertIn("machines", result)
        self.assertNotIn("_ts", result["machines"][0])
```

- [ ] **Step 2: Run tests and verify failure**

Run: `.venv/bin/python -m unittest tests.test_services.ObserveServiceTests -v`  
Expected: FAIL because `ObserveService` does not exist.

- [ ] **Step 3: Move observe use cases out of routes**

Move machine validation, sanitize calls, old/current diff, snapshot save, summary construction, machine history construction and stale reconciliation into `ObserveService`. Host config must be injected or loaded by a dedicated config object; service must not import `hub.web.load_hosts`. Keep the current `POST /api/ingest` result `{ok, changes}`, public status shape, machine detail shape, event redaction and scan semantics.

- [ ] **Step 4: Run service and existing observe tests**

Run: `.venv/bin/python -m unittest tests.test_services.ObserveServiceTests tests.test_observability tests.test_regressions.IngestApiSecurityTests -v`  
Expected: PASS; route behavior remains unchanged while business work now executes through the service.

- [ ] **Step 5: Commit**

```bash
git add hub/application/observe_service.py hub/scan.py hub/routes_observe.py hub/bootstrap.py tests/test_services.py
git commit -m "refactor: move observation use cases into service"
```

### Task 7: Task and runner application services

**Files:**
- Create: `hub/application/task_service.py`, `hub/application/runner_service.py`
- Modify: `hub/routes_tasks.py`, `hub/routes_commands.py`, `hub/bootstrap.py`
- Test: `tests/test_services.py`

**Interfaces:**
- Produces `TaskService(task_repo, observation_repo, event_publisher, host_config)` with `create(data, actor)`, `list(machine=None, state=None, limit=50)`, `get(task_id)`, `cancel(task_id, actor)`, `retry(task_id, actor)`.
- Produces `RunnerService(task_repo, event_publisher)` with `poll(machine, runner_id)`, `heartbeat(machine, attempt_id, nonce, log_lines)`, `result(machine, attempt_id, nonce, exit_code, log_summary, diff_stat, duration_s)`.
- Services return public/domain results and stable application errors, never Flask `jsonify` responses.

- [ ] **Step 1: Add service tests for policy, events and fencing delegation**

```python
class TaskServiceTests(unittest.TestCase):
    def test_create_rejects_offline_machine_before_repository_write(self):
        repo = FakeTaskRepository()
        service = TaskService(repo, FakeObservationRepository(), FakePublisher(),
                              FakeHostConfig(online=False))
        with self.assertRaises(ApplicationError) as ctx:
            service.create({"machine": "hk", "agent_type": "codex",
                            "project": "agent-fleet", "instruction": "run"}, "op")
        self.assertEqual(ctx.exception.code, "machine_offline")
        self.assertEqual(repo.created, [])

    def test_runner_result_publishes_task_finished_after_repository_result(self):
        repo = FakeTaskRepository(result={"task_id": "t-1", "state": "succeeded", "stored": True})
        publisher = FakePublisher()
        service = RunnerService(repo, publisher)
        result = service.result("hk", "a-1", "n-1", 0, "ok", "1 file", 1.2)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(publisher.events[-1]["event"], "task_finished")
```

- [ ] **Step 2: Run tests and verify failure**

Run: `.venv/bin/python -m unittest tests.test_services.TaskServiceTests -v`  
Expected: FAIL because task and runner services do not exist.

- [ ] **Step 3: Move task policy and event orchestration**

Move machine online check, project whitelist check, input length validation, public serialization, task create/list/get/cancel/retry and task event emission into `TaskService`. Move runner poll/heartbeat/result orchestration and log line bounds into `RunnerService`, while retaining repository-level lease fencing and result idempotency. `RunnerService` must use the authenticated machine passed by the HTTP adapter; it must never trust a machine field from runner JSON to widen authority.

- [ ] **Step 4: Run service and task/runner suites**

Run: `.venv/bin/python -m unittest tests.test_services tests.test_task_api tests.test_runner.AgentRunnerTests tests.test_runner.EndToEndRunnerTests -v`  
Expected: PASS; existing HTTP and real runner flow remain green.

- [ ] **Step 5: Commit**

```bash
git add hub/application/task_service.py hub/application/runner_service.py hub/routes_tasks.py hub/routes_commands.py hub/bootstrap.py tests/test_services.py
git commit -m "refactor: isolate task and runner application services"
```

### Task 8: Thin HTTP adapters, auth extraction and unified errors

**Files:**
- Create: `hub/http/__init__.py`, `hub/http/errors.py`, `hub/http/observe_routes.py`, `hub/http/task_routes.py`, `hub/http/command_routes.py`, `hub/http/pages.py`
- Modify: `hub/auth.py`, `hub/web.py`, `hub/routes_observe.py`, `hub/routes_tasks.py`, `hub/routes_commands.py`
- Test: `tests/test_http_contracts.py`, `tests/test_auth_matrix.py`

**Interfaces:**
- Produces `ApplicationError(code: str, detail: str, status: int = 400)` and `error_response(error, request_id: str)` with `{ok:false,error,detail,request_id}`.
- Produces `extract_operator_identity(request) -> str | None`, `extract_runner_identity(request) -> tuple[str, str] | None`, and `extract_ingest_token(request) -> str` as transport-only helpers.
- Produces HTTP adapters that receive services from `current_app.extensions["fleet"]`; route modules must not import `STATE_DIR`, `EVENT_LOG`, `DB_PATH`, `request`-independent stores, or hosts file parsers for business logic.

- [ ] **Step 1: Add contract and auth matrix tests**

```python
class AuthMatrixTests(unittest.TestCase):
    def test_ingest_header_cannot_fallback_to_dev_operator(self):
        client = make_test_client(dev_operator="dev@example.com")
        response = client.post("/api/tasks", json={},
                               headers={"X-Agent-Fleet-Token": "test-only"})
        self.assertEqual(response.status_code, 401)

    def test_runner_header_cannot_call_operator_task_api(self):
        client = make_test_client(dev_operator=None)
        response = client.post("/api/tasks", json={},
                               headers={"X-Runner-Credential": "hk:test-only"})
        self.assertEqual(response.status_code, 401)

class ErrorContractTests(unittest.TestCase):
    def test_application_error_has_stable_public_shape(self):
        response = client.post("/api/tasks", json={})
        self.assertEqual(set(response.get_json()), {"ok", "error", "detail", "request_id"})
        self.assertFalse(response.get_json()["ok"])
```

- [ ] **Step 2: Run tests and verify failure**

Run: `.venv/bin/python -m unittest tests.test_http_contracts tests.test_auth_matrix -v`  
Expected: FAIL because the new adapters and error contract do not exist.

- [ ] **Step 3: Implement thin adapters and preserve old blueprints**

Create new HTTP modules with the current URL rules and decorators. Each route parses input, obtains the authenticated identity, invokes one service method, and serializes the result. Use one error conversion function for validation, application, unavailable-store and lease errors. The compatibility `routes_*.py` modules may re-export/register the new blueprints during migration, but may not duplicate business code. Keep page routes in `pages.py`; page routes may render the compatibility templates only.

Add an opaque per-request ID generated at the HTTP boundary and returned in errors; never include exception repr, absolute paths, SQL or credentials. Keep public observe endpoints unauthenticated at the hub layer as currently designed, while preserving edge policy documentation separately.

- [ ] **Step 4: Run the complete backend contract suite**

Run: `.venv/bin/python -m unittest tests.test_http_contracts tests.test_auth_matrix tests.test_observability tests.test_task_api tests.test_push_only -v`  
Expected: PASS; existing status codes, public fields, auth isolation, SSE headers and degradation behavior remain compatible.

- [ ] **Step 5: Commit**

```bash
git add hub/http hub/auth.py hub/web.py hub/routes_observe.py hub/routes_tasks.py hub/routes_commands.py tests/test_http_contracts.py tests/test_auth_matrix.py
git commit -m "refactor: make Flask routes thin HTTP adapters"
```

### Task 9: Phase 1 boundary gate and compatibility cleanup

**Files:**
- Modify: `hub/web.py`, `hub/state.py`, `hub/events.py`, `hub/task_store.py`, `docs/HANDOFF.md`
- Test: existing backend tests and `tests/test_boundaries.py`

**Interfaces:**
- Produces static boundary assertions: HTTP route modules cannot import legacy path globals or call direct storage functions; `web.make_app` delegates to bootstrap; compatibility facades remain callable.

- [ ] **Step 1: Add source boundary tests**

```python
class BoundaryTests(unittest.TestCase):
    def test_http_adapters_do_not_reference_storage_globals(self):
        for path in Path("hub/http").glob("*.py"):
            source = path.read_text()
            self.assertNotIn("STATE_DIR", source)
            self.assertNotIn("DB_PATH", source)
            self.assertNotIn("EVENT_LOG", source)

    def test_legacy_entrypoint_uses_bootstrap(self):
        source = Path("hub/web.py").read_text()
        self.assertIn("create_app", source)
```

- [ ] **Step 2: Run complete Phase 1 validation**

Run the focused boundary tests, full unittest suite, compileall and `bash deploy/e2e-smoke.sh`. Expected: all pass; smoke reports `SMOKE OK`.

- [ ] **Step 3: Document compatibility and single-writer assumption**

Add a short `docs/HANDOFF.md` section stating that Phase 1 uses one backend writer for JSONL/event files, SQLite remains WAL-backed, old paths remain stable, and production deployment is not changed by this phase. Do not document credentials or execute deployment.

- [ ] **Step 4: Commit**

```bash
git add hub/web.py hub/state.py hub/events.py hub/task_store.py docs/HANDOFF.md tests/test_boundaries.py
git commit -m "test: lock down backend boundary compatibility"
```

---

# Phase 2：Application service 完成

### Task 10: Reconciliation and lifecycle service isolation

**Files:**
- Create: `hub/application/reconciliation_service.py`
- Modify: `hub/web.py`, `hub/scan.py`, `hub/bootstrap.py`
- Test: `tests/test_services.py`, `tests/test_push_only.py`, `tests/test_task_api.py`

**Interfaces:**
- Produces `ReconciliationService(observe_service, task_repository, event_publisher, *, clock=time.time)` with `reconcile_observation(machine=None) -> list[dict]`, `expire_task_leases() -> list[str]`, `expire_tasks() -> list[str]`.
- `start_reconciliation` and `start_lease_reconciler` accept injected callbacks and stop events; they must not import Flask globals or execute remote processes.

- [ ] **Step 1: Add lifecycle tests**

```python
class ReconciliationServiceTests(unittest.TestCase):
    def test_observation_reconcile_only_calls_push_state_service(self):
        service = ReconciliationService(FakeObserveService(), FakeTaskRepository(), FakePublisher())
        with patch("subprocess.run", side_effect=AssertionError("remote execution")):
            result = service.reconcile_observation()
        self.assertEqual(result, [])
```

- [ ] **Step 2: Run and verify failure**

Run: `.venv/bin/python -m unittest tests.test_services.ReconciliationServiceTests -v`  
Expected: FAIL because the lifecycle service does not exist.

- [ ] **Step 3: Move daemon orchestration behind bootstrap**

`web.py` must become a compatibility CLI entrypoint. `bootstrap` assembles services and exposes explicit `start_background_jobs(app)` or equivalent only for the real process entrypoint. Tests calling `make_app` must not silently spawn daemon threads. Reconciliation must call only observation/task service methods and publisher emission; it must never call `ssh`, `subprocess`, runner adapters or agent commands.

- [ ] **Step 4: Run lifecycle and E2E tests**

Run: `.venv/bin/python -m unittest tests.test_services.ReconciliationServiceTests tests.test_push_only tests.test_task_api.LeaseReconcilerTests -v` and `bash deploy/e2e-smoke.sh`. Expected: PASS and `SMOKE OK`.

- [ ] **Step 5: Commit**

```bash
git add hub/application/reconciliation_service.py hub/web.py hub/scan.py hub/bootstrap.py tests/test_services.py tests/test_push_only.py tests/test_task_api.py
git commit -m "refactor: isolate backend reconciliation lifecycle"
```

### Task 11: Final service failure/degradation and contract regression gate

**Files:**
- Modify: `hub/application/*.py`, `hub/http/errors.py`, `hub/bootstrap.py`
- Test: `tests/test_services.py`, `tests/test_http_contracts.py`, `tests/test_task_api.py`, `tests/test_observability.py`

**Interfaces:**
- Produces explicit application error classes for `invalid_json`, `invalid_machine`, `machine_offline`, `project_not_allowed`, `tasks_unavailable`, `lease_expired`, `lease_mismatch`, `not_found`.
- All services preserve bounded user-visible details and never expose raw exception text.

- [ ] **Step 1: Add failure isolation tests**

```python
class FailureIsolationTests(unittest.TestCase):
    def test_task_repository_failure_keeps_status_endpoint_available(self):
        app = make_app_with_failing_task_repository()
        client = app.test_client()
        self.assertEqual(client.get("/api/status").status_code, 200)
        self.assertEqual(client.post("/api/tasks", json={}).status_code, 503)

    def test_event_repository_failure_does_not_fail_ingest(self):
        app = make_app_with_failing_event_repository()
        response = app.test_client().post(
            "/api/ingest", json={"machine": "hk", "agents": {}},
            headers={"X-Agent-Fleet-Token": "test-only"})
        self.assertEqual(response.status_code, 200)
```

- [ ] **Step 2: Implement explicit failure handling**

Catch infrastructure failures at the service boundary only where the spec requires degradation. Task repository initialization failure sets `TASKS_ENABLED=False`; observe repositories remain available. Event append errors are logged and live subscribers still receive the event. Unexpected programmer errors remain visible to tests/logs but are converted to a bounded 500 response at the HTTP edge.

- [ ] **Step 3: Run Phase 2 gate**

Run full unittest, compileall and `bash deploy/e2e-smoke.sh`. Expected: all tests pass, no real credentials are read, no external requests are made by backend tests.

- [ ] **Step 4: Commit**

```bash
git add hub/application hub/http/errors.py hub/bootstrap.py tests/test_services.py tests/test_http_contracts.py tests/test_task_api.py tests/test_observability.py
git commit -m "test: enforce service failure isolation and API contracts"
```

---

# Phase 3：独立静态前端

### Task 12: Static frontend shell, runtime config and route helpers

**Files:**
- Create: `frontend/index.html`, `frontend/config.js`, `frontend/routes.js`, `frontend/styles/app.css`
- Test: `tests/test_frontend_contracts.py`, `tests/test_release_layout.py`

**Interfaces:**
- Produces `window.FleetConfig.apiBaseUrl` defaulting to `/api`, with no secret values.
- Produces route helpers `apiPath(...segments)`, `pagePath(...segments)` that encode each path segment and never accept a raw slash as a segment.
- Produces static entry that can be opened from a static server without Flask template rendering.

- [ ] **Step 1: Add static shell tests**

```python
class StaticFrontendTests(unittest.TestCase):
    def test_frontend_entry_has_no_flask_template_expression(self):
        source = Path("frontend/index.html").read_text()
        self.assertNotIn("{{", source)
        self.assertNotIn("url_for", source)

    def test_runtime_config_contains_no_credential_names(self):
        source = Path("frontend/config.js").read_text()
        for secret_name in ("X-Agent-Fleet-Token", "X-Runner-Credential", "private_key"):
            self.assertNotIn(secret_name, source)
```

- [ ] **Step 2: Run and verify failure**

Run: `.venv/bin/python -m unittest tests.test_frontend_contracts tests.test_release_layout -v`  
Expected: FAIL because `frontend/` does not exist.

- [ ] **Step 3: Implement static shell and CSS migration**

Create `index.html` with page mount points for Fleet, machine and task views, `<script type="module">` imports, and a non-secret config script. Copy the existing visual language and responsive dimensions from `hub/static/style.css` into `frontend/styles/app.css` without changing the information architecture. Do not embed task/observation data in HTML. Use `data-page` or URL routing only as non-sensitive navigation state.

- [ ] **Step 4: Run static checks**

Run: `.venv/bin/python -m unittest tests.test_frontend_contracts.StaticFrontendTests -v`; expected PASS. Open through a local static server only if a browser is available; no Flask process should be required to load HTML, CSS and modules.

- [ ] **Step 5: Commit**

```bash
git add frontend/index.html frontend/config.js frontend/routes.js frontend/styles/app.css tests/test_frontend_contracts.py tests/test_release_layout.py
git commit -m "feat: add standalone frontend shell and runtime config"
```

### Task 13: Public contracts and API client

**Files:**
- Create: `frontend/api/contracts.js`, `frontend/api/client.js`
- Modify: `frontend/config.js`
- Test: `tests/test_frontend_contracts.py`

**Interfaces:**
- Produces `ApiError` with `.kind`, `.status`, `.code`, `.detail`, `.requestId`.
- Produces async client functions: `getStatus`, `getMachine`, `getEvents`, `listTasks`, `getTask`, `createTask`, `cancelTask`, `retryTask`.
- Produces contract functions `parseStatus`, `parseMachine`, `parseEvents`, `parseTask`, `parseSseEvent` that return validated public models or throw bounded contract errors.

- [ ] **Step 1: Add source contract tests**

```python
class ApiClientSourceTests(unittest.TestCase):
    def test_views_are_not_allowed_to_call_fetch_directly(self):
        for path in Path("frontend/views").glob("*.js"):
            self.assertNotIn("fetch(", path.read_text())

    def test_client_owns_all_api_paths(self):
        source = Path("frontend/api/client.js").read_text()
        for path in ("/status", "/machines/", "/tasks", "/events"):
            self.assertIn(path, source)
```

- [ ] **Step 2: Add executable contract fixture tests**

Use only synthetic non-secret payloads. Assert task parsers reject `attempt_id`, nonce and unknown result internals when those appear in public payloads; assert machine parser retains sanitized fields; assert error parser preserves only `ok`, `error`, `detail`, `request_id`.

- [ ] **Step 3: Implement client and parsers**

`client.js` must resolve `FleetConfig.apiBaseUrl`, encode path segments, use `cache: "no-store"`, set JSON headers for mutation requests, parse JSON once, convert non-2xx and network/timeout failures to `ApiError`, and run response parsers. It must not add ingest or runner headers. Use `AbortController` timeout with a bounded default. Keep `client_token` generation inside task creation as a browser idempotency value; never treat it as a credential.

- [ ] **Step 4: Run frontend contract tests**

Run: `.venv/bin/python -m unittest tests.test_frontend_contracts -v`; expected PASS. Existing `tests/test_frontend_xss.py` must remain green; do not weaken its source assertions.

- [ ] **Step 5: Commit**

```bash
git add frontend/api frontend/config.js tests/test_frontend_contracts.py
git commit -m "feat: add frontend API client and public contracts"
```

### Task 14: SSE client and state store

**Files:**
- Create: `frontend/realtime/sse.js`, `frontend/state/store.js`
- Modify: `frontend/api/client.js`
- Test: `tests/test_frontend_contracts.py`

**Interfaces:**
- Produces `FleetStore` with `getState()`, `subscribe(listener)`, `setStatus(data)`, `setMachine(data)`, `setTask(data)`, `applySseEvent(event)`, `setConnection(status)`, `getLastEventTs()`.
- Produces `SseClient(store, { apiBaseUrl, onEvent })` with `start()`, `stop()`, `isOpen()`; only this module calls `new EventSource`.

- [ ] **Step 1: Add lifecycle source tests**

```python
class SseBoundaryTests(unittest.TestCase):
    def test_only_sse_module_creates_eventsource(self):
        files = list(Path("frontend").rglob("*.js"))
        owners = [path for path in files if "new EventSource" in path.read_text()]
        self.assertEqual([p.as_posix() for p in owners], ["frontend/realtime/sse.js"])

    def test_store_deduplicates_or_rejects_stale_event_timestamps(self):
        source = Path("frontend/state/store.js").read_text()
        self.assertIn("lastEventTs", source)
```

- [ ] **Step 2: Implement store and SSE lifecycle**

Store event application must update the timestamp using `Math.max`, validate event type through contracts, append bounded task logs, update machine summaries, and notify listeners without touching DOM. `SseClient.start()` must be idempotent; `onerror` marks disconnected and starts one bounded poll fallback; `onopen` clears fallback; `stop()` closes the source and clears timers. Build the URL from `apiBaseUrl + "/stream?since=" + encodeURIComponent(lastEventTs)`.

- [ ] **Step 3: Implement terminal task refresh**

When a task event reaches `succeeded`, `failed`, `cancelled` or `expired`, call `client.getTask(taskId)` and update the store. Do not call `location.reload()` from new frontend code. Log events use text data only and cap rendered log lines to 500 as a client-side display bound.

- [ ] **Step 4: Run lifecycle source checks**

Run: `.venv/bin/python -m unittest tests.test_frontend_contracts.SseBoundaryTests -v`; expected PASS. Verify `rg -n "fetch\(|new EventSource|location\.reload" frontend` shows calls only in `api/client.js`, `realtime/sse.js`, and no business reload.

- [ ] **Step 5: Commit**

```bash
git add frontend/realtime frontend/state frontend/api/client.js tests/test_frontend_contracts.py
git commit -m "feat: add frontend SSE lifecycle and state store"
```

### Task 15: Fleet and machine views

**Files:**
- Create: `frontend/views/fleet.js`, `frontend/views/machine.js`
- Modify: `frontend/index.html`, `frontend/routes.js`
- Test: `tests/test_frontend_contracts.py`, `tests/test_frontend_xss.py`

**Interfaces:**
- Produces `mountFleet(root, store, client) -> ()` and `mountMachine(root, machineName, store, client) -> ()`.
- Views use store subscriptions and client methods only; no direct `fetch`, `EventSource`, Flask bootstrap object, or inline event handler.

- [ ] **Step 1: Add view boundary tests**

```python
class ViewBoundaryTests(unittest.TestCase):
    def test_views_use_dom_safe_writes_for_dynamic_text(self):
        for path in (Path("frontend/views/fleet.js"), Path("frontend/views/machine.js")):
            source = path.read_text()
            self.assertNotIn("insertAdjacentHTML", source)
            self.assertNotIn("innerHTML", source)
            self.assertIn("textContent", source)
```

- [ ] **Step 2: Implement Fleet view**

Render health counts, machine cards, status/error state, sanitized agent summaries, system metrics and event list. Use `createElement`, `textContent`, `setAttribute`, and encoded route helpers. Clicks navigate to `pagePath("machine", machineName)`; no user-controlled value is inserted into an HTML string. SSE machine updates refresh the relevant store/read model and retain bounded event history.

- [ ] **Step 3: Implement machine view and task creation**

Load machine detail and recent tasks through client methods. Render system metrics, agent table, online timeline placeholder/renderer, task list and create-task form. Validate required project/instruction fields client-side for UX but rely on backend validation for authorization. Create-task errors use textContent; successful creation navigates using `pagePath("task", taskId)`.

- [ ] **Step 4: Run source/XSS checks**

Run: `.venv/bin/python -m unittest tests.test_frontend_contracts.ViewBoundaryTests tests.test_frontend_xss -v`; expected PASS. Synthetic `<img>` and quoted payloads must remain text, never markup.

- [ ] **Step 5: Commit**

```bash
git add frontend/views/fleet.js frontend/views/machine.js frontend/index.html frontend/routes.js tests/test_frontend_contracts.py tests/test_frontend_xss.py
git commit -m "feat: add standalone fleet and machine views"
```

### Task 16: Task view, navigation and compatibility shell handoff

**Files:**
- Create: `frontend/views/task.js`
- Modify: `frontend/index.html`, `hub/templates/base.html`, `hub/templates/fleet.html`, `hub/templates/machine.html`, `hub/templates/task.html`, `hub/static/app.js`
- Test: `tests/test_frontend_contracts.py`, `tests/test_observability.py`, `tests/test_task_api.py`

**Interfaces:**
- Produces `mountTask(root, taskId, store, client) -> ()`.
- Compatibility Flask pages may redirect or render a static shell, but must not inject task/observation business objects into the new frontend.

- [ ] **Step 1: Add task boundary tests**

```python
class TaskViewBoundaryTests(unittest.TestCase):
    def test_new_task_view_refreshes_via_client(self):
        source = Path("frontend/views/task.js").read_text()
        self.assertIn("getTask", source)
        self.assertNotIn("location.reload", source)
        self.assertNotIn("fetch(", source)

    def test_compatibility_pages_still_render_until_release_cutover(self):
        html = make_test_client().get("/task/t-unknown")
        self.assertIn(html.status_code, (404, 503))
```

- [ ] **Step 2: Implement task detail view**

Render instruction, state flow, result fields, bounded diff summary and log area. Bind cancel/retry actions through delegated event listeners. Apply task_update and task_log store events; on terminal state fetch task detail and rerender result/buttons. All dynamic instruction, error and log values use textContent/pre text nodes. Keep visible loading/error/empty states.

- [ ] **Step 3: Make old pages a compatibility shell**

Keep current Flask page URLs for rollback and old bookmarks. Add a documented cutover mode in runtime config or page adapter so static frontend routes can own `/`, `/machine/<name>`, and `/task/<id>` after deployment. Until cutover, old templates remain functional and are not deleted. Remove new business fetches from templates; old shell may retain only a compatibility bootstrap that points to the static release.

- [ ] **Step 4: Run page and frontend integration checks**

Run: `.venv/bin/python -m unittest tests.test_frontend_contracts tests.test_observability.PageViewTests tests.test_task_api.TaskPageTests tests.test_frontend_xss -v`; expected PASS. Run `rg -n "fetch\(|EventSource|location\.reload" frontend/views frontend/index.html`; expected no direct view fetch/EventSource/reload.

- [ ] **Step 5: Commit**

```bash
git add frontend/views/task.js frontend/index.html hub/templates hub/static/app.js tests/test_frontend_contracts.py tests/test_observability.py tests/test_task_api.py
git commit -m "feat: add standalone task view and shell handoff"
```

### Task 17: Phase 3 frontend gate and static-only smoke

**Files:**
- Create: `deploy/test-static-frontend.sh`
- Modify: `tests/test_release_layout.py`, `docs/HANDOFF.md`

**Interfaces:**
- Produces a local static smoke command that starts a standard-library HTTP server for `frontend/`, checks `index.html`, `config.js`, modules and CSS, and never starts Flask or reads credentials.

- [ ] **Step 1: Add release layout and static smoke tests**

```python
class ReleaseLayoutTests(unittest.TestCase):
    def test_frontend_has_required_modules(self):
        required = ("index.html", "config.js", "api/client.js", "api/contracts.js",
                    "realtime/sse.js", "state/store.js", "views/fleet.js",
                    "views/machine.js", "views/task.js")
        for relative in required:
            self.assertTrue((Path("frontend") / relative).is_file(), relative)
```

- [ ] **Step 2: Implement `deploy/test-static-frontend.sh`**

Use `python3 -m http.server` on a dynamically selected loopback port, poll readiness with curl, fetch the required paths, assert HTTP 200 and assert no response contains credential header names. Ensure a trap kills only the server PID started by the script. Do not use `git clean`, delete `.playwright-mcp/`, or access external services.

- [ ] **Step 3: Run Phase 3 gate**

Run full unittest, compileall, `bash deploy/test-static-frontend.sh`, and `bash deploy/e2e-smoke.sh`. Expected: static shell works independently, existing Flask compatibility pages work, and runner/observation smoke remains green.

- [ ] **Step 4: Commit**

```bash
git add deploy/test-static-frontend.sh tests/test_release_layout.py docs/HANDOFF.md
git commit -m "test: validate standalone frontend release"
```

---

# Phase 4：独立发布、兼容 API 和部署文档

### Task 18: Versioned API adapters and public contract tests

**Files:**
- Create: `hub/http/v1/__init__.py`, `hub/http/v1/observe_routes.py`, `hub/http/v1/task_routes.py`, `hub/http/v1/command_routes.py`
- Modify: `hub/bootstrap.py`, `hub/http/errors.py`
- Test: `tests/test_http_contracts.py`

**Interfaces:**
- Produces `/api/v1/status`, `/api/v1/machines/<name>`, `/api/v1/events`, `/api/v1/stream`, `/api/v1/tasks*` adapters; runner v1 command paths are registered only if the runner compatibility contract is explicitly tested, while old `/api/commands/*` remains authoritative for deployed runners.
- All v1 adapters call the same service objects as old routes and return public DTOs only.

- [ ] **Step 1: Add old/v1 equivalence tests**

```python
class VersionedApiTests(unittest.TestCase):
    def test_status_old_and_v1_use_same_public_shape(self):
        old = client.get("/api/status").get_json()
        new = client.get("/api/v1/status").get_json()
        self.assertEqual(set(old), set(new))
        self.assertEqual(set(old["machines"][0]), set(new["machines"][0]))

    def test_v1_task_error_preserves_stable_fields(self):
        response = client.post("/api/v1/tasks", json={})
        self.assertFalse(response.get_json()["ok"])
        self.assertIn("error", response.get_json())
        self.assertIn("detail", response.get_json())
        self.assertIn("request_id", response.get_json())
```

- [ ] **Step 2: Implement v1 adapters by delegation**

Register v1 blueprints with explicit prefixes. Do not copy validation, state transition, event emission, or repository access. Add contract tests for status, machine detail, events, stream headers, task CRUD, cancel/retry, and all three auth domains. Do not switch frontend default to v1 until these tests pass.

- [ ] **Step 3: Run compatibility and security tests**

Run: `.venv/bin/python -m unittest tests.test_http_contracts tests.test_auth_matrix tests.test_task_api tests.test_observability -v`; expected PASS. `git diff` must show no credential literals.

- [ ] **Step 4: Commit**

```bash
git add hub/http/v1 hub/bootstrap.py hub/http/errors.py tests/test_http_contracts.py
git commit -m "feat: add versioned API compatibility adapters"
```

### Task 19: Frontend/backend release packaging

**Files:**
- Create: `deploy/package-frontend-release.sh`, `deploy/frontend-release-layout.md`, `tests/test_release_layout.py`
- Modify: `README.md`, `docs/HANDOFF.md`

**Interfaces:**
- Produces `deploy/package-frontend-release.sh [output_dir]` that copies only `frontend/` static files into a release directory, writes a non-secret manifest with release version supplied by caller, and fails if credentials/state/database files are included.
- Backend release remains the repository Python package plus `requirements.txt`; no frontend files are required for JSON API startup.

- [ ] **Step 1: Add package safety tests**

```python
class PackageSafetyTests(unittest.TestCase):
    def test_package_script_source_rejects_secret_and_state_paths(self):
        source = Path("deploy/package-frontend-release.sh").read_text()
        for forbidden in ("credentials/", "state/", "runner-credential", "ingest-token"):
            self.assertIn(forbidden, source)
```

- [ ] **Step 2: Implement deterministic static packaging**

The script must use `set -eu`, accept an explicit output directory, copy `frontend/` preserving relative paths, write `manifest.json` containing only release version and file list, and fail if any copied path matches `credentials`, `state`, `.env`, `*.pem`, `*.key`, or runner credential names. It must not call npm, git clean, external network, or production endpoints.

- [ ] **Step 3: Add release and rollback documentation**

Document frontend release layout, immutable hashed asset policy, non-cacheable `index.html`/`config.js`, backend-before-frontend compatibility order, frontend-only rollback, backend rollback restrictions around SQLite and active leases, and local package/smoke commands. Do not include real host credentials or deployment tokens.

- [ ] **Step 4: Run package tests**

Run: `.venv/bin/python -m unittest tests.test_release_layout -v`; run the package script into a temporary directory and assert required files and no forbidden files. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add deploy/package-frontend-release.sh deploy/frontend-release-layout.md tests/test_release_layout.py README.md docs/HANDOFF.md
git commit -m "feat: define independent frontend release packaging"
```

### Task 20: Nginx/Access route documentation and local proxy contract

**Files:**
- Create: `deploy/nginx-frontend-backend.example.conf`, `deploy/test-release-routing.sh`
- Modify: `deploy/nginx-expose.md`, `deploy/cloudflare-access.md`
- Test: `tests/test_release_layout.py`, `tests/test_http_contracts.py`

**Interfaces:**
- Produces documented route matrix: `/` and `/assets/*` static frontend; `/api/*` backend; `/api/stream` buffering disabled and long read timeout; `/api/ingest`, `/api/scan`, `/api/commands/*` Access Bypass with hub credentials; page/task/operator paths remain Access Include.
- Produces a configuration/source test that rejects `proxy_buffering on` for SSE and rejects wildcard CORS.

- [ ] **Step 1: Add routing documentation tests**

```python
class RoutingDocumentationTests(unittest.TestCase):
    def test_sse_location_disables_buffering(self):
        source = Path("deploy/nginx-frontend-backend.example.conf").read_text()
        self.assertIn("location /api/stream", source)
        self.assertIn("proxy_buffering off", source)
        self.assertNotIn("Access-Control-Allow-Origin *", source)
```

- [ ] **Step 2: Write the example config and route matrix**

Use placeholder upstream names such as `fleet_frontend` and `fleet_backend`; do not include real certificates, keys, tokens or credentials. Include `proxy_set_header Host`, `X-Forwarded-Proto`, and the SSE timeout/buffering directives. Keep probe/runner paths routed to backend and document that Access bypass is edge policy, not an authorization bypass at hub.

- [ ] **Step 3: Add local routing smoke without external deployment**

`deploy/test-release-routing.sh` may inspect rendered config and start only local test processes if needed. It must verify static asset and API route intent, never contact Cloudflare or production, and clean up only its own temporary processes.

- [ ] **Step 4: Run route/documentation tests**

Run: `.venv/bin/python -m unittest tests.test_release_layout tests.test_http_contracts -v`; run shell syntax checks with `bash -n deploy/package-frontend-release.sh deploy/test-static-frontend.sh deploy/test-release-routing.sh`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add deploy/nginx-frontend-backend.example.conf deploy/test-release-routing.sh deploy/nginx-expose.md deploy/cloudflare-access.md tests/test_release_layout.py tests/test_http_contracts.py
git commit -m "docs: define same-origin frontend backend routing"
```

### Task 21: Whole-branch integration and release gate

**Files:**
- Modify: `deploy/e2e-smoke.sh`, `README.md`, `docs/HANDOFF.md` only where final commands or topology are stale
- Test: all existing tests plus `tests/test_release_layout.py`, `tests/test_frontend_contracts.py`

**Interfaces:**
- Produces one local acceptance path covering static frontend packaging, backend API startup, ingest, public status, operator task create, runner poll/heartbeat/result, SSE and frontend/backend route contract; no external credentials or production services.

- [ ] **Step 1: Extend local smoke assertions**

Keep the existing loopback runner E2E. Add temporary static package creation and assert the package contains `index.html`, `config.js`, client, SSE and view modules. Add a backend-only startup check that imports `hub.bootstrap.create_app` without loading frontend files. Do not alter production `load_config()` HTTPS validation or use real credentials.

- [ ] **Step 2: Run the full validation matrix**

```bash
.venv/bin/python -m unittest discover -s tests -v
python3 -m compileall -q connectors hub tools tests
bash -n deploy/*.sh
bash deploy/test-static-frontend.sh
bash deploy/test-release-routing.sh
bash deploy/e2e-smoke.sh
```

Expected: all unittest cases pass, compileall and shell syntax checks are clean, static smoke passes, routing checks pass, and E2E ends with `SMOKE OK`.

- [ ] **Step 3: Run final boundary/security scans**

```bash
rg -n "X-Agent-Fleet-Token|X-Runner-Credential|runner-credential|ingest-token|private_key|BEGIN .* PRIVATE" frontend deploy docs README.md
rg -n "ssh|paramiko|reverse tunnel|任意 shell|subprocess" hub/application hub/http hub/domain
rg -n "new EventSource|fetch\(" frontend
```

Expected: frontend has no credential values or credential headers; backend application/domain/http contains no remote-agent execution path; frontend has exactly one EventSource owner and exactly one API client owner.

- [ ] **Step 4: Verify working tree safety and document closeout**

Confirm `git status --short` contains no generated state/database/credential files and leaves pre-existing `.claude/` and `.playwright-mcp/` untouched. Update README/HANDOFF with independent release order, same-origin topology, rollback order, old `/api/*` compatibility, `/api/v1` status, SSE proxy requirements and the fact that no production deployment was performed.

- [ ] **Step 5: Commit**

```bash
git add deploy/e2e-smoke.sh README.md docs/HANDOFF.md tests
git commit -m "test: close frontend backend separation release gate"
```

## Plan Self-Review

### Spec coverage

- Deployment topology, separate release artifacts and same-origin routing: Tasks 12, 17, 19, 20, 21.
- No Node build chain and runtime non-secret config: Tasks 12, 19, 21.
- API client, public contracts, SSE lifecycle, store and three views: Tasks 13–16.
- Backend config/bootstrap, domain, application, HTTP layers: Tasks 1, 5–8, 10–11.
- Observation/event/task repository ownership and explicit paths: Tasks 2–4.
- App-instance publisher, bounded subscribers, replay and persistence failure isolation: Task 3.
- Three authentication domains and unified errors: Tasks 8, 11, 18, 20.
- Legacy `/api/*`, `/api/v1`, probe/runner compatibility: Tasks 8, 18, 21.
- Push-only and no remote execution: Tasks 6, 7, 10, 21.
- Caching, SSE buffering, Access bypass and rollback: Tasks 17, 19, 20, 21.
- Existing observation, runner and XSS regressions: every phase gate and Task 21.
- No credentials or external deployment: Global Constraints, Tasks 19–21.

### Placeholder scan

The plan contains no `TBD`, `TODO`, “implement later”, “appropriate error handling”, or unspecified test-only step. Every task names exact files, produced interfaces, focused tests, commands and commit boundaries.

### Interface consistency

- `FleetConfig` is created in Task 1 and consumed by bootstrap and repositories in Tasks 2–4.
- `ObservationRepository`, `EventRepository`, and `SqliteTaskRepository` are introduced before services consume them.
- `EventPublisher` is introduced in Task 3 and injected into Observe/Task/Runner services.
- `ObserveService`, `TaskService`, `RunnerService`, and `ReconciliationService` are introduced before HTTP adapters consume them.
- `ApplicationError` and `error_response` are introduced in Task 8 before v1 adapters and frontend client contract tests rely on their fields.
- `apiBaseUrl`, route helpers, client functions, store methods and view mount functions are introduced in order from Tasks 12–16.
- Release packaging and routing tests consume only paths and interfaces created by Phase 3.

### Scope and safety ruling

The spec spans multiple subsystems, but it is intentionally one migration plan because every phase is a separately deployable compatibility slice and the public API, storage and frontend boundaries must be coordinated. No task changes the task database schema, runner wire protocol, production credentials or external deployment state. If an implementation discovers that a repository or service cannot preserve the current compatibility contract, it must stop that task and record the incompatibility rather than silently changing the deployed API.
