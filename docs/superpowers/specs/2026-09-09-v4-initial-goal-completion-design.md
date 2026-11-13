# agent-fleet v4 初始目标补齐设计

> 日期：2026-09-09
> 状态：已确认实施依据（补齐，不重开延期项）
> 权威现状：`docs/HANDOFF.md` §六 + LIVE `182e034`
> 原始目标：`docs/architecture-v4-control-plane.md` + `docs/superpowers/specs/2026-08-19-v4-optimization-design.md`

## 1. 背景

2026-09-08 把能力清单收成 `done` / `deferred-with-condition`。那次收口把
**v4 控制面原文里尚未兑现的产品面**也标成了完成或延期，导致「开发 TODO 收口」
和「初始目标做完」不是同一件事。

2026-09-09 LIVE 已切 `182e034`：files 读面进了公网。其余 v4 原文缺口仍在。

本设计只补 **v4 初始目标未完成项**。下列项保持延期，不在本轮：

- WebSocket（SSE+10s 轮询仍够）
- frontend-v2 替换生产 SPA
- main 自动部署（禁止拷 `arch-v2-primary` `deploy.yml`）
- 密钥下发 / LLM 自动 terminate / Hermes 结构化 spawn
- exact capture 默认开 / TG 直推 / 更多采集器

## 2. 原始目标 vs 现网

产品原句（v4 控制面 §0 / §4 / §6）：

1. 页面可以查看某次开发任务的 agent 输出、变更和结果。
2. 页面可以创建、暂停、继续和重试开发任务。

| 原文承诺 | 现网 `182e034` | 本轮 |
|---|---|---|
| 有界实时日志 | SSE `task_log` + `log_summary` | 保持 |
| `git diff --stat` | `results.diff_stat` ≤5KB | 保持 |
| 经脱敏的 patch | **无**（只有 stat） | **做** |
| 测试结果 | 只有 `exit_code` | **做** 有界 JUnit/摘要 |
| 按需文件查看 | Hub-local files API LIVE | 保持 |
| 取消 / 重试 | 有 | 保持 |
| 暂停 / 继续 | **无** task 动作（会话页有 pause_session，不是任务） | **做** |
| 人工确认 | **无** | **做** gate |
| operator SSO / CF Access | adoptions 有；任务页靠 CF Access 边缘 | 保持；补任务路径文档 |
| runner credential 轮换/吊销 | 每机独立文件，**无 SOP/API** | **做** |
| 审计保留 / 告警 / 备份演练 | bak 目录有，无演练记录 | **做** SOP+脚本，不做新告警通道 |
| 部署脚本和回滚验证 | §0 手工 SOP 已跑通 | **做** 只读验证脚本扩展，不自动部署 |
| Phase 4/5 薄客户端实发 | 闸开、未向座位下发 | **做** 一次受控实发验收（可回滚） |

「暂停」在原始 §0 出现，Phase 3 正文写的是「继续、重试和人工确认」。本轮把
暂停定义为 **对 queued/leased/running 任务发 cancel 语义的 pause**：停发/停跑，
不丢 instruction；`continue` 从暂停处重新入队同一 instruction，新 `attempt_id`。
不把会话 `pause_session` 复用到任务。

## 3. 约束（不可破）

- push-only：hub 不反向连 runner；files / patch / tests 都随 result 上传或已在 Hub。
- 三认证域不串：ingest / operator / runner。
- 有界：patch ≤100KB、测试摘要 ≤20KB、路径 allowlist、拒 `..`/symlink/密钥名、脱敏。
- 不把完整 agent transcript 或整个项目当结果。
- LIVE = bind-mount，切 LIVE 禁止 `ln -sfn`；发布仍走 CI artifact + 部署笔记 §0。
- 前端真相源 = `frontend/` tracked SPA；不碰 `frontend-v2/`。
- schema 变更必须可回滚：新列/新表可空或独立；旧 web 读新库不得 500。

## 4. 数据模型增量

现有 `results`：`exit_code, log_summary, diff_stat, duration_s, finished_at`。
`result_files` 已有。本轮只加可空字段和新表，不改旧列语义。

```sql
-- results 新增可空列（旧行 NULL = 无 patch / 无测试）
ALTER TABLE results ADD COLUMN diff_patch TEXT;          -- 脱敏 unified diff，≤100KB
ALTER TABLE results ADD COLUMN test_summary TEXT;        -- 有界测试摘要 JSON/文本，≤20KB

-- 任务门闩：人工确认。无行 = 不需要确认（旧任务行为不变）
CREATE TABLE IF NOT EXISTS task_gates (
  task_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('confirm_before_dispatch')),
  state TEXT NOT NULL CHECK(state IN ('pending','confirmed','rejected')),
  requested_by TEXT NOT NULL,
  decided_by TEXT,
  decided_at TEXT,
  created_at TEXT NOT NULL
);

-- 暂停：queued/leased/running → paused；continue 生成新 attempt 回 queued
-- tasks.state CHECK 扩展加入 'paused'
```

`test_summary` 允许的键（allowlist，丢弃未知）：

```json
{
  "framework": "pytest|unittest|unknown",
  "passed": 0,
  "failed": 0,
  "skipped": 0,
  "errors": 0,
  "duration_s": 0.0,
  "failed_names": ["test_foo"]
}
```

`failed_names` ≤20 条，每条 ≤200 字符。没有测试跑过时字段为 NULL，页面显示「无测试结果」。

## 5. API 增量

全部 operator 域，CF Access / `--dev-operator`。错误格式不变。

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/api/tasks/<id>/pause` | `queued\|leased\|running` → `paused`。已终态 409。幂等：已 paused 返回当前行。 |
| `POST` | `/api/tasks/<id>/continue` | `paused` → 新 `attempt_id` 的 `queued`。其它状态 409。 |
| `POST` | `/api/tasks/<id>/confirm` | gate `pending` → `confirmed`，此后 runner 才可 poll 到该任务。无 gate 时 404。 |
| `POST` | `/api/tasks/<id>/reject` | gate `pending` → `rejected`，任务 `cancelled`。 |
| `GET` | `/api/tasks/<id>` | DTO 增可选 `diff_patch` 摘要（截断标志）、`test_summary`、`gate`。patch 正文不进列表 DTO。 |
| `GET` | `/api/tasks/<id>/diff` | 脱敏 patch 正文。operator + 限速 + 审计。无 patch=404。 |

创建任务可选 `{"confirm": true}`：先写 gate=pending，state 仍 `queued` 但对 poll 不可见，直到 confirm。

runner `POST /api/commands/<id>/result` 增可选 `diff_patch`、`test_summary`。缺省兼容旧 runner。

## 6. Runner 增量

- `tools/worktree.py`：现有 `diff_stat` 外新增 `diff_patch(worktree, max_bytes=102400)`，
  `git diff --binary --no-color` 后走与 files 相同的脱敏（token/私钥/绝对 home 路径）。
- `tools/agent-runner.py`：result 附 `diff_patch`；若 worktree 内存在 pytest/unittest
  产物或 adapter 约定的 `test-results.json`，解析为 `test_summary`，否则 NULL。
  **不**为了本轮强行跑测试——只采集 adapter/worktree 已产生的结果。
- pause：hub 将任务标 paused 后，下次 heartbeat 返回 409/cancelled 语义，runner 停跑并 cleanup worktree（与 cancel 相同）。continue 是新 attempt，runner 无特殊协议。

## 7. 页面

`frontend/views/task.js`（tracked SPA，不改 frontend-v2）：

- 结果区：现有 log / diff_stat / files 之外加 **测试摘要面板** 和 **patch 折叠面板**
  （默认折叠，点开再 `GET /diff`，textContent 渲染，≤100KB 截断提示）。
- 按钮：cancel / retry 保留；运行中加 **暂停**；paused 加 **继续**；
  gate=pending 加 **确认 / 拒绝**，此时不显示派发中。
- 忙栅栏与事件委托沿用 files 修复，禁止每次重绘重复绑点击。

## 8. 生产加固（v4 Phase 4 原文）

不做新服务。补资产与一次演练：

1. **runner credential 轮换/吊销**
   - Hub：`credentials/runner-credentials.json` 支持每机多版本？不。保持单 secret。
   - SOP：生成新 secret → 写入 Hub JSON 与机器 `runner-credential`（0600）→ 旧 secret 立即失效。
   - 吊销 = 从 JSON 删该机键；poll 变 403。
   - 脚本：`deploy/rotate-runner-credential.sh`（本地生成+打印 scp 步骤，不连生产）。
2. **审计保留**
   - 现有 `audit` 表不删。文档写清：随 `state/fleet.db` 走 LIVE bak；无单独 TTL job。
   - 本轮不加自动 purge（避免误删）。
3. **备份恢复演练**
   - 在 HK 只读副本验证：`cp -a LIVE bak` 已存在；补 `deploy/hk-backup-drill.md` 清单：
     停 web → 从 bak tar 覆盖 bind-mount → TERM → 拨测 `/api/status` 与一任务详情。
   - 2026-09-09 已有 bak `agent-fleet.bak-20260909-pre-182e034` 可作演练源，**不**在生产真切回除非 operator 批准。
4. **告警**
   - 保持现有 stdout→cron。本轮不接 TG。文档写清「v4 告警 = stale 机器 + 任务 failed 事件进 SSE」，不是 pager。
5. **Phase 4/5 实发**
   - 对 **一台** 已 adopted 的一台 claude_code 座位发一次只读 `append_user_turn`（instruction 为无副作用 ping），确认 native resume 或有界拒绝码。
   - 失败码只认固定集。可立刻 TERM 无副作用。需 operator 在场。

## 9. 明确不做

- 完整 IDE diff review（三窗、注释、行级 accept）
- 任意测试框架自适应；只认 pytest/unittest 有界摘要
- 任务中途改 instruction（continue 用原 instruction）
- 自动部署、WebSocket、密钥下发、frontend-v2
- hub 反向拉取 runner 磁盘

## 10. 验收

- 单测：schema 迁移、pause/continue 状态机、confirm gate 挡 poll、patch 脱敏截断、test_summary allowlist、旧 runner 不传新字段仍能 complete。
- 前端契约：task.js 有 pause/continue/confirm；diff 面板不 `innerHTML`。
- 本地 `deploy/e2e-smoke.sh`：假 adapter 回 patch+test_summary → 页面 API 能读。
- LIVE：§0 发布后 HK 出站拨测 files 仍 401；新路由无 operator=401；features 双 true；不实发控制命令除非 §8.5 获批。

## 11. 回滚

- 代码：回上一 CI artifact tar 覆盖 LIVE。新列留在 SQLite 无害。
- gate：未 confirm 的任务保持 queued 但对旧 web 仍可见——迁移须兼容旧 web 忽略未知列。
- pause：旧 web 不认识 `paused` 时列表仍显示该 state 字符串；旧 runner 不会 poll 到 paused（hub 过滤）。
