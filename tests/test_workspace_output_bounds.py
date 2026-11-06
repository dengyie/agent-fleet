"""Workspace data and command output stay bounded before receipt creation."""
from pathlib import Path
import sys
import time
import tracemalloc

import pytest

from fleet_core.backends.directory import DirectoryBackend, MAX_READ_BYTES, MAX_OUTPUT_BYTES
from fleet_core.backends.sandbox import SandboxBackend
from tests.support.processes import assert_process_exited


def execution(tmp_path: Path, backend_type: type[DirectoryBackend], source: str,
              monkeypatch: pytest.MonkeyPatch) -> tuple[DirectoryBackend, list[str]]:
    from fleet_core.backends import directory

    script = tmp_path / "output.py"
    script.write_text(source, encoding="utf-8")
    if backend_type is SandboxBackend:
        return SandboxBackend(tmp_path, launcher=[sys.executable, str(script)]), ["ls"]
    bounded = directory.run_bounded
    monkeypatch.setattr(directory, "run_bounded",
                        lambda argv, **kw: bounded([sys.executable, *argv[1:]], **kw))
    return DirectoryBackend(tmp_path), ["python3", str(script)]


def test_workspace_large_read_has_bounded_memory(tmp_path: Path) -> None:
    with (tmp_path / "large.txt").open("wb") as stream:
        for _ in range(128):
            stream.write(b"x" * 65536)
    backend = DirectoryBackend(tmp_path)
    tracemalloc.start()
    try:
        receipt = backend.read("large.txt")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert receipt.state == "succeeded", receipt
    assert receipt.truncated
    assert receipt.result["content"] == "x" * MAX_READ_BYTES
    assert peak < 1024 * 1024


@pytest.mark.parametrize("backend_type", [DirectoryBackend, SandboxBackend])
def test_workspace_command_output_has_bounded_memory(tmp_path: Path, backend_type: type[DirectoryBackend],
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    backend, argv = execution(tmp_path, backend_type,
                              "import os\nfor _ in range(128):\n    os.write(1, b'x'*65536)\n", monkeypatch)
    tracemalloc.start()
    try:
        receipt = backend.execute("output-test", argv)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert receipt.state == "succeeded", receipt
    assert receipt.truncated
    assert receipt.result["output"] == "x" * MAX_OUTPUT_BYTES
    assert peak < 2 * 1024 * 1024


@pytest.mark.parametrize("extra", [0, 1])
def test_read_exact_limit_distinguishes_truncation(tmp_path: Path, extra: int) -> None:
    (tmp_path / "bounded.txt").write_bytes(b"x" * (MAX_READ_BYTES + extra))
    receipt = DirectoryBackend(tmp_path).read("bounded.txt")
    assert receipt.result["content"] == "x" * MAX_READ_BYTES
    assert receipt.truncated is bool(extra)


@pytest.mark.parametrize("backend_type", [DirectoryBackend, SandboxBackend])
def test_failed_command_preserves_exit_and_bounded_stderr(tmp_path: Path, backend_type: type[DirectoryBackend],
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    backend, argv = execution(tmp_path, backend_type,
                              "import os, sys\nos.write(2, b'failure'*100000)\nsys.exit(7)\n", monkeypatch)
    receipt = backend.execute("failure-test", argv)
    assert receipt.state == "failed"
    assert receipt.error_code == "process_failed"
    assert receipt.result["returncode"] == 7, receipt
    assert receipt.result["output"].startswith("failure")
    assert len(receipt.result["output"].encode("utf-8")) <= MAX_OUTPUT_BYTES
    assert receipt.truncated


@pytest.mark.parametrize("backend_type", [DirectoryBackend, SandboxBackend])
@pytest.mark.parametrize("close_pipes", [False, True])
def test_workspace_timeout_reaps_process(tmp_path: Path, backend_type: type[DirectoryBackend], close_pipes: bool,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    source = "import os, time\nopen('child.pid', 'w').write(str(os.getpid()))\n"
    if close_pipes:
        source += "os.close(1)\nos.close(2)\n"
    backend, argv = execution(tmp_path, backend_type, source + "time.sleep(30)\n", monkeypatch)
    started = time.monotonic()
    receipt = backend.execute("timeout-test", argv, timeout_s=0.5)
    assert receipt.error_code == "timeout", receipt
    assert time.monotonic() - started < 3
    pid = int((tmp_path / "child.pid").read_text())
    assert_process_exited(pid)


@pytest.mark.parametrize("kind", ["read", "directory-exec", "sandbox-exec"])
def test_invalid_utf8_replacements_remain_within_byte_limit(tmp_path: Path, kind: str,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    if kind == "read":
        (tmp_path / "invalid.txt").write_bytes(b"\xff" * MAX_READ_BYTES)
        receipt = DirectoryBackend(tmp_path).read("invalid.txt")
        text = receipt.result["content"]
    else:
        backend_type = SandboxBackend if kind == "sandbox-exec" else DirectoryBackend
        backend, argv = execution(tmp_path, backend_type, f"import os\nos.write(1,b'\\xff'*{MAX_OUTPUT_BYTES})\n", monkeypatch)
        receipt = backend.execute("utf8-test", argv)
        text = receipt.result["output"]
    assert receipt.state == "succeeded", receipt
    assert receipt.truncated
    assert len(text.encode("utf-8")) <= MAX_OUTPUT_BYTES
