"""tools/adapters/claude_code.py — Claude Code CLI adapter"""
from tools.adapters.base import BaseAdapter


class ClaudeCodeAdapter(BaseAdapter):
    name = "claude_code"
    # claude -p：非交互 print 模式
    default_command = ["claude", "-p", "{instruction}"]
    # clap / commander 均支持 "--" 结束选项解析
    end_of_options = True
    env_keys = ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
                "CLAUDE_CONFIG_DIR")
