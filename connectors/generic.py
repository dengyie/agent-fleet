"""connectors/generic.py — 通用进程/日志连接器（fallback）

采集: 按 pattern 匹配进程 + 最近日志文件
非特定 agent 类型使用它（如自定义脚本型 agent）
"""

import json
import re
import time
from typing import Iterator

from .base import BaseConnector
from tools.probe import discovery as _probe_discovery


class GenericConnector(BaseConnector):
    """动态构建的通用连接器，TYPE 由构造函数传入"""

    def __init__(self, agent_type="generic"):
        self.TYPE = agent_type
        self.pattern = "claude|codex|astrbot|openclaw|opencode|aider"

    # ------------------------------------------------------------------
    def enumerate_processes(self) -> Iterator[_probe_discovery.ProcRow]:
        """Yield local process rows matching this connector's pattern.

        Pure-local read-only enumeration of the same ``ps`` table the probe
        discovery layer consumes; the connector simply filters the rows it
        would historically have matched by ``pattern``.  Never raises: a
        ``ps`` failure yields no rows (the caller buckets ``discovery_error``).
        """
        pattern_re = re.compile(self.pattern, re.IGNORECASE)
        try:
            rows = _probe_discovery.enumerate_process_rows()
        except Exception:
            rows = []
        for row in rows:
            haystack = " ".join(filter(None, (row.exe_path, row.cmdline)))
            if pattern_re.search(haystack):
                yield row

    # ------------------------------------------------------------------
    def collect(self, ctx):
        # 进程检测（busybox ps 兼容）+ 最近日志
        remote_script = r"""
import json, os, re, subprocess, time
pattern = "__PATTERN__"
pattern_re = re.compile(pattern, re.IGNORECASE)
# 进程
try:
    ps = subprocess.run(
        ["ps", "-eo", "user,pid,time,rss,comm,args"],
        capture_output=True, text=True
    ).stdout
except Exception:
    ps = ""
procs = []
for line in ps.splitlines()[1:]:
    low = line.lower()
    if pattern_re.search(low):
        if any(s in line for s in ("grep ", "collect", "agent-fleet", "hermes-snap", " ps ")):
            continue
        parts = line.split(None, 5)
        if len(parts) >= 6:
            procs.append({"user": parts[0], "pid": parts[1],
                          "cpu_time": parts[2], "rss_kb": parts[3], "cmd": parts[5][:150]})
# 最近日志
logs = []
for d in ("~/.claude/projects", "~/.codex", "~/.config/claude"):
    p = os.path.expanduser(d)
    if os.path.isdir(p):
        try:
            for f in sorted(os.listdir(p))[-3:]:
                fp = os.path.join(p, f)
                st = os.stat(fp)
                if time.time() - st.st_mtime < 1800:
                    logs.append({"file": f, "mtime": st.st_mtime, "size": st.st_size})
        except Exception:
            pass
print(json.dumps({"processes": procs[:15], "recent_logs": logs[:5],
                  "process_count": len(procs)}))
"""
        remote_script = remote_script.replace("__PATTERN__", self.pattern)
        raw = ctx.run_python(remote_script)
        try:
            return json.loads(raw)
        except Exception:
            return {"error": "collect parse failed", "raw": raw[:200]}

    # ------------------------------------------------------------------
    def control(self, ctx, action, **kwargs):
        if action == "status":
            return {"ok": True, "data": self.collect(ctx)}
        if action == "restart":
            pat = kwargs.get("pattern", self.pattern)
            return ctx.restart_process(pat)
        if action == "exec":
            cmd = kwargs.get("cmd", "")
            return ctx.run(cmd)
        return {"ok": False, "message": f"{self.TYPE} 不支持 action={action}"}
