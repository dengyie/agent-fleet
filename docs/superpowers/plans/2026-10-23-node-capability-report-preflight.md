# F4.3 Node 能力报告与执行前复核实施计划

> **For agentic workers:** 使用 `executing-plans` 在现有隔离工作树逐项实施；先运行失败用例，再复验同一用例。不提交、推送、合并或部署。

**Goal:** 让显式配置的 NodeRuntime 发送真实、有界报告，并在原 Command 执行前核对固定能力、软件和本地策略。

**Architecture:** 不新增执行器或数据库。复用原 NodeClient、Command 签名、journal 与有界 HTTP；保留无新配置的旧路径。纯领域值定义签名上下文，本地服务负责事实采集与策略交集。候选和软件索引用字典，O(注册能力数＋软件数)，不逐项执行外部版本探测命令。

**Tech Stack:** Python 3.10+、标准库、现有 cryptography/Flask/SQLite，不新增依赖。

## 全局约束与边界

- 规格：[能力契约 §6](../specs/2026-10-20-artifact-capability-integration-contract.md#6-节点能力目录)、[WF-05](../../testing/workflow-platform-acceptance.md#7-wf-05节点选择与资源)。
- `NodeRuntimeConfig` 的新配置默认 None；原布尔 manifest、heartbeat 与旧 journal 语义保持原合同。
- 报告在显式 `report_capabilities()` 或现有 `poll_once()` 生命周期内发送；不启动后台线程。每次重采 OS/架构、软件和资源，最多 15 秒、64 KiB 响应，不携带凭据跟随重定向。
- 软件默认仅报告当前 Python 解释器；不执行任意程序或把配置字符串冒充探测结果。额外事实由管理员注入的类型化 observer 提供。默认不能可靠取得可用内存时报告 0 字节；硬盘读取当前 workspace 所在卷。offer revision 由管理员显式管理，软件变化但 revision 未提升时保留 Hub 冲突，不自动改号绕过。
- 管理员登记能力完整定义和允许动作；仅当这些动作在当前 Node 确实可用时报告该能力。本地 allowlist 单独配置且与实际实现取交集，报告不能增加 Node 权限。
- 签名上下文保存在 `arguments._fleet_capability`，固定 schema_version=1、capability_id、adapter_id、binding；单次动作必须由 binding 中明确的一项能力提供。原 args_hash 和 Ed25519 覆盖全部上下文，无新增签名字段或状态表。仅支持已登记的 tool/postcheck 动作；receive/export 继续原独立协议，不在这一子集接受新上下文。
- 执行前比对 node、offer revision/config digest、完整定义 digest、adapter、动作、本地 allowlist、健康/槽位和当前资源。拒绝为 `capability_changed`，不触发副作用。旧路径未装配服务时，出现新上下文必须拒绝，不能直接忽略。
- journal 已确认的终态只查回原结果；guard 置于首次执行前，不能因后来的能力变化重跑或撤回旧完成事实。NodeClient 验签覆盖上下文，executor 只在核验后去掉保留参数，保留原命令标识。
- 本轮不使现有协调器自动选点；Hub 的可信绑定注入/派发复核与协调器接线在后续完成，完整 F4.3/WF-05 继续开放。

## Task 1：纯签名上下文与本地事实/策略

**Files:** 新增 `hub/domain/capability_execution.py`、`tools/platform/node_capabilities.py`、`tests/test_node_capabilities.py`。

**Interfaces:** `CapabilityExecutionContext` 固定解析/绑定；`LocalCapability`、`NodeCapabilityConfig`、`NodeCapabilityFacts`；`NodeCapabilityService.offer(now)` 与 `prepare(command, now)`。observer 是本机 I/O port，不给内部纯逻辑增加接口。

- [x] 用实际签名命令验证上下文更改使签名无效、缺字段/重复键/超限拒绝。
- [x] 用首次执行前的能力/软件/本地策略变化复现拒绝；同 revision 资源刷新不改变 config digest，但不足仍拒绝。
- [x] 实现完整类型、索引、默认事实采集和有界报告客户端，保留原异常链与稳定错误码。
- [x] 运行 `PYTHONPATH=. python3 -m pytest tests/test_node_capabilities.py tests/test_capability_http.py -q --junitxml=/tmp/agent-fleet-f4-3-node-command-types-after.xml`。

## Task 2：原 NodeRuntime/Command 生命周期与实际 HTTP

**Files:** 修改 `tools/platform/node_runtime.py`、`node_client.py`、`node_executor.py`；扩充 `tests/test_node_capabilities.py`、`tests/test_capability_http.py`。

**Interfaces:** `NodeRuntime.report_capabilities()` 返回本节点 UTC receipt；显式配置时 poll 先报告，报告失败以稳定码附着结果并继续处理原在途命令；`NodeToolExecutor` 在首次执行前调用 prepare。

- [x] 保留未配置 Node 的原请求数量/manifest；关闭 Node 后取消新报告。
- [x] 实际 loopback HTTP 向原受鉴权入口报告并取得同一 NodeOffer；覆盖凭据、坏 receipt、重定向、响应上限、deadline 与停止。
- [x] 真实 workspace 文件写入验证成功一次、改变软件/本地策略时零副作用、完成后重放不重跑。
- [x] 运行新测试及原 NodeRuntime/NodeClient/Command、artifact receive/export 和有界 HTTP 回归；Python 3.10/3.13 compileall。

## Task 3：合同与验收记录

**Files:** 修改产物契约、路线、实施进度、验收矩阵与 `docs/testing/journeys.json`。

- [x] 写明 wire schema、本机事实来源、默认关闭及尚未自动派发的边界；仅把已实现实例挂到 NODE-01/WF-08。
- [x] 冻结代码、矩阵和规格，执行严格完整门禁并记录真实平台 skip/外部未验收，不能以上一轮 4330 passed 覆盖新代码。
- [x] 核对 diff 空白、UTF-8 无 BOM、本地路径/锚点及冻结摘要；只对已验证子集作结论。
