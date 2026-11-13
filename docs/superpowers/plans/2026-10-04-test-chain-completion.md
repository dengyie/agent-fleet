# Test Chain Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 补齐 PR #6 验收入口到真实 HTTP 节点、持久化恢复及质量报告的测试证据，并提供可复现的链路文档。

**Architecture:** 复用真实账号、SQLite、调度器和严格模型 HTTP fixture。新增 loopback NodeClient 轮询线程，保持产品 provider、Broker、HTTP transport、journal 和 executor 原样；故障只注入外部响应或响应发送边界。

**Tech Stack:** Python 3.10+、pytest、Flask/Werkzeug、SQLite、stdlib HTTP、Playwright 1.63.0。

## Global Constraints

- 仅在 `codex/pr6-review-fixes` 工作树开发，不改主工作树。
- 不访问真实模型、邮箱或生产节点；明确 loopback 不能证明实际远程部署。
- 不削弱既有门禁；新必需 selector 加入 `docs/testing/journeys.json`。
- 不合并或部署；完成本地与 Linux CI 验证后更新 PR #6。

---

### Task 1: 验收至节点的真实 HTTP 链路

**Files:** `tests/support/full_flow.py`、`tests/support/strict_provider.py`、新建 `tests/support/loopback_node.py`、`tests/test_acceptance_flow.py`。

**Interfaces:** `FullFlowHub(..., remote=False)` 开启临时 Hub 的远程执行 gate；`StrictProvider(forced_tool=None)` 独立校验请求后可返回越权工具；`LoopbackNode(hub)` 使用 `NodeClient`、`Transport(mode='direct', attempts=1)`、`NodeJournal` 和 `DirectoryBackend`。

- [x] 新增本地/远程成功用例，断言 CLI 报告、完整事件序列、节点 journal、实际列表内容、零文件变更、重复命令零重执行。
- [x] 参数化 write/artifact/exec/read 的恶意 wire 响应，断言只声明 list、单次模型请求、零派发、零产物、原文件字节不变。
- [x] 运行 `PYTHONPATH=. .venv/bin/python -m pytest tests/test_acceptance_flow.py -q`，任何产品失败先保留失败输出再定位最小修复。

### Task 2: 入口权限、重启与不确定提交恢复

**Files:** `tests/test_acceptance_flow.py`。

**Interfaces:** 真实账号 Cookie 调用 `/acceptance-turns`；通过 Flask `after_request` 阻塞已持久化提交的响应；通过 GET 对话内的 `client_token` 找回唯一 Run。

- [x] 覆盖匿名、伪造 Origin、跨 owner、撤销会话；拒绝后数据库无新消息/Run，模型无请求。扩权 override 被忽略，持久化策略和新 worker 声明仍只允许 list。
- [x] 覆盖普通/验收幂等键双向冲突及同策略重试。
- [x] 排队后重建 Hub，验证冻结策略由新 worker 执行、最终回复恢复且无重放。
- [x] 已提交但响应未到即截止，验证 `unconfirmed`、定位 token、唯一 Run 及重启恢复；旧服务 404 不回退普通入口。
- [x] 运行新增文件与既有验收、截止时间、worker、节点、门禁测试。

### Task 3: 可执行矩阵与文档

**Files:** `docs/testing/journeys.json`、`docs/testing/README.md`、新建 `docs/testing/test-chains.md`、`docs/testing/release-acceptance.md`、`README.md`。

- [x] 按职责将新 selector 加入 AUTH-04、ASSIST-02、NODE-01、RELEASE-01；保持整个参数集合为必需。
- [x] 文档列出链路阶段、对应测试、关键断言、替身边界、五项审查回归和按层调试命令。
- [x] 提供 JUnit、revision、dirty、矩阵摘要、参数数量、截图和外部 `not_run` 的证据核验命令；注明 Linux CI 与本机跳过项的区别。
- [x] 执行 README 的完整门禁命令，审查 diff 与报告。提交后以相同 tree 更新 PR，Linux CI 的结果记录在 PR 当前检查中。

## 执行证据

- 新增 21 个用例，相关回归集 117 passed。
- 在独立测试进程将验收入口降为普通权限时，新增本地贯穿用例失败；恢复正常进程后回归集通过，源码未作临时改动。
- 文档中的测试函数和相对链接已检查可解析；矩阵为 27 条流程、103 个唯一必需 selector。
- 本地完整门禁：2286 passed、2 skipped（macOS 无 `/proc`）、154 subtests、27/27 journeys，12 张浏览器截图。
- 提交后的 Linux CI 结果以 PR #6 当前检查与 artifact 为准，不将历史运行编号固定为持续有效的凭据。
