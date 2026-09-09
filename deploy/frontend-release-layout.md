# Frontend Release Layout and Rollback

独立静态前端 release 的布局、缓存、发布顺序与回滚契约。对应 Task 19。
前端 release 与 backend release 是两个可独立构建、发布、回滚的发布产物；本文档不
包含任何真实主机凭据、token 或部署秘密。

## 1. Release 边界

| Release | 内容 | 启动方式 |
|---|---|---|
| **frontend** | `frontend/` 静态文件 + `manifest.json` | 任意纯静态服务器（`python3 -m http.server`）或 Nginx `/` 静态 root |
| **backend** | 仓库 Python 包 + `requirements.txt` | `.venv/bin/python hub/web.py`（JSON API / SSE / probe / runner） |

- 前端不依赖 backend 进程提供 HTML 或静态资源。
- backend 启动 JSON API 不需要任何 `frontend/` 文件。
- 浏览器同源：`/` 与 `/assets/*` 走前端 release，`/api/*` 走 backend release。

## 2. frontend/ 目录结构（release 文件集合）

```text
frontend/
  index.html
  config.js
  routes.js
  api/
    client.js
    contracts.js
  realtime/
    sse.js
  state/
    store.js
  views/
    fleet.js
    machine.js
    task.js
  styles/
    app.css
```

`manifest.json` 写入 release 根目录，只含两项：

```json
{ "version": "2026-08-24.1", "files": ["index.html", "config.js", ...] }
```

- `version` 由调用者显式传入（脚本第二参数或 `FRONTEND_RELEASE_VERSION` 环境变量）。
- `files` 为相对 `frontend/` 的稳定排序文件清单。
- **绝不**包含时间戳、随机值、机器名、token、私钥、数据库快照或任何秘密。

## 3. 本地打包与冒烟

```bash
# 打包（显式输出目录；版本号必须由调用者给出）
deploy/package-frontend-release.sh /tmp/agent-fleet-frontend 99.0.0

# 或者用环境变量传版本
FRONTEND_RELEASE_VERSION=99.0.0 deploy/package-frontend-release.sh /tmp/agent-fleet-frontend

# 静态纯服务器冒烟（命令 17 已定义）
bash deploy/test-static-frontend.sh
```

打包脚本约束：

- 仅复制 `frontend/` 静态 release 文件，且完整保留相对目录结构。
- **fail-closed**：任何被复制路径命中 `credentials`、`state`（数据库/状态快照）、
  `runner-credential`、`ingest-token`、`.env`、`*.pem`、`*.key` 即整体拒绝并返回
  非零退出码，且不产生任何输出文件（`frontend/state/store.js` 是允许的静态模块）。
- 不使用 npm/Node 构建链，不调用 `git clean`，不访问外部网络或生产端点。
- manifest 生成确定，重复打包同版本字节一致。

## 4. 缓存策略

- **带内容哈希的静态资源**：`/assets/*`（未来含哈希文件名的 JS/CSS）可以使用
  长期 immutable cache（`Cache-Control: public, max-age=31536000, immutable`）。
- **非缓存入口**：`index.html` 与 `config.js` 是运行时入口/配置，**不得**做不可
  失效的长期缓存。用 `Cache-Control: no-store`（或 `no-cache` + ETag）。
- JSON API 全部 `Cache-Control: no-store`。
- `/api/stream`（SSE）：`Content-Type: text/event-stream`、`Cache-Control: no-cache`、
  `X-Accel-Buffering: no`，代理读超时 > keepalive。

## 5. 发布顺序（backend 先于 frontend）

1. 先发布/回滚 **backend**：新 client 依赖的任一响应字段、`/api/v1`、错误形状
   必须先上线。前端不能先依赖尚未部署的字段。
2. 再发布 **frontend** 静态 release（替换静态 root 或 `index.html`）。
3. 新前端必须能在旧 `/api/*` backend 上工作，或 release 配置显式选择旧 client；
   禁止隐式猜测版本。

## 6. 回滚

### frontend-only 回滚

- 只切换静态 release 目录（例如 保留上一个 `frontend-<ver>/` 目录并切回），
  **不**影响 probe、runner、backend state/数据库。

### backend 回滚限制

- backend 回滚必须保留现有 JSONL、SQLite schema 与旧 `/api/*` 路径；
  不允许通过回滚破坏尚未完成的 task lease。
- 即：回滚前的任务状态（`state/`、SQLite）与回调契约保持兼容，禁止**降级时改
  schema** 或丢失正在进行的 lease、heartbeat、结果回调。

## 7. 参考

- 打包/测试命令：见 §3。
- 路由与 Access (Task 20)：`deploy/nginx-expose.md`、`deploy/cloudflare-access.md`。
- 全链路验收 (Task 21)：`deploy/e2e-smoke.sh`。