# v4 初始目标补齐 Implementation Plan

> **Status (2026-09-09): code complete in working tree; LIVE overlay is Task 7.** Spec: `docs/superpowers/specs/2026-09-09-v4-initial-goal-completion-design.md`.
> 本计划是 v4 原文未兑现项的实施清单，不是 2026-09-08 收口里那些条件触发延期项。

> **For agentic workers:** 按任务顺序做。每个 Task 先写失败测试再改代码。不要从本文件未勾 checkbox 倒推去重做 2026-08-19 Phase 1–4 历史计划。

**Goal:** 把 v4 控制面初始产品目标做完：脱敏 patch、测试摘要、任务暂停/继续、人工确认闸、runner 凭据轮换 SOP、备份演练清单。LIVE 发布仍走部署笔记 §0。

**非目标:** WebSocket、frontend-v2、main 自动部署、密钥下发、LLM terminate、Hermes spawn、TG 直推、exact capture 默认开。

**Tech Stack:** 现有 Python 3.10 + Flask + sqlite3 + tracked `frontend/`。无新依赖。

**前置:** LIVE `182e034` files API 已 overlay。工作树干净后再开工。

## Global Constraints

- push-only；patch/tests 随 result 上传，Hub 不反向连 runner。
- schema 只加可空列/新表；旧 web 读新库不得 500。
- `paused` 必须进 `TASK_STATES` 与前端 `contracts.js` 白名单，否则 DTO 丢字段。
- 脱敏复用 `tools/result_files.py` 的红线（token/私钥/`..`/symlink）。
- 切 LIVE 禁止 `ln -sfn`。CI 绿 → artifact → §0 tar 覆盖。
- 测试：`PYTHONPATH=. python3 -m pytest tests -q`。

## 文件结构

| 文件 | 动作 | 职责 |
|---|---|---|
| `hub/domain/task.py` | 改 | `paused` 状态；gate DTO；RESULT 增 `diff_patch`/`test_summary` |
| `hub/infrastructure/task_repository.py` | 改 | ALTER + task_gates；pause/continue/confirm；poll 过滤未确认/paused |
| `hub/application/task_service.py` | 改 | pause/continue/confirm/reject |
| `hub/http/task_routes.py` | 改 | 新路由 + GET `/diff` |
| `hub/http/command_routes.py` | 改 | result 收新字段；heartbeat 对 paused 失败关闭 |
| `tools/worktree.py` | 改 | `diff_patch()` |
| `tools/result_files.py` | 改 | patch 脱敏入口 |
| `tools/agent-runner.py` | 改 | 上传 patch + 可选 test_summary |
| `frontend/api/contracts.js` | 改 | 新字段/状态/动作白名单 |
| `frontend/api/client.js` | 改 | pause/continue/confirm/reject/getTaskDiff |
| `frontend/views/task.js` | 改 | 面板与按钮 |
| `frontend/styles/app.css` | 改 | 测试/patch/gate 样式 |
| `tests/test_task_store.py` 等 | 改/新建 | 状态机 + 脱敏 + 旧 runner 兼容 |
| `deploy/rotate-runner-credential.sh` | 新建 | 轮换步骤（不连生产） |
| `deploy/hk-backup-drill.md` | 新建 | 备份演练清单 |

---

### Task 1: 状态机与 schema — paused / gate / 新结果列

**Files:** `hub/domain/task.py`, `hub/infrastructure/task_repository.py`, `tests/test_task_store.py`（或现有 repository 测试）

**Steps:**

- [x] 失败测试：`init_db` 后 `complete_task` 带 `diff_patch`/`test_summary` 能读回；旧行两列 NULL。
- [x] 失败测试：`queued` pause → `paused`；再 continue → 新 `attempt_id` 且 `queued`；`succeeded` pause → 409。
- [x] 失败测试：`confirm=True` 创建的任务 `lease_task` 领不到，confirm 后能领；reject → `cancelled`。
- [x] 实现：`TASK_STATES` 加 `paused`。**允许 cancel 从 paused 进 cancelled**。
- [x] `init_db` 对已有库 `ALTER TABLE` 忽略 duplicate column。`task_gates` `CREATE IF NOT EXISTS`。
- [x] poll/lease SQL 排除 `paused` 和 `gate.state='pending'`。
- [x] 跑相关单测至绿。

### Task 2: HTTP + service

**Files:** `hub/application/task_service.py`, `hub/http/task_routes.py`, `hub/http/command_routes.py`, `tests/test_task_api.py`, `tests/test_services.py`

- [x] 失败测试：operator `POST /pause|/continue|/confirm|/reject` 状态码与 JSON。无 operator=401。
- [x] 失败测试：`GET /api/tasks/<id>/diff` 有 patch 返回 JSON `{diff_patch, truncated}`；无 patch=404。
- [x] 失败测试：旧 result POST 不带新字段仍 200。
- [x] heartbeat/result 若 task 已 paused/cancelled → 409，runner 停。
- [x] Fake repo 补方法，避免 test_task_api 装配失败。
- [x] 跑 `tests/test_task_api.py tests/test_services.py` 绿。

### Task 3: runner patch + test_summary

**Files:** `tools/worktree.py`, `tools/result_files.py`, `tools/agent-runner.py`, `tests/test_runner.py`（或新建 `tests/test_worktree_diff.py`）

- [x] 失败测试：worktree 改一文件，`diff_patch` 含 unified hunk，长度截断。
- [x] 失败测试：patch 含假 token 被脱敏。
- [x] 失败测试：`test-results.json` allowlist 解析；未知键丢弃；无文件 → None。
- [x] runner result 载荷含新字段（假 adapter 单测，不打网）。
- [x] 跑 runner/worktree 测试绿。

### Task 4: 前端 review 面

**Files:** `frontend/api/contracts.js`, `frontend/api/client.js`, `frontend/views/task.js`, `frontend/styles/app.css`, `tests/test_frontend_contracts.py`, `tests/test_frontend_xss.py`

- [x] contracts：`paused` 合法 state；result 可选 `test_summary`/`has_diff_patch`；`gate` 可选；`parseTaskDiff`。
- [x] client：`pauseTask` / `continueTask` / `confirmTask` / `rejectTask` / `getTaskDiff`。
- [x] task.js：测试面板、patch 折叠（点击再拉 `/diff`）、pause/continue/confirm/reject 按钮；委托+busy。
- [x] XSS：patch/test 名全部 `textContent`。
- [x] 跑 frontend 契约与 xss 测试绿。

### Task 5: 运维资产（不连 LIVE）

**Files:** `deploy/rotate-runner-credential.sh`, `deploy/hk-backup-drill.md`

- [x] 轮换脚本：本地 `openssl rand -hex 32`，打印 Hub JSON 键替换与机器文件 0600 步骤；`--apply` 默认关。
- [x] 演练文档：只读核对 bak 存在、tar 覆盖步骤、拨测矩阵、**禁止**未批准真切回。
- [x] 不在本 Task SSH 改生产。

### Task 6: 文档与清单

**Files:** `docs/HANDOFF.md` §六, `docs/architecture-v4-control-plane.md` Phase 3/4 标注, 本 plan checkbox

- [x] HANDOFF 把本计划项标成 **in-progress（工作树已实现；LIVE overlay = Task 7）**。
- [x] v4 控制面 Phase 3/4 每条标注 done/本轮代码。
- [x] 历史 2026-08-19 plans 保持 historical，不改 checkbox。

### Task 7: 发布（单独批准）

- [ ] pytest 全绿 → commit → push main → 等 CI artifact。
- [ ] 按 [[agent-fleet 部署与运维]] §0 overlay LIVE。
- [ ] HK 出站拨测：新路由无 operator=401；旧矩阵不变。
- [ ] Phase 4/5 实发 **另批**，不捆绑本发布。

## 验收完成定义

页面能：看有界日志、看测试摘要、展开脱敏 patch、点开 files、取消、暂停、继续、确认/拒绝、重试。  
Hub 能：挡未确认任务的 poll、paused 不派发、凭据按 SOP 可吊销。  
生产：仍手工 §0；不自动部署。
