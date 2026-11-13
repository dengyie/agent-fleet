"""tools/supervisor — 受管 CLI 进程生命周期（本地权威，Task 8）。

Supervisor 在 Agent 本地启动 allowlisted CLI、绑定 session/attempt/process-group，
并只执行固定枚举的 pause/resume/terminate/quarantine/cancel_attempt 动作。
本包不建立反向连接、不注入 stdin、不执行任意 shell 字符串。
没有归属证明的既有进程永不被自动 adoption。
"""
from .model import (  # noqa: F401
    GROUP_CAPABILITY_CGROUP,
    GROUP_CAPABILITY_PGROUP,
    ManagedSessionManifest,
    manifest_public,
    new_opaque_id,
    validate_opaque,
)

__all__ = [
    "GROUP_CAPABILITY_CGROUP",
    "GROUP_CAPABILITY_PGROUP",
    "ManagedSessionManifest",
    "manifest_public",
    "new_opaque_id",
    "validate_opaque",
]

# ``tools.supervisor.supervisor`` is imported lazily by callers so that the
# model/kernel can be reasoned about independently of the platform backend.
