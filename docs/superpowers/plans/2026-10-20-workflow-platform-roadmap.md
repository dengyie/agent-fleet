# Agent Fleet 深度优化开发路线

> **For agentic workers:** 实施时使用 `executing-plans`，逐切片验证并更新 checkbox。本文件是子项目路线图；首批完整代码计划在[领域基础实施计划](2026-10-20-workflow-foundation.md)。F1 与 F2.1–F2.5 已落地，F3 本机受控链与 F4.1 Hub 分片发布已实现，外部退出条件仍开放；详细证据和下一切片见[实施进度](../../platform/workflow-platform/implementation-progress.md)。

**Goal:** 在现有执行与权限合同上，交付可恢复、可核查、能交接文件的多步骤工作平台。

**Architecture:** 增加 Workflow 协调域，复用 Run、Task、Command 和节点主动出站。先完成一个固定版本的代码交付闭环，再增加跨节点文件、能力路由与外部工具。

**Tech Stack:** Python 3.10+、Flask、SQLite WAL、既有静态 SPA、pytest、现有 Playwright 流程门禁。

## 全局约束

- 本地源码基线、工作树与文档权威关系以[开发入口](../../platform/workflow-platform/README.md)为准；不据此推断生产部署。
- 主工作树与已有未提交改动保留。开发使用 `codex/` 分支和独立工作树；不顺带合并、推送或发布。
- 所有文件 UTF-8 无 BOM。删除、软件大文件下载、真实发送和部署遵循用户与既有能力的授权规则。
- v1 最多 8 步线性链；默认每步 max_generations=3。合同 16 KiB、完整快照 64 KiB、事件 16 KiB。
- generation、coordinator_epoch、Run/Task attempt 分开。派发 token 不含 epoch；unknown 不自动重放。
- 执行、校验、接受分别存储；当前 subject_digest 的必需证据和接受条件共同决定 delivered。
- 新领域表、备份和引用扫描先就绪，再启用新执行；停 admission 后继续回执、取消和核对。
- SQLite 只用于单 Hub 主机的本地存储。进程内 ResourceLeaseManager 不承担跨进程容量保证。
- 每个切片通过自身验证后再推进；含新功能的切片同步增加实际 journey selector。未实现的本路线不提前写入可执行门禁。

## 1. 里程碑与依赖

| 阶段 | 可验收结果 | 依赖与退出条件 |
|---|---|---|
| F0 | 当前证据与试点范围固定 | 选定现有 CLI、受限源码仓库和隔离执行边界；明确浏览器等剩余缺口 |
| F1 | 无 I/O 的状态、身份、合同和证据领域 | 非法输入、越权字段、旧版本证据、非法迁移均能确定性拒绝 |
| F2 | 重启后查回同一子任务的持久协调 | outbox 故障点、多进程、取消/未知、预算、备份恢复通过 |
| F3 | 开发→独立评审→固定测试→人工验收 | 一个获准执行环境内完整产品链路；F3 为首个可用交付 |
| F4 | 两个真实节点间固定版本文件交接 | V2 发布恢复、Node 接收完整性、持久槽位和系统差异验收 |
| F5 | 一个只读 MCP、一个受控外部 Agent、窄工具出口 | 各集成分别证明鉴权、幂等、状态与取消的真实支持范围 |
| F6 | 用真实需求决定的扩展 | 至少两个用例要求分支/汇合或测得容量瓶颈，才立新规格 |

顺序：F0 → F1 → F2 → F3；F4 依赖 F2 的身份/备份与 F3 的产物证据，F5 依赖 F4 所需的接入边界。F3 不依赖 MCP、工具市场、云机创建或 DAG 编辑器。

F3 的源码包限制在已有 10 MiB ArtifactStore 内。开发、评审、测试各用独立工作目录，但先限定同一个已验证的执行环境；只读评审和测试 sandbox 独立配置。跨机器通用文件传输由 F4 交付，不能借旧摘要接力冒充完成。

## 2. F0：基线与试点

### F0.1 证据清单与模板冻结

- [x] 在 `docs/testing/workflow-platform-acceptance.md` 登记试点源码 SHA、CLI/model 身份、sandbox 能力、配置摘要和外部检查状态。2026-11-11私有macOS真实CLI试点的1159文件摘要、CLI/native/config摘要、所选模型及独立会话、sandbox和未完成外部检查，见[真实试点证据](2026-11-11-real-workflow-cli-pilot.md#verified-results--2026-11-11)。不代表生产配置或其他执行器。
- [x] 对照 `docs/testing/current-spec-audit.md`、`browser-notification-proposal.md`、`release-acceptance.md`，保留真实浏览器、接管、通知、备份与生产外部检查的未完成边界。2026-11-12复核已记录于[当前审计](../../testing/current-spec-audit.md)与[开发总账](../../platform/development-completion.md)；本勾选只完成边界登记，不表示这些外部能力已验收。
- [x] 确定 `coding-delivery` revision 1：三个步骤、同一源码 subject、只读评审、固定测试 recipe、最终人工接受；只在预登记仓库和工作区中运行。模板和当前私有试点已固定，真实流程保留awaiting_acceptance；固定人工接受规则不等于已经有人执行接受。

**接口/产出：** 脱敏试点清单与固定模板合同；不创建模型调用或节点任务。检查命令：`git diff --check`，并人工确认每项外部事实有证据或明确 `not_run`。

**退出：** 可以准确说出首个试点在哪个版本、哪种隔离边界运行，以及哪些能力尚未证明。没有验证的 sandbox 不开放 `workspace.exec`。

## 3. F1：领域基础

### F1.1 稳定身份与步骤迁移原语

**文件：** 新增 `hub/domain/workflow_identity.py`、`hub/domain/workflow.py`、`tests/test_workflow_identity.py`、`tests/test_workflow_state.py`。

**接口：** `canonical_digest(value)`、`dispatch_token(owner_id, workflow_id, step_key, generation)`、`transition_step(current, target, *, reconciled=False)`；精确定义及完整示例见首批计划 Task 1–2。

- [x] 先验证键顺序、NUL/Unicode、非法数字、代数变化与固定派发向量，再实现纯函数。
- [x] 覆盖等待、取消确证、unknown 只能核对解决、终态不可普通重开；业务前置条件由下一切片判断。

**验证：** `PYTHONPATH=. python3 -m pytest tests/test_workflow_identity.py tests/test_workflow_state.py -q`。退出：无 DB/HTTP/线程副作用，错误类型与返回值固定。

### F1.2 当前版本证据与交付投影

**文件：** 新增 `hub/domain/workflow_evidence.py`、`tests/test_workflow_evidence.py`。

**接口：** `ValidationEvidence`、`validation_state(subject_digest, required_validators, evidence)`、`can_release_successor(...)`、`delivery_state(...)`。首批计划 Task 3 给出完整代码与断言。

- [x] validator ID/revision 和 subject 同时匹配；相同 evidence key 异内容拒绝；旧 subject 不参与当前判断。
- [x] 证明 succeeded 不等于 delivered、人工接受不覆盖校验失败、unknown 优先于其他维度。

**验证：** `PYTHONPATH=. python3 -m pytest tests/test_workflow_evidence.py -q`。退出：文档优先级、运行前置放行与 UI 投影共用同一组确定性函数。

### F1.3 完整 WorkContract / WorkflowDefinition 校验

**文件：** 新增 `hub/domain/workflow_contract.py`、`tests/test_workflow_contract.py`；调用 F1.1 的规范摘要。

**接口：** `parse_definition(raw_json: bytes) -> WorkflowDefinition`、`resolve_contract(definition, authorized_inputs) -> ResolvedContract`。定义 frozen dataclass；未知字段拒绝；每个 schema/ref 在注册时展开。

- [x] 按[执行契约](../specs/2026-10-20-workflow-execution-contract.md)逐字段落实类型、字节/数量上限、固定 Git 对象、引用作用域、线性依赖与预算；包含 bool-as-int、重复 JSON 键、NaN、Unicode、超深输入。
- [x] 纯解析不查文件、不联网、不从描述文本增加权限。实际授权和当前撤销检查由应用服务完成。

**验证：** `PYTHONPATH=. python3 -m pytest tests/test_workflow_contract.py -q`。退出：有效快照可完整重建，非法合同不会生成部分持久记录。

## 4. F2：可恢复协调

### F2.1 附加 schema、事务与恢复保护

**文件：** 新增 `hub/domain/workflow_records.py`、`hub/infrastructure/workflow_schema.py`、`workflow_repository.py`、`tests/test_workflow_repository.py`；修改 `hub/infrastructure/platform_backup.py`、`hub/application/conversation_service.py`、`hub/infrastructure/platform_db.py` 的引用删除入口、`browser_capture_gc.py` 及对应测试。

**接口：** `WorkflowRepository.transaction()` 提供同一 SQLite connection；`create_or_get(owner, client_token, snapshot_hash)`；`claim_due(now, limit, lease_seconds)` 返回 epoch；更新须带 owner、revision、epoch。新增领域独立 `workflow_schema_meta`，不改变旧基础库版本来掩盖兼容限制。

- [x] 一次事务保存 run/step、intent、预算、目标槽位及 event；目标槽位使用持久 `capability_reservations`，F4 再接自动选点。子执行已有模型槽仍由原 worker 管理。
- [x] 跨 owner/workspace 外键、唯一键、CAS、索引、迁移原子性、并发初始化和将来 schema 拒绝全部覆盖。
- [x] backup inventory、restore 最低 reader、产物引用/会话删除检查必须即使 gate 关闭也识别已存在的新表；恢复默认 frozen。

**验证：** `PYTHONPATH=. python3 -m pytest tests/test_workflow_repository.py tests/test_platform_backup.py tests/test_platform_backup_encrypted.py tests/test_platform_conversation.py -q`。退出：不存在“已创建流程但备份漏掉引用”的运行窗口；迁移失败保留原库。

### F2.2 执行绑定与派发 intent

**文件：** 新增 `hub/domain/workflow_execution.py`、`hub/application/workflow_dispatch.py`、`hub/infrastructure/workflow_run_executor.py`、`tests/test_workflow_dispatch.py`；扩展 Workflow repository 的派发阶段与 binding CAS。通过原 `legacy_task_links` 复用 `LegacyTaskBridge`、TaskService 和 Run worker，不改写原执行引擎。

**接口：** `StepExecutor.create_or_lookup(dispatch_token, payload_hash, resolved_contract) -> ExecutionBinding`；`inspect(binding) -> ExecutionObservation`；adapter 明确 idempotence/lookup 支持程度。

- [x] 同 token 同 payload 查回原 Run/Task，不同 payload 返回 `dispatch_conflict`。不把 Workflow ID 传入旧 bridge 的 run_id 字段。
- [x] 在 intent 提交前、提交后/调用前、子任务创建后/绑定前、绑定后分别强制中断并恢复；核查底层仅有一个逻辑任务。
- [x] 当前未声明 context artifact 读取能力，旧 instruction 超过 2000 字符时 `context_too_large`，不默默截断；将来接入实际读取能力后再使用已授权 artifact。

**验证：** `PYTHONPATH=. python3 -m pytest tests/test_workflow_dispatch.py tests/test_legacy_task_bridge.py tests/test_platform_run_worker.py -q`。退出：可查回的执行不会因 Hub 重启再创建；无法查回且可能执行过的意图变 unknown。

### F2.3 有界协调器与多进程 fencing

**文件：** 新增 `hub/application/workflow_coordinator.py`、`tests/test_workflow_coordinator.py`；修改 `hub/bootstrap.py`、`tools/platform/assistant_worker.py` 的显式生命周期。

**接口：** `tick(now) -> TickSummary`、`start() -> StopHandle`；注入 clock/executor/repository。每 tick 默认 32 workflows / 64 状态动作，所有队列查询有界。

- [x] 先 inspect 现有 binding，再推进新步骤；模型/工具调用不占 DB 写事务；不在 Flask handler 中等待执行。
- [x] 两个真实进程共享临时 SQLite，验证租约过期、旧 epoch 写回拒绝、owner 公平性、空闲不调用模型和 stop/join。

**验证：** `PYTHONPATH=. python3 -m pytest tests/test_workflow_coordinator.py tests/test_platform_run_scheduler.py tests/test_platform_schedule_recovery.py -q`。退出：重启、租约回收和重复 tick 不改变 generation/token。

### F2.4 取消、失效、预算与受控核对

**文件：** 新增 `hub/application/workflow_control.py`、`hub/application/workflow_budget.py`；测试 `tests/test_workflow_control.py`、`tests/test_workflow_budget.py`；复用 `hub/infrastructure/usage_repository.py`。

**接口：** `request_cancel(expected_revision)`、`invalidate(step_key, expected_generation)`、`reconcile(mode="inspect")`、`reserve/settle`。操作带稳定 client_token；当前 input digest 与审批同时校验。

- [x] 不支持取消/停止未确认到期 → unknown；迟到旧回执不覆盖新一代；原 unknown Run 不改写，由 Workflow 追加核对事实。
- [x] 输入失效保留旧证据；存在旧 running/unknown 时禁止冲突资源重用。目标槽位不能仅因协调租约过期就视为执行停止。
- [x] 实际 usage 只记一次，缺 usage 保留未知结算；请求/Token/active/wait/generation 超预算不再 admission。

**验证：** `PYTHONPATH=. python3 -m pytest tests/test_workflow_control.py tests/test_workflow_budget.py tests/test_platform_usage.py -q`。退出：取消和预算不会伪造成功、退还未知费用或丢弃存活任务。

### F2.5 API、gate 与事件读取

**文件：** 新增 `hub/http/workflow_routes.py`、`tests/test_workflow_http.py`、`tests/test_workflow_gates.py`；修改 `hub/config.py`、`hub/bootstrap.py`、`hub/web.py`。

**接口：** [执行契约 API 表](../specs/2026-10-20-workflow-execution-contract.md#9-operator-api)，两个 gate 与 open/drain/frozen 控制；当前 owner 取自认证上下文。

- [x] 分页、after、revision、幂等、非法 JSON/类型、同源、跨 owner 404、节点凭据拒绝与 DTO 脱敏贯穿真实 Flask/SQLite。
- [x] 关闭新派发后仍接收回执和取消、保留读/恢复；有在途或 unknown 时不允许隐藏整个域。

**验证：** `PYTHONPATH=. python3 -m pytest tests/test_workflow_http.py tests/test_workflow_gates.py tests/test_feature_gate_runtime.py tests/test_auth_matrix.py -q`。退出：默认全关，无新增后台线程/外网请求；启用后严格遵守公开合同。

## 5. F3：首个完整产品

### F3.1 固定代码交付与独立验证

**文件：** 新增 `hub/application/workflow_templates.py`、`tools/platform/workflow_source.py`、`tools/platform/workflow_validators.py`；测试 `tests/test_workflow_source.py`、`tests/test_workflow_coding_delivery.py`。

**接口：** 登记 `coding-delivery@1`、source exporter、只读 review adapter、固定 test recipe；输出 CodeRevisionRef、review evidence、测试报告和 delivery_digest。

- [x] exporter 只包含获准固定 tree；排除 `.git`、常见凭据目录/文件和未提交用户文件；安全解包并验证大小/hash/tree 一致。并发创建目标目录时以平台排他 rename 拒绝覆盖；这不证明任意源码内容没有秘密。
- [x] 评审与测试固定同一 head/tree，记录独立执行/model 身份与 recipe revision；测试环境不继承 Hub 凭据。本机 HTTP/fixture 与 macOS profile 的证据范围见实施进度，外部试点另行验收。
- [x] 评审失败阻断测试放行；返工显式 invalidate/retry，旧报告不计入新一代；最终接受不触发 merge/push/deploy。

**验证：** `PYTHONPATH=. python3 -m pytest tests/test_workflow_source.py tests/test_workflow_coding_delivery.py -q`。退出：真正完成一次开发、独立评审、固定测试及接受/退回；不能只断言 mock 返回了三个状态。

#### F3.1a 固定测试的持久 Run 接线

- [x] `hub/domain/workflow_delivery.py`：增加不可变 Run lease 身份；仅真实 worker 提供，保留外部执行 receipt 合同。
- [x] `workflow_run_executor.py` / `workflow_delivery_repository.py`：固定 recipe target、索引查回、pending binding 安全回队、同事务 lease/attempt/取消检查与原 receipt 查回。
- [x] 运行中按索引核对授权与完整绑定；流程取消、工作区撤销、frozen、generation/binding/状态失效先回收进程，不发布证据；普通 revision 更新不误杀同一执行。
- [x] `hub/application/workflow_test_execution.py` / 原 `run_worker_service.py`：在原 worker 生命周期内解包、执行固定配方和发布报告；有界 CAS 重试不重跑进程；默认缺 sandbox 拒绝。
- [x] 丢响应先查回原完整 target 的提交事实；原 `PlatformRepository.finish_run` 在有效 lease 内以同一绑定的持久 test receipt 判定完成与迟到取消的先后，缺回执不绕过取消。
- [x] `workflow_validators.py`：记录可信 adapter 的环境摘要，核验报告字段与实际 receipt/JUnit 一致。
- [x] `tools/bounded_process.py` 与原 Directory/Sandbox backend：增加可选取消回调，在静默输出和关闭管道后仍检查；调用方只在进程组已回收后收到取消结果，旧调用保持原合同。
- [x] `tests/test_workflow_test_execution.py`：真实 SQLite/Run/pytest，覆盖成功、断言失败、取消、失租、重启不重放、发布丢响应、binding 竞态及前驱评审阻断；加入已实现 WF-01/03 selector。

按执行契约 §6.2 逐文件完成；点查使用既有索引，归档验证 O(n)，进程时间受固定配方/launcher 限制。没有第二个队列或执行引擎。本切片的历史证据不覆盖后续 CLI/评审实现。

#### F3.1b CLI 源码采集与双库恢复

- [x] 可信 develop Run/Task 来源和当前采集 policy；普通 Task 无源码上传权限；Workflow 失租不自动重跑。
- [x] 原 Runner/Supervisor 心跳持续至采集与回传；固定私有包、原成功结果、原截止和同 attempt 恢复。
- [x] source HTTP 精确长度/hash/nonce/机器认证；排他 artifact 发布；双库崩溃只修复原 receipt，成功 result 必须晚于源码。
- [x] 真实 Git/进程/Flask/SQLite 故障回归；永久失效 pending 保留核对记录，不删除缓存或伪报成功。

#### F3.1c 只读评审的持久 Run 接线

按产物契约 §8.3 实施，复用 F3.1a 的原 Run 生命周期：

- [x] 纯领域评审身份/登记快照，固定 review 合同与可信 target；拒绝 marker 注入和重叠配置。
- [x] 固定命令 adapter：私有目录、有界 JSON 协议、精确环境白名单、显式只读 launcher、超时/取消与进程组回收；运行前后核对源码不变。
- [x] 评审 Run 按索引核对租约/绑定/当前源码，只发布同一报告；默认无登记/无 launcher 失败关闭。
- [x] 原 worker 与 bootstrap 接线；已提交评审回执和终态写回维持同一完成事实。
- [x] 真实 SQLite/命令进程回归：正负面评审、伪造身份、取消/失租、崩溃不重放和发布丢响应；登记 journey 与实测边界。

普通 Runner/Supervisor × 结果响应正常/丢失的四个受控全链路实例经过原协调器、命令评审、实际 pytest/JUnit 和 HTTP 接受。macOS 独立评审和固定测试 profile 另测宿主文件/网络/凭据边界，固定测试包括无隔离对照并接入完整 HTTP 链；真实模型/商业 CLI、生产配置和其他平台试点仍待完成，F3 总退出条件保持未完成。

### F3.2 工作流界面与刷新恢复

**文件：** 新增 `frontend/assets/views/assistant/workflow.js`；修改 `frontend/assets/api/platform.js`、`frontend/assets/views/assistant.js`、`frontend/assets/styles/assistant.css`，按需要接入 `frontend/assets/views/task.js`；新增 `tests/fixtures/frontend/workflow_browser.cjs`、`tests/test_workflow_browser.py`。

**接口：** API wrapper 与有界 Workflow DTO；一个流程摘要卡和步骤/证据详情，复用现有页面导航，不建设新 Canvas。

- [x] 显示三维状态、当前版本、等待原因、下一动作、文件和预算；received/queued 不显示“完成”。模板固定输入摘要可见，创建保留其精确引用；摘要列表没有交付事实时只显示执行状态。
- [x] 刷新/重新登录恢复同一流程与事件游标；未确认取消保持“正在停止”，已确认取消停止该提示；版本冲突刷新并要求重新确认。真实 Cookie 下验证账号作用域与旧未决请求隔离。
- [x] 390px 布局、键盘、空/读取恢复/冲突态、恶意产物名/评审正文、Cookie 账号隔离和 accepted 刷新覆盖实际浏览器；无 Cookie 请求由真实 HTTP 账号边界验证，仍不宣称外部浏览器身份供应商。

**验证：** 按[验收矩阵](../../testing/workflow-platform-acceptance.md)配置浏览器后运行 `PYTHONPATH=. python3 -m pytest tests/test_workflow_browser.py -q`。退出：页面操作贯穿真实 HTTP/SQLite/worker，刷新后的证据仍一致。

### F3.3 门禁与受限真实试点

**文件：** 增量修改 `docs/testing/journeys.json`、`docs/testing/test-chains.md`、`docs/testing/current-spec-audit.md`、`.github/workflows/ci.yml`（确有新增依赖才改），新增 `tests/test_workflow_e2e.py`。

- [x] WF-01/02/03/07/08 的已实现 selector 纳入 required gate；WF-01 登记本机受控完整链，不声称获准商业 CLI/模型或生产隔离验收。不得用未执行或 skip 的测试代替通过。
- [x] 经过严格本机模型 HTTP 对端、真实 Cookie、协调器、CLI 测试对端、文件下载和人工接受完成贯穿；模型对端、fixture launcher 和 DirectoryBackend 均是受控本机证据，不等同外部模型或生产隔离。
- [x] Linux 测试专用实际 sandbox 及严格零 skip 完整门禁：冻结 revision `8b93185d81dab3eb2442ef8fd0b863dc8eda2552`，5724 passed、197 passing subtests、42/42 journeys，pytest 与原 release verifier 均退出 0；44 张 PNG 完整核验，七项外部检查 not_run。Python 3.13 Linux 全仓与 CI Python 3.10、生产配置分别验收，见[Linux沙箱计划](2026-10-29-linux-workflow-sandbox.md#2026-10-30-linux-全仓门禁结果)。
- [x] 在已授权试点中验证真实 CLI 与 sandbox，产物可下载且版本可核对；其他真实服务维持 `not_run`。2026-11-11私有macOS试点真实Codex开发、独立同模型会话评审、31项固定sandbox测试及5产物HTTP/Chrome下载通过；[证据与限制](2026-11-11-real-workflow-cli-pilot.md#verified-results--2026-11-11)。生产sandbox、完整Linux门禁和人工接受仍开放，不能据此宣布F3整体完成。

**验证：** 使用测试入口的完整 `--require-journeys` 命令；核对 checkout revision、矩阵 hash、JUnit 和外部证据。退出：F3 全部必需证据齐备，才可讨论独立发布；本路线不授权发布。

## 6. F4：文件与节点

### F4.1 V2 分片发布与备份恢复

**文件：** 新增 `hub/domain/artifact_transfer.py`、`hub/infrastructure/artifact_transfer_repository.py`、`hub/application/artifact_transfer_service.py`、`hub/http/artifact_transfer_routes.py`；修改 `tools/platform/artifacts.py`、`hub/infrastructure/platform_backup.py`；测试 `tests/test_artifact_transfer.py`、`tests/test_artifact_transfer_recovery.py`。

**接口：** `reserve` 固定 artifact_id；`put_part(offset, bytes, hash)`；`commit`；所有请求绑定原 command/owner/node/output/generation。配置 `platform_artifact_transfer_enabled=false`。

- [x] 落实默认 64 MiB、硬上限 256 MiB、1 MiB chunks、最多 256 parts、并发/配额/TTL；旧 10 MiB 接口回归。
- [x] 重叠/缺块/篡改/断传、rename 前后崩溃、重复 commit、磁盘不足、quota 并发和 failed staging 实际占盘均有真实文件证据。
- [x] 先支持 V2 inventory、恢复和引用扫描，再开放上传；GC 仅产出 dry-run 候选。

F4.1 的完成范围为本机 POSIX Hub，上传 gate 保持关闭；包括实际 256 MiB/256 片乱序上传、有界内存、三个发布窗口的真实进程退出、四类最后额度的跨进程争抢、明文/加密备份恢复。未收敛 transfer 的备份失败关闭，ready scratch 不恢复并清零目标记账。相关 selector 登记在 FILES-01/WF-08；这不满足 WF-04 的 Node 接收和两台真实机器退出条件。WSGI 底层慢读超时、Linux 门禁与 Windows 句柄仍分别验收。

**验证：** `PYTHONPATH=. python3 -m pytest tests/test_artifact_transfer.py tests/test_artifact_transfer_recovery.py tests/test_platform_artifact_routes.py tests/test_platform_backup.py -q`。退出：不存在“ready 但内容缺失”或重启发布第二份文件。

### F4.2 Node 接收与真实跨节点交接

**文件：** 新增 `tools/platform/artifact_transfer.py`、`tests/test_artifact_receive.py`、`tests/test_workflow_two_nodes.py`；小范围修改 `tools/platform/node_executor.py`、`node_runtime.py`、`node_client.py` 与 journal。

**接口：** 固定 export/receive 能力；input_alias 映射 inputs root；grant_id + 节点机器凭据；接收 receipt 确认完整 hash 后才放行下游。

- [x] 接收子集：固定签名 grant、独立 Node journal、断传重下/重启恢复、原回执重传与下游放行；Node 错配、redirect、POSIX 路径/链接、目录替换、覆写、原凭据撤销和接收槽位均有本机测试。
- [x] 本机 loopback Hub/Node 领取一份真实 Git 源包，接收重算 hash，随后以独立 F3 操作解包核对 tree/blob 并执行固定 pytest；备份保留 grant/receipt 和 waiting_node，恢复为 frozen。
- [x] 本机 POSIX 固定 Node export 客户端、独立上传账本、原 ID 补片/commit/回执恢复；收发统一为每节点 2/Hub 全局 4，跨进程最后槽争抢与异常退出释放均有证据。
- [x] Linux 新鲜完整门禁和原发布核验通过；源码、矩阵、JUnit、PNG 与配置摘要已保存，见[Linux沙箱计划](2026-10-29-linux-workflow-sandbox.md)。
- [x] Windows 接收句柄/NTFS owner-ACL/reparse、排他发布、内核锁和原 journal 恢复已在 home-win 验证；原 NodeRuntime 经远程 HTTP/丢回执/重启后由 Hub 原事务放行。见[Windows 接收计划](2026-10-30-windows-artifact-receive.md)。
- [x] Windows 固定源文件导出：home-win 原生合并 131 passed、实际原 Hub HTTP 2 passed、同源 Linux 原全仓 5785 passed/0 skip/42 journeys；双 Python 关联各 295 passed，远端与本地原 verifier 均通过。见[Windows 导出计划](2026-10-30-windows-artifact-export.md#final-frozen-verification-2026-10-30)。
- [x] 两个产物 Node 的真实源包交接与独立 tree/blob/固定 pytest 验证：home-win Windows → 原 Hub → gz-cvm Linux，独立控制用例 1 passed、接收后固定测试 2 passed，原 ID/完整 bytes 与丢响应恢复核验通过。见[两机计划](2026-10-30-two-host-artifact-handoff.md#actual-frozen-verification--2026-10-30)。

本机收发的具体状态、登记与限制见[产物契约 §4.1/4.2](../specs/2026-10-20-artifact-capability-integration-contract.md#41-已实现的接收子集与恢复边界)。新增 `hub/domain/artifact_export.py`、`tools/platform/artifact_export.py`、`artifact_export_journal.py`、`hub/infrastructure/artifact_capacity.py`，测试为 `test_artifact_export.py`、`test_artifact_capacity.py`、`test_transport_bounded.py`。两个独立 NodeRuntime 已通过真实 loopback HTTP 完成 Node A 导出、原 output binding、Node B 接收与独立 tree/blob/pytest 验证。FILES-01/NODE-01/WF-08 继续按既有 required 范围登记；另有真实 Windows→Linux 两机源包交接与独立验证的显式 opt-in 证据。完整 WF-04/05、生产与外部执行验收继续开放，transfer gate 保持默认关闭。

**验证：** `PYTHONPATH=. python3 -m pytest tests/test_artifact_export.py tests/test_artifact_capacity.py tests/test_transport_bounded.py tests/test_artifact_receive.py tests/test_workflow_two_nodes.py tests/test_platform_node_http_e2e.py -q`。退出：文件流转和 Node 接收确证齐全，不依赖操作者手动拷贝；平台与真实两机证据须独立补齐。

### F4.3 管理能力目录与确定性路由

**文件：** 新增 `hub/domain/capability.py`、`hub/infrastructure/capability_repository.py`、`hub/application/capability_router.py`；修改节点能力报告入口；测试 `tests/test_capability_router.py`、`tests/test_capability_reservations.py`。

**接口：** CapabilityDefinition、NodeOffer、`select_target(requirements, authorized_nodes, now)`；持久 reservation 参与 admission。配置 `platform_capability_routing_enabled=false`。

- [x] 纯选点与显式 admission：权限/驻留/平台/版本/新鲜度/容量依次过滤，priority/占用/node_id 确定性选择；无候选给出有界 reason_code，目录/schema/hash 固定。
- [x] 受鉴权报告与显式 admission：freshness 初值 120 秒独立于 browser 300 秒；启动后要求新报告；Node 自报不扩权；重复 claim 和真实双进程不能超售最后槽，未知/isolated/资源证据不足保留占位。
- [x] NodeRuntime 显式报告发送及本地签名上下文/软件/策略复核；原 journal 的完成/unknown 不重跑，旧路径与 receive/export 独立协议保持兼容。
- [x] Hub 固定 binding 的原生 remote Node Run、事务内可信签名准备与首次 lease 复核；原审批/幂等与 gate-off 失败关闭。
- [x] 自动选点接入现有协调器并固定输入目标的本机子集；原 receive/ready/admission/Run 协议闭环、同代不换机与双进程容量复核。
- [x] capability-bound unknown/isolated workspace digest postcheck 的本机 POSIX/loopback slice：descriptor-backed 独立 read_only helper、原 binding/held-or-isolated 保持、Node digest journal idempotency 与 evidence-only 语义。
- [x] 原 workspace/service inspect/service logs postcheck 终态 evidence 与唯一 audit 同事务提交；历史终态缺 audit 仅从保存证据修复，完整终态读取不占 writer。双 Python 关联各 279 passed、独立本地能力各 6 passed，见[原子审计计划](2026-10-30-postcheck-atomic-audit.md)。
- [x] 获准 home-win/gz-cvm 的固定 workspace 能力 postcheck 平台矩阵：Windows/Linux × held/isolated × matched/mismatch/read_failed，原单次报告 12 passed、零失败/错误/跳过；冻结 `16f10ebd630d38fa53a839ce9d2c432719014120` 的 895 文件与两端前后 hash 一致，原 journal/唯一 audit/预约核验通过。见[原生固定验证](2026-10-30-capability-platform-postcheck.md#native-frozen-verification)，不代填完整 Workflow。
- [x] 完整终态/audit 查回补齐原 Command owner/unknown 核验：十二种边界、本机双 Python 关联各 291 passed、独立能力各 6 passed、release/passive-gate 各 121 passed；见[终态保护计划](2026-10-31-postcheck-terminal-guard.md)。原仓储主键查询保持零写事务和原 404/409。
- [x] 修正冻结 `85488105...`、898 文件的原两机十二项矩阵：12 passed/零失败错误跳过；三端 hash、原 journal/十二 Hub 数据库/64 文件 545,258 bytes 全核验，原 unknown/预约保持。见[修正版证据](2026-10-31-postcheck-terminal-guard.md#corrected-native-result-and-full-run-blocker--2026-10-31)。
- [x] 同一修正源码的原 Linux 严格全仓：逐目录授权恢复容量后，新根/fresh basetemp 原样完成 **5845 passed / 197 subtests / 0 failures/errors/skips**；42 journeys、44 PNG、898 源码摘要、50 文件 7,940,720 bytes 及原远端/本机 verifier 全通过。首次退出 120 的失败证据与旧 5833-pass 基线独立保留；见[修正版全仓](2026-10-31-postcheck-full-gate.md#verified-corrected-source-rerun---2026-10-31)。
- [x] 原协调器自动 admission → 原 Run worker → 实际 Windows/Linux Node 的固定能力生命周期：902 文件冻结 `719d8e7007ded5ef1ebe6322a4715ac30521e7da`，两机 success/matched/mismatch/read_failed 共 8 passed、零失败/错误/跳过；八份 Hub 数据库、两端 journal、固定 bytes 与 42 份复制证据独立核验。success 正常观察后释放容量，unknown 保留原 held 预约且不可 retry，重复/重启零 I/O；见[自动生命周期计划](2026-11-01-capability-subprocess-lifecycle.md#verified-native-automatic-lifecycle--2026-11-01)。此显式探针不加入 required selectors，同源严格全仓结果另记。
- [x] 独立 `coding.test@1` Linux 远程固定测试：原接收回执/CodeRevisionRef/subject/generation/token/binding、manual_only/no-replay、Hub完整报告事务与reader10恢复；四场景实际Node证据独立保留，修正918文件冻结原全仓6010 passed/197 subtests/0 failures-errors-skips、42 journeys/1839 selectors/44 PNG，原远端及本地verifier均通过。见[远程测试终态](2026-11-02-remote-fixed-test-capability.md#corrected-frozen-strict-linux-result--2026-11-02)。
- [x] 已实现WF-04/05要求逐项映射、登记116/147必需自动selector及当前产品同源的Linux/Windows原生与实际双机试点；新清单冻结bd2f0ea0的Linux专项567 passed/0skip、2/2 scoped journeys。产品等价冻结另有Linux707、Windows133、实际双机控制21项分别通过；详见[逐项验收](2026-11-12-file-routing-acceptance.md)。不是完整Linux/browser发布通过。
- [ ] 包含当前WF-04/05清单的完整Linux/browser发布门禁、实际生产配置和任务恢复、其余获准外部验收仍须完成。历史跨机试点及本轮专项不代替这些条件。

### 2026-11-01 same-source strict Linux gate recovery

- [x] Preserve the original failed 902-file run and establish the Chromium `/tmp` backing-file ENOSPC root cause from the failed assertion and syscall trace.
- [x] After exact-path user authorization and complete fixture archive verification, restore capacity and re-run the original browser failure chain without changing code, assertions, timeout, selector, runtime, or sandbox.
- [x] Re-run the exact strict full command against the same 902-file frozen source in a new evidence root; preserve PID/start ticks and terminal command/JUnit/journeys/screenshots.
- [x] Pass both original remote and copied-evidence release verifiers; copy 50 evidence files with matching sizes and SHA-256 and confirm all 902 source hashes/clean checkout.

The first run remains **5848 passed / 1 failed** and is retained as failed evidence; it is not merged with the independent rerun. The rerun is **5849 passed / 197 passing subtests / zero failures, errors, or skips**, 42/42 journeys / 1751 selectors / 44 PNG. Root cause was temporary disk exhaustion: 13 Chromium 2 MiB `fallocate` calls returned `ENOSPC` when only 2,641,920 bytes remained. Fresh focused auth passed after the approved fixture capacity recovery. Full WF-04/05, real CLI/model, production isolation and seven external checks remain open.

本轮实现边界、报告 wire schema、版本与备份规则见[产物契约 §6.2](../specs/2026-10-20-artifact-capability-integration-contract.md#62-f43-目录报告与显式-admission-子集)，执行步骤见[F4.3 子集计划](2026-10-23-capability-catalog-admission.md)。gate 默认关闭；现有固定 target 路径不由本轮替换。

Node 报告和本地复核的独立边界见[产物契约 §6.3](../specs/2026-10-20-artifact-capability-integration-contract.md#63-node-报告发送与本地执行前复核子集)与[Node 实施计划](2026-10-23-node-capability-report-preflight.md)。默认软件仅为当前 Python，未知内存按零可用量；不能据此声称自动协调器或生产隔离已经完成。

固定目标准入和 retry/invalidate 的 isolated 资源屏障另按[修复计划](2026-10-23-isolated-reservation-guards.md)补齐：全局目标容量计入 held/isolated，旧代有未释放预约则拒绝新一代。该修复不改变能力 postcheck 的 evidence-only 语义；释放仍须原执行的确定证据。

Hub 子集的实际边界见[产物契约 §6.4](../specs/2026-10-20-artifact-capability-integration-contract.md#64-hub-固定能力-run-与-command-派发子集)和[Hub 派发计划](2026-10-23-hub-capability-command-dispatch.md)。仅支持原生 remote Node Run；自动选点和原子输入 target 固定由[契约 §6.5](../specs/2026-10-20-artifact-capability-integration-contract.md#65-协调器自动选点与输入目标固定子集)和[协调器计划](2026-10-23-capability-coordinator-input-pinning.md)记录。Node poll 在原 claim 事务重查认证指纹，见[poll 修复计划](2026-10-23-node-poll-credential-fencing.md)。

**验证：** `PYTHONPATH=. python3 -m pytest tests/test_capability_router.py tests/test_capability_reservations.py tests/test_capability_dispatch.py -q`。退出：同输入同选择，已有执行不漂移，旧节点照常工作。

## 7. F5：接入与出口

### F5.1 一个只读 MCP connector

**文件：** 新增 `tools/platform/connectors/mcp.py`、`hub/application/connector_catalog.py`、`tests/test_mcp_connector.py`；通过 `tools/platform/tool_broker.py` 暴露获准子集。

**接口：** 有界 discover/register/call；固定 endpoint、SecretRef、schema hash、版本与策略，配置 `platform_mcp_connectors_enabled=false`。

- [x] 本机验证 tools/list 的 100 工具、256 KiB 总 schema、32 KiB 单 schema、depth 16 与拒绝外部 `$ref`；新增 MCP 只使用 Run 的 20 工具剩余额度。
- [x] 本地原生 Run 提交时固定私有授权摘要；排队/重启后完整定义与 scope 必须一致，缺失 pins 不获得后来新增权限，模型声明及调用边界复核。修复与验证见[Run 固定计划](2026-10-25-mcp-run-pinning.md)。
- [x] 本机验证恶意描述、完整 discovery 变化、伪造 readOnlyHint、SSRF/redirect、错误结果、超时与输出脱敏。
- [x] 目录持久登记/待审与批准版本、跨进程 CAS/撤销、返回对象隔离、独立平台 reader 4 及禁用恢复；无 rediscovery 的默认 bootstrap/原生 Run/HTTPS 路径已有证据，见[目录持久化计划](2026-10-25-mcp-catalog-persistence.md)。
- [x] 账户管理员的 MCP 目录管理界面：脱敏分页、完整定义审阅、精确 epoch/revision 审批/撤销、未决响应读回及页面/身份隔离，见[目录管理计划](2026-10-25-mcp-catalog-administration.md)。
- [ ] 其余集成配置 UI、真实只读 MCP 服务和实际账号/策略试点。
- [x] 正常 Hub 从受信文件挂载 MCP 适配器，支持环境/gates/显式空值优先级；独立 CLI 离线预览、显式单连接器发现、重启复用原目录，见[部署入口](../../platform/workflow-platform/mcp-startup.md)。真实外部服务及其余 UI 继续开放。

**验证：** `PYTHONPATH=. python3 -m pytest tests/test_mcp_connector.py tests/test_platform_provider.py -q`。退出：外部目录不能直接变成执行权限，现有固定工具名/模型协议不退化。

### F5.2 外部 Agent adapter

**文件：** `hub/domain/external_agent.py`、`workflow_external.py` 定义纯领域合同；`tools/platform/external_agents/base.py` 保留兼容导出，`muse.py` 复用既有受控 bridge。`hub/application/workflow_external_execution.py`、`hub/infrastructure/workflow_external_schema.py`、`workflow_external_repository.py` 接入原协调器/admission；测试为 `tests/test_external_agent_adapter.py`、`test_external_agent_workflow.py`、`test_external_agent_persistence.py`。

**接口：** submit/lookup/inspect/request_cancel/export_outputs 与实际支持标志；管理员显式登记 `RegisteredExternalTarget` 和 adapter，原 admission 原子固定注册/intent/预算/预约；配置 `platform_external_agents_enabled=false`。

- [x] adapter 合同逐项登记 lookup/idempotence/cancel/export 支持程度；Muse 复用受控 bridge 的稳定 task 规则，取消/导出 unsupported。
- [x] 本机原 Workflow 派发/恢复接线：固定非秘密配置与 journal 身份，三类崩溃窗口查回原任务；只有 lookup/幂等证明才可同 token/原 payload 重提。状态/取消回执精确匹配原 task/session，重复取消仍收尾 owned I/O，`agent_reported` 不生成独立验证或 acceptance；reader 3/备份识别原绑定。当时 WF-06 共 128 selectors / 261 cases 全部通过；最新身份切片和完整门禁见实施进度，外部派发详见[外部派发计划](2026-10-25-external-agent-workflow-dispatch.md)。
- [x] 完整有界 UTF-8 文本产物输入：原请求/发送事务、同 prompt 原任务查回、当前权限拒绝重发、独立 reader 2/Workflow reader 5 与旧库兼容；Node 与外部输入不混用。见[文本输入计划](2026-10-25-external-agent-text-inputs.md)。
- [x] 显式 owner/revision Memory 输入：完整内容与共享预算、原请求/reader 原子固定、当前权限拒绝重发和原任务查回；见[Memory 计划](2026-10-25-external-agent-memory-inputs.md)。
- [x] 显式export-capable adapter的已发布ArtifactRef输出：完整artifact/structured/code_revision字节与源码核验、原事务绑定、reader8及私有恢复；见[输出计划](2026-10-26-external-agent-artifact-outputs.md)。
- [x] 本地native_run显式完整Memory：原Run事务/reader7、provider前当前授权与历史闭包；见[Native Memory计划](2026-10-26-native-workflow-memory.md)。
- [x] 本地 Native 完整文本产物输入：原任务已保存正文、完整共享预算、reader9 与历史闭包；见[Native文本计划](2026-10-27-native-workflow-text-inputs.md)。
- [x] 显式 operator 版本摘要：独立权威、Native/外部完整消费、当前授权及原请求恢复；4a3e15f1全仓7043 passed/5平台skip、Linux关联832 passed，后续Windows备份修复三端各96 passed，见[摘要计划](2026-11-11-versioned-summary-context.md)。完整发布与真实模型验收仍开放。
- [x] 本地 Native Run 固定 `code_revision` 源码包只读输入（默认关闭、schema4/reader12、有界临时视图与恢复）；证据及未完成矩阵见[代码包输入计划](2026-11-11-native-code-package-inputs.md)。
- [x] 外部 Agent 的 Node capability requirements 已接入不可变 target/configuration 快照，并在 admission 前按 adapter 显式 `id/revision` 匹配；不匹配稳定返回 `external_capability_unavailable`，旧 target 快照和空能力 canonical bytes 保持兼容。受控外部回归证据见 [外部派发计划](2026-10-25-external-agent-workflow-dispatch.md)。
- [x] 本地Native Run有界PNG/JPEG/WebP图片输入切片已实现，默认关闭、schema5/reader13、attempt内构造provider图片、持久元数据不含base64；本机边界与恢复证据见[图片输入计划](2026-11-12-native-binary-media-inputs.md)。真实vision模型和最终发布仍待验收。
- [x] 本地Native Run有界WAV/MP3音频输入已接入schema6/reader14；按固定provider audio capability授权，attempt内读取后序列化为`input_audio`，不持久化base64。受控传输/恢复和边界证据见[音频输入计划](2026-11-12-native-audio-inputs.md)；真实音频模型和最终发布仍待验收。
- [ ] 视频与CLI/评审/测试/远程 context 媒体输入、其余集成配置仍未实现；当前不声明这些执行能力。
- [x] Muse受控适配器正常启动文件登记、原journal身份校验/显式离线初始化、与coding-delivery共存及gate-off原任务恢复，见[部署入口](../../platform/workflow-platform/muse-startup.md)。实际账号/远程浏览器/其他context与生产验收仍开放。
- [ ] 真实 session 标识、断网/休眠、接收与完成区分、重复消息、artifact 导出和账号撤销均有证据；原生 Muse 登录/审批边界保持独立。

**验证：** `PYTHONPATH=. python3 -m pytest tests/test_external_agent_adapter.py tests/test_external_agent_workflow.py tests/test_external_agent_persistence.py tests/test_muse_fleet_integration.py tests/test_muse_native.py -q`。退出：统一界面准确反映各方法支持范围；本机证据不替代真实试点。

### F5.3 窄权限 Fleet MCP 出口

**文件：** 新增 `hub/http/fleet_mcp_routes.py`、`hub/application/integration_identity.py`、`tests/test_fleet_mcp_boundary.py`。

**接口：** 仅 `workflow.create_from_template`、`workflow.get`、`workflow.list_artifacts`；独立集成身份、预授权模板/预算/工作区与稳定 client_token；配置 `platform_fleet_mcp_enabled=false`。

- [x] 本机身份不能调用 approve/acceptance/shell/SecretBroker/策略修改；跨 owner/caller、幂等冲突、超量和令牌撤销均拒绝，工作流创建前复核总预算。
- [x] 使用独立 HTTPS MCP client 验证发现/调用、认证和身份重建后的持久 Workflow 归属。
- [x] Fleet 身份登记/撤销持久化：不可变快照、全局 token 摘要索引、跨进程竞争、原 Workflow 事务内授权与预算复核、reader 4、备份恢复禁用导入凭据；启动历史扫描为 WAL 只读事务。具体证据见[身份持久化计划](2026-10-25-fleet-integration-identities.md)。MCP 目录持久化和目录管理界面见 F5.1。
- [x] 提供正常 Hub 的身份运维登记入口：可信摘要配置、离线预览、显式原库登记、重启/撤销/恢复禁用语义，见[部署用法](../../platform/workflow-platform/integration-setup.md)。实际客户端账号策略与生产任务验收仍开放。
- [x] Fleet 身份查看/撤销 UI：Cookie 管理员脱敏复合分页、完整权限/预算/恢复禁用事实、原 digest 及 active 状态事务复核、显式确认和不确定结果只读核对；登记/令牌仍由可信配置提供。见[身份管理计划](2026-10-25-fleet-identity-administration.md)。
- [x] Fleet 身份页面登记：浏览器生成令牌、digest-only 原库写入、完整 allowlists/scopes/预算、事务内 workspace/admission 复核、原身份重复/撤销/恢复语义和精确 UTF-8 保留；正常 Hub 重启、原 MCP 认证与浏览器创建/丢响应/关窗/账号切换已验证。见[页面登记计划](2026-11-13-fleet-identity-registration-ui.md)。仅完成此配置入口，其他集成 UI 和真实外部试点仍开放。

**验证：** `PYTHONPATH=. python3 -m pytest tests/test_integration_identity.py tests/test_fleet_mcp_boundary.py tests/test_mcp_http.py tests/test_auth_matrix.py -q`。退出：外部助手可发起预授权工作，无法替人扩大权限。

F5本机实现与未完成边界见[集成计划](2026-10-24-f5-integrations.md)及[实施进度](../../platform/workflow-platform/implementation-progress.md)。文本/Memory/版本摘要、本地Native固定代码包和有界图片输入及已发布ArtifactRef输出的本机子集已实现；完整F5、其余配置UI、其他二进制媒体与执行上下文、真实外部试点仍开放，所有集成gate默认关闭。

## 8. F6：按证据扩展

- [ ] 两个真实用例均要求分支/汇合后，再设计 DAG schema v2、汇合证据和取消传播；线性 v1 快照仍可恢复。
- [ ] 记录 SQLite lock wait、协调延迟、传输吞吐、进程 RSS、人工干预与返工率；只有测得瓶颈才引入专门队列或数据库。

2026-11-12后续：macOS/Linux各20组独立进程写锁竞争已通过原repository入口测量，持锁时读取继续、写入等待后提交；与已有协调/传输/RSS基线一起见[性能验收记录](../../testing/workflow-platform-acceptance.md#2026-11-12-当前功能候选基线)。其包含连接/锁获取/Python开销，不等于纯引擎锁等待；独立Node RSS、真实网络/模型用量、人工介入与返工仍缺，F6本项不关闭。

- [ ] Blender、ComfyUI、Android、固定出口等每种能力单独写 adapter 规格、授权和真实验收；不因原帖提到便自动部署。
- [ ] 通知先消费稳定事件键，只在完成、失败、重要变化或需人处理时发送；邮箱/IM 仍单独验证真实投递。

这些项是后续立项条件，不是首期欠下的空实现。确定具体用例后先补子系统规格与完整实施计划，再修改代码。

## 9. 发布与回退检查点

| 时点 | 必须可审查的结果 |
|---|---|
| 合并前 | 当前切片测试、旧 journey 回归、输入/权限兼容、文档实现状态、已知外部限制 |
| 首次启用前 | 完整 Linux 门禁、真实试点证据、备份恢复演练、最低兼容 reader、默认 gate 关闭 |
| 发现问题 | admission 切 drain；继续处理已存在回执/取消/unknown；禁止通过删表回滚 |
| 数据恢复后 | frozen；先对照节点 journal 与外部任务，核查旧副作用后再开放 |
| 回退代码 | 新表/产物保留；只退到认识这些引用与 schema 的版本；不兼容则保留最后兼容 reader 并关闭新派发 |

实施进度由每个切片的checkbox、实际代码和新鲜验收证据共同维护。当前未提交、合并、推送或部署；F1–F2、F3本机交付链、F4本机收发/能力路由/postcheck与F5本机集成边界已落盘，证据及首轮间歇失败见[实施进度](../../platform/workflow-platform/implementation-progress.md)。F0真实试点、F4跨平台/真实两机、F5其余配置UI、未支持的输入上下文及真实外部集成仍开放；停止确证port的受控测试不代替真实Supervisor adapter验收。

## F5 显式 Memory 后续切片

- [x] [Memory 输入计划](2026-10-25-external-agent-memory-inputs.md)：external_agent 完整owner Memory/精确revision（含0）、原请求恢复和当前发送授权，schema3/reader6、历史全量闭包，31selectors/84cases。
- [x] 本地Native完整Memory及已发布ArtifactRef输出已按各自契约实施，最新证据见下节与实施进度。
- [x] 本地 Native 完整文本：共享完整输入读取器、原 Run schema2/reader9、当前授权与恢复；见[Native文本计划](2026-10-27-native-workflow-text-inputs.md)。
- [x] 显式operator版本summary的本机与原生摘要/备份范围已按[独立版本摘要计划](2026-11-11-versioned-summary-context.md)验证。
- [x] Native 本地 `code_revision` 源码包输入已实现，schema4/reader12、只读工具与安全checkpoint重建；测试/门禁范围见[计划](2026-11-11-native-code-package-inputs.md)。
- [ ] 二进制媒体、其他执行context、外部能力要求及其余集成配置继续开放。
- [ ] Linux严格零skip、Windows与获准真实节点/模型/CLI/MCP/Muse试点继续开放。

全仓 **5256 passed、0 failures/errors、5 skipped、195 subtests passed**，WF-06 **302 selectors / 681 cases 全通过**，**41/42 journeys**；严格退出 **1**、ci_status=failed，唯一 incomplete 为 Linux RELEASE-01，七项外部检查均 not_run。证据目录：`/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-external-memory-full-20261025-dnxqbpck`。 关联双Python各784passed/0skip。完整平台不因本机子集完成而关闭；细节见[实施进度](../../platform/workflow-platform/implementation-progress.md)。


## 2026-10-27 外部已发布产物输出

- [x] 依据[ArtifactRef计划](2026-10-26-external-agent-artifact-outputs.md)实施原任务只读导出、完整有界文件/schema/源码包核验和原事务输出/subject/state绑定。reader8只随output admission提高，external3和旧无输出字节保持。
- [x] 真实回滚、进程退出/竞争、历史完整闭包/备份恢复、默认coordinator/HTTP/downstream测试已落盘并有通过证据。
- [x] 最终双Python关联各1153 passed/0skip，完整冻结5524 passed/5个平台skip；WF-06为374/937、WF-08为179/380，全通过。866文件摘要未变，严格退出1仅因Linux RELEASE-01，外部检查not_run。
- [ ] Linux/Windows和获准真实试点继续开放，完整平台尚未交付。

本轮状态与真实失败/自修正记录见[实施进度](../../platform/workflow-platform/implementation-progress.md#2026-10-27-外部-agent-已发布-artifactref-输出)，不把局部通过替代整体交付。

## 2026-10-27 Native 完整文本产物输入

显式开启的本地 native_run 已消费完整 text/plain、text/markdown、application/json ArtifactRef 正文，文本与显式 Memory 共享 8192 bytes 预算。原 Run 私有 schema2/reader9、同 token 查回、当前权限复核、有界历史与 frozen 备份保持原执行链；正文不进入 DTO、事件或 repr。

新增 39 selectors / 115 cases；串行 Python 3.10.21/3.13.15 的 38 模块关联集合各 **1532 passed、2 subtests passed、0 failures/errors/skips**。 全仓 **5639 passed、0 failures/errors、5 个平台 skip、195 passing subtests**；JUnit 实际 5644 个 testcase，不重复累计 subtests。WF-06 **413 selectors / 1052 cases**、WF-08 **201 / 451** 全通过，41/42 journeys。严格退出 1、ci_status=failed，仅 RELEASE-01 缺少三项 Linux pidfd/proc 必需实例；另两项 Supervisor proc 平台 skip，七项外部检查均 not_run。

详细证据见[实施进度](../../platform/workflow-platform/implementation-progress.md#2026-10-27-native-完整文本产物输入)。所有 gate 默认关闭；summary 已完成本机范围，Native 代码包输入见[2026-11-11 计划](2026-11-11-native-code-package-inputs.md)；二进制媒体、CLI/评审/测试/远程或 capability context、其余集成配置、Linux/Windows 与获准真实两机/模型/MCP/Muse试点继续开放。Muse export 仍 unsupported，整体平台与生产验收未完成。

## 2026-10-30 Linux 门禁闭环与后续范围

[Linux沙箱计划](2026-10-29-linux-workflow-sandbox.md)的全部实施项已有真实证据；冻结原全仓 5724 passed / 0 skip、42/42 journeys，原 release verifier 退出 0。876 个源码摘要前后相同，44 张 PNG 完整核验，完整本地/远端证据一致。以上各历史切片所记录的 Linux 待验收属于其当时的状态，现由本结果更新。

后续优先核验 F4.2 的两台真实测试节点及 Windows 安全边界，并按已批准的合同继续 F3/F5 真实试点。真实 CLI/模型、生产 sandbox 配置、外部 MCP/Muse 和未定义的输入/配置扩展仍未完成；F6 按实际用例和性能证据再立规格。不得把 Linux 门禁通过视为这些要求的证据。

## 2026-10-30 Windows 接收验证闭环

[Windows 接收计划](2026-10-30-windows-artifact-receive.md)全部实施与验证项已完成：home-win 原生 60 passed/0 skip，原 NodeRuntime 远程 HTTP/丢回执/重启 canary 单独 1 passed；macOS Python 3.10.21/3.13.15 关联各 264 passed/0 skip。相同 882 源文件冻结 revision `9868cb0c9a51dfe33086bca3c9b15293efaf6404` 的 Linux 原严格全仓 5754 passed、197 passing subtests、0 failures/errors/skips、42/42 journeys。远端和本地原 release verifier 均退出 0，44 PNG 完整核验，50 个证据文件大小/SHA-256 一致，七项外部检查仍 `not_run`。

F4.2 下一实施边界为 Windows 固定源文件导出，以及两个真实产物 Node 的源包交接/独立 tree/blob/固定 pytest。Windows 接收完成不关闭这些条件，F3/F5 外部试点和完整项目继续开放，gate 保持默认关闭。

## 2026-11-01 Hub输入位置修复与新完整门禁

- [x] [输入位置计划](2026-11-01-workflow-input-location.md)从实际Hub Run consumer派生精确scope/revision/kind登记，协调器和原admission共用O(1)索引；未登记与已固定Node的generation继续要求原签收。
- [x] 开启transfer的原HTTP/CLI/源码/评审/固定测试/人工接受及Native完整文本链通过，冲突/身份/版本/直接admission/Node固定边界已登记，既有required selectors保留。
- [x] 新904文件冻结0de03b72e0b2ea34f595f334ac3f6a95ac44864c的原严格Linux全仓5891 passed、197 passing subtests、零失败/错误/跳过，42 journeys/1781 selectors/44 PNG；原远端与本机release verifier、904源hash和50份复制证据核验全部通过。

结果详见[计划终态](2026-11-01-workflow-input-location.md#verified-strict-linux-terminal-result--2026-11-01)；旧902文件全仓只证明其历史revision。已登记Hub消费者不再为不存在的Node alias等待；这不增加跨节点review/test、receive/export能力路由或新的远程执行协议。完整WF-04/05、真实模型/商业CLI、生产隔离及七项外部验收继续开放，F6仍以实际用例和性能证据为前提；本切片未提交、推送、合并或部署。
