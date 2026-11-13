# Muse 本地浏览器桥接

这是可选的早期 CDP 节点接入。需要独立登录态、多窗口、原生子会话和自动审批时，请使用新版 [Muse 工作区](muse-workspace.md)，无需配置本文的 Chrome 或节点进程。下文的限制与旧实测记录只适用于 CDP 桥接。

这一接入把你已登录的 Muse 账号作为外部 Agent 使用。Fleet 在选定节点上发送你委托的任务、读取该任务的新回复；Muse 继续使用自己的云电脑和审批流程。它不是模型 API Provider，也不会接管 Muse 的云电脑。

## 本地使用

需要 macOS/Linux、Python 3.10+、Node.js 22+（全局 WebSocket），以及本机 `127.0.0.1:9222` 上已启动、已登录 Muse 的 Chrome。桥接不启动或重启你的浏览器、不导入 Cookie。只识别唯一的 `https://muse.ai/` 主聊天标签；多个符合条件的标签会拒绝自动选择。源码需包含 `tools/browser_bridge/cdp_muse.cjs`。

在仓库根目录执行：

```sh
python -m tools.browser_bridge.muse_cli status
printf '%s' '只回复你好，不调用工具。' | python -m tools.browser_bridge.muse_cli submit hello-001
python -m tools.browser_bridge.muse_cli read hello-001
```

`submit` **会向 Muse 发送消息**。任务 ID 是持久化幂等键，相同 ID、工作区和任务内容不会重复发送；更换内容必须使用新 ID。`read` 只核查同一任务，不会发送消息。`status` 只显示连接、输入框和忙碌状态，不返回已有聊天历史。

默认状态文件为 `~/.local/share/agent-fleet/muse-bridge/tasks.db`。可在子命令前指定 `--state /absolute/path/tasks.db --scope workspace-id`。同一个 Muse 账号只能使用一份状态文件和一个桥接节点；不要用第二份状态绕过忙碌锁。状态中保留任务哈希、消息锚点和有界回复，不存浏览器凭据或提交的原始任务文本。回复可能包含你的任务数据，状态目录应保持私有。

CLI 与 Fleet 节点联用时，让 CLI 的 `--state` 指向节点的 `state_directory/muse.db`，并用同一 workspace 作为 `--scope`。保持账号和标签不变；浏览器重启、主标签关闭或切换账号后，第一版不会自动重新绑定未完成任务。

## 接入 Fleet

Hub 与本地节点均需使用包含此功能的版本。复用现有平台节点注册、节点凭据、Ed25519 命令签名和 remote execution；Hub 仍不连接本机 9222。节点只主动发起 HTTPS 轮询及回执上报。

当前仓库的节点目录通过服务端 `PlatformRepository.upsert_node(owner_id, data)` 配置，尚无创建节点的 Web 表单或 POST 路由。由 Hub 管理员在现有平台初始化入口为**当前用户**注册专用节点（此方法会更新同名记录，先确认节点 ID 未占用），数据为：

```json
{
  "node_id": "mac-muse",
  "label": "本机 Muse",
  "capabilities": {
    "web_agent.status": true,
    "web_agent.submit": true,
    "web_agent.read": true
  },
  "enabled": true
}
```

管理员可在已加载的 Hub app 中使用 `app.extensions["fleet"]["platform_repository"].upsert_node(owner_id, data)`；`owner_id` 必须是登录用户的真实身份，`data` 是上面的对象。这一步不自动部署或修改线上目录。

通过现有 `POST /api/platform/v1/nodes/mac-muse/credential` 取得节点凭据，存到本机私有文件；将 Hub 的 32 字节原始 Ed25519 **公钥**以 base64 存入另一文件。不要把私钥交给节点。示例本机配置（路径按实际情况填写）：

```json
{
  "node_id": "mac-muse",
  "hub_url": "https://fleet.example.com",
  "credential_file": "/absolute/private/muse-node-credential",
  "public_key_file": "/absolute/private/hub-public-key.base64",
  "state_directory": "/absolute/private/muse-node",
  "workspace_ids": ["my-workspace"],
  "allow_muse_tasks": true,
  "poll_interval_s": 2
}
```

```sh
python -m tools.browser_bridge.node --config /absolute/private/muse-node.json --manifest
python -m tools.browser_bridge.node --config /absolute/private/muse-node.json --once
python -m tools.browser_bridge.node --config /absolute/private/muse-node.json
```

这是前台进程，不安装后台服务。`--manifest` 不联系 Hub、不发送 Muse 消息；`--once` 会执行一条已授权排队命令。停止进程不代表取消 Muse 已接收的任务。

节点自动以 `0700` 创建新的状态目录；已有目录若允许组或其他用户访问，会拒绝启动。配置和凭据文件也应仅允许当前用户读取。节点命令回执会保留任务回复。

在 Fleet Assistant 中选择该节点和允许的 workspace，主助手即可看到三个工具：

- `web_agent.status {}`：查看连接状态。
- `web_agent.submit {"task_id":"research-001","prompt":"具体任务"}`：持久化派发一次。
- `web_agent.read {"task_id":"research-001"}`：查看结果，允许后续 Run 在同一 workspace 继续核查。

工具仅在选中的、当前用户可见且启用的节点声明能力后出现。本地节点还需要 `allow_muse_tasks: true` 与 workspace 白名单；普通 Hub 本地工具不能使用它。未配置签名的命令不能驱动 Muse。通用 `browser.*` 的联网与 submit 开关不变。

## 状态与恢复

| 状态 | 含义 |
|---|---|
| `sent` | 页面发送动作已发起，尚未确认任务结果 |
| `running` | 已找到完全匹配的用户消息，尚无终态标记 |
| `waiting_input` | Muse 明确回复需要用户输入；打开 Muse 处理 |
| `completed` / `failed` | Muse 在非流式回复末尾返回了该任务的对应标记，属于 **Agent 自报**，不等于独立验证产物正确 |
| `unknown` | 发送中断、页面变化、消息锚点丢失或人工插入了其他消息；禁止自动重发 |

桥接在任务末尾附加随机关联标记的回复约定，只有匹配的完整用户消息之后、没有其他用户消息插入、且回复不在流式输出时，才接受终态。进度回复和其他会话的旧结果不会被当成完成。发送前有草稿时拒绝发送；输入后页面或草稿变化也会停止。

已有流式回复时也拒绝发送。若点击 Muse 自己的审批控件后没有新增用户聊天消息，后续 `read` 可以识别继续执行后的完成回复；如果需要在聊天框人工补充内容，第一版会将其视为人工插入并保留 `unknown`，需要人工核对。

`unknown` 后保持同一状态文件、恢复原 Muse 主聊天标签，再对原任务 ID 执行 `read`。若仍无法核对，需人工查看 Muse；不要删除状态文件、换任务 ID 重发或假定远端已经取消。`waiting_input` 或 `unknown` 都保留单任务锁。第一版不提供自动审批、取消远端任务、自动释放未知任务或附件上传。

## 验证

```sh
PYTHONPATH=. python -m pytest tests/test_muse_browser_bridge.py tests/test_muse_fleet_integration.py -q
node --check tools/browser_bridge/cdp_muse.cjs
```

真实 Chromium 夹具使用 `FLEET_PLAYWRIGHT_MODULE` 指向已安装 Playwright；在本地伪造的 Muse DOM 上测试，不向真实 Muse 发送消息。真实账号的端到端验证需单独明确授权一次测试消息。DOM 选择器、登录失效和 Muse 是否遵守回复标记都是运行时依赖；本地测试通过不代表所有真实任务都可自动判断完成。

2026-10-13：已验证本机登录态和只读状态查询，实际发送按钮及关联标记往返尚未通过真实账号验收，`MUSE-LIVE=not_run`。实现与测试记录见 [实施记录](../superpowers/plans/2026-10-13-muse-browser-bridge.md)。
