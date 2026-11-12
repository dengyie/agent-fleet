# Agent Fleet 深度优化开发入口

本轮[生产发布审查](../../reviews/2026-11-13-production-release.md)已修复七项实际问题；完整同版CI、宿主receiver同步及main部署/真实验收分别留证。用户已授权发布。

模型摘要生成功能正在按[独立规格](../../superpowers/specs/2026-11-13-model-summary-generation.md)和[实施计划](../../superpowers/plans/2026-11-13-model-summary-generation.md)推进，复用原Run队列及摘要权威；未完成或发布的部分不由既有operator摘要/视频CI代填。

部署前补充[摘要 reader 保留修复](../../superpowers/plans/2026-11-13-summary-reader-preservation.md)：首次摘要发布不再降低图片/音频/视频 Run 已要求的 reader13/14/15，原摘要事务失败仍完整回滚。当前未发布；其独立验证不能由PR44/45既有门禁代填。

最新公开试运行候选为PR42→PR43→[PR44](https://github.com/dengyie/agent-fleet/pull/44) / `6ea7e09`；[完整CI43292358951](https://github.com/dengyie/agent-fleet/actions/runs/43292358951)经原工件独立核验7605Linux cases、46journeys/2560selectors、Windows332passed/2POSIXskips。候选包通过真实生产receiver check，尚未合并/切换，线上仍为301e259。生产Workflow登记、可用Node/本地执行环境与任务/下载/恢复验收仍待完成；容器namespace探针实际拒绝，不能用healthz或目录预检代填。见[最新发布与使用条件](../development-completion.md#pr44-最终-ci-与实际使用条件2026-11-13)。

后续开发增量已补本地Native完整MP4引用输入，固定`video_input=True`、schema7/reader15和完整`video_url`data URL，不改旧图片/音频快照；见[视频计划](../../superpowers/plans/2026-11-13-native-video-inputs.md)和[执行合同](../../superpowers/specs/2026-10-20-workflow-execution-contract.md#544-本地-native-run-的完整-mp4-视频输入)。此增量不属于上方PR44证据，须独立验证和发布；真实视频模型与非Native媒体context继续开放。

Fleet 身份页面登记与Native初始化已进入独立[PR43](https://github.com/dengyie/agent-fleet/pull/43)：[官方CI43272902162](https://github.com/dengyie/agent-fleet/actions/runs/43272902162) Linux7541项、46流程、2529selectors全部通过，Windows277通过/2平台跳过；原报告与精确版本已独立核对。见 [操作说明](integration-setup.md#从账号页面登记)、[实施计划](../../superpowers/plans/2026-11-13-fleet-identity-registration-ui.md)及[开发总账](../development-completion.md#pr43-完整-ci-与后续-mcp-页面接入2026-11-13)。未合并/上线；后续[MCP页面接入](../../superpowers/plans/2026-11-13-mcp-connection-registration.md)、正式生产配置、真实业务验收及其余TODO仍开放。

> 当前状态（2026-11-13）：公开候选 `db4c7ff` 的 PR42 官方 CI 已通过：Linux7482项、0失败/跳过、46/46流程、2508selectors；Windows224通过/2平台跳过。真实CLI试点完成开发/独立评审/31固定测试/5产物下载和重登恢复，仍待人工接受；实际跨机Node四场景通过。生产receiver的候选包check已过，未合并/部署，main/现网仍为 `301e259`。正式CI打包部署、生产模型/新鲜Node的实际任务与恢复、完整WF-04/05及其余F5/F6 TODO仍开放。详见[最新开发总账](../development-completion.md#公开候选-ci-与生产包预检2026-11-13)。

> 2026-11-13独立增量：Native workspace/conversation 可通过同一 preview/apply CLI 初始化，支持精确模板选择、1–8 step和共用会话去重；原对象保留及事务不变。此改动已进入PR43并通过上方官方门禁，不混用PR42或生产证据。见 [初始化操作](startup-registration.md#工作区和会话初始化)与 [实施计划](../../superpowers/plans/2026-11-13-native-workflow-setup.md)。

> 2026-11-12功能补齐：外部 Agent 合同的 `capability_requirements` 必须与已登记 adapter 显式声明的能力 revision 匹配，并随不可变 target/configuration digest 固定；不匹配在 admission 前稳定拒绝。旧空能力 target 快照保持兼容。受控外部 Workflow 回归 427 项通过；真实外部账号与生产试点仍按原边界单独验收。

> 历史部署判断（2026-11-11）：19:42 SGT复核现网/GitHub main仍为301e259，本轮未发布。当时90374763冻结本机完整7201 passed/11 Linux专用skip/零失败错误，41/44 journeys、2154 selectors、46完整PNG；同冻结Linux全量7162 passed/50 skipped、零失败错误，28/44 journeys，48项因Chromium未配置跳过、2项rsync用例随后独立补测8 passed，原核验拒绝journey_incomplete。另有Linux261项关联及11专用实例通过，Windows CI选择与新增回归238 passed/2 POSIX skips。32流程tick两次P95为494/498ms，达到500ms初值但余量小。宿主container receiver尚缺候选jsonschema预检。当时浏览器准备条件已由11/12复用现有私有程序解决；这些旧报告不代填当前候选。详见[历史部署判断与证据](../development-completion.md#当前部署判断2026-11-11)。

> 2026-11-11 最新正式模型评审入口：新增仓库内置单次HTTP评审命令与Linux launcher，原Hub登记/Run worker/报告校验可直接使用；完整文本源码分片发送，不默默截断。`a2aed0eb`冻结1182文件，18模块macOS关联335 passed/6 Linux-only skips、Linux同集合341 passed/0skip，各2 subtests。原包1106后端/67前端源文件字节一致；44 journeys/2114 selectors/7148 cases仅收集核验。对端为受控HTTP，真实模型服务/生产配置、最终完整Linux/browser门禁与部署仍开放。见[正式评审入口证据](../../superpowers/plans/2026-11-11-deployable-model-reviewer.md#verified-results--2026-11-11)及[配置方式](startup-registration.md#仓库内置模型评审器linux)。

> 2026-11-11 最新固定测试部署入口：正式Linux launcher已可用于原Hub/Node配置；`2570449a`冻结1178文件，macOS关联230 passed/3 Linux-only skips、Linux同集合233 passed/0skip，各2 subtests。原包1102后端/67前端字节一致；44 journeys/2104 selectors/7107 cases仅完成收集核验。该版本完整Linux/browser发布门禁、生产模型评审配置与部署仍开放。见[入口与验证结果](../../superpowers/plans/2026-11-11-linux-workflow-launcher.md#verified-results--2026-11-11)。

> 2026-11-11 最新初始化切片：`1d982fce` 冻结1175文件完成工作区/三会话CLI与平台迁移竞争根因修复，三端相同31项全部通过，原CLI额外40组/80进程全通过。本机完整7089 passed/6个Linux专用skip/204 subtests，42/44 journeys、2097 selectors、46完整PNG；Linux关联281 passed/1浏览器未配置skip，Linux进程79 passed/2subtests，Windows31 passed/0skip。原release verifier仍拒绝journey_incomplete，不能拼合不同平台报告代填完整门禁。详见[初始化与完整证据](../../superpowers/plans/2026-11-11-workflow-bootstrap.md#verified-results--2026-11-11)。生产配置、完整Linux/browser与部署仍开放。

> 最新真实试点（2026-11-11）：当前目标树原Workflow/Runner已完成真实Codex开发、独立模型会话评审、31项固定sandbox测试及5产物核验；原任务在真实Chrome9222桌面/手机刷新和页面下载通过，仍等待人工验收。同模型独立会话、单私有macOS环境，不代填生产隔离、完整Linux门禁或其他外部试点。详见[真实CLI试点计划](../../superpowers/plans/2026-11-11-real-workflow-cli-pilot.md)与[开发总账](../development-completion.md)。

> 2026-11-11摘要完成与根因修复：4a3e15f1冻结本机全仓7043 passed/5个Linux专用skip/204 subtests、43/44 journeys、2071 selectors、46完整PNG；同源Linux关联832 passed。后续b44fe1bc修复Windows备份fsync，三端相同摘要/明文加密备份集合各96 passed/0skip。当前矩阵44条/2073 selectors、7050实例可解析；原release verifier仍拒绝journey_incomplete。源码与原失败、报告及范围见[摘要计划](../../superpowers/plans/2026-11-11-versioned-summary-context.md)，完整Linux/browser、真实CLI/model和发布仍开放。

> 2026-11-10 最新目标树：远程取消已贯穿worker/broker/回执等待，未领取不再派发、已派发不明保持unknown，轮询不重复续写租约。冻结 `69028deb…` 全仓6903 passed/5平台skip/204 subtests，43/44 journeys、1984 selectors、46完整PNG，1124源码不变、checkout clean。严格Linux门禁仍开放；旧数字保留原版本归属。见[开发总账](../development-completion.md)。

> 当前目标分支（2026-11-10）：以下源树历史检查点只描述各自冻结版本。本次 `codex/functional-root-fixes` 按 `hub -> fleet_core <- tools` 增量集成并保留最新main功能，当前证据和开放项以[文档开发总账](../development-completion.md)为准。本机全仓6887 passed/5平台skip/204 subtests，43/44 journeys；严格Linux验收仍开放，未部署。


> **当前整合状态（2026-11-10）**：本目录从 `codex/workflow-platform-docs` 源工作树带入当前 `codex/functional-root-fixes`。下文的实现和 Linux/Windows 数字属于源树及各自冻结版本。目标树已接通F1–F5本机子集，补齐CI隔离工具/F401及流式PNG校验；第四轮1975-selector完整回归零失败/错误、5个平台skip。随后运行依赖分类修复另验182 passed，新增启动回归后当前矩阵1976 selectors；当前树真实跨平台/外部试点尚未验证。逐项状态、命令和证据见[文档开发总账](../development-completion.md)。

维护日期：2026-11-02。状态：F1 与 F2.1–F2.5 已落盘，F3 本机受控交付链、F4.1 Hub V2 分片发布/恢复、F4.2 POSIX Node 固定收发/统一容量、F4.3 能力目录/原子 admission、Node 报告/本地执行前复核、Hub 固定 binding 派发、协调器自动选点/输入目标固定及 capability-bound unknown/isolated workspace digest postcheck 本机子集已实现。独立远程固定测试修正后的918文件冻结（2915fe6c5ca859f99650951b6e07981d1da44634）原严格Linux全仓已通过：6010 passed、197 passing subtests、零失败/错误/跳过、42 journeys/1839 selectors、44 PNG，远端与本地原 release verifier 均通过；传输与能力 gate 默认关闭。Windows 接收已有真实 NTFS/句柄/ACL 与原 NodeRuntime 远程 HTTP 证据；Windows 固定源导出也已通过原生/原 HTTP 与同源 Linux 全仓；两 Node 源包已在实际 Windows→Hub→Linux 完成交接与独立固定验证；完整 WF-04/05、获准真实模型/CLI、生产隔离及外部验收尚未完成。

本轮目标是让一个需求经过专业执行、产物交接、独立校验和人工验收，形成可恢复的交付流程。沿用 Flask、静态 SPA、SQLite、既有 Run/Task/Command、签名投递与节点主动出站连接。

F5 的本机集成边界已落盘：固定只读 MCP connector/目录与原生 Run 接线、受控 Muse adapter、三工具 Fleet MCP 出口。MCP Run 在提交时持久固定授权摘要，排队/重启不能取得新定义或新权限，撤权在模型请求前拒绝。外部 Agent 已接入原 Workflow 派发/核对链：原 admission 原子保存注册快照，固定 journal 身份，恢复只查原任务，状态与取消回执精确匹配原 task/session；Agent 自报完成保留 `agent_reported`，不生成验证或接受证据。外部步骤已支持完整有界 UTF-8 文本产物输入与持久固定请求；外部显式 Memory 与本地 native_run 完整文本/Memory 已实现；显式 operator 版本摘要已接独立权威和 Native/外部请求（见[摘要计划](../../superpowers/plans/2026-11-11-versioned-summary-context.md)）；本地 Native 固定代码包输入已实现，外部 adapter 的 capability requirements 已接入不可变 target 快照并在 admission 前匹配；本地Native图片/音频及后续MP4输入已实现；CLI/review/test/remote媒体context 及 Muse cancel/export 仍 unsupported。显式 export-capable adapter 可只读查回已发布 Hub ArtifactRef 的 artifact/structured/code_revision 输出，经完整核验后进入原 Workflow 输出链。Fleet 身份登记/撤销已持久化，创建与查询在原 Workflow 事务复核当前授权；恢复备份禁用导入凭据。MCP 目录已持久化登记、批准/待审 revision 和跨进程撤销；默认生产装配与 Workflow gate 独立，适配器须显式附着，恢复副本禁用原目录。MCP 目录管理员界面的本机子集已实现，提供脱敏查看、完整定义审阅、精确版本批准和撤销；Fleet 身份查看/撤销界面已实现，保留原不可变登记和事务授权；其余集成配置和真实外部试点尚未完成，完整 F5 继续开放，三个集成 gate 默认关闭。

Windows 导出冻结源码的 Linux 全仓 **5785 passed、197 passing subtests、0 failures/errors/skips**，42/42 journeys passed；远端及本地原 release verifier 均退出 0，44 张 PNG 完整核验，七项外部检查均 `not_run`。Windows 合并 131 项原生测试与远程导出 HTTP 2 项分别通过，双 Python 关联各 295 passed。Python 3.13.16 的 Linux 全仓与 CI Python 3.10 配置分别记录，下方历史切片保留原计数。完整证据见[实施进度](implementation-progress.md#windows-导出最终冻结闭环)。

固定能力 workspace postcheck 的后续 Windows/Linux 原生矩阵 **12 passed、零失败/错误/跳过**，原 Command/Run/Workflow 仍 unknown，held/isolated 预约保持。冻结 `16f10ebd630d38fa53a839ce9d2c432719014120` 的 895 文件与两机前后摘要一致，原 journal、唯一 audit 和 64 份复制证据均已核验；详情见[能力原生验证](../../superpowers/plans/2026-10-30-capability-platform-postcheck.md#native-frozen-verification)。这一结果独立于导出全仓报告，完整 WF-04/05、真实模型/CLI 与生产/外部验收仍开放。

终态 owner/unknown 保护的修正冻结 `85488105cd2d3b610fcbfa18fc2273bd02618db1`、898 文件，独立完成原两机 **12 passed、零失败/错误/跳过**；两端 journal、十二份 Hub 数据库及 64 份复制证据一致。同源原 Linux 全仓首次因容量耗尽退出 120；容量恢复后新根/basetemp 原样重跑 **5845 passed、197 passing subtests、零失败/错误/跳过**，42 journeys、44 PNG、原远端/本机 verifier 均通过。详见[修正版完整结果](../../superpowers/plans/2026-10-31-postcheck-terminal-guard.md#authorized-recovery-and-verified-corrected-full-gate---2026-10-31)。

两组完整夹具的本机归档保留 52,079 个条目；用户逐目录确认后，复核完整清单/归档/身份/挂载/引用，再以两条独立命令删除精确远端夹具子树，范围外证据不变。同源修正版全仓的 50 份复制证据 / 7,940,720 bytes 全匹配，898 个源码摘要不变、冻结 checkout clean。见[恢复与完整门禁](../../superpowers/plans/2026-10-31-postcheck-full-gate.md#verified-corrected-source-rerun---2026-10-31)；完整平台与外部验收继续开放。

## 2026-10-30 Linux 门禁与实际沙箱续接

冻结 revision `8b93185d81dab3eb2442ef8fd0b863dc8eda2552` 在 Node 22.21.1 / Playwright 1.63.0 / Chromium 153.0.8010.12 / Python 3.13.16 / `LC_ALL=C` 上运行原完整门禁：**5724 passed、197 passing subtests、0 failures/errors/skips、42/42 journeys**。pytest 与原 release verifier 均退出 0；876 份源码前后摘要一致、checkout clean，44 张 PNG 完整核验，七项外部检查 `not_run`。此前 revision `1a4e4a29ff92760955d10ed74d52b3c70ec774aa` 的四个 macOS 专项 skip 和 verifier 拒绝属于保留的历史证据。

原 launcher 的测试专用 Linux bubblewrap user/mount/PID/network namespace 及 libseccomp 已补齐，源码/控制/运行时只读，写路径精确限制，缺依赖失败关闭。内核状态断言、无隔离对照、独立评审与完整 Cookie HTTP 链均在 Linux 执行；三模块 Linux **50 passed / 0 skip**，macOS Python 3.10.21 与 3.13.15 各 **50 passed / 0 skip**。WF-01 新增 3 selectors / 4 cases，原 verifier 保持零 skip 规则。Windows、真实两机、真实 CLI/模型、生产配置和 MCP/Muse 外部验收继续开放。

完整持久证据：`/Users/mango/.cache/agent-fleet/verification/linux-20261028-cm40i40b/sandbox-full-20261030-nzb5qq22/`。51 个证据文件与远端逐字节摘要一致，本地原 verifier 复核也退出 0；来源记录包含开发 HEAD 与独立测试 revision 的关系。Chromium 156 缓存已按用户授权恢复，301 个文件与官方归档一致，153 使用独立目录。实现与验证清单见[Linux沙箱实施计划](../../superpowers/plans/2026-10-29-linux-workflow-sandbox.md)。

## 2026-11-02 独立远程固定测试能力

[专用契约](../../superpowers/specs/2026-11-02-remote-fixed-test-capability.md)与
[实施计划](../../superpowers/plans/2026-11-02-remote-fixed-test-capability.md)已落盘：
`coding.test@1` / `tool.workflow.test` 仅执行管理员固定 recipe，绑定原接收回执、
源码、subject、generation、dispatch token 与 capability binding。原 Node journal
防止重放，Hub 核验完整报告/JUnit 后复用交付事务；unknown 与 held/isolated 保持。
远程 Run 创建事务钉定 minimum reader 10，备份恢复 frozen。

本机 35 模块关联回归 **1272 passed、零失败/错误/跳过**，其中四个新模块 115 项；
NODE-01/WF-03/WF-08 登记本机实现范围。独立原生夹具随后在 gz-cvm 上通过真实
NodeRuntime、签名 Command、HTTP 接收、journal 和固定 Linux launcher 完成通过、
断言失败、丢回执恢复及 unknown 四场景：**4 passed、零失败/错误/跳过**。每场
recipe 只执行一次，terminal replay 不重新执行，unknown 保留 held 预约。此动作仅
接受 Linux route；生产隔离验收仍开放，Windows 动作不可用，gate 默认关闭。
918 文件旧冻结原严格全仓为 **6009 passed、1 failed、197 passing subtests**，唯一
失败是过时的 reader 10 拒绝夹具。修正后的独立918文件冻结 `2915fe6c…` 已通过
原严格Linux全仓：**6010 passed、197 passing subtests、零失败/错误/跳过**；
**42/42 journeys、1839 selectors、44 PNG**，远端与本地原 verifier 均退出0，
50份证据/7,986,144 bytes逐项大小及摘要一致，源码摘要不变、checkout clean。
旧失败与成功运行分别保留，门禁后仅补录文档；七项外部检查仍not_run，
完整 WF-04/05 不标记完成。报告与限制见[实施进度](implementation-progress.md#2026-11-02-独立远程固定测试能力)。

## 阅读顺序

| 文档 | 回答的问题 |
|---|---|
| [总体设计](../../superpowers/specs/2026-10-20-workflow-platform-design.md) | 产品目标、现状、架构边界与设计裁决 |
| [工作流执行契约](../../superpowers/specs/2026-10-20-workflow-execution-contract.md) | 对象、状态机、幂等、审批、取消、预算和 API |
| [产物、节点与工具接入契约](../../superpowers/specs/2026-10-20-artifact-capability-integration-contract.md) | 文件如何交接、如何选节点、如何接 MCP 和外部 Agent |
| [分阶段开发路线](../../superpowers/plans/2026-10-20-workflow-platform-roadmap.md) | 实施顺序、文件归属、依赖、交付物和阶段退出条件 |
| [Hub 输入位置修复](../../superpowers/plans/2026-11-01-workflow-input-location.md) | 本地消费者与Node签收边界、904文件冻结完整门禁 |
| [首批领域合同实施计划](../../superpowers/plans/2026-10-20-workflow-foundation.md) | 第一批可直接执行的代码与回归计划 |
| [能力目录与原子 admission 计划](../../superpowers/plans/2026-10-23-capability-catalog-admission.md) | F4.3 当前可验证子集和后续两端接线 |
| [Node 报告与执行前复核计划](../../superpowers/plans/2026-10-23-node-capability-report-preflight.md) | 显式本地能力、真实报告与原签名/journal 生命周期 |
| [Hub 固定能力派发计划](../../superpowers/plans/2026-10-23-hub-capability-command-dispatch.md) | 原 Run/binding、事务内签名准备、审批与首次 lease 复核 |
| [协调器选点与输入目标固定计划](../../superpowers/plans/2026-10-23-capability-coordinator-input-pinning.md) | 管理员路由登记、同事务 input target/receive、原 admission 和双进程容量 |
| [Node poll 凭据复核计划](../../superpowers/plans/2026-10-23-node-poll-credential-fencing.md) | 认证后轮换/禁用与首次 claim 的事务边界 |
| [isolated 资源屏障修复计划](../../superpowers/plans/2026-10-23-isolated-reservation-guards.md) | 固定目标准入/代际屏障与原能力路由统一计入隔离占用 |
| [F5 集成与出口计划](../../superpowers/plans/2026-10-24-f5-integrations.md) | 固定只读 MCP、外部 Agent 能力声明与三工具 Fleet 出口的本机边界 |
| [MCP Run 授权固定计划](../../superpowers/plans/2026-10-25-mcp-run-pinning.md) | 排队/重启后的私有授权摘要、定义复核与同 token 原配置恢复 |
| [外部 Agent Workflow 派发计划](../../superpowers/plans/2026-10-25-external-agent-workflow-dispatch.md) | 原子固定注册、原任务/journal 查回、取消与自报证据边界 |
| [外部 Agent ArtifactRef 输出计划](../../superpowers/plans/2026-10-26-external-agent-artifact-outputs.md) | 原任务只读导出、完整字节/schema/源码包核验、原事务与 reader8 恢复 |
| [外部 Agent 文本输入计划](../../superpowers/plans/2026-10-25-external-agent-text-inputs.md) | 完整 UTF-8 产物输入、固定私有请求、原任务恢复与 reader 兼容 |
| [Fleet 身份持久化计划](../../superpowers/plans/2026-10-25-fleet-integration-identities.md) | 不可变登记、跨进程撤销、事务内复核与恢复凭据禁用 |
| [MCP 目录持久化计划](../../superpowers/plans/2026-10-25-mcp-catalog-persistence.md) | 独立平台 reader、持久版本审批、跨进程撤销与恢复禁用 |
| [MCP 目录管理计划](../../superpowers/plans/2026-10-25-mcp-catalog-administration.md) | Cookie 管理员、脱敏分页、完整待审定义与 epoch 审批/撤销 |
| [MCP 启动登记与发现](mcp-startup.md) | 正常 Hub 文件配置、环境 SecretRef、离线预览和显式单连接器发现 |
| [Fleet 身份管理计划](../../superpowers/plans/2026-10-25-fleet-identity-administration.md) | Cookie 管理员、完整权限查看、复合分页与事务内撤销确认 |
| [Fleet MCP 身份运维登记](integration-setup.md) | 摘要配置、离线预览、显式单身份登记与正常 Hub 持久鉴权 |
| [Muse 正常启动登记](muse-startup.md) | 本地 journal 显式初始化、模板/目标文件装配、关闭新发送后保留原任务恢复 |
| [能力跨平台 postcheck 计划](../../superpowers/plans/2026-10-30-capability-platform-postcheck.md) | 固定只读 digest helper；Windows/Linux 原生机 held/isolated × 三种观察结果 12 项通过 |
| [postcheck 原子审计计划](../../superpowers/plans/2026-10-30-postcheck-atomic-audit.md) | 终态证据与审计同事务、历史缺审计恢复；双 Python 关联各 279 项通过 |
| [postcheck 修复前全仓基线](../../superpowers/plans/2026-10-31-postcheck-full-gate.md) | 旧冻结 5833 项通过、42 journeys、44 PNG；不证明后续终态保护 |
| [postcheck 终态原命令保护](../../superpowers/plans/2026-10-31-postcheck-terminal-guard.md) | 完整终态复核 owner/unknown；双 Python 关联各 291 项、修正版两机 12 项和同源全仓 5845 项通过 |
| [自动能力子进程/原生生命周期](../../superpowers/plans/2026-11-01-capability-subprocess-lifecycle.md) | 原协调器与 Run worker 驱动实际 Windows/Linux 固定能力；8 项原生测试、unknown/held 和重启零 I/O 的独立证据 |
| [真实两机源包交接计划](../../superpowers/plans/2026-10-30-two-host-artifact-handoff.md) | Windows 生产/原 Hub/实际 Linux 接收、原回执恢复及独立固定验证 |
| [实施进度与续接](implementation-progress.md) | 已落地文件、验证证据、下一切片与未实现边界 |
| [验收与恢复矩阵](../../testing/workflow-platform-acceptance.md) | 测试边界、故障注入、真实试点、性能和发布证据 |

## 基线与事实口径

- 本轮独立文档工作树建立于本地 origin/main：4cd816230fd1d8d5bf02c9f086959d8b5b1a27cf。这个 SHA 是可复现的源码基线；本轮没有查询远端最新指针或验证线上部署。
- 勘察开始时，主工作树位于 codex/windows-agent-fleet，HEAD 8056137，并有其他开发中的未提交改动。用户确认保留现有工作；文档与后续代码均在 codex/workflow-platform-docs 独立工作树实施。主工作树的并行演进不作为本设计已验证的功能事实。
- 代码事实依据：RunWorkerService、ToolBroker、RemoteToolBroker、NodeToolExecutor、ArtifactStore、LegacyTaskBridge、HandoffService 和现有测试契约。
- 当前生产版本、启用配置和凭据边界由私有运维入口维护。历史计划中的测试数字或“待合并”文字不能替代当前部署事实。
- 已实现范围以[实施进度](implementation-progress.md)及测试证据为准。未实现端点、配置和外部接入继续作为规格；Workflow、dispatch 与新增 transfer gate 均默认关闭。启用后才注册相应 HTTP 与界面，执行 worker 仍须显式启动；transfer 还要求有效 Command signing key。

## 当前资产与新增范围

普通 Hub 启动的配置方式见 [Coding delivery 启动登记](startup-registration.md)，常驻节点见 [Linux 测试 Node 配置](node-startup.md)。这两个入口复用现有模板和执行器；当前线上版本与剩余验收以开发完成账为准。

新环境先按[工作区和会话初始化](startup-registration.md#工作区和会话初始化)预览并准备登记对象，再启动 Hub；不要依赖测试脚本造数据。

| 领域 | 已有资产 | 本轮新增设计 |
|---|---|---|
| 执行 | 固化模型/工作区/单执行节点的 Run；旧 CLI Task；签名 Command 和 Node journal | 多步骤 Workflow 在上层关联执行，不复制已有执行引擎 |
| 协作 | 有界会话接力；CLI adapters；显式 LegacyTaskBridge | 版本化 WorkContract、稳定输入、交付标准和证据关联 |
| 文件 | opaque artifact ID、owner/workspace 隔离、SHA-256、文本预览 | 通用跨节点导出/接收、分片、来源关系与配额 |
| 工具 | 固定 ToolBroker 白名单和部分节点能力声明 | 经管理员登记的能力目录及 MCP 适配 |
| 外部 Agent | Muse 原生工作区与可选 web_agent 桥接 | 按实际支持程度声明提交、查询、取消和文件导出能力 |
| 恢复 | Run/Command 租约、幂等、unknown、审计 | 流程协调恢复、分步重试、输入变更后校验失效 |
| 验收 | 测试摘要、diff、journeys、独立外部验收 | 执行、验证、人工接受三个事实维度 |

## 开发顺序

1. F0：整理当前证据、配置和未完成的真实运行边界，冻结试点范围。
2. F1–F2：纯领域合同、持久步骤状态和可靠派发；先做离线可恢复链路。
3. F3：贯通同一代码版本的开发、评审、测试和人工验收，交付第一个可用版本。
4. F4：通用产物交接和确定性节点选择，增加第二个真实节点。
5. F5：经批准的 MCP 接入、外部 Agent 适配和窄权限 Fleet 工具出口。
6. F6：有实际用例后扩展分支/汇合、业务模板和容量优化。

F3 是首个产品里程碑。MCP、复杂图编辑器和多种创作软件不是这个里程碑的前置条件。

## 文档维护规则

新协作领域以本入口链接的契约为准。已有浏览器提交/接管、Supervisor、账号、Muse 登录与审批、服务动作合同继续生效；协作层不能放宽它们。旧平台设计保留为背景与已实现能力索引。

每个实施切片同时更新代码、必需 journey、当前实现记录和兼容说明。证据至少标明源码版本、配置摘要、测试范围及真实外部边界。公开仓库只保留脱敏设计和可复现命令。

## 本轮文档校验

2026-10-20 检查了七份新文档及三个导航改动：UTF-8 无 BOM、68 个本地 Markdown 链接与锚点、六段 Python 示例语法、两段 JSON 示例及 diff 空白检查通过。首批计划的完整示例在仓库外临时目录验证，先确认缺模块失败，再运行 21 个领域测试通过；没有把示例写入项目运行时代码。

以上是文档编写时的历史证据。后续实现与新鲜验证分别记录在[实施进度](implementation-progress.md)，不得据此推断运行时或生产验收完成。

## 设计来源

[LINUX DO 经验帖](https://linux.do/t/topic/3046980)提供了跨机器工具协作、行业交付规范和文件流转的实践启发。帖子中的模型效果、套餐额度和云机规格属于作者经验，未作为本设计的已验证产品事实。

相关基础：[现有平台设计](../agent-fleet-self-hosted-platform.md)、[v4 控制面](../../architecture-v4-control-plane.md)、[Muse 工作区](../muse-workspace.md)、[现有测试入口](../../testing/README.md)。

## 显式 Memory 输入切片

2026-10-25 的[Memory 计划](../../superpowers/plans/2026-10-25-external-agent-memory-inputs.md)已闭合外部Agent显式owner Memory完整输入与原请求恢复，WorkContract2/reader6和external3保持旧prompt。Native/summary、二进制/代码包、能力要求、外部结构化产物及真实/跨平台验收仍开放。全仓 **5256 passed、0 failures/errors、5 skipped、195 subtests passed**，WF-06 **302 selectors / 681 cases 全通过**，**41/42 journeys**；严格退出 **1**、ci_status=failed，唯一 incomplete 为 Linux RELEASE-01，七项外部检查均 not_run。证据目录：`/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-external-memory-full-20261025-dnxqbpck`。 完整记录见[实施进度](implementation-progress.md)。


## 2026-10-26 Native Workflow Memory verification

本地 native_run 完整显式 Memory 已实现：原 Run 事务私有快照/reader7、provider 前当前授权复核、原任务恢复、DTO 隐私与完整备份历史。20 selectors / 52 cases；串行 Python 3.10/3.13 关联各 886 passed、无 skip。该轮冻结门禁 **5346 passed、零失败、5 macOS 平台 skip、195 subtests passed**；WF-06 **327/759**、41/42 journeys，严格退出 1/failed 仅因 Linux RELEASE-01 未完成，七项外部检查 not_run；860 文件运行前后摘要一致。外部输出的后续实现以下节和[实施进度](implementation-progress.md)为准，完整平台与生产验收未完成。

## 2026-10-27 已发布 ArtifactRef 输出

原任务只读导出已发布artifact/structured/code_revision引用，完整核验后由原事务绑定输出/subject/state并提高reader8；Muse导出仍unsupported，不生成validator/人工接受事实。47个新selectors/178cases全部通过；双Python关联各1153passed/0skip，完整冻结5524passed/5个平台skip，WF-06为374/937、WF-08为179/380全通过，866文件摘要一致。严格退出1仅因Linux RELEASE-01，七项外部检查not_run。详细证据见[实施进度](implementation-progress.md#2026-10-27-外部-agent-已发布-artifactref-输出)；本地Native完整文本、其余上下文/配置及跨平台/真实试点继续开放。

## 2026-10-27 Native 完整文本产物输入

显式开启的本地 native_run 已消费完整 text/plain、text/markdown、application/json ArtifactRef 正文，文本与显式 Memory 共享 8192 bytes 预算。原 Run 私有 schema2/reader9、同 token 查回、当前权限复核、有界历史与 frozen 备份保持原执行链；正文不进入 DTO、事件或 repr。

新增 39 selectors / 115 cases；串行 Python 3.10.21/3.13.15 的 38 模块关联集合各 **1532 passed、2 subtests passed、0 failures/errors/skips**。 全仓 **5639 passed、0 failures/errors、5 个平台 skip、195 passing subtests**；JUnit 实际 5644 个 testcase，不重复累计 subtests。WF-06 **413 selectors / 1052 cases**、WF-08 **201 / 451** 全通过，41/42 journeys。严格退出 1、ci_status=failed，仅 RELEASE-01 缺少三项 Linux pidfd/proc 必需实例；另两项 Supervisor proc 平台 skip，七项外部检查均 not_run。

详细证据见[实施进度](implementation-progress.md#2026-10-27-native-完整文本产物输入)。所有 gate 默认关闭；summary、二进制/代码包输入、CLI/评审/测试/远程或 capability context、其余集成配置、Linux/Windows 与获准真实两机/模型/MCP/Muse试点继续开放。Muse export 仍 unsupported，整体平台与生产验收未完成。

## 发布证据 PNG 完整性

原 CI 核验入口已补齐截图结构、CRC、完整压缩数据与尾部检查；支持有效色型/位深、分片 IDAT 和 Adam7，流式读取和解压，保留原错误原因链。设计与完整代码见[PNG 证据计划](../../superpowers/plans/2026-10-27-release-png-evidence.md)，历史修复证据见[实施进度](implementation-progress.md#2026-10-27-发布证据-png-完整性)。后续 gz-cvm 独立快照的 Linux 完整门禁已通过，44 张实际截图逐一核验；该结果不改变生产验收范围。

## Windows 接收（2026-10-30）

[Windows 实施计划](../../superpowers/plans/2026-10-30-windows-artifact-receive.md)记录实际 home-win 的原生句柄/NTFS owner-ACL、reparse/硬链接拒绝、无覆盖发布、四窗口进程退出及原 journal/回执恢复：最终 60 passed、0 skip。原 NodeRuntime 经临时 SSH 回环转发调用 Hub HTTP，回执响应丢失后重启不再下载，Hub 原事务确认后才放行，该独立 canary 1 passed。相同 882 份源码的冻结 revision `9868cb0c9a51dfe33086bca3c9b15293efaf6404` 通过 Linux 原全仓 5754 passed/42 journeys 与原发布核验，50 个证据文件的本地/远端摘要相同。`artifact.export` 仍仅 POSIX，transfer gate 默认关闭；远程接收不是两产物 Node 源包交接或完整 WF-04 的证明。


## Windows 固定源导出（2026-10-30）

[导出计划](../../superpowers/plans/2026-10-30-windows-artifact-export.md#final-frozen-verification-2026-10-30)记录 NTFS 固定源句柄、精确时间戳/owner/DACL 摘要、写入/rename/可写映射拒绝及原 ID/journal/回执恢复；原协议、共享两槽和默认关闭 gate 保持。最终 home-win 原生合并 131 passed、原 NodeRuntime→原 Hub HTTP 2 passed、双 Python 关联各 295 passed；同源 Linux 原全仓 5785 passed/197 passing subtests/0 skip/42 journeys，远端及本地原 verifier 与 44 PNG 核验通过。真实两产物 Node 源包交接、完整 WF-04/05、真实 CLI/模型及生产和外部试点继续开放。


## 真实 Windows→Linux 源包交接（2026-10-30）

[两机计划](../../superpowers/plans/2026-10-30-two-host-artifact-handoff.md#actual-frozen-verification--2026-10-30)已通过实际 home-win producer → 原临时 Hub → gz-cvm receiver：控制端 1 passed，接收后独立 Linux sandbox 固定测试 2 passed，均零失败/skip。原 SourceExporter 只导出固定 Git 提交，三端 10,240 bytes 完全一致；原 export 丢片响应只 PUT 一次，原 receive 丢回执响应下载一次/同回执两次，原 input 服务核对后才 ready。890 文件冻结和每机前后摘要、原身份、完整报告/JUnit 与八份远端证据逐项核验。该显式试点和上一导出冻结的 5785-pass 全仓分别记录；完整 Workflow/WF-04/05、模型/CLI/生产和真实外部验收继续开放，gate 默认关闭。详见[实施进度](implementation-progress.md#2026-10-30-真实两机源包交接)。
