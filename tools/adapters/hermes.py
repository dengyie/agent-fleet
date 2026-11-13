"""tools/adapters/hermes.py — Hermes adapter

Hermes CLI 无统一非交互任务入口，default_command=None：
必须在 runner.yaml agents.hermes.command 显式配置本机命令模板。
"""
from tools.adapters.base import BaseAdapter


class HermesAdapter(BaseAdapter):
    name = "hermes"
    default_command = None