"""Read-only assertions for fixture process cleanup on supported platforms."""
from __future__ import annotations

import os
import subprocess
import time


def assert_process_exited(pid: int) -> None:
    if os.name != "nt":
        deadline = time.monotonic() + 2
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            state = subprocess.run(["ps", "-p", str(pid), "-o", "stat="],
                                   capture_output=True, text=True, check=False, timeout=2)
            if "Z" in state.stdout or not state.stdout.strip():
                return  # exited orphan awaiting init's wait; no live execution
            if time.monotonic() >= deadline:
                raise AssertionError(f"fixture process {pid} is still running")
            time.sleep(0.01)

    # os.kill(pid, 0) calls TerminateProcess on Windows; it is not a probe.
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: no process with this PID
            return
        raise ctypes.WinError(error)
    try:
        deadline = time.monotonic() + 3
        while kernel.WaitForSingleObject(handle, 0) != 0:
            if time.monotonic() >= deadline:
                raise AssertionError(f"fixture process {pid} is still running")
            time.sleep(0.01)
    finally:
        kernel.CloseHandle(handle)
