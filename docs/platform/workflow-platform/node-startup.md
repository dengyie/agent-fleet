# 常驻 Linux 测试 Node 配置

返回 [Workflow 入口](README.md)、[Hub 登记](startup-registration.md)、[实施计划](../../superpowers/plans/2026-11-11-node-workflow-registration.md)及[开发完成账](../development-completion.md)。这是候选代码的配置入口，当前线上是否已发布须单独核对。

现有 `python3 -m tools.plugins.node` 支持可选 `workflow_test`，继续使用原 NodeRuntime、签名接收、能力上报、固定测试、journal 和进程锁。已有插件配置保持兼容；纯测试 Node 使用空 `plugins` 对象。此入口提供固定 `coding.test@1`，不接收自定义 shell/action。Muse 入口仍独立运行。

同一 owner 下多个工作区可按[Hub 共享节点登记](startup-registration.md#远程-linux-固定测试)共用此 Node；本地仍只启动一个常驻进程，共用同一凭据、接收目录和journal。Hub 保留各工作区的独立接收授权，物理节点的执行槽位不会随工作区数量增加。

## 准备与登记

运行账号应拥有持久 state 目录和一个预先创建的接收目录，接收目录供原 `source` alias 使用、只收 `.tar`。Hub 已有相同 owner 下启用的 Node、node-scoped credential 和对应 Command public key。凭据文件内容为原 `node-id:secret`，public key 文件为原 32-byte Ed25519 公钥的 Base64 文本。沿用原凭据准备流程，不将 secret 粘贴到运行日志或 Hub manifest。

`node.json` 结构如下。`workflow_test.capability` 要复制 [Hub 登记](startup-registration.md) 中 `test.remote.capability` 的完整 JSON 对象，包含两个 schema hash，不能只复制 ID。下面示例的完整 capability 与 Hub 示例一致；实际运行须核对精确匹配。

```json
{
  "node_id": "linux-test-a",
  "hub_url": "https://fleet.example.com",
  "credential_file": "/var/lib/agent-fleet-node/node-credential",
  "public_key_file": "/var/lib/agent-fleet-node/command-public-key",
  "state_directory": "/var/lib/agent-fleet-node/state",
  "plugins": {},
  "poll_interval_s": 2,
  "workflow_test": {
    "enabled": true,
    "offer_revision": 1,
    "capability": {
      "capability_id": "coding.test",
      "revision": 1,
      "adapter_id": "fixed-tests",
      "input_schema_ref": "in-v1",
      "output_schema_ref": "out-v1",
      "input_schema": {
        "type": "object"
      },
      "output_schema": {
        "type": "object"
      },
      "input_schema_hash": "a2c799262a3ce3c19ef5cdd983bf3d12b43ab3c426227091b909dcb7054738c0",
      "output_schema_hash": "a2c799262a3ce3c19ef5cdd983bf3d12b43ab3c426227091b909dcb7054738c0",
      "risk": "workspace_read",
      "retry_class": "manual_only",
      "allowed_owner_roles": [
        "operator"
      ],
      "max_active_seconds": 3600,
      "result_limit_bytes": 65536
    },
    "input_directory": "/var/lib/agent-fleet-node/inputs",
    "sandbox_launcher": ["/opt/agent-fleet/.venv/bin/python", "-I", "/opt/agent-fleet/fleet_core/workflow/linux_launcher.py", "--fixed-test", "--bwrap", "/usr/bin/bwrap", "--"]
  }
}
```

下面的 Python 命令从已审阅的 Hub manifest 构造完整配置；在 Node 上准备仅含该 manifest 的文件，再按实际目录修改参数即可。manifest 不含密钥。多项 manifest 要精确选择对应 owner/workspace，不能默认选首项。

```python
import json
from pathlib import Path

manifest = json.loads(Path("/var/lib/agent-fleet-node/hub-registration.json").read_text(encoding="utf-8"))
matches = [row for row in manifest["coding_delivery"]
           if row["owner_id"] == "operator-a" and row["workspace_id"] == "workspace-a"]
assert len(matches) == 1
remote = matches[0]["test"]["remote"]
node = {
    "node_id": remote["node_id"], "hub_url": "https://fleet.example.com",
    "credential_file": "/var/lib/agent-fleet-node/node-credential",
    "public_key_file": "/var/lib/agent-fleet-node/command-public-key",
    "state_directory": "/var/lib/agent-fleet-node/state", "plugins": {},
    "workflow_test": {"enabled": True, "offer_revision": 1, "capability": remote["capability"],
        "input_directory": "/var/lib/agent-fleet-node/inputs",
        "sandbox_launcher": ["/opt/agent-fleet/.venv/bin/python", "-I",
            "/opt/agent-fleet/fleet_core/workflow/linux_launcher.py", "--fixed-test",
            "--bwrap", "/usr/bin/bwrap", "--"]},
}
with Path("/var/lib/agent-fleet-node/node.json").open("x", encoding="utf-8") as stream:
    stream.write(json.dumps(node, ensure_ascii=False, indent=2) + "\n")
```

launcher 首项必须为绝对路径，参数最多 64 项；实际 launcher 必须为 Linux 测试隔离提供 Python、pytest、只读源码和可写 JUnit 位置。程序不会下载或安装 launcher。原生 Linux 是否提供此能力由原执行器检查，其他系统不宣称可用。`--manifest` 只检查配置与组件可用性，不证明 launcher 隔离或固定测试已成功运行。

示例使用[仓库内置 Linux 启动器](startup-registration.md#仓库内置-linux-固定测试启动器)，无需复制测试夹具。将 Python、源码和 bubblewrap 改为该 Node 的实际绝对路径，Python 须与 Node 执行器使用的解释器一致，并已具备 pytest。保留末尾 `--`；启动器从每次真实测试 cwd 取得源码和 JUnit 路径。libseccomp、namespace 和其余运行前提与 Hub 本地测试相同；没有配置或依赖不足时仍拒绝执行。

## 运行与重启

```bash
python3 -m tools.plugins.node --config /var/lib/agent-fleet-node/node.json --manifest
python3 -m tools.plugins.node --config /var/lib/agent-fleet-node/node.json --once
python3 -m tools.plugins.node --config /var/lib/agent-fleet-node/node.json
```

`--manifest` 不访问 Hub，但会初始化本地 journal 并取得同一进程锁。`--once` 沿原 HTTP heartbeat→能力上报→poll/receipt 路径处理一条可立即开始的命令，输出计数；能力上报失败时计数附带 `capability_report_error`，不能把 poll 成功当作能力已被接受。纯 Workflow Node 不调用 plugins 注册路由。有插件时保留原插件公告流程。

部署的进程管理器应使用同一运行账号、绝对配置路径和对应源码的 Python，保留 state/inputs 和解释器依赖。不要对重启清空 journal 或使用另一 state 目录。重复启动同一 state 会因进程锁退出 75；SIGTERM/SIGINT 同时通知外层循环和原运行时的在途取消，资源在 poll 收尾后关闭并释放锁。停止确认仍沿原 CommandControl/unknown 合同，进程管理器强杀或外部结果不明不能被标记为成功取消。

Hub 使用当前解释器的实际 Python 版本和本机 architecture 精确匹配能力。该入口登记一个 slot，不虚报可用内存；资源需求仍由原选点策略核对。重启复用原接收与命令 journal，已完成固定测试返回保存结果，不重新执行；过期签名仍先按原协议拒绝。

## 验证边界

`tests/test_node_workflow_startup.py` 覆盖正常配置、CLI manifest/once、真实 Hub HTTP、单实例锁、签名接收重启、信号传递及原生 Linux 固定测试保存结果复用。最后一项在 macOS 跳过，必须另在 Linux 执行；其 fixture launcher 仅证明配置接线和执行，不证明生产隔离。真实 sandbox 和跨机整链另见 [真实跨节点试点](../../superpowers/plans/2026-11-11-real-cross-node-workflow.md)。
