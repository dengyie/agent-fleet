"""Atomic directory publication that never replaces an existing entry."""
from __future__ import annotations

import ctypes
import errno
import os
import sys
from pathlib import Path


def rename_at_no_replace(source: str, target: str, *, directory_fd: int) -> None:
    """Publish an entry relative to one pinned POSIX directory descriptor."""
    if any(not name or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name
           for name in (source, target)):
        raise ValueError("invalid publication entry")
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = getattr(libc, "renameatx_np", None)
        flags = 0x00000004
    elif sys.platform.startswith("linux"):
        rename = getattr(libc, "renameat2", None)
        flags = 0x00000001
    else:
        raise OSError(errno.ENOTSUP, "no-replace rename unavailable")
    if rename is None:
        raise OSError(errno.ENOTSUP, "no-replace rename unavailable")
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    if rename(directory_fd, os.fsencode(source), directory_fd, os.fsencode(target), flags) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), target)


def rename_directory_no_replace(stage: Path, target: Path) -> None:
    """Fail closed if the platform cannot provide exclusive directory rename."""
    if os.name == "nt":
        os.rename(stage, target)
        return
    source, destination = os.fsencode(stage), os.fsencode(target)
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = getattr(libc, "renamex_np", None)
        if rename is None:
            raise OSError(errno.ENOTSUP, "no-replace rename unavailable")
        rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        result = rename(source, destination, 0x00000004)
    elif sys.platform.startswith("linux"):
        rename = getattr(libc, "renameat2", None)
        if rename is None:
            raise OSError(errno.ENOTSUP, "no-replace rename unavailable")
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        result = rename(-100, source, -100, destination, 0x00000001)
    else:
        raise OSError(errno.ENOTSUP, "no-replace rename unavailable")
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), os.fspath(target))
