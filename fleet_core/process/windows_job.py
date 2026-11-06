"""Own a suspended Windows child and its descendants through a Job Object."""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
        ("max_working_set", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
        ("scheduling", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "read_operations", "write_operations", "other_operations",
        "read_bytes", "write_bytes", "other_bytes",
    )]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits), ("io", _IoCounters),
        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("user_time", ctypes.c_int64), ("kernel_time", ctypes.c_int64),
        ("period_user_time", ctypes.c_int64), ("period_kernel_time", ctypes.c_int64),
        ("page_faults", wintypes.DWORD), ("total", wintypes.DWORD),
        ("active", wintypes.DWORD), ("terminated", wintypes.DWORD),
    ]


class WindowsJob:
    """Job Object ownership; assignment must precede child execution.

    ``kill_on_close=True`` (the default, used by bounded processes) sets
    ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` so the tree dies with the last job
    handle, including when the owner process dies.  ``kill_on_close=False``
    keeps the job purely as a kill/query handle: members survive the owner's
    death (needed when a restarted supervisor re-attaches to a live tree).
    """

    def __init__(self, kill_on_close: bool = True) -> None:
        self.kill_on_close = bool(kill_on_close)
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        self._kernel.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._kernel.TerminateJobObject.restype = wintypes.BOOL
        self._kernel.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._kernel.QueryInformationJobObject.restype = wintypes.BOOL
        self._kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel.CloseHandle.restype = wintypes.BOOL
        self._handle = self._kernel.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if not self.kill_on_close:
            return
        limits = _ExtendedLimits()
        limits.basic.flags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel.SetInformationJobObject(
                self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def start(self, process_handle: int) -> None:
        """Assign the suspended child or fail without running its command."""
        if not self._kernel.AssignProcessToJobObject(self._handle, process_handle):
            raise ctypes.WinError(ctypes.get_last_error())
        resume = ctypes.WinDLL("ntdll").NtResumeProcess
        resume.argtypes = [wintypes.HANDLE]
        resume.restype = ctypes.c_long
        if resume(process_handle) < 0:
            raise OSError("windows_child_resume_failed")

    def _active(self, handle) -> int:
        accounting = _Accounting()
        if not self._kernel.QueryInformationJobObject(
                handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(accounting.active)

    def active_processes(self) -> int | None:
        """Live member count, or ``None`` when the job is closed/unreadable."""
        if self._handle is None:
            return None
        try:
            return self._active(self._handle)
        except OSError:
            return None

    def terminate(self) -> bool:
        """Kill every member now; the job handle stays open for queries."""
        if self._handle is None:
            return False
        return bool(self._kernel.TerminateJobObject(self._handle, 1))

    def release(self) -> None:
        """Close the handle without terminating (caller proved it empty)."""
        if self._handle is None:
            return
        handle, self._handle = self._handle, None
        self._kernel.CloseHandle(handle)

    def close(self) -> None:
        if self._handle is None:
            return
        handle, self._handle = self._handle, None
        try:
            if not self._kernel.TerminateJobObject(handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())
            deadline = time.monotonic() + 2
            while True:
                if self._active(handle) == 0:
                    return
                if time.monotonic() >= deadline:
                    raise OSError("windows_job_exit_unconfirmed")
                time.sleep(0.01)
        finally:
            self._kernel.CloseHandle(handle)
