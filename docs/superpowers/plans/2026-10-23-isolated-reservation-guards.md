# Workflow isolated 资源屏障修复与验证计划

> **For agentic workers:** 使用 `executing-plans` 在既有独立工作树逐项执行；本计划记录已经复现和修复的同源缺陷以及尚待执行的最终门禁，不提交、推送、合并或部署。

**Goal:** 固定目标 admission 和 retry/invalidate 与能力路由一致保留 isolated reservation，不超售物理容量或绕过旧代未决资源。

**Architecture:** 只在原 SQLite 写事务的容量计数与旧代屏障查询补齐 isolated，不新增表、服务、接口、状态或后台任务。目标/状态复用 `capability_held(target_key,state)` 索引；代际查询最多 8 步并按 reservation 主键查证，边界为 O(8 log n)。

**Tech Stack:** Python 3.10+、既有 Flask/SQLite、pytest；无新增依赖。

## 全局约束与证据

- 基准：[产物契约 §6.1](../specs/2026-10-20-artifact-capability-integration-contract.md#61-确定性选择)、[执行契约 §7](../specs/2026-10-20-workflow-execution-contract.md#7-版本变更重试和恢复)、[WF-05](../../testing/workflow-platform-acceptance.md#7-wf-05节点选择与资源)。target_key 是全局物理资源键，不能按 owner 划出额外容量。
- held/isolated 均保留占用；released 不占用。这里不增加将 isolated 释放或自动核清 unknown 的协议，完整能力 postcheck 保持开放。
- 所有文件 UTF-8 无 BOM；复用用户已授权的 `codex/workflow-platform-docs` 工作树，保留主工作树的其他工作。

## Task 1：失败证据与最小修复

**Files:** `tests/test_workflow_repository.py`、`tests/test_workflow_control.py`、`hub/infrastructure/workflow_repository.py`、`hub/infrastructure/workflow_control_repository.py`。

**Interfaces:** 原 `admit_step(...) -> DispatchIntent` 与 `control(WorkflowControlRequest, ...) -> WorkflowControlReceipt` 不变；拒绝仍使用 `capacity_exhausted` / `unresolved_execution`，保留现有异常链。

- [x] 单一临时 SQLite 回归先复现 fixed target 仍获准，`/tmp/agent-fleet-isolated-fixed-capacity-before.xml` 为 1 failed。
- [x] 扩展同 owner/跨 owner、held/isolated/released、其他 target、两个真实 OS 进程最后容量竞争，以及 retry/invalidate 旧代屏障；`/tmp/agent-fleet-isolated-reservation-guards-before.xml` 为 5 failed/4 passed。
- [x] 确认同源根因：原容量查询及 `_regenerate` 只检查 held；动态路由已计入 held/isolated。只将这两个原事务条件统一为：

```sql
state IN ('held','isolated')
```

- [x] 重跑原失败实例，`/tmp/agent-fleet-isolated-reservation-guards-after.xml` 为 9 passed。拒绝后 revision/events/intent/预算/槽位/operation/generation 保持原值；原 isolated 不被隐式释放。

## Task 2：关联回归与文档合同

**Files:** 更新 `docs/testing/journeys.json`、测试入口/链路/验收、两份核心契约、开发入口/路线与实施进度。

- [x] Python 3.10.21 与 Python 3.13.7 分别运行：

```bash
PYTHONPATH=. python3 -m pytest \
  tests/test_workflow_repository.py tests/test_workflow_control.py \
  tests/test_workflow_coordinator.py tests/test_capability_router.py \
  tests/test_capability_reservations.py tests/test_capability_dispatch.py \
  tests/test_capability_coordinator.py tests/test_workflow_budget.py -q
```

两版本均 281 passed，分别见 `/tmp/agent-fleet-isolated-reservation-related-python310.xml` 与 `...-python313.xml`。

- [x] 将两个容量 selector 加入 NODE-01/WF-02/WF-08，代际 selector 加入 WF-03/WF-08；完整 WF-05 不以此子集代填。
- [x] Python 3.10.21/3.13.7 `compileall`、16 个 Node 前端 fixture 语法、UTF-8 无 BOM、120 份 Markdown 的 206 个本地链接/锚点及 `git diff --check` 均通过；787 个 UTF-8 文本与 2 个二进制文件均无编码/AST 检查错误。
- [x] 冻结源码/规格/矩阵后运行完整 `--require-journeys`，保存配置、逐文件摘要、JUnit、journeys、截图、核验摘要与退出码。macOS 的 Linux pidfd/`/proc` skip、dirty 状态和外部 `not_run` 均如实记录于实施进度；RELEASE-01 因此保持 incomplete，不作为发布门禁通过。
