# F4.3 Hub 固定能力 Run 与 Command 派发实施计划

> **For agentic workers:** 使用 `executing-plans` 在现有独立工作树逐项执行。当前会话直接实施，不提交、推送、合并或部署。

**Goal:** 由原 admission 的固定 binding 驱动受信任 Run/Command 准备，并在首次投递前重新核验，形成可独立验证的 Hub/Node 闭环。

**Architecture:** 复用 WorkflowRepository、RunStepExecutor、CommandDeliveryService 的原事务与签名，新增一个 SQLite 派发适配器。纯领域解析固定 requirements/action 注册；单节点查询复用能力目录，排除本步骤自身的持久预约，再加回原需求验证资源。动作注册字典 O(1) 查询，每命令固定 binding 最多 16 项；无候选全量排序、第二执行器或第二状态库。

**Tech Stack:** Python 3.10+、现有 Flask/SQLite/cryptography、标准库；无新依赖。

## 全局约束

- 基准：[产物契约 §6.1–6.4](../specs/2026-10-20-artifact-capability-integration-contract.md#6-节点能力目录)、[执行契约](../specs/2026-10-20-workflow-execution-contract.md)与[验收 WF-05](../../testing/workflow-platform-acceptance.md#7-wf-05节点选择与资源)。此计划不新增业务授权。
- 只接受管理员登记的 capability revision、adapter 与固定 Node 动作映射；定义 retry_class 必须等于既有工具 retry 合同及 Command 值。
- 普通 tool/operator 参数中的 `_fleet_capability` 一律拒绝；实际上下文从原 binding/Run/token/当前 generation 查回，置于原签名覆盖范围。
- Run 仅支持已登记的 native remote Node 路径；旧 CLI Task、Hub 本机 test/review 不冒称支持 Node preflight。
- 新 Run 在原创建事务验证固定 target 与配置；已有 Run/Command 查回原事实，不因目录后续变化重新创建。
- Command 准备、浏览器审批消费、签名、入队和 outbox 原子提交；重复先查回，审批不得再次消费。
- 首次 Command lease 再核对当前 binding、报告、策略、generation、Run 生命周期与工作流控制。pause/drain/frozen 暂停新投递，失效不产生 Node 副作用；已执行回执仍按原协议接收，unknown 不释放槽位。
- gate-off/未装配不能把 capability-bound Run 退回普通无绑定命令；旧无 binding 路径保持原语义。
- 自动协调器/输入 target 选点、capability receive/export、跨平台和外部真实验收在后续切片。完整 F4.3/WF-05 保持开放。

## Task 1：固定记录解析与派发复核

**Files:** 修改 `hub/domain/capability.py`、`hub/domain/capability_execution.py`、`hub/infrastructure/capability_repository.py`；新增 `hub/infrastructure/capability_dispatch.py`、`tests/test_capability_dispatch.py`。

**Interfaces:** `SelectionRequirements.from_snapshot(raw: bytes) -> SelectionRequirements`；`RegisteredCapabilityAction(adapter_id: str, capability: CatalogRef, action: str)`；`CapabilityDispatchRepository.validate_dispatch(connection, token, *, now) -> TargetBinding | None`。I/O 适配器持有目录 port，纯领域无框架依赖。

- [x] 写并运行失败证据：原 admission 占满单槽后仍可复核自身；软件/revision/策略变化、坏摘要、缺预算或槽位拒绝。
- [x] 严格解析 requirements，快照字段、类型及摘要闭包与原合同一致。
- [x] 以原 identity 排除自身 held 预约，保留其他 held/isolated/资源未知记录；只检查原节点与原能力 revision。
- [x] 用真实 SQLite 回归 `PYTHONPATH=. python3 -m pytest tests/test_capability_dispatch.py tests/test_capability_reservations.py tests/test_capability_router.py -q`。

核心测试断言：

```python
with repository.workflows.transaction() as connection:
    assert guard.validate_dispatch(connection, admitted.intent.token, now=104) == admitted.selection.binding
```

## Task 2：Run 与原 Command 事务接线

**Files:** 修改 `hub/infrastructure/workflow_run_executor.py`、`hub/application/command_delivery_service.py`、`hub/infrastructure/command_repository.py`、`tools/platform/remote_tool_broker.py`、`hub/infrastructure/workflow_bootstrap.py`；扩充 `tests/test_capability_dispatch.py` 和实际 HTTP 用例。

**Interfaces:** `CapabilityDispatchRepository.prepare_command(connection, command, *, now) -> PlatformCommand`；`guard_command(connection, row, now) -> None`；RunStepExecutor 显式传入 `capability_dispatch`；gate-on 装配 `workflow_capability_actions`。Action retry 合同由 RemoteToolBroker 原规则统一提供。

- [x] 先复现：caller 保留字段可入队、gate-off 无绑定回退、已有审批/Command 重试以及派发前配置变化。
- [x] 新 Run 原事务核对 node/adapter/current binding；保存受信任摘要标记，lookup 使用持久旧值而非当前报告。
- [x] Command 原事务查回 Run 与 binding、准备保留参数并重新签名；审批消费在同一事务，失败整体回滚。
- [x] 重复先比较原工具参数与 retry/作用域，返回原完整签名 Command；不重跑准备或再次消费审批。
- [x] Node claim 前再复核：控制暂停暂缓、能力变化保存确定拒绝，未派发命令不变成 unknown；在途命令保持原 journal/receipt 语义。
- [x] 用真实 HTTP 从登记报告/显式 admission/原 Run/Command 到 Node 文件读取；另测软件变化和旧结果重放。

核心测试断言：

```python
saved = delivery.enqueue(command, idempotency_key=command.command_id)
assert verify_command(saved, public_key)
assert CapabilityExecutionContext.from_command(saved).binding == admitted.selection.binding
assert delivery.enqueue(command, idempotency_key=command.command_id) == saved
```

## Task 3：证据、矩阵和边界记录

**Files:** 更新本计划、产物契约、路线、开发入口、实施进度、验收说明和 `docs/testing/journeys.json`。

- [x] Python 3.10/3.13 关联集合及 compileall，包含原审批、Command reconciliation、Node 收发和 Workflow dispatch。
- [x] 挂入已实现 NODE-01/WF-08 selector；保持完整 WF-05 未完成。
- [x] 冻结最终代码/规格/矩阵并运行完整严格门禁；记录 JUnit、journeys、配置、文件摘要、实际平台 skip 与外部 `not_run`。
- [x] 核对 UTF-8 无 BOM、Markdown 本地路径/锚点与 `git diff --check`。只为实际验证子集作结论。

完整门禁沿用现有 Playwright/Chrome：

```bash
FLEET_PLAYWRIGHT_MODULE=/tmp/agent-fleet-f4-1-browser-deps/node_modules/playwright \
FLEET_BROWSER_CHANNEL=chrome \
FLEET_SCREENSHOTS=/tmp/agent-fleet-f4-3-hub-final-gate/browser \
PYTHONPATH=. python3 -m pytest tests -q --require-journeys \
  --journey-report=/tmp/agent-fleet-f4-3-hub-final-gate/journeys.json \
  --junitxml=/tmp/agent-fleet-f4-3-hub-final-gate/results.xml
```

验证结果：Python 3.10.21 关联集合 539 passed/45.15 秒；Python 3.13.7 预检集合 164 passed/15.02 秒。最终完整门禁 4459 passed、5 skipped、195 subtests passed/557.77 秒；JUnit 实际 4464 个 testcase、0 failures/0 errors。40/41 journeys 通过，RELEASE-01 的三个 Linux 必需用例在 macOS 跳过，严格退出 1；另两个 Supervisor `/proc` 用例跳过。五项外部检查仍 `not_run`，未改动 required selector。

`/tmp/agent-fleet-f4-3-hub-final-gate/` 保存配置、JUnit、journeys、source-digests、verification 和截图。782 个冻结文件运行后逐项一致；矩阵 SHA-256 为 `cbcfd9ae891d4dc19a4eb56b388571bdd026f108033fee6aaa9f5a9b6515ed94`。预检 8 份文档、67 个本地路径/锚点和 780 个 UTF-8 文本无 BOM，Python 3.10/3.13 compileall 与 diff 检查通过。详见[实施进度](../../platform/workflow-platform/implementation-progress.md#f43-hub-固定能力-run-与-command-派发2026-10-23)。本段在验证后追加，只记录该冻结树实际覆盖的子集；完整 F4.3/WF-05 仍开放。
