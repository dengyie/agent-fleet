"""Workflow cancellation retains ownership until bounded processes are reaped."""
from __future__ import annotations

import sys
import time
import os
from pathlib import Path

import pytest

from fleet_core.backends.directory import DirectoryBackend
from fleet_core.backends.sandbox import SandboxBackend
from fleet_core.process import bounded_process
from fleet_core.process.bounded_process import ProcessCancelled, run_bounded


@pytest.mark.parametrize("closed_pipes", [False, True])
@pytest.mark.parametrize("threaded", [False, True])
def test_bounded_process_cancels_silent_child_and_reaps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, closed_pipes: bool, threaded: bool,
) -> None:
    if threaded:
        # Exercise the Windows pipe-draining algorithm without emulating a Job.
        monkeypatch.setattr(bounded_process, "_drain", bounded_process._drain_threaded)
    marker = tmp_path / "started"
    program = tmp_path / "silent.py"
    program.write_text(
        "import os, time\nfrom pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('started')\n"
        + ("os.close(1)\nos.close(2)\n" if closed_pipes else "")
        + "time.sleep(30)\n", encoding="utf-8",
    )
    children = []
    original = bounded_process.popen_local

    def capture(*args, **kwargs):
        process = original(*args, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(bounded_process, "popen_local", capture)
    with pytest.raises(ProcessCancelled):
        run_bounded([sys.executable, str(program)], cwd=tmp_path, timeout_s=5,
                    stdout_limit=1024, stderr_limit=1024, should_cancel=marker.exists)
    assert marker.exists()
    assert len(children) == 1
    assert children[0].poll() is not None


def test_bounded_process_checks_cancellation_before_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("a cancelled process must not start")

    monkeypatch.setattr(bounded_process, "popen_local", forbidden)
    with pytest.raises(ProcessCancelled):
        run_bounded([sys.executable, "unused.py"], cwd=tmp_path, timeout_s=5,
                    stdout_limit=1024, should_cancel=lambda: True)


def test_callback_failure_preserves_original_error_after_reaping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "started"
    program = tmp_path / "silent.py"
    program.write_text(f"from pathlib import Path\nimport time\nPath({str(marker)!r}).touch()\ntime.sleep(30)\n",
                       encoding="utf-8")
    failure = RuntimeError("lease lookup failed")
    children = []
    original = bounded_process.popen_local

    def capture(*args, **kwargs):
        process = original(*args, **kwargs)
        children.append(process)
        return process

    def check() -> bool:
        if marker.exists():
            raise failure
        return False

    monkeypatch.setattr(bounded_process, "popen_local", capture)
    with pytest.raises(RuntimeError) as caught:
        run_bounded([sys.executable, str(program)], cwd=tmp_path, timeout_s=5,
                    stdout_limit=1024, should_cancel=check)
    assert caught.value is failure
    assert len(children) == 1 and children[0].poll() is not None


def test_cancelled_process_with_failed_job_cleanup_remains_unconfirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = bounded_process.popen_local
    children = []

    def capture(*args, **kwargs):
        process = original(*args, **kwargs)
        if os.name != "nt":
            process._handle = 0
        children.append(process)
        return process

    class FailedJob:
        def start(self, process_handle: int) -> None:
            return None

        def close(self) -> None:
            raise OSError("injected_job_cleanup_failure")

    def cancelled(*args):
        raise ProcessCancelled("process_cancelled")

    monkeypatch.setattr(bounded_process, "popen_local", capture)
    with pytest.raises(RuntimeError, match="process_stop_unconfirmed"):
        bounded_process._run_owned(
            [sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, None,
            time.monotonic() + 5, 5, 1024, 1024, {}, FailedJob(), drain=cancelled,
        )
    assert len(children) == 1 and children[0].poll() is not None


@pytest.mark.skipif(os.name == "nt", reason="POSIX owned-group observation")
def test_cancelled_process_with_unverifiable_group_remains_unconfirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "started"
    program = tmp_path / "silent.py"
    program.write_text(f"from pathlib import Path\nimport time\nPath({str(marker)!r}).touch()\ntime.sleep(30)\n",
                       encoding="utf-8")
    monkeypatch.setattr(bounded_process, "posix_group_alive", lambda pgid: True, raising=False)
    monkeypatch.setattr(bounded_process, "posix_group_only_zombies", lambda pgid: False, raising=False)
    with pytest.raises(RuntimeError, match="process_stop_unconfirmed"):
        run_bounded([sys.executable, str(program)], cwd=tmp_path, timeout_s=5,
                    stdout_limit=1024, should_cancel=marker.exists)


@pytest.mark.parametrize("sandbox", [False, True])
def test_backend_passes_cancellation_to_owned_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sandbox: bool,
) -> None:
    marker = tmp_path / "started"
    program = tmp_path / "test_recipe.py"
    program.write_text(f"from pathlib import Path\nimport time\nPath({str(marker)!r}).touch()\ntime.sleep(30)\n",
                       encoding="utf-8")
    launcher = [sys.executable, "-c", "import os,sys; os.execv(sys.argv[1], sys.argv[1:])"]
    backend = SandboxBackend(tmp_path, launcher=launcher) if sandbox else DirectoryBackend(tmp_path)
    receipt = backend.execute("command-a", [sys.executable, str(program)], should_cancel=marker.exists)
    assert marker.exists()
    assert receipt.state == "cancelled"
    assert receipt.error_code == "cancelled"
