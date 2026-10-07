#!/usr/bin/env python3
"""Stop the expected Agent Fleet web process through a Linux pidfd."""

from __future__ import annotations

import os
import select
import signal
import sys


class ProcessControlError(RuntimeError):
    pass


def _read_process_identity(pid: int) -> tuple[int, str, tuple[bytes, ...], int] | None:
    try:
        with open(f"/proc/{pid}/status", "rb") as status_file:
            status = status_file.readlines()
        uid = int(next(line for line in status if line.startswith(b"Uid:")).split()[1])
        parent_pid = int(next(line for line in status if line.startswith(b"PPid:")).split()[1])
        cwd = os.readlink(f"/proc/{pid}/cwd")
        with open(f"/proc/{pid}/cmdline", "rb") as cmdline_file:
            argv = tuple(part for part in cmdline_file.read().split(b"\0") if part)
    except (OSError, StopIteration, ValueError):
        return None
    return uid, cwd, argv, parent_pid


def _matches_process(
    identity: tuple[int, str, tuple[bytes, ...], int],
    expected_cwd: str,
    expected_uid: int,
    process_kind: str,
    allow_existing: bool,
    expected_parent_pid: int | None,
) -> bool:
    uid, cwd, argv, parent_pid = identity
    if uid != expected_uid or os.path.realpath(cwd) != os.path.realpath(expected_cwd):
        return False
    if process_kind == "sleep":
        return (
            expected_parent_pid is not None
            and parent_pid == expected_parent_pid
            and len(argv) == 2
            and os.path.basename(argv[0]) == b"sleep"
        )
    marker = b"hub/web.py" if process_kind == "web" else b"deploy/hk-self-report-loop.sh"
    if not any(marker in arg for arg in argv):
        return False
    return process_kind == "probe" or allow_existing or b"--no-serve-frontend" in argv


def _send_pidfd_signal(pidfd: int, signum: int) -> bool:
    sender = getattr(signal, "pidfd_send_signal", None)
    if sender is None:
        raise ProcessControlError("pidfd_send_signal is unavailable")
    try:
        sender(pidfd, signum, None, 0)
    except ProcessLookupError:
        return False
    except OSError as error:
        raise ProcessControlError(f"pidfd signal failed: {error}") from error
    return True


def _wait_for_exit(pidfd: int, timeout_seconds: float) -> bool:
    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    return bool(poller.poll(round(timeout_seconds * 1000)))


def stop_web_process(
    pid: int,
    expected_cwd: str,
    *,
    expected_uid: int | None = None,
    process_kind: str = "web",
    allow_existing: bool = False,
    expected_parent_pid: int | None = None,
    term_timeout: float = 2,
    kill_timeout: float = 1,
) -> str:
    if sys.platform != "linux":
        raise ProcessControlError("Linux pidfd support is required")
    if pid <= 0:
        raise ProcessControlError("PID must be positive")
    if process_kind not in {"web", "probe", "sleep"}:
        raise ProcessControlError("unsupported process kind")
    if process_kind == "sleep" and (expected_parent_pid is None or expected_parent_pid <= 0):
        raise ProcessControlError("sleep process requires a positive parent PID")
    if expected_uid is None:
        expected_uid = os.geteuid()
    if expected_uid < 0:
        raise ProcessControlError("UID must be non-negative")
    pidfd_open = getattr(os, "pidfd_open", None)
    if pidfd_open is None or getattr(signal, "pidfd_send_signal", None) is None:
        raise ProcessControlError("Linux pidfd support is unavailable")

    try:
        pidfd = pidfd_open(pid, 0)
    except ProcessLookupError:
        return "absent"
    except OSError as error:
        raise ProcessControlError(f"pidfd_open failed: {error}") from error

    try:
        identity = _read_process_identity(pid)
        if identity is None:
            if not _send_pidfd_signal(pidfd, 0):
                return "absent"
            raise ProcessControlError("live process identity could not be inspected")
        if not _matches_process(
            identity,
            expected_cwd,
            expected_uid,
            process_kind,
            allow_existing,
            expected_parent_pid,
        ):
            return "mismatch"
        if not _send_pidfd_signal(pidfd, 0):
            return "absent"
        if not _send_pidfd_signal(pidfd, signal.SIGTERM):
            return "absent"
        if _wait_for_exit(pidfd, term_timeout):
            return "stopped"
        if not _send_pidfd_signal(pidfd, signal.SIGKILL):
            return "stopped"
        if _wait_for_exit(pidfd, kill_timeout):
            return "stopped"
        raise ProcessControlError("process remained alive after SIGKILL")
    finally:
        os.close(pidfd)


def main(argv: list[str]) -> int:
    if len(argv) != 6:
        print("usage: hk-web-process-control.py PID EXPECTED_CWD EXPECTED_UID web|probe MODE", file=sys.stderr)
        return 2
    try:
        pid = int(argv[1])
        process_kind = argv[4]
        mode = argv[5]
        expected_parent_pid = None
        if process_kind in {"web", "probe"} and mode in {"api_only", "allow_existing"}:
            allow_existing = mode == "allow_existing"
        elif process_kind == "sleep" and mode.startswith("child:"):
            expected_parent_pid = int(mode.removeprefix("child:"))
            allow_existing = False
        else:
            raise ValueError("invalid process mode")
        status = stop_web_process(
            pid,
            argv[2],
            expected_uid=int(argv[3]),
            process_kind=process_kind,
            allow_existing=allow_existing,
            expected_parent_pid=expected_parent_pid,
        )
    except (ValueError, ProcessControlError) as error:
        print(f"guardian process control failed: {error}", file=sys.stderr)
        return 1

    print(status)
    return 3 if status == "mismatch" else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
