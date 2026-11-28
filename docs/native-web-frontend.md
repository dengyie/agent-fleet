# 电脑 Agent 网页前端接入

本轮仅实现静态前端与前端测试。总体设计见[网页工作区计划](superpowers/plans/2026-11-21-native-agent-web-workspace.md)，原生记录与控制协议见[会话文档](session-conversations.md)。后端与节点由独立工作流维护；本文件区分已消费的公开 API 与等待后端提供的能力。

## 页面与职责

AGY 多 Google 账号通过[账号 API](native-provider-accounts.md)接入。
`/settings/provider-accounts` 集中管理所选节点的账号创建、登录、授权结果提交、
取消登录、启停和退出登录；状态及命令回执持续轮询。节点必须在线并上报已安装
AGY 与账号 schema 1，才允许新操作。账号名称只是标签，不代表已验证的邮箱。

电脑 Agent 新建会话在选择 AGY 后显示 Google 账号选择器，只有已启用且 `ready`
的账号可选。明确选择的账号不可用或目录读取失败时暂停发送，不能静默换为
节点默认登录。同一节点刷新目标目录或切换 Agent 后返回 AGY，仍保留原账号选择；
只有主动更换节点或账号才重置选择。未指定独立账号仍可使用原默认登录。启动、响应丢失恢复及后续
原生续聊固定原 `provider_account_id`；工作面板展示绑定标识，不提供中途换号。

账号写请求由用户明确触发，结果未知时保留原 token 和请求供恢复；刷新页面仍
沿用同一请求。授权码只保留在当前视图内存，不写入浏览器存储；若响应丢失后
刷新页面，需重新输入同一授权结果。已知命令只查询原回执。登录回执成功不等于
账号可用，仍以账号 `ready` 状态为准。关闭或挂起设置页会清空授权输入。

| 入口 | 行为 |
|---|---|
| `/assistant` | 保留主助手；空白页可切换电脑 Agent / 已有会话 |
| `/assistant?view=computer&machine=<可选节点>` | 节点 → 登记项目 → Agent → 发送目标 |
| `/settings/provider-accounts` | 所选节点的 AGY Google 账号目录与授权管理 |
| `/assistant?view=sessions&machine=<可选节点>` | 查看最近同步记录，筛选节点及已载入的 Agent / 标识；正文搜索使用原搜索页 |
| `/task/<task_id>?view=chat` | 查询原启动任务，等待其真实 session_id，原地更新地址 |
| `/session/<session_id>?view=chat` | 消息、工具、提问、即时投递反馈、工作面板 |
| `/session/<session_id>` | 原始分页时间线、定位链接、纳管、本机 profile 与接力等已有诊断能力 |

使用既有可直接刷新的文档路径，不新增 Hub 页面路由。普通成员账号继续遵循现有账号边界；电脑会话入口及路由限管理员 / 运维身份。

- `api/client.js`：唯一 HTTP 边界，负责 URL、认证、超时、取消和响应 parser。
- `api/contracts.js`：公开字段投影；安装状态保留 true / false / null，不从 Agent 名称推导权限。
- `views/agent-chat/service.js`：原 Task、原会话及原命令的用例适配，不创建平台 conversation 或 Run 副本。
- `views/agent-chat/state.js`：纯事件合并、分片消息整理和工具展示模型；用 Hub cursor 标识事件，不按文本或原生 sequence 去重。
- `views/agent-chat/live.js`：单一可见读取调度；SSE 摘要、focus、resume 唤醒。失败退避，挂起中止读，迟到结果不发布。
- `messages.js` / `inspector.js`：消息与工作面板展示；复用 StreamMarkdown、ToolCallBadge、ChatPromptInput、AutoScrollAnchor、UiIcon 和现有 inspector。
- `operations.js`：工作面板中的授权说明、本机配置和运行控制；只消费当前会话声明的动作与节点登记的配置，不推导平台或 Agent 权限。
- `conversation-target.js` / `sessions.js`：选择器和目录；不读取节点本地路径、不生成 CLI 参数。

## 已消费的接口

| 接口 | 前端行为 |
|---|---|
| `GET /api/status` | 节点与产品功能开关；不把在线观测等同于控制能力 |
| `GET /api/sessions?machine=&limit=100&cursor=&capability=` | 稳定快照目录分页；能力筛选由后端当前权限投影决定，Agent / 标识筛选限定在已载入结果内 |
| `GET /api/sessions/<id>` | 真实 allowed_actions、message_delivery、纳管状态、采集与执行状态 |
| `GET /api/sessions/<id>/events?tail=1&limit=100` | 首屏读取最近事件；`before_cursor` 只用于显式向前加载历史，`after_cursor` 独立用于实时增量 |
| `POST /api/tasks` | 冻结目标、正文和 client_token；同一页内重试同一启动时复用冻结请求 |
| `GET /api/tasks/<id>` | 原任务与真实会话绑定；提交返回或绑定返回不得抢走其它活动页面 |
| `POST /api/sessions/<id>/messages` | 普通消息发送 `{text, client_token}`；响应未知时用户可显式用原 token 和原正文恢复，问题回答继续用独立协议 |
| `POST /api/sessions/<id>/control` | 当前能力声明允许的具体操作；终止和隔离需在页面确认其后果 |
| `GET /api/supervisor/receipts/<id>` | 查询原命令，核对 session_id；正文故障与回执读取分别处理 |
| `GET /api/machines/<machine>` | 打开操作面板时读取匹配当前 Agent 的本机配置；保留未提交选择，提供刷新和错误反馈 |
| `GET/POST /api/sessions/<id>/handoff` | 在接力页签复用既有上下文预览与同 token 提交；创建后仍到任务页确认放行 |

侧栏最近条目带电脑来源图标。原生条目不会获得平台重命名、归档或删除动作。原始时间线保留为可用诊断入口；现有机器任务表单没有被尚未集成的选择器删除替代。

## 后端已提供：登记目标与任务恢复

GitHub main `54a93a2`（PR #110）已提供目标目录和 Task `client_token` 找回。缺失时发送按钮禁用，明确提示已有记录仍可阅读。

```http
GET /api/machines/<machine>/task-targets
```

```json
{
  "machine": "my-mac",
  "projects": ["agent-fleet"],
  "observed_at": "2026-11-21T10:00:00Z",
  "agents": {
    "codex": {"installed": true},
    "claude_code": {"installed": null},
    "hermes": {"installed": false}
  }
}
```

安装三态只是启动提示；项目准入、可执行 family 和当前在线性仍由任务创建服务验证。`observed_at` 可为 null；不得返回本地可执行路径、密钥或原生数据库信息。

## 后端新会话接口的集成状态

- **已实现的前端适配**：session `tail` / `before_cursor` / `after_cursor` 三向读取，稳定目录 cursor，普通消息操作 token 和按用户动作重放；token 仅保存在当前标签页 sessionStorage，并按 API、账号、session 隔离。恢复按钮重放原文本和 token，不自动重发。
- **后端接口已发布**：分页、消息 token 和任务找回接口已由 PR #111 合入 main（`d3e77f0`）。本前端适配需基于该 main 版本联调后一起发布；旧线上后端若尚未升级，不支持这些请求和响应字段。
- **响应未知找回语义**：收到 HTTP admission 后显示 queued/receipt 状态，不等同 Agent 已完成；只对用户显式点击的恢复操作重放冻结 token 和正文。刷新本页会恢复未确认操作卡片，但不会自动发送 POST。
- **模型目录切换**：新任务沿用本机模型配置。已有会话可在工作面板“操作”中选择节点上报、匹配当前 Agent 的本机 profile；同时受 feature flag 和 `allowed_actions` 约束。后台尚无网页专用 models / model 路由，前端不借用主助手工具 RPC，也不伪造任意 Agent 的模型切换能力。
- **源端流式边界**：页面实时显示源端实际产生的可见回答增量、完整消息快照、工具调用/结果和状态活动；Agent 未提供增量正文时按完整消息呈现。聊天读取带 `include_source=0`，不下载 source_record；内部 `thinking` / `reasoning`、系统提示词和原始记录字段不会作为对话内容展示。

## 会话名称与详细信息

目录和聊天标题优先使用节点原生明确提供的会话名；缺失时以首条已脱敏用户消息作为明确标注的回退。目录卡和工作面板展示客户端/采集适配器、客户端版本、provider、模型、原生开始时间或 Hub 首次采集时间、最后活动和持续时间。开始时间缺少原生来源时不从普通消息时间推断。

消息正文与工具结果来自带 Hub ingestion cursor 的逐事件 HTTP 记录。`message_id` 将同一原生回答的 delta 或 snapshot 更新合并；快照覆盖当前消息文本，delta 追加文本。交互问题及回答、工具、状态变化也显示在时间线。Pi、ZCode rollout 和 ZCode 桌面分页等不提供增量流的记录保持真实完成态，不模拟打字。ZCode 桌面会话只投影原生会话摘要明确提供的标题、模型与 provider，并在变化时记录。source_record 仍是独立诊断存档，原生聊天不请求该存档。

ZCode 桌面桥的正文与工具记录在 Agent 空闲后补采，工具完成/失败事件沿用现有折叠卡片显示参数与结果。页面仅在公开 `agent_evidence.protocol` 确认 `desktop_bridge` 且 family 为 `zcode` 时提示该同步方式，并说明采集顺序不等于完整历史；不据 Agent 名称推断实时能力。其余原生/CLI 会话保持各自实际增量行为。实现计划见[桌面采集前端适配](superpowers/plans/2026-11-27-zcode-desktop-capture-ui.md)，后端边界见[会话 API](native-session-api.md#zcode-desktop-tool-event-capture)。

## 验证

`tests/test_native_chat_frontend.py` 包含纯状态 / 契约 / 单一读者生命周期检查，以及真实 Hub、SQLite、签名命令队列和 Chrome 的网页验收。新建目录在测试 app 中使用上面的明确接点，任务发行、绑定与读取调用产品服务；不运行用户电脑上的 vendor CLI。

浏览器场景覆盖 1440 / 390px：已有记录与工具结果、即时发送卡、持续消息增量、响应丢失不重复投递、设置浮层返回、手机工作面板、目录过滤、撤销权限、缺失目标接口，以及 Task → 原生 session 原地绑定与刷新。

`zcode-desktop-tools` 场景从真实临时 SQLite 经节点 `read_events`、Hub 入库、公开 HTTP 到浏览器，验证 Read/Bash 工具的参数、完成/失败结果、延迟补采、重放去重、展开状态保留、字面文本渲染、隐藏 reasoning 排除及手机宽度。该测试不启动用户电脑上的 ZCode，也不调用模型。

2026-11-22 前端分页与恢复补充：聊天首读使用 tail，历史页和实时增量使用独立游标；目录使用 snapshot cursor 的加载更多；普通消息持久化当前账号 / 会话下的同 token 操作，结果未知时只由用户点击恢复。配套浏览器验收需在后端 session API 变更合入后运行。

2026-11-21 交互回归补充：长消息展开后的完整原文随真实片段更新；已完成投递卡在读轮询时保留同一 DOM 与展开状态；节点最初离线时可点击“刷新节点与目录”重新读取状态与目录，恢复后启用发送，不重复追加节点或产生写请求。刷新和初始节点查询均接受页面生命周期的取消信号。

2026-11-23 会话详情补充：原生名称优先、首条用户消息回退、客户端/版本/provider/model 与起始/持续时间展示；Codex snapshots/deltas 和 Claude visible text deltas 按原生消息 ID 投影。source_record 通过 `include_source=0` 从聊天 HTTP 响应排除；隐藏推理内容不会进入消息流。

2026-11-23 操作面板补充：新聊天可直接进入所选节点会话与节点管理；主助手工作面板的节点卡也提供这两个入口。已有会话工作面板分为会话、操作、接力、投递记录：消息/上下文通过输入框注入，配置与运行控制使用既有签名命令队列，接力使用既有证据预览与任务协议。读取权限失败或页面挂起后，下一次验证完成前停用新写入；已入队命令仍可查询，不自动重放。终止/隔离/取消均需明确确认。目录可按可续聊、可控制、仅阅读筛选，范围仍为已载入的会话。

新增浏览器验收覆盖本机 profile 按 Agent 过滤与轮询保留选择、精确控制载荷、打开页面后撤销权限、权限读取 503、危险操作取消与单次确认、节点快捷导航，以及聊天工作面板中的接力预览/提交/同 token 恢复。均使用临时 Hub/SQLite 与命令入队，不执行真实电脑 Agent 或修改线上会话。

```sh
FLEET_PLAYWRIGHT_MODULE=/path/to/existing/playwright FLEET_BROWSER_CHANNEL=chrome \
python3 -m pytest tests/test_native_chat_frontend.py -q
```

原始实现最初基于公开 main `d882c5c` 验证：前端契约、静态分离、发布打包与 capability parser **185 passed / 3 skipped**；新网页、导航及设置浏览器回归 **55 passed**；更早的旧时间线、导航及设置组合回归是在 PR #108 前的 `73e0db0` 基线上 **61 passed**，不作为新基线的证据。各轮有重复用例，不相加为总覆盖数。独立静态包生成 100 个文件，未修改部署脚本。

2026-11-22 前端分页 / 幂等恢复实现：原生聊天专属 API、契约、静态分离、能力、导航及前端浏览器组合 **154 passed / 45 skipped**；使用 `codex/native-session-api-hardening` (`592aafe`) 后端工作树启动真实 Hub/SQLite/HTTP + Chrome 的聊天验收 **15 passed**（桌面和窄屏），另有原 session 诊断页与导航浏览器组合 **15 passed**，覆盖目录加载更多、聊天 tail/before/after、普通消息刷新恢复、Task 刷新恢复和响应未知显式重放；同后端分支的 HTTP/SQLite API 回归 **269 passed / 10 subtests passed**。静态发布打包 **100 files**，独立静态服务冒烟通过。以上新接口集成基于未合入分支，不代表 GitHub main 或线上已提供。

后续迭代重新验证：原生聊天、前端契约/隔离、能力展示、导航、设置、真实浏览器和静态发布组合 **244 passed**；CI 失败项所在的安全写入/发布布局/主助手提交与原生聊天组合 **65 passed / 2 subtests passed**（与前者重叠，不相加）。修复前已本地复现原 CI `44905923972` 的同样 **10 failed / 7 passed**；根因是测试 DOM 未覆盖标准 append/prepend、安全检查没有沿共享 DOM helper 追踪，以及新模块未加入发布文件清单。本轮同步补齐这些测试契约，并新增长消息、投递卡展开和离线后刷新三个浏览器回归。静态包仍为 100 文件；完整跨平台 GitHub CI 状态以 PR #109 的实际检查为准。

配套门禁与旧界面回归入口见[测试索引](testing/README.md)。这些结果证明前端与公开服务的本地集成，不代表真实三端 CLI、外部模型或线上新建目录已完成验收。

## 原任务结果查看

聊天工作面板新增“任务结果”页签，使用现有 `GET /api/tasks/<id>`、`/diff`、
`/files` 和 `/files/<path>`。仅在点击“查看原任务结果”或“读取文件快照”时读取；
不会查询 VM 文件系统。任务 ID 只来自当前启动任务或会话事件中的 `task_id`，
读取前后均核对任务 DTO 的 `session_id`，防止任务重试重新绑定后展示错误结果。
旧历史 tail 页未包含关联元数据时提示加载更早记录；原生导入会话可能没有启动任务。

面板展示原任务状态、已有结构化测试摘要、变更统计、日志摘要、diff 和文件快照。
截断/脱敏按 API 标记显示，所有源代码作为纯文本渲染。明确标注为原启动任务结束时
保留的快照；续聊后的修改和测试不会更新该快照。暂无结果、空文件目录、读取错误均
可见并可重试；挂起、销毁或身份变化会中止读取，迟到响应不能发布到新会话。

Google Antigravity CLI 以 `agy` 从目标目录动态出现，遵循现有启动、读取与消息
投递接口。网页不能据此推断任意旧会话可续聊或可切换模型。

## 原生工作区可靠性回归

- 节点命令进入 failed / expired / rejected / cancelled 时，保留公开回执的
  `reason`，同时显示在消息投递卡、操作反馈和投递记录中。终态不会触发自动重发。
- 原生消息的最终 snapshot 可以缩短或替换先前正文；不足完整原文阈值时移除旧的
  原文展开区，避免最终回答与展开内容矛盾。
- 不同文件快照独立加载；打开第二个文件不会取消第一个文件。刷新整个结果、挂起、
  销毁或切换关联会话才统一取消。旧请求完成不能覆盖新文件内容或解锁新请求按钮。
- 结果面板以路由 / Task 响应已确认的会话标识建立关联，不等待会话详情才能确定
  身份；详情读取较慢时，返回后不会清空已加载的同一任务结果。
- 接力首次响应丢失后，后续恢复即使收到 4xx 也保留原 token、目标和指令。
  恢复请求被限流不代表原任务未创建；首次提交被明确拒绝时仍允许修改表单。

`tests/test_native_chat_frontend.py` 的 snapshot-revision、receipt-failure 和
result-file-concurrency 场景覆盖以上消息、回执、并发文件与迟到响应行为；
task-results-binding 覆盖 Task → 原生会话绑定过程中慢详情响应与结果读取的竞态；
`tests/test_handoff_routes.py` 的真实浏览器场景覆盖“已创建但响应丢失 → 429 →
原凭据找回”，同时验证聊天面板和原始时间线入口，桌面与手机宽度均执行。

## 原生传输与进程回收反馈（PR137/138 后端适配）

后端的 ACP 增量分帧、写截止、桌面检查点原子替换与进程回收确认沿用现有 HTTP/SQLite DTO；前端不增加私有接口、CLI 参数或进程状态推断。明确的 `agent_state_changed`（`completed` / `failed` / `stopped`）及 `session_close` 只结束同一 `stream_id` 的未完成正文等待，失败/停止时保留正文并显示“输出已中断”。缺失结果的工具标记“结果未知”，迟到的真实工具结果仍可更新卡片，展开状态不丢失；其他采集流和后续新消息继续正常更新。`unknown` 状态不作为执行终态。

原控制回执中的 `escape_unverified` 与 `liveness_unknown` 显示中文原因和检查建议，同时保留诊断码。页面仍只提供当前公开 `allowed_actions` 允许的操作；不自动重新发送消息或执行终止。节点本地 `desktop_checkpoint_invalid` 没有新增公开字段，前端不伪造诊断或自动重置源端检查点。

回归入口：`tests/test_native_chat_frontend.py` 中的 `test_native_stream_interruption_projection`、`native-stream-interruption` 和 `cleanup-receipt`。后两者用真实 Hub/SQLite 和签名命令队列，在桌面/手机 Chrome 验证增量、失败、迟到结果与控制失败反馈；不运行真实模型或读取隐藏推理。实施与发布清单见[原生运行时前端适配计划](superpowers/plans/2026-11-27-native-runtime-ui.md)。

## 会话内执行详情

电脑 Agent 和主助手正文中的工具卡默认展开，直接展示公开 API 提供的调用参数、执行结果与错误详情。JSON 容器格式化缩进，命令和普通输出保留原文；没有参数、等待返回、结果未知及已完成但无输出分别显示明确提示。长内容在卡片内滚动，页面不截掉内容。工作面板及 Muse 沿用原来的默认收起行为。

工具卡的默认展开仅在首次挂载生效。增量、迟到结果与快照更新复用原按钮和详情 DOM，保留用户手动收起、键盘焦点、未变动输入区的选择和滚动。刷新页面后依照服务端历史恢复内容，折叠状态不跨页面刷新保存。所有动态字段作为纯文本展示。

原生会话按公开游标顺序将**同一采集流的相邻状态事件**合并到“执行过程”折叠区；不会跨正文、工具或其他流把后续步骤移动到前面。已知状态原因显示中文，未知原因不推断；`unknown` 仍不作为执行终态。主助手用相同折叠组件显示公开 thinking/waiting/responding/tool_call 阶段和运行结束说明，其公开工作说明与工具卡仍按进度投影顺序展示。

当前后端没有公开推理正文的 API；原生 normalization 和主助手进度投影均不发布私有 thinking/reasoning 文本。这次前端展示现有公开状态、工作说明、步骤和工具事实，不读取 archived source records 或声称存在完整思维链。若需供应商允许公开的思考摘要，需要后端提供明确的公开契约后再接入。

测试复用 `native-stream-interruption`、`zcode-desktop-tools` 和 `test_assistant_live_model_stream_retry_and_reload` 的真实 Hub/SQLite/Chrome 流程，补充默认可见、折叠保留、焦点/文本选择、迟到结果、字面 HTML 与手机无溢出断言。实施清单见[执行详情计划](superpowers/plans/2026-11-28-conversation-tool-details.md)。

## 前端状态与生命周期 review

执行详情上线后的 review 检查路由与身份守卫、HTTP/SSE/store、主助手和原生聊天、
目标与账号选择、设置、节点和任务诊断、接力、集成与工作流、插件、监控、
用量、搜索和 Muse，并复核移动端及资源版本契约。修复范围只涉及前端与回归测试，
不改变公开 API、节点执行策略或授权边界。实施清单见[生命周期 review 计划](superpowers/plans/2026-11-28-frontend-lifecycle-review.md)。

| 已确认问题 | 根因与修复 | 回归场景 |
|---|---|---|
| 刷新节点目录后，下一条任务可能改用第一个项目或 Agent | 刷新清空 select 导致显式选择丢失；按当前节点独立保存选择，已移除目标保留空选项并暂停发送，等待用户重新选择 | `target-selection`：非首项刷新、项目/Agent 删除、改变另一字段后再次刷新 |
| 返回缓存会话或有效身份重新验证后跳离正在阅读的位置 | 脱离布局时滚动偏移丢失，跟底组件重连；组合层在隐藏前保存偏移，身份验证成功后先恢复布局，再还原位置并通知现有跟底组件 | `cached-scroll`：1440/390px 阅读历史、离开和返回、有效身份重新验证、保持非跟底状态 |
| 关闭产物预览后，迟到响应再次将其打开；加载中重开会重复请求 | 请求结果覆盖用户当前展开状态；只合并数据和加载状态，复用在途/已加载预览，核对工作区与请求归属 | `artifact-disclosure`：成功/失败迟到、加载中开合、缓存重开 |
| 旧实时连接或已停止轮询仍可更新状态或触发身份检查 | 消息处理和 HTTP/body 完成缺少当前 source/controller 归属检查；停止及替换后的回调不再发布 | `test_sse_lifetime_ownership_fixture`：旧源事件、迟到 200/401/JSON、替代请求继续工作 |
| 从用量、Muse、插件、会话搜索进入登录后返回主助手 | 登录返回地址的已有页面允许名单遗漏；补齐现有路由，继续拒绝外站地址和登录循环 | `test_assistant_auth_in_real_browser`：匿名各入口、登录返回及身份/草稿恢复 |

登录浏览器回归同时修正了测试同步：侧栏两个历史源分别发布，中间空态不代表
删除已完成。测试用受控响应明确重现这个窗口，并等待删除确认弹窗关闭后才
填写草稿，防止把不可交互期间未填入的文本误报为设置页清空草稿。

本轮没有确认必须删除的废弃模块或重复实现。机器、任务和原始会话诊断页仍有
导航与诊断调用；文件较大本身不构成重写依据。前端保持 HTTP/DTO、用例服务、
纯状态投影和展示层的现有边界；后续拆分应围绕可独立验证的职责进行。
