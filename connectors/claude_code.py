"""connectors/claude_code.py — Claude Code 连接器

采集: ~/.claude/projects/*.jsonl 会话日志（最近写入的文件 + 尾部摘要）
管控: tmux 注入 / kill 重启
"""

import json
import os

from .base import BaseConnector


class ClaudeCodeConnector(BaseConnector):
    TYPE = "claude_code"

    # ------------------------------------------------------------------
    def collect(self, ctx):
        remote_script = r"""
import json, os, glob, time
base = os.path.expanduser('~/.claude/projects')
out = {'installed': os.path.isdir(base)}
if not out['installed']:
    print(json.dumps(out)); raise SystemExit(0)
files = []
try:
    for fp in glob.glob(os.path.join(base, '*.jsonl')):
        st = os.stat(fp)
        files.append({'file': os.path.basename(fp), 'mtime': st.st_mtime,
                      'size': st.st_size, 'age_s': int(time.time() - st.st_mtime)})
except Exception:
    pass
files.sort(key=lambda x: x['mtime'], reverse=True)
out['projects'] = files[:10]
out['active_count'] = sum(1 for f in files if f['age_s'] < 3600)
print(json.dumps(out))
"""
        raw = ctx.run_python(remote_script)
        try:
            return json.loads(raw)
        except Exception:
            return {"error": "collect parse failed", "raw": raw[:200]}

    # ------------------------------------------------------------------
    def control(self, ctx, action, **kwargs):
        if action == "status":
            return {"ok": True, "data": self.collect(ctx)}
        if action == "tmux":
            # 向 tmux 会话注入命令（继续/打断 agent）
            session = kwargs.get("session", "")
            cmd = kwargs.get("cmd", "")
            if not session:
                return {"ok": False, "message": "需要 session 名"}
            return ctx.tmux_send(session, cmd)
        if action == "restart":
            return ctx.restart_process("claude")
        return {"ok": False, "message": f"claude_code 不支持 action={action}"}

