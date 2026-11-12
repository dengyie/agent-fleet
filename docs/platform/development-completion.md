# 文档开发总账

## Fleet 身份页面登记开发增量（2026-11-13，独立于公开 PR42）

账号页现在可以创建 Fleet MCP 集成身份：填写原 owner/integration、模板/工作区列表、三工具权限及预算，浏览器生成令牌并仅提交摘要；原身份仓库在同一写事务中验证新登记的工作区和 admission。相同定义重复登记保持原时间、撤销和恢复禁用，定义/token 冲突拒绝；原 CLI/bootstrap 默认合同不变，正常 Hub 无需重启即可使用新凭据。

原缺口 29 项测试失败、浏览器缺按钮失败均已留证。实际浏览器另外复现异步 `close` 事件之前发送登记的窗口，以及 `trim()` 改写合法 UTF-8 owner 的问题；现发送前检查 dialog 的同步 open/connected 状态，关闭只中止本次登记观察，所有标识原文保留。令牌辅助说明标签和 CSS import 顺序也已修正；可见性断言复现令牌处于 modal 视口之外，现生成后自动定位并选中，桌面/手机实际 hit-test 通过。中间的工作区 name 超限、缺浏览器 window 替身及两权限工具数量断言属于验收夹具问题，原失败报告保留。

后端/身份/CLI/Fleet MCP/明文与加密备份七模块 **247 passed、零失败/错误/跳过**；两个集成管理浏览器模块 **20 passed、零跳过**，含桌面/390px 创建、权限选择、实际原 MCP 认证、丢响应固定重试、关窗、账号切换和精确 UTF-8 标识。最新登记专属 **31 passed**；共享管理边界、账号矩阵、前端合同、架构、打包和门禁八模块 **351 passed、零跳过**。这几组报告独立保存，不标为全仓门禁。

原证据位于本机私有 verification 根 `identity-registration-20261113/`。26张最新PNG完整校验并抽查配置/令牌视图；46条journey/2529selectors/7541实例收集齐全。Windows CI新增两个实际模块（Native setup与Fleet登记），复用runner已有Node，不新增运行时安装；该配置变更后164项CI/打包/架构关联复验通过，尚无新Windows终态结果。操作与范围见 [页面登记说明](workflow-platform/integration-setup.md#从账号页面登记)及 [实施计划](../superpowers/plans/2026-11-13-fleet-identity-registration-ui.md)。当前尚未进入 PR42/生产；新的完整 Linux/browser/Windows 门禁和正式发布仍须单独验证。其余 MCP/外部集成配置 UI、自动摘要、视频/其他执行 context、真实外部试点及完整 TODO 继续开放。

## Native 初始化开发增量（2026-11-13，未进入公开候选）

原 `hub.workflow_setup` 仅初始化 coding-delivery 的三个独立会话，Native-only manifest 无法通过该入口准备普通 Hub 所需对象。开发分支现复用启动登记的纯 parser，支持 Native 1–8 step、精确 `--template` 选择及共享会话去重；沿用原只读 preview 和 create-only 写事务。模型及 secret 单独配置，初始化不派发任务。错误定义、歧义、超限 target 和不合法 model profile 在建库前拒绝；既有对象、归档冲突、插入回滚和两个 CLI 同时 apply 均有实际存储断言。

原缺口 6 failed、边界补充 3 failed 的报告保留。最终 setup/startup 集合 **81 passed**，扩展到 Native 文本/代码/图片/音频/Memory/恢复、明文与加密备份、架构边界的 14 模块 **390 passed、零失败/错误/跳过**；门禁关联 **134 passed、零跳过**，CI 必需 Ruff 规则、Python 3.10 语法、UTF-8 无 BOM 与 diff 检查通过。46 条 journey、2516 个 selector 全部对应当前收集的 7504 个实例，仅证明收集完整。原报告位于本机私有 verification 根 `native-workflow-setup-20261113/`。首次 green 命令因引用不存在的模块退出 4、未执行测试，未计入通过结果。

这组结果只覆盖本地开发增量，**不属于公开 PR42 / db4c7ff 的官方 CI 或候选包**，也不代替新版本完整 Linux/browser/Windows 门禁及生产验收。实施细节见 [Native 初始化计划](../superpowers/plans/2026-11-13-native-workflow-setup.md)，操作示例见 [工作区和会话初始化](workflow-platform/startup-registration.md#工作区和会话初始化)。

## 公开候选 CI 与生产包预检（2026-11-13）

公开候选 `db4c7ffa72f054d5bd5dd1e22d765b8ecf7188f1` 的 [PR42 CI](https://github.com/dengyie/agent-fleet/actions/runs/43249983897) 已成功：Linux **7482 passed、0 failed/skipped、204 passing subtests**，**46/46 journeys、2508 selectors**；Windows **224 passed、2 POSIX skips**。已下载并核对原 JUnit（7482 cases，零 failure/error/skip）、journeys 和 51 张 PNG。GitHub 的 PR merge-test revision `e6b6aecdde1ff571232265054783a52b415536a4` 与公开候选 Git tree 相同，均为 `0f47c6c1313d72bcc3352ef6d68f73a0c0adc2a0`。PR 的 package/deploy 按设计跳过，五项外部检查仍为 `not_run`。原报告位于本机私有 verification 根的 `ready-4bdb9c6-20261112/pr42-linux/` 与 `pr42-windows/`。

同公开候选完成真实 Codex CLI 开发、同模型独立会话评审、31 项固定测试、5 份 HTTP 产物下载及重登恢复，184.72 秒；桌面和 390px reload 通过，无 pageerror，1224 源文件前后不变。交付仍为 `awaiting_acceptance` / revision 22，人工接受未执行。独立 Linux Node 的原 `remote_test_probe.py` passed/failed/lost/unknown 四场景 **4 passed、17.57 秒**；每场景实际固定测试只执行一次，recover/replay 零执行、零下载，unknown 保留 held。测试失败证据仍为 failed，执行报告的 Run 成功不改写测试结论。原 root 降 DAC 后无法遍历 nobody 私有 runtime 的四项启动失败报告保留；切换到 runtime 所有者运行后重跑，未修改产品或放松隔离断言。远端产品/测试源码与候选相同；这不是完整生产 Workflow 或生产 Node 验收。

按正式脚本本地打包公开候选，包为 7158996 bytes、SHA-256 `fa657ec13b821c5a7b955e87456401fb1979be22c16428fd0448f45b558679d5`；实际生产 receiver 的 `check` 返回 `checked`，包含运行依赖、账号探针和候选导入。此包明确标记为本地预检包，不冒充 GitHub CI package。检查前后 LIVE inode、RELEASE_ORIGIN、gates/hosts 摘要一致，无 pending/维护标记，容器 healthz 为 200。公开分支 fetch/rebase 最新 main 后提交不变；[PR42](https://github.com/dengyie/agent-fleet/pull/42) 已转为 ready for review，尚未合并或部署。生产仍为 `301e259`。

剩余验收和功能保持开放：正式同次 CI package/deploy、生产每个启用模型与新鲜常驻 Node 的真实任务/下载/恢复、外部 MCP/Muse/邮件、其余集成配置界面、视频及其他执行 context、完整 WF-04/05 故障/竞争矩阵和 F6 的条件准入。上述试点和完整 CI 不代填这些范围。证据另位于本机私有 verification 根的 `live-cli-db4c7ff-20261112/`、`pr42-remote-node-20261113-owner-rerun/` 和 `pr42-production-candidate-check-20261113/`；CLI 证据含私有运行配置，不应公开整个目录。

## 当前交付候选与生产依赖准备（2026-11-12）

候选 `4bdb9c65efa00ade252112f962b9b7d34fa2cfd1` 在独立 Linux 环境完成原完整 pytest/browser/journey 门禁：**7482 passed、0 failed、0 skipped、204 passing subtests**，2230.42 秒；原发布核验器退出 0，确认 **46/46 journeys、2508 selectors、7482 JUnit cases**。运行前后源码摘要与 clean checkout 一致。原证据位于独立测试机 `/var/tmp/g4bdb/full/`，源码 `/var/tmp/agent-fleet-candidate-4bdb9c6/source/`；外部检查仍为 `not_run`。插件停用后取消登录的真实 Linux Chromium 流程在相同候选连续 8 次通过。后续仅文档修订不改变该测试覆盖的产品及测试源码，最终公开提交仍需自己的 CI。

同候选 macOS 完整结果为 **7468 passed、2 failed、12 platform skips**；两项部署依赖检查失败均为验证 venv 缺少已声明的 `websocket-client`。安装 lock 中的 1.9.2 后，同源运行依赖模块 **23 passed**。原完整失败报告不改写，也不把局部复验拼作全量通过。本机证据根为 `/Users/mango/.cache/agent-fleet/verification/ready-4bdb9c6-20261112/`。

生产只读核对仍为旧应用 `301e259` / CI 42540826668。随后在无 pending、无运行中部署事务并持有原 deploy lock 时，备份并原子更新宿主 `auto_deploy_container.py`；唯一源码变化是增加 `jsonschema` 导入，当前 SHA-256 为 `22081a08941a1de1d706bbe8e665a4182fb291b92da61d6e05081d4457055265`，旧文件保存在部署 state 下 `preparations/20261112T152833Z-jsonschema/`。容器以 Hub 用户安装 lock 版本 `jsonschema==4.26.0`、`jsonschema-specifications==2025.9.1`、`referencing==0.37.0`、`rpds-py==2026.9.1` 到原 user site；`pip check`、五项直接运行依赖导入及实际 schema validation 通过。首次 pip 因 report 路径不可写在安装前失败，更换到用户持有的 `.local` 后成功。未重启或切换应用，原 RELEASE_ORIGIN 和本地 healthz 均复核。

剩余：公开增量提交/PR、最终 CI 的 test/package/deploy、候选包 check、真实模型/Node/产物下载/重启恢复及尚未实现的 F5/F6 条目。生产 receiver/依赖准备不等于新版已发布，测试通过不代填外部验收。

## Native 启动登记重启修复（2026-11-12）

Native 文本、代码和图片/音频输入现可通过普通 Hub 启动登记装配；binding 必须固定完整 `provider_snapshot`，启动时只校验 `model_profile_id` 仍是启用目录项，重启后的已排队 Run 继续使用登记文件中的原 provider、model、endpoint、capabilities 与 secret 引用。平台模型档案变化不会再造成 `dispatch_unknown`，也不会因媒体能力档案变化而拒绝重启。`tests/test_workflow_native_startup.py` **21 passed**；Native 文本/代码/媒体/音频/provider 关联回归 **110 passed**，启动/恢复/浏览器关联回归 **66 passed、14 platform skips**。该修复尚未进入 GitHub main，当前候选仍需新的精确 Linux/browser 冻结、真实模型/媒体任务和部署验收；线上仍运行旧版 `301e259`。

## 精确快照与验收运行环境修正（2026-11-12）

此前`c6bbdc8130b3a6379862a421c8143642a2014965`复跑已终态：**7328 passed、99 failed、41 skipped、204 passing subtests，1905.76秒**；原核验器补跑拒绝`journey_incomplete`。调查发现该快照在macOS归档传输时增加并提交了1221个AppleDouble `._*`元数据文件（magic均为`00051607`），实际并非仅含工作树1221个真实文件。夹具枚举和源码扫描会读取这些二进制sidecar，浏览器子进程另因本次runner的PATH遗漏私有工具目录而找不到`node`；同目录的`bwrap`和`rsync`也不可见。首9个F对应账号浏览器，先前归为deadline失败的交接判断已纠正；9项deadline原场景独立复跑全部通过。不能将此旧运行作为产品回归结论或发布通过。

原报告、独立postrun verifier、快照审计与11份报告的大小/SHA-256复制核验保留于`/Users/mango/.cache/agent-fleet/verification/current-candidate-linux-20261112-runtime-invalid/`。旧shell runner的`set -e`导致pytest失败后不执行退出码记录/核验；旧`pytest.exit`和`verifier.exit`属于此前尝试，不是本次终态。独立补跑核验报告使用`postrun-*`文件，不覆盖旧记录。旧源码和临时夹具均保留。

改用Python tarfile按Git tracked及非忽略untracked文件的显式清单归档，逐成员比对SHA-256，禁止sidecar、隐式目录递归和额外成员。新冻结`dc6a42b83de78c209b88c0cadea3b4c68281faf0`为1221个真实文件、0个sidecar，上传前后归档大小/摘要及解包后的所有源文件均核验。与旧快照的真实文件相比，仅3份验收文档不同，产品和测试源码完全一致。新runner复用原已验证私有Node24/bwrap/rsync/Playwright，独立HOME/TMPDIR、分阶段新报告目录；无论pytest结果均记录终态并在完整阶段运行原发布核验。

独立Linux预检**38 passed、0skip，71.13秒**，覆盖9个账号浏览器实例、rsync回滚、真实review sandbox、80线程固定测试预算与Native音频；1221源码前后摘要不变且checkout clean。原完整Linux/browser命令已在同一冻结继续执行，不能以预检代替完整门禁。当前unit `agent-fleet-exact-candidate-20261112.service`，source `/var/tmp/agent-fleet-exact-candidate-20261112/source/`，evidence `/var/tmp/agent-fleet-runtime-corrected-20261112/`；最新stage/PID以`progress.json`及实际进程为准。

另以锁定25个适配wheel（合计7544502bytes、单文件最大3943588bytes、大小/SHA-256全核验）离线安装独立Python3.10.21 venv，`pip check`通过；当前14模块媒体关联集合**425 passed、0skip，48.05秒**，工作树1221文件摘要与冻结清单均未变。证据`/Users/mango/.cache/agent-fleet/verification/audio-python310-20261112/verified-summary.json`。这关闭了音频Python3.10关联测试的缺依赖收集缺口，不代表完整CI runtime或真实模型通过。没有全局Python或生产环境变更，生产配置、任务/下载/重启恢复及其余项目TODO仍开放。

## Native WAV/MP3 输入切片（2026-11-12）

本地Native `native_run` 新增受限WAV/MP3 ArtifactRef输入，复用可信 `inline_media_inputs` target和已验证ArtifactStore读取。provider capability显式固定 `audio_input=true`，音频元数据按schema6/reader14进入原Run快照；verified bytes只在派发预检与每次attempt中读取，发送 `input_audio` wire part前严格校验base64、格式和4引用/4MiB总上限。ready恢复重新读取原引用；pending仍unknown且不重发。图片schema5/reader13及既有纯文本/图片序列化保持原格式，音频/图片不在同Run混用。

验证：本机Python 3.14当前媒体关联14模块 **425 passed/0 skipped**（37.65秒）；另外三模块89 passed、六模块278 passed。正式收集有46 journeys/2508 selectors/7461实例，0 selector缺失；`--collect-only --require-journeys`因用例未执行而按设计返回不完整，不能算完整门禁通过。相关模块的CI必需Ruff规则、UTF-8无BOM及`git diff --check`通过。Python3.10初次因缺`cryptography`无法收集，后续隔离锁定依赖环境的14模块**425 passed/0skip**已关闭此关联范围缺口，见上节；更早402项仍只覆盖图片集合。没有真实外部模型调用或生产变更。源冻结Linux/browser、真实生产音频模型、任务/产物下载/重启恢复仍未验收。实现计划：[Native音频输入](../superpowers/plans/2026-11-12-native-audio-inputs.md)。

## 当前候选全量复验（2026-11-12）

对当前dirty功能树执行正式macOS/Chrome完整命令，实际 **7,449 passed、12 Darwin/Linux平台skip、0 failed/error、204 passing subtests**，17:09；JUnit计数7665（含subtests）。journey为 **43/46**，NODE-01、RELEASE-01、WF-01的9个Linux-only实例未执行，严格门禁退出1；原release verifier另外以`dirty_checkout`拒绝。JUnit、journey JSON和51张截图已复制并保留在`/Users/mango/.cache/agent-fleet/verification/current-candidate-macos-20261112/`。

当时标记为精确当前树的快照`c6bbdc8130b3a6379862a421c8143642a2014965`已上传到独立Linux验收机的`/var/tmp/agent-fleet-current-candidate-20261112/source/`；后续审计证实其中混入AppleDouble元数据，准确修正及新冻结见上节。此前全量尝试因 `/tmp` 987MiB tmpfs 被原测试自己生成的452MiB多分片暂存夹具填满而中止；cgroup未发生OOM，根分区剩余约11GiB。该次无效JUnit/报告留存于`/Users/mango/.cache/agent-fleet/verification/current-candidate-linux-20261112-enospc/`，本次创建的`/tmp/pytest-of-nobody/pytest-0`精确临时目录已删除。同快照改用专用TMPDIR复跑的终态及环境问题已另行记录，不把级联失败记作产品缺陷或通过。生产无改动。

## 冻结候选 Linux/browser 完整验收（2026-11-12）

冻结`827e40222d6d4e21be360adc1ed140590c561ea1`在独立Linux测试机按原完整命令重跑终态：**7438 passed、0 failed、0 skipped、204 passing subtests**，2206.32秒；46/46 journeys、2496 selectors全部通过。JUnit含7642个计数（含subtests），错误/失败/skip均为0；release verifier通过并确认7438 JUnit cases、51张PNG、源码1219个文件与冻结清单逐项匹配、checkout clean。证据位于远端`/var/tmp/agent-fleet-verify-7e5f85f-20261112/feature-auth-full-rerun-20261112/`，源码`.../feature-auth-source/`。此前相同冻结的两个全量浏览器超时在独立复跑分别通过，且此次完整套件通过。

运行环境Python3.11.18、Node24、Playwright1.64 beta/Chromium156，与CI Python3.10、Node22、Playwright1.63不同；systemd测试unit设CPUQuota150%、MemoryMax900MiB，观察到memory cgroup回收，无OOM kill。该结果证明这一个干净冻结版本的Linux/browser门禁通过，不覆盖其后工作树的未提交源码、当前CI runtime差异、实际部署依赖、真实任务/产物下载/重启恢复或外部服务。`MODEL-LIVE`、`MAIL-LIVE`、`NODE-LIVE`、`DEPLOY-LIVE`、`MUSE-LIVE`均保持`not_run`。当前`codex/functional-root-fixes`工作树包含冻结后媒体输入等变更，因此仍需对交付revision重新冻结并验收；本轮未推送、合并或部署。

## 最新冻结完整验收状态（2026-11-12）

冻结`827e40222d6d4e21be360adc1ed140590c561ea1`（1219文件）包含下述旧业务401修复。本机原完整命令终态为7426passed、12Linux/proc专用skip、204passing subtests、零failure/error、982.22秒；源码摘要前后一致、checkout clean，51张PNG通过原parser完整校验。43/46journeys通过，NODE-01/RELEASE-01/WF-01缺Linux实例，原verifier仍拒绝journey_incomplete。这不是发布通过。正式收集7438实例/46journeys/2496selector全部可解析。

同冻结Windows当前CI完整选择经参数逐项对照（含全部进程模块、三容量实例、原生artifact probe）为224passed/2POSIX专用skip/零failure-error、59.78秒，源码不变。Python3.12.7与CI3.10差异保留。独立Linux预检6passed后串行完整命令仍运行，已有一个F标记，须读原最终报告定位，不以macOS或预检覆盖。unit为mesh-huawei上的`agent-fleet-verify-feature-auth-20261112`；受限内存/CPU，生产HK不运行测试。正常packagers输出1140后端源码+origin、70前端文件逐字节匹配冻结，后端6836672bytes。证据根`final-review-alias-fix-20261112-bhjc67rf/feature-auth-*`及`continuation-checkpoint.json`。新功能没有推送或部署，生产依赖/receiver和真实任务下载恢复仍开放。

## 旧业务401导致账号切换误导航（2026-11-12）

独立Linux环境已准备完成：仓库requirements-lock适用的23个wheel逐项校验bytes/SHA-256后离线安装、pip check通过；Python/Node/Playwright/Chromium复用该机已有程序至私有目录，bubblewrap/rsync仅解包私有路径，以nobody执行并限制测试unit的内存、CPU和任务数。7e5f85f的1219源码摘要与clean状态不变。原预检4passed/1failed，真实沙箱review及80线程预算通过；held-session浏览器失败为后台conversations请求在logout/login间返回401，HTTP全局事件直接跳转login，早于被拦截的会话检查释放。全量未启动，原失败报告保留。

新增held-feature-unauthorized用例固定旧业务响应，切换账号后释放401；原7e app在此确定1failed。壳层现在先销毁旧视图/停止流并强制重查当前会话，只有当前账号发生变化才reload；同身份业务401或当前会话401仍要求重新登录，重查临时失败提供重新验证。首次实现曾对同身份401反复reload，原令牌浏览器测试检出1failed/127passed；补齐同身份分支后128passed。最终调整夹具先注册导航等待再释放响应，原7e再次1failed，修后浏览器/HTTP契约/operator/账号生命周期137passed、0skip/错误（93.51秒）。不改共享客户端其他业务401事件，不放宽超时；新参数继续由WF-08原必需selector覆盖。证据`final-review-alias-fix-20261112-bhjc67rf/feature-stale-auth-final-{red,green}.{xml,log}`。此后完整门禁须绑定新冻结，旧7e全量报告不代填。

## 实际部署前现网复核与验收负载退出（2026-11-12）

本次只读复核GitHub main、公网manifest仍同为`301e259000a7d1027181ee716b29d5dd5fa9b951`，最近main CI42540826668成功；功能候选`7e5f85f`仍未发布。公网登录静态页200，但Mac两次与mesh-huawei一次请求healthz均超时，不能继续声称当前后端健康。经备用SSH跳板实测HK load average为23.43/25.37/24.28，memory PSI avg10 some98.89/full90.78，两组pytest均处于D状态。压力与服务不可用同时存在，尚不据此断言全部原因。

为优先恢复实际使用环境，核对UID、启动tick、命令行、父子树并以pidfd停止本会话隔离验收：取消排队runner509423，向pytest480481/508498及确认的子进程发送SIGKILL，保留报告runner与原日志；生产Hub/guardian及其他服务未发信号。D状态可能延迟退出，必须另查进程消失和healthz恢复；被中断结果不计发布通过。替代Linux私有环境准备停在MarkupSafe3.0.4无适配Python3.11的wheel，尚未执行测试。最新状态见证据根`final-review-alias-fix-20261112-bhjc67rf/continuation-checkpoint.json`。部署前仍须完整Linux/browser门禁、receiver/运行依赖及真实任务/下载/恢复验收，安全专项后置不影响这些功能条件。

随后确认480475/480481/508480/508498/509423全部消失；旧db06完整runner写出terminal/exit-9、verifier1、源码不变，关联组progress仍留running但进程已不存在，排队7e未开始，均不作通过。HK memory PSI avg10 some降至6.93/full5.94，公网healthz恢复200/ok（1.761秒），支持本次验收负载影响线上响应的判断。后续完整门禁迁往独立验证环境，不在该生产宿主叠加完整pytest/Chromium负载。宿主127.0.0.1:8790拒连不能代替容器内健康结论；公网恢复证据独立保留。

独立环境依赖后续核对纠正：PyPI版本JSON实际列出MarkupSafe3.0.4的cp311/manylinux/x86_64 wheel（22961bytes），因此前述“无适配wheel”只是pip当时的错误输出，并非已证实原因。后续按仓库requirements-lock逐项选择适配wheel、核验size/SHA-256并离线安装，保留原索引失败证据，不通过降级版本规避准备失败。该环境Python3.11、Node24及复用的Playwright版本仍与正式CI不同，必须分别记录。

## 最终会话修复冻结与独立导出RSS（2026-11-12）

冻结`7e5f85fc65c02db8cfbac6d848af97658f0f487e`本机完整命令已终态：7425passed、12Linux/proc专用skip、204passing subtests、零failure/error，986.96秒；1219源码摘要前后一致、checkout clean。46journeys中43通过，NODE-01/RELEASE-01/WF-01缺Linux实例，原verifier仍以journey_incomplete拒绝；51张新PNG经原parser完整核验。最终矩阵7437实例/2496selector全部收集、无缺项；原packagers的1140后端/70前端文件逐字节匹配冻结，包6833623bytes。这不是Linux或上线通过。

同冻结两个独立原Node导出组件×两路真实HTTP上传已测8/64MiB文件：64MiB设计规模的256MiB总量3.422秒/74.812MiB/s；Hub RSS峰值增量12.391MiB、两个独立导出进程各56.672/56.469MiB。四路调用确有重叠、四份产物独立hash一致、重复export零HTTP、数据库完整性和源码不变。完整NodeRuntime、多台物理节点、公网和长时间内存界仍未证明，见[性能样本及边界](../testing/workflow-platform-acceptance.md#2026-11-12-当前功能候选基线)。

HK原db06完整进程480475仍运行，不重启原任务。后启7e关联浏览器组与其并行时，共享宿主891MiB物理内存、swap使用1066MiB，两个pytest均处于jbd2_log_wait_commit，实测memory PSI avg10 some95.49/full89.47、I/O some99.59/full93.01；该压力是环境事实，不能据此臆断所有失败根因。已对确认属于关联组508480的pytest子进程508498以pidfd发送SIGINT，保留原完整进程/宿主服务和所有证据；是否结束仍以进程与最终报告为准。最终7e完整Linux门禁进程509423按原任务和关联组的终态串行等待，未把等待状态当作测试执行或通过。原结果、资源诊断和信号记录在`final-review-alias-fix-20261112-bhjc67rf/`；没有推送、部署或生产配置变更。

## 重叠会话检查丢失根因修复（2026-11-12）

原Workflow账号切换浏览器的DOMContentLoaded超时现有可重跑故障链：拦住真实会话检查的旧账号响应，另一标签切换账号后触发第二次检查，原shell的`checking`分支直接返回，旧响应释放后无后续检查，原脚本10秒超时。最小改动将重叠且已到期/显式强制的请求合并为一次后续重查，过时成功或失败响应不挂载旧视图；普通未到60秒的focus保留原节流。原故障脚本修后通过，三个场景（正常/旧成功/旧401）先3passed。

随后增加“中途也不得经过/login”断言，旧401场景确定1failed：共享HTTP客户端在shell catch之前先发全局登录失效事件，旧响应仍触发导航。现在只让本shell的会话检查延后401事件并自行裁决，其他业务请求和默认getOperatorSession仍沿用原全局事件；相同失败项及客户端/operator组合118passed。旧初版3passed会漏掉中途跳转，保留原报告，不拿它作为最终修复证据。新增两种浏览器实例仍由原WF-08必需selector覆盖；完整Linux当前db06冻结没有这些后续改动，需对最终候选另验收。原日志/夹具、red/green和报告在`/Users/mango/.cache/agent-fleet/verification/final-review-alias-fix-20261112-bhjc67rf/session-*`。验证码偶发超时仍未定因，不以延长等待代替修复。

## 真实写锁竞争与调度分页复核（2026-11-12）

在`db06fbc7`冻结上补齐真实独立进程写锁竞争：macOS/Linux各20组，子进程持有`BEGIN IMMEDIATE`时原Workflow读取继续，随后写入通过原repository事务等待并提交。竞争入口P95分别184.929/180.562ms、并发读取P95为0.874/1.752ms；每端40条写入、原记录不变、完整性/外键和源码摘要检查通过。测量包括连接/锁获取/Python开销，不称纯SQLite内部等待；Linux与完整测试共享宿主，未测多worker饱和或生产容量。原始逐样本与边界见[性能记录](../testing/workflow-platform-acceptance.md#2026-11-12-当前功能候选基线)。独立Node RSS、真实网络传输、模型用量和人工介入/返工率仍开放。

调度分页原5秒端到端断言在每owner仅增加40ms延迟时确定失败：完成前100owner后测试停止后台线程，未给第101owner下一轮机会。改为原stop_event支持的可控轮询批次，同真实库逐项核验前100及第101owner执行/最终trigger成功，20秒仅用于线程失活保护；不改产品调度器。故意忽略分页cursor时新测试正确失败；相同40ms注入在macOS完成101owner/5.261秒、Linux完成101owner/15.126秒，两模块各35passed。本机裸Python3.10环境因缺Flask collection失败保留，不充当产品失败；Linux真实Python3.10两模块35passed/31.98秒。证据`final-review-alias-fix-20261112-bhjc67rf/schedule-*`。这是测试可靠性修复，不能据此断言旧全量调度超时只有一个原因；运行中的db06冻结未改写。

## 固定测试共享用户进程预算根因修复（2026-11-12）

本轮候选冻结95b51985的完整macOS实际7423passed/11Linux平台skip/204subtests，1217源码前后摘要一致且clean；43/46journeys，严格exit1，原verifier拒绝journey_incomplete。旧Linux15b439c任务已核对终态：7350passed/40failed/0skip，源码不变，原报告复制保留。产物夹具在宿主umask0002下为664，符合现有拒绝合同；固定022后上传与接收通过。独立固定测试仍3failed/5passed，实际stderr误报libseccomp缺失；追查确认原SandboxBackend在用户namespace之前设置RLIMIT_NPROC32，同用户已有51线程使find_library无法fork。库实际已安装，对照128成功。

新增真实回归同时持有80宿主线程，先1failed；后端默认不施加整用户NPROC限制（独占账号仍可显式配置），Linux原launcher在新用户namespace内执行前设置32上限。内测实际fork至少一次、达到上限后errno11、清理所有子进程，原Run发布JUnit及passed证据。修复冻结d168dc29（1219文件）：Linux五模块65passed/0skip；macOS七模块124passed/6平台skip；Windows适用9模块+3容量实例151passed/1POSIXskip，源码不变。Windows首个较广错误选择72failed/6errors、CI四个进程模块因同名tests包遮蔽的collection错误保留，151通过只证明后续适用子集，不是完整Windows门禁。

原packagers已生成1140后端/70前端文件，逐文件与冻结hash一致，包含新媒体模块/修复launcher，包6826915bytes。完整Linux/browser在同一修复冻结、既有Python3.10.12/Playwright1.62/Chromium1234环境运行，原严格门禁不放宽；尚未终态。无新下载、生产变更或开发分支提交/推送。证据`/Users/mango/.cache/agent-fleet/verification/process-budget-fix-20261112-yaib9xd2/`，具体范围及运行句柄见[进程预算计划](../superpowers/plans/2026-11-12-sandbox-process-budget.md)。WF-01新增压力selector在冻结后登记，后续最终矩阵需独立核对。

后续四处测试辅助导入统一到原`support.processes`约定，Windows原collection错误集合41passed/1POSIXskip，macOS42passed；新增tests包标记曾破坏正常collection，已移除，正式7435实例/2496selector收集无缺项。部署receiver三个测试另复现SIGINT/SIGTERM/SIGALRM处理器泄漏，测试finalizer恢复后完整部署模块38passed且原处理器逐项不变。两项均为冻结后测试夹具修正，不改生产receiver；当前Linux运行的旧测试快照仍保持不变，须单独验收最终矩阵。

Linux d168dc29全量现已终态：7431passed/4failed/0skip/204subtests，4452.09秒，42/46journeys，原verifier拒绝journey_incomplete，源码前后不变。captcha响应/调度器/Workflow DOM三个超时单独重跑3passed，不能记为根因已修。第四项原review sandbox稳定失败，确认新namespace exec原始字符串比较拒绝同Python的合法解析路径；改为strict resolved身份比较后，新db06fbc7冻结Linux原review+fixed+80线程压力4passed，本机关联158passed/3skip。最终db06fbc7完整Linux/browser已新启，PID480475，尚未终态；原失败和细节见[预算计划](../superpowers/plans/2026-11-12-sandbox-process-budget.md#full-linux-result-and-corrected-reviewer--2026-11-12)。禁止将局部结果拼作完整发布通过。

## 实际使用部署判断复核（2026-11-12）

本轮重新执行`git ls-remote origin refs/heads/main`，远端main仍为`301e259000a7d1027181ee716b29d5dd5fa9b951`；公网manifest精确一致，`healthz`返回`{"status":"ok"}`。HK `hermes-gateway`以mango用户只读检查的`RELEASE_ORIGIN`同为该SHA/CI42540826668，`importlib.util.find_spec("jsonschema")`仍为None。这证明旧线上服务健康，但新版Workflow运行依赖尚未补齐；本功能分支和未提交图片输入切片均未发布。

首批实际使用的范围可限定为一个模型、一个新鲜Node和一条完整任务。部署前仍需固定候选并通过完整Linux/browser回归、更新receiver并补齐运行依赖；切换前后按专用workspace验证真实任务终态、产物下载及重启恢复。原完整macOS运行7369 passed/10 failed/11平台skip在下述新诊断中未复现，但根因尚未证明，不用一次通过覆盖旧故障。安全专项后置；上述为功能与运行可靠性条件。本轮无推送、合并、部署或生产配置变更。

## 全量连接失败诊断续验（2026-11-12）

原测试树启用已安装Chrome及临时urllib opener观测插件，执行`pytest tests -q -x`，结果**7405 passed、11平台skip、204 subtests passed、零failure/error，1246.58秒，退出0**。原Komari、rollback、Runner十项连接失败本轮均通过；默认opener在full_flow首次建立，记录的loopback bypass为true，未观察到后续全局opener/代理状态变更。先前“临时代理污染全局opener”仍只是未证实假设，未据此修改传输代码。原58项组合排查也通过；单次不复现不能宣称间歇故障已根治。

该完整命令在测试收集后仍有媒体测试/派发错误转换和文档变动，故为诊断证据，不是同源冻结发布证据。最终媒体关联13模块在两种Python各402 passed，最终7434实例的收集比诊断时7416实例多18项；不拼成新的全量通过数。报告、日志、opener插件/记录及摘要保存在`/Users/mango/.cache/agent-fleet/verification/native-media-boundaries-20261112-neh4d9jp/`，见[计划与验收范围](../superpowers/plans/2026-11-12-native-binary-media-inputs.md)。随后curl复核公网healthz仍ok，manifest仍301e259，无部署变更。

## Native图片输入兼容与恢复复核（2026-11-12）

有界图片输入本机实现采用schema5/reader13、显式可信target和provider图片能力；仅支持PNG/JPEG/WebP，不保存base64。修复旧文本引用顺序丢失、读图期间执行状态变更后仍创建provider、图片附到历史首条user消息等问题后，原关联集合268 passed。新增原worker→OpenAI-compatible捕获HTTP请求体的三种MIME验证，以及pending恢复不重读/不重发验证。后续先复现两个兼容/manifest错误（2 failed/21 passed），分别将新增flag移至旧位置参数之后、严格检查manifest整数size；扩大复验的1 failed/273 passed为新夹具target/node不一致，修正后原两项通过。另先复现provider错误诊断丢失（1 failed/2 passed），补齐已知错误码。

较早13模块回归为383 passed/43.11秒。续接边界测试先复现不支持的代码引用泄漏领域异常（1 failed/28 passed），现于派发层转换为确定的WorkflowDispatchError，且没有文件读取或Run副作用。补齐重复引用一次物理读取/保留逻辑part、4引用与4MiB总预算、reader1/8/9/10/11/12降级、MIME/flag/ref/missing Run历史损坏及明文/加密恢复。新增历史夹具的导入与损坏注入方式错误单独保留，不记为产品缺陷。最终Python3.10/3.14四模块各**159 passed**，扩大13模块各**402 passed**（105.57/108.58秒），零skip/失败；WF-06追加6个selector，46 journeys/2495 selector entries/7434实例收集无缺项，Ruff与UTF-8无BOM检查通过。这是dirty工作树本机子集，未冻结、未执行真实vision服务，不代替全量发布门禁。Linux、同源完整回归和生产真实任务验收仍开放；原报告与范围见[图片输入计划](../superpowers/plans/2026-11-12-native-binary-media-inputs.md)。全项目TODO保持开放。

## 当前功能候选性能基线（2026-11-12）

对本地提交`15b439c66aab540eb2f53dced80f5000bab86a9d`串行运行现有F6容量探针：1000流程列表/详情/事件页P95分别为1.837/1.477/1.666ms；32个活跃流程、32个持久Run的协调tick为P50 267.300ms、P95 477.237ms、最大478.031ms；4路×64MiB loopback上传为256MiB/3.604s、71.022MiB/s，RSS峰值增量11.422MiB，四份产物SHA-256独立复核一致。4路×8MiB上传RSS增量9.594MiB。协调测量覆盖3429次`BEGIN IMMEDIATE/EXCLUSIVE`调用，调用耗时P50/P95/最大为1.250/95.179/463.110ms；此值包含SQLite锁获取与Python开销，但没有人为制造竞争锁，不能作为纯锁等待值。环境为Apple M5/16GiB/macOS 26.6.2/Python3.14.8/SQLite3.53.4；四份报告均绑定同一源码清单SHA-256`69c91557c79b87cce7ad14004c5f1f6d9c886a7991d4ef9d6409d65044bc17b4`及1213个文件，运行期间源码未变化，工作树dirty状态明确保留。原始数据见[性能验收记录](../testing/workflow-platform-acceptance.md#2026-11-12-当前功能候选基线)。

这是本机、同进程/loopback样本，不是生产容量声明。F6的实际竞争锁等待、独立Node进程RSS/远端吞吐、真实模型用量、返工及人工介入仍开放；全量发布门禁和生产任务恢复也未闭合。

## 全量回归失败复核与正式收集范围（2026-11-12）

上一份同SHA全量macOS报告仍为**7369 passed / 10 failed / 11平台skip**，旅程核验不完整，不能记作通过。将Komari loopback、回滚恢复listener及Runner loopback三个失败模块/用例按同一进程串行独立重跑，**27 passed**；这排除了稳定的单用例失败，但没有证明完整运行中的间歇拒绝连接根因，因此原全量失败及当前发布阻断保持不变。另一次从仓库根目录默认执行pytest收集，误把两个兼容`*_test.py`文件包含进来，因相同模块basename触发import mismatch；按正式命令范围`python3 -m pytest tests --collect-only -q`重新收集为**7390项、退出0**。这项调用范围错误不是产品缺陷，也不覆盖行为门禁。

## 发布前浏览器等待与外部能力顺序修复（2026-11-12）

`f0414be` dirty 工作树的完整浏览器门禁原结果为7362 passed、10 failed、11 Darwin/Linux条件skip、204 subtests，41/46 journeys；10项均卡在截图的`document.fonts.ready`。独立Chrome对照证明：挂起的async验证码脚本让document保持interactive、fonts状态虽loaded但ready Promise不完成；移除脚本后ready和截图均完成。现在壳层不再无条件加载第三方验证码；注册/重置按配置挂载Turnstile，hCaptcha在连续请求/服务端要求时挂载。两者共用有界加载、错误/超时重试、表单销毁与延迟回调隔离；未完成挑战的本地提示不发送请求或重置挑战。原身份生命周期夹具继续挂起意外的验证码请求，并明确断言认证页面零请求。

新增四配置回归修复前4 failed。首次实现扩大回归27 passed/3 failed，失败为新verifier替身不接收`remote_ip`参数，原报告保留；修正替身后4 passed，再扩展到5个错误/超时实例。截图开启的浏览器、账号、验证码、前端、打包、架构、门禁10模块191 passed/0skip，包括原10个失败场景；最后保留挑战修正后的账号/门禁29 passed/0skip。24张PNG完整解码校验通过，并抽查工作流截图。没有取消截图或放宽等待上限。

另复现外部能力集合按不同顺序声明时，target snapshot排序而dataclass equality未排序，实际首次派发报`dispatch_conflict`（快照与派发2 failed）。在能力对象构造时统一顺序，保持旧canonical bytes和空能力快照不变；6个外部执行/持久化/产物模块281 passed/0skip，新增回归证明重启换序仍恢复原binding、仅发送一次。新增账号和能力selector已接入AUTH-01/WF-06矩阵。

证据：`/Users/mango/.cache/agent-fleet/verification/browser-capability-fixes-20261112-9gfvg7xo/verified-summary.json`及同目录原始失败/通过JUnit、日志、截图。这些是本地关联验证，不能改写原全量失败，也不是同SHA完整Linux/browser发布通过。HK宿主只读检查发现已有Chromium1234缓存，`chrome --version`成功；宿主Python尚缺pytest/Flask/websocket、未发现现成Playwright模块，继续核验隔离测试环境。真实生产任务、同版发布门禁和其余平台TODO保持开放；没有推送、合并或生产变更。

## 外部 Agent 能力要求闭环（2026-11-12）

外部 Agent 合同的 `capability_requirements` 已接入已登记 target 的能力快照：每项要求必须匹配 adapter 显式声明的 `id/revision`，不匹配在 admission 事务中以 `external_capability_unavailable` 拒绝，绝不静默忽略或降级为无要求发送。能力支持随 target/configuration digest 固定并参与原 target snapshot；旧的六字段 target snapshot 仍可读取，空能力列表保持原 canonical bytes。Muse 默认没有声明外部能力，因此现有 Muse 登记行为不变。外部 adapter、Workflow 持久化、文本/Memory/输出回归共 **427 passed**，兼容修正后的核心外部集合 **101 passed**；这些是本地受控证据，不代表真实外部账号或生产部署已验收。

## 功能候选本地提交（2026-11-12）

已在功能分支建立本地提交 当前功能分支提交（见 HEAD）（`feat: complete workflow platform functional slices`），包含489个文件、全部Workflow/F4/F5功能切片及对应测试/文档。没有推送、合并或部署。该提交相对47bcd3b5测试冻结只增加验收文档和5个测试文件末尾空白清理；因此先执行边界、启动、配置、运行依赖和发布布局专项 **163 passed、2 subtests、12.28s**，CI精确Ruff选择、diff空白检查通过。全量报告仍绑定47bcd3b5，不能把本地提交直接标为新的完整release证据；若要发布该Git提交，需在CI重新产生同SHA报告。

## 生产只读配置与运行依赖核验（2026-11-12）

在 HK `hermes-gateway` 容器内以 `mango` 用户执行只读探针：现网 `RELEASE_ORIGIN` 仍为 `301e259000a7d1027181ee716b29d5dd5fa9b951` / CI `42540826668`；健康服务未改动。现网 `cryptography/flask/websocket/yaml` 可导入，但 **`jsonschema` 缺失**。候选 receiver 已将 `jsonschema` 纳入部署前依赖检查，因此在更新宿主 receiver/运行环境前不能切换候选包。当前平台总开关和 worker scheduler 开启，Workflow/dispatch/remote/capability/artifact/MCP/Muse gate 均未配置；三个 Workflow/MCP/Muse 登记文件均不存在。现有四模型均 enabled，三个 workspace enabled，两个节点记录虽标 online 但最后心跳已分别约 4.0 天、4.25 天前，不能作为当前可用节点证据。只读探针记录于`boundary-release-20261112-l3nrsd6i/production-readonly.json`，`production_mutated=false`。

下一步上线前置条件：先按 receiver 独立更新流程安装候选四脚本并在容器运行时补齐 `jsonschema`，再准备一个专用 workspace、一个新鲜 Node、一个模型和登记文件，执行真实任务/产物下载/重启恢复验收。

## 修复候选发布包组合验证（2026-11-12）

新冻结`47bcd3b56620733d2537189539f8f833eeca7f18`（1211文件）同12模块：macOS252 passed/2subtests/0skip，Linux251 passed/1浏览器环境skip/2subtests；两端源码前后不变且checkout clean。原packager打出的1134后端文件和69前端文件逐字节核对；实际解包后的journal/MCP CLI及正常Hub双集成装配、关闭发送后的恢复挂载在macOS/Linux均通过，零外部请求、零任务。原receiver对真实组合包build/extract/validate在两端通过，测试来源明确为local-verification-unpublished，未部署。详见[Muse计划发布验证](../superpowers/plans/2026-11-12-muse-startup-registration.md#integrated-release-packaging-verification--2026-11-12)，证据`boundary-release-20261112-l3nrsd6i/verified-summary.json`。

原`6c99ffdd` macOS完整回归终态为7369 passed/1已定位边界failure/11 Linux专用skip/204subtests；43/46 journeys，46 PNG完整校验通过，原核验器仍拒绝journey_incomplete。原Linux完整终态7331 passed/1同类边界failure/49浏览器环境skip/204subtests、30/46 journeys，五份原报告复制SHA一致，源码前后clean；核验器同样拒绝。修复候选macOS完整原范围已终态7370 passed/0 failure/11 Linux专用skip/204subtests，46 PNG有效；Darwin条件下NODE-01、RELEASE-01、WF-01不完整，原核验拒绝。Linux完整候选终态7332 passed/0 failure/49浏览器环境skip/204subtests；与macOS候选均无测试失败，但严格核验仍因平台条件导致journey_incomplete（macOS缺NODE-01/RELEASE-01/WF-01，Linux缺浏览器相关16条）。不能将其写成完整发布门禁通过。矩阵仍46 journeys/2466selectors/7381收集实例，Python3.10语法和UTF-8无BOM检查通过。生产版本仍旧版，完整新门禁与真实目标配置/任务/下载/恢复继续开放。

## 实际使用前状态复核与全量回归发现（2026-11-12）

本次重新核验公网首页、登录页、助手页、healthz、manifest均200，匿名`/api/operator/session`401；manifest与GitHub main仍为`301e259000a7d1027181ee716b29d5dd5fa9b951`，main CI42540826668为success。当前功能修复未提交/推送/发布，重新部署main不能取得本轮新增功能。最初urllib探测403在curl重查中未复现，不能据此报告线上故障。

同冻结`6c99ffdd`两端完整回归仍运行；均出现原架构边界失败，独立复现确认Muse启动loader/CLI直接导入tools。已改为由现有connector构造原bridge、共享异常从fleet_core导入，未放松边界测试。原检查1failed留档；相关8模块170 passed/2subtests/0skip，最终导入排序后4模块81 passed/0skip。详见[Muse计划后续记录](../superpowers/plans/2026-11-12-muse-startup-registration.md#full-regression-dependency-boundary-follow-up--2026-11-12)，证据`full-integrations-20261112-dveensic/`。正在运行的原全量只验证旧冻结，不能改记为本次修复后的完整通过。

当前结论：旧线上服务可访问；本轮新版仍未完成发布验收，尚不作为日常使用版本交付。首批实际使用聚焦一个模型、一个执行节点和一条真实任务；固定版本回归、receiver预检更新、目标配置、真实任务/产物下载/重启恢复需闭合，安全专项后置。无生产变更或大软件下载。

## Muse 正常启动与恢复入口（2026-11-12）

补齐原Muse适配器的正常Hub文件登记，以及`python -m hub.muse_setup`离线预览/显式journal初始化；模板、路由和适配器复用原执行链，可与coding-delivery正常配置共存。启动校验原journal身份，不访问浏览器；外部发送gate关闭后仍挂载原适配器用于恢复，新流程不发送。受控生命周期证明一次发送、原token/binding重启查回、agent_reported完成且acceptance pending，取消/导出仍unsupported。原3项入口回归均失败；扩展回归额外抓到CLI损坏SQLite异常和装配期间journal变化，均先失败后修复。

冻结`6c99ffddcc0b8029e12faf7103dd75ff3a5a0330`，1211文件/7610243字节bundle：macOS启用已安装Chrome的7模块**200 passed/0skip，10.25秒**；Linux同7模块**199 passed/1浏览器环境skip，40.88秒**，源文件前后摘要不变且checkout clean。未设置浏览器环境的本机199/1及夹具错误的197/1failed/1skip报告分别保留。新增11个WF-06 selector/33实例在Linux全部通过；完整矩阵46 journeys/2466 selectors/7381实例收集无缺项。本机配置/门禁/发布5模块另**191 passed、2 subtests passed**。文档生成示例已实际生成配置并用原loader解析，无浏览器I/O；Linux原Workflow与journal独立完整性/唯一记录审计保留，复制报告与数据库逐项SHA核对。

使用方法见[Muse启动登记](workflow-platform/muse-startup.md)，实施和证据见[计划](../superpowers/plans/2026-11-12-muse-startup-registration.md)。本入口只支持Hub本机127.0.0.1:9222；真实账号/浏览器、断网休眠、远程Node、其他context、完整Linux/browser及生产任务验收继续开放，本轮未提交/推送/部署。

## Fleet MCP 身份运维入口（2026-11-12）

新增受信摘要配置及`python -m hub.integration_setup`，默认离线预览，显式apply只登记一个owner/integration复合键到已初始化Workflow的原库，正常Hub每次请求读取该持久权威。原两项入口回归均失败，实施后扩展至7 selector/27实例；macOS和冻结`08fc115abecac80f9378f2209d3d1a94a6fdcba9`（1206文件）的Linux关联7模块各**270 passed/0skip**，Linux96.49秒，源码前后不变且checkout clean。真实HTTPS验证CLI登记的身份可鉴权；原快照重复、撤销、restore_disabled、scope/token冲突、缺库与缺失/禁用工作区均覆盖。本机配置/journey/release证据/打包4模块另**183 passed、2 subtests passed**。

同一Linux快照的独立组合探针经真实Workflow bootstrap CLI、正常Hub、身份CLI和HTTPS调用验证：200000/50单步预算对三步模板返回403且零Workflow；600000/150可创建并幂等查回同一Workflow、get和空产物列表，**2 passed**，未执行任务。最初探针把身份apply放在Hub初始化前，两项均按合同拒绝；更正顺序的原新报告独立保留。文档示例现按三步合计预算给值。完整矩阵46 journeys/2455 selectors/7348实例可收集，新增7 selector/27实例在Linux报告全部通过；不是完整WF-06或Linux/browser发布通过。用法：[身份登记](workflow-platform/integration-setup.md)；计划及原报告：[运维入口计划](../superpowers/plans/2026-11-12-integration-identity-setup.md)。真实客户端账号/生产执行、其余配置UI、完整发布及其余项目TODO继续开放，无提交/推送/部署。

## MCP 正式启动入口（2026-11-12）

确认原正常Hub只能通过Python依赖注入挂载MCP适配器；新增受信文件配置和显式发现CLI，复用原持久目录、环境SecretBroker和HTTPS transport。启动只校验并挂载，不解析token/请求服务；默认CLI只预览，显式指定现有数据库和单connector才发现。首次active、变更pending、原Run pins、撤销与重启语义保持。原新回归5 failed/1 passed，修复后macOS关联8模块**325 passed、2 subtests passed**；独立冻结`568a56421e758c00898ac0aee7768ea72ee05024`（1201文件）Linux同8模块**325 passed、2 subtests passed、0skip，95.60秒**，前后源码摘要不变、checkout clean。本机另5模块配置/门禁/发布/运行依赖**207 passed**。

新增8个必需WF-06 selector，完整清单46 journeys/2448 selectors均有收集实例，7321实例仅完成收集；Linux关联报告中新增8 selector的35实例全部passed，但WF-06其他浏览器/外部范围未完整执行，不宣称整条journey或完整发布通过。第一次收集命令误用仓库根而非`tests`导致兼容模块被当测试导入，保留原日志；改用正式`pytest tests --collect-only`后退出0。部署使用方法见[MCP启动入口](workflow-platform/mcp-startup.md)，实施与证据见[计划](../superpowers/plans/2026-11-12-mcp-startup-registration.md)。真实外部MCP/账号策略、其余配置UI、完整Linux/browser及生产任务验收继续开放；本轮未提交、推送、合并或部署。

## 文件交接与能力路由验收（2026-11-12）

WF-04/05逐项要求已映射到原上传/恢复/收发/容量/选点/协调/Node断言；登记两个独立必需journey，原完整Linux/browser门禁保持不变。选点原permutation用例未让占用独立决定结果，已新增三个参数分别证明priority先于占用、占用先于node_id及node_id平局顺序，产品排序未变。原产品冻结49a83f23的Linux18模块707 passed/0skip、Windows原生133 passed/0skip、实际Windows→Hub→Linux交接及两端自动生命周期/独立postcheck控制21 passed/0skip；二十份原Hub数据库及两端持久journal/file另行审计。

新清单冻结`bd2f0ea0e28e20812ea57f5f420c827f2c45ca7e`（1196文件）在Linux按原插件执行WF-04/05专项`--require-journeys`：**567 passed、零failure/error/skip、2/2 scoped journeys、263 selectors全部参数通过，215.26秒**。完整清单46 journeys/2440 selectors均可解析，7286实例仅完成收集；不回写旧44-journey全仓报告，也不将两流程专项当完整发布门禁。新冻结与实际两端试验冻结的产品源码逐项相同，只有清单、测试和文档变化。本机路由/门禁/发布证据回归191 passed，清除测试旧未使用import后路由57 passed；排序mutation验证确认新参数能拒绝原测试遗漏的两类错误排序。两端106文件/910132 bytes及交接8文件/83582 bytes的归档与成员摘要全部核对，源码保持clean。

逐项映射、命令和证据见[WF-04/05计划](../superpowers/plans/2026-11-12-file-routing-acceptance.md)。生产配置、完整同版Linux/browser和项目其余TODO仍开放，未提交、推送、合并或部署。

## 上线准备复核（2026-11-12）

本次公网只读复核：首页、登录、助手、healthz与manifest均200，匿名会话401；manifest和GitHub main仍为`301e259000a7d1027181ee716b29d5dd5fa9b951`，最近main CI 42540826668成功。本轮功能仍在`codex/functional-root-fixes`的未提交工作树，未推送、合并或部署；从GitHub main重新部署不能取得这些修复。首批建议Linux Hub、一个固定测试节点和一个模型。当前是试用候选，正式切换仍需固定发布版本的完整Linux/browser门禁、宿主receiver依赖预检更新、目标Hub/Node/model配置与真实任务/产物下载/恢复验收；未启用的外部集成不要求在首批全部上线，安全专项继续后置。

证据根目录：`/Users/mango/.cache/agent-fleet/verification/publication-staging-20261112-3i0_14ep/`，公网结果见`readiness-live.json`。完整Linux/browser所缺Chromium195836009 bytes及libllvm15 23115156 bytes尚无用户大文件下载批准，均未下载；这不影响已完成的关联验收，也不能把关联结果当完整发布门禁。

## 交付暂存文件修复（2026-11-12，关联验收完成）

Windows上一轮三项专用消费者失败继续下钻后，确认并非只应跳过的夹具限制：`WorkflowCodingDeliveryService`源码上传与报告发布均把仍打开的`NamedTemporaryFile`交给ArtifactStore按路径重读，Windows因此PermissionError。新增五项真实发布/失败清理/原回执重试用例在原Windows冻结全部失败；现改为私有TemporaryDirectory中的普通文件，写句柄关闭后才执行原store I/O，异步owned I/O结束前保留目录，三项临时平台跳过已移除。此结论更正下方历史记录把全部三项视为POSIX夹具限制的判断；原失败报告不变。

修复冻结`49a83f2313a0fed3c1139813065abd7b4b33c4c1`（1195文件）：macOS关联8模块235 passed；同源Linux28模块与11必需实例1027 passed/0skip/436.41秒；Windows18模块736 passed/1 failed/0skip，源码前后摘要不变、checkout clean。Windows剩余一项是远程接收夹具未为输入/接收journal创建私有DACL目录，接收器正确拒绝能力；复用原两节点私有目录创建方式修正夹具后，独立Windows原Native/source消费者和五项发布回归46 passed/0skip，macOS加完整远程执行模块66 passed/0skip。后两项运行只多改一个测试文件，不能回写原737项报告为全绿。两端原报告与复制摘要均核对，原Windows五项发布失败也已归档。计划及证据：[发布暂存修复](../superpowers/plans/2026-11-12-workflow-publication-staging.md)。本轮无新的完整Linux/browser通过报告，生产配置与全项目剩余TODO继续开放。

## 最新修复后验收（2026-11-12）

独立冻结 `5fe6bbb2b55ac8a728dcd2e9f7173840c8d31d6e`（1193文件）完整macOS Python3.14/Playwright1.63/Chrome验收：**7267 passed、0 failure/error、11 Linux-only skips、204 passing subtests**，946.79秒；44 journeys/2175 selectors均解析，41条通过，46张PNG完整核验。原release verifier仍退出1/`journey_incomplete`，缺NODE-01、RELEASE-01、WF-01的Linux实例。本次全仓已经执行本文下方133项产品回归、两项feature断言与原usage浏览器，不能保留“修复后未复验”的现状判断。

同冻结原生Linux25模块及11个Linux必需实例合计 **963 passed/0skip/426.60秒**。Windows同16模块 **709 passed/3新POSIX Hub夹具failed**；该平台不支持这些Hub夹具的临时文件共享，显式平台标记后Native代码模块38 passed/3 POSIX skips，原Windows报告不回写。最新工作树的源码/Native集合80 passed；产品源码与冻结一致，仅测试平台标记及后补文档不同。129 Run × 16引用的页边界/损坏/明文加密恢复探针已在修正冻结重跑通过。后端1115/前端69文件逐项hash匹配，候选包6750407 bytes；生产receiver只读SHA核对仍仅缺jsonschema预检，已准备未安装更新包。

证据：[verified-summary.json](/Users/mango/.cache/agent-fleet/verification/source-consumer-fix-20261112-tyf7lkdc/verified-summary.json)。完整同版Linux/browser尚未通过；本机全仓与Linux子集不能拼接。Linux现有Playwright1.63但无浏览器，HEAD确认Chromium195836009 bytes、依赖libllvm15 23115156 bytes，用户AGENTS大文件下载批准仍缺，均未下载。生产配置/真实任务、其余F5/完整WF-04/05等全局TODO继续开放，无提交/推送/部署。

## 当前候选核验（2026-11-12）

首个Native代码输入冻结 `af633263` 完整本机/browser结果为 **7127 passed、136 failed、11 Linux-only skipped、204 passing subtests**。其中133项来自新增Native code opt-in误伤原独立评审/固定测试/远程测试的源码引用；现已把校验限定到Native输入目标，原专用消费者保留自身合同。另两项是summary UI加入后的旧功能清单断言，已更新；一项usage页面原30秒导航超时，原浏览器单项fresh 3.91秒通过，未改超时/断言。修复后的完整冻结仍须独立复验，不能上线或把旧7201-pass视为覆盖本次变更。

相同首个冻结Linux关联16模块 **708 passed/0skip**；Windows **706 passed/2 POSIX执行位断言failed**。执行位夹具改为显式固定Git 100755、所有平台核对tar/metadata mode及解包字节、POSIX额外核对文件系统执行位后，macOS/Windows源码模块各39 passed，独立留证。新Native opt-in/专用消费者与身份管理集合60 passed；此前广范围修复复验278 passed/3项新增测试SQL查询错误，查询已更正，不能将该次当全绿。

独立原派发129 Run × 16引用探针确认128行代码历史页及末页、2064引用、两页边界10类损坏拒绝，明文/加密frozen恢复均保持原配置/引用/reader12与SQLite完整性。原打包器1115后端/69前端文件逐项hash一致；这些仅属于首个冻结的未发布候选。全部原失败、平台报告与探针见[Native代码包计划](../superpowers/plans/2026-11-11-native-code-package-inputs.md#full-gate-findings-and-extended-verification--2026-11-12)；完整Linux/browser及生产实际任务仍开放，无提交/推送/部署。

## 当前部署判断（2026-11-11）

最新部署准备核验（19:42 SGT）：GitHub main 与公网 manifest 仍为 `301e259`，healthz、首页、登录页、助手页均200；本轮功能未提交、推送或部署。宿主四个receiver脚本逐项SHA-256对照，三项与候选相同，仅 `auto_deploy_container.py` 缺少新增 `import jsonschema` 依赖预检；生产尚未更新该项。不能直接部署main或从未提交工作树打包交付本轮功能。

最新独立冻结 `90374763513e1b7878544d4d4256cfcab93268ad` 完整本机验收：**7201 passed、11 Linux-only skipped、204 passing subtests、零失败错误**，946.92秒；44 journeys/2154 selectors全部收集，41条通过，46张完整PNG核验。原release verifier仍退出1/`journey_incomplete`（NODE-01、RELEASE-01、WF-01）。同冻结Linux关联11模块261 passed；本机跳过的11个Linux必需实例另跑11 passed；Windows CI选择及新增回归238 passed/2 POSIX skips。三端1184源码文件摘要不变、checkout clean、远端报告复制hash一致。独立Linux补验不能与本机完整报告拼作完整Linux发布通过。证据：[verified-summary.json](/Users/mango/.cache/agent-fleet/verification/bound-dispatch-read-20261111-2k3dvekt/verified-summary.json)。

随后对同冻结在Linux验收机运行完整测试树：**7162 passed、50 skipped、零失败错误**，1711.23秒；44 journeys中28条完整通过、2154 selectors全部收集。48项跳过因未设置 `FLEET_PLAYWRIGHT_MODULE`（当前Linux无Chromium验收模块），另2项因 `rsync` 不可用。原release verifier退出1/`journey_incomplete`，列出16条不完整journey；没有截图证据。三端1184文件SHA与干净状态均保持。该运行比此前261模块和11项平台补验覆盖更广，但缺浏览器门禁，仍不能作为完整Linux release gate通过。详见[Linux完整运行报告](/Users/mango/.cache/agent-fleet/verification/bound-dispatch-read-20261111-2k3dvekt/linux/verified-summary.json)。

随后在同一冻结/Linux验收机安装小型Debian `rsync` 包（424700字节），重跑两个原先因缺二进制而跳过的完整测试模块 **8 passed/0 skipped**。这是独立补测，不回写全量JUnit的7162/50；远端JUnit复制SHA一致，1184源码保持不变、checkout clean。Linux全量门禁当前剩余48项Chromium浏览器跳过，journey覆盖仍28/44；摘要见[rsync补测证据](/Users/mango/.cache/agent-fleet/verification/bound-dispatch-read-20261111-2k3dvekt/linux/rsync-summary.json)。

Windows原生补验最初在两节点集成模块出现4项失败；证据定位为测试夹具把input与接收journal放在普通临时ACL根目录，Windows原生接收器按既有契约拒绝该配置。夹具现与Windows接收计划相同，创建当前用户私有DACL根目录并复用原目录核验ACL；没有放宽Node产品检查。最新同一Windows 62项检查点/暂停/容量/两节点集合 **62 passed/0 skipped**，Linux原两节点模块29 passed。保留修复前JUnit与重跑报告；Windows跨平台证据见[Windows复测摘要](/Users/mango/.cache/agent-fleet/verification/bound-dispatch-read-20261111-2k3dvekt/windows/windows-native-rerun2-summary.json)。这不替代Linux Chromium全量门禁或生产验收。

改正Windows接收夹具后，macOS Python 3.14 同一14模块检查点/暂停/容量/Workflow控制与两节点集合 **323 passed/0 skipped/65.91秒**；Windows该集合62项也全通过。macOS与Windows作用域分别留证，不合并成一项全平台计数。生产源码未因夹具修复而改变；测试文件SHA已与Windows执行副本核对一致。原Linux Chromium门禁及生产接受保持开放。

原生 Run 检查点、远程等待暂停与有界容量索引合同（见[执行契约 §5.1.1-5.1.2](../superpowers/specs/2026-10-20-workflow-execution-contract.md#511-原生-run-检查点与安全恢复)）现已补齐：8 MiB私有 lease-fenced checkpoint 只恢复已保存的安全阶段；provider/tool pending、旧 Run 与损坏检查点保持unknown。原 Node Command 等待保存相同命令身份和原 deadline，释放全局 worker lease，同时通过独立 workspace reservation 保留工作区容量；取消与 Node claim 使用原事务围栏，终态/备份恢复清除reservation。终态等待容量使用owner部分索引与 `(owner_id, trigger_message_id)` 索引。检查点15模块280 passed、额外兼容122 passed/2 subtests；远程等待10模块212 passed、另10模块集成287 passed；容量索引实际4000终态Run workload，SQLite VM progress counts为slot 0/candidate 400（每100指令采样），九模块190 passed并含真实Chrome历史。各计数/冻结分别保留，均不是替代最终完整Linux/browser或生产任务验收的发布证据。具体原始范围见[检查点计划](../superpowers/plans/2026-11-11-native-run-checkpoints.md)、[远程暂停计划](../superpowers/plans/2026-11-11-native-remote-suspension.md)和[容量索引计划](../superpowers/plans/2026-11-11-remote-wait-capacity-index.md)。

该冻结在先前批量租约释放基础上，让已绑定派发的轮询经过只读事务；未绑定操作进入新的写事务并重新检查epoch/revision/frozen/generation，避免以旧读快照写入。真实写锁先确定复现旧轮询失败；新回归验证可读、并发revision变更拒绝、现有围栏保持。两次串行Linux32流程tick P95为**494.353/498.105ms**（P50为442.946/435.392ms），写事务开始数3429，均仍为32个Run/32次逻辑执行。两次样本达到500ms初值但余量小，不代表生产容量。最新版完整Linux/browser、生产Hub/Node/model配置与上线后真实任务/恢复验收继续开放；首批范围建议Linux Hub、单固定测试节点、单模型。安全专项后置。

上一批协调器性能修复：Linux实际32流程tick P95为895.182ms，超出500ms初值。剖析及确定失败回归定位无变化owner队列写入与逐条租约释放提交；现队列值相同时不写，每轮至多32条租约单事务释放并逐条保留原围栏、每owner刷新一次，故障整批回滚。独立最终冻结 `ca45f26574df045c9f435e49ff83c088de4863d2` P95降为517.816ms（约42.2%），仍未达到500ms。macOS/Linux同十模块各229 passed，Windows CI选择及新增回归231 passed/2 POSIX skips；三端源码不变。新增5个必需selector/7实例，原较早7187-pass完整门禁不涵盖此产品改动。证据在 `/Users/mango/.cache/agent-fleet/verification/batch-claim-release-20261111-xojwqjub/`，实际读取/传输样本、边界及原失败见[容量测量](../testing/workflow-platform-acceptance.md#2026-11-11-linux-当前候选测量与协调器修复)。最新版完整Linux/browser及发布仍待执行。

部署回滚文档已按原实现与55项部署/回滚回归更正：schema标记变化时receiver会恢复部署前数据库，并把失败版本数据库及sidecar保留到 `failed-databases`；schema标记不变才保留新版在线写入。该检测不识别任意Workflow reader变化，宿主已安装版本也仍须独立核对；不据此宣称生产receiver已更新。见[恢复行为](../../deploy/auto-deploy.md#失败与恢复)。

最新完整门禁续验：先前未冻结工作树的 Python 3.13 全仓为7179 passed/2 failed/11 skipped，两项失败来自运行环境缺少 `websocket-client`；Python 3.14 全仓为7176 passed/5 failed/11 skipped，五项失败来自超深 JSON 在新解释器中可成功解码、继续进入业务路由。原报告分别保留在 `/Users/mango/.cache/agent-fleet/verification/current-20261111/` 和 `current-20261111-python314/`。现统一在请求解码后按128层容器深度拒绝，保留解码时的递归错误映射；该检查不构成解析前资源限制。数组/对象127、128、129层边界与关联路由在Python3.14五模块92 passed，Python3.13接管/深层JSON两模块39 passed。这些局部结果不拼作完整门禁通过。

修复源码独立冻结 `d7d846a347a71549740b116d3e7de9fd1c84bd39` 的本机完整门禁已结束：显式Python3.14.8、Playwright1.63.0与本机Chrome，**7187 passed/11 Linux-only skipped/204 passing subtests/零失败错误**，1196.26秒；41/44 journeys、2138 selectors、46完整PNG。NODE-01、RELEASE-01、WF-01只缺Linux必需实例，原发布核验器仍退出1/`journey_incomplete`。1184文件摘要前后不变、checkout clean，原打包器的1108后端/67前端源文件逐字节一致。完整证据位于 `/Users/mango/.cache/agent-fleet/verification/full-gate-depth-20261111-sh9rpq2k/`。首次启动误用目录下解析出的Python3.13，已中止并另留日志，不纳入本次计数。同版完整Linux/browser门禁仍缺获准的Chromium下载。

同一冻结的Windows CI选择为224 passed/2 POSIX-only skips；在同一次运行中额外加入接管、深层JSON和运行依赖三模块，扩展部分45 passed/17 failed。17项均在启用Supervisor的Hub夹具初始化时因Windows无 `os.getuid` 失败，尚未进入JSON解析；完整扩展运行实际为269 passed/17 failed/2 skipped，不能记为全绿。既有平台合同只证明Windows Node支持，不据此声称Windows Hub可用。源文件摘要不变、checkout clean，原JUnit及按范围分类记录均保留在 `windows/`。

完整门禁之后补登8个必需selector，覆盖部署helper逐项依赖预检、preflight/stage/verify拒绝，以及JSON层数边界与原路由错误映射。后补清单不改写上述2138-selector报告；下一次完整Linux门禁须使用包含新清单的固定快照。

同冻结Linux补验本机跳过的11个专用实例：首轮因SSH最小PATH未包含已安装bwrap目录得到6 failed/5 passed；补上既有工具路径后 **11 passed/0skip/7.17秒**，产品源码未修改，摘要不变且checkout clean。原失败和新报告分存 `linux-required-before/`、`linux-required/`；不与macOS报告拼成完整Linux门禁。后补矩阵现44 journeys/2146 selectors，7198实例收集且无缺失；三模块回归62 passed，门禁/发布证据/CI依赖三模块138 passed，diff检查通过。

Linux同版快照已复制至既有SG测试机私有目录 `full-gate-depth-d7d846a347a7`，1184文件逐项SHA-256一致、checkout clean，pytest/Flask/websocket-client/jsonschema均可加载。原完整pytest及发布核验runner已准备，尚未启动；需批准下载Chrome for Testing 153的195836009字节包及libllvm15的23115156字节依赖包。具体命令与未运行状态见同证据目录的 `linux-prepared.json`，未更改生产实例。

部署依赖预检修复：`jsonschema` 已是 Workflow 运行依赖，但仓库部署 helper 原先未检查该模块。隔离子进程明确屏蔽各模块并核对异常来源后，缺少 `jsonschema` 的直接检查、preflight/stage/verify 和正常加载共 5 项失败，其他 17 项通过；补充检查后，部署、运行依赖、pidfd 和发布布局回归共 106 passed、2 subtests passed。此修复仅在本地工作树；生产已安装的受限 receiver 独立管理，尚未更新，不能据此认为现网预检已修复。

现网 `/manifest.json` 和 GitHub main 均为 `301e259000a7d1027181ee716b29d5dd5fa9b951`；本次只读核验 `/healthz`、首页、登录页及助手页均为200，匿名会话为401，main CI `42540826668` 成功。本轮修复仍在本地功能工作树，未提交、推送或部署。后端打包脚本只归档 Git HEAD，不能直接在尚未提交改动的开发工作树中打包来交付这些修复。

新版可作为 **Linux Hub、单个固定测试节点、单模型的首批试用候选**；完整 Linux/browser 发布门禁、目标主机实际可达的模型 endpoint、Hub/Node 登记配置和部署后的真实任务恢复仍需闭合。既有真实 CLI 开发→独立评审→跨节点固定测试→产物下载试点可作为主链依据；F5/F6 其余扩展不冒记为已验收功能，安全专项仍按用户要求后置。

最新同源冻结 `3cc7485054186c53176435d0d95fe39a250c9637` 的 Linux 22模块 **545 passed/0skip**，完整 Windows CI 选择 **224 passed/2 POSIX skips**；两端源码不变、checkout clean、复制报告摘要一致。本机完整 **7158 passed/1 failed/11 Linux-only skips/204 subtests**，40/44 journeys、45完整PNG；原 release verifier 退出1/journey_incomplete。唯一失败是集成身份页面的 SPA 测试默认等待整页load，被可选第三方脚本拖住；故意挂起该脚本稳定复现后，等待改为domcontentloaded，三个相关真实浏览器模块 **25 passed/55.63s**。这是后续测试修复验证，不将其拼成新的完整门禁通过数。原全仓、确定性失败及后续回归均保留于 `/Users/mango/.cache/agent-fleet/verification/shared-test-node-20261111-48qvrrtz/`。

该冻结原发布脚本生成的1108个后端文件及67个前端源文件（另1个生成manifest）逐字节匹配冻结源码；源码保持不变。共享节点及Windows下载修复见[实施与证据](../superpowers/plans/2026-11-11-shared-test-node-registration.md)，正式模型评审器的[真实服务调用](../superpowers/plans/2026-11-11-deployable-model-reviewer.md#real-model-service-verification--2026-11-11)另有9.079秒验证，生产网络配置尚未代填通过。

> 2026-11-11 最新正式模型评审入口：新增仓库内置单次HTTP评审命令与Linux launcher，原Hub登记/Run worker/报告校验可直接使用；完整文本源码分片发送，不默默截断。`a2aed0eb`冻结1182文件，18模块macOS关联335 passed/6 Linux-only skips、Linux同集合341 passed/0skip，各2 subtests。原包1106后端/67前端源文件字节一致；44 journeys/2114 selectors/7148 cases仅收集核验。对端为受控HTTP，真实模型服务/生产配置、最终完整Linux/browser门禁与部署仍开放。见[正式评审入口证据](../superpowers/plans/2026-11-11-deployable-model-reviewer.md#verified-results--2026-11-11)及[配置方式](workflow-platform/startup-registration.md#仓库内置模型评审器linux)。

> 2026-11-11 最新固定测试部署入口：正式Linux launcher已可用于原Hub/Node配置；`2570449a`冻结1178文件，macOS关联230 passed/3 Linux-only skips、Linux同集合233 passed/0skip，各2 subtests。原包1102后端/67前端字节一致；44 journeys/2104 selectors/7107 cases仅完成收集核验。该版本完整Linux/browser发布门禁、生产模型评审配置与部署仍开放。见[入口与验证结果](../superpowers/plans/2026-11-11-linux-workflow-launcher.md#verified-results--2026-11-11)。

维护日期：2026-11-11。用户目标：按照文档完成所有开发。整体状态：进行中。

最新初始化与完整回归（2026-11-11）：同 manifest 的工作区/三会话初始化 CLI 已完成，空库双进程竞争定位为平台 additive migration 在写锁前检查列，现移入同一迁移事务；原三项确定失败已转绿，后续迁移失败同时回滚新增列。独立 `1d982fce` 冻结1175文件，本机完整7089 passed/零失败错误/6个Linux专用skip/204 subtests，42/44 journeys、2097 selectors、46完整PNG；原核验器仍拒绝journey_incomplete。Mac/Linux关联各281 passed/各1个不同原因skip，三端相同初始化/启动31项均通过，Linux原生进程79 passed/2 subtests；原CLI额外40组/80子进程竞争全通过。原包1099后端/67前端字节一致，三端源码不变、checkout clean。详见[初始化与迁移证据](../superpowers/plans/2026-11-11-workflow-bootstrap.md#verified-results--2026-11-11)。完整Linux/browser、生产配置和部署仍开放。

较早本机全仓：摘要/查询修复冻结 `4a3e15f186b5bcd2b303123012876681dd1a2076` 为7043 passed/零失败错误/5个Linux专用skip/204 subtests，43/44 journeys、2071 selectors、46完整PNG；1156源码不变、checkout clean，原发布核验仍拒绝journey_incomplete。后续Windows备份修复冻结 `b44fe1bc` 的三端相同96项摘要/备份集合分别通过，不能拼成新完整门禁。

最新真实业务切片：2026-11-11 私有 macOS 试点已通过真实 Codex CLI 开发→独立模型会话评审→原 sandbox 固定测试31项→5产物HTTP下载及摘要核验→退出/重登恢复；实际Chrome9222桌面/手机刷新与5产物页面下载通过。保留 `awaiting_acceptance`，人工接受未执行。所选模型 `newapi/gpt-6-luna`/`magpie`，同模型独立会话，不声称跨模型独立性。范围、原失败及证据见[真实CLI试点计划](../superpowers/plans/2026-11-11-real-workflow-cli-pilot.md)。

本账用于区分规格、已有实现、当前集成与最终验收。完成某个切片不代表完成整个目标。

最新启动配置切片（2026-11-11）：普通 Hub 已可从持久 manifest 登记模板与执行器；常驻 Node 原 CLI 已接显式能力/源码接收/固定测试配置和在途停止通知。独立 `feab24dd` 冻结1170文件在macOS关联240 passed/1 Linux-only skip、SG Linux同集合241 passed/0skip，Linux启动13项均通过。真实HTTP、签名接收和固定测试journal重启复用已覆盖；这不是生产sandbox或完整发布门禁。操作见[Hub登记](workflow-platform/startup-registration.md)、[Node配置](workflow-platform/node-startup.md)，原失败与证据见[Node计划](../superpowers/plans/2026-11-11-node-workflow-registration.md)。初始化既有workspace/conversation已由后续[初始化CLI](workflow-platform/startup-registration.md#工作区和会话初始化)完成；最终完整门禁和发布继续开放。
平台既有合同见[平台设计](agent-fleet-self-hosted-platform.md)。Workflow 的版本化规格已带入
[开发入口](workflow-platform/README.md)与[完整路线](../superpowers/plans/2026-10-20-workflow-platform-roadmap.md)。
已有实现来源为本机隔离工作树 `/Users/mango/.codex/worktrees/workflow-platform-docs/agent-fleet`。
源工作树保留原样；移植到本分支的内容必须重新验证，不能沿用源树历史测试计数。

## 固定范围与证据

| 要求 | 权威依据 | 当前目标树状态与完成证据 |
| --- | --- | --- |
| 上一轮六项功能修复 | 当前分支源码与既有回归 | 已实现，集成后重新执行关联与完整门禁 |
| 持续补位、公平调度、工作区容量一致 | 架构 review；平台 Durable Run Scheduling 合同 | 持续补位、忙工作区预过滤、同工作区容量和单调 epoch 已实现；已纳入本轮6887-pass全仓；局部集合不另行累计 |
| 远程执行截止、租约与回执一致 | Command/Run 合同；架构 review | 队列/执行/回执总截止、原始幂等期限、逐条claim和unknown不重放已实现；Node/F4能力关联496 passed，Hub能力/期限闭包543 passed；整合后重新验证；通用执行强制停止/持久模型步仍开放 |
| 工作区绑定、上下文预算与历史分页 | Workspace/Conversation 合同；架构 review | 显式workspace→backend映射已修；历史按32完整turn/64KiB，HTTP20turn分页及旧token索引恢复已修；新增10项及真实Chrome已纳入本轮全仓 |
| F0 固定试点身份与模板 | Workflow 路线 §2、验收矩阵 §1 | 当前私有macOS coding-delivery@1试点已登记真实CLI/配置摘要/模型、独立评审身份、源码及原sandbox；1159源码执行前后不变。此单试点不覆盖生产配置或其他执行器 |
| F1 身份、迁移、证据与完整合同 | Workflow 路线 §3、执行契约 | 已移植七个纯领域模块，五组原测试加当前边界 135 passed / 30 subtests；运行接线属于 F2 |
| F2 持久协调、派发、控制、预算、API、备份 | Workflow 路线 §4 | 持久协调/派发/控制/预算/备份已集成，关联242 passed/1环境skip；API+MCP+HTTP传输221 passed；历史引用保护89 passed、清理保护292 passed/1环境skip（集合重叠不累计）；F3接线后重验 |
| F3 固定版本开发、独立评审、固定测试、UI与人工接受 | Workflow 路线 §5、WF-01/03/08 | 原真实Codex开发、独立同模型会话评审、31项固定sandbox测试、5产物下载及HTTP/桌面/手机恢复已通过；人工接受未执行，生产隔离及其他执行器仍开放。历史848与142项测试分别保留，不与真实试点相加 |
| F4 分片产物、Node 收发、能力目录与容量 | Workflow 路线 §6、WF-04/05 | 目标树分片/收发、capability上报/选点/admission、远程固定测试及postcheck原子审计已接通；Node关联496 passed，Hub543 passed，输入/容量112 passed；集合重叠不累计，当前真实Windows/Linux两机未重验 |
| F5 只读 MCP、外部 Agent、窄出口、管理配置 | Workflow 路线 §7、WF-06 | 目录/Run固定授权、原任务恢复、完整文本/Memory、已发布ArtifactRef输出、窄MCP及管理UI已接；auth/HTTP/UI合同282 passed，管理浏览器14 passed；summary/二进制等扩展和真实试点仍开放 |
| F6 容量测量与后续扩展 | Workflow 路线 §8 | 当前树本机1000流程读取/32活跃协调/4×64MiB HTTP上传样本已记录，见验收矩阵§11；真实Node/模型与人工返工数据待测，DAG及新专业adapter遵循真实需求前提 |
| 功能后的安全加固 | 用户优先级与原合同 | 最后处理；保留已实现的协议与状态不变量 |
| 完整回归与逐要求交付审计 | 原 journeys、release evidence、Workflow acceptance | 检查点冻结06e87586为6921 passed/5平台skip/204 subtests，43/44 journeys、1995 selectors及46完整PNG；1127源码不变、checkout clean，原发布核验仍拒绝journey_incomplete；该冻结早于远程等待与索引改动，Linux/跨平台/真实试点开放 |

## 实施顺序

最新证据：[真实跨节点完整代码流程](../superpowers/plans/2026-11-11-real-cross-node-workflow.md)：原 macOS Runner 真实 Codex 开发、独立模型会话评审、Hub 自动选点及签名接收、SG Linux 原 bwrap 固定测试31项，均在同一 Workflow/subject 成功。三个 Run 成功，三条预约释放，5份 HTTP/Chrome 下载摘要一致，恢复后仍等待人工接受。原脚本预约计数断言失败留档，由独立审计验证业务结果；未重复调用模型或测试。仍需完整发布门禁和生产环境验收。

最新完成切片：[原生能力生命周期集成](../superpowers/plans/2026-11-11-native-capability-integration.md)，当前cd7019aa冻结补齐真实节点自动admission/Run/Command/postcheck与重启不重放的20场景证据。下一阶段仍按下方完整门禁、真实试点和后续功能范围推进；浏览器大文件下载尚未获准。

1. 修复已复现的执行协调问题，计划见[持续调度实施计划](../superpowers/plans/2026-11-10-continuous-run-scheduling.md)。
2. 将已有 Workflow 实现按依赖闭包移植到当前最新架构；逐批保持可运行，保留源树和现有修复。
3. 按原路线补齐未实现的功能，再执行真实 SQLite/HTTP/进程/浏览器恢复与文件证据验证。
4. 收集容量数据和外部验收；需要真实账号、发送、部署或超过 10 MB 软件下载时，先准备可审阅结果并遵守授权边界。
5. 完整核对每项规格、测试、门禁、交付物及未决项；全部证据成立后才将总体目标标记完成。

## 当前实现记录

- 2026-11-10：确认 GitHub/main 基线 `301e259`；目标分支为 `codex/functional-root-fixes`。
- 2026-11-10：发现 Workflow 源树已有大量 F1–F5 实现。知识库中“尚未实施”的摘要已落后于源树，实施状态以版本化规格和实际源码为准。
- 2026-11-10：本账建立时，调度、超时、工作区锁、通用 Node 目录绑定和长会话问题均已用临时真实 SQLite/执行器复现；修复和验证记录随实现追加。
- 2026-11-10：持续补位与忙工作区三项回归先得到 `3 failed / 6 passed`（`/tmp/agent-fleet-scheduler-before.xml`）。修复后 scheduler、worker、heartbeat、legacy bridge、conversation 五模块得到 `114 passed / 4.95s`（`/tmp/agent-fleet-scheduler-regressions.xml`）；七项新增回归已登记 RUN-02，完整门禁待整合后重跑。
- 2026-11-10：F1 原五模块先得到五项缺模块 collection errors；带入对应纯领域实现后连同 `tests/test_boundaries.py` 得到 `135 passed / 30 subtests / 0.74s`。F2 原五模块已先复现缺少持久化/服务模块，正在按当前边界移植依赖闭包；不开放未完成的执行入口。

- 2026-11-10：F2 运行与备份集成保持当前 `hub -> fleet_core <- tools` 边界；API/MCP 目录恢复新增 reader4 支持与真实 HTTPS 地址策略传递，当前六模块221 passed（`/tmp/agent-fleet-mcp-transport-current.xml`）。
- 2026-11-10：Workflow 清理保护先红6项，修复用量账本/来源回执/取消围栏引用丢失及批次饥饿；关联292 passed/1环境skip，新增7项真实SQLite回归。长期归档及围栏退休策略未实现。
- 2026-11-10：F3 原五模块已在目标树复现5项缺模块collection errors（`/tmp/agent-fleet-f3-before.xml`）；正在复用源固定源码/评审/测试链并提取中性协议，服务接入当前composition，保持已有底层Windows与租约修复。

- 2026-11-10：F3/F5开发交付与外部执行器原任务/文本/Memory/输出/恢复20模块848 passed（`/tmp/agent-fleet-workflow-delivery-external.xml`）；先前缺少Muse journal身份/lookup与装配的失败已补齐，真实MCP/Muse未运行。
- 2026-11-10：F4 Node收发先90 passed/3失败，业务正确拒绝但HTTP错误映射503；补冲突409后103 passed。capability收紧命令expiry与默认360s timing冲突先复现，三phase同比例收紧且原幂等不延长；Hub原生命周期/Node固定测试闭包验证通过。Node177 passed、协议边界43 passed，Hub543 passed与输入容量112 passed互有重叠。
- 2026-11-10：F5账户管理和前端取消先12失败；补features、两个管理页、外部signal贯穿body与cause，独立Bearer入口采用当前machine marker机制。auth/HTTP/UI合同282 passed，管理Chrome14 passed；独立固定源码e2e/sandbox/HTTPS MCP等142 passed。
- 2026-11-10：长会话先6 failed/1 passed，旧60回合输入263533 UTF-8 bytes；当前完整最近suffix64761 bytes/29messages，本机读取0.841ms/Python traced peak76763B。最终207 passed/0skip，含真实Chrome加载更早/新终态合并/刷新；见[实现计划](../superpowers/plans/2026-11-10-bounded-conversation-history.md)。HTTP最近20turn测试样本166563 bytes，这不是模型token或全局吞吐度量。
- 2026-11-10：保留main原38条journey（含插件），合入实际存在的WF范围与新回归形成44条/1957 selectors。首轮完整测试为6791 passed、12 failed、5平台skip、204 passing subtests/876.56s；原始失败保留在`/tmp/agent-fleet-functional-root-final-20261110/`。43/44 journey通过，RELEASE-01的Linux pidfd实例在macOS不能执行。没有用源树历史全仓数替代当前证据。
- 2026-11-10：首轮失败逐项闭环：postcheck的4个旧替身补齐仓库原子审计；3个Node回归补显式workspace绑定；4个长回复回归验证完整持久化/UI读取及超出64KiB后的上下文整回合省略。上传reserve/commit绕过统一decoder的生产问题，新增2个真实HTTP用例先复现500，再复用`json_object`返回400；关联123 passed/17.53s。新增selector挂入FILES-01，现为44条/1958 selectors；第二轮完整门禁在`/tmp/agent-fleet-functional-root-rerun-20261110/`运行中。
- 2026-11-10：发布归档测试原来只测旧HEAD，不能证明当前新增模块会进入候选包。新增6类模块逐字节检查先复现缺文件，随后用独立临时Git冻结当前候选再运行原packager；冻结后制造未提交漂移、未跟踪模块和var库，核验只含冻结源码。原fleet_core/runtime与插件检查保留，37 passed/2 subtests/7.29s（`/tmp/agent-fleet-current-source-package-green.xml`），生产打包逻辑未改。
- 2026-11-10：第二轮完整结果6804 passed、1 failed、5平台skip、204 passing subtests/972.27s，报告`/tmp/agent-fleet-functional-root-rerun-20261110/`原样保留。失败是导出内存探针进程全局tracemalloc峰值11084841 bytes超8MiB；独立/完整模块/全收集后单项均通过，前27模块449 passed，诊断峰值3226334 bytes，原额外分配的具体来源尚未重现。新增12MiB无关分配对照先稳定1 failed/1 passed，再将同一真实导出链置于独立探针进程，保留12MiB输入及8MiB上限，49 passed/10.38s。没有把原间歇失败描述为生产泄漏已修复。
- 2026-11-10：四组容量探针在全仓测试停止后串行通过：1000条流程读取P95≤1.680ms，32活跃tick P95为448.152ms，4×64MiB原HTTP发布80.477MiB/s、RSS峰值增量10.328MiB；4份大小/hash独立核验。环境、源码清单、10ms采样与WSGI/回环/固定时钟等局限见[验收矩阵§11](../testing/workflow-platform-acceptance.md#11-性能初值与测量)。脚本已纳入`tests/fixtures/workflow_capacity_probe.py`，不自动调用外部服务。第三轮完整验证在`/tmp/agent-fleet-functional-root-verified-20261110/`完成，6806 passed/5平台skip/204 passing subtests/850.75s，43/44 journeys、1958 selectors全收集；46张截图随后通过完整PNG校验。
- 2026-11-10：当前373个改动/未跟踪文本文件通过UTF-8无BOM、Python AST，fatal/undefined Ruff与`git diff --check`通过。Obsidian原master上的canonical/router/MOC/项目入口就地同步，未新增孤岛。
- 2026-11-10：完整CI Ruff补跑暴露5个F401；两处仍被消费的兼容名改为显式原名再导出，其余移除未使用导入。Linux workflow还缺原计划的bubblewrap/libseccomp2和45分钟预算，新增配置回归先1 failed，再补原步骤；当前main的依赖锁、lint/audit、Windows和部署合同均保留。10模块334 passed/2 subtests（`/tmp/agent-fleet-ci-integration-green.xml`）。原验收机gz-cvm已由知识库canonical确认于11/10到期注销；没有使用旧端点或擅自换生产机。
- 2026-11-10：发布核验器遗漏源树PNG完整性实现，8类损坏文件在目标树先8 failed；复用原流式CRC/IDAT/scanline/IEND校验器后拒绝损坏，保留原错误cause。独立内存探针保持16MiB像素及2MiB上限；父进程3MiB无关分配先1 failed/1 passed，再以独立进程隔离测量。发布/旅程/包/CI五模块213 passed/2 subtests（`/tmp/agent-fleet-release-integration-green.xml`）；GATE-01/RELEASE-01新增17 selectors。
- 2026-11-10：第四轮完整命令在`/tmp/agent-fleet-functional-root-ci-png-20261110/command.json`，原日志/JUnit/旅程/截图和`verified-summary.json`完整保存：6887 passed、5平台skip、204 passing subtests/873.92s，零failure/error，43/44 journeys、1975 selectors全部收集、46张PNG完整核验。1121源码文件运行前后SHA-256不变。Python3.13.7、Node26.9.0、Playwright1.62.1、Chrome157.0.8103.40属于本机配置，不冒称CI的Python3.10/Node22/Playwright1.63.0环境。原release verifier仍退出1/`dirty_checkout`，且Linux必需实例未执行；五项外部检查保持not_run。
- 2026-11-10：随后确认运行依赖分类错误：`jsonschema`只在requirements-test声明，真实Workflow装配及Fleet MCP却在运行时导入。独立Python仅放行requirements.txt的已安装依赖闭包，真实Hub先因`undeclared_runtime_dependency:jsonschema`失败；移到运行依赖后182项启动/gates/MCP/输出关联回归通过（`/tmp/agent-fleet-runtime-dependency-green.xml`）。原jsonschema锁定4.26.0与版本范围不变，没有下载或安装程序。新增启动回归登记RELEASE-01；当前矩阵1976 selectors，这一后续声明修复不冒记成第四轮1975-selector全仓内容。
- 2026-11-10：源树新增1067个测试函数与目标树逐项对照；两项旧期限测试由当前`test_transport_deadline`覆盖，跨locale打包及流式GET原探针直接执行当前代码也通过。证据为`/tmp/agent-fleet-source-test-integration-audit-20261110.json`，独立探针不拼入完整门禁计数。
- 2026-11-10：最终收集为6893个测试实例，44条journey的1976个selector全部可解析，无缺失；这只证明收集，不追加完整执行通过数。当前本机未安装Docker/Podman/Colima/Lima/Multipass，不能就地补出Linux原生结果。最终源码候选与证据持久目录为`/Users/mango/.cache/agent-fleet/verification/functional-root-runtime-deps-20261110/`；其中全仓报告保留其运行时源码清单，后续依赖回归独立记录，不改写旧报告revision或dirty字段。旧`functional-root-ci-png-20261110/`冻结保留为依赖修复前的历史候选。
- 2026-11-10：远程取消review在真实HTTP→worker→command→NodeToolExecutor链复现取消后仍写文件；新增回归先10 failed/4 passed。取消回调现贯穿broker与等待层，同SQLite写事务与Node claim串行：未领取expired、已派发unknown、已有终态保持，取消等待结束后释放worker槽。另复现直接复用回调导致每100ms轮询续写租约，改为只读取消/失租检查，后台心跳维持原节奏。18模块275 passed/47.06s；新模块15参数实例、8个selector登记RUN-01，当前矩阵1984 selectors。方案及原红绿证据见[远程取消计划](../superpowers/plans/2026-11-10-remote-run-cancellation.md)，完整门禁将在新冻结中独立运行；之前6887全仓不涵盖本次实现。

- 2026-11-10：取消修复独立冻结 `69028deb2cf7b2908e3a4d415aba95e819dd1fa6` 完整门禁6903 passed、5平台skip、204 passing subtests/900.15s，零failure/error；43/44 journeys、1984 selectors全部收集、46完整PNG，1124源码hash前后不变且checkout clean。原release verifier在同一干净冻结仍退出1/`journey_incomplete`，未放宽Linux必需实例；五项external checks仍not_run。原命令/JUnit/报告/源码bundle及原packager候选在`/Users/mango/.cache/agent-fleet/verification/functional-root-remote-cancel-20261110/`；1048后端/67前端文件逐项hash一致。门禁后只补录文档，不冒称冻结包含后补结果。

## 当前剩余范围

2026-11-11 续接：显式 operator 版本 summary 的管理员 UI 已完成并纳入 WF-06；API DTO parser 和 exact-head wrapper 的真实浏览器问题已修，关联服务/消费/API/浏览器回归80 passed。此项只覆盖人工编写、不可变版本和历史查看，不包括自动摘要生成；下表较早的“自动摘要/UI仍开放”表述中，UI部分已由本项取代。完整 Linux/browser 门禁、生产配置及全局其他 TODO 仍开放。

| 项目 | 已实现边界与尚缺证据 |
| --- | --- |
| Linux及Windows验收 | 最新51f71614修复当前Python路径被拒绝；真实SG固定测试passed/failed/lost/unknown四场景4 passed，原outbox恢复不重跑；同冻结Linux十模块172 passed、Windows运行时/控制/期限/进程子集102 passed，无失败/skip，1146源码三端不变。更早0d11d8b4原生Linux288 passed，Windows289 passed/2 POSIX skip；a9da2c29原生NTFS收发131 passed、实际两机窄交接1 passed及独立固定测试2 passed分别保留。旧68-failure广范围报告不被这些子集替代。完整Linux/浏览器、全WF-04/05和真实CLI/model仍开放。 |
| 当前真实能力生命周期 | cd7019aa/1149源码冻结：home-win与SG Linux共20 passed/0skip，含8项自动协调器/原worker场景及12项独立held/isolated postcheck场景；本机原矩阵10 passed、关联417 passed分别记录。20份Hub数据库与两端journal/file独立核验，106份原生证据大小/hash一致，三端源码不变/checkout clean。没有真实商业模型或完整Workflow交付声明。 |
| 原生模型步恢复 | 2026-11-11已接原生持久检查点并完成独立冻结6921-pass全仓；后续remote_wait释放worker、工作区预约及原命令恢复已纳入4f4b6478的Linux关键子集240-pass验证。完整Linux门禁与真实跨机仍缺；Node控制后续改动须绑定新冻结。 |
| 停止与保留策略 | Node workspace进程/固定测试已接可协商的控制观察、原截止内续租与本机停止；245项关联、336项额外集成分别通过。任意外部adapter的停止、长期归档及取消围栏退休仍开放；Hub旧unknown不被本机停止结果覆盖。 |
| F0/F3/F5真实链路 | 私有macOS单个真实Codex/所选模型的开发→独立评审→固定测试及UI恢复已通过，停在等待人工验收。生产sandbox、其他CLI/模型、外部MCP/Muse、邮件与候选部署仍未验收；本试点不代填MODEL-LIVE或完整发布。 |
| F5后续输入与配置 | 显式 operator 版本summary已接独立权威和Native/外部消费，三端摘要/备份96项及4a3e15f1全仓证据见[摘要计划](../superpowers/plans/2026-11-11-versioned-summary-context.md)。Native固定代码包本机输入已完成主要实现；代码输入37 passed，含混合文本/源码、六项manifest字段绑定、UTF-8/路径/特殊归档条目拒绝、篡改包尾、聚合字节/文件/引用上限、异常清理、已提交Run不重读、安全checkpoint response/receipt/ready 恢复、pending unknown、明文/加密冻结备份恢复及reader12 gate-off拒绝；关联十六模块708 passed。129 Run×16引用历史闭包、页边界10种损坏及明文/加密恢复已独立验证，完整发布门禁仍开放，见[计划](../superpowers/plans/2026-11-11-native-code-package-inputs.md)。自动摘要、二进制媒体、其他执行context、外部能力要求及其余集成配置仍开放；Muse cancel/export仍unsupported。 |
| F6后续扩展 | 保留Flask/SQLite WAL；当前样本不要求新增数据库或队列。实际Node/模型、人工干预/返工数据仍待测；DAG与专业adapter遵守至少两个真实用例或测得瓶颈的前提。 |

- 2026-11-11：Native固定源码包输入按独立计划完成本机实现：默认关闭的可信本地target、schema4/reader12、owner/workspace/hash/size/MIME授权、完整source stream解包校验、只读list/read工具、私有attempt视图和安全ready checkpoint恢复。先修复Native文本进程恢复测试夹具的`collect(..., references=...)`接口漂移，关联十模块315 passed；代码输入模块12 passed，真实provider读取及进程中断后恢复验证包含在内。尚未完成专属frozen backup/gate-off历史矩阵、完整Linux/browser发布门禁与全项目剩余TODO；不以本机切片宣称已发布。计划：[Native代码包输入](../superpowers/plans/2026-11-11-native-code-package-inputs.md)。
- 2026-11-11：延续Native代码包边界验收，新增篡改流尾、17引用/多包10 MiB预算、跨包4096文件、10 MiB+1 byte展开声明及临时目录清理测试，并登记WF-06/WF-01 selectors。焦点代码模块22 passed，包含source streamer、Native text/memory/checkpoint/dispatch/worker/provider/backup和release evidence的十一模块441 passed；完整项目TODO、加密/历史corruption矩阵与Linux/browser发布门禁仍开放。
- 2026-11-12：Native代码包根因修复与扩展验证：重复同一不可变源码引用此前因`prior is current`被错误重复计费，已改为按唯一artifact ID累计；新增回归先稳定1 failed，再修复后通过。混合文本+代码引用此前被文本校验错误拒绝，已按引用类型分流并通过实际provider链。新增 response/receipt/ready 恢复、pending unknown、历史末页损坏、加密备份及特殊归档条目验证。最新16模块 **708 passed**；Ruff选定规则、compileall、JSON、UTF-8/no-BOM、diff检查通过；完整浏览器/Linux门禁仍未运行，原因已记入计划。

- 2026-11-11：按“做完所有todo”继续推进原生持久模型步；新增真实worker/SQLite中断用例先2 failed（缺检查点API），接入私有lease-fenced检查点后首批91 passed。取消与持久最终结果竞态先1 failed/94 passed，再修复保存结果优先；后续OS进程退出、真实用量、备份/删除与坏记录回归独立记录，最终结果见检查点计划。旧6903全仓仅属于11/10冻结，不涵盖本次实现。

- 2026-11-11：原生检查点最终15模块280 passed/51.75s，额外兼容/打包/清理122 passed/2 subtests/10.34s，范围不拼成新全仓。实际OS进程退出后恢复、结果已落盘后配置停用仍可完成、迟到取消、真实用量账本、坏检查点、旧备份恢复unknown与归档会话删除均覆盖；新11函数selector加入ASSIST-02，当前44旅程/1995 selectors。严格跨平台、正常远程等待释放worker及其余todo仍开放。

- 2026-11-11：检查点冻结06e87586全仓完成6921 passed/5平台skip/204 passing subtests，43/44 journeys、1995 selectors及46完整PNG；1127源码前后不变、checkout clean，1051后端/67前端文件逐字节匹配。原核验器仍退出1/journey_incomplete。证据 `/Users/mango/.cache/agent-fleet/verification/native-checkpoints-20261111/verified-summary.json`；五项外部检查not_run，本冻结不含后续remote_wait实现。
- 2026-11-11：远程等待释放worker、原命令零等待观察、恢复claim间隙工作区预约、HTTP取消与Node claim原子围栏已接。补齐依赖停用后的观察优先级、unknown优先、终态/备份预约清理及submit回执归因；三个新缺陷均保留修复前失败。十模块212 passed/10.53s，新增8函数/13实例注册RUN-01，当前44旅程/2003 selectors；新完整门禁仍待执行。见[远程等待计划](../superpowers/plans/2026-11-11-native-remote-suspension.md)。
- 2026-11-11：线上只读复核：manifest与GitHub main均为301e259，CI 42540826668 attempt 2 的deploy于2026-11-10 18:02:28 UTC成功；首页/login/assistant/muse/healthz均200，匿名operator session为401。此前SSH banner超时已被成功重试取代。此发布不含本地功能分支；本次未部署或触发真实模型/Node任务。
- 2026-11-11：远程等待额外十模块Node HTTP/runtime/workspace/routes、原生Workflow memory/text/recovery、受控两节点、完整流程及heartbeat回归287 passed/53.33s；与212项关联验证分别记录，不能当新完整门禁或真实设备验收。线上两项关键助手资源与main源码hash一致；最新readiness报告位于 `/Users/mango/.cache/agent-fleet/verification/native-remote-suspension-20261111/readiness.json`。
- 2026-11-11：远程等待容量探针用4000条真实终态Run/Message复现每次slot/candidate认领分别20000/80300条SQLite指令；仅补稀疏等待索引后candidate仍48300条，逐语句定位到两次缺少(owner_id,trigger_message_id)索引的历史关联。增加两条原位索引后，在100指令步长下slot/candidate进度计数为0/400，保留5000上限和容量语义。九模块含真实Chrome新验190 passed/0skip，探针独立2 passed；RUN-02新增selector，矩阵44条/2004 selectors。见[索引实施与红绿证据](../superpowers/plans/2026-11-11-remote-wait-capacity-index.md)。新全仓证据需另行冻结，既有6921结果不涵盖本次实现。
- 2026-11-11：最新功能源码独立冻结4f4b6478b64720f29bf5a94e23b025f5b2e0add0（1131文件）。新加坡Debian12/Linux6.1/Python3.11.2/Node22.23.4/bwrap0.6.1的私有目录中，原pidfd、Supervisor进程、沙盒、独立评审、原生检查点、远程挂起、调度/容量/索引及有界进程11模块240 passed/2 passing subtests/0skip，78.58s；源码摘要不变，checkout clean。原packager另产出1055后端/67前端文件并逐项hash匹配。证据 `/Users/mango/.cache/agent-fleet/verification/native-linux-capacity-20261111/`。这是关键Linux子集，不是完整门禁或生产隔离验收；Playwright1.63.0两个小包已校验安装，Chromium153完整包195836009 bytes尚待用户按大文件规则批准，未下载。正式门禁命令已准备，不能提前记为passed。
- 2026-11-11：继续补Node停止/续租：真实HTTP→NodeRuntime→workspace子进程在Hub取消和Node关闭后仍写文件，新增用例先8 failed；另复现unknown被映射failed、响应延迟重新起算租约以及关闭后继续poll。已接同事务控制观察、不可延长原截止的续租、单调本地界限和owned进程清理；后端取消检查不做HTTP/SQL。新增17函数/33实例登记NODE-01，矩阵44条/2021 selectors；15模块245 passed/23.39s，额外12模块336 passed/55.97s，均无skip，集合不合并成全仓。方案见[Node控制计划](../superpowers/plans/2026-11-11-node-command-control.md)；旧4f4b6478不涵盖本次实现，新Linux冻结待验。浏览器下载批准仍待用户答复，没有据自动继续视为批准。
- 2026-11-11：Node控制后的独立冻结为0d11d8b4474d439faa08c91ee03ef3fc3b2af5a9（1134源码文件）；原生Linux17模块288 passed/75.83s/0skip。home-win先用损坏的C:\Python313解释器执行较广范围得到68 failed/338 passed/2 skip；33项新Node控制在该次已全部通过。改用主机现有且stderr为空的E:\code\conda\python.exe建立私有Python3.12.7环境后，原Windows CI10模块加Node控制、命令期限及Windows产物路径策略共13模块289 passed/31.56s，2项仅POSIX skip，无失败。两端1134源码摘要不变、checkout clean；未修改全局Python、服务或PATH。原广范围失败留档，POSIX服务夹具及NTFS收发前置条件仍需独立闭环，不将其描述为全Windows通过。
- 2026-11-11：同一0d11d8b4冻结由原packager产出1058后端/67前端文件，逐项hash一致；后端包6567086 bytes、SHA256为634a7beebccae56a84c6522a02dbbfc35da74552bd86e112a17546ebce89151d。完整收集6974实例、44旅程/2021 selectors全部可解析；收集不是执行通过。留证 `/Users/mango/.cache/agent-fleet/verification/node-command-control-20261111/`，后补账目不改写冻结源码。
- 2026-11-11：上线准备复核（UTC 2026-11-10 19:44）：公网manifest及远端main仍为301e259；最近main CI42540826668为success且已部署。首页/login/assistant/muse/healthz/manifest均200，匿名operator session401；功能分支尚未发布、无PR。现网页面与健康检查可用，不能据此判定本地新增Workflow或真实模型/节点业务验收通过。最新候选适合准备隔离试点，全面替换前仍需同一版本完整Linux门禁与所选真实核心链路验收；安全加固按用户要求后置。
- 2026-11-11：发现目标树漏带Windows原生收发与实际两机验收依赖；原命令先报缺文件，复用源树七个既有夹具后又复现旧hub.domain.control导入错误。改用现有fleet_core协议模块、显式选择Windows正常Python，验收路径恢复。独立冻结a9da2c29/1142源码在home-win Windows11/Python3.12.7完成原生收发131 passed/10.30s/0skip；macOS便携关联313 passed/24.52s/0skip。实际home-win→原Hub→SG Linux交接控制1 passed/9.08s，固定源码10240 bytes、SHA256 c5de78025951b553bc165aa960e206c36288fc13044f6ea383b2fdf4335ce122；一次PUT、一次下载、两次同接收回执及原协调核对ready均通过。Linux后续独立固定测试2 passed，报告/JUnit复制摘要一致，三端全部源码不变、checkout clean。见[原生交接计划](../superpowers/plans/2026-11-11-native-handoff-integration.md)。这不等于完整Workflow、真实CLI/model或发布门禁；原较广Windows失败及一次错误本机解释器启动均留档，无安装/服务变更/部署。
- 2026-11-11：上线准备复核发现固定测试的sys.executable与后端仅接受python/python3名称不兼容；实际Python3.14调用两后端均command_not_allowed，新回归先2 failed/8 passed。共享精确当前运行时准入后本机157 passed及独立36 passed，逐项覆盖原142用例和37项失败；去掉取消测试中掩盖此问题的allowlist补丁。另恢复远程固定测试三夹具，保留缺文件、旧导入、should_cancel签名不匹配及旧丢回执假设各次失败。新冻结51f716144721758f27db021a25324912f03aa837/1146源码完成真实SG Linux Node四场景4 passed/80.91s，丢回执通过原journal outbox一次补传、随后零重跑；相同冻结Linux十模块172 passed/73.58s、Windows五组运行时/控制/期限/进程102 passed/13.76s，无失败/skip，三端源码不变/checkout clean。产品源码与冻结一致，后补仅文档及两处测试未使用导入清理；809 Python文件3.10 AST、Ruff、UTF-8无BOM及diff检查通过。新增2 selectors后为44条/2023 selectors；这些子集不作为完整门禁，真实模型/CLI完整交付、发布与部署仍未完成。见[固定测试集成计划](../superpowers/plans/2026-11-11-native-fixed-test-integration.md)与`/Users/mango/.cache/agent-fleet/verification/readiness-20261110T201052Z/verified-summary.json`。
- 2026-11-11：复用源树遗漏的两个能力控制器，先保留缺文件/旧导入collection失败，再复现10项运行失败。Node journal实际为workspace_unavailable、自动等待器缺当前should_cancel参数；仅修正验收工作区与接口接线，本机原矩阵10 passed/10.80s、关联12模块417 passed/31.75s。独立cd7019aadc3e1b64429da597df967d94474469f6/1149源码冻结，在home-win/SG Linux完成20 passed/133.32s/0skip，其中8项自动coordinator/Run/worker，12项独立held/isolated postcheck，不能混称全部自动准入。原unknown与预约保留、成功原位settle/release、一个写入/只读digest/重启零I/O均通过。20份Hub数据库及两端原journal/file独立核验，106文件/910132 bytes复制大小/hash一致，三端冻结源码不变/checkout clean。完整Linux/browser、真实模型/CLI完整交付与其余todo仍开放；无产品代码、安装、全局配置/服务、提交/推送/部署变更。见[原生能力集成计划](../superpowers/plans/2026-11-11-native-capability-integration.md)与`/Users/mango/.cache/agent-fleet/verification/native-capability-20261111/verified-summary.json`。

- 2026-11-11：显式版本摘要权威已接 operator 发布/有界列表/精确读取/启停，完整不可变来源/hash、CAS幂等、Native3/外部prompt3/metadata4及Workflow reader11；原事务/当前授权/恢复历史闭包与明文/加密frozen备份覆盖。最初31项权威缺失、16项消费缺失，以及停用竞态和gate-off损坏均保留失败证据。首次ebd91052冻结本机关联831 passed、全仓7041 passed/1旧远程事件断言failed/5平台skip；Linux830 passed/1真实性能failed。SQLite3.40.1 tuple-IN仅用三列索引，1500版本闭包21600指令，改为VALUES驱动精确四列主键查询后selected/closure为3100/3600（100指令采样），原4000上限不变。
- 2026-11-11：修正4a3e15f1冻结Linux23模块832 passed/0skip/346.992s，本机完整7043 passed/零失败错误/5平台skip/204 subtests/911.90s，43/44旅程、2071 selectors、46完整PNG，1156源码前后不变、checkout clean；原核验器仍退出1/journey_incomplete。原远程验收测试保留准确两种事件时序及2模型请求/1节点命令，额外52项通过。随后Windows摘要64 passed/6备份failed，实际单文件fsync复现Bad file descriptor；owned暂存文件在Windows改用可写描述符，保留失败关闭与暂存数据。新b44fe1bc冻结三端相同四模块各96 passed/0skip（macOS8.58s、Windows76.234s、Linux38.856s），不假定NTFS目录掉电耐久。原packager1080后端文件逐项hash一致，包6628994 bytes；最终矩阵44条/2073selectors、7050实例全部收集。完整证据、原失败、源码bundle与边界见[摘要计划](../superpowers/plans/2026-11-11-versioned-summary-context.md#verified-results--2026-11-11)及 `/Users/mango/.cache/agent-fleet/verification/summary-context-20261111/`。无提交/推送/部署或真实CLI/model调用，完整Linux/browser及全局todo继续开放。
