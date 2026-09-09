# agent-fleet 架构 v3 — push-only agent fleet（2026-08-17）

## 目标

让中央 hub 只承担接收、存储、告警和展示；所有 agent 状态由机器本地 probe 主动上报。这样 Mac、VPS、容器都只需要出站 HTTPS，不需要中央持有机器级凭据。

## 数据流

```
agent machine
  └─ local probe: system + hermes + claude_code + codex + generic
       └─ HTTPS POST /api/ingest + X-Agent-Fleet-Token
            └─ HK hub: validate -> diff -> atomic state -> events -> web
```

## 组件职责

- `tools/probe_collectors.py`：本地采集器注册和统一 payload。
- `tools/agent-self-report.py`：定时执行采集、dry-run、HTTPS 上报。
- `hub/web.py`：认证 ingest、状态 API、面板、reconciliation API。
- `hub/scan.py`：只做 stale reconciliation；没有远程执行能力。
- `hub/state.py`：JSONL 历史和原子 current 快照。
- `connectors/`：被本地 probe 复用的 agent 适配器。

## Transport

`hosts.yaml` 中所有节点使用 `transport: ingest`。节点不需要注册才能上墙；登记只影响展示顺序、描述和 `stale_after_s`。

## 认证

hub token 来源：

1. `--ingest-token`
2. `AGENT_FLEET_INGEST_TOKEN`
3. `credentials/ingest-token`

生产启动没有 token 会失败。agent 每次请求必须发送 `X-Agent-Fleet-Token`。

## Stale 处理

每次 ingest 写入 `_ts`。reconciliation 只读取 `state/*.json`，超过默认 300 秒没有新报告就生成 offline 状态；不会触发任何机器命令。

## 控制方向

push-only 当前只做观测。中心控制需要未来增加带认证、可审计、带回执的 agent-side command queue；在此之前禁止通过 hub 执行任意命令。

## 部署

HK hub：

```bash
export AGENT_FLEET_INGEST_TOKEN='<secret>'
python3 hub/web.py --host 0.0.0.0 --port 8790
```

每台机器：

```bash
python3 tools/agent-self-report.py --endpoint https://hub.example.com --name <machine> --token '<secret>'
```

部署状态：hub 跑 push-only release；各节点本地 probe 出站 HTTPS 上报。其余节点各自安装 probe，不由 hub 反向连接。生产机名与域名不写进本文件。

## 验证

```bash
python3 tools/agent-self-report.py --dry-run --name <machine>
.venv/bin/python -m unittest discover -s tests -v
```
