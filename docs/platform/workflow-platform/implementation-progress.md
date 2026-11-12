# Workflow 实施进度与续接

> **当前整合状态（2026-11-10）**：此页保留源工作树的历史实现与验证证据。目标 `codex/functional-root-fixes` 的移植、架构兼容和新鲜验证单独记入[文档开发总账](../development-completion.md)。历史通过计数不能用于判断当前目标树完成；当前尚未开放 Workflow 运行入口。

维护日期：2026-11-02。开发分支 `codex/workflow-platform-docs`，独立工作树 `/Users/mango/.codex/worktrees/workflow-platform-docs/agent-fleet`。源码基线 `4cd816230fd1d8d5bf02c9f086959d8b5b1a27cf`；下列证据来自有未提交改动的实现树。主工作树的其他开发保留，本轮未提交、推送、合并或部署。

规格入口：[开发文档](README.md)、[执行契约](../../superpowers/specs/2026-10-20-workflow-execution-contract.md)、[路线图](../../superpowers/plans/2026-10-20-workflow-platform-roadmap.md)、[验收矩阵](../../testing/workflow-platform-acceptance.md)。

## 2026-11-02 独立远程固定测试能力

按用户选择的独立 capability 方案完成[专用契约](../../superpowers/specs/2026-11-02-remote-fixed-test-capability.md)
与[实施计划](../../superpowers/plans/2026-11-02-remote-fixed-test-capability.md)的本机实现。
`coding.test@1` / `tool.workflow.test` 仅运行 `coding-delivery-tests@1`，retry 为
`manual_only`。签收先于 admission/Run，原 receipt、CodeRevisionRef、subject、
generation、token 和 binding 在 Hub 入队事务及 Node preflight 再次核对。Node
通过签收 journal 的固定 descriptor 校验完整字节并流式解包；manifest 索引查询均摊
O(1)，整体 O(归档字节数 + entries × log(entries))，受既有源码包/manifest/entry
上限约束。复用原队列、worker、SQLite 事务、FixedTestRunner、SandboxBackend 和
交付发布服务；只新增跨 I/O port，不引入依赖、通用 shell、第二套收发协议或队列。

结果完整封套仍限 64 KiB，报告/JUnit/环境解码上限为 8/32/4 KiB，严格字段、base64、
hash、source/subject/recipe/executor 与 JUnit 均由 Hub 重新验证。完整失败断言报告
可使执行 succeeded、验证 failed，继续阻断交付；缺失或损坏报告不产生 passed
evidence。命令与 Node journal 使用稳定 ID，重启只查原 receipt；Hub 发布冲突最多
重试同一报告三次。取消、失租、丢回执和已入队后的不确定失败保留 unknown 与
held/isolated，不重放命令，不借用 read_only postcheck 释放原预约。

跨节点路径回归先复现了 Hub 使用本机 `sys.executable` 计算远程 argv 摘要导致合法
Node 被拒绝。现由回执携带有界 `recipe_argv`，首项必须绝对 POSIX 路径，固定尾部
逐项匹配，整个摘要须与环境一致；不同 Python 安装路径可通过，篡改参数即使重新
计算摘要也拒绝。此数组不进入请求、不改变执行，也不构成隔离证明。远程 Run
创建的原事务将 minimum reader 提高至 10，schema 保持 1；明文/加密备份保留原
绑定、预约并恢复 frozen。旧 reader 拒绝，支持 reader 10 的 Native 文本启动/备份
回归已补齐；未来 reader 拒绝夹具由 10 改为 11，活动 Workflow 仍阻止关闭域。

新鲜串行关联回归使用项目 Python 3.14，35 模块 **1272 passed，181.04秒，
零 failures/errors/skips**，JUnit 保存于 `/tmp/agent-fleet-remote-fixed-test-final-related-20261102.xml`。
其中四个新模块分别为 contract 46、Node 19、target 30、execution 20，共 115 项，
已包含于 1272，不重复相加。覆盖原本地固定测试/评审、产物接收/源码/双 Node/输入
位置、capability 路由/admission/dispatch/lifecycle、Node/runtime/poll、worker/
command reconciliation、Workflow 状态/控制/HTTP、Native/外部输入及备份。此前
reader 三模块 168-pass 与聚焦 113-pass 报告分别保留，只作诊断检查点。

Python 3.14 与现有 Python 3.10 的 `compileall -q hub tools tests` 均退出 0；没有
配置静态类型检查器，不将编译检查当作类型证明。补录前静态预检覆盖 903 份
UTF-8/no-BOM 文本、642 份 Python AST、9 份核心文档的 262 个本地链接/锚点，
1546 个去重 required selector 的实际定义及 34 个远程函数完整登记，全部通过；
`git diff --check` 无错误。最终文档补录后的复检覆盖 263 个链接/锚点，报告、文件
清单与矩阵/JUnit 摘要保存于 `/tmp/agent-fleet-remote-fixed-test-preflight-final-20261102.json`。

矩阵 NODE-01/WF-03/WF-08 现为 251/138/235 selectors，新增函数登记及全部参数实例保留；七项外部检查原样。原本机回归的 Linux availability 是替身；以下独立原生验收补齐该边界，未改变 required selectors。

新增三个 opt-in 夹具：`tests/fixtures/workflow/remote_test_launcher.py`、`remote_test_node_canary.py` 和 `remote_test_probe.py`。macOS Hub 经有界 SSH 反向转发驱动 gz-cvm 上真实独立 Linux NodeRuntime：原 capability report/preflight、HTTP artifact.receive、固定签名 Command、journal、Run worker 和 Hub evidence 事务均参与；没有替换 availability 或直接制造终态。夹具曾因 worker 测试时钟与 RunEventService 实时时钟不一致而得到 lease_lost；另补齐首次 prepare 的父目录创建。只修正夹具组装，产品租约校验不变。

冻结 revision `d78be33e3a86f9fe13644076e10100730d78de33` 的 918 个文件在 Linux 侧逐项 hash 匹配、checkout clean。四个 passed/failed/lost/unknown 场景 **4 passed、0 failures/errors/skips，29.77 秒**。每场固定 recipe 执行一次；terminal replay 的 preflight/execute 计数均为零。lost 用独立重启进程提交原 terminal journal，未重跑。unknown 原 Run/Command 均 unknown，产物及验证 evidence 为空，预约 held；其余三场完整报告/JUnit 字节经 Hub 重新核验发布，确定终态 observation 后才 released；断言失败仍是执行 succeeded、验证 failed。每个正常报告的六项 testcase 均不含 error/skip；只有 failed 场景的 test_delivery 是断言失败。

固定 recipe 内验证真实 user/mnt/pid/net namespace、capability 清零、NoNewPrivs=1、Seccomp=2、秘密不继承、scratch 可写、source/宿主不可写、宿主秘密不可读及 socket 拒绝。原有 Linux sandbox 无隔离负控制另行 fresh 运行 **2 passed，6.88 秒**，确认非隔离路径能读写宿主并连接监听端口。它们仅证明该机器和夹具 launcher 的边界，不是生产 sandbox 认证，也未关闭完整 WF-04/05。Windows 此动作仍不可用，所有 gate 默认关闭。

证据根：`/Users/mango/.cache/agent-fleet/verification/remote-fixed-test-native-ud96payd`；远端 `/tmp/afrt-ud96payd`。保留冻结 bundle/zip、918 文件摘要、原 Hub DB、Node/receive journals、每操作 stdout/stderr、JUnit 和报告完整字节。原生验收后 Linux 逐项复核摘要未变且 checkout clean。同一旧冻结原严格 Linux 全仓已结束，结果见下段；旧 904 文件/5891-pass 不代填本轮。项目 Python 3.14 四模块 fresh 回归另 **115 passed，17.98 秒**。系统 Python 3.13 的 pytest 只在 user site，sandbox 清洁 HOME 后 `python -s -m pytest` 无法导入 pytest，因而没有 JUnit；这不是 Python 3.13 语法不兼容或已证实的 64 KiB 超限。该环境诊断不替换项目虚拟环境结果。未增加依赖、安装软件、改变长期服务、删除路径或写 Obsidian；开发树未提交/推送/合并/部署。

全仓在旧冻结发现 `test_external_output_reader_fences_startup_backup_and_gate_off[10]` 失败，单项可复现 `DID NOT RAISE WorkflowRepositoryError`：产品现正式支持 reader 10，该测试仍将其当作未来版本。只把参数 `[6, 7, 10]` 改为 `[6, 7, 11]`，保留旧 reader、备份、启动和 gate-off 断言。相关 external output recovery、native text recovery、remote target 三模块 fresh **97 passed、零失败/错误/跳过，20.523 秒**，JUnit `/tmp/afrt-reader-final.xml`。该修正不在上述 d78 冻结里；旧冻结全仓与修正后完整重跑分开保留，不能拼接为通过。

原生证据补录：远端 Node/receive journal、固定源包与锁文件共 **24 文件 / 204,892 bytes**，已归档复制至本机并逐项核验大小与 SHA-256。`native-evidence-audit.json` 另对八份 Node SQLite 及四份 Hub SQLite 做只读 integrity check，核对原 terminal 结果、接收一次、报告/JUnit 原字节、test 预约与 evidence；unknown 无输出、无 evidence 且 held，其余三场 released。静态 fresh 复检覆盖 902 份选定扩展名文本、645 份 Python AST、9 文档 / 263 链接锚点及 1546 unique selectors，无错误；文本统计范围只含脚本列举扩展名，不与前轮 903 文件口径混用。Python 3.14/3.10 全量 compileall 与 diff 检查通过；报告 `/tmp/afrt-static-check.json`。

旧 d78 冻结原严格全仓在 `2026-11-01T20:36:38Z–21:04:34Z` 完成：**6009 passed、1 failed、197 passing subtests，零 errors/skips**，pytest 1674.55 秒，driver 1676.00 秒、退出 1。6010 个显式 JUnit testcase；唯一失败为上述 reader 10 夹具，40/42 journeys passed，WF-06/WF-08 因这一项 incomplete。七项 external checks 均 not_run。918 个源文件运行后摘要不变、checkout clean；包装 verifier 因 pytest exit 1 在前置断言拒绝，不存在 release-verifier.json，不记为发布核验通过。失败报告及夹具本机归档独立保留；修正树完整重跑结果如下，不拼接计数。

### 修正918文件冻结的严格Linux终态

修正 reader 未来版本夹具后的独立冻结 `2915fe6c5ca859f99650951b6e07981d1da44634`，
在 gz-cvm 新 `/tmp/afrt-vn9iy3ix` 以原严格全仓命令运行一次：
`2026-11-02T04:31:50Z–05:00:06Z`、PID 105483 / start ticks 211464361。
结果 **6010 passed、197 passing subtests、零 failures/errors/skips**，pytest
1694.22 秒、受控驱动 1696.14 秒，退出 0；JUnit 为 6010 个显式 testcase，
含 subtests 的 summary 不重复累计。**42/42 journeys、1839 selectors、44 张 PNG**
核验通过，`ci_status=passed`，七项 external checks 全部 `not_run`。
沿用 Python 3.13.16、Node 22.21.1、Playwright 1.63.0、Chromium 153.0.8010.12、
`LC_ALL=C`、原沙箱及移除 DAC override/read-search / NoNewPrivs 配置；
没有修改测试、断言、selector 或超时。918 个源码摘要前后一致、冻结 checkout clean；
结果补录前开发树亦逐项匹配。矩阵 SHA-256 为
`1edb4ec4acd110e606cfb24f4237286963b9af58269992b448ed4d21ced57e44`。

原 `tools.testing.release_evidence` 在远端和复制后的本地冻结 checkout 均退出 0，
各核验 42 journeys / 1839 selectors / 6010 JUnit cases。全部 **50 文件 /
7,986,144 bytes** 复制大小和 SHA-256 匹配；归档 5,531,169 bytes，SHA-256
`e1b1766400d0da06d519e48c7f93b449c9f3cc07814ac24d857facb3c41fcc9b`。
本机证据根 `/Users/mango/.cache/agent-fleet/verification/remote-fixed-test-native-vn9iy3ix/`
的 `full-copy/controlled-full/` 保存原日志、命令、JUnit、journeys、PNG 和远端 verifier；
`full-copy/copy-verification.json` 保存复制与本地 verifier 复核。

本轮按精确确认只删除旧 `/tmp/afrt-ud96payd/evidence/controlled-full/fixtures`：
47,057 项 / 2,004,736,351 逻辑文件字节已完整归档，归档 65,759,988 bytes /
SHA-256 `d3068c18198e1c41c65520111cf559f0de9a2544917de515769fde518cbfde7d`，
重新核验全部字节、模式、硬链接、无子挂载/进程引用后独立删除；postcheck 确认
目录外 49 项不变。启动空闲 4,793,753,600 bytes，结束仍有 2,587,959,296 bytes。
旧源码、失败报告、日志、44 PNG、journals 和运行时保持；用户随后允许本次测试 VPS
必要清理直接执行。当前成功运行的夹具与证据也保留。

门禁后仅补录文档，运行时代码、测试及矩阵与已测冻结一致。该结果闭合独立远程
固定测试的严格全仓验收；四场景原生证据仍归属旧 d78 冻结，二者不合并计数。
完整 WF-04/05、Windows 本动作、真实模型/商业 CLI、生产隔离与七项外部验收
继续开放，gate 默认关闭；整体项目尚未完成。本轮无安装、下载、长期服务或
全局配置修改，无开发提交/推送/合并/部署或 Obsidian 变更。

## 当前交付范围

F1、F2.1–F2.5 的实现已落盘，F3 已接通本机受控的 CLI 源码采集、独立命令评审、固定 pytest/JUnit 和 HTTP 人工接受；F4.1 新增 Hub V2 分片发布、崩溃恢复、配额与备份兼容，F4.2 本机 POSIX Node 已接通固定收发、独立 journal、原 ID/回执恢复及收发统一容量；F4.3 已落盘管理员能力目录、受鉴权报告、原子 admission、Node/Hub 执行前复核与协调器自动选点/输入目标固定本机子集。F5 本机边界包括只读 MCP/Run 授权固定、三工具 Fleet 出口、持久集成身份/跨进程撤销和外部 Agent Workflow 派发/原任务恢复；本地 Native 完整文本/Memory、外部已发布 ArtifactRef 输出和管理员查看/撤销亦已接通；具体限制及最新完整门禁见上方独立远程固定测试记录；后文保留历史切片。Workflow/dispatch/transfer/capability/integration gate 均默认关闭；仅显式启用才装配对应入口，adapter/模板目录默认为空。capability-bound unknown/isolated workspace digest postcheck 已实现，本机回归及实际 Windows/Linux 固定 12 项矩阵各有独立证据；原状态、binding、generation 和预约保持。2026-11-02 独立远程固定测试修正冻结 `2915fe6c5ca859f99650951b6e07981d1da44634` 的原严格 Linux 全仓以 Python 3.13.16/Node 22.21.1/Playwright 1.63.0/Chromium 153 完成：6010 passed、197 passing subtests、0 failures/errors/skips、42/42 journeys、1839 selectors、44 PNG；远端及复制后的本地原 release verifier 均通过，918 源文件摘要一致且 checkout clean。Windows 接收、固定源导出和两产物 Node 源包交接另有各自独立证据；完整 WF-04/05、获准真实 CLI/模型、生产隔离配置和真实外部试点仍待实现或验收，不能宣称生产就绪或完整交付。

| 需求 | 当前实现与边界 |
|---|---|
| 稳定身份、状态和交付投影 | 纯函数；generation 与 epoch 分离；unknown 只允许受控核对；执行完成不等于已交付 |
| 严格合同与输入冻结 | 8 步线性链、16 项引用、16/64 KiB、深度 32、重复键/非法数字/Unicode 拒绝；不可变作用域引用；解析不授予权限 |
| 持久化与 fencing | 基础 schema 3，持久 MCP 目录使用 4；Workflow schema=1/当前 reader=10，旧库最低 reader=1、capability 为 2、外部 schema 1/2/3 分别要求 3/5/6、Fleet 身份要求至少 4，Native Memory 原 Run 事务要求 7、外部 ArtifactRef output admission 要求 8，本地 Native 文本 schema2 要求 9、独立远程测试 Run 创建事务要求 10；短事务、CAS、租约、预算、槽位、intent 与外部注册原子保存 |
| 稳定执行绑定 | 异步有界 StepExecutor、intent/sending/bound 与 unknown；真实 Run/旧 Task outbox 复用；完整 legacy 上下文超过 2000 字符明确拒绝 |
| 有界协调 | 32 流程/64 动作，已有执行先对账；SQLite owner 队列、公平轮转、epoch fencing、当前证据投影；无变化 poll 不改 revision/事件 |
| 资源和生命周期 | 确定终态才释放执行槽位；无 usage 仍保留 unknown settlement；显式 start/stop/join，重启 generation/token 不变 |
| 备份与恢复 | gate 关闭也检查 schema、摘要、外键与文件归属/hash/size；历史引用按 128 行分页；明文和加密恢复默认 frozen，保留未决 reservation，恢复副本的全部集成凭据禁用 |
| 引用生命周期 | Workflow 直接及 binding Run 引用阻止永久删除会话；GC 识别输入、resolved 快照及 evidence 报告；只测试候选标记，没有删除真实文件 |
| F4.1 Hub 产物传输 | 签名 Command 与当前租约/代数绑定；默认 64 MiB、硬上限 256 MiB，1 MiB/256 片；固定 ID、恢复核对、V2 备份和只读 GC 候选；默认关闭 |
| F4.2 Node 本机收发 | 固定 grant/content 与 output_key、POSIX 固定 fd、独立收发账本、64 KiB hash/1 MiB 分片、原 ID/receipt 恢复、Node 2/Hub 每节点 2 全局 4 的统一容量；Linux 冻结全仓门禁已通过，Windows 接收句柄/reparse/ACL 已有 home-win 原生及远程 HTTP 证据；Windows 固定源导出原生/原 HTTP 已验收；实际 home-win→Hub→gz-cvm 源包交接与独立 tree/blob/固定 pytest 已通过显式试点 |
| F4.3 能力路由子集 | 固定 schema/hash、受鉴权新报告、双端策略/120 秒 freshness、原 intent/预算/槽位/binding；Node/Hub 复核、可信协调器选点与 input target/receive 同事务；完整 WF-05 和外部验收开放 |
| F5 本机集成边界 | 固定只读 HTTPS MCP、原生 Run/ToolBroker 授权摘要、持久目录及管理员审阅/撤销 UI；独立持久身份三工具 Fleet 出口与查看/撤销 UI；原 Workflow 外部派发/原任务查回、固定配置/journal 身份与精确 task/session 核对，完整有界 UTF-8 文本及显式 owner/revision Memory 固定请求；显式 export-capable adapter 支持既有 Hub ArtifactRef 的 artifact/structured/code_revision 输出，完整字节、源包与 schema 核验后在原事务绑定。本地 Native 完整文本/Memory 经原事务与当前权限复核进入模型输入；summary、二进制/代码包输入、Node 能力要求、其余集成配置和真实试点开放；Muse cancel/export unsupported，三个 gate 默认关闭 |
| 真实外部行为 | 临时库中运行 fixture CLI/评审/pytest；macOS/Linux 测试隔离已验证，获准 home-win 已运行真实 Windows 接收与远程 NodeRuntime/Hub HTTP。已在两台真实产物 Node 运行固定源包交接与独立验证；真实模型/商业 CLI、生产配置和外部集成试点仍 not_run |

## 已落盘文件

- [x] `hub/domain/workflow_errors.py`、`workflow_identity.py`、`workflow.py`：稳定错误、规范摘要/token、状态迁移。
- [x] `hub/domain/workflow_evidence.py`：当前 subject 与 validator revision 的证据、后继放行和三维交付投影。
- [x] `hub/domain/workflow_contract_types.py`、`workflow_json.py`、`workflow_contract.py`：不可变类型、有界原文解析、合同与输入冻结。
- [x] `hub/domain/workflow_records.py`：存储读取、租约、intent 和事件值对象。
- [x] `hub/domain/workflow_execution.py`、`hub/application/workflow_dispatch.py`、`hub/infrastructure/workflow_run_executor.py`：稳定 Run/Task 派发与绑定。
- [x] `hub/domain/workflow_projection.py`、`hub/application/workflow_coordinator.py`：确定性当前版本投影、有界协调与受管生命周期；`hub/bootstrap.py`、`tools/platform/assistant_worker.py` 仅提供显式启动入口。
- [x] `hub/infrastructure/workflow_schema.py`、`workflow_repository.py`：附加 schema、事务、创建/读取/claim、输入冻结和 admission。
- [x] `hub/infrastructure/platform_backup.py`：Workflow 兼容检查、引用清单和恢复冻结。
- [x] `hub/infrastructure/platform_db.py`、`hub/application/conversation_service.py`：会话引用保护和带原始 cause 的 409 错误。
- [x] `hub/infrastructure/browser_capture_gc.py`：Workflow 引用扫描与不兼容 schema 失败关闭。
- [x] `tests/test_workflow_identity.py`、`test_workflow_state.py`、`test_workflow_evidence.py`、`test_workflow_contract.py`、`test_workflow_boundaries.py`、`test_workflow_repository.py`、`test_workflow_dispatch.py`、`test_workflow_coordinator.py`：领域、真实 SQLite、文件与多进程故障证据。
- [x] `docs/testing/journeys.json`、`README.md`、`test-chains.md` 和 Workflow 文档：按当前范围登记 WF-01/02/03/07/08，不提前登记未实现能力。

## F1 / F2.1 验证记录

运行环境：macOS arm64；复用既有虚拟环境 Python 3.14.8 / pytest 9.1.1。Python 3.10.21 对新增领域及修改文件的 `compileall` 通过；这属于语法兼容检查，不是静态类型检查。没有增加第三方依赖。

相关回归 **259 passed、30 subtests passed**，耗时 4.20 秒；覆盖六份 Workflow 测试和现有 GC、明文/加密备份、会话、机器领域、defaults 与 runtime。JUnit：`/tmp/agent-fleet-workflow-20261020-focused.xml`。

全量命令复用已安装 Chrome 155.0.8059.85 与缓存 Playwright 1.62.1，设置 `FLEET_BROWSER_CHANNEL=chrome`、`FLEET_PLAYWRIGHT_MODULE` 和 `FLEET_SCREENSHOTS`，执行 `python -m pytest tests -q --require-journeys --journey-report=…/journeys.json --junitxml=…/results.xml --tb=short -rs`。仓库 CI 指定 Playwright 1.63.0，本地浏览器证据不代替指定版本的 Linux CI。

| 运行 | 观察结果 | 证据目录 |
|---|---|---|
| 首轮全量 | 3590 passed、1 failed、5 skipped、195 subtests；侧栏键盘调整尺寸断言预期 71、实际 60 | `/tmp/agent-fleet-workflow-20261020-f1-f2-1/` |
| 失败用例复跑 | `test_assistant_auth_in_real_browser` 连续 4 次通过；尚无稳定根因，未修改或放宽断言 | 同轮日志及隔离复跑终端记录 |
| 修订后的全量 | **3595 passed、0 failed、5 skipped、195 subtests**，423.18 秒；37/38 journeys 通过 | `/tmp/agent-fleet-workflow-20261020-f1-f2-1-final/` |

最终这轮 JUnit 包含 3795 个 cases（含 subtests），errors=0、failures=0、skipped=5。WF-02 的 127 个参数实例和 WF-08 的 28 个参数实例通过。矩阵 SHA-256 为 `8b8e3aa2e638187f9d7f5b9d950d3272735cb396890605a0ad7e73eaf5400f2b`，revision 为上述基线、`working_tree_dirty=true`。后续修改不能冒用这轮证据覆盖。

严格门禁命令退出 1，报告 `ci_status=failed`：RELEASE-01 仍缺 3 个 Linux pidfd `/proc` 用例；另 2 个 supervisor `/proc` 用例也因 macOS 跳过。没有把 skip 改写为通过。`MODEL-LIVE`、`MAIL-LIVE`、`NODE-LIVE`、`DEPLOY-LIVE`、`MUSE-LIVE` 仍为 not_run。

实际修复均保留先失败后通过的证据：等待态完成边、巨大整数转换错误、前驱产物类型/大小、validator 紧凑引用上限，以及 evidence 报告被 GC 错误列为候选。覆盖真实 OS 进程争抢初始化、同一租约和最后槽位；没有用两个对象冒充多进程。

## F2.2 验证记录（2026-10-21 续接）

新增 `hub/domain/workflow_execution.py`、`hub/application/workflow_dispatch.py`、`hub/infrastructure/workflow_run_executor.py` 和 `tests/test_workflow_dispatch.py`；存储补齐派发阶段、复合作用域 Run 外键与 fenced binding。原 Run worker/Task bridge 继续管理实际执行，没有建立第二套引擎。

相关回归 **348 passed、30 subtests passed**，18.17 秒，JUnit `/tmp/agent-fleet-workflow-20261020-f2-2-focused.xml`。Python 3.10 的 F1 实测 **127 passed、30 subtests passed**；修改模块的 Python 3.10 `compileall` 与 `git diff --check` 通过。新增真实进程退出、同 token 并发、上下文超限、超时/调用者取消及查询分页 VM 指令预算证据。

全量 `/tmp/agent-fleet-workflow-20261020-f2-2-final/` 为 **3620 passed、1 failed、5 skipped、195 subtests**，442.43 秒。WF-02/WF-08/WF-03 分别通过 127/29/25 个实例，38/39 journeys 通过；矩阵 SHA-256 `66c694acc713693a5d34b0b8e5daefd41268f1472486ceec6353dd8b8adda0b4`。失败发生在既有浏览器测试的 SPA history 回退：已回到 `/assistant`，但 `goBack()` 继续等待不存在的新文档 load 事件直到 5000ms 超时。

测试 fixture 改为 `history.back()` 后等待实际 hash/path，并在 resize 事件的两帧处理完成后测量尺寸；保留文档 timeOrigin、DOM 身份、草稿和几何断言。原失败 selector 新鲜复跑 **1 passed，8.74 秒**，截图目录 `/tmp/agent-fleet-workflow-20261021-browser-repair`。这次单例结果不替代修改后的全量回归；全量和 Linux RELEASE-01 门禁仍待后续验证。

## F2.3 验证记录（2026-10-21）

协调器早期单文件回归 **38 passed，2.89 秒**；当前新增边界后包含 29 个 selector，已追加到 WF-03（共 51 个 selector）。初次聚合验证 **104 passed，7.93 秒**，覆盖协调器、持久化、派发和原 Run/日程恢复。扩大相关回归 **345 passed、35 subtests passed，22.19 秒**，JUnit `/tmp/agent-fleet-workflow-20261021-f2-3-focused.xml`；该次扩大回归早于最后的 unknown/控制标志/兼容队列测试，不能替代最终树全量。

新回归先复现了 token/hash/目标漏检（4 个失败实例）、超大整数 OverflowError，以及普通失败入口可覆写 unknown 的问题，再通过修复后复跑。持久绑定写回现双检身份；unknown 仅接受显式 reconciliation，不能静默写为“从未开始”。2000 个已完成流程的索引查询验证受限于 10000 条 SQLite VM 指令；旧 schema-v1 的可选队列只在显式 init 重建，保持 frozen。

核对旧取消入口后，另两个失败实例证明 legacy Task 的 cancelled/expired 投影不能直接用作停止确证：旧代码分别显示 cancelled/failed 并释放槽位。adapter 现将它们映射 unknown/legacy_stop_unconfirmed，保留槽位；原 Task/Run 历史不改写。额外用实际 TaskService.cancel、原 bridge 和仍存活的 Python 测试子进程验证，三个边界实例 **3 passed，0.98 秒**。测试进程以关闭 stdin 收尾，没有执行真实 CLI 或节点任务。确证停止与主动取消编排仍归 F2.4。

第一轮 F2.3 全量 `/tmp/agent-fleet-workflow-20261021-f2-3-final/` 为 **3659 passed、0 failed、5 skipped、195 subtests**，420.59 秒；38/39 journeys 通过，矩阵 SHA-256 `e46a4e9f3a9a800f6d9d849812de656895b7bbc1847dbaab282a5cff54bf35d1`。这轮早于最后的 legacy 停止 guard，作为历史证据保留。之后 `/tmp/agent-fleet-workflow-20261021-f2-3-verified/` 在代码继续变化时尝试中断；仓库导入的部署信号处理器把 SIGINT/SIGTERM 转成 deployment_interrupted，造成 2 failed/1 error。该轮受代理主动信号干扰，结果无效，不能当作最终通过或产品回归结论；最终树已从新进程重新执行完整命令。

本切片不写出或采信模型自述产物。必需产物缺失则保留等待原因，当前 subject/validator 的证据决定后继，未接 F3 的动态前驱输出继续关闭。完成事实与交付投影分离；缺产物时不能显示 delivered。

本地初测使用 Apple M5 / 16 GiB RAM / macOS 26.6.2 arm64 / APFS（所在卷 460 GiB、可用 211 GiB）和 Python 3.14.8，32 个活跃流程、32 个本地 Run，不访问外部网络。共 14 次 tick，预热 4 次后 10 次稳态中位数 **216.277ms**、最大值 **320.835ms**（10 样本 nearest-rank P95）；动作预算按 32/64/63/54 后稳定 32。该测量与全量 pytest 同时运行，只是一个本机小样本，不替代生产容量结论。原始报告 `/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/fleet-coordinator-benchmark-uhpg13jd/report.json`；没有测量 RSS 或 SQLite lock wait，仍按矩阵保留后续测量。

最终树的 Python 3.10.21 语法编译与 F1 实测 **127 passed、30 subtests passed，0.18 秒**；Node fixture 语法、全部修改文档的 95 个本地链接/锚点、44 个修改/新增文件的 UTF-8 无 BOM 和 diff 空白检查通过。仓库未配置静态类型检查器，`compileall` 只证明语法兼容。

最终完整命令在新进程执行，证据目录 `/tmp/agent-fleet-workflow-20261021-f2-3-release-check/`：**3662 passed、0 failed、0 errors、5 skipped、195 subtests passed，449.76 秒**。JUnit 包含 3862 cases；WF-02 为 54 selectors/127 cases、WF-08 为 22/29、WF-03 为 51/66，全部 passed；当前协调器文件为 29 selectors/41 cases。矩阵 SHA-256 `c0bf00e30057624f4f2c2e539d7bfa6b48f128d42e8ee345eb47f0db08c21765`，revision 为源码基线且 dirty=true。`source-digests.json` 记录 27 个相关源码/测试/矩阵文件，运行结束逐项复核未改变。

严格命令退出 1，`ci_status=failed`，**38/39 journeys 通过**：RELEASE-01 的三个 Linux pidfd `/proc` selector 未在 macOS 执行；另两个 Supervisor `/proc` 用例跳过。未修改门禁、不把 skip 计为通过。最终浏览器回归通过，原 SPA 回退超时未再出现。Playwright 1.62.1 / Chrome 155 的本地证据仍不能代替仓库指定 Playwright 1.63.0 / Chromium 的 Linux CI。全部五项外部验收仍 not_run，没有提交、推送、合并、部署或真实模型/CLI/节点执行。

## F2.3 当时的下一切片（历史记录）

- [x] F2.2：`hub/application/workflow_dispatch.py`、执行器适配及 `tests/test_workflow_dispatch.py`；稳定 token、同 payload 查回、跨边界 unknown、四个故障点，完整上下文超限明确拒绝。
- [x] F2.3：`hub/domain/workflow_projection.py`、`hub/application/workflow_coordinator.py`、`tests/test_workflow_coordinator.py` 及显式 worker 生命周期；每 tick 32 workflows / 64 动作、租约回收、公平性与 stop/join。
- [x] F2.4：`hub/application/workflow_control.py`、`workflow_budget.py` 及相应测试；取消确证、输入失效、旧代回执、预留/结算与受控核对。
- [x] F2.5：`hub/http/workflow_routes.py`、HTTP/gate 测试及 config/bootstrap/web；默认全关、owner DTO、分页/CSRF/CAS。
- [ ] F3：固定代码交付模板、源码导出、独立评审/测试、人工接受、页面与真实浏览器贯穿。
- [ ] F4：通用产物传输、确定性能力路由和两个真实节点。
- [ ] F5：获准 MCP、外部 Agent 和 Fleet 窄权限出口；F6 由实际用例另立规格。

F2.4 已核对的续接约束：`PlatformRepository.cancel_run` 只保存 cancelling；`TaskService.cancel` 在控制路由前就保存 cancelled，均不能单凭返回状态证明进程退出。`UsageRepository.get_run_usage` 对缺行返回零，不能据此将未知费用结算为零；流程应读取已有 ledger 的归属和完整性，不再次调用 `settle` 重复累计实际费用。retry/invalidate 从终态恢复后须同事务刷新 owner 调度队列，旧 current generation、subject、证据和 acceptance 一并受 CAS 保护；未决旧代的 held 槽位不能因协调 epoch 变化释放。

续接时先核对分支、dirty 文件和本记录，再读执行契约对应章节。保留当前未提交实现；不得将 frozen 恢复、已有 intent 或 unknown 当作新执行授权。


## F2.4 / F2.5 与 F3 续接记录（2026-10-21）

F2.4 的 `workflow_control*` / `workflow_budget*` 已实现 CAS、持久幂等回执、暂停/继续/取消、显式 retry/invalidate/reconcile、新 generation 和旧证据失效。沿用原 usage ledger 投影，不重复计费；无 usage 保留未知结算。原 Task 增加持久 submission fence，阻断跨库取消后的迟到创建；活跃 CLI 的真实停止仍依赖具体 Supervisor 适配，未确认时保留 unknown。

F2.5 新增 operator DTO、模板目录、严格 HTTP 入口、配置 gate 与 bootstrap。修复 retry 缺少 expected_generation 字段；reconcile 在仓库事务内解析当前 generation，避免 HTTP 先读后写改变幂等内容。详情采用显式 DTO；当前证据来源/报告 artifact/hash/size/content_digest 纳入 delivery_digest。事件 HTTP 投影去除 dispatch token、binding 和 attempt 内部字段；超过 JavaScript 安全整数范围的预算总额转十进制字符串。默认目录为空，不自动登记或授权执行模板。

F3 已落盘的完整文件：`hub/domain/workflow_delivery.py`、`hub/infrastructure/workflow_delivery_repository.py`、`hub/application/workflow_coding_delivery.py`、`tools/platform/workflow_source.py`、`tools/platform/workflow_validators.py`、`frontend/assets/views/assistant/workflow.js` 和相应测试。代码版本引用保存为 JSON artifact，源码包另列 source_bundle；测试必需输出包含 report 和 JUnit。应用层通过存储及产物 I/O port 编排；提交绑定当前 generation、CAS revision、真实 execution binding。产物引用、evidence、revision、事件及幂等回执同事务写入；重复报告先查回原 artifact，不重新发布。

源码包只从固定 Git commit/tree 导出；包含有界 tree 清单，排除凭据与未提交工作，解包核对 tree、blob、包 hash/size 并拒绝重复成员/链接/越界路径。评审报告核对 adapter 提供的实际身份，开发者不能给自身交付生成独立评审证据。固定测试器默认 SandboxBackend，缺 launcher 拒绝执行；测试进程复用有界 backend，报告及 JUnit 相互核验。DirectoryBackend 的测试只证明配方与进程行为，不是隔离验收证据。

界面复用现有工作面板，默认保留产物页。只有 defaults 明确返回流程 gate 与账号作用域后才挂载；选择/事件游标与未决操作按作用域恢复。过期详情响应不覆盖当前选择；丢响应重试保持原 token/body，409 刷新后要求重新确认。支持创建已登记模板、步骤控制、文件下载链接、三维结果、预算和人工接受。源码/评审/测试的真实执行适配尚未完成，页面可用不能替代完整交付。

本轮局部验证：F2.4/F2.5 联合曾 88 passed；重构后的交付与 HTTP 68 passed；源码与前端契约 16 passed；HTTP 与前端 46 passed。扩大相关回归 487 passed、30 subtests，22.74 秒，JUnit `/tmp/agent-fleet-workflow-20261021-f3-foundation.xml`。该轮之后增加了动态输入解析，不能借此覆盖后续改动。

浏览器新鲜验证：`test_workflow_panel_in_real_browser` 两个 gate 场景，加既有 workspace/auth 五个场景，共 7 passed，27.66 秒；覆盖真实 Flask/SQLite、默认关闭无流程请求、原默认导航、刷新恢复、409 处理、丢响应幂等重试、取消、390px 与键盘关闭。本机 Playwright 1.62.1 / Chrome 155，不代替 CI 指定版本与 Linux 门禁。尚未运行本轮最终全量；上文 F2.3 全量仅是历史证据。真实模型/CLI、sandbox、跨节点、MCP、外部 Agent 均为 not_run。

## F3 源码与界面边界修复（2026-10-21 续接）

新增 `hub/infrastructure/workflow_input_resolver.py` 与测试：以当前 revision 有界读取前驱输出，按哈希索引关联；只把已成功当前代的 code_revision JSON 解析为真实固定引用，冻结前重新核验 source_bundle 归属/hash/size。没有 memory/context 解析授权时明确拒绝，不用模型描述补造输入。

先失败后修复的生命周期与文件证据：取消 source exporter 的同步 artifact 发布时，旧实现先删除临时源文件；`tools/async_io.py::run_owned_io` 现等待已开始的有界 worker 收尾，再传播原取消，重复取消不提前释放资源，worker 失败保留 cause。交付服务、源码发布、固定测试及 JUnit 读取复用该原语。`tests/test_async_io.py` 与相关源码/交付/解析回归当时 **51 passed，6.70 秒**，JUnit `/tmp/agent-fleet-workflow-20261021-owned-io.xml`；后续增加的用例不由这轮覆盖。

凭据测试先证实 `.git-credentials`、`.ssh/config`、`credentials.json`、`secrets.yaml` 与常见私钥进入旧包；现扩展已知文件名/目录排除，不声称源码内容绝对不含秘密。另一真实目录竞态先证实 `Path.rename` 会覆盖检查后新建的空目录；新 `tools/atomic_directory.py` 提供系统排他 rename，源码解包与原 backup 共用，竞争目标保留。源码及明文/加密 backup 回归 **37 passed，3.75 秒**。没有执行用户目录删除；Windows 仍缺实机证据。

HTTP 创建回执增加授权撤销恢复：已有 owner/client_token 先通过原同 payload 检查查回，新建继续核验当前输入权限和 admission。模板 DTO/前端合同新增固定 `input_refs`；界面显示已登记输入摘要，创建保留精确引用，不提供任意输入编辑器。前端严格拒绝非法 Git 对象、重复/额外字段和不安全整数；HTTP 与 frontend/repository 回归曾 **76 passed，2.98 秒**，其后非空模板 HTTP/前端/浏览器回归 **50 passed，6.78 秒**。

真实 Cookie 浏览器增加两个 operator 及普通账号：跨 owner 读写 404、匿名 401、普通账号 403/无流程页；不扩大原 ACL。第一账号丢响应的未决操作不泄露到第二账号。另覆盖固定输入创建、恶意 reason 文本、迟到旧详情、390px 与 Escape；截图检查发现 cancelled 仍显示停止中，先增加失败断言后修正。最新三个浏览器场景 **3 passed，6.23 秒**，截图 `/tmp/agent-fleet-workflow-20261021-browser-inputs/workflow-panel.png` 已人工核对。真实 HTTP 事件及固定 CodeRevision 模板的 Node 契约测试 **3 passed，0.49 秒**；没有用纯手写 JSON 代替真实事件。

WF-02/08/03 当前登记 73/32/71 个 selector；F3 局部 WF-01 登记 34 个、预算 WF-07 登记 14 个。以上是 selector 数，参数实例数以新鲜完整报告为准；登记不代表门禁通过。

扩大相关回归 **639 passed、35 subtests passed，33.67 秒**，JUnit `/tmp/agent-fleet-workflow-20261021-f3-boundaries.xml`。Python 3.10.21 `compileall hub tools` 通过；领域/源码/有界 I/O 实测 **138 passed、30 subtests passed，2.02 秒**，JUnit `/tmp/agent-fleet-workflow-20261021-py310-boundaries.xml`。其间 I/O 取消测试在 Python 3.10 先失败：Task 会增加一层 `CancelledError.__context__`，原始 I/O cause 仍完整保留。测试现验证完整异常链，保留 worker 收尾与同一原错误的断言；Python 3.14 对应两项也已新鲜通过。未为兼容测试而修改生产取消语义。

完整门禁 `/tmp/agent-fleet-workflow-20261021-f3-boundary-gate/`：**3815 passed、0 failures/errors、5 skipped、195 subtests passed，465.47 秒**；JUnit 4015 cases。WF-02/08/03/01/07 分别通过 174/42/94/51/14 cases。600 个源码/测试/矩阵文件的 digest 在运行后逐项核对未变化。矩阵 SHA-256 `209ad485951a621af1138444b59b7b99523a3fd116e2b14650fe9c7a183c2822`，基线 revision、dirty=true。严格退出 1、`ci_status=failed`、40/41 journeys：RELEASE-01 缺 3 个 Linux pidfd `/proc` 实例，另 2 个 Supervisor `/proc` 用例跳过；没有放宽门禁。本机无已配置的 Docker/Podman/Linux 测试 runtime，不把本机结果代填 Linux CI。

随后单独复现回执查回的身份遗漏：相同 token/payload 改 generation、execution_id、kind、run_id 或 target_key，旧实现仍返回原 receipt；五个新参数实例均先失败。请求摘要现固定完整执行作用域，五种错配返回 idempotency_conflict；源码/交付/解析/I/O 的 **58 passed，8.53 秒**，JUnit `/tmp/agent-fleet-workflow-20261021-delivery-scope.xml`。前一全量早于此修复，最终树门禁须重新运行。Node 语法、96 个本地文档链接/锚点、92 个修改文件 UTF-8 无 BOM 及 diff 空白检查已通过；最终文档新增记录后仍需重检。

F3 仍需完整文件：CLI Task 完成后的固定源码采集、可信只读评审执行 adapter、固定测试与原 Run/Command 的持久绑定、完整 `tests/test_workflow_e2e.py`。长执行必须复用原生命周期，不能塞入协调器短 tick 或另建执行引擎。真实 sandbox/CLI 试点、Linux 门禁、F4/F5 仍未完成。

## F3 接线前的完整树验证（2026-10-21）

回执完整身份修复后的完整门禁在 `/tmp/agent-fleet-workflow-20261021-f3-foundation-final-gate/` 完成：**3820 passed、0 failures/errors、5 skipped、195 subtests passed，429.67 秒**；JUnit 4020 cases。WF-02/08/03/01/07 分别为 73/32/71/34/14 selectors、174/42/94/56/14 cases，全部通过。600 个源码/测试/矩阵文件在结束后逐项校验摘要，均未变化。矩阵 SHA-256 `aed3b7ab3b1f35f8a6d2788a85154f2d818bd9f8e60b249cbb40f33b967f51ce`，revision 为上述基线，dirty=true。

严格退出 1、`ci_status=failed`、40/41 journeys：macOS 未运行 RELEASE-01 的 3 个 Linux pidfd `/proc` 用例，另有 2 个 Supervisor `/proc` skip；没有放宽规则。五项外部检查仍 not_run。这轮验证是后续固定测试执行接线前的 checkpoint，不能覆盖后续代码。

同一回执修复树的 Python 3.10.21 扩大回归为 **460 passed、30 subtests passed，42.42 秒**，证据 `/tmp/agent-fleet-workflow-20261021-py310/results.xml`。覆盖 Workflow、实际 pytest/JUnit、浏览器、SQLite、明文/加密备份、原 Run/Task bridge、取消与 usage。临时环境 `/tmp/agent-fleet-py310-workflow-20261021` 仅安装已有项目依赖，未改变全局环境或 requirements；没有新增项目依赖。

## F3.1a 固定测试的持久执行接线（2026-10-21）

`RegisteredRunTarget.test_recipe` 只接受精确整数 revision 的 `coding-delivery-tests@1` 和固定 test 合同。新 `hub/application/workflow_test_execution.py` 接入原 `LocalRunWorkerService`：原队列、Run lease、heartbeat、取消、scheduler 和崩溃 unknown 规则全部复用。协调器不运行测试，未建立第二个执行引擎。Workflow gate 关闭时无新增执行；启用后也须显式启动原 worker，模板目录仍默认为空。

`DeliveryRunLease` 和存储的索引查回核对 owner/workspace、Run/worker/lease/attempt、完整 binding 与当前 generation；bound 保存前可安全回队，run_started 后不自动重放。执行使用私有源码副本，固定 pytest/JUnit 配方不调用模型、不读取用户工作目录、不继承 Hub 环境。默认 SandboxBackend 缺 launcher 仍拒绝执行；DirectoryBackend 只用于契约测试。

当前源码、评审 evidence 和报告文件的 hash/size 通过后才启动。步骤截止在启动、进程控制和提交时检查；取消、失租或截止必须先回收进程组。静默输出、关闭 pipes 和回调抛错都有真实子进程证据。产物/evidence/receipt 的同事务写入再检查 Run lease/attempt、取消、工作区和源版本。CAS 与丢响应最多重试发布三次，不重跑 pytest；已提交 receipt 查回原 expected_revision。配方正常结束但断言失败记执行成功、validation failed；未确认发布保留 unknown。

`workflow_validators.py` 报告新增环境摘要：明确 scope=adapter_host，记录主机 OS/architecture/Python、backend、固定 argv 与 launcher 摘要，不声称 sandbox 内部环境或隔离已验收。报告字段严格拒绝 bool/float 代替整数或数字代替布尔值，并校验环境和 JUnit。先失败的证据包括四个配方类型/持久 marker/过期执行实例，以及五个报告标量类型实例；I/O helper 的可调用对象参数改为 positional-only，避免与目标函数的 operation keyword 冲突。

已完成文件：`workflow_delivery.py`、`workflow_delivery_repository.py`、`workflow_coding_delivery.py`、`workflow_run_executor.py`、`workflow_templates.py`、`workflow_test_execution.py`、`workflow_bootstrap.py`、原 `run_worker_service.py`、`workflow_validators.py`、`bounded_process.py`、原 Directory/Sandbox backend、`async_io.py` 及相应测试/矩阵。点查复用现有索引；源码与归档检查 O(n)，每次读取上限和发布重试次数固定，没有增加项目依赖。

扩大回归阶段 **248 passed，28.06 秒**，`/tmp/agent-fleet-workflow-20261021-fixed-test-related.xml`；补齐运行中截止与当前评审引用查询后的聚焦回归 **98 passed，17.12 秒**，`/tmp/agent-fleet-workflow-20261021-fixed-test-focused-final.xml`。随后 Python 3.10 扩大回归 **499 passed、30 subtests passed，46.34 秒**，`/tmp/agent-fleet-workflow-20261021-fixed-test-py310/results.xml`；该轮早于运行中授权撤销修复。

生命周期回归先复现四项失败：流程取消、工作区禁用、frozen 和当前 generation 失效后，子进程仍运行到结束；证据 `/tmp/agent-fleet-workflow-20261021-test-revocation-red.xml` 为 4 failed、2 passed。新增 `check_run` 通过固定数量的索引记录检查当前授权，不解析完整 Workflow 或重复读取产物；原进程控制回调在解包后、启动前、运行中和执行后检查，先回收进程组再传播取消或保留 cause 的类型化错误。普通 revision 更新不阻断同一绑定，存储读取不确定性保守记 unknown。

原失败 selector 新鲜复跑 **6 passed，3.60 秒**；补齐解包竞态、普通 revision 更新和读取错误链后的聚焦回归 **98 passed，22.60 秒**。再补 binding、resolved digest、步骤状态和失租的真实子进程停止证据，Python 3.10 扩大回归 **505 passed、30 subtests passed，53.57 秒**，`/tmp/agent-fleet-workflow-20261021-fixed-test-revocation-py310/results.xml`。Python 3.10 compileall、当前 WF selector 的 AST 校验、99 个修改/新增文件 UTF-8 无 BOM 和 diff 空白检查通过。

运行中撤销修复树完整回归 `/tmp/agent-fleet-workflow-20261021-fixed-test-final-gate/`：**3868 passed、0 failures/errors、5 skipped、195 subtests passed，459.53 秒**；JUnit 4068 cases，602 个源码/测试/矩阵文件结束后摘要未变。WF-02/08/03/01/07 为 73/32/85/45/14 selectors、174/42/125/73/14 cases，全部 passed。矩阵 SHA-256 `19a09beed5f44ff23cb7697f17c8a5a47a2caabb1793894cd6b6b0b12970afe4`。严格退出 1、ci_status=failed、40/41 journeys：RELEASE-01 缺 3 个 Linux pidfd `/proc` 用例，另 2 个 Supervisor `/proc` skip，外部检查仍全部 not_run。这轮为以下完成/取消竞态修复前的 checkpoint。

收尾竞态另有 **5 failed、3 passed** 的先失败证据 `/tmp/agent-fleet-workflow-20261021-test-completion-race-red.xml`：报告提交后，丢响应重试遇到工作区撤销/frozen/流程取消会误记失败；原 Run 终态写回也会把已完成的固定测试覆盖为 cancelled。发布现先用原完整 target 查回 receipt，只在未提交的 revision 冲突时刷新；仍最多三次、不重跑。Run 终态写回继续要求有效 lease，并在同一事务核对固定 recipe、完整 dispatch binding 和持久 test receipt，才允许保留 succeeded。未提交或 marker/binding 不匹配继续按取消收尾。原失败与 CAS 边界复跑 **10 passed，4.67 秒**；包含原 Run worker/scheduler 和提交审批的相关回归 **119 passed，28.65 秒**。

真实 SQLite 异常注入又证明原 `RunEventService.state` 丢失存储 cause，现与 `finish_run` 一并保留完整异常链并验证事务回滚。Python 3.10 首轮扩大回归为 1 failed、543 passed、30 subtests，失败是新测试误从隐藏租约的公开 Run DTO 取 lease_id；修正为事务内直接读取 state/lease_id 后，新鲜扩大回归 **544 passed、30 subtests passed，56.93 秒**，证据 `/tmp/agent-fleet-workflow-20261021-fixed-test-completion-py310-verified/results.xml`。Python 3.10 compileall、100 个修改/新增文件 UTF-8 无 BOM、当前 WF selector AST 与 diff 空白检查通过；13 份 Markdown 的 97 个本地链接/锚点有效。最终完整门禁与摘要核对待补充。

收尾修复树第一次完整门禁 `/tmp/agent-fleet-workflow-20261021-fixed-test-completion-final-gate/`：**3881 passed、1 failed、5 skipped、195 subtests passed，476.05 秒**。602 个源码/测试/矩阵文件及 6 份核心契约摘要未变；WF-01/02/03/07/08 全部 passed，WF-03 为 88 selectors/139 cases。SESSION-01 的既有 `test_large_stderr_is_drained_and_failure_is_not_hidden` 测得峰值 10,271,299 bytes，超过原 2 MiB 上限，严格报告为 39/41 journeys、ci_status=failed。RELEASE-01 仍缺 Linux 专属用例。

该内存 selector 独立复跑通过；按执行邻接顺序复跑 **79 passed，28.78 秒**；完整收集后只运行该 selector 为 **1 passed、3886 deselected，3.41 秒**。临时分配探针位于仓库外，不改变生产代码和原断言；同一进程内重复 **10 passed、60 deselected，5.36 秒**，峰值为 502,346–504,193 bytes，采样时仅 MainThread。尚未稳定复现或归因此前 10 MiB 峰值，不能据此声称修复了读取器。

带分配探针的完整诊断回归 `/tmp/agent-fleet-workflow-20261021-fixed-test-completion-diagnostic-gate/` 为 **3882 passed、0 failures/errors、5 skipped、195 subtests passed，450.90 秒**；JUnit 4082 cases。602 个源码/测试/矩阵文件、6 份核心契约及临时探针的摘要结束后逐项核对未变；矩阵 SHA-256 `0ba5b30aa7b2653e5ee48a396794841d2ca7fd16e92e02401a1f3f3cc2bf09c0`，基线 revision/dirty 状态同上。WF-02/08/03/01/07 分别为 73/32/88/45/14 selectors、174/42/139/73/14 cases，全部 passed。

这一轮内存目标用例峰值 **502,090 bytes**；当时另有 transcript-retention 和两个 HTTP process_request_thread 存活。该线程清单仅说明全局 tracemalloc 的采样环境与隔离进程不同，不能证明此前峰值由某个线程引起。探针只附加在该 selector 上，原 2 MiB 断言和生产读取器保持原样；该轮属于诊断配置下的完整回归，不改写前一标准配置的失败历史。性能峰值波动仍为未归因风险，后续须保留分配栈证据，不能通过提高阈值或忽略 selector 关闭问题。

严格命令仍退出 1、ci_status=failed、40/41 journeys：RELEASE-01 的 3 个 Linux pidfd `/proc` 用例未在 macOS 执行，另 2 个 Supervisor `/proc` skip；全部五项外部检查仍 not_run。Playwright 1.62.1 / Chrome 155 的本机证据仍不代替 Linux CI 指定的 Playwright 1.63.0 / Chromium。当前 F3 仅完成固定测试 Run 接线及其恢复/撤销边界，CLI 源码、可信只读评审、真实 sandbox 和完整贯穿仍未完成；没有提交、推送、合并、部署或执行真实模型/CLI/节点任务。

### CLI 源码接线前的历史约束（已由下节接线）

- `tools/agent-runner.py` 的 `run_task` 和 `run_task_managed` 在 disposable worktree 清理前只收集有界文本、diff 和测试摘要；尚无固定 CodeRevision/source_bundle 回执。
- `tools/result_files.py` 的文件快照最多 16 项、每项 16 KiB/400 行，还会截断、脱敏且拒绝二进制；不能把它或 diff 拼成已校验的固定源码包。
- 当前 runner 在 adapter 返回后先停止 heartbeat，再采集结果。新增源码导出/回传必须继续绑定原 Task attempt/nonce、心跳、取消和截止；不能在失租后发布源码，不能阻塞 coordinator tick。
- legacy Task 与 platform Workflow 分属两个数据库。接线须保留原 outbox/幂等和结果 fencing，最终输出仍由当前 generation/binding/source 的短事务提交。
- `tools/worktree.py` 的 diff 采集会对 disposable worktree 执行 git add；固定源码只能来自该任务拥有的隔离工作树，不能借用用户主工作树或从模型 summary 推断 head/tree。


## F3.1b/c 源码采集、独立命令评审与本机交付链（2026-10-21）

已完成的完整文件包括 `tools/runner_source.py`、`tests/test_workflow_runner_source.py`、`tests/test_workflow_task_recovery.py`、`tests/test_workflow_source_http.py`、`hub/domain/workflow_review.py`、`tools/platform/workflow_review.py`、`hub/application/workflow_review_execution.py` 和 `tests/test_workflow_review_execution.py`。同时更新原 Runner/Transport/Worktree、Task/bridge、源码 HTTP、交付 repository、Run target/worker、bootstrap、终态写回和固定模板校验。无新增项目依赖。

源码采集遵循产物契约 §8.2：仅可信 develop Run 的来源可取得采集 policy；原 Task attempt/nonce 与原截止贯穿 CLI、源码及结果回传。普通 Runner/Supervisor 均在 disposable worktree 清理前固定输出，私有临时 index/commit 不移动用户 HEAD 或 index。私有缓存绑定完整身份，保存原成功结果、固定包与回执；崩溃或丢响应仅恢复同 attempt，不自动重跑 CLI。HTTP 按块核对精确长度/hash，source receipt 必须先于成功 result；双库提交窗口只补原 receipt。Workflow Task 失租保留 unknown，不使用旧 Task 的直接 retry/pause/continue 绕过 generation。旧完整 control schema-v1 可读取，新增 source 表只在显式 init 补齐。

真实失败证据包括 Task/Workflow 双库提交中断、同包并发/异包冲突、最终提交前撤权、父目录被换成 symlink 的文件竞态，以及 artifact 竞争空目录被覆盖。现通过原事务 fencing、排他发布和逐级目录句柄处理。另三个失败实例复现过期/永久拒绝/损坏的 pending 每轮重试；现保留原文、缓存和错误码到私有 reconciliation 目录，临时故障仍须确认原租约才能重传。旧通用 pending 扫描仍使用全量目录排序；本切片没有完成全局 spool 的容量/公平性改造。urllib 阻塞响应读取也未被改写为严格绝对截止，当前 deadline 约束重试/fallback 的剩余预算和后续尝试。

评审按产物契约 §8.3 使用原 Run 队列/lease/heartbeat/取消/unknown。纯领域 ReviewIdentity 与 adapter 快照固定 id/revision、实际执行/model 身份和 argv/launcher/timeout 摘要；未登记、配置变化、开发者自评、无 launcher 均失败关闭。命令只读取私有固定源码与有界 request JSON，stdout 严格校验报告，stderr 分开限额，不继承 Hub 环境或模型凭据。运行前后 O(n) 核对文件集合/权限/hash，拒绝内容、新文件或链接变化；此核对不能证明运行期间从未临时改写源码。运行中按索引核对授权/绑定/取消/截止，先回收实际进程组再返回；只重试同一报告发布，持久 review receipt 决定完成与迟到取消的先后。

`test_real_runner_http_capture_review_test_and_acceptance` 的四个参数实例覆盖普通/Supervisor × 正常/丢失 result 响应。fixture CLI 实际生成源码和测试文件，经 Flask source/result routes、原 coordinator 的动态输入冻结与派发、独立评审命令和真实 pytest/JUnit，最后用当前 delivery_digest 执行 HTTP 人工接受；三步 subject 相同。这是本机临时 SQLite/Git/子进程证据，HTTP 使用 test_client，评审 launcher 和测试 DirectoryBackend 不证明生产隔离、商业 CLI 或真实模型。

macOS 专用 `test_review_with_real_macos_sandbox_denies_source_host_and_network_writes` 另外执行真实 sandbox-exec profile：允许登记 Python 运行时、固定源码/control 和私有 scratch；断言源码/请求写入、已知宿主探针文件读取/写入及真实 loopback listener 连接全部被拒绝，scratch 写入成功。初次 profile 的 Python 启动被 SIGABRT 终止；最小重现定位到运行时祖先目录读取，增加精确 literal 目录规则并以 Python -I 执行后通过，未开放源码/宿主写入或网络。此项仅证明当前 macOS/Python/profile 的具体探针，不是 Linux/Windows、固定测试 sandbox 或生产配置验收；不放入所有平台均 required 的 journey。

本轮先收齐上一会话 **379 passed，34.95 秒**；源码 pending 修复后相关 **113 passed，18.78 秒**。评审与原固定测试/派发 **152 passed，40.74 秒**；新增源码目录句柄后评审 **32 passed，14.12 秒**；补齐事务/身份/bootstrap 后扩大相关 **254 passed，68.50 秒**，JUnit `/tmp/agent-fleet-workflow-20261021-source-review-focused.xml`。这轮早于最后的 macOS profile 和记录 fsync 改动，最终树证据另列，不能据此宣称当前全量通过。

当前 WF-01/02/03/07/08 登记 61/73/126/14/32 个 selector；全部参数实例和平台门禁以新鲜报告为准。下一工作保留：获准真实 CLI/model 与仓库/workspace 登记、固定测试 sandbox 的文件/网络/凭据验证、账号创建到最终接受的获准外部贯穿、Linux RELEASE-01、Windows 实测、F4/F5。没有提交、推送、合并、部署或调用真实模型/商业 CLI/远程节点。

### F3.1b/c 最终树验证记录

2026-10-21 本机完整运行得到 **3995 passed、5 skipped、195 subtests passed，513.48 秒**；严格 `--require-journeys` 退出码为 **1**，40/41 journeys 通过。五个 Workflow journey 全部通过：WF-01 61 selectors / 117 cases，WF-02 73 / 174，WF-03 126 / 206，WF-07 14 / 14，WF-08 32 / 42。RELEASE-01 的三个必需 Linux pidfd selector 在 macOS 跳过，故发布门禁不通过；另有两个既有 `/proc` 测试跳过。没有放宽 selector 或跳过判定。

证据目录 `/tmp/agent-fleet-workflow-20261021-source-review-gate/` 保存配置、JUnit、journeys、浏览器截图和 589 项源码/契约摘要。测试结束后逐项重算全部摘要，无变更或缺失。报告基线 `4cd816230fd1d8d5bf02c9f086959d8b5b1a27cf`、dirty=true；矩阵 SHA-256 `183a0f9c9358b5332afad0499dd987eb672e7d67189b19638aed12419c6ad838`。本机 Python 3.13.7、缓存 Playwright 1.62.1、Chrome 155.0.8059.85；不能据此替代 CI 的 Playwright 1.63.0/Chromium/Linux 环境。五项真实外部检查均为 `not_run`。

同一最终代码树在 Python 3.10.21 的聚焦兼容回归 **255 passed，63.68 秒**，JUnit `/tmp/agent-fleet-workflow-20261021-source-review-py310-verified.xml`。首次兼容运行有 1 failed / 254 passed，定位到 macOS profile 使用虚拟环境入口需要读取 `pyvenv.cfg`；改用登记解释器的实际路径与 `-I` 后通过，未扩充源码/宿主写入或网络权限。Python 3.10/3.13 `compileall`、306 个 WF selector AST 校验、115 项修改/新增文本的 UTF-8 无 BOM、13 份 Markdown 的 97 个本地链接/锚点及 `git diff --check` 已通过。这些记录只适用于上述摘要的树；后续切片另行记录新鲜证据。


## F3.2/3 浏览器、Cookie HTTP 贯穿与固定测试隔离（2026-10-22）

本切片复用 Flask、静态 SPA、SQLite、原协调器/Run worker/Runner/Transport 与现有模型 provider，没有新增依赖、执行队列或 UI 框架。文档基准为执行契约 §9.1、产物契约 §8.1/8.3 和路线 F3.2/3。读取错误使用固定三项 Map，查找/清除 O(1)；焦点恢复扫描有界 DOM，源码/下载完整性校验 O(n)，HTTP 和子进程均有界关闭。

完整文件：更新 `frontend/assets/views/assistant/workflow.js`、`frontend/assets/styles/assistant.css`、`tests/test_workflow_browser.py` 和 `tests/fixtures/frontend/workflow_browser.cjs`；新增 `tests/fixtures/frontend/workflow_states_browser.cjs`、`workflow_evidence_browser.cjs`、`tests/test_workflow_e2e.py`、`tests/test_workflow_sandbox.py`、`tests/fixtures/workflow/cli_developer.py` 与 `model_reviewer.py`。

真实浏览器先复现两个问题：轮询清空重建详情导致键盘焦点丢失，列表 503 后恢复仍显示旧读取错误。现用稳定 workflow/step/generation/action key 恢复有效控件焦点，并将列表/详情/模板读取错误和未决写回执分开；读成功只清理对应错误，原未决 token/body 不变。390px 验证 empty/read recovery、waiting_node、waiting_external、unknown、cancelling、awaiting_acceptance→accepted、接受冲突后人工重确认、刷新、键盘 Tab/Home/End/Enter/Escape 与无 Cookie/账号隔离。实际 Git/pytest/JUnit 产物和恶意名称/报告经 HTTP 下载并重算 hash；负面评审不能测试或接受。评审正文按下载内容检查，不宣称页面已经提供 Markdown 评审预览。

`test_cookie_http_runner_model_review_and_human_acceptance` 的正负两例从真实账号 login/Cookie 到 create、原 Runner poll/heartbeat/source/result、CLI 子进程、动态协调、命令评审、项目真实 OpenAICompatibleProvider、严格 loopback HTTP 模型对端、实际 pytest/JUnit、逐项下载和人工接受；重复创建与 exclusive CLI trace 阻止静默重跑。对端独立检查公开字段、模型/身份/source hash，不使用被测 serializer 构造预期；负面 verdict 阻断测试和接受。合成凭据仅用于临时 loopback；生产 Runner HTTPS 配置规则未改。评审 launcher 仅进程转发，CLI/模型均受控 fixture，不证明生产隔离、外部模型质量或商业 CLI。

macOS 固定测试用真实 sandbox-exec 执行原 leased worker，允许登记 runtime/pytest、私有工作目录和 /dev/null，拒绝已知宿主合成文件读写及 loopback 连接，HOME/TMPDIR scratch 可写且不继承 Hub 合成凭据；无隔离对照必须生成 failed 证据。新增完整 HTTP 链将同一 profile 用作固定测试 backend，并核对下载报告的实际 backend/launcher 摘要；该链仅固定测试进程被此 profile 隔离，评审/开发仍是 fixture。两个平台专用 selector 不注册为跨平台 required，也不证明 Linux/Windows 或生产配置。

验证中的 fixture 修正：首次浏览器证据定位器同时匹配摘要/多个证据，收窄至摘要后通过；HTTP create 的既有状态为 201，修正新测试原先误写的 202。sandbox 初始启动因可信 pytest 路径和 /dev/null 权限失败，明确 runtime PYTHONPATH 和 exact literal 后通过；无隔离对照最初误用 Path.home() 写入本机 scratch，核对其为本次合成内容后移动至 `/tmp/agent-fleet-workflow-20261021-ui-sandbox/unconfined-control-scratch`，改为当前临时目录。没有删除用户文件。新增 HTTP sandbox 报告断言首次 2 failed/3 passed：报告使用完整 backend 类名，按原契约修正测试后 5 passed/6.95 秒，证据 `http-sandbox-verified.xml`。

本轮浏览器/前端/HTTP/sandbox 聚焦 18 passed/27.99 秒，证据 `/tmp/agent-fleet-workflow-20261022-final/focused.xml`；它早于新增整链 sandbox 断言。最终代码树的兼容回归与完整门禁另列，不复用此前 3995 passed 的旧树证据。WF-01/02/03/07/08 当前登记 62/73/126/14/34 selectors；macOS 本机隔离探针不加入 universal required。

后续范围保留：获准真实 CLI/model 与仓库/workspace 登记、生产隔离配置、Linux RELEASE-01、Windows、F4 分片产物/节点接收/路由与 F5 集成。当前只接通本机受控链；F3 总退出仍未完成。没有提交、合并、推送、部署或启动外部真实任务。


## 2026-10-22 最终树完整门禁

在文档、测试和当前代码冻结后运行：

```text
PYTHONPATH=. python3 -m pytest tests -q --require-journeys \
  --journey-report=/tmp/agent-fleet-workflow-20261022-final-gate/journeys.json \
  --junitxml=/tmp/agent-fleet-workflow-20261022-final-gate/results.xml
```

结果：**4008 passed、5 skipped、195 subtests passed，506.50 秒**。WF-01/02/03/07/08 全部通过，分别为 62/73/126/14/34 selectors、119/174/206/14/50 cases；新增浏览器和 HTTP/sandbox 链均在 fresh tree 报告中。严格 journey 状态仍 `ci_status=failed`、40/41：RELEASE-01 的 3 个 Linux pidfd/`/proc` selector 在 macOS skip，另 2 个既有 Supervisor `/proc` selector skip；没有修改 required 判定或把 skip 计为通过。报告为 `/tmp/agent-fleet-workflow-20261022-final-gate/results.xml` 与 `journeys.json`。

门禁前冻结配置：基线 revision `4cd816230fd1d8d5bf02c9f086959d8b5b1a27cf`、dirty=true；Python 3.13.7、macOS 26.6.2 arm64、Playwright 1.62.1、Chrome 155.0.8059.85；矩阵 SHA-256 `2bebe5e11bd4f6b9d6df782eb0aec3993898c408d4344945fe410dc0cf19afcb`；606 个源码/测试/核心文档摘要在运行后全部一致、无缺失，证据 `/tmp/agent-fleet-workflow-20261022-final-gate/source-digests.json` 与 `configuration.json`。该环境不能替代 CI 的 Python 3.10/Playwright 1.63.0/Chromium/Linux，也不能替代真实外部模型、商业 CLI、生产 sandbox、Windows 或远端节点。

兼容回归在 Python 3.10.21 为 143 passed/72.17 秒，覆盖新增浏览器、严格 loopback HTTP、Runner/provider、固定测试 sandbox、源码上传、评审和测试执行；Python 3.10/3.13 compileall、Node syntax、309 个 Workflow selector AST、99 个本地 Markdown 链接/锚点、121 个文本文件 UTF-8 无 BOM 和 `git diff --check` 均通过。聚焦浏览器/HTTP/sandbox 为 18 passed/27.99 秒；HTTP 正负面与 sandbox 对照最终为 5 passed/6.95 秒。

测试运行期间没有提交、合并、推送、部署、真实商业 CLI/model、远程节点或外部通知。固定测试使用 macOS profile 的完整链属于本机平台证据；F0 外部试点、Linux RELEASE-01、Windows/F4/F5 仍开放，F3 总退出条件保持未完成。


## F4.1 Hub V2 分片发布、恢复与备份兼容（2026-10-23）

文档基准为[产物契约](../../superpowers/specs/2026-10-20-artifact-capability-integration-contract.md) §2/3/3.1/5、路线 F4.1 和验收 WF-04 的 Hub 子集。沿用 Python 3.10+、同步 Flask、SQLite 与现有 ArtifactStore，无新增运行依赖或执行引擎。part offset 由复合主键索引查找，完整文件流转 O(n)；part 清单最多 256 项，所有 inventory/recovery 扫描设上限，未知结构或超限失败关闭。只有存储边界新增仓库，内部编排使用具体服务，不增加空壳 Facade。

已落盘的完整文件：

- `hub/domain/artifact_transfer.py`：不可变 scope/manifest、状态和固定大小/分片校验；领域无时钟、随机或框架 I/O。
- `hub/infrastructure/artifact_transfer_repository.py`：独立版本化附加 schema、真实签名/租约/当前绑定校验、事务配额与并发、ready/reference 原子提交。
- `hub/application/artifact_transfer_service.py`：POSIX 私有 staging、非阻塞单传输 flock、分片 fsync、同 ID 流式发布/恢复、实际占盘核账与只读 GC 候选。
- `hub/http/artifact_transfer_routes.py`：四个 Node-bound 入口，正文前授权、精确长度/hash、分块读取及 UTC 截止响应。
- `tools/platform/artifacts.py`、`hub/http/artifact_routes.py`：独立 V2 流式/隐藏发布、持久可见性、核验后复用同一下载描述符；旧 10 MiB put_file/PNG 和 64 KiB 文本预览不变。
- `hub/infrastructure/platform_backup.py`、`hub/infrastructure/browser_capture_gc.py`：V2 来源/引用闭包、未知 schema 拒绝、明文/加密 frozen 恢复。存在任何非 ready transfer 时备份失败关闭；ready scratch 不复制，恢复副本对应记账清零，源数据不变。
- `hub/config.py`、`hub/bootstrap.py`、`hub/infrastructure/workflow_bootstrap.py`、`hub/accounts/http.py`：独立默认关闭 gate、Workflow domain 依赖、签名公钥装配及机器认证入口；停止新派发不关闭在途回传。
- `tests/test_artifact_transfer.py`、`tests/test_artifact_transfer_recovery.py`：45 个顶层 selector / 70 个参数实例，纳入 FILES-01 的 50 selectors；恢复、备份、默认关闭及停止 admission 后回传同步至 WF-08 的 52 selectors。WF-04 完整跨节点 journey 不提前登记。

协议落实默认 64 MiB、配置硬上限 256 MiB、1 MiB/最多 256 片、每节点 2/全局 4、workspace 1 GiB/流程 256 MiB、30 分钟续租且绝对不超过两小时。workspace 计入旧/新 ready、保留 staging、活跃预留和发布额外副本；流程预算按逻辑文件计。发布直接由分片写入 `.publication/content`，不生成额外完整 assembled 文件。V2 manifest 的 `input_subject_digest` 固定为原步骤 resolved contract digest，不伪造源码验证主体。

本机使用真实 SQLite/文件/机器认证 Flask 和 Ed25519：实际 11 MiB 以上及 256 MiB/256 片乱序上传、重复末片、完整 hash、tracemalloc 峰值小于 16 MiB（Python 分配追踪，不是进程 RSS 上限）；两个 OS 进程分别竞争节点、全局、workspace 和流程的最后额度。rename 前、rename 后/DB ready 前、DB ready 后/可见响应前均注入真实 `os._exit(73)`，恢复仍为同一 ID。覆盖磁盘不足/部分写入、分片回执中断、失效租约/代数、慢正文/断连、DB trigger 失败、缺失/篡改内容、引用及 provenance 损坏隔离。GC 仅返回 dry-run，没有执行产物删除。

修复过程中已消除真实签名授权、workflow/transfer 状态混用、命令截止取错行、零长度 part、满 256 片后的末片重传、URL-safe ID 首字符，以及迟到 expiry 覆写 ready 等问题。未知 transfer schema/manifest、来源或引用不闭合均拒绝，不把文件存在当成可下载的 ready 事实。

终审前的切片证据（当时 `/tmp/agent-fleet-f4-1-source-digests.json` 的 14 项摘要一致；以下结果早于后述生命周期修复，不能覆盖最终代码树）：

| 范围 | 结果 | 本机证据 |
|---|---|---|
| Python 3.13.7 新增传输/恢复 | 55 passed，26.26 秒 | `/tmp/agent-fleet-f4-1-transfer.xml` |
| Python 3.10.21 同一切片 | 55 passed，9.05 秒 | `/tmp/agent-fleet-f4-1-python310-verified.xml` |
| Workflow、原产物/备份/GC/Node HTTP 等相关回归 | 672 passed、11 skipped、30 subtests passed，99.22 秒；11 skip 均为当时未配置 Playwright 的 Workflow 浏览器实例 | `/tmp/agent-fleet-f4-1-final-regression.xml` |
| 随后配置本机 Playwright 1.63.0/Chrome 的浏览器与前端复验 | 15 passed，无 skip，29.14 秒；包括上述 11 个浏览器实例 | `/tmp/agent-fleet-f4-1-browser-verified.xml` |

可复现聚焦命令：

```bash
PYTHONPATH=. python3 -m pytest \
  tests/test_artifact_transfer.py tests/test_artifact_transfer_recovery.py \
  tests/test_platform_artifact_routes.py tests/test_platform_backup.py \
  tests/test_platform_backup_encrypted.py tests/test_browser_capture_gc.py -q
FLEET_PLAYWRIGHT_MODULE=/tmp/agent-fleet-f4-1-browser-deps/node_modules/playwright \
FLEET_BROWSER_CHANNEL=chrome PYTHONPATH=. python3 -m pytest \
  tests/test_workflow_browser.py tests/test_frontend_artifact_contracts.py -q
```

Playwright 1.63.0 从既有 npm 缓存离线恢复到临时目录，没有下载浏览器或修改全局环境。Python 3.10 使用已有 `/tmp/agent-fleet-py310-workflow-20261021` 依赖环境；裸解释器缺依赖的最初收集失败已用该环境重新执行修复，不修改项目依赖以掩盖错误。

范围限制：现有 WSGI 每次阻塞 read 的底层超时仍依赖服务器/代理；应用只在有界读取前后核验租约/reservation 截止。本机 POSIX 证据不代表 Linux 门禁或 Windows 句柄验收，上传 gate 仍为 false。F4.2 的 `tools/platform/artifact_transfer.py`、Node export/receive、grant/content、独立 journal、`tests/test_artifact_receive.py` 和 `tests/test_workflow_two_nodes.py` 尚未实现；两台获准真实机器的交接仍 not_run。F4.3/F5、真实 CLI/model、生产隔离与发布验收继续开放；没有提交、推送、合并、部署或访问远程节点。此前 2026-10-22 的完整门禁是历史树证据，不能覆盖本轮新增代码。


### F4.1 终审：停止 admission 与执行撤销的区别

按执行契约 §4/§10 复核发现，上传把 `pause_requested` 当作取消，且配置错误地要求 dispatch gate 保持开启；暂停/停止新派发会阻断已授权执行的输出。相反，原实现未检查 Run 的 `cancel_requested` 和步骤 `active_deadline`，有效 Command 租约可能使过期或已取消执行继续发布。

新增四个顶层 selector / 15 个参数实例，先得到 **12 failed、3 passed，1.83 秒**，证据 `/tmp/agent-fleet-f4-1-admission-before.xml`：真实 pause 在 reserve/part/commit 三处返回错误 409；配置工厂关闭 transfer 或直接配置被 bootstrap 拒绝；已取消 Run/过期步骤未拒绝，慢正文越步骤截止仍返回 200。drain 三个对照实例原本通过。

最小修复只涉及原上传仓库、`hub/config.py` 和 `hub/bootstrap.py`：transfer 依赖 Workflow domain 与签名密钥，独立于允许新 admission 的 dispatch 开关；原活跃绑定不因 pause/drain 失效。每次新写入重新检查 Run 取消和步骤 active deadline；正文读取有效截止取 reservation、Command expiry/worker lease 与步骤 active deadline 的最小值。签名、当前 generation、workspace、frozen 和其他原有权限检查继续生效，不放行未派发/已终结执行。

同一复现加默认 gate/签名边界得到 **16 passed，1.63 秒**，证据 `/tmp/agent-fleet-f4-1-admission-after.xml`。随后 Python 3.13 传输/恢复与 Workflow gate/control **102 passed，13.81 秒**，`/tmp/agent-fleet-f4-1-admission-related.xml`；Python 3.10 同一传输/恢复切片 **70 passed，13.21 秒**，`/tmp/agent-fleet-f4-1-admission-python310.xml`。FILES-01/WF-08 已同步四个/两个新增 selector，分别为 50/52 selectors。

修复前完整本机门禁曾得到 **4063 passed、5 skipped、195 subtests passed，613.12 秒**，FILES-01 与五个 Workflow journeys 全部通过，严格状态仍为 RELEASE-01 缺 3 个必需 Linux pidfd 实例而失败。旧报告 `/tmp/agent-fleet-f4-1-final-gate/` 与 602 项摘要保留为修复前 checkpoint；它没有发现上述遗漏，不能替代回归新增后的最终验证。最终树结果和摘要另行记录。


### F4.1 修复后完整门禁与交付快照

修复后在完整矩阵和真实本机 Chrome 上重新运行：

```bash
FLEET_PLAYWRIGHT_MODULE=/tmp/agent-fleet-f4-1-browser-deps/node_modules/playwright \
FLEET_BROWSER_CHANNEL=chrome \
FLEET_SCREENSHOTS=/tmp/agent-fleet-f4-1-admission-final-gate/browser \
PYTHONPATH=. python3 -m pytest tests -q --require-journeys \
  --journey-report=/tmp/agent-fleet-f4-1-admission-final-gate/journeys.json \
  --junitxml=/tmp/agent-fleet-f4-1-admission-final-gate/results.xml
```

**4078 passed、5 skipped、195 subtests passed，590.67 秒；0 failures/errors。** JUnit 共 4278 testcase 节点，pytest subtest 汇总单独统计。FILES-01 为 50 selectors / 75 cases 全过；WF-01/02/03/07/08 分别 62/73/126/14/52 selectors、119/174/206/14/80 cases，全过。transfer 两个测试模块的 70 个参数实例全部通过；五个 Workflow journey 与 FILES-01 均通过。

严格全仓门禁仍返回 1、`ci_status=failed`、40/41 journeys：RELEASE-01 的三个必需 Linux pidfd/`/proc` selector 在 macOS skip；另两个 Supervisor `/proc` 用例 skip。它们不属于本切片，不能作为通过；没有删 selector 或把 skip 放行。五个真实外部验收项均 `not_run`。完整报告为 `/tmp/agent-fleet-f4-1-admission-final-gate/{results.xml,journeys.json,configuration.json,verification.json,source-digests.json}`。测试对应基线 `4cd816230fd1d8d5bf02c9f086959d8b5b1a27cf`、dirty=true、Python 3.13.7/macOS 26.6.2 arm64、Playwright 1.63.0、Chrome 155.0.8059.85；矩阵 SHA-256 `0903e6d1170228a4a4d3784b8296dfb273de0b69de74e2ea2d9af3f562dcd8ea`。602 个门禁前冻结文件摘要逐项一致。

最终文档复核覆盖七份 Markdown 的 UTF-8 无 BOM 和 61 个本地文件/锚点链接；676 个源码、测试与项目文档文件均 UTF-8 无 BOM。Python 3.10/3.13 compileall、`git diff --check` 通过。本地门禁成功证明已登记项目 journey 与 F4.1 POSIX Hub 子集；Linux 发布门禁、Windows、Node 接收、两台真实机器、生产部署和外部服务不由此替代。


## F4.2 Node 接收子集（2026-10-23）

本轮按[产物契约](../../superpowers/specs/2026-10-20-artifact-capability-integration-contract.md) §3.1/4/5 与路线 F4.2 实现接收子集，继续沿用 Python 3.10+、Flask、SQLite 和标准库传输，无新增依赖。输入 alias 以字典 O(1) 匹配，全部接收回执用一次有界 SQL 及 O(n) 字典核对；内容按 64 KiB 流式传递，内存不随总文件大小线性增长。仅网络/文件/存储边界有 Protocol，复用原 Command/outbox、机器认证与 Workflow 事务，不建立另一套执行引擎或透传 Facade。

已完整落盘：

- [x] `hub/domain/artifact_receive.py`：固定签名命令、强类型接收参数/receipt、grant 派生、严格大小/hash/alias/后缀校验与合法旧 artifact ID 兼容。
- [x] `tools/platform/artifact_journal.py`、`artifact_transfer.py`：独立 SQLite envelope/目录与文件 inode/receipt/ack/次数账本；断传重下、四个真实退出窗口恢复、排他原子发布、ready 文件复核及同回执重传，异常保留 reconciliation。
- [x] `hub/infrastructure/artifact_input_repository.py`、`hub/application/artifact_input_service.py`、`hub/http/artifact_input_routes.py`：原冻结事务签发固定 grant；限定原 Node/Command/worker/当前 generation，waiting_node 等全部匹配回执后才 ready，直接 admission 同样受检查。
- [x] `tools/atomic_directory.py`、`tools/platform/artifact_lock.py`、`tools/transport.py`：POSIX 目录 fd 排他 rename、内核非阻塞下载/接收槽位、固定 origin 的无重定向流式 GET。Node 2 槽位、Hub 下载每节点 2/全局 4；这些仍与上传容量分开核算。
- [x] `tools/platform/node_executor.py`、`node_client.py`、`node_runtime.py`：固定 receive 动作、显式 inputs 配置、独立 journal、启动恢复/待确认回执重传、关闭取消信号及平台能力过滤。
- [x] `hub/auth.py`、`hub/infrastructure/platform_db.py`、原 Command receipt 路径：入口生成内部凭据指纹；下载逐块、回执写事务复查原凭据，拒绝认证后禁用/轮换。指纹不进入公开 DTO、URL 或模型，逐块不重做密码派生/心跳写入。
- [x] `tests/test_artifact_receive.py`、`test_workflow_two_nodes.py` 及 `test_transport.py`：真实签名、SQLite、文件、OS 退出、loopback HTTP/NodeRuntime 与原下游门禁；FILES-01/NODE-01/WF-08 仅登记本机已实现子集，完整 WF-04 不提前登记。

本机证据覆盖：partial、rename 前、rename 后、ready 后真实 `os._exit(73)`；第三次已完成下载后仍恢复 publishing；Node 错配、伪造/缺失/hash/alias/类型回执拒绝；原凭据禁用/轮换、取消/frozen/过期/旧代/租约失效；缺 alias 持久等待、pause/drain 回传、默认 gate 兼容；目标已有/链接/硬链接/目录替换拒绝、原文件不覆写；12 MiB 接收的 Python tracemalloc 峰值小于 2 MiB（不等于 RSS）；原 receipt 响应丢失后重启不重下；备份保留 grant/receipt 和 waiting_node，恢复 frozen。真实 Git 包接收后由独立 F3 操作重算 tree/blob、解包并运行固定 pytest；通用接收不安装、解压或执行文件。

本轮缺陷均先留失败证据再修复：最后一次下载后发布恢复被次数过滤误阻止（`/tmp/agent-fleet-f4-2-last-attempt-before.xml`）；合法旧 URL-safe ID 首字符被拒绝（`/tmp/agent-fleet-f4-2-legacy-id-before.xml`）；流中原凭据撤销仍可读（`/tmp/agent-fleet-f4-2-credential-current-before.xml`）；入口认证后凭据撤销仍可写成功回执（`/tmp/agent-fleet-f4-2-receipt-credential-before.xml`）；损坏账本非对象命令抛 AttributeError（`/tmp/agent-fleet-f4-2-corrupt-journal-before.xml`）。同一复现已分别转绿，未放宽断言或修改既有安全合同。

终审前聚焦结果：Python 3.13.7 接收/Hub/Node/传输 **167 passed，12.13 秒**，`/tmp/agent-fleet-f4-2-receive-final-focused.xml`；Python 3.10.21 同一集合 **167 passed，10.07 秒**，`/tmp/agent-fleet-f4-2-python310-final-focused.xml`。上传/恢复、协调器、明文/加密备份、源包、gate、NodeRuntime/原 journal 回归 **212 passed，30.18 秒**，`/tmp/agent-fleet-f4-2-final-related.xml`。未知账本及损坏命令独立复验 5 passed，`/tmp/agent-fleet-f4-2-corrupt-journal-after.xml`。Python 3.10/3.13 compileall、62 个文档本地链接/锚点、704 个文本 UTF-8 无 BOM 及 diff 空白检查通过；仓库无静态类型检查器，compileall 仅证明语法兼容。

最终完整门禁须在本切片代码、矩阵与核心规格冻结后重新运行；F4.1 的 4078 passed 是历史树证据，不能覆盖新增接收代码。本轮完整结果与冻结摘要在下方追加。

剩余范围：

- [ ] 固定 Node export 客户端及上传恢复；上传/下载合并后的每节点 2/全局 4 容量账本。
- [ ] Linux 新鲜完整门禁、Windows 真实句柄/reparse；Windows 不声明 receive 能力。
- [ ] 两台获准真实机器的产物导出/Hub 转运/接收与固定验证；当前只有本机协议证据。
- [ ] F4.3 能力目录/确定性路由、F5 外部集成，以及获准真实 CLI/model 和生产隔离试点。

Workflow/dispatch/transfer 默认关闭，完整 F4.2/WF-04 仍未完成。未提交、推送、合并、部署、删除用户文件或访问远程节点。


### F4.2 检查点门禁与最后批量读取优化

`/tmp/agent-fleet-f4-2-final-gate/` 为最后优化前的检查点：**4150 passed、5 skipped、195 subtests passed，540.21 秒**，JUnit errors=0/failures=0，40/41 journeys 通过；FILES-01/NODE-01/WF-08 的 87/31/62 selectors 全过。严格门禁退出 1，RELEASE-01 仍缺 macOS 无法运行的 3 个 Linux pidfd 必需 selector，另 2 个 Supervisor 平台用例跳过。760 个冻结文件在运行后全部一致，矩阵摘要 `9cf9ef71e96ef9ed38f04e9ea551e1dece398cba8676cca462a37c21d6a89999`。环境为 Python 3.13.7/macOS 26.6.2 arm64、Playwright 1.63.0、现有 Chrome 156.0.8078.47；未下载浏览器。此检查点不能覆盖后述最终改动。

终审将 Node 待确认回执由逐个 get 改为一次有界读取完整记录，再以 O(n) 校验文件和 receipt；并发两个 ready 记录的测试直接验证共同恢复。补充零字节接收，确认实际空文件、空内容 hash 和固定 receipt。原 ready outbox 用例扩展到文件、命令和 receipt 三种损坏，先复现 null 命令的 AttributeError（`/tmp/agent-fleet-f4-2-ready-corruption-before.xml`，1 failed/2 passed），再在解析边界拒绝非对象并记录 reconciliation。最终代码与矩阵重新冻结后另跑完整门禁，结果在下方追加。


最后优化后的同一聚焦集合：Python 3.13.7 **170 passed，12.91 秒**（`/tmp/agent-fleet-f4-2-receive-verified-focused.xml`），Python 3.10.21 **170 passed，12.41 秒**（`/tmp/agent-fleet-f4-2-python310-verified-focused.xml`），含零字节、多个待确认回执及上述 ready 损坏复现。FILES-01/NODE-01/WF-08 现为 88/32/62 selectors；最终完整门禁使用这些新参数实例，不能复用检查点数字。


### F4.2 接收子集最终完整门禁与续接快照

最后代码、矩阵与核心规格冻结后执行：

```bash
FLEET_PLAYWRIGHT_MODULE=/tmp/agent-fleet-f4-1-browser-deps/node_modules/playwright \
FLEET_BROWSER_CHANNEL=chrome \
FLEET_SCREENSHOTS=/tmp/agent-fleet-f4-2-verified-gate/browser \
PYTHONPATH=. python3 -m pytest tests -q --require-journeys \
  --journey-report=/tmp/agent-fleet-f4-2-verified-gate/journeys.json \
  --junitxml=/tmp/agent-fleet-f4-2-verified-gate/results.xml
```

**4153 passed、5 skipped、195 subtests passed，532.01 秒；JUnit 0 failures/0 errors，共 4353 testcase。** FILES-01 为 88 selectors/150 cases、NODE-01 为 32/60、WF-08 为 62/100，均全部通过；WF-01/02/03/07 分别为 62/119、73/174、126/206、14/14，也全部通过。新接收回执、零字节、损坏 ready 账本和真实浏览器均由这一轮最终树覆盖。

严格门禁仍退出 1，`ci_status=failed`，40/41 journeys 通过：RELEASE-01 的三个必需 Linux pidfd/`/proc` 用例在 macOS 未执行；另两个原 Supervisor `/proc` 用例跳过。没有移除 required selector 或把 skip 当作通过。只读探测未发现本机已有 Docker、Podman、Colima、Lima 或 QEMU Linux 运行时；本轮没有安装或下载运行时。五项外部验收仍 `not_run`，Linux、Windows 与真实两机不能由此门禁替代。

完整报告为 `/tmp/agent-fleet-f4-2-verified-gate/{results.xml,journeys.json,configuration.json,source-digests.json,verification.json}`。源码基线 `4cd816230fd1d8d5bf02c9f086959d8b5b1a27cf`、dirty=true，Python 3.13.7/macOS 26.6.2 arm64、Playwright 1.63.0/Chrome 156.0.8078.47；矩阵 SHA-256 `e38d588db973d4b390d75f413e73280c0dedbaaf3cd04e6f73fed786c99792ed`。760 个冻结文件在运行后逐项一致，报告 revision/dirty/矩阵摘要与配置一致；734 个 Git 清单内文本为 UTF-8 无 BOM。最终 Python 3.10/3.13 的 170 项聚焦回归与 compileall 通过。此结果记录在测试结束后追加，运行时代码与矩阵保持原摘要。

下一步先补固定 Node export 客户端及其持久上传恢复，再完成上传/下载统一容量和获准两机交接；之后按路线推进 F4.3/F5。当前只完成 F4.2 接收子集，完整 F4.2/WF-04 仍开放。所有工作继续保留于 `codex/workflow-platform-docs` 独立工作树，主工作树既有工作保留；未提交、推送、合并、部署或启动外部真实任务，transfer gate 默认关闭。


## F4.2 本机固定导出、统一容量与双身份交接（2026-10-23）

文档基准为[产物契约](../../superpowers/specs/2026-10-20-artifact-capability-integration-contract.md) §3–5、路线 F4.2 和验收 WF-04 的本机子集。沿用 Python 3.10+、同步 Flask、SQLite 与标准库 HTTP，不新增依赖。整文件 hash/分片处理 O(n)，offset 字典查询 O(1)，part 清单最多 256；容量使用既有上传状态索引聚合与六个非阻塞内核锁探测。仅增加独立上传账本和文件/HTTP 边界，不另建执行器、下载租约表或空壳 Facade。

- [x] `hub/infrastructure/artifact_capacity.py`、`artifact_transfer_repository.py`、`artifact_input_repository.py`、`tools/platform/artifact_lock.py`：上传/下载共用同一 SQLite admission fence，每节点 2/Hub 全局 4；下载锁随流关闭和进程退出释放，unknown 上传继续占位。
- [x] `hub/domain/artifact_export.py`：固定签名 export 请求和受限 ArtifactRef/receipt，命令只可选择登记的 output_key。
- [x] `tools/platform/artifact_export.py`、`artifact_export_journal.py`：固定源文件 fd/身份、64 KiB 完整 hash、1 MiB 分片、原 ID 状态查询/补传/commit、独立账本与回执 outbox。临时失败保留原 Command；损坏/变化或尝试超限进入 reconciliation，未知 schema 拒绝装配。
- [x] `node_runtime.py`、`node_executor.py`、`node_client.py`：显式 output 登记、收发共用本地两槽池、签名与原租约、恢复完成后再发送成功回执；新导出请求/成功回执不跟随重定向，JSON 响应上限 64 KiB。Windows 不声明导出能力。
- [x] Hub Command 成功回执要求原 ready transfer 和完整 size/hash，一次写事务复查原凭据；HTTP commit/status 首次发布也在 ready 事务复查原认证凭据，已完成文件保留原完成事实。
- [x] `tests/test_artifact_capacity.py`、`test_artifact_export.py`、`test_transport_bounded.py`：真实分片/HTTP、四个进程退出窗口、三类丢响应、源文件/账本变化、跨进程最后槽与退出释放、收发同池、12 MiB 流式分配、原凭据竞态。Node A 实际导出 Git 源包后，Hub 保存原 output binding；另一身份 Node B 接收原 bytes/确认 hash，随后独立核验 tree/blob 并运行固定 pytest。

容量原漏洞由三个混合收发用例复现（`/tmp/agent-fleet-f4-2-unified-capacity-before.xml`）；统一 admission 后跨进程/自定义低限额/unknown 及原边界共 9 项通过（`/tmp/agent-fleet-f4-2-unified-capacity-process.xml`）。初版导出的三处恢复问题由 `/tmp/agent-fleet-f4-2-export-recovery-before.xml` 复现，修复后导出恢复/有界 HTTP 共 60 项通过（`...-export-recovery-after.xml`）；新增安全与本机双身份链得到 78 项通过（`...-export-safety.xml`）。它们均为中间树证据。

终审再用实际 HTTP/SQLite 复现认证后、最终 ready 写入前禁用/轮换凭据仍能发布，两个实例均失败（`/tmp/agent-fleet-f4-2-commit-credential-before.xml`）；根因为最终发布事务只有 Command/Run 约束，未复核入口认证指纹。修复在最终写事务加入复查，并覆盖 status 的发布恢复。相关新鲜结果与最终完整门禁在本节下方追加；此前接收子集的 4153 passed 不能覆盖本轮新增代码。

终审修复前，Python 3.10.21 / 3.13.7 对导出、容量、接收、NodeRuntime、分片恢复与传输各得到 **271 passed，48.40 / 52.69 秒**（`/tmp/agent-fleet-f4-2-export-python310.xml`、`...-python313.xml`）。最终源码仍须重新验证；FILES-01/NODE-01/WF-08 已按本机收发、容量与恢复扩充 required selectors，完整 WF-04 保持未登记。

下一步保持：Linux 新鲜门禁、Windows 真实句柄/reparse、两台获准真实机器交接与固定验证；随后 F4.3 能力目录/确定性路由和 F5 外部接入。获准真实 CLI/model、生产隔离与完整发布验收仍开放。未提交、推送、合并、部署、删除用户文件或访问远程节点，所有 gate 继续默认关闭。


### F4.2 导出与统一容量最终验证

本轮最终代码、矩阵和规格冻结后执行：

```bash
FLEET_PLAYWRIGHT_MODULE=/tmp/agent-fleet-f4-1-browser-deps/node_modules/playwright \
FLEET_BROWSER_CHANNEL=chrome \
FLEET_SCREENSHOTS=/tmp/agent-fleet-f4-2-export-final-gate/browser \
PYTHONPATH=. python3 -m pytest tests -q --require-journeys \
  --journey-report=/tmp/agent-fleet-f4-2-export-final-gate/journeys.json \
  --junitxml=/tmp/agent-fleet-f4-2-export-final-gate/results.xml
```

**4233 passed、5 skipped、195 subtests passed，662.91 秒。** JUnit 0 failure/0 error，共 4238 个 testcase 节点；subtest 汇总不额外相加。FILES-01 为 120 selectors/230 cases、NODE-01 为 64/140、WF-08 为 73/122；WF-01/02/03/07 分别为 62/119、73/174、126/206、14/14，均全部通过。

严格门禁退出 1，`ci_status=failed`，40/41 journeys 通过。唯一 incomplete 是 RELEASE-01：macOS 未执行三个必需 Linux pidfd/`/proc` selector；另两个 Supervisor `/proc` 平台用例跳过。没有删除 selector、放宽门禁或把 skip 当作通过。MODEL-LIVE、MAIL-LIVE、NODE-LIVE、DEPLOY-LIVE、MUSE-LIVE 均为 `not_run`，本机双身份 HTTP 链不能代替两台真实机器、Linux/Windows 或生产隔离验收。

报告目录 `/tmp/agent-fleet-f4-2-export-final-gate/` 保存 `results.xml`、`journeys.json`、`configuration.json`、`source-digests.json`、`verification.json` 和浏览器截图。基线 `4cd816230fd1d8d5bf02c9f086959d8b5b1a27cf`、dirty=true；Python 3.13.7、macOS 26.6.2 arm64、Playwright 1.63.0、Chrome 156.0.8078.47。767 个冻结文件在运行后以及本次补录前逐项一致；矩阵 SHA-256 为 `7be0044568bf3d00d5b4794e32f5b01e21211b3d678aa3d616e28de76ca69335`。补录仅改变本进度文档，运行时代码和矩阵仍对应冻结摘要。

Python 3.10.21 最终串行关联回归覆盖 export、bounded HTTP、capacity、两身份链、receive、transfer/recovery、NodeRuntime 与原 transport：**273 passed，41.11 秒**，报告 `/tmp/agent-fleet-f4-2-export-serial-python310.xml`。此前与全仓门禁并行运行的同一集合为 1 failed/272 passed：`test_two_node_receive_slots_are_shared_across_receiver_instances` 的五秒 Event 等待超时，报告 `/tmp/agent-fleet-f4-2-export-final-python310.xml`。该用例独立复核 1 passed/0.54 秒，随后完整串行集合通过；未修改或放宽测试，超时根因未确定，失败证据保留。

最终凭据竞态修复后的传输/恢复回归 72 passed，见 `/tmp/agent-fleet-f4-2-commit-credential-after.xml`。Python 3.10/3.13 `compileall -q hub tools tests`、`git diff --check`、四份核心 Markdown 的 26 个本地路径/锚点与 765 个可识别 UTF-8 文本无 BOM 检查通过。没有新增依赖、下载运行时、提交、推送、合并、部署或调用远程节点；完整 F4.2/WF-04 和 F4.3/F5 继续开放。


## F4.3 能力目录、报告与显式原子 admission（2026-10-23）

规格基准为[产物契约 §6/6.2](../../superpowers/specs/2026-10-20-artifact-capability-integration-contract.md#6-节点能力目录)、路线 F4.3、验收 WF-05；具体文件与执行次序见[子集实施计划](../../superpowers/plans/2026-10-23-capability-catalog-admission.md)。保留 Python 3.10+、同步 Flask/SQLite 和既有 Workflow/Run/Command，不新增依赖。候选一次过滤取最小 `(priority, held_slots, node_id)`，按定义/能力/软件字典匹配；每次有界候选查询、持久占用聚合及所需版本批量读取，避免逐节点查库。schema/hash 在定义构造时固定，候选循环不重复解析。

- [x] `hub/domain/capability.py`：严格不可变定义、NodeOffer/NodePolicy/requirements/binding/admission 值，固定 schema 与完整摘要；精确整数、UTC、有界原始 JSON、重复键/未知字段/错误类型拒绝。
- [x] `hub/application/capability_router.py`：按权限、实现/驻留/输入、报告新鲜度、双端策略、持久槽位/资源顺序过滤，稳定选择最高兼容且获准的 revision；候选最多 256、原因最多 32、跨 owner 不暴露 node_id。
- [x] `hub/infrastructure/capability_schema.py`、`capability_repository.py`：版本不可变、凭据指纹在写事务复核、报告单调序号和启动后新报告；显式 admission 复用原 intent/预算/槽位/事件事务并保存固定 binding。unknown/isolated 保留占位，旧记录资源未知时失败关闭；重复请求不换机、异输入/无关 revision 拒绝。
- [x] `hub/config.py`、`hub/bootstrap.py`、`hub/infrastructure/workflow_bootstrap.py`、`hub/http/node_routes.py`：独立默认关闭 gate、管理员装配登记、机器专用 64 KiB 报告入口和受限 UTC receipt；原 heartbeat/poll/布尔 capabilities 语义不变。
- [x] `workflow_schema.py`、`workflow_repository.py`、`platform_backup.py`：仅含新目录的库要求最低 reader=2；旧库仍为 1。gate-off reader/备份/GC 识别未知 schema；快照核对目录键、内容/hash、原 node/dispatch/budget/reservation，明文和加密恢复 frozen。
- [x] `tests/test_capability_router.py`、`test_capability_reservations.py`、`test_capability_http.py`：顺序过滤、120 秒边界、未来诊断时间、启动后同 wall-clock 新报告、同 revision 异配置、错误类型、双端策略、真实双进程最后槽、失败回滚、原凭据撤销、资源累计、原控制 fencing 和备份损坏。

关键失败证据保留：首次缺模块的红灯 `/tmp/agent-fleet-f4-3-router-before.xml`、`...-admission-before.xml`；目录 reader 初始化污染旧库的 5 个回归失败见 `...-core-after.xml`，按新域提升 reader 后原断言全部通过（`...-reader-compatibility-after.xml`，88 passed）。错误 JSON/hash 容器类型泄漏 TypeError 由 `...-hostile-types-before.xml` 复现；补类型前置检查后通过。`...-snapshot-before.xml` 的 5 个失败证明新域备份尚未校验内容及旧资源未知误计零，接入快照闭包/保守资源约束后关联集合 115 passed（`...-snapshot-after.xml`）。

终审 `...-final-review-before.xml` 复现 schema 在逐候选重复解析、损坏 role 类型泄漏和重复 binding 接受无关 revision；固定构造期快照、类型校验和原 admission revision 后通过。`...-corrupt-identity-before.xml` 再复现损坏 retry_class、原 frozen digest 不一致与缺失 budget 仍能备份；在解析/原事务/备份闭包分别修复。初始 HTTP 的 13 个失败是未装配入口时的 404，入口与权限落实后同一用例转绿；没有放宽状态或安全断言。三个 fixture 初期误把同 revision 的 slots=1 改成 2、调用不存在的测试 helper，修正 fixture 而未修改生产冲突语义。

最终 Python 3.13.7 聚焦回归覆盖上述三模块、Workflow repository/coordinator/control/gates、原 Node HTTP、明文/加密备份和 GC，**251 passed，12.99 秒**，`/tmp/agent-fleet-f4-3-final-focused.xml`。Python 3.10 与完整矩阵门禁必须以本轮新树重新运行，结果在下方追加；F4.2 的 4233 passed 是旧树证据，不能覆盖能力目录代码。NODE-01 已增至 103 selectors，WF-08 为 86；完整 WF-05 继续不登记为完成。

剩余范围：

- [ ] 将显式选点/admission 接入原固定 target 协调器；NodeRuntime 的报告发送、签名能力 binding 与执行前软件/本地策略复核，能力变化返回 capability_changed。两端闭合前不启用自动路由。
- [ ] Linux 新鲜完整门禁、Windows 句柄/reparse、两台获准真实机器交接，以及完整 F4.2/WF-04、F4.3/WF-05。
- [ ] F5 MCP/外部 Agent/窄出口、获准真实 CLI/model 和生产隔离/外部试点。

本轮没有提交、推送、合并、部署、下载运行时、删除用户文件或访问远程节点。Workflow/dispatch/transfer/capability gate 均默认关闭；当前交付仅为本机目录、报告和显式 admission 子集，不宣称整个系统生产就绪。

### F4.3 目录与显式 admission 最终验证

冻结最后代码、规格和矩阵后，Python 3.13.7 完整运行 `PYTHONPATH=. python3 -m pytest tests -q --require-journeys --journey-report=/tmp/agent-fleet-f4-3-final-gate/journeys.json --junitxml=/tmp/agent-fleet-f4-3-final-gate/results.xml`；沿用现有 Playwright 1.63.0、Chrome 156.0.8078.47 和 macOS 26.6.2 arm64。结果 **4330 passed、5 skipped、195 subtests passed，547.25 秒**，JUnit 0 failures/0 errors，共 4335 个 testcase 节点；subtest 不额外叠加。

严格门禁退出 1，`ci_status=failed`，40/41 journeys 通过。唯一 incomplete 为 RELEASE-01 的三个必需 Linux pidfd/`/proc` 用例；另两个 Supervisor `/proc` 平台用例跳过。未删减 selector、放宽门禁或把 skip 计为通过。MODEL-LIVE、MAIL-LIVE、NODE-LIVE、DEPLOY-LIVE、MUSE-LIVE 仍全部 `not_run`。

证据目录 `/tmp/agent-fleet-f4-3-final-gate/` 保存 configuration、journeys、JUnit、source-digests、verification 及浏览器截图。基线 `4cd816230fd1d8d5bf02c9f086959d8b5b1a27cf`、dirty=true，矩阵 SHA-256 `04dd93d6c23f8ce2610bd629e252a54bec8e1a0dff761111ad61ce8c87cacf58`；775 个冻结文件在测试后、本次补录前全部一致。最终 Python 3.10.21 关联回归 **251 passed，11.45 秒**，见 `/tmp/agent-fleet-f4-3-verified-python310.xml`。Python 3.10/3.13 compileall 和 diff 空白检查通过。

额外真实临时库探针 `/tmp/agent-fleet-f4-3-gc-probe.json` 证明 gate-off GC 在 capability schema=99 时失败关闭，错误为 `capture_gc_reference_surface_unknown`，保留 `unsupported_capability_schema` 原因链；未修改冻结源码。本次补录只改变结果文档，运行时代码和矩阵仍对应原摘要。后续自动协调器与 Node 链路必须产生新证据，不能沿用本次结果宣称新代码通过。

## F4.3 Node 报告与本地执行前复核（2026-10-23）

基准为[能力契约 §6.3](../../superpowers/specs/2026-10-20-artifact-capability-integration-contract.md#63-node-报告发送与本地执行前复核子集)，实施步骤见[Node 计划](../../superpowers/plans/2026-10-23-node-capability-report-preflight.md)。继续沿用 Python 3.10+、原 NodeClient/Command 签名/journal 和标准库有界 HTTP，无新依赖、线程、状态库或执行引擎。定义/软件/动作使用字典与预编译集合，单次处理 O(能力及动作项数)，schema 在登记时固定，不在执行热路径重解析。

- [x] `hub/domain/capability_execution.py`：强类型、严格有界的 `CapabilityExecutionContext`，固定原 node、能力、adapter 和 TargetBinding；保留参数加入原 args_hash/Ed25519，不能覆盖既有上下文，更改后清除旧签名。
- [x] `tools/platform/node_capabilities.py`：不可变本地注册/策略和类型化 observer；只报告当前可执行动作的实现与 allowlist 交集，默认 Python/OS/磁盘事实、未知内存零字节。固定 Hub origin、原机器头、15 秒采样/网络截止、停止检查、64 KiB 响应、严格 UTC receipt 与异常链。
- [x] `node_runtime.py`、`node_client.py`、`node_executor.py`：默认无配置保持旧路径；显式 poll 报告失败仍处理原在途命令；原验签后、首次实际动作前比对软件/定义/本地策略/动作/retry/资源，改变返回 capability_changed。旧 Node 拒绝新保留参数；完成/unknown 查回原 journal，不重新执行。
- [x] `tests/test_node_capabilities.py`、`test_capability_http.py`：真实文件副作用和 unknown、原签名篡改、动作/策略/软件变化、五类实际 HTTP 重定向与超大响应、停止/截止、错误类型。原受鉴权 HTTP 报告参与原目录选点，测试中显式准备的 admission binding 通过原 Command poll/receipt 到达 Node；软件变化保留 Hub revision 冲突并拒绝旧 binding。

失败证据从缺少新模块的 `/tmp/agent-fleet-f4-3-node-before.xml` 开始；接线前两项失败见 `...-node-core-before-wiring.xml`，接线后原 Node/HTTP 关联集合 79 passed。真实传输初次使用 fixture 的固定单调时间作为 OS deadline，8 项在发送前超时；改为真实 `time.monotonic` 而未放宽断言，见 `...-node-safety.xml` 与 `...-node-safety-after.xml`（57 passed）。

终审先复现采样后的停止/截止未阻止新请求、非法 origin/机器凭据类型未前置拒绝，`...-node-boundary-before.xml` 为 6 failed/1 passed；修正生命周期窗口及配置边界后同一集合转绿。报告端口装配缺少 callable 检查由 `...-node-ports-reproduced.xml` 的 4 failed 复现；初次 ports 测试还有重复参数的 fixture 问题，先修正 fixture 再保留四项真实失败。`...-node-command-types-before.xml` 的 7 failed 再复现错误 Run/action 类型和非对象命令泄漏，已在领域入口严格拒绝。

中间树关联回归 Python 3.13.7 **282 passed，44.47 秒**（`/tmp/agent-fleet-f4-3-node-focused-python313.xml`），Python 3.10.21 **282 passed，39.64 秒**（`...-node-verified-python310.xml`）。裸 Homebrew python3.10 未装 Flask/cryptography，首次调用发生 collection errors（`...-node-focused-python310.xml`）；随后复用已有 `/tmp/agent-fleet-py310-workflow-20261021` 环境，无下载或安装。最终代码/矩阵另行冻结验证，不能沿用上述中间树或上一轮 4330 passed。

NODE-01 已登记 138 selectors，WF-08 为 93，完整 WF-05 仍开放。待完成：

- [ ] 原协调器自动选点与输入 target 固定；Hub 在原事务查回 binding/Run/generation、可信上下文注入和派发前复核。
- [ ] capability 路由的 receive/export 接线；本机 Node 子集仅接受登记 tool/postcheck，不扩展两类传输动作的旧协议。
- [ ] Linux 完整门禁、Windows 句柄/reparse、获准两台真实机器、真实 CLI/model、生产隔离与 F5 外部接入。

自动派发与生产就绪均未宣称完成；Workflow/dispatch/transfer/capability gate 保持默认关闭。未提交、推送、合并、部署、删除用户文件或访问远程节点。最终验证结果在下方追加。

### F4.3 Node 子集最终验证

冻结代码、规格和矩阵后，Python 3.13.7 完整严格门禁结果 **4408 passed、5 skipped、195 subtests passed，546.40 秒**；JUnit 4413 个 testcase 节点，0 failures/0 errors，subtest 不重复累计。严格退出 1，40/41 journeys 通过；唯一 incomplete 为 RELEASE-01 的三个 Linux pidfd/`/proc` 必需项，另两个 Supervisor `/proc` 用例平台跳过。未删 selector、降低门禁或将 skip 视为通过；五项外部验收仍全部 `not_run`。

证据目录 `/tmp/agent-fleet-f4-3-node-final-gate/` 保存 configuration、journeys、JUnit、source-digests、verification 与浏览器截图。基线 `4cd816230fd1d8d5bf02c9f086959d8b5b1a27cf`、dirty=true，Python 3.13.7、macOS 26.6.2 arm64、Playwright 1.63.0、Chrome 156.0.8078.47；矩阵 SHA-256 `f56bc73412149da30f0bf924b906a33729bfee76fb5c8b83bff44c929d25318a`。测试后逐项核对 779 个冻结文件全部一致；本次仅补录验证结果，运行时代码、规格与矩阵对应冻结摘要。

最终 Python 3.10.21 关联集合 **290 passed，40.02 秒**，见 `/tmp/agent-fleet-f4-3-node-verified-final-python310.xml`。Python 3.10/3.13 compileall、旅程门禁自身 20 项、diff 空白检查通过。原文档预检覆盖 7 份 Markdown、57 个本地链接/锚点、777 个 UTF-8 文本无 BOM；补录后再核对记录文件。以上仅覆盖 Node 报告/本地执行前复核子集，不证明 Hub 自动绑定派发、完整 F4.3 或生产验收完成。

## F4.3 Hub 固定能力 Run 与 Command 派发（2026-10-23）

基准为[能力契约 §6.4](../../superpowers/specs/2026-10-20-artifact-capability-integration-contract.md#64-hub-固定能力-run-与-command-派发子集)，执行步骤见[Hub 派发计划](../../superpowers/plans/2026-10-23-hub-capability-command-dispatch.md)。继续沿用 Python 3.10+、同步 Flask/SQLite 和原 Workflow/Run/Command；同步 I/O 留在既有受控 worker/线程边界，无新增依赖、状态表、执行器或后台生命周期。动作注册字典匹配；固定记录按 token/Run 主键读取，候选仅为原节点；资源聚合只排除自身预约，其他 held/isolated 均保留。

- [x] `hub/domain/capability.py`、`capability_execution.py`：固定 SelectionRequirements 严格解析与不可变动作注册；注入上下文后仍受 64 KiB 限制，保留原签名约束。
- [x] `hub/infrastructure/capability_dispatch.py`：固定 binding/原合同/requirements/定义/预算/槽位闭包；从原 Run 反查 token，配置标记损坏不能回退；节点、generation、原消息与配置摘要、Run lease、工作流控制和固定能力在原事务内复核。只查原节点，精确排除本步骤预约，不迁移同代执行。
- [x] `workflow_run_executor.py`：仅原生 remote Node Run 支持新 binding；创建时核对目标与摘要，受信任标记进入原配置摘要；已提交 Run 优先查回原事实，后续报告变化不重建。CLI Task/Hub 本地 review/test 不冒充支持。
- [x] `command_delivery_service.py`、`command_repository.py`：原入队事务准备上下文、缩短至原 active_deadline、签名；审批消费/Command/outbox 一起提交或回滚。重复先查回、比较原工具参数及 retry/作用域，不再消费审批。首次 lease 重新复核，控制暂停暂缓，失效保存零次投递的确定拒绝；在途 receipt/unknown 与槽位事实保持原协议。gate-off 不能生成无绑定回退。
- [x] `remote_tool_broker.py`、`workflow_bootstrap.py`：统一原动作 retry 合同，保留参数在模型边界拒绝；可信装配同一 preparation 到原 delivery、repository 和现有 RunStepExecutor，不自动启动 worker。
- [x] `tests/test_capability_dispatch.py` 与 `test_capability_http.py`：真实 SQLite/Run/审批事务与 loopback Node HTTP，覆盖上述闭包和恢复；原测试中的手工上下文准备已替换为 Hub 实际准备/签名。

失败证据从缺新类型的 `/tmp/agent-fleet-f4-3-hub-before.xml` 开始。初次 core 验证误把完整机器凭据传给 secret 认证参数，修正 fixture；最初 old_report 的 104 秒并未超出 120 秒，改为 121 秒验证真实边界，不放宽断言。实际未接线失败保留在 `...-hub-wiring-reproduced.xml`。

`...-hub-boundaries-before.xml` 的 4 failed/26 passed 复现配置标记缺失时降级、配置摘要未核对，以及保留字段被拒绝后误报 unknown；从原 dispatch 查回并核对摘要/原消息、在模型边界稳定拒绝后关联集合 162 passed。`...-hub-approval-before.xml` 的 3 failed/30 passed 复现通用 enqueue 伪造审批、注入后参数超限与原截止未收紧，已在原事务/纯上下文边界修复。

后续 `...-hub-lifecycle-before.xml` 的 4 failed/35 passed 保留超限错误码、动作登记撤销仍 queued 与损坏时间丢失类型的证据；修复后关联集合 278 passed/13.30 秒（`...-hub-lifecycle-after.xml`）。service postcheck 的资源是 service_id，不能冒充本子集 workspace 作用域，`...-hub-service-scope-before.xml` 先复现错误登记被接受，再明确拒绝。原 service/receive/export 独立协议不受此扩展。

最终 Python 3.10.21 扩展关联集 **539 passed，45.15 秒**，`/tmp/agent-fleet-f4-3-hub-final-python310.xml`；中间树 538 passed/45.16 秒及独立装配 1 passed/0.34 秒保留为不同范围，不混加当作一次结果。最初命令误写不存在的 `test_platform_node_http.py`，无测试运行；修正到仓库现有 `test_platform_node_http_e2e.py` 后执行。无下载或安装。最终 Python 3.13 完整严格门禁及冻结摘要在下方补录，不沿用上一轮 4408 passed。

NODE-01 为 170 selectors，WF-08 为 125；完整 WF-05 未注册为完成。尚需自动选点协调器/输入 target 固定、capability receive/export、unknown/isolated 能力 postcheck、Linux/Windows、获准真实两机与 CLI/model、生产隔离及 F5 外部接入。默认 gate 均关闭，没有提交、推送、合并、部署、删除用户文件或访问远程节点。

### F4.3 Hub 固定派发子集最终验证

最终冻结树的严格门禁为 **4459 passed、5 skipped、195 subtests passed，557.77 秒**。JUnit 实际 4464 个 testcase、0 failures/0 errors，不叠加子测试；FILES-01 为 120 selectors/230 cases、NODE-01 为 170/366、WF-08 为 125/202，均通过。40/41 journeys 通过；RELEASE-01 的三个 Linux pidfd/`/proc` 必需用例在 macOS 跳过，另两个 Supervisor `/proc` 用例也跳过。严格退出 1、`ci_status=failed`；保留全部 required selector，五项外部验收仍 `not_run`。

证据目录 `/tmp/agent-fleet-f4-3-hub-final-gate/` 保存配置、JUnit、journeys、source-digests、verification 和浏览器截图。基线 `4cd816230fd1d8d5bf02c9f086959d8b5b1a27cf`、dirty=true，Python 3.13.7、macOS 26.6.2 arm64、Playwright 1.63.0、Chrome 156.0.8078.47；矩阵 SHA-256 `cbcfd9ae891d4dc19a4eb56b388571bdd026f108033fee6aaa9f5a9b6515ed94`。782 个冻结文件在运行后逐项一致，配置与报告的 revision/dirty/矩阵摘要一致。摘要在 pytest 启动后立即捕获，启动至捕获及最终核对之间无任何仓库文件修改；本段为验证结束后补录。

Python 3.13.7 最终预检集合 **164 passed，15.02 秒**，`/tmp/agent-fleet-f4-3-hub-preflight.xml`；Python 3.10.21 的 539 项关联回归见上。两版本 compileall、旅程门禁自身 20 项、`git diff --check` 通过，预检 8 份 Markdown/67 个本地链接与锚点、780 个 UTF-8 文本无 BOM。此结果只证明固定能力 Run/Command 派发子集；后续自动选点和输入目标固定须重新产生测试及门禁证据。

## F4.3 协调器自动选点、输入目标固定与 poll 凭据 fencing（2026-10-23）

基准为[产物契约 §6.5](../../superpowers/specs/2026-10-20-artifact-capability-integration-contract.md#65-协调器自动选点与输入目标固定子集)、原执行契约与 WF-05；具体实施见[协调器计划](../../superpowers/plans/2026-10-23-capability-coordinator-input-pinning.md)及[poll 凭据修复计划](../../superpowers/plans/2026-10-23-node-poll-credential-fencing.md)。继续沿用 Python 3.10+、Flask/SQLite 与原有签名/Node HTTP，无新依赖。注册哈希查询 O(1)，能力候选有界单次过滤；输入候选 alias 匹配 O(nk)，n≤256/k≤64，无逐节点 SQL。仅增加数据库 I/O port，不建立第二协调器、执行器、状态库或后台线程。

- [x] `hub/domain/capability_routing.py`：不可变管理员路由登记；固定 definition/step/adapter/policy 与可信筛选约束，实际 input digest/active budget 来自原合同，能力和作用域不符拒绝。
- [x] `hub/infrastructure/capability_workflow.py`：只选择已登记且精确匹配的原生 remote Node Run target；实际 artifact 元数据在事务外核对，选点/输入 target 固定/原 receive Command 同事务，签收完成后才走原 admission。原节点只查原节点，未签收时不预留执行槽。
- [x] `capability_repository.py`、`artifact_input_repository.py`、`artifact_input_service.py`：同库外层事务复用与 savepoint 回滚、有界候选限制和输入元数据核验；旧显式 admission 和固定输入准备协议兼容。
- [x] `workflow_coordinator.py`、`workflow_bootstrap.py`：原有界 tick、动态前驱解析、固定 target 与 active dispatch 恢复；仅 capability gate-on/有效签名装配新 admission port。配置缺失保存等待原因，不回退无绑定执行。
- [x] `node_routes.py`、`command_delivery_service.py`、`command_repository.py`：HTTP 将认证指纹传至原 claim 写事务，先复核当前 owner/node/凭据，再回收旧 lease 和领取命令；复用事务管理器关闭/回滚，原因链保留。
- [x] `tests/test_capability_coordinator.py`、`test_node_poll_credentials.py`：真实 SQLite/HTTP/两个 OS 进程，验证输入签收→ready→原 binding/Run/Command，目标不漂移、重启新报告、注册撤销恢复、控制门禁、失败回滚、最后槽争抢，以及普通/能力命令的认证后轮换/禁用。

先在仓库外临时 SQLite/HTTP 复现旧凭据认证后轮换仍返回 200/1 Command、leased/attempt=1，原凭据已不能认证。稳定回归 `/tmp/agent-fleet-node-poll-credentials-before.xml` 为 14 failed；透传指纹并在 claim 事务复核后 12 passed，另两处为测试将原认证拒绝误写成 401，按实际 403 合同修正后 14 passed。新凭据可领取原 queued 命令，旧请求不改 outbox/attempt/旧 lease；服务和数据库异常原因链另测。关联集合 Python 3.13/3.10 分别 262 passed/24.91 秒、262 passed/21.96 秒。

协调器初始 `/tmp/agent-fleet-capability-coordinator-before.xml` 缺新类型而收集失败；接线后 `/tmp/agent-fleet-capability-coordinator-wiring.xml` 为 2 failed/78 passed，一处是测试误读原列名 config（实际 config_snapshot），另一处复现 executor 缺失在 pending 步骤错误调用等待状态转换。修正为原 Workflow 等待投影后 core 集合 123 passed/14.18 秒。扩展文件在增加无签名配置用例之前 26 passed/4.53 秒；最终预检含该用例与 poll/门禁/解析/接收为 97 passed/8.79 秒。

中间树的 Python 3.10/3.13 关联集合为 569 passed/47.32 秒与 572 passed/54.30 秒，范围包括原 Node/Hub、显式目录/admission、固定协调器/dispatch、输入解析、收发、预算/控制和 gate；3.13 额外包括前端合同。新增七个边界实例及最终源码必须重新验证，下方记录最终结果，不沿用这些中间数字。

NODE-01 增至 193 selectors，WF-08 为 148；完整 WF-05 继续不登记为完成。能力 receive/export 仍走原独立协议，unknown/isolated 能力 postcheck、Linux/Windows、两台获准真实机器、真实 CLI/model、生产隔离与 F5 外部接入仍开放。默认 gate 保持关闭；未提交、推送、合并、部署、下载运行时、删除用户文件或访问远程节点。

### F4.3 协调器与 poll 最终验证

最终关联集合在新增前驱输出解析用例后，Python 3.10.21 **599 passed**（`/tmp/agent-fleet-f4-3-coordinator-verified-python310.xml`，JUnit 47.733 秒），Python 3.13.7 **619 passed**（`...-verified-python313.xml`，JUnit 59.434 秒）；两者均无 failure/error/skip。3.13 集合额外覆盖前端合同，不能将两个集合相加当作一次运行。

完整门禁前两次运行分别为 1 failed/4500 passed：`/tmp/agent-fleet-f4-3-coordinator-final-gate/` 的账号浏览器等待了外部验证码脚本的 load，`...-final-gate-retry/` 的助手浏览器在 SPA 路由已更新后等待导航 load。保留失败日志及报告。`accounts_browser.cjs` 改为等待 DOM 就绪；`assistant_auth_browser.cjs` 对同文档历史导航和已更新路径等待实际 URL/DOM 状态，并在侧栏布局测量前等待两帧。没有放宽页面、账号、草稿、缓存或几何断言。真实挂起第三方验证码资源的两条诊断流程通过，相关浏览器集合 11 passed 后重新冻结全部文件；不能用前两次失败报告代替最终结果。

```bash
FLEET_PLAYWRIGHT_MODULE=/tmp/agent-fleet-f4-1-browser-deps/node_modules/playwright \
FLEET_BROWSER_CHANNEL=chrome \
FLEET_SCREENSHOTS=/tmp/agent-fleet-f4-3-coordinator-final-gate-verified/browser \
PYTHONPATH=. python3 -m pytest tests -q --require-journeys \
  --journey-report=/tmp/agent-fleet-f4-3-coordinator-final-gate-verified/journeys.json \
  --junitxml=/tmp/agent-fleet-f4-3-coordinator-final-gate-verified/results.xml
```

最终结果 **4501 passed、5 skipped、195 subtests passed，564.25 秒**。JUnit suite 汇总 4701 tests，实际 4506 个 testcase 元素、0 failures/0 errors、5 skipped；保留 pytest/JUnit 的独立统计，不把 subtests 再加到 testcase 元素数。FILES-01 为 120 selectors/230 cases，NODE-01 为 193/408，WF-08 为 148/244；WF-01/02/03/07 分别为 62/119、73/174、126/206、14/14，均全部通过。全矩阵去重后 1036 个 selector、2008 个参数实例，其中 2005 通过、3 个 Linux 必需实例未运行。

严格门禁退出 **1**，`ci_status=failed`，**40/41 journeys** 通过；唯一 incomplete 为 RELEASE-01 的三个 Linux pidfd/`/proc` selector：停止匹配父进程的子进程、拒绝错误父进程身份、TERM 无效后 KILL 升级。另两个 Supervisor 真实 pause/resume 用例因 `/proc` 不可用跳过。未删 selector、降低门禁或把 skip 视为通过。MODEL-LIVE、MAIL-LIVE、NODE-LIVE、DEPLOY-LIVE、MUSE-LIVE 五项外部验收仍全部 `not_run`。

证据目录 `/tmp/agent-fleet-f4-3-coordinator-final-gate-verified/` 保存 `configuration.json`、`process-result.json`、`pytest.log`、`results.xml`、`journeys.json`、`source-digests.json`、`verification.json` 和 28 张 PNG 截图。源码基线 `4cd816230fd1d8d5bf02c9f086959d8b5b1a27cf`、dirty=true，Python 3.13.7、macOS 26.6.2 arm64、Playwright 1.63.0、Chrome 156.0.8078.47；矩阵 SHA-256 `5484770086d6c249b2583a031e08956b6c8a5cf3bf7ec48f4a84e400fb261d4d`。门禁完成后、补录前重新读取 Git 文件清单，788 个冻结文件无新增/缺失/变化；配置与报告的 revision/dirty/矩阵摘要一致，28 个 PNG 签名有效。补录只改变本进度及两个关联计划，运行时代码和矩阵保持冻结摘要。

门禁前 Python 3.10/3.13 compileall、Node fixture 语法、UTF-8 无 BOM、119 份 Markdown 的 198 个本地链接/锚点及 `git diff --check` 通过；补录后再次核验文档与编码。这里仅闭合协调器自动选点/输入固定和 poll 凭据竞态的本机切片，完整 F4.3/WF-05、unknown/isolated 能力 postcheck、Linux/Windows、真实两机与外部验收均未完成，所有 gate 继续默认关闭。

## F4.3 isolated 资源屏障补齐（2026-10-23）

按[修复计划](../../superpowers/plans/2026-10-23-isolated-reservation-guards.md)核对未知执行边界，发现原 `WorkflowRepository.admit_step` 与 `WorkflowControlRepository._regenerate` 仅把 held 当作占用，而已实现的动态能力路由计入 held/isolated。前者会超售全局 target，后者在底层 Run 终态而隔离预约仍未释放时允许 retry/invalidate 创建新代。两个原事务查询现统一统计 held/isolated，保留 released、其他 target 和现有失败码/异常链；没有新状态、表、接口、后台线程或隐式释放。

- [x] `tests/test_workflow_repository.py`：同 owner/跨 owner 的 held/isolated/released，对应拒绝/可用和其他目标对照；拒绝后无部分写入；已有一个 isolated、总容量 2 时两个真实 OS 进程争抢余下一槽只有一个获准。
- [x] `tests/test_workflow_control.py`：隔离未释放的 failed 步骤 retry/invalidate 均拒绝，generation/revision/events/operations 不变，保留 Application→Repository→Domain 原因链。
- [x] `hub/infrastructure/workflow_repository.py`、`workflow_control_repository.py`：原索引/写事务内补齐状态集合；动态路由、释放和 unknown 核对协议不改变。
- [x] 契约、测试入口/链路/验收、开发入口/路线与矩阵同步；NODE-01/WF-02/WF-03/WF-08 现为 195/75/127/151 selectors，完整 WF-05 仍开放。

初始单例 `/tmp/agent-fleet-isolated-fixed-capacity-before.xml` 为 1 failed；扩展 `/tmp/agent-fleet-isolated-reservation-guards-before.xml` 为 **5 failed/4 passed**：两种 owner 的 isolated 容量、两个进程同时获准和 retry/invalidate 两项均复现。最小修复后同一 9 个实例 **9 passed，2.43 秒**，见 `...-guards-after.xml`。Python 3.10.21 / 3.13.7 的关联回归各 **281 passed，14.07 / 16.10 秒**，见 `...-related-python310.xml` / `...-related-python313.xml`，均无失败/错误/跳过。

这些新代码在上方 4501 passed 门禁之后落盘；旧报告不能覆盖本修复。最终树的编译、文档预检及严格全仓门禁结果见下方。能力 unknown/isolated postcheck、完整 WF-05、Linux/Windows、真实两机、真实 CLI/model、生产隔离和 F5 外部接入仍未完成；gate 默认关闭，主工作树和未提交的其他工作保留。

### F4.3 isolated 资源屏障最终门禁

在修复计划所列前置检查后，针对含本修复的冻结源码、规格和测试矩阵执行完整浏览器启用 `--require-journeys`：

```bash
PYTHONPATH=. \
FLEET_PLAYWRIGHT_MODULE=/tmp/agent-fleet-f4-1-browser-deps/node_modules/playwright \
FLEET_BROWSER_CHANNEL=chrome \
FLEET_SCREENSHOTS=/tmp/agent-fleet-isolated-reservation-final-gate-2/browser \
python3 -m pytest tests -q --require-journeys \
  --journey-report=/tmp/agent-fleet-isolated-reservation-final-gate-2/journeys.json \
  --junitxml=/tmp/agent-fleet-isolated-reservation-final-gate-2/results.xml
```

pytest 实际结果为 **4510 passed、5 skipped、195 subtests passed，595.17 秒**，进程退出码为 **1**。JUnit 报告 suite 计数 4710、4515 个 testcase 元素、0 failures、0 errors、5 skipped；selector 矩阵去重后 1039 个 selector、2017 个实例，其中 2014 个通过、3 个 incomplete。40/41 个 journey 通过；只有 RELEASE-01 未闭合，原因是本机 macOS 无法运行三个要求 Linux pidfd 与 `/proc` 的 selector。另两个 Supervisor `/proc` pause/resume selector 在 JUnit 中按平台条件跳过。未删除 selector、降低 required gate 或把平台 skip 计为通过。

修复所属的 NODE-01 为 195 selectors/415 实例，WF-02 为 75/181，WF-03 为 127/208，WF-08 为 151/253；这些 journey 均通过。FILES-01 为 120/230 并通过。完整 WF-05 仍未完成。MODEL-LIVE、MAIL-LIVE、NODE-LIVE、DEPLOY-LIVE、MUSE-LIVE 均为 `not_run`。

证据保存在 `/tmp/agent-fleet-isolated-reservation-final-gate-2/`，包括 `configuration.json`、`source-digests.json`、`verification.json`、pytest/JUnit/journey/退出码及 28 张有效 PNG。报告绑定基线 `4cd816230fd1d8d5bf02c9f086959d8b5b1a27cf`、`dirty=true`、789 个冻结文件和矩阵 SHA-256 `194083103d433cba51eee1fd09630b9a5fbf8006eb960984c0a2b25e6507f2f8`；门禁后、补录文档前重新核对，文件无增删或内容变化，revision/dirty/matrix 摘要与报告一致。运行环境为 Python 3.13.7、macOS 26.6.2 arm64、Playwright 1.63.0、Chrome 156.0.8078.47。

Python 3.10.21 与 3.13.7 的 `compileall`、16 个 Node 前端 fixture 语法检查通过；Markdown/编码核验为 120 份 Markdown、246 个行内链接、206 个有效本地链接/锚点、零错误，787 个 UTF-8 文本和 2 个二进制文件均通过 UTF-8 无 BOM/AST 检查。`git diff --check` 在本次进度记录补录后再核验。该门禁证明本机测试范围内的实现和回归；不替代 Linux 零 skip 验收、能力 postcheck、完整 WF-05、Windows、真实两机或任何外部 live 验收，所有 gate 继续默认关闭。


## F4.3 capability-bound unknown/isolated workspace postcheck（2026-10-24）

本切片以[产物契约 §6.4](../../superpowers/specs/2026-10-20-artifact-capability-integration-contract.md#64-hub-固定能力-run-与-command-派发子集)、[工作流执行契约 §7](../../superpowers/specs/2026-10-20-workflow-execution-contract.md#7-版本变更重试和恢复)和[postcheck 计划](../../superpowers/plans/2026-10-23-capability-postcheck.md)为唯一基准。实现只扩展既有 `CommandPostcheckService`、`CapabilityDispatchRepository`、`NodeRuntime`/`NodeJournal` 与本地目录 backend；没有新 admission、预算、generation、Run、reservation、worker 或依赖。独立 `workspace.digest` helper 必须事先存在于 Workflow contract/binding、Hub action registration、Node local definition/allowlist/offer；派发 action 固定为 `reconcile.workspace.digest`，retry 固定为 `read_only`。

- [x] Hub 从 owner-scoped unknown `tool.workspace.write` 私有参数创建唯一 descriptor 和 deterministic check id；公开 POST/GET 只返回 path、状态和摘要 evidence，不返回 content、expected digest 或 `_fleet_capability`。
- [x] Hub 以 descriptor 回链原 binding、generation、payload hash、owner/node/resource/Run 和当前 unknown Workflow/Step/dispatch；只接受 `held`/`isolated` reservation，gate-off、pause/drain/frozen、released、stale report、definition/action/上下文/状态漂移均 fail closed。
- [x] Node 通过真实 loopback HTTP poll/receipt 和 Ed25519 签名执行独立摘要；本地文件按 1 MiB 块流式 SHA-256，有界大小与错误码。`held/isolated × matched/mismatch/read_failed` 六个组合均通过；原写入实际执行但丢回执时，摘要只核对当前文件。
- [x] Node journal 完成记录重放只查回结果，不再次写入或摘要；重复 POST/GET 只保留一个 check command 和一条 reconciliation evidence。原 Command、Run、Workflow step/execution、预算 settlement 与 reservation snapshot 保持 `unknown`/`held` 或 `isolated`。
- [x] 负向回归覆盖 helper 未声明/登记缺失或不唯一、foreign/missing descriptor、wrong path/node/run、原 binding/context/payload/generation/owner/resource/definition/report/deadline/cancel 状态变化，以及 gate-off/pause/drain/frozen。

聚焦验证（Python 3.13.7）为 `tests/test_capability_dispatch.py`、`tests/test_node_capabilities.py`、`tests/test_capability_http.py`、`tests/test_capability_coordinator.py`、`tests/test_node_poll_credentials.py`、`tests/test_platform_postcheck.py` 共 **208 passed，21.92 秒**；其中本切片的真实 HTTP 摘要组合为 6 passed。Python 3.10、compileall、前端 fixture、编码/链接和完整 `--require-journeys` 门禁须在本节冻结后重新执行；完整 WF-05、Linux/Windows、两台真实机器、生产隔离和五项 external live 仍不宣称完成。

### F4.3 capability-bound postcheck final local gate (2026-10-24)

冻结前重新执行 Python 3.13/3.10 聚焦回归、compileall、前端 fixture 语法、UTF-8/AST、Markdown 本地链接和 `git diff --check`。聚焦集合为 Python 3.13.7 **259 passed，26.78 秒**，Python 3.10.21 **217 passed，17.08 秒**；前置检查报告 793 个 UTF-8/AST 文件、122 份 Markdown 的 209 个本地链接/锚点，全部通过。

随后冻结 revision `4cd816230fd1d8d5bf02c9f086959d8b5b1a27cf`、dirty=true、Git 源文件 790 个及矩阵 SHA-256 `0a07e90517d6f1681e8c8eaa25765ffb5924bcb36b90bee29ca499951eb6a550`，运行完整浏览器启用门禁：

```bash
PYTHONPATH=. \
FLEET_PLAYWRIGHT_MODULE=/tmp/agent-fleet-f4-1-browser-deps/node_modules/playwright \
FLEET_BROWSER_CHANNEL=chrome \
FLEET_SCREENSHOTS=/tmp/agent-fleet-capability-postcheck-final-gate-4vBYJP/browser \
python3 -m pytest tests -q --require-journeys \
  --journey-report=/tmp/agent-fleet-capability-postcheck-final-gate-4vBYJP/journeys.json \
  --junitxml=/tmp/agent-fleet-capability-postcheck-final-gate-4vBYJP/results.xml
```

结果：**4578 passed、5 skipped、195 subtests passed，573.95 秒**，严格退出 1；NODE-01 **205 selectors passed**，WF-08 **161 selectors passed**。唯一 incomplete 是 RELEASE-01 的 3 个 Linux pidfd/`/proc` 必需实例，macOS 无法执行；另外 2 个 Supervisor `/proc` 用例按平台跳过。没有删除 selector、降低 required gate 或把 skip 计作通过；MODEL-LIVE、MAIL-LIVE、NODE-LIVE、DEPLOY-LIVE、MUSE-LIVE 均为 `not_run`。

门禁后的 Git 源文件摘要与冻结前一致（790/790，无增删或内容变化）；运行只生成了仓库外 `var/` 测试数据库变化，不纳入源码摘要。该证据覆盖本机 POSIX/loopback capability-bound unknown/isolated workspace digest slice；不覆盖 Linux/Windows、真实两机、生产隔离、完整 WF-05 或外部 live 验收，所有 gate 继续默认关闭。

### F4.3 capability-bound postcheck final verified gate (2026-10-24)

在修正一个既有 adoption guard 测试夹具的伪 PID 与 `received_at` 十进制子串碰撞后，单独复跑该回归 **7 passed，0.19 秒**；生产控制客户端未改动。随后以当前矩阵重新冻结：revision `4cd816230fd1d8d5bf02c9f086959d8b5b1a27cf`、dirty=true、Git 源文件 790 个、矩阵 SHA-256 `03917fca1886b27fe5e5ada2c2b603e71feaec880481be914efea283d68eb544`。

最终完整浏览器启用门禁证据目录为 `/tmp/agent-fleet-capability-postcheck-final-gate-final-r1lPyI/`，结果 **4578 passed、5 skipped、195 subtests passed，579.34 秒**，严格退出 1。NODE-01 为 **206 selectors passed**，WF-08 为 **162 selectors passed**；唯一 incomplete 为 RELEASE-01 的三个 Linux pidfd/`/proc` 必需 selector，另两个 Supervisor `/proc` 用例按平台跳过。没有删除 selector、降低 required gate 或将 skip 计为通过；MODEL-LIVE、MAIL-LIVE、NODE-LIVE、DEPLOY-LIVE、MUSE-LIVE 均为 `not_run`。门禁后的 790 个 Git 源文件逐项摘要与冻结前一致，无增删或内容变化；运行产生的 `var/` 测试数据库变化不属于 Git 源文件。

该最终证据覆盖 capability-bound unknown/isolated workspace digest 的本机 POSIX/loopback、真实签名 poll/receipt、Node journal 重放、helper 预声明与 fail-closed 负向边界；不覆盖 Linux/Windows、两台真实机器、生产隔离、完整 WF-05 或外部 live 验收。所有 workflow/dispatch/transfer/capability gates 继续默认关闭。

## F5 本机集成边界（2026-10-24）

本切片依据[产物接入契约 §7](../../superpowers/specs/2026-10-20-artifact-capability-integration-contract.md#7-工具接入与-mcp)和[F5 计划](../../superpowers/plans/2026-10-24-f5-integrations.md)。三个新增 gate 默认关闭；应用构建不发现外部工具、不发送外部任务、不启动新 worker。沿用 Flask/SQLite、原 Workflow application/repository、LocalRunWorkerService、ToolBroker 与有界 HTTP transport；既有 jsonschema 现在明确列入运行依赖，不引入第二套调度/存储。

- [x] `tools/platform/connectors/mcp.py`：固定 HTTPS/SecretRef、只读 risk/retry、100 工具/256 KiB schema/32 KiB 单 schema/depth 16、输入/输出校验、完整结果限长与脱敏、JSON/SSE 单次 HTTP 调用。精确私网 endpoint→IP alias、DNS/连接绑定、TLS、无代理/redirect/replay；超时保留 unknown。
- [x] `hub/application/connector_catalog.py`、`tools/platform/tool_broker.py`、`hub/application/run_worker_service.py`：进程内管理员登记、固定 schema/revision、待确认版本单调编号与并发发布 fencing；短 metadata 临界区不跨 I/O，调用保留独立固定快照。工具暴露/执行核对 owner/workspace/显式 allowlist/lease，MCP 只占 20 个模型工具总额度的余量。真实本机 Run 经注入模型与独立 HTTPS 对端完成一次调用；gate-off/验收只读策略拒绝。
- [x] `tools/platform/external_agents/base.py`、`muse.py`：强类型能力、稳定任务引用、接收/Agent 自报完成/unknown 分离；外来 task 不能成为成功证据。Muse 仅包装原受控 bridge，不复制账号/审批策略；取消与导出明确 unsupported。
- [x] `hub/application/integration_identity.py`、`hub/http/fleet_mcp_routes.py` 与原 Workflow service/repository：独立 Bearer、协议头/metadata、严格 64 KiB JSON、ID 安全范围、scope/模板/工作区/总预算前置校验。只公开 create/get/list_artifacts；namespace token 和原 workflow_runs 在同事务提供持久归属，身份对象重建后仍能查原流程，同 token 不同有效内容冲突。账号中间件沿用机器认证标记并保留独立 Bearer 守卫。
- [x] `tests/test_mcp_connector.py`、`test_mcp_http.py`、`test_external_agent_adapter.py`、`test_fleet_mcp_boundary.py` 与 worker gate 回归已登记 `WF-06`，共 66 个 required selector。新增 `MCP-LIVE` 和 `EXTERNAL-AGENT-LIVE`，保持 `not_run`。

协议选择为 `2026-07-28`，依据官方 [Streamable HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)、[versioning](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning) 与 [schema](https://modelcontextprotocol.io/specification/2026-07-28/schema)。独立对端核对 stateless discovery、version/method/name header、JSON/SSE 与匹配请求 ID；本机 client 不替代真实外部服务试点。

本轮先失败后修复的回归包括：身份重建后 403、预算越限仍写入、协议头/请求 ID 漏检、结构化结果未计限额、schema/allowlist 漂移、取消证据自相矛盾、账号模式 MCP 被 Origin guard 拦截、SSE 最终响应后仍等 EOF、不同 pending schema 复用 revision，以及邮箱 owner 被 connector 名称规则误拒。SSE 逐字节 CR/LF/UTF-8 和多行 data、通知、畸形/超限/未完成流与 timer/socket 回收均有实际 TLS 断言。模型工具总数的回归先复现 35 个，再固定至最多 20。

最终关联集合覆盖四个 F5 模块及原 worker/gate/HTTP/provider/Muse/account 回归，Python 3.13.7 **255 passed，34.30 秒**，Python 3.10.21 **255 passed，37.49 秒**；均无失败、错误或跳过。JUnit 为 `/tmp/agent-fleet-f5-final-python313.xml` 和 `/tmp/agent-fleet-f5-final-python310.xml`。两个解释器的 `compileall -q hub tools tests` 及 16 个前端 CJS fixture 的 `node --check` 均通过；未配置静态类型检查器，compileall 不等于类型证明。当前完整门禁另行冻结并运行，结果在本节后补录。

本机实现不等于完整 F5：目录/集成身份仍依赖管理员进程内注入，未实现持久化多进程目录/撤销管理、集成配置 UI、外部 Agent Workflow 派发，未开展获准真实 MCP/Muse 试点。声明的 Muse 幂等/lookup 只复用原 bridge 合同。Linux/Windows、真实两机、生产隔离及其他 live 验收继续开放，所有 gate 保持默认关闭，主工作树及并行工作保留。

### F5 完整本机门禁与复跑（2026-10-25）

在上述聚焦验证与预检后冻结 803 个 Git 源文件，基线 revision 为 `4cd816230fd1d8d5bf02c9f086959d8b5b1a27cf`、dirty=true、矩阵 SHA-256 为 `d8c3432e7d1cb68fbfddaeb86478978376d7d18405c6bc9a95cee0d9d8e177c6`。完整浏览器启用门禁保留全部 required selector，不把 macOS 的平台跳过计作通过。

| 运行 | 实际结果 | 脱敏证据 |
|---|---|---|
| 首轮完整门禁 | 3 failed、4706 passed、5 skipped、195 subtests passed，576.70 秒；39/42 journeys 通过，WF-06 通过 | `/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-f5-final-gate-f1kksfc0/` |
| 原失败场景单独复跑 | Muse 工作区、会话问题面板和 Git diff 两个管道参数实例共 4 passed，60.57 秒 | `/tmp/agent-fleet-f5-failure-repro.xml` |
| 相同冻结源码完整复跑 | **4709 passed、0 failures、0 errors、5 skipped、195 subtests passed，589.73 秒**；41/42 journeys 通过，严格退出 1 | `/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-f5-full-repeat-20261025-4yhks58r/` |

首轮失败分别为 Muse 复制消息按钮在滚动阶段超时、会话 confirm 问题等待 succeeded 回执超时，以及关闭 stdout/stderr 后的 diff 截止测试耗时 4.71 秒而超过 3 秒断言。读取调用栈、HTTP 请求记录、前端轮询和子进程回收路径后，尚无可证实根因；单独与完整复跑均未再现，因此没有修改生产逻辑、放宽断言、增加重试或提高超时。这三项保留为间歇失败风险，复跑通过不作为“已修复”的证据。

完整复跑命令如下；环境为 Python 3.13.7、macOS 26.6.2 arm64，复用既有 Playwright/Chrome：

```bash
F5_EVIDENCE=/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-f5-full-repeat-20261025-4yhks58r
PYTHONPATH=. \
FLEET_PLAYWRIGHT_MODULE=/tmp/agent-fleet-f4-1-browser-deps/node_modules/playwright \
FLEET_BROWSER_CHANNEL=chrome \
FLEET_SCREENSHOTS="$F5_EVIDENCE/browser" \
python3 -m pytest tests -q --require-journeys \
  --journey-report="$F5_EVIDENCE/journeys.json" \
  --junitxml="$F5_EVIDENCE/results.xml" --tb=short -rs
```

JUnit 的 suite 计数为 4909（含 subtests），实际 testcase 元素为 4714；failures=0、errors=0、skipped=5。WF-06 的 **66 selectors / 131 参数实例全部通过**；唯一 incomplete 为 RELEASE-01 的三个 Linux pidfd/`/proc` 必需 selector，另两个 Supervisor `/proc` 用例按平台跳过。严格退出码和 `ci_status=failed` 保留，没有删除 selector 或降低门禁。MODEL-LIVE、MAIL-LIVE、NODE-LIVE、DEPLOY-LIVE、MUSE-LIVE、MCP-LIVE、EXTERNAL-AGENT-LIVE 七项均为 `not_run`。

证据目录包含 configuration、803 文件的 source-digests、pytest 日志、JUnit、journey、退出码、verification 和 results-summary；28 张 PNG 的签名、各 chunk CRC、尺寸与 IEND 完整性均通过。两轮完整运行前后 803 个源文件摘要均一致，无增删或内容变化，矩阵/revision/dirty 与报告一致；本节补录属于门禁之后的文档更新。补录前再次运行 Python 3.10.21/3.13.7 的 compileall 及 16 个前端 CJS fixture 语法检查，均通过。文档补录后的 UTF-8/链接检查见同目录 `post-doc-preflight.json`：801 个文本、565 份 Python 3.10 AST、122 份 Markdown、219 个本地链接/锚点，零错误；`git diff --check` 通过。门禁后仅五份 Markdown 更新，运行时代码与测试矩阵未改，不将文档修改冒充另一次运行时门禁。

F5 本机边界和当前本机回归已有上述证据；Linux 零 skip 门禁、Windows、真实两机、生产隔离、真实 MCP/Muse、持久化多进程集成管理与外部 Agent Workflow 派发仍未完成。三个 F5 gate 继续默认关闭，完整 F5 和生产就绪状态保持开放。

## MCP Run 授权与定义持久固定（2026-10-25）

本轮对照[工具接入契约 §7.1/7.5](../../superpowers/specs/2026-10-20-artifact-capability-integration-contract.md#75-run-工具授权与定义固定)与[固定计划](../../superpowers/plans/2026-10-25-mcp-run-pinning.md)，复现排队 Run 在 worker 启动时借用当前进程目录的问题：已批准新 schema 被旧 Run 执行；目录重启后 revision 再次为 1 使异定义被接受；gate-off/空目录时提交的 Run 后来获得新工具权限。首批回归 **5 failed / 1 passed**，扩展类型边界后 **17 failed / 1 passed**，JUnit 分别为 `/tmp/agent-fleet-mcp-run-pinning-before.xml` 与 `/tmp/agent-fleet-mcp-run-pinning-expanded-before.xml`。根因是 Run 没有持久工具授权，单个进程 revision 不能证明跨重启的定义一致。

- [x] `hub/domain/mcp_tool_pins.py`：纯领域不可变 pin/parser；严格 public name、正整数安全范围、SHA-256、字段和最多 20 项限制，拒绝 bool、null 与额外字段。
- [x] `hub/application/connector_catalog.py`：摘要涵盖管理员配置/作用域/时间和结果上限、协议/server 版本、input/output schema 与描述；快照仅返回 name/revision/digest 的拷贝，不存 endpoint、schema、描述或 SecretRef 原文。选中项用字典逐项精确比较；调用 O(1)，短锁不跨 I/O。
- [x] `hub/application/conversation_service.py`、`hub/bootstrap.py`：新本地原生 Run 在原 append_turn 事务保存固定 pins；重复 token 先查回原配置。客户端 overrides 不得提供它们，公共 Run DTO 不返回它们。
- [x] `tools/platform/tool_broker.py`、`hub/application/run_worker_service.py`：模型声明前和实际调用前均复核；worker 明确传入原 config_snapshot，无 pin/空 pin 不从当前目录补权限。Workflow target 只接受管理员事先登记的不可变 pins，不在 dispatch/recovery 动态取目录。
- [x] `tests/test_mcp_run_pinning.py`：12 个 selector、34 个参数实例，涵盖上述旧 Run 漏洞、同内容重启对照、endpoint/scope/上限/定义漂移、损坏快照、声明后变更、目录/开关撤权、同 token 与脱敏，以及同一 SecretRef 背后轮换凭据仍可调用。WF-06 增至 78 个 selector / 165 个参数实例；外部检查保留。

初次 12 模块关联回归在 Python 3.13.7 为 **318 passed，42.41 秒**，Python 3.10.21 为 **318 passed，46.44 秒**，均无失败、错误或跳过。证据为 `/tmp/agent-fleet-mcp-run-pinning-related-python313.xml` 与 `/tmp/agent-fleet-mcp-run-pinning-related-python310.xml`；模型、SecretBroker 与 MCP transport 使用注入 fixture，Flask/SQLite/Run worker 为实际实现。后续撤权修复和最终门禁分别记录如下；此前 4709 项结果不覆盖这些新代码。

### 撤权边界与完整门禁收尾

首轮冻结 806 个 Git 文件，矩阵摘要 `b8c71f14942ed8cbf2aea7c623ac50dc1c417c22ab801d4bb3bbb96c901cdca9`，完整浏览器门禁为 **4739 passed、1 failed、5 skipped、195 subtests passed，653.42 秒**；WF-06 的 77 selectors / 162 cases 全部通过，40/42 journeys 通过。唯一失败是 `tests/test_account_browser.py::test_account_browser` 等待“密码已重置，请使用新密码登录。”超时。该测试随后在未改源码时单独复跑 **1 passed，4.09 秒**，没有修改断言或 timeout，原因未确定，不声明已修复。首轮完整证据为 `/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-mcp-run-pinning-full-20261025-rusx_90e/`，单独复跑 JUnit 为 `/tmp/agent-fleet-mcp-pin-account-repro.xml`。已核对矩阵/版本/dirty 状态、JUnit、运行期间源码摘要不变和 28 个 PNG 的 CRC、尺寸、IEND；严格退出 1，AUTH-01 和 RELEASE-01 incomplete，全部七项外部检查 `not_run`。

随后追加撤权核对，三个独立失败证明：目录关闭、目录缺失或 gate 撤销后，原固定 Run 仍先请求模型再拒绝 MCP 工具。证据为 `/tmp/agent-fleet-mcp-run-revocation-before.xml`（3 failed）。ToolBroker 现在独立解析显式 pins，非空原授权遇关闭 gate/缺失目录立即报 `mcp_disabled`；目录关闭且选中 pins 非空在生成声明前报 `mcp_connectors_disabled`。无 pin 的既有 gate-off 调用不变；不补权限、不重放。修复后 pinning/catalog 子集 **92 passed，3.66 秒**，JUnit 为 `/tmp/agent-fleet-mcp-run-revocation-after.xml`。

最终 12 模块关联集合在 Python 3.13.7 **321 passed，25.09 秒**，Python 3.10.21 **321 passed，27.58 秒**，JUnit 为 `/tmp/agent-fleet-mcp-run-pinning-final-related-python313.xml` 和 `/tmp/agent-fleet-mcp-run-pinning-final-related-python310.xml`。两个解释器的 compileall 再次通过，WF-06 实际 collection 为 78 selectors / 165 cases。完整门禁需要使用修复后的新摘要，不能沿用首轮结果。

### 修复后的最终门禁

修复后冻结的当前树完成完整浏览器门禁：**4743 passed、0 failures、0 errors、5 skipped、195 subtests passed，597.29 秒**。JUnit 实际有 4748 个 testcase 元素，suite 的 4943 计数包含 subtests，不额外叠加。WF-06 的 **78 selectors / 165 cases 全部通过**，全部 journeys 为 41/42 通过。唯一 incomplete 为 RELEASE-01 的三个 Linux pidfd/`/proc` 必需用例；另外两个 Supervisor `/proc` 用例按平台跳过。保留原 required selectors；严格退出 1、`ci_status=failed`，不是发布门禁通过。

证据目录为 `/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-mcp-run-pinning-full-20261025-4tyl0nwf/`，含 `configuration.json`、`command.json`、`source-digests.json`、`source-digests-after.json`、`pytest.log`、`results.xml`、`journeys.json`、`exit.json`、`verification.json` 与 `browser/`。运行时 revision 为 `4cd816230fd1d8d5bf02c9f086959d8b5b1a27cf`，branch 为 `codex/workflow-platform-docs`，dirty=true；806 个 Git 文件运行前后摘要完全一致。矩阵 SHA-256 为 `e2062d2927c3a894700251a0e18c493b544f29f8726f574d2265abc13de78c3f`，配置、journey 报告与冻结矩阵一致。28 个 PNG 的签名、CRC、正尺寸和终止 IEND 逐项有效；七项外部检查均为 `not_run`。

```bash
PIN_EVIDENCE=/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-mcp-run-pinning-full-20261025-4tyl0nwf
PYTHONPATH=. \
FLEET_PLAYWRIGHT_MODULE=/tmp/agent-fleet-f4-1-browser-deps/node_modules/playwright \
FLEET_BROWSER_CHANNEL=chrome \
FLEET_SCREENSHOTS="$PIN_EVIDENCE/browser" \
python3 -m pytest tests -q --require-journeys \
  --journey-report="$PIN_EVIDENCE/journeys.json" \
  --junitxml="$PIN_EVIDENCE/results.xml" --tb=short -rs
```

本机最终检查还包括 Python 3.10/3.13 compileall、16 个前端 CJS 语法、804 个 UTF-8 无 BOM 文本、567 个 Python AST、八份当前文档的本地链接及 diff 空白检查。未配置静态类型检查器，不将编译或 AST 检查声明为类型证明。最终完整运行中账号浏览器用例通过；首轮提示等待超时仍保留为未定位的间歇风险，没有声称根因修复。生产质量复核范围限于本轮 pin/parser、目录比较、提交/worker/broker 接线与相关测试；不把这些结果扩展为整个未提交平台改动的生产认证。门禁后仅补录文档证据，运行时、测试和矩阵不再改动。

本轮不新增表、worker、端点或依赖。三个 F5 gate 默认关闭；外部 Agent Workflow 派发、持久化多进程管理、Linux/Windows、获准真实两机/模型/CLI、生产隔离和真实 MCP/Muse 试点继续开放。完整平台目标保持 active；主工作树和并行工作保留，未提交、推送、合并、部署或调用外部真实服务。

## 外部 Agent Workflow 派发与原任务恢复（2026-10-25）

依据[接入契约 §7.6](../../superpowers/specs/2026-10-20-artifact-capability-integration-contract.md#76-外部-agent-workflow-派发与原任务查回)、执行契约 §5.2 和[外部派发计划](../../superpowers/plans/2026-10-25-external-agent-workflow-dispatch.md)，本切片补齐上节尚未实现的外部 Workflow 派发。复用原 coordinator、dispatcher、token、generation、预算/槽位、取消截止和显式 reconciliation；不新增调度器、队列、执行状态机、HTTP 管理端点或依赖。管理员 target 字典查找均摊 O(1)，dispatch/token 使用数据库索引，历史校验按 rowid keyset 每批最多 128 条；短事务不跨外部 I/O。

- [x] `hub/domain/external_agent.py`、`workflow_external.py`：纯领域能力、任务/状态/取消回执与不可变注册快照。`tools/platform/external_agents/base.py` 保持兼容导出；注册固定 owner/workspace/target、adapter/policy revision、agent_id、六项能力和非秘密配置摘要。严格解析 canonical resolved/target/task，不把损坏快照规范化成可用记录。
- [x] `hub/infrastructure/workflow_external_schema.py`、`workflow_external_repository.py` 及原 admission：注册快照与 intent、预算、槽位在同一事务提交；私有表仅保存注册、send_started_at 和原 TaskRef，不复制执行状态。发送前持久 CAS，接收后固定任务；三类崩溃窗口只从原 token/journal 查回。reader 3 独立识别外部 metadata 与 capability reader 2，gate-off/备份/明文和加密 frozen 恢复均校验原闭包，129 条历史记录验证跨批读取。
- [x] `tools/platform/external_agents/muse.py`、`tools/browser_bridge/muse.py`：显式只读 journal lookup，核对原 scope/prompt；配置摘要包含管理员 account_ref/transport_ref、workspace、journal 绝对路径及显式 initialize 产生的持久身份。摘要读取/lookup 不创建缺失数据库；同路径换库、身份缺失或查询异常均不能成为“尚未发送”的证明。Hub 备份不包含该外部 journal。
- [x] `hub/application/workflow_external_execution.py` 与原 coordinator/bootstrap/operator/dispatch：只有事先登记的 target/adapter 可用；原配置漂移、作用域/账号/版本变化失败关闭。首次 submit 和重入均复核原 admission，lookup 后、实际 submit 前再次检查权限/控制/截止。查询错误不当作不存在，仅 lookup+幂等合同允许同 token/完全相同 payload 重提；不支持者保留 unknown/预约。gate-off 阻止新 admission 与已准入但未发送的 intent，保留已登记原任务的查回/检查。
- [x] `hub/domain/workflow_execution.py`、原 observation/event 持久化、HTTP DTO 与前端 contracts/workflow copy：waiting_input 映射 waiting_external，不生成 Fleet approval；`agent_reported` 只证明来源，不产生 validation、产物或 acceptance。状态和取消回执同时精确匹配原 external_id/session_id，包含 session-only 任务。只有当前任务的 confirmed 取消和已声明能力才可停止；unsupported/未确认到截止仍 unknown，并保留原预约。
- [x] `tools/async_io.py` 与原 dispatch/stop/reconcile：同步边界继续用有界 `run_owned_io`；`wait_owned` 持有异步子任务、只发送一次取消，在重复取消/超时竞争下等自有线程收尾，再传递失败。通过读取 worker 原结果保留 Python 3.10 shield 丢失的取消原因链。
- [x] `tests/test_external_agent_workflow.py`、`test_external_agent_persistence.py` 及 adapter/Muse/async/frontend 关联回归：实际 SQLite、受控 journal、原协调器、两个 OS 进程、HTTP DTO→Node 前端解析器及备份闭包。WF-06 新增 50 selectors / 96 cases，合计 128 / 261，外部检查状态不变。

首版发送完整 canonical resolved JSON，prompt 最多 8192 字符；文件/上下文输入、Node capability_requirements 或 output_specs 在发送前报 `external_contract_unsupported`，不截断、不忽略要求、不伪造 ArtifactRef。Muse cancel/export 仍 unsupported，Agent 自报的完成与费用都不替代独立验证或可信用量。实际网络 transport 注入，未调用真实 Muse、模型或远程节点。

### 失败证据与修复闭环

| 复现 | 根因与修复 | 脱敏证据 |
|---|---|---|
| 显式 lookup 缺失，3 failed | 增加独立 lookup port，查询错误不降级成无记录 | `/tmp/agent-fleet-external-lookup-before.xml` |
| 只读 lookup 创建缺失数据库，1 failed | 查询/身份读取不执行 provision，显式 initialize 单独建 journal 身份；关联复跑 95 passed / 1 浏览器 fixture skip | `/tmp/agent-fleet-external-readonly-lookup-before.xml`、`...-after.xml` |
| 已准入 intent 绕过关闭 gate，1 failed | 原协调器和 executor 在发送前均遵守 gate；复跑 60 passed / 1 浏览器 fixture skip | `/tmp/agent-fleet-external-coordinator-gate-before.xml`、`...-after.xml` |
| 非规范 resolved 被重新规范化接受，1 failed | 严格比较原 resolved 快照；复跑 72 passed | `/tmp/agent-fleet-external-snapshot-before.xml`、`...-after.xml` |
| 前端拒绝 provenance 字段，1 failed | 保持强类型 evidence_source，实际 HTTP 与解析器贯穿；复跑 112 passed / 1 浏览器 fixture skip | `/tmp/agent-fleet-external-frontend-before.xml`、`...-after.xml` |
| lookup 期间 workspace 撤权仍发送，1 failed | lookup 返回后重新检查原 admission；原场景复跑 1 passed | `/tmp/agent-fleet-external-query-revocation-before.xml`、`...-after.xml` |
| Python 3.10 重复取消提前离开 send 线程，关联集合 1 failed | wait_owned 显式保留/收尾子任务并获取原错误链；两个解释器各 101 passed | `/tmp/agent-fleet-external-final-related-python310.xml`、`...-owned-io-after-python310.xml`、`...-owned-io-after-python313.xml` |
| session-only/复合任务缺失或外来 session 被接受，5 failed / 1 passed | Status/CancelReceipt 末尾增加可选 session_id，保留既有位置参数；状态/取消都精确匹配两个身份字段。两个 repo selector 覆盖 12 种组合；最终双运行时/完整门禁通过 | `/tmp/agent-fleet-external-session-before-resumed.xml`、`/tmp/agent-fleet-external-session-related-python310.xml`、`...-python313.xml` |

session 最初聚焦复跑为 72 passed；仓库外复现文件使 pytest rootdir 成为 `/`，产生一次 cache 写入警告。后续 repo-only 关联与最终完整门禁无该警告。没有隐藏中间失败、放宽断言、增加自动重试或降低 required selector。

session 修复前另有一轮完整运行：`/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-external-workflow-full-20261025-yujmbuhz/`，pytest 输出 **4824 passed、5 skipped、195 subtests passed、1 error、1 warning，589.68 秒**。该次 agent 向自己的 pytest PID 35841 两次发送 SIGINT；已由 deploy 测试安装的 signal handler 在 Komari HTTP server teardown 和 WeakSet 清理期间抛出 `DeployError('deployment_interrupted')`。这是操作者中断产生的无效最终证据，不是已证实的 Komari 生产缺陷，也不宣称由 session 修复解决；报告保留。JUnit testcase 状态为 4823 passed / 5 skipped / 1 error，差异是同一 case 的 call 通过而 teardown 报错。下节未中断的新冻结运行是当前代码证据。

### 最终双运行时回归与完整门禁

20 模块关联集合为 async I/O、capability coordinator/reservations、external adapter/workflow/persistence、Fleet MCP、frontend contracts、MCP pinning、Muse bridge/Fleet/native/workspace、platform backup 及原 Workflow control/coordinator/dispatch/gates/HTTP/repository。Python 3.10.21 **522 passed / 2 skipped，25.03 秒**；Python 3.13.7 **522 passed / 2 skipped，26.09 秒**。关联命令未提供 `FLEET_PLAYWRIGHT_MODULE`，两个跳过分别是 `test_real_chromium_fixed_helper` 和 `test_muse_workspace_in_browser`；它们在下述浏览器启用的 Python 3.13 完整门禁均通过。JUnit 分别为 `/tmp/agent-fleet-external-session-related-python310.xml` 与 `...-python313.xml`，未设置外部 live 配置。

最终完整门禁使用 Python 3.13.7、macOS 26.6.2 arm64、既有 Playwright/Chrome，UTC 开始 `2026-10-24T20:52:45.262646Z`、结束 `2026-10-24T21:02:49.085746Z`。源码基线 `4cd816230fd1d8d5bf02c9f086959d8b5b1a27cf`、分支 `codex/workflow-platform-docs`、dirty=true；814 个 Git 文件运行前后逐项摘要一致。矩阵 SHA-256 为 `466d9f58c64d2b83fedeba332ceb666cbd5421e16afd337b37e93291721db27c`，与配置/journey 报告一致。

```bash
EXTERNAL_EVIDENCE=/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-external-workflow-full-20261025-87lg7q77
PYTHONPATH=. \
FLEET_PLAYWRIGHT_MODULE=/tmp/agent-fleet-f4-1-browser-deps/node_modules/playwright \
FLEET_BROWSER_CHANNEL=chrome \
FLEET_SCREENSHOTS="$EXTERNAL_EVIDENCE/browser" \
python3 -m pytest tests -q --require-journeys \
  --journey-report="$EXTERNAL_EVIDENCE/journeys.json" \
  --junitxml="$EXTERNAL_EVIDENCE/results.xml" --tb=short -rs
```

结果为 **4836 passed、0 failures、0 errors、5 skipped、195 subtests passed，603.31 秒**。JUnit 实际 4841 个 testcase 元素，suite 5036 计数包含 subtests，不重复累加。WF-06 **128 selectors / 261 cases 全部通过**；41/42 journeys 通过。唯一 incomplete 是 RELEASE-01 的三个 Linux pidfd/`/proc` 必需实例，另外两个 Supervisor `/proc` 用例按平台跳过。严格退出 **1**、`ci_status=failed` 保留，不将本机测试无失败表述为发布门禁通过。MODEL-LIVE、MAIL-LIVE、NODE-LIVE、DEPLOY-LIVE、MUSE-LIVE、MCP-LIVE、EXTERNAL-AGENT-LIVE 七项均 `not_run`。

证据目录含 configuration/command、source-digests 前后快照、pytest 日志、JUnit、journey、exit、verification 和 28 张浏览器 PNG；PNG 签名、chunk CRC、正尺寸和 IEND 完整性逐一通过。预检包含 814 个 Git 文件、812 个 UTF-8 无 BOM 文本、574 份 Python AST、20 个 CJS 与两个修改 JS module 语法、9 份 Markdown 链接、两个解释器 compileall 和 `git diff --check`。未配置静态类型检查器，编译/AST 不作为类型证明。文档补录后的检查与源码差异单独保存为证据目录的 `post-doc-preflight.json`、`post-doc-source-comparison.json`；只允许上述文档变化，运行时代码、测试及矩阵保持最终冻结摘要。

质量复核限于本切片的原子注册、发送/lookup、journal、任务身份、owned I/O、provenance 和备份闭包，不扩展为全部未提交平台的生产认证。历史浏览器/管道耗时间歇失败仍未确定根因，新门禁通过不代表修复。外部产物/输入接线、持久化多进程目录/身份管理与 UI、Linux 零 skip 门禁、Windows、获准真实两机/模型/CLI、生产隔离及真实 MCP/Muse 试点继续开放。完整平台目标保持 active；三个 F5 gate 默认关闭，主工作树和其他并行工作保留，未提交、推送、合并、部署、删除用户文件、下载或修改 Obsidian。

## F5 Fleet 集成身份持久化与跨进程撤销（2026-10-25）

依据[接入契约 §7.7](../../superpowers/specs/2026-10-20-artifact-capability-integration-contract.md#77-fleet-集成身份持久化与撤销)及[身份持久化计划](../../superpowers/plans/2026-10-25-fleet-integration-identities.md)，本切片将三工具出口的进程内身份改为共享 SQLite 权威。复用原 Workflow 模板、作用域 token namespace、预算、事务与 DTO；不增加身份服务、后台线程、外部管理端点或依赖。

- [x] `hub/domain/integration_identity.py`：不可变、无 I/O 的登记快照；直接构造和 register 均验证 exact integer、有效引用、32–512 ASCII Bearer、摘要、allowlist/scopes 和 canonical UTF-8 JSON。只保存令牌摘要，域对象 repr 不暴露它；撤销状态不改变登记摘要或原 namespace。`hub/application/integration_identity.py` 保留兼容导出和唯一的认证 I/O port。
- [x] `hub/infrastructure/integration_identity_schema.py`、`integration_identity_repository.py`：独立 schema 1，存在身份域时 Workflow minimum reader 4，原 reader 1/2/3 继续独立。全局、非 partial、BINARY 的 token_digest 唯一索引点查；allowlist 检查均摊 O(1)，登记规范化在最多 128 项内排序。相同登记幂等，不同内容/复用 token 冲突；owner/id + expected digest 撤销保留首次时刻，旧对象/启动注入不能复活凭据。
- [x] 原 bootstrap、Fleet MCP route 和 Workflow operator/store：gate-off 不创建表或执行认证 I/O；启动对象只作幂等登记，没有注入对象的新实例仍能认证原记录。创建的既有回执读取及 admission 写入、详情读取均在原事务复核当前身份，重新检查 owner/namespace、模板定义与请求、工作区和总预算。撤销先提交则不再创建；读取线性化于同一授权/详情快照。不会因此取消或重放已经存在的 Workflow。
- [x] 备份/恢复：包含已撤销身份的全部历史按 rowid keyset 每批 ≤128 校验，覆盖非正 rowid、末批损坏和未来/残缺 schema。明文/加密恢复继续 frozen，只禁用恢复副本的全部集成凭据，源库和 namespace/历史保留，重复登记不能解除恢复禁用。
- [x] 生命周期：operator async 边界复用 `run_owned_io`，重复取消仍等待原事务完成；原 namespace/token 重试查回同一回执。启动历史校验使用 WAL 只读事务，单个写事务仅负责 schema 初始化，扫描不占 SQLite writer。

### 先失败后修正的证据

| 事实与根因 | 修正与回归 | 原始证据 |
|---|---|---|
| 输入授权期间撤销，旧实现仍 HTTP 200 并创建流程；仅应用入口检查身份 | 在回执读取/新建写入的同一事务检查持久身份，原 HTTP 场景通过 | `/tmp/agent-fleet-identity-before.xml`（1 failed）、`...-after.xml`（1 passed） |
| 遗留身份 tuple 装配返回 500；持久层只检查请求模板，未比对实际 definition | 认证 I/O port 类型拒绝为 503；事务核对实际 template/revision/policy；最终集合通过 | `/tmp/agent-fleet-identity-edge-before.xml`（2 failed / 2 passed） |
| partial 唯一索引被当作全局凭据唯一性 | schema 核对非 partial 及 BINARY 精确列，最终集合通过 | `/tmp/agent-fleet-identity-index-before.xml`（1 failed / 1 passed） |
| 运行时未拒绝更高 Workflow reader；启动历史扫描占 writer | 每次认证/授权检查完整兼容 schema；DDL 短写事务后在只读快照扫描。真实第二 SQLite writer 可立即领取；原复现与 repo 用例通过 | `/tmp/agent-fleet-identity-review-before.xml`（2 failed）、`...-review-after.xml`（5 passed） |

最初 HTTP 集合还有一次新增测试引用错误（NameError），已修正测试装配；另一条 gate-off 断言过强，原参数保留不等于发生认证/表写入，测试现验证实际边界。混合 `/tmp` 复现与仓库用例时 pytest rootdir 为 `/`，曾有一次 cache 路径只读警告；最终 repo-only 集合与完整门禁单独记录，不隐藏中间失败，也不把它们写成生产缺陷。

### 最终双运行时与完整门禁

13 模块关联集合包括 identity、Fleet MCP/HTTPS、Workflow HTTP/gates/repository、plain/encrypted backup、external persistence、MCP pinning/connector、async I/O 和 auth matrix。Python 3.10.21 **381 passed、0 skipped，16.03 秒**；Python 3.13.7 **381 passed、0 skipped，16.06 秒**。JUnit 为 `/tmp/agent-fleet-identity-verified-python310.xml` / `...-python313.xml`。包含真实 OS 进程的恢复/撤销/冲突、事务回滚、身份篡改、读取/既有回执/写入前撤销、两种恢复和独立 TLS client；无真实模型/Muse/MCP 服务调用。

复查最后两项修正前，完整门禁 `/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-integration-identities-full-20261025-vq1abl2f/` 为 **4904 passed、5 skipped、195 subtests passed，606.78 秒**，WF-06 152 selectors / 329 cases，通过 41/42 journeys、严格退出 1；819 个文件摘要不变。此轮完整保留为阶段证据，不能覆盖最后两项修正。

最终完整门禁目录：`/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-integration-identities-full-20261025-d7o01hqy/`。Python 3.13.7 / macOS 26.6.2 arm64，既有 Playwright 1.62.1 / Chrome 155；源码基线 `4cd816230fd1d8d5bf02c9f086959d8b5b1a27cf`，分支 `codex/workflow-platform-docs`、dirty=true。UTC 开始 `2026-10-24T22:10:58.974930Z`、结束 `2026-10-24T22:20:58.263621Z`。819 个 Git 文件运行前后摘要一致；矩阵 SHA-256 `946557e483f3b05009bdaf98c6ca3d5ff401ad8bf1ff601da9ca17efc12b190d` 与配置/journey 一致。

```bash
IDENTITY_EVIDENCE=/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-integration-identities-full-20261025-d7o01hqy
PYTHONPATH=. \
FLEET_PLAYWRIGHT_MODULE=/tmp/agent-fleet-f4-1-browser-deps/node_modules/playwright \
FLEET_BROWSER_CHANNEL=chrome \
FLEET_SCREENSHOTS="$IDENTITY_EVIDENCE/browser" \
python3 -m pytest tests -q --require-journeys \
  --journey-report="$IDENTITY_EVIDENCE/journeys.json" \
  --junitxml="$IDENTITY_EVIDENCE/results.xml" --tb=short -rs
```

实际 **4906 passed、0 failures/errors、5 skipped、195 subtests passed，598.62 秒**。JUnit 实际 4911 个 testcase 元素，suite 的 subtest 数不重复累加。WF-06 **154 selectors / 331 cases 全部通过**，新增 26 个 selectors / 70 个实例；全仓 **41/42 journeys**。唯一 incomplete 为 RELEASE-01 的三个 Linux pidfd/`/proc` 必需实例，另外两个 Supervisor `/proc` 用例按平台跳过。严格退出 **1**、`ci_status=failed` 保留；七项外部检查全部 `not_run`，未将 macOS 无测试失败表述为发布 gate 通过。

证据目录保存 configuration/command、source-digests 前后快照、pytest/JUnit/journey/exit/verification 与 28 张浏览器 PNG；验证签名、CRC、尺寸和 IEND。预检为 819 个文件、817 个 UTF-8 无 BOM 文本、578 份 Python AST、20 个 CJS/两个 JS module、双运行时 compileall、Markdown 链接与 diff 检查；未配置静态类型检查器，compileall/AST 不作为类型证明。文档补录后保存 `post-doc-preflight.json`、`post-doc-source-comparison.json` 和锚点检查，确认仅 Markdown 变化，runtime/tests/journey 保持最终摘要。

本机身份持久化切片闭环；完整平台目标继续 active。connector 目录持久化/配置 UI、外部 Agent 文件/上下文/结构化产物、Linux 零 skip 门禁、Windows、获准真实两机/模型/CLI、生产隔离及真实 MCP/Muse 试点继续开放。历史间歇失败的根因仍未确定。所有 gate 默认关闭，主工作树及其他工作保留，没有提交、推送、合并、部署、删除用户文件、下载或修改 Obsidian。

## F5 MCP 目录持久化与跨进程审批（2026-10-25）

依据[接入契约 §7.8](../../superpowers/specs/2026-10-20-artifact-capability-integration-contract.md#78-mcp-目录持久化与跨进程审批)及[目录实施计划](../../superpowers/plans/2026-10-25-mcp-catalog-persistence.md)，本轮将默认生产目录改为共享 platform.db 权威，沿用原 ConnectorCatalog、Run/ToolBroker、SecretBroker 与一次调用语义。没有增加执行器、调度器、管理端点或依赖。显式注入的进程内测试目录保持兼容，不作为默认持久化证据。

- [x] `hub/domain/mcp_catalog.py` 保存纯领域不可变 bytes/版本/撤销事实与强类型错误；`tools/platform/connectors/snapshots.py` 复用原 schema/endpoint 校验，严格编解码配置 ≤8 KiB、发现 ≤512 KiB 的 canonical UTF-8 JSON。拒绝额外字段、重复工具/键、非规范 JSON、伪造 schema hash、bool revision 和外部 schema 引用。保留原 100/20 工具、256 KiB 总 schema、32 KiB 单 schema 与深度 16 限制。
- [x] `mcp_catalog_schema.py` / `mcp_catalog_repository.py`：独立 schema 1、BINARY connector ID 点查，最多 128 个历史登记。配置完整一致才幂等；public-name 歧义、配置漂移和复用 ID 扩权拒绝。固定配置/active/pending snapshot 与 hash/索引字段逐项核对，epoch/monotonic revision 拒绝过期发布/审批，返回旧内容也不会复用旧 revision。revoke 要求原配置摘要并保留首次事实，启动重登记不复活。
- [x] `connector_catalog.py` 与原 connector：完整 discovery 身份包含协议/server、capabilities、选中 schema/描述/annotations，排序变化不增长 revision。返回 discovery、schema 与工具定义独立复制，不再通过公开可变 dict 污染原批准快照。调用只使用已固定的独立适配器，短锁/事务不跨网络；撤销在下一次快照/声明/调用读取生效，已开始调用保留实际单次结果。
- [x] 默认 bootstrap 以管理员 `mcp_connectors` 被动登记、显式附着运行时 transport/SecretBroker；不序列化私网 alias，不解析秘密或自动发现。ConversationService 与原 worker 使用同一目录；没有适配器的记录不暴露工具。平台 reader 4 仅由目录域启用，base-only 新库仍为 3；Workflow 初始化认识并验证独立目录 reader，组合启用与 gate-off 识别均有证据。
- [x] 备份不依赖 gate，以 rowid keyset 每批 8 条校验包括撤销/非正 rowid/末页的历史；启动扫描用 WAL 只读事务、不占 writer。明文/加密恢复在有/无 Workflow 下都禁用已有 connector，不能通过登记/刷新/审批解除，源库不变；Workflow 恢复继续 frozen。

应用通过 scope/name 哈希索引定位附着适配器，点调用只读取原 connector；SQLite ID 使用索引 O(log N)，同一次目录读取批量获取，不逐工具查库。枚举受 128 登记和每 connector 20 选择的硬上限约束，原 Run/模型仍最多暴露 20 项。历史按小批流式校验，领域对象没有时钟/网络；时间由应用注入。

### 失败证据与自检修正

`/tmp/agent-fleet-mcp-catalog-before.xml` 首轮 **7 failed**：四类 metadata 变化未进入待审、返回 discovery 污染 schema，以及两个尚未实现存储的边界。完成后保留其原复现，最终通过。中间 bootstrap 有一次新增 `time` 导入遗漏，另一次缺适配器错误从 `mcp_disabled` 变成 pin changed；补充 import 并保留原模型前拒绝契约，新用例据此核验实际边界，而非放宽 pins。

`/tmp/agent-fleet-mcp-catalog-review-before.xml` 证明没有 pending 时 active revision 与 high-water 不一致仍被读取；增加精确相等条件，原例与未来 reader/末页/版本耗尽/重复撤销五例在 `...-review-after.xml` 全通过。组合恢复发现 Workflow 原初始化只接受平台 3，`...-related-python313.xml` **423 passed / 2 failed**；如今只在目录 schema 完整验证后接受 reader 4，原恢复与默认组合启动、全目录 metadata 隐藏均回归通过。最终集合覆盖真实两进程登记竞争、另一进程审批/撤销、刷新与审批/撤销/新刷新竞争、返回对象突变和 90 KiB schema，不将未运行的真实服务记为通过。

### 最终双 Python 与完整门禁

13 模块为 catalog persistence、MCP connector/pins/HTTPS、Workflow gates/repository/dispatch、platform defaults/conversation/worker、plain/encrypted backup 和 Fleet identity。Python 3.10.21 **428 passed / 0 skipped，20.969 秒**；Python 3.13.7 **428 passed / 0 skipped，20.065 秒**。JUnit：`/tmp/agent-fleet-mcp-catalog-final-verified-python310.xml` 与 `...-python313.xml`。

完整门禁证据：`/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-mcp-catalog-full-20261025-pi2sm2o7/`。UTC 开始 `2026-10-24T22:52:03.978997Z`、结束 `2026-10-24T23:02:08.948819Z`；Python 3.13.7 / macOS 26.6.2 arm64、既有 Playwright/Chrome。基线 `4cd816230fd1d8d5bf02c9f086959d8b5b1a27cf`、分支 `codex/workflow-platform-docs`、dirty=true。825 个 Git 文件运行前后逐项摘要一致；矩阵 SHA-256 `ce1ea411ba30e7ef3c76b92f57effe25956bfc25680c87101081688962ec4efa` 与配置/journey 一致。

```bash
CATALOG_EVIDENCE=/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-mcp-catalog-full-20261025-pi2sm2o7
PYTHONPATH=. \
FLEET_PLAYWRIGHT_MODULE=/tmp/agent-fleet-f4-1-browser-deps/node_modules/playwright \
FLEET_BROWSER_CHANNEL=chrome \
FLEET_SCREENSHOTS="$CATALOG_EVIDENCE/browser" \
python3 -m pytest tests -q --require-journeys \
  --journey-report="$CATALOG_EVIDENCE/journeys.json" \
  --junitxml="$CATALOG_EVIDENCE/results.xml" --tb=short -rs
```

结果 **4971 passed、0 failures/errors、5 skipped、195 subtests passed，604.966 秒**。JUnit 实际 4976 个 testcase 元素，suite 的 subtest 不重复累加。WF-06 **188 selectors / 396 cases 全部通过**，本轮新增 34 selectors / 65 cases；全仓 **41/42 journeys**。唯一 incomplete 仍为 RELEASE-01 的三个 Linux pidfd/`/proc` 必需实例，另外两个 Supervisor `/proc` 按平台跳过。严格退出 **1**、`ci_status=failed`，七项外部检查全部 `not_run`，不把 macOS 无测试失败表述为发布通过。

证据包括 configuration/command、source-digests 前后、pytest/JUnit/journey/exit/verification 和 28 张浏览器 PNG（签名/CRC/尺寸/IEND 校验）。预检包含 825 Git 文件、823 UTF-8 无 BOM 文本、583 Python AST、20 CJS/两个 JS module、双 Python compileall 与 diff；静态类型检查器未配置，compileall/AST 不作为类型证明。后续文档补录单独保存 `post-doc-preflight.json`、`post-doc-source-comparison.json` 和 anchors，运行时代码、测试和矩阵必须保持冻结摘要。

本机目录切片闭环，完整平台目标继续 active。配置 UI、外部 Agent 文件/上下文/结构化产物、Linux 零 skip 门禁、Windows、获准真实两机/模型/CLI、生产隔离及真实 MCP/Muse 试点继续开放。历史间歇失败仍未确定根因；所有 gates 默认关闭，主工作树与其他工作保留，未提交、推送、合并、部署、删除用户文件、下载或修改 Obsidian。


## F5 MCP 目录管理员界面与正文取消修正（2026-10-25）

依据[接入契约 §7.9](../../superpowers/specs/2026-10-20-artifact-capability-integration-contract.md#79-mcp-目录管理界面与管理员-http-边界)及[管理实施计划](../../superpowers/plans/2026-10-25-mcp-catalog-administration.md)，完成 MCP 集成配置的目录管理子集。仍在原独立工作树/分支；复用 Flask、SQLite、静态 ES modules 和原请求边界，无新依赖、执行器、调度器、密钥或网络登记入口。

- [x] `hub/domain/mcp_administration.py`：不可变 command/summary/detail/discovery/tool DTO，精确字段、JSON 安全整数与标识校验，无框架/时钟 I/O。`mcp_catalog_repository.py` 复用原事务和索引，每页最多 8、lookahead 9，支持原 admit/revoke 的可选 epoch CAS；可信调用的原精确 revision/digest 契约保留。
- [x] `hub/application/mcp_catalog_admin.py`：当前持久权威的脱敏投影、精确事务变更；列出未附着/撤销/恢复禁用记录，但不因此暴露 Run 工具。公开 origin 无 path/query，完全省略 SecretRef、配置快照/摘要、transport/private alias。完整已批准/待审定义为独立副本，UTC ISO-8601；返回原写事务结果，不把后续并发状态冒充该操作回执。
- [x] `hub/http/mcp_admin_routes.py`、bootstrap 和 accounts/me：独立 Cookie 管理员边界、no-store、精确 Origin、2 KiB 严格 JSON、唯一 query selector 和有界错误；原 `.`/`..` ID 不经浏览器路径归一化。普通账户、伪造 CF、机器或 Fleet Bearer 无目录权限；无账户模式缺管理员证明返回 403。gate-off/临时内存目录无入口。成功动作只在 HTTP 边界记录 request ID/actor/connector/action/epoch，不记录秘密或定义正文。
- [x] `frontend/assets/api/integrations.js`、`views/account/integrations.js`、CSS 和 account 生命周期：严格 DTO、按需分页/详情、完整文本定义审阅、原生 dialog 与确认 checkbox、不可逆撤销提示；unknown/409 只读回、不自动重放。挂起/销毁取消请求和对话框，恢复读取新状态；身份变化销毁旧视图。390px、键盘/Escape/焦点、恶意元数据、悬挂读取和实际 Cookie 切换均有 Chrome 证据。
- [x] 共享 `api/client.js` 修复响应头后过早清理 timer/listener：默认 15 秒/最大 60 秒与外部取消覆盖完整 JSON 正文；成功、错误、取消后清理，公开错误码保持，cause 保留。两新 JS 模块显式进入 `tests/test_release_layout.py` 的 required/allowed/credential 检查，不放宽未知文件保护。

查询通过 PK keyset O(log N + k)，k≤8、目录历史 N≤128；附着/选中状态用 set/Map 均摊 O(1)，页面绘制 O(N) 且受历史上限约束。完整 definition 只在详情输出，列表不构造或 hash 全量公开 discovery DTO。SQLite 沿用同步 Flask 和既有 10 秒锁等待，不宣称驱动异步或外部服务健康。

### 失败证据、修正与范围

首轮 `/tmp/agent-fleet-mcp-admin-red-20261025.xml` 为 47 failed / 2 passed，证明新契约与端点尚未实现；初始实现 48 passed / 1 failed 来自测试重复初始化管理员，修正夹具后通过。`...-frontend-red-20261025.xml` 证明前端解析器缺失；`...-review-red-20261025.xml` 证明损坏 schema 被误报 400，改为仅明确的请求校验码返回 400，目录损坏保持 503。四种正文生命周期回归在 `...-body-red-20261025.xml` 全失败；补修后与发布、lease/smoke 原例共 43 passed / 2 subtests passed。

首个完整目录为 `/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-mcp-admin-full-20261025-6uid499z/`：请求停止的 SIGINT/SIGTERM 被当时导入的 deploy handler 转为 DeployError，运行继续完成，结果 3 failed / 5038 passed / 5 skipped / 195 subtests passed。两例堆栈明确含 signal-triggered interrupted/DeployError；另一例为发布允许清单漏登记两 JS 模块。该运行源摘要未变，但收到人为停止信号，不作为最终验收。保留 interruption-note、原日志/JUnit/源码摘要；不把这些信号相关失败与历史未定位间歇失败混为一谈。

最终新增 29 selectors / 74 cases，其中七个真实 Chrome 场景；其余使用实际 SQLite/HTTP 和确定性 Node 请求对端。目录管理全程无 MCP 网络或 SecretBroker 解析，原 Run pins/unknown/held-or-isolated、恢复禁用及 read_only helper 契约未放宽。

### 最终双 Python 与完整门禁

八模块：management HTTP/DTO/browser、目录持久化、MCP connector、Run pins、account HTTP、release layout 和 frontend contracts。Python 3.10.21 **381 passed / 0 skipped，suite 49.160 秒**；Python 3.13.7 **381 passed / 0 skipped，suite 44.889 秒**，两组另有两个通过的 subtests。JUnit 为 `/tmp/agent-fleet-mcp-admin-final-python310-20261025.xml` 和 `...-python313-20261025.xml`。

最终完整证据：`/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-mcp-admin-full-20261025-b_v944e0/`。UTC 开始 `2026-10-25T00:02:16.069522Z`、结束 `2026-10-25T00:13:31.246769Z`，耗时 675.131 秒；Python 3.13.7/macOS 26.6.2 arm64，既有 Playwright/Chrome。基线 `4cd816230fd1d8d5bf02c9f086959d8b5b1a27cf`、`codex/workflow-platform-docs`、dirty=true，835 个 Git 文件运行前后摘要逐项一致；矩阵 SHA-256 `09f2fb0e91027b8a9b12d27bfe919fcabf2a74e3dadb04ea1f0a3d6d625289bf` 与 journey/configuration 一致。

```bash
MCP_ADMIN_EVIDENCE=/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-mcp-admin-full-20261025-b_v944e0
PYTHONPATH=. \
FLEET_PLAYWRIGHT_MODULE=/tmp/agent-fleet-f4-1-browser-deps/node_modules/playwright \
FLEET_BROWSER_CHANNEL=chrome \
FLEET_SCREENSHOTS="$MCP_ADMIN_EVIDENCE/browser" \
python3 -m pytest tests -q --require-journeys \
  --journey-report="$MCP_ADMIN_EVIDENCE/journeys.json" \
  --junitxml="$MCP_ADMIN_EVIDENCE/results.xml" --tb=short -rs
```

结果 **5045 passed、0 failures/errors、5 skipped、195 subtests passed**。JUnit 实际 5050 个 testcase 元素，suite 的子测试不重复累加。WF-06 **217 selectors / 470 cases 全部通过**，全仓 **41/42 journeys**；唯一 incomplete 仍是 RELEASE-01 的三个 Linux pidfd/`/proc` 必需实例，另两例 Supervisor `/proc` 平台 skip。严格退出 **1**、`ci_status=failed`；七项外部检查全部 `not_run`，不能表述为生产发布通过。

证据包括 configuration/command、源码前后摘要、pytest/JUnit/journeys/exit/verification，及 36 张浏览器 PNG（逐项签名/CRC/尺寸/IEND 校验；已人工查看移动端定义/确认和最终页面）。预检为 835 Git 文件、833 UTF-8 无 BOM 文本、588 Python AST、21 CJS/六 JS module、双 Python compileall 和 diff；类型检查器未配置，不把编译/AST 称为类型证明。最终文档补录另存 post-doc-preflight/source-comparison/anchors，运行时代码、测试和矩阵继续冻结。

本机目录管理闭环，完整平台目标继续 active。Fleet 身份等其余配置界面、外部 Agent 文件/上下文/结构化产物、Linux 零 skip 门禁、Windows、获准真实两机/模型/CLI/完整隔离及 MCP/Muse 试点仍开放。所有 gate 默认关闭；未提交、推送、合并、部署、执行真实外部任务、删除用户文件、下载或修改 Obsidian。


## F5 Fleet 身份查看与撤销管理（2026-10-25）

依据[接入契约 §7.10](../../superpowers/specs/2026-10-20-artifact-capability-integration-contract.md#710-fleet-身份管理界面与撤销确认)和[身份管理计划](../../superpowers/plans/2026-10-25-fleet-identity-administration.md)，闭合 Fleet 配置 UI 的身份查看/撤销子集。登记、令牌和权限变更仍属于可信配置，未新增 schema、凭据域、外部调用或工具权限。

- [x] `hub/domain/integration_administration.py` 与 `hub/application/integration_identity_admin.py`：明确的 frozen key/record/command/summary/detail/page/receipt，以及仅跨持久 I/O 的 port。公开 scope/模板/工作区/额度及 UTC 状态，省略所有 token、摘要、快照与 namespace。`restore_disabled > revoked > active`，空 scope 仍保留有效登记事实。
- [x] `integration_identity_repository.py`：复用原复合主键 keyset，每页 ≤8/lookahead ≤9，实际 trace 的两个有界 SELECT 和查询计划有断言；每条原快照完整校验。原 revoke 新增可选 active 前置条件，在原 digest-checked 写事务拒绝并发撤销/恢复禁用；旧可信重复撤销仍保留首次时刻。未修改 Workflow/Run/Command 或 unknown/held/isolated/postcheck 运行时。
- [x] `integration_admin_boundary.py` / `integration_admin_routes.py` 与原 MCP route：共用 cookie-admin 证明和 2 KiB 严格 JSON；两个精确 query 选择 UTF-8 原身份，状态冲突 409、损坏/不兼容 503，并保留 cause。边界日志使用转义 JSON 保存原公开身份，控制字符不能插入伪日志。gate-off 无管理入口，CF/DEV 无角色证明返回 403；普通账户/机器/外部 Bearer 无元数据权限。
- [x] 两个新 ES modules、account 生命周期和共用 CSS：完整权限审阅、原生 dialog/checklist、Escape/焦点、一次 POST、丢响应显式读回、冲突新事实、成功回执后独立 GET；挂起读取取消与身份变化销毁。原 UTF-8 引用允许 BOM 字符、中文、路径字符与 supplementary codepoint，前端按 UTF-8 排序、保留精确值，不引入 MCP 名称限制。两模块进入明确发布与凭据清单。

查询 O(log N+k)、k≤8；原摘要/allowlist 受 64 KiB/128 项限制，Map 复合点选均摊 O(1)，渲染为已手动加载记录数的线性成本。SQLite 继续同步 Flask 和原 10 秒锁等待，没有伪称异步驱动或新增后台线程。

### 失败与修正证据

`/tmp/agent-fleet-identity-admin-red-20261025.xml` 为 51 failed / 2 passed，包含缺失模块/端点及首次 fixture 错把 Unicode 身份拼进 ASCII bearer 的错误；改为确定性摘要 fixture 后，三模块 `...-initial-20261025.xml` 为 188 passed / 1 failed，仅前端模块当时尚未创建。前端落盘后首轮浏览器/发布集合为 102 passed / 1 failed / 2 subtests，失败是生命周期 fixture 在普通账户切换、面板已按预期移除后继续等待读取旧面板；移动断言顺序后原六场景 6 passed。随后增加两个区域共存、第七场景、cause 与原合法 Unicode 边界，未放宽产品权限或超时。

双 Python 最初并行运行：3.10 为 323 passed，3.13 为 322 passed / 1 failed，均另有两个 passing subtests。失败来自 `test_parallel_full_release_archives_have_isolated_staging` 的仓库全局 `.package-release.*` 空目录断言；两 suite 同时开始且发布测试重叠，结束后 staging 为空。原 `...-parallel-python310-20261025.xml` 与 `...-parallel-python313-20261025.xml` 保留。改为串行复验，未修改发布测试的该断言或产品打包实现；不把它冒称为历史间歇问题的修复。

### 最终新鲜验证

11 模块：身份管理与浏览器、原持久身份、Fleet MCP、MCP 管理与浏览器、account HTTP、release layout、frontend contracts、Workflow gates、MCP HTTPS。Python 3.10.21 **323 passed / 0 skipped，suite 87.544 秒**；Python 3.13.7 **323 passed / 0 skipped，suite 87.163 秒**，各另有两个通过的子测试。JUnit 为 `/tmp/agent-fleet-identity-admin-final-python310-20261025.xml` 与 `...-python313-20261025.xml`。

新增 **26 selectors / 63 cases**，其中七个实际 Chrome 场景。最终完整证据 `/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-identity-admin-full-20261025-8_4rbeyu/`：UTC `2026-10-25T00:35:56.869959Z` → `2026-10-25T00:47:06.884146Z`，670.001 秒；Python 3.13.7、macOS-26.6.2-arm64-arm-64bit-Mach-O、既有 Chrome/Playwright。基线 `4cd816230fd1d8d5bf02c9f086959d8b5b1a27cf`、`codex/workflow-platform-docs`、dirty=true；845 个 Git 文件前后摘要逐项一致，矩阵 SHA-256 `2cf4c7193e5eb95d368d4a65dd25cfb95a0609aa8fa8e79b67ad4b7b848148df`。

```bash
IDENTITY_ADMIN_EVIDENCE=/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-identity-admin-full-20261025-8_4rbeyu
PYTHONPATH=. FLEET_PLAYWRIGHT_MODULE=/tmp/agent-fleet-f4-1-browser-deps/node_modules/playwright \
FLEET_BROWSER_CHANNEL=chrome FLEET_SCREENSHOTS="$IDENTITY_ADMIN_EVIDENCE/browser" \
python3 -m pytest tests -q --require-journeys \
  --journey-report="$IDENTITY_ADMIN_EVIDENCE/journeys.json" \
  --junitxml="$IDENTITY_ADMIN_EVIDENCE/results.xml" --tb=short -rs
```

结果 **5108 passed、0 failures/errors、5 skipped、195 subtests passed**，JUnit 实际 5113 个 testcase，不重复累计 subtests。WF-06 **243 selectors / 533 cases 全部通过**；全仓 **41/42 journeys**。严格退出 **1** / `ci_status=failed`，唯一 incomplete 为 RELEASE-01 三项 Linux pidfd/`/proc` 必需实例，另两项 Supervisor `/proc` 也因平台跳过；全部七项外部检查仍 `not_run`，不能认定发布或生产验收通过。

证据包括 config/command、源码前后摘要、JUnit/journeys/exit/verification 及 44 张 PNG 的签名/CRC/尺寸/IEND 校验；已人工查看身份移动端详情和确认。预检为 845 Git 文件、843 UTF-8 无 BOM 文本、594 Python AST、22 CJS/八 JS module、双 Python compileall/diff 和 17 份 Markdown 链接；无静态类型检查器，不把 AST/编译冒称类型证明。文档补录另存 post-doc preflight/source comparison/anchors，运行时代码、测试和矩阵不变。

本机身份管理子集完成，整体目标继续 active。其余集成配置、外部 Agent 文件/上下文/结构化产物、Linux 零 skip、Windows 和获准真实两机/模型/CLI/完整隔离/MCP/Muse 试点继续开放。所有 gate 默认关闭；未提交、推送、合并、部署、删除用户文件、下载、访问真实外部集成或修改 Obsidian。


## F5 外部 Agent 完整文本输入与固定请求（2026-10-25）

依据[接入契约 §7.11](../../superpowers/specs/2026-10-20-artifact-capability-integration-contract.md#711-外部-agent-文本文件输入与固定请求)和[文本输入计划](../../superpowers/plans/2026-10-25-external-agent-text-inputs.md)，闭合外部 Agent 的完整小型文本产物输入本机子集。原 `ExternalTaskRef`、六项 adapter 能力、target schema 1、预算/预约/控制/取消和 Muse journal 继续有效；未新增远程附件上传或 exporter。

- [x] `hub/domain/external_request.py`：frozen 完整文本/私有请求、精确 canonical envelope 与 parser；无输入 prompt 保持原 resolved JSON。三种明确 MIME、严格 UTF-8、完整字节长度/SHA-256、重复 ID 的引用及 MIME 一致性、逻辑总量 ≤8192 bytes、完整 prompt ≤8192 字符、私有快照 ≤65536 bytes；超限拒绝，不截断、不摘要。私有正文不进入 dataclass repr。
- [x] `hub/application/workflow_external_inputs.py`：只有 scope metadata/verified descriptor 的 I/O port，具体服务无请求态。字典去重，每个 artifact 一次 size+1≤8193 的读取，离开 context 总关闭；重新校验实际字节、摘要与完整解码。已保存数据用于 lookup；允许的发送前只重查当前 scope/hash/size/MIME，失权不重发。O(n+B) 时间/空间，n≤16、B≤8192。
- [x] 原 `workflow_external_repository.py`/schema：nullable 私有 request 与 send_started_at 同事务保存、不可改写；时间不得早于原 updated_at。独立 external schema/reader 2、Workflow reader 5；原子升级 schema 1，仅补 nullable 列与兼容 metadata。gate-off 可读旧 schema；旧无输入请求从原 resolved 确定性恢复。已开始的文本任务缺失/损坏请求拒绝读取/I/O/备份；128 行 keyset 历史检查保留，普通/加密恢复 frozen。
- [x] `workflow_external_execution.py`/bootstrap/coordinator/WorkflowRepository：采集在发送意图之前，原请求用于 journal 查回；任务已绑定时不重读文件。所有 lookup/许可重发保留原 token/任务、scope/control/lease/预算/held 检查和 owned I/O。external freeze 不接受 Node target；仅 Node 步骤要求 receive/alias，原 Node 与能力屏障不削弱。真实 bootstrap/动态 resolver/原协调器端到端有断言。HTTP 与事件省略正文和私有快照。

### 失败、自检与修正

`/tmp/agent-fleet-external-text-input-red-20261025.xml` 为 49 failed，来自尚未实现的模块和首版拒绝文件输入。最初三模块集合 `...-initial-20261025.xml` 为 126 passed / 8 failed：新 fixture 的三个异常类型预期错误，及旧 reader 3/1 断言需要按新合同调整。未放宽 reader fencing。

自检补充重复 ID 证据 `...-review-red-20261025.xml` 为 56 passed / 2 failed，随后在纯领域同时拒绝冲突引用和 MIME。Node 与外部组合证据 `...-node-red-20261025.xml` 为 58 passed / 3 failed，确认原 admission/协调器错误要求 Node receipt、external freeze 接受 Node target；按本节已先明确的职责修正，关联 Node/能力集合 `...-node-after-20261025.xml` 223 passed。

20 模块首次 Python 3.10 关联集合 `...-related-initial-python310-20261025.xml` 为 685 passed / 1 failed，仅组合身份/外部域的旧 reader 4 断言；更新为 reader 5，原 identity-only reader 4 不改。初次双版本各 686 passed。第一次完整门禁证据 `/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-external-text-input-full-20261025-rhhu30yl/` 单独保留，其结果不代表后续修正后的源码。

完整门禁运行期间只读审查，在仓库外隔离 fixture 复现 `prepare_send(now=101)` 早于记录 created_at=102 仍写入，随后 lookup 为 external_snapshot_invalid、外部发送为 0；证据 `/tmp/agent-fleet-external-text-input-clock-audit-20261025.json`。等待冻结运行结束后才补失败用例 `...-clock-red-20261025.xml`，加入原写事务 now≥updated_at 屏障并重新执行双版本关联和完整冻结门禁。没有通过停止信号打断运行或修改既有失败证据。

### 最终新鲜验证

20 模块包括文本输入、原外部 Workflow/持久化/adapter/Muse bridge、合同/gates/存储/协调/派发/解析/控制、Node 收发与能力协调、持久身份、普通/加密备份、HTTP 和 owned async I/O。Python 3.10.21 **688 passed / 0 skipped，suite 30.758 秒**；Python 3.13.7 **688 passed / 0 skipped，suite 31.106 秒**。JUnit：`/tmp/agent-fleet-external-text-input-final-python310-20261025.xml` 与 `...-python313-20261025.xml`。

新增 **28 selectors / 64 cases**；WF-06 **271 selectors / 597 cases 全部通过**。完整证据 `/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-external-text-input-full-20261025-65xjehc5/`：UTC `2026-10-25T01:35:13.368677Z` → `2026-10-25T01:46:07.741426Z`，654.347 秒，Python 3.13.7、macOS-26.6.2-arm64-arm-64bit-Mach-O、既有 Chrome/Playwright。基线 `4cd816230fd1d8d5bf02c9f086959d8b5b1a27cf`、`codex/workflow-platform-docs`、dirty=true；849 个 Git 文件前后摘要逐项一致，矩阵 SHA-256 `bcd656e86efd4bca1b1eb2c07c04a2f5649cc36aa3a38397fca12f82b865a909`。

```bash
TEXT_INPUT_EVIDENCE=/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-external-text-input-full-20261025-65xjehc5
PYTHONPATH=. FLEET_PLAYWRIGHT_MODULE=/tmp/agent-fleet-f4-1-browser-deps/node_modules/playwright \
FLEET_BROWSER_CHANNEL=chrome FLEET_SCREENSHOTS="$TEXT_INPUT_EVIDENCE/browser" \
python3 -m pytest tests -q --require-journeys \
  --journey-report="$TEXT_INPUT_EVIDENCE/journeys.json" \
  --junitxml="$TEXT_INPUT_EVIDENCE/results.xml" --tb=short -rs
```

全仓 **5172 passed、0 failures/errors、5 skipped、195 subtests passed**，JUnit 实际 5177 个 testcase，不重复累计 subtests；**41/42 journeys**。严格退出 **1** / `ci_status=failed`，唯一 incomplete 为 RELEASE-01 三项 Linux pidfd/`/proc` 必需证据，另两项 Supervisor `/proc` 因 macOS 跳过。七项外部检查均 **not_run**；没有真实 MCP/Muse/模型/节点调用。

证据含 configuration/command、源码前后摘要、JUnit/journeys/exit/verification 及 44 张 PNG 的签名/CRC/尺寸/IEND 校验。本轮预检为 849 Git 文件、847 UTF-8 无 BOM 文本、597 Python AST、22 CJS/八 JS module、双 Python compileall/diff 与 18 份 Markdown 链接；没有静态类型检查器，不把 AST/编译当类型证明。文档补录另存 post-doc preflight/source comparison/anchors，运行时代码、测试和矩阵不变。

本机文本输入子集完成，整体目标仍 active。其余集成配置、二进制/代码包、Memory/summary、Node 能力要求及结构化外部产物、Linux 零 skip、Windows 和获准真实两机/模型/CLI/完整隔离/MCP/Muse 试点继续开放。所有 gate 默认关闭；未提交、推送、合并、部署、下载、删除用户文件或修改 Obsidian。scope 检查确认能力/postcheck/Node poll 执行代码保持本轮开始摘要，原 approved helper/retry/unknown/held/isolated 契约继续生效。

## F5 外部 Agent 显式 Memory 输入与完整历史校验（2026-10-25）

依据[接入契约 §7.12](../../superpowers/specs/2026-10-20-artifact-capability-integration-contract.md#712-外部-agent-的显式-memory-输入与版本权威)、执行契约和[Memory 输入计划](../../superpowers/plans/2026-10-25-external-agent-memory-inputs.md)，本切片交付显式 owner-scoped Memory 的完整正文与精确任务恢复。Memory 没有 workspace 归属或历史表；授权仍要求当前 Workflow workspace，首次采集仅匹配当前 enabled、精确 revision 的记录，不冒充可读取已删除历史。

- [x] `workflow_memory.py`：无框架/时钟的 frozen Memory 值、精确字段/UTF-8/ID/整数/标签校验和完整 canonical item/hash；私有正文不进入 repr。`workflow_contract.py`/types 保留 WorkContract 1 的正 revision 和旧字节，新增 WorkContract 2 的 memory revision 0，definition/resolved 外层不变。schema 2 在原创建事务提升 Workflow reader 6，回滚同时撤回 marker；无新版合同/域的旧库 reader 不变。
- [x] `workflow_memory_repository.py`：最多 16 个选中 ID，一次 owner/PK 集合查询、LIMIT 17、完整投影、同一只读快照，超长/非 text 字段在 SQL 边界不返回正文；enabled/revision 只接受持久整数。字典去重后按逻辑顺序输出，冲突 revision 拒绝。不是 FTS 或会截断的旧 Native Memory renderer。
- [x] `external_request.py` 和原 input collector：Memory canonical 完整字段与现有逻辑文本共享 8192 bytes，prompt≤8192 Unicode 字符，private request≤65536 bytes。只有含 Memory 才使用 prompt envelope 2；旧无输入/纯文本 prompt 保持精确。当前授权批读并比较完整原值，首次采集/允许发送分离；lookup/绑定恢复不重采集。没有内联工具权限、summary、二进制/代码包或 exporter。
- [x] 原 external schema/reader 3 事务迁移 1/2，Workflow reader 6；gate-off 仍读旧域。schema 2 合同在低 reader、旧域 Memory、缺原请求/私有值损坏全部失败关闭。request 和 send marker 原事务保存，原 journal/task、六项能力、target schema 1、lease/control/预算/预约/取消均保留。
- [x] 原 resolver/Bootstrap 接通真实 owner Memory；只允许 external_agent 消费，Native/CLI/capability context 明确拒绝，不把裸引用冒称正文。备份保留变更/删除前的完整原请求，schema 2 合同 reader 屏障校验覆盖 definition/step/resolved；外部完整历史使用 rowid keyset，每批 128，首批无下界，覆盖负数/零 rowid。
- [x] `test_external_agent_memory_inputs.py`：**31 selectors / 84 cases**，实际 Memory/SQLite/Muse journal、owner/版本/禁用/损坏/预算/组合输入、进程间撤销、发送/创建/metadata 事务回滚、重启丢回执查原任务、HTTP DTO/事件无正文、历史末批与非正 rowid、普通/加密 frozen restore。WF-06 新增 31 selectors/84 cases；原文本/外部/identity 仅按真实新 reader 和扫描合同更新断言。

### 保留的失败与自修正证据

`/tmp/agent-fleet-external-memory-red-20261025.xml`：59 failed/9 passed，schema2/Memory 尚未实现。`...-initial-20261025.xml`：64 passed/4 failed，四项为新 fixture 的 UTF-8预算边界或 JSON/Muse marker 断言错误，修正断言不放宽实现。`...-related-initial-20261025.xml`：427 passed/11 failed，为旧 schema/reader 预期。中途尝试只由 schema2 数据提升 reader 的局部方案与先定 external schema3 合同冲突，`...-related-after-schema-20261025.xml` **297 passed/85 failed/56 errors**；已完整撤回，未引入每请求全表扫描或静默 JSON 异常。`...-related-second-20261025.xml` 438 passed。新增复核 `...-review-20261025.xml` 78 passed/1 failed，为 HTTP fixture 漏 /v1；`...-review-after-20261025.xml` 79 passed。

第一次双版本关联为 Python3.10 780 passed/1 browser skip、3.13 781 passed/no skip，证据 `...-final-python310/313-20261025.xml`。第一次完整 gate `/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-external-memory-full-20261025-25o55_n1` 有 5253 passed/5 skip/195 subtests、300/678；853 文件不变。该结果不覆盖随后修正。

最终复核先补 `...-history-red-20261025.xml` 的三个失败：原外部扫描从 rowid>0 开始遗漏负数/零历史；schema2 no-context dispatch 在 external2/reader5 错误标记下仍可读取。修复首批 keyset 无下界、原 row closure 核对实际 reader。`...-history-after-20261025.xml` 174 passed/1 failed，旧 SQL trace 只计 WHERE 页；更新为计全部 ORDER BY/LIMIT 128 页，保持三页和实际损坏检查。所有失败文件保留，未修改历史结果，没有以终止信号打断全仓 gate。

### 最终验证与范围

23 模块集合在浏览器依赖启用后**串行**运行，Python 3.10.21 和 3.13.7 各 **784 passed、0 skipped**；JUnit `/tmp/agent-fleet-external-memory-verified-python310-20261025.xml` 与 `...-python313-20261025.xml`。覆盖新增 Memory、原文本/external/adapter/Muse、合同/派发/解析/gate/identity/普通及加密备份/Workflow持久与控制/HTTP/Node收发与能力协调/原Memory及owned I/O。没有配置静态类型检查器，compileall/AST 不宣称类型证明。

全仓 **5256 passed、0 failures/errors、5 skipped、195 subtests passed**，WF-06 **302 selectors / 681 cases 全通过**，**41/42 journeys**；严格退出 **1**、ci_status=failed，唯一 incomplete 为 Linux RELEASE-01，七项外部检查均 not_run。证据目录：`/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-external-memory-full-20261025-dnxqbpck`。

冻结 853 文件均不变，matrix SHA-256 `f9ed83f016c9273cfd77d83622786ac148b501fdf588f3f0ae0f6418291aa653`；44 张 PNG 的签名/CRC/尺寸/IEND 验证通过。5 skips 是三项 Linux guardian pidfd/proc 必需项和两项 Supervisor proc 平台项，不是 Python 功能失败。UTF-8/BOM、600 Python AST、双版本 compileall、22 CJS/8 JS 语法、19 Markdown 本地链接与 git diff --check 另有 preflight；回填文档后再次检查。

本机显式 Memory 子集完成，完整平台目标继续 active。Native/summary context、二进制/代码包、Node capability 要求、结构化外部产物、其他配置、Linux 零skip/Windows、获准真实两机/模型/CLI/隔离/MCP/Muse试点继续开放。所有 gates 默认关闭；未提交、推送、合并、部署、下载、删除用户文件或修改 Obsidian。原 approved read_only helper/postcheck 精确 retry、unknown 和 held/isolated 契约保持，scope 摘要核对不把其他未提交工作算入此切片。

## Workflow 历史 budget link 扫描边界（2026-10-25）

依据现有 Workflow 初始化恢复合同及[历史扫描修复计划](../../superpowers/plans/2026-10-25-workflow-history-rowid-scan.md)，对 `_init_budget_links` 增加了可重放的历史 rowid 回归：零/负 rowid 及 SQLite 有符号 64 位边界的合法 bound dispatch 在 `WorkflowRepository.init()` 后都必须恢复 `child_request_id` 并建立唯一 `workflow_budget_checks`。原红证据为该字段保持 `NULL`；根因是首批查询固定使用 `rowid>0`，导致零/负 rowid 历史遗漏。

- [x] 首批 keyset 查询改为无 `WHERE`，后续页继续使用最后 rowid 的 `rowid>?`，每页最多 128 条；预算/绑定冲突检查和事务边界不变。
- [x] 初版回归 **2 passed**、repository/budget **53 passed**，当时 Workflow 集合 **590 passed、11 skipped、30 passing subtests**。这 11 项是未设置 `FLEET_PLAYWRIGHT_MODULE` 的浏览器依赖跳过，不能当作平台限制或发布通过证明；独立 `-rs` 复核保留于 `/tmp/agent-fleet-workflow-history-browser-unconfigured-20261025.xml`。
- [x] 新增三个 required WF-08 selectors / **12 cases**，覆盖空历史、128/129/257 条分页、负数/零/64 位 rowid、重复初始化保留 check 进度，以及第二页归属缺失/不一致时整次修复回滚。真实 SQLite 测试通过 `/tmp/agent-fleet-workflow-history-focused-green-20261025.xml`。在独立 Python 进程内重建修复前扫描，**9 failed、3 passed**，JUnit `/tmp/agent-fleet-workflow-history-prefixed-runtime-red-20261025.xml`；没有改动磁盘上的正确实现。
- [x] 本轮双 Python 3.10/3.13 关联集合各 **836 passed、30 passing subtests、0 skipped**；完整严格门禁 **5268 passed、5 skipped、195 passing subtests，41/42 journeys**。`WF-06` **302 selectors / 681 cases** 与 `WF-08` **165 selectors / 333 cases** 全通过；唯一 incomplete 是 Linux `/proc`/pidfd 受限的 `RELEASE-01`，七项真实外部检查均 `not_run`。证据目录：`/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-workflow-history-full-20261025-nv114p_9`；全仓结果 JUnit 与相关集合 JUnit 见同目录及 `/tmp/agent-fleet-workflow-history-related-python{310,313}-20261025.xml`。
- [x] Python 编译和 `git diff --check` 作为本切片收尾门禁；没有 schema migration、外部 I/O、删除或 gate 变更。

这只是历史恢复扫描的局部正确性修复，不改变整体 Workflow/F4/F5 完成状态；Linux/Windows、真实外部验收、其余外部 Agent 输入/输出范围与所有默认关闭 gate 仍按上文记录。

## F5 Muse 原任务身份校验（2026-10-26）

依据[接入契约 §7.6](../../superpowers/specs/2026-10-20-artifact-capability-integration-contract.md#76-外部-agent-workflow-派发与原任务查回)和[实施计划](../../superpowers/plans/2026-10-26-muse-external-task-identity.md)，在现有 Muse adapter 边界落实原 journal 的 `task_id=dispatch_token`：submit 返回异任务 ID 时抛出 uncertain 的 `external_task_mismatch`；inspect/cancel/export 只接受 agent=muse、external_id=原 token、session=None 的引用，并在 bridge I/O 前拒绝其他引用。缺身份仍为 `dispatch_identity_missing`，unknown 仍为 `dispatch_unknown`。仅增加有界字段比较，不增加查询、全表扫描、接口层或依赖；通用 adapter 的独立 task/session 模型不变。

新增 `tests/test_muse_external_identity.py` 的 **5 selectors / 26 cases**，同步 required WF-06。真实 SQLite/Muse journal 用例在一次发送后损坏回执身份，完整 cause 为 `dispatch_unknown → external_submit_unknown → external_task_mismatch`；无 task_snapshot/binding，原 unknown 与 held 保留，普通重派拒绝。重启按原 token 查回不自动核对；运行中显式核对仍 unknown/held，真实匹配完成后再次显式核对绑定原任务并释放预约，发送始终只有一次；validation=not_requested、acceptance=pending。原 adapter 有效 fixture 改用真实 journal 身份，异观察回执断言保留。

### 失败证据与关联验证

生产修复前 `/tmp/agent-fleet-muse-identity-red-20261026.xml` 为 **16 failed / 10 passed**；修复后 identity/adapter/外部 Workflow 集合 `...-green-20261026.xml` 为 **98 passed**。旧临时环境已缺依赖，初次 Python 3.10 收集因 Flask/cryptography 缺失产生 17 个 import errors，旧 Python 3.13 也缺 MarkupSafe；这是环境失败，未作为产品错误或 passing 证据。新任务局部 Python 3.10.21/3.13.15 环境仅安装现有 `requirements-test.txt`，31 个不同 wheel 均经大小检查，最大 3,943,588 bytes；未改变全局环境或仓库依赖。

21 模块关联集合**串行**运行：Python **3.10.21 和 3.13.15 各 676 passed、0 failures/errors/skips**，pytest 分别 44.47/43.76 秒。JUnit 为 `/tmp/agent-fleet-muse-identity-related-python310-20261026.xml` 与 `...-python313-20261026.xml`，覆盖外部 adapter/持久化/文本/Memory、派发/协调/控制/预算/Task 恢复、Muse bridge/集成、普通/加密备份、owned I/O 和能力预约/协调。预检 856 Git 文件、854 UTF-8 无 BOM 文本、601 Python AST、225 本地链接/20 文档、22 CJS/8 JS module、双 Python compileall、diff 检查通过；未配置静态类型检查器。

### 完整冻结门禁与剩余边界

证据目录 `/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-muse-identity-full-20261026-r1_9bsz4`：UTC `2026-10-25T19:07:32.864187Z` → `2026-10-25T19:18:38.924031Z`，666.065 秒；Python 3.13.15、macOS-26.6.2-arm64-arm-64bit-Mach-O、既有 Chrome/Playwright。基线仍为页首 commit/分支及 dirty=true；运行全程冻结的 **856 文件摘要前后一致**，矩阵 SHA-256 `523291b4fd6ac704f0ee8029612367617f1d63ef9a0839ca2240313f446c19f2`。

全仓 **5294 passed、0 failures/errors、5 skipped、195 passing subtests**；JUnit 实际 **5299 testcase**，suite 的 5494 含 195 subtests，不重复累计。WF-06 **307 selectors / 707 cases**、WF-08 **165 / 333** 全通过，**41/42 journeys**。严格退出 **1**、`ci_status=failed`：唯一 incomplete 为 RELEASE-01 三项 Linux pidfd/proc 必需实例；另两项 Supervisor proc 因 macOS 跳过。七项外部检查全为 `not_run`，不能认定发布或生产验收通过。

configuration/command、源码前后摘要、JUnit/journeys/exit/verification 均保留。44 张 PNG 的签名/CRC/尺寸/IEND 检查通过，抽查既有 Workflow unknown/waiting_external 截图，未修改 UI。文档补录后重新执行编码/语法/编译/链接/diff 预检，并对冻结摘要核对仅本节相关文档变化；证据同目录 `post-doc-preflight.json`、`post-doc-source-comparison.json`，运行时代码、测试和矩阵不变。

本机身份校验子集完成，整体平台目标继续开放。Native/summary、二进制/代码包、Node 能力要求、其他集成配置、Linux 零skip/Windows 及获准真实两机/模型/CLI/隔离/MCP/Muse 验收仍未完成。**结构化外部输出尚未确定采用既有 Hub ArtifactRef 还是远程文件导入，不推定已获批准，也未实现 exporter。**所有 gate 默认关闭，Muse cancel/export unsupported；原获准 read_only helper、精确 retry、原节点/token/payload/generation、unknown 和 held/isolated 契约不变。未提交、推送、合并、部署、删除用户文件、调用真实外部集成或修改 Obsidian。


## 2026-10-26 Native Workflow Memory：完整输入与当前授权

依据[执行契约 §5.3](../../superpowers/specs/2026-10-20-workflow-execution-contract.md#53-native-run-的显式完整-memory)及[实施计划](../../superpowers/plans/2026-10-26-native-workflow-memory.md)，本地 native_run 现在消费显式选择的完整 Memory。纯不可变快照在原 message/Run/link 事务固定并原子提高最低 reader 至 7；worker 在 provider 创建前核验 owner、lease、原 binding/current generation、当前 workspace 和 Memory revision/enabled/全部值。旧 token 查回原 Run，不依赖当前 Memory 仍存在；历史初始化和备份以 joined rowid keyset 每页 128 条校验，包含非正 rowid，避免 N+1。正文不进入 DTO、事件或错误；普通 turn override 不能注入标记。完整 item 总量 8192 bytes、snapshot/message 各 32768 bytes、最终 Run 配置 65536 bytes，超限整事务拒绝。

新增 hub/domain/workflow_native_memory.py、hub/infrastructure/workflow_native_memory_repository.py 和 tests/test_workflow_native_memory.py；复用原 executor/resolver/worker/Memory repository/schema/backup/bootstrap，没有新队列、引擎或依赖。20 selectors / 52 cases 全部登记 WF-06；最新专项 /tmp/agent-fleet-native-memory-focused-final-20261026.xml 为 52 passed。Python 3.10.21/3.13.15 串行关联集合各 886 passed、0 failures/errors/skips，JUnit 为 /tmp/agent-fleet-native-memory-related-final-v2-python310-20261026.xml 与 /tmp/agent-fleet-native-memory-related-final-v2-python313-20261026.xml。此前完整门禁中 MCP bootstrap 的线程集合比较受无关旧线程退出干扰，现直接捕获 construction 内 Thread.start 尝试；保留失败证据，未降低无后台线程要求。

最新冻结完整证据目录：/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-native-memory-full-20261026-y63sdmv2。全仓 **5346 passed、0 failures/errors、5 skipped、195 passing subtests**；JUnit 实际 5351 testcase，suite 的 5546 含 subtests，不重复累计。WF-06 **327 selectors / 759 cases**、WF-03 **127 / 208**、WF-08 **165 / 333** 全通过，41/42 journeys。严格退出 **1**、ci_status=failed；唯一 incomplete 为 RELEASE-01 的三项 Linux pidfd/proc 必需实例，另两项 Supervisor proc 平台 skip；七项外部检查全部 not_run。完整平台、Linux/Windows 与生产/真实外部验收未完成。

冻结 860 文件摘要前后一致，矩阵 SHA-256 5fdb9bc2e0f58e31fa4cd458ea3110400fb90e252dd85ac9e55f033579fe8857。预检 /tmp/agent-fleet-native-memory-preflight-final-20261026.json：858 UTF-8/no-BOM 文本、604 Python AST、22 CJS 与 8 JS module syntax、21 文档/232 本地链接、双 Python compileall 和 git diff --check 通过；未配置静态类型检查器。文档补录后重新预检与摘要对比，结果存同证据目录。

边界：仅本地 native_run Memory；summary、CLI/固定评审/测试、远程/capability context 仍拒绝。用户已确定外部结构化产物首版采用已有 Hub ArtifactRef（接入契约 §7.13）；exporter 尚未实现，当前 output_specs 仍拒绝。旧记录中的“交接方向尚未确定”为历史状态。所有 gate 默认关闭；精确 retry、独立 read_only helper、unknown 与 held/isolated 不变。没有真实外部调用、提交、推送、合并、部署、删除用户文件或 Obsidian 变更。


## 2026-10-27 外部 Agent 已发布 ArtifactRef 输出

基准为[接入契约 §7.13.1](../../superpowers/specs/2026-10-20-artifact-capability-integration-contract.md#7131-首版-artifactref-导出精确合同)与[实施计划](../../superpowers/plans/2026-10-26-external-agent-artifact-outputs.md)。用户已确定首版使用现有 Hub 产物入口发布的引用，adapter 只读查回原任务的引用；没有远程文件导入、聊天伪产物、新 worker/队列或额外依赖。Muse 仍声明 export unsupported。早前各节的“结构化输出未实现/方案未定”为当时状态。

- [x] `hub/domain/external_outputs.py`：精确不可变 DTO/原 task、token、workflow、step、generation 和输出规格匹配，必需/可选集合、最多16项及不同 artifact 总量256MiB。规范排序固定 private proof/subject，源码 bundle 进入同一引用闭包；不生成验证或接受事实。
- [x] `hub/application/workflow_external_outputs.py`：原 owner/workspace 已发布 manifest、owned descriptor 完整 hash/size、64KiB 分块与逐项限额；structured/descriptor 为完整 UTF-8 object≤64KiB/深度32/无重复或非有限数，冻结 Draft202012 schema 只在本地解析。原 F3 未压缩源码包≤10MiB，逐项 tree/blob/路径/成员/PAX/尾部核验，引用去重。
- [x] 原 external executor 在 qualified success 后核验当前权限与固定配置，查回原 task/specs；export 后及完整读取后再次核验配置。已提交 proof 优先恢复，不依赖当前账号配置、gate、Memory/文件重采集或新 I/O；未提交的 unknown 仅显式 reconcile。
- [x] 原 `apply_observation` 事务同原 claim/epoch/revision/binding/current generation/workspace/控制/截止提交输出、subject、私有 observation、状态、事件和预约；reader8 与输出 admission 原子提交，external schema3 不变、旧无输出 bytes 不变。历史使用 signed rowid 每页128行、每页一次完整引用集合查询；普通/加密恢复 frozen。
- [x] 三份专项测试覆盖真实 SQLite trigger 全事务回滚、两进程竞争提交、导出/核验/提交各退出窗口、原任务重读和完整历史末页损坏；默认 bootstrap/coordinator、下游三种引用、HTTP scoped完整下载/元数据隐私、独立 validator/接受屏障、pause/drain/gate-off、重复取消和12MiB heap/块大小。

专项初次139项中135 passed/4 failed（完整 binding 与 export 期间配置竞态）；修复后139 passed。本轮扩大回归325 passed/4 failed揭示原代码交付也使用kind=external，以及两个旧future reader假设；提交钩子改以持久步骤kind=external_agent判定，future reader改为9，原定向12项通过。大文件原失败证明ArtifactStore预核验仍读1MiB；统一64KiB后原测试通过。完整读取期间配置改变原未拒绝，新增读取后固定配置复核，原两竞态用例通过。所有红色JUnit保留于 `/tmp/agent-fleet-external-outputs-*-20261027.xml`，未弱化断言。

专项最终收集为47 selectors / 178 cases，全部在完整门禁通过。中间 `...-all-v3-20261027.xml` 为174 passed；边界集合 `...-boundaries-20261027.xml` 为13 passed。扩大23模块集合的1个 browser fixture skip来自缺少Playwright路径，最终命令已明确配置该路径。首轮最终Python3.10集合还发现取消用例错误地假设固定cause深度；按既有owned I/O回归遍历完整cause/context链并确认同一原始OSError，保留取消生命周期断言，运行时代码不变。相关红色JUnit仍保留。

最终30模块关联集合串行运行于Python3.10.21/3.13.15，各 **1153 passed、0 failures/errors/skips**。JUnit为 `/tmp/agent-fleet-external-outputs-related-verified-python310-20261027.xml` 与 `/tmp/agent-fleet-external-outputs-related-verified-python313-20261027.xml`。未配置静态类型检查器，AST/compileall只证明语法与版本兼容。

完整冻结证据目录：`/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-external-outputs-full-20261027-ilpb41wj`。全仓 **5524 passed、0 failures/errors、5 skipped、195 passing subtests**；JUnit实际5529 testcase，suite统计包含subtests，不重复累计。WF-06 **374 selectors / 937 cases**、WF-08 **179 / 380**全通过，41/42 journeys。严格退出 **1**、ci_status=failed；唯一incomplete是RELEASE-01的三项Linux pidfd/proc必需实例，另两项Supervisor proc平台skip；七项外部检查均not_run。44张浏览器PNG的signature、chunk CRC、尺寸和完整尾部通过核验。

冻结866文件运行前后摘要一致；矩阵SHA-256为 `3cc55faee99ee24cf41f62d2cf64d55924809517f87de286070fc3621f32a3a6`，报告与冻结源一致。预检 `/tmp/agent-fleet-external-outputs-preflight-20261027.json` 通过864份UTF-8/no-BOM文本、609份Python AST、22份CJS/8份JS module syntax、22文档/253本地链接、双Python compileall和git diff --check。文档补录后再次预检并核对仅Markdown摘要变化，结果保存在同证据目录。

47个新selectors登记WF-06，其中14个恢复selectors同时登记WF-08，实际收集、矩阵和报告一致。所有gate默认关闭；Linux零skip、Windows与真实两机/CLI/模型/隔离/MCP/Muse验收仍开放，整体平台目标保持active。本机Native文本产物仍仅作为合同引用，未宣称正文已消费。没有提交、推送、合并、部署、下载、删除用户文件或修改Obsidian。

## 2026-10-27 Native 完整文本产物输入

基准为[执行契约 §5.4](../../superpowers/specs/2026-10-20-workflow-execution-contract.md#54-本地-native-run-的完整文本产物输入)与[实施计划](../../superpowers/plans/2026-10-27-native-workflow-text-inputs.md)。本地 Native 文本输入是用户确认的本段范围；此前“仅传合同引用”的记录描述其当时状态。

- [x] `hub/domain/workflow_text_inputs.py` 与共享 `hub/application/workflow_text_inputs.py`：不可变强类型值、原 external 兼容导出、已发布 owner/workspace manifest、完整 size/SHA-256/UTF-8、caller-owned descriptor 和 EOF probe。同 ID 一次读取、逻辑重复计费；text 与 Memory 各最多 16 项，共享 8192 bytes，O(n+B)。
- [x] 原 `RegisteredRunTarget.inline_text_inputs` 为严格 bool，默认 False；仅固定 provider 的本地 Native 目标可启用。缺省/空/local/hub/host 沿用原本地语义，其他 Node ID 不消费正文。原 Run 在文件 I/O 前优先查回，新快照/reader9/message/Run/dispatch/预算关联同事务提交；并发创建再次查回原任务。
- [x] 原 Native guard 复核私有 schema2、完整引用/消息/配置与 Run/binding/generation；当前 manifest 在 SQL 外检查，再以注入时钟及只读快照核验最终授权和 Memory。queued 只用原已保存字节；快照和系统消息各最多 32768 bytes，最终配置最多 65536 bytes。
- [x] 真实 SQLite、原 coordinator/worker、已发布外部输出到 Native provider、双进程创建、三类退出窗口、取消收尾、128 行/16 输入历史末页、HTTP 标记注入、明文/加密 frozen 恢复均有测试。新增 39 selectors / 115 cases 登记 WF-06，其中 22 selectors / 71 cases 同时登记 WF-08；测试模型与外部 adapter 为 fixture。

补查复现了登记配置 65442 bytes 在添加 Run 元数据后超限却被误判为 unknown。`/tmp/agent-fleet-native-text-initial-config-bound-red-20261027.xml` 三项失败覆盖文本/Memory/混合输入；原请求构造现在从完整输入要求识别边界，复用 context_too_large/uncertain=False 并保留 json_too_large 直接 cause。同一组及兼容检查的 green 为 8 passed，无 descriptor、部分 Run、reader 升级或新 generation。先前完整 5636-pass 运行早于该修复；本节仅以修复后的证据判定当前状态。

新增 39 selectors / 115 cases；串行 Python 3.10.21/3.13.15 的 38 模块关联集合各 **1532 passed、2 subtests passed、0 failures/errors/skips**。

JUnit：`/tmp/agent-fleet-native-text-related-final-v2-python310-20261027.xml`、`/tmp/agent-fleet-native-text-related-final-v2-python313-20261027.xml`。相关与完整门禁各冻结 871 个源文件，摘要运行前后完全一致。

全仓 **5639 passed、0 failures/errors、5 个平台 skip、195 passing subtests**；JUnit 实际 5644 个 testcase，不重复累计 subtests。WF-06 **413 selectors / 1052 cases**、WF-08 **201 / 451** 全通过，41/42 journeys。严格退出 1、ci_status=failed，仅 RELEASE-01 缺少三项 Linux pidfd/proc 必需实例；另两项 Supervisor proc 平台 skip，七项外部检查均 not_run。 完整证据目录：`/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-native-text-full-20261027-u52fnau1`。 44 张浏览器 PNG 的 signature、chunk CRC、尺寸与完整尾部通过核验；矩阵 SHA-256 为 `72f435e112437a766adf2891f527fd096dac438f743cd59bd54e00b99bbf38db`，与冻结源码及报告一致。

写入本节前及文档补录后的预检均通过：869 份 UTF-8/no-BOM 文本、613 份 Python AST、22 CJS/8 JS module syntax、23 文档、双 Python compileall 与 git diff --check；补录后 273 个本地链接有效。静态类型检查器未配置。与最终门禁的冻结摘要对比，仅 11 份 Markdown 改变，运行时代码、测试及矩阵字节完全一致；实际 JUnit/矩阵再次确认 115 个 Native 参数实例及 WF-08 的 22/71 恢复子集。前置预检为 `/tmp/agent-fleet-native-text-preflight-final-v2-20261027.json`，补录后的预检与源码对比保存同证据目录。

所有 gate 默认关闭；summary、二进制/代码包输入、CLI/评审/测试/远程或 capability context、其余集成配置、Linux/Windows 与获准真实两机/模型/MCP/Muse试点继续开放。Muse export 仍 unsupported，整体平台与生产验收未完成。 原 token/payload/generation、精确 retry、独立 read_only helper、unknown 与 held/isolated 继续按原合同执行。本段未提交、推送、合并、部署、下载、删除用户文件或修改 Obsidian。

## 2026-10-27 发布证据 PNG 完整性

基准为[原测试门禁](../../testing/README.md)及[证据核验](../../testing/test-chains.md#核验-ci-证据)，完整实施代码和边界见[PNG 证据计划](../../superpowers/plans/2026-10-27-release-png-evidence.md)。现有 CI 仍调用 `tools.testing.release_evidence`，此次补齐其浏览器文件边界；原 Git HEAD/clean tree、矩阵、全部 selector/JUnit 和外部 not_run 规则保持。

- [x] `tools/testing/png_evidence.py`：流式核验合法 IHDR、色型/位深、块类型/顺序、CRC、调色板、连续 IDAT、完整 zlib/scanline/filter 和 IEND/EOF，支持 Adam7 与全页截图。输入/解压块各最多64 KiB，至多7个pass layout；时间O(B+D)、空间O(64 KiB)，不保留像素全量。parser state由单一文件调用拥有，没有运行时服务或新依赖。
- [x] 原发布核验器流式枚举每个PNG，caller负责descriptor关闭；损坏或I/O失败继续返回 `browser_evidence_invalid`，保留原ValueError/OSError/zlib cause，缺截图保持 `browser_evidence_missing`。
- [x] 新增16 selectors/79 cases，覆盖正常格式、Adam7各pass、跨64KiB过滤字节、部分读取与大图内存、损坏长度/CRC/压缩流、关闭/cause和实际CLI拒绝。原多文件损坏回归也纳入GATE-01；该组现为30 selectors/133 cases。

失败证据 `/tmp/agent-fleet-release-png-red-20261027.xml` 为 **8 failed**：仅签名、签名后垃圾、截断头/结束CRC、缺IEND、IEND后多余字节、CRC损坏和非法deflate均被旧实现接受。最小修复位于原release边界，未改变其他执行契约。相同八例转绿，最终PNG文件113实例通过；双Python3.10.21/3.13.15的四模块关联集合各 **208 passed、2 passing subtests、0 failures/errors/skips**。最终关联记录 `/tmp/agent-fleet-release-png-related-final-20261027.json`；两组JUnit为 `/tmp/agent-fleet-release-png-related-final-python310-20261027.xml`、`/tmp/agent-fleet-release-png-related-final-python313-20261027.xml`。两组运行前后873个源码摘要一致，实际44张既有浏览器图也通过新parser。

最终完整本机门禁为 **5718 passed、0 failures/errors、5平台skip、195 passing subtests**，实际JUnit **5723 testcase**，不重复累计subtests。**GATE-01 30/133、WF-06 413/1052、WF-08 201/451全部通过**；全仓41/42 journeys。严格退出1、`ci_status=failed`，唯一incomplete为RELEASE-01的三项Linux pidfd/proc必需实例，另两项Supervisor proc为平台skip。七项外部检查均not_run；原release verifier亦正确以dirty_checkout拒绝本机开发报告。

完整证据目录 `/tmp/agent-fleet-release-png-full-20261027-kqg47qjf` 包含JUnit、原journey报告、configuration/command、运行前后source digests、44张新截图和verified-summary。截图经实际新parser检查完整结构/CRC/解压/尾部，873个源文件运行前后逐项相同；矩阵SHA-256为 `759e48d6829e22c2ceac70f574713b0d51f06a644f94eb29737354c7bb313bea`。

文档补录前的预检 `/tmp/agent-fleet-release-png-preflight-20261027.json`：851份UTF-8/no-BOM文本、112个本地链接、1480个不同required selector定义均有效；PNG九个函数参数/返回有注解，最大控制流深度3，双Python compileall和git diff --check退出0。与冻结全仓摘要对比仅本入口和本实施进度两份Markdown变化，运行时代码、测试和矩阵字节相同。未配置静态类型检查器。

用户已授权从Obsidian的SSH管理和测试机推荐文档选择环境。依据 `ssh-credentials` → `ssh-config` / `VPS 总览`，荷兰 `mesh-nl` 是轻量沙盒；实测pidfd/proc可用，但0.25 vCPU且缺pytest/Node/浏览器。项目canonical索引另有pxed隔离测试记录；实测 `mesh-pxed` 有Python3.10.12、Node22.21.1、既有Chromium和旧测试venv。本次在新临时目录复用依赖，以独立Git源码快照运行原完整门禁与release entry，验收结果尚待执行。Windows句柄/reparse、真实两机与模型/CLI/隔离/MCP/Muse试点及尚无获准契约的扩展保持开放，完整平台目标仍active。没有开发仓库提交、推送、合并、部署、下载、删除用户文件或Obsidian修改。

## 2026-10-30 Linux 门禁与实际沙箱续接

2026-10-29冻结revision `1a4e4a29ff92760955d10ed74d52b3c70ec774aa` 的Linux全仓实际 **5720 passed、4 skipped、197 passing subtests、42/42 journeys**，873份源码未变、44张PNG完整验证；Node22.21.1/Playwright1.63/Chromium153/Python3.13.16。pytest退出0，但原release verifier以 `junit_incomplete` 退出1；四个skip全是macOS专项隔离测试。Linux Python3.13结果不冒称CI Python3.10验收。七项外部检查均not_run。

本轮补齐原 launcher 的测试专用 Linux bubblewrap user/mount/PID/network namespace 及 libseccomp，源码/控制/运行时只读，写路径精确限制，缺依赖失败关闭。内核状态断言、无隔离对照、独立评审与完整 Cookie HTTP 链均在 Linux 执行；三模块 Linux **50 passed / 0 skip**，macOS Python 3.10.21 与 3.13.15 各 **50 passed / 0 skip**。WF-01 新增 3 selectors / 4 cases，原 release verifier 保持零 skip 规则。

本地持久证据根：`/Users/mango/.cache/agent-fleet/verification/linux-20261028-cm40i40b`。Chromium156缓存已按用户授权完整恢复，301个文件与官方归档一致，独立153目录不再共享符号链接。实现与验证清单见[Linux沙箱实施计划](../../superpowers/plans/2026-10-29-linux-workflow-sandbox.md)。

### 最终冻结运行与原发布核验

冻结测试 revision `8b93185d81dab3eb2442ef8fd0b863dc8eda2552` 来自开发分支 `codex/workflow-platform-docs`、HEAD `4cd816230fd1d8d5bf02c9f086959d8b5b1a27cf` 的 876 个 Git 可见源文件；其中 874 个 UTF-8 无 BOM 文本、2 个既有二进制资源。matrix SHA-256 `f2bc851f3597b77e2a310cc2f642c9bc8df614de5a3ec6a44fbaad98709a5b8b`，1484 个不同 required selector；按 journey 累加为 1723 selectors。3,134,974-byte bundle SHA-256 为 `5fa8e98070d4e9df38b2bd39968cb0a2b0ffd2e658a7b59e64acf3c158b1de18`。这个独立测试 revision 不冒记为开发 HEAD，也没有提交开发工作树。

gz-cvm 使用 Python 3.13.16 / Node 22.21.1 / Playwright 1.63.0 / Chromium 153.0.8010.12 / `LC_ALL=C`，移除 `CAP_DAC_OVERRIDE/CAP_DAC_READ_SEARCH` 并启用 NoNewPrivs。原 `python -m pytest tests -q --require-journeys --journey-report=... --junitxml=... --tb=short -rs` 全量运行 **5724 passed、197 passing subtests、0 failures/errors/skips**，42/42 journeys passed，pytest 退出 0；进程耗时 1547.99 秒。运行前后 876 个文件摘要相同，冻结 checkout clean。WF-01 为 65 selectors / 123 cases，全部通过。Python 3.13 Linux 结果与 CI Python 3.10 配置差异继续明确保留；本地双版本关联测试不与其拼接。

原 `tools.testing.release_evidence` 在同一冻结 checkout 和完整报告上退出 0：`Verified 42 journeys, 1723 selectors, and 5724 JUnit cases; external checks remain not_run`。44 张 PNG 由仓库 parser 逐张核验，七项外部检查均 `not_run`。51 个证据文件共 7,937,234 bytes 已复制到本地，全部大小及 SHA-256 与远端一致；本地再次运行同一 verifier 也退出 0。文档补录前，开发工作树 876 个源文件与冻结快照全部匹配。

持久目录：`/Users/mango/.cache/agent-fleet/verification/linux-20261028-cm40i40b/sandbox-full-20261030-nzb5qq22/`，包含 `evidence/controlled-full/{full-command.json,full.log,results.xml,journeys.json,release-verifier.json,release-verifier.log,verified-summary.json,browser/}`、`provenance.json`、`frozen-source.bundle` 和 `local-evidence-audit.json`。Linux 原红证据及中间 PATH/venv alias/JUnit 配方失败已分别保存在同证据根 `sandbox-red-20261029`、`sandbox-green-20261029`，不与通过报告混合。

本次关闭 Linux 实际测试沙箱和严格零 skip 门禁缺口；Windows 句柄/reparse、两台真实节点交接、真实 CLI/模型、生产隔离配置、真实 MCP/Muse 试点仍未验收，整个项目目标继续 active。现有 gate 默认关闭；未提交、推送、合并、部署或修改 Obsidian。

## 2026-10-30 Windows 产物接收

按[Windows 接收计划](../../superpowers/plans/2026-10-30-windows-artifact-receive.md)新增 `artifact_windows.py`，复用原接收器、独立 SQLite journal、签名命令和回执；固定本地 NTFS 根、逐级父句柄、owner/DACL、reparse/硬链接拒绝、原 ID 排他发布、LockFileEx 两槽及进程退出恢复。manifest 每次核验配置根，Windows export 与 receive 支持独立。没有新增产品依赖、worker 或状态 schema。

home-win 的原始 Windows 签名接收因 capability_unavailable 失败；最终冻结的 60-case 原生组通过，包含路径规则、实际 NTFS/ACL、两个进程容量、四窗口退出、12 MiB 分块内存、篡改/取消及原回执重启。macOS Python 3.10.21/3.13.15 的最终关联组分别 264 passed、0 skip。真实 Windows NodeRuntime 经临时 SSH 反向回环访问本地临时 Hub，首个成功回执响应故意丢失；最终 canary 1 passed，Node 重启只重传原回执，下载一次、回执 POST 两次、Hub Command succeeded，原 coordinator 随后才将 waiting_node 转 ready。canary 前两轮仅 fixture 编码/导入设置失败，已保留原日志并修复；未弱化产品断言。

相同 882 个源文件冻结为独立测试 revision `9868cb0c9a51dfe33086bca3c9b15293efaf6404`；开发 HEAD 不变。matrix SHA-256 `d4e37da147eb33ca928335f93e7f40f94d726db973b3036db8182226682fab39`，1488 个不同 required selectors、各 journey 合计 1727 selectors。gz-cvm 沿用 Python 3.13.16/Node 22.21.1/Playwright 1.63.0/Chromium 153.0.8010.12/`LC_ALL=C`、移除 DAC 绕过能力及 NoNewPrivs 配置。原严格全仓 **5754 passed、197 passing subtests、0 failures/errors/skips、42/42 journeys**，pytest 退出 0、1592.54 秒。882 份源码前后摘要相同、checkout clean；文档结果补录前开发树也逐文件匹配。

原 release verifier 在远端和本地均退出 0：42 journeys、1727 selectors、5754 个实际 JUnit testcase，44 张 PNG 完整核验，七项外部检查 `not_run`。50 个证据文件共 7,922,292 bytes 已完整复制，大小/SHA-256 无差异。证据为 `linux-final-jfzfjw57/evidence/controlled-full/`，来源和复制审计为同证据根的 `verification-summary.json`、`source-before-document-results.json` 与 `linux-final-jfzfjw57/evidence/copy-audit.json`。Windows 最终 60 项又以显式保留的私有 basetemp 单独执行通过，两个运行计数不合并；详细命令、报告与范围见[计划最终记录](../../superpowers/plans/2026-10-30-windows-artifact-receive.md#final-frozen-verification-2026-10-30)。

证据根 `/Users/mango/.cache/agent-fleet/verification/windows-artifact-20261030/`，Windows 临时目录见 `environment.json`；测试数据和进程退出证据保留，没有安装软件、修改全局配置/服务或清理文件。Windows 接收实现与验证切片已闭环；Windows 导出、两个产物 Node 源包交接、完整 WF-04/05、真实 CLI/模型、生产隔离和 MCP/Muse 外部验收保持开放。历史 Linux 5724-pass 记录仍仅证明其冻结 revision；当前新增实现由上述独立 5754-pass 运行验证，不拼接报告。接收的进程退出恢复不宣称断电持久性，gate 继续默认关闭。


## 2026-10-30 Windows 产物导出

按[Windows 导出计划](../../superpowers/plans/2026-10-30-windows-artifact-export.md)扩展原 `artifact_windows.py` 和 `ArtifactExporter` 的文件边界：固定 NTFS 父/源句柄、FILE_SHARE_READ、精确原生时间戳/128-bit ID/owner/DACL 摘要，在原 hash/分片/commit 前核对源和路径。原 ExportRequest/Receipt、HTTP、journal schema、三次尝试/两小时截止和收发两槽保持原合同，没有新增依赖或任务引擎。OutputFile 拒绝损坏 Unicode，Windows 输出应用受限精确组件规则，POSIX 合法路径不收窄。

home-win 初步原生验证覆盖普通写入/rename/已存在写句柄/可写映射拒绝、ACL 变化、源元数据变化、12 MiB 分块、共享两槽和四个真实进程退出；原 NodeRuntime 到原临时 POSIX Hub 的 part/receipt 丢响应与原 ID/回执恢复各有单独实测。初次 fixture 的 Administrators owner、过长 receive basetemp 与旧 HTTP 拦截接口失败均保留，修复未放宽产品规则或断言。macOS Python 3.10/3.13 关联各 295 passed。具体每轮报告及失败见计划的 Pre-freeze evidence，计数不拼接。

当前证据根 `/Users/mango/.cache/agent-fleet/verification/windows-export-20261030-bc40db8dad18/`。最终完整源码即将独立冻结；Windows 合并组、同源码原 HTTP/双 Python/Linux 全仓及原 release verifier 尚待最终执行。接收切片的 5754-pass 旧冻结不能替代这些检查。两个真实产物 Node 源包交接、完整 WF-04/05、真实模型/CLI、生产隔离和 MCP/Muse 外部试点继续开放；gate 默认关闭。


### Windows 导出最终冻结闭环

最终 886 文件冻结 revision `6ba4384adde2a01f44dd96b8ec9c85c2e9a2d839`，矩阵 SHA-256 `061fcdd9c41f229ca0d6efa733e5a48a37a29ae2b2bcd7151b97fd8095eb2682`。home-win 原生合并组 **131 passed/0 skip**（60 neutral、41 export、30 原 receive），实际原 NodeRuntime→原临时 Hub HTTP 独立 **2 passed**；part/receipt 丢响应分别保持一次 PUT、1/2 次回执，原 transfer/artifact ID、原 output binding 和完整 Hub bytes/hash 正确。macOS Python 3.10.21 与 3.13.15 的同七模块集合各 **295 passed/0 skip**，compileall 均退出 0。

Linux 单次原严格全仓 **5785 passed、197 passing subtests、0 failures/errors/skips、42/42 journeys**，pytest 退出 0；Node 22.21.1/Playwright 1.63.0/Chromium 153.0.8010.12/Python 3.13.16/LC_ALL=C。原 release verifier 在 gz-cvm 与复制后的本地分别退出 0，核验 1729 selectors/5785 testcase 和 44 PNG；七项外部检查仍 not_run。所有源码前后摘要相同、frozen checkout clean；50 个 Linux 证据文件共 7,935,727 bytes 逐项大小/SHA-256 一致。

证据根 `/Users/mango/.cache/agent-fleet/verification/windows-export-20261030-bc40db8dad18/final-snipwvt1`，汇总 `verification-summary.json`，命令/JUnit/日志/平台事实、完整复制清单、源码来源和结果文档增量均单独保留。详见[导出计划最终记录](../../superpowers/plans/2026-10-30-windows-artifact-export.md#final-frozen-verification-2026-10-30)。这完成 Windows 导出切片，不代填两台真实产物 Node 源包交接、完整 WF-04/05 或生产/真实外部试点。原 receive 5754-pass 与更早各轮仅证明自己的冻结版本，不能拼接。所有 gate 默认关闭，未提交/推送/合并/部署或更改全局服务。


## 2026-10-30 真实两机源包交接

按[两机计划](../../superpowers/plans/2026-10-30-two-host-artifact-handoff.md#actual-frozen-verification--2026-10-30)新增三个完整 opt-in 夹具 `two_host_producer.py`、`two_host_receiver.py`、`two_host_handoff_probe.py`；复用原 SourceExporter、NodeRuntime、Hub output/input 服务及 FixedTestRunner。没有产品代码或依赖变更。逐块 hash/copy 为 O(n)，64 KiB block，测试包硬限 1 MiB、控制 JSON/报告 64 KiB；不新增内部接口、状态库或执行引擎。

冻结 revision `8f63ddd660c07ddf9d55123132b2b26d2b4f2296`，890 个源码文件，矩阵摘要仍 `061fcdd9c41f229ca0d6efa733e5a48a37a29ae2b2bcd7151b97fd8095eb2682`。真实 home-win Windows 11/Python 3.12.7 生产固定 Git 源包，经原临时 Hub 转给 gz-cvm Linux/Python 3.13.16；三个实际 hostname 不同，独立身份/凭据/目录/journal。控制端 **1 passed、0 failures/errors/skips，9.89s**；Windows 已接受分片丢响应后只发一次 PUT、保留原 transfer/artifact/binding，Linux 已接受回执丢响应后只下载一次、重传同回执两次。错误节点读 404；原 input 服务在 reconciliation 前保持 waiting_node，随后同代 ready；导出不完成原生产步骤。

接收之后单独明确授权的 Linux 验证检查相同 10,240 bytes / SHA-256 `c5de78025951b553bc165aa960e206c36288fc13044f6ea383b2fdf4335ce122`、原 repository/tree/blob，再用既有实际 bubblewrap/libseccomp FixedTestRunner 跑 **2 项固定 pytest 全通过**。合成 `.env`、未提交/未跟踪源码和机器凭据不进入测试输入；报告记录 SandboxBackend 与 launcher SHA-256。三端保留的源码包逐字节一致，原报告/JUnit 和八个远端复制文件的大小/摘要全部匹配。双 Python 3.10.21/3.13.15 compileall 均退出 0；两机源码运行前后不变，local frozen checkout clean。

证据根 `/Users/mango/.cache/agent-fleet/verification/two-host-20261030-czhi883m`：`verification-summary.json`、`provenance.json`、`two-host-command.json/log/xml`、`pilot/handoff-summary.json`、各原操作报告/命令/日志、双端源码审计、`remote-evidence-v2/`、复制清单和 compile 记录均保留。首次复制审计仅因 PowerShell 去掉 JSON 引号失败；独立 base64 参数审计及新目标复制通过，未改产品/夹具或重跑原成功案例。

该窄两机交接试点闭环，完整平台继续 active。完整 WF-04/05、真实模型/商业 CLI、生产配置与 MCP/Muse 外部验收继续开放，七项外部检查保持 not_run，`full_workflow_completed=false`。上一轮 5785-pass Linux 全仓仍只属于 `6ba4384...` 的导出冻结，本次新增夹具显式运行，不拼接为新全仓通过；journey matrix/发布 verifier 未改。所有 gate 默认关闭，无提交/推送/合并/部署、全局设置/服务变化、安装或清理。


## 2026-10-30 能力跨平台 postcheck：本地夹具准备

[能力平台计划](../../superpowers/plans/2026-10-30-capability-platform-postcheck.md#local-implementation-and-verification--2026-10-30)已落盘两个完整 opt-in 文件 `capability_node_canary.py`、`capability_hub_probe.py`，复用原平台报告/真实 HTTP、显式 admission/Run/签名/Node journal 与独立 digest helper，不改产品代码/协议/schema/依赖。实际 OS/architecture/Python/disk 报告先于原 binding 固定；内存未知仍为零，只开启固定 write 和 read_only digest，过程不调用模型或 shell 工具。

本地 macOS 子进程覆盖 held/isolated × matched/mismatch/文件缺失六组合。原 write 成功后测试注入异常形成原 journal/Hub unknown；沿用既有 test-only Workflow unknown 夹具，不能据此声称完整模型链。helper 只有原预声明独立 capability，原 retry 不变；一写一读，重复及重启零 I/O，原 Command/Run/Workflow/预算/binding/预约快照逐项不变。首轮六项在最终汇总访问不存在的 binding.digest 时失败；仅改为 hash 原 snapshot 后，Python 3.13.15 **6 passed/0 skip（6.35s）**、3.10.21 独立 **6 passed/0 skip（6.12s）**，两版 compileall 退出 0；失败日志完整保留。

证据索引 `/Users/mango/.cache/agent-fleet/verification/capability-local-verification-summary.json`，双版证据目录和精确 fixture 摘要见该索引与计划。当时实际 home-win/gz-cvm 新固定能力测试尚未授权或执行；其后通过的原生固定矩阵见本页末节，完整 WF-05、NODE-LIVE、生产/外部验收保持开放。上一导出冻结全仓及两机文件交接证据不代替此能力验证，gate 默认关闭。


最终自检将能力夹具风险声明精确为原 `workspace_write` 与独立 `workspace_read/read_only` helper，Node policy 显式允许两项；原 retry 保持。两份完整夹具随后双版新鲜独立重跑：3.10.21 **6 passed/0 skip（5.91s）**、3.13.15 **6 passed/0 skip（6.27s）**，compileall 各退出 0。最终证据 `/Users/mango/.cache/agent-fleet/verification/capability-platform-local-final-3zpgs6qw/verification-summary.json` 保留精确摘要和每例状态；上文旧版夹具测试仅为历史。该本地记录之后获准并完成的远端十二项见本页末节，原本地报告保持自身范围。

## 2026-10-30 postcheck evidence/audit 原子提交

[原子审计计划](../../superpowers/plans/2026-10-30-postcheck-atomic-audit.md#verified-local-result--2026-10-30)修复了真实本地 SQLite 故障窗口：结果先提交、审计随后失败时，终态查回会永久缺少 audit。三个原样外部复现均从失败转为通过；新增 48 个回归的红色基线是 36 failed/12 passed，修复后全部通过。证据及原始代码保存在 `/Users/mango/.cache/agent-fleet/verification/postcheck-atomic-20261030-zms34vh4/`。

原仓储现在把 workspace/service inspect/service logs 的 descriptor 与 audit 放在同一短事务；失败整体回滚，历史终态按保存证据补齐 audit，重复/并发只复用唯一原审计，完整终态读没有写事务。原 Command/Run/Workflow/binding/reservation、独立 helper、风险/retry 与结果语义不变。没有新增 schema、领域状态、worker 或依赖。SQLite cause 经仓储和应用层完整保留。

Python 3.10.21 与 3.13.15 各自关联八模块 **279 passed/0 failures/errors/skips**，各自原本地能力夹具 **6 passed/0 failures/errors/skips**，compileall 各退出 0、执行源码摘要前后一致。分轮结果不合并为发布门禁。两机固定能力测试已获用户明确授权，home-win/gz-cvm 的现有 Python 3.12.7/3.13.16 已只读核查；随后包含该修复及回归登记的新冻结已完成远端十二场景，结果见本页末节。完整 WF-05、NODE-LIVE、生产和真实外部能力仍开放，gate 默认关闭。


## 2026-10-31 能力 workspace postcheck 原生验证

[固定原生矩阵](../../superpowers/plans/2026-10-30-capability-platform-postcheck.md#native-frozen-verification)使用原已实现的完整 opt-in 夹具、已修复的原 postcheck 仓储/application 和预声明独立 read_only helper。原控制端单次 **12 passed/0 failures/errors/skips（52.80s）**，覆盖实际 home-win Windows/Python 3.12.7 与 gz-cvm Linux/Python 3.13.16，各含 held/isolated × matched/mismatch/read_failed；控制端 Python 3.13.15，完整命令/环境/JUnit/原操作日志均保留。

新冻结 `16f10ebd630d38fa53a839ce9d2c432719014120` 的 895 个文件在开发树、控制端和两机运行前逐项一致，控制端与两机运行后仍逐项相同且冻结 checkout clean。独立只读 Hub 数据库与每台原 Node journal 核验原 Command/Run/Workflow/Step/dispatch unknown、generation 1、相同 binding、unknown 预算、held/isolated 预约、精确 write/helper retry 与唯一 canonical audit；原 fixture 还断言一写一读、重复/重启零 I/O、全部原快照不变和 wrong-owner 404。原固定字节在所有负例中通过 rename 保留。

证据根 `/Users/mango/.cache/agent-fleet/verification/capability-platform-20261030-edwzm7df` 的 `verification-summary.json` 包含十二项独立 Hub 检查、两端前后 source reports 和原生复制清单。各端 32 份原生文件，共 64 份/545,258 bytes，全部大小/哈希与远端一致。独立 verifier 首轮错误地把审计引用当 helper command id、把 source 写成 workspace_state；原红脚本/日志和仓储/测试依据完整保留，改用原 descriptor evidence id/workspace_check 后全部核验通过，没有改产品代码、夹具或放宽原断言。

这完成固定能力 postcheck 的原生矩阵，`Fixture.isolate_write` 仍是明确的 test-only unknown Workflow 设置，`full_workflow_completed=false`。原导出 5785-pass 全仓及两机源包交接报告各自保留自己的冻结版本，不能替代含本次 Hub 修复的新全仓门禁。完整 WF-04/05、NODE-LIVE、真实模型/商业 CLI、生产隔离和外部验收仍开放，七项外部检查 not_run、所有 gate 默认关闭；没有开发提交/推送/合并/部署、安装、全局设置/服务变化、清理或 Obsidian 修改。

## 2026-10-31 postcheck 全仓基线与终态保护

[原严格全仓](../../superpowers/plans/2026-10-31-postcheck-full-gate.md#verified-pre-guard-baseline--2026-10-31)在旧冻结 `adf137251e21acbad036a3e51221d8cf6805731d` 完成 **5833 passed、197 passing subtests、零失败/错误/跳过**；42 journeys、44 PNG，远端与复制后的本地原 release verifier 均退出 0。896 文件前后摘要一致、checkout clean，双 Python 完整源码 compileall 各退出 0。50 份复制证据 / 7,943,776 bytes 大小和 hash 一致，原基线及 fixtures 全部保留；其 scope 明确为 `pre-terminal-guard-baseline`。

之后独立 HTTP 复现发现：完整 postcheck/audit 查回在原命令 owner/status 改变后仍返回 200。按原合同应为 404/409；[终态保护计划](../../superpowers/plans/2026-10-31-postcheck-terminal-guard.md)在原 fast return 前复用 `_require_unknown_command`，只增加一次原主键索引查询，保持零写事务。原命令、helper、descriptor/audit、Workflow/Run/binding/generation/预算和预约不被改写；缺 audit 的历史恢复仍使用原事务复核，没有新增 schema、接口、依赖或执行引擎。

新完整文件 `tests/test_postcheck_terminal_guard.py` 覆盖三种 postcheck × 两种原命令失效 × 完整/缺 audit，共十二项。in-repo 红色基线 **6 failed/6 passed**；修复后十二项通过，原样参数化 HTTP 复现单独六项通过。Python 3.10.21 与 3.13.15 各自关联 **291 passed**、独立本地能力 **6 passed**、独立 release/passive-gate **121 passed**，均零失败/错误/跳过、compileall 退出 0、执行源码和矩阵摘要不变。新增 exact selector 仅登记 NODE-01/WF-08；异常原因链及拒绝时零写入已验证。证据为 `/Users/mango/.cache/agent-fleet/verification/postcheck-terminal-guard-20261031-pijmzuw8/`，各运行计数不拼接。

修正源码已在新私有目录执行原两机 fixture；同源原严格 Linux 全仓因环境容量耗尽未完成，证据如下。旧 12 项与 5833 项报告各自只证明其原冻结版本。完整 WF-04/05、NODE-LIVE、真实模型/CLI/生产/外部仍开放，七项外部检查 not_run、gate 默认关闭，整体目标保持进行中。

## 2026-10-31 修正版原生矩阵与 Linux 空间阻塞

本节保留首次修正版全仓的失败检查点；后续已获逐目录授权、完成容量恢复，并在同一冻结原样重跑通过 5845 项。当前结果见下方“授权容量恢复与修正版完整门禁”，本节旧阻塞不代表当前全仓状态。

[修正冻结结果](../../superpowers/plans/2026-10-31-postcheck-terminal-guard.md#corrected-native-result-and-full-run-blocker--2026-10-31)使用 `85488105cd2d3b610fcbfa18fc2273bd02618db1`、898 文件，代码差异精确为一行原仓储 guard、新完整回归文件及 NODE-01/WF-08 追加 selector。原 native driver/fixture 字节不变，两个现有本地 Python 完整源码 compileall 均退出 0，冻结后 314 个文档链接、无 BOM/差异/登记检查通过。

实际 home-win Windows/Python 3.12.7 和 gz-cvm Linux/Python 3.13.16 的原单次矩阵 **12 passed、零失败/错误/跳过，pytest 49.85s**；控制端 50.28s。三端 898 文件前后 hash 全同、checkout clean；原一写一读/重启零 I/O、unknown/原 binding/generation/预算/held-or-isolated 保持。十二份临时 Hub 数据库只读独立核验和两端 journal 审计通过；64 文件 / 545,258 bytes 的复制证据逐项匹配。`Fixture.isolate_write` 与 `full_workflow_completed=false` 仍明确保留。

同源原全仓在独立 `/tmp/afcg-full-t0mae74m` 和 fresh basetemp 启动，838.98s 后退出 **120**，未生成最终 JUnit/journey。源码仍全同且 checkout clean；gz-cvm `/dev/vda1` 50 GB 根盘和 inode 均 100% 满，分别仅剩 28,672 bytes、100 inode。全日志仅写到 4096 bytes，因此不填最终测试计数、不把全部早期 `F` 臆断成同一原因、不运行替代门禁。原生通过不能替代此次无效全仓；需足够磁盘/inode 后原样全新重跑。

完整来源、原命令/JUnit、独立审计和本次失败诊断保存在 `/Users/mango/.cache/agent-fleet/verification/postcheck-guard-20261031-t0mae74m/`，`verification-summary.json` 为原生结果，`full/failure-diagnosis.json` 为环境阻塞。旧完整夹具约 2.00 GB、本次失败夹具约 1.07 GB 均保留，未删除或清理；没有安装、改全局设置/服务、开发提交/推送/合并/部署或 Obsidian 修改。新全仓和整体平台目标保持开放。

### 完整夹具保全与容量恢复待授权

本小节的“待授权”为历史保全时点；两项确认与分别删除已完成，完整归档及范围外证据保持，当前结果见下一节。

两组夹具随后直接流式归档到该证据根的 `full/baseline-fixtures-archive/` 和 `full/failed-fixtures-archive/`，远端不写归档、不解包、不删除。分别 **41,983 / 10,096** 个条目、**61,857,101 / 17,453,940 bytes** 的归档逐项核验字节 SHA-256、权限、硬链接 inode 身份、符号链接目标和 FIFO 元数据；远端前后清单一致，独立 gzip/归档摘要检查退出 0，均无外部硬链接。首次 baseline 核验器对 tar 硬链接类型的失败原样保留，仅证据脚本修正，原同一归档完整复核通过；未改变产品源码或验收断言。

只读检查 136 个进程、738 个 fd 及 cwd/root/exe/maps，未发现夹具目录引用或检查错误。两目录分配空间合计 **3,076,734,976 bytes / 52,075 du inode 条目**；本机完整归档为 **52,079 个条目 / 79,311,041 bytes**。精确目录与核验报告见 `full/capacity-recovery-proposal.md` / `.json` 和[保全检查点](../../superpowers/plans/2026-10-31-postcheck-terminal-guard.md#verified-fixture-preservation-and-pending-capacity-recovery--2026-10-31)。该历史时点尚未收到逐目录确认，远端夹具仍在；随后确认、复核、删除与全新重跑另列如下，旧计数不代填，整体平台目标仍开放。

## 2026-10-31 授权容量恢复与修正版完整门禁

用户分别确认删除两个精确夹具目录。执行前再次核对完整远端清单与本机归档、SHA-256/gzip、目录 device/inode、祖先路径、挂载和进程引用；每目录使用一条独立删除命令，均退出 0，两个目标均不存在。旧运行根内夹具子树外全部文件的字节、权限和 inode 不变；完整本机归档、历史失败及原报告均保留。最终观测 **6,126,874,624 bytes / 12,011,729 inode** 可用，不将第一次删除前已有的容量变化归因于本次操作。证据 `full/authorized-recovery-k5a1cb98/recovery-summary.json` 保留恢复时点的 `full_gate_completed=false`，后续测试另行记录。

同一修正冻结 `85488105cd2d3b610fcbfa18fc2273bd02618db1`、**898 文件**在新 `/tmp/afcg-rerun-7_12st95` / fresh basetemp 完成原严格全仓：**5845 passed、197 passing subtests、0 failures/errors/skips**，pytest **1730.57s**，控制器 **1732.42s**、退出 0。从启动到结束只有原 PID 3414182 / start ticks 193334474；没有因观察等待启动第二个 pytest。原 Python 3.13.16 / Node 22.21.1 / Playwright 1.63.0 / Chromium 153.0.8010.12、沙箱、测试、selector 和断言均保持，仅证据根路径改变。

原远端与复制后的本机 release verifier 均退出 0：**42 journeys / 1747 selectors / 5845 JUnit cases / 44 PNG**。898 文件前后摘要一致、冻结 checkout clean；**50 文件 / 7,940,720 bytes** 逐项大小/hash 相同，归档 SHA-256 `3eaeec70306e25075202b4a93a2ace343af1d61d2eb63dae8dd2bffcddcf8aef`。完整原命令、日志、JUnit/journeys/PNG、源码审计及 `corrected-preservation.json` 保存在 `/Users/mango/.cache/agent-fleet/verification/postcheck-guard-20261031-t0mae74m/full/rerun-prepared-7_12st95/`。原远端证据脚本完成后，本机 shell 包装记录退出码遇到 zsh 只读变量错误；该错误保留，直接原 verifier 的独立命令退出 0，没有改验收或重跑 pytest。

[终态保护计划](../../superpowers/plans/2026-10-31-postcheck-terminal-guard.md#authorized-recovery-and-verified-corrected-full-gate---2026-10-31)与[全仓计划](../../superpowers/plans/2026-10-31-postcheck-full-gate.md#verified-corrected-source-rerun---2026-10-31)的修正版全仓项已闭合；原生十二项、旧 5833-pass 基线和失败运行各自保留独立范围，不合并计数。完整 WF-04/05、NODE-LIVE、真实模型/CLI、生产隔离和外部验收继续开放；原生 `full_workflow_completed=false`、七项 external checks `not_run`、所有 gate 默认 false。没有新增开发提交、推送、合并、部署、安装、全局设置/服务或 Obsidian 变更。

## 2026-10-31 自动 capability Workflow lifecycle 闭环

[生命周期计划](../../superpowers/plans/2026-10-31-capability-workflow-lifecycle.md)新增 `tests/test_capability_workflow_lifecycle.py`：固定一阶段 native Workflow 只通过原 coordinator 自动 admission，接入原 Run worker、签名 Node HTTP、Node journal 和原 capability dispatch。写 capability 与独立 `read_only` digest helper 均在 Workflow contract、binding、Hub action registration、Node local definition/allowlist/offer 中预声明；没有新增重试、执行状态或传输协议。

红色证据先复现了真实边界：Node 已完成 workspace write 后回执丢失，原 Run/Workflow/Command 进入 `unknown`，reservation 保持 `held`，但 `workflow_dispatches.stage` 仍为 `bound`，导致只读 postcheck 稳定返回 `capability_changed`。最小修复位于 `hub/infrastructure/workflow_repository.py` 的 `apply_observation`：普通 unknown observation 与 step/observation snapshot 在同一事务将 dispatch stage 记录为 `unknown`；reconciled 路径、原 binding/token/generation、预算和 reservation 语义保持不变。没有重放写入，也没有放宽 retry。

修复后的生命周期模块 **4 passed**（Python 3.10）和 **4 passed**（Python 3.13），包含 successful write、matched/mismatch/read_failed 独立 digest、unknown retry 的 `unresolved_execution`、held reservation、原 binding/token/generation 和重复/重启零 I/O。相关 coordinator/dispatch、Workflow dispatch/control/coordinator、Node capability/runtime、worker 和 postcheck 回归各 **400 passed**；编译检查通过。证据分别保留在 `/tmp/af-lifecycle-red-cVRei9`、`/tmp/af-lifecycle-red-all-u3za4K`、`/tmp/af-lifecycle-red-pc-XZbYwV` 及 `/tmp/af-lifecycle-green-final-Kshrow`、`/tmp/af-lifecycle-313-K3y3Z6`、`/tmp/af-lifecycle-related-310b-r4onXc`、`/tmp/af-lifecycle-related-313-9ktvFX`。

本轮只闭合本地自动 capability lifecycle slice；NODE-01/WF-08 已登记两个真实测试函数，`docs/testing/journeys.json` 的七项 external checks 保持原样。完整 WF-04/WF-05、actual-host coordinator lifecycle、真实模型/CLI、生产隔离和 live external checks 仍开放；`full_workflow_completed=false`、external checks 仍 `not_run`，所有 gate 默认关闭。该轮计数不拼接到此前完整门禁。


## 2026-11-01 原生自动 capability lifecycle

[子进程生命周期计划](../../superpowers/plans/2026-11-01-capability-subprocess-lifecycle.md#verified-native-automatic-lifecycle--2026-11-01)的显式探针现已通过原 coordinator 自动准入、原 Run worker、签名 HTTP 和实际 Windows/Linux Node，移除了旧平台探针中 test-only unknown Workflow 设置的依赖。实际报告的 OS/architecture/software 在首次 tick 前固定为可信路由约束；同代执行保持原 offer/binding/token/generation。固定 provider 只请求一次固定 workspace 写入，没有调用真实模型或商业 CLI。

冻结 `719d8e7007ded5ef1ebe6322a4715ac30521e7da`、902 文件；home-win Windows/Python 3.12.7 与 gz-cvm Linux/Python 3.13.16 各覆盖 success、unknown+matched/mismatch/read_failed，原单次 **8 passed、零失败/错误/跳过，105.74s**。八份 Hub 数据库与两端 Node journal/固定 bytes 独立复核通过：success 仅在正常 observation 后带绑定证据释放容量；三种核对仍保留 unknown/held，retry 拒绝，重复/重启零 I/O；成功仍缺 required outputs，不能显示已交付。

证据 `/Users/mango/.cache/agent-fleet/verification/capability-native-auto-20261101-7sx6gzta/verification-summary.json`；三端902文件前后 hash 一致、冻结 checkout clean。两端 **42 文件 / 363,814 bytes** 复制大小/摘要全匹配。相同冻结的关联回归在本地Python3.10/3.13各 **400 passed**、完整compileall退出0；扩展及旧探针/原生命周期分别各 **14 passed**。原生固定矩阵、前轮全仓与本轮结果各保留独立范围，不拼接计数。

用户随后确认的单一旧夹具目录已完整归档复核后单独删除，范围外全部证据保持。相同902文件的原严格Linux全仓首次结果为 **5848 passed、1 failed、197 passing subtests、零errors/skips**，退出1/ci_status=failed；42 journeys均通过、44 PNG已生成。唯一失败是浏览器认证用例重复跳转后的登录标题超时。专项追踪捕获13次 Chromium `fallocate(..., 2097152)` 返回 `ENOSPC`，失败现场 `/tmp` 仅余2,641,920 bytes；renderer 已分配的临时 backing 文件耗尽磁盘空间，导致本地静态资源请求 `net::ERR_INSUFFICIENT_RESOURCES`。主机内存与 cgroup OOM 计数不支持内存耗尽解释。

用户确认删除并已完整备份核验的精确旧 pytest `fixtures` 目录释放约2.02 GB；同目录外全部证据的 inode、权限和文件摘要保持不变。相同冻结、原断言与5秒超时的认证浏览器用例在新私有 basetemp **23.52秒通过**，902源码摘要未变、checkout clean。随后以原测试/selector/runtime/sandbox 和新的证据/basetemp 根路径运行完整严格命令，PID `3914193`、start ticks `204085717`，启动时 `/tmp` 空闲4,276,719,616 bytes；1634.29秒后退出0，结束仍有2,166,775,808 bytes。最终为 **5849 passed、197 passing subtests、0 failures/errors/skips**；JUnit 5849 explicit cases / 6046 summary tests、42/42 journeys、1751 selectors、44 PNG，`ci_status=passed`。原远端及复制后本地 release verifier 均退出0；50份证据7,933,413 bytes大小/hash匹配，压缩归档5,518,851 bytes、SHA-256 `05e9a6a1d75d7f70d4b9baae63d154c3c660bf53b72c3d5e3e35582444861d99`；902源码摘要一致、checkout clean。原失败报告和诊断仍保留，计数不合并。完整WF-04/05、真实模型/CLI、生产隔离和七项外部检查仍开放；所有gate默认关闭。

## 2026-11-01 Hub 本地输入位置修复

按[输入位置计划](../../superpowers/plans/2026-11-01-workflow-input-location.md)修复 transfer gate 组合：已登记的 Hub 本地评审、固定测试和完整文本 Native Run 不再错误等待 Node alias/receipt。登记只从实际 RunStepExecutor 的可信 target 派生，固定 owner/workspace/target、执行 kind 及 adapter/policy ID/revision；最多256项，联合身份索引均摊 O(1)。协调器准备与原 admission 共用登记；未知、普通 Native、Legacy 和远程目标沿用原签收屏障。当前 generation 已固定 Node 的仍须原签收，不因后续 Hub 登记绕过。登记与 Node 输入位置冲突时先拒绝、后发布配置，拒绝不改变旧索引。未新增依赖、表/列、wire marker、队列或执行引擎。

原 Cookie HTTP→fixture CLI→源码→评审→固定 pytest/JUnit→人工接受组合，在 transfer gate 开启后两个正负 verdict 实例均以 input_alias_unregistered 失败；修复后原用例转绿。三个完整聚焦模块 **140 passed / 12.91s**，其中新增边界模块为9个函数、39个参数实例。最终注解后的 anchors **46 passed / 12.71s**。54模块关联集 Python3.10 **1949 passed、7 skipped、34 passing subtests / 224.80s**；Python3.13 串行 **1949 passed、7 skipped、34 passing subtests / 207.97s**，两个版本全量 compileall 均退出0。七项 skip 仅为本机未配置 Playwright，不计完整门禁。首次双版本并发打包产生 staging-presence 失败；原断言串行打包38项/4subtests及完整关联集随后通过，失败证据保留。

执行契约§5.5与产物契约§4.1明确实际消费位置；journey矩阵只追加30项登记，全部既有 required selectors 保留。独立证据冻结 revision `0de03b72e0b2ea34f595f334ac3f6a95ac44864c`、904文件；冻结前开发树逐项摘要一致，既有902文件5849-pass报告仅是历史基线。证据根 `/Users/mango/.cache/agent-fleet/verification/workflow-input-location-20261101-nzcdxbz9` 保存来源、双版本报告和完整门禁驱动。原严格Linux完整门禁随后通过，终态见下节，运行检查点不充当通过证明。

用户逐路径确认后，重新核验本机归档62311415 bytes / SHA-256 `310142c4f2f44536db7183774a0f627794d946a2ea1b41464849311657ef5871` 与42102项原清单、全部字节/模式/硬链接，确认无进程引用/子挂载，再以独立命令仅删除 gz-cvm 的 `/tmp/afla-7sx6gzta/evidence/controlled-full-rerun-1/fixtures`。目录外54项的inode/权限/大小/hash保持一致；删除后空闲3967401984 bytes。完整归档及删除前后审计保留，源码、原报告/日志/PNG和runtime未清理。本轮未新增安装、下载、全局配置/服务、开发提交/推送/合并/部署或Obsidian变更。

### 904文件冻结的严格Linux终态

冻结0de03b72e0b2ea34f595f334ac3f6a95ac44864c的原完整命令在2026-11-01T12:24:16Z–12:53:35Z运行，退出0；pytest **5891 passed、197 passing subtests、0 failures/errors/skips**，1757.26秒，受控驱动1759.25秒。JUnit为5891个显式testcase / 6088个含subtests的summary tests，不重复累计。42/42 journeys、1781 selector登记、44张完整PNG全部通过；矩阵SHA-256为489b6c6a6f98d84741e6284723680c4c7e56c5f9ec6aea731b6341621d742645。沿用Python3.13.16/Node22.21.1/Playwright1.63.0/Chromium153.0.8010.12/LC_ALL=C、移除DAC绕过能力和NoNewPrivs，未改变原断言、超时、selector或sandbox。Python3.13 Linux证据与CI Python3.10分别记录，不拼接双版本关联计数。

原tools.testing.release_evidence在gz-cvm与本机冻结checkout各退出0，均核验42 journeys / 1781 selectors / 5891 JUnit cases；七项external checks全部not_run。904份源文件前后hash一致、两份冻结checkout clean。50份复制证据共7,947,220 bytes，逐项大小/SHA-256完全匹配；证据归档5,518,828 bytes / SHA-256为6f97b7e8ff44e978d80bfad34f28c564f2f496aed54509512eb2da8e3ce04ead。原PID4008332/start ticks205658895、启动/结束空闲3,963,764,736 / 1,776,926,720 bytes和原命令保留于full-command.json。完整本机证据在上述根的full-copy/controlled-full/，复制复核在full-copy/copy-verification.json；来源、原红色报告、本地打包失败与串行通过报告均独立保留。

本轮[输入位置修复计划](../../superpowers/plans/2026-11-01-workflow-input-location.md#verified-strict-linux-terminal-result--2026-11-01)全部勾选完成，代码复核未发现本轮新增问题。结果补录只改变文档，不回写冻结checkout或把新文档冒充已测revision。完整WF-04/05、真实模型/商业CLI、生产隔离配置和七项外部验收仍开放；所有gate默认关闭，整体项目目标继续进行中。
