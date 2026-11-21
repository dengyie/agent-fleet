"""tools/adapters/pi.py — pi CLI adapter"""
from tools.adapters.base import BaseAdapter
from agent_profiles import default_command


class PiAdapter(BaseAdapter):
    name = "pi"
    # pi -p：非交互 print 模式
    default_command = list(default_command('pi'))
    end_of_options = True
    env_keys = ('PI_CODING_AGENT_DIR', 'PI_OFFLINE', 'ANTHROPIC_API_KEY',
                'ANTHROPIC_AUTH_TOKEN', 'OPENAI_API_KEY', 'GEMINI_API_KEY')
