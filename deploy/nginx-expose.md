# push-only hub 公网暴露

## 网络拓扑

```
机器上的 agent-self-report.py
  -> HTTPS POST https://hub.example.com/api/ingest
  -> Cloudflare / Nginx
  -> HK 容器 hub/web.py:8790
```

## Nginx

```nginx
server {
    listen 443 ssl http2;
    server_name hub.example.com;
    ssl_certificate     /etc/nginx/ssl/hub.example.com.pem;
    ssl_certificate_key /etc/nginx/ssl/hub.example.com.key;

    location / {
        proxy_pass http://127.0.0.1:8790;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 30s;
    }
}
```

## HK hub

启动前必须配置 token：

```bash
export AGENT_FLEET_INGEST_TOKEN='<secret>'
python3 hub/web.py --host 0.0.0.0 --port 8790
```

token 不写进 git、Nginx 配置或文档。

## agent 机器

每台机器只需出站 HTTPS。部署项目目录后运行：

```bash
mkdir -p ~/.config/agent-fleet
chmod 700 ~/.config/agent-fleet
printf '%s' "$AGENT_FLEET_INGEST_TOKEN" > ~/.config/agent-fleet/ingest-token
chmod 600 ~/.config/agent-fleet/ingest-token
python3 tools/agent-self-report.py --dry-run --name <machine>
```

定时任务示例见 [agent-self-report.cron.example](agent-self-report.cron.example)。

## 验证

```bash
curl -s https://hub.example.com/api/status
python3 tools/agent-self-report.py --endpoint https://hub.example.com --name <machine> --token-file ~/.config/agent-fleet/ingest-token
```

hub 不需要保存任何 agent 机器的私钥或远程连接配置。

## v4 控制面衔接（2026-08-19 起）

nginx 反代配置不变（443 → 8790）。operator 认证由 Cloudflare Access 在边缘完成，
配置见 `deploy/cloudflare-access.md`。注意 `/api/ingest`、`/api/scan`、`/api/commands/*`
必须在 Access 中 bypass，否则 probe/runner 会被登录页拦截。

## 同源前端/后端路由（Task 20,2026-08-24）

浏览器与 API 走**同一域**（同源），因此前端与后端之间**不需要 CORS**，也不应写
`Access-Control-Allow-Origin:*`。参考示例配置见
`deploy/nginx-frontend-backend.example.conf`（用占位 upstream `fleet_frontend` /
`fleet_backend`，不含真实证书/密钥/凭据）。

路由矩阵：

| 路径 | upstream | 说明 |
|---|---|---|
| `/` | 前端（静态 release） | `index.html` / 页面入口，`Cache-Control: no-cache` |
| `/assets/*` | 前端（静态 release） | 内容哈希资源，long immutable cache |
| `/api/*` | 后端（hub） | JSON API / probe / runner |
| `/api/stream` | 后端（hub） | SSE：`proxy_buffering off`、长读超时（示例 3600s 或等效） |
| `/api/ingest`、`/api/scan`、`/api/commands/*` | 后端（hub） | 机器侧，Access **Bypass**，凭据在 hub 校验 |

- Access Bypass 是**边缘策略**，不是 hub 的授权旁路：probe/runner 不带浏览器 cookie，
  但 hub 仍用 ingest token / runner credential 校验，见 `deploy/cloudflare-access.md`。
- `/api/*`（含 `/api/v1` 兼容适配器）保持既有 JSON/SSE 契约，旧路径不因前端拆分改变。
- 分层与回滚契约见 `deploy/frontend-release-layout.md`。
- 本地路由冒烟（不访问 Cloudflare/生产）：`bash deploy/test-release-routing.sh`。
