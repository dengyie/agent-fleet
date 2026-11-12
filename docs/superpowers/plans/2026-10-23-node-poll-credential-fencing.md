# Node Command poll 凭据竞态修复计划

> **For agentic workers:** 使用 `executing-plans` 在现有独立工作树逐项执行；先红后绿，不提交、推送、合并或部署。

**Goal:** 机器凭据在 HTTP 认证后被轮换或撤销时，原请求不得领取 Command 或改动过期租约事实。

**Architecture:** HTTP 将认证指纹传入原 delivery/repository；在原 `BEGIN IMMEDIATE` 事务的首次写入之前复用 `node_credential_is_current`。不改变命令、签名、审批、能力 binding 或内部无指纹调用协议，不增加状态、接口层或后台任务。

**Tech Stack:** Python 3.10+、既有 Flask/SQLite、标准库；无新依赖。

## 文档与证据

- 基准：[能力契约 §6.4](../specs/2026-10-20-artifact-capability-integration-contract.md#64-hub-固定能力-run-与-command-派发子集)的首次投递复核、[WF-05](../../testing/workflow-platform-acceptance.md#7-wf-05节点选择与资源)机器授权边界，以及用户要求的事务一致性和异常链。
- 仓库外真实临时 SQLite/HTTP 探针在认证后、claim 前轮换凭据，旧请求返回 HTTP 200/1 command，持久状态 leased/attempt=1；复现报告 `/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-poll-credential-race-3_f75y63/reproduction.json`。
- 主键 `(owner_id,node_id)` 查询一条凭据，索引查找 O(log n)，指纹比较固定长度 O(1)；原命令批次继续有界，不新增逐命令查询。

## Task 1：稳定失败证据

**Files:** 新增 `tests/test_node_poll_credentials.py`。

- [x] 参数化普通与能力绑定命令，认证后分别轮换凭据、禁用凭据、禁用节点；断言 HTTP 409/`command_credential_revoked`，queued/attempt=0、过期 leased 和 outbox 保持原值。
- [x] 检查恢复授权后仅原命令被领取一次；有效认证仍走原正常路径，旧 token 不得重新认证。
- [x] 直接 repository 作用域/错误指纹、服务异常链、SQLite 失败回滚和内部无指纹兼容回归。
- [x] 运行 `PYTHONPATH=. python3 -m pytest tests/test_node_poll_credentials.py -q --junitxml=/tmp/agent-fleet-node-poll-credentials-before.xml`，保存实际失败。

## Task 2：同事务复核

**Files:** 修改 `hub/http/node_routes.py`、`hub/application/command_delivery_service.py`、`hub/infrastructure/command_repository.py`。

**Interfaces:** `poll(..., credential_fingerprint: str | None = None)` 和 `claim_for_node(..., credential_fingerprint: str | None = None)`；HTTP 固定传原认证指纹，绝不从 body 读取。

- [x] HTTP 透传 `g.platform_node_credential_fingerprint`。
- [x] claim 事务起始验证原 owner/node/指纹后才允许 lease expiry/领取；失效抛 `CommandRepositoryError("command_credential_revoked")`。
- [x] delivery 映射 HTTP 409，使用 `raise ... from exc` 保留原因；claim 用上下文管理器回滚并关闭，数据库失败保留原异常。

事务内复核规则：

```python
if credential_fingerprint is not None and (
    owner_id is None or not node_credential_is_current(
        conn, owner=owner_id, node=node_id, fingerprint=credential_fingerprint
    )
):
    raise CommandRepositoryError("command_credential_revoked")
```

## Task 3：新鲜验证与真实状态记录

**Files:** 更新本计划、产物契约、验收、实施进度和 `docs/testing/journeys.json`。

- [x] 原失败信号转绿；Python 3.10/3.13 回归原 Node HTTP、Command、能力派发、receive/export、reconciliation 和 Workflow coordinator。
- [x] 新测试登记 NODE-01/WF-08；完整 WF-05 继续开放。
- [x] Python 3.10/3.13 compileall、文档链接/锚点、UTF-8 无 BOM 与 diff 检查。
- [x] 冻结新树重新跑严格完整门禁；记录 actual testcase 节点、平台 skip、外部 `not_run`，不沿用修复前门禁作修复后证据。

实际结果：最终冻结树 **4501 passed、5 skipped、195 subtests passed**，JUnit 4506 个 testcase、0 failures/0 errors；严格门禁退出 1，40/41 journeys 通过。RELEASE-01 的三个 Linux pidfd 必需 selector 在 macOS 跳过，另两个 Supervisor `/proc` 用例跳过；五项外部验收全部 `not_run`。788 个源码/文档/矩阵文件在运行后逐项核对一致，证据位于 `/tmp/agent-fleet-f4-3-coordinator-final-gate-verified/`；详见[实施进度](../../platform/workflow-platform/implementation-progress.md#f43-协调器与-poll-最终验证)。这里完成的是运行与记录，不代表严格门禁或完整 F4.3 已通过。
