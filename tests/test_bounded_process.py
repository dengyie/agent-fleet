"""Real descendant cleanup after a process leader exits."""
from __future__ import annotations

import subprocess
import os
import sys
import time
from pathlib import Path

import pytest

from tests.support.processes import assert_process_exited
from fleet_core.process.bounded_process import run_bounded


def test_timeout_reaps_descendant_after_leader_exits(tmp_path: Path) -> None:
    child = tmp_path / "child.py"
    child.write_text(
        "import os,pathlib,time\n"
        "pathlib.Path('descendant.pid').write_text(str(os.getpid()))\n"
        "time.sleep(15)\n", encoding="utf-8",
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import pathlib,subprocess,sys,time\n"
        "subprocess.Popen([sys.executable, 'child.py'])\n"
        "while not pathlib.Path('descendant.pid').exists(): time.sleep(0.01)\n",
        encoding="utf-8",
    )
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded([sys.executable, str(parent)], cwd=tmp_path, timeout_s=1.5,
                    stdout_limit=100, stderr_limit=100)
    assert time.monotonic() - started < 6
    assert_process_exited(int((tmp_path / "descendant.pid").read_text()))


def test_output_exit_and_owned_handle_cleanup(tmp_path: Path) -> None:
    count_handles = None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetProcessHandleCount.restype = wintypes.BOOL

        def count_handles() -> int:
            count = wintypes.DWORD()
            if not kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(count)):
                raise ctypes.WinError(ctypes.get_last_error())
            return count.value

    before = count_handles() if count_handles else None
    for _ in range(8):
        result = run_bounded(
            [sys.executable, "-c",
             "import sys;print('owned-output');print('owned-error',file=sys.stderr);sys.exit(7)"],
            cwd=tmp_path, timeout_s=5, stdout_limit=100, stderr_limit=100,
        )
        assert result.returncode == 7
        assert result.stdout.strip() == b"owned-output"
        assert result.stderr.strip() == b"owned-error"
    if count_handles:
        assert count_handles() <= before + 2


def test_ownership_failure_never_runs_the_command(tmp_path: Path, monkeypatch) -> None:
    from fleet_core.process import bounded_process

    marker = tmp_path / "must-not-run"
    if os.name != "nt":
        # The POSIX boundary fails before the owned child can be created.
        def reject(*args, **kwargs):
            raise OSError("ownership_denied")

        monkeypatch.setattr(bounded_process, "popen_local", reject)
    else:
        from fleet_core.process.windows_job import WindowsJob

        def reject(self, process_handle):
            raise OSError("ownership_denied")

        monkeypatch.setattr(WindowsJob, "start", reject)
    with pytest.raises(OSError, match="ownership_denied"):
        run_bounded([sys.executable, "-c",
                     f"import pathlib;pathlib.Path({str(marker)!r}).write_text('ran')"],
                    cwd=tmp_path, timeout_s=5, stdout_limit=100)
    assert not marker.exists()


def test_job_cleanup_failure_still_reaps_child_and_retains_timeout(tmp_path: Path, monkeypatch) -> None:
    from fleet_core.process import bounded_process

    launch = bounded_process.popen_local
    children = []

    def spawn(*args, **kwargs):
        proc = launch(*args, **kwargs)
        if os.name != "nt":
            proc._handle = 0
        children.append(proc)
        return proc

    class FailedJob:
        def start(self, process_handle: int) -> None:
            return None

        def close(self) -> None:
            raise OSError("injected_job_cleanup_failure")

    monkeypatch.setattr(bounded_process, "popen_local", spawn)
    started = time.monotonic()
    # Job 清理失败不得把正在传播的超时改写成 OSError（adapter 会误报 127）。
    with pytest.raises(subprocess.TimeoutExpired) as error:
        bounded_process._run_owned(
            [sys.executable, "-c", "import time;time.sleep(2)"], tmp_path, None,
            started + 0.1, 0.1, 100, 100, {}, FailedJob(),
        )
    assert time.monotonic() - started < 1.5
    assert "injected_job_cleanup_failure" in " ".join(getattr(error.value, "__notes__", []))
    assert children[0].poll() is not None
