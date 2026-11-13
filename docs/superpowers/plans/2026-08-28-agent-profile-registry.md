# Agent Profile Registry 实施计划

> **Status (2026-09-08): historical / implemented.** Open checkboxes below are the original plan and were not kept in sync after merge. Do not re-implement from unchecked boxes. Remaining work is recorded in `docs/HANDOFF.md` §六 as `done` or `deferred-with-condition`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 引入 fleet 级 agent profile 单一事实源(`agent_profiles.py`),消除三处 `DEFAULT_AGENT_TYPES` 漂移与每机 runner.yaml 的 argv 复制,并把 session 的 `agent_family` 从事件一路落到 hub 会话行与公开 DTO。

**Architecture:** 纯 stdlib 根模块 `agent_profiles.py` 作为唯一权威,`hub/` 与 `tools/` 双侧 import;runner 配置加载后对缺省家族回退到注册表默认;sessions 表加 `agent_family` 列(v1→v2 幂等迁移)。

**Tech Stack:** Python 3(纯 stdlib + 现有 yaml/sqlite3 依赖)。

**Spec:** `docs/superpowers/specs/2026-08-28-agent-profile-registry-design.md`

## Global Constraints

- **零行为回归**:`hub/domain/task.py` 的 `DEFAULT_AGENT_TYPES` 值必须仍为 `("codex", "claude_code", "hermes")`,任务校验错误信息不变。
- **fail-closed 不动**:不新增任何 provider/endpoint/model/API-key 字段;不改 `env_allowlist={}`;不下发配置到节点。
- **幂等迁移**:老 sessions 库重复 `init()` 不报错、不丢行。
- **有界 sanitize**:`agent_family` 在 repo 边界做 ≤64 且拒绝 path/secret 形状的处理,沿用 `_public_id` 语义。
- **测试命令**:`python3 -m pytest <path> -q`;提交前跑相关模块测试,最终由集成验证跑全量。
- **工作目录**:所有路径相对于工作树根 `/opt/agent-fleet/.claude/worktrees/agent-profile-registry/`。

---

### Task 1: `agent_profiles.py` 注册表模块

**Files:**
- Create: `agent_profiles.py`
- Test: `tests/test_agent_profiles.py`

**Interfaces:**
- Produces: `Profile`(frozen dataclass: `family`, `default_command`, `default_timeout_s`, `executable`, `observable`, `pattern`);`PROFILES: dict[str, Profile]`;`EXECUTABLE_AGENT_TYPES`(== `("codex", "claude_code", "hermes")`);`OBSERVABLE_AGENT_TYPES`(== `("codex", "claude_code", "hermes", "generic")`);`DEFAULT_TIMEOUT_S = 1800`;helper `default_command(agent_type) -> tuple[str, ...] | None` 与 `is_executable(agent_type) -> bool`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_agent_profiles.py
from agent_profiles import (
    DEFAULT_TIMEOUT_S, EXECUTABLE_AGENT_TYPES, OBSERVABLE_AGENT_TYPES,
    PROFILES, default_command, is_executable,
)


def test_executable_types_exactly_three():
    assert EXECUTABLE_AGENT_TYPES == ("codex", "claude_code", "hermes")


def test_observable_types_four_including_generic():
    assert OBSERVABLE_AGENT_TYPES == ("codex", "claude_code", "hermes", "generic")
    assert set(OBSERVABLE_AGENT_TYPES) == set(PROFILES)


def test_codex_default_command_has_instruction_placeholder():
    cmd = default_command("codex")
    assert cmd is not None and "{instruction}" in cmd


def test_hermes_has_no_default_command():
    assert default_command("hermes") is None


def test_generic_not_executable_but_observable():
    assert not is_executable("generic")
    assert PROFILES["generic"].pattern == "claude|codex|astrbot|openclaw|opencode|aider"


def test_default_timeout_applies():
    assert all(p.default_timeout_s == DEFAULT_TIMEOUT_S for p in PROFILES.values())
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_agent_profiles.py -q`
Expected: FAIL(ModuleNotFoundError: agent_profiles)

- [ ] **Step 3: 实现 `agent_profiles.py`**

```python
"""Fleet-level agent profile registry — single source of truth for agent families.

Mirrors report_schema.py / session_schema.py: stdlib-only, importable by both
hub/ and tools/ without Flask or CLI dependencies.  The runner falls back to
these defaults when a machine's runner.yaml omits a family; the hub and probe
derive their canonical agent-type lists from here.
"""
from dataclasses import dataclass

DEFAULT_TIMEOUT_S = 1800


@dataclass(frozen=True)
class Profile:
    family: str                              # canonical family id (adapter/connector key)
    default_command: tuple[str, ...] | None  # argv template with {instruction}
    default_timeout_s: int = DEFAULT_TIMEOUT_S
    executable: bool = True                  # has a runner adapter (tools/adapters)
    observable: bool = True                  # probe/connector knows it (report_schema)
    pattern: str | None = None               # generic process match (generic only)


PROFILES: dict[str, Profile] = {
    "codex": Profile("codex", ("codex", "exec", "--full-auto", "{instruction}")),
    "claude_code": Profile("claude_code", ("claude", "-p", "{instruction}")),
    "hermes": Profile("hermes", None),       # no unified CLI; node must supply
    "generic": Profile(
        "generic", None, executable=False,
        pattern="claude|codex|astrbot|openclaw|opencode|aider",
    ),
}

EXECUTABLE_AGENT_TYPES = tuple(k for k, p in PROFILES.items() if p.executable)
OBSERVABLE_AGENT_TYPES = tuple(PROFILES)


def is_executable(agent_type: str) -> bool:
    profile = PROFILES.get(agent_type)
    return bool(profile and profile.executable)


def default_command(agent_type: str) -> tuple[str, ...] | None:
    profile = PROFILES.get(agent_type)
    return profile.default_command if profile else None


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "EXECUTABLE_AGENT_TYPES",
    "OBSERVABLE_AGENT_TYPES",
    "PROFILES",
    "Profile",
    "default_command",
    "is_executable",
]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m pytest tests/test_agent_profiles.py -q`
Expected: PASS(6 passed)

- [ ] **Step 5: 提交**

```bash
git add agent_profiles.py tests/test_agent_profiles.py
git commit -m "feat: agent profile registry — fleet-level single source of truth"
```

---

### Task 2: 收敛三处 `DEFAULT_AGENT_TYPES` 到注册表

**Files:**
- Modify: `hub/domain/task.py:16`(DEFAULT_AGENT_TYPES 派生)
- Modify: `tools/probe_collectors.py:14`(DEFAULT_AGENT_TYPES 派生)
- Test: `tests/test_agent_profiles.py`(追加派生一致性测试)+ 现有 `tests/test_task_*.py` 回归

**Interfaces:**
- Consumes: `EXECUTABLE_AGENT_TYPES`, `OBSERVABLE_AGENT_TYPES` from `agent_profiles`(Task 1)。
- Produces: `hub/domain/task.DEFAULT_AGENT_TYPES` 仍为 `("codex", "claude_code", "hermes")`;`tools/probe_collectors.DEFAULT_AGENT_TYPES` 为 4 类(顺序可能变为 codex 开头,语义不变)。

- [ ] **Step 1: 写失败测试(派生一致性)**

在 `tests/test_agent_profiles.py` 追加:

```python
from hub.domain.task import DEFAULT_AGENT_TYPES as HUB_TYPES
from tools.probe_collectors import DEFAULT_AGENT_TYPES as PROBE_TYPES


def test_hub_default_agent_types_derived_from_registry():
    assert HUB_TYPES == EXECUTABLE_AGENT_TYPES == ("codex", "claude_code", "hermes")


def test_probe_default_agent_types_derived_from_registry():
    assert set(PROBE_TYPES) == set(OBSERVABLE_AGENT_TYPES)
    assert len(PROBE_TYPES) == 4
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_agent_profiles.py -q`
Expected: FAIL(probe 的 `DEFAULT_AGENT_TYPES` 是字面量 `("hermes", ...)`,与 `OBSERVABLE_AGENT_TYPES` 集合相等但顺序不同 —— 第二步 `set()` 断言会过,但 hub 派生断言需改为 import 后仍成立;若实现前已字面量相等,该步以「hub 尚为字面量、未 import」为依据,确认第一行 `test_hub_default_agent_types_derived_from_registry` 因 hub 未 import 而语义上未达成;实现后全过)

- [ ] **Step 3: 实现派生**

`hub/domain/task.py` 顶部 import 处加:

```python
from agent_profiles import EXECUTABLE_AGENT_TYPES
```

并将 `DEFAULT_AGENT_TYPES = ("codex", "claude_code", "hermes")` 改为:

```python
DEFAULT_AGENT_TYPES = EXECUTABLE_AGENT_TYPES
```

`tools/probe_collectors.py` 顶部加:

```python
from agent_profiles import OBSERVABLE_AGENT_TYPES
```

并将 `DEFAULT_AGENT_TYPES = ("hermes", "claude_code", "codex", "generic")` 改为:

```python
DEFAULT_AGENT_TYPES = OBSERVABLE_AGENT_TYPES
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m pytest tests/test_agent_profiles.py tests/test_task_api.py tests/test_task_domain.py -q`
Expected: PASS;若某 task 测试按顺序断言 probe 输出,改为按集合断言(在报告中说明)。

- [ ] **Step 5: 提交**

```bash
git add hub/domain/task.py tools/probe_collectors.py tests/test_agent_profiles.py
git commit -m "feat: derive DEFAULT_AGENT_TYPES from profile registry"
```

---

### Task 3: runner_config 回退到注册表默认

**Files:**
- Modify: `tools/runner_config.py:97-112`(`load_config` 的 agents 解析后追加回退)
- Modify: `deploy/agent-runner.yaml.example:12-21`(标注 agents 段可选 + 回退说明)
- Test: `tests/test_runner_config.py`(不存在则新建;或并入现有 `tests/test_config.py` 旁新建)

**Interfaces:**
- Consumes: `PROFILES`, `is_executable`, `default_command` from `agent_profiles`(Task 1)。
- Produces: `RunnerConfig.agents` 对 runner.yaml 完全缺省的可执行家族,回退为注册表默认 `{"command": [...] 或 None, "timeout_s": 1800}`;部分指定的家族保持原样。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_runner_config.py
import tempfile
from pathlib import Path

from tools.runner_config import load_config


def _write(tmp: Path, body: str) -> Path:
    cred = tmp / "runner-credential"
    cred.write_text("secret-token")
    p = tmp / "runner.yaml"
    p.write_text(body)
    return p


def test_missing_agents_section_falls_back_to_registry(tmp_path):
    p = _write(tmp_path, """
hub: https://example.com
machine: m1
credential_file: runner-credential
projects:
  p1: {path: "%s"}
""" % tmp_path)
    cfg = load_config(p)
    assert cfg.agents["codex"]["command"] == ["codex", "exec", "--full-auto", "{instruction}"]
    assert cfg.agents["claude_code"]["command"] == ["claude", "-p", "{instruction}"]
    assert cfg.agents["hermes"]["command"] is None
    assert cfg.agents["codex"]["timeout_s"] == 1800


def test_explicit_override_wins(tmp_path):
    p = _write(tmp_path, """
hub: https://example.com
machine: m1
credential_file: runner-credential
projects:
  p1: {path: "%s"}
agents:
  codex:
    command: ["/opt/bin/codex", "run", "{instruction}"]
    timeout_s: 3600
""" % tmp_path)
    cfg = load_config(p)
    assert cfg.agents["codex"]["command"] == ["/opt/bin/codex", "run", "{instruction}"]
    assert cfg.agents["codex"]["timeout_s"] == 3600


def test_explicit_null_command_not_overridden(tmp_path):
    p = _write(tmp_path, """
hub: https://example.com
machine: m1
credential_file: runner-credential
projects:
  p1: {path: "%s"}
agents:
  hermes:
    command: null
""" % tmp_path)
    cfg = load_config(p)
    assert cfg.agents["hermes"]["command"] is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_runner_config.py -q`
Expected: FAIL(`cfg.agents["codex"]` 是 KeyError —— 缺省家族未回退)

- [ ] **Step 3: 实现回退**

在 `tools/runner_config.py` 顶部 import `from agent_profiles import PROFILES`;在 `load_config` 构建完 `agents` dict 之后、`return RunnerConfig(...)` 之前插入:

```python
    # 注册表默认回退:runner.yaml 完全缺省的可执行家族用内置默认值补齐。
    # 部分指定(含显式 command: null)的家族保持原样,尊重操作者意图。
    for family, profile in PROFILES.items():
        if not profile.executable or family in agents:
            continue
        agents[family] = {
            "command": (list(profile.default_command)
                        if profile.default_command else None),
            "timeout_s": profile.default_timeout_s,
        }
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m pytest tests/test_runner_config.py -q`
Expected: PASS(3 passed)

- [ ] **Step 5: 更新样例并提交**

`deploy/agent-runner.yaml.example` 的 `agents:` 段上方加注释:

```yaml
# agents 段可省略:未列出的可执行家族(codex/claude_code)自动回退到
# agent_profiles.py 的内置默认 command。hermes 无统一 CLI,必须按本机自供。
```

```bash
git add tools/runner_config.py deploy/agent-runner.yaml.example tests/test_runner_config.py
git commit -m "feat: runner config falls back to registry defaults for omitted agents"
```

---

### Task 4: session→agent_family 绑定(hub 存储 + 服务 + producer)

**Files:**
- Modify: `hub/infrastructure/session_repository.py`(SCHEMA_VERSION 1→2、schema、`_normalize_spec`、`_clean_row`)
- Modify: `hub/application/session_service.py:184-205`(`_session_spec_from_event`)
- Modify: `tools/session/bridge.py` 或 session_start/session_metadata 发出点(确保 `payload.agent_family` 存在;若已存在则只补测试固定它)
- Test: `tests/test_session_repository.py` / `tests/test_session_service.py`(不存在则新建/就近并入)

**Interfaces:**
- Consumes: `session_schema.public_session_dto`(已读 `agent_family`,不改);capability manifest 的 family(Task 2 的 probe 产物)。
- Produces: `SessionRepository._normalize_spec` 接受可选有界 `agent_family`;`_clean_row` 输出 `agent_family`(缺省 `""`);`_session_spec_from_event` 从 `event["payload"]["agent_family"]` 提取。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_session_repository.py 追加
def test_agent_family_roundtrips(tmp_path):
    repo = SessionRepository(tmp_path / "s.db")
    repo.init()
    repo.upsert_session({
        "session_id": "s1", "machine_id": "m1", "managed": True,
        "process_group_id": "grp_1", "agent_family": "codex",
    })
    row = repo.get_session("s1")
    assert row["agent_family"] == "codex"


def test_agent_family_defaults_empty_when_absent(tmp_path):
    repo = SessionRepository(tmp_path / "s.db")
    repo.init()
    repo.upsert_session({"session_id": "s2", "machine_id": "m1"})
    row = repo.get_session("s2")
    assert row["agent_family"] == ""


def test_legacy_db_migrates_without_losing_rows(tmp_path):
    db = tmp_path / "s.db"
    repo = SessionRepository(db)
    repo.init()
    repo.upsert_session({"session_id": "old", "machine_id": "m1"})
    # 模拟老库:直接删列后重建一个 v1 库,再 init 触发迁移
    import sqlite3
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("ALTER TABLE sessions DROP COLUMN agent_family")
    except sqlite3.OperationalError:
        pass
    conn.close()
    repo2 = SessionRepository(db)
    repo2.init()  # 应幂等,不抛
    assert repo2.get_session("old")["session_id"] == "old"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_session_repository.py -q`
Expected: FAIL(`row["agent_family"]` KeyError —— 列不存在)

- [ ] **Step 3: 实现存储迁移**

`session_repository.py`:

1. `SCHEMA_VERSION = 1` → `2`。
2. `init()` 的 CREATE TABLE `sessions(...)` 加一列 `agent_family TEXT`(放在 `attempt_id TEXT` 后),并 `CREATE TABLE IF NOT EXISTS` 之后加幂等迁移:

```python
            self._migrate_v2(conn)
```

3. 新增 `_migrate_v2(conn)` 方法(在 `init` 之后定义):

```python
    def _migrate_v2(self, conn) -> None:
        """Idempotent v1->v2: add the ``agent_family`` column."""
        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN agent_family TEXT")
        except sqlite3.OperationalError:
            pass  # column already present (fresh v2 schema or re-run)
```

4. `_normalize_spec`:在 `clean` dict 中,若 `spec.get("agent_family")` 为 str 且非空,则截断到 64 并写入 `clean["agent_family"]`(非 str 忽略,不报错)。
5. `_clean_row`:返回 dict 加 `"agent_family": str(row.get("agent_family") or "")[:64]`。
6. `upsert_session` 的 INSERT 列与 ON CONFLICT 更新语句加入 `agent_family`。

- [ ] **Step 4: 实现服务提取**

`session_service.py::_session_spec_from_event` 在 `spec` 构建后加:

```python
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        family = payload.get("agent_family") or event.get("agent_family")
        if isinstance(family, str) and family:
            spec["agent_family"] = family[:64]
```

(顶部已 import `Mapping` from `collections.abc`。)

- [ ] **Step 5: 检查/固定 producer**

读 `tools/session/bridge.py`(及 session_start 事件发出点):
- 若 `session_start` 的 payload 已带 `agent_family`,补一条测试固定它(断言 payload.agent_family 是已知家族之一)。
- 若缺,从 capability manifest 的 family 字段填充 `payload.agent_family`,再补测试。
- 在报告中说明「已存在」或「已新增」及确切改动行。

- [ ] **Step 6: 运行测试确认通过**

Run: `python3 -m pytest tests/test_session_repository.py tests/test_session_service.py -q`
Expected: PASS;同时 `python3 -m pytest tests/test_session_schema.py -q` 通过(public_session_dto 无回归)。

- [ ] **Step 7: 提交**

```bash
git add hub/infrastructure/session_repository.py hub/application/session_service.py \
        tools/session/bridge.py tests/test_session_repository.py tests/test_session_service.py
git commit -m "feat: bind session rows to agent_family (schema v2)"
```

---

## Self-Review

**Spec coverage:** P0 三消费方(Task 1/2/3)+ P1 全链路(Task 4)全覆盖;P2/热切换/provider 密钥明确 out of scope 并在 spec 记录。

**Placeholder scan:** 无 TBD/TODO;每步含实际代码与断言。

**Type consistency:** `EXECUTABLE_AGENT_TYPES`/`OBSERVABLE_AGENT_TYPES`/`default_command` 在 Task 1 定义,Task 2/3 按同名引用;`agent_family` 字段名在 repo/service/DTO 三处一致(沿用既有 `public_session_dto` 的 `agent_family` 键)。
