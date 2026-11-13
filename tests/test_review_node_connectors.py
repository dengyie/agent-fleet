"""Review fixes #5/#13/#14 for connectors.

#5  Claude Code 会话在 ~/.claude/projects/<slug>/<id>.jsonl（两层），旧 glob
    ``projects/*.jsonl`` 永远数到 0。
#13 generic 采集脚本的 ps 必须带 timeout，且不再取会被空格拆错的 comm 列。
#14 connector.control() 与 ProbeContext 的管控桩是死代码，已删除。
"""
import json
import os
import time

from connectors.base import BaseConnector, JsonlConnector
from connectors.claude_code import ClaudeCodeConnector
from connectors.generic import GenericConnector
from connectors.hermes import HermesConnector
from connectors.probe import ProbeContext


class _Ctx:
    """把 run_python 的脚本直接在当前解释器里执行，HOME 指向夹具目录。"""

    def __init__(self, home):
        self.home = str(home)
        self.scripts = []

    def run_python(self, script):
        import subprocess
        import sys
        self.scripts.append(script)
        env = dict(os.environ, HOME=self.home, USERPROFILE=self.home,
                   PATH="/nonexistent")
        return subprocess.run([sys.executable, "-"], input=script, text=True,
                              capture_output=True, env=env, timeout=30).stdout


def test_claude_code_counts_two_level_project_sessions(tmp_path):
    projects = tmp_path / ".claude" / "projects"
    for slug, names in {"-Users-me-a": ["s1", "s2"], "-Users-me-b": ["s3"]}.items():
        (projects / slug).mkdir(parents=True)
        for name in names:
            (projects / slug / f"{name}.jsonl").write_text("{}\n")
    old = projects / "-Users-me-b" / "old.jsonl"
    old.write_text("{}\n")
    stale = time.time() - 7200
    os.utime(old, (stale, stale))
    # 顶层散落文件不是 Claude 布局，不计入
    (projects / "stray.jsonl").write_text("{}\n")

    out = ClaudeCodeConnector().collect(_Ctx(tmp_path))
    assert out["installed"] is True
    assert out["active_count"] == 3
    files = {p["file"] for p in out["projects"]}
    assert files == {os.path.join("-Users-me-a", "s1.jsonl"),
                     os.path.join("-Users-me-a", "s2.jsonl"),
                     os.path.join("-Users-me-b", "s3.jsonl"),
                     os.path.join("-Users-me-b", "old.jsonl")}


def test_generic_collect_ps_has_timeout_and_no_comm_column(tmp_path):
    ctx = _Ctx(tmp_path)
    out = GenericConnector().collect(ctx)
    assert "process_count" in out
    script = ctx.scripts[0]
    assert "timeout=10.0" in script and "__PS_TIMEOUT__" not in script
    assert "comm" not in script.split("ps\", \"-eo\", ")[1].split("]")[0]


def test_generic_collect_survives_ps_timeout(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    ps = fake_bin / "ps"
    ps.write_text("#!/bin/sh\nexec /bin/sleep 30\n")
    ps.chmod(0o755)

    class _SlowCtx(_Ctx):
        def run_python(self, script):
            import subprocess
            import sys
            script = script.replace("timeout=10.0", "timeout=0.5")
            env = dict(os.environ, HOME=self.home, PATH=str(fake_bin))
            start = time.monotonic()
            out = subprocess.run([sys.executable, "-"], input=script, text=True,
                                 capture_output=True, env=env, timeout=20).stdout
            assert time.monotonic() - start < 10
            return out

    if os.name == "nt":
        return
    out = GenericConnector().collect(_SlowCtx(tmp_path))
    assert json.loads(json.dumps(out))["process_count"] == 0


def test_control_dead_code_removed():
    for cls in (BaseConnector, JsonlConnector, ClaudeCodeConnector,
                GenericConnector, HermesConnector):
        assert not hasattr(cls, "control"), cls
    ctx = ProbeContext({"name": "m"})
    for name in ("run_batch", "kill_gateway", "restart_process", "tmux_send",
                 "inner_reachable"):
        assert not hasattr(ctx, name), name
    assert callable(ctx.run) and callable(ctx.run_python)
