# Workflow 平台验收与恢复矩阵

> 2026-11-12精确快照修正：c6bbdc81终态7328passed/99failed/41skip；该传输快照混入1221个AppleDouble sidecar，runner另遗漏私有node/bwrap/rsync路径，不能作为产品回归或发布通过。新冻结dc6a42b8逐文件匹配原工作树1221真实文件且无sidecar，产品/测试与旧快照的真实文件一致，仅3份验收文档不同；恢复原私有工具目录后Linux实际浏览器/rsync/sandbox/音频预检38passed/0skip，原完整门禁正在同一冻结执行。Python3.10独立锁定依赖环境的媒体14模块425passed/0skip，关闭此前缺cryptography的关联测试缺口；不是完整CI或生产通过。原报告/审计/新进程与证据路径见[开发总账](../platform/development-completion.md#精确快照与验收运行环境修正2026-11-12)。生产未变更。

> 2026-11-12共享用户固定测试修复：先通过原Run复现RLIMIT_NPROC32在namespace前消耗整个宿主用户线程预算，导致已安装libseccomp被误报缺失、无JUnit；新增80宿主线程回归先失败，再在namespace内施加32上限。d168dc29冻结Linux65passed/0skip，macOS124passed/6平台skip，Windows适用子集151passed/1POSIXskip；较广Windows失败/collection错误均保留，不宣称完整平台通过。95b51985较早冻结macOS全仓7423passed/11Linuxskip、43/46journeys，原verifier仍拒绝journey_incomplete。修复冻结的完整Linux/browser正在运行，原包1140后端/70前端字节一致。见[修复计划](../superpowers/plans/2026-11-12-sandbox-process-budget.md)和[总账](../platform/development-completion.md#固定测试共享用户进程预算根因修复2026-11-12)。生产未变更。

> 2026-11-12全量诊断续验：临时urllib观测插件＋本机Chrome执行原完整测试树，7405 passed/11平台skip/204 subtests/零failure/error，退出0；原十项连接失败未复现，根因未证实。运行在收集后仍有产品/测试修改，故不是最终冻结发布门禁，亦不能与后续402项局部结果合并成完整通过数。最终收集7434实例比诊断7416多18项。完整证据目录`/Users/mango/.cache/agent-fleet/verification/native-media-boundaries-20261112-neh4d9jp/`；见[诊断总账](../platform/development-completion.md#全量连接失败诊断续验2026-11-12)。同源Linux/browser及生产验收继续开放。

> 2026-11-12图片边界续验：先复现不支持code引用的领域异常逃逸，派发层转换后新增原回归通过；重复引用/逻辑总预算、reader降级、历史损坏与明文/加密恢复已纳入WF-06。Python3.10/3.14关联13模块各402 passed/0skip；完整矩阵46 journeys/2495 selector entries/7434实例仅完成收集核对。报告与精确范围见[图片输入计划](../superpowers/plans/2026-11-12-native-binary-media-inputs.md)。这是本地dirty候选证据，不覆盖完整Linux/browser或生产验收；旧全量连接失败诊断仍独立跟进。

> 2026-11-12 Native WAV/MP3输入：本地Native `native_run`支持schema6/reader14及显式`audio_input`能力，attempt中严格验证并发送Chat Completions `input_audio`，不将bytes写入持久快照/checkpoint；ready恢复重读引用，pending不重发。Python3.14与后续隔离Python3.10的当前14模块关联测试各425 passed/0skip；journey矩阵形式收集7461实例、46 journeys、2508 selectors、0缺项。`--collect-only`使严格journey结果为0/46未执行，因此不是完整门禁通过。Python3.10初次缺`cryptography`的收集失败报告保留，更早402项证据仍只对应图片切片。全量source-frozen Linux/browser、真实模型与生产任务/下载/重启恢复仍未验收。详见[音频输入计划](../superpowers/plans/2026-11-12-native-audio-inputs.md)。

> 2026-11-12当前dirty候选全量回归：本机Chrome **7449 passed/12平台skip/204 subtests/0失败错误**，51张截图；严格journey 43/46，NODE-01、RELEASE-01、WF-01缺Linux-only实例，发布核验以dirty_checkout拒绝。独立Linux首跑因为测试主机/tmpfs被真实452MiB多分片测试夹具占满而ENOSPC，原JUnit留存但不作产品结论；同快照TMPDIR复跑后另发现快照元数据污染/私有工具路径遗漏，终态和新精确冻结见本页首条，不再将c6bbdc81称为精确工作树。详见[全量状态与报告目录](../platform/development-completion.md#当前候选全量复验2026-11-12)。

> 2026-11-12实际使用部署复核：重新核对远端main、公网manifest和容器RELEASE_ORIGIN均为301e259，healthz正常；容器仍缺jsonschema。功能候选和图片输入切片未发布。最新图片兼容/恢复/原worker→捕获HTTP传输及关联13模块383 passed/43.11秒，只证明dirty工作树本机子集；先前完整macOS7369 passed/10 failed/11平台skip仍未闭环，不能被下面更早7370零失败记录替代。完整Linux/browser、receiver/依赖、专用workspace/新鲜Node以及真实任务/下载/重启验收仍开放。见[当前开发总账](../platform/development-completion.md#实际使用部署判断复核2026-11-12)与[图片输入计划](../superpowers/plans/2026-11-12-native-binary-media-inputs.md)。本轮无生产变更。

> 2026-11-12现网前置只读检查：`hermes-gateway` 当前301e259，缺少`jsonschema`，Workflow/dispatch/remote/capability/artifact/MCP/Muse gate均未开启，登记文件不存在，两个已登记节点心跳约4天以上。候选receiver更新与依赖补齐、专用workspace/新鲜Node、真实任务/产物/重启验收仍未执行；没有生产变更。

> 2026-11-12完整回归与发布包复核：原6c99ffdd的macOS7369 passed/1边界failure/11 Linux专用skip/204subtests、43/46 journeys及46完整PNG；Linux7331 passed/1同类failure/49浏览器环境skip/204subtests、30/46 journeys。两端原源码clean，原核验拒绝journey_incomplete，原报告复制SHA核对。已修复直接Hub→tools依赖，47bcd3b5同12模块macOS252 passed/2subtests/0skip、Linux251 passed/1浏览器环境skip/2subtests；1134后端/69前端包字节核对、两端解包CLI/双集成正常启动/恢复挂载、原receiver组合验证通过。当前候选两端完整复验已终态：macOS7370 passed/11 Darwin专用skip，Linux7332 passed/49浏览器环境skip，均0测试失败；严格release verifier仍拒绝平台条件导致的journey_incomplete，未发布；这些结果和包证据不代替完整Linux/browser或实际账号/生产任务验收。证据与范围见[Muse后续计划](../superpowers/plans/2026-11-12-muse-startup-registration.md#integrated-release-packaging-verification--2026-11-12)和[开发总账](../platform/development-completion.md)。

> 2026-11-12 Muse正常装配：新11个WF-06 selector/33实例，完整清单46 journeys/2466 selectors，7381实例收集无缺项。冻结6c99ffdd的正常Hub/文件配置/原journal/协调器关联7模块，macOS启用Chrome为200 passed/0skip，Linux为199 passed/1浏览器环境skip；新33实例全部通过。配置/门禁/发布另191 passed、2 subtests passed。原受控任务一次发送，关闭新外部发送后的重启仍查回同token/binding并完成，acceptance pending；原两库完整性与复制摘要另验。不据此声称真实Muse或完整发布验收通过。见[部署入口](../platform/workflow-platform/muse-startup.md)与[实施证据](../superpowers/plans/2026-11-12-muse-startup-registration.md)。

> 2026-11-12 Fleet身份运维入口：新增7个WF-06必需selector/27实例，完整矩阵46 journeys/2455 selectors，7348实例收集无缺项。冻结08fc115a的身份CLI/原持久目录/正常Hub/HTTPS及关联7模块，macOS和Linux各270 passed/0skip；同版Linux配置组合探针另2 passed，证明三步预算403与正确预算下的创建/幂等/get/产物列表，未派发执行。配置/发布门禁另183 passed、2 subtests passed。见[部署用法](../platform/workflow-platform/integration-setup.md)和[证据计划](../superpowers/plans/2026-11-12-integration-identity-setup.md)；完整Linux/browser及生产/外部验收继续开放。

> 2026-11-12 MCP正常启动配置：新增8个WF-06必需selector/35实例；完整清单现46 journeys/2448 selectors，7321实例收集无缺项。冻结568a5642的真实环境SecretBroker/HTTPS对端/SQLite/重启调用/变更待审与撤销及原关联8模块，macOS和Linux各325 passed、2 subtests passed，Linux零skip且1201源码摘要前后不变。本机另5模块207 passed。此为正式入口专项验收，完整WF-06、Linux/browser和生产/外部验收继续开放。见[使用入口](../platform/workflow-platform/mcp-startup.md)与[实施证据](../superpowers/plans/2026-11-12-mcp-startup-registration.md)。

> 2026-11-12 WF-04/05登记与专项验收：逐项要求/断言映射见[文件与路由验收计划](../superpowers/plans/2026-11-12-file-routing-acceptance.md#requirement-audit)。新冻结bd2f0ea0（1196文件）的Linux专项567 passed/0skip，原插件2/2 scoped journeys、263 selectors全部参数通过；完整矩阵现46 journeys/2440 selectors，7286实例收集无缺项。实际双机/Windows试验使用产品源码完全相同的49a83f23：Linux关联707 passed、Windows原生133 passed、实际交接/两端自动生命周期/独立postcheck控制21 passed，各自无skip。二十份Hub数据库、两端journal与全部复制成员摘要独立核验。本结果登记WF-04/05已实现合同的自动/原生验收；完整同版Linux/browser、生产和其他外部验收仍开放，不能把专项2/2写作全仓46/46。

> 2026-11-12上线准备：公网页面/healthz均200，但manifest与GitHub main仍为301e259，本轮未提交发布。发布暂存修复冻结49a83f23的macOS关联235 passed，Linux28模块与11专用实例1027 passed/0skip；Windows原18模块736 passed/1 failed，剩余接收夹具私有目录修正后独立46 passed/0skip，macOS额外66 passed。原报告不合并、不回写；旧7267-pass不覆盖新产品改动。首批Linux Hub/单固定测试节点/单模型仍须完整同版Linux/browser、receiver预检和生产真实任务/恢复验收。计划：[暂存文件生命周期](../superpowers/plans/2026-11-12-workflow-publication-staging.md)，现状：[上线准备复核](../platform/development-completion.md#上线准备复核2026-11-12)。

> 当前部署判断（2026-11-11）：现网/GitHub main 为301e259，本轮未发布。较早d7d846a3本机完整测试7187通过/11 Linux专用skip/零失败错误，41/44 journeys，原核验仍拒绝journey_incomplete；同源Linux11专用实例另验通过。后续ca45f265协调器性能修复已在macOS/Linux各229项、Windows231项通过（另2 POSIX skips），tick P95从895降至518ms，仍未达500ms初值；旧完整报告不覆盖新改动。完整Linux/browser门禁、生产配置和上线验收继续开放；Linux浏览器大文件下载待批准。详见[当前部署判断与证据](../platform/development-completion.md#当前部署判断2026-11-11)。

> 2026-11-11 最新正式模型评审入口：新增仓库内置单次HTTP评审命令与Linux launcher，原Hub登记/Run worker/报告校验可直接使用；完整文本源码分片发送，不默默截断。`a2aed0eb`冻结1182文件，18模块macOS关联335 passed/6 Linux-only skips、Linux同集合341 passed/0skip，各2 subtests。原包1106后端/67前端源文件字节一致；44 journeys/2114 selectors/7148 cases仅收集核验。对端为受控HTTP，真实模型服务/生产配置、最终完整Linux/browser门禁与部署仍开放。见[正式评审入口证据](../superpowers/plans/2026-11-11-deployable-model-reviewer.md#verified-results--2026-11-11)及[配置方式](../platform/workflow-platform/startup-registration.md#仓库内置模型评审器linux)。

> 2026-11-11 最新固定测试部署入口：正式Linux launcher已可用于原Hub/Node配置；`2570449a`冻结1178文件，macOS关联230 passed/3 Linux-only skips、Linux同集合233 passed/0skip，各2 subtests。原包1102后端/67前端字节一致；44 journeys/2104 selectors/7107 cases仅完成收集核验。该版本完整Linux/browser发布门禁、生产模型评审配置与部署仍开放。见[入口与验证结果](../superpowers/plans/2026-11-11-linux-workflow-launcher.md#verified-results--2026-11-11)。

> 2026-11-11 最新初始化切片：`1d982fce` 冻结1175文件完成工作区/三会话CLI与平台迁移竞争根因修复，三端相同31项全部通过，原CLI额外40组/80进程全通过。本机完整7089 passed/6个Linux专用skip/204 subtests，42/44 journeys、2097 selectors、46完整PNG；Linux关联281 passed/1浏览器未配置skip，Linux进程79 passed/2subtests，Windows31 passed/0skip。原release verifier仍拒绝journey_incomplete，不能拼合不同平台报告代填完整门禁。详见[初始化与完整证据](../superpowers/plans/2026-11-11-workflow-bootstrap.md#verified-results--2026-11-11)。生产配置、完整Linux/browser与部署仍开放。

> 最新跨机试点（2026-11-11）：同一 `coding-delivery@1` Workflow 已贯穿 macOS 原 Runner 真实 Codex 开发→独立同模型会话评审→Hub 自动选点→Linux Node 签名接收与原 bwrap 固定测试31项→5产物HTTP/Chrome下载与刷新。两条 Node 命令成功、三条预约释放、同一源码 subject，停在等待人工接受。冻结 `0b3fd2fc` 1162文件两端一致；原汇总脚本错误地预期一条预约，失败报告保留，由独立审计核验原流程及产物。详见[跨机试点](../superpowers/plans/2026-11-11-real-cross-node-workflow.md#verified-results--2026-11-11)。此证据覆盖 WF-04/05 的真实主链，完整故障/竞争矩阵和 Linux/浏览器发布门禁仍开放。

> 最新真实试点（2026-11-11）：本目标树私有macOS coding-delivery@1经原Cookie HTTP→Coordinator→Runner，完成真实Codex0.160.0开发与独立模型会话评审（所选newapi/gpt-6-luna/magpie），原sandbox固定测试31项、5产物HTTP及页面下载hash、退出重登恢复、真实Chrome9222桌面与手机刷新均通过。原Workflow停在awaiting_acceptance/revision22，人工接受未执行；同模型独立会话不声称跨模型评审。1159源码执行前后不变；旧失败保留，后续仅补只读UI验收和文档。见[试点计划与逐项证据](../superpowers/plans/2026-11-11-real-workflow-cli-pilot.md)。本试点不代填完整Linux门禁、生产隔离、MODEL-LIVE或其他外部项。

> 2026-11-11摘要完成与根因修复：4a3e15f1冻结本机全仓7043 passed/5个Linux专用skip/204 subtests、43/44 journeys、2071 selectors、46完整PNG；同源Linux关联832 passed。后续b44fe1bc修复Windows备份fsync，三端相同摘要/明文加密备份集合各96 passed/0skip。当前矩阵44条/2073 selectors、7050实例可解析；原release verifier仍拒绝journey_incomplete。源码与原失败、报告及范围见[摘要计划](../superpowers/plans/2026-11-11-versioned-summary-context.md)，完整Linux/browser、真实CLI/model和发布仍开放。

> 2026-11-11摘要管理界面：真实 DTO parser 接受发布、分页、head、历史版本与启停响应；管理员 Chrome 流程覆盖发布两版、读取历史、停用和普通用户不可见。首次浏览器复验发现 head wrapper 丢失 `{summary}` 包装及发布 DTO parser 拒绝真实响应，均已修复。当前服务/消费/API/浏览器关联 **80 passed**（含真实浏览器 1 项）；Ruff、JS 语法与 diff 检查通过。两个新 selector 已追加 WF-06；自动摘要生成、完整 Linux/browser 发布门禁和外部真实服务仍开放。


> 2026-11-11最新原生能力切片：`cd7019aa`/1149源码冻结的home-win与SG Linux矩阵20 passed/0skip，其中8项自动协调器/原Run worker和12项独立held/isolated postcheck；本机原矩阵10 passed、关联417 passed分别记录。独立20份Hub数据库及两端journal/file核验、106文件/910132 bytes复制校验通过；三端源码不变、checkout clean。真实商业模型、完整WF-04/05及完整Linux/browser发布门禁仍开放。见[原生能力证据](../superpowers/plans/2026-11-11-native-capability-integration.md#verified-results--2026-11-11)。

> 2026-11-12修复后冻结`5fe6bbb2`完整macOS/browser **7267 passed/零失败错误/11 Linux专用skip/204 subtests**，41/44 journeys、2175 selectors、46完整PNG；原核验器仍拒绝journey_incomplete。相同冻结Linux关联25模块及11必需实例 **963 passed/0skip**。Windows原16模块709 passed/3新增POSIX Hub夹具failed，显式平台声明后的Native模块38 passed/3 skips另记；不修改原失败报告。129原Run×16引用及页边界损坏/明文加密恢复在修正冻结重新通过，后端1115/前端69文件hash匹配。产品代码与冻结一致，仅后补测试平台声明及文档不同。完整Linux/browser、生产与全局其他TODO继续开放；见[开发总账](../platform/development-completion.md#最新修复后验收2026-11-12)与[代码输入证据](../superpowers/plans/2026-11-11-native-code-package-inputs.md#corrected-frozen-results--2026-11-12)。

> 2026-11-12完整复验发现：首个代码输入冻结`af633263`的本机/browser全仓7127 passed/136 failed/11 Linux-only skips。133项为新增Native opt-in误伤已有评审/固定测试/远程测试；已限定作用域并增加三类原派发/lookup及Native关闭前拒绝回归。两项旧功能清单断言已修，一项usage原浏览器导航单独重跑通过；修复后完整冻结继续复验，不宣称发布通过。相同首冻结Linux关联708 passed，Windows706 passed/2执行位夹具failed；修正夹具后源码模块macOS/Windows各39 passed。129原派发×16引用、页边界损坏及明文/加密恢复探针另验通过，不并入全仓计数。原报告与范围见[代码输入计划](../superpowers/plans/2026-11-11-native-code-package-inputs.md#full-gate-findings-and-extended-verification--2026-11-12)。

> 2026-11-12 Native源码包输入：代码输入模块37 passed；包括真实源码包导出/manifest核验、六项manifest字段绑定、UTF-8/路径与特殊归档条目拒绝、异常清理、篡改包尾、17引用与多包10 MiB预算、跨包4096文件上限、10 MiB+1 byte展开声明拒绝、混合文本+源码provider链、已提交Run查回不重读包、`response`/`receipt`/`ready` checkpoint恢复、pending unknown、明文/加密备份恢复及reader12 gate-off/startup拒绝。代码输入相关Native/外部文本/Memory/摘要/输入位置/边界与release-evidence十六模块 **708 passed**。完整历史损坏/闭包矩阵及当前完整Linux/browser发布门禁仍开放；见[实施计划](../superpowers/plans/2026-11-11-native-code-package-inputs.md)。

> 2026-11-11最新切片：冻结`51f71614`修复固定测试的当前Python路径被拒绝，完成真实SG Linux Node passed/failed/lost/unknown四场景4 passed；丢回执由原outbox补传一次，之后重放零执行。相同冻结Linux十模块172 passed、Windows运行时/控制/期限/进程子集102 passed，均无失败或skip；1146源码三端不变、checkout clean。本机关联157 passed及独立36 passed覆盖原142用例和37项失败。详见[固定测试集成与根因证据](../superpowers/plans/2026-11-11-native-fixed-test-integration.md#verified-results--2026-11-11)。当前44条/2023 selectors，完整Linux/browser门禁、真实CLI/model完整交付与其他外部试点仍开放。

> 2026-11-11较早集成证据：Node控制冻结`0d11d8b4`的原生Linux关键17模块288 passed/0skip，Windows CI范围及控制等289 passed/2项POSIX skip；后续只恢复验收夹具的`a9da2c29`冻结完成Windows原生NTFS收发131 passed/0skip、实际home-win→Hub→SG Linux窄交接控制1 passed和独立sandbox固定测试2 passed，1142源码三端不变、checkout clean。最新完整全仓仍是较早检查点冻结`06e87586`的6921 passed/5平台skip/204 subtests、43/44 journeys、1995 selectors，不能代填当前完整门禁。完整Linux/browser、全WF-04/05与真实CLI/model及其他外部试点仍开放。见[开发总账](../platform/development-completion.md)与[原生交接证据](../superpowers/plans/2026-11-11-native-handoff-integration.md#verified-results--2026-11-11)。

> 历史2026-11-10目标树：远程取消冻结 `69028deb2cf7b2908e3a4d415aba95e819dd1fa6` 全仓6903 passed/5平台skip/204 subtests，43/44 journeys、1984 selectors、46完整PNG；1124源码摘要不变、checkout clean。原发布核验仍拒绝journey_incomplete；不覆盖后续检查点、远程等待与索引实现。

> 2026-11-10当前目标树：`codex/functional-root-fixes`的本机全仓6887 passed/5平台skip/204 subtests，43/44 journeys、1975 selectors及46完整PNG；其后运行依赖声明修复另验182 passed，当前矩阵1976 selectors。当前范围与容量样本见[开发总账](../platform/development-completion.md)及本页§11。下文Linux/Windows/真实两机记录属于原冻结源码；原gz-cvm已于11/10到期下线，不推断当前树通过对应验收。

日期：2026-10-30。状态：F1 与 F2.1–F2.5、F3 本机受控交付链、F4.1 Hub V2 分片发布、F4.2 本机 POSIX Node 收发/统一容量与 F4.3 目录/显式 admission、Node 报告/本地执行前复核、Hub 固定绑定派发及协调器自动选点/输入目标固定子集已落盘；WF-01/02/03/06/07/08、FILES-01 与 NODE-01 按已实现范围登记 required selector。transfer/capability/integration gate 默认关闭；能力 postcheck 的本机 workspace digest slice 和 F5 本机集成边界已登记，Linux 全仓、Windows 产物收发及两台真实机器源包交接/独立固定验证已有各自证据；完整 WF-04/05、获准真实模型/商业 CLI、生产隔离与外部试点未完成；详细证据见[实施进度](../platform/workflow-platform/implementation-progress.md)。

上位合同：[执行与验收](../superpowers/specs/2026-10-20-workflow-execution-contract.md)、[文件与工具接入](../superpowers/specs/2026-10-20-artifact-capability-integration-contract.md)；实施顺序见[路线图](../superpowers/plans/2026-10-20-workflow-platform-roadmap.md)。已有[测试入口](README.md)和[发布验收](release-acceptance.md)继续生效。

## 1. 证据状态与边界

证据用 passed、failed、not_run、blocked 四种状态。未实现/未运行写 not_run；依赖缺失且已尝试查明写 blocked 和原因。skip、xfail、mock 成功、HTTP 202、进程健康、功能开关为关都不能填写 passed。

每次记录至少包含源码 revision/dirty 状态、矩阵摘要、配置/gate、模板与 validator revision、平台/CLI/模型身份、输入 subject_digest、期望、观察结果及脱敏证据路径。费用缺失保留 unknown，不填写零。

| 证据层 | 必须真实经过 | 仍不能证明 |
|---|---|---|
| 纯领域 | 实际解析/迁移/摘要/证据函数 | HTTP 鉴权、事务、外部行为 |
| Hub 集成 | Flask 认证入口、真实 SQLite、持久 intent、coordinator、原 Run/Task/Command 路径 | 外部模型质量、生产隔离 |
| Node 贯穿 | HTTP poll/receipt、真实签名、独立 journal、实际文件系统 | 未参与测试的操作系统和真实远端链路 |
| 浏览器贯穿 | 真浏览器→账号 Cookie→提交→worker→文件/证据→刷新恢复 | 真实外部模型/邮件/云机 |
| 受限外部试点 | 指定真实 CLI/模型、获准节点与 sandbox、实际网络/文件 | 其他服务、其他平台和自动发布 |

模型 loopback 对端独立检查公开协议、工具名和调用关联，不导入被测序列化器生成预期。CLI 测试对端保存收到的任务标识、源码 hash 和调用次数；不替换 coordinator 或直接往库里写“成功”。外部模型能力评估与这些协议测试分别记录。

手工真实试点入口为 `tests/manual/workflow_live_pilot.py --evidence-root <新的私有目录> --run-live`；只使用已安装、已配置的真实CLI，独立临时Hub/账号/源码。`tests/manual/workflow_live_ui.py --evidence-root <原试点目录> --ui-output-dir <新的私有UI目录> --playwright-module <现有模块目录>` 只读恢复这条原Workflow，连接已有Chrome9222，不启动worker或再次调用模型、不点击接受。两者不属于默认pytest收集，也不将其计数加入journeys。完整目录含私有凭据和provider配置，不能整目录公开；仅分享检查过的脱敏结果和产物。

## 2. 新 journey 的登记方式

以下 WF 编号是稳定 ID。WF-02 覆盖 F1 与 operator HTTP；WF-03 覆盖派发、协调、取消/失效/核对、源码双库恢复及评审/测试 Run 的租约/进程恢复；WF-07 覆盖原用量账本与预算；WF-08 覆盖存储恢复、gate 和浏览器账号隔离。WF-01 包含普通 Runner/Supervisor 的实际 fixture CLI 子进程、Flask source/result 路由、原协调器的动态派发、独立命令评审、严格 loopback 模型 HTTP 对端、真实 pytest/JUnit、Cookie 会话、文件下载和 HTTP 人工接受。HTTP test_client、fixture launcher、DirectoryBackend 和 loopback 模型对端不证明真实外部模型、商业 CLI、跨节点或生产隔离。真实隔离探针现按平台运行 macOS sandbox-exec 或 Linux bubblewrap namespace/seccomp；覆盖独立评审、固定测试、无隔离失败对照及完整HTTP链，三个共享selectors全部参数登记WF-01。Linux直接检查namespace分离、capabilities为零、NoNewPrivs=1/Seccomp=2。仅证明对应配置，不替代生产隔离或其他平台。WF-06 按本机集成边界登记；WF-04/05于2026-11-12完成逐项映射并登记116/147个自动selector，实际双机与Windows特定探针独立opt-in，不进入默认CI的跨机跳过。每个 selector 的全部参数实例、setup/call/teardown 都必须通过。

| ID | 用户可见能力 | 首次阶段 | 拟议测试文件 |
|---|---|---|---|
| WF-01 | 同一版本的开发、独立评审、测试与人工接受 | F3 | `tests/test_workflow_coding_delivery.py`、`test_workflow_source_http.py`、`test_workflow_review_execution.py`、`test_workflow_test_execution.py`、`test_workflow_e2e.py`；获准外部 E2E 待补 |
| WF-02 | 严格合同、归属、审批和证据隔离 | F1–F2 | `tests/test_workflow_contract.py`、`test_workflow_http.py`、`test_workflow_evidence.py` |
| WF-03 | 重启恢复、取消、迟到回执和未知结果 | F2 | `tests/test_workflow_dispatch.py`、`test_workflow_coordinator.py`、`test_workflow_control.py` |
| WF-04 | 文件完整传到另一节点，失败可恢复 | F4 | `tests/test_artifact_transfer.py`、`test_artifact_transfer_recovery.py`、`test_artifact_receive.py`、`test_workflow_two_nodes.py` |
| WF-05 | 按获准能力选点且不超售资源 | F4 | `tests/test_capability_router.py`、`test_capability_reservations.py` |
| WF-06 | MCP/外部 Agent 本机边界与权限收敛 | F5 | `tests/test_mcp_connector.py`、`test_mcp_catalog_persistence.py`、`test_mcp_run_pinning.py`、`test_mcp_http.py`、`test_external_agent_adapter.py`、`test_external_agent_workflow.py`、`test_external_agent_persistence.py`、`test_fleet_mcp_boundary.py`、`test_integration_identity.py`、`test_mcp_catalog_admin.py`、`test_mcp_catalog_admin_browser.py`、`test_integration_identity_admin.py`、`test_integration_identity_admin_browser.py`、`test_frontend_workflow_summary_contracts.py`、`test_workflow_summary_admin_browser.py`；外部服务另验 |
| WF-07 | 有限预算、代数与准确用量归属 | F2–F3 | `tests/test_workflow_budget.py`、`test_workflow_coding_delivery.py` |
| WF-08 | 页面恢复、备份回退与状态可信 | F2–F4 | `tests/test_workflow_browser.py`、`test_workflow_repository.py`、`test_workflow_gates.py` 及既有 backup 测试 |

## 3. WF-01：完整代码交付

2026-11-11补齐正常启动配置：[Hub登记](../platform/workflow-platform/startup-registration.md)及[常驻Node配置](../platform/workflow-platform/node-startup.md)使用原执行链。Hub文件加载、显式空配置优先级、禁派发及受控Cookie→Runner→评审→sandbox测试链已回归；`feab24dd`同源macOS十二模块240 passed/1 Linux-only skip、SG Linux241 passed/0skip，启动专用Linux13项通过。Node配置相关七selector在NODE-01，Hub五selector在WF-01；最终44journeys/2085selectors/7070cases全部可收集，不表示全仓执行通过。完整原失败、哈希与范围见[启动计划](../superpowers/plans/2026-11-11-node-workflow-registration.md)。Linux新固定测试使用fixture launcher，仅证明配置/原能力预检/原recipe和持久重放边界；真实隔离以独立实际试点为准，未自动填写人工接受。

1. 用真实 Git 临时仓库建立 base_commit，冻结三个步骤的合同与模板 revision。通过账号 API 创建 Workflow，同 client_token 重复提交返回同一个实例。
2. 开发步骤经过既有 CLI Task 路径产生可识别源码版本和源码包。验证 `head_commit/tree_id`、包 hash、owner/workspace 与固定输出绑定；不只检查 summary 文本。
3. 独立评审在不同执行上下文中读取同一版本的只读源码；保存实际 adapter/model 身份、评审结构化报告和 subject_digest。测试尝试写入评审源目录必须失败。
4. 测试步骤在获准隔离环境中运行固定 recipe，报告含真实退出码、JUnit、配方 revision、环境和源码 subject。测试失败时流程显示 validation_failed，人工接受不能把它改成 delivered。
5. 所有证据齐备后，execution=succeeded、validation=passed、acceptance=pending，页面仍显示待验收。指定人工主体用当前 delivery_digest 接受后才显示 delivered。
6. 修改开发输出、显式创建新 generation；旧评审/测试保留但不放行新版本。用旧 digest 接受返回 409。存在旧活跃/unknown 执行时，冲突工作区不得启动新一代。

重做 succeeded/failed 的流程时，仅显式 retry/invalidate 能创建新一代并恢复 running，普通 poll 不重开终态；旧 generation 保持原结果。cancelled 要新建实例，unknown 先核清，不能通过返工按钮消除未知风险。

评审拒绝的路径应停在评审证据，不继续测试。缺任意必需产物、缺校验、报告针对旧版本、仅得到模型 final、仅外部确认接收均不得交付。未要求验证的模板显示“未要求校验”，不写 passed。

F3 仅承诺一个已验证执行环境内的独立目录/执行上下文。F4 再把评审或测试放到第二个节点，复用同一交付主体并增加真正的文件接收证据。

## 4. WF-02：合同、身份与批准

- HTTP 原文覆盖重复 JSON 键、非法 Unicode、NaN/Infinity、嵌套/字节超限、未知字段、bool-as-int、负值、数字字符串。请求失败前后 DB 记录数与预算一致，不能部分落库。
- 验证 8 步线性上限、每步 16 项引用上限、循环/分叉/悬空前驱拒绝；模板在登记时展开固定合同，不在运行中跟随“最新”目录。
- 两个真实账号分别创建相同外观的流程、文件和工作区；交叉 get/list/events/accept/retry/download 都拒绝，集合不能侧漏 ID、总数和等待原因。跨 owner 与不存在统一 404。
- Node、runner、ingest 和 MCP 集成凭据不能审批、最终接受、修改模板或创建任意 Workflow。正文中的 owner_id、shell、路径、lease、策略覆盖字段拒绝。
- 输入、目标、参数 hash 或策略 revision 改变使旧 grant 失效；撤销的权限在下一次 admission 生效。工具输出或聊天中的“用户同意”不能变成 grant。
- 证据必须匹配 subject 与 validator revision；相同 evidence key 异内容 409；旧 evidence 和 Agent 自报测试通过不充当固定测试器的权威证据。

## 5. WF-03：恢复、取消与 fencing

使用真实临时 SQLite、显式时钟和可中断执行器。至少一个测试用两个 OS 进程共享同一数据库，不能只用两个 Python 对象模拟跨进程锁。

| 注入点 | 预期恢复结果 | 必须观察的证据 |
|---|---|---|
| admission 事务提交前退出 | 无 intent、无子执行、无孤立预算/槽位 | 同一事务回滚，调用次数 0 |
| intent 提交后、调用前退出 | 恢复同 generation/token 并派发一次 | token 一致、底层一个逻辑任务 |
| 子 Run/Task 已创建、binding 尚未落库 | create_or_lookup 查回原执行 | 子任务 ID 不变，无第二份逻辑执行 |
| 外部可能接收但查询不可用 | unknown，禁止盲重放 | 保留原 intent/请求标识和等待截止 |
| 协调器领取后过期，第二进程回收 | 新 epoch 生效、旧写回 CAS 失败 | generation/token 不随 epoch 改变 |
| Node 收到命令后断网/重启 | 使用原 command ID/journal，回传原回执 | journal 内一次执行事实 |
| 旧一代完成回执晚于新一代 | 留审计、不改变当前状态/证据 | generation、binding、目标匹配检查 |
| 取消到达未派发步骤 | 不再 admission，可写 cancelled | 该步骤无子执行 |
| 已派发且收到停止确证 | 保留停止证据后 cancelled | 原任务/进程的实际停止事实 |
| 不支持取消或无法确认停止 | 截止后 unknown，冲突槽位不释放为可用 | UI 显示未确认，重复取消幂等 |
| 取消与完成竞态 | 保留真实 succeeded/failed 结果 | 不把已完成执行重写为“已停止” |
| pause/resume/重复 tick | 只阻止后续 admission；不重启已完成步骤 | 原 binding、调用次数与事件游标稳定 |

底层 Run/Command 已终结 unknown 时保持原记录。Workflow reconciliation 追加证据解决自身投影；普通 poll 不能修改历史执行事实。

## 6. WF-04：真实文件交接

F4.1 的上传、发布恢复、配额与备份兼容已实现；F4.2 本机收发按文件、Node 与恢复边界登记 FILES-01/NODE-01/WF-08。原 F4.1 的实际 256 MiB/256 片、三窗口真实退出、跨进程最后额度争抢和明文/加密恢复证据保留；接收涵盖真实签名/HTTP/独立账本、四个真实退出窗口、12 MiB 流式 hash、原凭据逐块及回执事务复查、同回执重传与 Hub ready 门禁。新增固定导出、独立上传账本、四个真实退出窗口、三类丢响应、目录/文件变化和损坏账本失败关闭；Hub 与 Node 均合并收发容量，含跨进程最后槽争抢和进程死亡释放。两个 NodeRuntime 经真实 HTTP 完成 Node A 导出原 output binding → Node B 接收 → 独立 tree/blob/固定 pytest。相关回归及浏览器另列[实施进度](../platform/workflow-platform/implementation-progress.md)。2026-11-12已按以下要求登记WF-04的116个必需自动selector，并通过当前产品同源的Windows原生和实际两机试点；专项门禁与完整发布门禁分别记录。下方日期记录保留各自历史范围。

使用实际 bytes 与文件系统，不能仅比较两个函数返回的 manifest。

- 上传边界：旧 10 MiB 接口保持原行为；新协议默认 64 MiB、硬上限 256 MiB；零字节、整块/末块、最大 part 数、非法大小、超量并发和过期 reservation 均覆盖。
- 分片边界：乱序合法块、重复同块、同 offset 异内容、重叠、缺块、超总长、hash 篡改、非法 Content-Length、连接中断和慢速截止。失败不能发布半文件。
- 发布故障：在 rename 前、rename 后/DB ready 前、DB ready 后/响应前中断真实进程。恢复使用同一个 artifact_id，重复 commit 查回同文件，缺文件/错 hash 转 quarantined。
- 配额边界：两个并发 reservation 不越 workspace 配额；失败状态的 staging 实际占盘仍纳入统计。清理只验证 dry-run 候选与引用，不执行真实删除。
- Node 接收：两个不同 node_id、不同机器凭据、独立工作目录和 journal；通过真实 HTTP 传递源码包/测试产物。接收端自己重算大小与 hash，收到 receipt 前下游不得 ready。
- 接收安全：拒绝跨节点 grant、其他 owner 文件、任意 URL/redirect、绝对路径、`..`、symlink、junction/reparse、覆盖既有目标和归档解压逃逸。测试失败后原文件内容不变。
- 接收重启：stage 中断可恢复；已完成接收查回同一 receipt；下载文件不能自动触发安装、运行或 HTML 执行。
- 导出恢复：仅登记的 workspace/output_key 可选择文件；固定 fd/完整 hash，改变内容或路径停止发布；reservation/part/commit/回执丢响应及四个进程退出窗口均保留原 ID；非法账本/截止、未知 schema 和尝试超限停止自动重放。伪造导出成功及认证后凭据轮换在回执事务拒绝。
- 统一容量：上传 active/unknown 与真实下载流合计每节点 2/Hub 4；两类独立进程争抢最后槽只能一个获准；持流进程退出后内核释放下载槽；NodeRuntime 本地收发使用同一两槽池。

Linux/macOS 的 fd/path 证据不能证明 Windows 句柄边界。Windows reparse/共享锁行为必须在 Windows 跑；缺证据时该 Node 不声明相应 receive 能力。

F4 外部试点至少在两台获准真实机器上完成一次 Hub 转运、目标接收与固定验证。loopback 两节点是协议证据，不代填该试点。

2026-10-30 [实际两机试点](../superpowers/plans/2026-10-30-two-host-artifact-handoff.md#actual-frozen-verification--2026-10-30)的冻结 revision `8f63ddd660c07ddf9d55123132b2b26d2b4f2296` 已在 home-win→原 Hub→gz-cvm 完成源包交接：控制用例 1 passed，后续独立 Linux sandbox 固定测试 2 passed，均无 failure/error/skip。原 ID/绑定、一次 PUT/一次下载/两次同接收回执、wrong-node 404、waiting_node→原核对→ready 与完整源码 bytes/tree/blob/report/JUnit 均已核验。它满足两台真实机器的窄文件交接试点，不等同完整 Workflow/模型/CLI/生产能力，也不更新 NODE-LIVE 或其他外部检查；显式夹具不加入普通 CI 的 required skip。上一 5785-pass 导出全仓及本次 890 文件试点分别保留，不相加或冒用源码版本。

## 7. WF-05：节点选择与资源

固定输入下，按 owner/授权、驻留/平台/能力版本、freshness、策略、容量过滤，再按 priority、占用、node_id 排序；顺序与原因码逐项断言。

覆盖无心跳、120 秒边界、未来/错误时间、能力 revision 变化、软件版本不符、Node 自报 trusted/readOnly、磁盘不足、仅旧布尔能力节点及所有候选耗尽。新选点阈值不能改动旧 browser 的 300 秒合同。

两个 OS 进程同时申请最后一个槽位，只有一个 admission 成功。持久 reservation 与 intent 同事务；单纯协调器租约过期不代表旧执行停止。已绑定目标不能因心跳变化在同 generation 自动换机。

旧固定 target 与动态能力路由统一计入该物理目标的 held/isolated reservation，覆盖同 owner、跨 owner 和其他目标；released 才能作为空余量。固定目标在已有一个 isolated、容量为 2 时，两个真实进程争抢余下一槽只能一个成功，拒绝方没有 intent/预算/槽位或 revision/event 的部分写入。即使底层 Run 已 failed，仍有 isolated reservation 的步骤在 retry/invalidate 时必须返回 unresolved_execution，保留原 generation、事件和操作回执数；这些已实现回归登记 WF-02/03/08 与 NODE-01，不代表跨平台能力 postcheck 或完整 WF-05 已闭合。

F4.3 本机已实现能力定义/schema 固定、受鉴权报告、纯选点及显式原子 admission。新增 `test_capability_router.py`、`test_capability_reservations.py`、`test_capability_http.py` 覆盖上述过滤、两个真实进程、原 intent/预算/槽位/binding 一致性、失败回滚、原凭据撤销、重启后新报告、reader 与明文/加密 frozen 恢复。已实现子集登记 NODE-01 和 WF-08；完整 WF-05 保持开放；自动路由须使用后续协调器子集，不能仅凭显式 port 测试推断。具体 wire/版本/资源边界见[产物契约 §6.2](../superpowers/specs/2026-10-20-artifact-capability-integration-contract.md#62-f43-目录报告与显式-admission-子集)。

Node 子集增加 `test_node_capabilities.py` 与实际受鉴权 HTTP：默认无配置保持旧请求/manifest；只报告实际装配动作，未配置 sandbox launcher 不报告 exec；当前 Python/OS/磁盘事实，未知内存为零；15 秒包含采样及网络，采样后停止/超时不得新发请求；凭据不跟随五类重定向，超大/错误响应拒绝。固定能力上下文由原签名覆盖，执行前软件/本地策略/定义/adapter/动作/retry_class/资源变化拒绝且无文件写入；完成与 unknown 重放均不再执行。实际 HTTP 用例现已使用原 Run 的 Hub 事务内查回与签名准备，Node 软件变化导致报告冲突并拒绝原 binding；后续协调器自动选点和输入目标固定子集见下文。见[产物契约 §6.3](../superpowers/specs/2026-10-20-artifact-capability-integration-contract.md#63-node-报告发送与本地执行前复核子集)，默认 gate 继续关闭。

后续协调器子集由 `test_capability_coordinator.py` 覆盖：管理员动态路由、原前驱解析/冻结/同库选点、有 alias 的实际 HTTP 输入签收后才创建原 binding/Run/Command、同代目标不漂移、重启等待新报告、注册撤销后仍查原事实、pause/cancel/drain/dispatch-off、写入失败回滚及两个真实进程的最后容量竞争。能力 postcheck 另由 `test_capability_dispatch.py` 和 Node capability 回归覆盖：只有 descriptor-backed unknown workspace write 可派生摘要命令，helper 必须预先出现在 contract/binding/Hub action/Node local registration，retry 精确为 `read_only`；held/isolated reservation、Workflow/Run/Command unknown 事实保持不变，匹配与失配均不释放容量。详见[产物契约 §6.4](../superpowers/specs/2026-10-20-artifact-capability-integration-contract.md#64-hub-固定能力-run-与-command-派发子集)。`test_node_poll_credentials.py` 另覆盖普通/能力命令在认证后凭据轮换/禁用、节点禁用、错误指纹/作用域、SQL 回滚和原因链；旧请求不能领取命令或修改旧 lease。新增子集登记 NODE-01/WF-08；完整 WF-05、跨平台和外部试点继续开放。

## 8. WF-06：MCP 与外部 Agent

本机实现登记413个required selectors，覆盖固定只读MCP/持久目录和管理员审批撤销、Run授权、Fleet身份、外部Agent原任务派发/文本与Memory输入、本地Native完整文本/Memory，以及已发布ArtifactRef输出。独立TLS对端验证JSON/SSE、CR/LF/UTF-8、完整有界响应和单次调用生命周期；真实SQLite、受控Muse journal、原协调器、进程退出/竞争与frozen备份覆盖持久边界。模型、SecretBroker和export-capable外部adapter为注入fixture，不宣称真实服务。完整证据与历史失败见[实施进度](../platform/workflow-platform/implementation-progress.md)。

全仓 **5639 passed、0 failures/errors、5 个平台 skip、195 passing subtests**；JUnit 实际 5644 个 testcase，不重复累计 subtests。WF-06 **413 selectors / 1052 cases**、WF-08 **201 / 451** 全通过，41/42 journeys。严格退出 1、ci_status=failed，仅 RELEASE-01 缺少三项 Linux pidfd/proc 必需实例；另两项 Supervisor proc 平台 skip，七项外部检查均 not_run。 新增 39 selectors / 115 cases；串行 Python 3.10.21/3.13.15 的 38 模块关联集合各 **1532 passed、2 subtests passed、0 failures/errors/skips**。 871文件摘要运行前后一致，矩阵与报告匹配。完整证据与历史失败见实施进度。

Fleet身份与MCP目录保持原持久授权、可信登记和Cookie管理员管理边界；其余集成配置UI与真实MCP/Muse试点未完成。外部Agent只在登记target/adapter并开启gate后接入原协调器，构造不启动外部I/O或worker。完整有界UTF-8文本和显式owner/revision Memory输入已实现，二进制/代码包、summary及Node capability_requirements仍拒绝；显式export-capable adapter可只读导出已发布artifact/structured/code_revision引用，经完整核验后进入原输出链。`agent_reported`来源保留，字节/schema完整性不生成validator或acceptance；Muse取消/导出仍unsupported。上述本机范围不作为完整F5或生产就绪证明。

**Native 完整输入：** 可信目标须显式启用文本模式，仅接受本地固定 provider；文本和显式 Memory 共用 8 KiB，完整快照/系统消息≤32 KiB、Run 配置≤64 KiB，超限确定拒绝。当前权限复核不替换已保存正文；reader9、原引用/Run 缺失、标记/配置/当前授权不一致均在 provider 前失败。实际进程退出、并发与 frozen 恢复使用原生命周期，文本不授予工具或接受权限。

**MCP 输入/目录：** 独立协议对端提供有效/错误版本协商、无权限工具、工具 schema 变更、恶意说明、假 readOnlyHint、超 100 个工具、超 32 KiB 单 schema/256 KiB 总 schema、递归/外部 `$ref`。固定 revision 的运行不能偷偷调用新 schema。

**持久目录：** `test_mcp_catalog_persistence.py` 的 34 selectors / 65 cases 覆盖无 rediscovery 的重启、完整 discovery 身份（描述/server/capabilities/annotations/schema）、返回对象隔离、登记冲突/全局 public-name 冲突与 128 条历史上限。真实 OS 进程验证审批/撤销和登记竞争；过期刷新 CAS 不覆盖另一进程的新状态，旧 Run 在模型前拒绝、声明后调用再次检查。已开始调用的单次结果保留，后续调用拒绝。独立 HTTPS 对端经过默认 bootstrap、原 Run、worker 与 ToolBroker，重启不重新发现。平台 reader 4 独立于 Workflow reader，组合启用可用；gate-off、未来/残缺 schema、损坏/非规范快照、非正 rowid、末页和版本耗尽失败关闭。启动历史扫描用 WAL 读事务、每批 8 条；明文/加密恢复在有/无 Workflow 下都禁用旧 connector，不能启动重登记激活。详见[§7.8](../superpowers/specs/2026-10-20-artifact-capability-integration-contract.md#78-mcp-目录持久化与跨进程审批)。

**目录管理界面：** 新增 29 selectors / 74 cases（含 7 个真实 Chrome 参数场景）：有效 Cookie 管理员、普通账户/机器/Bearer/伪造 CF 头隔离、精确 Origin/2 KiB JSON、8 行 keyset/完整历史、私有配置过滤及 UTC；原 ID（含 `.`/`..`）通过唯一 query selector 管理。epoch/revision 的同事务 CAS、禁用记录保持与原 Run 不重放；结构损坏返回 503。页面验证恶意定义只作文本、完整审阅/确认、390px/键盘/Escape/焦点、响应丢失和冲突只读回、悬挂读取取消/页面恢复、账户切换与 gate-off/临时目录隐藏。浏览器对端是本机 Flask/SQLite，MCP transport/SecretBroker 为 fixture，管理动作不访问外部。见[§7.9](../superpowers/specs/2026-10-20-artifact-capability-integration-contract.md#79-mcp-目录管理界面与管理员-http-边界)。

**持久 Run 授权：** 原 config_snapshot 仅保存有界 public name、revision 和定义摘要；没有 pin 的旧 Run、空目录、gate-off、remote/acceptance 不从后续目录取得权限。排队后批准新 revision、重启后同 revision 异定义、endpoint/作用域/上限/input/output schema/描述/server 版本变化均失败关闭；模型声明前和调用前各复核。损坏 pin 不能回退当前目录，重复 token 返回原配置，客户端不能注入 pins，公共 DTO 不暴露它们。同一 SecretRef 背后的凭据轮换不改变授权。Workflow target 只能使用管理员登记时的固定 pins，dispatch/recovery 不补齐。

**MCP 调用：** 精确 origin、TLS、DNS/连接绑定、私网 alias、拒绝凭据重定向；输入/输出 schema、structuredContent、畸形/超限结果、超时/重复和 SecretRef 脱敏有独立断言。错误结果不会成为 Fleet 策略指令。第一版不安装包、不启动任意 stdio 进程。

**外部Agent：** 原admission同事务保存注册、intent、预算和预约。lookup错误、配置/journal漂移、崩溃/重启、撤权与双进程均保留原token/payload/任务身份；未知不自动重放。状态与取消精确匹配task/session，unsupported取消到期仍unknown。`agent_reported`经真实HTTP/前端保留来源，本身不生成workflow_evidence或acceptance；产物仅来自另行声明的已发布ArtifactRef导出和Hub完整核验。新增输出回归覆盖16项/256MiB预算、完整64KiB读取、structured schema/JSON、源码descriptor/tar/tree/blob、原事务回滚、private proof恢复、下游引用、独立接受屏障和两进程竞争。reader8只在输出admission提高，external schema3和旧无输出字节兼容；历史按signed rowid每页128行及批量引用闭包校验，明文/加密恢复frozen。外部账号/journal不包含在Hub备份内；重复取消/超时收尾owned I/O并保留原异常链。

**Fleet 出口：** 用实际 MCP client 发现并调用三个允许工具；测试集成身份只能访问获准模板、工作区和任务。不能调用 approve/acceptance/shell/SecretBroker、策略修改或节点注册。相同 client_token 不同内容冲突。`test_integration_identity.py` 与真实 HTTP/HTTPS 覆盖不可变登记、全局 token_digest 唯一索引、跨 owner/身份冲突、无启动注入的重建、两个 OS 进程的撤销/登记竞争、认证后/既有回执/读取/写入前撤销，以及重复取消后仍收尾原事务。原 Workflow 事务核对当前身份、实际 definition/请求及预算；撤销先提交则无新流程。reader 4 独立于 2/3，运行时及 gate-off 拒绝未来/残缺 schema；历史以 ≤128 行 keyset 校验，包括非正 rowid、撤销记录和末批损坏。启动扫描使用只读事务，真实第二 writer 不被扫描占用。明文/加密恢复 frozen 并禁用所有导入凭据，源库/namespace 不变，重复登记不能复活；正常 operator 仍可核对既有任务。具体契约见[§7.7](../superpowers/specs/2026-10-20-artifact-capability-integration-contract.md#77-fleet-集成身份持久化与撤销)。

Muse 原生会话和审批是独立事实域；测试 adapter 不得把原生自动审批设置映射为 Fleet 本地或生产权限。真实 Muse 与真实 MCP server 各自有外部试点记录，不能由 loopback 测试代填。

## 9. WF-07：预算、返工和账目

一个流程分多个子执行，聚合现有请求账本并验证每个 request 只归属一次。加入失败重试、超时但实际计费、usage 缺失、迟到 usage 和外部未知用量；不能在流程层再记一份实际用量。

分别耗尽 max_tokens、max_model_requests、max_active_seconds、max_wait_seconds 和 max_generations。默认 generation 1–3 可用，第 4 代拒绝；协调器重启不消耗一代。等待人工/节点不占模型并发，但等待截止仍生效。

预算竞争使用真实事务：两步骤/两进程不能预留同一余额。未知 settlement 保留为未知义务；释放模型/节点槽位不能把未知费用清零。日常 tick/poll、心跳和重复事件不调用模型、不产生重复通知。

## 10. WF-08：界面、gate、备份和回退

实际浏览器至少覆盖正常、空、错误、waiting_node、waiting_external、validation_failed、unknown、取消未确认、待验收和 accepted。测试 390px 宽度、键盘焦点、长中文目标、恶意产物名、评审 Markdown、事件截断和文件下载归属。

提交后刷新/关闭重开，同一 workflow_id 恢复；after 游标不漏不重展示最终事实。旧响应晚到不能覆盖新 revision。退出并切换账号后，流程/文件/预算不残留。接受动作遇 digest/revision 冲突必须显示当前证据，不能后台自动重试接受。

| 操作 | 必须保留的行为 |
|---|---|
| 默认 gate 全关 | 无新外网/执行线程；不影响旧 Run/Task |
| dispatch gate 关闭或 drain | 无新 admission；回执、停止请求、核对、审计继续 |
| 有在途/unknown 时关闭整个域 | 明确拒绝或保留只读/恢复入口，不隐藏存活执行 |
| gate 关但新表存在 | backup、引用扫描、Conversation 删除仍识别 Workflow 引用 |
| 含 V2/publishing 的备份 | manifest/文件一致、未知 schema fail closed；恢复保留同 ID |
| 恢复数据库和文件 | 默认 frozen；先核查旧 Node journal，不重新派发全部步骤 |
| 回退到旧代码 | 只支持认识现有 schema/引用的 reader；不删除新表/产物 |

备份用临时目录和现有加密路径验证正确/错误密钥、内容篡改、缺文件、过期/中断和恢复目标不可覆盖。不能用生产库或真实凭据完成自动化测试。

## 11. 性能初值与测量

以下为设计初值；本机样本与适用边界记录在下方，不用样本替代生产容量结论。每次测量记录 CPU/RAM/磁盘、OS、并发、数据量、配置、P50/P95、RSS 和 SQLite lock wait；失败保留原数据，再决定优化。

| 项目 | 测试规模与初始目标 |
|---|---|
| 详情/事件读取 | 1000 个流程、分页 100；本机 API P95 ≤ 300ms，不含外部网络 |
| 协调 tick | 32 workflows / 64 动作、无外部等待；P95 ≤ 500ms，不在写事务中调用模型 |
| 文件传输 | 单文件 64 MiB、4 路流式并发；进程峰值增量 RSS ≤ 96 MiB，随文件大小不线性增长 |
| 多进程容量 | 两个 worker 同库；0 次超售、0 次旧 epoch 覆盖、0 次恢复造成的新逻辑副作用 |
| 轮询成本 | 无状态变化时 0 次模型调用、0 次重复通知；按 next_poll_at 退避 |

### 2026-11-12 当前功能候选基线

冻结`7e5f85fc65c02db8cfbac6d848af97658f0f487e`补测独立导出进程：Hub在父进程运行，两个spawn子进程分别使用原`ArtifactExporter`和`Transport`，每个并发两条已签名命令，通过真实loopback HTTP上传；子进程与Hub各自每10ms采样RSS，未把客户端和服务端内存混算。保留四条调用起止单调时间戳并验证共同重叠区间，四份发布文件另以流式读取核对大小/SHA-256，重复export零新增HTTP，原库完整性与外键检查通过，1219源码摘要不变且checkout clean。

| 每文件规模 / 总量 | 总用时 / 聚合吞吐 | Hub RSS峰值增量 | 两个独立导出进程RSS峰值增量 |
| --- | --- | --- | --- |
| 8MiB / 32MiB | 0.524s / 61.082MiB/s | 9.016MiB | 21.484 / 21.453MiB |
| 64MiB / 256MiB | 3.422s / 74.812MiB/s | 12.391MiB | 56.672 / 56.469MiB |

环境为M5/10CPU/16GiB/macOS26.6.2/Python3.14.8。64MiB样本的Hub和两个导出进程增量各低于96MiB初值，但两种大小不足以证明长期驻留或文件大小增长时的渐近界，10ms采样也不保证捕获更短瞬时峰值。此处是独立Node导出组件，不是完整常驻NodeRuntime、多台物理节点或公网吞吐；未调用模型、未访问生产库。可重跑脚本`independent_node_export_probe.py`及两个`independent-node-export-{8,64}/report.json`位于`/Users/mango/.cache/agent-fleet/verification/final-review-alias-fix-20261112-bhjc67rf/`，包含逐进程RSS序列和逐请求时间/长度/状态；不含凭据。完整NodeRuntime与真实网络/模型/人工介入/返工率仍须分别测量。

同日后续冻结`db06fbc7c417e3554e599c22266df571cf1bfeb9`补充独立进程真实写锁竞争：每组先测无锁写事务入口，再由spawn子进程取得`BEGIN IMMEDIATE`，父进程读取原Workflow；读取完成后，子进程再持锁150ms，父进程通过原`WorkflowRepository.transaction()`等待写锁并提交。两端各20组，使用独立临时库，未访问生产库；报告保留逐样本父/子PID及单调时间戳，证明读取位于持锁区间、写事务进入晚于锁释放。

| 环境 | 无锁写入口 P50/P95/max ms | 竞争写入口 P50/P95/max ms | 持锁期间读取 P50/P95/max ms |
| --- | --- | --- | --- |
| macOS26.6.2/M5/Python3.14.8/SQLite3.53.4 | 0.522/0.737/4.171 | 180.471/184.929/184.953 | 0.752/0.874/1.069 |
| Linux6.8/Python3.10.12/SQLite3.37.2 | 0.301/1.672/1.687 | 179.039/180.562/182.570 | 1.095/1.752/1.783 |

每端40条探针写入均提交，原Workflow逐字段不变、`integrity_check=ok`、外键检查空、WAL开启，冻结源码逐文件摘要不变且checkout clean。竞争指标是事务上下文进入耗时（包含连接、SQLite获取锁及Python开销），不伪称纯引擎锁等待；本样本为单个并发writer、顺序20组，Linux与完整测试同机并行，不能推导生产容量或多worker饱和吞吐。证据和可重跑脚本位于`/Users/mango/.cache/agent-fleet/verification/final-review-alias-fix-20261112-bhjc67rf/`：`sqlite_contention_probe.py`、`sqlite-contention-macos/report.json`、`sqlite-contention-linux-report.json`；Linux报告复制SHA-256为`0ef3ace34d729cc242a8a05027dce8eb9cea05d2832f5684794ca7f143c4fda8`。独立Node RSS、真实网络传输、模型用量、人工介入和返工率仍开放。

在候选提交`15b439c66aab540eb2f53dced80f5000bab86a9d`串行运行四组容量探针，环境为 Apple M5/10 CPU/16 GiB、macOS 26.6.2 arm64、Python 3.14.8、SQLite 3.53.4、psutil 7.2.2。工作树含验收矩阵及跨平台读取测试调整，`working_tree_dirty=true`；四份报告均绑定源码清单 SHA-256 `69c91557c79b87cce7ad14004c5f1f6d9c886a7991d4ef9d6409d65044bc17b4`，各自记录1213个源码文件摘要，运行期间源码未变化。

| 测量 | 规模与样本 | P50 / P95 / max 或吞吐 | RSS 峰值增量 |
| --- | --- | --- | --- |
| 流程列表 | 1000条流程，每页100；预热5次后50次 | 1.578 / 1.837 / 2.014 ms | 三种读取合并0.875 MiB |
| 流程详情 | 同库50次 | 1.304 / 1.477 / 2.096 ms | 同上 |
| 事件页 | 100条事件，每页100；50次 | 1.364 / 1.666 / 1.885 ms | 同上 |
| 协调tick | 32活跃流程、32个持久Run、30次稳态样本 | 267.300 / 477.237 / 478.031 ms | 4.016 MiB |
| 并发上传 | 4路、4×8 MiB、1 MiB分片 | 32 MiB/0.474 s，67.471 MiB/s | 9.594 MiB |
| 并发上传设计规模 | 4路、4×64 MiB、1 MiB分片 | 256 MiB/3.604 s，71.022 MiB/s | 11.422 MiB |

协调tick保持32次真实持久执行、无外部模型/CLI调用；3429次`BEGIN IMMEDIATE/EXCLUSIVE`调用的耗时P50/P95/max为1.250/95.179/463.110 ms。该指标包含SQLite锁获取和Python开销，不是纯锁等待；长尾提示仍需在Linux目标配置和并发writer下复测，不能单凭一个本机样本调整数据库或队列。4×8 MiB与4×64 MiB的上传RSS增量近似，均低于96 MiB初值；各四份发布产物独立重读并校验大小及SHA-256。读取使用Flask WSGI客户端，不含TCP；上传在同进程loopback完成，不代表公网吞吐、独立Node RSS或生产容量保证。

原始报告与清单：`/tmp/agent-fleet-capacity-current-head-20261112-{reads,coordinator,transfer8,transfer64}/report.json`及各自`source-manifest.json`。本次样本仅完成F6本机性能基线；真实模型用量、返工、人工介入、Linux/browser严格门禁和生产任务恢复仍分别开放。

### 2026-11-11 Linux 当前候选测量与协调器修复

复用同一容量探针，在SG测试机私有目录串行运行，各组独立进程、新夹具，不与pytest并行。
环境：Debian12/Linux6.1、Python3.11.2、SQLite3.40.1、psutil7.2.2，2 vCPU
Intel Xeon Gold6278C、1894342656 bytes物理内存。源码冻结`90a07ef3`，1184文件
摘要运行前后一致、checkout clean；本节记录测试环境容量，不推导生产容量。

| 测量 | P50 / P95 或吞吐 | RSS峰值增量 |
| --- | --- | --- |
| 1000流程列表/每页100 | 4.333 / 4.915 ms | 三种读取合并1.930 MiB |
| 流程详情 | 3.903 / 4.842 ms | 同上 |
| 100条事件页 | 4.207 / 4.788 ms | 同上 |
| 32活跃流程协调tick，修复前 | 755.368 / 895.182 ms | 10.957 MiB |
| 4×8MiB上传 | 32MiB/1.165s，27.465 MiB/s | 19.871 MiB |
| 4×64MiB上传 | 256MiB/9.522s，26.885 MiB/s | 27.328 MiB |

读取为Flask WSGI客户端；上传为同进程四路真实loopback HTTP，已预先reservation，
不包含公网、真实Node进程或同时reserve竞争。两组各四份产物独立核验大小与SHA-256。
RSS每10ms采样，未证明更短瞬时峰值；BEGIN调用耗时包含锁获取与Python开销。

协调tick超出500ms初值后，保留串行writer、只读轮询、限制并发及整库串行等隔离实验，
未把实验注入当作产品修复。性能剖析发现5个稳定tick合计1290次连接/事务；
无变化观察会无条件更新owner队列，每轮释放32条租约也会单独提交32次。
新增回归先确定失败（重复观察每次写1行、4条释放产生4事务），随后：

- owner队列只在实际`ready_at`变化时更新，保留NULL与`last_served_at`公平性计算；
- 原协调器每轮至多32条租约改为一个原子事务，每条保留owner/workspace/worker/epoch/到期围栏，每个受影响owner仅刷新一次；失败整批回滚。

独立最终冻结`ca45f265`同探针P50/P95为**461.631/517.816ms**，较原P95下降约42.2%，
5527次写事务开始降为4442次；RSS增量14.910MiB，仍只创建原32个Run。
该样本**尚未满足500ms初值**，不记为容量验收完成。
macOS与Linux同十模块关联各229 passed，Windows CI选择及新增回归231 passed/2 POSIX skips；最终完整门禁须在此产品改动之后重新执行。

基线、原始采样与全部实验在
`/Users/mango/.cache/agent-fleet/verification/registered-full-gate-20261111-9ek7mk__/capacity/`；
正式修复源码bundle、失败回归、Linux原报告在
`/Users/mango/.cache/agent-fleet/verification/batch-claim-release-20261111-xojwqjub/`。
当前交付状态以[开发总账](../platform/development-completion.md)为准。

后续正式修复冻结 `90374763513e1b7878544d4d4256cfcab93268ad` 把已绑定派发轮询移到只读事务；尚未绑定则关闭读事务后进入新的写事务，重新复核全部围栏。持有真实SQLite写锁的旧实现确定失败，修复后可正常读取；读写之间真实revision变化仍拒绝。新增3个必需selector/7实例登记WF-03。

同探针两次串行、各新进程/目录，均不与Linux pytest并行：

| 32流程协调tick样本 | P50 / P95 / max（ms） | 写事务开始数 | RSS峰值增量 | Run / 逻辑执行数 |
| --- | --- | --- | --- | --- |
| 首轮 | 442.946 / 494.353 / 504.625 | 3429 | 14.707 MiB | 32 / 32 |
| Linux回归完成后复测 | 435.392 / 498.105 / 498.267 | 3429 | 13.148 MiB | 32 / 32 |

两次各30样本的P95均低于500ms初值，首轮max仍高于500ms；P95余量很小，不将该负载推广为生产容量保证。原始采样、环境和源码摘要保留在 `/Users/mango/.cache/agent-fleet/verification/bound-dispatch-read-20261111-2k3dvekt/linux/capacity-coordinator{,-repeat}/report.json`。

同冻结本机完整7201 passed/11 Linux-only skipped/204 passing subtests/零失败错误，41/44 journeys、2154 selectors、46完整PNG；原核验器仍因 `journey_incomplete` 退出1。Linux关联261 passed及11必需实例另跑通过，Windows CI选择和新增回归238 passed/2 POSIX skips。1184源码摘要三端不变、报告复制hash一致；不合并平台报告作为完整Linux门禁。版本、原日志、核验限制与生产只读对照见同证据根 `verified-summary.json`。生产container receiver尚缺jsonschema预检，配置/部署后真实任务和同版完整Linux/browser继续开放。

**同冻结的Linux全量补跑**：7162 passed、50 skipped、零失败错误，1711.23秒。44条journey / 2154个selector全部收集，28条journey完整；48项跳过由于没有配置 `FLEET_PLAYWRIGHT_MODULE`（Linux Chromium 环境未准备），另外2项因 `rsync` 缺失。原release verifier按实际覆盖退出1/`journey_incomplete`，缺16条journey；该报告不与本机报告拼合，也不冒充完整Linux gate。运行前后1184文件SHA一致且checkout clean，远端原JUnit/journey/log与本地复制逐项hash一致。摘要和原始证据在 `/Users/mango/.cache/agent-fleet/verification/bound-dispatch-read-20261111-2k3dvekt/linux/`。

**rsync环境补测**：在同一Linux验收机安装424700字节的`rsync 3.2.7-1+deb12u6`包，再跑`test_review_rollback.py`与`test_review_store_release_ownership.py`两个完整模块，8 passed/0skip/0失败/0错误。原全量JUnit及其50 skip不改写；独立JUnit远端/本地SHA一致，源码仍相同冻结且checkout clean。Linux全量的48个浏览器用例跳过仍未补，journey覆盖与release verifier结论不变。证据见`rsync-summary.json`与`rsync-regressions.xml`。

**Windows 原生交接复验**：首次以未保护的通用`tmp_path` 作为 Node 接收根目录，使严格Windows ACL检查正确地不声明`artifact.receive`，4个接收/回执/备份断言随之失败。两节点夹具已改用当前用户私有 DACL 的 input 和 runtime/journal 根目录，并在同一 Node 重启时复核现有根目录 ACL。Windows 11/Python 3.12.7 新鲜运行检查点、远程暂停、容量索引、两节点实际 HTTP 交接 **62 passed/0skip/0fail/0error，43.01秒**；同两节点模块 Linux/Python 3.11.2 **29 passed**。没有改Windows产品 ACL或将该两节点受控HTTP伪称为两台真实设备。初次失败与修正后JUnit独立保留，后者SHA-256为`aeec3c4eb092d0101fd3092c908c04b2bcd0077a964f436e99486d1b9f030cc5`，摘要见`/Users/mango/.cache/agent-fleet/verification/bound-dispatch-read-20261111-2k3dvekt/windows/windows-native-rerun2-summary.json`。

macOS Python 3.14 的对应14模块集合另跑 **323 passed/0skip/65.91秒**；该数值不与Windows 62项合并。测试 fixture SHA-256 `5d4d3bafd558cb785af8b41744e578810bdbe9e1d543c745e3a70689280f8a31` 与Windows副本一致，产品运行源码没有因这次测试夹具修正而改变。

### 2026-11-10 当前目标树本机基线

探针为[workflow_capacity_probe.py](../../tests/fixtures/workflow_capacity_probe.py)，显式执行，不参与 pytest 自动收集。
四组串行运行，各自使用独立 Python 进程与新夹具目录，未与全仓 pytest 同时运行。
环境为 Apple M5（10 CPU）/16 GiB、macOS 26.6.2 arm64、Python 3.13.7、SQLite 3.50.4、已安装的 psutil 7.2.2；
卷总量494332366848 bytes，运行前空闲约236.8–236.9 GB。详细负载、磁盘与进程数据见各原始报告。

| 测量 | 规模与样本 | P50 / P95 或吞吐 | RSS 峰值增量 |
| --- | --- | --- | --- |
| 流程列表 | 1000条单步流程，每页100；预热5次后50次 | 1.476 / 1.680 ms | 三种读取合并0.875 MiB |
| 流程详情 | 同库，50次 | 1.277 / 1.372 ms | 同上 |
| 事件页 | 同库，每页100条事件，50次 | 1.314 / 1.357 ms | 同上 |
| 协调tick | 32活跃流程、32个真实持久Run，动作上限64；预热5次后30次 | 254.642 / 448.152 ms | 4.781 MiB |
| 并发上传小样本 | 2个fixture节点身份、每节点2路；4×8MiB、1MiB分片 | 32MiB/0.425s，75.309 MiB/s | 9.266 MiB |
| 并发上传设计规模 | 同样4路、4×64MiB，1MiB分片 | 256MiB/3.181s，80.477 MiB/s | 10.328 MiB |

读取经过Flask WSGI测试客户端，未含TCP与外部网络；详情使用单步、无产物/验证证据的流程。
协调器未调用模型或真实CLI，测量结束仍只有原32个Run，没有新增逻辑执行。
协调写事务5527个`BEGIN IMMEDIATE/EXCLUSIVE`观测的P50/P95为0.173/83.125ms；
这是事务开始调用耗时（含锁获取与Python开销），不能视为纯锁等待或端到端排队延迟。
64MiB上传同项P95为4.692ms。

上传使用真实回环HTTP、签名命令与原SQLite/文件发布链，四份落盘文件均独立重读验证大小和SHA-256；
四路在测量前分别完成reservation，未测同时reserve竞争或Node exporter/journal路径。
Hub与HTTP客户端位于同一进程，RSS以psutil每10ms采样，表中为相对测量前基线的峰值增量。
文件从8MiB增到64MiB时，观测增量只从9.266到10.328MiB，且均低于96MiB初值；
短于10ms的瞬时峰值、远程带宽、真实Node进程与生产容量仍需独立测量。

四组报告的源码清单SHA-256相同：`2009c32aab6d85da0c0bdc3c9a4814d5d19ce3f11e77f7792caa5629983814ab`。
报告保留probe自身hash、源码逐文件hash、版本、dirty事实、原始延迟样本、RSS和负载。
证据位于`/tmp/agent-fleet-capacity-{reads,coordinator,transfer8,transfer64}-20261110/report.json`，对应日志位于同名前缀`.log`。

复现命令（输出目录必须尚不存在；psutil为显式探针依赖，不自动安装）：

~~~bash
PYTHONPATH=.:tests /usr/local/bin/python3 tests/fixtures/workflow_capacity_probe.py reads /tmp/fleet-capacity-reads
PYTHONPATH=.:tests /usr/local/bin/python3 tests/fixtures/workflow_capacity_probe.py coordinator /tmp/fleet-capacity-coordinator
PYTHONPATH=.:tests /usr/local/bin/python3 tests/fixtures/workflow_capacity_probe.py transfer /tmp/fleet-capacity-transfer8 --file-mib 8
PYTHONPATH=.:tests /usr/local/bin/python3 tests/fixtures/workflow_capacity_probe.py transfer /tmp/fleet-capacity-transfer64 --file-mib 64
~~~

保留Flask/SQLite与现有执行生命周期继续迭代。这组样本未证明需要更换数据库或引入第二套队列；
后续先扩大真实负载并关注协调阶段的写锁竞争。人工干预、返工率、真实模型用量尚未采样。

真实模型质量不使用“21 个领域测试”或协议 fixture 成功率代替。试点单独记录目标完成率、返工代数、人工介入和消耗，比较相同输入与模板版本。

## 12. 执行命令与交付判定

首批纯领域计划可以仅用标准库测试，详见[首批计划](../superpowers/plans/2026-10-20-workflow-foundation.md)。对应功能文件落地后，局部命令示例为：

~~~bash
PYTHONPATH=. python3 -m pytest tests/test_workflow_dispatch.py tests/test_workflow_control.py tests/test_workflow_budget.py -q
PYTHONPATH=. python3 -m pytest tests/test_artifact_transfer_recovery.py tests/test_workflow_two_nodes.py -q
~~~

发布候选必须在浏览器依赖就绪的 Linux 环境运行完整门禁；安装依赖沿用[现有入口](README.md)，并遵守操作者下载规则。Linux 实际隔离测试另需 bubblewrap、libseccomp 和 user/mount/PID/network namespace；缺依赖必须失败。2026-10-30 的冻结 Linux 全仓及原发布核验已通过，具体运行时、源码摘要和外部边界记录在[实施进度](../platform/workflow-platform/implementation-progress.md#2026-10-30-linux-门禁与实际沙箱续接)。

~~~bash
FLEET_PLAYWRIGHT_MODULE=/tmp/fleet-browser/node_modules/playwright \
FLEET_BROWSER_CHANNEL=chromium \
FLEET_SCREENSHOTS=/tmp/fleet-workflow-evidence/browser \
PYTHONPATH=. python3 -m pytest tests -q \
  --require-journeys \
  --journey-report=/tmp/fleet-workflow-evidence/journeys.json \
  --junitxml=/tmp/fleet-workflow-evidence/results.xml
~~~

必需 selector 不得 skip/xfail、部分参数排除或缺失；macOS 平台限制不代替 Linux gate，Windows 接收另外跑平台矩阵。报告与 JUnit 的 checkout revision、dirty 与矩阵 hash 要一致。

受限外部试点的默认登记如下，实际运行后附脱敏证据：

| 外部边界 | 本轮状态 |
|---|---|
| 获准真实 CLI / 模型代码交付 | not_run：本机受控子进程链已接通，未开展真实模型/商业 CLI 试点 |
| 真实 sandbox 文件/网络/凭据隔离 | 部分实际平台证据：macOS sandbox-exec 与 Linux bubblewrap namespaces/seccomp 的评审/固定测试拒绝宿主文件读写和网络，评审源码/请求只读；无隔离对照及 HTTP 链通过。Linux 三模块 50 passed，macOS 双 Python 各 50 passed；最新同源 Linux 原全仓 5754 passed / 0 skip、42/42 journeys，远端/本地原 verifier 退出 0，见 [Windows 计划最终冻结记录](../superpowers/plans/2026-10-30-windows-artifact-receive.md#final-frozen-verification-2026-10-30)。Windows 执行 sandbox 和获准生产部署配置 not_run；Linux Python 3.13 全仓不替代 CI Python 3.10 或生产配置验收 |
| 两台真实节点文件交接 | 固定源包窄试点已通过：home-win→原 Hub→gz-cvm，控制 1 passed、独立 Linux sandbox 固定验证 2 passed，原 ID/bytes/tree/blob、一次 PUT/一次下载/两次同接收回执核验；见[实际两机试点](../superpowers/plans/2026-10-30-two-host-artifact-handoff.md#actual-frozen-verification--2026-10-30)。该结果不代表完整 WF-04/05 或 NODE-LIVE。 |
| Windows 接收安全（独立接收切片的历史冻结） | home-win Windows 11/NTFS 最终显式组 60 passed/0 skip，覆盖真实句柄、reparse/ACL、无覆盖发布、内核锁与四窗口退出恢复；原 NodeRuntime 经 SSH 回环访问原 Hub HTTP 的独立 canary 1 passed，丢回执后重启仅重传原回执、一次下载，Hub 才 ready。最终冻结结果见 [Windows 计划](../superpowers/plans/2026-10-30-windows-artifact-receive.md)；进程退出不证明断电持久性，Windows 导出及完整 WF-04 开放 |
| 只读 MCP 服务 | not_run：本机固定协议 2026-07-28/HTTPS JSON/SSE 已验证，尚未登记获准外部服务 |
| Muse 外部任务导出/取消支持 | not_run：须按 adapter 能力逐项核查 |
| 邮件/IM 真实投递及部署回退 | not_run：仍按原发布合同独立验收 |

F3 交付需 WF-01/02/03/07/08 对应首期范围通过、现有门禁不退化、真实试点与恢复证据齐全。F4/F5 逐项增加 required 范围；没有证据的集成维持关闭。任何自动化测试均不隐含授权真实发送、合并、推送或部署。

Hub 固定绑定子集增加 `test_capability_dispatch.py`：原单槽预约只计一次、其他 isolated 不被扣除；原 Run/token/generation/消息/配置摘要/预算/租约闭包；错误配置标记不产生无绑定回退；注入后参数上限与原 active_deadline；审批消费/签名/outbox 原子回滚，重复不再消费；pause/drain/frozen/gate-off 暂缓首次投递，取消/失代/软件/策略/动作登记变化拒绝；在途回执和 unknown 保留。Node HTTP 用例使用真实 Hub preparation，并保留软件变化后的本地拒绝。NODE-01 登记 170 selectors、WF-08 登记 125；完整 WF-05 继续开放。详见[产物契约 §6.4](../superpowers/specs/2026-10-20-artifact-capability-integration-contract.md#64-hub-固定能力-run-与-command-派发子集)。

## 显式 Memory 输入的本机验收（2026-10-25）

[接入契约 §7.12](../superpowers/specs/2026-10-20-artifact-capability-integration-contract.md#712-外部-agent-的显式-memory-输入与版本权威)的 external_agent Memory 子集已由 31 selectors/84 cases 验证：WorkContract1保留正revision，2显式支持Memory0、reader6；完整owner Memory批读、共享8KiB预算、exact原请求恢复、当前撤销阻止发送、schema1/2到3原子迁移、历史非正rowid/末批和普通/加密frozen恢复。Native/summary仍拒绝，未产生真实调用。

串行双Python23模块各784 passed/0skip。全仓 **5256 passed、0 failures/errors、5 skipped、195 subtests passed**，WF-06 **302 selectors / 681 cases 全通过**，**41/42 journeys**；严格退出 **1**、ci_status=failed，唯一 incomplete 为 Linux RELEASE-01，七项外部检查均 not_run。证据目录：`/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-external-memory-full-20261025-dnxqbpck`。 原失败和首次完整gate单独保留，详见[实施进度](../platform/workflow-platform/implementation-progress.md)。这不是Linux/Windows或真实F5试点通过。


## 2026-10-26 Native Workflow Memory verification

本地 native_run 完整显式 Memory 已实现：原 Run 事务私有快照/reader7、provider 前当前授权复核、原任务恢复、DTO 隐私与完整备份历史。20 selectors / 52 cases；串行 Python 3.10/3.13 关联各 886 passed、无 skip。该轮完整冻结门禁 **5346 passed、零失败、5 macOS 平台 skip、195 subtests passed**；WF-06 **327/759**、41/42 journeys，严格退出 1/failed 仅因 Linux RELEASE-01 未完成，七项外部检查 not_run；860 文件运行前后摘要一致。详细证据及剩余工作见[实施进度](../platform/workflow-platform/implementation-progress.md)。summary/CLI/评审/测试/远程 capability context 与外部输出仍开放；ArtifactRef 方向已批准，exporter 未实现；完整平台与生产验收未完成。


## 2026-10-27 外部 ArtifactRef 输出

显式 export-capable adapter 已接通原任务已发布 Hub ArtifactRef 输出：完整 owner/workspace、字节/schema/源码包核验后，由原事务提交输出及 subject。47 个新 selectors / 178 cases 全通过，Python 3.10/3.13 关联各 1153 passed、无 skip。完整冻结门禁 5524 passed、0 failures/errors、5 个平台 skip；WF-06 374/937、WF-08 179/380 全通过，866 文件摘要未变。严格退出 1 仅因缺少 Linux RELEASE-01 必需实例，七项外部检查均 not_run。完整核验不代替 validator/人工接受，Muse export 仍 unsupported；Linux/Windows、真实试点及整体生产验收继续开放，gate 默认关闭。详见[实施进度](../platform/workflow-platform/implementation-progress.md#2026-10-27-外部-agent-已发布-artifactref-输出)。

## 2026-10-27 Native 完整文本产物输入

显式开启的本地 native_run 已消费完整 text/plain、text/markdown、application/json ArtifactRef 正文，文本与显式 Memory 共享 8192 bytes 预算。原 Run 私有 schema2/reader9、同 token 查回、当前权限复核、有界历史与 frozen 备份保持原执行链；正文不进入 DTO、事件或 repr。

新增 39 selectors / 115 cases；串行 Python 3.10.21/3.13.15 的 38 模块关联集合各 **1532 passed、2 subtests passed、0 failures/errors/skips**。 全仓 **5639 passed、0 failures/errors、5 个平台 skip、195 passing subtests**；JUnit 实际 5644 个 testcase，不重复累计 subtests。WF-06 **413 selectors / 1052 cases**、WF-08 **201 / 451** 全通过，41/42 journeys。严格退出 1、ci_status=failed，仅 RELEASE-01 缺少三项 Linux pidfd/proc 必需实例；另两项 Supervisor proc 平台 skip，七项外部检查均 not_run。

详细证据见[实施进度](../platform/workflow-platform/implementation-progress.md#2026-10-27-native-完整文本产物输入)。所有 gate 默认关闭；summary、二进制/代码包输入、CLI/评审/测试/远程或 capability context、其余集成配置、Linux/Windows 与获准真实两机/模型/MCP/Muse试点继续开放。Muse export 仍 unsupported，整体平台与生产验收未完成。


## Windows 固定源导出验收（2026-10-30）

[导出计划最终记录](../superpowers/plans/2026-10-30-windows-artifact-export.md#final-frozen-verification-2026-10-30)已完成本切片：home-win NTFS 原生合并 131 passed/零 skip（41 export、60 neutral、30 unchanged receive），原 NodeRuntime→原 Hub 的 part/receipt 丢响应恢复 2 passed。源元数据/ACL 变化拒绝 commit，writer/rename/可写映射拒绝，原 ID/回执、12 MiB 流式约束及共享两槽保留。两个独立原生收发 canary 不等于两个真实产物 Node 的连续源包交接。

同源 Linux 原严格全仓 5785 passed、197 passing subtests、零失败或 skip、42/42 journeys，原远端/本地 verifier 均退出 0，44 PNG 完整核验；双 Python 关联各 295 passed。上表 Windows 接收 60 项与 Linux 5754-pass 为原接收冻结的历史结果。七项外部检查仍 not_run；两真实 Node 源包交接及完整 WF-04/05、真实 CLI/模型、生产配置、MCP/Muse 试点继续开放，gate 默认关闭。

## postcheck 终态拒绝边界（2026-10-31）

已有完整 audit 仍须核验原 owner/unknown：三种 postcheck × owner/status 改变 × 完整/缺 audit 的十二项回归，直接仓储、fresh application 原因链、原 HTTP 404/409 和拒绝时零写入均通过。Python 3.10/3.13 各自关联 291 passed、独立本地能力 6 passed、独立 evidence/passive-gate 121 passed，compileall 各退出 0。新 selector 仅追加 NODE-01/WF-08。[终态保护计划](../superpowers/plans/2026-10-31-postcheck-terminal-guard.md#authorized-recovery-and-verified-corrected-full-gate---2026-10-31)的修正冻结 `85488105...`、898 文件已独立完成原两机十二项矩阵与原 journal/Hub/复制核验。同源原全仓首次因容量耗尽退出 120；逐目录授权恢复容量后，fresh root/basetemp 原样完成 **5845 passed / 197 subtests / 0 failures/errors/skips**，42 journeys / 1747 selectors / 44 PNG、898 源码摘要、50 文件 7,940,720 bytes 及原远端/本机 verifier 全通过。失败与成功运行独立保留。该结果闭合已实现子集的原严格全仓；完整 WF-04/05/NODE-LIVE、模型/CLI/生产与外部验收仍开放，所有 gate 默认 false。

旧 `adf137251e21acbad036a3e51221d8cf6805731d` 全仓为 **pre-terminal-guard-baseline**：5833 passed、197 subtests、42 journeys、44 PNG、零失败/错误/跳过，远端和完整复制后的本地原 verifier 均通过。它不包含后续 guard/test/selector；完整 Workflow 与生产/外部验收不因此完成，所有 gate 默认关闭。

## 自动 capability Workflow lifecycle（2026-10-31）

生命周期切片由 `tests/test_capability_workflow_lifecycle.py` 覆盖，并同时登记到 NODE-01/WF-08。测试沿用原 coordinator → Run worker → signed Node HTTP → Node journal → observation 链：自动创建原绑定 Run，真实完成一次 workspace write；在写入完成但回执丢失时，原 command/Run/Workflow 进入 `unknown`，reservation 继续 `held`。独立 digest helper 必须事先被 Workflow contract、binding、Hub action、Node local registration 和 offer 接受，且只使用 `read_only` retry。

红色证据证明普通 unknown observation 曾把 dispatch stage 留在 `bound`，从而阻断了合法 postcheck。`WorkflowRepository.apply_observation` 现在在同一事务将普通 unknown 的 dispatch stage 更新为 `unknown`；这只修正持久化事实，保留原 token、payload hash、binding、generation、预算、reservation、retry 和不重放约束。

Python 3.10/3.13 新模块各 **4 passed**；相关 400-case 回归集合各 **400 passed**。四个参数结果覆盖 matched、mismatch、read_failed 及成功写入，反复查询和 Node journal 重启均无额外 write/digest I/O。该结果只证明本地自动 capability lifecycle，完整 WF-04/WF-05、真实模型/CLI/生产隔离、NODE-LIVE 与外部检查仍未完成；七项 external checks 为 `not_run`，gate 默认关闭。

## 实际 Windows/Linux 自动 capability lifecycle（2026-11-01）

[显式原生探针](../superpowers/plans/2026-11-01-capability-subprocess-lifecycle.md#verified-native-automatic-lifecycle--2026-11-01)在同一 902 文件冻结 `719d8e7007ded5ef1ebe6322a4715ac30521e7da` 上执行两机 × success/matched/mismatch/read_failed：**8 passed、零失败/错误/跳过**。原 coordinator 自动准入，原 Run worker 经过签名 HTTP 驱动实际 home-win Windows 与 gz-cvm Linux 独立 Node 进程；未显式调用 admission，未直接制造 unknown 终态。首次 tick 前的可信路由固定真实 offer 的 OS/architecture/software 和资源要求，实际写入完成后丢失完成回执，普通 observation 形成原 unknown 事实。

成功路径在终态 observation 前仍 held，之后以绑定 token/binding/observation 的摘要释放；必需输出缺失仍为 outputs_unavailable，不生成验证或接受事实。三种独立 read_only digest 观察均保留原 unknown/held、binding/token/generation 和预算事实，retry 返回 unresolved_execution。重复 HTTP 查询与重启只读原 journal，不重复 write/digest，后续普通 tick 不创建替代 Run/Command。八份原 Hub 数据库、两端 journal 与固定字节独立核对；三端 902 文件前后 hash 一致、冻结 checkout clean，42 份原生证据 / 363,814 bytes 复制摘要一致。

这是已授权固定 workspace 能力的自动生命周期证据，使用确定性 provider，单独保留于 `/Users/mango/.cache/agent-fleet/verification/capability-native-auto-20261101-7sx6gzta`。显式探针不进入 required selectors。相同冻结原严格 Linux 全仓首次为5848 passed/1 failed；专项已证实 Chromium 临时 backing-file `fallocate` 因 `/tmp` 磁盘耗尽返回 `ENOSPC`。获准容量恢复后原认证浏览器单项以原断言/超时 fresh 通过；完整全仓独立重跑后 **5849 passed、197 subtests、零失败/错误/跳过，42 journeys / 1751 selectors / 44 PNG**，远端及本地副本 release verifier 均通过，902 源文件摘要未变。失败运行和通过重跑分别保留，结果不合并。完整 WF-04/05、真实模型/商业 CLI、生产隔离和七项外部检查继续开放，gate 默认关闭。

## 独立远程固定测试验收边界（2026-11-02）

按[专用契约](../superpowers/specs/2026-11-02-remote-fixed-test-capability.md)实现的
`coding.test@1` 只执行固定 recipe，原签收/source/subject/generation/token/binding
必须一致。四个 `test_remote_workflow_test_*` 模块覆盖严格结果字节、实际 pytest/JUnit、
原子 Hub 发布、失败断言阻断、未知不重放与预约保持、取消/失租及 reader 10 frozen
备份恢复；登记 NODE-01/WF-03/WF-08，七项外部检查不变。

除本机受控回归外，冻结源码 `d78be33e3a86f9fe13644076e10100730d78de33` 已在 gz-cvm 真实运行独立 Linux NodeRuntime 与原 Hub/Run/Command/Artifact 链：通过、断言失败、丢回执恢复、unknown 四场景全过；unknown 保留 held，重启与 replay 不重跑。现有 Linux sandbox 无隔离负控制另 2 项通过。修正过时 reader 夹具后的918文件冻结 `2915fe6c5ca859f99650951b6e07981d1da44634` 已独立通过原严格 Linux 全仓：6010 passed、197 passing subtests、零失败/错误/跳过，42/42 journeys / 1839 selectors / 44 PNG；远端及本地原 release verifier 均退出0，50份复制证据逐项匹配，源码摘要一致、checkout clean。旧失败冻结与原生四场景证据分别保留，不拼接计数。生产隔离、完整 WF-04/05 继续验收，七项外部检查not_run；Windows 本动作不可用，所有 gate 默认关闭。显式原生夹具不进入 required selectors，新树精确结果见
[实施进度](../platform/workflow-platform/implementation-progress.md#2026-11-02-独立远程固定测试能力)。

## Hub本地输入位置组合验收（2026-11-01）

按[输入位置契约§5.5](../superpowers/specs/2026-10-20-workflow-execution-contract.md#55-hub-本地消费者与-node-输入位置)，固定Hub命令评审、固定测试和显式完整文本Native消费者沿用原Hub reader；Node接收仍用于实际Node目标。可信登记的owner/workspace/target、adapter/policy版本及kind必须全部匹配，缺登记默认要求原回执，已固定Node的generation不受后续Hub登记影响。bootstrap拒绝同scope/node输入位置冲突，且失败不替换旧登记。

新边界模块39实例及开启transfer的原HTTP/CLI→源码→独立评审→固定pytest/JUnit→人工接受正负路径均纳入门禁；Native原bootstrap/coordinator/worker/provider/HTTP隐私用例同时覆盖transfer关闭/开启。FILES-01追加10项selector，WF-08追加10项、WF-01追加1项、WF-06追加9项，既有required selectors与七项external checks保持原样。局部双Python回归与完整门禁分别记录。

904文件冻结0de03b72e0b2ea34f595f334ac3f6a95ac44864c的原严格Linux全仓 **5891 passed、197 passing subtests、零失败/错误/跳过**；42/42 journeys、1781 selector登记、44完整PNG，pytest及远端/本机原release verifier均退出0。源hash不变、冻结checkout clean，50份证据7,947,220 bytes大小/hash一致。证据根为/Users/mango/.cache/agent-fleet/verification/workflow-input-location-20261101-nzcdxbz9，详见[修复计划终态](../superpowers/plans/2026-11-01-workflow-input-location.md#verified-strict-linux-terminal-result--2026-11-01)。旧902文件5849-pass证据只证明旧冻结。该组合修复不完成WF-04/05、真实模型/商业CLI或生产验收，七项外部检查仍not_run，gate默认关闭。
