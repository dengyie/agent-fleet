# F4.3 协调器能力选点与输入目标固定实施计划

> **For agentic workers:** 使用 `executing-plans` 在既有独立工作树逐项执行；先证据后实现。当前会话直接开发，不提交、推送、合并或部署。

**Goal:** 原 WorkflowCoordinator 根据管理员路由注册自动选择已登记的 remote Node executor；输入领取与目标固定原子提交，输入收齐后使用原 admission/binding/Run 协议。

**Architecture:** 不可变注册只携带受信任身份与筛选约束，capability/adapter/input/预算来自原冻结合同。原协调器调用单一 SQLite I/O port，在原事务复用能力选择、输入准备与 admission；静态 target 路径保留。注册索引 O(1)，候选最多 256，过滤 O(n＋报告项数)，输入候选匹配 O(nk)，n≤256、每节点别名 k≤64，不循环查每个节点、不另建执行状态表。

**Tech Stack:** Python 3.10+、Flask/SQLite、标准库和既有签名依赖；无新增依赖。

## 全局约束

- 基准：[产物契约 §6](../specs/2026-10-20-artifact-capability-integration-contract.md#6-节点能力目录)、[执行契约](../specs/2026-10-20-workflow-execution-contract.md)、[路线 F4.3](2026-10-20-workflow-platform-roadmap.md#f43-管理能力目录与确定性路由)及[WF-05](../../testing/workflow-platform-acceptance.md#7-wf-05节点选择与资源)。本计划不增加 operator 或模型权限。
- domain、dispatch、capability、transfer gate 继续默认关闭；只有管理员显式注册的新路由使用自动选择。无目录/port/能力/目标/签名配置时失败关闭，不回退普通 fixed target。
- 第一版仅原生 remote Node Run；CLI Task、Hub 本地 review/test 和任意 StepExecutor 不作为 Node 实现证据。
- 稳定 owner/workspace/definition digest/step/adapter revision/policy revision；具体 capability revision、input digest、active budget取原合同，资源/平台/软件/角色约束取可信注册。
- 有输入的候选必须有匹配的 `RegisteredNodeInputs`，全部 ref_key 和管理员 input_alias 均获准。先在事务外校验原 artifact 元数据，再于同一短事务选点、固定当前 generation 的 target 并入队原 receive 命令。未收齐前无 intent/预算/槽位/binding/Run。
- 已固定输入 target 只检查原节点；报告、优先级或容量变化不能迁移本代。收齐后按原事务重新选择原节点并预留，两个进程不得超售。
- 崩溃、pause/cancel/drain/frozen、旧 claim/revision/generation、缺输入签收、坏快照保持原 fencing；已有 sending/bound/unknown 仍走原恢复和对账。
- UTF-8 无 BOM；不改主工作树，不访问真实远程节点。完整 WF-05、跨平台和外部验收仍分别记录。

## Task 1：路由注册、原事务组合与失败证据

**Files:** 新增 `hub/domain/capability_routing.py`、`hub/infrastructure/capability_workflow.py`、`tests/test_capability_coordinator.py`；修改 `capability_repository.py`、`artifact_input_repository.py`、`artifact_input_service.py`。

**Interfaces:** `RegisteredCapabilityRoute` 保存上述可信约束；`requirements(definition, resolved) -> SelectionRequirements` 从原合同创建固定需求。`CapabilityWorkflowAdmission.advance(claim, *, expected_revision, generation, resolved, registration, now) -> None` 是协调器使用的 SQLite port 实现。

- [x] 先写并运行失败回归：无手工 admission 的真实协调器选点与 Run 创建；没有候选/port/匹配 executor 时保留等待且不产生副作用。
- [x] 严格验证路由标识、引用 revision、预算/资源整数与元组上限；不得改写原合同能力/输入或 owner。
- [x] 原能力 `_select` 增加有界 eligible_nodes 约束，`admit` 支持同库外层事务且保留 savepoint 回滚；省略参数的显式 admission 行为不变。
- [x] 原输入 repository 支持同库事务复用；输入 service 提取元数据核验供组合使用。验证选点至写入的凭据/报告变化、失败触发器、输入 target 不漂移及 original ID 恢复。

核心断言：

```python
await coordinator.tick(clock())
assert workflows.current_steps(owner, workflow)[0].target_key == "node-a"
assert count("workflow_dispatches") == count("capability_step_bindings") == 0
assert receive_command["target_node"] == "node-a"
```

## Task 2：原协调器与可信装配

**Files:** 修改 `hub/application/workflow_coordinator.py`、`hub/infrastructure/workflow_bootstrap.py`；扩充 `tests/test_capability_coordinator.py`。

**Interfaces:** 协调器注册 union 增加 `RegisteredCapabilityRoute`；新增 `WorkflowCapabilityAdmission` I/O Protocol 和可选 `capability_admission`。有界 tick 中解析动态输入后直接调用该 port；固定 target 路径不变。

- [x] 从真实前驱输出解析、冻结、等待 receive、原事务 admission 到原 Run/Command；注册冲突稳定 defer，配置缺失不回退。
- [x] bootstrap 仅 capability gate-on/有效 signing key 装配 port，使用现有 native Node RunStepExecutor.target 和 transfer gate-on 的原输入服务；不启动线程。
- [x] 测试重启/新鲜报告、控制门禁、已派发优先查回、当前代只检查原节点；两个真实进程争抢最后容量仍只有一次 admission。

核心断言：

```python
assert sum(count(table) for table in (
    "workflow_dispatches", "workflow_budget_reservations",
    "capability_reservations", "capability_step_bindings"
)) == 4
assert run["config"]["execution_node_id"] == pinned_node
```

## Task 3：质量闭环与状态记录

**Files:** 更新本计划、产物契约、路线、开发入口、实施进度、验收与 `docs/testing/journeys.json`。

- [x] Python 3.10/3.13 关联回归覆盖新路由、原 fixed target coordinator/Run dispatch、Node poll 凭据、receive/export/HTTP、备份与控制。
- [x] 登记已实现 NODE-01/WF-08 selector；完整 WF-05 不以本机子集代填。
- [x] compileall、UTF-8 无 BOM、Markdown 本地路径/锚点及 diff 检查。
- [x] 冻结最终代码/规格/矩阵，运行 `PYTHONPATH=. python3 -m pytest tests -q --require-journeys` 并保存 JUnit/journeys/配置/逐文件摘要；平台 skip 与外部 `not_run` 如实保留。

最终关联回归：Python 3.10.21 **599 passed**、Python 3.13.7 **619 passed**。浏览器 fixture 的外部资源/SPA 导航等待修正后，最终全仓严格门禁为 **4501 passed、5 skipped、195 subtests passed**，564.25 秒。JUnit 实际 4506 个 testcase、0 failures/0 errors；NODE-01 的 193 selectors/408 cases、WF-08 的 148/244 全过。严格退出 1，40/41 journeys；RELEASE-01 缺三个本机无法执行的 Linux pidfd 必需项，另两个 Supervisor `/proc` 用例平台跳过；五项外部验收仍 `not_run`。

证据在 `/tmp/agent-fleet-f4-3-coordinator-final-gate-verified/`，包含配置、JUnit、journeys、运行日志、788 个冻结文件摘要与运行后核验；补录前文件全部一致，矩阵 SHA-256 为 `5484770086d6c249b2583a031e08956b6c8a5cf3bf7ec48f4a84e400fb261d4d`。详见[实施进度](../../platform/workflow-platform/implementation-progress.md#f43-协调器与-poll-最终验证)。完整 WF-05、能力 unknown/isolated postcheck、跨平台与真实外部验收保持开放；此计划的运行与记录已完成，不把平台 skip 计为通过。
