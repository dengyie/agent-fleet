# agent-fleet

AI agent 舰队观测系统 —— **push-only 架构**：各机器本地运行 probe，主动上报到 hub；hub 不反向连接机器。

License: [MIT](LICENSE)。生产域名、内网拓扑和机清单不要提交；用 `hosts.yaml` 与 `deploy/*.example` 做本机副本。

> 接手入口：[docs/HANDOFF.md](docs/HANDOFF.md)
> 架构说明：[docs/architecture-v3.md](docs/architecture-v3.md)
> 页面开发链路：[docs/architecture-v4-control-plane.md](docs/architecture-v4-control-plane.md)
> 路线 A 薄客户端控制台（Phase 1–5 接线；代码缺省仍关；Phase 4 native resume 无身份禁止 sibling spawn；**2026-09-08 LIVE 已开** `AGENT_FLEET_APPEND_USER_TURN_ENABLED` / `AGENT_FLEET_APPLY_LOCAL_PROFILE_ENABLED`）：[docs/superpowers/specs/2026-09-07-thin-client-remote-control-design.md](docs/superpowers/specs/2026-09-07-thin-client-remote-control-design.md)

## 架构

```
┌─ 每台机器 ──────────────────────────────────┐
│ tools/agent-self-report.py                  │
│ 本地采集 Hermes / Claude Code / Codex / 通用进程 │
│ 每 2 分钟 POST /api/ingest + token           │
└──────────────────────┬──────────────────────┘
                       ▼
┌─ hub（hub.example.com）──────────────────┐
│ POST /api/ingest 认证、落盘、事件通知         │
│ GET  /api/status 面板 API                   │
│ hub/web.py 只读展示 + stale reconciliation   │
│ POST /api/tasks 受控任务队列                 │
└─────────────────────────────────────────────┘

runner: tools/agent-runner.py --once（每分钟主动 poll 任务）
```

机器无需 SSH、无需反向隧道、无需中央私钥。自报告节点自动上墙；`hosts.yaml` 只提供展示描述和 stale TTL。

## Hub 启动

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export AGENT_FLEET_INGEST_TOKEN='generate-a-long-random-secret'
.venv/bin/python hub/web.py --host 0.0.0.0 --port 8790
```

hub 未配置 token 时会拒绝生产启动。token 不进 git、不写文档。

示例部署：hub 默认听 `0.0.0.0:8790`（可用 `AGENT_FLEET_WEB_HOST=127.0.0.1` 只听 loopback），由宿主 Nginx 反代。路径用操作员提供的 `FLEET_HOME` / `FLEET_USER`，不要默认调用者 HOME。LIVE 切换：`FLEET_LIVE_MODE=symlink` 或 bind-mount 场景的 `overlay`（禁止对 bind-mount 做 `ln -s`）。容器镜像若自带 `site-packages/tools` 正规包，release 必须含 `tools/__init__.py`。只读探针：`deploy/hk-readonly-evidence.sh`。GitHub public `main` 不是现网 overlay。

## 机器 probe 部署

在每台机器部署项目目录（至少包含 `tools/`、`connectors/`、`hub/`），然后执行：

```bash
python3 tools/agent-self-report.py \
  --endpoint https://hub.example.com \
  --name worker-a \
  --token "$AGENT_FLEET_INGEST_TOKEN"
```

建议 cron 每 2 分钟执行。先用 dry-run 检查本地采集：

```bash
python3 tools/agent-self-report.py --dry-run --name worker-a
```

支持的本地采集器：

- `hermes`：gateway 状态、平台、会话数量
- `claude_code`：项目数量、活跃项目数
- `codex`：会话数量、活跃会话数
- `generic`：匹配到的进程数量

只上传计数、系统指标和状态，不上传会话内容。
`report_schema.py` 对 probe 出站数据和 hub 公共状态执行同一套字段白名单；
会话 ID、显示名、文件路径、命令行和 collector 原始输出不会进入公网 API。

## Runner 部署（受控开发链路）

在每台执行开发任务的机器上，把 `deploy/agent-runner.yaml.example` 复制为
`~/.config/agent-fleet/runner.yaml`（不进 git），并按本机填写项目白名单路径、
adapter 命令模板与凭据文件。runner 凭据文件建议权限 0600。配置字段说明见样例内注释。

cron 单轮模式（每分钟检查一次新任务）：

```cron
* * * * * cd /path/to/agent-fleet && /usr/bin/python3 tools/agent-runner.py --once >> ~/.cache/agent-fleet/runner.log 2>&1
```

## Web API

- `GET /`：Fleet 总览（机器网格 + 实时事件流）
- `GET /machine/<name>`：机器详情（系统指标 + agent 表 + 24h 在线时间线）
- `GET /api/status`：当前机器状态
- `GET /api/machines/<name>`：单机详情 + 历史
- `GET /api/events`：最近事件摘要
- `GET /api/stream`：SSE 实时事件流（断线自动重连并补发）
- `POST /api/ingest`：机器主动上报，必须带 `X-Agent-Fleet-Token`
- `POST /api/scan`：仅执行 stale reconciliation，必须带 token；不执行机器采集
- `POST /api/tasks`：operator 创建开发任务（受控 command plane）
- `GET /api/tasks` / `GET /api/tasks/<id>`：任务列表与详情（含结果摘要）
- `POST /api/tasks/<id>/cancel` / `retry`：取消 / 重试任务
- `POST /api/commands/poll` / `<attempt>/heartbeat` / `<attempt>/result`：runner pull 侧 API（runner credential 认证域）
- 以下路由由 gate 控制（`AGENT_FLEET_*_ENABLED` / `fleet-gates.conf`，kwarg > env > 文件 > 默认关，显式 False 优先；开启顺序 Session → Supervisor → Adoption → AppendUserTurn → ApplyLocalProfile。**2026-09-01 生产三 gate 已全部开启**；**2026-09-08 LIVE 已加开** Phase 4 `append_user_turn` / Phase 5 `apply_local_profile`）：
  - `GET/POST /api/sessions`、`GET /api/sessions/<id>`、`POST /api/session-events`（operator）
  - `POST /api/supervisor/poll`、`POST /api/supervisor/receipts`（`X-Supervisor-Credential`）
  - `GET/POST /api/adoptions`、`DELETE /api/adoptions/<session_id>`、`POST /api/adoptions/<session_id>/retry`、`POST /api/adoptions/<session_id>/capture-exact`（operator）
  - `POST /api/adoption/<session_id>/source/control`（operator；仅五个固定动作；ingest/runner/supervisor 头 → 401）
- Adoption-only（未开 session）时 `make_app` 把 adoption 失败关闭，observe 仍启动。不要只翻 adoption。
- Exact capture 默认 `best_effort`，须 operator 显式 `capture-exact`，不随 gate 自动打开。

hub 进程默认每 60 秒自动执行一次 stale reconciliation。

## 通知和 stale 状态

- 正常 heartbeat 不通知
- gateway 异常、采集错误、系统阈值异常才产生告警
- 默认超过 300 秒没有 heartbeat 的机器标记为 offline
- `hosts.yaml` 可为节点设置 `stale_after_s`

## 受控开发链路（v4）

浏览器 → CF Access → `POST /api/tasks` → SQLite 任务队列 → agent runner 主动 poll
→ 本地 worktree + adapter（codex/claude_code/hermes）→ 有界日志/diff 回传 → 页面实时展示。

- 任务创建要求目标机器在线且项目在 hosts.yaml `projects` 白名单内
- runner 只执行白名单项目 + 注册 adapter，无任意 shell 接口
- lease 300s + 30s 心跳；runner 崩溃任务自动回队列重派
- 结果幂等（attempt_id），网络失败本地缓存重传

`GET /api/tasks/<id>/files/<path>` 已落地：runner 在 result 附有界脱敏快照，Hub 本地读（operator 认证、限速、审计）。延期（条件触发，不是待办）：WebSocket 无替换 SSE 计划。权威清单见 `docs/HANDOFF.md` §六。

## 纳管生命周期（adoption）

Operator 经 `/api/adoptions` 显式纳管座机会话，生命周期 `pending → adopted → revoked` 已闭环：

- probe 接受纳管并回执 `adopted` 后，`pending → adopted`（幂等）；受控 source 面板只对 `adopted` 座席出现 5 个固定动作。
- 显式撤销 / 漂移自动撤销都把座席置 `revoked` 并只入队一次 detach——**detach 从不发送信号**。
- 漂移撤销（保卫器拒绝且 reason 属于固定漂移码集）追加一条 `adoption_drift` 有界审计（只含 reason + policy，无 pid/exe/cmdline 明文）。
- 关闭 `adoption_repositories_enabled` 时不建 adoption 服务，`/api/adoptions` operator 表面不可用：完整产品上（SSR/静态 `/<path>` catch-all 已挂载）未注册路径的 POST 返回有界 405 `method_not_allowed`；仅当该 catch-all 路由也被移除时才是 404 `not_found`。既有的 `/api/*`、`/api/v1`、task/runner/SSE 与前端契约不变。
- 证据路径：签名回执经 `SupervisorReceiptHook` 扇出到 adoption 服务，审计落在独立 adoption transcript DB；验收只比较固定有界码。

## 前后端分离发布与回滚

后端与静态前端是两个可独立发布、回滚的 release。细节见
[deploy/frontend-release-layout.md](deploy/frontend-release-layout.md)。

- **backend**: 仓库 Python 包 + `requirements.txt`；启动 JSON API 不需要任何 `frontend/` 文件。
- **frontend**: `frontend/` 静态文件，纯静态服务器即可发布：

```bash
deploy/package-frontend-release.sh /tmp/agent-fleet-frontend 99.0.0   # 打包（显式输出目录+版本）
bash deploy/test-static-frontend.sh                                    # 静态纯服务器冒烟
```

- 打包脚本 fail-closed：任何命中 `credentials`/`state`/`runner-credential`/`ingest-token`/
  `.env`/`*.pem`/`*.key` 的路径即整体拒绝且不产生任何输出；不用 npm/构建链，不访问外部
  网络或生产端点。`manifest.json` 只含调用者提供的版本与稳定排序的文件清单。
- **缓存**：带内容哈希的 `/assets/*` 可长期 immutable；`index.html`/`config.js` 是运行时
  入口，**不得**长期缓存。
- **发布顺序**：backend 先于 frontend 上线新 client 依赖的字段/`/api/v1`。
- **回滚**：frontend 只切换静态 release，不影响 probe/runner/state；backend 回滚必须
  保留 JSONL、SQLite schema 与旧 `/api/*` 路径，不得破坏未完成的 task lease。

也正是这个独立 release 拓扑：

- **旧 `/api/*` 与 `/api/v1` 兼容**：`/api/v1/*` 是旧 `hub.http.*` view 的版本化兼容表面，复用同一
  callable；旧 `/api/*` 仍是权威路径（含 probe/runner、SSE），未做任何破坏性路由迁移。
- **同源拓扑**：浏览器同源——`/` 与前端静态 release 同域，`/api/*` 走后端；不需要 CORS。
  示例见 `deploy/nginx-frontend-backend.example.conf`，路由/回滚细节见
  `deploy/frontend-release-layout.md`。
- **SSE 代理要求**：`/api/stream` 反向代理必须关闭 `proxy_buffering` 并设置长读超时
  （详见 nginx 示例注释）；否则 SSE 首帧会被缓冲、长连接会被代理改写。
- **本仓库只做本地验收**：独立 release 打包/启动/路由/E2E 全部在本地回环验证；
  **本次未做任何生产部署**，不触碰线上 Nginx、Cloudflare 或现有机器配置。

## 依赖和测试

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
python3 -m compileall -q connectors hub tools tests
```

## 安全红线

- token、私钥、凭据不进入 git 或文档
- ingest machine 名称经过路径安全校验
- state current JSON 原子替换，避免并发写损坏
- probe 出站和 hub 公共 API 双重应用字段白名单
