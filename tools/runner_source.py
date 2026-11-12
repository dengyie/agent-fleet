"""Private, durable records for one CLI attempt and its immutable source bundle."""
from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import tempfile
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from fleet_core.atomic_directory import rename_directory_no_replace
from tools import worktree
from tools.transport import Transport
from tools.runner_config import RunnerConfig

from tools.client_update.runtime import client_headers

USER_AGENT = client_headers("runner")["User-Agent"]
SOURCE_RETRY_DELAYS = (0.5, 1.5)
MAX_SOURCE_BYTES = 10 * 1024 * 1024
MAX_RECORD_BYTES = 2 * 1024 * 1024
IDENTITY_KEYS = frozenset({"hub", "machine", "runner_id", "task_id", "attempt_id", "nonce", "project",
                           "agent_type", "source_repository_id"})


class SourceUploadError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    @property
    def retryable(self) -> bool:
        return self.code in {"source_upload_unavailable", "source_delivery_interrupted", "source_lease_unconfirmed", "source_lease_lost"}


def retain_pending(cache_dir: Path, pending: Path, code: str) -> None:
    """Retain the original record outside the automatic replay queue."""
    root = cache_dir / "reconciliation"
    private_directory(root)
    retained = Path(tempfile.mkdtemp(prefix="source-", dir=root))
    atomic_private_json(retained / "reason.json", {"error_code": code, "pending_file": pending.name})
    _sync_directory(root)
    pending.rename(retained / pending.name)
    _sync_directory(retained)
    _sync_directory(pending.parent)


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
        raise SourceUploadError("unsafe_source_cache")
    path.chmod(0o700)


def private_read(path: Path, limit: int) -> bytes:
    if path.is_symlink():
        raise SourceUploadError("unsafe_source_cache")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise SourceUploadError("invalid_source_cache")
        if os.name != "nt" and (info.st_mode & 0o077 or info.st_uid != os.getuid()):
            raise SourceUploadError("unsafe_source_cache")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise SourceUploadError("source_cache_limit")
    return raw


def atomic_private_json(path: Path, value: object) -> None:
    raw = _json(value)
    if len(raw) > MAX_RECORD_BYTES:
        raise SourceUploadError("source_cache_limit")
    private_directory(path.parent)
    # A random sibling and atomic replace keep readers away from partial JSON.
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".record-", delete=False) as stream:
        stage = Path(stream.name)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(stage, path)
    _sync_directory(path.parent)


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def attempt_identity(cfg: RunnerConfig, task: Mapping[str, Any]) -> dict[str, str]:
    value = {"hub": cfg.hub, "machine": cfg.machine, "runner_id": cfg.runner_id,
             **{key: task.get(key) for key in IDENTITY_KEYS - {"hub", "machine", "runner_id"}}}
    if any(type(item) is not str or not item or len(item.encode("utf-8")) > 512 for item in value.values()):
        raise SourceUploadError("invalid_source_identity")
    return value


class SourceCache:
    """A path derived solely from the complete attempt identity; no credentials."""

    def __init__(self, cache_dir: Path, identity: Mapping[str, str]) -> None:
        if set(identity) != IDENTITY_KEYS or any(type(v) is not str or not v or len(v.encode("utf-8")) > 512 for v in identity.values()):
            raise SourceUploadError("invalid_source_identity")
        self.identity = dict(identity)
        self.root = Path(cache_dir) / "sources"
        self.path = self.root / hashlib.sha256(_json(self.identity)).hexdigest()

    def begin(self, deadline_utc: float) -> bool:
        if not math.isfinite(deadline_utc):
            raise SourceUploadError("invalid_source_deadline")
        private_directory(self.root)
        try:
            self.path.mkdir(mode=0o700)
        except FileExistsError:
            self.state()
            return False
        atomic_private_json(self.path / "attempt.json", {"identity": self.identity,
            "deadline_utc": datetime.fromtimestamp(deadline_utc, timezone.utc).isoformat(), "result": None, "source_receipt": None})
        _sync_directory(self.root)
        return True

    def state(self) -> dict[str, Any]:
        private_directory(self.path)
        try:
            value = json.loads(private_read(self.path / "attempt.json", MAX_RECORD_BYTES))
            if type(value) is not dict or set(value) != {"identity", "deadline_utc", "result", "source_receipt"} or value["identity"] != self.identity:
                raise SourceUploadError("source_cache_identity_mismatch")
            deadline = datetime.fromisoformat(value["deadline_utc"])
            if deadline.tzinfo is None or deadline.utcoffset().total_seconds() != 0:
                raise SourceUploadError("invalid_source_deadline")
            if value["result"] is not None and (type(value["result"]) is not dict or any(
                    value["result"].get(key) != self.identity[key] for key in ("attempt_id", "nonce"))):
                raise SourceUploadError("invalid_source_result")
            receipt = value["source_receipt"]
            if receipt is not None and (type(receipt) is not dict or set(receipt) != {"artifact_id", "sha256", "size"} or
                    type(receipt["artifact_id"]) is not str or not receipt["artifact_id"] or type(receipt["size"]) is not int or
                    not 1 <= receipt["size"] <= MAX_SOURCE_BYTES or type(receipt["sha256"]) is not str or
                    len(receipt["sha256"]) != 64 or any(char not in "0123456789abcdef" for char in receipt["sha256"])):
                raise SourceUploadError("invalid_source_receipt")
            return value
        except (OSError, ValueError, TypeError, OverflowError) as exc:
            raise SourceUploadError("invalid_source_cache") from exc

    def remaining(self, now: float) -> float:
        return datetime.fromisoformat(self.state()["deadline_utc"]).timestamp() - now

    def save_result(self, body: dict[str, Any]) -> None:
        state = self.state()
        if any(body.get(key) != self.identity[key] for key in ("attempt_id", "nonce")):
            raise SourceUploadError("invalid_source_result")
        if state["result"] is not None and state["result"] != body:
            raise SourceUploadError("source_result_conflict")
        atomic_private_json(self.path / "attempt.json", {**state, "result": body})

    def save_receipt(self, receipt: dict[str, Any]) -> None:
        raw = self.read_archive()
        if receipt.get("sha256") != hashlib.sha256(raw).hexdigest() or receipt.get("size") != len(raw):
            raise SourceUploadError("invalid_source_receipt")
        value = {key: receipt[key] for key in ("artifact_id", "sha256", "size")}
        state = self.state()
        if state["source_receipt"] is not None and state["source_receipt"] != value:
            raise SourceUploadError("source_receipt_conflict")
        atomic_private_json(self.path / "attempt.json", {**state, "source_receipt": value})

    def archive(self) -> Path | None:
        target = self.path / "bundle"
        if not target.exists():
            return None
        if target.is_symlink() or not target.is_dir():
            raise SourceUploadError("unsafe_source_cache")
        return target / "source.tar"

    def publish(self, stage: Path) -> Path:
        archive = stage / "source.tar"
        archive.chmod(0o600)
        raw = private_read(archive, MAX_SOURCE_BYTES)
        if not raw:
            raise SourceUploadError("empty_source_archive")
        with archive.open("rb") as stream:
            os.fsync(stream.fileno())
        atomic_private_json(stage / "bundle.json", {"identity": self.identity,
            "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)})
        _sync_directory(stage)
        try:
            rename_directory_no_replace(stage, self.path / "bundle")
        except FileExistsError:
            existing = self.read_archive()
            if existing != raw:
                raise SourceUploadError("source_cache_conflict")
        _sync_directory(self.path)
        return self.path / "bundle" / "source.tar"

    def read_archive(self) -> bytes:
        archive = self.archive()
        if archive is None:
            raise SourceUploadError("source_capture_unconfirmed")
        try:
            record = json.loads(private_read(archive.parent / "bundle.json", 8192))
            raw = private_read(archive, MAX_SOURCE_BYTES)
        except (OSError, ValueError) as exc:
            raise SourceUploadError("invalid_source_cache") from exc
        if type(record) is not dict or record != {"identity": self.identity, "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}:
            raise SourceUploadError("source_cache_digest_mismatch")
        return raw


def _check_source_lifecycle(lease_lost: threading.Event | None, deadline: float) -> None:
    if lease_lost is not None and lease_lost.is_set():
        raise SourceUploadError("source_lease_lost")
    if time.monotonic() >= deadline:
        raise SourceUploadError("source_deadline_exceeded")


def _submit_source(cfg: RunnerConfig, task: Mapping[str, Any], raw: bytes, *, lease_lost: threading.Event,
                   deadline: float) -> dict[str, Any]:
    if not 1 <= len(raw) <= MAX_SOURCE_BYTES:
        raise SourceUploadError("source_archive_limit")
    headers = {
        "Content-Type": "application/x-tar",
        "Content-Length": str(len(raw)),
        "X-Task-Nonce": task["nonce"],
        "X-Content-SHA256": hashlib.sha256(raw).hexdigest(),
        "X-Runner-Credential": f"{cfg.machine}:{cfg.credential}",
        **client_headers("runner"),
    }
    if cfg.transport is None:
        cfg.transport = Transport()
    for delay in (0, *SOURCE_RETRY_DELAYS):
        _check_source_lifecycle(lease_lost, deadline)
        if delay:
            lease_lost.wait(min(delay, max(0, deadline - time.monotonic())))
        _check_source_lifecycle(lease_lost, deadline)
        status, payload = cfg.transport.post_bytes(cfg.hub + f"/api/commands/{task['attempt_id']}/source", raw, headers,
            timeout=min(15, deadline - time.monotonic()), deadline=deadline, should_cancel=lease_lost.is_set)
        _check_source_lifecycle(lease_lost, deadline)
        if status == 200:
            if payload.get("ok") is not True or not isinstance(payload.get("artifact_id"), str) or not payload["artifact_id"] or (
                    payload.get("sha256") != headers["X-Content-SHA256"] or type(payload.get("size")) is not int or payload["size"] != len(raw)):
                raise SourceUploadError("invalid_source_receipt")
            return payload
        if status != 0 and not 500 <= status < 600:
            raise SourceUploadError(f"source_upload_rejected_{status}")
    raise SourceUploadError("source_upload_unavailable")


def _source_deadline(cfg: RunnerConfig, task: Mapping[str, Any]) -> float:
    from datetime import datetime

    remaining = float(cfg.agents.get(task["agent_type"], {}).get("timeout_s", 1800))
    if task.get("source_deadline_utc") is not None:
        try:
            deadline = datetime.fromisoformat(task["source_deadline_utc"])
            if deadline.tzinfo is None or deadline.utcoffset().total_seconds() != 0:
                raise ValueError("source deadline must be UTC")
            remaining = min(remaining, deadline.timestamp() - time.time())
        except (ValueError, TypeError, OverflowError) as exc:
            raise SourceUploadError("invalid_source_deadline") from exc
    return time.monotonic() + remaining


def _source_archive(cfg: RunnerConfig, task: Mapping[str, Any], wt: Path | None, base_commit: str | None, *,
                    lease_lost: threading.Event | None = None, deadline: float | None = None) -> dict[str, Any]:
    """Recover or freeze exactly one private package, then retry that package."""
    import asyncio
    from fleet_core.workflow.source import SourceExporter

    lease_lost = lease_lost if lease_lost is not None else threading.Event()
    deadline = _source_deadline(cfg, task) if deadline is None else deadline
    _check_source_lifecycle(lease_lost, deadline)
    cache = SourceCache(cfg.cache_dir, attempt_identity(cfg, task))
    cache.begin(time.time() + max(0, deadline - time.monotonic()))
    deadline = min(deadline, time.monotonic() + cache.remaining(time.time()))
    _check_source_lifecycle(lease_lost, deadline)
    if cache.archive() is None:
        if wt is None or base_commit is None:
            raise SourceUploadError("source_capture_unconfirmed")
        with tempfile.TemporaryDirectory(dir=cache.path, prefix=".capture-") as directory:
            object_directory = Path(directory) / "objects"
            head, tree = worktree.freeze_source_commit(wt, base_commit, task_id=task["task_id"], timestamp=time.time(),
                object_directory=object_directory, deadline=deadline, should_cancel=lease_lost.is_set)
            stage = Path(directory) / "bundle"
            stage.mkdir(mode=0o700)

            async def export() -> None:
                operation = asyncio.create_task(SourceExporter(wt, object_directory=object_directory).export_archive(
                    repository_id=task["source_repository_id"], base_commit=base_commit, head_commit=head,
                    tree_id=tree, destination=stage / "source.tar"))
                try:
                    while not operation.done():
                        _check_source_lifecycle(lease_lost, deadline)
                        await asyncio.wait({operation}, timeout=min(.1, max(0, deadline - time.monotonic())))
                    await operation
                    _check_source_lifecycle(lease_lost, deadline)
                finally:
                    if not operation.done():
                        operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)

            asyncio.run(export())
            cache.publish(stage)
    return _submit_source(cfg, task, cache.read_archive(), lease_lost=lease_lost, deadline=deadline)
