# 工作流执行、交接与验收契约

日期：2026-10-21。状态：schema v1 的 F1 与 F2.1–F2.5 已落盘；两个 gate 默认关闭，worker 显式启动。F3 正在贯通，完整交付与真实试点尚未完成。上位设计见[总体设计](2026-10-20-workflow-platform-design.md)。

## 1. 命名、版本与作用域

新增 WorkflowRun 表示多步骤交付，既有 Run 仍表示一次原生助手执行。旧 Task 表示 CLI 编码任务，Command 表示受控工具命令。禁止把其中任何一个 ID 当作另一个 ID 使用。

所有持久记录携带 owner_id；涉及文件的记录携带 workspace_id。owner 从已认证上下文取得，不能由请求正文覆盖。节点凭据只能提交其获准目标的回执，不能调用 operator 管理接口。

时间持久化为 UTC；显示使用用户时区。schema_version、模板 revision、策略 revision 和 adapter revision 分开记录。运行实例冻结定义快照，修改模板只影响新实例。

定义中的上游输出使用结构化引用。步骤进入 ready 时解析为具体不可变输入，生成 resolved_contract_digest。不得把未解析的“最新文件”作为真正执行输入。

## 2. WorkContract

WorkContract 必须描述以下字段；全部未知字段拒绝，不能通过自然语言扩展权限。

| 字段 | schema v1 要求 |
|---|---|
| schema_version | 精确整数 1 或 2，bool 不算整数；2 仅扩展显式 memory revision 0，兼容屏障见接入契约 §7.12 |
| objective | 非空 UTF-8，最多 4096 bytes |
| input_refs | 最多 16 项，类型为 artifact、code_revision、prior_step_output |
| output_specs | 最多 16 项；每项有 output_key、kind、required、最大字节数或结果 schema |
| capability_requirements | 最多 16 项；管理员目录中的逻辑能力及允许版本 |
| policy_ref | 已授权策略 ID/revision；执行时再次检查撤销状态 |
| validator_refs | 最多 16 个固定 validator ID/revision；不能传 executable 或 shell |
| budget | max_tokens、max_model_requests、max_active_seconds、max_wait_seconds、max_generations |
| retry_class | read_only、reconcile_before_retry、manual_only 三选一 |
| context_refs | 已授权的有界摘要、资料和显式 MemoryItem revision；不携带凭据 |

policy_ref 固化审计依据，不代表永远有效。模板、请求、owner、工作区和节点本地权限取交集；运行期间新撤销的权限立即阻断下一次 admission。

code_revision 输入包含 opaque repository_id、base_commit、head_commit、tree_id 和源码包 artifact 引用。Git 对象 ID 允许经校验的 40/64 位十六进制形式；不能使用分支名代替固定版本。repository_id 不能编码任意宿主路径或凭据 URL。

subject_digest 使用规范 JSON 计算 SHA-256，覆盖固定代码版本、合同指定的待验证交付产物完整 hash，以及影响交付语义的输入。评审/测试报告属于证据，不放回它所验证的 subject，避免自引用。字符编码为 UTF-8，键排序、紧凑分隔，不接受 NaN/Infinity、非法 Unicode 或重复 JSON 键。

摘要输入最多 64 KiB、嵌套深度 32；原始 JSON 解析前检查字节上限，解析时拒绝重复键。内部 owner/workflow/step ID 及 evidence key、validator 紧凑引用最多 128 UTF-8 bytes，禁止空值与 NUL。外部 JSON 的字段和权限仍按完整 schema 检查，摘要 helper 不替代解析器。

每次 validator 的 recipe/revision 另列为 validator_ref；报告必须同时绑定 subject_digest 与 validator_ref，避免对相同文件使用过期规则。

### 2.1 三种摘要的固定含义

| 摘要 | 输入与用途 |
|---|---|
| resolved_contract_digest | 展开的完整合同、已解析输入、模板/策略/adapter revision；冻结步骤语义，不含 lease、时间或模型输出 |
| subject_digest | schema_version、repository/base/head/tree、按 output_key 排序的交付 artifact_id/sha256/size、固定语义输入；评审和测试读取同一个主体 |
| delivery_digest | workflow/template revision、当前 step_key/generation/subject、当前产物引用、排序后的 evidence key/validator_ref/结果/报告 hash、接受规则 revision；绑定用户正在接受的具体交付 |

上表数组按稳定键排序后计算 canonical_digest；存在重复键/重复 output_key 时拒绝，不靠数组插入顺序区分同一主体。delivery_digest 不包含 acceptance 本身、workflow CAS revision、时间、lease 或普通进度事件，避免接受动作/刷新改变被接受对象。新增有效证据、产物替换、generation 变化或接受规则变化均使原接受决定失效，旧决定仍留审计。

三者不互换。subject 不因评审报告生成而改变，但改验证配方后必需 evidence 的 validator_ref 改变；delivery_digest 随当前证据改变。尚未形成完整交付时不能发起 acceptance；拒绝/返工使用步骤控制入口。

## 3. WorkflowDefinition

schema v1 是最多 8 个步骤的线性链。step_key 使用模板内稳定 ID，每项 requires 必须恰好指向前一步；第一步 requires 为空。不允许循环、分叉、任意跳转、递归创建 Workflow 或隐式生成新角色。

步骤 kind：

| kind | 执行绑定 | 完成依据 |
|---|---|---|
| native_run | 现有 Run/NativeAssistantRuntime | 原 Run 终态及其受限结果 |
| coding_task | 现有 TaskService/CLI adapter | task/attempt 回执和固定版本产物 |
| fixed_tool | 目录登记的固定命令/工具 adapter | Command 回执及必要 post-check |
| external_agent | 声明能力的 ExternalAgentAdapter | 外部任务标识、可核对状态和导出产物 |
| validation | 管理员登记的固定 validator | 当前 subject_digest 的证据 |

人工最终接受是 WorkflowRun 的独立动作；需要中途授权的步骤使用 waiting_approval，不借此新增一种可绕过原审批的工具。

模板示意：

~~~json
{
  "schema_version": 1,
  "template_id": "coding-delivery",
  "revision": 1,
  "steps": [
    {"step_key": "develop", "kind": "coding_task", "requires": [], "contract_ref": "develop-v1"},
    {"step_key": "review", "kind": "coding_task", "requires": ["develop"], "contract_ref": "review-v1"},
    {"step_key": "test", "kind": "validation", "requires": ["review"], "contract_ref": "test-v1"}
  ],
  "acceptance_required": true
}
~~~

contract_ref 在模板登记时展开为完整合同；运行快照内不保存会随目录修改而变化的悬空引用。示例只表示结构，不是现有 API 输入。

`coding-delivery@1` 的固定输出与验证规则：

| 步骤 | 结构化输出与放行要求 |
|---|---|
| develop | 必需 CodeRevisionRef 与 source_bundle；先用固定 exporter 校验 commit/tree/包一致，形成 source subject |
| review | 只读 review_report，包含 source subject、verdict=passed/failed/inconclusive、有界 findings 和实际执行身份；固定 review-report validator 验格式/主体，verdict 是 Agent 判断来源 |
| test | 固定 recipe 的报告与 JUnit，包含 source subject、recipe revision、exit code/结果；结果由固定测试器核查，不采信聊天自述 |

review/test 的报告分别存 artifact 并生成不同来源的 evidence；结构校验通过不等于 review verdict 通过。必需 validation 的通过规则在模板登记时固定，模型不能临时降低。

v1 创建入口只能实例化管理员已登记的模板。模型可提出符合模板的任务参数；新模板、工具权限和高风险步骤必须经原管理入口配置。

### 3.1 F1 注册快照的精确结构

以下细化已有合同的字段编码，供纯领域解析器与后续 registry 共用；它不是向 operator 开放任意模板创建的 API。`parse_definition(bytes)` 只接受展开后的注册快照。上文 `contract_ref` 示例是展开前的结构示意；展开后每步使用 `contract`，不可同时出现或保存悬空引用。

- Workflow 根字段精确为 `schema_version=1`、`template_id`、正整数 `revision`、`steps`、布尔 `acceptance_required`。每步精确为 `step_key`、`kind`、`requires`、`contract`；至少 1 步、最多 8 步，所有步骤都是必需步骤。
- CatalogRef 编码为 `{id, revision}`；revision 为 1..9007199254740991 的精确整数。policy_ref 和 validator_refs 使用该结构。capability_requirements 每项为 `{id, revisions}`，revisions 为 1..16 个不重复的正整数，解析后排序。
- input_refs 每项有唯一 `ref_key` 与 `kind`。artifact 另含 `artifact_id/sha256/size`；code_revision 另含 `repository_id/base_commit/head_commit/tree_id/source_bundle`，source_bundle 是 `{artifact_id,sha256,size}`；prior_step_output 另含 `step_key/output_key`。该前驱必须是定义中更早的步骤，output_key 必须存在于其必需输出。
- context_refs 每项有唯一 `ref_key/kind/ref_id/revision`，kind 为 memory 或 summary；summary 另有完整 `sha256`。WorkContract 1 的 revision 仍为正整数；WorkContract 2 仅对 memory 允许 0。外层 definition/resolved schema 1 允许嵌套的两种合同，持久 schema 2 前必须在原事务提高 Workflow reader 至 6，见[接入契约 §7.12](2026-10-20-artifact-capability-integration-contract.md#712-外部-agent-的显式-memory-输入与版本权威)。不接受内联凭据、任意路径、URL 或未固定版本的“最新”引用。input/context 的 ref_key 在同一合同内不重名。
- output_specs 每项为 `output_key/kind/required` 加格式约束。kind=artifact 必须有非负 `max_bytes`；kind=code_revision 或 structured 必须有内联对象 `result_schema`。schema 只冻结 JSON 数据，F1 不宣称执行任意 JSON Schema validator；不允许外部 `$ref`、`$dynamicRef` 或 `$recursiveRef`。schema 的目录解释与验证由固定 validator 负责，不由文本运行 shell。
- budget 必需 `max_tokens/max_model_requests/max_active_seconds/max_wait_seconds`；前两者允许 0，后两者必须为正整数；max_generations 可省略并规范化为 3。所有预算整数上限为 JSON 安全整数；实际能力/管理员预算可进一步收紧，不得因合同数值大就扩权。
- 注册 ID 使用 1..128 bytes 的 ASCII `[A-Za-z0-9][A-Za-z0-9_.:-]*`，且不含 `..`；owner 来自认证上下文，不走该注册 ID 编码。artifact_id 保持既有 opaque ID 语义。Git 对象为 40/64 位 hex，解析后小写，同一 code_revision 的三项 Git 对象长度一致。sha256 规范为小写 64 位 hex。

纯领域 `resolve_contract(definition, authorized_inputs)` 的第二参数由可信应用服务构造，携带 owner_id、workspace_id、step_key、固定 adapter_ref，以及已查证的有界作用域引用。每个绑定有 ref_key、owner/workspace、具体不可变引用；前驱产物另带 producer step/output 身份。解析器拒绝跨作用域、漏项、多项、重复键、具体引用不符或前驱输出身份错配；前驱产物还须匹配声明的输出 kind 和 artifact 字节上限。structured 输出通过 artifact 引用承载 JSON，内容 schema 由固定 validator 核查。input ready/当前代数、权限撤销、资源和审批仍由应用服务核查，不能把“形状合法”当作授权。

解析/冻结保持零 I/O；快照使用不可变 dataclass、tuple 和 bytes，调用方不能通过共享 dict 更改已固定输入。完整 JSON schema 运行、数据库和 API 不属于 F1。JSON 源码在解码前检查字节/嵌套上限，重复键与非法 Unicode/数字错误保留异常链。

## 4. 状态与交付投影

### 4.1 WorkflowRun

execution_state：

~~~text
queued → running
running → waiting_node | waiting_external | waiting_approval | paused
等待/暂停 → running
非终态 → cancelling → cancelled | unknown | succeeded | failed
running → succeeded | failed | unknown
~~~

draft 只存在于用户编辑的模板/参数，不作为已经提交的执行实例。当前执行终态为 succeeded、failed、unknown、cancelled，普通 tick 不会从终态自动再派发。对 succeeded/failed 的显式 retry/invalidate 可在 CAS 与预算检查后创建新步骤 generation，使 Workflow 回到 running；旧步骤终态不改写，旧交付决定失效。cancelled 不重开，需新建流程；unknown 必须先核清或在独立隔离资源中新建流程，不能借 retry 绕过未决副作用。

validation_state：not_requested、pending、passed、failed、inconclusive。

acceptance_state：not_requested、pending、accepted、rejected。用户接受记录绑定当前 delivery_digest 和 expected_revision；接受行为不能改变验证结果。

派生 delivery_state 的优先级：

1. 执行 unknown：needs_reconciliation。
2. 执行 failed/cancelled：execution_failed/cancelled。
3. 执行非终态：in_progress。
4. 必需验证 failed：validation_failed；必需验证未全部 passed：awaiting_validation。
5. 最终接受 rejected：rejected；需要接受但未 accepted：awaiting_acceptance。
6. 其余：delivered。

execution_state=succeeded 只显示“步骤执行完成”。只有 delivery_state=delivered 显示“已交付”。没有要求验证的流程明确标注“未要求校验”，不能伪造 passed。

若上游执行成功但必需校验失败，且仍有下游未执行，Workflow 进入 waiting_approval，reason_code=blocked_by_validation；下游保持 pending。此时 delivery_state 仍是 in_progress，独立 validation_state/页面明确显示失败与返工动作。若全部执行均已完成而校验失败，execution=succeeded、delivery=validation_failed。不为完成线性链而强行执行被阻断的下游，也不把 Agent 的负面评审伪写成进程崩溃。

### 4.2 WorkflowStepRun

状态集合：pending、ready、dispatching、running、waiting_node、waiting_external、waiting_approval、paused、cancelling、succeeded、failed、unknown、cancelled、skipped。

关键迁移：

| 当前状态 | 触发事实 | 后继状态 |
|---|---|---|
| pending | 所有前置步骤执行完成且其必需验证通过，输入成功解析 | ready |
| ready | 输入、策略、预算与资源 admission 成功，intent 已落库 | dispatching |
| ready | 无合格节点/容量 | waiting_node，保存明确 reason_code |
| dispatching | 查回同 token 的既有执行绑定 | running 或相应真实终态 |
| dispatching | 可能已跨外部边界但无法查回 | unknown |
| running | 外部任务已接收且有可核对任务 ID | waiting_external |
| running/等待 | 收到当前 generation 的权威回执 | succeeded / failed / unknown |
| 尚未派发 | 用户取消 | cancelled |
| 已派发 | 用户取消 | cancelling，等待实际停止证据 |
| unknown | 独立核对、CAS 成功并保留审计 | 相应已证实终态 |

skipped 只表示模板中明确的可选步骤；schema v1 的必需步骤不使用 skipped。不能通过跳过失败步骤推进交付。

pause 先阻止后续 admission。正在运行的子执行继续显示真实状态；抵达安全边界后才呈现 paused。它不等同于暂停 OS 进程。resume 恢复协调，不重启已完成步骤。

### 4.3 三种不同的计数

- generation：同一步因显式重试或输入失效重新执行的代数，从 1 开始。
- coordinator_epoch：协调器领取/回收租约的 fencing 版本；重启领取不增加 generation。
- Run.attempt / Task attempt：既有子执行自身的尝试次数，继续由原仓库管理。

自动只读重试也创建新 generation，消费上限和预算。schema v1 默认 max_generations=3，即首次执行加最多两次重试；不是三个计数相乘后的隐式无限重试。

## 5. 持久化与事务边界

附加表使用同一个 platform.db，独立 workflow_schema_meta。不得直接把现有 PlatformRepository.SCHEMA_VERSION 从 3 提高而声称旧二进制可回滚；基础库版本和新增领域版本分别处理。

| 新表 | 关键约束 |
|---|---|
| workflow_schema_meta | 独立领域 schema 版本及最低兼容 reader |
| workflow_runs | opaque workflow_id；owner/workspace；定义快照/hash；状态、预算、revision；UNIQUE(owner_id, client_token) |
| workflow_steps | UNIQUE(owner_id, workflow_id, step_key, generation)；resolved inputs/hash、目标、状态、epoch 与租约 |
| workflow_dispatches | UNIQUE(owner_id, workflow_id, step_key, generation)；稳定 token、payload hash、binding、投递阶段 |
| workflow_events | PRIMARY KEY(workflow_id, sequence)；受限 payload；事务内追加 |
| workflow_evidence | validator、subject_digest、来源及不可变结果；同 evidence key 异内容冲突 |
| workflow_artifact_refs | owner/workspace、workflow/step/generation、output_key、artifact_id、完整 hash |
| workflow_budget_reservations | Workflow 预留、子请求归属和 settlement；不重复记录实际 Token |
| capability_reservations | owner/target/workflow/step/generation 的持久目标槽位；与 admission 同事务，保留未决执行占用 |

所有 owner/workspace 关联由带作用域的复合约束和应用层双检保证，不能只凭全局 opaque ID 认定归属。相同数据库文件上的多个连接仍不是同一事务。

步骤状态、intent、预算/槽位预留和 admission 事件需要一次事务提交；事务外才调用既有执行器。子 Run/Task/外部任务创建使用稳定 token，允许协调器在提交关联前崩溃后查回。

唯一派发 token 定义：

~~~text
sha256("workflow-v1" + NUL + owner_id + NUL + workflow_id
       + NUL + step_key + NUL + decimal(generation))
~~~

相同 token + 相同 payload_hash 返回原绑定；不同 payload 返回 dispatch_conflict。coordinator_epoch 不进入 token，避免回收租约生成第二个逻辑任务。

旧 Task bridge 的 2000 字符 instruction 限制仍然有效。超过上限时使用节点获准读取的上下文 artifact；该能力尚未就绪时返回 context_too_large，不默默截断目标。

### 5.1 F2.1 存储与恢复的精确边界

`WorkflowRepository` 必须显式 `init()`，在兼容的基础平台数据库上增加表；构造对象不建表、不建目录、不启线程。F2.1 原基线为 schema=3；后续 MCP 目录将基础 reader 提至 4 时，Workflow 初始化必须先验证独立目录 schema 再接受该版本，见[接入契约 §7.8](2026-10-20-artifact-capability-integration-contract.md#78-mcp-目录持久化与跨进程审批)。Workflow 域保留独立 schema=1；F2.1 初始 minimum_reader_version=1，Workflow 本身不修改基础版本，后续附加域按各自合同提高最低 reader。F2.1 交付时仅有内部存储接口；F2.5 后的 bootstrap/HTTP 装配见 §9.1，不能把早期边界当作当前实现状态。

`transaction()` 为短写事务；读取使用同库的只读事务，不占 SQLite writer。复用 connection 时核查数据库路径、事务和 foreign_keys，并以 savepoint 保证调用方捕获失败后也不留下部分 admission。初始化逐条执行 DDL，失败整体回滚；将来 schema 和不完整附加表失败关闭。

流程创建绑定 owner/workspace/可选 conversation，定义快照和解析值必须一致；同 owner/client_token 同内容查回，不同内容冲突。每步骤的 validation 初始值按 validator_refs 选择 pending/not_requested。定义与已解析输入的 artifact 引用同时保存；`workflow_artifact_refs.output_key` 在存储层使用 `input:ref_key` / `output:output_key` 区分引用；外部 key 本身不因此前缀改变。

admission 事务要求当前 revision、owner/workspace、有效 coordinator epoch、current generation、ready 状态及 open 模式。步骤的预算对历代 reservation 累计，未知结算继续占用预留；本切片不进行实际用量结算。槽位通过持久 `target_key` 计数，调用方必须使用固定资源目录的唯一键与容量，用户/模型不能提供容量。F4 再接能力选点；F2.2 才将 intent 绑定真正执行。

备份即使 gate 关闭也检查现有 schema/最低 reader、复合外键、定义与 resolved 快照摘要、全部输入/输出/报告的 owner/workspace/hash/size。历史引用使用 rowid keyset、每批 128 行扫描，不一次载入全部历史。备份 envelope 在含 Workflow 时增加 `workflow={schema_version,minimum_reader_version}`，无此域的旧备份保持兼容。明文和加密恢复在发布恢复目录前把 admission_mode 改为 frozen，并重建恢复副本的数据库 hash；原备份不改写。租约与 reservation 保留，恢复不推断旧执行已停止。

### 5.1.1 原生 Run 检查点与安全恢复

Workflow 的 `native_run` 仍由既有助手 Run/worker 执行，恢复状态保存在原 Run 行的私有、最大 8 MiB JSON 检查点，不创建第二套执行引擎或公开接口。每次读取/写入都须核验 owner、Run、未过期 lease、worker 身份；检查点版本、阶段、计数和完整结构均需验证。检查点不进入公开 Run 投影。

可恢复阶段为 `ready`、`response`、`receipt`、`complete`，表示原输入/步骤、已保存模型响应、已保存工具回执或终态已持久化。worker 在 provider 调用前记为 `provider_pending`，工具分派前记为 `tool_pending`；如果进程在任一不确定窗口退出，不能推断外部副作用是否发生，必须保留 `unknown`，不得重放模型/工具调用。旧 Run 无检查点以及缺失、损坏、超限或格式不匹配的检查点均使用保守恢复并保持 `unknown`。已持久完成结果可在配置被停用后直接返回，不应重新打开 provider/workspace。恢复要保留 transcript、step、原响应/回执和累计用量；只在原 lease 围栏内继续写回。

### 5.1.2 原生远程命令等待与 worker 槽位

Native worker 遇到已派发但仍等待 Node 回执的既有 Command 时，保存原始等待响应及稳定 `run_id:step:N` 命令身份到 `remote_wait` 检查点，随后释放全局 Run lease/worker 槽。`runtime_wait_command_id` 是私有的工作区 reservation：Run 暂停、被重新 claim 与进入 slot admission 的间隙仍占用其原 workspace 容量；reservation 只对有效等待生效，并在终态或恢复副本 fencing 时清理。查询未完成等待使用零本地等待观察原 Command，不重新 enqueue/dispatch、创建新 token 或调整其原截止时间。独立 owner/command 覆盖索引使容量判定只扫描活动等待，不随终态 Run 历史线性增长。

取消请求与 Node claim 必须在原事务围栏中串行：取消先提交时不再派发原未领取命令；Node 已领取而结果不确定时保持 `unknown`，不得用本机 Run 取消假装远端命令已停止。等待回执先于 provider、workspace 与重新启用状态检查，以免配置停用阻断对原命令事实的观察。恢复备份会将未完成原生执行保守 fence 为 `unknown` 并清除等待 reservation，不推断旧实例或 Node 已停止。

会话只要被 workflow_runs 直接引用或已派发 binding_run_id 引用，就拒绝永久删除，返回 `conversation_workflow_referenced`（HTTP 409）。此检查独立于功能 gate。

### 5.2 F2.2 执行绑定边界

`StepExecutor` 是跨执行存储/进程边界的异步 port；明确声明 token 幂等与 token 查回能力，返回不可变 `ExecutionBinding`，另用 `inspect` 读取事实。SQLite 短操作通过受管 `asyncio.to_thread` 执行，不阻塞协调事件循环；每次 dispatch 有界超时。构造 adapter/service 不建表、不启 worker、不调用模型。

在 intent 已提交后，将投递阶段改为 sending 并提交，再于事务外调用 executor。恢复 sending 时先 lookup 原 token；查不到且接收端不能保证幂等，或查询本身失败，保存 unknown 与原因，保留预算和目标槽位。已绑定只 inspect，不重新 create。epoch/revision/current generation/target/payload 的 CAS 约束同样适用于阶段与 binding 写回；旧协调器不得覆盖新领取者。

首批具体 adapter 复用平台 Run。每个固定 adapter revision 的可信登记项绑定 owner/workspace、target、policy、既有 conversation 和已授权 Run config；登记不是用户请求字段，也不根据 objective 扩权。完整 canonical resolved JSON 作为消息内容。Run/message ID 从稳定 token 派生，与 Workflow ID 不同。相同 token/hash/固定配置查回同一 Run，配置或 payload 改变返回 dispatch_conflict；查回在当前目录可用性检查之前进行，权限撤销阻止新执行但不隐藏旧事实。

coding_task 适配在同一 platform.db 事务内创建 message、waiting_task Run 和既有 legacy_task_links，普通 Run worker 不会抢到中间 queued 状态。后续 Task 仍由原 LegacyTaskBridge 以原 owner/真实 Run ID 派生 token 创建/查询；没有创建另一套 Task 引擎。为了覆盖创建后/最终 binding 前崩溃窗口，同事务保存 dispatch 的 binding_run_id 供会话引用保护，最终 binding_kind/binding_id 仍由 fenced 写回完成。

完整 legacy instruction 超过 2000 字符时，在任何 Task/Run 创建前返回 context_too_large；本切片不声明节点已能读取 context artifact。原 Run 消息仍遵守 32 KiB 上限。确定未开始的拒绝保存 step.failed 与受限原因；dispatch 阶段保留历史 intent/sending，observation 标记 not_started。超时或不确定异常保存 unknown，保留 cause；调用者取消使 sending 留待查回，不把取消请求当作停止证据。F2.3 已聚合步骤/流程状态；F2.4 再处理停止和实际用量结算。

### 5.3 Native Run 的显式完整 Memory

本节扩展 §5.2 与接入契约 §7.12 的 Memory 权威，仅接通 `native_run` 的本地模型输入。显式 owner/revision（含 WorkContract 2 的 revision 0）、完整字段、一次最多 16 项选择和当前 workspace 授权保持原合同；不搜索、摘要、截断或选择最新值。summary、CLI/固定评审/测试、远程 Node 或 capability-bound context 继续明确拒绝，不把引用本身冒称正文已消费。结构化外部输出是独立实施项；用户已选择已有 Hub ArtifactRef 交接，见接入契约 §7.13，本节不实现 exporter。

RunStepExecutor 在创建 message/Run/budget child link 的同一个 SQLite 写事务内，通过现有 owner/ID 索引批读完整 Memory，固定私有 `workflow_memory={schema_version:1,resolved_digest,inputs:[{ref_key,item:<完整 Memory 值>}]}`。每个逻辑 item 的 canonical 字节累计最多 8192 bytes；重复 ID 同 revision 只读一次但重复逻辑输入计入预算。私有快照最多 32768 bytes；固定系统消息为资料提示加同一 canonical 快照，最多 32768 UTF-8 bytes。超限明确 `context_too_large`，所有字段完整保留。无 context 的旧 Run 配置、消息和 digest 字节不变；新标记不可由 target config 或普通 turn override 注入，不与旧可截断 `memory_context` 混用。

持久化该私有快照前在原事务提升 Workflow minimum reader 至 7；schema 仍为 1，无新增表或列。旧库不主动升级，reader 7 仍读取 reader 1–6；低 reader 标记、缺少/损坏/非规范私有快照、引用/正文/原 message 或配置摘要错配均失败关闭。Run 查回优先返回原事实，Memory 编辑/禁用/删除不重建原 Run，不替换其正文。完整历史初始化与备份按 dispatch rowid keyset 每批 128 条核对，包括非正 rowid；明文/加密恢复保持 frozen，保存历史原值，不要求当前 Memory 仍存在。

原 LocalRunWorkerService 在首次 provider factory/模型调用前，通过注入的 I/O guard 核对 Run owner/当前租约、原 dispatch/binding/current generation、原 resolved/message/配置摘要和 reader，然后在同一只读快照批读当前 Memory，比对 enabled、精确 revision 和全部原值。绑定尚未最终写回时复用原安全回队；其他失权/不一致失败关闭，不调用模型。授权读快照是本次开始执行的线性化点；不承诺中断已经开始的模型调用。worker 从固定快照生成独立 reference-only 系统消息，不使用旧截断 renderer；Memory 不扩大 ToolBroker/MCP 工具、策略、验证或接受权限。原 Run 租约/心跳/取消/unknown 和 Workflow held/isolated 继续有效；已开始中断不自动重放。HTTP DTO、事件和错误仅保留计数/稳定错误码，不附私有正文或快照。

有界索引批读与 canonical 校验为 O(n+B)，n≤16、B≤8192，字典匹配均摊 O(1)。异步派发/解析复用 owned I/O，worker 沿用原同步执行入口；SQLite/Flask 不因此宣称完全异步。无新队列、worker、通用内部门面或第三方依赖。实施与测试见[Native Memory 计划](../plans/2026-10-26-native-workflow-memory.md)。

### 5.4 本地 Native Run 的完整文本产物输入

本地执行沿用原 worker 的 `execution_node_id` 语义：缺省、空字符串、`local`、`hub`、`host` 都是本地别名，不要求额外 Node 目录登记；其他 ID 属于远程 Node，不支持本节完整文本消费。

本节在 §5.3 之上接通已授权 ArtifactRef 的实际正文，复用接入契约 §7.11 的完整有界文本规则。只有可信 `RegisteredRunTarget.inline_text_inputs=True` 的本地 native_run 才声明消费正文；该字段为精确bool，默认False，不能由operator、合同正文或模型设置。启用目标必须具备固定provider_snapshot，且不能是CLI、固定评审/测试、源码采集、remote Node、capability-bound或旧memory_context执行。目标支持声明与实际私有输入参与原Run配置摘要；同adapter revision的配置漂移不能换原任务输入。未启用目标保留旧合同JSON/引用行为，不冒称模型读到了产物正文。无input_refs时原消息和配置字节不变。

首版只消费kind=artifact的精确已解析引用。code_revision/二进制/其他类型在开始模型I/O前拒绝，不下载包、不解码媒体、不解包或执行输入。允许的MIME为text/plain、text/markdown、application/json；JSON在此仅作为完整参考文本，不从内容派生工具或校验权限。逐个owner/workspace已发布manifest与caller-owned verified descriptor核验ID、hash、精确size和完整UTF-8正文，拒绝短读、超长、篡改及错误scope/MIME；同一ID仅读取一次，冲突引用拒绝。每个逻辑文本输入的全部UTF-8字节与全部逻辑Memory canonical字节共享8192 bytes预算；重复逻辑引用重复计费。input_refs与context_refs各最多16项；所有字段保留，不截断正文、转摘要或只传预览。已声明完整消费的目标缺少reader时失败关闭，不回退为引用JSON。

首次创建按原token优先查回已提交Run及私有快照，当前文件/Memory变化或reader暂不可用不替换原输入；原事实查回不进行文件I/O。无原Run时在原owned I/O生命周期内、SQLite写事务外采集有界正文；随后原message/Run/dispatch/budget child link事务复核当前owner/workspace、原intent/generation/控制和目标，批读当前Memory并固定全部值。并发创建在写事务再次查回同一Run；采集失败或最终授权失效不留下部分Run、消息、reader升级或child link。

复用私有workflow_memory字段：无文本的schema_version=1快照完全保持；有文本时为schema_version=2，精确字段为resolved_digest、inputs（原Memory数组）和text_inputs（按合同顺序的ref_key/artifact_id/sha256/size/content_type/text）。输入内容只进入该原私有配置快照，原user消息仍是canonical resolved JSON。可信配置另外固定workflow_text_mode={revision:1}；有mode却缺快照/正文、有文本却缺mode、错误目标或不符合同均失败关闭。schema2快照和reference-only系统消息各最多32768 UTF-8 bytes，最终Run配置仍最多65536 bytes。text与Memory的SHA、scope、顺序、重复定义和原配置摘要必须完整对应。

在保存schema2私有快照的同一事务将Workflow minimum reader提高至9；schema仍为1，不新增表/列。旧schema1 Native Memory与reference-only Run继续按原格式读取，不补写正文或追认新权限；新启用目标缺原schema2时查回冲突，不重新采集。reader9读取旧1–8，新text记录即使gate-off/备份也要求reader9。历史以原dispatch signed-rowid每页128行joined校验全部快照、消息、绑定、输入引用与配置摘要，不逐项查库或依赖当前Memory/文件；普通/加密恢复仍frozen，所有原引用进入既有完整闭包。

worker复用原Native Memory I/O guard，在provider factory之前读取原当前Run/lease、dispatch/binding/generation、控制/截止和workspace授权，并核验私有快照/原配置/消息。文本字节以原已核验私有快照为权威；queued时不再采集或替换正文。当前已发布manifest/scope/MIME核验在SQLite事务外，之后使用注入时钟再次以只读事务复核完整原绑定及当前Memory值；第二次读取是执行授权的线性化点。guard/已发布引用失效、配置/内容/Memory不一致或最后截止到期均在模型I/O之前拒绝，保留cause和稳定错误码。没有guard不能消费正文；绑定尚未最终写回时只按原规则安全回队。已经开始的模型调用继续按原租约/取消/unknown合同收尾，不宣称撤权即时终止模型或自动重放。

系统消息明确把全部输入作为参考资料，不授予工具、MCP、策略、验证或acceptance权限。HTTP DTO、Workflow/Run事件、错误及repr不附私有正文、mode或快照；普通turn override与目标config不能注入受信任标记。构造和gate-off默认路径无文件I/O、线程、网络或新队列。正文采集与当前授权复用一个通用文本artifact I/O port，外部Agent文本输入仍保持原请求/错误码/字节合同；不新建空壳内部接口或第二执行器。

复杂度O(n+B)，n≤32、B≤8192；ID匹配/去重均摊O(1)，索引批读Memory和既有引用，不在未限制历史上全表读取。验证必须覆盖实际provider收到完整Unicode/空文件/混合Memory、默认bootstrap与上游ArtifactRef输出到Native执行、重复读取/预算、错误scope/hash/size/MIME/UTF-8、缺reader/guard/marker、原事务回滚/双进程创建/取消收尾、采集与当前权限竞态、重启原任务不重读、旧字节、reader9和frozen明文/加密历史末页闭包。实施见[Native文本输入计划](../plans/2026-10-27-native-workflow-text-inputs.md)，实现状态以实际代码与验收证据为准。

### 5.4.1 本地 Native Run 的固定源码包输入

只有可信注册的 `RegisteredRunTarget.inline_code_inputs=True` 本地 provider-backed `native_run` 可以消费 `code_revision`；默认关闭。CLI、review、fixed test、远程 Node、源码采集、capability-bound 和旧 `memory_context` target 不支持。合同中的固定 `repository_id/base_commit/head_commit/tree_id/source_bundle` 必须与当前 owner/workspace 发布的 `application/x-tar` manifest 完全对应；来源包通过 ArtifactStore verified descriptor 流式解包，并再次校验 Git tree/blob 与源码排除规则。每个 Run 最多16个引用、不同包总计10 MiB、不同版本解包文件总计4096；相同不可变 revision 共享一个只读私有视图。输入只读，不写入用户 workspace。

原 Run 私有 `workflow_memory` schema 4 在保持 `inputs` 原值后增加按合同顺序的 `code_inputs` 引用元数据；archive 与文件内容不写入 Run 配置。配置固定 `workflow_code_mode={revision:1}`，同一Run事务将 Workflow minimum reader 提升至12。历史闭包要求 schema、可信模式、resolved code revisions 与既有 `workflow_artifact_refs` 完全一致；schema 1/2/3 和无输入配置保持旧字节。已提交 Run 查回先于 artifact I/O，返回原固定快照；新建 Run 的 manifest 授权在 SQLite 写事务外执行，提交时仍使用原 dispatch/target/deadline 围栏。

启用的 worker 仅通过固定 `workflow.input.list` 与 `workflow.input.read` 工具按 `ref_key`、相对路径和有界分页/UTF-8 字节偏移读取；工具不返回宿主绝对路径，不提供写入、执行或额外工具权限。普通会话与未启用 target 不声明这两个工具。私有源码视图仅在本次 worker attempt 生命周期内存在；安全 `ready` checkpoint 恢复时重新核验并解包同一固定引用，已完成的只读工具调用不重放。provider/tool pending、旧 Run 或损坏检查点仍按原合同为 unknown，不自动重试外部副作用。

本机 Native provider 与安全 `ready` checkpoint 恢复由[代码包输入计划](../plans/2026-11-11-native-code-package-inputs.md)覆盖；完整历史备份/gate-off矩阵和完整 Linux/browser 发布门禁仍须单独通过。CLI/review/test/remote/capability 输入仍不支持。

### 5.4.2 本地 Native Run 的有界图片输入

只有可信注册的 `RegisteredRunTarget.inline_media_inputs=True` 本地、provider-backed `native_run` 可以接收图片 ArtifactRef。目标配置必须固定 `provider_snapshot.capabilities.image_input=True`；媒体模式不能与文本/源码 inline 模式、Memory、summary 或 capability-bound context 混用。Workflow 定义、operator 请求和模型消息不能声明此能力。默认关闭，非图片、code_revision、remote Node、CLI/review/test、缺少 reader 或未声明图片能力的 provider 均在模型 I/O 前拒绝。

媒体 MIME 仅允许 `image/png`、`image/jpeg`、`image/webp`。每项最多2MiB，每Run最多4项、原始字节总计最多4MiB；重复ArtifactRef只做一次物理读取，但按每个逻辑引用保留顺序。Run创建时，在SQLite事务外核对owner/workspace manifest、MIME、长度及完整SHA-256；原Run事务仅保存私有 `workflow_memory` schema5 的有序 `{ref_key,artifact_id,sha256,size,content_type}`，不保存媒体字节、base64或data URL，并将minimum reader提升到13。旧schema1-4、reader1-12以及未启用媒体的配置/消息字节不变。输入引用闭包、明文/加密备份与历史检查按既有ArtifactRef闭包校验，不要求原媒体字节进入数据库备份。

Worker在当前Run、lease、dispatch/binding/generation、截止、workspace和固定引用授权通过后，按同一不可变ArtifactRef重新打开verified descriptor并核对完整长度/hash；图像只在本次attempt的有界内存和Provider请求体中存在。安全ready checkpoint恢复重新读取相同引用，不从checkpoint恢复图像字节；pending provider调用仍遵守原unknown合同。OpenAI-compatible序列化器保留经过约束的text/image_url parts，请求体超过8MiB时在transport之前以`request_too_large`拒绝；旧纯文本消息wire保持不变。当前实现与验证见[Native二进制媒体计划](../plans/2026-11-12-native-binary-media-inputs.md)。

### 5.4.3 本地 Native Run 的有界音频输入

只有可信注册的 `RegisteredRunTarget.inline_media_inputs=True` 本地、provider-backed `native_run` 可以接收音频 ArtifactRef。目标配置必须固定 `provider_snapshot.capabilities.audio_input=True`；音频不与图片、文本/源码 inline 模式、Memory、summary 或 capability-bound context 混用。Workflow定义、operator请求和模型消息不能声明此能力。默认关闭，CLI/review/fixed-test/remote Node、缺少reader或未声明音频能力的provider均在Run binding前拒绝。

仅允许 `audio/wav` 和 `audio/mpeg`，分别序列化为Chat Completions `input_audio` 的 `wav` 与 `mp3` 格式。每项最多2MiB、每Run最多4个逻辑引用且原始字节合计最多4MiB；重复引用按逻辑数和总字节计费，但物理读取一次并保留每个输入位置。私有 `workflow_memory` schema6按顺序保存 `{ref_key,artifact_id,sha256,size,content_type}` 元数据，不保存音频字节或base64；同一Run事务将minimum reader提升到14。旧schema1-5、reader1-13、图片schema5及纯文本/图片请求wire不变。单个Run不混合图片和音频，以保持固定输入顺序及历史格式清晰。

Run创建、worker授权和每次真实attempt均重新核对owner/workspace manifest、MIME、大小、长度和SHA-256。音频只存在于有界attempt内存及发送请求；安全 `ready` checkpoint恢复重新读取原不可变引用，`pending` provider调用仍保持unknown且不重发。provider序列化器严格验证part字段、base64、格式、4引用/4MiB上限及8MiB请求体上限。该切片仅覆盖受控Native本机传输证据，不包含真实音频模型、CLI/review/test/remote context或生产验收；计划见[Native音频输入](../plans/2026-11-12-native-audio-inputs.md)。

### 5.4.4 本地 Native Run 的完整 MP4 视频输入

视频复用可信`inline_media_inputs=True`本地provider-backed Native目标，额外要求固定`provider_snapshot.capabilities.video_input=True`。仅允许`video/mp4`，完整原视频序列化为`{"type":"video_url","video_url":{"url":"data:video/mp4;base64,..."}}`；格式依据[vLLM当前多模态Chat Completions示例](https://docs.vllm.ai/en/stable/examples/generate/multimodal/)。图片或音频能力不授予视频能力；本实现不抽帧、转摘要或假定所有OpenAI-compatible模型接受视频。视频单独使用一个媒体模式，不混用图片/音频、Memory/summary、text/code inline或Node capability context。

沿用2MiB/单引用、4个逻辑引用、4MiB原始字节和8MiB序列化请求界限。重复位置保持完整顺序和逻辑计费，每attempt对同一不可变artifact只读取一次。原Run事务只保存`workflow_memory`schema7的有序`video_inputs`元数据`{ref_key,artifact_id,sha256,size,content_type}`，原`workflow_media_mode={revision:1}`不变，并把minimum reader提升至15；无视频的schema1–6、reader1–14和图片/音频wire保持不变。

Run创建、worker读取及读取后的授权复核沿用owner/workspace/manifest/hash/size、lease、binding/generation、取消/暂停和deadline的原权威。完整视频字节、base64与data URL只存在于本次attempt及provider请求中，不进入Run持久快照、checkpoint或事件。安全ready恢复重新读取原引用，pending-provider仍unknown且不重发。gate关闭、旧reader、损坏输入闭包及普通/加密备份按原历史规则校验，不用新空输入替代历史视频。

这完成本地Native MP4传输合同；真实视频模型、CLI/review/fixed-test/remote媒体context、Muse能力及生产业务验收仍须独立实现或验收。见[视频实施计划](../plans/2026-11-13-native-video-inputs.md)。

### 5.5 Hub 本地消费者与 Node 输入位置

传输 gate 启用时，输入位置按可信执行目标确定。固定命令评审、固定测试及 `inline_text_inputs=True` 的本地 Native Run 复用原 Hub worker/reader，不生成 `artifact.receive`。普通 reference-only Native、Legacy Task、远程 Node 和缺登记目标仍遵循原 Node 接收准入，不从 ArtifactRef 的存在推断已在目标机器就绪。

装配仅从实际 `RunStepExecutor.target` 派生不可变 `HubInputTarget`，精确固定 owner/workspace/target、adapter/policy 的 ID 与 revision、步骤 kind。登记最多 256 项，以联合身份建索引；外层 executor 注册身份不一致、重复登记或同作用域 Hub target 与 `workflow_node_inputs.node_id` 冲突均拒绝。冲突检查失败不改变原登记。登记不进入合同、HTTP、备份或持久豁免；启动必须由可信配置重新派生。operator 与模型不能选择本地消费或自报豁免。

协调器输入准备和仓储 admission 使用同一登记索引。任一作用域、revision 或 kind 不匹配时，默认仍要求原 Node 签收。当前 generation 已通过 input_target 固定 Node 后，即使后来相同 key 登记为 Hub，也不能跳过原节点、完整字节签收及恢复核验；直接 admission 同样失败关闭。原 unknown、held/isolated、retry、授权、正文预算和最终验证合同保持约束。

索引构建 O(t)、t≤256，身份查找均摊 O(1)，adapter 比较使用原有界 resolved 快照。回归见[输入位置修复计划](../plans/2026-11-01-workflow-input-location.md)，包含开启 transfer 的原 HTTP/CLI→源码→评审→固定测试→人工接受及完整文本 Native provider 链。该组合证据不代替真实两机完整 Workflow 或真实模型/商业 CLI 验收。

### 5.6 显式版本摘要上下文（2026-11-11）

本节扩展 §5.3/§5.4，取代早期实现中“summary 尚无权威、拒绝消费”的限制。摘要为 operator 明确发布的参考资料，独立于 Memory；不自动生成、搜索或截断，不把摘要当验证证据。每个不可变版本完整保存 `owner_id/workspace_id/summary_id/revision/title/content/created_by/created_at_ms/source_kind`，规范 JSON 另含 `schema_version=1`；SHA-256 覆盖全部字段。`created_by` 固定为认证 owner，`source_kind=operator`，时间由服务端提供，调用方不能伪造来源。

版本 revision 从 1 递增；head 的 epoch 随发布及启停递增。发布使用 expected_epoch，首次为 0；同 owner/client_token、相同原 payload 的重试返回原版本，即使之后已追加或停用，改 payload 则冲突。追加新版本不撤销旧版本；停用 head 阻止各版本的新消费，启用后可以再次显式选择。选择必须精确给出 summary ID、正 revision 与完整 hash，不跟随 latest。scope 为原 owner/workspace，不能跨工作区借用摘要。

单版本完整规范字节最多 8192；Memory、文本及摘要共享每请求 8192-byte 逻辑预算，重复逻辑引用重复计费，context 最多 16 项。Native 私有 `workflow_memory` 仅在含摘要时使用 schema 3，外部内层 prompt 同样使用 schema 3；有序 `summary_inputs` 完整保存每项 ref_key、summary_id、revision、sha256 与原版本 item。旧 Native schema 1/2、外部内层 schema 1/2 及无摘要的请求字节保持原样。第一次持久摘要在原事务创建独立 authority 域并提高 Workflow reader 至 11；外部含摘要请求同时要求 external metadata 4。开启 gate、构造 service 或只读空列表不创建摘要表。

Native 仍使用原 Run 事务保存完整私有输入，并在 provider 前的原 guard 复核当前 head/workspace 与精确版本；外部在原 prepare_send 写事务复核同样的当前权威与完整已采集值，防止采集后停用仍写发送意图。已存在执行查回始终读取原快照，不改成新 head、不重建正文、不因停用重发任务。历史核验不依赖当前 enabled，但必须闭合到原不可变版本；损坏或缺失权威、过低 reader 在启动、gate-off、原任务恢复及备份时均拒绝。历史以 signed-rowid 每页 128 行扫描，选定版本按索引有界读取。普通/加密恢复仍为 frozen，保留全部版本与启停状态。

仅本地 Native 模型和 external_agent 消费该上下文。CLI、固定评审/测试、远程 capability context 及二进制/代码包输入仍是后续独立范围；本节不增加工具或执行授权。摘要正文仅存在私有请求和显式摘要版本 API，不加入普通 Workflow/Run DTO、事件或 repr。operator API 与外部合同见[接入契约 §7.14](2026-10-20-artifact-capability-integration-contract.md#714-显式版本摘要权威与外部消费2026-11-11)，实施与验证见[摘要计划](../plans/2026-11-11-versioned-summary-context.md)。

## 6. 协调器算法

每 tick 默认最多检查 32 个 Workflow 和推进 64 个状态动作。注入 clock，查询使用 owner/状态/next_poll_at 索引。进程构造不自动启动线程，生命周期由既有 worker 入口显式管理。

~~~text
claim workflow（短租约 + epoch）
  → 读取有界当前步骤/未决 intent
  → 先对账既有 binding，处理取消/撤销/预算
  → 更新已存在执行与证据的投影
  → 若前置条件满足：冻结 resolved inputs
  → 事务写 admission / intent / 资源预留 / 事件
  → 事务外 create_or_lookup(stable_token)
  → CAS 写回 binding 与真实状态
  → 安排下一次有界检查，释放协调租约
~~~

协调器不能跨模型调用或长工具执行占有 SQLite 写事务。活跃外部任务不长期占用协调器线程；长等待只持有必要的任务/资源关系。

外部派发不承诺 exactly-once。承诺是稳定意图、接收端幂等、状态查回和未知结果不盲重放。查回失败不能推断任务不存在。

协调器租约与执行资源租约不同。前者过期可以回收协调权；后者只有确认执行结束/停止或已安全隔离才释放。不能仅因心跳超时就把仍可能活跃的资源交给下一代。

### 6.1 F2.3 协调器的实施边界

协调服务只持有依赖和可信注册配置，不保存某个请求的可变状态。`tick(now)` 是有界异步调用，仅等待短存储/submit/inspect，不等待模型或工具执行结束。每轮最多领取 32 个流程；按“已有投递优先、未派发其次”线性分组，动作预算最多 64。冻结、admission、投影各预留一个动作；dispatching 的 sending/binding/observation 最多三个动作，执行前先预留全部预算。无变化的 poll 不增加业务 revision 或重复事件。性能矩阵的 500ms 目标在无外部网络等待条件下测量。

同库的 `workflow_coordinator_owners` 保存有索引的 owner ready_at 与 service_sequence；领取和更新公平顺序同事务。每次从一个 owner 领取一个最早可用流程，再回到 owner 队列；相同 now 也按单调 sequence 轮转。队列是可重建的调度元数据，不是执行权威记录；显式 init 才创建/补齐，兼容既有 schema v1 核心表。owner/流程选择均使用有界索引查询，不在 tick 中 GROUP BY 全部历史记录。租约 expiry 只影响协调领取。

读取当前步骤、输出 key 和证据按一批最多 32 流程合并查询，分别限制 8/16/32 项；当前证据只按本代 subject 与固定 validator revision 投影，不能直接相信旧的 validation_state 缓存。observation、步骤状态、Workflow 投影、终态槽位释放和事件在同一短事务提交。确有停止证据的 cancelled 观察通过同事务的 cancelling→cancelled 迁移保存。旧 Task 的 cancel 只更改任务状态并栅栏旧回执；cancelled 或 expired 的 bridge/Run 投影不证明 runner 退出，当前 adapter 将其保守映射 unknown/legacy_stop_unconfirmed，待 F2.4 的确证核对。known terminal 的执行槽位可释放；没有 usage 的预算转 unknown settlement，预留继续保留；unknown 执行的槽位保持 held。

新 admission 仅接受可信注册的固定 definition digest、owner/workspace、step、resolved 合同、目标/容量/预留和对应 executor。未登记、必需产物缺失、上游验证未通过均明确等待。F3 的 `WorkflowArtifactInputResolver` 已提供当前前驱输出的有界批读和权限/hash/size 校验；code_revision 输出从有界 JSON artifact 读取并再次校验 source_bundle 的作用域。冻结仍以当前 revision/generation 做 CAS；memory/context 没有实际解析授权时明确拒绝。此解析能力不代表生产执行 adapter 已接通，不允许静态配置伪造前驱产物。

F3 的 `WorkflowCodingDeliveryService` 通过存储和 artifact I/O port 发布 source/review/test。`WorkflowDeliveryRepository` 要求真实 execution binding、当前 generation/revision 和固定模板；输出、证据、交付投影、事件及操作回执在同事务写入。旧代/错绑定/旧 subject 拒绝。操作请求摘要包括 workflow/step/generation/revision 及完整 execution binding，不能用相同 token 搭配另一执行身份查回。相同请求先查回 receipt，已存在报告的顺序重试不再次发布；并发发布中未被事务引用的临时冗余 artifact 仍由原生命周期管理。运行终态与输出齐备仍是独立事实，交付计算必须检查必需产物齐备。固定测试已按下一节接入原 Run worker；开发/评审的受控 fixture 不替代 CLI 完成后的采集与只读评审执行。

`start()` 显式返回 StopHandle；句柄拥有线程、事件循环及本轮子任务，stop 取消受管任务，join 等待它们与短存储收尾。SQLite 线程中的已开始事务不能靠 asyncio 取消强行中断，保留 sending/稳定 token 供恢复。异常保留 cause，协调层不重复日志，由 worker 最外层记录。bootstrap 受 F2.5 gate 和可信注册控制；构造 Flask 应用不自动启动协调器，worker 仍须显式 start/stop/join。

### 6.2 F3 固定测试的执行接线约束

固定测试使用现有 Run 队列、worker 租约、heartbeat、取消与崩溃恢复。可信 `RegisteredRunTarget` 可登记 `coding-delivery-tests@1`；仅匹配 `coding-delivery@1` 的 test/validation 合同。请求、模型或源码不能指定命令、配方或 sandbox launcher。该 Run 不调用模型，不暴露工具，不复用用户工作目录执行源码；worker 在私有临时目录中校验并解包本次固定 source_bundle。

worker 在执行前核对当前 Run lease、attempt、owner/workspace、完整 dispatch binding、当前 generation、源 subject、必需前驱评审证据与已启用工作区。协调器尚未保存 bound 时，未越过执行边界的 Run 可安全回队；一旦写入 run_started，租约过期仍按原规则变 unknown，不能自动重跑。测试配方没有已配置 sandbox launcher 时失败关闭。

解包后及运行中继续通过原进程控制回调检查当前 Run lease/attempt、工作区授权、完整 binding/current generation、步骤状态、流程取消、frozen 和 active_deadline。检查只读取固定数量的索引记录，不重复解析完整流程或读取产物；普通业务 revision 更新不触发重跑或错误撤销。流程取消只在实际进程组停止并回收后返回 cancelled；其他授权失效以明确错误停止，不发布测试证据。读取授权失败也先回收受管进程，保留原异常链。

输出提交与证据写入继续使用同一短事务，同时检查未过期的 Run lease/attempt 和取消状态。当前 revision 改变时，只允许在有界次数内重新读取同一 binding/source；不得重新执行测试来修复 CAS。提交回执可查回原 expected_revision；相同执行同内容的重试不再发布产物，另一 generation/绑定不能恢复该回执。进程执行完成且报告/JUnit 已发布可记 Run succeeded；测试断言失败作为 validation failed 保留，不伪装成执行基础设施崩溃。缺报告、失租、取消、冻结或源码失效均不能产生 passed evidence。

已提交回执是完成事实：丢响应后先用原完整 target/请求摘要查回同一 receipt，只在尚未提交且发生 revision 冲突时刷新当前 target。提交之后的取消、工作区撤销或 frozen 不撤回已完成的报告，也不再次执行测试。原 Run 的有效 worker lease 仍是终态写回前提；取消与终态写回竞争时，仅有同一固定 test Run 的完整 dispatch binding 和持久 test receipt 才能保留 succeeded。没有该完成证据的 Run 继续按原取消合同收尾，不能用成功意图或配置 marker 代替回执。

测试报告还记录可信 adapter 所观察的主机运行时、平台、backend 和已配置 launcher/固定 argv 的摘要，明确其观察范围；这不证明 sandbox 内部环境或真实隔离已验收。测试环境继续只使用 backend 的精确环境白名单，不继承 Hub 凭据。

### 6.3 独立远程固定测试 capability

远程 `test/validation` 目标使用独立 `coding.test@1` / `tool.workflow.test`，只执行
`coding-delivery-tests@1`，retry 固定 `manual_only`。它复用原 capability admission、
Run worker、签名 Command 和 Node journal；不进入模型工具目录。原 source 的 Node
接收回执确认前不得 admission；generation、token、binding、CodeRevisionRef 和
subject 必须逐项绑定，Node 只能从原签收 journal 的受保护 descriptor 流式消费。
Hub 核验完整报告/JUnit 后复用原 `record_test` 原子提交；发布冲突只重试提交，
不重放测试。未知保持 unknown 与 held/isolated。远程 Run 创建事务将 minimum reader
提高到 10，旧 reader 拒绝，恢复 frozen。

首版仅接受精确 Linux route，默认 gate 关闭；本机受控夹具不是实际 Linux Node
执行或生产隔离证据。完整 wire、大小限制、取消/恢复与平台边界见
[远程固定测试契约](2026-11-02-remote-fixed-test-capability.md)，实际证据见
[实施进度](../../platform/workflow-platform/implementation-progress.md#2026-11-02-独立远程固定测试能力)。

## 7. 版本变更、重试和恢复

同输入重试由 retry 创建新 generation；变更输入或重做成功上游由显式 invalidate 创建新 generation，并在同事务使所有消费旧 subject_digest 的后续步骤失效，其原证据保留但不再计入当前交付。新一代输入仍须通过模板和权限检查，不能修改原运行的权限/模板快照来获得额外能力。

存在 running/unknown 的旧下游时，先确认停止或隔离新资源，才能执行冲突的新 generation。不得把旧进程仍可写入的同一工作区交给新任务。

原步骤的 `held` 或 `isolated` reservation 均是未决资源屏障；即使 Run 已有 failed 等终态，retry/invalidate 也不能绕过仍未释放的原 reservation 创建新一代。固定目标准入与能力路由共用该占用事实；释放仍必须携带与原执行绑定的确定证据。

对于 capability-bound 的 unknown workspace write，只允许一个已持久化的 `workspace_digest` descriptor 触发证据核对。descriptor 必须回链原 owner、node、resource、Run、generation、payload hash 和当前 unknown Workflow/Step/dispatch，并且原 Workflow contract/binding、Hub action registration 与 Node local registration 都必须预先声明独立的 `workspace.digest` helper。派生的 `reconcile.workspace.digest` 只带 Hub 从原私有参数得到的 `path`，retry 固定为 `read_only`；它复用原 binding，不新建 admission、预算、generation、Run 或 reservation，不释放 `held`/`isolated` 容量，也不把匹配摘要当成因果证明。匹配、失配和读取失败均只追加有界 evidence/audit，原 Command、Run、Workflow 状态继续为 `unknown`。descriptor、helper、定义、动作、报告、策略、凭据、资源或状态任一不一致都必须拒绝；本机 POSIX/loopback 回归已覆盖该 slice，跨平台和完整 WF-05 仍需独立验收。

read_only：仅在工具目录与本地策略均声明可重试、已确认未决副作用为空时有限重试。

reconcile_before_retry：必须先确认旧任务未产生效果或效果可安全识别。

manual_only：用户明确触发新一代，仍不能绕过未决资源冲突和原审批。

Hub/worker 重启后恢复 intent 与 binding；Node 断网后保留 journal 与原 command ID；迟到回执必须匹配 generation、目标、binding 与 fencing。旧 receipt 不得覆盖新 generation。

Workflow 的 reconciliation 追加 evidence/event，不篡改底层已终结的 unknown Run/Command。若无法证明停止或完成，保持 unknown，并允许用户在独立隔离资源中新建另一个流程。

## 8. 审批、取消与预算

审批复用对应能力已有 grant。新增一般中途审批时，grant 至少绑定 owner、workflow、step/generation、目标、resolved_contract_digest、策略 revision、动作参数 hash 和有效期。描述变化、目标变化或输入变化使 grant 失效。

人类身份的验收/审批与模型可用工具分开。外部回复中的“用户同意”不能成为有效 grant。Muse 完全访问设置不会授予 Fleet 本地节点或生产服务权限。

取消以持久 cancel_requested 驱动。没有派发的步骤不再派发；已派发的执行通过原取消入口请求停止。adapter 不支持取消或无法确认停止时，到达有界取消截止后转 unknown，不写 cancelled。整个流程有未知或存活子执行时不能归档成“已取消完成”。

取消与完成竞态保留真实完成事实：已完成子步骤仍是 succeeded/failed；若整个流程此前已完成，取消不重写其结果。其余步骤不再派发，且所有已派发执行均有确定结果后，才可确认流程取消。

流程预算在 admission 前预留，现有用量账本继续记录实际请求。每个子请求有唯一 workflow/step 归属，禁止在聚合时再计一次费用。无法取得 usage 的调用保留 unknown settlement；释放槽位不代表退还未知费用。

预算至少控制 Token、模型请求次数、每步骤 active deadline、外部等待 deadline 和 generation 数。等待人工/节点不占模型并发槽，但受 max_wait_seconds 限制。费用来源不完整时只展示 Token/请求和“费用未知”。

## 9. Operator API

前缀 /api/platform/v1。均需现有 operator 鉴权；写操作经过同源保护，body 不接受 owner_id、lease、任意路径或 shell。

| 方法与路径 | 输入/语义 |
|---|---|
| GET /workflow-templates | 当前用户可用的模板摘要与固定 revision |
| POST /workflows | template_id/revision、workspace_id、input_refs、client_token、expected_policy_revision；相同键同内容查回 |
| GET /workflows | limit=1..100，opaque cursor；只返回摘要 |
| GET /workflows/{id} | 有界步骤、三维结果、等待原因、当前产物和预算 |
| GET /workflows/{id}/events | after 非负整数、limit=1..100；独立持久 sequence/next_cursor |
| POST /workflows/{id}/pause、/resume、/cancel | expected_revision、client_token；返回操作回执，不伪报进程已停 |
| POST /workflows/{id}/steps/{key}/retry | expected_revision、expected_generation、client_token；重新校验预算/未决执行 |
| POST /workflows/{id}/steps/{key}/invalidate | expected_revision、expected_generation、client_token、reason、经模板限定的 input_refs；原子失效后继并准备新一代 |
| POST /workflows/{id}/steps/{key}/reconcile | mode=inspect、expected_revision、client_token；只做受控核查，不接受客户端指定终态 |
| POST /workflows/{id}/acceptance | decision=accepted/rejected、delivery_digest、expected_revision、client_token；仅指定人工角色 |

拒绝非法数字类型，特别是 bool、负数、浮点 revision、NaN 和数字字符串。不存在及跨 owner 资源统一 404。参数错误 400，CAS/幂等内容冲突 409，大小超限 413，预算/容量不足 429，依赖不可用 503；错误只返回稳定 code 和有界说明。

节点继续使用现有 poll/receipt 与签名合同。新增步骤 API 不允许节点凭据反向创建 Workflow 或审批。

### 9.1 F2.5 与 F3 界面的实施边界

路由受现有 operator 身份及两个默认关闭 gate 控制。模板目录默认空；可信登记项固定 owner/workspace、模板 revision 与 policy revision。模板摘要 DTO 的 `input_refs` 只暴露当前 owner 的根步骤固定 artifact/code_revision 引用，页面原样提交这些引用。新创建先校验当前输入权限；同 owner/client_token 的已存在创建先核对原 payload 后查回，权限撤销不隐藏既有回执。此页面不提供任意输入选择器，也不因展示引用取得额外权限。

详情/事件/模板均显式映射公共 DTO，不直接序列化领域对象。事件移除 dispatch token、binding 和 attempt 内部字段，预算超过 JavaScript 安全整数范围时序列化十进制字符串。人工接受同时校验 revision 与完整 delivery_digest；它不合并、推送或部署代码。

页面的选择、事件游标和未决请求按 defaults 返回的账号作用域恢复。丢响应仅用原 token/body 重试；409 刷新后要求人工重新确认。AbortController 与读取序号拒绝旧选择的迟到响应。列表、详情、模板读取错误分别维护，成功读取只清除对应读取错误，不覆盖未决写回执。重建列表、详情或重试动作后按稳定 workflow/step/generation/action key 恢复焦点；对象消失或控件失效时聚焦容器。390px 下不产生水平溢出，恶意产物名和评审正文只能作为文本/下载元数据处理。未确证取消持续显示停止中，unknown 提示核对，确定 cancelled 不再显示停止中。当前浏览器证据不替代 CLI、只读评审与隔离测试的完整执行贯穿。

## 10. 资源上限与启用

schema v1 固定上限：8 步、每步 16 个输入/输出/能力/validator 引用、单合同 16 KiB、完整定义快照 64 KiB、单事件 16 KiB、每步 32 条当前证据、全流程 256 条当前证据。历史事件采用有界查询和保留策略，不在一次响应中全量返回。

已实现 gate（默认关闭）：

- platform_workflows_enabled：附加 schema、读取与控制 API；默认 false。
- platform_workflow_dispatch_enabled：允许新执行 admission；依赖 workflows gate，默认 false。

运行控制 admission_mode=open/drain/frozen 分别表示正常、新任务停止但继续回执/对账、禁止新副作用并保留只读/人工恢复。关闭 dispatch 不停止已授权执行的回执、取消和 reconciliation。

只有确认没有在途或 unknown 执行后才能隐藏整个 workflows 域。备份、引用扫描和 Conversation 删除检查必须识别已经存在的 Workflow 表，即使当前 gate 已关闭，避免孤儿产物或删除仍被使用的会话。

备份记录 workflow schema/模板快照及关联产物。恢复默认 frozen，先核查旧节点执行，再决定恢复派发；恢复备份不能自动成为重新执行所有旧步骤的理由。

测试及真实边界见[验收矩阵](../../testing/workflow-platform-acceptance.md)，实施接口拆分见[路线图](../plans/2026-10-20-workflow-platform-roadmap.md)。
