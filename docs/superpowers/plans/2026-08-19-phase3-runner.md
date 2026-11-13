# Phase 3 Runner Implementation Plan

> **Status (2026-09-08): historical / implemented.** Open checkboxes below are the original plan and were not kept in sync after merge. Do not re-implement from unchecked boxes. Remaining work is recorded in `docs/HANDOFF.md` §六 as `done` or `deferred-with-condition`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 agent 侧执行器：`tools/agent-runner.py` 主循环（poll → lease → worktree → adapter → heartbeat → result）+ git worktree 隔离 + codex/claude_code/hermes 三种 adapter。

**Architecture:** Runner 是独立进程（与 self-report 并列），主动 HTTPS pull，hub 不反向连接。每个任务在独立 git worktree 执行，adapter 只允许已注册类型 + 项目白名单内的绝对路径，结果有界（log ≤10KB / diff ≤5KB）。崩溃安全：lease 300s 自然过期 → hub 重派；结果提交失败缓存本地下次重传。

**Tech Stack:** Python 3 标准库 + PyYAML（现有依赖）；git CLI；unittest。

**Spec:** docs/superpowers/specs/2026-08-19-v4-optimization-design.md（§4.2、§5.2 runner 侧、§7.2 runner 错误处理、§7.4 安全防线）

**前置：** Phase 2 计划已完成——hub 侧 `/api/commands/poll|heartbeat|result` 可用，runner 认证头 `X-Runner-Credential: <machine>:<secret>`，lease 载荷 `{task_id, machine, agent_type, project, instruction, attempt_id, nonce, lease_ttl_s, lease_expires_at}`。

## Global Constraints

- 沿用 Phase 1/2 全部约束。
- 不接受页面传入的任意可执行文件或 shell 字符串：adapter argv 模板来自 runner.yaml 或内置默认，`{instruction}` 是唯一插值点，且作为**单个 argv 元素**传递（不经 shell）。
- 项目路径必须绝对、存在于 runner.yaml `projects` 白名单；worktree 建在项目**同级**目录（`<project>.fleet-worktrees/<task_id>`），不污染原工作区。
- 日志上传：heartbeat 携带 ≤50 行 × 500 字符；最终 log_summary 取尾部 ≤10KB；diff_stat ≤5KB。
- runner.yaml / runner credential 不进 git（`~/.config/agent-fleet/`）。
- 测试不打网络、不调用真实 codex/claude/hermes CLI（用假 adapter / 假命令）；唯一例外是 worktree 测试用真实本地 git 仓库。
- 测试运行：`.venv/bin/python -m unittest discover -s tests -v`。

## 文件结构

| 文件 | 动作 | 职责 |
|---|---|---|
| `tools/runner_config.py` | 新建 | 加载/校验 `~/.config/agent-fleet/runner.yaml` |
| `tools/worktree.py` | 新建 | git worktree 创建/diff_stat/清理 |
| `tools/adapters/__init__.py` | 新建 | adapter 注册表 + `create(agent_type, command_override=None, timeout_s=None)` |
| `tools/adapters/base.py` | 新建 | `BaseAdapter`、`AdapterResult`、子进程执行核心 |
| `tools/adapters/codex.py` | 新建 | codex adapter |
| `tools/adapters/claude_code.py` | 新建 | claude_code adapter |
| `tools/adapters/hermes.py` | 新建 | hermes adapter（command 必须在 runner.yaml 配置） |
| `tools/agent-runner.py` | 新建 | 主循环 + heartbeat 线程 + pending 结果缓存 |
| `tools/adapters/__init__.py` 等 | — | 包文件 |
| `tests/test_runner.py` | 新建 | 全部单测 + E2E（内存 hub 线程 + 假 adapter） |
| `deploy/agent-runner.yaml.example` | 新建 | 配置样例 |

---

### Task 1: runner_config.py

**Files:**
- Create: `tools/runner_config.py`
- Test: `tests/test_runner.py`（新建）

**Interfaces:**
- Produces:
  - `RunnerConfig` dataclass：`hub: str`、`machine: str`、`credential: str`、`runner_id: str`、`projects: dict[str, Path]`、`agents: dict[str, dict]`（agent_type → {command: list[str], timeout_s: int}）、`poll_interval_s: int = 15`、`heartbeat_interval_s: int = 30`、`cache_dir: Path`
  - `load_config(path: Path) -> RunnerConfig` — 校验失败抛 `ConfigError(msg)`
  - `ConfigError(Exception)`
  - yaml 格式：
    ```yaml
    hub: https://hub.example.com
    machine: mac-local
    runner_id: mac-local-1          # 可选，默认 machine
    credential_file: ~/.config/agent-fleet/runner-credential   # 0600，单行 secret
    projects:
      agent-fleet:
        path: /opt/agent-fleet
    agents:
      codex:
        command: ["codex", "exec", "--full-auto", "{instruction}"]
        timeout_s: 1800
    poll_interval_s: 15
    ```

- [ ] **Step 1: 写失败测试**

`tests/test_runner.py`:

```python
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from tools import runner_config


def load_agent_runner():
    path = Path(__file__).resolve().parents[1] / "tools" / "agent-runner.py"
    spec = importlib.util.spec_from_file_location("agent_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RunnerConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.project = self.temp / "proj"
        self.project.mkdir()
        self.cred = self.temp / "runner-credential"
        self.cred.write_text("s3cr3t\n")

    def _write(self, **overrides):
        import yaml
        data = {
            "hub": "https://hub.example.com",
            "machine": "mac-local",
            "credential_file": str(self.cred),
            "projects": {"agent-fleet": {"path": str(self.project)}},
            "agents": {"codex": {"command": ["codex", "exec", "{instruction}"],
                                  "timeout_s": 1800}},
        }
        data.update(overrides)
        path = self.temp / "runner.yaml"
        path.write_text(yaml.safe_dump(data))
        return path

    def test_load_valid_config(self):
        cfg = runner_config.load_config(self._write())
        self.assertEqual(cfg.hub, "https://hub.example.com")
        self.assertEqual(cfg.machine, "mac-local")
        self.assertEqual(cfg.runner_id, "mac-local")
        self.assertEqual(cfg.credential, "s3cr3t")
        self.assertEqual(cfg.projects["agent-fleet"], self.project)
        self.assertEqual(cfg.agents["codex"]["timeout_s"], 1800)
        self.assertEqual(cfg.poll_interval_s, 15)

    def test_missing_project_path_rejected(self):
        with self.assertRaises(runner_config.ConfigError):
            runner_config.load_config(self._write(
                projects={"ghost": {"path": str(self.temp / "nope")}}))

    def test_relative_project_path_rejected(self):
        with self.assertRaises(runner_config.ConfigError):
            runner_config.load_config(self._write(
                projects={"rel": {"path": "relative/path"}}))

    def test_missing_credential_rejected(self):
        with self.assertRaises(runner_config.ConfigError):
            runner_config.load_config(self._write(
                credential_file=str(self.temp / "missing-cred")))

    def test_unknown_hub_scheme_rejected(self):
        with self.assertRaises(runner_config.ConfigError):
            runner_config.load_config(self._write(hub="http://insecure.example"))
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_runner.RunnerConfigTests -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tools.runner_config'`

- [ ] **Step 3: 实现 tools/runner_config.py**

```python
"""tools/runner_config.py — agent-runner 配置加载与校验

配置位于 ~/.config/agent-fleet/runner.yaml（不进 git）。
项目白名单 + adapter 命令模板都在这里定义，页面无法越权。
"""
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "agent-fleet" / "runner.yaml"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "agent-fleet"


class ConfigError(Exception):
    pass


@dataclass
class RunnerConfig:
    hub: str
    machine: str
    credential: str
    runner_id: str
    projects: dict  # name -> Path（绝对、已存在）
    agents: dict    # agent_type -> {"command": [..], "timeout_s": int}
    poll_interval_s: int = 15
    heartbeat_interval_s: int = 30
    cache_dir: Path = field(default=DEFAULT_CACHE_DIR)


def _err(msg):
    raise ConfigError(msg)


def load_config(path=DEFAULT_CONFIG_PATH):
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except OSError as exc:
        _err(f"无法读取配置 {path}: {exc}")
    if not isinstance(data, dict):
        _err("runner.yaml 必须是 mapping")

    hub = str(data.get("hub") or "").rstrip("/")
    if not hub.startswith("https://"):
        _err("hub 必须是 https:// URL（runner 凭据仅允许走 HTTPS）")
    machine = str(data.get("machine") or "")
    if not machine:
        _err("machine 必填")

    cred_file = Path(data.get("credential_file")
                     or Path.home() / ".config" / "agent-fleet" / "runner-credential")
    try:
        credential = cred_file.read_text().strip()
    except OSError:
        _err(f"无法读取 runner credential 文件 {cred_file}")
    if not credential:
        _err("runner credential 为空")

    projects = {}
    for name, entry in (data.get("projects") or {}).items():
        p = Path(str((entry or {}).get("path") or ""))
        if not p.is_absolute():
            _err(f"项目 {name} 路径必须是绝对路径")
        if not p.is_dir():
            _err(f"项目 {name} 路径不存在: {p}")
        projects[str(name)] = p
    if not projects:
        _err("projects 白名单为空")

    agents = {}
    for agent_type, entry in (data.get("agents") or {}).items():
        entry = entry or {}
        command = entry.get("command")
        if command is not None and not (
                isinstance(command, list) and all(isinstance(c, str) for c in command)):
            _err(f"agents.{agent_type}.command 必须是字符串数组")
        agents[str(agent_type)] = {
            "command": command,
            "timeout_s": int(entry.get("timeout_s") or 1800),
        }

    return RunnerConfig(
        hub=hub,
        machine=machine,
        credential=credential,
        runner_id=str(data.get("runner_id") or machine),
        projects=projects,
        agents=agents,
        poll_interval_s=int(data.get("poll_interval_s") or 15),
        heartbeat_interval_s=int(data.get("heartbeat_interval_s") or 30),
        cache_dir=Path(data.get("cache_dir") or DEFAULT_CACHE_DIR),
    )
```

- [ ] **Step 4: 运行确认通过 + 提交**

Run: `.venv/bin/python -m unittest tests.test_runner.RunnerConfigTests -v`
Expected: PASS

```bash
git add tools/runner_config.py tests/test_runner.py
git commit -m "feat: runner 配置加载 — 项目白名单/adapter 模板/凭据校验"
```

---

### Task 2: worktree.py

**Files:**
- Create: `tools/worktree.py`
- Test: `tests/test_runner.py`（追加）

**Interfaces:**
- Produces:
  - `create_worktree(project: Path, task_id: str) -> Path` — `git -C <project> worktree add <project>.fleet-worktrees/<task_id> -b fleet/<task_id>`；返回 worktree 路径
  - `diff_stat(worktree: Path, max_bytes=5120) -> str` — `git add -A` 后 `git diff --cached --stat`，尾部截断到 max_bytes
  - `cleanup_worktree(project: Path, worktree: Path, task_id: str) -> None` — `git worktree remove --force` + `git branch -D fleet/<task_id>`，异常吞掉（best effort）
  - 异常 `WorktreeError(RuntimeError)`：创建失败时抛出（含 stderr 摘要）

- [ ] **Step 1: 写失败测试**

```python
import subprocess

from tools import worktree


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=True)


class WorktreeTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.project = self.temp / "proj"
        self.project.mkdir()
        git("init", cwd=self.project)
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit",
            "--allow-empty", "-m", "init", cwd=self.project)

    def test_create_diff_cleanup_cycle(self):
        wt = worktree.create_worktree(self.project, "t-abc123")
        self.assertTrue((wt / ".git").exists())
        self.assertEqual(wt.name, "t-abc123")
        # worktree 在项目同级目录，不在项目内
        self.assertEqual(wt.parent, self.temp / "proj.fleet-worktrees")
        # 在 worktree 里改文件 → diff_stat 可见
        (wt / "hello.txt").write_text("hi\n")
        stat = worktree.diff_stat(wt)
        self.assertIn("hello.txt", stat)
        # 清理后目录与分支都不存在
        worktree.cleanup_worktree(self.project, wt, "t-abc123")
        self.assertFalse(wt.exists())
        branches = git("branch", "--list", "fleet/*", cwd=self.project).stdout
        self.assertNotIn("t-abc123", branches)

    def test_diff_stat_truncates(self):
        wt = worktree.create_worktree(self.project, "t-big")
        for i in range(50):
            (wt / f"file-with-a-rather-long-name-{i:03d}.txt").write_text("x\n")
        stat = worktree.diff_stat(wt, max_bytes=200)
        self.assertLessEqual(len(stat), 220)  # 截断 + 标记
        worktree.cleanup_worktree(self.project, wt, "t-big")

    def test_create_failure_raises_worktree_error(self):
        with self.assertRaises(worktree.WorktreeError):
            worktree.create_worktree(self.temp / "not-a-repo", "t-x")
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_runner.WorktreeTests -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tools.worktree'`

- [ ] **Step 3: 实现 tools/worktree.py**

```python
"""tools/worktree.py — 任务级 git worktree 隔离

每个任务在 <project>.fleet-worktrees/<task_id> 建独立 worktree + fleet/<task_id> 分支，
不污染原工作区；结束后 best-effort 清理。
"""
import re
import subprocess
from pathlib import Path

TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class WorktreeError(RuntimeError):
    pass


def _git(project: Path, *args, check=True):
    proc = subprocess.run(["git", *args], cwd=str(project),
                          capture_output=True, text=True, timeout=60)
    if check and proc.returncode != 0:
        raise WorktreeError(f"git {' '.join(args)} 失败: {proc.stderr.strip()[:300]}")
    return proc


def _root(project: Path) -> Path:
    return project.parent / f"{project.name}.fleet-worktrees"


def create_worktree(project: Path, task_id: str) -> Path:
    project = Path(project)
    if not TASK_ID_RE.fullmatch(task_id):
        raise WorktreeError(f"非法 task_id: {task_id!r}")
    if not (project / ".git").exists():
        raise WorktreeError(f"{project} 不是 git 仓库")
    target = _root(project) / task_id
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        cleanup_worktree(project, target, task_id)
    _git(project, "worktree", "add", str(target), "-b", f"fleet/{task_id}")
    return target


def diff_stat(worktree: Path, max_bytes=5120) -> str:
    worktree = Path(worktree)
    _git(worktree, "add", "-A")  # 让未跟踪文件进入 diff 视野（worktree 即用即弃）
    proc = _git(worktree, "diff", "--cached", "--stat")
    out = proc.stdout
    if len(out.encode("utf-8", "replace")) > max_bytes:
        out = out.encode("utf-8", "replace")[:max_bytes].decode("utf-8", "replace")
        out += "\n…[truncated]"
    return out


def cleanup_worktree(project: Path, worktree: Path, task_id: str) -> None:
    try:
        _git(Path(project), "worktree", "remove", "--force", str(worktree), check=False)
        _git(Path(project), "branch", "-D", f"fleet/{task_id}", check=False)
    except Exception:
        pass
```

- [ ] **Step 4: 运行确认通过 + 提交**

Run: `.venv/bin/python -m unittest tests.test_runner.WorktreeTests -v`
Expected: PASS

```bash
git add tools/worktree.py tests/test_runner.py
git commit -m "feat: 任务 worktree 隔离 — 创建/diff 摘要/清理"
```

---

### Task 3: adapters 基类 + 注册表 + codex

**Files:**
- Create: `tools/adapters/__init__.py`、`tools/adapters/base.py`、`tools/adapters/codex.py`
- Test: `tests/test_runner.py`（追加）

**Interfaces:**
- Produces:
  - `adapters.AdapterResult` dataclass：`exit_code: int`、`log_tail: str`（尾部 ≤10KB）、`timed_out: bool`、`aborted: bool`
  - `adapters.base.BaseAdapter`：`name: str`、`default_command: list[str] | None`、`__init__(command=None, timeout_s=1800)`、`run(instruction: str, workdir: Path, on_line=None, should_abort=None) -> AdapterResult`
    - argv 构建：`command`（或 default_command）中字面量 `"{instruction}"` 替换为 instruction（单 argv 元素，不经 shell）；模板无占位符时 instruction 追加为最后一个元素
    - `on_line(line: str)` 每行回调（截断 500 字符）
    - `should_abort() -> bool` 每秒检查，True → terminate 子进程，`aborted=True, exit_code=130`
    - 超时 → kill，`timed_out=True, exit_code=124`
    - command 为 None 且无 default → `AdapterResult(exit_code=127, log_tail="adapter ... 未配置 command", ...)`
  - `adapters.create(agent_type, command=None, timeout_s=1800) -> BaseAdapter`；未知类型抛 `ValueError`
  - `CodexAdapter`：`name="codex"`，`default_command=["codex", "exec", "--full-auto", "{instruction}"]`

- [ ] **Step 1: 写失败测试**

```python
import sys
import time

from tools import adapters


class AdapterTests(unittest.TestCase):
    def _python(self, script):
        return [sys.executable, "-c", script]

    def test_run_captures_output_lines(self):
        adapter = adapters.create("codex", command=self._python(
            "import sys; print('line1'); print('line2'); sys.argv and None"),
                                  timeout_s=30)
        lines = []
        result = adapter.run("do something", Path(tempfile.mkdtemp()), on_line=lines.append)
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.timed_out)
        self.assertEqual(lines, ["line1", "line2"])
        self.assertIn("line2", result.log_tail)

    def test_instruction_interpolated_as_single_argv(self):
        adapter = adapters.create("codex", command=self._python(
            "import sys; print(sys.argv[1])") + ["{instruction}"], timeout_s=30)
        lines = []
        adapter.run("fix $(rm -rf /) ; `whoami`", Path(tempfile.mkdtemp()),
                    on_line=lines.append)
        # instruction 作为单 argv 元素原样传递，不经 shell 展开
        self.assertEqual(lines, ["fix $(rm -rf /) ; `whoami`"])

    def test_timeout_kills_process(self):
        adapter = adapters.create("codex", command=self._python(
            "import time; time.sleep(60)"), timeout_s=1)
        start = time.time()
        result = adapter.run("x", Path(tempfile.mkdtemp()))
        self.assertTrue(result.timed_out)
        self.assertEqual(result.exit_code, 124)
        self.assertLess(time.time() - start, 10)

    def test_abort_terminates_process(self):
        adapter = adapters.create("codex", command=self._python(
            "import time; print('start', flush=True); time.sleep(60)"), timeout_s=60)
        flag = {"abort": False}
        lines = []

        def on_line(line):
            lines.append(line)
            flag["abort"] = True

        result = adapter.run("x", Path(tempfile.mkdtemp()), on_line=on_line,
                             should_abort=lambda: flag["abort"])
        self.assertTrue(result.aborted)
        self.assertEqual(result.exit_code, 130)

    def test_unconfigured_adapter_returns_127(self):
        result = adapters.create("hermes", command=None, timeout_s=1).run(
            "x", Path(tempfile.mkdtemp()))
        self.assertEqual(result.exit_code, 127)
        self.assertIn("未配置", result.log_tail)

    def test_unknown_adapter_type_rejected(self):
        with self.assertRaises(ValueError):
            adapters.create("bash")

    def test_log_tail_bounded(self):
        adapter = adapters.create("codex", command=self._python(
            "print('x' * 20000)"), timeout_s=30)
        result = adapter.run("x", Path(tempfile.mkdtemp()))
        self.assertLessEqual(len(result.log_tail), 10240)
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_runner.AdapterTests -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tools.adapters'`

- [ ] **Step 3: 实现 tools/adapters/base.py**

```python
"""tools/adapters/base.py — adapter 执行核心

子进程执行 agent CLI；instruction 只作为单 argv 元素传递（不经 shell）。
输出行流式回调 on_line；should_abort 轮询用于 lease 失效后主动终止。
"""
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

MAX_LOG_TAIL = 10240
MAX_LINE = 500


@dataclass
class AdapterResult:
    exit_code: int
    log_tail: str
    timed_out: bool = False
    aborted: bool = False


class BaseAdapter:
    name = "base"
    default_command = None  # list[str]，"{instruction}" 为插值点

    def __init__(self, command=None, timeout_s=1800):
        self.command = command if command is not None else self.default_command
        self.timeout_s = int(timeout_s)

    def build_argv(self, instruction):
        argv = list(self.command)
        if "{instruction}" in argv:
            return [instruction if c == "{instruction}" else c for c in argv]
        return argv + [instruction]

    def run(self, instruction, workdir, on_line=None, should_abort=None):
        if not self.command:
            return AdapterResult(exit_code=127,
                                 log_tail=f"adapter {self.name} 未配置 command")
        argv = self.build_argv(instruction)
        tail = []
        tail_len = [0]

        def feed(line):
            line = line[:MAX_LINE]
            tail.append(line)
            tail_len[0] += len(line) + 1
            while tail_len[0] > MAX_LOG_TAIL and tail:
                tail_len[0] -= len(tail.pop(0)) + 1
            if on_line:
                try:
                    on_line(line)
                except Exception:
                    pass

        try:
            proc = subprocess.Popen(
                argv, cwd=str(workdir),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace",
                start_new_session=True,  # 独立进程组，超时/中止可整组杀
            )
        except OSError as exc:
            return AdapterResult(exit_code=127,
                                 log_tail=f"启动失败: {type(exc).__name__}: {exc}"[:MAX_LINE])

        timed_out = threading.Event()

        def watchdog():
            proc.wait()
            timed_out.set()

        waiter = threading.Thread(target=watchdog, daemon=True)
        waiter.start()

        def reader():
            try:
                for line in proc.stdout:
                    feed(line.rstrip("\n"))
            except Exception:
                pass

        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()

        deadline = time.monotonic() + self.timeout_s
        aborted = False
        while not timed_out.wait(1.0):
            if should_abort and should_abort():
                aborted = True
                _kill(proc)
                break
            if time.monotonic() > deadline:
                _kill(proc)
                break
        timed_out.wait(10)  # 等进程确认退出
        reader_thread.join(timeout=5)

        if aborted:
            return AdapterResult(exit_code=130, log_tail="\n".join(tail) + "\n[aborted: lease lost]",
                                 aborted=True)
        if proc.returncode is None or (proc.returncode and proc.returncode < 0 and
                                       time.monotonic() > deadline):
            return AdapterResult(exit_code=124,
                                 log_tail="\n".join(tail) + f"\n[timeout after {self.timeout_s}s]",
                                 timed_out=True)
        return AdapterResult(exit_code=proc.returncode or 0, log_tail="\n".join(tail))


def _kill(proc):
    import os
    import signal
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass
```

- [ ] **Step 4: 实现 tools/adapters/codex.py 与 __init__.py**

`tools/adapters/codex.py`:

```python
"""tools/adapters/codex.py — Codex CLI adapter"""
from tools.adapters.base import BaseAdapter


class CodexAdapter(BaseAdapter):
    name = "codex"
    # codex exec：非交互执行单个任务，--full-auto 允许在工作区内自主改代码
    default_command = ["codex", "exec", "--full-auto", "{instruction}"]
```

`tools/adapters/__init__.py`:

```python
"""tools/adapters — 任务执行器注册表（仿 connectors 工厂模式）"""
from tools.adapters.base import AdapterResult, BaseAdapter
from tools.adapters.codex import CodexAdapter

_REGISTRY = {}


def register(cls):
    _REGISTRY[cls.name] = cls
    return cls


register(CodexAdapter)

# claude_code / hermes 在 Task 4 register

from tools.adapters.claude_code import ClaudeCodeAdapter  # noqa: E402
from tools.adapters.hermes import HermesAdapter  # noqa: E402
register(ClaudeCodeAdapter)
register(HermesAdapter)


def create(agent_type, command=None, timeout_s=1800):
    cls = _REGISTRY.get(agent_type)
    if cls is None:
        raise ValueError(f"未知 adapter 类型: {agent_type}")
    return cls(command=command, timeout_s=timeout_s)
```

Task 3 阶段 `claude_code.py`/`hermes.py` 还不存在——先建最小占位（Task 4 填默认命令）：

`tools/adapters/claude_code.py`:

```python
"""tools/adapters/claude_code.py — Claude Code CLI adapter"""
from tools.adapters.base import BaseAdapter


class ClaudeCodeAdapter(BaseAdapter):
    name = "claude_code"
    # claude -p：非交互 print 模式
    default_command = ["claude", "-p", "{instruction}"]
```

`tools/adapters/hermes.py`:

```python
"""tools/adapters/hermes.py — Hermes adapter

Hermes CLI 无统一非交互任务入口，default_command=None：
必须在 runner.yaml agents.hermes.command 显式配置本机命令模板。
"""
from tools.adapters.base import BaseAdapter


class HermesAdapter(BaseAdapter):
    name = "hermes"
    default_command = None
```

（即 Task 3 与 Task 4 的 adapter 文件一次性落地，Task 4 只补测试。）

- [ ] **Step 5: 运行确认通过 + 提交**

Run: `.venv/bin/python -m unittest tests.test_runner.AdapterTests -v`
Expected: PASS

```bash
git add tools/adapters/ tests/test_runner.py
git commit -m "feat: adapter 执行核心 + codex/claude_code/hermes 三种 adapter"
```

---

### Task 4: adapter 默认命令覆盖测试

**Files:**
- Test: `tests/test_runner.py`（追加）

- [ ] **Step 1: 写测试**

```python
class AdapterDefaultsTests(unittest.TestCase):
    def test_default_commands(self):
        self.assertEqual(adapters.create("codex").build_argv("修 bug"),
                         ["codex", "exec", "--full-auto", "修 bug"])
        self.assertEqual(adapters.create("claude_code").build_argv("修 bug"),
                         ["claude", "-p", "修 bug"])
        self.assertIsNone(adapters.create("hermes").command)

    def test_argv_without_placeholder_appends_instruction(self):
        adapter = adapters.create("codex", command=[sys.executable, "-c", "pass"])
        self.assertEqual(adapter.build_argv("do it")[-1], "do it")
```

- [ ] **Step 2: 运行 + 提交**

Run: `.venv/bin/python -m unittest tests.test_runner -v`
Expected: 全绿

```bash
git add tests/test_runner.py
git commit -m "test: adapter 默认命令与 argv 构建"
```

---

### Task 5: agent-runner.py 主循环

**Files:**
- Create: `tools/agent-runner.py`
- Test: `tests/test_runner.py`（追加，HTTP 层 mock）

**Interfaces:**
- Consumes: `runner_config.load_config`、`worktree.*`、`adapters.create`；hub API（Phase 2）。
- Produces（模块级函数，供测试与 CLI）：
  - `post_json(cfg, path, body, timeout=15) -> (status:int, data:dict)` — urllib POST，`X-Runner-Credential: <machine>:<credential>`，User-Agent `agent-fleet-runner/1.0`；网络异常返回 `(0, {"error": ...})`
  - `flush_pending(cfg) -> None` — 重传 `cache_dir/pending/*.json` 的结果
  - `run_task(cfg, task) -> dict` — 完整执行一个任务（worktree → adapter + heartbeat 线程 → result 提交/缓存 → 清理），返回结果 dict
  - `poll_once(cfg) -> bool` — flush_pending → poll → 有任务则 run_task；返回是否有任务
  - `main(argv=None) -> int` — `--config`、`--once`、`--interval`

- [ ] **Step 1: 写失败测试**

```python
import threading
from unittest import mock


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class AgentRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.project = self.temp / "proj"
        self.project.mkdir()
        subprocess.run(["git", "init"], cwd=self.project, capture_output=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "--allow-empty", "-m", "init"],
                       cwd=self.project, capture_output=True)
        self.runner = load_agent_runner()
        self.cfg = runner_config.RunnerConfig(
            hub="https://hub.test", machine="mac-local", credential="s3",
            runner_id="r1", projects={"proj": self.project},
            agents={"codex": {"command": None, "timeout_s": 30}},
            poll_interval_s=1, heartbeat_interval_s=1, cache_dir=self.temp / "cache")

    def _task(self):
        return {"task_id": "t-1", "machine": "mac-local", "agent_type": "codex",
                "project": "proj", "instruction": "do it",
                "attempt_id": "a1", "nonce": "n1",
                "lease_ttl_s": 300, "lease_expires_at": "..."}

    def test_post_json_sets_runner_headers(self):
        captured = {}

        def fake_urlopen(req, timeout):
            captured["cred"] = req.headers.get("X-runner-credential")
            captured["ua"] = req.headers.get("User-agent")
            return FakeResponse(200, {"ok": True})

        with mock.patch.object(self.runner.urllib.request, "urlopen",
                               side_effect=fake_urlopen):
            status, data = self.runner.post_json(self.cfg, "/api/commands/poll", {})
        self.assertEqual(status, 200)
        self.assertEqual(captured["cred"], "mac-local:s3")
        self.assertEqual(captured["ua"], "agent-fleet-runner/1.0")

    def test_run_task_success_flow(self):
        posts = []

        def fake_urlopen(req, timeout):
            posts.append((req.full_url, json.loads(req.data.decode())))
            return FakeResponse(200, {"ok": True, "task_id": "t-1",
                                      "lease_expires_at": "..."})

        fake_result = adapters.AdapterResult(exit_code=0, log_tail="done")
        with mock.patch.object(self.runner.urllib.request, "urlopen",
                               side_effect=fake_urlopen), \
             mock.patch.object(self.runner.adapters, "create",
                               return_value=mock.Mock(run=mock.Mock(return_value=fake_result))):
            out = self.runner.run_task(self.cfg, self._task())
        self.assertEqual(out["exit_code"], 0)
        result_posts = [p for p in posts if p[0].endswith("/result")]
        self.assertEqual(len(result_posts), 1)
        self.assertEqual(result_posts[0][1]["nonce"], "n1")
        self.assertEqual(result_posts[0][1]["log_summary"], "done")
        # worktree 已清理
        self.assertFalse((self.temp / "proj.fleet-worktrees" / "t-1").exists())

    def test_result_post_failure_cached_to_pending(self):
        def fail_urlopen(req, timeout):
            raise OSError("network down")

        fake_result = adapters.AdapterResult(exit_code=0, log_tail="done")
        with mock.patch.object(self.runner.urllib.request, "urlopen",
                               side_effect=fail_urlopen), \
             mock.patch.object(self.runner.adapters, "create",
                               return_value=mock.Mock(run=mock.Mock(return_value=fake_result))):
            self.runner.run_task(self.cfg, self._task())
        pending = list((self.temp / "cache" / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1)
        saved = json.loads(pending[0].read_text())
        self.assertEqual(saved["attempt_id"], "a1")

    def test_flush_pending_retries_upload(self):
        pending_dir = self.temp / "cache" / "pending"
        pending_dir.mkdir(parents=True)
        (pending_dir / "a1.json").write_text(json.dumps({
            "attempt_id": "a1", "nonce": "n1", "exit_code": 0,
            "log_summary": "done", "diff_stat": "", "duration_s": 1}))
        posts = []
        with mock.patch.object(self.runner.urllib.request, "urlopen",
                               side_effect=lambda req, timeout: (
                                   posts.append(req.full_url),
                                   FakeResponse(200, {"ok": True}))[1]):
            self.runner.flush_pending(self.cfg)
        self.assertEqual(len(posts), 1)
        self.assertEqual(list(pending_dir.glob("*.json")), [])

    def test_project_not_in_whitelist_refused(self):
        task = self._task()
        task["project"] = "evil"
        with mock.patch.object(self.runner.urllib.request, "urlopen",
                               return_value=FakeResponse(200, {"ok": True})) as uo, \
             mock.patch.object(self.runner.adapters, "create") as create:
            out = self.runner.run_task(self.cfg, task)
        self.assertNotEqual(out["exit_code"], 0)
        create.assert_not_called()  # adapter 未启动
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_runner.AgentRunnerTests -v`
Expected: FAIL — `agent-runner.py` 不存在

- [ ] **Step 3: 实现 tools/agent-runner.py**

```python
#!/usr/bin/env python3
"""tools/agent-runner.py — agent 侧任务执行器（push/pull-only）

主循环：poll → lease → worktree → adapter → heartbeat → result。
runner 主动出站 HTTPS；hub 不反向连接。崩溃安全：lease 过期由 hub 重派。

用法:
  python3 tools/agent-runner.py [--config ~/.config/agent-fleet/runner.yaml]
                                [--once] [--interval 15]
"""
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

FLEET_HOME = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(FLEET_HOME))

from tools import adapters, runner_config, worktree

USER_AGENT = "agent-fleet-runner/1.0"
MAX_BACKOFF_S = 60
MIN_BACKOFF_S = 5


def post_json(cfg, path, body, timeout=15):
    """POST JSON 到 hub；网络异常返回 (0, {...}) 不抛。"""
    req = urllib.request.Request(
        cfg.hub + path,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Runner-Credential": f"{cfg.machine}:{cfg.credential}",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode() or "{}")
        except Exception:
            return exc.code, {"error": "http_error"}
    except Exception as exc:
        return 0, {"error": f"{type(exc).__name__}: {exc}"}


def _pending_dir(cfg):
    d = cfg.cache_dir / "pending"
    d.mkdir(parents=True, exist_ok=True)
    return d


def flush_pending(cfg):
    """重传上次失败的结果（attempt_id 幂等，重复提交安全）。"""
    for f in sorted(_pending_dir(cfg).glob("*.json")):
        try:
            body = json.loads(f.read_text())
            status, _ = post_json(cfg, f"/api/commands/{body['attempt_id']}/result", body)
            if status in (200, 409):  # 409 = lease 已失效但结果已记录/任务已重派
                f.unlink()
        except Exception:
            pass


def _submit_result(cfg, attempt_id, nonce, result, diff_stat, duration_s):
    body = {
        "attempt_id": attempt_id, "nonce": nonce, "exit_code": result.exit_code,
        "log_summary": result.log_tail, "diff_stat": diff_stat,
        "duration_s": round(duration_s, 1),
    }
    status, _ = post_json(cfg, f"/api/commands/{attempt_id}/result", body)
    if status != 200:
        (_pending_dir(cfg) / f"{attempt_id}.json").write_text(
            json.dumps(body, ensure_ascii=False))
    return status


def run_task(cfg, task):
    """执行一个已领取的任务。返回 {"exit_code", ...} 摘要。"""
    task_id = task["task_id"]
    attempt_id = task["attempt_id"]
    nonce = task["nonce"]
    started = time.monotonic()

    project = cfg.projects.get(task["project"])
    if project is None:
        result = adapters.AdapterResult(
            exit_code=126, log_tail=f"项目 {task['project']} 不在 runner 白名单")
        _submit_result(cfg, attempt_id, nonce, result, "", 0.0)
        return {"exit_code": 126}

    wt = None
    log_buffer = []
    buffer_lock = threading.Lock()
    lease_lost = threading.Event()

    def on_line(line):
        with buffer_lock:
            log_buffer.append(line)

    def heartbeat_loop():
        while not lease_lost.wait(cfg.heartbeat_interval_s):
            with buffer_lock:
                lines, log_buffer[:] = log_buffer[:], []
            status, _ = post_json(cfg, f"/api/commands/{attempt_id}/heartbeat",
                                  {"nonce": nonce, "log_lines": lines})
            if status == 409:
                lease_lost.set()  # adapter 将在 1s 内被 should_abort 终止
                return

    try:
        wt = worktree.create_worktree(project, task_id)
        agent_cfg = cfg.agents.get(task["agent_type"], {})
        adapter = adapters.create(task["agent_type"],
                                  command=agent_cfg.get("command"),
                                  timeout_s=agent_cfg.get("timeout_s", 1800))
        hb = threading.Thread(target=heartbeat_loop, name="lease-heartbeat", daemon=True)
        hb.start()
        result = adapter.run(task["instruction"], wt, on_line=on_line,
                             should_abort=lease_lost.is_set)
        lease_lost.set()  # 停 heartbeat 线程
        diff = worktree.diff_stat(wt)
        duration = time.monotonic() - started
        _submit_result(cfg, attempt_id, nonce, result, diff, duration)
        return {"exit_code": result.exit_code}
    except Exception as exc:
        lease_lost.set()
        result = adapters.AdapterResult(
            exit_code=125, log_tail=f"runner 内部错误: {type(exc).__name__}: {exc}"[:500])
        _submit_result(cfg, attempt_id, nonce, result, "", time.monotonic() - started)
        return {"exit_code": 125}
    finally:
        lease_lost.set()
        if wt is not None:
            worktree.cleanup_worktree(project, wt, task_id)


def poll_once(cfg):
    """单轮：先重传缓存结果，再 poll；有任务则执行。返回是否有任务。"""
    flush_pending(cfg)
    status, data = post_json(cfg, "/api/commands/poll", {"runner_id": cfg.runner_id})
    if status != 200 or not data.get("ok"):
        return False
    task = data.get("task")
    if not task:
        return False
    run_task(cfg, task)
    return True


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="agent-fleet runner（pull-only 任务执行器）")
    parser.add_argument("--config", default=str(runner_config.DEFAULT_CONFIG_PATH))
    parser.add_argument("--once", action="store_true", help="单轮执行（cron 模式）")
    parser.add_argument("--interval", type=int, default=None, help="常驻轮询间隔秒")
    args = parser.parse_args(argv)

    cfg = runner_config.load_config(args.config)
    interval = args.interval or cfg.poll_interval_s

    if args.once:
        poll_once(cfg)
        return 0

    backoff = MIN_BACKOFF_S
    while True:
        try:
            did_work = poll_once(cfg)
            backoff = MIN_BACKOFF_S
            if not did_work:
                time.sleep(interval)
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            print(f"[runner] 轮询异常: {type(exc).__name__}: {exc}", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_S)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/python -m unittest tests.test_runner.AgentRunnerTests -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add tools/agent-runner.py tests/test_runner.py
git commit -m "feat: agent-runner 主循环 — poll/lease/heartbeat/result + pending 重传"
```

---

### Task 6: E2E 集成测试（内存 hub + 真 runner + 假 adapter）

**Files:**
- Test: `tests/test_runner.py`（追加）

**Interfaces:**
- Consumes: Phase 2 `web.make_app`、Task 5 runner 全部接口。

- [ ] **Step 1: 写 E2E 测试**

用 werkzeug 在 ephemeral 端口起真实 hub 线程，runner 走真实 HTTP 全链路；adapter 用 `sys.executable -c` 假命令（真实子进程、真实 worktree、真实心跳/结果流）：

```python
class EndToEndRunnerTests(unittest.TestCase):
    def test_hub_runner_full_cycle(self):
        from werkzeug.serving import make_server
        from hub import web, events, state as store, task_store

        temp = Path(tempfile.mkdtemp())
        old = (store.STATE_DIR, events.EVENT_LOG, task_store.DB_PATH)
        store.STATE_DIR = temp
        events.EVENT_LOG = temp / "events.jsonl"
        task_store.DB_PATH = temp / "fleet.db"
        task_store.init_db()
        try:
            store.save_snapshot("mac-local", {"machine": "mac-local", "source": "ingest",
                                              "reachable": True, "agents": {}, "system": {}})
            app = web.make_app(
                ingest_token="it", dev_operator="op@example.com",
                runner_credentials={"mac-local": "rs"},
                project_whitelist={"mac-local": ["proj"]})
            server = make_server("127.0.0.1", 0, app)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                # operator 建任务
                import urllib.request as ur
                req = ur.Request(f"http://127.0.0.1:{port}/api/tasks",
                                 data=json.dumps({"machine": "mac-local",
                                                  "agent_type": "codex", "project": "proj",
                                                  "instruction": "写个文件"}).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Cf-Access-Authenticated-User-Email": "op@e.com"},
                                 method="POST")
                created = json.loads(ur.urlopen(req, timeout=10).read())
                task_id = created["task"]["task_id"]

                # runner 单轮（假 codex 命令：真实子进程写文件到 worktree）
                runner = load_agent_runner()
                project = temp / "proj"
                project.mkdir()
                subprocess.run(["git", "init"], cwd=project, capture_output=True)
                subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                                "commit", "--allow-empty", "-m", "init"],
                               cwd=project, capture_output=True)
                cfg = runner_config.RunnerConfig(
                    hub=f"http://127.0.0.1:{port}", machine="mac-local",
                    credential="rs", runner_id="r1", projects={"proj": project},
                    agents={"codex": {"command": [
                        sys.executable, "-c",
                        "import pathlib; pathlib.Path('out.txt').write_text('hi');"
                        " print('wrote out.txt')"], "timeout_s": 30}},
                    poll_interval_s=1, heartbeat_interval_s=1,
                    cache_dir=temp / "cache")
                self.assertTrue(runner.poll_once(cfg))

                task = task_store.get_task(task_id)
                self.assertEqual(task["state"], "succeeded")
                self.assertIn("wrote out.txt", task["result"]["log_summary"])
                self.assertIn("out.txt", task["result"]["diff_stat"])
            finally:
                server.shutdown()
        finally:
            store.STATE_DIR, events.EVENT_LOG, task_store.DB_PATH = old
```

- [ ] **Step 2: 运行确认通过**

Run: `.venv/bin/python -m unittest tests.test_runner.EndToEndRunnerTests -v`
Expected: PASS（一轮完成 创建→领取→执行→心跳→结果→succeeded）

- [ ] **Step 3: 全量回归 + 提交**

Run: `.venv/bin/python -m unittest discover -s tests -v && python3 -m compileall -q connectors hub tools tests`
Expected: 全绿

```bash
git add tests/test_runner.py
git commit -m "test: runner E2E — 内存 hub 全链路（假 adapter 真实子进程）"
```

---

### Task 7: 配置样例 + 文档

**Files:**
- Create: `deploy/agent-runner.yaml.example`
- Modify: `README.md`、`docs/HANDOFF.md`

- [ ] **Step 1: 写 deploy/agent-runner.yaml.example**

```yaml
# agent-fleet runner 配置样例
# 部署到 ~/.config/agent-fleet/runner.yaml（不进 git）
hub: https://hub.example.com
machine: mac-local            # 必须与 hosts.yaml / ingest 名一致
runner_id: mac-local-1        # 可选；同机多 runner 时区分
credential_file: ~/.config/agent-fleet/runner-credential  # 0600；对应 hub credentials/runner-credentials.json 的本机键

projects:                     # 项目白名单：页面只能创建这些项目的任务
  agent-fleet:
    path: /opt/agent-fleet

agents:
  codex:
    command: ["codex", "exec", "--full-auto", "{instruction}"]
    timeout_s: 1800
  claude_code:
    command: ["claude", "-p", "{instruction}"]
    timeout_s: 1800
  hermes:
    command: null             # hermes 无统一 CLI，必须按本机实际配置
    timeout_s: 1800

poll_interval_s: 15           # 常驻模式轮询间隔
heartbeat_interval_s: 30      # lease 心跳间隔（TTL 300s）
# cache_dir: ~/.cache/agent-fleet   # 结果重传缓存
```

部署方式（写进 README）：

```cron
# cron 单轮模式（每分钟检查一次新任务）
* * * * * cd /path/to/agent-fleet && /usr/bin/python3 tools/agent-runner.py --once >> ~/.cache/agent-fleet/runner.log 2>&1
```

- [ ] **Step 2: 更新 README.md / docs/HANDOFF.md**

README「当前限制」改为：受控开发链路已完整实现（任务队列 + runner pull + worktree 隔离 + 三种 adapter）；runner 部署见 `deploy/agent-runner.yaml.example`。

HANDOFF「未实现能力」删除 runner 未实现条目，改为：

```markdown
- `GET /api/tasks/<id>/files/<path>` 按需读文件：未实现（需 runner 侧文件回传通道，当前结果只含 diff 摘要与有界日志）。
- WebSocket：未实现；实时性由 SSE 提供。
```

- [ ] **Step 3: 最终回归 + 提交**

Run: `.venv/bin/python -m unittest discover -s tests -v && python3 -m compileall -q connectors hub tools tests`

```bash
git add deploy/agent-runner.yaml.example README.md docs/HANDOFF.md
git commit -m "docs: runner 配置样例与部署说明"
```

---

## Self-Review 记录

- **Spec 覆盖**：§4.2 五个新文件全落地 ✅；§5.2 runner 侧 worktree/adapter/heartbeat/result ✅；§7.2 五行错误处理——poll 网络失败指数退避 ✅（main backoff 5→60）、执行崩溃 lease 过期 ✅（hub 侧 Phase 2 expire_leases）、adapter 失败上报 ✅（非零 exit_code → failed）、heartbeat 409 放弃+清理 ✅（lease_lost → abort → finally cleanup）、结果缓存重传 ✅（pending/）；§7.4 adapter 白名单/无任意 shell/路径白名单/有界结果 ✅。
- **类型一致性**：`AdapterResult(exit_code, log_tail, timed_out, aborted)` 与 runner 使用一致 ✅；`RunnerConfig` 字段与 agent-runner/runner_config 测试一致 ✅；`create(agent_type, command, timeout_s)` 签名与注册表一致 ✅；`poll_once -> bool`、`post_json -> (status, dict)` 与测试一致 ✅。
- **安全**：instruction 仅作单 argv 元素 ✅（测试验证 shell 元字符不展开）；credential 仅 HTTPS ✅（ConfigError 拒绝 http://，E2E 用本地 http 属测试豁免——测试直接构造 RunnerConfig 绕过 load_config 校验）；worktree task_id 正则校验 ✅。
- **有意偏离 spec 处**：spec §5.2 heartbeat 只续期；实现允许 heartbeat 携带 `log_lines`（≤50×500）以支撑 spec §5.3 的 task_log SSE 事件——这是 spec 事件流定义所要求的唯一上行通道，属加法不属冲突。
