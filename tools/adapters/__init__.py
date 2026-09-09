"""tools/adapters — 任务执行器注册表（仿 connectors 工厂模式）"""
from tools.adapters.base import AdapterResult, BaseAdapter
from tools.adapters.codex import CodexAdapter

_REGISTRY = {}


def register(cls):
    _REGISTRY[cls.name] = cls
    return cls


register(CodexAdapter)

# claude_code / hermes / pi 按注册表顺序 register
from tools.adapters.claude_code import ClaudeCodeAdapter  # noqa: E402
from tools.adapters.hermes import HermesAdapter  # noqa: E402
from tools.adapters.pi import PiAdapter  # noqa: E402
register(ClaudeCodeAdapter)
register(HermesAdapter)
register(PiAdapter)


def create(agent_type, command=None, timeout_s=1800):
    cls = _REGISTRY.get(agent_type)
    if cls is None:
        raise ValueError(f"未知 adapter 类型: {agent_type}")
    return cls(command=command, timeout_s=timeout_s)
