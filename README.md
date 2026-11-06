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
│  • 会话归档入 Hub，密钥/凭据在节点侧先脱敏        │
│  • 基于 Lease + Attempt 的幂等任务队列与安全审计  │
└─────────────────────────────────────────────┘
```

完整的可视化架构、状态机、身份绑定、调度生命周期和发布验收图见
[可视化架构](docs/visual-architecture.md)。最新浏览器截图覆盖见
[UI 视觉证据](docs/testing/visual-evidence.md)。

---

## 🌟 核心优势

- 🛡️ **绝对的 Push-Only 零信任安全**
  - 各节点通过本地 Probe 主动上报、Runner 主动拉取。
  - **Hub 绝不反向连接机器**：无需向中心暴露 SSH 私钥、无需公网入站端口、无需 FRP/内网穿透。
- 🔒 **严格的隐私与数据脱敏**
  - Probe 出站和 Hub API 双重白名单过滤；会话转录（含完整原始记录）会归档到 Hub，但密钥/凭据形态（`sk-`、`ghp_`、AWS 密钥、Bearer token、`*_API_KEY=` 等）在节点侧上传前即被脱敏，Hub 落库前再兜底一次；环境变量与密钥本身从不上传。
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

## 测试与发布验收

完整测试入口：[全流程测试与质量门禁](docs/testing/README.md)。[可执行流程矩阵](docs/testing/journeys.json)绑定必需用例；[测试链路与证据核验](docs/testing/test-chains.md)提供阶段断言、审查回归和可复制命令。CI 在打包前检查实际执行结果，缺失、跳过或失败均阻断。真实模型、邮件和节点按[发布验收手册](docs/testing/release-acceptance.md)分别验证。

main 在测试、前后端打包通过后进入[自动部署](deploy/auto-deploy.md)，使用 production 环境的受限 SSH 入口，保留运行数据与配置，探活失败时回滚代码和前端。PR 不部署，也不读取部署凭据。

## 最新更新

### 2026-11-07

- 客户端重启后重新校验并恢复纳管会话、控制连接和增量采集；升级完成回执等待恢复结束。
- 修复短进程 probe 成功后被误回滚、升级中断恢复错位，以及长任务使用中的旧版本被清理。
- 助手运行记录完整读取分页，完成或重新打开长运行后保留末尾工具结果和结束事件。

### 2026-11-04

- Claude Code 新会话会按自身的明确会话标识关联原生记录，不会再误绑定其他工作区的历史会话；身份缺失、冲突或重复时保持未关联状态。
- 新增系统拓扑、数据/控制平面、任务状态机、身份解析、调度生命周期和发布验收六组可视化图，并固化 19 张桌面/移动端 UI 证据图的覆盖索引与 contact sheet。

### 2026-11-05

- 修复 1280×720 桌面高度下侧栏可调列表越过页脚的布局缺陷；浏览器回归现在验证列表容器在页脚上方独立滚动。
- 将 19 张 CI 原始 PNG 固化到仓库并逐字节核验，视觉证据页提供每张全分辨率图片的直接链接；明确 `full-flow-recovered` 是 HTTP 502 的待确认结果夹具。
- 校正六组架构图并导出可缩放 SVG 与高清 PNG，逐张检查布局；任务状态、调度心跳/释放和发布依赖均对齐当前实现。

完整历史见[发布说明](docs/releases/release-notes.md)。

## Muse 工作区

侧栏 **Muse**（`/muse`）可导入 Muse 登录态，直接查看和使用原生会话，不需要运行浏览器或 9222。支持最多 6 个对话窗口、原生子会话、引用回复、停止生成、按会话开启自动审批，以及浏览器任务、截图、文件和工具卡片。登录态按 Fleet 用户加密保存。使用步骤、限制与实测记录见 [Muse 工作区](docs/platform/muse-workspace.md)。

此前的 [本地浏览器桥接](docs/platform/muse-browser-bridge.md) 作为可选的 Assistant 执行节点保留；使用 Muse 栏目不需要配置该桥接。

## 📖 进阶文档

- [系统架构详解 (Architecture v3)](docs/architecture-v3.md)
- [受控开发链路设计 (Control Plane v4)](docs/architecture-v4-control-plane.md)
- [可视化架构与状态机 (Visual Architecture)](docs/visual-architecture.md)
- [UI 视觉证据 (Visual Evidence)](docs/testing/visual-evidence.md)
- [前后端分离与发布规范 (Frontend Release Layout)](deploy/frontend-release-layout.md)
- [交接与运维手册 (Handoff Guide)](docs/HANDOFF.md)

---

## 📄 License

本项目基于 [MIT License](LICENSE) 开源。欢迎 Star、Issue 与 Pull Request！

### 账号体系（通过配置启用）

支持邮箱验证码注册、邮箱密码登录、密码找回、个人资料、会话撤销及管理员账号管理。配置接口、权限边界和现有数据归属限制见 [账号管理说明](docs/account-management.md)。
