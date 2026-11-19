"""tools/adapters/claude_code.py — Claude Code CLI adapter"""
from tools.adapters.base import BaseAdapter
from agent_profiles import default_command


class ClaudeCodeAdapter(BaseAdapter):
    name = "claude_code"
    # claude -p：非交互 print 模式
    default_command = list(default_command("claude_code"))
    # clap / commander 均支持 "--" 结束选项解析
    end_of_options = True
    env_keys = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
                "CLAUDE_CONFIG_DIR")
