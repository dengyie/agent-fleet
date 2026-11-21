# Native Agent Web Workspace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task, inline. Steps use checkbox (`- [ ]`) syntax for tracking. This document records a proposed implementation; unchecked steps are not completed functionality.

**Goal:** 用户从网页的新建对话入口选择电脑节点、项目和 Agent，启动真实原生会话；也能找到、阅读和继续电脑上的已有会话，并准确跟踪每次发送及控制结果。

**Architecture:** 复用 Task → Runner → Supervisor → native session → encrypted capture → Hub 的既有链路。主助手对话和电脑原生会话共享交互组件，但分别保留原来的身份、存储和执行生命周期。HTTP、主助手工具共同调用应用服务；浏览器不执行主助手工具、不连接电脑的 localhost，也不解释 PID 或原生数据库。

**Tech Stack:** 现有 Python / Flask / SQLite、原生 ES modules、awesome-ui-kit 的 Vanilla Web Components、现有 SSE、pytest 与 Playwright；不引入前端框架、消息中间件或新模型代理。

## Global Constraints

- 初始用户要求“先深度规划”；随后批准实施并明确本工作流只负责前端，后端由其它 agent 维护。
- 前端实施状态、实际 URL、接口接点和验收边界以[网页前端接入](../../native-web-frontend.md)为准。下面仍保留总体计划与未完成的后端步骤，不能将勾选列表整体视为已上线证明。
- 后续前端迭代已补齐 PR109 的测试 DOM、安全 helper 与发布清单契约，并修复长消息完整原文滞后、投递卡轮询折回及离线节点无法刷新恢复；本地前端组合 244 passed，详细重叠测试结果和未上线边界见上述接入文档。
- 调研基线：GitHub `main` 的 `73e0db0be4151626355d529effe572e62ba0b3bf`，2026-11-21 fetch 确认。
- 保护主工作树；本计划位于现有隔离工作树的 `codex/native-agent-web-plan`。实施、合并前重新 fetch；按用户要求先 rebase 最新 main。
- 所有修改使用 UTF-8，无 BOM。已有用户会话、配置、任务和失败证据保留。
- UI 保持简洁；基础设施配置留在设置中，运行详情留在工作面板。所有图标使用现有 UiIcon。
- 复用现有账号和管理员能力边界；网页使用正常账号登录，不能依赖浏览器持有运维 service token。
- Agent 能力依据后端当前会话和节点证据决定，不能仅凭 OS、Agent 名称、安装标记或 `managed=true` 推定。
- 发送后立即反馈；读失败只重试读取。未知投递不换 ID 重发，不暗中新建替代会话。
- 不新增模型回合次数限制；分页、单次网络超时和 DOM 展示预算不能终止后台 Agent 工作。
- 没有源端数据时显示缺失，不伪造逐 Token 流、思考内容、工具结果、用量或成功状态。
- 复用原自动发布及公网资源逐字节校验；实现完成后部署验收，不能只凭 main SHA 声称新 UI 上线。

---

## 1. 已核实的现状和缺口

| 事实 | 代码依据 | 对方案的约束 |
|---|---|---|
| 最新三批改动是跨平台适配、尾流完成和用户回显去重 | PR104 / PR106 / PR107；`docs/testing/agent-platform-acceptance.md` | 前端接入这些已存在的能力，不再建设另一套执行器 |
| 新任务已支持五类 Agent，绑定后返回 `session_id` | `agent_profiles.py`；`TaskService.create/get/targets/_public_task` | 用真实 task_id 等待原 session 绑定；不能在浏览器拼造 session_id |
| 节点项目和安装目录已能通过应用服务查询 | `TaskService.targets`；`AssistantAdminService` 的 `tasks.targets` | 缺的是面向浏览器的薄 HTTP 入口；现在机器任务表单仍需手填项目 |
| 已有列表、详情、历史事件、发送、控制和回执 HTTP API | `hub/http/session_routes.py`、`supervisor_routes.py`、`adoption_routes.py` | 扩展原表面；不能增加通用“执行主助手工具”的网页 RPC |
| 后端区分 `allowed_actions`、`message_delivery`、纳管状态及采集来源 | `SessionService._live_session/_check_action` | 原生可读与可写必须分别呈现；撤销纳管不能被前端恢复成可控 |
| 浏览器丢弃 `message_delivery`、`capture`、`control_unavailable_reason`，且不认识 `configure_model` | `frontend/assets/api/contracts.js` 的 SESSION_FIELDS / SOURCE_CONTROL_ACTIONS | 先对齐契约，否则新 UI 仍无法准确解释功能是否可用 |
| 前端硬编码不显示 Hermes 续聊 | `frontend/assets/views/session.js:renderFollowUpTurn` | 改为能力判断；不能把 Windows 当前未安装 Hermes 扩大成全部平台不支持 |
| 普通消息 HTTP 发送没有可恢复的浏览器幂等标识 | `api_session_message`、`SessionService.send_message`、`client.sendSessionMessage` | 正式提供聊天交互前必须补齐同一操作的找回与去重 |
| 主助手工具已有命令幂等和响应丢失找回 | `AgentSessionService._original_command/_execute`、Supervisor command repository | 抽回共享应用路径供网页复用，不在前端复制恢复逻辑 |
| 历史 JSONL 同步已存在，常驻 ControlClient 默认启动它 | `tools/session/sync.py:NativeArchiveSync`；`control_client_cli.py` | 直接阅读已同步记录；未完成扫描不能显示为“电脑没有会话” |
| ZCode 桌面会话使用官方 bridge，能向原对话投递并查询原操作 | `integrations/zcode/README.md`；`desktop_control.py` | 与 CLI 模式分开呈现；不能以终止共享桌面进程代替会话控制 |
| 侧栏最近对话仅查询平台 conversations | `shell/navigation.js` | 增加带来源的电脑会话入口，不能将 native session 伪装成平台 conversation |
| 当前 session 页面主要是事件时间线，且整段重绘 | `frontend/assets/views/session.js`，1216 行 | 将聊天作为主视图；保留可定位的诊断面板，避免往大文件继续堆功能 |
| 全局 SSE 仅推 session 摘要，正文需通过 cursor 拉取 | `realtime/sse.js`；`SessionService.session_events` | 用 SSE 唤醒增量读，断线补读；不要用全页刷新实现实时聊天 |
| `cancel_attempt` 实际调用终止；主动终止不同于正常完成 | `Supervisor.cancel_attempt/terminate_session`、`ContinuationCapture.send` | 不能把它标成可随时继续的“停止生成” |
| 模型目录接管服务目前装配在主助手 wiring，worker 关闭时提前返回 | `hub/composition/assistant_management.py` | 浏览器直接配置会话模型不应依赖主助手 Worker 启用 |

调研验证：以下既有回归在本机本轮重新运行，**142 passed / 3 subtests passed**。这是当前链路证据，不是新网页能力已经通过验收。

```sh
python3 -m pytest tests/test_assistant_agent_tasks.py tests/test_assistant_session_capabilities.py tests/test_assistant_vm_sessions.py tests/test_session_query_api.py tests/test_native_model_config.py tests/test_zcode_session_binding.py tests/test_cross_platform_agents.py tests/test_session_frontend_contracts.py -q
```

生产现状参考：Obsidian canonical《agent-fleet 部署与运维》记录同版本 Hub、Mac/Windows `s26` 客户端，以及 9 条实际安装 CLI 的 FIRST/FOLLOW 验收。本轮没有重新发送这些生产指令；Linux 有既有发布门禁，不能将双端 9 条真实模型结果写成新的三端实机全量验收。

## 2. 能力矩阵：网页必须如实反映

| 来源 / Agent | 网页新建 | 已有历史阅读 | 原会话续聊 | 模型选择 |
|---|---|---|---|---|
| 主助手 | 现有平台 conversation | 现有 owner 历史 | 现有平台 Run | 现有平台模型目录 |
| Runner Codex / Claude Code | 节点已安装、项目已登记时可创建 Task | capture / 已同步 JSONL | 同 native ID、续聊身份有效且空闲时可用 | 当前仅在已有会话空闲且具备 `configure_model` 时接管，协议分别为 Responses / Anthropic |
| Runner Pi | 同上 | capture / 已同步 JSONL | 持久化身份有效时可用 | 使用本机配置，当前不宣称平台模型接管 |
| Runner ZCode CLI | 同上，需节点正确 CLI 配置 | capture / 已同步 JSONL | 当前仅已证明身份的 Runner 自有会话 | 使用原 provider |
| Runner Hermes | 节点已安装并显式配置时 | Runner capture；精确 ID/cwd 的原生 SQLite 采集 | 当前仅已证明身份的 Runner 自有会话 | 使用原 provider |
| 本地 JSONL 历史 | 不适用 | 已同步的数据可读 | 单独同步不会取得控制权 | 不显示可写模型选择器 |
| 已纳管本地 CLI | 不适用 | 根据精确源绑定及已保存记录阅读 | 仅后端声明可投递时 | 本机 profile 与会话模型接管分开，不能混成一个功能 |
| ZCode 桌面 bridge | 首版只打开已有对话 | 官方分页消息 / 已同步 rollout | 原桌面对话、官方队列、原 operation receipt | 当前不宣称任意桌面对话模型切换 |
| generic / 不明进程 | 不提供通用执行 | 有已保存证据则阅读 | 不显示发送 | 不显示 |

节点 OS 作为展示维度，真实能力来自该节点安装、配置和会话状态。Windows Hermes 当前验收机未安装；macOS 的 Hermes 已有实际验收。其它电脑的安装状况须重新读目标节点上报。

**历史边界：** NativeArchiveSync 目前扫描注册的 JSONL 根目录并可连接 ZCode 桌面 bridge。Hermes 的任意旧 SQLite 会话全库枚举并不是这个扫描器的已有能力。首版展示实际同步到 Hub 的历史与已纳管实例；不能承诺已经发现电脑所有离线存档。缺少具体来源时在连接详情中说明，后续只有出现明确缺失需求才补相应采集器。

## 3. 用户看到的入口和布局

### 3.1 一个“开始新对话”入口，三个明确动作

```text
开始新对话
  主助手                  使用项目总管和平台模型
  在电脑上新建 Agent 会话  选择节点 → 已登记项目 → Agent
  打开电脑已有会话        进入电脑会话列表，先阅读再按能力继续
```

保留现有 `/assistant` 作为主助手默认入口。新建页顶部的当前对象可以切换；不会强制每次主助手新对话都走多步向导。

选择“电脑 Agent”后的配置区保持紧凑：节点、项目、Agent 三个选择器；下面就是输入框。节点显示真实名称、平台、最近上报和在线状态。项目来自后端登记；Agent 显示已安装、未安装或状态待确认。`installed=null` 不能画成“未安装”，可以按现有准入规则尝试启动，结果由后端给出。

首版新建使用“电脑原有模型配置”，旁边提供说明。不能把主助手的所有平台模型直接放进本地 Agent 下拉框，因为现有 Task 创建没有首轮模型覆盖协议；Codex / Claude 的空闲会话模型配置在工作面板提供。若将来需要首轮指定平台模型，应先补冻结配置与节点交付协议，再开放选项。

### 3.2 电脑会话列表

新增 `/sessions`，按节点、Agent、来源筛选；列表为每页 50 条、稳定 cursor 分页。行展示：可读标题、节点和 Agent、预览、时间及来源、当前状态、能力标签。

- 有明确能力时显示“继续对话”。只有阅读能力时显示“查看记录”和具体原因。
- 已发现但尚未纳管的活动 CLI 在独立“发现的活动实例”区显示“连接此会话”；连接后等待原纳管回执。共享桌面宿主不能在此误认成一个会话。
- 历史、Runner 和纳管记录没有经过精确身份关联时保留来源标签；不按标题、相同文本或“最新文件”做跨源合并。
- 空态写“尚未同步到可读会话”，提供节点连接详情；不能写成“电脑没有聊天记录”。
- 正文搜索复用现有 `/session-search`；搜索功能未启用时保留目录筛选，并说明全文搜索未启用。
- 标题取已登记的标题或首条规范化用户消息的短预览；无证据时用“Agent · 节点 · 日期”。不会让一长串内部 ID 成为主标题。

### 3.3 聊天页

```text
┌ 最近对话 ┐  Codex · 我的 Mac · agent-fleet       [连接状态] [工作面板]
│ 主助手   │
│ 电脑会话 │  我：检查这个项目的测试失败
│ ...      │  已发送 → 节点已接收 → Agent 正在执行
│          │  工具：读取 tests/test_agent_profiles.py   [展开]
│          │  助手：已定位失败原因……
└──────────┘  [输入下一条消息                         ][发送]
```

主区域只保留对话和有意义的运行反馈。右侧工作面板收纳节点 / 项目 / 采集状态、工具详情、命令回执、模型配置和高级控制；手机上改为抽屉。原始事件、源记录及策略信号放在诊断标签页，不再抢占首屏。

复用 `StreamMarkdown`、`ToolCallBadge`、`ChatPromptInput`、`AutoScrollAnchor`、`UiIcon`。用户上翻时不抢滚动；有新内容时显示“有新消息”。工具卡显示实际动作、文件或命令摘要、执行结果和错误；只有工具名时就显示工具名，不能编造描述。

侧栏最近区域支持“全部 / 主助手 / 电脑 Agent”筛选，条目带小型来源和节点标识；基础设施仍留在工作面板与设置。主助手条目继续使用原有重命名 / 归档操作；首版不对原生会话伪造删除或归档能力。

## 4. 状态、身份与实时交互

### 4.1 统一外观，分开真实身份

前端引用使用明确的联合类型，而不是把所有 ID 塞进 `conversation_id`：

```js
// 每个值只保留对应 kind 的字段。
const assistantRef = {kind: 'assistant', conversationId: 'conv_example'};
const startingRef = {kind: 'native-task', taskId: 't-example'};
const nativeRef = {kind: 'native-session', sessionId: 'runner_example'};
```

原生引用不创建平台 conversation / Run 副本。新建时采用 `/agent-chat`；首个 Task 返回后改为 `/agent-task/<task_id>`；获得原 session 绑定后原地切换为 `/session/<session_id>`。复用 app 已有的路由缓存身份更新方式，不能销毁并重建正在输入 / 发送的聊天 DOM。后台返回不能将用户从当前页面强制带走。

### 4.2 三种状态分别跟踪

| 维度 | 显示和判断 |
|---|---|
| 浏览器提交 | 发送中、Hub 已入队、明确未入队、提交结果待确认 |
| 命令投递 | 原 command_id 的 queued / delivered / accepted / executing / terminal receipt |
| Agent 回合 | 原 session 的执行状态、消息 / 工具事件、session_close 或原桌面 operation |

CLI 的命令 `succeeded/appended` 仅表示原指令已交付，不等于模型回复完成。ZCode 桌面 bridge 的原 operation 完成与消息同步也分别显示。`session_busy` 如果没有命令 ID 就保留草稿并等待原状态更新；首版不暗中建立浏览器自动发送队列。

### 4.3 即时反馈和消息唯一性

1. 点击发送立即把本次文本放进正在发送区域，清空可编辑输入，保留可恢复的本次操作记录；无需等待模型、Task 完成或命令回执。
2. 每次用户主动发送生成一个 `client_token`，网络恢复沿用该 token。服务端以 actor + token 绑定目标、动作和 payload；同 token 不同内容返回 409。
3. 对 Runner CLI，给规范化 `user_message` 增加可选 `origin_command_id`，由节点从验证过的签名命令取得；首轮使用 `origin_task_id`，由节点从领取的真实 Task 取得。pending 气泡与持久消息按此身份对账，不能仅靠相同文字消除重复。
4. `source_record` 保留原始回显证据；PR107 的原生回显去重继续工作。合法连续两次发送“继续”必须保留两个回合。
5. 对未能提供消息关联 ID 的已纳管源 / ZCode 桌面，发送区域明确标为“待投递 / 已进入原会话队列”，真实聊天气泡只来自规范化原生记录。原操作结束后收起为回执卡，不猜测哪条本地消息是网页回显。若后续官方 API 提供可证明的 message identity，再将其接入同一对账协议。
6. 断网、刷新、账号切换、后台返回不能产生新写命令。会话内恢复信息与账号绑定；退出或身份更换立即销毁订阅和私有内存记录。

### 4.4 实时读取与展示预算

- 复用全局 `SseClient` 的 session 摘要；只对当前可见会话安排增量读取。单视图同一时间最多一个正文读请求，多次通知合并。
- 正文以 Hub `cursor` / `next_cursor` 推进，不以原生 `sequence` 去重；不同 stream 可以重复 sequence。
- `include_source=false` 为聊天默认；诊断页显式读取源记录，避免大量重复上下文占满聊天首页。
- 初次打开加载最近 100 个可读事件；向上加载更早事件，当前页保留尾部游标继续追加。需要补后端 `before_cursor` / tail 查询，不能从第一页循环扫到最后。
- SSE 重连、窗口重新可见以及返回缓存页时，先按持久游标补齐并刷新能力。断流期间以单一调度器回退读取，延迟从 1 秒退避到最多 15 秒；显示连接状态，不因重试次数终止 Agent。
- `suspend/destroy/identitySignal` 中止在途读取并释放定时器 / 订阅，恢复时不重发写操作。挂起不取消后台任务。
- DOM 使用消息分页及窗口，正文支持完整展开；展示预算不截断持久历史或停止运行。

**流式边界：** 当前 Pi 只采集完整消息，ZCode 桌面无 rollout 时在 idle 读取官方完整消息。第一期承诺“状态、工具和已产生消息实时更新”，不能承诺每个 Agent 都逐 Token 输出。界面显示“等待 Agent 返回内容”，禁止前端假打字；内部 source_record 不作为思考过程展示。

### 4.5 控制动作的文案和结果

- 发送、暂停、恢复、本机 profile 和模型配置仅按实时 `allowed_actions` 显示。
- 尚未绑定会话的排队任务可以通过原 Task cancel 取消启动；绑定后按当前 session 的真实动作处理，不另发一个替代任务。
- 终止 / 隔离 / 撤销纳管放入高级菜单，解释实际后果。终止确认明确指向当前节点与会话，不暗示可以保证继续。
- 首版不把 `cancel_attempt` 放进普通“停止生成”按钮；它当前就是终止。真正“中断本轮后仍可继续”需要节点独立协议和续聊身份验证，单列为后续能力。
- 模型配置以原命令回执确认，生效范围标为“下一次续聊”；配置期间不允许把消息抢先发往旧模型。Pi / Hermes / ZCode 无目录接管能力时显示本机配置说明。
- 节点下线可继续读已保存历史，发送与控制显示最后上报和当前投递状态。不能因为旧安装标记就宣称 ControlClient 正常。

## 5. 后端最小接口改动

保持现有 `/api` 接口及公开字段规范，新增的内容是具体业务表面，不是通用执行代理。

| 接口 | 复用 / 增补 |
|---|---|
| `GET /api/machines/<machine>/task-targets` | 薄适配 `TaskService.targets`，返回登记项目、安装三态和观测时间；节点平台来自现有 status/detail |
| `POST /api/tasks` | 继续原创建路径；网页显式持有 token；补原提交找回，重复请求先识别原 Task，再判断是否创建新任务 |
| `GET /api/tasks/<task_id>` | 继续查询原任务和已存在的 `session_id`，排队 / 绑定中不另建任务 |
| `GET /api/sessions?machine=&agent=&source=&before_cursor=&limit=50` | 原目录增加稳定分页、来源和列表预览，不通过前端遍历全部会话筛选 |
| `GET /api/sessions/<id>/events?limit=100&include_source=false&tail=true` | 最近事件首屏；返回 after/next/previous cursor。tail 与 before/after 互斥，错误组合返回 400 |
| `GET /api/sessions/<id>/events?before_cursor=<cursor>&limit=100&include_source=false` | 取更早事件，响应始终正序；after_cursor 增量路径保留；sequence 定位继续支持 |
| `POST /api/sessions/<id>/messages` | 普通消息明确传 `{text, client_token}`；交互问题继续传 request_id / selected 等；统一共享幂等发行路径 |
| `POST /api/sessions/<id>/control` | 指定 action / payload / client_token，复用共享幂等控制与当前会话准入 |
| `GET /api/sessions/<id>/commands?client_token=<token>` | 只读查询当前 actor 的原操作；已签发返回原 command_id，未签发明确不存在，不触发执行 |
| `GET /api/sessions/<id>/commands?limit=20&before_cursor=<cursor>` | 复用 Supervisor.session_logs 的公开回执 / 审计分页，按目标 session 验证 |
| `GET /api/supervisor/receipts/<command_id>` | 保留既有回执接口；视图核对 session 绑定 |
| `GET /api/sessions/<id>/models` | 薄适配 SessionModelService.list_models；无能力仍返回真实 unavailable 和 scope |
| `POST /api/sessions/<id>/model` | `{mode:'catalog', profile_id, client_token}` 或 `{mode:'original', client_token}`；冻结 digest 并找回原发行，不接受浏览器 API Key / URL |

共享幂等实现放回现有 `SessionService.control` 的应用路径，主助手 `_original_command` 的目标 / action / payload 校验与发行后找回逻辑迁移到这里，避免两个实现。主助手保留其原有幂等 key namespace，已有命令继续可以找回；这是保留在途身份，不是另加旧协议兼容层。模型 digest 快照和原请求恢复放在 SessionModelService，两个入口共用。

Task 创建的 token 同时核对 requested_by、machine、agent_type、project、instruction、confirm。已有 token 不能因节点随后下线而失去原 task_id；同 token 改请求返回 `idempotency_conflict`。创建回应丢失时沿用冻结请求查询 / 提交，不生成第二个 token。

列表增加 `display_title`、`last_message_preview`、`source_kind`、`activity_at`、`activity_time_source`；`source_kind` 为 `runner` / `native_archive` / `adopted_cli` / `zcode_desktop` / `unknown`，由可信采集路径登记。`activity_time_source` 为 `native` / `fleet` / `sync` / `unknown`；时间缺乏原生依据时明确显示“最近同步”，不冒充原会话活跃时间。预览从已脱敏 transcript 批量查询，不能每行读取一次完整历史。分页使用固定目录锚和唯一 ID 排序；实时更新不改动当前分页锚。列表不强行融合未经精确证明的跨源记录。

需要新增的 `origin_command_id`、`origin_task_id` 和来源元数据贯穿共享 schema、节点生成、Hub 投影和前端 parser；这些字段只是已验证操作的公开引用，不把 browser token、原生路径、credentials 或 provider 密钥带入事件。

## 6. 文件职责地图

| 文件 | 职责 / 变更 |
|---|---|
| `hub/http/task_routes.py` | targets 薄 HTTP 路由，复用 TaskService |
| `hub/application/task_service.py`、`hub/infrastructure/task_repository.py` | 原提交找回、幂等内容冲突及当前 task → session 查询 |
| `hub/http/session_routes.py`、`hub/http/supervisor_routes.py` | messages/control 幂等参数、目录、事件分页、原操作 / 模型薄适配 |
| `hub/application/session_service.py` | 共享当前能力、发行 / 找回逻辑、目录和增量 / 反向事件用例 |
| `hub/application/agent_session_service.py` | 主助手参数适配；移除迁走的重复幂等实现 |
| `hub/application/session_model_service.py` | 模型目录、冻结配置、原发行恢复供网页和主助手共同使用 |
| `hub/composition/assistant_management.py` | 公共会话 / 模型服务先装配，Worker 可选；不把网页能力绑到 Worker 存活 |
| `hub/infrastructure/session_repository.py`、`hub/infrastructure/transcript_repository.py` | 稳定目录分页、批量预览、before/tail cursor；不新建一套对话存储 |
| `session_schema.py`、`tools/session/bridge.py`、`tools/session/runner_capture.py` | 来源、原命令引用的合法字段与持久事件 |
| `tools/supervisor/control_client.py`、`tools/supervisor/continuation_capture.py` | 从签名命令传递已验证 command_id 到用户消息采集 |
| `tools/session/sync.py` | 登记 archive / desktop 来源，保持原 checkpoint / 单一源采集所有权 |
| `frontend/assets/api/client.js`、`contracts.js` | 唯一网络入口和 DTO parser；API 可传 signal、include_source 和 cursor |
| 新增 `frontend/assets/views/conversation-target.js` | 轻量入口菜单及节点 / 项目 / Agent 选择；加载、错误和键盘状态 |
| 新增 `frontend/assets/views/sessions.js` | 目录、筛选、分页、活动实例连接入口 |
| 新增 `frontend/assets/views/agent-chat.js` | 新建 / 待绑定 / 已绑定三种 native 引用的页面协调 |
| 新增 `frontend/assets/views/agent-chat/service.js` | 原 Task、会话、命令、模型用例编排；不操作 DOM |
| 新增 `frontend/assets/views/agent-chat/state.js` | 规范化消息、cursor、pending 操作与能力投影；不发 HTTP、不访问 DOM |
| 新增 `frontend/assets/views/agent-chat/live.js` | 可见会话单一增量读取调度及 suspend / resume / dispose |
| 新增 `frontend/assets/views/agent-chat/messages.js` | 复用 Web Components 的消息 / 工具渲染，增量更新和滚动锚 |
| 新增 `frontend/assets/views/agent-chat/inspector.js` | 节点、回执、模型和高级控制 |
| 新增 `frontend/assets/views/agent-chat/diagnostics.js` | 迁移现有可定位事件、源记录及策略信号视图 |
| `frontend/assets/views/session.js` | 原 session 路由装配与事件定位；删除已迁走的重复 UI 实现 |
| `frontend/assets/views/machine.js` | 机器详情保留观测与连接；新建任务入口转到预选节点的统一新建页，删除替代后的任务表单 |
| `frontend/assets/shell/navigation.js`、`app.js`、`routes.js`、`frontend/index.html` | 菜单、来源区分、账号边界、路由缓存身份更新及最近条目 |
| 新增 `frontend/assets/styles/agent-chat.css`、修改 `styles/app.css` | 沿用 token、桌面 / 手机布局、空态 / 骨架 / 连接状态和 reduced motion |
| `hub/http/pages.py` | `/sessions`、`/agent-chat`、`/agent-task/<id>` 的直接访问与刷新 |
| `docs/session-conversations.md`、`docs/testing/README.md`、`docs/testing/journeys.json` | 同步用户操作、真实接口、能力边界和门禁 |

新文件按已有职责拆分，实施中不为每个按钮再增加抽象层。不把主助手 assistant.js / 平台 Run 状态机拉入原生 Agent 控制器。

## 7. 分阶段实施与验收

### Task 1：打通真实能力和可恢复的网页操作

**Files:** task/session HTTP、TaskService / SessionService / AgentSessionService / SessionModelService、相关仓储、composition、`api/client.js` / `contracts.js`。

**Interfaces:** `getTaskTargets(machine)`；`sendSessionMessage(sessionId, text, clientToken)`；`controlSession(sessionId, action, reasonCode, payload, clientToken)`；`getSessionOperation(sessionId, clientToken)`；`getSessionModels(sessionId)`；`configureSessionModel(sessionId, input)`。

- [ ] 在新增 `tests/test_native_web_api.py` 编写真实 Flask / SQLite 回归：targets 登记项目与安装 true/false/null；无 Worker 的模型服务；当前管理员身份失效；同 token 两次 POST 仅一个签名命令；同 token 换目标或文本返回 409；已入队后响应失败 / 重新创建 app 仍找到原命令；原命令未知不重放。
- [ ] 扩充 `tests/test_assistant_agent_tasks.py` 和 Task 仓储回归：原创建响应丢失、节点随后下线仍取回原 task_id；内容变化冲突；同 token 并发只创建一次。
- [ ] 用现有 fixture 验证最小关键行为，随后将同样断言贯穿新 HTTP 接口：

```python
from tests.test_assistant_vm_sessions import app  # noqa: F401


def test_control_replay_keeps_original_command(app):
    client = app.test_client()
    body = {'text': 'continue exactly once', 'client_token': 'web-turn-1'}
    first = client.post('/api/sessions/s1/messages', json=body)
    replay = client.post('/api/sessions/s1/messages', json=body)
    assert first.status_code == replay.status_code == 202
    assert first.json['command_id'] == replay.json['command_id']
    changed = client.post('/api/sessions/s1/messages', json={**body, 'text': 'other'})
    assert changed.status_code == 409
    assert len(app.extensions['fleet']['services']['supervisor'].poll('m1')) == 1
```

- [ ] 先运行 `python3 -m pytest tests/test_native_web_api.py tests/test_assistant_agent_tasks.py -q`，记录目标回归的失败；再实现薄路由和共享应用路径，重复运行通过。
- [ ] 在 `tests/test_session_frontend_contracts.py` 增加 `message_delivery` / `capture` / 模型动作及未知字段边界；实现 parser，不用 family 硬编码覆盖服务端能力。
- [ ] 运行本计划第 1 节的原有 142 项范围及新增回归，审查原助手工具行为没有因共享实现改变。
- [ ] 提交 `feat: expose recoverable native session operations to web clients`。

**独立交付：** 管理员通过正常账号的 HTTP API 能查目标并执行 / 找回一次原命令；尚不声称完整聊天页已完成。

### Task 2：电脑会话目录与最近消息首屏

**Files:** session / transcript repositories、SessionService、session_routes、`views/sessions.js`、pages / navigation、测试及样式。

**Interfaces:** `listSessions({machine, agent, source, before_cursor, limit}) -> {sessions, next_cursor}`；`getSessionEvents(sessionId, {tail, beforeCursor, afterCursor, atSequence, includeSource, limit, signal}) -> {events, after_cursor, next_cursor, previous_cursor, has_more, has_earlier}`。事件始终按 Hub cursor 正序返回。当前位置参数调用在同一提交中改为此 options 形式，不再增加两种签名的兼容分支。

- [ ] 新增 `tests/test_native_session_directory.py`，构造两台电脑、五种 Agent、只读 archive、Runner、纳管和相同时间的记录，断言 51 / 1001 条会话均能遍历；新记录插入不破坏已持有目录锚；批量预览只读脱敏内容、不逐行拉完整历史。
- [ ] 在 `tests/test_session_query_api.py` 增加 tail / before / after 互斥验证、跨 stream 重复 sequence、超过 1000 事件、source_record 占满前页和 sequence 定位回归。
- [ ] 实现仓储分页及批量预览，再接薄路由和 parser。默认聊天排除 source_record；诊断仍可完整读取。用 `rg -n -F 'getSessionEvents(' frontend tests` 找齐所有位置参数调用及 browser fixture，同步改成 options；原 session 页面直到 Task 5 完成迁移前继续可用。
- [ ] 新增 `tests/test_native_agent_web_browser.py` 与 `tests/fixtures/frontend/native_agent_web_browser.cjs`，以现有 session browser 的真实 HTTP 启动方式验证 `directory` 场景：筛选、下一页、只读原因、离线历史可读、空态、加载错误和重试。
- [ ] 运行：

```sh
python3 -m pytest tests/test_native_session_directory.py tests/test_session_query_api.py tests/test_session_frontend_contracts.py -q
FLEET_PLAYWRIGHT_MODULE=/Users/mango/.codex/worktrees/openpet-final-acceptance/OpenPet/node_modules/playwright FLEET_BROWSER_CHANNEL=chrome python3 -m pytest tests/test_native_agent_web_browser.py -k directory -q
```

- [ ] 核对初次读取位于最后一页而不是扫完整历史，先前跳转 `?sequence=` 仍定位原事件。
- [ ] 提交 `feat: browse computer agent sessions with cursor history`。

**独立交付：** 用户可以在网页筛选、找到并阅读实际已同步的电脑会话；尚未具备控制权的行明确只读。

### Task 3：新建电脑 Agent 会话并立即反馈

**Files:** `conversation-target.js`、`agent-chat.js` / `service.js` / `state.js`、app / route / navigation / pages、Task API 及浏览器测试。

**Interfaces:** `mountAgentChat(root, {reference, service, store, identitySignal, onReferenceChanged})`；service 方法 `targets(machine)`、`createTask(frozenRequest)`、`getTask(taskId)`，均使用 Task 1 的真实接口。

- [ ] 浏览器增加 `new_native` 场景。暂扣创建 Task 的 HTTP 响应；提交后下一帧已显示文本和发送状态，响应释放前可切设置、返回草稿与发送记录保持。
- [ ] 测试机器切换使项目 / Agent 重新查询；迟到的旧机器响应不覆盖当前选择；中文输入法 Enter 不误发；双击不创建两个 Task。
- [ ] 实现选择器、冻结请求和任务等待。Task 回执返回后用原 ID 更新 URL；后端绑定 session 后原地接管；主助手入口沿用既有 conversation 路径。
- [ ] 测试刷新 `/agent-task/<id>` 后跟踪原任务；返回绑定 session；Task 明确失败时展示原错误与诊断入口；无 session 不能自动再创建。
- [ ] 再测试用户已离开页面时后台创建成功，只更新缓存身份，不抢当前路由；退出账号后迟到结果不能进入新账号页面。
- [ ] 运行：

```sh
FLEET_PLAYWRIGHT_MODULE=/Users/mango/.codex/worktrees/openpet-final-acceptance/OpenPet/node_modules/playwright FLEET_BROWSER_CHANNEL=chrome python3 -m pytest tests/test_native_agent_web_browser.py -k new_native -q
```

- [ ] 提交 `feat: start native agent chats from the conversation entry`。

**独立交付：** 从网页新建真实电脑 Agent 会话，发送后有即时反馈，并进入原任务绑定的原会话。

### Task 4：聊天正文、实时更新和回执对账

**Files:** shared schema、bridge / runner / control client / continuation capture、`agent-chat/state.js` / `live.js` / `messages.js`、session route、Web Components 引用与测试。

**Interfaces:** `record_user_turn(text, origin_command_id=None, origin_task_id=None)`；`ContinuationCapture.send(session_id, text, command_id=None)`；`createNativeChatState()`、`reduceNativeChat(state, action) -> newState`，action 为 `submitted` / `admitted` / `receipt` / `events` / `capabilities` / `connection`。副作用仅在 service / live，状态 reducer 不发 HTTP。

- [ ] 在现有 `tests/test_dispatch_capture_reliability.py` 及 `tests/test_runner_conversation.py` 增加 origin_command_id / origin_task_id 的真实采集→Hub 往返；两条同内容不同命令必须都保留；重复上传和一次原生回显不能多一条聊天消息；原始 source 证据仍存在。不另建重复的回显专项测试模块。
- [ ] 新增 `tests/test_native_chat_state.py`，由 Node 加载 ES module 检查确定性状态。关键断言：

```js
let state = createNativeChatState();
state = reduceNativeChat(state, {type: 'submitted', token: 'a', text: '继续'});
state = reduceNativeChat(state, {type: 'admitted', token: 'a', commandId: 'cmd_a'});
state = reduceNativeChat(state, {type: 'receipt', commandId: 'cmd_a', status: 'succeeded', reason: 'appended'});
if (state.pending.get('a').responseState === 'completed') throw Error('delivery is not reply completion');
```

- [ ] 实现当前视图单一增量调度，cursor 去重、向上加载与尾部追加独立；按身份对账 pending 与真实 CLI 用户消息。无 message identity 的源使用明确回执区域。
- [ ] 浏览器增加 `live_chat` / `recovery` 场景：模型回复受阻期间用户消息可见，先工具后最终回复；503 保留已有内容；SSE 断线后补读；回执 succeeded 后无回复不能显示完成；繁忙草稿保留；未知投递无新增 POST。
- [ ] 验证上翻不抢滚动、中文长文本 / Markdown / 工具结果完整展开；会话切换、五视图缓存淘汰、账号切换都没有残留定时器、重复请求或丢失焦点。
- [ ] 运行：

```sh
python3 -m pytest tests/test_native_chat_state.py tests/test_runner_conversation.py tests/test_native_tail_drain.py tests/test_dispatch_capture_reliability.py -q
FLEET_PLAYWRIGHT_MODULE=/Users/mango/.codex/worktrees/openpet-final-acceptance/OpenPet/node_modules/playwright FLEET_BROWSER_CHANNEL=chrome python3 -m pytest tests/test_native_agent_web_browser.py -k 'live_chat or recovery' -q
```

- [ ] 提交 `feat: render native conversation progress with command reconciliation`。

**独立交付：** 原生会话具备稳定聊天交互、增量工具 / 消息更新和真实投递反馈。此步若修改节点闭包，发布必须同时升级对应签名客户端再做生产验收。

### Task 5：工作面板、已有会话连接和能力驱动的控制

**Files:** `agent-chat/inspector.js` / `diagnostics.js`、session.js、machine.js、session-questions / handoff、menu / navigation / styles、模型接口与浏览器测试。

**Interfaces:** 面板消费经过 parser 的 session、message_delivery、capture、allowed_actions；service 提供 `control` / `getReceipt` / `listModels` / `configureModel` / `adopt` / `getHandoff`，直接复用现有业务路径。

- [ ] 浏览器增加 `capabilities` 场景，逐项验证 Hermes 可用即显示、只读 archive 无写按钮、撤销纳管立即移除写能力、ZCode 桌面只显示官方 bridge 投递及原回执。
- [ ] 验证活动实例连接 pending → adopted / rejected；PID 漂移不连接；浏览器正常账号在公网可以经过当前 Access / 账号边界读取和连接，不能使用注入的运维 service token 冒充普通用户验收。
- [ ] 模型切换测试覆盖 Codex Responses、Claude Anthropic、本机原配置恢复、busy 拒绝、配置成功前不抢发、目录变化时找回原命令；不显示 Pi / ZCode / Hermes 的假模型接管。
- [ ] 迁移现有原始事件、源记录、策略信号、问题卡片和 handoff，保持 sequence 链接与交互问题幂等；删除已被统一聊天输入替代的旧 follow-up textarea 与 Hermes 排除分支。
- [ ] 机器详情的新建入口指向预选机器的统一新建页；移除被替代的 `createTaskForm`、FALLBACK_AGENT_TYPES、PROMPT_CHIPS 及专属样式 / 测试选择器。尚在使用的实例连接与观测代码保留。
- [ ] 最近对话以 discriminated reference 区分来源；不将原生会话带入平台删除 / 归档 API。桌面 / 手机菜单、Esc、外点、键盘和 reduced-motion 沿用现有规范。
- [ ] 运行：

```sh
FLEET_PLAYWRIGHT_MODULE=/Users/mango/.codex/worktrees/openpet-final-acceptance/OpenPet/node_modules/playwright FLEET_BROWSER_CHANNEL=chrome python3 -m pytest tests/test_native_agent_web_browser.py tests/test_session_access_browser.py tests/test_session_management_browser.py tests/test_session_search_browser.py tests/test_navigation_sessions.py -q
python3 -m pytest tests/test_assistant_vm_sessions.py tests/test_assistant_session_capabilities.py tests/test_session_model_takeover.py tests/test_native_model_config.py tests/test_session_access_consistency.py -q
```

- [ ] 提交 `feat: manage computer agent sessions from the chat workspace`。

**独立交付：** 用户在网页阅读、连接、续聊并执行当前会话真实可用的控制操作，页面首屏保持简洁。

### Task 6：端到端验收、清理文档与上线

**Files:** `docs/testing/journeys.json`、testing README、session-conversations、README 最新更新 / release notes、现有 CI / 静态发布检查需要覆盖的新增资源。

- [ ] 新流程登记 `NATIVE-CHAT-01`：真实账号 Cookie → targets → Task → 真实 fixture 子进程 → capture → Hub → 网页首轮 / FOLLOW；测试不能在浏览器替换产品状态机，也不能仅 mock 返回一句成功文本。
- [ ] 同步修改现有测试引用和文档中的旧 `after_sequence` 为实际 Hub cursor 语义；更新真实支持的来源、模型配置范围及三种回执状态。
- [ ] 本机执行本任务相关回归；完整发布继续运行仓库现有 Linux `--require-journeys`、Windows launch-policy、macOS Agent、package 及 deploy gates，不把缺失外部条件标为通过。
- [ ] 新鲜 fetch、rebase 最新 main、完整检查候选差异；按原发布流程提交 GitHub 并合并，跟进该 main run 的所有门禁和自动部署。
- [ ] 若节点文件闭包变化，发布签名客户端，先确认原运行工作不会被打断，再按现有 SOP 升级目标 Mac / Windows / Linux；核对版本、签名、闭包和回滚锚。
- [ ] 公网使用正常测试账号浏览新建和目录；保留旧静态缓存普通导航验证同 run 的新资源，核对自动和独立公网逐字节报告、health 与数据库一致性。
- [ ] 在独立测试项目创建 FIRST：读取预置 UTF-8 文件 / README；FOLLOW：在同 native ID 回忆唯一标记。核对真实工具结果、唯一输入、原命令回执、工作目录和输出。Mac / Windows 的已安装 Agent 分别验收；Linux 选已配置真实节点实测后才能标三端通过。
- [ ] 对用户原电脑会话先只读验证；不会向无关正在工作的会话发送测试文本。ZCode 桌面使用独立测试对话验证原 operation、队列和消息分页。
- [ ] 故障验收包括断线 / 页面刷新 / busy / 未安装 / 模型503 / 配置变更 / 撤销纳管；保留失败原任务，不能重建同名任务覆盖证据。
- [ ] 在现有 canonical 运维与项目索引记录版本、真实场景和剩余边界，运行 vault doc-check；最终报告明确哪些节点 / Agent / 来源通过、哪些未安装或未执行。

**完成标准：** 用户无需 SSH、无需主助手替代点击，就能在网页为真实电脑节点创建 Agent 会话、读已有记录、继续受支持会话并看到全过程；生产资源和实际节点行为均有独立证据。

## 8. 必须覆盖的失败场景

| 场景 | 正确结果 |
|---|---|
| Task 已创建但响应丢失 | 同 token 找回原 Task，不启动第二个进程 |
| Task 还未生成 session | 显示排队 / 节点启动 / 等待会话绑定，继续原 task_id |
| 绑定前页面刷新或切页 | 恢复原任务；后台成功不抢路由 |
| 命令已入队但 receipt 读取503 | 重试原 GET，消息不重新 POST |
| CLI receipt 为 appended，模型尚未回复 | 显示已投递 / 等待输出，不显示已完成 |
| 发送瞬间会话变 busy | 无 command_id 时保留草稿并显示繁忙；已有 ID 时只查原操作 |
| Hub / 节点 / 浏览器重启 | 保留原 task/session/native identity 与 cursor，补读不重写 |
| 多个 stream 使用相同 sequence | 以 Hub cursor / event_id 保留全部合法事件 |
| 正常连续发送两次相同“继续” | 两次用户操作、两个唯一命令和回合，不能按文本误合并 |
| 原生源有回显 | CLI origin_command_id 对账及 PR107 回显规则避免重复气泡，source 证据仍存在 |
| 历史 JSONL 同步未完 / 配额问题 | 显示已保存内容与同步边界，不能声称历史完整 |
| 用户撤销纳管或节点 PID 漂移 | 原读历史仍在，写能力及时消失，禁止对共享桌面进程发信号 |
| 跨平台 Agent 未安装或未知安装 | 分别显示未安装 / 待确认，不能都写“系统不支持” |
| ZCode desktop busy 队列 | 显示官方已排队及原 operation，不能换成 CLI 新会话 |
| 模型接管失败或原目录变更 | 明确回执，不用旧模型冒充已切换；原命令可查询 |
| Agent 仅输出完整消息 | 状态 / 工具先可见，完整消息到达后呈现，无假 Token 流 |
| 登录账号切换 | 销毁旧私有视图和订阅，不把前一个账号 pending 发到新账号 |
| 直接打开新路由 / 旧缓存升级 | HTML 与路由可访问；资源使用当前发布 SHA，浏览器无白屏 |

## 9. 本次范围与后续能力

第一版完整覆盖统一入口、目标选择、网页创建、电脑目录、已有可读记录、受支持原会话续聊、即时反馈、实时事件、回执恢复、模型与控制面板，以及三端验收。优先顺序是先可读和可恢复，再开放聊天与控制；不是先画全套按钮再补执行语义。

以下能力有明确额外协议成本，未作为现有能力承诺：

- 任意 Agent 桌面应用的点击接管；当前只有已接入 ZCode 官方 bridge 的具体会话路径。
- 电脑所有应用全部离线存档的自动全库枚举；当前复用已支持的同步来源。
- 将任意只读旧历史无条件变成可写会话；当前需要精确身份、工作目录和受支持执行机制。
- 向所有本地 Agent 首轮注入平台模型；当前 Task 创建使用本机配置，已有 Codex / Claude 会话可以按能力配置下一次续聊。
- 所有 Agent 逐 Token 与公开 reasoning 流；需要源端协议真的提供并有规范化事件合同。
- 普通停止本轮后继续的 `interrupt_turn`；当前 terminate/cancel 语义不等同于该能力。
- 不同节点 / Agent 之间的原会话迁移；现有 handoff 明确创建新任务并带快照，不能假称 native identity 没变。

## 10. 可确认的替代清理清单

仅在相应新流程通过测试后移除：

1. `session.js` 的旧 follow-up 输入区、Hermes 硬编码排除与已迁移诊断渲染：统一聊天 composer 和诊断模块已覆盖其行为。
2. `machine.js` 中重复的任务创建表单及专属 fallback 类型 / prompt chips / 样式 / 测试选择器：机器入口转为同一个预选目标的新建页面。
3. `AgentSessionService` 中迁移到共享应用路径的重复原命令匹配 / 找回实现：网页与主助手使用同一实现，保留有效在途幂等身份。
4. 文档中已经失真的参数和能力描述：例如 `after_sequence`、仅 VPS 文案、Hermes 一律无法续聊的描述。

保留仍有调用的 task 详情、session search、session 问题卡片、handoff、原始证据与合法 v1 查询接口。本计划不据文件年龄判断废弃，也不新增 deprecated 空壳或通用兼容层。

## 11. 计划自审

- 用户目标：新建时使用电脑 Agent、网页阅读已有会话、继续和控制，分别落到 Task 2 / 3 / 4 / 5。
- UI / 后端解耦：transport 在 client / HTTP；应用服务拥有准入、幂等、模型和身份；state / renderer 不读节点文件、不签命令。
- 真实边界：Mac/Windows 已安装矩阵、Linux 真实验收、archive、adoption、CLI 与桌面桥分别记录。
- 反馈链路：即时 pending、Task 绑定、命令、原生事件和完成语义分开；未知不重放。
- 数据与生命周期：不同 ID 明确类型，cursor 为字符串，分页不扫全库；设置覆盖、视图缓存、账号更换及路由抢占均有场景。
- 发布：保留当前静态资源版本化、公网核对、三端门禁、签名节点升级与回滚证据。
- 本轮状态：仅调研与计划，产品实施步骤均未勾选，不能对外写“新前端已上线”。

## 关联文档

- [现有对话采集与控制合同](../../session-conversations.md)
- [双端 Agent 适配与验收](../../testing/agent-platform-acceptance.md)
- [测试与完整发布门禁](../../testing/README.md)
- [ZCode 原桌面对话桥](../../../integrations/zcode/README.md)
- Obsidian canonical：`01.项目/agent-fleet/agent-fleet 文档索引.md`、`Note/Infra/agent-fleet 部署与运维.md`；项目索引登记本计划与研究基线，实施和发布后更新同一入口。
