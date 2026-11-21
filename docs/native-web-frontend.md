# 电脑 Agent 网页前端接入

本轮仅实现静态前端与前端测试。总体设计见[网页工作区计划](superpowers/plans/2026-11-21-native-agent-web-workspace.md)，原生记录与控制协议见[会话文档](session-conversations.md)。后端与节点由独立工作流维护；本文件区分已消费的公开 API 与等待后端提供的能力。

## 页面与职责

| 入口 | 行为 |
|---|---|
| `/assistant` | 保留主助手；空白页可切换电脑 Agent / 已有会话 |
| `/assistant?view=computer&machine=<可选节点>` | 节点 → 登记项目 → Agent → 发送目标 |
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
- `conversation-target.js` / `sessions.js`：选择器和目录；不读取节点本地路径、不生成 CLI 参数。

## 已消费的接口

| 接口 | 前端行为 |
|---|---|
| `GET /api/status` | 节点与产品功能开关；不把在线观测等同于控制能力 |
| `GET /api/sessions?machine=&limit=100&cursor=` | 稳定快照目录分页；Agent / 标识筛选明确限定在已载入结果内 |
| `GET /api/sessions/<id>` | 真实 allowed_actions、message_delivery、纳管状态、采集与执行状态 |
| `GET /api/sessions/<id>/events?tail=1&limit=100` | 首屏读取最近事件；`before_cursor` 只用于显式向前加载历史，`after_cursor` 独立用于实时增量 |
| `POST /api/tasks` | 冻结目标、正文和 client_token；同一页内重试同一启动时复用冻结请求 |
| `GET /api/tasks/<id>` | 原任务与真实会话绑定；提交返回或绑定返回不得抢走其它活动页面 |
| `POST /api/sessions/<id>/messages` | 普通消息发送 `{text, client_token}`；响应未知时用户可显式用原 token 和原正文恢复，问题回答继续用独立协议 |
| `POST /api/sessions/<id>/control` | 当前能力声明允许的具体操作；终止和隔离需在页面确认其后果 |
| `GET /api/supervisor/receipts/<id>` | 查询原命令，核对 session_id；正文故障与回执读取分别处理 |

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
- **模型目录切换**：新任务沿用本机模型配置。后台尚无网页专用 models / model 路由，前端不借用主助手工具 RPC，也不伪造任意 Agent 的模型切换能力。本机 profile 的既有入口仍在原始时间线中。
- **源端流式边界**：页面实时显示已产生的消息、工具和状态；Agent 未提供增量正文时等待真实内容。不会生成模拟打字或把 source_record 当思考内容。

## 验证

`tests/test_native_chat_frontend.py` 包含纯状态 / 契约 / 单一读者生命周期检查，以及真实 Hub、SQLite、签名命令队列和 Chrome 的网页验收。新建目录在测试 app 中使用上面的明确接点，任务发行、绑定与读取调用产品服务；不运行用户电脑上的 vendor CLI。

浏览器场景覆盖 1440 / 390px：已有记录与工具结果、即时发送卡、持续消息增量、响应丢失不重复投递、设置浮层返回、手机工作面板、目录过滤、撤销权限、缺失目标接口，以及 Task → 原生 session 原地绑定与刷新。

2026-11-22 前端分页与恢复补充：聊天首读使用 tail，历史页和实时增量使用独立游标；目录使用 snapshot cursor 的加载更多；普通消息持久化当前账号 / 会话下的同 token 操作，结果未知时只由用户点击恢复。配套浏览器验收需在后端 session API 变更合入后运行。

2026-11-21 交互回归补充：长消息展开后的完整原文随真实片段更新；已完成投递卡在读轮询时保留同一 DOM 与展开状态；节点最初离线时可点击“刷新节点与目录”重新读取状态与目录，恢复后启用发送，不重复追加节点或产生写请求。刷新和初始节点查询均接受页面生命周期的取消信号。

```sh
FLEET_PLAYWRIGHT_MODULE=/path/to/existing/playwright FLEET_BROWSER_CHANNEL=chrome \
python3 -m pytest tests/test_native_chat_frontend.py -q
```

原始实现最初基于公开 main `d882c5c` 验证：前端契约、静态分离、发布打包与 capability parser **185 passed / 3 skipped**；新网页、导航及设置浏览器回归 **55 passed**；更早的旧时间线、导航及设置组合回归是在 PR #108 前的 `73e0db0` 基线上 **61 passed**，不作为新基线的证据。各轮有重复用例，不相加为总覆盖数。独立静态包生成 100 个文件，未修改部署脚本。

2026-11-22 前端分页 / 幂等恢复实现：原生聊天专属 API、契约、静态分离、能力、导航及前端浏览器组合 **154 passed / 45 skipped**；使用 `codex/native-session-api-hardening` (`592aafe`) 后端工作树启动真实 Hub/SQLite/HTTP + Chrome 的聊天验收 **15 passed**（桌面和窄屏），另有原 session 诊断页与导航浏览器组合 **15 passed**，覆盖目录加载更多、聊天 tail/before/after、普通消息刷新恢复、Task 刷新恢复和响应未知显式重放；同后端分支的 HTTP/SQLite API 回归 **269 passed / 10 subtests passed**。静态发布打包 **100 files**，独立静态服务冒烟通过。以上新接口集成基于未合入分支，不代表 GitHub main 或线上已提供。

后续迭代重新验证：原生聊天、前端契约/隔离、能力展示、导航、设置、真实浏览器和静态发布组合 **244 passed**；CI 失败项所在的安全写入/发布布局/主助手提交与原生聊天组合 **65 passed / 2 subtests passed**（与前者重叠，不相加）。修复前已本地复现原 CI `44905923972` 的同样 **10 failed / 7 passed**；根因是测试 DOM 未覆盖标准 append/prepend、安全检查没有沿共享 DOM helper 追踪，以及新模块未加入发布文件清单。本轮同步补齐这些测试契约，并新增长消息、投递卡展开和离线后刷新三个浏览器回归。静态包仍为 100 文件；完整跨平台 GitHub CI 状态以 PR #109 的实际检查为准。

配套门禁与旧界面回归入口见[测试索引](testing/README.md)。这些结果证明前端与公开服务的本地集成，不代表真实三端 CLI、外部模型或线上新建目录已完成验收。
