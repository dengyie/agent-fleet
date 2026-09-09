# Agent Profile Registry 设计文档

> 深度 review + 参考 cc-switch 的 provider 管理策略后,为 agent-fleet 提出的 agent 管理配置方案。
> 范围裁定:**实施 P0(agent profile 单一事实源)+ P1(session→agent_family 绑定可观测)**。
> **P2(hub 下发 profile)暂缓** —— 它触碰 fail-closed 安全模型(控制面绝不下发密钥),需单独安全设计。

## 背景与问题

agent 的「家族」知识(有哪些 agent 类型、默认怎么跑)散落在多个位置,且**不一致、会漂移**:

| 位置 | 内容 | 问题 |
|---|---|---|
| `hub/domain/task.py` | `DEFAULT_AGENT_TYPES = ("codex", "claude_code", "hermes")` | 3 类,无 generic |
| `tools/probe_collectors.py` | `DEFAULT_AGENT_TYPES = ("hermes", "claude_code", "codex", "generic")` | 4 类,顺序不同 |
| `report_schema.py` | `AGENT_FIELDS` 按 4 类 key 展开 | 允许列表,非清单 |
| `deploy/agent-runner.yaml.example` | 默认 argv(`codex exec --full-auto {instruction}` 等) | 只在样例里 |
| 每机 `~/.config/agent-fleet/runner.yaml` | 每机复制一份 argv 模板 | 手工对齐,可漂移 |

同时,**hub 的 `sessions` 表没有 agent 家族列**:supervisor manifest 已带 `agent` 字段,
session 事件 `payload.agent_family` 也已 allowlist,但 agent_family **从未落到 hub 会话行/DTO**,
hub 无法报告「这个会话跑在哪个 agent 上」。

## 设计:单一事实源 `agent_profiles.py`

仿照 `report_schema.py` / `session_schema.py` 的既有模式:纯 stdlib、repo 根目录、
hub 与 tools 双侧 import、无 Flask/CLI 依赖。

```python
# agent_profiles.py
DEFAULT_TIMEOUT_S = 1800

@dataclass(frozen=True)
class Profile:
    family: str                              # 规范家族 id(== adapter/connector 键)
    default_command: tuple[str, ...] | None  # argv 模板,{instruction} 占位
    default_timeout_s: int = DEFAULT_TIMEOUT_S
    executable: bool = True                  # 有 runner adapter(tools/adapters)
    observable: bool = True                  # probe/connector 认识它(report_schema)
    pattern: str | None = None               # generic 进程匹配(generic 专用)

PROFILES: dict[str, Profile] = {
    "codex":       Profile("codex", ("codex", "exec", "--full-auto", "{instruction}")),
    "claude_code": Profile("claude_code", ("claude", "-p", "{instruction}")),
    "hermes":      Profile("hermes", None),   # 无统一 CLI,节点必须自供
    "generic":     Profile("generic", None, executable=False,
                           pattern="claude|codex|astrbot|openclaw|opencode|aider"),
}

EXECUTABLE_AGENT_TYPES = tuple(k for k, p in PROFILES.items() if p.executable)   # codex, claude_code, hermes
OBSERVABLE_AGENT_TYPES = tuple(PROFILES)                                          # 4 类
```

### 消费者

1. **`hub/domain/task.py`** → `DEFAULT_AGENT_TYPES = EXECUTABLE_AGENT_TYPES`。
   值不变(仍 `("codex", "claude_code", "hermes")`),任务校验行为零变化。
2. **`tools/probe_collectors.py`** → `DEFAULT_AGENT_TYPES = OBSERVABLE_AGENT_TYPES`。
   顺序从 `hermes` 开头变为 `codex` 开头;语义无差(输出是 dict),测试按集合断言。
3. **`tools/runner_config.py`** → `load_config()` 后,**对 runner.yaml 中完全缺省的可执行家族
   回退到注册表默认**(command + timeout_s)。部分指定的家族保持原样(尊重显式 `command: null`)。
4. `report_schema.py` **不改**:它按类型 key 的 allowlist,不是清单;新增类型时才动它。

## P0 边界(明确不做)

- **不改** supervisor manifest(已有 `agent` 字段)。
- **不改** `hosts.yaml` 的 `agents[]` 清单 —— 它是展示性节点清单,不是权威;
  v4 ingest 模型下由 probe 动态发现 agent。
- **不引入** provider/endpoint/model/API-key 字段 —— 密钥继续由节点本地解析(`~/.claude`/`~/.codex`)。
- **不新增** 控制动作、不下发任何配置到节点(P2 范畴)。

## P1:session→agent_family 绑定

把事件里已 allowlist 的 `payload.agent_family` 从 producer 一路落到 hub 会话行与公开 DTO:

- **producer**(`tools/session/bridge.py` 或 session_start 发出点):确保
  `session_start` / `session_metadata` 事件带 `payload.agent_family`(取自 capability manifest 的 family)。
- **hub 存储** `hub/infrastructure/session_repository.py`:schema v1→v2,加 `agent_family TEXT` 列;
  老库 `ALTER TABLE` 迁移(列已存在则跳过);`_normalize_spec` 接受有界 `agent_family`;
  `_clean_row` 输出(缺省空串)。
- **hub 服务** `hub/application/session_service.py::_session_spec_from_event`:从事件 payload
  提取 `agent_family` 写入 spec。
- **DTO** `session_schema.py::public_session_dto` **已经**读 `agent_family`,repo 行一旦带上即自然浮现。

## 安全不变量(必须保持)

- fail-closed:控制面不存、不下发、不注入密钥(`env_allowlist={}` 不动)。
- manifest/session 行只持久化 allowlisted 字段;`agent_family` 是家族名,非秘密,可持久化。
- `agent_family` 值在 repo 边界做有界 sanitize(≤64、拒绝 path/secret 形状),沿用 `_public_id` 语义。
- 老 DB 迁移幂等:重复 `init()` 不报错、不丢行。

## 验收

1. 全量测试套件通过(pytest)且 `python3 -m py_compile` 通过。
2. hub 任务校验仍只接受 `codex|claude_code|hermes`(行为零回归)。
3. runner.yaml 省略 `agents` 段时,`cfg.agents` 含 codex/claude_code 默认 command,hermes 为 None。
4. session 事件带 `agent_family` 时,hub `get_session`/`list_sessions` 的公开 DTO 含 `agent_family`;
   老库无列时迁移后正常读写。
