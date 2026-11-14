# Production Root Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 修复已确认的远程工具能力漂移、VM 后代进程逃逸、节点接入配置不可安装和初始控制请求瞬时失败四个生产问题，并通过 CI 自动部署到 main 后验证生产链路。

**Architecture:** Hub 根据节点真实能力生成远程模型工具声明和 admission 集合；Node 继续执行已签名命令。VM 命令在 Linux 上进入由本次命令拥有的 cgroup，Windows 继续复用 Job Object。接入配置在凭据轮换前按 Node 实际解析格式验证序列化大小，控制请求只在尚未启动命令时做有界、可取消的传输重试。

**Tech Stack:** Python 3.10, Flask, SQLite, pytest, Linux cgroup v2 / Windows Job Object, GitHub Actions deployment.

## Global Constraints

- 保持工作在当前隔离 worktree，不直接改主 worktree。
- 不扩大 VM 执行权限；本轮不处理安全策略重设计。
- 不自动重放已开始或结果不确定的命令；所有重试必须发生在执行前且受原始 deadline 约束。
- 所有新增测试必须先证明原始复现失败，再证明修复后通过。

### Task 1: Remote tool capability authority

**Files:**
- Modify: `hub/application/run_worker_service.py`
- Modify: `hub/platform/tool_broker.py` or the smallest existing capability helper
- Modify: `tools/platform/workspace_registration.py` if the shared capability contract needs correction
- Test: `tests/test_platform_run_worker.py`, `tests/test_vm_execution.py` or a focused new regression module

- [x] Add a regression where a VM node advertises only its actual workspace capabilities and the model cannot receive `fleet.list_services`, `service.*`, or `workspace.artifact` unless a matching executor exists.
- [x] Add a regression proving an advertised remote tool is admitted by the broker and executable by the Node, while unsupported tools fail before dispatch with a stable configuration error.
- [x] Derive remote declarations from the Node capability set and Hub-owned executors instead of subtracting only `LOCAL_ONLY_TOOLS`.
- [x] Run the focused tests and the original unsupported-tool reproduction.

### Task 2: VM descendant lifecycle ownership

**Files:**
- Modify: `fleet_core/process/bounded_process.py`
- Modify: `fleet_core/backends/vm.py` only if the process API needs an explicit scope
- Test: `tests/test_bounded_process.py`, `tests/test_vm_execution.py`

- [x] Add a Linux regression using a detached `setsid` child that writes after the parent exits; the parent receipt must not return while an owned descendant remains, and cleanup on timeout/cancel must reclaim it.
- [x] Implement an owned per-command cgroup scope when cgroup v2 is available, with refusal before spawn when delegation is unavailable and `unknown` when descendant termination cannot be confirmed.
- [x] Keep unrelated Node processes outside the scope and preserve Windows Job Object behavior.
- [x] Run the focused lifecycle tests and the original delayed-write reproduction.

### Task 3: Enrollment serialization contract

**Files:**
- Modify: `hub/application/workspace_node_setup.py`
- Modify: `tools/plugins/node.py`
- Modify: `frontend/assets/views/settings/catalogs.js` only if the download encoding must be aligned
- Test: `tests/test_workspace_node_setup.py`

- [x] Add Unicode, maximum roots, launcher and exact serialized-byte boundary tests.
- [x] Validate the exact downloaded enrollment and generated `node.json` byte budgets before rotating the live credential.
- [x] Ensure rejection leaves the prior credential and files untouched; valid output still round-trips through install/load.
- [x] Run the original oversized enrollment reproduction.

### Task 4: Initial command-control transport retry

**Files:**
- Modify: `tools/platform/command_control.py`
- Modify: `tools/platform/node_client.py` only for retry budget/configuration plumbing
- Test: `tests/test_node_command_control.py`

- [x] Add a regression where the first control request fails transiently and the second succeeds before execution; no command may spawn before control is accepted.
- [x] Distinguish transient transport failure from definitive `ControlRejected` and user cancellation.
- [x] Retry only before first execution, within the command expiry/deadline and bounded transport attempts; preserve terminal no-replay behavior after execution starts.
- [x] Run the original one-request cancellation reproduction.

### Task 5: Release verification and deployment

- [ ] Run focused regressions, full Python suite, lint and release packaging checks.
- [ ] Commit the root fixes and tests on the feature branch.
- [ ] Rebase onto the latest `origin/main`, merge/push through the repository's release workflow, and wait for CI test/package/deploy jobs.
- [ ] Verify the deployed revision, Node heartbeat, one successful command, transient-control retry, and cancellation cleanup using the existing production runbook.

