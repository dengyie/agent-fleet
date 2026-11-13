# Cloudflare Access — operator 认证配置（v4 控制面）

hub 的 `/api/tasks*` 与页面路由依赖 `Cf-Access-Authenticated-User-Email` 头确认
operator 身份。该头由 Cloudflare Access 边缘注入；nginx/容器不改动。

浏览器与 API 走**同一域**（同源），因此前端与后端之间**不需要 CORS**，也不应写
`Access-Control-Allow-Origin:*`。同源路由示例见
`deploy/nginx-frontend-backend.example.conf`。

## 路径划分

| 路径 | 认证 | Access 策略 |
|---|---|---|
| `/`、`/assets/*`、`/machine/*`、`/task/*` | operator 页面/静态（operator 前端） | **Include** 策略 |
| `/api/status`、`/api/machines/*`、`/api/events`、`/api/stream`、`/api/tasks*` | CF Access（operator） | **Include** 策略 |
| `/api/ingest`、`/api/scan` | X-Agent-Fleet-Token | **Bypass** 策略 |
| `/api/commands/*` | X-Runner-Credential | **Bypass** 策略 |

- `/`、`/assets/*` 与页面路由（`/machine/*`、`/task/*`）是 operator 前端，保持
  **Include**；它们由静态 release 提供（见 `deploy/frontend-release-layout.md`）。
- `/api/stream`（SSE）是 operator 观察流，保持 **Include**；Nginx 侧需
  `proxy_buffering off` + 长读超时（见示例配置）。
- 机器 probe/runner 不带浏览器 cookie，必须走 **Bypass**；它们有自己的凭据
  （ingest token / runner credential），bypass 不降低安全性。
- **Bypass 是边缘策略，不是 hub 的授权旁路**：hub 在 `/api/ingest`、`/api/scan`、
  `/api/commands/*` 上仍按自身凭据校验，满足凭据才能放行。
- `/api/*`（含 `/api/v1` 兼容适配器）保持既有 JSON/SSE 契约，旧路径不因前端拆分改变。

## 配置步骤（Zero Trust Dashboard）

1. Zero Trust → Access → Applications → Add an application → Self-hosted。
2. Application domain: `hub.example.com`（与现有 CF 代理同域）。
3. 建两条 Policy（顺序：先 Bypass 后 Include，Access 按顺序匹配）：
   - Policy A「machine-credentials」：Action = **Bypass**，规则：
     `http.request.uri.path starts with "/api/ingest"` **or**
     `http.request.uri.path starts with "/api/scan"` **or**
     `http.request.uri.path starts with "/api/commands"`。
   - Policy B「operators」：Action = **Allow**，规则：Emails = 运维邮箱列表；
     Session duration 24h。
4. 保存后用浏览器访问 `https://hub.example.com/api/status`：应跳 CF 登录页，
   登录后返回 JSON。用 curl 无 cookie 访问同路径：应 302 到登录页（不是 200）。
5. 验证 bypass：`curl -X POST https://hub.example.com/api/commands/poll` 无 credential
   应返回 **403 JSON**（hub 的拒绝），而不是 CF 登录页 HTML。

## 安全注意

- Access 头只在经 CF 边缘的请求上可信。**源站必须只接受 Cloudflare IP**；公网入口只有
  CF 代理。hub 进程听本机 loopback（示例 `127.0.0.1:8790`），Nginx `proxy_pass` 指向它。
  不要把真实 Docker 桥接 IP 或公网地址写进 git。
- 不要把 `--dev-operator` 带到生产启动参数。
- 吊销 operator：从 Policy B 移除邮箱即可，无需动 hub。
- runner credential 吊销：编辑 HK 容器 `credentials/runner-credentials.json` 删对应机器键，
  重启 hub（下次 poll 即 403）。

## 上线前 checklist

- [ ] Policy A/B 按上表生效（curl 验证通过）
- [ ] 生产 hub 启动命令不含 `--dev-operator`
- [ ] `credentials/runner-credentials.json` 0600 root 所有，只含已部署 runner 的机器
- [ ] hosts.yaml 目标机器已配 `projects` 白名单
