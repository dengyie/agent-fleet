# CoDock-inspired AI Management Implementation Plan

> **执行状态（2026-10-11）**：功能实现与根因修复已完成；Supervisor outbox、交互幂等协调、搜索恢复队列、无碰撞 Hook ID、stream sequence 冲突保护和执行结果未知恢复已补齐。当前工作树全量 Python 门禁 `3291 passed, 22 skipped, 165 subtests passed`，另有 `compileall` 与 `git diff --check` 通过；发布前仍需在公开 `main` CI 重新验证。远端没有既有 `githubmain`，且部署 workflow 只对 `main` 生效，因此不能把推送 `githubmain` 误称为已部署。README 不存在 latest-update 区块，因此没有新增该区块。


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Agent Fleet 现有的会话数据面和受控控制面之上，补齐跨会话接力、统一 Agent 生命周期状态、会话全文搜索、用量看板和结构化交互卡片。

**Architecture:** 采用增量扩展，不改变 push-only 观测链路、runner pull、Supervisor 签名命令和 managed/unmanaged 边界。Hub 增加纯函数领域服务、SQLite 读模型和 operator API；节点只上报已经声明能力允许的事件；前端通过现有 API client 和公开 DTO 展示结果。MCP 同伴协同作为后续 gated 项目，不进入本版本的默认路径。

**Tech Stack:** Python 3.10+ 标准库、SQLite/WAL、现有 Flask 路由、现有 session schema/bridge/uploader、原生 ES modules、现有 unittest/pytest 和浏览器 fixture。

## Global Constraints

- Hub 不通过 SSH、反向连接、反向隧道或浏览器直连节点。
- 新功能只能使用 operator、runner 或 Supervisor 已有认证域，不能复用 ingest token 作为 operator 身份。
- 原始加密 transcript 不进入全文搜索、摘要或浏览器公开 DTO；搜索只使用脱敏规范化文本。
- 非受管会话只能显示 `best_effort`，不能获得控制动作或结构化状态升级。
- 新的任务或接力必须生成新的 `attempt_id`，不能复用旧 lease、旧 nonce 或旧 client token。
- 所有新字段、事件、错误码、列表和文本均有固定 allowlist、长度上限和分页上限。
- 不新增第三方运行时依赖；SQLite 不支持 FTS5 时，搜索 gate 保持关闭且不影响会话采集。
- 旧 `/api/*`、probe、runner、session uploader 和现有前端入口保持兼容。
- 自动分类器不能成为 terminate、pause、审批或权限放行的唯一依据。

---

## 现有基础与边界

当前已经具备以下可复用基础：

- Session Bridge 支持 hooks、structured stream、native transcript、PTY，并将事件写入加密 spool；见 [`tools/session/bridge.py`](../../../tools/session/bridge.py)。
- Hub 支持 redacted event stream、加密 raw stream、事件分页、源记录和断线重放；见 [`docs/session-conversations.md`](../../session-conversations.md)。
- 受管会话支持 Supervisor 控制、追加用户回合、回执和身份守卫；追加入口是 `POST /api/sessions/<id>/messages`。
- Command Plane 已有 task、lease、attempt、worktree、bounded logs、脱敏 patch、测试摘要和人工确认闸；见 [`docs/architecture-v4-control-plane.md`](../../architecture-v4-control-plane.md)。
- UsageRepository 已记录 reservation、usage event、run usage 和限额；见 [`hub/infrastructure/usage_repository.py`](../../../hub/infrastructure/usage_repository.py)。

本计划不重新设计以上协议，只把它们组合成更易用的 Agent 管理表面。

## 版本边界

本版本交付四个 P0/P1 能力：

1. 会话接力包和接力创建流程；
2. Agent 生命周期状态和结构化交互请求；
3. 脱敏 transcript 全文搜索；
4. 按项目、机器、Agent、模型和日期聚合的用量看板。

以下内容保留为后续版本：

- Agent-facing MCP 的 `read_peer_tab` 和跨 Agent 同伴读取；
- 新增 Gemini、Cline、OpenCode 等适配器；
- 自动 LLM 摘要、自动 terminate 和自动权限放行；
- 专门的移动端原生应用。响应式 Web 的问题卡片包含在本版本，但不做独立移动客户端。

---

### Task 1: 固化事件、状态和接力 DTO 契约

**Files:**

- Modify: `session_schema.py`
- Modify: `hub/domain/session.py`
- Modify: `frontend/assets/api/contracts.js`
- Test: `tests/test_session_schema.py`
- Test: `tests/test_frontend_contracts.py`

**Interfaces:**

- 新增事件 `agent_state_changed`，payload：

```json
{
  "state": "running|waiting_for_user|waiting_for_approval|idle|completed|failed|stopped|unknown",
  "source": "hook|structured_stream|native_transcript|pty|supervisor",
  "confidence": "high|medium|low",
  "reason": "bounded_reason_token"
}
```

- 新增事件 `interaction_request`，payload：

```json
{
  "request_id": "opaque-id",
  "question_type": "single|multi|text|confirm",
  "prompt": "bounded text",
  "options": [{"id": "opaque-id", "label": "bounded text"}],
  "allow_custom": true,
  "tool_name": "bounded tool name"
}
```

- 新增事件 `interaction_response`，payload 只允许 `request_id`、`selected`、`text`。
- `public_session_dto` 增加 `current_state`、`state_source`、`state_confidence`；未知状态统一为 `unknown`。
- 前端 contract parser 必须拒绝未知状态、超过选项上限、重复 option id 和未编码的原始对象。

- [x] **Step 1: 写失败测试**

  覆盖事件 allowlist、状态枚举、问题选项上限、未受管会话状态降级和响应 DTO 拒绝未知字段。

- [x] **Step 2: 运行契约测试确认失败**

  Run: `python -m pytest tests/test_session_schema.py tests/test_frontend_contracts.py -q`

- [x] **Step 3: 实现最小 schema 和 parser**

  保持既有 `try_validate_event`、`public_session_dto` 和前端 `parse*` 的错误形状；不在 schema 层引入 HTTP 或数据库依赖。

- [x] **Step 4: 运行契约测试确认通过**

  Expected: 新增事件的正例通过，未受管升级、未知字段和超限输入被拒绝。

- [ ] **Step 5: 提交**

  Commit: `feat: define agent state and interaction contracts`

---

### Task 2: 接入 Agent 生命周期状态和 Hook 映射

**Files:**

- Modify: `tools/session/capture/hooks.py`
- Modify: `tools/session/bridge.py`
- Modify: `hub/infrastructure/session_repository.py`
- Modify: `hub/application/session_service.py`
- Modify: `frontend/assets/views/session.js`
- Modify: `frontend/assets/styles/features.css`
- Test: `tests/test_session_capture_sources.py`
- Test: `tests/test_session_service.py`
- Test: `tests/test_frontend_browser.py`

**Interfaces:**

- Hook、structured stream 和 native transcript 只负责产生候选状态事件；状态合并由 Hub 按 `sequence` 和 `source` 处理。
- 优先级固定为 `supervisor > hook > structured_stream > native_transcript > pty`。
- PTY 静默只能产生 `unknown` 或低置信度 `idle`，不能直接产生 `waiting_for_user`。
- 同一状态重复事件必须幂等；状态反向跳转必须保留为事件但不能覆盖更高置信度的当前状态。

- [x] **Step 1: 写失败测试**

  覆盖 `SessionStart`、`UserPromptSubmit`、`PreToolUse`、`PostToolUse`、`SessionEnd` 的状态映射，覆盖 Hook 断线后的 `unknown` 和非受管降级。

- [x] **Step 2: 运行测试确认失败**

  Run: `python -m pytest tests/test_session_capture_sources.py tests/test_session_service.py -q`

- [x] **Step 3: 实现状态归一化和持久化**

  在 session repository 中保存最新状态摘要，在 transcript 中保留完整状态事件；任何来源异常只写固定 reason token，不写异常文本、路径或环境变量。

- [x] **Step 4: 实现前端状态条和时间线**

  会话头部显示当前状态、来源和置信度；状态变化进入时间线；等待状态显示明确的下一步提示，但不自动执行控制命令。

- [x] **Step 5: 运行后端和浏览器测试**

  Run: `python -m pytest tests/test_session_capture_sources.py tests/test_session_service.py tests/test_frontend_browser.py -q`

- [ ] **Step 6: 提交**

  Commit: `feat: expose bounded agent lifecycle state`

---

### Task 3: 实现确定性的会话接力包

**Files:**

- Create: `hub/domain/handoff.py`
- Create: `hub/application/handoff_service.py`
- Modify: `hub/http/session_routes.py`
- Modify: `hub/bootstrap.py`
- Modify: `frontend/assets/api/client.js`
- Modify: `frontend/assets/api/contracts.js`
- Modify: `frontend/assets/views/session.js`
- Modify: `frontend/assets/views/task.js`
- Test: `tests/test_handoff.py`
- Test: `tests/test_session_routes.py`
- Test: `tests/test_frontend_browser.py`

**Interfaces:**

- `build_handoff_snapshot(session, events, task_result, *, max_files=100, max_chars=12000) -> dict`
- `render_handoff_prompt(snapshot, instruction_override="") -> str`
- `GET /api/sessions/<session_id>/handoff` 返回脱敏预览，不创建任务。
- `POST /api/sessions/<session_id>/handoff` 请求体：

```json
{
  "target_machine": "registered-machine-id",
  "target_agent": "registered-agent-family",
  "target_project": "registered-project-id",
  "instruction_override": "optional bounded text",
  "client_token": "idempotency token"
}
```

- 返回 `202` 和新的 `task_id`、`attempt_id`、`handoff_id`；不把接力当成原 session 的继续写入。

接力包固定包含：`objective`、`current_state`、`completed`、`changed_files`、`tests`、`last_conclusion`、`open_items`、`source_event_refs`、`capture_quality`。文件路径只能是项目相对路径，经过 `..`、symlink、密钥文件名和绝对路径拒绝；完整 raw transcript 不默认带入。

- [x] **Step 1: 写领域服务失败测试**

  覆盖空会话、事件乱序、重复工具调用、超限路径、脱敏命中、无测试结果、非受管会话和重复 client token。

- [x] **Step 2: 运行测试确认失败**

  Run: `python -m pytest tests/test_handoff.py -q`

- [x] **Step 3: 实现纯函数快照和 Prompt 渲染**

  第一版只做确定性提取：首个用户目标、规范化工具路径、最近助手结论、任务结果和开放策略信号。不调用模型，不把自然语言摘要当作事实来源。

- [x] **Step 4: 接入 operator API 和任务创建**

  复用现有 project/agent/machine 白名单、任务队列、确认闸、worktree 和 attempt 机制；接力创建失败不得关闭源 session。

- [x] **Step 5: 接入页面预览、编辑和确认**

  页面先展示结构化字段和最终 Prompt，再允许选择目标 Agent、机器、项目和初始 Prompt。提交后显示新的 task/attempt 链接。

- [x] **Step 6: 运行完整验证**

  Run: `python -m pytest tests/test_handoff.py tests/test_session_routes.py tests/test_frontend_browser.py -q`

- [ ] **Step 7: 提交**

  Commit: `feat: add deterministic session handoff`

---

### Task 4: 建立脱敏 transcript 全文搜索

**Files:**

- Create: `hub/infrastructure/session_search_repository.py`
- Create: `hub/application/session_search_service.py`
- Modify: `hub/http/session_routes.py`
- Modify: `hub/bootstrap.py`
- Modify: `frontend/assets/api/client.js`
- Modify: `frontend/assets/api/contracts.js`
- Create: `frontend/assets/views/session-search.js`
- Modify: `frontend/assets/routes.js`
- Modify: `frontend/assets/shell/navigation.js`
- Test: `tests/test_session_search.py`
- Test: `tests/test_session_routes.py`
- Test: `tests/test_frontend_browser.py`

**Interfaces:**

- 新增 gate：`AGENT_FLEET_SESSION_SEARCH_ENABLED`，默认关闭，独立于 session ingest 和 Supervisor。
- `GET /api/session-search?q=<text>&machine=&project=&agent=&from=&to=&limit=&after=`
- 返回：

```json
{
  "items": [
    {
      "session_id": "opaque-id",
      "sequence": 42,
      "kind": "assistant_message",
      "snippet": "...匹配片段...",
      "highlight": [[3, 10]],
      "machine_id": "opaque-id",
      "project_id": "opaque-id",
      "agent_family": "codex",
      "emitted_at": "RFC3339"
    }
  ],
  "next_cursor": "opaque-cursor"
}
```

- 搜索索引只接收 redacted `user_message`、`assistant_message`、`tool_call`、`tool_result` 的 bounded text；`source_record`、raw JSON、token、路径凭据和内部思考不进索引。
- 优先使用 SQLite FTS5 trigram；中文两字查询通过受限 bigram 辅助表实现。FTS5/bigram 初始化失败时返回 `search_unavailable`，不阻断会话写入。
- 查询最少 2 个字符，最多 128 个字符；单页最多 50 条；snippet 最多 400 字符；结果按 session 时间和 sequence 稳定排序。

- [x] **Step 1: 写存储和安全测试**

  覆盖增量索引、重复事件、删除/保留、中文两字词、token/path 脱敏、分页游标、查询上限、FTS5 不可用和 raw 不入索引。

- [x] **Step 2: 运行测试确认失败**

  Run: `python -m pytest tests/test_session_search.py -q`

- [x] **Step 3: 实现索引仓储和 ingest 钩子**

  在 transcript accepted 后异步或同事务写入搜索读模型；索引失败只记录固定诊断，不回滚已经成功的 redacted/raw transcript。

- [x] **Step 4: 实现 operator 查询路由和前端搜索页**

  搜索结果点击后进入 session 详情并定位 `sequence`；详情页保留会话内搜索，不复制 raw 内容。

- [x] **Step 5: 运行 API、浏览器和回归测试**

  Run: `python -m pytest tests/test_session_search.py tests/test_session_routes.py tests/test_frontend_browser.py -q`

- [ ] **Step 6: 提交**

  Commit: `feat: add bounded transcript search`

---

### Task 5: 增加结构化交互问题卡片

**Files:**

- Modify: `tools/session/capture/hooks.py`
- Modify: `tools/session/bridge.py`
- Modify: `hub/application/session_service.py`
- Modify: `hub/http/session_routes.py`
- Modify: `frontend/assets/api/client.js`
- Modify: `frontend/assets/views/session.js`
- Modify: `frontend/assets/styles/features.css`
- Test: `tests/test_session_capture_sources.py`
- Test: `tests/test_frontend_browser.py`

**Interfaces:**

- 能力声明中增加 `interactive_questions`；没有能力声明时不得猜测。
- `interaction_request` 只读展示时进入事件流；提交答案复用现有 `POST /api/sessions/<id>/messages` 或 Supervisor 命令，不直接写 PTY stdin。
- 每个问题只能有一个 `request_id`，答案提交必须带幂等 `client_token`；过期、重复或非受管会话返回固定错误码。
- 卡片支持单选、多选、自定义文本和确认四种类型；选项最多 32 个，单项最多 256 字符，总文本最多 2000 字符。

- [x] **Step 1: 写失败测试**

  覆盖 AskUserQuestion/Inquirer 规范化、重复提交、失效 request、非受管会话、长文本和多选排序。

- [x] **Step 2: 实现事件映射和答案命令**

  只从已声明的结构化来源生成卡片；PTY 纯文本不能被猜测为交互问题。

- [x] **Step 3: 实现前端卡片**

  单选点击提交，多选显示选中数量后确认，自定义文本使用受限 textarea；所有提交状态显示为 queued/delivered/executing/succeeded/failed。

- [x] **Step 4: 运行浏览器回归**

  Run: `python -m pytest tests/test_session_capture_sources.py tests/test_frontend_browser.py -q`

- [ ] **Step 5: 提交**

  Commit: `feat: render interactive agent questions`

---

### Task 6: 增加用量聚合读模型和看板

**Files:**

- Create: `hub/application/usage_summary_service.py`
- Modify: `hub/infrastructure/usage_repository.py`
- Modify: `hub/http/platform_routes.py`
- Modify: `frontend/assets/api/client.js`
- Modify: `frontend/assets/api/contracts.js`
- Create: `frontend/assets/views/usage.js`
- Modify: `frontend/assets/routes.js`
- Modify: `frontend/assets/shell/navigation.js`
- Test: `tests/test_platform_usage_summary.py`
- Test: `tests/test_frontend_browser.py`

**Interfaces:**

- `GET /api/platform/v1/usage?from=&to=&group_by=day,project,machine,agent,model`
- 返回每个桶的 `requests`、`input_tokens`、`output_tokens`、`total_tokens`、`estimated_cost`、`cost_source` 和 `unknown_count`。
- `cost_source` 只有 `provider`、`configured`、`unknown`；缺少完整 token 或计价配置时不得填零冒充真实成本。
- 聚合按 `reservation_id`/usage event 幂等，流式 transcript 行不得重复计费；时间范围最多 90 天，单次最多 1000 个桶。

- [x] **Step 1: 写失败测试**

  覆盖重复 settle、unknown usage、provider cost 优先级、configured 估算、日期边界、维度过滤和 90 天上限。

- [x] **Step 2: 实现服务和读模型**

  优先复用已有 usage event 和 run metadata；不能从前端传入单价或 owner_id。

- [x] **Step 3: 实现 API contract 和看板**

  看板提供日期、项目、机器、Agent、模型筛选；费用数字旁显示来源，不把估算写成账单。

- [x] **Step 4: 运行测试**

  Run: `python -m pytest tests/test_platform_usage_summary.py tests/test_frontend_browser.py -q`

- [ ] **Step 5: 提交**

  Commit: `feat: add usage summary dashboard`

---

### Task 7: 安全、迁移和发布验收

**Files:**

- Modify: `fleet-gates.conf`
- Modify: `docs/session-conversations.md`
- Modify: `docs/architecture-v4-control-plane.md`
- Modify: `docs/testing/journeys.json`
- Modify: `docs/testing/test-chains.md`
- Create: `tests/test_codock_management_security.py`
- Modify: `.github/workflows/ci.yml` only if new test entry需要显式加入

**Interfaces:**

- 新 gate：`AGENT_FLEET_SESSION_SEARCH_ENABLED`；接力和状态功能随 session/supervisor 现有 gate 运行，问题卡片必须额外检查 capability。
- gate 默认关闭时，旧页面和旧 API 继续工作；开启失败时只关闭新表面，不关闭 observe、task、session 或 supervisor。
- 迁移必须是幂等 SQLite migration，初始化失败返回固定 `feature_unavailable`，不得删除已有 transcript。

- [x] **Step 1: 写安全回归测试**

  覆盖 operator/runner/ingest credential 隔离、非受管控制拒绝、raw 不出搜索、路径/密钥脱敏、接力跨项目拒绝、client token 幂等和错误详情不泄露。

- [x] **Step 2: 更新 journey 和 test chain**

  至少加入：状态变化、接力预览/创建、中文搜索、搜索命中跳转、问题卡片回答、usage unknown、gate 关闭和迁移重启。

- [x] **Step 3: 运行全套检查**

  Run:

  ```bash
  python -m pytest -q
  bash deploy/test-static-frontend.sh
  bash deploy/test-release-routing.sh
  ```

  发布前还要用 390px 和 1440px 浏览器尺寸完成接力、搜索、问题卡片和用量看板验收。

- [x] **Step 4: 更新交接和发布说明**

  在 `docs/HANDOFF.md` 记录新 gate、数据库表、回滚方式和 API 兼容性；README 的 latest-update 区域按 release 流程更新。

- [ ] **Step 5: 提交**

  Commit: `test: close AI management release gates`

---

## 验收矩阵

| 场景 | 必须观察到的结果 |
|---|---|
| 受管 Codex 会话执行工具 | 状态变为 `running`，工具事件和状态来源可追溯 |
| Agent 等待用户选择 | 页面显示问题卡片；答案入队并有回执；不会写正在运行进程 stdin |
| 会话接力 | 预览只含脱敏目标、相对路径、结论和测试；创建新 task/attempt；源会话不被关闭 |
| 跨会话搜索 | 返回 snippet、sequence 和过滤维度；点击后定位详情事件；raw/source_record 不泄露 |
| 无 usage 或不完整 usage | 显示 `unknown`，不显示 `$0` 或虚构成本 |
| 非受管会话 | 永远是 `best_effort`，没有控制按钮、接力控制和问题回答命令 |
| gate 关闭 | 新功能返回固定不可用状态，旧观测、任务和会话链路继续工作 |
| runner/HUB 断线 | spool、lease、attempt 和搜索索引不会重复计费或重复创建接力任务 |

## 后续版本：MCP 协同设计约束

MCP 不进入本版本默认发布。开始设计时必须先满足以下条件：

- 工具最初只开放 `search_history`、`get_session`、`list_active_runs`；
- `read_peer_tab` 必须是显式 operator 授权、单会话 scope、只读、限长、脱敏和审计；
- MCP credential 与 ingest、runner、Supervisor credential 分离；
- Streamable HTTP 端点不能未经认证暴露到公网或可信边界之外；
- MCP 工具错误只返回固定 code，不返回路径、命令、token、异常文本或 raw transcript。
