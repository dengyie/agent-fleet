# Model Summary Generation Implementation Plan

> **For agentic workers:** Execute inline using evidence-driven-bugfix and verification-before-completion; preserve the original Run/summary authorities and record each actual result.

**Goal:** 管理员可用已完成对话运行生成模型摘要、恢复同一生成Run并明确发布带真实来源的不可变版本。

**Architecture:** 原Run队列执行一次无工具模型请求；摘要domain的不可变生成记录固定目标/来源/provider绑定。专用生成API只创建或查询Run，发布从成功Run读完整正文。schema2生成版本与旧operator schema1共存。

**Tech Stack:** Python3.10+、SQLite、Flask、现有Native runtime、原JavaScript模块与pytest/真实浏览器夹具，无新运行依赖。

返回[规格](../specs/2026-11-13-model-summary-generation.md)、[开发账](../../platform/development-completion.md)、[Workflow入口](../../platform/workflow-platform/README.md)。

## Global Constraints

- 不改main；UTF-8无BOM；所有工作在当前feature worktree。
- 1–16个成功Run、同owner/workspace/conversation；完整规范模型输入≤49152 bytes，完整摘要版本≤8192 bytes；超限拒绝，不截断。
- 原Run只执行1个模型步，无工具；同token不改变输入/provider或隐式重发。
- 摘要schema1/reader1原byte格式保持；生成域schema2/reader2、Workflow reader16在原创建事务提升。
- 发布固定原expected_epoch；生成内容不是独立验证或人工接受。
- 实际模型/生产试点、其他context、归档与全部原TODO仍按自己的证据验收。

## Task 1: Generated version and original schema authority

**Files:** `hub/domain/workflow_summary.py`、`hub/infrastructure/workflow_summary_schema.py`、`hub/infrastructure/workflow_schema.py`、`tests/test_workflow_summary_generation.py`。

**Interfaces:** `WorkflowSummaryGenerationProvenance(run_id, source_run_ids, source_sha256, model_profile_id, provider_digest)`；`WorkflowSummaryVersion.generation`可空；原`parse_summary/summary_value`支持精确schema1/2；`inspect_summary_schema`接受完整旧域或完整新域。

- [x] 保存生成版本/旧operator byte兼容/非法来源和reader的失败证据。
- [x] 添加精确生成provenance，旧operator snapshot不增加字段。
- [x] 定义两个可选生成表及原事务升级语句，检查旧域、新域与损坏域。
- [x] 运行新领域回归与原summary/Native/外部解析回归。

## Task 2: Durable creation, lookup and publication

**Files:** `hub/infrastructure/workflow_summary_generation_repository.py`、`hub/infrastructure/workflow_summary_repository.py`及新后端测试模块。

**Interfaces:** `WorkflowSummaryGenerationRepository.create(owner, values, now)`、`get(owner, run_id)`、`find(owner, client_token)`、`prepare(claim, now)`、`validate_snapshot(connection)`；`WorkflowSummaryRepository.publish_generated(owner, run_id, now)`。

- [x] 用真实SQLite验证来源/模型/摘要CAS检查、相同token查回与拒绝漂移、事务失败零Run/表残留。
- [x] 同原写事务创建独立conversation/message/queued Run、固定输入和来源外键；模型读取固定快照。
- [x] 生成发布仅从原成功Run取完整正文，固定provenance与原CAS；保留旧operator发布幂等。
- [x] 以实际生成/成功结果/重复发布/取消/unknown/超大回复及并发人工发布复核。

## Task 3: Original worker and cleanup/history closure

**Files:** `hub/application/run_worker_service.py`、`hub/composition/workflows.py`、`hub/infrastructure/platform_db_conversations.py`、原retention与备份验证入口。

**Interfaces:** worker注入`workflow_summary_generation_guard`；guard在provider前返回原固定输入，原Native runtime使用`RuntimeLimits(max_steps=1)`与空工具集合。

- [x] 真实provider夹具核对完整输入、一次请求和零工具；先验证缺guard/错误绑定的失败，再接原worker。
- [x] 保留原checkpoint/lease/取消/unknown，正常Hub构造不启动Run或model。
- [x] 来源Run/生成Run保留guard、归档删除拒绝与128-row历史closure接入；明文/加密备份继续frozen。
- [x] 执行原worker/runtime/retention/备份与schema兼容关联测试。

## Task 4: Owner API and actual account UI

**Files:** `hub/application/workflow_summary_generation.py`、`hub/http/workflow_routes.py`、`frontend/assets/api/workflow-summaries.js`、`frontend/assets/views/account/workflow-summary-generation.js`、原摘要管理页及浏览器夹具。

**Interfaces:** `POST /workflow-summary-generations`、`GET /workflow-summary-generations/run?run_id=...`、`GET /workflow-summary-generations/token?client_token=...`、`POST /workflow-summary-generations/publish`；响应使用原完整候选/稳定状态与精确DTO。

- [x] API缺口红证据、owner隔离、严格请求字段、幂等回执与无provider I/O。
- [x] 工作区/对话/成功Run及启用模型选择，生成状态/完整候选显示；原token恢复与显式发布。
- [x] dispose/suspend/账号和工作区切换停止旧响应与poll，响应不确定保留相同请求；编辑内容走operator路径。
- [x] 真实桌面/手机宽度浏览器和旧summary页面回归；schema1/2精确parser拒绝伪造来源。

## Task 5: Evidence and publication

**Files:** 原WF-06矩阵、Workflow入口/开发账、本计划与规格、Windows CI选择。

- [ ] 收集新增selector并按真实后端/浏览器范围登记，核对全部矩阵无缺项。
- [ ] 完整关联验证、Ruff、3.10语法、UTF-8/noBOM、JS语法、diff和原打包合同。
- [ ] 固定源码、提交、导出公开短历史并逐项核对完整tree；建立独立PR与精确CI。
- [ ] 下载原Linux/Windows证据并用同revision干净checkout核验；生产/真实模型结果单列。

## Verified results — 2026-11-13

- 原领域/仓库/worker/API/parser/页面缺口已留red证据；增加相同token、完整版本超限/空白、固定provider、一次请求/零工具、pending/response恢复、并发CAS、owner隔离、Native/外部消费和tuple连接第129条损坏。
- 真实桌面/390px浏览器覆盖旧页来源、生成/发布/刷新、接受回执503和跨工作区同token恢复；四次模型调用对应四个不同显式生成Run，恢复没有多发。
- 最终22模块458passed、2passing subtests、零failure/error/skip，39.32s；另部署/回滚48passed、1.97s，集合不累计。原失败含夹具字段错误和新文件release allowlist遗漏，均保留。
- [严格生产审查](../../reviews/2026-11-13-production-release.md)已修复七项问题；新增领域schema/reader回滚识别，failed数据库原写入保留。
- 用户已授权main部署与实际深度测试。完整精确CI、实际receiver同步、正式发布和真实商业模型结果仍待自己的终态证据。
