# Agent-Fleet 受管会话与 Agent Supervisor 设计

> 状态：P0–P4 已落地（2026-09-08）；剩余 LLM classifier / Hermes 结构化 spawn / exact 生产升级见 HANDOFF §六 deferred-with-condition
> 版本：v5 session supervision
> 适用基线：v4 control plane + frontend/backend separation
> 范围：受管 Hermes/Codex/Claude 会话的内容采集、审计、策略判断和受限进程控制

## 1. 摘要

当前 agent-fleet 的 observation 平面只上传机器状态、进程状态、Agent 数量、负载、磁盘等 metadata。现有连接器读取 session 文件的大小、mtime 或目录元数据，但不能提供可靠的用户消息、Assistant 回复、tool call、tool result 或完整进程生命周期。

现有 Runner 控制面支持 Agent 主动 poll、heartbeat 和 result upload，但 task cancel 主要改变 Hub 的 task/lease 状态，不能可靠终止已经运行的远端 CLI 进程。对手工启动、未由 agent-fleet 管理的 CLI，也不能保证实时读取或可靠控制。

本设计新增两个 Agent-side 组件：

1. **Session Bridge**：从 CLI 的 native transcript、结构化事件、hooks 或受管 PTY 读取事件，在本地做 allowlist、脱敏、持久化、断点续传和重试。
2. **Agent Supervisor**：负责启动和跟踪受管 CLI，将 session、attempt、process group 绑定，并执行固定枚举的暂停、恢复、终止和隔离动作。

Hub 仍然不 SSH、不建立反向连接、不执行任意远程 shell。控制采用 Agent-initiated polling / pull-mediated control：Agent 主动 poll，Hub 返回签名、限时、作用域明确的受限控制命令，Agent-side Supervisor 在本地执行并回报结果。

强保证范围是由 Supervisor 启动的新会话。历史手工启动的非受管会话只能 best-effort 追读，不能在 API 或界面中显示为完整监管对象。

## 2. 设计决策

### 2.1 受管会话优先

新任务默认经过 Supervisor 启动。Supervisor 为每个会话创建不透明的 `session_id`、`attempt_id` 和本地 `process_group_id`，建立可验证的归属关系。

已有手工进程不自动 adoption。只有在未来增加显式、可验证、用户确认的 adoption 流程后，才可以改变其 managed 状态。没有归属证明的进程永远显示为非受管。

### 2.2 push-only 的重新定义

push-only 继续表示：

- Hub 不主动连接 Agent；
- Hub 不 SSH；
- Hub 不建立 reverse tunnel；
- Agent 的网络连接全部由 Agent 发起。

push-only 不再表示 Hub 永远不能表达控制意图。Agent 主动 poll 时，Hub 可以返回固定枚举的控制命令。命令的执行权始终在本地 Supervisor，Hub 不获得远程 shell 权限。

### 2.3 Hermes 能力边界

当前 Hermes 没有经过验证的统一非交互 spawn 入口、结构化 transcript API 或稳定的双向会话事件协议。因此 Hermes 在第一阶段只能作为观察型、best-effort session source：

- 可以读取已有日志或元数据；
- 可以在受控条件下做 PTY/日志追读；
- 不承诺结构化 user/assistant/tool 事件；
- 不作为第一阶段完整受管 spawn/resume 目标；
- 未经 capability probe 验证的接口不能写入设计或代码的 exact/structured 能力。

Claude Code 和 Codex CLI 的结构化能力同样必须通过运行时 capability probe 验证。文档只定义适配器边界，不把特定版本的未验证内部格式当成永久契约。

## 3. 保持不变的现有契约

以下契约是本设计的硬约束：

- `POST /api/ingest` 使用 `X-Agent-Fleet-Token`，属于 Observe/ingest 域；
- `/api/tasks/*` 和页面使用 Operator authentication；
- `/api/commands/*` 使用 `X-Runner-Credential`，属于 Runner 域；
- 三个认证域互斥，携带其他域 credential 时不能回退到开发 Operator 身份；
- Observation 仍然是 metadata-only，不把完整 transcript 放入 observation snapshot；
- `report_schema.py` 的出站和 Hub 入站 allowlist 继续有效；新增 observation 字段必须同时通过两侧 schema；
- `state/<machine>.jsonl`、`state/<machine>.json`、`state/events.jsonl` 和 `state/fleet.db` 的逻辑路径、格式、rotation 和 WAL 语义保持；
- malformed JSONL 行必须被跳过，单条持久化失败不能拖垮其他 observation；
- task 状态机保持 `queued -> leased -> running -> succeeded|failed|cancelled|expired`；
- lease、heartbeat、attempt fencing、retry、result 幂等和过期语义保持；
- Runner heartbeat 日志仍然有界，不被替换成完整 transcript；
- 旧 `/api/*` 是权威兼容表面，`/api/v1/*` 只做 adapter；
- 当前没有 `/api/v1/commands/*` Runner 表面，新设计不能误称它已经存在；
- `/api/stream` 的 SSE MIME、no-cache、`X-Accel-Buffering: no`、keepalive、replay 和代理超时契约保持；
- 同源部署不增加通配 CORS；
- frontend/backend 独立 release、backend-first 发布顺序和独立回滚保持；
- 后端回滚不能破坏旧 JSONL、SQLite schema、旧 API 或在途 lease；
- 前端 release 不携带 credential、状态快照、session raw 或环境文件；
- Hub 不执行 SSH、任意 shell、任意文件写入、任意 stdin 注入或 tmux 命令。

## 4. 被扩展的旧设计

| 旧条款 | 新定义 |
|---|---|
| push-only | 保留网络方向约束；新增 Agent 主动 poll 返回受限控制命令。 |
| 默认不上传完整 transcript | 仍适用于 metadata-only observation、非受管会话和未启用 session capture 的旧路径；受管会话使用独立 transcript 数据平面。 |
| Hub 只接收、存储和展示状态 | 扩展为 Hub 可存储受管 transcript、产生 policy signal、维护 control queue；实际进程控制仍由本地 Supervisor 执行。 |
| task cancel 只改变 Hub 状态 | 受管 attempt 可额外生成 `cancel_attempt`，等待 Agent receipt 后完成最终状态转换。 |
| Runner 上传有界日志 | 保持不变。Runner 日志和完整 session transcript 是两个不同的契约。 |

以下旧设计明确废止：

- 通过 Hub SSH 进入 Agent 执行探针或控制；
- 通过中心持有的 Agent 私钥执行命令；
- 用任意 shell 接口替代 project whitelist 和 adapter allowlist；
- 用 ps、tmux 截图、终端缓存或 session 文件 mtime/size 伪装完整监管；
- 把普通 Runner credential 自动升级为暂停、终止或隔离权限。

## 5. 会话分类和保证等级

### 5.1 受管会话

受管会话必须由 Supervisor 启动，或者经过未来明确定义的 adoption 流程并完成归属证明。受管会话必须具有：

- Supervisor-owned 标记；
- session、attempt、process group 绑定；
- 启动和退出记录；
- capture source 和 capability manifest；
- 本地 bounded spool；
- control command receipt；
- 可验证的 sequence、cursor 和 capture gap。

### 5.2 非受管会话

非受管会话包括手工启动进程、Supervisor 启动前存在的进程、无法确认归属的进程，以及只有旧文件可读的历史 session。

系统可以提供已落盘内容的 best-effort 预览和元数据，但不能保证实时性、完整性、顺序、tool call/tool result 完整性或进程控制能力。

API 和前端必须显示：

```text
managed: false
capture_quality: best_effort
control_capability: unavailable
```

### 5.3 Capture quality

每个 session 和每个 event 都必须声明：

- `exact`：来源是受管且对声明事件类型具有完整性证明的 native transcript、官方 hook 或 Supervisor-controlled PTY；
- `structured`：来源是通过 schema 校验的 CLI 结构化事件流或适配器事件；
- `best_effort`：来源是历史文件追读、非受管日志、stdout/stderr 或终端近似解析。

`exact` 不表示 CLI 所有内部状态都可见；它只表示声明的事件源没有已知丢失。PTY 的终端字节流可以是 exact terminal-output，但把终端字节解释成 user/assistant/tool 语义时最多是 structured 或 best_effort。

来源切换必须生成 `capture_quality_changed`。出现无法恢复的 gap 时，受管 session 必须降级为 `capture_degraded`，不能继续显示为完整审计。

## 6. 总体架构

```text
+------------------------- Agent machine --------------------------+
| CLI: Hermes / Codex / Claude                                     |
|          |                                                        |
|          v                                                        |
| Session Bridge: native / hook / structured / PTY adapters        |
|          |                                                        |
| Encrypted bounded local spool                                    |
|          |                                                        |
|          +--------- Agent主动 HTTPS POST session events --------+
|                                                                   |
| Agent Supervisor: launch / process group / local policy / poll    |
|          |                                                        |
|          +--------- Agent主动 HTTPS POST control receipts ------+
|                                                                   |
+-------------------------------------------------------------------+
                 ^                         |
                 | Agent主动 supervisor poll |
                 |                         v
+--------------------------- Hub ----------------------------------+
| Observe API | Runner API | Session ingest | Supervisor poll       |
| observation repository    | transcript repository | task service   |
| policy engine             | control queue | audit publisher       |
| Operator API / SSE / independent frontend                         |
+-------------------------------------------------------------------+
```

Hub 不主动向 Agent 建立网络连接。所有 session event、receipt 和 control poll 都由 Agent 发起。

## 7. Session Bridge

### 7.1 职责

Session Bridge 负责：

- 从已验证 source 读取事件；
- 将 source event 映射为统一 schema；
- 执行 allowlist 和脱敏；
- 分配 sequence；
- 写本地 spool；
- 上传、重试、ack 和 replay；
- 报告 capture quality 和 gap。

Session Bridge 不负责进程启动、Hub 命令执行、策略终止、task lease 修改或其他机器的资源访问。

### 7.2 Source 优先级

适配器按以下顺序选择：

1. CLI 官方结构化 transcript；
2. CLI 官方 hooks 或事件接口；
3. Supervisor 持有的 PTY；
4. Supervisor 可验证的 stdout/stderr；
5. 历史文件或非受管追读。

每个适配器必须发布 capability manifest，至少声明：

- agent family 和版本；
- `spawn`、`resume`、`native_transcript`、`structured_stream`、`hooks`、`pty`；
- 支持的 event kinds；
- 每种 event kind 的 capture quality；
- capability probe 的结果和时间；
- capability 失败时的降级行为。

### 7.3 Claude Code

Claude adapter 优先使用经过 capability probe 验证的 native transcript、stream-json 或官方 hooks。`--resume`、输入输出格式和事件字段均需在启动时探测。

未验证的版本差异不得标记为 exact。无法使用结构化 source 时可以回退到 Supervisor PTY，但语义解析只能标记为 structured 或 best_effort。

### 7.4 Codex CLI

Codex adapter 优先使用经过 capability probe 验证的 `exec --json` 或 rollout JSONL。`--resume` 选项不作为静态保证，必须由实际 CLI help/version probe 确认。

没有 hooks 或结构化结果时，adapter 不得推断 tool result。PTY 只能作为文本兜底。

### 7.5 Hermes

当前 Hermes adapter 只提供观察型能力：已有日志、session metadata 和可配置的 PTY/log 追读。没有经过验证的统一结构化 transcript 或非交互 spawn 协议之前，Hermes 不进入第一阶段完整 managed spawn/resume 矩阵。

## 8. 统一 Session Event Schema

事件 envelope：

```json
{
  "schema_version": 1,
  "event_id": "opaque-event-id",
  "stream_id": "opaque-stream-id",
  "machine_id": "bound-machine",
  "session_id": "opaque-session-id",
  "attempt_id": "opaque-attempt-id",
  "process_group_id": "opaque-process-group-id",
  "sequence": 42,
  "kind": "assistant_message",
  "capture_quality": "structured",
  "source": "native_transcript",
  "emitted_at": "RFC3339 timestamp",
  "payload": {},
  "redaction": {
    "state": "redacted",
    "uncertain": false,
    "rules": ["credential_pattern"]
  }
}
```

公开 ID 不得使用原始 PID、filesystem path、token、secret 或可猜测的 credential 派生值。

第一版固定 event kinds：

```text
session_start
session_metadata
user_message
assistant_message
tool_call
tool_result
process_spawn
process_exit
step_start
step_end
session_close
capture_quality_changed
capture_gap
policy_signal
supervisor_state_changed
```

未知字段必须被丢弃或拒绝，未知 event kind 不能进入公共 transcript API。原始 collector output 不得透传。

### 8.1 Payload 约束

`user_message` 和 `assistant_message` 使用有界文本字段，带 `is_complete`。`tool_call` 包含 allowlisted `tool_name`、opaque `call_id`、经脱敏 arguments 和 digest。`tool_result` 包含 call_id、状态、经脱敏结果和 digest。

`process_spawn` 只暴露 allowlisted process family、owner 和 group binding，不暴露完整 command、环境变量、工作目录、原始 PID 或 secret。

`capture_gap` 必须包含起止 sequence、原因和降级后的 quality。

## 9. Sequence、Cursor、Dedupe、Replay、Ack

- sequence 在单个 stream 内从 1 开始递增；
- sequence 在写 spool 前分配；
- 重试保留原 event_id 和 sequence；
- Hub 去重键为 `(machine_id, stream_id, sequence)`；
- Hub 只在 durable staging 或 transcript repository 成功写入后返回 ack；
- Agent 只有收到 `accepted_through` 后才能删除对应 spool segment；
- batch 最多 100 events、最大 256 KiB；
- malformed event 按 event 返回 reject，不得使合法 event 全部丢失；
- Hub 允许有限窗口内乱序，超出窗口返回 `sequence_gap`；
- Operator API 使用只读 cursor 和 bounded limit，不暴露文件偏移或 SQL。

推荐 ingest API：

```text
POST /api/session-events
POST /api/v1/session-events
```

成功响应：

```json
{
  "ok": true,
  "stream_id": "opaque-stream-id",
  "accepted_through": 42,
  "next_cursor": "opaque-cursor",
  "request_id": "opaque-request-id"
}
```

## 10. Local Spool 和离线行为

本地 spool 使用 append-only bounded segments：

- segment 有 schema version、header 和 checksum；
- 完成 segment 使用原子 rename；
- malformed segment 不阻塞其他 session；
- checkpoint 记录 last persisted sequence、last acknowledged sequence 和 native cursor；
- Hub 不可达时继续本地保存；
- 重连后从最后 acknowledged cursor 重放。

第一版硬上限：

- 单 event 脱敏后最大 64 KiB；
- 单 batch 最大 256 KiB；
- 单 batch 最多 100 events；
- 单 session spool 最大 32 MiB；
- 单机 spool 最大 128 MiB；
- Hub raw transcript 单 session 最大 256 MiB；
- raw retention 14 天；
- metadata retention 90 天。

best-effort 内容达到上限时，可以丢弃低优先级旧内容，但必须生成 capture gap。受管 exact event 不得静默丢弃；spool 满载时 Supervisor 按本地 policy 暂停 session，并将状态改为 `capture_blocked`。

## 11. Hub Transcript Repository

Transcript repository 与以下数据隔离：

- observation snapshot；
- `events.jsonl` 原始 payload；
- task SQLite 普通 task row；
- Runner bounded logs；
- frontend release；
- HTTP error response。

Repository 至少分为 session metadata index、redacted event stream、encrypted restricted raw stream、policy signal index 和 control audit index。

要求：

- raw stream 加密保存；
- 目录名和对象名使用不透明 ID；
- API 不返回本地路径、SQL 或原始异常；
- repository 有字节、事件数和 retention 上限；
- malformed record 只影响该 record；
- transcript storage 故障不能影响 observation ingest；
- raw 删除有审计，但审计不含被删除内容。

普通 Operator 读取 redacted transcript。restricted raw capability 需要额外授权、分页、持续脱敏和访问审计，不允许 repository dump。

## 12. Agent Supervisor

### 12.1 生命周期

Supervisor 负责：

- 启动 allowlisted CLI；
- 创建 process group；
- 绑定 session、attempt 和 group；
- 记录启动、暂停、恢复、终止、退出；
- 恢复 durable manifest；
- 捕获 control receipt。

Supervisor 不自动接管没有归属证明的已有进程。

### 12.2 Linux

优先使用 process group 和 cgroup v2。cgroup 不可用时至少使用 process group，并把 capability 标为 `process_group_only`。对 double-fork 或逃逸子进程必须降低控制保证。

### 12.3 macOS

使用 Supervisor 创建的 process group 和 `killpg`。网络隔离只有在预配置 helper 可用时才报告为 supported。macOS 不把 tmux 当作控制权威。

### 12.4 固定控制动作

只允许：

```text
pause_session
resume_session
terminate_session
quarantine_session
cancel_attempt
```

禁止：

```text
raw_cmd
exec_shell
write_file
inject_stdin
tmux_command
arbitrary_signal
change_credential
change_policy
```

`terminate_session` 先执行有限 grace period 的可处理终止，再执行强制组级终止。只有确认目标组已经退出或明确为 `already_finished` 才能返回成功。

## 13. Agent-Initiated Pull-Mediated Control

Supervisor 使用独立 API 和 credential：

```text
POST /api/supervisor/poll
POST /api/supervisor/receipts
```

Supervisor credential 不能读取 transcript，也不能控制其他机器。

命令 envelope 至少包含：

```json
{
  "command_id": "opaque-command-id",
  "action": "pause_session",
  "target": {
    "machine_id": "bound-machine",
    "session_id": "opaque-session-id",
    "attempt_id": "opaque-attempt-id"
  },
  "issued_at": "RFC3339 timestamp",
  "expires_at": "RFC3339 timestamp",
  "nonce": "opaque-nonce",
  "reason_code": "operator_requested",
  "signature": "detached-signature"
}
```

Agent 固定保存 Hub public key，并验证 canonical JSON signature。命令必须满足：

- machine scope 正确；
- session/attempt 属于本机 Supervisor manifest；
- nonce 未使用；
- command 未过期；
- action 在本地 capability 中；
- command_id 未执行或重复执行时返回原 receipt。

控制状态：

```text
queued
 delivered
accepted
rejected
executing
succeeded
already_finished
failed
expired
```

Hub 返回“已入队”不等于执行成功。最终状态以 Supervisor receipt 为准。

冲突优先级：`terminate_session` 和 `quarantine_session` 高于 `resume_session`。旧 resume 不能撤销更高优先级的 quarantine。

## 14. 权限边界

### Observe

可上传 metadata observation 和经过 allowlist 的 session event batch；不能读取 transcript、创建 control 或 poll Supervisor command。

### Operator

可在 scope 内读取 redacted transcript、policy signal、session metadata，并请求固定 control action。restricted raw 和 destructive control 需要独立 capability。

### Runner

只可 poll task、heartbeat、上传 result。不能读取 transcript，不能暂停、终止或隔离 session。

### Supervisor

只可 poll 属于本机的固定命令、上传 receipt 和恢复 cursor。不能读取 transcript、改变 policy 或请求任意 command。

### Policy engine

可以读取被授权的 redacted event，产生 policy signal 和受限控制建议，但不能绕过 Hub control queue 直接连接 Agent。

## 15. 脱敏和 Secret Handling

脱敏在 Agent 首次采集、Hub durable ingest 前和 Operator API 返回前至少执行三次。规则至少覆盖：

- access token、API key、bearer token；
- password 字段；
- private key block；
- cookie、webhook secret；
- 数据库连接字符串；
- credential 文件内容；
- 环境变量值；
- filesystem path；
- 内部网络地址；
- 机器认证信息。

敏感值替换为固定占位符，例如 `[REDACTED:credential]`。原始值不得进入普通日志、SSE、task event、metrics label、request_id、frontend HTML 或错误响应。

raw transcript 只保存在加密 restricted repository。即使 restricted Operator 读取，也继续执行 secret/path redaction、分页和审计，不提供 repository dump。

无法确定是否敏感时，设置 `redaction.uncertain=true`，降低公开质量并产生 policy signal，而不是把原文写入普通 DTO。

## 16. Policy Engine 和告警

确定性规则优先处理：

- credential pattern；
- 未授权工具调用；
- 超出 project scope 的文件行为；
- 非 allowlisted executor；
- Supervisor 归属丢失；
- capture gap；
- control target 不一致。

LLM classifier 只能辅助告警、分类和提出建议，不能成为唯一的自动 terminate 依据。自动不可逆控制必须绑定确定性规则、policy version、目标 session、事件证据和本地 Supervisor capability。

Hub 不可达时，本地 Supervisor 继续执行最小确定性安全规则，例如归属丢失、spool 满载、非法 executor 和过期命令。

## 17. Task、Attempt、Lease、Result 关系

模型保持：

```text
task -> attempt -> session -> process_group
```

受管 task 启动时必须绑定 task_id、attempt_id、project scope、adapter name 和 policy bundle version。Standalone session 可以没有 task_id，但不能没有 session_id 和 process_group_id。

Task cancel 流程：

```text
Operator cancel
  -> Hub 标记 cancel_requested
  -> Hub 写入 cancel_attempt control command
  <- Agent 主动 supervisor poll
  -> Supervisor 验证并终止目标 process group
  -> Agent 上传 receipt
  -> Hub 完成 attempt 最终状态
  -> stale Runner result 被 attempt fencing 拒绝
```

自然完成与 cancel 竞态继续由现有 attempt fencing 和 task 状态机决定。旧 lease heartbeat、过期 lease、retry 和 result 幂等语义不变。

## 18. API 和 SSE

新增建议接口：

```text
POST /api/session-events
POST /api/v1/session-events
GET  /api/sessions
GET  /api/sessions/<session_id>
GET  /api/sessions/<session_id>/events
GET  /api/sessions/<session_id>/policy-signals
POST /api/sessions/<session_id>/control
POST /api/supervisor/poll
POST /api/supervisor/receipts
```

新接口使用统一 bounded error：

```json
{
  "ok": false,
  "error": "invalid_event",
  "detail": "request could not be accepted",
  "request_id": "opaque-request-id"
}
```

禁止返回 raw exception、traceback、SQL、filesystem path、credential、原始 collector output 或未经脱敏的 CLI 内容。

SSE 继续使用现有 `/api/stream` 契约。新增事件只发送 session_id、sequence/cursor、kind、quality、metadata 和安全摘要；前端需要全文时再通过 Operator-authenticated bounded API 拉取，不在 SSE 中发送 raw transcript。

前端新增 session timeline 时必须独立于 backend release，使用 allowlisted DTO、bounded rendering 和明确的 pending/expired/failed/already_finished 状态。best_effort 不能显示为 exact，queued 不能显示为 executed。

## 19. 迁移阶段

### P0：Capability Probe

在真实 Hermes、Codex、Claude 环境验证 native transcript、structured stream、hooks、PTY、resume 和 process control 能力。建立 adapter capability manifest 和 fixture。P0 不改变生产控制行为。

### P1：Session Bridge、Spool、Repository

增加 schema、redaction、本地 spool、cursor、ack、dedupe、replay 和 Hub transcript repository。先以 shadow mode 运行，不启用远程控制，不改变旧 observation、Runner 和 task 行为。

### P2：Supervisor 启动新任务

新 task attempt 默认经过 Supervisor，建立 process group 和 session/attempt binding。Supervisor 启动失败时必须明确失败，不能静默回退到无监管 CLI。历史手工进程仍为 best-effort。

### P3：Pull-Mediated Control

增加独立 Supervisor credential、命令签名、nonce、TTL、scope、poll、receipt 和 audit。先 dry-run，再 pause/resume，再 terminate，最后开启 quarantine。task cancel 在此阶段接入 `cancel_attempt`。

### P4：Operator UI 和 Policy

新增 session timeline、redacted transcript、tool call/result、capture gap、policy signal、control receipt、restricted raw capability 和 SSE 增量刷新。LLM classifier 仅在确定性 policy 和审计链路稳定后接入。

## 20. 回滚

### Hub backend rollback

- transcript repository 与旧 observation/event/task storage 分离；
- 新 schema 只做 additive migration；
- 不删除旧 JSONL、SQLite 表、lease 或 API；
- 未 ack 的 Agent spool 保留；
- 旧 Hub 可以停止提供新 transcript API，但不能破坏旧 observation/task/runner。

### Agent rollback

- 停止启动新的 managed session；
- 不向旧 Supervisor 发放不认识的命令；
- 保留未上传 spool；
- 旧 Agent 继续 metadata observation；
- 恢复新版本后继续 replay。

### Frontend rollback

前端回滚不能影响 transcript repository、control queue、task DB 或 observation state。即使旧前端没有 session 页面，后端的 API、审计和控制安全边界仍然有效。

## 21. 测试和故障演练

必须覆盖：

- schema allowlist、超大 event、malformed event、sequence gap、dedupe、replay、ack；
- Hermes/Codex/Claude adapter 的 source probe、版本差异、structured parse、native tail、resume downgrade；
- spool fsync、crash recovery、rotation、retention、disk full、Hub unreachable；
- Linux process group/cgroup、macOS process group、子进程回收、escape detection；
- command signature、nonce replay、TTL、scope mismatch、幂等 receipt、冲突优先级；
- Observe/Operator/Runner/Supervisor auth isolation；
- task cancel、自然完成竞态、stale result fencing、lease reconciliation；
- API bounded errors、`/api/v1` adapter、旧 `/api/*`、SSE 首帧和断线重连；
- redaction 对 token、password、private key、path、环境变量和不确定敏感值的处理；
- Hub storage failure 不影响 observation；
- frontend/backend 独立发布、backend-first 和回滚；
- policy engine 不可用、本地 policy 生效、LLM classifier 不成为唯一 terminate 依据。

故障演练至少包括 Hub 不可达、repository 只读、spool 满载、Agent 在 ack 前崩溃、Supervisor 在 terminate 中崩溃、时钟偏差、signing key rotation、CLI source 断开、子进程逃逸和前端/后端回滚。

## 22. 完成定义

只有满足以下条件才可称为“受管会话监管完成”：

1. 新 Supervisor session 有 session、attempt、process group 绑定；
2. 支持范围内的 user、assistant、tool call、tool result 能通过 bounded API 查询；
3. 每个事件有 sequence、cursor、source 和 capture quality；
4. 重试不重复，malformed event 不拖垮合法事件；
5. gap 可见，quality 会真实降级；
6. Hub 不主动连接 Agent；
7. control command 固定枚举、签名、限时、带 scope 和 nonce；
8. Supervisor 只控制自己管理的 process group；
9. task cancel 最终得到明确 receipt；
10. Runner credential 无法控制 session；
11. raw transcript 与 observation/task/runner 隔离；
12. secret/path/credential 不进入普通 DTO、日志、SSE 或错误响应；
13. 旧 observation、task、lease、runner、SSE、`/api/*` 和 `/api/v1` 不回归；
14. frontend/backend 可以独立发布和回滚；
15. Hermes、Codex、Claude 的能力只按真实 probe 结果声明；
16. 非受管会话明确为 best-effort，不能被显示成完整监管。

## 23. 统筹与任务分派规则

本项目采用“主控统筹、子代理实现”的工作方式：

- 主控只负责拆解任务、确认依赖、维护接口契约、安排评审、合并结果和运行集成验收；
- 子代理按实施计划逐任务实现，不跨任务修改无关模块；
- 同一阶段共享接口先由契约任务落地，再分派依赖实现；
- 每个实现任务必须有独立测试和回滚边界；
- 每个子代理完成后必须经过 spec compliance review 和 code quality review；
- 未经过 capability probe 的 CLI 行为不能被实现任务写成硬编码保证；
- 任何涉及生产发布、credential、权限扩大或不可逆控制的任务必须单独设置验收门。
