# agent-fleet 架构 v4：观测链路 + 受控开发链路

本文是 v3 push-only 观测架构的扩展设计。它解决两个产品问题：

1. 页面可以查看某次开发任务的 agent 输出、变更和结果。
2. 页面可以创建、暂停、继续和重试开发任务。

运行时仍然不使用 SSH。SSH 只用于人工发布 hub、安装 probe/runner 和回滚 release。

> **2026-09-09 补齐状态**：观测 + 队列 + runner + files 已进 LIVE `182e034`。
> 未兑现的初始目标（脱敏 patch、测试摘要、pause/continue、人工确认、凭据轮换 SOP、备份演练）
> 以 [`docs/superpowers/specs/2026-09-09-v4-initial-goal-completion-design.md`](superpowers/specs/2026-09-09-v4-initial-goal-completion-design.md)
> 为准实施，计划 [`docs/superpowers/plans/2026-09-09-v4-initial-goal-completion.md`](superpowers/plans/2026-09-09-v4-initial-goal-completion.md)。

## 1. 两条链路

### 1.1 观测链路（已实现）

```text
agent machine
  -> local probe
  -> HTTPS POST /api/ingest
  -> HK hub: authenticate -> sanitize -> atomic state
  -> events/state
  -> dashboard GET /api/status
```

观测数据只包含在线状态、计数、系统指标和错误摘要。不会上传完整 session、prompt、文件路径、命令行、token 或私钥。

### 1.2 开发链路（新增设计）

```text
operator browser
  -> operator-authenticated HK API
  -> task queue: validate -> persist -> audit
  -> agent runner polls HTTPS /api/commands
  -> local task adapter + isolated project worktree
  -> runner uploads bounded logs, diff and artifacts
  -> HK task state/events
  -> dashboard task detail/review
```

这条链路是 agent 主动出站的 pull-based command plane，不是 hub 反向连接机器。浏览器永远不直接连接 agent，也不持有 agent token。

## 2. 核心对象和状态

### 2.1 Task

任务至少包含：

```json
{
  "task_id": "01J...",
  "machine": "worker-a",
  "agent_type": "codex",
  "project": "agent-fleet",
  "instruction": "修复并测试 ...",
  "requested_by": "operator-id",
  "created_at": "2026-08-18T...Z",
  "expires_at": "2026-08-18T...Z",
  "state": "queued"
}
```

`project` 必须来自 agent 本地配置的项目白名单，不能由页面传入任意路径。`instruction` 有长度上限并记录审计摘要。

任务状态：`queued -> leased -> running -> succeeded|failed|cancelled|expired`，另可 `queued|leased|running → paused`，`paused → queued`（新 attempt）。状态迁移必须幂等，重试/继续生成新的 `attempt_id`，不能复用旧 lease。

### 2.2 Lease

runner 领取任务时携带一次性 `nonce`，HK 发放短 TTL lease。runner 需要定期 heartbeat；超时后任务回到 `queued` 或进入 `expired`，由策略决定。所有领取、开始、结束、取消和失败都写入审计事件。

### 2.3 Result

结果按任务隔离保存，只允许以下类别：

- 有界 stdout/stderr 摘要（大小和行数上限）；
- `git diff --stat` 和经脱敏的 patch；
- 测试结果、退出码、耗时；
- 显式声明的构建产物引用（结果时附带的有界文件快照）。

默认不上传整个项目目录或完整 agent transcript。runner 在 result POST 附带 allowlisted 文本快照（拒 symlink/`..`、字节/行上限、脱敏）；Hub 落本地 `result_files`，operator `GET /api/tasks/<id>/files/<path>` 只读 Hub，不反向连 runner，并记录审计事件。

## 3. 服务端接口草案

以下接口必须使用独立的 operator 身份认证。不能复用 `X-Agent-Fleet-Token`：该 token 只用于机器 ingest。

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/api/tasks` | 查询任务列表，默认只返回摘要 |
| `POST` | `/api/tasks` | 创建任务，校验 machine/agent/project 白名单 |
| `GET` | `/api/tasks/<id>` | 查询任务状态、日志摘要、diff 摘要 |
| `POST` | `/api/tasks/<id>/cancel` | 请求取消，幂等 |
| `POST` | `/api/tasks/<id>/retry` | 创建新 attempt |
| `POST` | `/api/tasks/<id>/pause` | queued/leased/running → paused（本轮） |
| `POST` | `/api/tasks/<id>/continue` | paused → 新 attempt queued（本轮） |
| `POST` | `/api/tasks/<id>/confirm` | 人工确认闸放行 poll（本轮） |
| `POST` | `/api/tasks/<id>/reject` | 闸拒绝并 cancelled（本轮） |
| `GET` | `/api/tasks/<id>/diff` | 脱敏 patch 正文（本轮） |
| `GET` | `/api/tasks/<id>/events` | SSE 或轮询获取有界事件流 |
| `GET` | `/api/tasks/<id>/files` | 列出该任务 Hub-local 有界快照（path/bytes/truncated/redacted，无正文）。operator 认证。 |
| `GET` | `/api/tasks/<id>/files/<path>` | 读取脱敏快照正文。path allowlist、拒 `..`/symlink/密钥名；Hub 限速+审计。不反向连 runner。 |
| `POST` | `/api/commands/poll` | runner 领取自己的任务 |
| `POST` | `/api/commands/<id>/heartbeat` | runner lease heartbeat |
| `POST` | `/api/commands/<id>/result` | runner 回传结果、最终状态，以及可选有界 `files` 快照 |

`/api/commands/*` 必须校验 machine-bound runner credential、nonce、lease 和请求时间窗；失败默认拒绝。所有 API 响应使用 allowlist，禁止透传原始 collector 或进程数据。

## 4. 页面工作流

```text
Fleet
  -> 选择 machine
  -> 选择已注册 project
  -> 选择 agent 类型和 instruction
  -> 创建任务

Task detail
  -> queued/leased/running/paused 状态 + 可选确认闸
  -> 有界实时日志
  -> 测试摘要和脱敏 patch（折叠后拉 /diff）
  -> 取消 / 暂停 / 继续 / 确认 / 拒绝 / 重试
  -> 显式打开单个脱敏文件
```

页面保留现有 agent chain：`Local probe -> HTTPS ingest -> HK hub -> state -> dashboard`；开发任务详情在其旁边增加：`browser -> task queue -> runner -> result -> dashboard`。

## 5. Runner 约束

- 每个任务使用独立 worktree 或临时分支，避免污染长期工作目录。
- 只允许已注册的 agent adapter，不接受页面传来的任意可执行文件或 shell 字符串。
- 默认网络和文件访问遵循项目级策略；敏感目录、凭据目录和 token 文件永不返回。
- 日志、diff、文件内容均有字节、行数、路径深度和请求频率限制。
- runner 崩溃、断网或 lease 过期后，HK 可以安全重派任务；结果提交使用 `attempt_id` 去重。

## 6. 分阶段实现

### Phase 1：可见性

- 静态 dashboard、机器详情、agent chain、recent events API；
- 只读任务列表和任务详情模型；
- 390px/1440px 页面验收。

### Phase 2：队列和 runner

- HK 持久化 task/lease/audit；
- agent runner 的 HTTPS poll/heartbeat/result；
- 任务取消、TTL、幂等和重试。

### Phase 3：开发体验

- Codex/Claude/Hermes adapter；**done**
- 有界实时日志；**done**（SSE + log_summary）
- 测试结果、diff review；**代码本轮**（`test_summary` + 脱敏 patch + `/diff`）；LIVE 待 §0
- 按需文件查看和脱敏；**done（LIVE `182e034`）**
- 页面上的继续、重试和人工确认；重试 **done**；暂停/继续/确认/拒绝 **代码本轮**；LIVE 待 §0

### Phase 4：生产加固

- operator SSO/Cloudflare Access；**done**（边缘 Access；adoptions 另有 Nginx email 注入）
- runner credential 轮换和吊销；**SOP 本轮**（`deploy/rotate-runner-credential.sh`，默认不连生产）
- 审计保留策略、告警和备份恢复演练；审计随 SQLite；告警保持 stdout→cron（TG 仍延期）；备份演练 **清单本轮**（`deploy/hk-backup-drill.md`）
- 部署脚本和回滚验证；§0 手工 SOP **done**；main 自动部署仍禁止

## 6.5 会话数据面与 Supervisor 控制面（Task 12 扩展）

本设计上的「受控开发链路」脱胎于 commit 之后的 session 收口 + supervisor 控制面，
两者合起来构成一条新的**数据面 + 控制面**，独立于既有 ingest/observation。核心不变：

- pull-mediated：agent 轮询 `/api/supervisor/poll`（带 `X-Supervisor-Credential`），
  response 是 Ed25519 签名的 bounded 命令 JSON（canonical 序列化后签名），上传回执到
  `/api/supervisor/receipts`。hub 从不反向连接机器。
- 与 Phase 4 的「runner credential」区分：supervisor credential 只能领取/回执
  supervisor 命令，读不到任务、观测或会话数据。

受控命令的错误码是有界固定集（`allowed`/`unsupported_action`/`scope_mismatch`/
`malformed_signature`/`invalid_signature`/`invalid_public_key`/`malformed_timestamp`/
`clock_skew`/`expired`/`malformed_nonce`/`nonce_replay`/`malformed_command`）。
验收只比较这些有界码，不把自然语言输出当 pass。

### managed / unmanaged 边界

- 定义：`process_group_id` 存在 → managed（Supervisor 可治理，会话捕获等级由 probe
  能力声明决定）；缺省/不可用 → unmanaged，捕获等级一律收敛为 `best_effort`，
  `control_capability` 为 `unavailable`。
- 会话 bridge 只读取能力标志（`native_transcript`/`structured_stream` 等），不臆测
  exact/transparent 声明；unmanaged 会话从不登出 Supervisor 控制命令。

### 数据面存储与时序

- 会话元数据（sessions 表）与转录（redacted + encrypted-at-rest raw）各自独立于
  既有 JSONL 观测 / 任务 SQLite 持久化；任何一个失效不影响另一个。
- spool 是 per-session 落盘事件管道：超过配额的结构化事件标记 `capture_blocked`
  （严禁静默丢弃），best-effort 事件可选丢弃且记 `gap_sequence`。
- 上传是回执游标（ack）驱动：崩溃/断网后从 ack 处重放，不多传不漏传；中断的
  尾部帧在加载时修复到最后一个完整帧。

## 6.6 纳管（adoption）与运行时 gate

三条新表面默认关闭，互相独立，显式 False 优先：

- `AGENT_FLEET_SESSION_REPOSITORIES_ENABLED`
- `AGENT_FLEET_SUPERVISOR_ENABLED`
- `AGENT_FLEET_ADOPTION_REPOSITORIES_ENABLED`

也可写 `<FLEET_HOME>/fleet-gates.conf` 的 `KEY=VALUE`（无对应 env 时生效）。正式发布顺序：**Session → Supervisor → Adoption**。Exact capture 不随 gate 打开。

Adoption 审计写入 session transcript。`make_app` **不会**在只开 adoption 时自动打开 session；该组合把 adoption 失败关闭，observe / task 仍启动。`create_app` 在 adoption-on 且无 transcript root 时仍 `RuntimeError`（直接装配契约）。控制路由需要 session **和** supervisor。

Operator 表面：`/api/adoptions`、`/api/adoptions/<session_id>/retry`、`/api/adoptions/<session_id>/capture-exact`、`POST /api/adoption/<session_id>/source/control`（五个固定动作）。ingest / runner / supervisor 凭据头不能当 operator。公开 JSON 不含 raw pid / path / cmdline。`detach` 不发信号；`terminate` 先做身份守卫。

## 7. 明确不做的事

- hub 通过 SSH、反向隧道或中央私钥执行 agent 命令；
- 浏览器直接访问机器本地端口；
- 上传所有 agent session 或整个项目目录；
- 把 ingest token、runner credential 或私钥嵌入前端资源；
- 用“任意 shell”接口替代 adapter 和项目白名单。
- 在未恢复公网 `/api/status` 之前打开 session / supervisor / adoption gate。

