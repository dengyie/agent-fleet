"""Registered review command protocol; isolation is owned by its explicit launcher."""
from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from fleet_core.workflow.contract_types import CatalogRef, CodeRevisionReference
from fleet_core.workflow.identity import canonical_digest, canonical_json
from fleet_core.workflow.review_protocol import ReviewAdapterSpec, ReviewIdentity
from fleet_core.process.bounded_process import ProcessCancelled, ProcessStopUnconfirmed, run_bounded
from .source import MAX_BYTES, MAX_FILES
from .validators import (MAX_REPORT_BYTES, ReviewValidation, WorkflowValidatorError,
                                  revision_payload, validate_review_report)


@contextmanager
def _directory(root: Path, parts: tuple[str, ...]) -> Iterator[int]:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY") or os.open not in os.supports_dir_fd:
        raise WorkflowValidatorError("review_file_boundary_unavailable")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    descriptor = os.open(root, flags)
    try:
        for part in parts:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _entries(root: Path, parts: tuple[str, ...]) -> Iterator[tuple[str, os.stat_result]]:
    with _directory(root, parts) as descriptor, os.scandir(descriptor) as entries:
        for entry in entries:
            yield entry.name, entry.stat(follow_symlinks=False)


def _file_fingerprint(root: Path, parts: tuple[str, ...], remaining: int) -> tuple[int, str, int]:
    with _directory(root, parts[:-1]) as directory:
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=directory)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > remaining:
            raise WorkflowValidatorError("invalid_review_source")
        digest, size = hashlib.sha256(), 0
        while chunk := stream.read(min(65536, remaining - size + 1)):
            size += len(chunk)
            if size > remaining:
                raise WorkflowValidatorError("invalid_review_source")
            digest.update(chunk)
    return stat.S_IMODE(info.st_mode), digest.hexdigest(), size


def _source_snapshot(root: Path) -> dict[str, tuple[int, str, int]]:
    snapshot: dict[str, tuple[int, str, int]] = {}
    stack: list[tuple[str, ...]] = [()]
    total, files = 0, 0
    while stack:
        parts = stack.pop()
        if len(parts) > 32:
            raise WorkflowValidatorError("invalid_review_source")
        for name, info in _entries(root, parts):
            entry_parts = (*parts, name)
            relative = "/".join(entry_parts)
            if len(relative.encode("utf-8")) > 512 or len(snapshot) >= MAX_FILES * 33:
                raise WorkflowValidatorError("invalid_review_source")
            if stat.S_ISDIR(info.st_mode):
                snapshot[relative] = (stat.S_IMODE(info.st_mode), "directory", 0)
                stack.append(entry_parts)
                continue
            if not stat.S_ISREG(info.st_mode) or files >= MAX_FILES:
                raise WorkflowValidatorError("invalid_review_source")
            fingerprint = _file_fingerprint(root, entry_parts, MAX_BYTES - total)
            snapshot[relative] = fingerprint
            files += 1
            total += fingerprint[2]
    return snapshot


class CommandReviewAdapter:
    def __init__(self, adapter_ref: CatalogRef, executor: ReviewIdentity, argv: tuple[str, ...], *,
                 launcher: tuple[str, ...] = (), timeout_seconds: float = 60) -> None:
        for value in (argv, launcher):
            if type(value) is not tuple or len(value) > 64 or any(
                    type(arg) is not str or not arg or len(arg.encode("utf-8")) > 4096 or "\x00" in arg for arg in value):
                raise WorkflowValidatorError("invalid_review_command")
        if not argv or not Path(argv[0]).is_absolute() or launcher and not Path(launcher[0]).is_absolute():
            raise WorkflowValidatorError("invalid_review_command")
        if type(timeout_seconds) not in (int, float) or not 1 <= timeout_seconds <= 300:
            raise WorkflowValidatorError("invalid_review_timeout")
        if type(executor) is not ReviewIdentity or executor.kind == "human":
            raise WorkflowValidatorError("invalid_executor_identity")
        self.argv = argv
        self.launcher = launcher
        self.timeout_seconds = timeout_seconds
        self.spec = ReviewAdapterSpec(adapter_ref, executor, canonical_digest({
            "argv": list(argv), "launcher": list(launcher), "timeout_seconds": timeout_seconds}))

    async def run(self, source: Path, revision: CodeRevisionReference, subject: str, *, developer_executor_id: str,
                  should_cancel: Callable[[], bool]) -> ReviewValidation:
        if not self.launcher:
            raise WorkflowValidatorError("sandbox_unavailable")
        if self.spec.executor.executor_id == developer_executor_id:
            raise WorkflowValidatorError("independent_reviewer_required")
        request = canonical_json({"schema_version": 1, "source_revision": revision_payload(revision),
                                  "subject_digest": subject, "executor": self.spec.executor.payload()})
        stop = threading.Event()
        worker = asyncio.create_task(asyncio.to_thread(self._execute, source, request,
            lambda: stop.is_set() or should_cancel()))
        cancelled: asyncio.CancelledError | None = None
        while True:
            try:
                raw = await asyncio.shield(worker)
            except asyncio.CancelledError as exc:
                stop.set()
                cancelled = cancelled or exc
                continue
            except BaseException as exc:
                if cancelled is not None:
                    raise cancelled from exc
                raise
            if cancelled is not None:
                raise cancelled
            return validate_review_report(raw, source_revision=revision, subject_digest=subject,
                                          executor=self.spec.executor, developer_executor_id=developer_executor_id)

    def _execute(self, source: Path, request: bytes, should_cancel: Callable[[], bool]) -> bytes:
        try:
            source = source.resolve(strict=True)
            snapshot = _source_snapshot(source)
            with tempfile.TemporaryDirectory(prefix="fleet-review-adapter-") as temporary:
                root = Path(temporary).resolve()
                control, scratch = root / "control", root / "scratch"
                control.mkdir(mode=0o700)
                scratch.mkdir(mode=0o700)
                request_path = control / "request.json"
                with request_path.open("xb") as stream:
                    stream.write(request)
                request_path.chmod(0o600)
                launcher = [arg.replace("{source}", str(source)).replace("{control}", str(control)).replace("{scratch}", str(scratch))
                            for arg in self.launcher]
                environment = {"PATH": os.defpath, "HOME": str(scratch), "TMPDIR": str(scratch),
                               "FLEET_REVIEW_SOURCE": str(source), "FLEET_REVIEW_REQUEST": str(request_path)}
                result = run_bounded([*launcher, *self.argv], cwd=source, env=environment,
                    timeout_s=self.timeout_seconds, stdout_limit=MAX_REPORT_BYTES + 1, stderr_limit=8192,
                    should_cancel=should_cancel)
                try:
                    current = _source_snapshot(source)
                except (OSError, WorkflowValidatorError) as exc:
                    raise WorkflowValidatorError("review_source_changed") from exc
                if current != snapshot:
                    raise WorkflowValidatorError("review_source_changed")
                if result.returncode != 0:
                    raise WorkflowValidatorError("review_process_failed") from subprocess.CalledProcessError(
                        result.returncode, result.args, stderr=result.stderr)
                if len(result.stdout) > MAX_REPORT_BYTES:
                    raise WorkflowValidatorError("review_report_too_large")
                return result.stdout
        except subprocess.TimeoutExpired as exc:
            raise WorkflowValidatorError("review_timeout") from exc
        except ProcessCancelled as exc:
            raise WorkflowValidatorError("review_cancelled") from exc
        except ProcessStopUnconfirmed as exc:
            raise WorkflowValidatorError("review_stop_unconfirmed") from exc
        except OSError as exc:
            raise WorkflowValidatorError("review_execution_failed") from exc


__all__ = ["CommandReviewAdapter"]
