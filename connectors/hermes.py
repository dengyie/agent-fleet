"""connectors/hermes.py — Hermes agent 连接器

采集: 读取远端 ~/.hermes/gateway_state.json + sessions/sessions.json
只读采集，不做管控（push-only）
"""

import json

from .base import BaseConnector


class HermesConnector(BaseConnector):
    TYPE = "hermes"

    # ------------------------------------------------------------------
    def collect(self, ctx):
        # 远端执行：一次性读取 Hermes 状态 + 会话
        remote_script = r"""
import json, os, sys
base = os.path.expanduser('~/.hermes')
out = {'installed': os.path.isdir(base)}
if not out['installed']:
    print(json.dumps(out)); sys.exit(0)
gw = {}
try:
    gw = json.load(open(os.path.join(base, 'gateway_state.json')))
except Exception:
    gw = {}
sessions = []
try:
    d = json.load(open(os.path.join(base, 'sessions/sessions.json')))
    for k, v in d.items():
        if k == '_README' or not isinstance(v, dict):
            continue
        sessions.append({
            'key': k,
            'session_id': str(v.get('session_id', '')),
            'display_name': v.get('display_name'),
            'updated_at': v.get('updated_at', ''),
            'created_at': v.get('created_at', '')
        })
except Exception:
    sessions = []
out.update({
    'gateway_state': gw.get('gateway_state', 'unknown'),
    'active_agents': gw.get('active_agents', 0),
    'platforms': list(gw.get('platforms', {}).keys()),
    'pid': gw.get('pid'),
    'sessions': sessions[:30],
    'session_count': len(sessions),
})
print(json.dumps(out))
"""
        raw = ctx.run_python(remote_script)
        try:
            return json.loads(raw)
        except Exception:
            return {"error": "collect parse failed", "raw": raw[:200]}

    # ------------------------------------------------------------------
    def detect(self, ctx):
        # 是否存在 ~/.hermes
        return ctx.run("test -d ~/.hermes && echo yes || echo no").strip() == "yes"
