# agent-fleet — 项目交接与权威现状（2026-08-17）

> 本文档是 push-only 架构的唯一交接入口。HK hub 只接收上报，不反向连接任何机器。

## 一、项目一句话

AI agent 舰队观测系统：每台机器运行本地 probe，向 hub `/api/ingest` 主动上报；hub 负责认证、存储、告警和 Web 展示。

## 二、当前架构

| 部件 | 位置 | 职责 |
|---|---|---|
| hub/web.py | hub 进程 | app 工厂、页面路由（fleet/machine 视图）、启动入口 |
| hub/routes_observe.py | hub 进程 | `/api/ingest`、`/api/status`、`/api/machines/<name>`、`/api/events`、SSE `/api/stream` |
| hub/auth.py | hub 进程 | 认证域装饰器（ingest token） |
| hub/state.py | hub 进程 | JSONL 历史（>5MB 轮转）+ 原子 current 快照 |
| hub/events.py | hub 进程 | 事件总线 + SSE 队列桥 |
| agent-self-report.py | 每台 agent 机器 | 本地采集并主动 POST |
| probe_collectors.py | 每台 agent 机器 | Hermes/Claude/Codex/generic 本地采集 |
| state/ | hub 数据目录 | JSONL 历史 + 原子 current 快照 + events.jsonl |
| hosts.yaml | hub 配置 | 展示描述和 stale TTL，不是机器注册门槛 |

## 三、上报数据

```json
{
  "machine": "worker-a",
  "agents": {
    "hermes": {"installed": true, "gateway_state": "running"},
    "codex": {"installed": true, "active_count": 1}
  },
  "system": {"platform": "darwin", "load": "1.2", "disk_used_pct": "54%"},
  "ts": 0
}
```

只上传状态和计数；`report_schema.py` 在 probe 出站与 hub 公共状态两侧执行字段白名单。
不上传 session 内容/标识、显示名、文件路径、命令行、prompt、token 或私钥。

## 四、部署状态

1. 公网面板走 Cloudflare Access 保护的 operator 域；probe/runner 走 Bypass + hub 自身凭据。具体域名、容器名、内网地址只写在私有运维笔记，不进本仓库。
2. 发布产物来源（2026-09-02 起）：main 的 release tgz 一律由 CI 产出（`.github/workflows/ci.yml`：test = 全量 pytest 门禁 → package = `deploy/package-release.sh` + artifact `agent-fleet-release`）。package 仅在 main push/手动触发时运行（PR run 不产部署用 artifact）；tarball 内嵌 `RELEASE_ORIGIN`。无 secrets、无 SSH、不触宿主/容器（不复制 `arch-v2-primary` 的 `deploy.yml`）。部署仍手工：`gh run download` 取 artifact → 私有部署笔记 §0 staging SOP。
3. 生产拓扑（模式）：边缘 → 宿主 Nginx → 容器内 hub（默认 `0.0.0.0:8790`，可用 `AGENT_FLEET_WEB_HOST=127.0.0.1` 只听 loopback）。LIVE 为 `${FLEET_HOME}/agent-fleet`。bind-mount 场景必须 `FLEET_LIVE_MODE=overlay`（禁止 `ln -s`）；仅非 bind-mount 才允许 `FLEET_LIVE_MODE=symlink`。容器镜像若自带 `site-packages/tools` 正规包，release 必须含 `tools/__init__.py`。GitHub public `main` 是脱敏孤儿快照，**不是**现网 overlay；现网 SHA 只写在私有运维笔记。
4. 每台 agent 机器把 ingest token 放在 `~/.config/agent-fleet/ingest-token`（权限 0600）；macOS 可用 LaunchAgent，Linux 用 cron。
5. 节点清单以运行中的 `/api/status` 为准；`hosts.yaml` 仓库副本只是示例，生产机名不要提交。

### v4 控制面上线步骤（2026-08-19）

1. HK 容器：`git pull` → `.venv/bin/pip install -r requirements.txt`（无新依赖，确认即可）
   → 重启 web 进程。启动时自动建 `state/fleet.db`（SQLite，WAL）。
2. HK 容器建 runner 凭据文件（0600）：`credentials/runner-credentials.json`，
   内容 `{"<machine>": "<openssl rand -hex 32 生成>"}`，每台 runner 机器一键。
3. Cloudflare 按 `deploy/cloudflare-access.md` 配 Access（Bypass + Allow 两条策略）。
4. 每台 agent 机器：`~/.config/agent-fleet/runner.yaml`（样例 `deploy/agent-runner.yaml.example`）
   + `~/.config/agent-fleet/runner-credential`（0600，与 hub 侧对应键同值）
   + cron `* * * * * ... tools/agent-runner.py --once`（或常驻进程）。
5. 验收：`bash deploy/e2e-smoke.sh`（本地）→ 生产浏览器建一个真实任务 →
   runner 机器确认 worktree 执行与结果回传 → 页面 `/task/<id>` 实时日志。

## 五、常用命令

```bash
# HK hub
AGENT_FLEET_INGEST_TOKEN='<secret>' python3 hub/web.py --host 0.0.0.0 --port 8790

# agent 机器
python3 tools/agent-self-report.py --dry-run --name <machine>
python3 tools/agent-self-report.py --endpoint https://hub.example.com --name <machine> --token '<secret>'

# 查看测试
.venv/bin/python -m unittest discover -s tests -v
```

## 六、能力清单（2026-09-09 重开 v4 初始目标）

公开 GitHub `main` 是脱敏孤儿快照，**不是**现网 LIVE。下表里的生产 SHA 只描述当时 overlay，权威现状在私有运维笔记。

2026-09-08 收口把「能延期的」标成了 `deferred-with-condition`，也把 **v4 原文尚未兑现的产品面** 误标成 done。2026-09-09 起清单分两栏：

- **已落地**：现网 LIVE 以私有运维笔记为准（公开仓 orphan `main` 不是 overlay）。
- **v4 初始目标未完成（本轮必做）**：见 `docs/superpowers/specs/2026-09-09-v4-initial-goal-completion-design.md` 与 plan `docs/superpowers/plans/2026-09-09-v4-initial-goal-completion.md`。
- **条件触发延期**：仍不是待办（WebSocket / frontend-v2 / 自动部署 / 密钥下发等）。

历史计划里的未勾 checkbox 仍不是待办。

| 项 | 状态 | owner | 证据 / 触发 | 回滚 |
|---|---|---|---|---|
| Session / Supervisor / Adoption | **done（2026-09-01）** | ops | LIVE gates 全 1；sessions/adoptions 无 operator=401；poll 无凭据=403。隔离 `var/sessions`、`var/adoptions`。 | 删 conf 对应行 + TERM web |
| 路线 A 薄客户端 Phase 1–5 接线 | **done（2026-09-11 实发）** | ops | LIVE `da54e48`；gates 双 true。mac-local adopted 座位已发 Phase 5（幂等 applied）+ Phase 4 入队。native resume 无身份拒 sibling。 | conf 删 Phase 4/5 两行 + TERM web |
| Release 打包含 `frontend/` | **done（2026-09-02）** | eng | `cc8fdc2` + `tests/test_release_layout`。`frontend/` 为发布真相源；`frontend-v2/` 未上 main。 | 回上一 CI artifact |
| Session Bridge / Supervisor / Adoption 代码 | **done** | eng | `tools/session/*`、`tools/supervisor/*`、hub session/supervisor/adoption 路由与测试已合 main。 | 关对应 gate |
| Profile SSOT P0/P1 | **done** | eng | `agent_profiles.py` + session `agent_family` DTO。 | 无运行时开关 |
| `GET /api/tasks/<id>/files/<path>` | **done（LIVE `da54e48`）** | eng | Hub-local 有界快照；无 operator=401。任务详情 DTO 仍不含 file body。 | 停 ingest files 字段 + 关读路由；回滚 `bak-20260911-pre-da54e48` |
| 脱敏 patch review | **done（2026-09-11 LIVE `da54e48`）** | eng | result 可空 `diff_patch` ≤100KB；详情 DTO 只暴露 `has_diff_patch`；`GET /api/tasks/<id>/diff`；任务页折叠后拉取。 | 停收 patch 字段 |
| 测试结果面板 | **done（2026-09-11 LIVE `da54e48`）** | eng | 可空 `test_summary` allowlist。无测试=空态，不造跑。 | 停收 test_summary |
| 任务暂停 / 继续 | **done（2026-09-11 LIVE `da54e48`）** | eng | `POST /pause` `/continue`，paused 不派发，continue 新 attempt_id。不复用 `pause_session`。 | 忽略 paused 行 |
| 人工确认闸 | **done（2026-09-11 LIVE `da54e48`）** | eng | 创建可选 `confirm=true`；未确认 poll 不可见；`/confirm` `/reject`。旧任务无 gate=现行为。 | 不写 gate 行 |
| runner credential 轮换/吊销 | **done（2026-09-11 LIVE mac-local）** | ops | `deploy/rotate-runner-credential.sh`。LIVE 轮换后旧 secret poll=403，新 secret poll=200 空任务，无凭据=403。 | bak `runner-credentials.json.bak-20260911-pre-rotate` |
| 备份恢复演练清单 | **done（2026-09-11 真切回再切回 `da54e48`）** | ops | 先 `cp -a` LIVE → `bak-20260911-pre-rollback-drill`，tar 覆盖 `bak-20260911-pre-da54e48`（`182e034`），拨测 `/diff`=404；再覆盖回 `da54e48`，`/diff` 无 operator=401。禁止 `ln -sfn`。 | pre-rollback bak |
| WebSocket | **deferred-with-condition** | eng | 触发=实测 SSE+10s 轮询不够。需单独 spec（auth/proxy/reconnect/origin/downgrade）。无替换计划。 | 保持 SSE |
| 更多 agent 专用采集器 | **deferred-with-condition** | eng | 触发=具体机器/家族需求。须有界 metadata + 隔离失败 + fixture。不预做。 | 删 collector |
| Exact capture 生产升级 | **deferred-with-condition** | ops | 路由已接；默认 `best_effort`。触发=加密/配额/留存/审计核查通过后 operator 显式 `capture-exact`。不随 gate 自动开。 | 不调用 upgrade |
| `agent_profiles` P2 / 密钥下发 | **deferred-with-condition** | eng | 触发=单独安全设计获批。控制面永不下发密钥；Phase 5 只翻本机 `is_current`。 | 保持禁令 |
| LLM classifier / 自动 terminate | **deferred-with-condition** | eng | 触发=确定性 policy + 审计稳定。classifier 不得成为唯一不可逆控制依据。 | 不接入 |
| Hermes 结构化 spawn/resume | **deferred-with-condition** | eng | 触发=runtime capability probe 证明。在此之前 Hermes 永久 `unsupported_action`。 | 保持拒绝 |
| TG/微信直推（`hub/notifier.py`） | **deferred-with-condition** | ops | 现有 stdout→cron 已满足不乱通知。触发=需要 Hermes send_message 且不把 bot token 写入 fleet。 | 保持 print |
| main 自动部署 | **deferred-with-condition** | ops | **禁止**把 `arch-v2-primary` `deploy.yml` 拷上 main。CI 只测+打包；部署手工 §0。触发=另写容器感知、无 SSH secret 的新 workflow 获批。 | 保持手工 |
| 远端机常驻 probe/runner | **deferred-with-condition** | ops | 节点已在 `/api/status` 上墙。触发=该机需要常驻 ingest 或 runner。 | 停 cron/LaunchAgent |
| frontend-v2 替换生产 SPA | **deferred-with-condition** | eng | 触发=独立 frontend release + `--frontend-dir` 拨测获批。当前真相源=`frontend/`。 | 保持 tracked SPA |
| Hub 拓扑 | **done（2026-09-01）** | ops | 边缘 → Nginx → 容器内 hub `:8790`。LIVE=bind-mount 时禁止 `ln -sfn`。 | bak 目录 tar 覆盖 |

### 页面开发链路

页面上的开发任务独立于现有 ingest 状态上报，走受控 command plane（已实现）：

```text
浏览器 -> operator-authenticated HK task API -> task queue
       -> agent runner HTTPS poll -> 本地隔离 worktree/adapter
       -> 有界日志、测试结果、diff、文件快照 -> HK -> 页面
```

任务队列、runner pull、worktree 隔离与三种 adapter（codex/claude_code/hermes）已落地；
runner 侧配置见 `deploy/agent-runner.yaml.example`（部署到 `~/.config/agent-fleet/runner.yaml`，不进 git）。
按需读文件（`GET /api/tasks/<id>/files` 与 `/files/<path>`）已落地：Hub 只读结果时附带的有界快照，不反向连 runner。
**v4 初始目标代码已 overlay LIVE `da54e48`（2026-09-11）**：脱敏 patch、测试摘要、任务 pause/continue、人工确认闸、files API。2026-09-11 已对 LIVE 做 runner 轮换、备份真切回再切回、Phase 5 幂等 applied、Phase 4 拉取后原生 `resume_unverified`（recovered 席无 cwd/env/token/exe，拒 sibling）。计划见 `docs/superpowers/plans/2026-09-09-v4-initial-goal-completion.md`。

详细对象、状态、接口和分阶段实现见
[`docs/architecture-v4-control-plane.md`](architecture-v4-control-plane.md)。

### 前后端分离 Phase 1：边界与兼容假设（2026-08-21）

Phase 1 只做边界基础（`hub` package/import 契约、`config.py`/`bootstrap.py`、
repository 接口与现有 JSONL/SQLite adapter、app 实例级配置与事件 publisher），
不改变存储所有权、路径或部署方式。本阶段明确保留以下假设：

- **单一后端写入者**：JSONL 观测/事件文件和 SQLite 任务库由同一个 hub 后端进程写入。
  写入 ownership 是单 writer 进程（或由部署层保证单 writer），进程内锁只作为单进程
  优化，不代表多 worker 一致性保证。
- **SQLite 保持 WAL 回滚模式**：任务库仍以 SQLite WAL（`PRAGMA journal_mode=WAL`）运行，
  不修改 schema 或事务语义。
- **旧路径保持稳定**：`state/` 下的 JSONL 文件、`state/fleet.db`、`state/events.jsonl` 的
  路径与格式不变；`/api/*` 兼容路径、probe、runner 协议和页面入口均不变。
- **生产部署不变**：本阶段不改变公网部署方式（同源反向代理、Cloudflare Access、
  ingest/runner 认证、SSE 行为），也不需要重启或重新配置现有机器。

### 静态前端 cutover（Task 16，默认关闭）

- 运行时配置新增非敏感开关：`FleetConfig.frontend_cutover`（默认 `False`）与
  `FleetConfig.frontend_dir`（默认仓库内 `frontend/`）。`hub/web.make_app` 等既有
  调用点不传参时行为完全不变（cutover 关闭，旧 Flask 模板照常渲染）。
- 开启后 `hub/http/pages.py` 的 `/`、`/machine/<name>`、`/task/<id>` 三个页面路由
  改为返回静态 `frontend/index.html` shell（浏览器 URL 不变，由前端路由解码真实页面），
  并注入 `<base href="/">` 保证嵌套路径下的相对资源解析到站点根。
- **同一 app 服务静态资产**：cutover 开启时，`frontend/` 下的首屏静态资产（
  `config.js`、`styles/app.css`、`api/*.js`、`views/*.js`、`realtime/*.js`、
  `state/*.js`、`routes.js`）由同一个 Flask app 的 catch-all 路由直接下发
  （`send_file` 提供正确 MIME），无需额外静态服务器；遍历与非文件返回 404，
  未知 `/api/*` 仍走既有 API JSON 404 契约。cutover 关闭时不服务这些资产。
- **回滚**：关闭 cutover（或删除 `frontend_dir` 下 shell）即回到旧模板页面；旧模板
  未删除，始终可用。
- **shell 归属**：shell 文件归前端 release 目录所有，由仓库 `frontend/` 提供；页面
  路由只负责按 URL 返回 shell 与 API 静态资产存取，不内嵌任何观测/任务业务数据。
- **不新增安全承诺**：本开关只做页面/模板层的展示切换，不引入新的凭据、CORS 或
  部署配置；shell 缺失时失败关闭为有界非敏感响应（404 `frontend shell unavailable`）。

### 独立前端发布 gate（Task 17）

`frontend/` 必须是「纯标准库静态服务器即可独立发布」的 release：不需要 Flask、
不需要构建工具，也不携带或读取任何凭据。Phase 3 gate 由两部分组成：

- **`deploy/test-static-frontend.sh`（独立静态冒烟）**：用 `python3 -m http.server`
  在动态分配的回环端口上服务仓库 `frontend/`，curl 轮询就绪后，请求
  index.html、config.js、routes.js、styles/app.css 及全部 api/realtime/state/views
  模块，逐路径断言 HTTP 200，并断言每个响应体都不含 `X-Agent-Fleet-Token` /
  `X-Runner-Credential` 凭据 header 名；另断言越界路径（路径遍历）返回 404。
  `trap cleanup` 只 kill 本脚本启动的服务器 PID；不起 Flask、不访问外部服务、
  不用 Node/npm/构建产物、不做 `git clean`、不触碰 `.playwright-mcp/`。
  若配置 `CURL`/`PYTHON` 环境变量可覆盖所用 curl/python3。
- **`tests/test_release_layout.ReleaseLayoutTests.test_frontend_has_required_modules`**：
  锁定静态冒烟依赖的模块清单，缺任一文件即 fail。

Run: `bash deploy/test-static-frontend.sh`（成功输出 `STATIC OK`），并独立跑
`tests.test_release_layout` 保持绿。

### 独立前端 release 打包（Task 19）

`frontend/` 可打包成静态 release 并独立回滚，后端 JSON API 起启动不需要前端文件。
打包与回滚细则见 `deploy/frontend-release-layout.md`。

```bash
deploy/package-frontend-release.sh /tmp/agent-fleet-frontend 99.0.0
```

打包脚本约束：

- 仅复制 `frontend/` 静态 release 文件并保留相对目录；写入 `manifest.json`，只含
  调用者显式提供的 release 版本与稳定排序文件清单（无时间戳/随机值/秘密）。
- **fail-closed**：任何命中 `credentials`、`state`（数据库/状态快照，
  `frontend/state/store.js` 模块除外）、`runner-credential`、`ingest-token`、`.env`、
  `*.pem`、`*.key` 的路径即整体拒绝且不产生输出；不使用 npm/构建链、不调用
  `git clean`、不访问外部网络或生产端点。manifest 重复打包字节一致（确定性）。
- 缓存：内容哈希 `/assets/*` 可长期 immutable；`index.html`/`config.js` 不得长期缓存。
- 发布顺序：**backend 先于 frontend** —— 新 client 依赖的字段/`/api/v1` 先上线。
- frontend-only 回滚只切换静态 release，不影响 probe/runner/backend state；backend
  回滚必须保留 JSONL、SQLite schema 与旧 `/api/*` 路径，不得破坏未完成的 task lease。

测试入口：`tests.test_release_layout.PackageSafetyTests`（Task 19），
`bash deploy/test-static-frontend.sh`（Task 17）。

### 整支 release gate 收口（Task 21）当前现状

后端与前端是两个可独立发布/回滚的 release，**当前 baseline 全链路本地验收通过
（打包 + 后端独立启动 + 路由 + E2E），未做生产部署**：

- **发布顺序（backend 先于 frontend）**：新 client 依赖的响应字段、`/api/v1`、错误形状必须
  先上线；前端不能先依赖尚未部署的字段。见 `deploy/frontend-release-layout.md` §5。
- **同源拓扑**：浏览器同源——`/` 与前端静态 release 同域，`/api/*` 走后端，无需 CORS；
  示例 `deploy/nginx-frontend-backend.example.conf`。Access bypass 只是边缘代理策略，不是
  hub 授权旁路（`deploy/cloudflare-access.md`）。
- **回滚顺序**：frontend-only 回滚只切换静态 release，不影响 probe/runner/backend state；
  backend 回滚必须保留 JSONL、SQLite schema 与旧 `/api/*` 路径，不得破坏未完成的 task lease。
- **旧 `/api/*` 与 `/api/v1` 兼容**：`/api/v1/*` 是旧 `hub.http.*` view 的版本化兼容表面（同一
  callable，`url_prefix=/api/v1`）；旧 `/api/*` 仍是权威路径（含 probe/runner、SSE），未做
  破坏性路由迁移。
- **SSE 代理要求**：`/api/stream` 是长连接 SSE，反向代理必须关闭 `proxy_buffering`、设置长读
  超时（≥10min），并传 `Connection`/HTTP/1.1；否则首帧缓冲或长连接被改写。见 nginx 示例与
  `deploy/test-release-routing.sh`。
- **何未部署**：基线仅通过本地回环验收（`bash deploy/e2e-smoke.sh` 以 `SMOKE OK` 收尾；
  `bash deploy/test-static-frontend.sh` 以 `STATIC OK` 收尾；`bash deploy/test-release-routing.sh`
  以 `ROUTE OK` 收尾）。**未触碰生产 Nginx、Cloudflare 或现有机器部署**。

### 会话数据面 / supervisor 控制面 gate（Task 12）

会话采集与受控执行是独立于 ingest 上报的**新数据面/控制面**，与既有 task/runner
完全解耦，失败的静默不是通过：

- **数据面**：`/api/session-events`（收口，bounded JSON 数组）、`/api/sessions`
  （列举/详情）。会话元数据与转录（encrypted at rest）落在独立的 session metadata DB
  + transcript root，不写入 `state/` 下的 JSONL 观察/事件文件——观察、任务、会话
  三条持久化路径互相独立，一次故障不拖垮另两条。
- **控制面**：`/api/supervisor/poll` + `/api/supervisor/receipts`（被套的 tag unsigned
  Ed25519 签名命令）。命令有固定错误码（`allowed`/`expired`/`clock_skew`/
  `invalid_signature`/`malformed_timestamp`/`scope_mismatch` 等），验收只比较这些
  有界码——自然语言/静默只代表未通过。
- **managed/unmanaged 边界**：`process_group_id` 存在即 managed（Supervisor 可治理，
  捕获质量由 probe 能力标志决定，非 managed 一律 best-effort 且无控制能力）。会话
  bridge 只读能力标志，不臆造 exact/结构化声明。
- **释放顺序（backend 先于 frontend）**：signing key / 路由 / 端点字段先上线，前端
  再依赖；回滚 frontend-only 只切静态 release，backend 回滚保留 JSONL/SQLite schema
  与旧 `/api/*` 路径，不破坏未完成任务 lease。
- **发布/回滚开关**：`session_repositories_enabled`、`supervisor_enabled` 独立开关；
  关闭会话或 supervisor 只移走 /api/session-events 与 supervisor 路由，不影响
  ingest/task/SSE 的既有路径。
- **失效分叉（既有真）**：Hub 不可达→`poll_transport_error`（有界、不抛出）；仓库
  只读→sqlite `OperationalError` 失败关闭且**连接不泄漏**（`_connect` 支持关闭
  半初始化连接）；spool 满→结构化事件 `capture_blocked`（绝无静默丢弃）；签名键
  轮换→旧键命令 `invalid_signature`；超时→`expired`；时钟偏斜→`clock_skew`；
  source 断连→补 `capture_quality_changed`/`capture_gap` 缺口对；逃逸子进程→
  `escape_unverified`；健康组→`terminated`。
- 验收证据在 `tests/test_session_chaos.py`（11 类故障演练，正反两方面都断言）与
  `deploy/e2e-smoke.sh` 6c（session 数据面 + supervisor 控制面在无 前端时独立装配）。

### 纳管生命周期闭环（Task 12）

纳管生命周期已闭环：`pending -> adopted -> revoked`，撤销与漂移都**只 detach、
永不发信号**：

- **adopt 回执提升**：operator 的纳管命令被 probe 接受、回执 reason 为
  `adopted` 时，座席 `pending -> adopted`（幂等，重放不重复写）。
- **显式 revoke**：置 `revoked` 并只入队**一个 detach**；detach 回执为有界确认
  （`dettached`），不再改行。detach 信封与联动流程永远不带 signal 字段。
- **漂移自动撤销**：受控动作被保卫器拒绝且 reason 属于固定漂移码集
  （`pid_reused`/`exe_changed`/`no_permission`/`unsupported_family`/
  `native_file_unreadable`）时，座席 `adopted -> revoked`，追加**一条**
  `adoption_drift` 有界审计（detail 只含 reason 与固定 policy 令牌，不携带
  pid / started_at / exe 路径 / cmdline 明文），并幂等只入队一个 `detach`。
- **回执证据路径**：`/api/supervisor/receipts` 收到带签名回执后，统一
  `SupervisorReceiptHook` 扇出到 adoption 服务（与 Task 10 的 router 共享同一
  回执 wrapper）；审计落在独立 adoption transcript DB，验收只比较有界码
  （`accepted`/`executing`/`adopted`/漂移码/错误码），不读自然语言。
- **禁用旗标**：`adoption_repositories_enabled` 关闭时不建 adoption 服务、
  `/api/adoptions` operator 表面不可用——完整产品（SSR/静态 catch-all 已挂载）下
  未注册路径的 POST 返回有界 405 `method_not_allowed`（拆掉 catch-all 路由时为 404
  `not_found`）；旧 `/api/*`、`/api/v1`、task/runner/SSE 与前端表面完全不变。
- **Phase-5 gate**：`tests/test_adoption_lifecycle.py`（promote / 漂移撤销 /
  幂等空单 detach / 无信号 / bounded 审计）与 `tests/test_adoption_e2e.py`
  （4 条 golden，真实 hub + REAL ControlClient + REAL Supervisor 探针）全部通过，
  完整 `pytest -q` 通过；旧“/api/*”、`/api/v1`、观察 / 会话 / 任务 / runner / SSE
  与前端契约保持不变。

## 七、安全红线

- token 不进入 git、聊天、文档或日志。
- 私钥不需要部署到 hub，也不进入仓库。
- ingest machine 名称必须匹配安全字符集。
- 公网 hub 没有 token 时拒绝生产启动。

### 路线 A 薄客户端控制台（已落地，非下一步）

2026-09-07 产品决策：走路线 A（薄客户端 + 远程机器继续干活）。设计见
[`docs/superpowers/specs/2026-09-07-thin-client-remote-control-design.md`](superpowers/specs/2026-09-07-thin-client-remote-control-design.md)。
Phase 1–5 已接线：Fleet 会话条、机器页显式纳管/撤销、会话页对 adopted 座位发五动作、task↔session 可选深链（仅当已有 `attempt_id` 绑定，不造列）；热路径会话 I/O 透传。Phase 4 composer / Phase 5 本机 profile 切换已进枚举。**2026-09-08 LIVE 已开** gates；**2026-09-11 已向 mac-local adopted 座位实发** Phase 5（幂等 applied）与 Phase 4（节点 `resume_unverified`，recovered 席无 native identity，拒 sibling）。Phase 5 只翻本机 `is_current`，不热切正在跑的进程。

*架构或部署变化必须同步更新本文件、`docs/architecture-v3.md`、适用的 v4 control-plane 文档，以及已批准的后续 spec。*
