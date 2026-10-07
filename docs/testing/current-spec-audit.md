# 当前规格审计矩阵

审计日期：2026-10-08
审计目标主线：本地 `main` 的开发集成分支 `codex/development-completion-todo`；当前 HEAD 为 `d715e743cf176be3af52f40635703ea58270a555`，本轮验证代码提交 `88b12483691bc41c630df68d0bdce00430d87572`。两者之间只有开发计划与本审计文档变化。

本轮开发分支先 rebase 到本地 `main` 基线 `676672a`，保留 main 上已有的 29 条 journey 和审计 selector，再完成干净提交态验证。主工作树仍在受保护的 `feat/t3.7-offline-browser-boundary`；只进行本地集成，没有远端推送或生产部署。

## 证据规则

当前平台文档、已批准的 specification 和 `docs/testing/` 契约优先于历史计划中的未勾选复选框。历史计划如果明确标记为 `historical / implemented`，不重新解释为未完成任务。测试矩阵的绿色结果只证明 disposable/local boundary；真实节点、真实邮箱、真实模型和部署必须有各自的外部证据。

本次本地证据：

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

## 明确的开放项

以下不是隐藏的实现 TODO，而是需要外部条件或新批准规格的开放项：

1. 提供专用 Linux 测试节点和固定 Chromium/隔离运行时，完成 T3.6/T3.7 的 browser lifecycle、三层网络隔离、崩溃清理和不确定命令不重放验收；Darwin 的 `/proc` 两项跳过需由 Linux CI 补证。
2. 批准或修改 observation、Take Control/Return Control、SecretBroker、Browserbase 的产品参数和数据保留边界，再实现真实 driver 适配器。
3. 批准通知的触发事件、渠道、收件人、幂等、保留和外发授权，再实现事务 outbox 与投递回执；现有账号邮件不能代替业务通知规则。
4. 为 `MODEL-LIVE`、`MAIL-LIVE`、`NODE-LIVE`、`DEPLOY-LIVE` 提供各自环境、凭据和 operator approval，并留下脱敏验收证据；不能用 fixture 或本地测试替代。Komari/test-node pilot、off-host backup durability、key escrow 和生产恢复/回滚也需对应真实环境证据。
5. Exact capture 仍需 signed probe-side source/capability upgrade，并验证 AEAD、quota、retention、raw-read audit 的端到端闭环；现有 adoption API 的 `exact` 标签不代表 source 已升级。

这些开放项保持 default-off/未执行状态，直到对应规格和环境证据出现。它们不会被本地绿测或历史计划复选框自动关闭。

## TODO 归类

`docs/HANDOFF.md` §六与各历史计划状态说明优先于旧计划中的未勾选 checkbox；旧 checkbox 不自动构成当前 TODO。对 `hub/`、`tools/`、`connectors/`、`deploy/` 和 `frontend/` 的 `TODO`、`FIXME`、`XXX` 搜索没有发现实现占位符。当前仍开放的代码/验收事项仅以上述批准规格与外部条件为准；`docs/superpowers/plans/2026-10-06-development-completion.md` Task 3/4 保持开放，直至这些要求获得批准并有对应证据，不能通过删 checkbox 或缩小目标关闭。

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

当前剩余项仍未关闭：Ubuntu CI 尚未为此提交执行；pidfd 信号、Linux 安装回滚和三层浏览器网络隔离需 Linux 环境实测；浏览器观察/接管/凭据/Browserbase 与通知参数仍待 owner 批准；四项 live acceptance、Komari/test-node pilot、异地备份、key escrow、生产恢复/回滚和 signed probe-side exact-capture upgrade 仍需各自真实环境或批准契约。

尝试将功能分支推到公开 `origin` 以触发 Ubuntu 测试时，远端在更新 ref 前拒绝了包含 178 个提交的 push，提示该分支像是私有历史并要求从公开 orphan snapshot 的克隆发布；没有提交被推送。当前主检出仍干净且未变。本机没有 Docker、Podman、Lima、Colima、Multipass、QEMU 或其他可用 Linux VM。不得通过重写 orphan 历史规避远端保护；获取此 revision 的 Linux CI 证据需要先确定公开快照发布流程及允许公开的变更范围。

## 公开基线快照审查（未发布）

为准备符合远端保护要求的审阅候选，在 Codex 隔离工作树 `/Users/mango/.codex/worktrees/public-snapshot-review/agent-fleet` 从 `origin/main` 建立预览；把本地 `main` 相对公开基线的 tracked diff 应用为一个提交 `03569ba61a25d124ac994ca7321ddc9afef542ac`，其唯一父提交为公开 `f6c35f9a5f8e5e6da9ad0abaf7c37ff257b6ea58`。该候选仅存在本地，没有推送。

公开快照路径审查未发现 credentials/state/var/`.env`/hosts/private-key 路径；高熵凭据模式扫描命中 `tests/test_task_patch_bounds.py` 和 `tests/test_task_files.py` 中用于脱敏回归的相同合成 `ghp_...` 字符串。完整 Python 3.10 + Chrome 门禁为 **3080 passed、5 macOS Linux-only skips、156 subtests、29/29 journeys**，耗时 336.09 秒；唯一警告是既有 `tools/probe/discovery.py` docstring 的无效转义弃用警告。证据在 `/tmp/agent-fleet-public-snapshot-committed/`。Release validator 返回 `junit_incomplete`，原因仍为 5 个平台 skip；包归档包含 pidfd helper，解包 compileall 与使用临时测试 token 创建 Hub app 通过。该本机候选仍需 owner 批准发布及真正 Ubuntu clean CI，不能当作远端 gate 通过。
