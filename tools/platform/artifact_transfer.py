"""Fixed-origin byte reception into private, descriptor-pinned Node inputs."""
from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time
from typing import Any, BinaryIO, ContextManager, Protocol
from urllib.parse import urlsplit

from fleet_core.artifact_receive import (
    ArtifactReceiveError, RECEIVE_SUFFIXES, ReceiveReceipt, ReceiveRequest, validate_alias, validate_receipt,
)
from tools.atomic_directory import rename_at_no_replace
from .artifact_journal import ArtifactReceiveJournal
from .artifact_lock import ArtifactSlotError, artifact_io_slot
from . import artifact_windows as windows

CHUNK_BYTES = 65536


class ContentResponse(Protocol):
    status: int
    headers: Mapping[str, str]
    def read(self, size: int) -> bytes: ...


class ContentTransport(Protocol):
    def get_stream(self, url: str, headers: dict[str, str], *, deadline: float,
                   should_cancel: Callable[[], bool]) -> ContextManager[ContentResponse]: ...


@dataclass(frozen=True, slots=True)
class InputDirectory:
    root: Path
    suffixes: frozenset[str] = frozenset({".bin", ".tar"})

    def __post_init__(self) -> None:
        root = Path(self.root)
        if not root.is_absolute() or ".." in root.parts or not self.suffixes or not self.suffixes <= RECEIVE_SUFFIXES:
            raise ArtifactReceiveError("invalid_input_directory")
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "suffixes", frozenset(self.suffixes))


def receive_supported() -> bool:
    if os.name == "nt":
        return windows.windows_supported()
    return export_supported()


def export_supported() -> bool:
    if os.name == "nt":
        return windows.windows_supported()
    return (os.name == "posix" and (sys.platform == "darwin" or sys.platform.startswith("linux"))
        and hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd)


def hub_origin(url: str) -> str:
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme in {"http", "https"} and parsed.hostname and parsed.port != 0
            and parsed.username is None and parsed.password is None and parsed.path in {"", "/"}
            and not parsed.query and not parsed.fragment and "\\" not in url
            and all(0x20 < ord(char) < 0x7f for char in url))
    except (TypeError, ValueError) as exc:
        raise ArtifactReceiveError("invalid_hub_origin") from exc
    if not valid:
        raise ArtifactReceiveError("invalid_hub_origin")
    return parsed.scheme + "://" + parsed.netloc


@contextmanager
def _root_handle(root: Path) -> Iterator[int]:
    if os.name == "nt":
        with windows.root_handle(root) as descriptor:
            yield descriptor
        return
    descriptor = None
    try:
        descriptor = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for component in root.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise ArtifactReceiveError("unsafe_input_path")
        yield descriptor
    except OSError as exc:
        raise ArtifactReceiveError("unsafe_input_path") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _file_info(descriptor: int) -> os.stat_result | windows.FileInfo:
    return windows.file_info(descriptor) if os.name == "nt" else os.fstat(descriptor)


def _open_input(root_fd: int, name: str, *, writable: bool, create: bool = False) -> int:
    if os.name == "nt":
        return windows.open_child(root_fd, name, writable=writable, create=create, exclusive=create)
    flags = (os.O_WRONLY if writable else os.O_RDONLY) | os.O_NOFOLLOW | os.O_NONBLOCK
    return os.open(name, flags | (os.O_CREAT | os.O_EXCL if create else 0), 0o600, dir_fd=root_fd)


class ArtifactReceiver:
    """All per-request state belongs to the stack and independent SQLite ledger."""

    def __init__(self, *, node_id: str, credential: str, worker_id: str, hub_url: str,
                 public_key: bytes, inputs: Mapping[str, InputDirectory], journal_path: Path,
                 transport: ContentTransport | None = None, slot_path: Path | None = None, max_bytes: int = 64 * 1024 * 1024,
                 clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic,
                 should_cancel: Callable[[], bool] = lambda: False) -> None:
        if type(max_bytes) is not int or not 1 <= max_bytes <= 256 * 1024 * 1024:
            raise ArtifactReceiveError("invalid_receive_limit")
        if not isinstance(public_key, bytes) or len(public_key) != 32:
            raise ArtifactReceiveError("invalid_receive_key")
        if not credential.startswith(node_id + ":") or not worker_id:
            raise ArtifactReceiveError("invalid_receive_identity")
        for alias, directory in inputs.items():
            validate_alias(alias)
            if not isinstance(directory, InputDirectory):
                raise ArtifactReceiveError("invalid_input_directory")
        self.node_id, self.credential, self.worker_id = node_id, credential, worker_id
        self.origin, self.public_key, self.inputs = hub_origin(hub_url), public_key, dict(inputs)
        self.max_bytes, self.clock, self.monotonic, self.should_cancel = max_bytes, clock, monotonic, should_cancel
        if transport is None:
            from tools.transport import Transport
            transport = Transport()
        self.transport = transport
        self.journal = ArtifactReceiveJournal(journal_path)
        self.journal.init()
        self.slot_path = Path(slot_path or journal_path).absolute()

    @property
    def available(self) -> bool:
        if not receive_supported() or not self.inputs:
            return False
        if os.name != "nt":
            return True
        try:
            for root in (self.slot_path.parent, *(value.root for value in self.inputs.values())):
                with _root_handle(root):
                    windows.validate_root(str(root))
        except (ArtifactReceiveError, OSError):
            return False
        return True

    @contextmanager
    def _locked(self, root_fd: int, request: ReceiveRequest) -> Iterator[None]:
        if os.name == "nt":
            with self._windows_lock(root_fd, request):
                yield
            return
        import fcntl
        descriptor = None
        try:
            descriptor = os.open("." + request.grant_id + ".lock", os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600, dir_fd=root_fd)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                raise ArtifactReceiveError("unsafe_input_path")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        except BlockingIOError as exc:
            raise ArtifactReceiveError("receive_busy") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    @contextmanager
    def _windows_lock(root_fd: int, request: ReceiveRequest) -> Iterator[None]:
        try:
            with windows.lock_file(root_fd, "." + request.grant_id + ".lock"):
                yield
        except OSError as exc:
            if exc.winerror == 33:
                raise ArtifactReceiveError("receive_busy") from exc
            raise

    @contextmanager
    def _slot(self) -> Iterator[None]:
        try:
            with _root_handle(self.slot_path.parent) as directory:
                with artifact_io_slot(directory, self.slot_path.name, 2):
                    yield
        except ArtifactSlotError as exc:
            code = "receive_concurrency_limit" if exc.code == "transfer_concurrency_limit" else exc.code
            raise ArtifactReceiveError(code) from exc

    def _check(self, request: ReceiveRequest, deadline: float) -> None:
        if self.should_cancel():
            raise ArtifactReceiveError("receive_cancelled")
        if self.clock() >= request.expires_at or self.monotonic() >= deadline:
            raise ArtifactReceiveError("receive_expired")

    @staticmethod
    def _entry(root_fd: int, filename: str) -> os.stat_result | windows.FileInfo | None:
        if os.name == "nt":
            return windows.entry(root_fd, filename)
        try:
            return os.stat(filename, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    @staticmethod
    def _identity(info: os.stat_result | windows.FileInfo, record: Mapping[str, Any]) -> None:
        safe = (not info.directory if isinstance(info, windows.FileInfo)
            else stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid())
        if (not safe or info.st_nlink != 1
                or (str(info.st_dev), str(info.st_ino)) != (record["stage_device"], record["stage_inode"])):
            raise ArtifactReceiveError("input_target_conflict")

    def _verify(self, root_fd: int, name: str, request: ReceiveRequest, record: Mapping[str, Any]) -> None:
        descriptor = _open_input(root_fd, name, writable=False)
        with os.fdopen(descriptor, "rb") as source:
            info = _file_info(source.fileno())
            self._identity(info, record)
            if info.st_size != request.size:
                raise ArtifactReceiveError("artifact_hash_mismatch")
            digest = hashlib.sha256()
            for chunk in iter(lambda: source.read(CHUNK_BYTES), b""):
                digest.update(chunk)
            if digest.hexdigest() != request.sha256:
                raise ArtifactReceiveError("artifact_hash_mismatch")

    def _stage(self, root_fd: int, request: ReceiveRequest, record: dict[str, Any]) -> int:
        name = "." + request.grant_id + ".part"
        if record["stage_inode"] is not None:
            descriptor = _open_input(root_fd, name, writable=True)
            try:
                self._identity(_file_info(descriptor), record)
                os.ftruncate(descriptor, 0)
            except BaseException:
                os.close(descriptor)
                raise
            return descriptor
        descriptor = _open_input(root_fd, name, writable=True, create=True)
        try:
            info = _file_info(descriptor)
            self.journal.attach_stage(request.command_id, device=info.st_dev, inode=info.st_ino, now=self.clock())
            record.update(stage_device=str(info.st_dev), stage_inode=str(info.st_ino))
            if os.name != "nt":
                os.fsync(root_fd)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def _download(self, root_fd: int, request: ReceiveRequest, record: dict[str, Any], deadline: float) -> None:
        headers = {"X-Platform-Node-Credential": self.credential, "X-Platform-Command-ID": request.command_id,
            "X-Platform-Worker-ID": self.worker_id, "Accept-Encoding": "identity"}
        url = self.origin + "/api/platform/v1/node/artifact-inputs/" + request.grant_id + "/content"
        self._check(request, deadline)
        self.journal.attempt(request.command_id, now=self.clock())
        with self.transport.get_stream(url, headers, deadline=deadline, should_cancel=self.should_cancel) as response:
            if response.status != 200:
                raise ArtifactReceiveError("receive_redirect_forbidden" if 300 <= response.status < 400 else "receive_http_failed")
            length = response.headers.get("Content-Length", "")
            if (not isinstance(length, str) or not length.isascii() or not length.isdecimal()
                    or int(length) != request.size or response.headers.get("Content-Encoding")
                    or response.headers.get("Transfer-Encoding")):
                raise ArtifactReceiveError("invalid_content_length")
            with os.fdopen(self._stage(root_fd, request, record), "wb") as output:
                self._copy_content(response, output, request, deadline)
        self.journal.mark(request.command_id, "publishing", now=self.clock())

    def _copy_content(self, response: ContentResponse, output: BinaryIO, request: ReceiveRequest, deadline: float) -> None:
        remaining, digest = request.size, hashlib.sha256()
        while remaining:
            self._check(request, deadline)
            chunk = response.read(min(CHUNK_BYTES, remaining))
            self._check(request, deadline)
            if type(chunk) is not bytes or not chunk or len(chunk) > remaining or len(chunk) > CHUNK_BYTES:
                raise ArtifactReceiveError("invalid_content_length")
            output.write(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
        self._check(request, deadline)
        if response.read(1):
            raise ArtifactReceiveError("invalid_content_length")
        self._check(request, deadline)
        if digest.hexdigest() != request.sha256:
            raise ArtifactReceiveError("artifact_hash_mismatch")
        output.flush()
        os.fsync(output.fileno())

    def _publish(self, root_fd: int, request: ReceiveRequest) -> None:
        try:
            if os.name == "nt":
                windows.publish(root_fd, "." + request.grant_id + ".part", request.filename)
            else:
                rename_at_no_replace("." + request.grant_id + ".part", request.filename, directory_fd=root_fd)
        except FileExistsError as exc:
            raise ArtifactReceiveError("input_target_conflict") from exc
        if os.name != "nt":
            os.fsync(root_fd)

    @staticmethod
    def _check_root(root: Path, root_fd: int) -> None:
        with _root_handle(root) as current:
            expected, actual = _file_info(root_fd), _file_info(current)
            if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
                raise ArtifactReceiveError("unsafe_input_path")

    def receive(self, command: Mapping[str, Any]) -> ReceiveReceipt:
        if not self.available:
            raise ArtifactReceiveError("capability_unavailable")
        request = ReceiveRequest.parse(command, node_id=self.node_id, public_key=self.public_key,
            now=self.clock(), max_bytes=self.max_bytes)
        directory = self.inputs.get(request.input_alias)
        if directory is None or request.suffix not in directory.suffixes:
            raise ArtifactReceiveError("invalid_input_alias")
        deadline = self.monotonic() + min(1800., request.expires_at - self.clock())
        try:
            with self._slot(), _root_handle(directory.root) as root_fd, self._locked(root_fd, request):
                return self._receive_locked(root_fd, directory.root, request, command, deadline)
        except ArtifactReceiveError:
            raise
        except (OSError, TimeoutError) as exc:
            raise ArtifactReceiveError("receive_io_failed", uncertain=True) from exc

    def _receive_locked(self, root_fd: int, root: Path, request: ReceiveRequest,
                        command: Mapping[str, Any], deadline: float) -> ReceiveReceipt:
        info = _file_info(root_fd)
        existing = self._entry(root_fd, request.filename)
        envelope = {key: command.get(key) for key in ("command_id", "target_node", "action", "resource_id",
            "arguments", "retry_class", "expires_at", "args_hash", "run_id", "grant_id", "signature")}
        record = self.journal.begin(request, command=envelope, device=info.st_dev, inode=info.st_ino, now=self.clock())
        if record["state"] == "reconciliation":
            raise ArtifactReceiveError("receive_reconciliation_required", uncertain=True)
        if existing is not None:
            if record["state"] not in {"publishing", "ready"}:
                raise ArtifactReceiveError("input_target_conflict")
            self._verify(root_fd, request.filename, request, record)
            self._check_root(root, root_fd)
            self.journal.mark(request.command_id, "ready", now=self.clock())
            return request.receipt()
        if record["state"] == "ready":
            raise ArtifactReceiveError("input_target_missing", uncertain=True)
        if record["state"] == "receiving":
            self._download(root_fd, request, record, deadline)
        self._verify(root_fd, "." + request.grant_id + ".part", request, record)
        self._check(request, deadline)
        self._check_root(root, root_fd)
        self._publish(root_fd, request)
        self._verify(root_fd, request.filename, request, record)
        self._check_root(root, root_fd)
        self.journal.mark(request.command_id, "ready", now=self.clock())
        return request.receipt()

    def _pending_receipt(self, record: Mapping[str, Any]) -> ReceiveReceipt:
        receipt, command = json.loads(record["receipt"]), json.loads(record["command"])
        if type(command) is not dict:
            raise ArtifactReceiveError("receive_journal_corrupt")
        request = ReceiveRequest.parse(command, node_id=self.node_id, public_key=self.public_key,
            now=self.clock(), max_bytes=self.max_bytes, allow_expired=True)
        validate_receipt(request, receipt)
        if request.command_digest != record["command_digest"]:
            raise ArtifactReceiveError("receive_journal_corrupt")
        directory = self.inputs.get(request.input_alias)
        if directory is None or request.suffix not in directory.suffixes:
            raise ArtifactReceiveError("invalid_input_alias")
        with _root_handle(directory.root) as root_fd:
            info = _file_info(root_fd)
            if (str(info.st_dev), str(info.st_ino)) != (record["root_device"], record["root_inode"]):
                raise ArtifactReceiveError("unsafe_input_path")
            self._verify(root_fd, request.filename, request, record)
            self._check_root(directory.root, root_fd)
        return receipt

    def pending_receipts(self, *, limit: int = 20) -> tuple[tuple[str, ReceiveReceipt], ...]:
        pending: list[tuple[str, ReceiveReceipt]] = []
        for record in self.journal.pending(limit=limit):
            command_id = record["command_id"]
            try:
                receipt = self._pending_receipt(record)
            except (ArtifactReceiveError, OSError, ValueError, TypeError) as exc:
                self.journal.reconcile(command_id, getattr(exc, "code", "receive_journal_corrupt"), now=self.clock())
                raise ArtifactReceiveError("receive_reconciliation_required", uncertain=True) from exc
            pending.append((command_id, receipt))
        return tuple(pending)

    def recover(self, *, limit: int = 20) -> tuple[tuple[str, str], ...]:
        errors: list[tuple[str, str]] = []
        for command in self.journal.incomplete(limit=limit):
            try:
                self.receive(command)
            except ArtifactReceiveError as exc:
                errors.append((str(command.get("command_id", "")), exc.code))
                if exc.code not in {"receive_busy", "receive_concurrency_limit", "receive_io_failed"}:
                    self.journal.reconcile(str(command.get("command_id", "")), exc.code, now=self.clock())
        return tuple(errors)

    def acknowledge(self, command_id: str, receipt: ReceiveReceipt) -> None:
        self.journal.acknowledge(command_id, receipt, now=self.clock())

    @contextmanager
    def open_received(self, receipt: ReceiveReceipt, *, workspace_id: str, workflow_id: str,
                      generation: int, input_digest: str) -> Iterator[BinaryIO]:
        """Consume only an original signed, ready grant through its pinned descriptor."""
        if not self.available or type(receipt) is not dict:
            raise ArtifactReceiveError("capability_unavailable")
        record = self.journal.get("receive-" + str(receipt.get("grant_id", "")))
        try:
            command = json.loads(record["command"])
            request = ReceiveRequest.parse(command, node_id=self.node_id, public_key=self.public_key,
                now=self.clock(), max_bytes=self.max_bytes, allow_expired=True)
            validate_receipt(request, receipt)
            validate_receipt(request, json.loads(record["receipt"]))
            arguments = command["arguments"]
            if (record["state"] != "ready" or record["command_digest"] != request.command_digest
                    or record["grant_id"] != request.grant_id or command["resource_id"] != workspace_id
                    or tuple(arguments[key] for key in ("workflow_id", "step_key", "generation", "ref_key", "input_digest")) != (
                        workflow_id, "test", generation, "source", input_digest)):
                raise ArtifactReceiveError("receive_scope_unavailable")
            directory = self.inputs.get(request.input_alias)
            if directory is None or request.suffix != ".tar" or request.suffix not in directory.suffixes:
                raise ArtifactReceiveError("invalid_input_alias")
            with _root_handle(directory.root) as root_fd, self._locked(root_fd, request):
                info = _file_info(root_fd)
                if (str(info.st_dev), str(info.st_ino)) != (record["root_device"], record["root_inode"]):
                    raise ArtifactReceiveError("unsafe_input_path")
                with os.fdopen(_open_input(root_fd, request.filename, writable=False), "rb") as source:
                    info = _file_info(source.fileno())
                    self._identity(info, record)
                    if info.st_size != request.size:
                        raise ArtifactReceiveError("artifact_hash_mismatch")
                    digest = hashlib.sha256()
                    remaining = request.size
                    while remaining:
                        if self.should_cancel():
                            raise ArtifactReceiveError("receive_cancelled")
                        chunk = source.read(min(CHUNK_BYTES, remaining))
                        if not chunk:
                            raise ArtifactReceiveError("artifact_hash_mismatch")
                        digest.update(chunk)
                        remaining -= len(chunk)
                    if source.read(1) or digest.hexdigest() != request.sha256:
                        raise ArtifactReceiveError("artifact_hash_mismatch")
                    self._check_root(directory.root, root_fd)
                    source.seek(0)
                    yield source
                    self._identity(_file_info(source.fileno()), record)
                    self._check_root(directory.root, root_fd)
        except ArtifactReceiveError:
            raise
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ArtifactReceiveError("receive_source_unavailable") from exc


__all__ = ["ArtifactReceiver", "ArtifactReceiveError", "InputDirectory"]
