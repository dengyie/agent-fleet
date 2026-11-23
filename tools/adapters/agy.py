"""Google Antigravity CLI: native binary, headless NDJSON, exact conversation."""
from agent_profiles import default_command
from tools.adapters.base import BaseAdapter


class AgyAdapter(BaseAdapter):
    name = 'agy'
    default_command = list(default_command('agy'))
