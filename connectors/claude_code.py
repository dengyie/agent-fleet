"""connectors/claude_code.py — Claude Code 连接器

采集: ~/.claude/projects/<项目>/*.jsonl 会话日志（最近写入的文件元数据）
只读采集，不做管控（push-only）
"""

import json

from .base import BaseConnector


class ClaudeCodeConnector(BaseConnector):
    TYPE = "claude_code"

    # ------------------------------------------------------------------
    def collect(self, ctx):
        remote_script = r"""
import json, os, glob, time, shutil
base = os.path.expanduser('~/.claude/projects')
out = {'installed': bool(shutil.which('claude')) or os.path.isdir(base)}
if not out['installed']:
    print(json.dumps(out)); raise SystemExit(0)
files = []
try:
    # 实际布局是 projects/<项目 slug>/<session>.jsonl（两层）
    for fp in glob.glob(os.path.join(base, '*', '*.jsonl')):
        try:
            st = os.stat(fp)
        except OSError:
            continue
        files.append({'file': os.path.relpath(fp, base), 'mtime': st.st_mtime,
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
