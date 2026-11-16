# VM 工作区执行节点

返回 [项目入口](../../README.md)、[官方插件 Node](official-plugins.md)及[测试与发布门禁](../testing/README.md)。实施与验收步骤见[VM 接入计划](../superpowers/plans/2026-11-14-vm-direct-execution.md)及[生产根因修复计划](../superpowers/plans/2026-11-15-production-root-fixes.md)。

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

`POST /api/platform/v1/settings/nodes/<node_id>/enrollment` 使用管理员权限及现有 `owner_id` 查询作用域；body 为 `{hub_url,execution_mode,sandbox_launcher}`。工作目录从该 owner 的启用工作区读取，拒绝缺少节点、能力、工作目录、远程开关或签名的配置；校验失败不会轮换凭据。工作区配置以 UTF-8 格式化 JSON 计最多 48 KiB，为接入信封及安装路径预留空间；下载文件和最终 `node.json` 各最多 64 KiB。安装器在创建文件前验证最终配置大小。

## 执行与生命周期

- `execution_mode=vm_direct`：任意 argv，可显式使用 `sh -c`、`python3 -c` 等解释器；不套用沙盒命令白名单。cwd 是已绑定目录，HOME/TMP 位于其 `.fleet-home` / `.fleet-tmp`，继承 PATH，不继承 Node 服务的令牌环境。命令可访问执行账号拥有的 VM 文件和网络；此模式不是目录隔离或虚拟机管理器。
- `execution_mode=sandbox`：原沙盒策略保持；必须提供启动器。未提供启动器的普通 Node 不再声称支持 exec。Hub 本地执行继续使用原沙盒模式。
- 单条命令最长 30 秒，stdout/stderr 合并，原始读取最多 64 KiB。远程结果还受整个 JSON 回执 64 KiB 界限约束，包含信封和转义开销，因此文本实际保留量可能更少；截断通过 `truncated` 标记，不改变退出码、错误或执行状态。文件读取和目录列表遵守同一界限，列表只删除末尾完整条目。
- Linux VM 直接执行需要支持 `cgroup.kill` 的 cgroup v2 委派，Windows 使用 Job Object；macOS 不提供此直接执行模式。Linux 每条命令先进入自己的 cgroup，成功、失败、超时和取消返回前均递归停止全部后代，包括 `setsid` 和双重 fork；确认范围为空后才移除它。缺少委派时命令不启动，返回 `command_scope_unavailable`；不能确认停止时返回 unknown。后台常驻服务应通过专用服务管理，不靠命令遗留子进程。
- 启动命令前的控制观察最多尝试 3 次，退避分别为 0.1、0.2 秒，每次 HTTP 仍受两秒传输边界及原命令截止时间/取消约束。明确拒绝不重试；传输故障耗尽返回 `command_control_unavailable`。已开始或结果不确定的命令不因重试而重放。
- 非零退出保留 returncode、输出及 failed 工具卡片，模型可据此修改下一条命令。没有自动重放失败命令。超时、取消、权限/路径错误和未知执行仍按原终止语义处理；普通对话不设固定步骤或累计Token上限，显式任务预算由其合同指定。
- 模型正文、重试提示及工具状态沿现有运行事件实时反馈；本版不宣称逐行流式转发命令 stdout。VM 文件读写可用，普通远程工作区不因此新增产物上传或浏览器能力。固定 Workflow 测试 Node 的配置及沙盒规则仍独立。
- 远程模型工具声明与 broker admission 使用同一集合：只有 Node 执行器已实现且节点能力明确启用的工具才允许投递。服务目录、健康、日志、incident 和普通产物发布不会作为不存在的 Node 工具暴露给模型。

## 常驻服务、更新与回滚

### 受管服务动作

标准 Node 也可执行服务目录中声明的固定动作与日志读取。需先升级到支持这些能力的发布版，在现有私有 `node.json` 增加所需动作，并在 Hub 对同一 Node 显式登记相应的 `service.inspect` / `service.restart` / `service.logs` 能力。例如启用只读检查与日志：

```json
{"service_actions": ["inspect", "logs"]}
```

保留其余字段、工作区绑定、凭据和原 `commands.db`。`service_actions` 只接受不重复的 `inspect`、`restart`、`logs`，默认不启用；它可以与普通工作区共用 Node，但不能与专用 Workflow fixed-test 配置混用。执行账号必须能访问对应的 systemd、Supervisor 或 Docker 管理端；此配置不会授予 OS 权限。设置页下载的工作区接入文件不会自动开启服务动作。

普通主助手的 `service.read_logs` 由 Hub 定位服务目录的实际 Node，再以签名 `service.logs` 命令读取固定 adapter/alias 的有限日志；服务 Node 可以不同于工作区 Node。等待期间保存原检查点，恢复只观察原回执，不重新执行。Node 返回的文本经过脱敏和字节截断，工具卡片显示日志预览；读取程序非零退出是明确失败，成功的 Docker 日志包含 stdout 和 stderr。未配置本地 reader 且不存在支持日志的服务 Node 时，不向模型声明日志工具。普通 `service.logs` 与 `reconcile.service.logs` 各自授权；unknown 的日志命令可以申请现有只读 post-check，补充当前证据后原命令仍保留 unknown。

Hub 服务目录的 `adapter`、`target_alias`、`allowed_actions` 和服务版本决定固定命令；模型不能增加 argv。重启先创建现有审批单，用户批准后才派发签名命令；Node 持久 journal 对同一命令只执行一次。普通 inspect 与 reconcile inspect 使用独立能力，互相不授予权限。inspect 明确返回不健康进程状态时，命令已完成且结果为 unhealthy；重启超时或中断保持 unknown，不自动重放。

Docker 的 inspect 命令成功仅表示查询成功；容器状态为 `running` 才记为进程健康，`exited`、`paused` 等停止/过渡状态记为 unhealthy，未识别或空输出记为 unknown。普通检查、复核和采集器使用同一解释规则；这不代表应用级健康检查通过。

主助手的 `service.request_action` 成功仅表示请求已受理。它可用 `service.get_action` 查询原 command_id 的状态与有限结果，或用只读的 `service.get_approval` 查询 grant_id；这两个查询留在 Hub，不会变成工作区命令，也不能批准重启。相关流程门禁为 [ACTION-01](../testing/journeys.json)。生产验收应使用明确命名的独立测试服务，不重启 Hub 或 Node 自身来证明功能。

### Node 常驻与升级

Linux 使用 systemd，指定执行账号、发布目录、Python 和 `--config` 绝对路径；配置 `Restart=on-failure`、`KillMode=mixed`、`Delegate=yes`，停止宽限应大于命令执行与回执收尾时间。Node 自动使用自身服务的 cgroup；显式委派其他范围时可设置 `FLEET_COMMAND_CGROUP_ROOT`，Node 本身必须已经处于该委派子树内。只修改目录所有者而把 Node 留在子树外不能授予跨子树迁移权限。源码/依赖与 state/工作区分离，切换发布目录前保留上一版。

更新执行模式、目录或凭据时，先停止 Node。备份 `node.json`、`node-credential`、`command-public-key`；保留同一 state 的 `commands.db` 及其 SQLite 伴随文件。首次导入会拒绝覆盖已有三个配置文件，需将这三个文件移入备份目录后再导入并启动。不要删除 journal 或换用空 state，否则会丢失命令去重证据。轮换后的旧凭据不能用备份直接恢复，需要重新签发配置。

回滚前先停止 VM Node，确认在途 Run 的真实状态，将账号默认工作区恢复为之前记录的值，再切换 Hub/Node 发布目录。历史 Run、工作区文件和 journal 必须保留。对 unknown 命令先核对实际副作用，不能通过重建 Run 自动补跑。

## 验证

```bash
python3 -m pytest tests/test_vm_execution.py tests/test_vm_command_scope.py tests/test_workspace_node_setup.py \
  tests/test_platform_readiness.py tests/test_native_tool_recovery.py -q
FLEET_PLAYWRIGHT_MODULE=/absolute/path/to/playwright \
  python3 -m pytest tests/test_settings_browser.py -q
```

自动化覆盖真实子进程、签名 HTTP Hub→Node→回执→模型纠正、崩溃恢复不重放、取消及后代清理、输出界限、接入下载与账号作用域。真实 VM 还需独立核对服务账号、发布 SHA、文件落点、模型工具结果、刷新恢复和取消后无遗留进程。
