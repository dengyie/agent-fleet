"""tools/supervisor/group_ops.py — 进程组后端（``GroupOps``）与有界输出缓冲。

从 ``supervisor.py`` 拆出：平台进程组抽象（POSIX killpg / cgroup 检测、
Windows taskkill / 挂起）、deadline-safe 的 ``pump`` 与有界行缓冲。
``tools.supervisor.supervisor`` 继续 re-export 这里的全部名字。
"""
from __future__ import annotations

import os
import platform as _platform_mod
import queue
import select
import signal
import subprocess
import threading
import time
from collections import deque
from typing import Any, Mapping, Sequence

from fleet_core.process.local_process import popen as popen_local
from fleet_core.process.local_process import run as run_local
from fleet_core.process.process_group import (posix_group_alive, posix_group_only_zombies,
                                              process_start_identity, windows_pid_alive)
from tools.supervisor.handles import _AttachedHandle, _Bound
from tools.supervisor.model import GROUP_CAPABILITY_CGROUP, GROUP_CAPABILITY_PGROUP

# Raw byte-buffer cap per process group handle (drops oldest bytes beyond
# this, so a child emitting a huge line-free stream cannot OOM the Reader).
_ABUF_MAX = 1 << 20
# 单行保留上限：行 head + 一个读块恒 ≤ _ABUF_MAX。
_LINE_MAX = _ABUF_MAX - 65536

_SIGTERM = getattr(signal, "SIGTERM", 15)
_SIGKILL = getattr(signal, "SIGKILL", 9)
_SIGSTOP = getattr(signal, "SIGSTOP", 19)
_SIGCONT = getattr(signal, "SIGCONT", 18)


class GroupError(RuntimeError):
    """Stable, bounded process-group error (no raw text)."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail[:200]
        super().__init__(code)


# --------------------------------------------------------------------------- #
# GroupOps — smallest possible platform abstraction over a process group.
# --------------------------------------------------------------------------- #

class GroupOps:
    """Contract for a supervised process-group backend."""

    def __init__(self, platform: str | None = None):
        self._platform = (platform or _platform_mod.system()).lower()

    # ---- capability --------------------------------------------------------

    def capability(self):
        """Return ``(capability_level, version)``."""
        if self._platform == "linux" and self._cgroup_available():
            return (GROUP_CAPABILITY_CGROUP, "2")
        return (GROUP_CAPABILITY_PGROUP, "1")

    def _cgroup_available(self) -> bool:
        try:
            for p in ("/sys/fs/cgroup/cgroup.controllers",
                      "/sys/fs/cgroup/unified/cgroup.controllers"):
                if os.path.exists(p) and os.access(p, os.R_OK):
                    return True
        except OSError:
            return False
        return False

    # ---- spawn / query ----------------------------------------------------- #
    def create(self, argv: Sequence[str], cwd: str, env: Mapping[str, str]):
        raise NotImplementedError

    def attach(self, pid: int, started_at: str, exe_path: str):
        """Create the private handle for an EXISTING local process group."""
        raise NotImplementedError

    def proc_poll(self, handle) -> int | None:
        raise NotImplementedError

    def proc_wait(self, handle, timeout: float | None = None) -> int | None:
        raise NotImplementedError

    def group_alive(self, handle) -> bool:
        raise NotImplementedError

    def group_terminate(self, handle, sig: int) -> bool:
        """Best-effort signal to the whole group (returns False on failure)."""
        raise NotImplementedError

    def group_kill(self, handle, sig: int) -> bool:
        raise NotImplementedError

    def group_reap(self, handle, timeout: float | None = None) -> bool:
        raise NotImplementedError

    def escaped(self, handle) -> bool:
        return False

    def owner_identity(self, handle) -> dict[str, Any] | None:
        """Durable owner record ``{pid, pgid, start}`` for a spawned handle.

        供另一个 Supervisor 实例（control-client 进程）跨实例校验并控制本
        实例拉起的进程组。无真实进程的后端返回 None（不写记录）。
        """
        return None

    def close(self, handle) -> None:
        pass

    def pump(self, handle, timeout: float, on_line=None,
             should_abort=None) -> str:
        """Output drain with a bounded wait.

        Returns one of ``"line"`` (a line was delivered to ``on_line``),
        ``"eof"`` (pipe closed), ``"timeout"`` (no line within ``timeout``),
        ``"abort"`` (``should_abort`` became true).
        """
        time.sleep(max(0.001, min(0.05, float(timeout))))
        return "timeout"


# --------------------------------------------------------------------------- #
# Real POSIX backend (subprocess in a fresh session + os.killpg)
# --------------------------------------------------------------------------- #

class POSIXGroupOps(GroupOps):
    """Shared POSIX implementation (start_new_session + ``os.killpg``)."""

    _READ_CHUNK = 65536

    @staticmethod
    def _attached(handle) -> bool:
        """True iff ``handle`` binds an *attached* (not spawned) process."""
        return isinstance(handle, _AttachedHandle)

    def attach(self, pid: int, started_at: str, exe_path: str):
        """Bind an existing local group (pgid of the leader pid)."""
        pgid = os.getpgid(int(pid))
        return _AttachedHandle(pid=int(pid), pgid=int(pgid),
                               started_at=started_at, exe_path=exe_path)

    def create(self, argv, cwd, env):
        try:
            proc = popen_local(
                list(argv),
                cwd=cwd or "/tmp",
                env=_bounded_spawn_env(env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
                restore_signals=True,
                bufsize=0,
            )
        except OSError:
            raise GroupError("spawn_failed")
        return _Bound(proc=proc)

    def proc_poll(self, handle):
        if self._attached(handle):
            return None  # not our child; liveness lives on the group
        return handle.proc.poll()

    def proc_wait(self, handle, timeout=None):
        if self._attached(handle):
            return None
        try:
            return handle.proc.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            return None

    def group_alive(self, handle):
        if self._attached(handle):
            # liveness = the process GROUP still exists (no /proc deref).
            try:
                os.killpg(int(handle.pgid), 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                # macOS 对仅剩僵尸的组返回 EPERM：全员僵尸才视为已退出，否则按存活处理
                return not posix_group_only_zombies(int(handle.pgid))
            except OSError:
                return True  # cannot verify → assume alive
            # 非本进程子进程的 leader 无法回收：全员僵尸即视为已退出
            return not posix_group_only_zombies(int(handle.pgid))
        if handle.proc.poll() is None:
            return True
        # leader 已退出不代表组已空：仍在组内的孙进程（含 trap TERM 的）
        # 同样是存活成员，必须由 killpg(pgid, 0) 判定。
        return posix_group_alive(handle.proc.pid)

    def group_terminate(self, handle, sig):
        if self._attached(handle):
            try:
                os.killpg(int(handle.pgid), sig)
            except (ProcessLookupError, PermissionError, OSError):
                return False
            return True
        try:
            os.killpg(handle.proc.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return False
        return True

    def group_kill(self, handle, sig):
        return self.group_terminate(handle, sig)

    def group_reap(self, handle, timeout=None):
        if self._attached(handle):
            return True  # not our child — nothing to wait/reap
        budget = max(0.0, timeout or 0)
        try:
            handle.proc.wait(timeout=budget)
        except (subprocess.TimeoutExpired, OSError):
            return False
        # leader 已回收；组内残留成员（例如把 stdout 重定向走的孙进程）
        # 同样属于本次执行，必须一并 SIGKILL 并在有界时间内确认消失。
        pgid = handle.proc.pid
        if not posix_group_alive(pgid):
            return True
        try:
            os.killpg(pgid, _SIGKILL)
        except OSError:
            pass
        deadline = time.monotonic() + max(1.0, budget)
        while time.monotonic() < deadline:
            if not posix_group_alive(pgid):
                return True
            time.sleep(0.02)
        return not posix_group_alive(pgid)

    def owner_identity(self, handle):
        if self._attached(handle) or getattr(handle, "proc", None) is None:
            return None
        try:
            pid = int(handle.proc.pid)
            pgid = os.getpgid(pid)
        except (AttributeError, OSError, TypeError, ValueError):
            return None
        start = process_start_identity(pid)
        if start is None:
            return None
        return {"pid": pid, "pgid": int(pgid), "start": start}

    def close(self, handle):
        if self._attached(handle):
            return  # metadata close only; an attached handle owns no pipe
        try:
            handle.proc.stdout.close()
        except Exception:
            pass

    # ---- deadline-safe output reading --------------------------------------

    def _drain_lines(self, handle, on_line, flush=False) -> bool:
        """Deliver complete lines buffered on the handle.

        超长行保留前 ``_LINE_MAX`` 字节并以 ``…[truncated N bytes]`` 标注，
        计入 ``handle._truncated_lines``——有界但绝不静默丢失。
        Returns True if at least one line was delivered.
        """
        if b"\n" in handle._abuf:
            # 兼容直接写入 _abuf 的旧调用方：重新按行切分。
            raw, handle._abuf = handle._abuf, b""
            _buffer_chunk(handle, raw)
        pending = _pending_lines(handle)
        if flush and (handle._abuf or getattr(handle, "_dropped", 0)):
            pending.append(_finish_line(handle))
        delivered = False
        while pending:
            line = pending.popleft()
            if not line:
                continue
            delivered = True
            try:
                if on_line:
                    on_line(line)
            except Exception:
                pass
        return delivered

    def pump(self, handle, timeout, on_line=None, should_abort=None) -> str:
        """Read the child pipe with progress/timeout guarantees.

        Never blocks past ``timeout`` (per-step cap 0.2s), so a silent child
        cannot stall ``Supervisor.wait_session``.  ``select`` + ``os.read``
        fills the byte buffer; complete lines are handed to ``on_line``.
        """
        if handle is None or handle.proc is None:
            return "eof"
        fds = handle.proc.stdout
        if fds is None:
            return "eof"
        try:
            fd = fds.fileno()
        except Exception:
            return "eof"
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            if should_abort is not None and should_abort():
                return "abort"
            if self._drain_lines(handle, on_line):
                return "line"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout"
            try:
                r, _, _ = select.select([fd], [], [],
                                        max(0.0, min(remaining, 0.2)))
            except (OSError, ValueError):
                return "eof"
            if not r:
                continue
            try:
                chunk = os.read(fd, self._READ_CHUNK)
            except (OSError, ValueError):
                return "eof"
            if not chunk:
                # EOF — emit any partial trailing line, report eof.
                self._drain_lines(handle, on_line, flush=True)
                handle._abuf = b""
                return "eof"
            # 有界追加：partial 行上限 _LINE_MAX，超出部分计数并在行尾标注。
            _buffer_chunk(handle, chunk)


class LinuxGroupOps(POSIXGroupOps):
    """Linux backend: cgroup v2 when available, else process-group only."""

    def __init__(self, cgroup_available: bool | None = None):
        super().__init__(platform="linux")
        self._override = cgroup_available

    def _cgroup_available(self) -> bool:
        if self._override is not None:
            return self._override
        return super()._cgroup_available()

    def capability(self):
        if self._cgroup_available():
            return (GROUP_CAPABILITY_CGROUP, "2")
        return (GROUP_CAPABILITY_PGROUP, "1")


class MacGroupOps(POSIXGroupOps):
    """macOS backend: process-group only (os.killpg), never tmux."""

    def __init__(self):
        super().__init__(platform="darwin")

    def capability(self):
        return (GROUP_CAPABILITY_PGROUP, "1")


class WindowsGroupOps(GroupOps):
    """Windows process-tree backend for runner and adopted CLI sessions.

    Windows has no POSIX process groups.  A supervised root is therefore
    treated as the root of a Win32 parent/child tree.  A spawned root starts
    suspended inside a Job Object, so force-kill and reap reach every
    descendant even after the root exited.  The job is NOT kill-on-close: a
    supervised tree must survive supervisor death so a restarted supervisor
    can re-attach from the manifest (as POSIX children do).  Without a job
    handle (re-attached trees, hosts refusing jobs) force-kill falls back to
    killing every enumerated descendant individually; ``taskkill /T`` handles
    graceful termination and the native ``NtSuspendProcess``/``NtResumeProcess`` APIs
    provide the pause/resume controls needed by the supervisor.  Output is
    consumed by a daemon reader thread because ``select()`` cannot wait on a
    Windows anonymous pipe.
    """

    _READ_CHUNK = 65536

    def __init__(self):
        super().__init__(platform="windows")

    def capability(self):
        return (GROUP_CAPABILITY_PGROUP, "1")

    @staticmethod
    def _attached(handle) -> bool:
        return isinstance(handle, _AttachedHandle)

    @staticmethod
    def _new_job():
        """A Job Object that survives supervisor death, or ``None``."""
        try:
            from fleet_core.process.windows_job import WindowsJob
            return WindowsJob(kill_on_close=False)
        except (AttributeError, ImportError, OSError, ValueError):
            return None

    @staticmethod
    def _job_of(handle):
        return getattr(handle, "_job", None)

    def create(self, argv, cwd, env):
        flags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
        job = self._new_job()
        if job is not None:
            # The child cannot spawn descendants before it belongs to the job.
            flags |= 0x00000004  # CREATE_SUSPENDED
        try:
            proc = popen_local(
                list(argv),
                cwd=cwd or os.getcwd(),
                env=_bounded_spawn_env(env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=flags,
                close_fds=True,
                bufsize=0,
            )
        except (OSError, ValueError):
            if job is not None:
                self._close_job_quietly(job)
            raise GroupError("spawn_failed")
        if job is not None:
            try:
                job.start(int(proc._handle))
            except (AttributeError, OSError, TypeError, ValueError):
                # Never let an unowned (or still suspended) child run on.
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=5)
                except (subprocess.TimeoutExpired, OSError):
                    pass
                self._close_job_quietly(job)
                raise GroupError("spawn_failed")
        return _Bound(proc=proc, _job=job)

    @staticmethod
    def _close_job_quietly(job) -> None:
        try:
            job.close()
        except Exception:
            pass

    def _job_terminate(self, handle) -> bool:
        job = self._job_of(handle)
        if job is None:
            return False
        try:
            return bool(job.terminate())
        except Exception:
            return False

    def _job_active(self, handle) -> int | None:
        job = self._job_of(handle)
        if job is None:
            return None
        try:
            return job.active_processes()
        except Exception:
            return None

    def _job_release_if_empty(self, handle) -> None:
        """Drop the job handle once the tree is provably gone (no kill)."""
        if self._job_active(handle) == 0:
            job, handle._job = handle._job, None
            try:
                job.release()
            except Exception:
                pass

    def attach(self, pid: int, started_at: str, exe_path: str):
        pid_i = int(pid)
        if pid_i <= 0:
            raise GroupError("pid_reused")
        pids = self._tree_pids(pid_i)
        if pids is None:
            raise GroupError("no_permission")
        if not pids:
            raise GroupError("pid_reused")
        root_alive = self._pid_alive(pid_i)
        if root_alive is None:
            raise GroupError("no_permission")
        if not root_alive:
            raise GroupError("pid_reused")
        return _AttachedHandle(pid=pid_i, pgid=pid_i,
                               started_at=started_at, exe_path=exe_path)

    def proc_poll(self, handle):
        if self._attached(handle):
            return None if self.group_alive(handle) else 0
        return handle.proc.poll()

    def proc_wait(self, handle, timeout=None):
        if self._attached(handle):
            return None
        try:
            return handle.proc.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            return None

    def group_alive(self, handle):
        if self._attached(handle):
            pids = self._tree_pids(handle.pid)
            if pids is None:
                return True  # enumeration failure is not exit evidence
            return any(self._pid_alive(pid) is not False
                       for pid in pids)
        if handle.proc.poll() is None:
            return True
        active = self._job_active(handle)
        if active is not None:
            # The job counts every descendant, including those orphaned when
            # the root exited (their parent pid no longer links the tree).
            return active > 0
        pids = self._tree_pids(getattr(handle.proc, "pid", 0))
        if pids is None:
            return True
        return any(self._pid_alive(pid) is not False
                   for pid in pids)

    def group_terminate(self, handle, sig):
        if sig == _SIGSTOP:
            return self._suspend_tree(_handle_pid(handle))
        if sig == _SIGCONT:
            return self._resume_tree(_handle_pid(handle))
        return self._taskkill(_handle_pid(handle), force=False)

    def group_kill(self, handle, sig):
        if sig in (_SIGSTOP, _SIGCONT):
            return self.group_terminate(handle, sig)
        # An owned tree is killed through its Job Object: unlike
        # ``taskkill /T`` this still works after the root has exited.
        if not self._attached(handle) and self._job_terminate(handle):
            return True
        return self._kill_tree(handle)

    def _root_exited(self, handle) -> bool:
        if self._attached(handle):
            return self._pid_alive(_handle_pid(handle) or 0) is False
        try:
            return handle.proc.poll() is not None
        except (OSError, ValueError):
            return False

    def _kill_tree(self, handle, rounds: int = 3) -> bool:
        """Kill the tree pid by pid (no job handle available).

        ``taskkill /PID root /T`` fails once the root exited, yet Windows
        keeps the dead root's pid as its children's parent pid, so the
        snapshot walk still finds them.  An exited root's pid is never
        signalled itself (it may have been reused).  Repeats a few rounds to
        catch descendants spawned between snapshot and kill.
        """
        root = _handle_pid(handle)
        for _ in range(max(1, rounds)):
            pids = self._tree_pids(root)
            if pids is None:
                return self._taskkill(root, force=True)
            root_exited = self._root_exited(handle)
            live = [pid for pid in pids
                    if not (pid == root and root_exited)
                    and self._pid_alive(pid) is not False]
            if not live:
                return True
            for pid in live:
                self._kill_pid(pid)
        pids = self._tree_pids(root)
        if pids is None:
            return False
        root_exited = self._root_exited(handle)
        return not any(self._pid_alive(pid) is not False for pid in pids
                       if not (pid == root and root_exited))

    def group_reap(self, handle, timeout=None):
        if self._attached(handle):
            deadline = time.monotonic() + max(0.0, float(timeout or 0))
            while self.group_alive(handle) and time.monotonic() < deadline:
                time.sleep(0.05)
            return not self.group_alive(handle)
        budget = max(0.0, float(timeout or 0))
        try:
            handle.proc.wait(timeout=budget)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            return False
        # Root reaped; leftover descendants belong to this run too and are
        # killed like POSIX ``group_reap`` SIGKILLs the remaining group.
        if self.group_alive(handle):
            if self._job_of(handle) is not None:
                self._job_terminate(handle)
            else:
                self._kill_tree(handle)
            budget = max(1.0, budget)
        deadline = time.monotonic() + budget
        while self.group_alive(handle) and time.monotonic() < deadline:
            time.sleep(0.05)
        gone = not self.group_alive(handle)
        if gone:
            self._job_release_if_empty(handle)
        return gone

    def owner_identity(self, handle):
        if self._attached(handle) or getattr(handle, "proc", None) is None:
            return None
        try:
            pid = int(handle.proc.pid)
        except (AttributeError, TypeError, ValueError):
            return None
        start = process_start_identity(pid)
        if start is None:
            return None
        return {"pid": pid, "pgid": pid, "start": start}

    def close(self, handle):
        if self._attached(handle):
            return
        handle._reader_closed = True
        try:
            if handle.proc.stdout is not None:
                handle.proc.stdout.close()
        except Exception:
            pass
        # Keep the job handle while members live so a later kill can still
        # use TerminateJobObject; release it once the tree is gone.
        self._job_release_if_empty(handle)

    # ---- Windows process tree ---------------------------------------------

    @staticmethod
    def _tree_pids(root_pid: int | None) -> list[int] | None:
        try:
            root = int(root_pid or 0)
        except (TypeError, ValueError):
            return []
        if root <= 0:
            return []
        try:
            import ctypes
            from ctypes import wintypes

            class PROCESSENTRY32W(ctypes.Structure):
                _fields_ = [
                    ("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260),
                ]
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateToolhelp32Snapshot.argtypes = [
                wintypes.DWORD,
                wintypes.DWORD,
            ]
            kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
            kernel32.Process32FirstW.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(PROCESSENTRY32W),
            ]
            kernel32.Process32FirstW.restype = wintypes.BOOL
            kernel32.Process32NextW.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(PROCESSENTRY32W),
            ]
            kernel32.Process32NextW.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            ctypes.set_last_error(0)
            snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
            if snapshot == wintypes.HANDLE(-1).value:
                return None
            rows = []
            try:
                item = PROCESSENTRY32W()
                item.dwSize = ctypes.sizeof(item)
                ctypes.set_last_error(0)
                ok = kernel32.Process32FirstW(snapshot, ctypes.byref(item))
                if not ok and ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                    return None
                while ok:
                    rows.append((int(item.th32ProcessID), int(item.th32ParentProcessID)))
                    ctypes.set_last_error(0)
                    ok = kernel32.Process32NextW(snapshot, ctypes.byref(item))
                    if not ok and ctypes.get_last_error() != 18:
                        return None
            finally:
                kernel32.CloseHandle(snapshot)
        except (AttributeError, OSError, TypeError, ValueError):
            return None
        children: dict[int, list[int]] = {}
        for pid, parent in rows:
            children.setdefault(parent, []).append(pid)
        found: list[int] = []
        pending = [root]
        seen: set[int] = set()
        while pending:
            pid = pending.pop(0)
            if pid in seen:
                continue
            seen.add(pid)
            found.append(pid)
            pending.extend(children.get(pid, ()))
        return found

    @staticmethod
    def _pid_alive(pid: int) -> bool | None:
        return windows_pid_alive(pid)

    @staticmethod
    def _kill_pid(pid: int) -> bool:
        """Force-kill one process (no ``/T``: its tree is walked by caller)."""
        try:
            pid_i = int(pid)
        except (TypeError, ValueError):
            return False
        if pid_i <= 0:
            return False
        try:
            result = run_local(["taskkill.exe", "/PID", str(pid_i), "/F"],
                               capture_output=True, stdin=subprocess.DEVNULL,
                               timeout=8, check=False)
        except (OSError, subprocess.SubprocessError, ValueError):
            return False
        return result.returncode == 0

    @staticmethod
    def _taskkill(root_pid: int | None, *, force: bool) -> bool:
        try:
            root = int(root_pid or 0)
        except (TypeError, ValueError):
            return False
        if root <= 0:
            return False
        argv = ["taskkill.exe", "/PID", str(root), "/T"]
        if force:
            argv.append("/F")
        try:
            result = run_local(argv, capture_output=True,
                               stdin=subprocess.DEVNULL, timeout=8,
                               check=False)
        except (OSError, subprocess.SubprocessError, ValueError):
            return False
        return result.returncode == 0

    @staticmethod
    def _suspend_process(pid: int, resume: bool) -> bool:
        if os.name != "nt":
            return False
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            ntdll = ctypes.WinDLL("ntdll")
            access = 0x0800 | 0x0400 | 0x1000  # suspend/query/synchronize
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(access, False, int(pid))
            if not handle:
                return False
            try:
                fn = ntdll.NtResumeProcess if resume else ntdll.NtSuspendProcess
                fn.argtypes = [wintypes.HANDLE]
                fn.restype = ctypes.c_long
                return int(fn(handle)) == 0
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError, TypeError, ValueError):
            return False

    @classmethod
    def _suspend_tree(cls, root_pid: int | None) -> bool:
        pids = cls._tree_pids(root_pid)
        if not pids:
            return False
        suspended = []
        for pid in reversed(pids):
            if not cls._suspend_process(pid, False):
                for prior in suspended:
                    cls._suspend_process(prior, True)
                return False
            suspended.append(pid)
        return True

    @classmethod
    def _resume_tree(cls, root_pid: int | None) -> bool:
        pids = cls._tree_pids(root_pid)
        return bool(pids) and all(cls._suspend_process(pid, True) for pid in pids)

    # ---- deadline-safe output reading -------------------------------------

    def _ensure_reader(self, handle):
        if getattr(handle, "_reader_started", False):
            return
        handle._reader_started = True
        handle._queue = queue.Queue(maxsize=256)

        def reader():
            try:
                stream = handle.proc.stdout
                while stream is not None:
                    raw = stream.read(self._READ_CHUNK)
                    if not raw:
                        break
                    # 队列满时阻塞等待消费（反压传给子进程），不丢块；
                    # handle 被 close 后才放弃并计数。
                    while True:
                        try:
                            handle._queue.put(raw, timeout=0.2)
                            break
                        except queue.Full:
                            if getattr(handle, "_reader_closed", False):
                                handle._dropped_chunks = getattr(
                                    handle, "_dropped_chunks", 0) + 1
                                return
            except Exception:
                pass
            finally:
                handle._reader_eof = True

        handle._reader_thread = threading.Thread(
            target=reader, name="agent-fleet-windows-reader", daemon=True)
        handle._reader_thread.start()

    def pump(self, handle, timeout, on_line=None, should_abort=None):
        if handle is None or getattr(handle, "proc", None) is None:
            return "eof"
        self._ensure_reader(handle)
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            if should_abort is not None and should_abort():
                return "abort"
            if POSIXGroupOps._drain_lines(self, handle, on_line):
                return "line"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout"
            try:
                raw = handle._queue.get(timeout=min(remaining, 0.2))
            except queue.Empty:
                if handle._reader_eof and handle._queue.empty():
                    POSIXGroupOps._drain_lines(self, handle, on_line, flush=True)
                    handle._abuf = b""
                    return "eof"
                continue
            if not isinstance(raw, bytes):
                raw = str(raw).encode("utf-8", "replace")
            _buffer_chunk(handle, raw)


def _pending_lines(handle) -> deque:
    lines = getattr(handle, "_lines", None)
    if lines is None:
        lines = deque()
        handle._lines = lines
    return lines


def _finish_line(handle) -> str:
    """把 handle 上的 partial 行收尾为一行文本（含截断标注）。"""
    text = handle._abuf.decode("utf-8", "replace").rstrip("\r")
    dropped = getattr(handle, "_dropped", 0)
    if dropped:
        handle._truncated_lines = getattr(handle, "_truncated_lines", 0) + 1
        text += f"…[truncated {dropped} bytes]"
    handle._abuf = b""
    handle._dropped = 0
    return text


def _buffer_chunk(handle, chunk: bytes) -> None:
    """有界行切分：完整行进入待投递队列，partial 行 ≤ ``_LINE_MAX``。"""
    pending = _pending_lines(handle)
    start = 0
    while True:
        idx = chunk.find(b"\n", start)
        seg = chunk[start:] if idx < 0 else chunk[start:idx]
        room = _LINE_MAX - len(handle._abuf)
        if room > 0:
            handle._abuf += seg[:room]
        overflow = len(seg) - max(0, room)
        if overflow > 0:
            handle._dropped = getattr(handle, "_dropped", 0) + overflow
        if idx < 0:
            return
        pending.append(_finish_line(handle))
        start = idx + 1


# Spawn environment: the *only* env a managed CLI sees.  An empty allowlist
# must never map to ``None`` (which would inherit the entire parent env,
# including secrets); it maps to a bounded, well-known-safe base instead.
# Arbitrary parent env vars are never passed through.
def _bounded_spawn_env(env):
    """Return the exact allowlist, or the shared bounded base env when empty.

    基础白名单与 legacy adapter 路径共用 ``spawn_policy.build_child_env``。
    """
    if env:
        return dict(env)
    from tools.spawn_policy import build_child_env
    return build_child_env()


def _handle_pid(handle) -> int | None:
    """Return the raw pid bound to a private handle, or None when absent."""
    if handle is None:
        return None
    raw = getattr(handle, "pid", None)
    if raw is None and getattr(handle, "proc", None) is not None:
        raw = getattr(handle.proc, "pid", None)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def validate_command(argv: Sequence[str]) -> bool:
    if not isinstance(argv, Sequence) or isinstance(argv, (str, bytes)):
        return False
    if not argv:
        return False
    return all(isinstance(a, str) and a for a in argv)
