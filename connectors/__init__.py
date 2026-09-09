# connectors/__init__.py — 连接器注册表与工厂

from .base import BaseConnector
from .hermes import HermesConnector
from .claude_code import ClaudeCodeConnector
from .codex import CodexConnector
from .pi import PiConnector

# 注册表：TYPE → Connector 类
# 注意：文件里有实现但没进注册表的 connector 不会被调度（历史坑：claude_code）。
_REGISTRY = {
    c.TYPE: c
    for c in (HermesConnector, ClaudeCodeConnector, CodexConnector, PiConnector)
}


def register(cls):
    """装饰器：注册新的 connector 类"""
    _REGISTRY[cls.TYPE] = cls
    return cls


def create(agent_type):
    """工厂：按类型创建 connector 实例。未知类型回退到 generic 行为。"""
    cls = _REGISTRY.get(agent_type)
    if cls is None:
        from . import generic
        return generic.GenericConnector(agent_type)
    return cls()


def available_types():
    return sorted(_REGISTRY.keys())
