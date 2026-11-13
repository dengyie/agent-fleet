"""fleet_core/process/process_group.py — bounded_process 与 supervisor 共用的进程组原语。

* ``posix_group_alive`` / ``signal_group`` / ``posix_group_only_zombies``：以
  ``os.killpg`` 判断/信号整组（不是只看 leader），并识别只剩僵尸的组；
* ``process_start_identity``：pid 的启动身份（用于跨实例校验 pid 未被复用）；
* ``LineAssembler``：有界行组装——超长行保留前缀并显式标注被截掉的字节数，
  绝不静默丢弃。

POSIX-only 调用全部有 ``os.name`` 守卫；Windows 由调用方走 Job / 进程树。
"""
from __future__ import annotations

import os
import signal
import subprocess

_SIGKILL = getattr(signal, "SIGKILL", 9)
_SIGTERM = getattr(signal, "SIGTERM", 15)

#: 单行上限（字节）。超出部分丢弃但计数，行尾附 ``…[truncated N bytes]``。
MAX_LINE_BYTES = 1 << 20


def posix_group_alive(pgid: int) -> bool:
    """组内是否仍有任何成员（含已脱离 leader 的孙进程）。无法确认按存活处理。"""
    if os.name == "nt":
        raise OSError("process groups are POSIX-only")
    try:
        os.killpg(int(pgid), 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # EPERM 等：不能证明已消失
    return True


def signal_group(pgid: int, sig: int) -> bool:
    if os.name == "nt":
        return False
    try:
        os.killpg(int(pgid), sig)
    except OSError:
        return False
    return True


def posix_group_only_zombies(pgid: int) -> bool:
    """组内成员是否已全部是僵尸（已退出、仅等父进程回收）。无法确认时返回 False。

    跨实例控制时 leader 不是本进程的子进程，无法 ``wait`` 回收；僵尸会让
    ``killpg(pgid, 0)`` 仍然成功，需要按进程状态区分「真存活」与「已退出」。
    """
    if os.name == "nt":
        return False
    pgid = int(pgid)
    states: list[str] = []
    if os.path.isdir("/proc/self"):
        try:
            for name in os.listdir("/proc"):
                if not name.isdigit():
                    continue
                try:
                    with open(f"/proc/{name}/stat", "rb") as fh:
                        raw = fh.read()
                except OSError:
                    continue
                fields = raw[raw.rindex(b")") + 2:].split()
                if int(fields[2]) == pgid:
                    states.append(fields[0].decode("ascii", "replace"))
        except (OSError, ValueError, IndexError):
            return False
    else:
        ps = next((p for p in ("/bin/ps", "/usr/bin/ps") if os.path.isfile(p)), None)
        if ps is None:
            return False
        try:
            from fleet_core.process.local_process import run as run_local
            done = run_local([ps, "-A", "-o", "pgid=,stat="],
                             capture_output=True, text=True, timeout=3,
                             stdin=subprocess.DEVNULL, check=False,
                             env={"PATH": "/bin:/usr/bin", "LC_ALL": "C"})
        except (OSError, subprocess.SubprocessError):
            return False
        if done.returncode != 0:
            return False
        for row in (done.stdout or "").splitlines():
            parts = row.split()
            if len(parts) >= 2 and parts[0].isdigit() and int(parts[0]) == pgid:
                states.append(parts[1])
    return bool(states) and all(state.startswith("Z") for state in states)


def pid_alive(pid: int) -> bool | None:
    """True/False；None = 无法判断。Windows 绝不用 ``os.kill(pid, 0)``（那是 CTRL_C）。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        return windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def process_start_identity(pid: int) -> str | None:
    """pid 的启动身份字符串（同一 pid 被复用后必然不同）；读不到返回 None。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if os.name == "nt":
        return _windows_creation_time(pid)
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            raw = fh.read()
        # comm 可能含空格/括号：从最后一个 ')' 之后切字段；starttime 是第 22 列。
        fields = raw[raw.rindex(b")") + 2:].split()
        return "linux:" + fields[19].decode("ascii")
    except (OSError, ValueError, IndexError):
        pass
    ps = next((p for p in ("/bin/ps", "/usr/bin/ps") if os.path.isfile(p)), None)
    if ps is None:
        return None
    try:
        from fleet_core.process.local_process import run as run_local
        done = run_local([ps, "-o", "lstart=", "-p", str(pid)],
                         capture_output=True, text=True, timeout=3,
                         stdin=subprocess.DEVNULL, check=False,
                         env={"PATH": "/bin:/usr/bin", "LC_ALL": "C", "TZ": "UTC"})
    except (OSError, subprocess.SubprocessError):
        return None
    text = (done.stdout or "").strip()
    if done.returncode != 0 or not text:
        return None
    return "ps:" + " ".join(text.split())


def _windows_creation_time(pid: int) -> str | None:
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
                return None
            created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            return f"win:{created}"
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def windows_pid_alive(pid: int) -> bool | None:
    """Windows-only liveness: True/False, None = unknown (e.g. access denied)."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
        ]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x00100000, False, int(pid))  # SYNCHRONIZE
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:  # ERROR_INVALID_PARAMETER: PID no longer exists
                return False
            return None
        try:
            result = kernel32.WaitForSingleObject(handle, 0)
            if int(result) == 0x00000102:  # WAIT_TIMEOUT
                return True
            if int(result) == 0x00000000:  # WAIT_OBJECT_0
                return False
            return None
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


class LineAssembler:
    """有界行组装器：内存 ≤ ``max_line`` + 一个读块，超长行截断并标注。"""

    def __init__(self, max_line: int = MAX_LINE_BYTES):
        self._max = int(max_line)
        self._buf = bytearray()
        self._dropped = 0  # 当前行已丢弃的字节数
        self.truncated_lines = 0

    def _emit(self, raw: bytes) -> str:
        text = raw.decode("utf-8", "replace").rstrip("\r")
        if self._dropped:
            self.truncated_lines += 1
            text += f"…[truncated {self._dropped} bytes]"
            self._dropped = 0
        return text

    def _absorb(self, part: bytes) -> None:
        room = self._max - len(self._buf)
        if room > 0:
            self._buf.extend(part[:room])
        self._dropped += max(0, len(part) - max(0, room))

    def feed(self, chunk: bytes) -> list[str]:
        lines: list[str] = []
        start = 0
        while True:
            idx = chunk.find(b"\n", start)
            if idx < 0:
                self._absorb(chunk[start:])
                return lines
            self._absorb(chunk[start:idx])
            lines.append(self._emit(bytes(self._buf)))
            self._buf.clear()
            start = idx + 1

    def flush(self) -> list[str]:
        if not self._buf and not self._dropped:
            return []
        line = self._emit(bytes(self._buf))
        self._buf.clear()
        return [line]


__all__ = [
    "LineAssembler",
    "MAX_LINE_BYTES",
    "pid_alive",
    "posix_group_alive",
    "process_start_identity",
    "signal_group",
    "posix_group_only_zombies",
    "windows_pid_alive",
]
