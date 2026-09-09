"""tools/adapters/pi.py — pi CLI adapter"""
from tools.adapters.base import BaseAdapter


class PiAdapter(BaseAdapter):
    name = "pi"
    # pi -p：非交互 print 模式
    default_command = ["pi", "-p", "{instruction}"]
