# 官方插件接入与 GitHub CLI 管理

本实现提供随 Fleet 发布的官方插件注册表、插件中心 `/plugins`、按节点启停、节点版本/依赖/运行环境上报，以及持久操作查询。首个插件为 `github-cli` `1.0.0`，插件协议为 v1。它尚不提供第三方包在线安装、独立热更新或市场；节点上的旧 Probe/Runner/ControlClient 不会因此自动成为插件执行器。

## 使用流程

1. Hub 已启用 `platform_enabled`，并配置平台命令签名私钥。现有平台节点认证和主动出站 HTTP 链路继续使用。
2. 管理员打开「插件中心」→「接入新节点」，填唯一节点 ID 和名称，生成并下载接入文件。ID 应与现有 Muse/执行节点不同；不同进程不能共用同一节点队列。已有节点 ID 会拒绝创建，不会轮换其凭据。
3. 将接入文件放到目标 VM。目标机器已有项目源码、Python 3.10+、项目依赖和 GitHub CLI；本流程不自动下载或安装程序。
4. **以需要使用 GitHub 的开发用户运行**，从项目根目录导入配置：

   ```bash
   python -m tools.plugins.node --enroll /absolute/path/enrollment.json \
     --state-directory /absolute/path/fleet-plugin-state
   ```

   Windows 同样使用 `python -m tools.plugins.node`，路径使用绝对路径，例如 `C:/Users/Developer/AppData/Local/agent-fleet/plugins`。导入将生成权限受限的 `node.json`、`node-credential` 和 `command-public-key`。若这些文件已存在，拒绝覆盖。接入文件只供本次初始化，不放入源码仓库。

5. 查看配置与依赖识别结果，再启动常驻节点：

   ```bash
   python -m tools.plugins.node --config /absolute/path/fleet-plugin-state/node.json --manifest
   python -m tools.plugins.node --config /absolute/path/fleet-plugin-state/node.json
   ```

   `--manifest` 不连接 Hub；`--once` 上报并执行一次命令后退出，适合状态检查，**不用于设备码登录**。常驻节点每 30 秒上报实际插件版本和运行环境，默认每 2 秒拉取一条命令。操作最长可用 90 秒等待节点领取，普通 CLI 调用最长 20 秒；设备授权任务单独运行，最长 15 分钟。

6. 回到插件中心启用 GitHub CLI 插件，再为已接入的节点启用。选择节点、运行环境和 GitHub 主机，点击「检查状态」。节点报告超过 120 秒未刷新会显示离线；未安装 `gh`、版本不兼容、未接入和插件未启用分别显示。
7. 「登录 GitHub」显示一次性设备码与授权链接。用户在自己的浏览器完成授权；页面每轮查询已持久保存的进度。刷新恢复所选节点和同一登录任务。完成后自动检查当前账号。可以切换已有账号、退出指定账号，或输入 `owner/repository` 验证读取与推送等权限。

插件与节点的“停用”阻止新业务操作；既有操作的查询和已有设备登录的取消保持可用。停用不是进程终止。SIGTERM/Ctrl-C 会关闭节点启动的登录 worker，worker 负责停止其自有 `gh` 进程组。节点异常退出时，worker 检测父管道 EOF 后停止登录。重启后遗留未完成记录显示 `unknown`，需要重新检查实际账号状态，不重放旧登录。

最近操作按所选节点、环境和主机查询；当前登录恢复单独查询原始登录及后续进度，不受最近 50 条历史上限影响。环境锁由登录 worker 持有至 `gh` 完全退出，节点重启后的新登录、切换和退出会等待旧进程清理，遇到 `context_busy` 时稍后重试。

## 系统用户与配置环境

插件不做 sudo，也不切换 OS 用户。页面显示节点进程实际用户与配置目录。想管理其他系统用户，应由该用户运行另一个独立节点 ID 和状态目录。

默认配置目录遵循 `GH_CONFIG_DIR` → `XDG_CONFIG_HOME/gh` → Windows `APPDATA/GitHub CLI` → `HOME/.config/gh`。默认继承节点进程环境，因此运行服务时应确认与开发 Agent 的用户、PATH、HOME、代理及令牌环境一致。

可在节点 `node.json` 的插件配置中增加最多 16 个命名环境：

```json
{
  "plugins": {
    "github-cli": {
      "executable": "/absolute/path/to/gh",
      "contexts": [
        {"context_id": "default", "label": "日常开发"},
        {"context_id": "work", "label": "工作配置", "config_dir": "/absolute/path/work-gh-config"}
      ]
    }
  }
}
```

这段是 `node.json` 中的配置片段，其他初始化生成的字段保留。`executable` 可省略，此时从 PATH 查找 `gh`；它是单个程序路径，不是 shell 命令。每个配置目录必须不同。命名配置仍属于同一个 OS 用户，系统凭据库可能由该用户共享，不能当作独立系统账号。

`github.com`/`*.ghe.com` 的 `GH_TOKEN`、`GITHUB_TOKEN` 以及企业主机的 `GH_ENTERPRISE_TOKEN`、`GITHUB_ENTERPRISE_TOKEN` 会覆盖保存的账号。插件显示生效来源，只返回变量名。生效环境令牌存在时，登录/切换/退出保存账号会被拒绝为 `environment_override`，避免页面显示切换成功而 Agent 仍使用旧令牌。状态和仓库检查继续可用。

设备登录先读取该主机的 `gh config get git_protocol`，保留现有 HTTPS/SSH 选择，跳过 SSH 密钥上传。协议无法识别时返回 `git_protocol_unavailable`，由开发用户修正 gh 配置后重试。

## 常驻启动

- Linux：用户级 systemd service；`WorkingDirectory` 指向发布源码，`ExecStart` 使用明确 Python 路径和上述常驻命令，设置正确 PATH/代理，`Restart=on-failure`。使用 `systemctl --user`，使其与开发用户保持一致。
- macOS：用户的 LaunchAgent；`ProgramArguments` 分别填写 Python、`-m`、`tools.plugins.node`、`--config` 和配置路径，`WorkingDirectory` 指向源码，`KeepAlive` 可设 true。登录窗口中的环境变量不会自动传给 LaunchAgent，需要显式配置实际依赖。
- Windows：该开发用户的任务计划程序；程序填写 Python 路径，参数填写模块和配置参数，起始目录指向源码。节点子进程复用 Fleet 的隐藏窗口启动策略。需要凭据库解锁时，应选与实际开发相同的交互会话。

节点状态目录有进程间单实例锁。重复启动返回 75；正常结束释放锁。不同节点应有不同状态目录。客户端更新系统目前管理 probe/runner/control-client，插件节点尚需在同版本代码发布后单独重启；插件中心会把不兼容节点标出。

## 插件开发合同

| 层 | 注册位置 | 责任 |
|---|---|---|
| 共享定义 | `fleet_core/plugins.py` 的 `PluginDefinition` / `PluginRegistry` | ID、语义版本、协议版本、动作及参数校验、UI contribution ID |
| Hub | `PluginRepository` / `PluginService` | 设置、节点实际 offers、观察结果、操作派发/查询；按 owner 隔离 |
| 节点 | `tools/plugins/runtime.py` 的 `NODE_PLUGIN_FACTORIES` | 本地依赖探测、环境上下文、执行结果和关闭清理 |
| 前端 | `frontend/assets/plugins/registry.js` | 稳定 UI contribution ID 对应的 mount 函数 |

增加官方插件时，在共享注册表加入一个定义，并在节点工厂与 UI 注册表分别注册。新动作使用 `plugin.<plugin_id>.<action>`，Hub/Node 共同使用同一个参数校验器，不需要为每个动作修改核心 `NodeToolExecutor`。插件只接受结构化参数，返回标准 `{state,result,error_code,command_id}` 信封。

UI contribution 的 `mount(host,{plugin,client})` 返回销毁函数；可附加 `suspend()`、`resume()`、`updatePlugin(plugin)`。切换路由时停止轮询，恢复时重读持久状态。网络请求经过 `assets/api/client.js`，动态文本使用 DOM textContent。

Hub 在现有 `platform.db` 中增加 `plugin_settings`、`plugin_targets`、`plugin_offers`、`plugin_observations`；操作仍存在原 `platform_commands`，复用签名、outbox、租约与 NodeJournal 去重。备份包含整个 SQLite 数据库，无新增外部 artifact 引用。旧版代码忽略新增表，但不会管理新插件；回退前应停止插件节点并处理未完成操作。

模型工具导出不是本版功能。插件管理使用确定性操作接口，后续可在同一动作合同上添加助手工具贡献，单独声明哪些动作允许助手调用。

## HTTP 合同

前缀 `/api/platform/v1/plugins`，operator/admin 身份：

| 方法 / 路径 | 请求与结果 |
|---|---|
| `GET /` | manifests、enabled、revision |
| `POST /nodes` | `{node_id,label}` → 201，一次性 enrollment、节点凭据与签名公钥 |
| `PUT /<plugin_id>` | `{enabled,revision}`；旧 revision 返回 409 |
| `GET /<plugin_id>/nodes` | 节点、目标启停、readiness、实际 offer、最近 auth observations |
| `PUT /<plugin_id>/nodes/<node_id>` | `{enabled}` |
| `POST /<plugin_id>/operations` | `{node_id,action,arguments,idempotency_key}` → 202 operation |
| `GET /<plugin_id>/operations` | 最新 50 条操作；可传 `node_id`，加 `context_id` + `hostname` 可限定环境，并返回独立于历史上限的 `login` 恢复值 |
| `GET /<plugin_id>/operations/<id>` | 同一 owner 的单次操作 |

节点使用已有 `X-Platform-Node-Credential` 调用 `POST /api/platform/v1/nodes/plugins` 上报完整 inventory（`{plugins:[...]}`），以及已有 poll/receipts/heartbeat。上报空 inventory 表示该节点不再安装插件。

GitHub 动作默认 `context_id=default`、`hostname=github.com`：`status`、`login_start`、`login_status {login_id}`、`login_cancel {login_id}`、`switch {username}`、`logout {username}`、`repo_check {repository}`。恢复/取消必须引用同 owner、同节点、同配置和同主机的原始登录操作。

`operation.status=succeeded` 表示节点执行了该动作；账号是否有效看 `result.auth.state`；登录是否完成看 `result.state`。创建登录操作成功并不表示用户已完成授权。认证 JSON 模式的退出码 0 也不表示所有账号都有效。

## 验证

```bash
python -m pytest tests/test_plugins.py tests/test_plugin_node.py tests/test_github_cli_plugin.py -q
FLEET_PLAYWRIGHT_MODULE=/absolute/path/to/playwright \
  python -m pytest tests/test_plugins_browser.py -q
```

自动化使用真实 SQLite、签名命令、NodeJournal、实际子进程与真实浏览器；外部 GitHub 用临时 `gh` 可执行夹具模拟，可验设备码等待/取消/刷新/完成、账号切换、权限读取与故障分支。本机真实 `gh` 的只读状态及仓库权限检查另有实测。真实 GitHub 设备授权、远端 VM 和 Windows/macOS/Linux 各系统凭据库仍需部署后逐项验收，不能由夹具结果代填。
