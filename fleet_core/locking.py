"""fleet_core/locking.py — 可诊断的进程间互斥锁。

锁本体仍是 advisory 文件锁（POSIX ``flock`` / Windows ``msvcrt.locking``），
进程退出时由内核释放，不存在「死锁残留」。在此之上，持有者取锁成功后原子
写一份旁路元数据 ``<lock>.owner.json``（用途、进程身份、nonce、取锁时间），
释放时先删元数据再解锁，于是：

* :func:`inspect_lock` 能回答「谁拿着这把锁」；
* 取锁成功却发现元数据仍在 = 上一代持有者异常退出，只是元数据残留
  （``stale``），:func:`reclaim_stale` 只清理这一种；正被持有的锁一律拒绝，
  不提供强制解锁。

元数据绝不含凭据；``extra`` 只放调用方给的短字符串（如版本号）。
"""
from __future__ import annotations

import json
import os
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fleet_core.identity import ProcessIdentity

try:  # pragma: no cover - platform specific
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

try:  # pragma: no cover - platform specific
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None

OWNER_SUFFIX = ".owner.json"
OWNER_SCHEMA = 1
_POLL_S = 0.05
_MAX_OWNER_BYTES = 4096


class LockBusy(Exception):
    """另一个持有者正拿着锁；``owner`` 为其元数据（可能为 None）。"""

    def __init__(self, path: Path, owner: Mapping[str, Any] | None = None):
        super().__init__("lock_busy")
        self.path = path
        self.owner = dict(owner) if owner else None


def try_lock(handle) -> bool:
    """对已打开的文件句柄做一次非阻塞排他锁；被占用返回 False。"""
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except (BlockingIOError, PermissionError):
        return False
    except OSError as exc:
        # EACCES/EAGAIN/EDEADLK are contention on every supported platform.
        if exc.errno in (11, 13, 35, 36):
            return False
        raise


def unlock_and_close(handle) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    finally:
        try:
            handle.close()
        except OSError:
            pass


def owner_path(path: Path) -> Path:
    return path.with_name(path.name + OWNER_SUFFIX)


def read_owner(path: Path) -> dict[str, Any] | None:
    try:
        raw = owner_path(Path(path)).read_bytes()[:_MAX_OWNER_BYTES + 1]
    except OSError:
        return None
    if len(raw) > _MAX_OWNER_BYTES:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(value, fh, sort_keys=True, separators=(",", ":"))
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


@dataclass
class OwnedLock:
    """带持有者元数据的排他锁。``acquire`` 失败抛 :class:`LockBusy`。"""

    path: Path
    purpose: str
    extra: Mapping[str, str] = field(default_factory=dict)
    _handle: Any = field(default=None, init=False, repr=False)
    _nonce: str = field(default="", init=False, repr=False)

    def __post_init__(self):
        self.path = Path(self.path)

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self, timeout: float = 0.0) -> OwnedLock:
        if self._handle is not None:
            raise RuntimeError("lock already held by this object")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        deadline = time.monotonic() + max(0.0, float(timeout))
        try:
            while not try_lock(handle):
                if time.monotonic() >= deadline:
                    raise LockBusy(self.path, read_owner(self.path))
                time.sleep(_POLL_S)
        except BaseException:
            handle.close()
            raise
        self._handle = handle
        self._nonce = secrets.token_hex(8)
        record = {
            "schemaVersion": OWNER_SCHEMA,
            "purpose": str(self.purpose)[:64],
            "nonce": self._nonce,
            "acquiredAt": int(time.time()),
            **ProcessIdentity.current().to_dict(),
        }
        if self.extra:
            record["extra"] = {str(k)[:32]: str(v)[:128] for k, v in self.extra.items()}
        try:
            _atomic_write_json(owner_path(self.path), record)
        except OSError:
            pass  # 元数据只服务诊断；写不了不影响互斥本身
        return self

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        current = read_owner(self.path)
        if current is not None and current.get("nonce") == self._nonce:
            try:
                owner_path(self.path).unlink()
            except OSError:
                pass
        unlock_and_close(handle)

    def __enter__(self):
        # 允许 ``with lock.acquire(timeout=...)``：已持有就直接进入
        return self if self._handle is not None else self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False


@dataclass(frozen=True)
class LockState:
    path: Path
    state: str  # absent | free | held | stale
    owner: dict[str, Any] | None = None
    owner_alive: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "state": self.state,
                "owner": self.owner, "ownerAlive": self.owner_alive}


def inspect_lock(path) -> LockState:
    """只读诊断（会短暂试锁一次）：absent / free / held / stale。"""
    path = Path(path)
    if not path.exists():
        return LockState(path, "absent")
    owner = read_owner(path)
    try:
        handle = open(path, "a+b")
    except OSError:
        return LockState(path, "held", owner, None)
    try:
        acquired = try_lock(handle)
    except OSError:
        acquired = False
    if not acquired:
        handle.close()
        ident = ProcessIdentity.from_dict(owner)
        return LockState(path, "held", owner, ident.alive() if ident else None)
    try:
        owner = read_owner(path)
        return LockState(path, "stale" if owner is not None else "free", owner,
                         False if owner is not None else None)
    finally:
        unlock_and_close(handle)


def reclaim_stale(path) -> bool:
    """只清理「锁可取但元数据残留」的上一代记录；正被持有时返回 False。"""
    path = Path(path)
    if not path.exists():
        return False
    handle = open(path, "a+b")
    try:
        if not try_lock(handle):
            handle.close()
            return False
    except OSError:
        handle.close()
        return False
    try:
        try:
            owner_path(path).unlink()
            return True
        except FileNotFoundError:
            return False
    finally:
        unlock_and_close(handle)
