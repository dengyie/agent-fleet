# Task 3 brief: runner_config 回退到注册表默认

这是你的唯一需求来源。按 Step 1-5 顺序执行(TDD)。本任务让 runner 在 runner.yaml **完全缺省**某可执行家族时,回退到 `agent_profiles.py` 的内置默认 command/timeout,消除每机 argv 复制。

**工作目录**:`/opt/agent-fleet/.claude/worktrees/agent-profile-registry/`(先 `cd` 过去)

**背景**:Task 1 已提交 `agent_profiles.py`,导出 `PROFILES`(每项 `Profile` 有 `default_command`、`default_timeout_s`、`executable` 等)。`tools/runner_config.py` 的 `load_config` 现在只读 runner.yaml,缺省家族会 KeyError。

**全局约束**:零行为回归 —— 部分指定(含显式 `command: null`)的家族保持原样,绝不覆盖操作者意图;不新增任何 provider 字段。

## Step 1: 写失败测试

新建 `tests/test_runner_config.py`:

```python
from pathlib import Path

from tools.runner_config import load_config


def _write(tmp: Path, agents_block: str = "") -> Path:
    cred = tmp / "runner-credential"
    cred.write_text("secret-token")
    body = (
        "hub: https://example.com\n"
        "machine: m1\n"
        f"credential_file: {cred}\n"
        "projects:\n"
        f'  p1:\n    path: "{tmp}"\n'
        + agents_block
    )
    p = tmp / "runner.yaml"
    p.write_text(body)
    return p


def test_missing_agents_falls_back_to_registry(tmp_path):
    cfg = load_config(_write(tmp_path))
    assert cfg.agents["codex"]["command"] == ["codex", "exec", "--full-auto", "{instruction}"]
    assert cfg.agents["claude_code"]["command"] == ["claude", "-p", "{instruction}"]
    assert cfg.agents["hermes"]["command"] is None
    assert cfg.agents["codex"]["timeout_s"] == 1800


def test_explicit_override_wins(tmp_path):
    p = _write(tmp_path, (
        "agents:\n"
        "  codex:\n"
        '    command: ["/opt/bin/codex", "run", "{instruction}"]\n'
        "    timeout_s: 3600\n"
    ))
    cfg = load_config(p)
    assert cfg.agents["codex"]["command"] == ["/opt/bin/codex", "run", "{instruction}"]
    assert cfg.agents["codex"]["timeout_s"] == 3600


def test_explicit_null_command_not_overridden(tmp_path):
    p = _write(tmp_path, "agents:\n  hermes:\n    command: null\n")
    cfg = load_config(p)
    assert cfg.agents["hermes"]["command"] is None
```

## Step 2: 运行测试确认失败

Run: `python3 -m pytest tests/test_runner_config.py -q`
Expected: FAIL(`test_missing_agents_falls_back_to_registry` 会 `KeyError: 'codex'` —— 缺省家族未回退;另两个 also 因 hermes/codex 部分缺省而可能失败,但至少第一个明确失败)

## Step 3: 实现回退

`tools/runner_config.py`:

1. 顶部 import 区加:

```python
from agent_profiles import PROFILES
```

2. 在 `load_config` 里 agents 解析循环(现有 lines 97-112 那段 `for agent_type, entry in raw_agents.items()` 构建完 `agents` dict)之后、`return RunnerConfig(...)` 之前,插入:

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

## Step 4: 运行测试确认通过

Run: `python3 -m pytest tests/test_runner_config.py -q`
Expected: PASS(3 passed)

## Step 5: 更新样例并提交

`deploy/agent-runner.yaml.example` 的 `agents:` 段上方加注释:

```yaml
# agents 段可省略:未列出的可执行家族(codex/claude_code)自动回退到
# agent_profiles.py 的内置默认 command。hermes 无统一 CLI,必须按本机自供。
```

```bash
git add tools/runner_config.py deploy/agent-runner.yaml.example tests/test_runner_config.py
git commit -m "feat: runner config falls back to registry defaults for omitted agents"
```

**完成报告**:写 status、提交哈希、一行测试摘要、疑虑到
`/opt/agent-fleet/.claude/worktrees/agent-profile-registry/.superpowers/sdd/2026-08-28-agent-profile-registry/task-3-report.md`
返回消息只给这些字段。不得派发子 agent。