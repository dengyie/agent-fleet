# Fleet MCP 身份运维登记

此入口把[原持久身份](../../superpowers/plans/2026-10-25-fleet-integration-identities.md)登记到正常 Hub 已使用的数据库，供外部 MCP 客户端调用三个 Workflow 工具。返回[平台索引](README.md)；查看与撤销仍使用[账号集成页面](../../superpowers/plans/2026-10-25-fleet-identity-administration.md)，实现与证据见[登记计划](../../superpowers/plans/2026-11-12-integration-identity-setup.md)。

## 准备配置

先启用现有平台、Workflow 和 Fleet MCP gate，正常启动 Hub，完成目标 owner/workspace 和 Workflow 模板配置。相应 gate 是 `AGENT_FLEET_PLATFORM_ENABLED`、`AGENT_FLEET_PLATFORM_WORKFLOWS_ENABLED`、`AGENT_FLEET_PLATFORM_FLEET_MCP_ENABLED`。身份登记不会创建账号、工作区或模板；模板是否存在继续由原目录和创建事务检查。流程执行还需已有的 dispatch、worker 和执行目标配置。

客户端使用自己的长随机 Bearer token；只将 SHA-256 摘要写入受信 JSON 文件。以下脚本从操作者已配置的 `FLEET_INTEGRATION_TOKEN` 环境变量读取 token，使用原领域构造器校验，保存摘要及范围，不输出 token。将输出路径、owner、模板、工作区、预算改成已确认值：

```bash
python3 - /absolute/persistent/config/fleet-identities.json <<'PY'
import json
import os
import sys
from pathlib import Path
from hub.domain.integration_identity import IntegrationIdentity

identity = IntegrationIdentity.register(
    "client-a", "operator@example.com", os.environ["FLEET_INTEGRATION_TOKEN"],
    template_ids={"coding-delivery"}, workspace_ids={"workspace-a"},
    max_artifacts=20, max_budget_tokens=600000, max_budget_requests=150,
)
Path(sys.argv[1]).write_text(json.dumps({
    "schema_version": 1, "identities": [json.loads(identity.snapshot)],
}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
```

文件必须是绝对路径指向的常规 UTF-8 JSON 文件，最大 256 KiB，1–128 条身份。每条身份为原完整快照字段：`integration_id`、`owner_id`、`token_digest`、`template_ids`、`workspace_ids`、`scopes`、`max_artifacts`、`max_budget_tokens`、`max_budget_requests`。列表使用排序且无重复的字符串数组；scope 只能来自 `workflow.create_from_template`、`workflow.get`、`workflow.list_artifacts`。owner/integration 复合键与 token 摘要在文件中各自唯一；不会接收 raw token、未知字段或撤销状态覆盖。

身份预算按整个模板所有步骤的合同上限相加检查。当前三步 coding-delivery 每步为 200000 token/50 次请求，因此示例为 600000/150；使用默认单步的 200000/50 会在创建前得到 `budget_forbidden`，不生成 Workflow。这个上限是授权预算，不表示创建时就会消费相同用量；各执行步骤仍受原预算账本限制。

## 预览与应用

默认仅检查整个文件并选定一个身份，不打开或创建数据库，不发网络请求：

```bash
python3 -m hub.integration_setup \
  --registration /absolute/persistent/config/fleet-identities.json \
  --owner operator@example.com --integration client-a
```

确认范围后，显式写入原 Hub 已初始化 Workflow 的数据库：

```bash
python3 -m hub.integration_setup \
  --registration /absolute/persistent/config/fleet-identities.json \
  --owner operator@example.com --integration client-a \
  --database /absolute/persistent/state/platform.db --apply
```

应用前所有允许的工作区必须属于相同 owner 且已启用；缺库或未初始化 Workflow 时返回错误，不创建替代库。命令只登记选定身份；相同快照重复应用保持原记录与时间。输出只含模式、owner、integration 和状态，错误退出 2 并输出稳定错误码；token、摘要及私有快照不会回显。

正常 Hub 无需再次注入身份或为了这次登记而重启：原 MCP 端点每次请求读取相同持久库。客户端 endpoint 为 Hub 的 `/api/platform/mcp`；使用原 Bearer 和协议头。只登记身份并不表示已经执行了 Workflow，仍须用实际配置验收创建、查询与产物列表。

修改已登记 token、范围或预算会返回 `integration_registration_conflict`；轮换时使用新 integration_id 与新 token，再通过账号页面撤销旧身份。撤销后重复应用返回 `state=revoked`，不会重新激活；导入恢复禁用的身份返回 `state=restore_disabled`，或被原数据库 frozen 屏障拒绝，不能通过登记命令绕过恢复流程。旧 token 的 Workflow 归属和命名空间保持原语义。

本入口的验收使用受控本地 HTTPS 对端和原数据库，不代表真实外部账号/策略、生产服务或完整 Linux/browser 发布门禁已完成。
