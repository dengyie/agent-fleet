"""connectors/pi.py — pi agent 连接器

采集: ~/.pi/agent/sessions/<encoded-cwd>/*.jsonl（pi CLI 会话日志）
      只读目录元数据（mtime/size），不解析内容，避免隐私与性能开销。
管控: status 查看活动会话；restart 暂不支持（由 CLI 自管）。
"""

from .base import JsonlConnector


class PiConnector(JsonlConnector):
    TYPE = "pi"
    SESSION_BASE = '~/.pi/agent/sessions'
