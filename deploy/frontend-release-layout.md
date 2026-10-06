# Frontend Release Layout and Rollback

前端和后端使用两个独立产物。控制台只有一个静态入口，不再保留 Flask 模板或另一套前端渲染器。

## 1. 架构与发布边界

| Release | 内容 | 启动方式 |
|---|---|---|
| frontend | `index.html`、`config.js`、`assets/`、许可证与 `manifest.json` | Nginx 静态 root |
| backend | Python 包、配置样例、`requirements.txt`；不包含 `frontend/` | `python hub/web.py --no-serve-frontend` |

浏览器保持同源：`/`、`/assets/*`、`/config.js` 走静态 release，`/api/*` 代理给 Hub；SSE 使用 `/api/stream`。沿用部署边缘的 operator 身份验证；操作员登录经受保护 API 验证，令牌只存当前浏览器会话、仅发送同源 API。机器和 runner 凭据只由后端处理，不写入前端配置。

`hub/http/pages.py` 只提供可选静态托管，不读取业务状态、不注入 HTML 数据。完整源码下，默认 `serve_frontend=True`，本地运行即可预览；生产独立后端显式使用 `--no-serve-frontend`。需要 Flask 托管另一目录时，指定 `--serve-frontend --frontend-dir <release-directory>`。

旧 `frontend_cutover` 配置和 `--frontend-cutover` 参数已经删除。升级启动配置时必须同步替换；删除前端目录不会恢复旧 SSR 页面。生产安装脚本和守护进程统一启动 API-only 服务，并通过 `/api/status` 检查健康，避免首页 404 引发重启循环。

## 2. 前端目录与代码职责

```text
frontend/
  index.html                    # 静态文档，固定 base href=/
  config.js                     # 公开 API 基址，无凭据
  THIRD_PARTY.md                 # 组件来源和本地修改
  assets/
    app.js                      # 组合 client/store/SSE，装配并销毁页面
    routes.js                   # 页面/API 路径与共享图标适配
    api/{client,contracts,platform}.js
    realtime/sse.js              # 重连、游标回放、去重
    state/store.js              # 有界客户端投影
    shell/{navigation,theme}.js  # 导航、移动抽屉、主题
    views/                      # 六个功能视图和 assistant/panels.js
    styles/                     # tokens、shell、features、assistant、components
    ui/                         # 适配后的 awesome-ui 单文件组件
    vendor/                     # 本地 Markdown 解析器、净化器与许可证
```

页面通过 API client 读取数据和发起操作；后端持有授权、状态机、幂等、执行与持久化规则。助手从已持久化会话恢复答案，工具事件仅作执行记录。复用 awesome-ui 的 UiIcon、ChatPromptInput、StreamMarkdown、ToolCallBadge、AutoScrollAnchor；Markdown 经 DOMPurify 净化，禁用嵌入媒体、脚本和危险链接。无 CDN 或运行时 npm 依赖。

## 3. 本地开发和验收

```bash
# 完整源码下预览（dev-operator 仅限回环开发环境）
python hub/web.py --host 127.0.0.1 --dev-operator local@example.test

# 独立静态资源冒烟：不启动 Flask，不访问外部服务
bash deploy/test-static-frontend.sh

# 临时 Hub + runner + SSE 全链路冒烟
PYTHON=/path/to/venv/bin/python bash deploy/e2e-smoke.sh

# 真实 Chromium + 临时 API/数据库；使用确定性 provider
FLEET_PLAYWRIGHT_MODULE=/path/to/node_modules/playwright \
  python -m pytest -q tests/test_frontend_browser.py
```

浏览器测试需要 Node、Playwright 和 Chrome（本地默认 `channel: chrome`）；CI 安装固定版本 Playwright 与 Chromium，并设置 `FLEET_BROWSER_CHANNEL=chromium` 强制执行浏览器验收。它们仅是开发依赖。未配置 `FLEET_PLAYWRIGHT_MODULE` 时该测试显式跳过。普通 `python -m http.server --directory frontend` 可检验静态资源，但不提供嵌套路由回退或 API 代理；完整页面验收应使用可选 Flask 托管或 Nginx 配置。

## 4. 打包与缓存

```bash
# backend 从已提交 HEAD 打包
bash deploy/package-release.sh /tmp/agent-fleet-backend.tgz

# frontend 从源码打包到新的版本目录
bash deploy/package-frontend-release.sh /tmp/agent-fleet-frontend-20260929 2026-09-29.1
```

CI 的 `agent-fleet-release` artifact 只含后端归档；`agent-fleet-frontend` artifact 含独立静态目录。原先只下载后端 artifact 的发布流程需要增加前端 artifact 和静态 root 切换。

前端打包保留目录结构并写入稳定排序的 `manifest.json`（版本、文件清单）。凭据、私钥、数据库/状态快照路径会使打包整体失败；`assets/state/store.js` 是允许的客户端模块。每次使用新的版本目录，避免复用目录留下旧文件。

当前 JS/CSS 文件名**不含内容哈希**，所有入口和 `/assets/*` 使用 `Cache-Control: no-cache`，允许 ETag/304 重新验证，不能使用 immutable 缓存。JSON API 使用 `no-store`；SSE 关闭代理缓冲和缓存，并设置足够长的读超时。

同源 Nginx 示例见 [nginx-frontend-backend.example.conf](nginx-frontend-backend.example.conf)。`/assets/` 与 `/config.js` 缺失返回 404，页面路径回退到原样 `index.html`，`/api/*` 不允许回退到 HTML。

## 5. 发布和回滚

1. 在新的不可变目录解包并验收后端与前端产物。保留当前两个版本的路径及 API 契约信息。
2. 首次分离时先准备前端静态 root、API 代理和既有边缘认证，再将 Hub 启动参数改为 `--no-serve-frontend`。这些配置应作为同一部署步骤验收。
3. 新 API 字段或能力由后端先上线，再切换依赖它的前端。通过 `/api/status`、六个页面和嵌套页面刷新验证路由、认证和资源加载。
4. 后续 frontend-only 回滚只切换静态目录，不触碰后端、probe、runner、SQLite 或任务租约。只回滚到已验证兼容当前 API 的前端版本。
5. 后端代码回滚必须保留当前 `state`、`var`、凭据、库存和已有租约；不通过回滚代码修改 schema 或恢复数据库。数据恢复单独执行备份演练流程。

此文档和本地测试不代表已经变更生产代理、容器启动配置或远端服务。
