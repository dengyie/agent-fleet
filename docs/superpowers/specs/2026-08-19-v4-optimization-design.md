# agent-fleet v4 优化设计：观测加固 + 控制面落地

> 日期：2026-08-19
> 状态：已确认方案 A（渐进增强），本文档为实施依据

## 1. 背景与目标

agent-fleet 当前是 push-only 观测系统（v3 已上线）：各机器 probe 每 2 分钟 POST 状态到 HK hub，hub 只做认证、存储、告警、展示。v4 设计要求增加受控开发链路（任务队列 + runner），目前只有设计文档、零代码。

本次优化目标：
1. **观测链路加固**：解决状态存储增长、模块边界混乱、前端体验差的问题
2. **v4 控制面完整落地**：实现任务队列、lease 状态机、三种 agent adapter、runner 执行器
3. **页面重做**：三视图钻取（Fleet 总览 → 机器详情 → 任务详情），SSE 实时推送

## 2. 方案决策

采用**方案 A：渐进增强**：
- 后端：Flask 蓝图拆分 + SQLite 任务存储（标准库，零新增重型依赖）
- 前端：原生 JS + SSE，不引入构建链，模板抽离到 `templates/` + `static/`
- 部署链路不变：git pull + 重启，SSH 只用于人工部署

否决方案：
- 方案 B（前后端分离 SPA）：引入 Node 构建链，HK 部署复杂化，dashboard 收益不成比例
- 方案 C（换 FastAPI）：重写成本高，当前 Flask 代码量小，SSE 支持足够

## 3. 整体架构

```
agent machine                          HK hub (hub.example.com)
┌──────────────────────────┐          ┌────────────────────────────────────┐
│ tools/agent-self-report  │─ POST ──▶│ hub/routes_observe.py  /api/ingest │
│   (probe, 每 2min)       │  ingest  │                                    │
└──────────────────────────┘          │ hub/routes_tasks.py    /api/tasks* │
                                      │   ↑ CF Access 认证 (operator)      │
┌──────────────────────────┐          │                                    │
│ tools/agent-runner.py    │─ poll ──▶│ hub/routes_commands.py /api/commands*│
│   (新增, 长驻/循环)       │◀ lease ─│   ↑ runner credential 认证          │
│   ├ adapters: codex/     │─ result▶ │                                    │
│   │   claude_code/hermes │          │ hub/task_store.py  (SQLite 新)     │
│   └ worktree 隔离        │          │ hub/state.py       (JSONL 观测)    │
└──────────────────────────┘          │ hub/events.py      (总线 + SSE 桥)  │
                                      │ hub/web.py         (app 工厂+挂载)  │
                                      │ hub/templates/ + hub/static/ (新)  │
                                      └────────────────────────────────────┘
                                                   ▲
                                            browser (CF Access)
                                            GET / 三视图 + SSE
```

关键结构决策：
1. **认证域 = 蓝图边界**：`observe`（ingest token）、`tasks`（CF Access operator）、`commands`（runner credential）三个蓝图，认证不混用
2. **观测 JSONL 不动，任务 SQLite 新建**：观测是只追加时间序列，JSONL 够用；任务要事务和幂等，SQLite 解决 lease 竞争
3. **SSE 桥接在事件总线上**：复用现有 `events.subscribe()`，任务事件加 `task_id` 维度
4. **Runner 是独立进程**：和 self-report 并列，主动 HTTPS poll，不合并为子命令

## 4. 组件设计

### 4.1 Hub 侧

| 模块 | 职责 | 关键接口 |
|---|---|---|
| `hub/web.py` | app 工厂、蓝图挂载、启动入口 | `make_app()` |
| `hub/routes_observe.py` | 观测 API：`/api/ingest`、`/api/status`、`/api/machines/<name>`、SSE `/api/stream` | ingest token 认证 |
| `hub/routes_tasks.py` | 任务 API：`POST/GET /api/tasks`、`/cancel`、`/retry`、`/files/<path>` | CF Access 认证 |
| `hub/routes_commands.py` | runner API：`/api/commands/poll`、`/heartbeat`、`/result` | runner credential 认证 |
| `hub/auth.py`（新） | 三种认证装饰器 | `require_ingest_token` / `require_operator` / `require_runner` |
| `hub/task_store.py`（新） | task/lease/audit 的 SQLite 操作 | `create_task/lease_task/heartbeat/complete_task/expire_leases` |
| `hub/state.py` | 观测 JSONL（加轮转） | 现有 + `rotate_if_needed` |
| `hub/events.py` | 事件总线（加 SSE 订阅分发） | 现有 + `sse_subscribe` |
| `hub/scan.py` | stale reconciliation（修 filter 语义） | `reconcile_ingest(machine=None)` |

### 4.2 Agent 侧

| 模块 | 职责 |
|---|---|
| `tools/agent-runner.py`（新） | 主循环：poll → lease → 执行 → heartbeat → result。支持 `--once`（cron）和长驻 |
| `tools/runner_config.py`（新） | 读 `~/.config/agent-fleet/runner.yaml`：项目白名单（`projects: {name: {path: <abs_path>}}`）、adapter 类型、runner credential |
| `tools/adapters/__init__.py`（新） | adapter 注册表（复用 connectors 工厂模式） |
| `tools/adapters/codex.py` / `claude_code.py` / `hermes.py`（新） | 各 agent 任务执行器：接收 instruction + worktree，返回有界结果 |
| `tools/worktree.py`（新） | git worktree 创建/清理、diff 提取、结果打包 |

### 4.3 数据模型（SQLite `state/fleet.db`）

```sql
CREATE TABLE tasks (
  task_id TEXT PRIMARY KEY,
  client_token TEXT UNIQUE,  -- 幂等键
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

CREATE TABLE leases (
  attempt_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  runner_id TEXT NOT NULL,
  nonce TEXT NOT NULL,
  leased_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
-- 防双重领取：lease_task 事务内先作废旧 lease 再 INSERT（应用层保证，见 5.2）
CREATE INDEX idx_lease_task ON leases(task_id);

CREATE TABLE results (
  attempt_id TEXT PRIMARY KEY,  -- 幂等：重复提交冲突即成功
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  exit_code INTEGER,
  log_summary TEXT CHECK(length(log_summary) <= 10240),
  diff_stat TEXT CHECK(length(diff_stat) <= 5120),
  duration_s REAL,
  finished_at TEXT NOT NULL
);

CREATE TABLE audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  task_id TEXT,
  detail TEXT  -- JSON
);
```

WAL 模式 + `BEGIN IMMEDIATE` 事务保证并发安全。

### 4.4 前端

```
templates/
  base.html        -- 布局骨架 + SSE 连接
  fleet.html       -- 总览: 健康条 + 机器网格 + 事件流
  machine.html     -- 机器详情
  task.html        -- 任务详情
static/
  style.css        -- 全部样式
  app.js           -- SSE 客户端 + 局部 DOM 更新 + fetch 封装
  components.js    -- 卡片/时间线/日志区渲染（纯函数）
```

三视图：
- `/` Fleet 总览：健康条（在线数/告警数/任务数）+ 机器卡片网格 + 右侧实时事件流
- `/machine/<name>` 机器详情：系统指标 + agent 状态表 + 24h 在线时间线 + 该机器任务列表
- `/task/<id>` 任务详情：状态机可视化 + 实时日志（SSE）+ diff/测试结果 + 取消/重试

## 5. 数据流

### 5.1 观测链路（加固）

```
probe → POST /api/ingest [X-Agent-Fleet-Token]
  → routes_observe: 校验 token + machine 正则
  → scan._diff 归一化对比 → events.emit（有变化才发）
  → state.save_snapshot: JSONL 追加（>5MB 轮转）+ current.json 原子替换
  → events → SSE → 浏览器
```

### 5.2 任务链路

**创建**：
```
browser → CF Access → POST /api/tasks {machine, agent_type, project, instruction, client_token}
  → require_operator 校验 CF 头
  → 校验 machine 在线 / project 白名单 / instruction ≤2000
  → task_store.create_task: INSERT tasks (client_token UNIQUE 防重复)
  → audit + events.emit("task_queued")
```

**领取**：
```
runner → POST /api/commands/poll [X-Runner-Credential + machine]
  → require_runner 校验 credential 且 machine 匹配
  → task_store.lease_task:
      BEGIN IMMEDIATE
      SELECT task WHERE machine=? AND state='queued' AND expires_at>now LIMIT 1
      UPDATE tasks SET state='leased', attempt_id=uuid
      UPDATE leases SET expires_at=now WHERE task_id=? AND expires_at>now  -- 作废旧 lease
      INSERT leases (attempt_id PK)  -- 事务保证原子，防双重领取
      COMMIT
  → 返回 {task_id, attempt_id, instruction, project, lease_ttl=300}
```

**执行与心跳**：
```
runner 本地:
  worktree.create(project, task_id)
  adapter.run(instruction, worktree)
  每 30s → POST /api/commands/<attempt_id>/heartbeat
    → 校验 lease 未过期 → 续期 300s
    → 已过期 → 409，runner 放弃
```

**回传结果**：
```
runner → POST /api/commands/<attempt_id>/result {exit_code, log_summary, diff_stat, duration_s}
  → task_store.complete_task:
      校验 attempt_id 匹配当前 lease
      INSERT results (attempt_id PK 冲突即幂等成功)
      UPDATE tasks SET state=succeeded|failed
  → audit + events.emit("task_finished")
```

**Lease 过期**：
```
hub 每 60s: task_store.expire_leases()
  SELECT leases WHERE expires_at < now
  对应 task → state=queued（可重派）
  audit: "lease expired, task requeued"
```

### 5.3 SSE 事件流

```
browser → GET /api/stream [CF Access]
  → 注册到 events 总线
  → 持续 yield:
      event: machine_update  data: {machine, online, ...}
      event: task_update     data: {task_id, state, ...}
      event: task_log        data: {task_id, line}
      event: fleet_event     data: {event, machine, ts}
```

前端一个 EventSource 连接，按 event 类型分发。

## 6. 页面设计

### 6.1 信息架构

三级钻取：Fleet 总览 → 机器详情 → 任务详情

### 6.2 视觉

- 深色主题（`#0f172a`），三级灰度文字（`#f8fafc`/`#94a3b8`/`#64748b`）
- 状态色：绿=在线/成功，红=离线/失败，黄=告警/进行中，紫=Hermes
- SSE 事件到达时对应区域 200ms 高亮闪烁
- 响应式：≥1440px 三列+右栏事件流；768-1440px 两列；<768px 单列

### 6.3 关键交互

| 交互 | 实现 |
|---|---|
| 首屏 | Flask 模板渲染，JS 接管后 SSE 增量更新 |
| 机器状态变化 | SSE 更新对应卡片，不重渲网格 |
| 任务日志 | SSE append，DOM 最多 500 行 |
| 创建任务 | 模态框 → POST → 跳转任务详情 |
| 取消/重试 | 乐观更新 + SSE 确认 |
| SSE 断开 | EventSource 自动重连，`?since=<ts>` 补发 |

## 7. 错误处理

### 7.1 Hub 错误响应

| 类别 | HTTP | 行为 |
|---|---|---|
| 认证失败 | 401/403 | 拒绝 + audit（不含 credential），不透露 token 存在性 |
| 参数校验 | 400 | 返回具体字段错误 |
| 不存在 | 404 | `{"ok": false, "error": "not_found"}` |
| 状态冲突 | 409 | 返回当前状态（lease 过期/重复提交） |
| 服务端 | 500 | 通用错误，不泄露堆栈 |

统一格式：`{"ok": false, "error": "<code>", "detail": "<msg>"}`

### 7.2 Runner 错误处理

| 场景 | 行为 |
|---|---|
| Poll 网络失败 | 指数退避（5s→60s 封顶） |
| 执行中崩溃 | lease 300s 自然过期 → hub 重派 |
| Adapter 失败 | 上报 failed + 有界错误摘要 |
| Heartbeat 409 | 放弃任务，清理 worktree，回 poll |
| 结果提交失败 | 缓存到 `~/.cache/agent-fleet/pending/`，下次 poll 重传 |

### 7.3 数据保护

| 场景 | 保护 |
|---|---|
| SQLite 损坏 | 启动 `PRAGMA integrity_check`，损坏则重建（可从 audit 重放） |
| JSONL 中断 | 追加模式 + 坏行跳过 |
| 并发写 | WAL + `BEGIN IMMEDIATE` |
| SSE 断连 | 自动重连 + `since` 补发 |

### 7.4 安全防线

| 威胁 | 防御 |
|---|---|
| 恶意 instruction | ≤2000 字符，audit 摘要，adapter 白名单，不执行任意 shell |
| 路径穿越 | files API 校验 worktree 内，拒绝 `..` |
| 日志注入 | SSE JSON 编码，前端 `textContent` |
| Credential 泄露 | 每机独立，可单独吊销，仅 HTTPS |
| 大结果 | log ≤10KB / diff ≤5KB / 文件 ≤100KB |

### 7.5 降级

| 故障 | 降级 |
|---|---|
| SQLite 不可用 | 任务 API 503，观测继续 |
| SSE 失败 | 前端降级 10s 轮询 |
| CF Access 缺头 | `--dev-operator <email>` 开发模式（生产拒绝） |
| 事件写入失败 | 只记 stderr，不影响主流程 |

## 8. 测试策略

### 8.1 单元测试

| 模块 | 覆盖点 |
|---|---|
| `report_schema.py` | 白名单、截断、嵌套限制 |
| `hub/auth.py` | 三种装饰器、CF 头解析、runner-machine 绑定 |
| `hub/task_store.py` | CRUD、lease 原子领取（并发）、幂等提交、过期重派 |
| `hub/state.py` | JSONL 轮转、原子写、坏行跳过 |
| `tools/adapters/*` | 输出解析、worktree 隔离、结果截断 |

### 8.2 集成测试（Flask test client + 内存 SQLite）

| 测试 | 场景 |
|---|---|
| `test_ingest_flow.py` | probe POST → 落盘 → SSE 事件 |
| `test_task_flow.py` | 创建 → 领取 → heartbeat → 结果 → 状态迁移 |
| `test_task_lease_race.py` | 两 runner 同时 poll，只有一个成功 |
| `test_task_expiry.py` | lease 过期 → 回 queued → 可重领 |
| `test_auth_isolation.py` | 三种 token 不串 |

### 8.3 E2E 冒烟（手动）

- 真实 probe → 公网 POST 200
- 真实 runner → 执行任务并回传
- SSE 实时性（浏览器 1s 内更新）
- Lease 恢复（kill runner，300s 后任务回 queued）

### 8.4 测试基础设施

```python
# tests/conftest.py
- make_test_app(): Flask app + 内存 SQLite + 临时 state 目录
- mock_runner: poll/heartbeat/result helper
- freeze_time: 控制时间测 TTL
```

## 9. 实施顺序

1. **Phase 1 观测加固**（不动 v4）：
   - 拆分 web.py → 蓝图模块
   - state.py 加 JSONL 轮转
   - scan.py 修 filter 语义
   - 前端抽离 templates/static，三视图 + SSE

2. **Phase 2 任务存储和 API**：
   - task_store.py + SQLite schema
   - routes_tasks.py + routes_commands.py
   - auth.py 三种认证

3. **Phase 3 Runner**：
   - agent-runner.py 主循环
   - worktree.py
   - 三种 adapter

4. **Phase 4 联调和加固**：
   - E2E 冒烟
   - CF Access 配置
   - 部署脚本更新

每个 Phase 交付可独立部署的增量，不破坏现有观测链路。
