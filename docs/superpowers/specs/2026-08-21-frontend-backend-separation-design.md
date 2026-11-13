# agent-fleet 前后端分离与架构隔离设计

**状态：已获批准的设计，待实施计划**  
**日期：2026-08-21**  
**适用基线：v4 控制面（`8be14b2`）**

## 1. 目标

将 agent-fleet 从“Flask 进程同时托管页面、API、业务编排和存储访问”的 v4 形态，渐进迁移到两个可独立发布的运行时产物：

1. **静态前端 release**：只包含 HTML、原生 JavaScript、CSS 和不含秘密的运行时配置。
2. **backend API release**：只负责 JSON API、SSE、认证适配、业务服务、后台 reconciliation 和持久化。

公网入口第一阶段保持同源反向代理：浏览器访问同一个域名，edge/Nginx 按路径将静态页面和 API 转发到不同 release。这样可以获得独立发布能力，同时保留 Cloudflare Access cookie、SSE 同源连接和现有机器上报协议。

本设计不改变 v4 的核心安全边界：

- agent probe 主动 POST observation；
- runner 主动 poll、heartbeat、result；
- hub 不通过 SSH、反向隧道或中央私钥连接 agent；
- 浏览器不直接连接 agent；
- observe、operator、runner 三个认证域互不复用；
- runner adapter allowlist 和 project whitelist 只能由 runner 本地配置强制执行；
- token、credential、私钥、session 内容和原始 collector 输出不进入前端或 git。

## 2. 非目标

本次设计不包含以下能力：

- 不实现 `GET /api/tasks/<id>/files/<path>` 文件查看；
- 不引入 WebSocket；实时事件继续使用 SSE；
- 不引入 Node、npm、webpack、Vite 或其他前端构建链；
- 不把任意 shell 命令接口暴露给页面或 backend；
- 不一次性重写 SQLite、JSONL 或 runner 协议；
- 不要求第一阶段启用跨域部署；
- 不在本设计执行 Cloudflare、Nginx 或生产机器变更。

## 3. 当前问题与设计原则

### 3.1 当前问题

当前代码已经按 Blueprint 拆分 observe、tasks、commands，但仍存在以下跨层耦合：

- `hub/web.py` 同时承担 app factory、页面路由、Blueprint 注册、配置路径、reconciliation 启动和 lease 回收启动；
- `routes_observe.py` 直接读取状态文件、加载 hosts 配置并构造页面和接口 read model；
- `routes_tasks.py` 直接执行输入校验、在线判断、project 白名单判断、SQLite 写入和事件发布；
- `routes_commands.py` 直接操作 lease、heartbeat 和 result store；
- `hub/events.py` 将 domain event、JSONL 持久化、全局 subscriber 和 SSE 队列放在同一模块；
- `FLEET_HOME` 和 `sys.path.insert(...)` 在多个入口重复出现；
- 页面模板和 `app.js` 直接拼接 `/api/...`、页面 URL 和 SSE URL；
- `location.reload()`、模板 bootstrap 数据和 Flask `url_for('static', ...)` 使页面发布依赖 Flask；
- 前端渲染仍有 `innerHTML` 字符串拼接，虽有 escaping 回归测试，但没有独立的 data、client、view 边界；
- observation JSONL、event JSONL 和 SQLite 的所有权通过模块全局变量表达，不能清楚区分 repository、application service 和 HTTP transport。

### 3.2 原则

1. **先隔离边界，再迁移实现**：每个阶段都保持现有 API 和 observation 链路可运行。
2. **同源优先，独立发布**：发布产物分离，公网路径暂时同源，避免不必要的 CORS 和 Access 会话变化。
3. **transport 不拥有业务规则**：HTTP route 只负责请求解析、认证适配、调用 service 和响应序列化。
4. **依赖由外向内**：domain 不依赖 Flask、文件路径、SQLite 或浏览器；infrastructure 实现接口；bootstrap 组装对象。
5. **契约优先**：浏览器只消费 public DTO；内部 attempt、nonce、credential、collector 原始数据永不进入 public contract。
6. **失败隔离**：事件持久化或任务存储故障不能破坏 observation ingest；任务 API 降级不能让观测 API 失效。
7. **可测试的实例隔离**：app、publisher 和 repository 使用显式实例及构造参数，测试不得依赖仓库真实 `state/`。

## 4. 目标部署拓扑

```text
Browser
  |
  v
Cloudflare Access
  |
  +-- / and /assets/* ----> static frontend release
  |
  +-- /api/* -------------> backend API release
          |
          +-- /api/stream --> SSE transport, proxy buffering disabled
          +-- /api/ingest --> bypass + X-Agent-Fleet-Token
          +-- /api/commands/* -> bypass + X-Runner-Credential

Agent probe  --主动 HTTPS POST /api/ingest--> backend API
Agent runner --主动 HTTPS poll/heartbeat/result--> backend API
```

### 4.1 发布边界

前端 release 和 backend release 必须可以独立构建、发布、回滚。静态前端不依赖 backend 进程提供 HTML 模板或静态资源；backend 不依赖前端文件才能提供 JSON API、SSE、probe 和 runner 协议。

第一阶段可继续在开发环境由 Flask 提供 compatibility page shell，但生产目标是由 edge/Nginx 将 `/` 和 `/assets/*` 指向静态 release，将 `/api/*` 指向 backend release。页面和 API 仍使用同一公开 origin。

### 4.2 缓存与 SSE

- 带内容哈希的 frontend assets 可以使用长期 immutable cache。
- `index.html` 和运行时配置不得使用不可失效的长期缓存。
- JSON API 使用 `Cache-Control: no-store`，除非某个 endpoint 在后续契约中明确允许缓存。
- `/api/stream` 必须设置 `Content-Type: text/event-stream`、`Cache-Control: no-cache` 和 `X-Accel-Buffering: no`；代理读取超时必须覆盖正常 keepalive 周期。
- probe、runner 和 operator API 的路由认证规则不因静态前端分离而改变。

## 5. 前端架构

新前端位于独立 `frontend/` 目录，使用浏览器原生 JavaScript，不引入构建链：

```text
frontend/
  index.html
  config.js
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

### 5.1 `config.js`

只提供非秘密运行时配置，例如：

```javascript
window.FleetConfig = Object.freeze({
  apiBaseUrl: "/api",
  pageBaseUrl: ""
});
```

配置可以在静态 release 部署时生成，也可以由不含秘密的 `<script>` 配置文件提供。不得包含 ingest token、runner credential、Access header、内部文件路径或数据库信息。默认 `apiBaseUrl` 为同源 `/api`。

### 5.2 API client 与 contracts

`api/client.js` 是前端唯一的 HTTP 入口。view 和 state 层不得调用 `fetch`，不得自行拼接 `/api` 路径。client 至少提供：

- `getStatus()`；
- `getMachine(name)`；
- `getEvents(limit)`；
- `listTasks(filters)`；
- `getTask(taskId)`；
- `createTask(input)`；
- `cancelTask(taskId)`；
- `retryTask(taskId)`。

client 统一完成：

- API base URL 解析；
- URL path segment 编码；
- JSON 请求和响应解析；
- HTTP 非 2xx 转换为统一 `ApiError`；
- 响应 `ok` 和必要字段校验；
- 请求超时和网络错误分类；
- `no-store` 请求策略。

`api/contracts.js` 只描述浏览器可见 DTO 和 SSE public event DTO。至少覆盖：Fleet status、machine detail、task summary、task detail/result、task error、machine update、task update、task log。contract 校验失败时，前端显示有限的通用错误，不将原始响应或内部异常直接渲染到页面。

### 5.3 SSE client 与 state store

`realtime/sse.js` 唯一拥有 `EventSource` 生命周期：

- 从 store 读取 `lastEventTs` 并发送 `since`；
- 处理 `machine_update`、`task_update`、`task_log`、`fleet_event`；
- 成功接收事件后更新游标；
- 连接失败时通知 store 并允许有限间隔的 status 轮询降级；
- reconnect 不得重复创建多个活动连接；
- close/unsubscribe 必须释放连接和计时器。

SSE client 不直接调用 DOM。收到终态 task event 后，由 store 调用 `getTask(taskId)` 更新 task detail，禁止使用无条件 `location.reload()` 作为业务同步机制。

`state/store.js` 保存页面模型、task 模型、SSE 状态和事件游标，并向 view 发出变化通知。store 不知道 HTML 结构，也不包含认证策略。

### 5.4 views 和安全渲染

`views/fleet.js`、`views/machine.js`、`views/task.js` 只接收已通过 contracts 校验的数据。优先使用 `document.createElement`、`textContent`、属性赋值和事件委托；必须生成片段时使用一个集中安全 renderer，并保证所有动态文本、属性、URL segment 都经过对应编码。

任务终态、错误、空列表、SSE 断开、API 503 和首次加载状态必须是显式 UI 状态。view 不应依赖 Flask server-rendered task object 或模板中的业务数据。

现有 `hub/templates/` 和 `hub/static/` 在迁移期保留为 compatibility shell。它们不能成为新前端逻辑的第二个业务实现；新 client、contract 和 SSE 行为必须有单一来源。

## 6. 后端分层

目标 backend 结构如下：

```text
hub/
  __init__.py
  bootstrap.py
  config.py
  http/
    pages.py
    observe_routes.py
    task_routes.py
    command_routes.py
    errors.py
  application/
    observe_service.py
    task_service.py
    runner_service.py
  domain/
    events.py
    task.py
    machine.py
  infrastructure/
    state_repository.py
    event_repository.py
    task_repository.py
```

文件可以按阶段落地；迁移前不得为了目录形式复制同一套业务逻辑。

### 6.1 `config.py` 与 `bootstrap.py`

`config.py` 统一解析 repository 路径、hosts 配置、token 来源、runner credential 来源、任务开关和运行环境。`FLEET_HOME` 只在配置入口解析一次，禁止业务模块重复计算。路径、repository 和 publisher 通过构造参数注入。

`bootstrap.py` 负责：

1. 创建配置对象；
2. 创建 observation、event、task repositories；
3. 创建 event publisher；
4. 创建 observe、task、runner application services；
5. 创建 Flask app 并注册 HTTP adapters；
6. 在明确的进程启动入口启动 reconciliation 和 lease reconciler。

测试可以传入临时目录和 fake repository 创建 app instance，不修改模块级路径变量。

### 6.2 domain

`domain/` 定义不依赖 Flask 或存储的对象和规则：

- task state 和合法状态迁移；
- machine public summary 的字段含义；
- domain event 类型和 public event 映射；
- 任务输入、结果和 lease 的边界值。

现有 attempt fencing、nonce 校验、lease TTL、结果幂等和 adapter/project 安全约束必须保持。domain 不读取请求头，也不决定 Cloudflare、ingest 或 runner 认证方式。

### 6.3 application services

application service 实现可复用的用例：

- `ObserveService.ingest(payload)`：验证机器、sanitize payload、比较旧快照、保存 snapshot、发布 state event；
- `ObserveService.status()` 和 `machine_detail(name)`：从 repository 生成 public read model；
- `TaskService.create/list/get/cancel/retry()`：执行 task policy、在线和 project 注册判断、调用 task repository、记录 actor 和发布 task event；
- `RunnerService.poll/heartbeat/result()`：执行 runner-facing lease 和 bounded result 用例；
- reconciliation service：只做 push observation stale reconciliation 和 task lease expiration，不执行远程命令。

service 可以返回 typed result 或明确的 application error。它们不得返回 Flask response，也不得读取 `request`, `g`, `current_app`。

### 6.4 HTTP adapters

`http/` 只负责：

- 读取 JSON、query、path 和可信认证 identity；
- 调用对应 application service；
- 将 public result 和 application error 序列化；
- 设置 HTTP status、SSE headers 和 request id。

HTTP route 不得直接导入 `state.STATE_DIR`、`events.EVENT_LOG`、`task_store.DB_PATH`，不得直接执行 SQLite、JSONL I/O 或 hosts 文件解析。认证 header extraction 属于 transport adapter；authorization policy 由 auth/application boundary 表达。

## 7. 存储和事件所有权

### 7.1 repositories

定义三个明确的 repository 边界：

- `ObservationRepository`：current snapshot、machine history JSONL、原子替换、轮转和历史读取；
- `EventRepository`：event append、bounded recent query、replay cursor 和轮转；
- `TaskRepository`：SQLite task、lease、result、audit，以及事务和 fencing 所需的原子操作。

第一期继续使用 JSONL 和 SQLite 的现有格式，repository implementation 通过构造参数接收目录或 DB path。迁移 repository 不得改变现有 API payload 或 task state semantics。

### 7.2 并发和进程边界

当前进程内 lock 只能作为单进程优化，不能被当作多 worker 一致性保证。repository 必须明确部署假设：第一阶段 backend 的写入 ownership 为单一 writer 进程或由部署层保证单 writer；SQLite 使用现有 WAL/事务；event/state 文件的 append 和 rotation 必须在同一 repository 内使用可说明的锁策略。

如果后续支持多 backend worker，必须先增加跨进程锁、独立 event broker 或数据库 event store 的设计和测试，不能仅复制当前全局列表。

### 7.3 event publisher

将当前 `hub/events.py` 的职责拆为：

```text
domain event
  -> EventPublisher
       +-> EventRepository
       +-> bounded SSE subscriber registry
       +-> optional notifier subscriber
```

publisher 是 app instance 级对象。SSE subscriber 生命周期必须显式注册和注销；队列满时丢弃实时事件但不阻塞 ingest、task completion 或 repository commit。SSE replay 只发送脱敏 public event DTO；事件持久化失败不得让 observation 或 task 主操作失败，但必须留下可诊断的本地日志。

## 8. API 和认证契约

### 8.1 兼容路径

第一阶段保留并继续支持这些路径：

```text
/api/status
/api/machines/<name>
/api/events
/api/stream
/api/ingest
/api/scan
/api/tasks
/api/tasks/<id>
/api/tasks/<id>/cancel
/api/tasks/<id>/retry
/api/commands/poll
/api/commands/<attempt_id>/heartbeat
/api/commands/<attempt_id>/result
```

旧路径和未来 versioned path 必须调用同一 application service，不得复制业务实现。probe、runner 和现有部署第一阶段继续使用兼容路径。

### 8.2 版本化路径

为未来迁移预留 `/api/v1` adapter，至少覆盖 public observe、task operator API 和 SSE。版本化 adapter 只有在 contract tests 覆盖旧响应、认证矩阵和错误语义后才启用前端默认使用。旧路径暂不删除，并在独立迁移完成前继续作为 probe/runner 稳定入口。

### 8.3 public response

错误响应统一为：

```json
{
  "ok": false,
  "error": "machine_offline",
  "detail": "target machine is not reachable",
  "request_id": "opaque-request-id"
}
```

`detail` 只能是用户可理解且有界的描述；不返回 Python traceback、绝对路径、SQL、token、credential、原始 collector output 或内部异常文本。现有客户端依赖的 `ok`, `error`, `detail` 字段必须保持兼容。

### 8.4 认证矩阵

| 域 | 路径 | 认证和权限 |
|---|---|---|
| public observe | `/api/status`、`/api/machines/*`、`/api/events`、`/api/stream` | 只返回已脱敏 public read model；生产访问策略由 edge 控制 |
| observe ingest | `/api/ingest`、`/api/scan` | `X-Agent-Fleet-Token`；不得回退 operator 或 runner 身份 |
| operator | 页面和 `/api/tasks*` | Cloudflare Access operator identity；不得接受 ingest 或 runner header 作为 fallback |
| runner | `/api/commands/*` | machine-bound `X-Runner-Credential`，并校验 machine、attempt、nonce、lease |

静态前端只依赖 operator 的浏览器会话。它不读取、转发或存储 observe token 和 runner credential。

### 8.5 CORS 和同源策略

第一阶段生产路径采用同源反向代理，不启用宽泛 CORS。若未来需要不同 origin，必须单独增加：

- 明确的 allowed origins 配置；
- 不允许 `*` 与 credentials 组合；
- operator cookie 的 `SameSite`, `Secure` 和 credentials 策略测试；
- SSE 跨域连接和 Access session 测试；
- trusted proxy/header 验证；
- preflight 和错误响应测试。

## 9. 迁移阶段与独立验收

### Phase 1：边界基础

交付：

- 正式 `hub` package/import contract；
- `config.py` 和 `bootstrap.py`；
- repository interface 和现有 JSONL/SQLite adapters；
- app instance 级配置和事件 publisher；
- 旧 routes、probe、runner 和 API 行为保持不变。

验收：现有全量测试、compileall、observation ingest/status/detail/events/SSE、task API、runner E2E 通过；测试 storage 完全隔离仓库真实 `state/`。

### Phase 2：application services

交付：

- observe、task、runner application services；
- routes 变成薄 HTTP adapters；
- auth transport extraction 与 authorization policy 分离；
- event publisher/repository/SSE registry 拆分；
- 统一 public error serialization。

验收：service 可脱离 Flask context 单测；认证矩阵测试覆盖三域互斥；旧 API 响应兼容；push-only 静态检查和真实 runner E2E 通过；任务存储故障时 observation 仍可工作。

### Phase 3：独立静态前端

交付：

- `frontend/` Fleet、machine、task 三视图；
- API client、contracts、SSE client、state store；
- 同源 `/api` runtime config；
- 终态任务按 API 更新，不使用业务性 `location.reload()`；
- DOM 安全渲染和前端错误/断线/空状态；
- 旧 Flask page shell 可回滚但不再作为新前端逻辑来源。

验收：静态文件无需 Flask 页面渲染即可打开；浏览器只通过 client 请求 API；SSE 重连、since replay、轮询降级、task create/cancel/retry、machine drill-down 和 task detail 全部通过集成测试；XSS regression 继续通过；390px 和 1440px 页面检查继续通过。

### Phase 4：独立发布和 compatibility API

交付：

- frontend/backend 两个 release 目录或等价发布产物；
- Nginx/edge 路由文档和 SSE buffering 配置；
- `/api/v1` adapters 与 contract tests；
- 静态缓存策略、回滚顺序和兼容矩阵；
- Cloudflare Access、observe bypass、runner bypass 的路径验收文档。

验收：前端和 backend 可以分别发布和回滚；旧 probe、runner 和 `/api/*` 仍可用；Access operator 页面、SSE、task API 通过；ingest 和 runner 不被登录页拦截；生产配置不含真实凭据；未执行未经授权的外部部署。

## 10. 测试策略

测试按边界而不是按实现文件组织：

- `tests/test_config.py`：配置、路径注入和环境解析；
- `tests/test_repositories.py`：JSONL rotation、atomic current、SQLite WAL、lease fencing、实例路径隔离；
- `tests/test_services.py`：observe/task/runner 用例，不创建 Flask request context；
- `tests/test_http_contracts.py`：旧路径和 `/api/v1` 的响应、错误和 status code；
- `tests/test_auth_matrix.py`：observe/operator/runner 互斥；
- `tests/test_events.py`：publisher、bounded subscriber、unsubscribe、replay 和持久化失败隔离；
- `tests/test_frontend_contracts.py`：静态 client URL、DTO 校验、SSE lifecycle、safe rendering 的 source/runtime checks；
- 现有 `tests/test_frontend_xss.py`、observation tests、task API tests、runner tests 和 E2E smoke 必须保留并适配新边界，不得删除以绕过回归。

每一阶段至少执行：

```text
.venv/bin/python -m unittest discover -s tests -v
python3 -m compileall -q connectors hub tools tests
bash deploy/e2e-smoke.sh
```

静态前端若没有浏览器测试依赖，至少提供可重复的 source/contract checks；新增浏览器集成测试不得引入 Node 构建链。

## 11. 兼容与回滚

- backend 先于 frontend 发布新 client 所需的兼容 API；前端不能先依赖尚未部署的响应字段。
- 新前端必须能在旧 `/api/*` backend 上运行，或 release 配置必须明确选择旧 client；禁止隐式猜测版本。
- frontend rollback 只切换静态 release，不影响 probe、runner 和 backend state。
- backend rollback 必须保留现有 JSONL、SQLite schema 和旧 API path；不允许通过回滚破坏尚未完成的 task lease。
- `/api/v1` 只有在旧路径和新路径共用 service 且兼容测试通过后才能成为前端默认入口。
- 每一阶段结束保留旧页面 shell 或明确的静态 release rollback path，直到下一阶段验收完成。

## 12. 完成定义

本设计的实施完成必须同时满足：

1. frontend 和 backend 是可分别发布、回滚的两个产物；
2. 浏览器通过单一 API client 和 SSE client 访问 backend；
3. Flask HTTP routes 不直接执行业务存储 I/O；
4. application service 可脱离 Flask context 测试；
5. observation、task、runner 三个认证域仍然互斥；
6. push-only、adapter allowlist、project whitelist 和敏感数据白名单没有退化；
7. 现有 probe、runner、observation 链路和任务 E2E 继续通过；
8. 同源部署不依赖宽泛 CORS；
9. 文档明确静态缓存、SSE、Access bypass、回滚和 release compatibility；
10. 不包含任何真实 token、密码、runner credential、私钥或外部发布凭据。

## 13. 设计决策记录

### 决策 A：采用同源反向代理作为第一阶段生产入口

独立发布和同源访问同时满足；可以避免第一阶段引入跨域 cookie、SSE credentials 和 Cloudflare Access header 传递风险。不同 origin 作为后续显式设计，而不是迁移中的隐含副作用。

### 决策 B：保留旧 `/api/*`，以 adapter 形式增加 `/api/v1`

probe 和 runner 已部署，立即改路径会扩大上线风险。统一 service 加多个 HTTP adapter 可以兼容现有客户端，同时为前端和未来 contract versioning 提供稳定迁移点。

### 决策 C：继续使用原生 JavaScript

项目当前没有 Node 构建链；前端分离的核心是发布边界和依赖边界，不是引入新的构建系统。原生模块、runtime config、source checks 和浏览器集成测试足以支持当前规模。

### 决策 D：先抽 repository 和 application service，不先迁移数据库

当前 JSONL snapshot、event log 和 SQLite task store 已有生产行为和测试覆盖。先建立所有权与接口，再考虑更换存储，可以降低一次性迁移造成的观测链路回归风险。
