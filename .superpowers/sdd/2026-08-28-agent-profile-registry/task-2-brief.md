# Task 2 brief: 收敛三处 DEFAULT_AGENT_TYPES 到注册表

这是你的唯一需求来源。按 Step 1-5 顺序执行(TDD)。本任务把 `hub/domain/task.py` 与 `tools/probe_collectors.py` 各自的字面量 `DEFAULT_AGENT_TYPES` 改为从 Task 1 的 `agent_profiles` 注册表派生,消除漂移。

**工作目录**:`/opt/agent-fleet/.claude/worktrees/agent-profile-registry/`(先 `cd` 过去)

**背景**:Task 1 已提交 `agent_profiles.py`(根目录,纯 stdlib),导出 `EXECUTABLE_AGENT_TYPES == ("codex","claude_code","hermes")` 与 `OBSERVABLE_AGENT_TYPES == ("codex","claude_code","hermes","generic")`。repo 根在 sys.path 上(既有 `from report_schema import ...` 同模式),所以 `from agent_profiles import ...` 可正常 import。

**全局约束**:零行为回归 —— hub 任务校验错误信息不变(仍接受 codex|claude_code|hermes);fail-closed 不动;不新增任何 provider 字段。

## Step 1: 写失败测试(派生一致性,用「身份断言」确保真正派生)

在 `tests/test_agent_profiles.py` **末尾追加**:

```python
from hub.domain.task import DEFAULT_AGENT_TYPES as HUB_TYPES
from tools.probe_collectors import DEFAULT_AGENT_TYPES as PROBE_TYPES


def test_hub_default_agent_types_derived_from_registry():
    # 身份断言:必须与注册表同一个 tuple 对象(派生),而非字面量副本
    assert HUB_TYPES is EXECUTABLE_AGENT_TYPES
    assert HUB_TYPES == ("codex", "claude_code", "hermes")


def test_probe_default_agent_types_derived_from_registry():
    assert PROBE_TYPES is OBSERVABLE_AGENT_TYPES
    assert set(PROBE_TYPES) == set(OBSERVABLE_AGENT_TYPES)
    assert len(PROBE_TYPES) == 4
```

## Step 2: 运行测试确认失败

Run: `python3 -m pytest tests/test_agent_profiles.py -q`
Expected: FAIL(身份断言失败 —— hub/probe 仍是字面量 tuple,与注册表对象不同)

## Step 3: 实现派生

`hub/domain/task.py`:在顶部 import 区(现有 `import re` / `from dataclasses import dataclass` 附近)加:

```python
from agent_profiles import EXECUTABLE_AGENT_TYPES
```

把 `DEFAULT_AGENT_TYPES = ("codex", "claude_code", "hermes")` 改为:

```python
DEFAULT_AGENT_TYPES = EXECUTABLE_AGENT_TYPES
```

`tools/probe_collectors.py`:在 import 区加:

```python
from agent_profiles import OBSERVABLE_AGENT_TYPES
```

把 `DEFAULT_AGENT_TYPES = ("hermes", "claude_code", "codex", "generic")` 改为:

```python
DEFAULT_AGENT_TYPES = OBSERVABLE_AGENT_TYPES
```

## Step 4: 运行测试确认通过 + 回归

Run:
```
python3 -m pytest tests/test_agent_profiles.py -q
python3 -m pytest tests/test_task_api.py tests/test_task_domain.py -q
```
Expected: 全 PASS。若任一 task 测试按顺序断言 probe 输出或字面量顺序,改为按集合断言,并在报告中说明(顺序由 hermes 开头变为 codex 开头,语义无差,ruling 已批准)。

## Step 5: 提交

```bash
git add hub/domain/task.py tools/probe_collectors.py tests/test_agent_profiles.py
git commit -m "feat: derive DEFAULT_AGENT_TYPES from profile registry"
```

**完成报告**:写 status、提交哈希、一行测试摘要、疑虑到
`/opt/agent-fleet/.claude/worktrees/agent-profile-registry/.superpowers/sdd/2026-08-28-agent-profile-registry/task-2-report.md`
返回消息只给这些字段。不得派发子 agent。