# agent-fleet 架构 Review（2026-08-16）

> **历史文档**：本文记录已废弃的中心拉取方案。当前以 [architecture-v3.md](architecture-v3.md) push-only 架构为准，本文不指导部署。P1/P2（并行 SSH scan、config 路径、events 轮转、web 异步扫描）针对已删除的中心拉取路径，**不要当待办重做**。现行实时性由 probe ingest + SSE 提供，见 `docs/HANDOFF.md` §六。

> 目标：验证连接器抽象架构是否满足「实时 + 省资源 + 易扩展 + 不乱通知」四大诉求，
> 找出现有问题与改进点。

## 一、总体评价 ✅

**架构方向正确**。连接器抽象 + 增量扫描 + 事件总线 + 静默告警，四点核心诉求全部满足，
实测性能（0.1s/全舰）与静默性（3 次扫描 0 事件）达标。以下是发现的具体问题，按严重度排列。

## 二、问题清单

### P1 — 结构性（建议尽快修）

1. **`scan.py` 逐台串行 + SSH 无并行** — 目前 `scan_all()` 循环内逐个 `scan_machine()`，
   远程 SSH 探测+采集是串行的。机器多了（10+）会线性变慢（即使单台 0.1s，10 台 = 1s）。
   **改进**：`ThreadPoolExecutor` 并行扫描各机器（SSH 是 IO 密集，线程足够）。
   预估 10 台 → < 0.5s。

2. **`_collect_system` 在远端跑 4 条独立 SSH 命令** — 每条命令一次 SSH 往返（load/mem/disk/uptime）。
   对远端机器 = 4 次连接开销。**改进**：合成一条 `bash -c` 多行命令一次执行（类型 shell 脚本），
   一次 SSH 拿全系统状态。对 4 台远端机器从 16 次 SSH 降为 4 次。

3. **`credentials/config` 现在与容器路径绑定** — 若将来在中枢机器 A/B 间迁移，绝对路径
   `${HOME}/agent-fleet/credentials/keys/*` 会挂。
   **改进**：config 里用 `~/.ssh/...` 相对用户目录，或启动脚本里 sed 替换 `__FLEET_HOME__` 占位符。

### P2 — 可靠性（建议做）

4. **`_diff` 用 `agents` 整块 JSON 比较** — agents 里包含 sessions 列表（每几秒更新），
   只要 Hermes 有新会话/会话更新时间变化，`agents` 整块就不等 → 触发 state_changed 事件。
   虽然 notifier 里只有"值得关注"的才输出，但 events.jsonl 会被会话更新时间变化刷爆。
   **改进**：diff 用**规范化摘要对比**（如 `summarize()` 后的短字符串），
   而不是原始 JSON 全等；或对 sessions 只取 key 列表与创建时间。

5. **`_diff` 首轮 `["initial"]` 事件** — 每台机器首次接入都会发 initial 事件（好），
   但 events.jsonl 持久化会无限增长。**改进**：events.jsonl 加轮转（如 >1MB 截断为最近 1000 行）。

6. **web.py `collect_all` 未捕获告警** — `/api/scan` 触发 `scan.scan_all()` 时同步执行，
   若 SSH 慢会被 Flask 阻塞（dev server 单线程）。**改进**：后台线程触发扫描，立即返回。

### P3 — 体验（可后置）

7. **web 页面 60s 整页刷新** — 简单但粗糙。**改进**：前端 `fetch('/api/status')` 轮询 10s，
   局部更新 DOM（不动页面），未来可上 SSE/WebSocket 实时推送。

8. **notifier 只支持 stdout** — 告警输出到 stdout，由 cron 捕获。直接推送微信/TG 是占位。
   **改进**：接 Hermes 的 send_message 能力（TG/微信），但现在"stdout→cron→local"已满足不乱通知。

9. **hosts.yaml 里没有 SSH 入口的节点无面板 API 采集** — 只能标"无 SSH 入口"。
   **改进**：将来加 panel connector（面板 API），用面板 key 采集。

### P4 — 凭证安全（继承约束）

10. **私钥传输是当前唯一阻塞** — 用户约束"私钥不进文档"，但 SSH 必须有文件本体。
    **建议**：私钥放 `credentials/keys/`（已被 gitignore），通过**安全通道**（如用户 scp 或
    一次性粘贴）进容器，不进 obsidian/聊天记录。config 引用路径已就绪。

## 三、架构图（现状）

```
┌─ 各节点（示例）──────────────────────────┐
│  worker-a: Cloudflare Tunnel + ssh      │
│  worker-b: 直连                         │
│  hub-host: 本机 (local)                 │
└──────────────┬──────────────────────────┘
               │ SSH（探针，只读状态文件）
┌──────────────▼──────────────────────────┐
│ hub/scan.py  增量扫描 (0.1s)            │
│   ├─ _probe_ssh → 不可达跳过            │
│   ├─ connectors.collect() × N           │
│   └─ _diff → 只对比重要字段             │
├ hub/state.py  JSONL 历史 + current      │
├ hub/events.py 事件总线                  │
├ hub/notifier.py 阈值告警（stdout）       │
├ hub/control.py 管控（connector 路由）    │
└ hub/web.py     Flask 总览 0.0.0.0:8790  │
connectors/                               │
├ base.py / hermes.py / claude_code.py /  │
│ generic.py / probe.py                   │
hosts.yaml + credentials/（gitignore）    │
```

## 四、建议实施优先级

| 优先级 | 项 | 收益 | 估时 |
|---|---|---|---|
| P1 | ①并行扫描 + ②系统命令合并 | 性能 ↑↑（10台<0.5s） | 0.5h |
| P2 | ④规范化 diff | 事件日志不刷爆 | 0.5h |
| P2 | ⑤events 轮转 | 持久化不膨胀 | 10min |
| P2 | ⑥web 异步扫描 | 页面不卡 | 10min |
| P3 | ⑦前端轮询 | 体验↑ | 0.5h |
| P1 | ③config 路径抽象 | 可迁移性 | 10min |

**建议本轮实施**：P1 全部 + P2 的 ④⑤⑥。P3 下一轮。P4 凭证待用户提供。

## 五、结论

架构骨架正确且经实测验证。主要差距在**规模化性能**（串行→并行）和**事件噪音**（整块 diff→
规范化 diff）。修完 P1+P2 后，系统可承载 20+ 机器、事件零噪音、通知零骚扰。
凭证到位后即可接入全部机器，接 Nginx 即公网可访问。
