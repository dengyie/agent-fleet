"""Bounded child output with deadline and process-group ownership.

POSIX drains both pipes through one selector under a single deadline. Windows
has no ``select`` on pipes and no ``os.killpg``, so it drains with reader
threads and owns descendants through a Windows Job Object; both paths keep the same
contract: at most ``limit`` bytes per stream, one deadline, and the whole
group gone when the call returns.
"""
from __future__ import annotations

import math
import os
import selectors
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from tools.local_process import popen as popen_local

if TYPE_CHECKING:
    from tools.windows_job import WindowsJob


def decode_prefix(raw: bytes, limit: int) -> tuple[str, bool]:
    """Preserve valid text while counting replacement characters in the budget."""
    text = raw[:limit].decode("utf-8", errors="replace")
    encoded = text.encode("utf-8")
    truncated = len(raw) > limit or len(encoded) > limit
    return encoded[:limit].decode("utf-8", errors="ignore"), truncated


def _read_ready(selector: selectors.BaseSelector, timeout: float) -> None:
    for key, _ in selector.select(timeout):
        chunk = os.read(key.fd, 65536)
        if not chunk:
            selector.unregister(key.fileobj)
            continue
        target, limit = key.data
        target.extend(chunk[:max(0, limit - len(target))])


def _pump(stream, target: bytearray, limit: int) -> None:
    """Drain one pipe, keeping a bounded prefix and never blocking the child."""
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            room = limit - len(target)
            if room > 0:
                target.extend(chunk[:room])
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _drain_threaded(proc: subprocess.Popen[bytes], deadline: float,
                    stdout_limit: int, stderr_limit: int | None,
                    timeout_s: float) -> tuple[bytes, bytes]:
    """Windows drain: reader threads plus deadline-bounded joins.

    Each thread ends on EOF, so waiting for the threads keeps the POSIX
    contract: a descendant that still holds the pipe open past the deadline is
    a timeout, not a success.
    """
    output, error = bytearray(), bytearray()
    streams = [(proc.stdout, output, stdout_limit)]
    if stderr_limit is not None:
        streams.append((proc.stderr, error, stderr_limit))
    threads = [
        threading.Thread(target=_pump, args=(stream, target, limit), daemon=True)
        for stream, target, limit in streams
        if stream is not None
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(proc.args, timeout_s)
        thread.join(timeout=remaining)
        if thread.is_alive():
            raise subprocess.TimeoutExpired(proc.args, timeout_s)
    proc.wait(timeout=max(0.001, deadline - time.monotonic()))
    return bytes(output), bytes(error)


def _drain(proc: subprocess.Popen[bytes], deadline: float, stdout_limit: int,
           stderr_limit: int | None, timeout_s: float) -> tuple[bytes, bytes]:
    if os.name == "nt":
        return _drain_threaded(proc, deadline, stdout_limit, stderr_limit,
                               timeout_s)
    return _drain_posix(proc, deadline, stdout_limit, stderr_limit, timeout_s)


def _drain_posix(proc: subprocess.Popen[bytes], deadline: float,
                 stdout_limit: int, stderr_limit: int | None,
                 timeout_s: float) -> tuple[bytes, bytes]:
    output, error = bytearray(), bytearray()
    with selectors.DefaultSelector() as selector:
        assert proc.stdout is not None
        streams = [(proc.stdout, output, stdout_limit)]
        if stderr_limit is not None:
            assert proc.stderr is not None
            streams.append((proc.stderr, error, stderr_limit))
        for stream, target, limit in streams:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, (target, limit))
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(proc.args, timeout_s)
            _read_ready(selector, remaining)
    proc.wait(timeout=max(0.001, deadline - time.monotonic()))
    return bytes(output), bytes(error)


def _reap_group(proc: subprocess.Popen[bytes]) -> None:
    """Kill the owned group without ever masking the caller's exception."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    _wait_quietly(proc)


def _wait_quietly(proc: subprocess.Popen[bytes]) -> None:
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


class ProcessAborted(RuntimeError):
    """The caller withdrew permission to continue an owned child."""


def _drain_lines(proc: subprocess.Popen[bytes], deadline: float, timeout_s: float,
                 on_line: Callable[[str], None],
                 should_abort: Callable[[], bool] | None) -> tuple[bytes, bytes]:
    """Stream lines while the owner watches EOF, cancellation and the deadline."""
    done = threading.Event()
    errors: list[BaseException] = []

    def read_lines() -> None:
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                on_line(raw.rstrip(b"\r\n").decode("utf-8", errors="replace"))
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    threading.Thread(target=read_lines, name="owned-process-output", daemon=True).start()
    while True:
        if done.is_set() and errors:
            raise errors[0]
        if should_abort is not None and should_abort():
            raise ProcessAborted()
        if done.is_set() and proc.poll() is not None:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(proc.args, timeout_s)
        if done.is_set():
            # EOF does not imply process exit. Keep polling cancellation even
            # when the child deliberately closes both output descriptors.
            time.sleep(min(0.1, remaining))
        else:
            done.wait(min(0.1, remaining))
    return b"", b""


def run_streamed(argv: Sequence[str], *, cwd: Path, timeout_s: float,
                 on_line: Callable[[str], None],
                 should_abort: Callable[[], bool] | None = None) -> int:
    """Run an owned process with line callbacks and a deadline through pipe EOF."""
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("invalid_process_timeout")
    deadline = time.monotonic() + timeout_s
    job = None
    options = {"start_new_session": True, "preexec_fn": None}
    if os.name == "nt":
        from tools.windows_job import WindowsJob
        job = WindowsJob()
        options = {"creationflags": 0x00000004 | 0x00000200,
                   "preexec_fn": None}
    try:
        result = _run_owned(
            argv, cwd, None, deadline, timeout_s, 0, None, options, job,
            drain=lambda proc, end, seconds: _drain_lines(
                proc, end, seconds, on_line, should_abort),
        )
        return result.returncode
    finally:
        if job is not None:
            job.close()


def run_bounded(argv: Sequence[str], *, cwd: Path, timeout_s: float,
                stdout_limit: int, stderr_limit: int | None = None,
                env: Mapping[str, str] | None = None,
                preexec_fn: Callable[[], None] | None = None) -> subprocess.CompletedProcess[bytes]:
    """Retain at most each limit; drain excess and check the complete exit.

    A missing stderr limit merges stderr into stdout. Callers that need a
    truncation flag request one extra byte. All processes share an owned group
    that is reaped on success, failure, cancellation or timeout. This bounds
    parent memory; child memory/isolation belongs to the caller's launcher.
    """
    limits = (stdout_limit,) if stderr_limit is None else (stdout_limit, stderr_limit)
    if any(type(limit) is not int or limit < 0 for limit in limits):
        raise ValueError("invalid_output_limit")
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("invalid_process_timeout")
    deadline = time.monotonic() + timeout_s
    job = None
    options = {"start_new_session": True, "preexec_fn": preexec_fn}
    if os.name == "nt":
        from tools.windows_job import WindowsJob
        job = WindowsJob()
        # The child cannot spawn descendants before it belongs to our job.
        options = {"creationflags": 0x00000004 | 0x00000200,
                   "preexec_fn": preexec_fn}  # SUSPENDED | NEW_PROCESS_GROUP
    try:
        return _run_owned(argv, cwd, env, deadline, timeout_s, stdout_limit,
                          stderr_limit, options, job)
    finally:
        if job is not None:
            job.close()


def _run_owned(argv: Sequence[str], cwd: Path, env: Mapping[str, str] | None,
               deadline: float, timeout_s: float, stdout_limit: int,
               stderr_limit: int | None, options: dict[str, Any],
               job: WindowsJob | None,
               drain: Callable[[subprocess.Popen[bytes], float, float], tuple[bytes, bytes]] | None = None
               ) -> subprocess.CompletedProcess[bytes]:
    with popen_local(list(argv), cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                     stdout=subprocess.PIPE,
                     stderr=subprocess.STDOUT if stderr_limit is None else subprocess.PIPE,
                     **options) as proc:
        try:
            if job is not None:
                job.start(int(proc._handle))
            output, error = (drain(proc, deadline, timeout_s) if drain is not None
                             else _drain(proc, deadline, stdout_limit, stderr_limit, timeout_s))
            return subprocess.CompletedProcess(proc.args, proc.returncode, output, error)
        finally:
            if job is not None:
                try:
                    job.close()
                finally:
                    # Reap even if Job cleanup fails; an assignment failure
                    # also leaves an unstarted child outside the Job.
                    if proc.poll() is None:
                        proc.kill()
                    _wait_quietly(proc)
            else:
                _reap_group(proc)
