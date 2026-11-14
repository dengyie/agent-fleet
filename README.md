<div align="center">

# AgentFleet

**Lightweight, Push-Only AI Agent Fleet Telemetry & Orchestration Control Plane**

[![License](https://img.shields.io/badge/license-MIT-green.svg?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10+-3776AB.svg?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-3.0+-000000.svg?style=flat-square&logo=flask&logoColor=white)](https://flask.palletsprojects.com/)
[![Architecture](https://img.shields.io/badge/Architecture-Push--Only%20%7C%20Zero--Trust-orange.svg?style=flat-square)](#core-architecture--highlights)
[![Tests](https://img.shields.io/badge/tests-passing-brightgreen.svg?style=flat-square)]()

[English](./README.md) · [简体中文](./README.zh-CN.md) · [Official Plugins](./docs/platform/official-plugins.md) · [Release Notes](./docs/releases/release-notes.md)

</div>

---

## 最新更新

2026-11-14：修复新聊天与后台提交的路由串用；发送后定位到新消息并收起空输入框；手机模型报错保留对话视图；设置账号切换隔离旧表单和晚到的保存响应。完整历史见 [Release Notes](docs/releases/release-notes.md)。

## Why Agent Fleet?

When running autonomous AI coding agents (such as Claude Code, Codex CLI, Hermes, and ZCode) across heterogeneous development workstations, edge nodes, and cloud servers, engineers face three major challenges:
1. **Network Ingress & Attack Surface**: Nodes operate behind NATs and firewalls. Direct SSH access from a central plane requires extensive key distribution and open inbound ports, introducing critical security vectors.
2. **Fragmented Runtime Visibility**: No centralized observability into running agent processes, active sessions, system load, or heartbeat telemetry across nodes.
3. **Unsanitized Task Dispatch**: Remote command execution often degrades into arbitrary shell invocation lacking directory allow-lists, lease tracking, or secret redaction.

**Agent Fleet** addresses these challenges through a strict, push-only zero-trust design:

```
┌─ Workstation / VPS / Container Node ────────┐
│  • Probe (Self-Reporting): Agent & System Telemetry │
│  • Runner (Pull-Based Worker): Sandboxed Task Runner│
│  • Outbound HTTPS only — Zero Inbound / Zero Tunnel │
└──────────────────────┬──────────────────────┘
                       │ HTTPS POST /api/ingest (Push-only)
                       │ HTTPS POST /api/commands/poll (Pull-based)
                       ▼
┌─ Hub Control Plane (Web API & Real-Time UI) ────────┐
│  • Unified Dashboard (Sub-Second SSE Push / Web UI)  │
│  • Session Ingestion with Pre-Flight Secret Scrubbing│
│  • Lease-Fenced Idempotent Task Queue & Audit Engine │
└─────────────────────────────────────────────────────┘
```

For complete visual architecture diagrams, state machines, and dispatch lifecycles, see [Visual Architecture](docs/visual-architecture.md) and [UI Visual Evidence](docs/testing/visual-evidence.md).

---

## Core Architecture & Highlights

- **Strict Push-Only Zero-Trust Model**
  - Worker nodes communicate exclusively via outbound requests: Probes push telemetry and Runners pull assigned work.
  - **Zero Central-to-Node Ingress**: No SSH keys on the Hub, no listening inbound ports on nodes, and no reverse tunnels.
- **Pre-Flight Privacy & Credential Scrubbing**
  - Dual allow-lists on node Probes and Hub APIs. Session transcripts redact sensitive keys (`sk-`, `ghp_`, AWS credentials, Bearer tokens, `*_API_KEY=`) locally on the node before egress, with a secondary guard at Hub persistence.
- **Native Multi-Agent Ecosystem**
  - First-class support for **Claude Code**, **Codex CLI**, **ZCode**, **Hermes**, and generic autonomous processes.
  - Automatic detection of lifecycle state, active session concurrency, and hardware resource utilization.
- **Sandboxed Task Orchestration (Command Plane)**
  - Guarded by project directory allow-lists, time-bounded leases, and automatic lease recovery to eliminate shell escape risks.
  - Idempotent execution caching, network reconnection self-healing, bounded diffs, and log snapshot returns.
- **Lightweight, Single-Binary Control Plane**
  - Minimalist Hub architecture (Python 3.10+ and lightweight Flask) with zero external message broker dependencies.
  - Built-in responsive dashboard featuring dark/light modes and real-time Server-Sent Events (SSE).

---

## Quick Start

### 1. Launch the Hub

Deploy the centralized Hub server on a workstation or cloud instance:

```bash
# Clone repository and prepare virtual environment
git clone https://github.com/dengyie/agent-fleet.git
cd agent-fleet
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Export Ingest authentication token and start Hub
export AGENT_FLEET_INGEST_TOKEN='your-secure-random-secret'
python hub/web.py --host 0.0.0.0 --port 8790
```

Access the dashboard at `http://<hub-ip>:8790`.

---

### 2. Deploy Node Probe (Telemetry Collector)

On any machine running AI coding agents:

```bash
# 1. Test telemetry collection in dry-run mode
python3 tools/agent-self-report.py --dry-run --name worker-node-1

# 2. Report state to the Hub
python3 tools/agent-self-report.py \
  --endpoint https://hub.example.com \
  --name worker-node-1 \
  --token "your-secure-random-secret"
```

> **Recommended Cron Setup** (execute every 2 minutes):
> ```cron
> */2 * * * * python3 /path/to/agent-fleet/tools/agent-self-report.py --endpoint https://hub.example.com --name worker-node-1 --token "$AGENT_FLEET_INGEST_TOKEN" >/dev/null 2>&1
> ```

---

### 3. Configure Runner (Optional: Sandboxed Task Dispatch)

To execute controlled AI workflows dispatched from the Web UI:

1. Configure allow-list:
   ```bash
   cp deploy/agent-runner.yaml.example ~/.config/agent-fleet/runner.yaml
   # Edit runner.yaml: Define allowed project paths and agent adapter configurations
   ```
2. Start Runner polling:
   ```bash
   # Single polling run
   python3 tools/agent-runner.py --once
   ```

---

## Web & API Reference

| Endpoint | Type | Description |
|---|---|---|
| `GET /` | Web UI | Live fleet overview (machine matrix, system health, real-time event stream) |
| `GET /machine/<name>` | Web UI | Machine inspection view (metrics telemetry, running agents) |
| `GET /api/status` | REST API | Query overall fleet and agent availability |
| `GET /api/stream` | SSE | Low-latency event streaming (auto-reconnect with replay) |
| `POST /api/ingest` | REST API | Node telemetry ingest endpoint (requires `X-Agent-Fleet-Token`) |
| `POST /api/tasks` | REST API | Dispatch sandboxed tasks (allow-list validated with idempotent queue) |

---

## Testing & Quality Assurance

Comprehensive testing and acceptance suites are documented in [Testing & Quality Gates](docs/testing/README.md). Executable journeys are defined in [journeys.json](docs/testing/journeys.json), with assertions and reproducible commands in [Test Chains](docs/testing/test-chains.md).

For release criteria, automated deployment gates, and rollback policies, refer to [Release Acceptance](docs/testing/release-acceptance.md) and [Automated Deployment](deploy/auto-deploy.md).

---

## Muse Workspace

The **Muse** workspace (`/muse`) connects native Muse sessions directly without local browser automation overhead. It supports multi-dialog tabs, sub-sessions, quote replies, automated approval toggles, and tool execution cards. Detailed operational parameters and benchmarks are available in [Muse Workspace](docs/platform/muse-workspace.md).

---

## Advanced Documentation

- [Architecture Specification v3](docs/architecture-v3.md)
- [Control Plane Specification v4](docs/architecture-v4-control-plane.md)
- [Visual Architecture & State Machine](docs/visual-architecture.md)
- [UI Visual Evidence](docs/testing/visual-evidence.md)
- [Frontend Release Layout](deploy/frontend-release-layout.md)
- [VM Workspace Execution Node](docs/platform/workspace-node.md)
- [Handoff & Operations Guide](docs/HANDOFF.md)

---

## License

AgentFleet is released under the [MIT License](LICENSE).
