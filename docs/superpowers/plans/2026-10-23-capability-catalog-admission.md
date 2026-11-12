# F4.3 能力目录与原子 admission 实施计划

> **For agentic workers:** 使用 `executing-plans` 在当前隔离工作树逐项实施；每项包含真实 SQLite/HTTP 验证。当前用户已授权继续开发，不提交、推送、合并或部署。

**Goal:** 将管理员能力定义、节点报告和确定性选择接入原 Workflow admission 事务，保留已有执行身份与槽位。

**Architecture:** 纯领域值负责有界解析与固定 schema/hash；具体 router 负责顺序过滤；SQLite repository 负责受鉴权报告和同事务预留。复用 `WorkflowRepository.admit_step`，不复制协调器或执行引擎。

**Tech Stack:** Python 3.10+、Flask、SQLite、标准库；不新增依赖。

## 约束与本轮边界

- 规格：[产物契约 §6](../specs/2026-10-20-artifact-capability-integration-contract.md#6-节点能力目录)、[路线 F4.3](2026-10-20-workflow-platform-roadmap.md#f43-管理能力目录与确定性路由)、[WF-05](../../testing/workflow-platform-acceptance.md#7-wf-05节点选择与资源)。
- 仅在 `codex/workflow-platform-docs` 工作树修改，UTF-8 无 BOM。
- 新 gate `platform_capability_routing_enabled=false`；依赖 Workflow domain，不修改旧布尔 capabilities 或 browser 的 300 秒合同。
- 本轮提供显式 router/admission port 和机器报告入口；现有固定 target 协调器继续原路径。自动选点协调器、Node 执行前复核和完整 WF-05 由后续切片接线，不能宣称本轮完成全部 F4.3。
- 新选点默认 freshness=120 秒，以 Hub `received_at` 和本次进程启动标识判断；重启后旧报告不参与新选择。Node `observed_at` 仅诊断。
- 报告最多 64 KiB，128 个能力 revision、64 个软件项；目录最多 4096 个版本，单 schema 最多 32 KiB/16 层，拒绝外部引用。候选最多 256，返回最多 32 个受限排除原因。超限拒绝，不能截断后默默选择。
- 管理员 policy 登记 owner/node、workspace/template、允许能力、priority、驻留与输入 alias、槽位上限。节点报告的能力、软件和 local policy 只用于交集，不能扩权。
- 资源字节使用精确整数，单项不超过 1 PiB；时间统一 UTC ISO-8601/UTC epoch，revision 不超过 JSON 精确整数范围。

## Task 1：纯领域与确定性路由

**Files:** 新增 `hub/domain/capability.py`、`hub/application/capability_router.py`、`tests/test_capability_router.py`。

**Interfaces:** `CapabilityDefinition` 固定 input/output schema 及 hash；`NodeOffer` 和 `NodePolicy`；`SelectionRequirements`；`select_target(requirements, authorized_nodes, definitions, now, boot_id)` 返回固定 `TargetBinding` 或有界 `SelectionResult`。

- [x] 用表驱动用例分别验证 owner/授权、平台/软件/版本/输入/驻留、新鲜度、双端策略、槽位/内存/磁盘的首个拒绝原因。
- [x] 用 permutation 验证 priority、占用、node_id 的稳定选择，120 秒边界和未来 observed_at 不能延长报告。
- [x] 落盘完整不可变类型、严格 schema/报告解析和 O(n＋报告项数) 路由；无需全量排序。
- [x] 运行 `PYTHONPATH=. python3 -m pytest tests/test_capability_router.py -q`，覆盖非法整数/非 UTC/重复项/未知字段/外部 schema 引用及上限。

## Task 2：版本目录、报告与原子 admission

**Files:** 新增 `hub/infrastructure/capability_repository.py`、`hub/infrastructure/capability_schema.py`、`tests/test_capability_reservations.py`；小幅修改 `workflow_schema.py`、`platform_backup.py`。

**Interfaces:** `CapabilityRepository.register_definition/register_node/record_offer/select/admit`；`admit` 在原 SQLite 写事务中选点、调用原 `admit_step`、保存固定 binding；同代重复请求只返回原 intent，不能换 node/offer/schema/input。

- [x] 目录同 id/revision 异内容拒绝；同 offer revision 只允许资源/健康/诊断时间刷新，软件/能力/local policy 变化必须升 revision。
- [x] 在写事务复查机器凭据指纹、node/workspace enabled、原协调器 claim/revision、当前代和输入 digest。
- [x] 一次有界候选查询与一次持久槽位聚合；unknown/isolated 占位不能因协调器租约过期释放。无候选保持 waiting_node，保存受限原因。
- [x] 两个真实进程同时抢最后槽只成功一个；注入 binding 写失败时 intent/预算/槽位/event 全回滚；失效 claim 和重复 admission 不泄漏。
- [x] 新 schema 可被 gate-off reader/备份/恢复识别；提升最低 reader，未知或损坏 schema 拒绝；恢复 frozen，旧 offer 不变成新鲜报告。
- [x] 运行 `PYTHONPATH=. python3 -m pytest tests/test_capability_reservations.py tests/test_workflow_repository.py tests/test_platform_backup.py tests/test_platform_backup_encrypted.py -q`。

## Task 3：受鉴权报告、门禁与结果记录

**Files:** 修改 `hub/http/node_routes.py`、`hub/config.py`、`hub/bootstrap.py`、`hub/infrastructure/workflow_bootstrap.py`；更新核心契约/路线/进度/验收与 `docs/testing/journeys.json`。

**Interfaces:** `POST /api/platform/v1/nodes/capability-offers` 使用现有机器绑定身份；body 不可选择 owner/node/权限；响应仅含本节点 offer revision 与 Hub received_at。管理员目录通过可信装配参数登记，不新增 operator 写目录入口。

- [x] 验证 gate 默认关闭、operator/ingest 凭据拒绝、跨节点/未知字段/超量拒绝、入口认证后轮换凭据拒绝，旧 heartbeat/poll/manifest 不改变含义。
- [x] 运行上述两个新模块及 Node/Workflow gate/备份回归；Python 3.10/3.13 compileall 和聚焦测试。
- [x] 更新已实现子集 selector；完整 WF-05 保持开放，后续补自动协调器/Node 绑定复核。
- [x] 完整矩阵门禁记录实际失败和平台缺口；冻结源码摘要，文档只追加新鲜证据，不沿用 F4.2 数字覆盖新代码。

最终结果：4330 passed、5 skipped、195 subtests passed，547.25 秒；严格门禁退出 1，40/41 journeys 通过，RELEASE-01 缺三个 Linux pidfd 必需用例。775 个冻结文件运行后逐项一致，完整证据见 `/tmp/agent-fleet-f4-3-final-gate/verification.json` 及[进度记录](../../platform/workflow-platform/implementation-progress.md#f43-目录与显式-admission-最终验证)。自动协调器/Node 执行前复核仍不属于本计划已完成范围。
