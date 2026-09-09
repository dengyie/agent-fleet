# Task 1 brief: agent_profiles.py 注册表模块

这是你的唯一需求来源。按 Step 1-5 顺序执行(TDD:写失败测试 → 确认失败 → 实现 → 确认通过 → 提交)。

**工作目录**:`/opt/agent-fleet/.claude/worktrees/agent-profile-registry/`(所有相对路径以此为根;先 `cd` 过去)

**全局约束**:
- 零行为回归;fail-closed 不动;不新增 provider/endpoint/model/API-key 字段。
- 测试命令:`python3 -m pytest <path> -q`。

## Step 1: 写失败测试

新建 `tests/test_agent_profiles.py`:

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

## Step 2: 运行测试确认失败

Run: `python3 -m pytest tests/test_agent_profiles.py -q`
Expected: FAIL(ModuleNotFoundError: agent_profiles)

## Step 3: 实现 `agent_profiles.py`

新建 `agent_profiles.py`(repo 根,纯 stdlib,勿 import Flask/CLI 包):

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

## Step 4: 运行测试确认通过

Run: `python3 -m pytest tests/test_agent_profiles.py -q`
Expected: PASS(6 passed)

## Step 5: 提交

```bash
git add agent_profiles.py tests/test_agent_profiles.py
git commit -m "feat: agent profile registry — fleet-level single source of truth"
```

**完成报告**:把 status、提交哈希、一行测试摘要、疑虑写入
`/opt/agent-fleet/.claude/worktrees/agent-profile-registry/.superpowers/sdd/2026-08-28-agent-profile-registry/task-1-report.md`,
返回消息只给 status、提交哈希、一行测试摘要、疑虑。不得派发子 agent。
