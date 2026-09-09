"""hub/state.py — 状态模型与历史存储（兼容门面）

用 JSONL 追加模式存储每次采集快照，天然支持增量 diff 与回看历史。
state/<machine>.jsonl 每行一个 {ts, agents: {...}} 快照。

同时维护 state/<machine>.current.json 为最新快照（供 web 快速读取）。

本模块自 v2 起成为观测仓库的兼容门面：真正的持久化逻辑迁移到
``hub.infrastructure.state_repository.JsonlObservationRepository``，并以
legacy ``STATE_DIR`` 构造默认仓库。新代码应直接注入仓库实例，不应再依赖
本模块的全局路径。

所有公开签名与常量保持与历史版本兼容（save_snapshot / read_current /
read_history / diff_previous / rotate_if_needed / _mach_file /
MAX_JSONL_BYTES / ROTATE_KEEP_LINES / STATE_DIR / FLEET_HOME）。
"""

from pathlib import Path

from hub.infrastructure.state_repository import (
    JsonlObservationRepository,
    LegacyStateStoreAdapter,
    MAX_JSONL_BYTES,
    ROTATE_KEEP_LINES,
)

FLEET_HOME = Path(__file__).resolve().parent.parent
STATE_DIR = FLEET_HOME / "state"

# Default repository for the legacy facade, rebuilt lazily whenever the
# module-level ``STATE_DIR`` / ``MAX_JSONL_BYTES`` / ``ROTATE_KEEP_LINES`` are
# reassigned (existing tests and ``bootstrap`` repoint them at runtime).
_saved_state = None
_state_repository = None
_adapter = None


def _default_repository():
    """Return the cached default repository for the current module settings."""
    global _saved_state, _state_repository, _adapter
    if (
        _state_repository is None
        or _saved_state != (STATE_DIR, MAX_JSONL_BYTES, ROTATE_KEEP_LINES)
    ):
        _state_repository = JsonlObservationRepository(
            STATE_DIR,
            max_jsonl_bytes=MAX_JSONL_BYTES,
            keep_lines=ROTATE_KEEP_LINES,
        )
        _adapter = LegacyStateStoreAdapter(_state_repository)
        _saved_state = (STATE_DIR, MAX_JSONL_BYTES, ROTATE_KEEP_LINES)
    return _state_repository


def _default_adapter():
    _default_repository()
    return _adapter


def rotate_if_needed(machine):
    """JSONL 超过 MAX_JSONL_BYTES 时截断为最近 ROTATE_KEEP_LINES 行（原子替换）。

    兼容别名：委托给默认仓库的旋转实现，保持公开签名与历史版本一致。
    """
    repo = _default_repository()
    with repo._locks[machine]:
        return repo._rotate_if_needed(machine, repo._mach_file(machine))

# 旧私有名保留为别名，避免依赖 private 符号的调用方失效。
_rotate_if_needed = rotate_if_needed


def _mach_file(machine, ext="jsonl"):
    return _default_repository()._mach_file(machine, ext)


def save_snapshot(machine, snapshot: dict):
    """追加快照到 JSONL + 原子替换 current JSON。"""
    _default_adapter().save_snapshot(machine, snapshot)


def read_current(machine):
    return _default_adapter().read_current(machine)


def read_history(machine, limit=50):
    return _default_adapter().read_history(machine, limit)


def diff_previous(machine, snapshot):
    """与上次快照对比，返回 changed 字段列表（用于触发事件/通知）"""
    return _default_adapter().diff_previous(machine, snapshot)


def machines():
    return _default_adapter().machines()
