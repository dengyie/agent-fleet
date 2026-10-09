"""Bounded POSIX child output with deadline and process-group ownership."""
from __future__ import annotations

import math
import os
import selectors
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable, Mapping, Sequence


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


def _drain(proc: subprocess.Popen[bytes], deadline: float, stdout_limit: int,
           stderr_limit: int | None, timeout_s: float) -> tuple[bytes, bytes]:
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
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        proc.poll()
    proc.wait(timeout=2)


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
    with subprocess.Popen(list(argv), cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT if stderr_limit is None else subprocess.PIPE,
                          start_new_session=True, preexec_fn=preexec_fn) as proc:
        try:
            output, error = _drain(proc, deadline, stdout_limit, stderr_limit, timeout_s)
            return subprocess.CompletedProcess(proc.args, proc.returncode, output, error)
        finally:
            _reap_group(proc)
