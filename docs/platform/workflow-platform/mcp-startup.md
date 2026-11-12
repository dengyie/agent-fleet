# MCP 正常启动登记与显式发现

本入口补齐 [F5 集成](../../superpowers/plans/2026-10-24-f5-integrations.md)的部署装配，复用原持久目录、Run 授权固定及[目录管理页面](../../superpowers/plans/2026-10-25-mcp-catalog-administration.md)。返回[平台索引](README.md)；实现与证据见[启动登记计划](../../superpowers/plans/2026-11-12-mcp-startup-registration.md)。

## 从账号页面登记连接

页面登记与显式发现属于后续独立增量，发布状态及验证范围见[实施计划](../../superpowers/plans/2026-11-13-mcp-connection-registration.md)和[开发总账](../development-completion.md)。需要同时开启平台、MCP 及页面登记开关：

```ini
AGENT_FLEET_PLATFORM_ENABLED=1
AGENT_FLEET_PLATFORM_MCP_CONNECTORS_ENABLED=1
AGENT_FLEET_PLATFORM_MCP_CATALOG_REGISTRATION_ENABLED=1
```

管理员在账号设置的 MCP 集成区选择“登记连接”，填写已有 owner/workspace、唯一连接标识、完整 HTTPS 服务地址、环境凭据引用、选定的工具名称及请求/结果/发现数量上限。默认归属为当前账号 ID；所选工作区必须实际属于填写的身份且处于启用状态。每行填写一个工具名；页面使用与下方配置文件相同的字段、限制和只读策略。访问令牌在 Hub 进程环境中配置，页面仅填写 `env://MCP_API_KEY` 这样的引用。

登记写入原持久目录并附着原适配器，不访问外部服务或读取凭据。成功后先是 `registered`，尚无可调用的工具版本；关闭登记窗口，再选择“发现工具”。首次成功发现按原 CLI 合同成为 active；以后服务信息或工具定义发生变化会进入 pending，需在原页面审阅和批准。旧 Run 的固定版本不随新发现或批准改变。该入口不启动模型任务。

连接定义不可原地修改。相同定义重复登记返回原记录，保留原时间和 active/pending/禁用状态；地址、凭据引用、归属、工具或限额变化须使用新的连接标识。丢失登记响应时，“核对并重试原登记”只使用已锁定的原配置；冲突时关闭窗口后重新读取原记录。发现响应不确定时先重新读取目录，页面不会自动重发发现请求。离开页面、关闭窗口或切换账号只停止本次浏览器观察，不能据此判断服务端请求已取消。

页面登记开关开启时，普通 Hub 启动离线附着数据库中未禁用的登记，不要求另写配置文件；已运行的另一 Hub 会在原 scope 工具查询/调用或显式发现时附着新登记。原目录总数仍限制为128。已通过受信文件附着的 transport/private aliases 保留。关闭页面登记开关后回到原文件装配方式；连接库继续保留，未通过文件附着的连接不获得运行适配器。撤销或备份恢复导入的禁用记录不会因重复登记、重新加载或重启而启用。

## 配置文件

在 Hub 宿主的持久配置目录保存 UTF-8 JSON 文件。文件必须使用绝对路径，最多 256 KiB、128 个唯一 connector_id；不能将真实 token 放入文件。示例的地址、归属、工作区及工具名须换成已核实值：

```json
{
  "schema_version": 1,
  "connectors": [{
    "connector_id": "weather",
    "endpoint": "https://mcp.example.com/mcp",
    "secret_ref": "env://WEATHER_MCP_TOKEN",
    "owner_id": "owner",
    "workspace_id": "workspace",
    "selected_tools": ["weather"],
    "risk": "read_only",
    "retry_class": "read_only",
    "timeout_s": 10,
    "result_limit_bytes": 65536,
    "max_tools": 100
  }],
  "private_aliases": {}
}
```

`max_tools` 可省略，默认 100；其余 connector 字段必填。`selected_tools` 为 1–20 个唯一工具名，`timeout_s` 大于 0 且不超过 30 秒，结果上限 256 KiB。SecretRef 仅接受 `env://` 加 1–128 位大写字母、数字或下划线。token 必须在运行 Hub 和执行发现命令的两个进程环境中分别可用；启动过程不读取 token，缺少 token 在实际请求时返回 `secret_unavailable`。

`private_aliases` 可省略。需要访问私有 HTTPS 服务时，使用完整 endpoint 到固定私有 IP 的映射，例如 `{"https://mcp.internal.example:8443/mcp":"10.0.0.12"}`，且 endpoint 必须出现在同一文件的 connectors 中。原 TLS 主机名/证书和地址检查继续生效；该映射不代替 TLS 信任配置。一个文件中的所有连接器必须完整有效，否则启动拒绝装配。

## 启动与发现

在现有 `fleet-gates.conf` 或进程环境中加入：

```text
AGENT_FLEET_PLATFORM_MCP_CONNECTORS_ENABLED=1
AGENT_FLEET_PLATFORM_MCP_REGISTRATION_FILE=/absolute/persistent/config/mcp.json
```

平台总开关也须启用。文件中的 owner/workspace 必须已在该 Hub 登记且 workspace 已启用。路径优先级沿用原启动规则：显式 `make_app(platform_mcp_registration_file=...)` 参数优先，其次进程环境，最后 fleet-gates.conf；显式或环境空字符串关闭文件加载。MCP gate 关闭时不读取该文件。文件配置不能与自定义 Python 注入的目录/非空连接器集合同时使用。

先预览，默认不会访问数据库、解析 token 或请求外部服务：

```bash
python3 -m hub.mcp_setup --registration /absolute/persistent/config/mcp.json
```

确认地址、选定工具与工作区后，显式发现一个连接器；数据库必须是已初始化 Hub 的现有数据库绝对路径：

```bash
python3 -m hub.mcp_setup \
  --registration /absolute/persistent/config/mcp.json \
  --database /absolute/persistent/state/platform.db \
  --connector weather --discover
```

此命令只对选定连接器执行 `server/discover`、`tools/list`，不调用工具。沿用既有目录合同：**首次成功发现直接成为 active revision**；后续定义或服务信息变化保存为 pending revision，原 active 保持。pending 输出表示发现成功且待审批，命令退出 0；错误退出 2，仅输出稳定错误码。命令不提供审批或撤销的替代接口。

重启 Hub 后，正常启动从文件重新挂载适配器，从数据库读取已有 active/pending 状态，不自动重新发现。管理员可在账号集成页面审阅 pending 完整定义并批准，或撤销连接器。Run 继续固定提交时的原授权摘要；审批不会给旧 Run 添加新定义。撤销在各进程生效，重新加载配置或再次发现均不会恢复撤销。配置身份、地址、SecretRef、选定工具或限额与已登记快照不一致时返回 `connector_registration_conflict`；应使用新的 connector_id，旧记录由管理员撤销。

本入口已有受控独立 HTTPS 对端、真实环境 SecretBroker、持久 SQLite、重启工具调用及变更待审/撤销回归；真实外部服务、实际账号策略和生产配置验收仍需单独完成。它不构成完整 Linux/browser 发布通过。
