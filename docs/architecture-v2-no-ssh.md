# agent-fleet 架构 v2 — "Agent-as-CLI" 无 SSH 探针 (2026-08-16)

> **历史设计文档**：已由 [architecture-v3.md](architecture-v3.md) 的 push-only 实现取代；文中中心控制/旧通道不再是运行时路径。

## 动机 (用户洞察)

原架构假设"探针 SSH 进每台机器 → 读进程/状态"→ 需要机器级 SSH 权限，
Mac 侧死锁（cloudflared 凭据在 Mac、反向隧道需 Mac 先开）。

**修正**：Agent 本质是 CLI（stdin 输入 / stdout 输出）。探针要做的不是接管整台机器，
而是**接入每个 Agent 自己的输入输出端点** —— 无需系统 SSH 权限，只要该 Agent
在它所在机器上有执行权限（它天然有）。

## 数据流 (v2)

```
┌─ Agent 所在机器 ────────────────────────────┐
│                                             │
│  Agent CLI / 状态端点  ── 输出(状态) ──┐     │
│  Agent stdin/命令入口  ◄─ 输入(指令) ──┤     │
│                                        │     │
└────────────────────────────────────────┼─────┘
                                         ▼
┌──────────────────────────────────────────────┐
│  agent-fleet hub (中央)                       │
│  ▪ probe: 每2min 调用各 agent 的状态端点      │
│  ▪ control: 派 prompt 到 agent stdin          │
│  ▪ web: 总览面板                              │
└──────────────────────────────────────────────┘
```

## 每种 Agent 的接入点（无系统 SSH）

| Agent | 状态读取 (探测) | 指令下发 (控制) | 权限需求 |
|---|---|---|---|
| Hermes | `hermes serve` JSON-RPC/WS，或 `hermes status -json` / `hermes cron list -json` | `hermes -z "prompt"` 或 `hermes send` | Hermes 自身执行权限 |
| Claude Code | `claude -p "status" --output-format json` | `claude -p "task"` | claude CLI 权限 |
| 自定义 CLI | 脚本暴露 stdout/json | 收到 stdin | 该进程权限 |

## 远程执行通道（替代 SSH）

关键差异：**不需要 SSH 进机器，需要"能触发该机器上的 agent CLI"**。通道：

1. **Hermes 原生 `serve`** — 有 WebSocket/JSON-RPC；`--host 0.0.0.0` + auth（但 June 2026 加固：
   非 loopback 绑定强制 auth）。探针可用"反向隧道"建立到 serve 的连接，**不 SSH，只连应用端口**。
2. **`hermes cron` 作为远程执行钩子** — 在目标机器注册一个 cron 任务，内容=执行该 agent 的 CLI
   并上报结果到中央。中央只需"投递指令到 cron"而非"SSH 执行"。
3. **Agent 自带 API**（若 Hermes/Claude 提供）— serve/proxy 端点。

## 为什么这解决 Mac 死锁

不再需要："我 SSH 进 Mac → 拿 cloudflared 凭据"。改为：
- 在 Mac 上装一个"agent-side 报告器"（Hermes cron 或独立 agent），它**主动**向中央上报状态 + 拉取指令。
- 这跟"Mac 主动连中央"是同一个方向 —— 不需要中央反向 SSH 进 Mac。
- 权限 = Hermes 在 Mac 上的本地执行权限（用户已充分拥有）。

## 实施步骤

1. ✅ 新 connector 抽象：`probe`(读) + `control`(写) 分开，不再依赖 SSH probe context
2. `hermes-cron-connector`: 在目标机器注册"上报 cron"，中央读它的 cron 输出
3. `hermes-serve-connector`: 经反向隧道/直连连 serve WebSocket, JSON-RPC 读状态+派指令
4. Mac 接入：在 Mac 注册 "heartbeat cron" → 上报状态到中央（无需 SSH）
5. web 面板改用新的 probe 输出

## 状态

- 本文件: 设计 v2 定稿
- 待确认: 选 serve 通道 or cron 上报通道实现
