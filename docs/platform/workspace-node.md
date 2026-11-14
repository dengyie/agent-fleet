# VM 工作区执行节点

返回 [项目入口](../../README.md)、[官方插件 Node](official-plugins.md)及[测试与发布门禁](../testing/README.md)。实施与验收步骤见[计划](../superpowers/plans/2026-11-14-vm-direct-execution.md)。

普通助手可在独立 VM 账号中直接执行工作区命令。Hub 负责模型、会话、设置、调度和运行状态，Node 负责文件、子进程和回执。沿用签名命令、主动出站轮询、租约、取消及持久 journal，不需要再配置沙盒启动器。

## 设置与首次接入

1. Hub 启用平台、远程节点执行，并加载平台命令签名密钥。已有密钥必须保留；首次配置见[插件接入](official-plugins.md)。配置就绪检查不代表模型或 VM 网络已经实测。
2. 管理员在 `/settings/nodes` 保存一个独立 Node ID。工作区 Node 声明 `workspace.list`、`workspace.read`、`workspace.write`、`workspace.exec`。不能让多个进程共享同一 Node ID 的命令队列。
3. 在 `/settings/workspaces` 保存 VM 上的绝对目录，并将默认节点设为此 Node。目录不需要存在于 Hub；实际执行账号必须能创建或读写它。未显式设置工作区节点时，接入文件使用账号默认节点。
4. 回到节点编辑器，展开「部署或更新 VM 执行配置」，选择「VM 直接执行」，下载接入配置。该动作轮换此 Node 凭据，只在下载文件返回一次；不会创建云主机，也不会修改正在运行的 Node。自定义沙盒模式必须提供启动器 argv。
5. 将与 Hub 对应的已发布源码、Python 3.10+ 和项目运行依赖安装到 VM，以专用执行账号导入配置：

   ```bash
   python3 -m tools.plugins.node --enroll /absolute/path/enrollment.json \
     --state-directory /var/lib/fleet-node/state
   python3 -m tools.plugins.node --config /var/lib/fleet-node/state/node.json --manifest
   python3 -m tools.plugins.node --config /var/lib/fleet-node/state/node.json
   ```

   `--manifest` 可检查 `workspace_execution.mode`、绑定 ID 及 exec 可用性，不访问 Hub。常驻进程约每 30 秒心跳，每 2 秒领取一条命令；单个 Node 串行执行。接入文件含凭据，不能提交到 Git 或发到日志。
6. 看到心跳后，将新工作区选为账号默认工作区。新对话使用该绑定；已有对话覆盖和已提交 Run 不会被迁移，需在已有对话中显式切换工作区。旧工作区数据保留。

`POST /api/platform/v1/settings/nodes/<node_id>/enrollment` 使用管理员权限及现有 `owner_id` 查询作用域；body 为 `{hub_url,execution_mode,sandbox_launcher}`。工作目录从该 owner 的启用工作区读取，拒绝缺少节点、能力、工作目录、远程开关或签名的配置；校验失败不会轮换凭据。

## 执行与生命周期

- `execution_mode=vm_direct`：任意 argv，可显式使用 `sh -c`、`python3 -c` 等解释器；不套用沙盒命令白名单。cwd 是已绑定目录，HOME/TMP 位于其 `.fleet-home` / `.fleet-tmp`，继承 PATH，不继承 Node 服务的令牌环境。命令可访问执行账号拥有的 VM 文件和网络；此模式不是目录隔离或虚拟机管理器。
- `execution_mode=sandbox`：原沙盒策略保持；必须提供启动器。未提供启动器的普通 Node 不再声称支持 exec。Hub 本地执行继续使用原沙盒模式。
- 单条命令最长 30 秒，stdout/stderr 合并并截断至 64 KiB。超时、取消使用原进程组/Windows Job 清理；无法确认停止时返回 unknown，不冒充成功。后台常驻服务应通过专用服务管理，不靠命令遗留子进程。
- 非零退出保留 returncode、输出及 failed 工具卡片，模型可据此修改下一条命令。没有自动重放失败命令。超时、取消、权限/路径错误和未知执行仍按原终止语义处理；模型步骤预算继续生效。
- 模型正文、重试提示及工具状态沿现有运行事件实时反馈；本版不宣称逐行流式转发命令 stdout。VM 文件读写可用，普通远程工作区不因此新增产物上传或浏览器能力。固定 Workflow 测试 Node 的配置及沙盒规则仍独立。

## 常驻服务、更新与回滚

Linux 使用 systemd，指定执行账号、发布目录、Python 和 `--config` 绝对路径；配置 `Restart=on-failure`、`KillMode=mixed`，停止宽限应大于命令执行与回执收尾时间。源码/依赖与 state/工作区分离，切换发布目录前保留上一版。

更新执行模式、目录或凭据时，先停止 Node。备份 `node.json`、`node-credential`、`command-public-key`；保留同一 state 的 `commands.db` 及其 SQLite 伴随文件。首次导入会拒绝覆盖已有三个配置文件，需将这三个文件移入备份目录后再导入并启动。不要删除 journal 或换用空 state，否则会丢失命令去重证据。轮换后的旧凭据不能用备份直接恢复，需要重新签发配置。

回滚前先停止 VM Node，确认在途 Run 的真实状态，将账号默认工作区恢复为之前记录的值，再切换 Hub/Node 发布目录。历史 Run、工作区文件和 journal 必须保留。对 unknown 命令先核对实际副作用，不能通过重建 Run 自动补跑。

## 验证

```bash
python3 -m pytest tests/test_vm_execution.py tests/test_workspace_node_setup.py \
  tests/test_platform_readiness.py tests/test_native_tool_recovery.py -q
FLEET_PLAYWRIGHT_MODULE=/absolute/path/to/playwright \
  python3 -m pytest tests/test_settings_browser.py -q
```

自动化覆盖真实子进程、签名 HTTP Hub→Node→回执→模型纠正、崩溃恢复不重放、取消及后代清理、输出界限、接入下载与账号作用域。真实 VM 还需独立核对服务账号、发布 SHA、文件落点、模型工具结果、刷新恢复和取消后无遗留进程。
