# Muse 浏览器活动架构优化实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将 Muse 会话事件解析、浏览器活动状态投影和聊天控制器解耦，让浏览器活动按增量历史更新，并消除等待回复窗口与全局刷新之间的重复历史请求。

**Architecture:** 新增无 DOM、无网络依赖的 Muse 事件投影模块，统一事件序列、角色、文本、流式状态、可见消息和浏览器任务解析。cards.js 只负责卡片与浏览器活动 DOM 呈现；muse.js 只负责会话编排和状态生命周期，使用投影模块的增量 reducer 更新活动模型。等待回复的 Pane 由自己的短轮询刷新历史，全局刷新只负责空闲 Pane，避免同一窗口的重复 I/O。

**Tech Stack:** 原生 ES modules、现有 Web Components、Playwright fixture、pytest。

## Global Constraints

- Muse 浏览器仍由 Muse 云端控制；Fleet 只展示会话历史中的公开 browser_task 快照。
- 不新增实时视频、截图流、CDP、iframe、鼠标键盘接管、browser.submit 或新的外部依赖。
- 资源读取继续使用现有账号、连接版本和 workspace/ 8 MB 限制。
- 所有事件投影函数必须是纯函数；DOM 与 API 调用留在现有边界。
- 旧历史页加载时不能覆盖较新的浏览器活动；同序列事件更新可以替换现有快照。
- 运行现有 Muse 单元测试、浏览器验收、静态发布检查、JS 语法检查和 git diff --check。

## Task 1: 建立统一 Muse 事件投影层

**Files:**
- Create: frontend/assets/views/muse/model.js
- Modify: frontend/assets/views/muse/cards.js
- Test: tests/fixtures/browser/muse_workspace.cjs

**Interfaces:**
- eventSequence(event) -> number|null
- eventRole(event) -> string
- eventText(event) -> string
- eventIsStreaming(event) -> boolean
- eventCards(event) -> object[]
- isBrowserTaskCard(card) -> boolean
- latestBrowserTask(events) -> object|null
- mergeEvents(existing, incoming) -> object[]
- visibleMessages(events) -> object[]
- browserTaskIsActive(events) -> boolean
- reduceBrowserActivity(previous, incomingEvents) -> object|null

reduceBrowserActivity 返回 {card, data, order, signature} 或 null；只接受公开任务卡片，使用有限序列号作为顺序，较旧事件不能覆盖较新状态。

- [x] Step 1: 在纯模型测试和 Chromium fixture 中断言公开过滤、旧序列忽略、同序列更新及私有状态撤回。
- [x] Step 2: 运行投影和浏览器验收，先修复测试暴露的排序与可见性问题。
- [x] Step 3: 创建 model.js，只放事件归一化、可见性过滤、卡片提取、消息合并和浏览器活动 reducer，不导入 DOM、API 或 CSS。
- [x] Step 4: 从 cards.js 删除重复事件解析实现，改为导入模型函数；浏览器活动 DOM 更新函数接收投影模型。
- [x] Step 5: 更新 fixture 的导入路径和调用方式，模型及真实 Chromium 测试通过。

## Task 2: 让会话控制器消费投影模型并使用增量活动更新

**Files:**
- Modify: frontend/assets/views/muse.js
- Modify: frontend/assets/views/muse/cards.js

**Interfaces:**
- updateBrowserActivity(panel, model, context) -> void
- Pane 增加 browserActivityModel 字段，账号切换、断开、关闭窗口时清空。
- loadHistory 在合并历史后将本次 API 返回的 chat_events 交给 reduceBrowserActivity，不再扫描完整 Pane 历史。
- 投递状态直接复用模型层的 eventRole、eventText、eventSequence、eventIsStreaming、browserTaskIsActive。

- [x] Step 1: 替换 muse.js 内重复的事件解析函数和 latestBrowserTask 依赖。
- [x] Step 2: 在 Pane 维护活动投影，统一清理账号切换、断开、重新导入、关闭窗口和卸载时的定时器及投影状态。
- [x] Step 3: loadHistory 仅向 reducer 提交本次响应，旧分页不会回退活动状态。
- [x] Step 4: 浏览器验收确认会话隔离、截图按需加载和既有响应式布局。

## Task 3: 消除等待回复与全局刷新重复历史请求

**Files:**
- Modify: frontend/assets/views/muse.js
- Modify: tests/fixtures/browser/muse_workspace.cjs

**Interfaces:**
- 全局 refresh 只对可见且 !pane.awaitingReply 的 Pane 调用 loadHistory。
- 等待回复 Pane 继续由 scheduleReplyPoll 以现有 900ms 节奏驱动。
- Pane 从隐藏切换为可见时，若正在等待回复，立即触发一次历史刷新。

- [x] Step 1: fixture 记录回复轮询时间，并验证等待状态由 Pane 局部轮询推进。
- [x] Step 2: 全局刷新只读取空闲 Pane，等待回复继续走现有 900ms 局部调度器。
- [x] Step 3: 浏览器验收覆盖发送、思考、输出、未知回执和 Pane 切换。

## Task 4: 更新架构文档并执行完整验证

**Files:**
- Modify: docs/platform/muse-workspace.md
- Modify: docs/superpowers/plans/2026-10-19-muse-browser-architecture.md

- [x] Step 1: 在工作区文档中说明事件投影、增量活动状态、Pane 轮询分工和只读边界。
- [x] Step 2: 三处 Muse 模块的 Node 语法检查通过。
- [x] Step 3: Muse 原生和工作区测试通过：60 passed。
- [x] Step 4: 发布布局测试通过：37 passed、2 subtests passed；静态资源、Python 编译和 diff 检查通过。
- [x] Step 5: 完成最终 diff 与引用检查；代码保留在隔离功能 worktree 中供集成。
