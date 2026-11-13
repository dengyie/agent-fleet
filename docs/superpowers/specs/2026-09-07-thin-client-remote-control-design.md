# Agent-Fleet 路线 A：薄客户端远程控制台（v6）

> 状态：Phase 1–5 已接线；代码缺省仍关；**2026-09-08 LIVE 已开 Phase 4/5 gates**  
> 版本：v6 thin-client remote console  
> 日期：2026-09-07（2026-09-08 对齐：native resume 已接线；LIVE 已写 `fleet-gates.conf` ）  
> 适用基线：v3 push-only + v4 control plane + v5 session supervision + discovery/adoption  
> 产品路线：**路线 A — 浏览器是唯一控制台，agent 继续在远程机器上干活**  
> 范围裁定：前期控制台框架已接线；不改 agent 内部；不写云端 VM / 云端浏览器。Phase 4/5 已进枚举；composer 永远不是 `inject_stdin`，profile 只翻本机 `is_current`。Phase 4 节点执行器只在私有 native 身份（cwd/env/token/exe）齐全时走 `--resume`；无身份仍拒绝 `create(argv, None, {})` sibling。LIVE 开关不等于已向座位发过控制命令。
> 2026-09-07 负责人确认：§14 五条全部批准；会话热路径**透传 agent 输入输出**，不做自建脱敏。

本文是对以下文档的**增量**，不推翻任何既有硬约束：

- `docs/architecture-v3.md`
- `docs/architecture-v4-control-plane.md`
- `docs/superpowers/specs/2026-08-26-agent-session-supervision-design.md`
- `docs/superpowers/specs/2026-08-28-agent-profile-registry-design.md`
- `docs/superpowers/specs/2026-08-29-agent-instance-discovery-adoption-design.md`

---

## 0. 一句话

把现有「舰队观测 + 任务队列 + 有界会话 + 纳管控制」收成一个网页控制台：operator 在浏览器里看到远程机器上的 agent 会话、对已纳管会话做固定动作的控制、用受控任务开新会话。agent 的推理、工具、文件系统仍在那台机器上完成；hub 不反向连接、不 SSH、不注入任意 stdin、不改 agent 内部。

这不是 ChatGPT Agent（云端 VM + 云端浏览器）。这是「ChatGPT 网页那种薄客户端体验」套在**已经存在的远程 CLI** 上。

```text
operator browser  ──HTTPS──►  HK hub（认证 / 队列 / 有界 transcript / SSE）
                                      ▲
                                      │ 机器主动出站
                          probe ingest + session-events
                          runner poll  /api/commands
                          supervisor poll /api/supervisor
                                      │
                              远程机器上的 Codex / Claude / Hermes / pi
```

---

## 1. 已拍板的产品决策

| 决策 | 选择 | 含义 |
|---|---|---|
| 产品路线 | **A：薄客户端 + 远程机器继续干活** | 浏览器从不直连 agent；执行面仍是节点上的现成 CLI |
| 前期目标 | **可控控制台框架** | 看会话、管生命周期、用白名单任务开新会话 |
| agent 内部 | **不改** | 不碰 Codex/Claude/Hermes/pi 源码、协议、模型实现 |
| 对话输入 | **Phase 4 代码缺省关；LIVE 2026-09-08 已开** | 禁止 `inject_stdin`；无 native 身份拒绝 sibling；有 cwd/env/token/exe 才 `--resume` |
| 配置切换 | **Phase 5 代码缺省关；LIVE 2026-09-08 已开** | hub 只下发本机已登记的 opaque `profile_id`；节点只翻 `is_current`，不下发密钥；不热切正在跑的进程 |
| 云端电脑 | **不做** | 无 worker pool、无云端浏览器、无 takeover 投屏 |
| 网络方向 | **push-only 不变** | hub 不 SSH、不反向隧道、不下发任意 shell |
| 会话内容 | **透传 agent I/O** | 热路径默认不做自建脱敏；schema allowlist + 大小上限仍在；observation 快照仍不含对话 |
| 前期完成线 | **Phase 2** | 能看透传时间线 + 对 adopted 会话发五动作；Phase 3 深链随后 |

---

## 2. 目标与非目标

### 2.1 前期目标（本期框架）

1. **一个控制台**：Fleet / 机器 / 会话 / 任务 四页组成唯一 operator 入口；会话不再是「藏在机器页后面的只读时间线」。
2. **看见远程会话**：已上报的 bounded 事件按 schema allowlist **透传** user/assistant/tool 文本，供后续系统开发；纳管座位显示 managed / capture_quality / control_capability。
3. **控制已纳管会话**：网页对 `adopted` 座位发出既有五个固定动作（pause / resume / terminate / quarantine / cancel_attempt），走已有签名 supervisor 通道；页面显示的是「已入队」，终态以 receipt 为准。
4. **用任务开新会话**：机器页创建任务仍是「新开一轮受控工作」的唯一入口；成功后任务页与对应 session 页可互相跳转（有 `session_id` 才跳）。
5. **控制面可扩展、代码缺省关**：`append_user_turn` / `apply_local_profile` 已加入 `CONTROL_ACTIONS`（现 9 个），签发靠独立 env/conf 开关。**2026-09-08 LIVE `fleet-gates.conf` 已开**。节点 `append_user_turn` 无私有 native 身份时必须返回 `unsupported_action` / `resume_unverified`，**禁止** `create(argv, None, {})` sibling；身份齐全时走 `--resume` + 原 cwd/env + DEVNULL，不得 `--ephemeral` / SIGCONT。

### 2.2 明确不做（前期 + 长期红线）

前期不做：

- 网页 composer 往正在跑的 CLI 打下一句（follow-up / `inject_stdin`）
- 自建脱敏（替换 token/路径/密码为 `[REDACTED:]`）；`Redactor` 模块保留，热路径默认 `passthrough=True`。observation `/api/status` 仍 metadata-only。exact capture / AEAD raw 通道仍独立，不随本期打开
- 在 hub 侧切换远程机器的模型、provider、API key（cc-switch 后期）
- 云端 VM、云端浏览器、远程桌面 takeover
- 改任何 agent 发行版的内部协议或源码
- WebSocket 替换 SSE
- WebSocket 替换 SSE（仍 deferred）

长期禁止（继承 v5 §12.4 / `hub/domain/control.py`）：

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

`change_credential` / `change_policy` 继续禁止，即使后期做 cc-switch：**配置切换不得变成密钥下发**。后期若做，也只能触发节点本地已允许的 profile 名（见 §9）。

### 2.3 保证等级（沿用 v5，不放松）

| 对象 | 网页能看 | 网页能控 | 说明 |
|---|---|---|---|
| 未发现实例 | 否 | 否 | 机器未装 probe 或不在线 |
| 已发现、未纳管 | 元数据（家族、是否 attachable） | 否 | 只能点「纳管」 |
| 纳管中 `pending` | 座位状态 | 否（可 retry adopt） | 等探针回执 |
| 纳管 `adopted` | bounded 时间线 | 五个固定动作 | 控制能力仍取决于 `control_capability` |
| 非受管 / best_effort | 有缺口的预览 | 否 | 必须显示降级，不得画成完整监管 |
| Hermes | 观察型预览 | 不承诺 spawn/resume | 沿用 v5：Hermes 第一阶段 observation-only |
| 已撤销 `revoked` | 历史摘要 | 否 | detach 不发信号 |

---

## 3. 保持不变的契约

以下与 v4 §7 / v5 §3 完全一致，本期框架不得破坏：

- 三认证域互斥：ingest token / runner credential / supervisor credential；浏览器只走 operator（Cloudflare Access）
- observation 仍是 metadata-only；完整对话不进 `/api/status` 快照
- `report_schema.py` / `session_schema.py` 双侧 allowlist
- JSONL observation、`state/fleet.db`、session/adoption 隔离库路径与 WAL 语义
- task 状态机 `queued → leased → running → succeeded|failed|cancelled|expired`
- supervisor 命令：Ed25519 签名、TTL、nonce、machine/session/attempt scope、receipt 才是终态
- 前端独立静态 release、backend-first、无通配 CORS、SSE 契约不变
- 前端不携带任何凭据；`client.js` 仍是唯一 HTTP 入口
- 公开控制/会话 **元数据** DTO 永不含 nonce / signature / token 头；会话 **事件 payload** 按 `session_schema` allowlist 透传 agent 文本（含工具参数/结果）
- observation `/api/status` `/api/events` 仍 metadata-only，不带对话
- 实例发现行仍可含 pid 等 metadata（既有 discovery 契约），不得进入 control 请求体
- feature gate 顺序仍是 Session → Supervisor → Adoption；新控制台不得在 gate 关闭时暴露动作按钮

---

## 4. 现状缺口（框架要补的，不是新发明）

代码里已经有管道，缺的是**控制台产品层**把它们接上：

| 已有 | 缺口 |
|---|---|
| `GET /api/sessions`、`/events`、`/policy-signals` | 前端 `client.js` 有 session 读 API，但 Fleet 页没有会话列表入口 |
| `POST /api/adoption/<id>/source/control`（五动作） | `frontend/api/client.js` **没有** control / adopt / revoke 方法 |
| `frontend/views/session.js` 只读时间线 | 没有控制按钮、没有 pending/receipt 状态、没有「不可控」空态 |
| `frontend/views/machine.js` 可建任务 | 任务成功后不保证跳到对应 session；发现实例的纳管入口不完整 |
| `CONTROL_ACTIONS` 含 adopt/detach + 五动作 | 前端契约 `CONTROL_RECEIPT_FIELDS` 已有，但视图没用 |
| supervisor 无签名 key → issuance fail-closed | 控制台必须把 `control_failed` / 无 key 显示成有界错误，不许假装已执行 |
| 多数节点未装 probe | 框架不依赖全节点在线；离线机器只读、不可控 |

本期框架 = **接线 + 页面信息架构 + 控制状态机展示**。不新增执行引擎。

---

## 5. 控制台信息架构

四个视图，路径保持现有静态 shell 约定（`/`、`/machine/<name>`、`/task/<id>`、`/session/<id>`），不引入新站点、不改 cutover 开关语义。

```text
Fleet
  ├─ 机器网格（已有）
  ├─ 新增：活跃会话条（adopted / pending / unmanaged 计数 + 入口）
  └─ 新增：在途控制命令条（queued/executing，有界）

Machine /<name>
  ├─ 系统指标 + agent 表（已有）
  ├─ 发现实例 → 显式纳管（已有后端；补前端）
  ├─ 本机会话列表（只读摘要 → 进 session 页）
  └─ 开新任务表单（已有；成功后优先跳 session，否则跳 task）

Session /<id>
  ├─ 徽章：managed / capture_quality / control_capability / adoption status
  ├─ 时间线（已有）
  ├─ 新增：控制条（仅 adopted + control_capability=available）
  ├─ 新增：控制历史（receipt 有界列表）
  └─ 若绑定 task_id：跳转任务页

Task /<id>
  ├─ 状态 / 日志 / diff（已有）
  ├─ 取消 / 重试（已有；受管 attempt 已桥 cancel_attempt）
  └─ 新增：若有 session_id，跳转会话页
```

导航规则：

- 浏览器 URL 仍由静态 shell + 前端路由解码，不新增 Flask 业务模板
- 所有跳转走 `pagePath(...)`，所有 API 走 `client.js`
- 动态文本一律 `textContent`；动作名不得拼进 CSS class（沿用 session.js 白名单映射）

---

## 6. 控制模型（前期）

### 6.1 网页可发的动作

只复用已存在、已签名、已有测试的动作：

```text
pause_session
resume_session
terminate_session
quarantine_session
cancel_attempt
```

加上已有的纳管动词（不是进程信号）：

```text
adopt      # POST /api/adoptions
detach     # DELETE /api/adoptions/<session_id>  （不发信号）
retry      # POST /api/adoptions/<session_id>/retry
```

exact capture 按钮可以在 UI 占位为 disabled，并写明「须显式升级；本期不接」。不自动调用 `capture-exact`。

### 6.2 页面状态 vs 进程状态

沿用 v5：Hub「已入队」≠ 已执行。

```text
operator 点击 pause
  → POST /api/adoption/<session_id>/source/control
     { "action": "pause_session", "reason_code": "operator_requested" }
  → 202 + command_id + status=pending
  → 页面控制条进入 queued/executing
  → SSE 或短轮询 receipt
  → succeeded | already_finished | failed | rejected | expired
```

规则：

- 按钮在 `pending/executing` 时 busy，防重复提交
- `terminate` / `quarantine` 优先于 `resume`（已有 supervisor 冲突优先级）
- 无签名 key、gate 关闭、unmanaged、revoked、`control_capability=unavailable`：按钮不渲染或渲染为不可用 + 有界原因码
- 错误只展示 bounded code（`unsupported_action` / `scope_mismatch` / `invalid_signature` 等），不展示异常文本、路径、pid

### 6.3 不把 task 和 session 合成一个对象

前期保持两个对象：

- **Task**：operator 发起的一轮工作（instruction、lease、result）
- **Session**：机器上一次 agent 运行的监管记录（事件、控制、纳管）

关联是可选的：runner/supervisor 若带上 `session_id` ↔ `task_id`，两边互相链接；没有绑定就各管各的。**禁止**为了 UI 漂亮把 task 状态机改掉。

---

## 7. 前端接线（本期唯一实质改动面）

不改后端领域模型。改这些文件，且每个仍保持单一职责：

| 文件 | 改动 |
|---|---|
| `frontend/api/client.js` | 增加 `listAdoptions` / `adoptInstance` / `revokeAdoption` / `retryAdoption` / `controlSession`；不发任何凭据头 |
| `frontend/api/contracts.js` | 增加 adoption 列表/控制回执的 allowlist parser；拒绝 pid/cmdline/signature |
| `frontend/index.html` | Fleet 导航增加「会话」入口；不改 shell 归属 |
| `frontend/views/fleet.js` | 活跃会话计数 + 入口；失败时空态，不阻塞机器网格 |
| `frontend/views/machine.js` | 实例纳管按钮；本机会话列表；创建任务成功后的 session 深链（有则跳） |
| `frontend/views/session.js` | 控制条 + receipt 历史 + 不可控空态；沿用 MAX_EVENT_RENDER 等有界 |
| `frontend/views/task.js` | 若 DTO 将来带 `session_id` 则显示链接；没有就不显示 |
| `tests/test_frontend_contracts.py` 等 | 锁住：control 请求体只有 `action`+`reason_code`；响应无内部字段 |

后端若缺「按机器列会话」的过滤，只用已有 `GET /api/sessions?machine=`，不新开列表 API。

### 7.1 控制请求体（前端硬约束）

```json
{ "action": "pause_session", "reason_code": "operator_requested" }
```

其它键一律不发送。后端已丢弃 pid/signal/shell；前端不得给攻击者可填的额外字段。

`reason_code` 前期只允许：

```text
operator_requested
```

需要更多码时再扩 allowlist，不由页面自由输入。

---

## 8. 分阶段（前期框架 → 后期缝）

### Phase 0 — 对齐（本文档，无代码）

- 负责人确认 §1 决策表、§2 非目标、§9 后期缝
- 不改生产 gate、不发版

### Phase 1 — 控制台只读拼装（可合并发版）

- Fleet / Machine 露出会话入口
- Session 页作为主阅读面
- 无控制按钮也可发版（纯可见性）
- 验收：静态前端契约 + 现有 session HTTP 测试仍绿

### Phase 2 — 纳管与五动作接到网页（前期框架完成线）

- client 接 adoption/control
- Session 控制条 + receipt
- Machine 页显式纳管 / 撤销
- 无签名 key 时 fail-closed UI
- 验收：既有 `test_adoption_control_http` / `test_frontend_adoption_contracts` + 新增前端契约；**不改** `CONTROL_ACTIONS` 集合

### Phase 3 — 任务 ↔ 会话深链（仍属前期，可选）

- 公开 task DTO **仅当** 已有绑定数据时增加可选 `session_id`（allowlist 扩展，须后端先行）
- 无绑定则字段缺省，旧前端可忽略
- 发布顺序：backend 先于 frontend（沿用独立 release 规则）

### Phase 4 — 对话续写（已接线，默认关；节点 native resume 已接线）

动作名已加入 `CONTROL_ACTIONS`：

```text
append_user_turn
```

约束（工作树必须遵守）：

- 签发默认关：`AGENT_FLEET_APPEND_USER_TURN_ENABLED` / supervisor 构造参数，缺省 False
- 只对 `adopted` + `control_capability=available`；Hermes 永久 `unsupported_action`，直到 capability probe 证明 resume
- payload **精确**只有 `{"text"}`（≤2000）；多一个键（含 `cwd`）即 `invalid_payload`，不静默剥离
- **永远不是** `inject_stdin` / PTY 写 / 向活进程 stdin 写
- 无私有 native 身份（cwd/env/token/exe）时不得 `GroupOps.create`：`create(argv, None, {})` 会落到 `/tmp`、stdout PIPE、孤儿、`--ephemeral`。有 resume 声明但无身份仍 `unsupported_action`；无 resume 返回 `resume_unverified`；Hermes 永久 `unsupported_action`；quarantine 即使有身份也拒绝
- 身份齐全时才 native resume：`create(argv, cwd, env)` 且 cwd 不得为 `/tmp`/`None`，stdin=DEVNULL，argv 含 `--resume` + session token，禁止 `--ephemeral` / `-p` / adapter 默认 command。这是新 CLI 进程进同一 native session，不是 `resume_session` 的 SIGCONT
- 私有身份落在 `_ManagedEntry`（及 live handle），**永不**进 durable manifest / 公开 status。`_release_handle` 丢掉 pid 后身份仍在，自然完成后可再 resume
- terminate/quarantine 抢占已排队的 follow-up / resume，不得后发执行
- 会话页 gated textarea 草稿存在 `viewState.followUpText`；入队 ≠ 执行
- 代码缺省仍关；**2026-09-08 LIVE 已开** `AGENT_FLEET_APPEND_USER_TURN_ENABLED`。开开关 ≠ 已向座位发过 follow-up

### Phase 5 — 本机配置切换（已接线；LIVE 2026-09-08 已开）

动作名已加入 `CONTROL_ACTIONS`：`apply_local_profile`。代码签发缺省关；LIVE conf 已写 1。hub 只下发本机已登记的 opaque `profile_id`；节点只翻转 cc-switch db 的 `is_current`，永不把 `settings_config` / 密钥拷到线上、receipt、日志或 Hub payload。这**不是**正在跑的 agent 热切换模型。见 §9。

---

## 9. 后期缝：cc-switch 风格的本机配置（已接线，默认关）

用户明确：网页能像 cc-switch 那样便捷切换客户端机器上的模型/provider。前期框架必须把这条路**挡住且留缝**，避免以后用 `write_file` 或密钥下发走捷径。工作树已接 `apply_local_profile`，但只翻转本地 `is_current`。

### 9.1 原则

- 密钥、endpoint、token **永不进 hub、永不进控制命令、永不进前端**
- hub 最多下发一个**节点本地已登记的 profile 名**
- 真正改 cc-switch db 当前指针的是节点上的本地执行器；**不**把密钥拷进 `~/.claude` / `~/.codex`
- 与 2026-08-28 profile registry 的 P2（hub 下发 profile）同一安全结论：复制凭据到运行目录须单独安全设计

### 9.2 工作树对象（不建新表）

```text
LocalProfile  （节点本地，probe 上报无密钥）
  profile_id     不透明 id
  family         codex | claude_code | hermes | pi
  label          展示名（非秘密）
  origin         cc_switch | manual
  current        bool
  # 不含 api_key / base_url / credential / settings_config

ApplyLocalProfile 命令（默认关）
  action         apply_local_profile
  target         { machine_id, session_id, attempt_id? }
  payload        { profile_id }   # 精确一键
```

失败码有界：`unknown_profile` / `family_mismatch` / `unsupported_action` / `invalid_payload`。

### 9.3 必须遵守

- `FORBIDDEN_ACTIONS` 继续包含 `change_credential` / `change_policy` / `write_file`
- 会话页 gated picker 只在 `/api/status` `features.apply_local_profile === true` 时渲染；机器页不放 picker
- `agent_profiles.py` 继续只存家族默认 argv，不存 provider
- **2026-09-08 LIVE 已开** `AGENT_FLEET_APPLY_LOCAL_PROFILE_ENABLED`，语义仍是「只改指针、不热切正在跑的进程」

---

## 10. 组件与文件（前期）

加号 = 新文件，星号 = 改既有。后端领域层预期 **零新文件**。

### 10.1 前端

| 文件 | 职责 |
|---|---|
| `* frontend/api/client.js` | 纳管与控制的唯一 HTTP 入口 |
| `* frontend/api/contracts.js` | adoption / control 公开 DTO |
| `* frontend/index.html` | 导航入口 |
| `* frontend/views/fleet.js` | 会话摘要条 |
| `* frontend/views/machine.js` | 纳管 + 本机会话列表 |
| `* frontend/views/session.js` | 控制条 + receipt |
| `* frontend/views/task.js` | 可选 session 深链 |
| `* frontend/styles/app.css` | 控制条/不可用态样式（不把状态名当 class 注入） |

### 10.2 后端

前期默认不改。仅当 Phase 3 需要 `task.session_id` 时：

| 文件 | 职责 |
|---|---|
| `* hub/domain/task.py` | 公开 DTO 可选 `session_id` |
| `* hub/application/task_service.py` | 从已有绑定投影，不新造绑定逻辑 |
| `* tests/test_task_api.py` | 无绑定时字段缺省；有则 bounded |

若绑定数据根本不存在，Phase 3 跳过，不强造列。

### 10.3 文档

| 文件 | 职责 |
|---|---|
| `+` 本文 | 路线 A 权威设计 |
| `* README.md` | 增加设计入口链接 |
| `* docs/HANDOFF.md` | 「下一步」指向本文；不把未实现写成现状 |

---

## 11. 安全不变量（验收时逐条可测）

1. 浏览器请求不含 `X-Agent-Fleet-Token` / `X-Runner-Credential` / `X-Supervisor-Credential`
2. control POST 体只有 `action`、`reason_code`；Phase 4/5 可另附精确 `payload`（`text` 或 `profile_id`）。额外 HTTP 顶层键不签名、不回显；额外 payload 键 `invalid_payload`
3. 公开 JSON 无 pid / exe_path / cmdline / nonce / signature / raw；事件 payload 可含 agent 文本
4. unmanaged / revoked / gate 关闭 / 无签名 key / feature 关 → 不能发出成功控制
5. detach / revoke 不产生进程信号（已有测试继续锁）
6. `CONTROL_ACTIONS` 现为 9 个（原 7 + `append_user_turn` + `apply_local_profile`）；`FORBIDDEN_ACTIONS` 不变
7. 静态前端 release 仍不含凭据路径；`deploy/test-static-frontend.sh` 绿
8. observation ingest 不因控制台改动多上传对话
9. `append_user_turn` 无 native 身份时节点路径不得调用 `GroupOps.create`（测试断言 create 未被调用）。有完整私有身份时必须 `create(argv, cwd, env)`，cwd≠`/tmp`，stdin=DEVNULL，argv 含 `--resume`、禁止 `--ephemeral`。测试拆开错误 sibling vs 正确 native resume

---

## 12. 验收

### Phase 1

- 从 Fleet 能进会话列表/详情（有数据时）
- 无会话时明确空态，不报内部错误
- 现有 pytest 全绿；静态前端冒烟 `STATIC OK`

### Phase 2（前期完成线）

- 对 adopted 会话：pause 入队 → 页面显示 pending → 用测试替身回执 → succeeded
- 对 unmanaged：无控制按钮
- 对无 supervisor key 的 app：控制返回既有 bounded 失败，UI 不显示「已暂停」
- `CONTROL_ACTIONS` 与 `FORBIDDEN_ACTIONS` 测试断言不变
- 前端契约拒绝内部字段

### 明确不验收（开 gate ≠ 已向座位发命令）

- 向 LIVE adopted 座位实发 `append_user_turn` / `apply_local_profile`（本次只开签发闸，未下发控制命令）
- 模型/provider 热切换正在跑的进程
- exact capture 自动开启
- Hermes 受管 spawn
- 未装 probe 的 worker-a / worker-b / worker-c 等节点「能控」

---

## 13. 风险与依赖

| 风险 | 处理 |
|---|---|
| 生产 supervisor 签名 key 未配 → 一切控制 fail-closed | UI 必须暴露这个事实；不在框架里「先做假成功」 |
| 会话事件是 redact/best_effort，看起来不像 ChatGPT 全文 | 文案写清「脱敏时间线」；exact 仍走独立升级 |
| 任务与会话没有稳定绑定 | Phase 3 做成可选深链；没有就不显示 |
| 把控制台做成任意远程终端 | 代码审查拒绝任何 stdin/shell 动作；本文 §2.2 作为 PR 检查单 |
| cc-switch 后期被提前塞进来 | Phase 5 单独 spec；`agent_profiles` P2 继续暂缓 |

运维依赖（非本框架代码）：Mac 已有 probe；HK 容器有 self-report。其它节点未装 probe 时，控制台对那些机器只能显示 offline / 无会话。不把「装 probe」算进本期开发。

---

## 14. 已确认 + 工作树对齐

1. 前期完成线仍是 Phase 2（透传时间线 + adopted 五动作）。Phase 3 深链已接（仅投影已有 `attempt_id` 绑定）。
2. 网页打字续写已接线；**永远不是** `inject_stdin`；无身份禁止 sibling spawn；有身份才 `--resume` + 原 cwd/env + DEVNULL。代码缺省关；**2026-09-08 LIVE 已开**。
3. 不改 agent 发行版；resume argv 不得复用 adapter 默认 command（codex `--ephemeral` / claude `-p`）。attach 的 `capability_manifest={}`，家族 resume 声明 ≠ 本会话可续写。
4. cc-switch 已接线为「只翻 `is_current`」，禁止密钥下发。
5. HANDOFF / README 必须写清：Phase 1–5 接线、代码缺省关、**LIVE 2026-09-08 已开 Phase 4/5 gates**。开开关 ≠ 已向座位发过控制命令。

发版顺序：Phase 1–3 控制台可随 main 发。Phase 4/5 代码缺省仍关；LIVE 靠 `fleet-gates.conf` 打开，回滚=删对应行 + TERM web。
