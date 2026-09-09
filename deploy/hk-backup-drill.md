# HK 备份恢复演练清单（只读核对 + 批准后才真切回）

> 默认只核对 bak 存在与 tar 覆盖步骤。**禁止未批准在 LIVE bind-mount 上真切回。**
> 权威部署步骤仍是仓库外 / Obsidian 部署笔记 §0；本文不替代 §0。

## 已知演练源

- 2026-09-09 bak：`agent-fleet.bak-20260909-pre-182e034`（files API overlay 前）
- LIVE 根：容器内 `${HOME}/agent-fleet`（bind-mount，**禁止 `ln -sfn`**）
- 任务库 / 审计：`state/fleet.db`（随 bak 走；无单独 TTL purge）

## 0. 只读核对（本轮默认可做）

在 HK **不要**停 web、不要覆盖目录。只确认：

1. bak 目录存在，且含 `hub/` `frontend/` `state/` 或当时打包的等价树。
2. `tar -tzf`（若 bak 是 tarball）能列出 `hub/web.py`、`frontend/index.html`。
3. 当前 LIVE `RELEASE_ORIGIN` / 部署记录能对应到 CI artifact，而不是工作树脏拷。
4. Nginx 仍指向容器 `127.0.0.1:8790`，不是宿主 8790。

| 检查 | 命令提示 | 预期 | 实际 |
|---|---|---|---|
| bak 存在 | `ls -ld` bak 路径 | 目录或 tar 在 | |
| 树含 hub | `ls hub/web.py` 或 `tar -tzf … \| grep hub/web.py` | 命中 | |
| 树含 frontend | 同上 `frontend/index.html` | 命中 | |
| 不误用软链切 LIVE | `readlink` LIVE 根 | 非 `ln -sfn` 切换方案 | |

## 1. 真切回（必须 operator 书面批准）

批准前保持 web 运行。批准后按 §0，顺序不可反：

1. 停 web / 停 guardian watcher（避免覆盖中途自愈再拉起旧进程）。
2. 再做一次 `cp -a LIVE bak-<ts>-pre-rollback`（新 bak，不要覆盖演练源）。
3. 用批准的 bak tar **覆盖 bind-mount 内容**（不是换软链）。
4. TERM web，等 keepalive / 手工拉起。
5. 拨测矩阵（出站，无 operator cookie）：

| 路径 | 预期 |
|---|---|
| `GET /api/status` | 200 |
| `GET /api/tasks` | 无 operator = 401 |
| `GET /api/tasks/<id>/files` | 无 operator = 401 |
| `GET /api/tasks/<id>/diff` | 无 operator = 401 |
| `POST /api/ingest` 无 token | 403 |
| `POST /api/commands/poll` 无凭据 | 403 |

6. 有 operator 会话后再点开一任务详情：日志 / files / 若该 bak 含新列则 patch 面板不 500。

失败则立刻用步骤 2 的 pre-rollback bak 再覆盖一次，不要继续「修目录」。

## 2. 禁止

- 未批准真切回 LIVE。
- 用 `ln -sfn` 切 LIVE。
- 演练时打开 guardian 自动重启覆盖半写入树。
- 把 runner / ingest secret 写进本清单或 git。
- 为演练自动 purge `audit` 表。

## 3. 告警口径（v4）

v4 告警 = stale 机器 + 任务 `failed` 事件进 SSE / stdout→cron。不是 pager，不接 TG。
