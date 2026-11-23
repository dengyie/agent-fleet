# T3 Code 架构与 Agent 工作区源码对照

本文把 [pingdotgg/t3code](https://github.com/pingdotgg/t3code) 的源码与
agent-fleet 原生电脑 Agent 工作区做具体对照，记录值得复用的产品与工程决策，
并标出两种运行拓扑的差异。它是架构研究，不代表 T3 Code 的性能或安全审计，
也不表示本文提出的后续项目能力已经实现。

## 研究基线与方法

- 上游固定在 2026-11-23 的 `main` commit
  [`1bc44f2732ce0a24c0608626d59409f1a0893db4`](https://github.com/pingdotgg/t3code/commit/1bc44f2732ce0a24c0608626d59409f1a0893db4)，许可证为 MIT。
- 仓库 API 报告约 1 GB。没有克隆整仓、安装依赖或运行上游代码；按职责下载 183 个上游源码、测试和架构文档（约 5.7 MB），逐项核对 Git tree 中的 blob SHA。引用均固定到该 commit，可复查。
- 上游 README 将产品定义为跨设备的 agent harness control surface，支持 Codex、Claude、Cursor、Grok Build、OpenCode、Antigravity 等；源码不只是会话 UI，还包含 provider/runtime、远程授权、耐久编排、timeline、文件变更和测试基础设施。
- 当前 Fleet 工作树基于公开 `main` `afe29b1e175e34cfbe25e16e59c019eb4d087ac2`，包括 PR119 对 task/session 结果绑定的修复。此工作树为单提交浅克隆，因此没有用本地 Git 历史推断 Fleet 长期热点或贡献者趋势。比较依据是此版本源码、现有测试与权威契约。
- Fleet 的相关权威边界见[Agent 分层架构](../agent-capability-architecture.md)、[原生会话 API](../native-session-api.md)、[网页接入契约](../native-web-frontend.md)和[可视化架构](../visual-architecture.md)。

## 两个产品的基本拓扑

| 关注点 | T3 Code | agent-fleet | 结论 |
|---|---|---|---|
| 执行归属 | Provider 进程、工作区、Git、terminal、checkpoint 在拥有工作区的 T3 server 环境。Web、Desktop、Mobile 是连到该环境的客户端。 | Hub 负责授权、目录、排队与持久回执；VM/电脑节点主动 poll 已签名命令，启动本机 Agent 并把脱敏的事件与结果回传。浏览器只用公开 HTTP。 | 两边都坚持执行环境拥有文件和凭据；Fleet 由节点出站拉取命令、回传数据的拓扑是安全边界，不能替换成 Hub 直接 SSH/远程文件访问。 |
| 公共契约 | `packages/contracts` 定义 RPC、provider、thread、turn、stream 结构；客户端和服务器分离发布。 | `fleet_core` 定义共享 profile/schema；Flask、应用服务与 SQLite port/adapter 分层；静态前端通过受限 DTO 调用 HTTP。 | 继续保持现有 `hub -> fleet_core <- tools`。可借鉴显式事件/能力契约，不需要复制其 monorepo 或 RPC 技术栈。 |
| Provider 接入 | 独立 `provider-*` 包、`ProviderAdapter`/`ProviderHost` seam，协议细节与 provider instance/account 在 adapter 层。 | family profile、OS launcher、resume adapter、native capture/normalization 分层；不同 Agent 的输出能力和续聊方法差异很大。 | 保持单 Agent 适配器可独立验证；下一步把“能做什么、实际采到了什么”明确到每个 Agent，而不是让 UI 从 family 名推测。 |
| 运行控制 | 原生 thread/turn 能接收 steer、interrupt、审批和 structured question；只公开 provider 确实具备的能力。 | 网页通过受控 action、operator 授权、签名 pull queue 和原 native session identity 操作；CLI 不支持的动作保持禁用。 | 能力缺口要显式呈现。T3 的持久 ACP 会话模型比当前 Fleet 的一次 CLI 续聊更丰富，但不是所有 CLI 都能安全模拟。 |

## 上游源码中可迁移的设计

### Provider 能力是结构化事实

T3 把能力拆成 sessions、threads、turns、streaming、tools、approvals、planning、
subagents、context、checkpointing、identity 和 runtime policy；例如是否有稳定
native turn/item/request ID、是否发出 turn-start、是否能 steering、输出是 delta 还是
只给最终消息、原生审批 ID 是否存在，都不是一个 `supports_agent: true` 布尔值。
字段见 [`OrchestrationV2ProviderCapabilities`](https://github.com/pingdotgg/t3code/blob/1bc44f2732ce0a24c0608626d59409f1a0893db4/packages/contracts/src/orchestrationV2.ts#L323-L448)。

Fleet 已经区分 profile 的潜在能力、CLI 安装、可验证的 native session identity、
采集质量、adoption authority 和当前可执行 action；这条原则已经落地。当前 gap 是：
这些事实还没有形成一个按 Agent/客户端版本汇总的可观测矩阵，网页也不能统一说明
“有流式文本/只有完成快照、工具参数是否可见、usage 是否可取、审批能否从网页回答、
续聊由 CLI 还是桌面 bridge 提供”。推荐先补**只读能力与证据模型**，只放可观测状态，
不要以能力声明授予操作权限。

### 接收回执、durable state 和副作用分开

T3 的 command receipt、event、projection 和 effect outbox 在一次 SQLite transaction
中提交，side effect 在 commit 后由 effect worker 执行；命令 accepted 表示意图已提交，
不是 Agent 已运行或最终任务完成。见
[`EventSink.commitCommand`](https://github.com/pingdotgg/t3code/blob/1bc44f2732ce0a24c0608626d59409f1a0893db4/apps/server/src/orchestration-v2/EventSink.ts#L788-L888)
和[架构说明](https://github.com/pingdotgg/t3code/blob/1bc44f2732ce0a24c0608626d59409f1a0893db4/docs/internals/01_architecture.md)。

Fleet 的普通会话 `client_token`、命令、回执和事务性重复恢复已提供相同的核心保证；
Hub cursor 是持久事件身份，`202`/queued 也不宣称 Agent 已处理。我们应复用现有
transaction/receipt 服务，不另建一套通用编排器或消息中间件。

### 实时增量与耐久历史采用不同窗口

T3 首屏 thread stream 是有界 projection：通常最多 10 个 user turns，或 75 个 activity
rows；普通 turn 页面最多 500 rows，编码页目标预算 1 MiB（首条超大 row 是例外，见后文）。resume 事件通常最多 128 条/
1 MiB，追不上时发 bounded snapshot。cursor 锚定来源 thread/item identity 与 snapshot
sequence，而非展示文本。见
[`threadHistoryPaging.ts`](https://github.com/pingdotgg/t3code/blob/1bc44f2732ce0a24c0608626d59409f1a0893db4/apps/server/src/orchestration-v2/threadHistoryPaging.ts#L10-L53)
和[`ThreadStream.ts`](https://github.com/pingdotgg/t3code/blob/1bc44f2732ce0a24c0608626d59409f1a0893db4/apps/server/src/orchestration-v2/ThreadStream.ts#L18-L117)。

已展开的 terminal output 走 `CommandOutputHub` 的独立订阅，避免把大量 stdout
增量写进通用 thread event log；离线恢复仍由已保存的完成态 output 提供。通用活跃
assistant/tool 更新可短暂 coalesce，但不能让游标跳过未 coalesce 的事件：
[`CommandOutputHub.ts`](https://github.com/pingdotgg/t3code/blob/1bc44f2732ce0a24c0608626d59409f1a0893db4/apps/server/src/orchestration-v2/CommandOutputHub.ts#L29-L59)、
[`ThreadLiveEventCoalescer.ts`](https://github.com/pingdotgg/t3code/blob/1bc44f2732ce0a24c0608626d59409f1a0893db4/apps/server/src/orchestration-v2/ThreadLiveEventCoalescer.ts#L18-L76)。

Fleet 已有最新 tail、before 历史页、after 实时增量和目录快照分页。浏览器初始/live
阅读窗口默认最多 500 个事件；显式加载更早历史会扩大窗口，后续 live 读取保持已扩大的
窗口，裁剪时同步更新可重新读取的历史边界。因此不能把 500 写成整个会话的永久上限，
长时间翻页后的 DOM/内存预算仍需实测。它的 UI 目前以 4 秒间隔拉取游标；交互中感知的
流速仍受上传/入库与这个轮询间隔共同限制。可以先测端到端 delta 延迟，再让**活跃会话**
更频繁地复用既有 cursor endpoint、闲置时退避。耐久事件不能被 UI coalescing 删除；
不应仅为跟上 T3 增加新的通用 broker 或重复 stream 接口。

T3 客户端还将 projection 和已应用的 cursor 成对缓存：取消中的旧 scope 不能保存尚未
应用事件的 cursor，也不能覆盖接替它的新 scope。见
[`threads.ts`](https://github.com/pingdotgg/t3code/blob/1bc44f2732ce0a24c0608626d59409f1a0893db4/packages/client-runtime/src/state/threads.ts#L294-L345)。
Fleet 现有 reader 已有单 reader、generation 与 abort 隔离，以及失败退避；将来若增加离线
缓存，应把“事件已应用”与“恢复游标已提交”保持为同一个一致性边界，不能仅保存最新收到的
cursor。

### 历史完整度、分页和恢复各有契约

T3 的 cursor 同时包含 snapshot sequence 与 source item identity，并有页大小、行数和
字节上限；若 native history 页面不能安全续接会话，不能假定所有客户端等价。T3 还对
老 Claude/Codex/Pi 等 transcript 做显式 import，使用 provider instance + native session
身份去重，检查工作区与 thread 所属关系，创建可恢复的应用 thread：
[`AgentSessionImporter.ts`](https://github.com/pingdotgg/t3code/blob/1bc44f2732ce0a24c0608626d59409f1a0893db4/apps/server/src/project/AgentSessionImporter.ts#L88-L102)、
[导入主流程](https://github.com/pingdotgg/t3code/blob/1bc44f2732ce0a24c0608626d59409f1a0893db4/apps/server/src/project/AgentSessionImporter.ts#L243-L310)。

Fleet 已经用真实会话的 native title、模型/客户端信息与 Hub cursor 事件 API，且
`include_source=0` 把原始 native source archive 留在诊断边界外。可继续补老 native
metadata 回填与明确历史完成度；必须 exact-session / exact-workspace 绑定、有界读取、
只读原库，未知内容跳过并报告采集缺口。不要按时间、摘要或内容相似度合并会话。

### UI 将对话、执行活动与可展开成果分别呈现

T3 timeline 不只是 role/text 气泡：它把 assistant/reasoning、命令、工具调用、文件改动、
审批问题、计划、subagent、失败与 run settlement 显示为不同 row。工具活动聚合，但可
展开；滚动有 follow-end、free-scroll、新 turn anchor 三种状态，记住稳定 row/offset 与
disclosure 状态。见
[`MessagesTimeline`](https://github.com/pingdotgg/t3code/blob/1bc44f2732ce0a24c0608626d59409f1a0893db4/apps/web/src/components/chat/MessagesTimeline.tsx)、
[`timelineScrollAnchoring.ts`](https://github.com/pingdotgg/t3code/blob/1bc44f2732ce0a24c0608626d59409f1a0893db4/apps/web/src/components/chat/timelineScrollAnchoring.ts#L37-L151)
及[`WorkLog.tsx`](https://github.com/pingdotgg/t3code/blob/1bc44f2732ce0a24c0608626d59409f1a0893db4/apps/web/src/components/chat/WorkLog.tsx)。

Fleet 当前 static JS workspace 已有 keyed native message rows，保留工具展开与滚动 DOM；
user/assistant delta 与 snapshot、工具 call/result、ask/answer、lifecycle 和 capture gap
各自渲染。下阶段可借鉴更稳定的滚动锚点/跳到最新体验和大量历史窗口化，但不要为了
Timeline 视觉相似先迁移 React/LegendList。

### 结果绑定要精确，产物要按轮次归属

T3 明确把 provider turn 完成和后续 checkpoint/diff/PR 同步分成不同 milestone；
不能把后续收尾时间计成 Agent turn 时间。Git checkpoint 使用隐藏 refs，不污染用户分支；
provider 不能 rollback 对话时必须在碰文件前拒绝文件回滚：
[架构说明](https://github.com/pingdotgg/t3code/blob/1bc44f2732ce0a24c0608626d59409f1a0893db4/docs/internals/01_architecture.md)。

Fleet 已在 PR119 修正异步 session detail 加载不能重置 Task/session binding，读取结果前后
也检查绑定；但当前“原任务结果”只代表启动任务的快照，续聊产生的文件、diff 和测试
不会更新。这是本项目与 T3 最大的产品缺口之一。下个有价值的具体迭代是对**有明确
Runner task / command 与 Git workspace 绑定的每轮续聊**记录新完成区间、变更清单/diff、
结构化测试摘要及截断/脱敏状态；用 command 或 transcript turn identity 归属，不覆盖原
task 快照。外部/未采用进程没有可验证 worktree 时显示“无可归属产物”，不臆测 diff。

### 安全授权属于 server，不由 UI 或 provider 名推断

T3 environment scope 只能在配对后向下收窄；socket 已认证也不代表任意 RPC 均获准；
每个 RPC 单独过 access check。peer link 的 mode ceiling 只能向下取交集，idempotency key
需加 caller namespace：[`环境授权说明`](https://github.com/pingdotgg/t3code/blob/1bc44f2732ce0a24c0608626d59409f1a0893db4/docs/internals/04_environment-auth.md)、
[`远程边界`](https://github.com/pingdotgg/t3code/blob/1bc44f2732ce0a24c0608626d59409f1a0893db4/docs/internals/05_remote.md)。

Fleet 同样把浏览器 operator/account 身份与 runner、node、ingest 凭据分开；权限、adoption
和 node-native session identity 在每个 mutation 前重新确认。继续坚持浏览器不得取得
机器凭据、路径、shell 参数或通过 DTO 自行推断 action。

## Google Antigravity：两种接入不能混为一谈

> **时点说明：**本次源码比较写于 ACP 实施前。用户随后明确要求 Agy
> 完全按 T3 Code 的 `agy acp` 方式接入；当前分支已据此替换新执行路径，
> 本节原有“仅建议可选调研”的结论已被实施契约
> [native-provider-runtime.md](../native-provider-runtime.md) 取代。下列旧路径描述仅解释迁移来源。

Fleet 实施前主线用 `agy --output-format stream-json --print` 执行普通轮次；经过
init UUID/cwd 验证后，以精确 conversation ID 续聊。当前分支的新执行改为节点受管
`agy acp` worker：通过有界 stdio JSON-RPC、显式认证、精确 `session/resume`、协商模型/模式、
审批邮箱、可见增量/工具事件及续聊结果收集接入。旧 print 输出只保留给历史会话读取，
不能启动新 turn。严格独立 ACP 对端和真实 Hub/SQLite/HTTP 测试已覆盖多轮与失败边界；
真实 agy 二进制和模型调用尚无验收。T3 则在专用
[`AntigravityAdapterV2`](https://github.com/pingdotgg/t3code/blob/1bc44f2732ce0a24c0608626d59409f1a0893db4/apps/server/src/orchestration-v2/Adapters/AntigravityAdapterV2.ts)
和[`Antigravity ACP support`](https://github.com/pingdotgg/t3code/blob/1bc44f2732ce0a24c0608626d59409f1a0893db4/apps/server/src/provider/acp/AntigravityAcpSupport.ts)
里通过 `agy acp` 驱动持续 ACP session，基于握手版本识别能力，并实现 model/runtime-mode
切换、native approval/question、tool calls、active steer、background commands/subagents
与 continuation。它不是将 `--output-format stream-json` wrapper 改名为 ACP；两者控制面和
生命周期保证不同。

实现有意复用 T3 的 ACP 会话/权限/配置/后台结算模式，不复制 Effect.js、server RPC、
前端框架或 provider host。Hub 不执行 `agy`；权限答案仍由 Fleet operator 身份、SQLite 预约、
签名命令和节点私有 mailbox 交付。运行时 feature 来自握手能力，模型/模式必须是对端
公布的选项。Windows/macOS 的真实安装、认证和模型调用仍是独立发布验收项。

## 工程验证方式的启发

T3 对 Provider 使用真实录制的协议 transcript 重放 through adapter + orchestration +
projection；另有 receipt-race、migration/recovery、cursor paging、权限和 Windows spawn
测试，以及 wire byte-budget 和专门 perf checks。测试原则见
[`internals/03_providers.md`](https://github.com/pingdotgg/t3code/blob/1bc44f2732ce0a24c0608626d59409f1a0893db4/docs/internals/03_providers.md)、
[`internals/15_testing.md`](https://github.com/pingdotgg/t3code/blob/1bc44f2732ce0a24c0608626d59409f1a0893db4/docs/internals/15_testing.md)
和[`internals/16_performance-regressions.md`](https://github.com/pingdotgg/t3code/blob/1bc44f2732ce0a24c0608626d59409f1a0893db4/docs/internals/16_performance-regressions.md)。

Fleet 已有 Mac/Windows CI、CLI 生命周期/续聊身份接受、Hub SQLite/HTTP、DOM 行为与边界测试；
下一轮适合增强的是：为高价值 Provider 协议保留**脱敏、精简、版本标记的原始录制 fixture**，
让其重复通过现有 pure normalizer 和 session event 服务，并针对大 transcript 明确 wire/DOM
预算。记录失败案例时要保留 capture quality；fixture 重放通过不能冒充真实二进制或真实
模型调用通过。

## 按 Agent 对照协议方向

| Agent | T3 本次读到的实现 | Fleet 当前接入与可借鉴方向 |
|---|---|---|
| Codex | `CodexAdapterV2.ts` 通过 app-server protocol client 处理 thread/turn、原生 item、usage、审批与中断；有 session/model policy 与 resume 身份。 | 当前 CLI 执行 + native rollout/capture 路径已打通。可评估受控 node app-server path 增加 native turn/item、审批和 usage 的清晰度；不把它变成 Hub 或浏览器直接管理 RPC。 |
| Claude Code | `ClaudeAdapterV2.ts` 用 Agent SDK query session，暴露 message stream、模型切换、权限请求、interrupt 和原 session resume。 | 已有 CLI stream 与原 JSONL 逐块 capture。优先用 protocol replay 增强边界与 terminal-path 行为；引入 SDK 之前先证明审批/持续控制收益及平台安装/版本成本。 |
| Pi | `provider-pi/server/adapter.ts` 有 RPC stream、原生 session 文件绑定、tool/extension events、model/usage 和生命周期处理。 | 已支持 CLI 及原生文件采集/续聊。RPC 是可能改善实时性与原生交互的数据面，不意味着能从 JSONL 事后回答已过期的 live request。 |
| Antigravity | 专用 flavor 运行 `agy acp`，按版本与握手 feature 启用控制。 | 当前分支已运行节点受管 ACP worker；严格对端覆盖 handshake、认证、精确 resume、配置、审批、工具、增量文本与后台结算。真实 Agy/模型及两平台二进制验收仍待完成。 |
| ZCode CLI/desktop、Hermes | 本次查看的 T3 provider 包/adapter 清单没有对应专用实现。 | 继续保留 Fleet 的 adapter、verified storage / desktop bridge 和 Runner-only guards；不能以 T3 对其它厂商的实现当这两个 Agent 的兼容性证明。 |

实现证据：
[`CodexAdapterV2.ts`](https://github.com/pingdotgg/t3code/blob/1bc44f2732ce0a24c0608626d59409f1a0893db4/apps/server/src/orchestration-v2/Adapters/CodexAdapterV2.ts)、
[`ClaudeAdapterV2.ts`](https://github.com/pingdotgg/t3code/blob/1bc44f2732ce0a24c0608626d59409f1a0893db4/apps/server/src/orchestration-v2/Adapters/ClaudeAdapterV2.ts)、
[`Pi adapter`](https://github.com/pingdotgg/t3code/blob/1bc44f2732ce0a24c0608626d59409f1a0893db4/packages/provider-pi/src/server/adapter.ts)。

### 跨平台进程细节

T3 将进程启动与 event loop 隔离：Windows spawn worker 维护 pipe 生命周期，退出后仍
排空输出，并在 worker 失效时尝试终止其自有 process tree；Linux 还可用 systemd scope
限制/回收 Agent。见
[`SpawnWorker.ts`](https://github.com/pingdotgg/t3code/blob/1bc44f2732ce0a24c0608626d59409f1a0893db4/apps/server/src/process/SpawnWorker.ts#L96-L107)、
[worker 失效回收](https://github.com/pingdotgg/t3code/blob/1bc44f2732ce0a24c0608626d59409f1a0893db4/apps/server/src/process/SpawnWorker.ts#L428-L455)
和[`agentScope.ts`](https://github.com/pingdotgg/t3code/blob/1bc44f2732ce0a24c0608626d59409f1a0893db4/apps/server/src/process/agentScope.ts)。
Fleet 已有 Windows Job Object、launcher、bounded pipe collection、tail drain 与 exact
native identity；继续用既有平台实现验证关闭顺序、UTF-8 和中文路径、长命令参数、
PID 重用及未排空输出。T3 的 Node process worker 不应替换 Python 的平台进程层。

### 数据与授权方面不应照搬的行为

- T3 [架构文档](https://github.com/pingdotgg/t3code/blob/1bc44f2732ce0a24c0608626d59409f1a0893db4/docs/internals/01_architecture.md)明确 event log 只保留有限 replay window，projection 不能从头重建。Fleet 的完整历史采集要求需要独立耐久来源，不能为了降载直接裁掉原始记录。
- T3 [`selectOlderTimelinePage`](https://github.com/pingdotgg/t3code/blob/1bc44f2732ce0a24c0608626d59409f1a0893db4/apps/server/src/orchestration-v2/threadHistoryPaging.ts#L325-L374)总是至少放入一条 row，以免超大条目卡死分页。因此 1 MiB 是普通页预算，不是任何输入下的绝对上限。Fleet 若增加按 turn 投影，应明确定义单条超限的截断、分片或按需详情，不能把同一条件复制后声称硬上限。
- T3 的 `filesystem:read` [范围是 server OS 用户可读的文件](https://github.com/pingdotgg/t3code/blob/1bc44f2732ce0a24c0608626d59409f1a0893db4/docs/internals/04_environment-auth.md)。它的 project 是组织单位，不是 filesystem sandbox；Fleet 的项目白名单、path redaction 和 operator API 范围继续按自己的权威合同执行。
- T3 有显式 `reasoning` item；这不等于可以获取任意模型隐藏推理。Fleet 当前 public event 合同排除内部 reasoning/system/raw source。未来若增加可见“思考摘要”显示，只能使用 provider 确实公开给客户端的内容，明确来源和类型、脱敏与截断，不能把内部 source_record 或加密/未知 block 当正文透传。
- T3 的 `AntigravityAcpSupport.ts` 对旧版 runtime 有 clientInfo 兼容分支；新接入应只使用真实的 Fleet client identity，并依照本机受支持版本和握手能力降级。不要复制对其它客户端名字的模拟逻辑。

## 对 Fleet 的优先级

1. **P0 — 每轮续聊成果绑定。** 已实现 command 绑定的逐轮结果、durable draft、崩溃恢复、
   有界结果分页/详情及上传重放；仍须走发布门禁。继续复用 Task、session operation receipt 和公开 task result
   API；增加基于 command/native turn identity 的 per-turn result，而不是重写会话服务。
   先限定 managed Runner + 已登记 Git workspace，并测成功、部分失败、截断、无改动、续聊并发、
   response loss 与 task/session 错绑。
2. **P1 — Agent 能力与采集完整度账本。** 已加入只读、带来源/版本/质量的 evidence DTO 与
   持久化采集事实；对 Codex、Claude、Pi、ZCode CLI/desktop、Hermes、
   agy 的版本和来源逐项列出可见 delta/snapshot、message/item identity、tools、approvals、
   model/usage、resume、stop、历史深度及平台安装状态；每项用来源证据/unknown 标记。管理按钮
   仍由授权 DTO 决定。
3. **P1 — 活跃会话延迟与大 timeline。** 对 source emit → node spool/upload → Hub cursor →
   browser render 做延迟分布；只优化活跃会话拉取频率及 timeline 跳转/锚点/按需历史页。
   真实存档不丢事件，离开会话退避，旧 cursor 仍从 API 恢复。
4. **P2 — 历史 metadata 回填。** 在现有 exact native ID/cwd 只读快照下更新旧 title/model/provider/start
   metadata，并记录更新来源/质量；损坏、删除、复用 ID 或 revoked adoption 都 fail closed。
5. **P2 — agy ACP feasibility。** T3 模式的 ACP node adapter 已实现并由独立对端及 Hub 集成
   验证；真实 Agy handshake、审批、resume 和模型仍需按 macOS/Windows 分别实测，且不得把
   fixture 结果记为 vendor 验收。

不照搬 T3 完整 Effect.js runtime、host WebSocket/RPC 连接、Electron/React Native 客户端、
完整 provider catalog 或 MCP 编排器。它们依赖其单机工作区所有权模型、账户和产品范围，
会给 Fleet 增加第二套控制面。本轮没有将上游实现代码引入 Fleet；若今后有代码借用，需单独做
许可证、来源 attribution、依赖与安全审查。

## 本轮验证记录

为核对 Fleet 当前行为，运行现有相关测试：

```sh
python3 -m pytest tests/test_native_session_api.py tests/test_native_transcript_detail.py tests/test_agy.py tests/test_agent_capability_layers.py tests/test_boundaries.py tests/test_frontend_separation.py -q
```

结果为 **104 passed**。该结果只验证此版本的现有行为，不构成上文建议能力的交付或真实
Agent/模型验收。183 个上游文件核对 Git blob SHA 和 SHA256；源码清单 SHA256 为
`de7b18083bc5585db55bbb7c3bf5f4435b07dbb0a0fb08c6496a8539931a12b5`。
另检查固定 commit 源码引用、相对文档链接、`git diff --check` 和本轮文档的 UTF-8 无 BOM。
缓存清单用于本机复查，不是产品依赖。

## 结论与未完成事项

T3 最值得 Fleet 吸收的是 **每层清晰的 capability/evidence contract、有界且可恢复的
thread projection、按轮次区分 Agent 完成与结果收尾，以及高保真协议回放测试**。Fleet
自己的优势和职责仍是多 VM outbound node、operator authorization、signed receipt、
native session capture 与跨平台 CLI 生命周期。现有幂等、分页、会话事件身份、授权和
静态前端隔离无需重新实现。

尚未完成：旧会话 metadata 回填、活跃 timeline latency/virtualization 测量、真实双端
`agy acp` / 外部模型验收，以及当前分支的完整 release journey、main 集成和生产部署。
本轮没有运行真实 Agent CLI、外部模型或 T3 程序。
