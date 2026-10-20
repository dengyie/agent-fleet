"""Git result collection keeps memory bounded by the published byte budget."""
from pathlib import Path
import os
import subprocess
import sys
import time
import tracemalloc

import pytest

from tools import worktree


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    return tmp_path


def test_large_git_patch_has_bounded_parent_memory(repository: Path) -> None:
    with (repository / "large.txt").open("wb") as stream:
        for _ in range(8192):
            stream.write(b"x" * 1023 + b"\n")
    tracemalloc.start()
    try:
        patch = worktree.diff_patch(repository)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2 * 1024 * 1024, f"bounded patch collector allocated {peak} bytes"
    assert len(patch.encode("utf-8")) <= 102400
    assert patch.endswith("\n…[truncated]")


@pytest.mark.parametrize("kind", ["patch", "stat"])
def test_git_diff_marker_is_inside_utf8_budget(repository: Path, kind: str) -> None:
    for index in range(12):
        (repository / f"result-{index}.txt").write_text("🌲" * 100 + "\n")
    collector = worktree.diff_patch if kind == "patch" else worktree.diff_stat
    result = collector(repository, max_bytes=120)
    assert len(result.encode("utf-8")) <= 120
    assert result.endswith("\n…[truncated]")


def fake_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    binary = tmp_path / "git"
    binary.write_text(f"#!{sys.executable}\nimport os, sys, time\n"
                      "if sys.argv[1] == 'add':\n    sys.exit(0)\n" + script)
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))


def test_large_stderr_is_drained_and_failure_is_not_hidden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_git(tmp_path, monkeypatch,
             "for _ in range(128):\n"
             "    os.write(1, b'x' * 65536)\n"
             "    os.write(2, b'failure' * 10000)\n"
             "sys.exit(7)\n")
    tracemalloc.start()
    try:
        with pytest.raises(worktree.WorktreeError, match="git diff 失败") as error:
            worktree.diff_patch(tmp_path)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2 * 1024 * 1024
    assert len(str(error.value)) < 350


@pytest.mark.parametrize("close_pipes", [False, True])
def test_diff_deadline_reaps_child_and_retains_cause(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                   close_pipes: bool) -> None:
    fake_git(tmp_path, monkeypatch,
             "open('child.pid', 'w').write(str(os.getpid()))\n"
             + ("os.close(1)\nos.close(2)\n" if close_pipes else "")
             + "time.sleep(30)\n")
    monkeypatch.setattr(worktree, "DIFF_TIMEOUT_S", 0.5)
    started = time.monotonic()
    with pytest.raises(worktree.WorktreeError, match="超时") as error:
        worktree.diff_patch(tmp_path)
    assert time.monotonic() - started < 3
    assert isinstance(error.value.__cause__, subprocess.TimeoutExpired)
    pid = int((tmp_path / "child.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_invalid_utf8_diff_is_bounded_valid_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_git(tmp_path, monkeypatch, "os.write(1, b'\\xff' * 200)\n")
    patch = worktree.diff_patch(tmp_path, max_bytes=120)
    assert len(patch.encode("utf-8")) <= 120
    assert patch.endswith("\n…[truncated]")


def test_threaded_drain_keeps_a_bounded_prefix_and_real_exit() -> None:
    """The Windows drain branch must bound output without losing the exit code."""
    from tools import bounded_process, local_process

    proc = local_process.popen(
        [sys.executable, "-c",
         "import sys; sys.stdout.buffer.write(b'x' * 200000); sys.stdout.buffer.flush()"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        output, error = bounded_process._drain_threaded(
            proc, time.monotonic() + 30, 100, 10, 30.0)
        assert proc.returncode == 0
    finally:
        bounded_process._reap_group(proc)
    assert output == b"x" * 100
    assert error == b""


def test_threaded_drain_times_out_when_a_writer_holds_the_pipe() -> None:
    from tools import bounded_process, local_process

    proc = local_process.popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            bounded_process._drain_threaded(proc, time.monotonic() + 0.5, 100, 10, 0.5)
    finally:
        bounded_process._reap_group(proc)
    assert time.monotonic() - started < 5
    assert proc.poll() is not None


def test_reap_never_masks_the_original_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import bounded_process

    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(bounded_process, "_drain", boom)
    with pytest.raises(RuntimeError, match="boom"):
        bounded_process.run_bounded(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            cwd=Path.cwd(), timeout_s=5, stdout_limit=100)
