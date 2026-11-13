"""connectors/codex.py — Codex agent 连接器

采集: ~/.codex/sessions/**/*.jsonl（Codex CLI 的 rollout 会话日志）
      只读目录元数据（mtime/size），不解析内容，避免隐私与性能开销。
管控: status 查看活动会话；restart 暂不支持（由 CLI 自管）。
"""

from .base import JsonlConnector


class CodexConnector(JsonlConnector):
    TYPE = "codex"
    SESSION_BASE = '~/.codex/sessions'
