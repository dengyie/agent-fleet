# Muse 云端浏览器活动面板实施计划

## 目标

当 Muse 在自己的云端环境操作浏览器时，让 Fleet 用户在对应会话窗口中持续看到 Muse 最近报告的浏览器活动、状态、站点、步骤数和按需截图。

## 规格基准与边界

- `docs/platform/muse-workspace.md` 是 Muse 工作区当前行为的规范；会话历史已包含 `browser_task` 卡片，截图通过受限的 `museResource` 读取。
- `docs/superpowers/specs/2026-10-04-t3.7-browser-navigation-network-boundary.md` 第 8 节禁止在此范围增加浏览器截图流、观察流、接管/交还、writer grants 或 `browser.submit`。
- Muse 浏览器由 Muse 云端控制。Fleet 展示 Muse 在会话历史中返回的最新只读快照数据；不增加浏览器运行时、CDP/iframe、用户凭据、网络路径或提交接口。
- 用户可继续通过会话消息向 Muse 描述下一步；已有会话“停止”操作只请求 Muse 停止当前会话，不承诺直接终止远端浏览器进程。

## 设计与复杂度

- 按会话扫描该 Pane 已有的有序历史事件，选择最新公开的 `browser_task` 卡片；时间复杂度 O(n)，只在现有历史刷新完成后执行，不增加网络轮询。卡片和会话不会交叉读取。
- 活动状态、站点和步骤均用 `textContent` 渲染；站点沿用现有 HTTP(S) URL 校验。截图只在用户点击后通过现有账号和连接版本绑定的资源接口加载。
- 不新增 API、外部依赖、服务层或共享的浏览器控制抽象；沿用 Muse 卡片模块、当前四秒轮询和资源读取边界。

## 修改范围

1. `frontend/assets/views/muse/cards.js`：提取最新公开浏览器任务卡片，并渲染会话活动面板；复用安全链接和按需截图读取。
2. `frontend/assets/views/muse.js`：为每个会话 Pane 安装活动面板，并在同一历史刷新后更新。
3. `frontend/assets/styles/muse.css`：为会话内的可折叠活动面板与截图布局添加样式。
4. `tests/test_muse_workspace.py`、`tests/fixtures/browser/muse_workspace.cjs`：覆盖状态、标题、站点、截图、无任务空态、会话隔离和既有会话操作回归。
5. `docs/platform/muse-workspace.md`：说明只读快照、轮询刷新、截图入口与控制限制。

静态发布路径已包含 `muse.js`、`muse/cards.js`、`muse.css`，无需修改发布清单。

## 验收

- 真实 Chromium fixture 断言有任务的 Pane 显示最新状态、标题、站点和截图，并且截图只通过用户操作加载。
- 同一页面中没有浏览器任务的会话和空白子会话显示明确空态，不借用其他 Pane 的状态。
- 不安全站点 URL 不产生可点击链接；普通聊天、截图/文件、审批、停止、账号切换和移动端布局仍通过既有 Muse 验收。
- `PYTHONPATH=. python3 -m pytest tests/test_muse_native.py tests/test_muse_workspace.py -q`、`python3 -m pytest tests/test_release_layout.py -q`、`bash deploy/test-static-frontend.sh`、JS 语法检查及 `git diff --check` 通过。

## 基线

- `PYTHONPATH=. python3 -m pytest tests/test_muse_native.py tests/test_muse_workspace.py -q`：58 passed，1 skipped（缺少 `FLEET_PLAYWRIGHT_MODULE` 时跳过 Chromium 验收）。
- `bash deploy/test-static-frontend.sh`：通过，所有必需静态资源返回 200，路径遍历检查通过。

## 完成与验证

- [x] 增加只读会话活动区，复用当前 Pane 历史和受限截图资源读取。
- [x] 增加真实 Chromium fixture 断言活动字段、截图入口和不同会话空态；桌面与 390px 视口截图已检查。
- [x] 更新 Muse 使用文档，写明快照刷新、按需截图、停止语义和明确不支持的浏览器接管能力。
- [x] 无新增依赖/API/发布清单项；现有静态资源发布测试确认所需文件均已纳入。
- [x] `FLEET_PLAYWRIGHT_MODULE=... FLEET_BROWSER_CHANNEL=chrome PYTHONPATH=. python3 -m pytest tests/test_muse_*.py -q`：88 passed。
- [x] `FLEET_PLAYWRIGHT_MODULE=... FLEET_BROWSER_CHANNEL=chrome PYTHONPATH=. python3 -m pytest tests/test_frontend_browser.py -q`：7 passed。
- [x] `PYTHONPATH=. python3 -m pytest tests/test_release_layout.py -q`：37 passed，2 subtests passed。
- [x] `bash deploy/test-static-frontend.sh`、`python3 -m compileall -q hub tests`、Muse 前端和浏览器 fixture 的 `node --check`、`git diff --check` 均通过。
- [x] 初次通用浏览器测试用例因缺少默认 Playwright Chromium 可执行文件无法启动；设置现有系统 Chrome 通道后完整 7 项通过。
