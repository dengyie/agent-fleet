# Muse 工作区

在 Agent Fleet 侧栏打开 **Muse**（`/muse`），导入 `muse.ai` 登录态即可使用原生会话。Hub 直接与 Muse 通信；使用期间不需要 Chrome、CDP、9222、Playwright 或浏览器扩展。Muse 继续在其自身的云环境执行任务。一个 Fleet 用户可以保存多个 Muse 账号；账号切换会同时切换会话、审批、操作记录和独立登录态。

账号密码资料卡自动登录目前尚未实现。当前仍需从已登录 Muse 浏览器导入 Cookie。未来的凭据登录需要单独批准的认证浏览器安全契约，不能复用通用 `browser.*`，也不能在本功能之外暗含 MFA 接管；实施方案及当前运行时准入阻塞记录在[账号密码登录计划](../superpowers/plans/2026-10-15-muse-credential-login.md)。

## 使用

1. 更新包含本功能的前后端版本，并安装 `requirements.txt`（新增 `websocket-client>=1.8,<2`）。发布候选导入、切换前验证和容器安装器都会显式检查 `cryptography`、`flask`、`websocket-client` 与 `pyyaml`；缺少任一依赖时发布会失败并保持旧版本运行。沿用 Fleet 的平台开关 `platform_enabled`；普通 Fleet 用户也能访问自己的 Muse 工作区。
2. 从任意已登录 Muse 的浏览器导出 Cookie JSON，或使用已有的 Playwright storage state 文件。点击 **管理登录态**，选择 JSON 文件或粘贴内容，再点击 **导入并连接**。首次导入建立默认账号；点击 **添加账号** 可保存另一份登录态，并可填写账号名称。也支持原始 Cookie 字符串。导入只采纳 `muse.ai` 域 Cookie，必须含未过期的 `hatch_sess`；无需安装 Playwright 来使用导出的文件。
3. 点击已有会话打开窗口；**新建子会话**在第一次发送时创建独立的 Muse 原生 side chat。单窗口与并排窗口可切换，最多打开 6 个窗口，每个窗口有独立草稿和引用对象。窗口布局和草稿仅保留在当前页面内存，切换 Fleet 栏目可保留，刷新页面后不保留。
4. 在消息下点击 **引用回复**，或点击 **停止**请求 Muse 停止当前会话。历史支持加载更早消息；可见窗口约每 4 秒在上一轮请求结束后刷新，并非逐 Token 推送。
5. 在任务卡片点击 **查看浏览器截图**，或点击文件名下载。当前只读取 Muse `workspace/` 下 8 MB 以内的文件。PNG/JPEG/GIF/WebP 可显示；其他文件以下载方式返回，未知卡片保留可展开的数据详情。

Cookie 是登录凭据，不要发到聊天、提交到仓库或附在截图中。导入成功后输入框会清空，接口不返回 Cookie。服务器固定访问 Muse 鉴权域和官方网关，禁止跟随鉴权或网关重定向。

## 权限审批

每个已有会话的 **自动审批** 开关默认关闭，最多同时开启 20 个会话。打开后，Hub 后台每约 5 秒检查 Muse 权限请求，即使 Fleet 页面关闭仍生效。只有能通过 Muse 的 session/thread scope 明确归属所选会话的请求才会得到 `allow_once`；无会话归属的后台请求仍需手动处理。

**完全访问** 默认关闭，按 Muse 账号单独保存。主动打开后，该账号的所有会话和无会话归属的后台请求都会自动得到 `allow_once`，包括其他 Fleet 页面关闭时到达的请求；此时单会话开关会禁用，因为它已不再限制范围。关闭完全访问会立即阻止后续自动批准。正在进行的远端请求在设置提交前已经原子领取的情况下可能完成；设置版本校验确保关闭之后不能再领取新请求。每次决定均留有操作记录。重新导入登录态会重置完全访问和会话白名单，其他 Muse 账号授权不受影响。

**权限请求与操作记录**展示待处理卡片，可选择 **允许这一次** 或 **拒绝**。自动和手动决定均记录结果，并携带 Muse 的消息版本；同一请求版本只派发一次。禁用 Fleet 账号后后台停止审批；读取请求后会再次检查账号、登录版本和开关，防止切换登录态时使用旧列表。

真实账号验收没有开启自动审批，也没有批准任何真实权限请求；会话级与完全访问审批、显式授权、按账号隔离、重新导入重置、关闭后的并发撤销、版本检查与后台请求范围通过隔离夹具验证。

## 投递结果与断开

发送前保存操作标识；Hub 重启或客户端重试仍不会自动重复派发同一个标识。`confirmed` 表示 Muse 已确认接收，任务是否完成由会话内容判断。`retryable` 仅用于确定尚未向 Muse 派发的前置失败，可以用同一标识重试；并发重试由持久回执领取者串行化。`dispatching` 表示另一请求仍在操作，页面只核对历史并等待；超时或进程中断后的过期派发标记转为 `unknown`，不会自动重放。连接中断时保留 `unknown`，可点击 **核对投递结果**刷新该操作回执，并检查会话历史；回执未知不会被猜测为成功或失败。

确认历史后，可点击 **已核对，继续输入**开始下一条消息。此操作清空输入框、保留原未知回执，不撤回或重发原消息。原子回执避免的是相同操作标识的重复派发；人工发送新的消息仍是新的操作。

**断开连接**删除当前 Muse 账号保存的登录态和自动审批设置，清理该账号的内存窗口、草稿和对话。它不删除 Muse 远端会话、不注销官网登录，也不撤销已发送的任务。重新导入同一账号会递增连接版本并清空该账号的旧设置，需重新开启自动审批；其他 Muse 账号不受影响。

## 实现与存储

- `hub/integrations/muse/`：固定 HTTPS 鉴权、Noise XX（X25519/AESGCM/SHA256）加密 WebSocket、受限 protobuf 分片与服务请求。只使用自有实现和 `cryptography`、`websocket-client`，不加载或执行 Muse 前端代码。
- `hub/application/muse_service.py`：会话、历史、发送/停止、文件读取、审批与后台检查；`hub/http/muse_routes.py`：经 Fleet 身份验证和同源写入保护的 `/api/platform/v1/muse/*`。
- `hub/infrastructure/muse_repository.py`：`state_dir/muse/` 目录权限 0700，`muse.db` 与 `credentials.key` 权限 0600。Cookie 用 Fernet 加密，密钥原子发布；登录、设置与回执按 Fleet 用户和 Muse 账号双重隔离。SQLite 事务保护多 Hub worker 之间的账号租约、连接替换和回执领取。终态回执保留 30 天且每账号最多 5000 条，超出部分转为最多 10000 条幂等墓碑；`dispatching` 与 `retryable` 保留在详细回执中，`unknown` 转为墓碑后仍保留原幂等结果，避免不确定投递被重放。旧单账号表迁移为 `default` 账号。备份恢复需要同时保留数据库与密钥，磁盘加密不代替服务器账户隔离。
- `frontend/assets/views/muse*`：复用现有 StreamMarkdown、ChatPromptInput、AutoScrollAnchor 和 ToolCallBadge。消息及资源来自 Muse，不复制保存为 Fleet Assistant 会话；回执包含操作结果与时间，操作内容以哈希参与幂等校验。

这一接入依据 2026-10-14 实际使用的 Muse Web 协议，不是官方稳定 API。Muse 改动协议、登录验证或会话结构时可能需要适配。当前验证的是普通 `hatch_vm`；需要恢复密钥或机密计算认证的环境会明确拒绝。暂不支持附件上传、任意嵌套子会话、远端会话删除，以及执行 Muse 专有交互 widget；未知卡片显示数据，HTML 不执行。原有 CDP 桥接可选保留，独立于此栏目。

## 验证

```sh
# FLEET_PLAYWRIGHT_MODULE 指向测试用 Playwright；运行 Muse 功能本身不需要它。
FLEET_PLAYWRIGHT_MODULE=/absolute/path/node_modules/playwright \
FLEET_BROWSER_CHANNEL=chrome PYTHONPATH=. python -m pytest \
  tests/test_muse_native.py tests/test_muse_workspace.py -q
```

测试包含独立 Noise responder、加密破损/分片边界、固定域及重定向拒绝、多用户 HTTP 隔离、并发密钥创建、持久化去重、账号停用/换登录态后停止自动审批、文件路径/大小校验，以及实际 Chromium 中的多窗口草稿、卡片 XSS、截图/下载、审批、页面切换和 390px 布局。

2026-10-14，用户明确授权一次只读真实账号验收。通过导入的登录态，由独立 Python 客户端创建原生 side chat 并发送固定测试请求；Muse 确认接收，随后新进程从历史中确认预期完成回复。native `MUSE-LIVE` 往返通过；未复用浏览器连接、未发送第二条测试消息，也不在仓库记录真实对话正文。真实会话列表、消息历史、浏览器任务卡片和 PNG 截图只读查询也已验证。

私有 Cookie、真实历史和原始回执仅放在仓库外的私有临时目录，不属于测试工件。公开截图只使用本地夹具数据。本地测试与这一条真实往返不代表 Linux 发布门禁、线上部署或所有 Muse 工具均已验收。

最终完整本地回归（2026-10-14）：**3368 passed、5 skipped、165 subtests passed**，JUnit 零 failures/errors。36 条 journey 中 35 条通过，MUSE-WORKSPACE 与 WEB-AGENT-01 均通过；唯一未完成的是 RELEASE-01 的 Linux 专用检查，因此 required gate 退出 1，不能视为发布验收通过。证据：`/tmp/fleet-muse-final2-20261014/{journeys.json,results.xml,browser/}`。工作树未提交、未部署，报告记录 dirty checkout；真实 MUSE-LIVE 的单次往返另见上文。
