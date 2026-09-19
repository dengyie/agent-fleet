"""connectors/zcode.py — ZCode agent 连接器

采集: ~/.zcode/cli/rollout/**/model-io-sess_*.jsonl（ZCode 会话模型 IO 日志）
      只读目录元数据（mtime/size），不解析内容，避免隐私与性能开销。
管控: status 查看活动会话；restart 暂不支持（由 CLI 自管）。
"""

from .base import JsonlConnector


class ZcodeConnector(JsonlConnector):
    TYPE = "zcode"
    SESSION_BASE = '~/.zcode/cli/rollout'
