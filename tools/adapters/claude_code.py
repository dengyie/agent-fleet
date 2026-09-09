"""tools/adapters/claude_code.py — Claude Code CLI adapter"""
from tools.adapters.base import BaseAdapter


class ClaudeCodeAdapter(BaseAdapter):
    name = "claude_code"
    # claude -p：非交互 print 模式
    default_command = ["claude", "-p", "{instruction}"]