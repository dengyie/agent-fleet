# Agent Fleet

> **Lightweight, Zero-Trust AI Agent Fleet Monitoring & Orchestration System**  
> 轻量级、零信任架构的分布式 AI Agent 舰队观测与受控调度系统。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.10+](https://img.shields.io/badge/python-3.10+-brightgreen.svg)](https://www.python.org/)
[![Architecture: Push--Only](https://img.shields.io/badge/Architecture-Push--Only%20%2F%20Zero--Trust-orange.svg)](#-核心优势与架构亮点)

---

## 💡 为什么选择 Agent Fleet？

当你在多台开发机、VPS、云服务器上跑各类 AI Coding Agent（如 Claude Code、Codex CLI、Hermes、ZCode 等）时，通常面临三大痛点：
1. **网络穿透与安全风险**：服务器分散在 NAT/内网或不同云厂商，中心端若直连 SSH 需要大量私钥或打洞，存在严重安全隐患。
2. **状态碎片化**：无法一览所有机器上的 Agent 进程、活跃会话、系统负载与健康状况。
3. **任务派发缺乏审计与沙箱**：远程控制容易变成“任意 shell 执行”，缺乏项目白名单、Lease 租约与脱敏审计机制。

**Agent Fleet** 为此而生：

```
┌─ 各计算节点 (Mac / VPS / Container) ──────────┐
│  • Probe (自上报探针): 采集 Agent 状态与系统指标  │
│  • Runner (主动拉取执行器): 受控执行白名单任务   │
│  • 出站 HTTPS 单向连接，无需公网入站与反向隧道  │
└──────────────────────┬──────────────────────┘
                       │ HTTPS POST /api/ingest (Push-only)
                       │ HTTPS POST /api/commands/poll (Pull-based)
                       ▼
┌─ Hub 控制中心 (Web API + 实时面板) ───────────┐
│  • 统一状态面板 (SSE 毫秒级推送 / 响应式 Web 控制台) │
│  • 双重字段脱敏白名单，敏感数据/Session 不出机    │
│  • 基于 Lease + Attempt 的幂等任务队列与安全审计  │
└─────────────────────────────────────────────┘
```

---

## 🌟 核心优势

- 🛡️ **绝对的 Push-Only 零信任安全**
  - 各节点通过本地 Probe 主动上报、Runner 主动拉取。
  - **Hub 绝不反向连接机器**：无需向中心暴露 SSH 私钥、无需公网入站端口、无需 FRP/内网穿透。
- 🔒 **严格的隐私与数据脱敏**
  - Probe 出站和 Hub API 双重白名单过滤，绝不上报代码上下文、完整会话正文、敏感环境变量或密钥。
- 🤖 **多 Agent 生态原生支持**
  - 开箱即用支持主流 Agent：**Claude Code**、**Codex CLI**、**ZCode**、**Hermes** 及通用进程。
  - 自动识别进程状态、活跃会话计数与系统资源开销。
- ⚡ **受控开发与任务编排（Command Plane）**
  - 基于项目白名单 + 租约（Lease）+ 自动超期回收机制，杜绝任意 Shell 越权。
  - 结果幂等缓存，断网重连自动恢复，有界 Diff 与日志快照回传。
- 📊 **轻量级、现代化的实时控制台**
  - 极简单文件 Hub 服务（Python 标准库 + 轻量 Flask），无笨重外部中间件依赖。
  - 内置优雅的深浅色响应式 UI 与 SSE 实时状态流。

---

## 🚀 快速上手 (Quick Start)

### 1. 启动 Hub（控制中心）

在一台有固定访问地址的机器或 VPS 上启动 Hub：

```bash
# 克隆仓库并安装依赖
git clone https://github.com/dengyie/agent-fleet.git
cd agent-fleet
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 设置 Ingest 通信 Token 并启动 Web 节点
export AGENT_FLEET_INGEST_TOKEN='your-secure-random-secret'
python hub/web.py --host 0.0.0.0 --port 8790
```

启动后访问 `http://<hub-ip>:8790` 即可看到实时舰队控制台。

---

### 2. 部署节点 Probe（状态采集探针）

在需要观测的任意开发机或云主机上：

```bash
# 1. 本地测试采集（dry-run 模式）
python3 tools/agent-self-report.py --dry-run --name worker-node-1

# 2. 正式向 Hub 上报状态
python3 tools/agent-self-report.py \
  --endpoint https://hub.example.com \
  --name worker-node-1 \
  --token "your-secure-random-secret"
```

> **推荐使用 Cron 定时上报**（每 2 分钟执行一次）：
> ```cron
> */2 * * * * python3 /path/to/agent-fleet/tools/agent-self-report.py --endpoint https://hub.example.com --name worker-node-1 --token "$AGENT_FLEET_INGEST_TOKEN" >/dev/null 2>&1
> ```

---

### 3. 配置 Runner（可选：受控任务调度）

如果需要通过 Web 界面向该节点派发受控 AI 开发任务：

1. 配置白名单：
   ```bash
   cp deploy/agent-runner.yaml.example ~/.config/agent-fleet/runner.yaml
   # 编辑 runner.yaml：配置允许调度的本地项目路径与 Agent 适配器
   ```
2. 启动 Runner 轮询：
   ```bash
   # 单次轮询测试
   python3 tools/agent-runner.py --once
   ```

---

## 📡 Web & API 概览

| 路径 / 接口 | 类型 | 功能说明 |
|---|---|---|
| `GET /` | Web UI | Fleet 舰队实时大盘（节点矩阵、系统状态、实时事件流） |
| `GET /machine/<name>` | Web UI | 单机详情页（指标趋势、运行中的 Agent 列表） |
| `GET /api/status` | REST API | 获取全局计算节点与 Agent 在线状态 |
| `GET /api/stream` | SSE | 毫秒级实时事件推流（自动重连与离线补发） |
| `POST /api/ingest` | REST API | 节点探针上报入口（需 `X-Agent-Fleet-Token` 认证） |
| `POST /api/tasks` | REST API | 派发受控开发任务（白名单校验 + 幂等队列） |

---

## 📖 进阶文档

- [系统架构详解 (Architecture v3)](docs/architecture-v3.md)
- [受控开发链路设计 (Control Plane v4)](docs/architecture-v4-control-plane.md)
- [前后端分离与发布规范 (Frontend Release Layout)](deploy/frontend-release-layout.md)
- [交接与运维手册 (Handoff Guide)](docs/HANDOFF.md)

---

## 📄 License

本项目基于 [MIT License](LICENSE) 开源。欢迎 Star、Issue 与 Pull Request！
