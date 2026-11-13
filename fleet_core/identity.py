"""fleet_core/identity.py — 进程身份：``pid`` + 启动标记。

单独一个 pid 不能证明「还是原来那个进程」（pid 会被复用）。锁持有者、
运行时回执、客户端更新事务都记录 :class:`ProcessIdentity`，判断存活时同时
比对启动标记；标记取自 :func:`fleet_core.process.process_group.process_start_identity`
（Linux ``/proc`` starttime、Windows creation time、macOS ``ps lstart``）。
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from fleet_core.process.process_group import pid_alive, process_start_identity

_MAX_MARKER = 128


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_marker: str

    @classmethod
    def current(cls) -> ProcessIdentity:
        # 本进程的启动标记不会变；按 pid 缓存（fork 后 pid 变化自动失效），
        # 避免 macOS 每次取锁都起一个 ps 子进程。
        global _CURRENT
        pid = os.getpid()
        if _CURRENT is None or _CURRENT.pid != pid:
            _CURRENT = cls(pid, process_start_identity(pid) or "")
        return _CURRENT

    @classmethod
    def of(cls, pid: int) -> ProcessIdentity | None:
        marker = process_start_identity(pid)
        return None if marker is None else cls(int(pid), marker)

    @classmethod
    def from_dict(cls, value: Any) -> ProcessIdentity | None:
        if not isinstance(value, Mapping):
            return None
        pid, marker = value.get("pid"), value.get("startMarker")
        if (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
                or not isinstance(marker, str) or len(marker) > _MAX_MARKER):
            return None
        return cls(pid, marker)

    def to_dict(self) -> dict[str, Any]:
        return {"pid": self.pid, "startMarker": self.start_marker}

    def alive(self) -> bool | None:
        """True=同一进程仍在；False=已退出或 pid 已被复用；None=无法判断。"""
        live = pid_alive(self.pid)
        if live is False:
            return False
        marker = process_start_identity(self.pid)
        if marker is None or not self.start_marker:
            return None
        return marker == self.start_marker


_CURRENT: ProcessIdentity | None = None
