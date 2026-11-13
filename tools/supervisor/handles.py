"""tools/supervisor/handles.py — 私有进程句柄与跨实例句柄绑定。

从 ``supervisor.py`` 拆出：``_Bound`` / ``_AttachedHandle`` / ``_OwnerHandle``
私有句柄、``_ManagedEntry``，以及 ``Supervisor`` 的 owner 记录 / 句柄查找
方法（``HandlesMixin``）。raw pid/cwd/env 只存在于这里的私有对象上，永不进入
manifest 或公开输出。
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from fleet_core.process.process_group import pid_alive, posix_group_alive, process_start_identity
from tools.supervisor.model import (TERMINAL_STATES, ManagedSessionManifest,
                                    validate_opaque)


class UnknownSessionError(KeyError):
    """Raised for a session id the Supervisor has no manifest for."""

    def __init__(self, session_id: str):
        super().__init__(f"unknown session: {session_id}")


@dataclass
class _Bound:
    """Mutable handle bundle for an owned process group.

    The durable manifest never stores the raw pid/cwd/env/argv so a
    recovered Supervisor cannot auto-restart it.  Optional native-resume
    identity here is in-memory only and is also copied onto ``_ManagedEntry``
    so it survives ``_release_handle``.
    """

    proc: Any = None
    group_id: str = ""
    native_file_path: str | None = None
    cwd: str | None = None
    env: dict[str, str] | None = None
    resume_token: str | None = None
    exe_path: str | None = None
    _abuf: bytes = b""
    _dropped: int = 0
    _truncated_lines: int = 0
    _lines: Any = None
    _queue: Any = None
    _reader_thread: Any = None
    _reader_started: bool = False
    _reader_eof: bool = False
    _reader_closed: bool = False
    _dropped_chunks: int = 0
    # Windows only: kill-on-close Job Object owning the whole spawned tree.
    _job: Any = None


@dataclass
class _AttachedHandle:
    """Private handle for an EXISTING local process bound via ``attach_to_*``.

    ``pid`` / ``pgid`` / ``started_at`` / ``exe_path`` plus optional native
    resume identity (``native_file_path`` / ``cwd`` / ``env`` /
    ``resume_token``) live ONLY here — never on the durable manifest, never
    in any public status/JSON row.  ``proc`` stays ``None`` because the
    process was not spawned by the Supervisor, so there is no pipe/Popen to
    read; real backends signal it by the *process group* id
    (``os.killpg(pgid, …)``).
    """

    pid: int
    pgid: int
    started_at: str
    exe_path: str
    proc: Any = None
    native_file_path: str | None = None
    cwd: str | None = None
    env: dict[str, str] | None = None
    resume_token: str | None = None
    _abuf: bytes = b""


@dataclass
class _OwnerHandle(_AttachedHandle):
    """跨实例控制句柄：由另一个 Supervisor 实例 launch、经 owner 记录校验。

    与 adoption 一样按进程组（pgid）发信号、不持有管道；但身份来自本机
    owner 记录（pid + 启动身份），guard 失败时只丢弃句柄、绝不 detach /
    删除 manifest。
    """

    start_identity: str = ""


@dataclass
class _ManagedEntry:
    manifest: ManagedSessionManifest
    handle: Any = None  # GroupOps handle (live only after launch/resume)
    probe: Any = None   # the LIVE-identity reader used at attach (Task 9)
    # Private native-resume identity.  Never persisted, never public.
    # Survives ``_release_handle`` so a naturally finished session can still
    # be resumed into the same native transcript (not SIGCONT, not /tmp).
    cwd: str | None = None
    env: dict[str, str] | None = None
    native_file_path: str | None = None
    resume_token: str | None = None
    exe_path: str | None = None
    control_lock: Any = field(default_factory=threading.RLock, repr=False,
                              compare=False)
    control_epoch: int = 0
    resume_pending: object | None = field(default=None, repr=False,
                                          compare=False)


def _stamp_private_identity(handle, entry) -> None:
    """Copy private native-resume fields between entry and live handle."""
    if entry is None:
        return
    names = ("cwd", "env", "native_file_path", "resume_token", "exe_path")
    for name in names:
        val = getattr(entry, name, None)
        if val is None and handle is not None:
            val = getattr(handle, name, None)
        if val is None:
            continue
        if name == "env" and isinstance(val, Mapping):
            val = dict(val)
        try:
            setattr(entry, name, val)
        except Exception:
            pass
        if handle is None:
            continue
        try:
            setattr(handle, name, val)
        except Exception:
            pass


class HandlesMixin:
    """``Supervisor`` 的句柄管理：owner 记录、磁盘 manifest 回读与跨实例绑定。"""

    # ---- cross-instance ownership ------------------------------------------ #

    def _owner_path(self, session_id: str) -> Path:
        return self._manifest_dir / "owners" / f"{session_id}{self.MANIFEST_SUFFIX}"

    def _write_owner(self, session_id: str, handle) -> None:
        """Best-effort：把 launch 实例的进程身份落到共享 manifest_dir。

        owner 记录与 manifest 分目录存放（``recover`` 的 ``*.json`` glob 不会
        误读），只在本机私有目录，永不进入任何公开输出。
        """
        try:
            ident = self._ops.owner_identity(handle)
        except Exception:
            ident = None
        if not ident:
            return
        target = self._owner_path(session_id)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(ident))
            os.replace(str(tmp), str(target))
        except OSError:
            pass

    def _drop_owner(self, session_id: str) -> None:
        try:
            self._owner_path(session_id).unlink()
        except OSError:
            pass

    def _read_disk_manifest(self, session_id: str) -> ManagedSessionManifest | None:
        try:
            validate_opaque(session_id, "session_id")
        except ValueError:
            return None
        path = self._manifest_dir / f"{session_id}{self.MANIFEST_SUFFIX}"
        try:
            m = ManagedSessionManifest.from_json(path.read_text())
        except Exception:
            return None
        if (m.session_id != session_id or not m.process_group_id
                or m.machine_id != self._machine_id):
            return None
        return m

    def _refresh_from_disk(self, session_id: str) -> _ManagedEntry | None:
        """另一实例 launch 的会话：从共享 manifest_dir 读入（不持久化、无句柄）。"""
        if session_id in self._adoption_revoked:
            return None
        m = self._read_disk_manifest(session_id)
        if m is None:
            return None
        entry = self._entries.setdefault(session_id, _ManagedEntry(m, None))
        return entry

    def _rebind_owner(self, session_id: str, entry: _ManagedEntry):
        """无句柄会话的跨实例绑定。返回 ``(handle, code)``。

        * 可证明仍是同一进程（pid 存活 + 启动身份一致 + pgid 一致）→ 句柄；
        * 可证明 leader 与整组都已消失 → ``(None, "no_live_process")``；
        * 磁盘 manifest 已是终态 → 同步终态，``(None, <state>)``；
        * 其余一律 ``(None, "liveness_unknown")``——绝不当作已结束。
        """
        m = entry.manifest
        disk = self._read_disk_manifest(session_id)
        if disk is not None and disk.state in TERMINAL_STATES:
            m.state, m.reason, m.updated_at = disk.state, disk.reason, disk.updated_at
            return None, m.state
        try:
            raw = json.loads(self._owner_path(session_id).read_text())
            pid, pgid, start = int(raw["pid"]), int(raw["pgid"]), str(raw["start"])
        except FileNotFoundError:
            return None, "liveness_unknown"
        except (OSError, ValueError, TypeError, KeyError):
            return None, "liveness_unknown"
        if pid <= 0 or pgid <= 0 or not start:
            return None, "liveness_unknown"
        leader = pid_alive(pid)
        if leader is None:
            return None, "liveness_unknown"
        if leader:
            if process_start_identity(pid) != start:
                return None, "liveness_unknown"  # pid 已被复用或身份不可读
            if os.name != "nt":
                try:
                    if os.getpgid(pid) != pgid:
                        return None, "liveness_unknown"
                except OSError:
                    return None, "liveness_unknown"
            handle = _OwnerHandle(pid=pid, pgid=pgid, started_at="",
                                  exe_path="", start_identity=start)
            entry.handle = handle
            return handle, ""
        if os.name == "nt":
            return None, "no_live_process"
        # leader 已不在：组内仍有成员时无法证明 pgid 未被复用 → unknown。
        if posix_group_alive(pgid):
            return None, "liveness_unknown"
        return None, "no_live_process"

    def _live_handle(self, session_id: str, entry: _ManagedEntry):
        """``(handle, code)``：已有句柄直接返回，否则尝试跨实例绑定。"""
        if entry.handle is not None:
            return entry.handle, ""
        return self._rebind_owner(session_id, entry)

    def _handle_of(self, session_id: str):
        entry = self._entries.get(session_id)
        if entry is None:
            raise UnknownSessionError(session_id)
        return entry.handle

    def _release_handle(self, session_id: str) -> None:
        """Drop the private OS handle so no raw pid/cwd/argv remains live."""
        entry = self._entries.get(session_id)
        if entry is not None:
            entry.handle = None
