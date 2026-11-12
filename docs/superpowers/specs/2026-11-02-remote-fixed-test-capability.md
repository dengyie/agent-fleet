# Remote Fixed Test Capability Contract

日期：2026-11-02。状态：实现、本机回归和独立真实 Linux Node acceptance fixture 已落盘；默认 gate 关闭，生产隔离验收仍开放。

本规格扩展现有工作流执行契约的固定测试子集。它只允许管理员登记的
`coding.test@1` capability 执行固定 `coding-delivery-tests@1` recipe；不改变本地 Hub 固定
测试、普通 native tool、read-only postcheck、unknown/held/isolated 或精确
retry 合同。

## 1. 可信边界

远程固定测试是独立 capability action `tool.workflow.test`。它不是模型工具，
不进入 `NODE_TOOLS`，也不能通过 `workspace.exec` 等价执行。Hub 只从可信的
`RegisteredRunTarget`、固定 `coding-delivery-tests@1` 和当前 capability binding
构造命令；请求、模型消息、Node 自报 recipe、Node 自报路径均无权改变配方。

远程 target 必须同时满足：

- `test_recipe == coding-delivery-tests@1`；
- `remote_test == true`，`execution_node_id == target_key`，且 target_key 是登记的远程 Node；
- 冻结后的 Run 配置只含 `owner_id`、`workspace_id`、`execution_node_id`、`workflow_dispatch`、`workflow_test`、`workflow_remote_test`、`workflow_capability`；
- capability route 的 adapter/policy、owner/workspace、template digest、step=`test` 与 resolved contract 精确匹配；
- route 的 `operating_systems` 必须精确为 `('linux',)`，不能为空、为 Windows 或混合平台；
- Node offer、local registration、credential、资源和固定 action 同时通过原 admission/preflight。

没有有效 input receipt 时不得创建或开始测试 Run。对于 coding delivery，source
的 `ArtifactReference`、`CodeRevisionReference`、subject digest、owner、workspace、
workflow、step、generation、dispatch token/payload hash 必须逐项相等；Hub 的
ArtifactRef 存在本身不表示远程文件已就绪。

## 2. 输入与执行

协调器复用现有 `artifact.receive` grant 和 receive journal。输入与目标由原准备事务固定，
签收完成后才提交远程测试 admission；重复 tick 只查回原 grant/receipt，不重新下载。
Node 通过 grant 对应的 receive journal 和受保护 descriptor 读取完整 source bundle，
不得根据客户端路径拼接文件名，也不得读取任意 input alias。本动作首版仅启用 Linux：
`open_received` 消费路径使用 POSIX descriptor，平台、接收 adapter、已登记输入 root
或 launcher 不可用时返回 `capability_unavailable`。已有 Windows 产物接收能力不自动
授予本动作的执行能力。读取同一文件 descriptor，按 64 KiB 块重算 hash，消费前后
复核身份；tar 解包流式处理，沿用 10 MiB/4096 entries 等既有源码包上限和 tree/blob 校验。
成员以字典关联 manifest；blob 验证为 O(归档字节数)，tree 规范排序使整体时间
O(归档字节数 + entries × log(entries))，内存有界于 manifest、
entry 索引和块缓冲；不全量载入源码包，不增加第二套收发协议。

请求 wire 的字段集合必须精确为 `schema_version`、`recipe_id`、`recipe_revision`、
`workflow_id`、`generation`、`dispatch_token`、`input_digest`、`binding_digest`、
`source_revision`、`subject_digest`、`receive_receipt`。前三项固定为
`1`、`coding-delivery-tests`、`1`；`source_revision` 是既有 named reference wire，
`ref_key=source` 且类型为 `code_revision`。`receive_receipt` 只含 `grant_id`、
`artifact_id`、`sha256`、`size`、`input_alias`，必须匹配原签收及 source_bundle。

Node 使用现有 `FixedTestRunner`、管理员配置的 `SandboxBackend` launcher 和私有
临时 checkout。recipe argv、timeout、launcher、工作目录和环境白名单均来自 Node
固定内建 recipe 及管理员配置；命令只提供固定 source identity、grant identity 和 subject，不能提供 argv、
shell、launcher、输出路径或额外环境。没有 launcher 时 fail closed。Node 的固定
recipe timeout 为 30 秒；Hub 命令截止为 `min(active_deadline, now + 90秒)`，
回执等待不超过 90 秒，并继续检查原 Run/Workflow 的取消、租约和授权。

## 3. 结果 wire 与 Hub 提交

为保持现有 Command 结果 64 KiB 上限，Node 回执仅允许以下有界字段：

- 字段集合精确为 `schema_version`、`recipe_id`、`recipe_revision`、`executor_id`、`passed`、`exit_code`、`timed_out`、`report_hash`、`raw_report_b64`、`junit_b64`、`environment_b64`、`recipe_argv`；
- `schema_version=1`、`recipe_id=coding-delivery-tests`、`recipe_revision=1`；
- `raw_report_b64` 最多解码 8 KiB；`junit_b64` 最多解码 32 KiB；`environment_b64` 最多解码 4 KiB；
- base64 解码后必须满足现有 `TestValidation`/`test_report_bytes` 完整 schema、hash、source revision、subject、recipe、环境和 JUnit 核验。

回执还必须包含 Node 实际固定配方的 `recipe_argv`：长度最多 32 的非空字符串数组，
每项最多 4096 字符且不含 NUL。首项为 Node 的绝对 POSIX Python 路径，其余参数与
固定配方逐项相等；Hub 核验整个数组的摘要等于环境中的 `recipe_argv_sha256`。
Hub 与 Node 的 Python 安装路径可以不同，不能用 Hub 本机路径计算出的完整摘要
判断远程配方。该数组只是认证 Node 回执的有界执行证据，不进入请求，不改变执行，
也不构成隔离证明。

编码后完整 Node result 仍必须小于 64 KiB；超限失败并保留原异常链，禁止截断或
把聊天回复当报告。Node 不直接写 `workflow_artifact_refs`，也不把报告导出成普通
`artifact.export` output；Hub 在核验完整字节后复用 `WorkflowCodingDeliveryService.record_test`
发布 `test_report`/`junit` 并在同一事务提交 evidence、投影、事件和 operation receipt。

测试断言失败与执行失败分开：完整有效报告可使原 Command/Run 执行 succeeded，
同时 `passed=false` 使验证失败并阻断交付。JUnit 缺失、结果超限、不完整或校验失败
不生成 passed evidence；命令已持久入队后的未确认错误按 unknown 保守收尾。

## 4. 状态、取消、重启与未知

`tool.workflow.test` 使用 `manual_only`。Command journal、Run lease 和 workflow
binding 都是原有持久事实：

- Node 进程退出或回执丢失时，原 command id 查回 terminal receipt；不自动重放测试；
- `unknown`、取消、lease 失效、source 读取失败、报告不完整或 Hub 发布未确认均不产生 passed evidence；
- capability reservation 只有在原 Run 有确定 terminal observation 且 Hub 事务提交后按原规则释放；unknown 保持 held/isolated；
- Hub 只重试报告发布/receipt 查回，不重跑固定测试；generation、dispatch token、binding、source receipt 任一变化即拒绝；
- postcheck helper 仍是独立 read-only action，不能确认固定测试因果，也不能释放远程测试 reservation。

命令 ID 固定为 `workflow-test-<dispatch_token>`。原 journal 的 terminal receipt 优先
查回；报告发布最多尝试三次，只刷新同一 binding/source 的当前事务目标，不重跑 Node。
远程 Run 创建的同一事务将 `minimum_reader_version` 至少提高到 10；schema version
仍为 1。旧 reader 在打开时拒绝，不能将远程 target 当作本地固定测试执行。明文/加密
备份保留 reader 10、原绑定和 held 预约；恢复进入 frozen，不因 lookup 启动测试。

## 5. 平台边界与验收

`NodeRuntimeConfig.workflow_test_enabled` 默认 false。显式启用还要求固定 action、
`artifact.receive`、签名、本地 capability registration、输入 alias 和非空 sandbox launcher；
不满足可用性时不对外声明 offer。Hub 只在 Workflow、transfer、capability、remote execution
和命令签名均具备时装配远程执行服务，仍须显式启动原 worker。

launcher 可解析不证明隔离；环境报告的 `adapter_host` scope 也不证明沙箱内状态。
本机回归之外，独立 Linux acceptance fixture 已在 gz-cvm 的冻结源码副本上，通过原 Hub/Run/Command/Artifact HTTP 链实际驱动 Linux `NodeRuntime`、SQLite command/receive journal、固定 launcher 与 pytest/JUnit；通过、断言失败、丢回执恢复及 unknown 四个场景均有独立证据。它证明本夹具下的接线与 Linux launcher 行为，不等同于生产节点部署或生产隔离认证。Windows 此动作不可用；
其专用句柄消费/执行与验收属于后续独立范围。默认 capability/workflow/transfer gate
全部关闭，完整 WF-04/05 不标记完成。验收至少覆盖：签收先于 admission、固定
recipe/argv 不可篡改、报告字节和 JUnit 完整核验、丢回执重启不重跑、unknown 保留
reservation、错误 generation/token/receipt 被拒绝，以及本地固定测试回归不变。

实施清单见[实施计划](../plans/2026-11-02-remote-fixed-test-capability.md)，实际验证范围与报告见
[实施进度](../../platform/workflow-platform/implementation-progress.md#2026-11-02-独立远程固定测试能力)。
