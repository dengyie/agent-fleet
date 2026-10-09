# 当前规格审计矩阵

审计维护日期：2026-10-10（初始完整矩阵证据记录于 2026-10-08）
初始审计基线：本地 `main` 的开发集成分支 `codex/development-completion-todo`；当时 HEAD 为 `d715e743cf176be3af52f40635703ea58270a555`，验证代码提交为 `88b12483691bc41c630df68d0bdce00430d87572`。该基线之后的复验按下方各日期 checkpoint 记录。

2026-10-09 续审在同一隔离分支 HEAD `540c0f46c1591c1a621fdfc9293fe5c573f7e7bc` 上增加 overlay 回滚错误状态诊断与回归，并完成隔离 Linux 安装器故障注入；该工作树的这些实现和审计更新尚未提交。

本轮开发分支先 rebase 到本地 `main` 基线 `676672a`，保留 main 上已有的 29 条 journey 和审计 selector，再完成干净提交态验证。主工作树仍在受保护的 `feat/t3.7-offline-browser-boundary`；只进行本地集成，没有远端推送或生产部署。

## 证据规则

当前平台文档、已批准的 specification 和 `docs/testing/` 契约优先于历史计划中的未勾选复选框。历史计划如果明确标记为 `historical / implemented`，不重新解释为未完成任务。测试矩阵的绿色结果只证明 disposable/local boundary；真实节点、真实邮箱、真实模型和部署必须有各自的外部证据。

本次本地证据：

- `c89c444760c4cd9d264b1741ebf96f6c9d8bb734` 的 Python 3.10.21 clean gate 为
  `3063 passed`、`2 skipped`、`156 subtests`、`29/29 journeys`、404 个 selector
  定义及 901/901 个实例；JUnit 3221 cases、0 failures、0 errors、2 skips。报告在
  `/tmp/agent-fleet-final-c89c444/`，HEAD/clean/matrix SHA-256 均匹配。validator
  按契约以 `junit_incomplete` 拒绝 Darwin 的 `/proc` skip。schedule/memory 输入
  validation 聚焦回归 38 passed；运行时代码中 `_finite()` 仅接受精确 `int`/`float`
  （排除 bool），既有回归拒绝 `interval_s="10"` 和 `next_run_at="100"`，独立审查
  提出的该项无需再改代码。Linux CI 和四项 live external checks 仍无本提交证据。

- 修复 `account_assistant_browser.cjs` 的认证后 SPA 等待竞态后，提交
  `da4039575b53503cd53426549449cecd4b53ba71` 的 clean gate 为 `3063 passed`、
  `2 skipped`、`156 subtests`、`29/29 journeys`、404 个 selector 定义和 901/901
  个实例；JUnit 3221 cases、0 failures、0 errors、2 skips。报告在
  `/tmp/agent-fleet-final-da40395/`，revision/clean/matrix digest 均匹配。
  validator 按契约返回 `junit_incomplete`，原因是 Darwin 的两个 `/proc` skip；
  release/auto-deploy 回归 73 tests + 2 subtests，compileall、fixture JS、打包
  脚本语法和 diff 检查均通过。backend 归档 571 成员无状态/凭据/数据库/frontend
  并带对应 `RELEASE_ORIGIN`；frontend manifest 与 42 文件一致、无禁止路径；
  包输出在 `/tmp/agent-fleet-package-da40395/`。

- 提交 `861af433d3a24d4ef280b437e16cdaca295ed754` 的完整门禁在干净代码树上
  运行，门禁期间只新增了审计文档提交；应用代码和 journey matrix 未改变。结果为
  `3063 passed`、`2 skipped`、`156 subtests`、`29/29 journeys`，404 个 selector
  定义及 901/901 个参数实例。JUnit 有 3065 cases、0 failures、0 errors、2 skips；
  17 张 PNG 签名截图。证据位于
  `/tmp/agent-fleet-all-todo-20261007/head69/{journeys.json,results.xml,browser/}`。
  报告 revision 为该提交、`working_tree_dirty=false`、`ci_status=passed`，matrix
  SHA-256 `2a9f73a02e1774b10c9d5803ace9d072f74f77ee58006a964cf0b91eae65923b` 与
  当时及当前矩阵一致；新增 FILES-01 artifact selector 为 `1/1`。release validator
  按契约以 `junit_incomplete` 拒绝 Darwin 两项 `/proc` skip。Python 3.10 compileall、
  改动 fixture 的 Node syntax、whitespace check 与打包/自动部署回归（73 tests、
  2 subtests）通过；四项 live external checks 均为 `not_run`。Linux CI 尚无本提交证据。

- 对提交 `861af433d3a24d4ef280b437e16cdaca295ed754` 另外按 release layout
  契约重新构建并检查 backend/frontend 包：后端归档 571 个成员，无运行状态、凭据、
  数据库或 frontend 路径，含相同 revision 的 `RELEASE_ORIGIN`；前端 manifest 和
  42 个输出文件逐项相符，无禁止路径。文件在 `/tmp/agent-fleet-package-861af/`。

- 本轮提交 `ea186da` 的干净工作树完成完整浏览器 journey 门禁；证据位于 `/tmp/agent-fleet-release-evidence-clean/results.xml`、`journeys.json` 和 `browser/`。结果为 `3063 passed`、`2 skipped`、`156 subtests`、`29/29 journeys`、`403` 个 selector 定义及 `900/900` 个参数化 selector 实例。报告记录 revision `ea186dacc4886101d588b1aa67832f33795358e3`、`working_tree_dirty=false`、`ci_status=passed`，矩阵 SHA-256 为 `e3c88626e08e026829762fb8b830de1787fcb76912d4a2babd66e465c8f09650`，与当前 `docs/testing/journeys.json` 一致；JUnit 有 3065 个 testcase、0 failures、0 errors、2 skips，外部检查均为 `not_run`。
- `tools.testing.release_evidence` 对同一报告和实际 checkout 的独立核验按预期返回 `junit_incomplete`，因为 Darwin 的两项 `/proc` skip 不满足发布证据的零 skip 要求；没有把本机结果当成 Linux 发布凭据。发布布局/自动部署回归为 `73 passed, 2 subtests passed`，Python 3.10 `compileall`、fixture JavaScript syntax 与 `git diff --check` 通过。backend/frontend 包分别在 `/tmp/agent-fleet-release-evidence-clean/backend.tgz` 和 `frontend/` 生成并通过 manifest/归档检查。

- 本轮在提交 `640e939` 的干净工作树上运行完整浏览器 journey 门禁；证据位于 `/tmp/agent-fleet-current-goal-20261007/committed-rerun/results.xml`、`journeys.json` 和 `browser/`。结果为 `3027 passed`、`2 skipped`、`156 subtests`、`29/29 journeys`、`393` 个 selector 定义及 `865/865` 个参数化 selector 实例。
- 机器报告记录 revision `640e939efd102feb88d9e6fbdcfa92f40437eff3`、`working_tree_dirty=false`、`ci_status=passed`。矩阵 SHA-256 为 `d45464805d50dffea1ec39fc5faeaef07b2c7708a7f90e4457b841eb00698795`，与当前 `docs/testing/journeys.json` 一致。JUnit 有 3185 个 cases、0 failures、0 errors、2 skips；外部检查均为 `not_run`。
- Python 3.10 `compileall -q hub tools tests`、`git diff --check` 通过；`tests/test_release_layout.py tests/test_auto_deploy.py` 为 `73 passed, 2 subtests passed`。
- 两个 skip 是 Darwin 无 `/proc` 的真实进程暂停/恢复测试；它们没有被替换成通过。
- 最终开发提交 `7797d5475a95c00d55027fcbf17ffa236838bf6f` 在 Python 3.14.7 和 Python 3.10.21 上分别通过完整浏览器 journey 门禁：每次 `3063 passed`、`2 skipped`、`156 subtests`、`29/29 journeys`；报告 revision、clean 状态和矩阵摘要均与当前提交一致，403 selector 全通过。发布校验器按要求以 `junit_incomplete` 拒绝 Darwin 报告，因为仍有两个 `/proc` skip。包布局/自动部署回归 `73 passed, 2 subtests passed`；Python 3.10 compileall、Node fixture syntax 和 diff 检查通过。Linux CI 尚未验证该开发提交，远端成功 main run `37503412061` 验证的是 `f6c35f9`。
- AL 修复在工作树 `4005e69` 上重新运行完整门禁；证据位于 `/tmp/agent-fleet-current-goal-20261007/results-browser-policy-followup.xml`、`journeys-browser-policy-followup.json` 和 `browser-policy-followup/`。结果为 `3029 passed`、`2 skipped`、`156 subtests`、`29/29 journeys`；JUnit 为 3187 cases、0 failures、0 errors、2 skips。报告记录 `working_tree_dirty=true`、`ci_status=passed`，矩阵 SHA-256 为 `9b000a877de1b92c9904739a9fa75d764718c3b9c4e8cff2a12372164e4406bb`，四项 external checks 仍为 `not_run`。
- 提交态 `51aabe3c667d7f83b45a428ed259e8c028a15f1c` 重新运行同一完整门禁；证据位于 `/tmp/agent-fleet-current-goal-20261007/results-browser-policy-committed.xml`、`journeys-browser-policy-committed.json` 和 `browser-policy-committed/`。结果为 `3029 passed`、`2 skipped`、`156 subtests`、`29/29 journeys`、`395/395` selector instances；JUnit 为 3187 cases、0 failures、0 errors、2 skips。报告记录 `working_tree_dirty=false`、`ci_status=passed`，矩阵 SHA-256 为 `9b000a877de1b92c9904739a9fa75d764718c3b9c4e8cff2a12372164e4406bb`，四项 external checks 仍为 `not_run`。
- Rebase 后代码提交 `88b12483691bc41c630df68d0bdce00430d87572` 在干净工作树通过完整 Chrome journey 门禁：`3063 passed`、`2 skipped`、`156 subtests`、`29/29 journeys`、404 selector definitions 和 `901/901` 参数化 selector instances。JUnit 有 3065 cases、0 failures、0 errors、2 skips；报告记录 `working_tree_dirty=false`、`ci_status=passed`，矩阵 SHA-256 `2a9f73a02e1774b10c9d5803ace9d072f74f77ee58006a964cf0b91eae65923b` 与当前矩阵相同。证据位于 `/tmp/agent-fleet-main-rebase-final/{journeys.json,results.xml,browser/}`。
- 同一代码提交的 Python 3.10 compileall、fixture JavaScript syntax、`54` 个 journey/release-evidence 测试及发布布局/自动部署回归（`73 passed, 2 subtests passed`）通过。release validator 按契约返回 `junit_incomplete`，因为 Darwin 缺少的两项 `/proc` 测试仍是 skip；MODEL-LIVE、MAIL-LIVE、NODE-LIVE、DEPLOY-LIVE 均为 `not_run`，Linux CI 尚未验证该 revision。

门禁命令按 `docs/testing/README.md` 执行：设置 Playwright 模块、Chrome channel、截图目录与 Python 环境后，运行 `python -m pytest tests -q --tb=short -rs -p no:cacheprovider --require-journeys --journey-report=... --junitxml=...`。

## 规格与证据

| 权威来源 | 当前代码与边界 | 证据 | 状态 | 剩余条件 |
|---|---|---|---|---|
| `docs/platform/agent-fleet-self-hosted-platform.md` T1.5/T1.8 | Artifact 列表、预览、owner/workspace scope、完整性校验和 DOM 安全渲染 | `ASSIST-01`、`FILES-01`、artifact/release 测试 | verified-local | 真实 provider/VPS 不在本地门禁范围 |
| 平台文档 T1.6/T1.7 | legacy task bridge、bounded inbox、token/idempotency、Run 恢复和权限隔离 | `ASSIST-01`、`RUN-01`、platform conversation/bridge 测试 | verified-local | 生产节点与外部 provider 未验收 |
| 平台文档 M1.6-M1.11 | provider contract、Run-to-Node、unknown reconciliation、post-check、canary、固定服务日志 | `MODEL-01`、`MODEL-02`、`NODE-01`、provider/remote delivery 测试 | verified-local | `MODEL-LIVE`、`NODE-LIVE`、真实节点和生产网络未运行 |
| 平台文档 M2.1-M2.14 | service catalog、bounded collectors、Komari schema、HTTP probe、version fencing、backup/restore、monitoring read UI | `MONITOR-01`、`BACKUP-01`、monitoring/Komari/probe/backup 测试 | verified-local | Komari fork/export、test-node pilot、off-host durability、key escrow、live rollback 未证明 |
| 平台文档 M3/T3.4/T3.5 | approval grant、service-version fence、fixed actions、execution-window lease、monitoring action UI | `WINDOW-01`、`MONITOR-01`、execution-window/action 测试 | verified-local-default-off | 真实 PTY、浏览器附件、VPS service mutation 和 operator rollout 未运行 |
| 平台文档 M4.1-M4.4 | durable schedules、MemoryItems、revision fence、explicit picker、context payload bounds | `SCHEDULE-01`、`MEMORY-01`、conversation/frontend 测试 | verified-local-default-off | automatic memory extraction/injection、生产 schedule/memory gate 和 notification policy 未批准 |
| `2026-08-19-v4-optimization-design.md` | observation、task/runner、session/adoption、bounded control plane | `AUTH-*`、`RUN-*`、`SESSION-01`、`ADOPT-01`、`SSE-01` | verified-local | 历史文档中已明确延期的 WebSocket、密钥下发、Hermes spawn、exact production upgrade 仍按 HANDOFF 保持延期 |
| `2026-08-21-frontend-backend-separation-design.md` | tracked static frontend、单一 client/SSE boundary、release layout、same-origin routing | `UI-01`、`RELEASE-01`、frontend separation/release tests | verified-local | 真实独立发布、Nginx/Access 切换和生产回滚需 `DEPLOY-LIVE` |
| `2026-08-26-agent-session-supervision-design.md` | Supervisor、Session Bridge、spool、sequence/cursor/dedupe、固定控制动作和 native resume | `SESSION-01`、session chaos/spool/supervisor/transcript 测试 | verified-local-contract | 真实安装的 agent、跨进程协调、source capability probe 和 exact source upgrade 属 `NODE-LIVE`/条件延期 |
| `2026-08-28-agent-instance-discovery-adoption-design.md` | discovery metadata、显式 adoption、pid/start-time/exe fence、revoke/detach 不发信号 | `ADOPT-01`、adoption/control guard、discovery/supervisor 测试 | verified-local-contract | 真实 probe、真实进程 attach、真实跨进程 control 尚无节点证据 |
| `2026-09-07-thin-client-remote-control-design.md` | 薄客户端路由、五个固定动作、native resume identity fence、profile 本地切换 | `SESSION-01`、`UI-01`、frontend control contracts | verified-local-default-off | LIVE seat/control 命令和 exact capture source upgrade 必须由实际 operator/node 证据确认 |
| `2026-09-09-v4-initial-goal-completion-design.md` | patch、test summary、pause/continue、confirm gate、credential rotation SOP、backup drill | `ACTION-01`、`FILES-01`、`BACKUP-01`、release/auto-deploy tests | verified-local | LIVE overlay、真实 runner 轮换、HK backup/rollback 和 Phase 4/5 实发未执行 |
| `2026-10-04-t3.6-browser-data-plane.md` | default-off browser session、bounded result/artifact ticket、frame event、scope/lease fencing | `BROWSER-01`、`BROWSER-02`、`WINDOW-01`、browser backend/Node HTTP 测试 | verified-offline-contract | 没有生产 Chromium/Playwright worker 或 Browserbase adapter |
| `2026-10-04-t3.7-browser-navigation-network-boundary.md` | exact-origin policy、DNS/IP validation、redirect/TLS/pinned transport seams、bounded budgets | `BROWSER-01` 及 browser transport tests | partial-offline; runtime enforcement blocked | 当前实现覆盖获批的离线策略/固定传输边界；真实浏览器原生拦截、per-Node CONNECT adapter 与 OS/container socket denial 尚未实现/证明，需 Linux 隔离运行时与单独运行时验收 |
| `2026-10-05-browser-submit-approval-contract.md` | same Run/session/selector single-use approval、atomic command admission、revoke/expiry/idempotency | `BROWSER-01`、`NODE-01`、submit approval/E2E tests | verified-offline-contract | 所有 browser gates 仍 default-off；真实浏览器生命周期和真实节点验收未做 |
| `docs/testing/browser-notification-proposal.md` | 记录浏览器观察/接管/凭据/Browserbase/通知的待决策边界 | proposal 本身，不作为实现证据 | deferred-with-condition | 提案尚未获批准；不能自行推断触发器、渠道、收件人、保留期或凭据范围 |
| `docs/testing/release-acceptance.md` | 定义模型、邮件、节点、部署、备份/回滚的真实验收命令和证据 | local journey gate 只能证明 disposable boundary | blocked-by-environment | `MODEL-LIVE`、`MAIL-LIVE`、`NODE-LIVE`、`DEPLOY-LIVE` 均未运行；缺少真实环境、凭据和 operator approval |

## 2026-10-08 browser session reconciliation continuation

The completion branch now includes BrowserRepository.reconcile_stale_sessions() and a stoppable entrypoint-owned worker. It uses the existing owner/node heartbeat relation, strict last_seen_at < now - 300 freshness, a 128-session batch, and one SQLite BEGIN IMMEDIATE transaction. It changes only open/closing sessions for enabled nodes with an observed heartbeat; unknown/missing liveness stays untouched. Browser, remote-execution and command-signature gates all have to be enabled before bootstrap starts the worker. This records remote state as unknown, not confirmed browser-process termination.

Focused verification passed 37 browser reconciliation and submit-approval tests; compileall and whitespace checks passed. The stale-session regression selectors are required by RELEASE-01. The subsequent clean committed gate is recorded below in `2026-10-08 committed stale-session gate`.

At the time of this audit entry, the canonical Obsidian T3.6d published capture GC and Node/bridge-owned process cleanup were outside this branch. The bounded ArtifactStore staging cleanup was integrated here with the repository's existing 256 KiB artifact contract, snapshot barrier and browser-gate lifecycle. The proposal's 1–2 MiB upload ceiling conflicts with the repository platform document's 256 KiB screenshot contract and is not adopted. The published capture GC is recorded in the later 2026-10-08 entry below; no production Chromium, Linux process isolation, or remote-node termination evidence is inferred from either local method.

The prior clean HEAD 681da3d95b18ce3394c32a76c041f28923013d99 ran the browser-enabled required gate with **3085 passed, 5 skipped, 160 subtests**. The report under /tmp/agent-fleet-pidfd-matrix-681da3d/ records 428 selector definitions and 925 instances; RELEASE-01 is incomplete only for the three Linux pidfd selectors, which Darwin cannot execute. Its matrix digest is 17b16778fc969bb758a8282a6db02ebe651c06de01b339ff343a8de952c5f980; release evidence validation rejected it as journey_incomplete. This is not Linux pidfd release evidence.

## 2026-10-08 committed stale-session gate

The clean implementation commit `aa55e3b6e1506b0a08d58fe0a4fbdc4d3d59d38f` ran the complete browser-enabled required gate after the stale-node reconciliation change. The run reported **3097 passed, 5 Darwin `/proc`/pidfd skips, 160 subtests and 28/29 journeys**; JUnit recorded 3262 testcases, zero failures and zero errors. The report is `/tmp/agent-fleet-stale-reconciliation-committed/{journeys.json,results.xml,browser/}` with `working_tree_dirty=false`, 434 selector definitions, 937 selector instances and 934 passed instances. Matrix SHA-256 is `4590667378c6816c22aea8c723523b9e59161731b7dcc92ea97c5206b537fdd7`.

`RELEASE-01` remains incomplete only because Darwin skipped three Linux pidfd selectors; `tools.testing.release_evidence` rejected this report as `journey_incomplete`. The local browser, application, and reconciliation selectors passed. This is clean local regression evidence, not Linux pidfd, real Chromium network-isolation, or production deployment evidence.

## 2026-10-08 ArtifactStore staging cleanup and latest gate

The completion branch now also removes only interrupted staging directories created by `ArtifactStore`. A cleanup pass considers entries older than one hour, skips symlinks and unrelated paths, processes at most 128 entries, and reuses the artifact snapshot barrier before deletion. The worker is started only when the browser, remote-execution and command-signature gates are all enabled; the default-off lifecycle remains unchanged. Two staging-cleanup selectors are now bound to RELEASE-01, bringing the current matrix to 436 selector definitions and 939 selector instances. The existing 256 KiB artifact contract remains authoritative; no larger capture ceiling was introduced.

Clean revision `219d84800673c79566a32ec7af9ace6d6017da83` was rerun through the browser-enabled required gate. It reported **3099 passed, 5 Darwin `/proc`/pidfd skips, 160 subtests and 28/29 journeys**, with 436 selector definitions and 936/939 selector instances passing. Evidence is under `/tmp/agent-fleet-staging-cleanup/{results.xml,journeys.json,browser/}`; the report binds a clean checkout and matrix SHA-256 `2ba4be47b623ff74e1fde0d6f8cfedc7b068b59241471d14bfcd373c5cc296b4`. `RELEASE-01` is the only incomplete journey because its three Linux pidfd selectors cannot execute on Darwin; the release evidence validator returned `journey_incomplete`. This is local evidence only: Linux zero-skip CI, real Chromium network isolation, approved browser/notification contracts and MODEL/MAIL/NODE/DEPLOY live checks remain open.

## 2026-10-08 published browser capture retention/GC

The current completion branch contains the T3.6d published capture retention/GC contract using the existing 256 KiB `ArtifactStore` and `browser_artifact_tickets` tables. Each bounded pass uses a 30-day default cutoff and at most 128 consumed PNG candidates. It scans `execution_window_events`, `run_events`, `platform_commands.result`, and direct `artifact_id` columns across SQLite with a global 4096-row/reference budget. Malformed JSON/reference data, schema drift, invalid manifest/scope/PNG/hash/directory structure, SQLite failure, and barrier/lock failure fail the entire pass closed.

The pass persists a `browser_capture_gc` tombstone under SQLite `BEGIN IMMEDIATE`, then holds `artifact_snapshot_barrier` while it revalidates and quarantines `.gc-<artifact_id>` before removing only `manifest.json` and `content`. Tombstones make an interruption after rename recoverable on the next cycle. Ticket history and lifetime quota are unchanged. Bootstrap registers the worker only when browser, remote-execution and command-signature gates are simultaneously enabled.

At this checkpoint, eleven GC selectors were bound to RELEASE-01. Focused GC, staging and reconciliation tests passed **25 tests**; the combined GC and browser node HTTP/API slice passed **51 tests**. Python 3.14 compileall, journey JSON validation and `git diff --check` passed. This is local contract evidence only: real Chromium, Linux egress isolation, owned Node/bridge loss cleanup, and MODEL/MAIL/NODE/DEPLOY live checks remain unverified.

The fresh browser-enabled required gate on the dirty completion worktree passed **3110 tests, 5 Darwin `/proc`/pidfd skips, 160 subtests and 28/29 journeys**. It collected **447 selector definitions and 950 parameter instances**, with **947/950 instances passing**; the three incomplete instances are the Linux pidfd selectors in RELEASE-01. Evidence is under `/tmp/agent-fleet-gc-gate-20261008-2/{results.xml,journeys.json,browser/}` and binds revision `62035048b8e0437c413c9084ff675a98d3978a1c`, `working_tree_dirty=true`, and matrix SHA-256 `dcc7c5c93fbc22d395483cc42110760819abf6552ddef5f7456527679909e66b`. The release evidence validator rejected this pre-commit report as `dirty_checkout`; a clean committed rerun is still required. External MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE remain `not_run`.

The clean commit `5e9640a242fa39bffe1604933d3f9f34f45a6a12` was rerun through the browser-enabled required gate and reported **3110 passed, 5 Darwin `/proc`/pidfd skips, 160 subtests and 28/29 journeys**. The matrix contained **447 selector definitions and 950 parameter instances**, with **947/950 instances passing**. Evidence is under `/tmp/agent-fleet-gc-committed-20261008/{results.xml,journeys.json,browser/}`; the report records `working_tree_dirty=false` and matrix SHA-256 `dcc7c5c93fbc22d395483cc42110760819abf6552ddef5f7456527679909e66b`. `tools.testing.release_evidence` returned `journey_incomplete` solely because Darwin skipped the three Linux pidfd selectors in RELEASE-01. External MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE remain `not_run`.

## 2026-10-08 published browser capture GC starvation repair

The first GC implementation selected the oldest 128 eligible tickets and only then skipped IDs found in the reference scan. A deterministic regression with 128 referenced tickets ahead of one unreferenced expired capture reproduced starvation: the pass returned zero deletions and the later capture remained on disk. The repair materializes the bounded reference set in a temporary primary-key table inside the existing `BEGIN IMMEDIATE` transaction, so SQL excludes referenced IDs before applying the 128-candidate limit. The scan/reference budget, fail-closed validation, tombstone protocol and snapshot barrier are unchanged. The starvation regression is the twelfth GC selector bound to RELEASE-01.

Focused GC verification passed **12 tests** after the repair. The dirty browser-enabled required-gate run completed with **3111 tests passing, 5 Darwin `/proc`/pidfd skips, 160 subtests and 28/29 journeys**; it collected **448 selector definitions and 951 parameter instances**, with **948 instances passing**. Evidence is under `/tmp/agent-fleet-gc-starvation-20261008/{results.xml,journeys.json,browser/}` and records the expected three Linux pidfd skips in RELEASE-01; the report is intentionally dirty-checkout evidence and is not release evidence. Python 3.10 compileall, journey JSON validation and `git diff --check` passed. A clean committed rerun is required after this change.

The clean commit `960f4b84c2893c99296a210276ae1d085d530090` was then rerun through the browser-enabled required gate. The run completed with **3111 tests passing, 5 Darwin `/proc`/pidfd skips, 160 subtests and 28/29 journeys** in 330.43 seconds; JUnit recorded **3276 cases, zero failures, zero errors and five expected skips**. The report is under `/tmp/agent-fleet-gc-starvation-committed-20261008/{results.xml,journeys.json,browser/}` and binds `working_tree_dirty=false`, matrix SHA-256 `27a63e2203cd368c39b7aa542ce811fc51ecd0d17d4c381032af5868fab54567`, **448 selector definitions and 951 parameter instances with 948 passing**; 17 PNG screenshots were emitted. The overall required journey gate remains incomplete: `tools.testing.release_evidence` rejected the report as `journey_incomplete` solely because Darwin skipped the three Linux pidfd selectors in RELEASE-01. Python 3.10 compileall and the matrix/diff checks passed. External MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE remain `not_run`.

## 2026-10-08 probe PID-file lifecycle race repair

The installer probe stop path no longer unlinks the advisory PID file after taking a process snapshot, avoiding the remaining check/unlink race; the owning loop removes its PID file in its EXIT trap. The start readiness poll no longer removes the file after a transient `/proc` identity miss; stale cleanup occurs once before launch and subsequent polls retry identity validation. Both regressions were observed failing against the old source and then passed in the focused deployment/guardian slice (**14 passed, 3 Darwin `/proc`/pidfd skips**); Bash syntax, Python 3.10 compileall and whitespace checks passed. The two selectors are included in RELEASE-01.

Clean commit `bc74e13fa187aea9a45d4bc44cc13762d44d6fec` passed the full local browser-enabled run with **3113 passed, 5 skipped, 160 subtests and 28/29 journeys**; 450 selectors/953 parameter instances were collected and 950 passed. JUnit contains 3118 cases, zero failures/errors and five Darwin `/proc` skips. Evidence: `/tmp/agent-fleet-pidfile-races-committed-20261008/`. The journey report has `ci_status=failed` solely because Darwin skips three RELEASE-01 Linux pidfd selectors; the release verifier returned `journey_incomplete` as designed.

The same committed tree was packed with `git archive` into `/tmp` on the isolated Linux test host. Under Python 3.10.12, Guardian pidfd tests passed **8/8**, deployment pidfd tests **9/9**, and Supervisor process tests **65/65**. These targeted Linux runs supplied process/pidfd evidence but not the full pytest/journey gate. The host's Docker CLI had no reachable daemon; user/network namespace direct host networking was denied, so full Chromium three-layer egress validation remains open. MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE are still `not_run`; observation/takeover/credential/Browserbase/notification parameters and metadata-retention scope remain unapproved or undefined.

After adding the Linux results to the audit, exact clean revision `bbad088ab65eba7c94d8f8c6ccf9cdb43694c891` passed the browser-enabled required-gate test execution with **3113 passed, 5 Darwin `/proc`/pidfd skips, 160 subtests and 28/29 journeys**. It collected 450 selectors and 953 parameter instances, 950 passing; JUnit has 3118 cases, zero failures/errors and five expected skips, plus 17 valid PNG screenshots. Evidence is `/tmp/agent-fleet-pidfile-races-final-20261008/`; matrix SHA-256 is `e01d9aa0ed918a51c1364d960ce15ee124044f3b4013e67a625ff13d203ad912`. The release evidence validator returned `journey_incomplete` solely because the three RELEASE-01 pidfd selectors require Linux. No runtime or journey-matrix files changed for this final gate.

The exact source warning is fixed: `/opt/homebrew/bin/python3.10 -Werror::DeprecationWarning -m py_compile tools/probe/discovery.py` passes, `tests/test_discovery.py` passes **11/11**, Python 3.10 compileall passes, and shell syntax/`git diff --check` pass. Commit `8d7d8572a0e9bc640f94e69a8038c7c0f9c0cda6` passed a fresh browser-enabled required gate: **3113 passed, 5 Darwin `/proc`/pidfd skips, 160 subtests and 28/29 journeys**. It collected 450 selectors and 953 instances (950 passed); JUnit has 3118 cases, zero failures/errors, five skips and 17 valid PNG screenshots. Evidence: `/tmp/agent-fleet-completion-final-20261008/`; matrix SHA-256 `e01d9aa0ed918a51c1364d960ce15ee124044f3b4013e67a625ff13d203ad912`. The release verifier returned `journey_incomplete` only because Darwin cannot run the three RELEASE-01 Linux pidfd selectors.

The exact clean evidence-record commit `cd6df75bb2fdf66a553ae54a3ade813bb5d98ae2` also passed a fresh browser-enabled required gate: **3113 passed, 5 Darwin `/proc`/pidfd skips, 160 subtests and 28/29 journeys**; 450 selectors and 953 instances (950 passed), 3118 JUnit cases, and 17 valid screenshots. Evidence: `/tmp/agent-fleet-completion-audit-final-20261008/`; matrix SHA-256 remains unchanged. The release validator returned `journey_incomplete` for the three Linux pidfd selectors unavailable on Darwin.

The current clean revision `d80c36e5996b6e309b875fe7c4bb05e67809ee28` was then verified with the browser-enabled required gate. The report `/tmp/agent-fleet-final-clean-20261008/` binds this exact revision, `working_tree_dirty=false`, matrix SHA-256 `e01d9aa0ed918a51c1364d960ce15ee124044f3b4013e67a625ff13d203ad912`, 450 selectors and 953 parameter instances (950 passed), **3113 passed, 5 Darwin skips, 160 subtests and 28/29 journeys**, and 17 PNG screenshots. JUnit records 3278 cases, zero failures/errors and five skips. `tools.testing.release_evidence` returns `journey_incomplete` for the three skipped Linux pidfd selectors in RELEASE-01. The same gate does not establish the full Linux journey gate, browser egress isolation, or external live acceptance; MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE remain `not_run`.

The exact clean documentation commit `c3dc06f741a82248f80628efc38da26ca802d0e2` was independently rerun with the same browser-enabled required command. Evidence at `/tmp/agent-fleet-c3dc06f/{journeys.json,results.xml,browser/}` binds the exact revision, `working_tree_dirty=false`, matrix SHA-256 `e01d9aa0ed918a51c1364d960ce15ee124044f3b4013e67a625ff13d203ad912`, 450 selectors, 953 instances (950 passed), **3113 passed, 5 Darwin skips, 160 subtests and 28/29 journeys**, 3278 JUnit cases with zero failures/errors, and 17 PNG screenshots. Python 3.10 strict compile/AST checks and `git diff HEAD^ HEAD --check` passed. The release validator returned `journey_incomplete` for the same three Linux pidfd selectors.

## 明确的开放项

以下不是隐藏的实现 TODO，而是需要外部条件或新批准规格的开放项：

1. 提供专用 Linux 测试节点和固定 Chromium/隔离运行时，完成 T3.6/T3.7 的 browser lifecycle、三层网络隔离、崩溃清理和不确定命令不重放验收；Darwin 的 `/proc` 两项跳过需由 Linux CI 补证。
2. 批准或修改 observation、Take Control/Return Control、SecretBroker、Browserbase 的产品参数和数据保留边界，再实现真实 driver 适配器。
3. 批准通知的触发事件、渠道、收件人、幂等、保留和外发授权，再实现事务 outbox 与投递回执；现有账号邮件不能代替业务通知规则。
4. 为 `MODEL-LIVE`、`MAIL-LIVE`、`NODE-LIVE`、`DEPLOY-LIVE` 提供各自环境、凭据和 operator approval，并留下脱敏验收证据；不能用 fixture 或本地测试替代。Komari/test-node pilot、off-host backup durability、key escrow 和生产恢复/回滚也需对应真实环境证据。
5. Exact capture 仍需 signed probe-side source/capability upgrade，并验证 AEAD、quota、retention、raw-read audit 的端到端闭环；现有 adoption API 的 `exact` 标签不代表 source 已升级。
6. Session supervision 文档写明 metadata retention 为 90 天，但没有指明适用的 session/cursor/dedupe/redacted-event/policy/audit 表、起算时间或删除关联规则。明确范围前不自动删除这些数据；该本地实现项保持待决策。

这些开放项保持 default-off/未执行状态，直到对应规格和环境证据出现。它们不会被本地绿测或历史计划复选框自动关闭。

## TODO 归类

`docs/HANDOFF.md` §六与各历史计划状态说明优先于旧计划中的未勾选 checkbox；旧 checkbox 不自动构成当前 TODO。对 `hub/`、`tools/`、`connectors/`、`deploy/` 和 `frontend/` 的 `TODO`、`FIXME`、`XXX` 搜索没有发现实现占位符。当前仍开放的代码/验收事项仅以上述批准规格与外部条件为准；`docs/superpowers/plans/2026-10-06-development-completion.md` Task 3/4 保持开放，直至这些要求获得批准并有对应证据，不能通过删 checkbox 或缩小目标关闭。

2026-10-08 continuation audit: the current completion branch already contains the bounded `browser.frame` ticket/event/read path, Assistant frame rendering, and transactional operator writer-lease admission checks. Fresh focused verification on this branch passed `96` execution-window, submit-approval, Node HTTP E2E, and frontend contract tests. The committed browser-foundation checkpoint `0a742838872067a614511209b60c559014d82d73` was independently checked in a clean detached worktree; its capture domain/repository/store/ingest, Chromium and lifecycle regression slice passed `99` tests. That checkpoint is from a parallel development line and is not merged into this branch; its passing tests do not upgrade this branch's runtime evidence.

No production-facing change is authorized by the remaining evidence: this host is Darwin and has no Docker, Podman, or bubblewrap executable, so Linux `/proc`, process-level browser egress isolation, and real Chromium-to-T3.7 runtime integration remain unverified. A separate disposable Chromium loopback fixture is now covered in BROWSER-01; it is test-only evidence and does not provide those runtime proofs. The proposal's observation/credential/Browserbase and notification values still lack owner approval. Exact capture remains a control-plane label until a signed probe-side source transition is specified and proven. `MODEL-LIVE`, `MAIL-LIVE`, `NODE-LIVE`, and `DEPLOY-LIVE` remain `not_run`; their environments and acceptance evidence are not present in this worktree. Task 3/4 and the continuing completion goal remain open.

## 2026-10-09 test-only Chromium fixture

Added an opt-in real Chromium test that starts two disposable loopback HTTP fixtures and a private Playwright process. Chromium loads only the registered literal-loopback origin through a fixed request interceptor; the test checks the computed CSS color, decoded image dimensions, omission of the input value from DOM text, and a black pixel at the editable input's center in the masked PNG. It also checks rejection of a second loopback origin and a redirect before either destination receives a request. The PNG is checked against the platform's existing 256 KiB limit. The helper is under `tests/fixtures/browser/` and is not imported by `NodeRuntime`, Hub configuration or manifests.

Fresh focused verification on this worktree passed **242 browser/Node/transport tests**, including the real Chromium fixture using Node v26.9.0, Playwright from the local Codex runtime, and installed Chrome. The final required gate then finished with **3121 passed, 5 Darwin `/proc`/pidfd skips, and 160 subtests**; BROWSER-01 passed, and the only incomplete journey was RELEASE-01 because its three Linux pidfd selectors cannot run on Darwin. JUnit contains 3126 cases, zero failures/errors and five skips; all 17 PNG screenshots have valid signatures. Evidence is `/tmp/agent-fleet-chromium-fixture-final2-20261009/{journeys.json,results.xml,browser/}`. The report binds revision `cf68f55d23c7be832d04aa022b51a49c69d98541`, `working_tree_dirty=true`, and matrix SHA-256 `d2c356d774dfcdf459aadb5141aa6f00ae3a05acc3367251171b447cdc8a2479`, matching `docs/testing/journeys.json`; both new BROWSER-01 selectors collected and passed 1/1. The release evidence validator rejected the report as `dirty_checkout`. Python 3.10.21 compile/strict compile, Node syntax, matrix JSON, and `git diff --check` passed. The fixture confirms browser behavior only while requests pass through its in-process interception callback; it does not prove that Chromium cannot open direct sockets or bypass the callback, and it is not T3.7 runtime acceptance. Linux zero-skip CI and external live checks remain open.

本轮在同一隔离分支上还修复了 `ASSIST-01` 的 SPA 初始导航时序缺口：首次完整门禁确实失败于 `full_flow_browser.cjs` 等待 `load`，而服务端已返回文档和全部静态资源；改用 `domcontentloaded` 并保留后续语义就绪断言后，四次 focused 重跑及完整工作树门禁均通过（`3027 passed`、`2 skipped`、`156 subtests`、`29/29 journeys`）。证据位于 `/tmp/agent-fleet-current-goal-20261007/full-rerun/`；该修复提交后的干净 revision 门禁仍需单独记录。这个本地测试修复不改变真实浏览器运行时、Linux 隔离或外部验收状态。

随后提交的 `640e939efd102feb88d9e6fbdcfa92f40437eff3` 已完成干净门禁：报告记录 `working_tree_dirty=false`、`ci_status=passed`，为 `3027 passed`、`2 skipped`、`156 subtests`、`29/29 journeys`，JUnit 为 3185 cases、0 failures、0 errors、2 skips；矩阵 SHA-256 仍为 `d45464805d50dffea1ec39fc5faeaef07b2c7708a7f90e4457b841eb00698795`。证据位于 `/tmp/agent-fleet-current-goal-20261007/committed-rerun/`。发布布局/自动部署回归为 `73 passed, 2 subtests passed`，Python 3.10 compileall 和 fixture JavaScript syntax 也通过。该证据只关闭本地 `ASSIST-01` 回归，不改变 Linux 运行时、产品决策或四项外部验收的状态。

## 2026-10-08 外部状态复核

- 当前本地 `main` 与 `codex/development-completion-todo` 指向 `d715e743cf176be3af52f40635703ea58270a555`；远端 `origin/main` 仍指向 `f6c35f9a5f8e5e6da9ad0abaf7c37ff257b6ea58`。本地开发提交尚无对应远端 CI 证据。
- GitHub Actions run `37503412061` 对远端 `main` 的 `f6c35f9` 显示 `test`、`package`、`deploy` 均成功；GitHub `production` deployment 状态为 `success`。这证明的是 `f6c35f9` 的既有部署，不是当前本地开发提交的 Linux 验证或外部功能验收。
- 当前账号可见的 GitHub `production` environment 配置了 `SSH_HOST`、`SSH_PORT`、`SSH_USER`、`SSH_PRIVATE_KEY`、`SSH_KNOWN_HOSTS` 五项部署 secret；只读取名称，没有读取 secret 值。没有发现可见的模型、邮箱或节点验收凭据/变量，因此 `MODEL-LIVE`、`MAIL-LIVE`、`NODE-LIVE` 仍无可执行凭据证据。
- 本地仍为 Darwin，未发现 Docker、Podman 或 bubblewrap；Linux `/proc` 两项无法在本机补证。CI workflow 使用 Ubuntu，但当前本地提交尚未推送，不能把旧 `main` run 代作本提交结果。
- 本轮在当前 `d715e743cf176be3af52f40635703ea58270a555` 工作树重跑完整 Chrome 门禁，结果 `3063 passed`、`2 skipped`、`156 subtests passed`、`29/29 journeys`，耗时 326.59 秒；两项 skip 均为 Darwin 无 `/proc` 的 supervisor 进程测试。报告 `/tmp/agent-fleet-current-goal-20261008/journeys.json` 绑定当前 revision、`working_tree_dirty=true`、矩阵 SHA-256 `2a9f73a02e1774b10c9d5803ace9d072f74f77ee58006a964cf0b91eae65923b`；四项 live check 均为 `not_run`。
- 同一工作树通过 Python 3.10 compileall、`54` 个 release-evidence/journey-gate 测试，以及发布布局/自动部署回归（`73 passed, 2 subtests passed`）。release validator 对本轮报告返回 `dirty_checkout`，因为审计文档有未提交更新；不将该 dirty 运行称为 release evidence。先前干净代码提交 `88b1248` 的零失败本地证据仍见本审计上文，但它的两个 Darwin skip 仍需 Linux CI 补证。

## 2026-10-08 Guardian PID fencing

Guardian 重启旧 Web 服务时，原实现先读取并校验 `/proc/<pid>`，再对数字 PID 调用 `kill`；进程在两步之间退出并被复用时，信号可能到达无关进程。新增 `deploy/hk-web-process-control.py`：用 Linux pidfd 绑定身份和信号目标，校验 UID、cwd、argv 后发 TERM，超时后对同一 pidfd 发 KILL；pidfd 不可用或身份读取失败时 fail closed。现有 `agent-fleet-web.pid` 仍保持数字 PID 格式。

红绿证据：新增回归先因 Guardian 含有 `kill "$old_pid"` 失败；修复后 Python 3.10 的 Guardian/控制器、发布布局测试通过 **50 tests, 2 subtests**。Shell `bash -n`、Python 3.10 编译和 whitespace 检查通过。该主机为 Darwin，pidfd 测试通过模拟竞态；Linux 容器内 TERM/KILL 实测、干净提交包启动和 Linux CI 仍未完成，不能据此关闭运行时或发布验收。

同一数字 PID 复用窗口也存在于自动部署器、容器安装器和 probe 重启路径。auto_deploy_container.py 现在捕获 pidfd 后再次比对 (pid, kind, starttime)，并通过同一 pidfd 发 TERM/KILL；容器安装器对旧 Hub、probe 和回滚中的候选 Hub 复用 pidfd helper。安装器在删除 probe PID 文件前复扫身份，回滚时先停候选 probe，并在候选 Hub 停止未确认时跳过恢复启动，避免错误 PID 或重复 listener。

红绿证据：Guardian 直接数字 PID 信号的原回归先失败后通过；本轮部署控制器失效 pidfd、pidfd 不可用 fail-closed 和安装器回滚/source/cwd/UID 合约测试后，Python 3.10 定向切片为 **57 passed, 2 subtests passed**。Shell bash -n、Python 3.10 编译和 git diff --check 通过。该主机为 Darwin，pidfd 行为由模拟覆盖；Linux TERM/KILL 实测、symlink/overlay 容器安装器运行、跟踪文件提交后的干净发布归档/启动和 Linux CI 仍未完成，不能据此关闭运行时或发布验收。

补充覆盖 Python Guardian 和 Guardian sleep 子进程后，同一切片为 **64 passed, 2 subtests passed**；Python 3.14 自动部署专用套件为 **38 passed**。Python Guardian 通过 release 内 helper 停止旧 Hub，helper 失败时 fail closed；sleep 子进程按父 PID 身份 fencing。所有被审查的部署/Guardian 路径不再对 Hub、probe 或 sleep 使用裸数字 PID TERM/KILL；新 Hub 存活判断使用 Popen.poll 与进程身份复核。Linux runtime 限制仍同上。package-release.sh 针对当前 HEAD 的归档未含尚未跟踪的 helper，因此该结果不作为新代码包验收证据。

完整 Chrome 门禁随后在当前工作树通过：**3079 passed、5 skipped、156 subtests passed、29/29 journeys**，耗时 324.23 秒。3 个新增 skip 是 Linux pidfd/`/proc` 子进程测试，另 2 个是既有 supervisor `/proc` 测试。报告绑定 HEAD `d715e743cf176be3af52f40635703ea58270a555`，记录 `working_tree_dirty=true`、矩阵 SHA-256 `2a9f73a02e1774b10c9d5803ace9d072f74f77ee58006a964cf0b91eae65923b`；`MODEL-LIVE`、`MAIL-LIVE`、`NODE-LIVE`、`DEPLOY-LIVE` 全为 `not_run`。证据位于 `/tmp/agent-fleet-todo-continuation-20261008/`。这只证明本地回归与 journey 覆盖，不能替代干净发布包、Linux runtime 或外部验收证据。

续审又发现并修复三个本地交付缺口：release-layout 测试要求归档包含 pidfd helper；安装器无 PID 文件时初始化 advisory PID 变量以兼容 `set -u`；shell Guardian 不再静默吞掉 sleep 子进程 helper 失败。修复后 Guardian/部署定向回归为 **65 passed、3 个 Linux-only skipped**，Python 编译、Shell 语法与 whitespace 检查通过。当前尚未提交，所以 `git archive HEAD` 仍不会含新 helper；提交态归档/启动验证尚待执行。

最终提交 `51dff02` 的完整 Chrome 门禁通过 **3079 passed、5 skipped、156 subtests、29/29 journeys**，耗时 323.76 秒；报告记录 `working_tree_dirty=false`、矩阵 SHA-256 `2a9f73a02e1774b10c9d5803ace9d072f74f77ee58006a964cf0b91eae65923b`，404 个 selector 定义全部通过。五个 skip 仍是 macOS 缺少 `/proc`/pidfd 的平台测试，因此 release validator 按契约返回 `junit_incomplete`，不能作为 Linux 发布证据。代码提交 `faab01f` 的 release archive 已实际包含 `deploy/hk-web-process-control.py`；解包后 `deploy`/`hub`/`tools` compileall 和 `hub.web.make_app()` 导入检查通过。证据位于 `/tmp/agent-fleet-todo-continuation-20261008/`。四项 live check 仍为 `not_run`。

后续审计发现安装器在旧 probe 退出与新 probe 写 PID 文件的竞态下，停止流程可能删除新 PID 文件，启动轮询也可能在新进程身份暂未出现时删除该文件。失败回归先复现启动轮询的删除点；现已将清理限制为 launch 前清理旧文件，停止流程不再删除 PID 文件。原失败用例通过，Guardian/部署/release-layout 定向切片为 **101 passed、3 个 Linux-only skipped、2 subtests**；Python 编译、Shell 语法与 whitespace 检查通过。

提交 `a7c936f` 的完整 Chrome 门禁随后通过 **3080 passed、5 skipped、156 subtests、29/29 journeys**，报告记录 `working_tree_dirty=false`、矩阵 SHA-256 `2a9f73a02e1774b10c9d5803ace9d072f74f77ee58006a964cf0b91eae65923b`，404 个 selector 定义全部通过。5 个 skip 仍为 macOS 缺少 `/proc`/pidfd 的平台测试；release validator 因 JUnit skip 返回 `junit_incomplete`，不能作为 Linux 发布证据。a7c936f 的归档包含 pidfd helper，归档解包 compileall 与 `hub.web.make_app()` 导入检查通过；四项 live check 仍为 `not_run`。

复审确认 PID 文件修复还需消除两个 TOCTOU 窗口：停止阶段在重读后删除文件仍可能与新实例写入竞争；启动确认轮询也曾在身份暂未匹配时删除刚写入的新 PID 文件。现在停止函数完全不删除 advisory 文件，启动仅在启动命令之前清理旧文件，身份轮询失败时继续有界等待。失败回归先复现轮询内删除点，再随修复通过；定向 Guardian/部署/release-layout 套件 **101 passed、3 个 Linux-only skipped、2 subtests**，Python 编译、Shell 语法和 whitespace 检查通过。完整 journey 与提交包门禁仍需在后续提交上刷新。

提交 `c5081d1` 后的完整 Chrome 门禁通过 **3080 passed、5 skipped、156 subtests、29/29 journeys**，耗时 364.43 秒；报告绑定该提交，`working_tree_dirty=false`、矩阵 SHA-256 `2a9f73a02e1774b10c9d5803ace9d072f74f77ee58006a964cf0b91eae65923b`，404 个 selector 全部通过。5 个 skip 是 macOS 缺少 `/proc`/pidfd 的 Linux-only 测试，四项 live checks 均 `not_run`。`package-release.sh` 产物包含 pidfd helper；解包后 compileall 和 `hub.web.make_app()` 导入通过。release validator 因 skip 返回 `junit_incomplete`，不能作为 Linux 发布证据。证据位于 `/tmp/agent-fleet-todo-continuation-20261008/`。

## 2026-10-08 当前提交复验

完整门禁执行时，隔离分支 `codex/development-completion-todo` 的 HEAD `14073662eaae21a4d1591d6af25ad2088b32072f` 工作树干净。本轮使用 Python 3.10 和 Chrome 重跑完整 gate：**3080 passed、5 skipped、156 subtests、29/29 journeys**，耗时 323.33 秒。Journey 报告绑定该 revision，`working_tree_dirty=false`、`ci_status=passed`；矩阵 SHA-256 `2a9f73a02e1774b10c9d5803ace9d072f74f77ee58006a964cf0b91eae65923b` 与当时 `docs/testing/journeys.json` 一致，404 个 selector 定义通过。5 个 skip 为 macOS 无法执行的 Linux `/proc`/pidfd 用例；`MODEL-LIVE`、`MAIL-LIVE`、`NODE-LIVE`、`DEPLOY-LIVE` 仍为 `not_run`。证据在 `/tmp/agent-fleet-current-1407366/`。之后的 `f3c92a5` 和 `8701a1f` 仅追加本轮开发计划和审计记录，没有变更代码或 journey matrix。

同一提交的 Guardian/deployment/release-layout 定向切片为 **101 passed、3 skipped、2 subtests**；Python 3.10 compileall、相关 Shell `bash -n` 和 `git diff --check` 通过。Release archive 含 `deploy/hk-web-process-control.py` 与本地 provenance；解包 compileall 通过，使用仅供本地 smoke 的临时 ingest token 后 `hub.web.make_app()` 导入通过。Release validator 对 macOS JUnit 返回 `junit_incomplete`，这是预期的 Linux 发布门禁拒绝，不能当作 Linux 通过证据。

当前剩余项仍未关闭：实现代码与 journey matrix 已通过公开候选上的 Ubuntu clean CI，Linux pidfd 与 `/proc` 用例无 skip；CI 后只追加审计文档的开发分支提交尚无独立 run。Linux 安装回滚和三层浏览器网络隔离仍需专用运行时实测。浏览器观察/接管/凭据/Browserbase 与通知参数仍需 owner 确认；四项 live acceptance、Komari/test-node pilot、异地备份、key escrow、生产恢复/回滚和 signed probe-side exact-capture upgrade 仍需各自真实环境或批准契约。

尝试将功能分支推到公开 `origin` 以触发 Ubuntu 测试时，远端在更新 ref 前拒绝了包含 178 个提交的 push，提示该分支像是私有历史并要求从公开 orphan snapshot 的克隆发布；没有提交被推送。当前主检出仍干净且未变。本机没有 Docker、Podman、Lima、Colima、Multipass、QEMU 或其他可用 Linux VM。不得通过重写 orphan 历史规避远端保护；获取此 revision 的 Linux CI 证据需要先确定公开快照发布流程及允许公开的变更范围。

## 公开基线快照审查（未发布）

为准备符合远端保护要求的审阅候选，在 Codex 隔离工作树 `/Users/mango/.codex/worktrees/public-snapshot-review/agent-fleet` 从 `origin/main` 建立预览；把本地 `main` 相对公开基线的 tracked diff 应用为一个提交 `03569ba61a25d124ac994ca7321ddc9afef542ac`，其唯一父提交为公开 `f6c35f9a5f8e5e6da9ad0abaf7c37ff257b6ea58`。该候选仅存在本地，没有推送。

公开快照路径审查未发现 credentials/state/var/`.env`/hosts/private-key 路径；高熵凭据模式扫描命中 `tests/test_task_patch_bounds.py` 和 `tests/test_task_files.py` 中用于脱敏回归的相同合成 `ghp_...` 字符串。完整 Python 3.10 + Chrome 门禁为 **3080 passed、5 macOS Linux-only skips、156 subtests、29/29 journeys**，耗时 336.09 秒；唯一警告是既有 `tools/probe/discovery.py` docstring 的无效转义弃用警告。证据在 `/tmp/agent-fleet-public-snapshot-committed/`。本机候选的 release validator 返回 `junit_incomplete`，原因是 5 个 macOS 平台 skip；包归档包含 pidfd helper，解包 compileall 与使用临时测试 token 创建 Hub app 通过。

## 2026-10-08 Ubuntu CI 候选验证

公开基线快照提交 `a0b5372425a48a340f532203d044fc135d8a5ef1`（分支 `codex/public-ci-candidate`，父提交为公开 `f6c35f9`）已在 GitHub Actions Ubuntu 24.04 / Python 3.10 上通过 CI run [37695537251](https://github.com/dengyie/agent-fleet/actions/runs/37695537251)。完整套件结果为 **3085 passed、0 skipped、156 subtests passed**，耗时 359.96 秒；无失败，仅有既有 `tools/probe/discovery.py` 无效转义弃用警告。Release evidence verifier 成功核验 29 个 journeys、404 个 selector、3085 个 JUnit cases，外部 checks 仍为 `not_run`；测试证据 artifact ID 为 `11514843558`。

此 run 只证明该公开基线候选的 test 与 release-evidence 检查。Workflow 的 `package` 和 `deploy` jobs 均因条件未满足而 skipped；它不是最终开发分支的逐提交 CI，也不证明生产打包、部署、MODEL-LIVE、MAIL-LIVE、NODE-LIVE 或 DEPLOY-LIVE。合并到开发分支后仍需对最终提交重新运行 CI。

## 2026-10-08 本地 main 集成复验

`codex/development-completion-todo` 以本地 `main` 为祖先；核实远端 `origin/main` 仍为公开部署提交 `f6c35f9` 后，在隔离集成工作树将本地 `main` fast-forward 到 `42485d639e79a7a04a5d54122953c68a72bbaa52`。受保护主工作树保持在 `feat/t3.7-offline-browser-boundary` 且干净；没有 push 或部署。开发分支代码改动为 Python 3.10 docstring 转义修复，`tests/test_discovery.py` 为 11 passed。

在合并后本地 `main` 的 clean revision `42485d6` 重新运行完整 Python 3.10 + Chrome 门禁，结果 **3080 passed、5 skipped、156 subtests、29/29 journeys**，耗时 335.67 秒。五个 skip 是 Darwin 无法执行的 Linux pidfd/`/proc` 用例。报告 `/tmp/agent-fleet-main-42485-V2JRgh/journeys.json` 绑定该 revision、`working_tree_dirty=false`、`ci_status=passed`，矩阵摘要为 `2a9f73a02e1774b10c9d5803ace9d072f74f77ee58006a964cf0b91eae65923b`；404 个 selector、901/901 参数实例通过。JUnit 在 macOS release verifier 下按要求以 `junit_incomplete` 拒绝，不能替代 Linux 零跳过报告。

同一 revision 的 Guardian/deployment/release-layout 定向切片为 **159 passed、5 Linux-only skips、4 subtests**；Python 3.10 compileall、Shell `bash -n` 和 whitespace 检查通过。后端包 `/tmp/agent-fleet-package-42485-kF1w3W/backend.tgz` 解包后 compileall 通过，含 `deploy/hk-web-process-control.py`，临时测试 token 下 Hub app 构造成功（68 routes）；前端 release manifest 精确匹配 42 个文件。包无 credentials、state、var、`.env` 或 `hosts.yaml` 路径。

对规范入口指向的隔离 pxed 测试机只做只读探测：Linux x86_64、Python 3.10.12、Chromium 可运行，user+network namespace 可创建，但普通 network namespace 返回 `Operation not permitted`；Docker CLI 存在但 daemon socket 不可连接。user+network namespace 内可以启用 loopback 和创建 veth；在无路由 namespace 中对公网地址 TCP connect 返回 `ENETUNREACH`。这只证明空 namespace 没有直连出口，不证明代理转发、Chromium 原生拦截或三层策略闭环。项目 `.venv` 与系统解释器没有 pytest；另有旧验收 venv 提供 pytest 9.1.1、Flask 和 cryptography，但缺 jsonschema 与 Playwright。Guardian pidfd 单测可用标准库直接运行；要对当前提交执行，需先把当前包放入远端 `/tmp` 临时目录。本轮未安装依赖、创建副本、启动/修改服务或向 Linux 宿主发送信号，等待对临时副本操作的确认。故 pidfd 真实 TERM/KILL、安装回滚及浏览器三层隔离仍未验收。

`MODEL-LIVE`、`MAIL-LIVE`、`NODE-LIVE`、`DEPLOY-LIVE`、Komari/test-node pilot、off-host backup、key escrow、生产 restore/rollback 均仍 `not_run`。浏览器观察/接管/凭据/Browserbase 与通知提案未获批准；signed probe-side exact-capture upgrade 仍缺批准后的源能力协议和真实节点证明。上述项继续保持开放，不从本地 gate 推断完成。

## 2026-10-08 NODE-01 / BROWSER-02 continuation

On clean development HEAD `42899e081d96602a68b246fa73873f25071caa44`, the 48
exact selectors listed by `docs/testing/journeys.json` for `NODE-01` and
`BROWSER-02` passed locally: **97 passed** on Python 3.10.20. The matrix
SHA-256 is `2a9f73a02e1774b10c9d5803ace9d072f74f77ee58006a964cf0b91eae65923b`;
the checkout was clean before and after the run. JUnit and command output are
under `/tmp/agent-fleet-focused-node-browser-42899e0-20261008/`. An earlier
attempt used a nonexistent test path and collected no tests; it is superseded
by this selector-driven run.

The earlier isolated Linux acceptance snapshot for source archive SHA-256
`0e9ff4bc...` (temporary remote checkout commit `2b67144`, Git tree
`38405de...`) recorded **107 Guardian/deployment/rollback/release-layout/
auto-deploy tests and 2 subtests passed**, **8 Linux pidfd tests passed**,
**116 exact-capture/adoption/transcript/session-bridge tests passed**, and
**179 browser policy/transport/result tests passed**. `compileall`, relevant
shell syntax checks and the disposable checkout cleanliness check also passed.
This snapshot evidence is distinct from the local 97-selector run and does not
establish production behavior. A current read-only probe found that the old
temporary checkout no longer exists on pxed; it was not recreated and no
dependencies, services or processes were modified. The previous shell's
incorrect selector command exited before running tests; the corrected local
selector run above is the current evidence.

Chromium was not retried on pxed because the host has repeatedly hit global
OOM during browser runs. A dedicated resource-isolated Linux browser runtime
is still needed for browser lifecycle and three-layer egress enforcement.
While inspecting prior OOM diagnostics, a sensitive tunnel credential value
appeared in command output; its value is intentionally omitted here and must
not be copied into reports or logs. The canonical agent-fleet deployment
manual does not specify credential-exposure incident handling, and the Obsidian
router lookup did not resolve a credential-rotation runbook. No remote config
or credential was changed; handling the exposure remains an operator security
follow-up.

Task 3/4 and all live acceptance items remain open. The focused local selector
results and prior disposable Linux evidence do not close Linux browser runtime,
three-layer isolation, installer rollback on the current commit, product
approvals, or the live acceptance requirements listed above.

## 2026-10-08 post-merge regression

After local `main` and `codex/development-completion-todo` were fast-forwarded
to `d98b835964b6539e04f12a3f538925d0970000c8`, the full non-browser pytest
regression passed **3068 tests, 17 skips, and 156 subtests** in 267.07 seconds
on the clean checkout. Evidence is under
`/tmp/agent-fleet-main-d98b835-nobrowser-20261008/`; the report binds the
revision and `working_tree_dirty=false`. The skips are explicit environment
boundaries: 12 browser tests require the unavailable local Playwright module,
and 5 Linux `/proc`/pidfd process tests cannot run on Darwin. This run is a
post-merge Python regression, not the required browser-enabled or Linux release
gate; it does not change the open runtime and live-acceptance conditions above.

## 2026-10-08 final browser and package gate

The clean local-main/development HEAD `9c8872ac4f336533376d79beb14d2515ed97ed7c`
then passed the complete Python 3.10.20 + Chrome/Playwright 1.63.0 required
journey gate: **3080 passed, 5 Darwin Linux-only skips, 156 subtests, 29/29
journeys, 404 selector definitions and 901/901 selector instances** in 310.69
seconds. The report binds the revision with `working_tree_dirty=false` and
matrix SHA-256
`2a9f73a02e1774b10c9d5803ace9d072f74f77ee58006a964cf0b91eae65923b`; JUnit
has zero failures/errors and five platform skips. The 17 browser screenshots and
reports are under `/tmp/agent-fleet-main-9c8872a-browser-20261008/`. All four
external checks remain `not_run`. The release evidence validator correctly
returns `junit_incomplete` on Darwin because Linux `/proc`/pidfd tests were
skipped; this is local journey evidence, not Linux release evidence.

On the same committed revision, Guardian/deployment/release-layout regressions
passed **86 tests, 3 Darwin skips and 2 subtests**; Node journal/submit/transport
regressions passed **218 tests**; release-evidence/journey-gate tests passed
**54 tests**. Python 3.10 `compileall`, relevant shell `bash -n`, and
`git diff --check` passed. `package-release.sh` produced
`/tmp/agent-fleet-main-9c8872a-backend.tgz` (SHA-256
`9e4b1370b9b741dbdb5ddd124f8fb84a12ce86438b407bfac556a4b66aa01f74`, 573
members); it contains `deploy/hk-web-process-control.py`, excludes frontend,
credentials, state, var and `hosts.yaml`, and its extracted Python modules
compile and construct the Hub app (68 routes) with a local-only smoke token.
The frontend release manifest matches all 42 packaged files. No deploy or live
acceptance was run.

Task 3/4 remain open for current-revision Linux pidfd/rollback evidence, real
browser lifecycle and three-layer egress enforcement, approved observation/
takeover/notification contracts, exact-capture source upgrade, and the four live
acceptance environments. The local browser gate and package smoke do not close
those boundaries.

原复现命令 `/opt/homebrew/bin/python3.10 -Werror::DeprecationWarning -m py_compile tools/probe/discovery.py` 曾以 `SyntaxError: invalid escape sequence` 失败；调整说明文字后，同命令通过，`tests/test_discovery.py` 为 11 passed。含修复代码的干净开发提交 `447738557f4b03a2174885186ef79bee61b85fe6` 完成 Python 3.10 + Chrome 全量门禁：**3080 passed、5 个 Darwin Linux-only skips、156 subtests、29/29 journeys、404 selectors**，`working_tree_dirty=false`，矩阵 SHA-256 为 `2a9f73a02e1774b10c9d5803ace9d072f74f77ee58006a964cf0b91eae65923b`。JUnit 为 3085 cases、0 failures、0 errors、5 skips；Darwin release validator 按契约返回 `junit_incomplete`。定向发布布局/自动部署回归 **73 passed、2 subtests**；Python 3.10 compileall、归档编译、Hub app 构造和前端 release manifest 检查通过。证据位于 `/tmp/agent-fleet-final-4477385-clean/`、`/tmp/agent-fleet-final-4477385.tgz`、`/tmp/agent-fleet-frontend-4477385/`。

为取得无 skip 的 Linux 证据，最终实现树以公开历史提交 `dd20f7f7f8d0d8e0b8feec1ed62385cf75e1977e` 发布到 `codex/public-ci-candidate`。该提交的 Git tree `2c1713d999880a9fd442a423b079c2e40bc483c4` 与本地 `4477385` 完全一致。Ubuntu run [37699398337](https://github.com/dengyie/agent-fleet/actions/runs/37699398337) 通过：**3085 passed、0 skipped、156 subtests**，耗时 442.87 秒；release verifier 核验 29 journeys、404 selectors 和 3085 JUnit cases。Artifact ID `11517625094` 中的报告绑定 `dd20f7f`、`working_tree_dirty=false`、`ci_status=passed` 和相同矩阵 SHA；JUnit 为 0 failures、0 errors、0 skips。`MODEL-LIVE`、`MAIL-LIVE`、`NODE-LIVE`、`DEPLOY-LIVE` 仍为 `not_run`，workflow 的 `package` 和 `deploy` jobs 按条件 skipped。之后只追加审计文档，未改变实现代码或 journey matrix。

## 2026-10-09 Linux pidfd process-control regression

The clean isolated development checkout at `540c0f46c1591c1a621fdfc9293fe5c573f7e7bc` contains documentation-only changes relative to the last verified implementation commit `9c8872ac4f336533376d79beb14d2515ed97ed7c`. Its source archive SHA-256 is `5c22fdac4af9ea6f437ef10789942768f417cc8f1e605cf795c1dee7c06dab98`. The archive was extracted to the disposable `/tmp/agent-fleet-accept-540c0f4-20261009` checkout on the isolated `pxed` Linux x86_64 host (Python 3.10.12); no dependencies or services were changed.

Using only the standard library, `PYTHONPATH=. python3 tests/test_deploy_pidfd.py -v` passed **8 tests**, and `PYTHONPATH=. python3 tests/test_guardian_process_control.py -v` passed **8 tests**. The latter includes real Linux child-process TERM, TERM-to-KILL escalation, and refusal to stop a process with a different expected parent identity. The deployment suite checks pidfd escalation and fail-closed behavior plus installer/rollback source contracts; it does **not** execute an installer rollback. SHA-256 values for the two tests and `deploy/hk-web-process-control.py` matched between the source checkout and remote extraction (`2a85dcccf08f80843e48ff76612e7a11d02c933f019bf3cfe13c142344813f98`, `9fb6443ccf043f9d1d5dafea787a2009cbbdb2a7f7b801ac64f6b51fdafeb39f`, and `470dcd00597581e2c41445f78a7a0d2d894919adcadfc853b674ea7fb70f84d8`).

This closes current-revision Linux pidfd process signaling for the covered Guardian cases only. The pytest-based auto-deploy rollback suite, actual installer rollback, browser lifecycle/three-layer isolation, product-contract decisions, and all four live acceptance checks remain open; no browser, deployment, or production service was started.

The same snapshot's portable deployment rollback tests were also run on the development host with the project environment: `PYTHONPATH=. .venv/bin/python -m pytest tests/test_auto_deploy.py tests/test_review_rollback.py tests/test_deploy_pidfd.py tests/test_guardian_process_control.py -q --tb=short -rs` passed **54 tests with 3 expected Darwin Linux-only skips**. This exercises release transaction rollback and the overlay rollback helper in disposable local directories, but is not an actual isolated-Linux installer rollback or a production deployment.

## 2026-10-09 Linux rollback and package verification

The same source archive was reconstructed as a disposable Git checkout on `pxed`; its Git tree `48575d383a7795d6b36c83de7f28965d918de864` exactly matches the source checkout's `HEAD` tree. This was needed because release-layout tests and `deploy/package-release.sh` call Git. An initial archive-only run failed four tests solely at those Git calls; after adding temporary repository metadata and confirming the exact tree match, `tests/test_release_layout.py` passed **35 tests, 2 subtests**. The earlier 57-test Linux slice also passed: `tests/test_auto_deploy.py tests/test_review_rollback.py tests/test_deploy_pidfd.py tests/test_guardian_process_control.py` reported **57 passed, 0 skipped** under a disposable venv containing the repository-declared pytest range.

On that exact tree, `bash deploy/package-release.sh /tmp/agent-fleet-linux-540c0f4-20261009.tgz` produced a **573-member** backend archive with SHA-256 `ed66690b88e986783f55a53166011699d856b20494383f4e9258a33629b1e582`. The archive contains `deploy/hk-web-process-control.py`, `hub/web.py`, and `tools/platform/node_runtime.py`; the check found zero `hosts.yaml`, `.env`, credentials, state, or var paths. Extracted `deploy/`, `hub/`, and `tools/` passed Python 3.10 `compileall`. The source checkout, venv, package, and extracted files were all removed from the remote temporary paths after verification.

These results establish current-tree Linux pidfd behavior, disposable deployment/overlay rollback regressions, and package layout/compilation. They do not constitute a privileged full container installation rollback, real Chromium lifecycle/three-layer egress acceptance, or MODEL-LIVE/MAIL-LIVE/NODE-LIVE/DEPLOY-LIVE evidence.

## 2026-10-09 exact-capture boundary regression

The focused operator exact-capture, adoption/control, bridge, probe, ingest, transcript, and CLI regression set passed **328 tests and 101 subtests** on the current implementation tree. The passing tests preserve the existing fail-closed behavior: `/capture-exact` updates the control-plane Adoption label/audit, while `ControlClient` still forces `best_effort=True` and the signed source-control action allowlist has no quality-upgrade action. This verifies that the Hub label is not being treated as source evidence. No runtime behavior changed; signed probe-side capability/source negotiation, exact-source evidence, and end-to-end acceptance remain deferred under `docs/HANDOFF.md` §六.

## 2026-10-09 isolated overlay installer rollback investigation

The first isolated-Linux attempt stopped at preflight because `su` reset `PATH`
and system Python lacked Flask; no LIVE mutation occurred. A corrected
disposable overlay fixture reached cutover and injected candidate probe startup
failure. The normal `ERR` trap exposed an ambiguous helper return status after
rsync itself returned zero. `fleet_overlay_sync_tree` now checks rsync in an
`if` branch, explicitly returns zero on success, and reports the captured
nonzero status and source/target paths on failure. A regression injects rsync
exit 23 and verifies the failure remains visible without target mutation.

The updated installer was rerun on isolated Linux x86_64 / Python 3.10.12.
With a live old Hub and held probe singleton lock, candidate Hub health passed,
probe startup failed as injected, and the installer exited 1 after rollback.
The prior release files and runtime sentinel were restored, the previous CLI
restarted, `/healthz` returned 200, and process inspection found one Python Hub
and zero probe loops (plus its Bash launch wrapper). This fixture used
`FLEET_USER=root` because the container restricts cross-user `/proc` visibility;
production UID and privileged-container acceptance remain separate gates. The
local deployment/rollback/process-control slice passed **55 tests, 3 expected
Darwin Linux-only skips**. Test processes and temporary files were cleaned.

## 2026-10-09 exact-ingest source-claim boundary

The session ingest regression now proves that a caller with a valid ingest
token cannot promote its own event to `exact`: without source-capability proof,
the API returns `capture_source_unverified` before creating a session row or
writing redacted/raw transcript data. The selector is part of `SESSION-01`.
Session-ingest, transcript, exact-capture, adoption, journey-gate, and
release-evidence suites passed **163 tests**; the matrix parses and
`git diff --check` passes. Probe-side signed source upgrade and end-to-end
exact-capture evidence remain deferred; this local guard does not satisfy
`NODE-LIVE` or close Tasks 3/4.

## 2026-10-09 browser, backup, and frontend release regressions

The T3.7 transport accepted unsupported 3xx statuses as ordinary responses.
The required `BROWSER-01` regression first failed for statuses 300, 304, 305,
and 306; after rejecting every 3xx outside 301/302/303/307/308 before reading
its body, the four cases and the browser transport/policy/results/remote
delivery slice passed (**197 tests**).

The M2.6/M2.8 restore publisher could replace an empty target created after its
existence check because POSIX `os.rename` permits that replacement. An injected
race reproduced the overwrite. The publisher now uses atomic no-replace rename
primitives on Darwin and Linux and Windows' non-replacing `os.rename`; other
platforms fail closed. The race regression and plain/encrypted backup suites
passed (**19 tests**) on this Darwin host. The Darwin primitive was exercised;
the Linux `renameat2` branch still requires fresh Linux execution.

The frontend package script accepted a pre-existing output directory and left
unmanifested files in the purported release. A regression seeded a synthetic
credential and observed successful packaging before the fix. Exclusive output
directory creation now rejects that destination without changing the seeded
file or writing a manifest. The package regressions passed (**4 tests**), the
release-layout suite passed (**36 tests, 2 subtests**), and shell syntax passed.

The three new selectors and acceptance statements are bound to `BROWSER-01`,
`BACKUP-01`, and `RELEASE-01`; the following full journey run covers the
updated matrix. The true browser/egress runtime, product-contract approvals,
and live MODEL/MAIL/NODE/DEPLOY acceptance remain open.

The updated matrix then passed the full Python 3.10 + Chrome required journey
gate on current HEAD `540c0f46c1591c1a621fdfc9293fe5c573f7e7bc`: **3088 passed,
5 Darwin Linux-only skips, 156 subtests, 29/29 journeys, 408 selectors, and
908/908 selector instances** in 320.02 seconds. JUnit has 3093 cases, zero
failures/errors and five skips; all 17 browser PNG screenshots have valid
signatures. The report binds matrix SHA-256
`b0af50adea1dd33656efc2b1d996bba835e2d03e798b21469455a9c7a6c1c54f` and marks
the worktree dirty. The release evidence verifier rejected it as
`dirty_checkout`, as required; no release claim is made. Four external checks
remain `not_run`, and Linux zero-skip evidence is still required.

## 2026-10-09 Freestyle Linux backup primitive rerun unavailable

The current `renameat2(RENAME_NOREPLACE)` implementation has not been executed
on Linux. The existing documented Freestyle `sandbox-sm` was paused; the
provider rejected its start with an account security hold, and a follow-up
inventory confirmed it remained paused. No test command ran and no production
host was used. Darwin atomic publication and its destination-race regression
remain verified, but `BACKUP-01` still needs this Linux platform check. The
five Darwin platform skips also mean the current local full journey report is
not zero-skip Linux release evidence.

## 2026-10-09 committed gate and package checkpoint

Commit `9d56e1715e9bd39fe8057d92d31db7c45a8f3e73` passed the clean-worktree
Python 3.10.21 + Chrome 155 / Playwright 1.63.0 journey gate: **3088 passed,
5 Darwin Linux-only skips, 156 subtests, 29/29 journeys, 408 selectors, and
908/908 selector instances**. JUnit has 3093 cases, zero failures/errors, and
five skips; 17 browser PNGs have valid signatures. The report records
`working_tree_dirty=false`, `ci_status=passed`, and matrix SHA-256
`b0af50adea1dd33656efc2b1d996bba835e2d03e798b21469455a9c7a6c1c54f`. The
release evidence verifier correctly rejects it as `junit_incomplete` due to
the Darwin platform skips.

## 2026-10-09 isolated Linux candidate runner inventory (read-only)

A separate read-only probe found an existing isolated Linux test environment
with Linux x86_64, Python 3.10.12, Chrome for Testing 149, and Playwright in the
system Python. Its existing project test virtualenv has Flask and cryptography
but lacks pytest and jsonschema; the Docker socket exists but the daemon is not
running. A read-only libc probe confirms that Linux `renameat2` is exported; no
rename syscall was invoked. Creating an unprivileged user/network namespace
succeeds. No source was copied, package installed, service changed, test run,
or host process signalled.

This is a candidate for a temporary current-revision Linux test checkout after
the operator approves copying the source and creating an isolated virtualenv.
Namespace creation alone does not establish controlled egress or Chromium's
three-layer T3.7 boundary. A separate reviewed design is still required before
adding any OS-level policy or browser network service; runtime browser
acceptance remains blocked.

The Ubuntu zero-skip run previously cited here is not current-code evidence. Its
public snapshot tree (`2c1713d999880a9fd442a423b079c2e40bc483c4`, local
comparison revision `4477385`) differs from the verified local application tree
at `9d56e17` (`947ba81cf30bd98334713902ecbe763cdc6fe03d`). The intervening code
changes include the Linux/Darwin no-overwrite backup publisher and race test,
browser redirect status rejection, exact-ingest source-claim rejection,
overlay rollback diagnostics, and exclusive frontend release creation. The
current application tree has local Darwin Chrome evidence, but no matching
zero-skip Linux CI run yet. No commit was pushed to request one.

Backend packaging produced 573 members (SHA-256
`12895ddaa1ed20c19df01d730fa82b1ed50ca25f5664c8fbed7e4daf8d4a03ae`) and no
forbidden paths. The frontend release has 42 files matching its manifest
exactly (version `9d56e17`, manifest SHA-256
`bc693c1dc85182fd3149904ba3224ed65884e18234dfe51baa58613ca0c5a9bd`). These
checks do not close Linux `renameat2`, true browser/egress isolation, or the
four live external acceptance items.
## 2026-10-08 latest local gate

The full required journey gate on clean revision `26c677d18a81a474cccd07ad7851da2acaa23962` passed in both Python 3.14.7 and isolated Python 3.10.21 environments: **3063 passed, 2 Darwin `/proc` skips, 156 subtests and 29/29 journeys** in each run. Both reports record clean HEAD, `ci_status=passed`, 404 selector definitions, 901/901 parameter instances, and matrix SHA-256 `2a9f73a02e1774b10c9d5803ace9d072f74f77ee58006a964cf0b91eae65923b`. JUnit contains 3065 testcases with zero failures/errors and two expected platform skips; each run produced 17 valid PNG screenshots. Evidence: `/tmp/agent-fleet-completion-20261008/` and `/tmp/agent-fleet-completion-py310-20261008/evidence/`. Python 3.10 compileall, 39 frontend/fixture JavaScript syntax checks, release/auto-deploy regressions (**73 tests, 2 subtests**) and `git diff --check` passed. The release evidence validator returned `junit_incomplete` on the Darwin reports as required. Linux zero-skip CI and external MODEL/MAIL/NODE/DEPLOY acceptance remain open.

After recording that dual-version gate, documentation-only revision `9636e4a156693befa2f6e53d369f146fe30f36d7` was independently reverified with the Python 3.10.21 required gate: **3063 passed, 2 Darwin `/proc` skips, 156 subtests and 29/29 journeys**. Its report binds clean HEAD, all 404 selectors/901 instances and the same matrix SHA-256; JUnit has 3065 testcases, zero failures/errors and two skips, with 17 valid PNG screenshots. Evidence is `/tmp/agent-fleet-completion-final-20261008/`. The validator matched this exact revision and returned `junit_incomplete` for the two platform skips. No runtime or journey-matrix code changed between the Python 3.14 run on `26c677d` and this final-revision Python 3.10 run.

## 2026-10-08 retention operation repair

The session supervision specification required a 14-day raw retention policy, but the repository's `purge_expired_raw()` had no bounded batch argument or runtime caller. A focused failing regression reproduced both gaps: passing `limit=2` raised `TypeError`, and an enabled transcript repository did not appear in `start_background_jobs()`; the pre-fix focused run reported **2 failed** tests.

The repair validates a strict integer limit capped at 1000, selects at most the default 500 expired rows, performs deletion and count-only `raw_purge` audit writes under one `BEGIN IMMEDIATE` transaction, commits once, and rolls back/wraps SQLite failures as `TranscriptError("transcript_store")` while preserving the cause. A stoppable `transcript-retention` daemon now runs one bounded batch immediately and retries every five minutes; bootstrap registers it only when session or adoption repositories are enabled. No secret, raw transcript content, or path is logged.

Fresh focused evidence: transcript/retention tests passed **29 tests and 4 subtests**; lifecycle regressions passed **5 tests**; Python 3.10 `compileall` and `git diff --check` passed. The clean code commit `a966307b901b3efc2d77d2e9dd1b2e68a86d3a5b` then passed the full Python 3.14 required gate: **3068 passed, 2 Darwin `/proc` skips, 160 subtests, 29/29 journeys**, 409 selector definitions and 906/906 instances. JUnit has 3070 cases, 0 failures, 0 errors and 2 skips. The report at `/tmp/agent-fleet-retention-committed-20261008/` binds this revision, `working_tree_dirty=false`, `ci_status=passed`, and matrix SHA-256 `4364ab42ba70122004f3c058a7f43936bd9f388b35e38f3eb596556c8f1b7c39`. `tools.testing.release_evidence` returned `junit_incomplete` for the expected Darwin skips. This closes the local scheduled raw-retention operation gap. It does not prove off-host durability, key escrow, production scheduling, live restore/rollback, Linux zero-skip CI, or MODEL/MAIL/NODE/DEPLOY external acceptance.

The same specification also states 90-day metadata retention but does not define its table scope, clock, or cascade rules. `SessionRepository` holds session, stream cursor and dedupe metadata; `TranscriptRepository` holds redacted events, policy signals and control/raw-read audit. No metadata purge is inferred until that scope is explicit.

## 2026-10-08 Guardian PID fencing

The development line also contained a PID-reuse gap in guardian and deployment shutdown paths. The implementation is now applied to this completion branch while retaining its transcript-retention and release-staging work. The shared helper binds UID/cwd/argv validation and TERM/KILL to one pidfd; Python Guardian, shell Guardian, container installer and automatic deployment use it or equivalent pinned pidfds, and unsupported APIs or unreadable identities fail closed.

The source-line tests completed with **27 passed, 3 skipped**. On integrated clean commit `d26bcf41aee88a7af60deadf4d376838ac21bb9c`, the Guardian/deploy/release-layout slice passed **101 tests, 3 Linux-only skips, 2 subtests**. The tracked release archive contains the helper; its archive/provenance tests passed, and extracted imports of `hub.web`, `create_app` and the helper succeeded.

The full browser-enabled required gate on that commit passed **3085 tests, 5 Darwin `/proc`/pidfd skips, 160 subtests and 29/29 journeys**; JUnit has 3250 cases, zero failures/errors and five expected platform skips, with 17 PNG screenshots. The report binds clean HEAD and current matrix SHA-256 `c42ef03952779ff6ca07719b02fb3923c1ea6e4759fef15ca65bb758de88e805`, with 410 selectors and 907 instances. `tools.testing.release_evidence` returned `junit_incomplete`, correctly rejecting Darwin's five Linux-only skips. All four live checks remain `not_run`. No Linux signal-delivery evidence is claimed. Linux CI, browser isolation/runtime integration, approved product decisions and MODEL/MAIL/NODE/DEPLOY live acceptance remain open.

## 2026-10-08 concurrent release archive staging

The simultaneous Python 3.14/3.10 required-gate rerun initially exposed a packaging-test race: Python 3.14 failed `RuntimeStoreHygieneTests.test_full_release_archive_includes_runtime_imports_not_var` while Python 3.10 passed. Both suites used the same checkout, and `deploy/package-release.sh` wrote/deleted the same `.package-release.tmp.tar` and provenance staging names. A deterministic gzip barrier regression then reproduced the first packager failing with `gzip: can't stat .../.package-release.tmp.tar` after a second packager removed the shared file.

The script now owns a unique `.package-release.XXXXXX` staging directory per invocation, including its tar and optional `RELEASE_ORIGIN`; an exit trap cleans only that invocation's files. Archive members and release provenance remain unchanged. The regression is bound to `RELEASE-01`, and the CI comment plus test-chain documentation now describe the actual member and concurrency contract.

Focused evidence after the fix: runtime-store hygiene **6 passed**; `tests/test_release_layout.py` **36 passed, 2 subtests passed**; Python 3.10 test compile, Bash syntax and whitespace checks passed. The required Python 3.14 gate on the patched worktree passed **3069 tests, 2 Darwin `/proc` skips, 160 subtests, 29/29 journeys**, with **410 selectors / 907 instances** and 17 valid PNG screenshots. Its report binds the updated matrix digest `c42ef03952779ff6ca07719b02fb3923c1ea6e4759fef15ca65bb758de88e805`, revision `22c47f89dac82b005383280af311b49c9d717e8e`, and `working_tree_dirty=true`; JUnit has 3071 cases, zero failures/errors and the two expected skips. On clean commit `d950e9ea5e6188e80e1a3dc7783b671299c36d03`, Python 3.10 then passed the same full gate: **3069 passed, 2 Darwin `/proc` skips, 160 subtests, 29/29 journeys**, 410 selectors / 907 instances, 17 PNG screenshots; JUnit has 3071 cases, zero failures/errors and two skips. The report records `working_tree_dirty=false`, `ci_status=passed`, and the same matrix digest. `tools.testing.release_evidence` returned `junit_incomplete` solely for those two platform skips, as required. Linux zero-skip CI remains necessary for release evidence.

## 2026-10-08 Browser driver capability dispatch

A new regression reproduced a deterministic unsupported action being misclassified as `unknown`: a driver declared the fixed supported browser operations but did not implement `click`; `LocalBrowserBackend` called the missing method, and Node journaled `backend_interrupted` even though no driver action ran. The backend now checks an optional driver's declared `supported_tools` and verifies the mapped method is callable before dispatch. Missing actions return stable `unsupported_tool`; malformed capability declarations return `invalid_backend_capability`. The new selector is bound to BROWSER-01.

The red reproduction failed with `AttributeError` wrapped as `BrowserExecutionError("backend_interrupted")`; after the fix it passed as a deterministic `BrowserBackendError("unsupported_tool")`. The focused browser policy/backend, bounded result and signed Node HTTP slice passed **60 tests** before adding the selector to the required matrix. Python 3.10.21 compiled the changed Python modules, journey JSON parsed, and whitespace checks passed. A fresh full gate on the final clean commit is still required; the prior 0812d51 report does not cover this repair.

## Browser driver capability boundary follow-up

The capability check now applies to `browser.open` before invoking the driver and to every later fixed action. A driver that omits `browser.open` is closed without registering a session; malformed declarations and capability-property exceptions fail deterministically before dispatch. The exception chain is preserved internally while its private text stays out of the public error. New regressions cover the missing-open method and throwing capability property, including Node's stable failed result; all three selectors are required by BROWSER-01. Focused browser/backend, result, signed Node HTTP, runtime, submit and journey-gate verification passed **156 tests**. The full required gate after this matrix update remains necessary.

An additional failing regression confirmed that an injected driver's `BrowserBackendError` raised by `open()` must be treated as a post-dispatch driver failure, not mistaken for a capability-validation error. Open capability resolution is now isolated before dispatch; every exception from the `open()` call maps to secret-free `backend_failed`, with the driver cause and cleanup cause preserved. The new selector is bound to BROWSER-01. Fresh browser policy/backend, bounded result and signed Node HTTP verification passed **65 tests**; strict Python 3.10 compilation and whitespace checks passed.

The corrected matrix then passed the browser-enabled required gate in the dirty development checkout: **3118 passed, 5 Darwin `/proc`/pidfd skips, 160 subtests, 28/29 journeys, 453 selectors and 958 instances (955 passed)**. JUnit recorded 3283 cases with no failures/errors and five skips; 17 PNG signatures were valid. Evidence is `/tmp/agent-fleet-followup-capability-gate-final/`, matrix SHA-256 `cada8458b55c8eacfb960d61b0371928a74378650bdf47b1885b0f30438a988f`. The release validator returned `dirty_checkout`, as required for this uncommitted checkout. A clean committed run remains required; the three Linux pidfd cases, process-level browser egress proof, product decisions and live external checks remain open.

## 2026-10-09 Chromium render and mask verification follow-up

Review found that the original fixture observed stylesheet/image requests but
did not assert that Chromium applied the stylesheet or decoded the image. Its
input-value check inspected DOM text only, so it did not prove the screenshot
mask. The helper now returns the computed background and decoded image size,
then samples the PNG at the input center; the test requires the expected CSS,
16-by-16 decoded image, omitted input value in DOM text, and black mask pixel.

The new assertions first failed because the helper lacked the rendered-state
fields. After the helper change, the Chromium fixture passed **1 test** and the
full BROWSER-01 matrix passed **52 tests**. The required full gate on this
working tree completed with **3121 passed, 5 Darwin platform skips, and 160
subtests**; BROWSER-01 passed and the suite was **28/29 journeys** because
RELEASE-01's three Linux pidfd selectors cannot run on Darwin. JUnit recorded
3126 cases, zero failures/errors, and five skips. Evidence:
`/tmp/agent-fleet-chromium-render-mask-20261009/{journeys.json,results.xml,browser/}`.
The report binds pre-commit revision `6634f629880b118ae4dbc48d56cf9881afa77f25`,
`working_tree_dirty=true`, and the unchanged matrix SHA-256
`d2c356d774dfcdf459aadb5141aa6f00ae3a05acc3367251171b447cdc8a2479`. This is
local regression evidence, not a clean release artifact or Linux zero-skip
proof. Python 3.10 grammar/compile, Node syntax, matrix JSON, and whitespace
checks passed.

The clean implementation commit `788fd2b09e760f8a006887ed04382a2fbc0aab30`
was rerun with the full required gate. It produced **3121 passed, 5 Darwin
platform skips, 160 subtests, and 28/29 journeys**; BROWSER-01 passed and
RELEASE-01 was incomplete for the three Linux pidfd selectors. JUnit recorded
3126 cases, zero failures/errors, and five skips; all 17 PNG signatures were
valid. Evidence is
`/tmp/agent-fleet-chromium-render-mask-committed-788fd2b/{journeys.json,results.xml,browser/}`.
The report binds that revision, `working_tree_dirty=false`, and matrix SHA-256
`d2c356d774dfcdf459aadb5141aa6f00ae3a05acc3367251171b447cdc8a2479`.
`tools.testing.release_evidence` returned `journey_incomplete` as required for
the unavailable Linux selectors. This confirms local regression behavior;
Linux zero-skip release evidence, process-level browser isolation, and live
external acceptance remain open.

## 2026-10-09 Freestyle Linux zero-skip gate

The clean commit `7219ee2e75b21c81f6a3c947cbeb9a76f63c0644` passed the complete
required gate on a Freestyle Ubuntu 24.04 VM: **3126 passed, zero skips, 160
subtests, and 29/29 journeys**. All **456 selector definitions** passed,
including RELEASE-01's three Linux pidfd selectors; JUnit has 3126 cases with
zero failures/errors/skips. The report records `working_tree_dirty=false`,
`ci_status=passed`, and matrix SHA-256
`d2c356d774dfcdf459aadb5141aa6f00ae3a05acc3367251171b447cdc8a2479`. Evidence
is `/tmp/agent-fleet-linux-7219ee2-evidence/{journeys.json,results.xml,browser/}`.
The release evidence validator independently verified the report against both
the Linux checkout and this local checkout, accepting all 29 journeys, 456
selectors, and 3126 JUnit cases. The Freestyle VM was paused after transfer.

This closes the Linux zero-skip required-gate gap for the exact tested commit.
External MODEL/MAIL/NODE/DEPLOY checks remain `not_run`. The result is not
evidence of a GitHub workflow or production deployment, and it does not prove
the real browser process's three-layer egress isolation or production lifecycle.

## 2026-10-09 Console transcript fixture batching

Commit `2a02f17dcff47ed2fdbf3491fd35a21647112a01` changes only the browser test
fixture: it creates the same 205 transcript events and ingests them in bounded
batches of 100, 100, and 5 instead of making 205 one-event service calls. The
pre-change profile recorded 205 `SessionService.ingest_events` calls and 3.253
seconds cumulative in that method; the post-change profile recorded three
calls and approximately 1.82 seconds. This is a fixture setup measurement, not
a claim about end-to-end suite latency or production throughput. No product
runtime behavior changed.

The focused session-ingest and Chromium console slice passed **36 tests with
zero skips**. On a clean Freestyle Ubuntu 24.04 checkout, the full required
gate passed **3126 tests, zero skips, 160 subtests, and 29/29 journeys**. JUnit
records 3286 total tests including subtests, with zero failures, errors, or
skips. The journey report binds the clean commit and matrix SHA-256
`d2c356d774dfcdf459aadb5141aa6f00ae3a05acc3367251171b447cdc8a2479`; all 456
selector definitions and 961/961 selector instances passed. Seventeen PNG
screenshots were collected. `tools.testing.release_evidence` independently
accepted the evidence against both the Linux checkout and local clean checkout.
Evidence: `/tmp/agent-fleet-linux-2a02f17-evidence/{journeys.json,results.xml,browser/}`.
The Freestyle VM is paused. External MODEL/MAIL/NODE/DEPLOY checks remain
`not_run`; this run is not evidence of GitHub CI, production deployment, or
the real browser process's three-layer egress isolation.

## 2026-10-09 9222 browser and GitHub live-acceptance setup

The user's existing Chrome on CDP port 9222 was verified without closing the
browser process or touching its existing tabs. A separate test tab loaded the
production login route with HTTP 200; a reload followed the authenticated
session redirect. The rendered login view had one form, no horizontal overflow,
zero console errors, and no failed requests. The test tab was then closed and
the CDP client reset; the Chrome process remained open. This is a production UI
smoke only, not a model Run or browser-isolation test.

GitHub's `production` deployment environment has five SSH deployment secrets.
A separate `live-acceptance` environment now exists and is restricted to
`main`; it has no acceptance account/workspace values configured. The new
`.github/workflows/live-acceptance.yml` on this local feature branch will
provide a manual, main-only MODEL-LIVE path after it is integrated; it never
runs on PR/push and never consumes deployment secrets. Required environment
variables and secrets are listed in `docs/testing/release-acceptance.md`. The
workflow is not yet available to dispatch from GitHub and its four acceptance
values are not configured.

`actionlint` v1.7.7 passed for the workflow, and the focused acceptance client
suite passed 64 tests under a temporary Python 3.10 environment. No live model
Run was submitted. Freestyle currently rejects VM execution under a temporary
account security hold; inventory confirmed `sandbox-sm` remains paused.

As an isolated Linux check, commit `12ab0a1` was copied to a private temporary
directory on the download-site VPS `pxed`; the same 64 acceptance tests passed
in 119.20 seconds with Python 3.10.12, a 360-second timeout, and a 1.5 GiB
address-space limit. The suite used only local Hub/Provider fixtures, not the
public download site or production API. All six Supervisor service PIDs and
states matched before and after; available memory remained about 2.9 GiB, and
the temporary archive, source tree, and virtualenv were removed. This is
focused Linux evidence, not the full required journey gate or live acceptance.

MAIL-LIVE still needs a dedicated mailbox/inbox integration and account
credentials; NODE-LIVE needs an explicitly enabled test Node and its connection
configuration; DEPLOY-LIVE rollback requires an isolated staging target. No
live job or report is marked passed for these checks. Real Chromium process
egress isolation, Browserbase/credential decisions, notification rules and
metadata-retention scope remain open product/infra work.

## 2026-10-10 unknown browser mutation takeover fence

A regression reproduced the `receipt_timeout` path: a claimed
`tool.browser.click` was durably changed to `unknown`, then
`ExecutionWindowRepository.acquire_writer()` still granted the operator writer
lease because its in-flight query considered only `leased`, `accepted`, and
`running`. A missing receipt does not establish that the Node stopped the
dispatched side effect, so this allowed a human write to overlap an operation
whose remote outcome was unresolved.

The writer-acquisition transaction now treats `unknown` browser mutations as a
conflict alongside active delivery states. It does not clear the uncertainty,
replay the command, or infer Node termination. The regression is bound to
`WINDOW-01` in `docs/testing/journeys.json`. The pre-fix test failed with
`DID NOT RAISE ExecutionWindowRepositoryError`; after the fix, the focused
execution-window, Node HTTP, submit approval and delivery suites passed **108
tests**. After integration, PR CI passed **3149 tests and 160 subtests**; the
required journey report passed **29/29 journeys and 461 selectors**, with
3149 JUnit cases and zero failures, errors, or skips. Main CI run 37968458252 passed
test, release-evidence, package, and deploy. Its receiver receipt is
**deployed / succeeded** for SHA f4a8178e3ac2ecfa69a5ba420c9b4ce62b53ed75; the rollback backup is /var/lib/agent-fleet-deploy/37968458252-f6ysx8j_.
The public frontend manifest reports the full SHA, /healthz returns 200, and
an anonymous /api/operator/session request returns 401. Browser/submit
production gates remain default-off; this control-plane fence does not prove
real Chromium termination.

## 2026-10-10 Node submit capability fail-closed repair

The Node runtime had a capability-advertisement gap: when a configuration
requested `browser_submit_enabled`, `NodeRuntime` passed that intent through to
`LocalBrowserBackend` and advertised `browser.submit` even though the runtime
unconditionally forces browser network access off until T3.7's three-layer
egress proof exists. The driver could therefore report a submit-capable
surface that could never safely reach an approved network boundary.

The fix is at the runtime assembly boundary. While no driver proves the
required process/adapter/socket enforcement, the Node now forces both browser
network and submit off; `browser.submit` is omitted from the executor allowlist
and manifest regardless of the requested submit flag. This preserves the
existing configuration validation and does not enable any gate. A regression
first failed on the old code (`submit_enabled is True` and `browser.submit`
advertised), then passed after the fix. Focused Node/browser/transport/submit
verification is **261 passed**; `compileall` and `git diff --check` pass. Real
Chromium lifecycle, egress isolation, and production browser/submit gates stay
open under the T3.7/T3.8/T3.9 external conditions.

## 2026-10-10 post-merge and production evidence for repair AO

Repair AO was merged through PR [#16](https://github.com/dengyie/agent-fleet/pull/16)
as main revision `50c1b1d80d35c2a608572df3a521427fb10c4e53`. Main workflow
[37984348242](https://github.com/dengyie/agent-fleet/actions/runs/37984348242)
passed Linux test, release-evidence, package, and restricted deploy. Its
JUnit report records **3310 tests, zero failures, zero errors, and zero skips**;
the journey report is clean with all 29 required journeys passed. The receiver
receipt is `deployed/succeeded`, with backup
`/var/lib/agent-fleet-deploy/37984348242-5b9_jbc1`.

Independent production probes returned the same SHA from
`https://agent.mangoqwq.com/manifest.json`, `/healthz` HTTP 200, and anonymous
`/api/operator/session` HTTP 401. This closes merge/package/deploy evidence for
the capability-advertisement repair. It does not prove a real browser driver or
three-layer process/socket egress boundary; browser and submit gates remain
default-off. `MODEL-LIVE`, `MAIL-LIVE`, `NODE-LIVE`, and `DEPLOY-LIVE` external
acceptance remain `not_run`; Browserbase/credential/observation/notification
contracts and signed exact-capture source upgrade remain open.
