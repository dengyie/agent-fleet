"""Fixed local output exports with pinned files, bounded parts and durable IDs."""
from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import time
from typing import Any, BinaryIO, Protocol

from fleet_core.artifact_export import (
    ArtifactExportError, ENVELOPE_FIELDS, ExportReceipt, ExportRequest, validate_export_receipt,
)
from fleet_core.artifact_receive import ArtifactReceiveError
from fleet_core.artifact_transfer import CHUNK_BYTES, ID_RE, NAME_RE, SHA256_RE
from fleet_core.platform_command import args_hash
from .artifact_export_journal import ArtifactExportJournal
from .artifact_lock import ArtifactSlotError, artifact_io_slot
from .artifact_transfer import _root_handle, hub_origin, export_supported
from . import artifact_windows as windows


class ExportTransport(Protocol):
    def request_bounded_json(self, method: str, url: str, body: bytes | None,
                             headers: dict[str, str], *, deadline: float,
                             should_cancel: Callable[[], bool]) -> tuple[int, dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class OutputFile:
    root: Path
    relative_path: str
    name: str | None = None

    def __post_init__(self) -> None:
        if type(self.relative_path) is not str:
            raise ArtifactExportError("invalid_output_file")
        try:
            path_bytes = self.relative_path.encode("utf-8")
            if os.name == "nt":
                windows.validate_root(str(self.root))
                windows.validate_relative_path(self.relative_path)
        except (UnicodeError, ArtifactReceiveError) as exc:
            raise ArtifactExportError("invalid_output_file") from exc
        root, relative = Path(self.root), Path(self.relative_path)
        if (not root.is_absolute() or ".." in root.parts or not self.relative_path
                or not relative.parts or relative.is_absolute() or ".." in relative.parts or len(relative.parts) > 32
                or len(path_bytes) > 512 or "\\" in self.relative_path
                or any(ord(char) < 0x20 or ord(char) == 0x7f for char in self.relative_path)):
            raise ArtifactExportError("invalid_output_file")
        name = self.name if self.name is not None else relative.name
        if type(name) is not str or NAME_RE.fullmatch(name) is None or name in {".", ".."}:
            raise ArtifactExportError("invalid_output_file")
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "name", name)


def _file_identity(info: os.stat_result) -> dict[str, str]:
    return {key: str(getattr(info, "st_" + key)) for key in
        ("dev", "ino", "size", "mtime_ns", "ctime_ns", "mode", "uid", "nlink")}


@contextmanager
def _output_handle(output: OutputFile) -> Iterator[tuple[BinaryIO, dict[str, str]]]:
    if os.name == "nt":
        with windows.source_handle(output.root, output.relative_path) as value:
            yield value
        return
    with _root_handle(output.root) as root_fd, ExitStack() as handles:
        root_info, directory = os.fstat(root_fd), root_fd
        parts = Path(output.relative_path).parts
        for part in parts[:-1]:
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            handles.callback(os.close, directory)
            info = os.fstat(directory)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
                raise ArtifactExportError("unsafe_output_path")
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        source = handles.enter_context(os.fdopen(descriptor, "rb"))
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) & 0o022):
            raise ArtifactExportError("unsafe_output_path")
        yield source, _file_identity(info) | {"root_dev": str(root_info.st_dev), "root_ino": str(root_info.st_ino)}


class ArtifactExporter:
    def __init__(self, *, node_id: str, credential: str, worker_id: str, hub_url: str,
                 public_key: bytes, outputs: Mapping[tuple[str, str], OutputFile], journal_path: Path,
                 transport: ExportTransport | None = None, slot_path: Path | None = None,
                 max_bytes: int = 64 * 1024 * 1024, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic,
                 should_cancel: Callable[[], bool] = lambda: False) -> None:
        if type(max_bytes) is not int or not 1 <= max_bytes <= 256 * 1024 * 1024:
            raise ArtifactExportError("invalid_export_limit")
        if not isinstance(public_key, bytes) or len(public_key) != 32:
            raise ArtifactExportError("invalid_export_key")
        if (not credential.startswith(node_id + ":") or not worker_id or len(worker_id) > 128
                or any(ord(char) < 0x20 or ord(char) == 0x7f for char in credential + worker_id)):
            raise ArtifactExportError("invalid_export_identity")
        if any(type(key) is not tuple or len(key) != 2 or any(type(value) is not str or not value for value in key)
                or not isinstance(output, OutputFile) for key, output in outputs.items()):
            raise ArtifactExportError("invalid_output_file")
        try:
            self.origin = hub_origin(hub_url)
        except ArtifactReceiveError as exc:
            raise ArtifactExportError(exc.code) from exc
        if transport is None:
            from tools.transport import Transport
            transport = Transport()
        self.node_id, self.credential, self.worker_id = node_id, credential, worker_id
        self.public_key, self.outputs, self.transport = public_key, dict(outputs), transport
        self.max_bytes, self.clock, self.monotonic, self.should_cancel = max_bytes, clock, monotonic, should_cancel
        self.journal = ArtifactExportJournal(journal_path)
        self.journal.init()
        self.slot_path = Path(slot_path or journal_path).absolute()

    @property
    def available(self) -> bool:
        if not self._supported:
            return False
        if os.name != "nt":
            return True
        try:
            for root in {output.root for output in self.outputs.values()} | {self.slot_path.parent}:
                with _root_handle(root):
                    continue
        except (ArtifactReceiveError, OSError):
            return False
        return True

    @property
    def _supported(self) -> bool:
        return export_supported() and bool(self.outputs) and callable(getattr(self.transport, "request_bounded_json", None))

    def _context(self, request: ExportRequest, output: OutputFile) -> str:
        return args_hash({"origin": self.origin, "node": self.node_id, "worker": self.worker_id,
            "workspace": request.workspace_id, "output_key": request.output_key,
            "root": str(output.root), "path": output.relative_path, "name": output.name, "limit": self.max_bytes})

    @contextmanager
    def _slot(self, command_id: str) -> Iterator[None]:
        try:
            with _root_handle(self.slot_path.parent) as directory, artifact_io_slot(directory, self.slot_path.name, 2):
                with artifact_io_slot(directory, "export-" + hashlib.sha256(command_id.encode()).hexdigest(), 1):
                    yield
        except ArtifactSlotError as exc:
            code = "export_concurrency_limit" if exc.code == "transfer_concurrency_limit" else exc.code
            raise ArtifactExportError(code, uncertain=True) from exc

    def _check(self, request: ExportRequest, deadline: float) -> None:
        if self.should_cancel():
            raise ArtifactExportError("export_cancelled", uncertain=True)
        if self.clock() >= request.expires_at or self.monotonic() >= deadline:
            raise ArtifactExportError("export_expired", uncertain=True)

    @staticmethod
    def _unchanged(source: BinaryIO, identity: Mapping[str, str]) -> None:
        current = windows.source_identity(source.fileno()) if os.name == "nt" else _file_identity(os.fstat(source.fileno()))
        if any(value != identity.get(key) for key, value in current.items()):
            raise ArtifactExportError("export_source_changed", uncertain=True)

    def _verify_path(self, source: BinaryIO, output: OutputFile, identity: dict[str, str]) -> None:
        self._unchanged(source, identity)
        with _output_handle(output) as (_current, current_identity):
            if current_identity != identity:
                raise ArtifactExportError("export_source_changed", uncertain=True)

    def _hash(self, source: BinaryIO, identity: dict[str, str], request: ExportRequest, deadline: float) -> str:
        size = int(identity["size"])
        if size > self.max_bytes:
            raise ArtifactExportError("artifact_too_large")
        digest, remaining = hashlib.sha256(), size
        while remaining:
            self._check(request, deadline)
            self._unchanged(source, identity)
            chunk = source.read(min(65536, remaining))
            if not chunk:
                raise ArtifactExportError("export_source_changed", uncertain=True)
            digest.update(chunk)
            remaining -= len(chunk)
        self._unchanged(source, identity)
        self._check(request, deadline)
        source.seek(0)
        return digest.hexdigest()

    def _send(self, method: str, path: str, body: dict[str, Any] | bytes | None, *,
              request: ExportRequest, deadline: float, status: int = 200,
              extra: dict[str, str] | None = None) -> dict[str, Any]:
        self._check(request, deadline)
        data = json.dumps(body, allow_nan=False, separators=(",", ":")).encode("utf-8") if type(body) is dict else body
        headers = {"X-Platform-Node-Credential": self.credential, "X-Platform-Command-ID": request.command_id,
            "X-Platform-Worker-ID": self.worker_id, "Content-Type": "application/json", "Accept-Encoding": "identity"}
        headers.update(extra or {})
        if data is not None:
            headers["Content-Length"] = str(len(data))
        try:
            code, payload = self.transport.request_bounded_json(method,
                self.origin + "/api/platform/v1/node/artifact-transfers" + path, data, headers,
                deadline=deadline, should_cancel=self.should_cancel)
        except OSError as exc:
            raise ArtifactExportError("export_io_failed", uncertain=True) from exc
        except (ValueError, TypeError) as exc:
            raise ArtifactExportError("export_response_invalid", uncertain=True) from exc
        self._check(request, deadline)
        if type(code) is not int:
            raise ArtifactExportError("export_response_invalid", uncertain=True)
        if 300 <= code < 400:
            raise ArtifactExportError("export_redirect_forbidden")
        if code != status:
            raise ArtifactExportError("export_http_failed", uncertain=code == 429 or code >= 500 or code == 0)
        if type(payload) is not dict or payload.get("ok") is not True:
            raise ArtifactExportError("export_response_invalid", uncertain=True)
        return payload

    @staticmethod
    def _reservation(value: Any) -> tuple[str, str, float]:
        if (type(value) is not dict or any(type(value.get(key)) is not str or ID_RE.fullmatch(value[key]) is None
                for key in ("transfer_id", "artifact_id")) or type(value.get("chunk_size")) is not int
                or value["chunk_size"] != CHUNK_BYTES or type(value.get("expires_at")) is not str):
            raise ArtifactExportError("export_response_invalid", uncertain=True)
        try:
            expiry = datetime.fromisoformat(value["expires_at"])
            if expiry.utcoffset() != timedelta(0):
                raise ValueError("expiry must have UTC offset")
            return value["transfer_id"], value["artifact_id"], expiry.timestamp()
        except (ValueError, OverflowError, OSError) as exc:
            raise ArtifactExportError("export_response_invalid", uncertain=True) from exc

    @staticmethod
    def _record(record: Mapping[str, Any], request: ExportRequest) -> None:
        times = (record["started_at"],) if record["state"] == "prepared" else (record["started_at"], record["expires_at"])
        if any(type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= request.expires_at for value in times):
            raise ArtifactExportError("export_journal_corrupt", uncertain=True)
        if record["state"] != "prepared" and any(type(record[key]) is not str or ID_RE.fullmatch(record[key]) is None for key in ("transfer_id", "artifact_id")):
            raise ArtifactExportError("export_journal_corrupt", uncertain=True)

    @staticmethod
    def _parts(value: Any, record: Mapping[str, Any]) -> dict[int, tuple[int, str]]:
        if (type(value) is not dict or any(value.get(key) != record[key] for key in ("transfer_id", "artifact_id", "size", "sha256"))
                or type(value.get("size")) is not int or type(value.get("chunk_size")) is not int
                or value["chunk_size"] != CHUNK_BYTES or value.get("state") not in {"reserved", "uploading", "ready"}
                or type(value.get("parts")) is not list or len(value["parts"]) > 256):
            raise ArtifactExportError("export_response_invalid", uncertain=True)
        parts: dict[int, tuple[int, str]] = {}
        for part in value["parts"]:
            if type(part) is not dict or set(part) != {"offset", "length", "sha256"}:
                raise ArtifactExportError("export_response_invalid", uncertain=True)
            offset, length, digest = part["offset"], part["length"], part["sha256"]
            if (type(offset) is not int or offset < 0 or offset % CHUNK_BYTES or offset >= record["size"]
                    or type(length) is not int or length != min(CHUNK_BYTES, record["size"] - offset)
                    or type(digest) is not str or SHA256_RE.fullmatch(digest) is None or offset in parts):
                raise ArtifactExportError("export_response_invalid", uncertain=True)
            parts[offset] = (length, digest)
        return parts

    def _upload(self, source: BinaryIO, output: OutputFile, identity: dict[str, str],
                request: ExportRequest, record: dict[str, Any], deadline: float) -> ExportReceipt:
        path = "/" + record["transfer_id"]
        status = self._send("GET", path, None, request=request, deadline=deadline).get("transfer")
        parts = self._parts(status, record)
        if status["state"] != "ready":
            for offset in range(0, record["size"], CHUNK_BYTES):
                self._check(request, deadline)
                self._unchanged(source, identity)
                content = source.read(min(CHUNK_BYTES, record["size"] - offset))
                self._verify_path(source, output, identity)
                digest = hashlib.sha256(content).hexdigest()
                expected = (min(CHUNK_BYTES, record["size"] - offset), digest)
                if len(content) != expected[0] or (offset in parts and parts[offset] != expected):
                    raise ArtifactExportError("export_source_changed", uncertain=True)
                if offset in parts:
                    continue
                if record["state"] == "committing":
                    raise ArtifactExportError("export_reconciliation_required", uncertain=True)
                part = self._send("PUT", path + "/parts/" + str(offset), content,
                    request=request, deadline=deadline, extra={"Content-Type": "application/octet-stream", "X-Content-SHA256": digest}).get("part")
                if type(part) is not dict or part != {"offset": offset, "length": len(content), "sha256": digest} or type(part.get("offset")) is not int or type(part.get("length")) is not int:
                    raise ArtifactExportError("export_response_invalid", uncertain=True)
        self._verify_path(source, output, identity)
        self.journal.committing(request.command_id, now=self.clock())
        artifact = self._send("POST", path + "/commit", {"command_id": request.command_id,
            "size": record["size"], "sha256": record["sha256"]}, request=request, deadline=deadline).get("artifact")
        if (type(artifact) is not dict or type(artifact.get("size")) is not int
                or any(artifact.get(key) != record[key] for key in ("artifact_id", "size", "sha256", "name"))
                or artifact.get("workspace_id") != request.workspace_id or type(artifact.get("schema_version")) is not int
                or artifact["schema_version"] != 2
                or type(artifact.get("provenance_ref")) is not dict
                or artifact["provenance_ref"].get("producer_binding") != {"command_id": request.command_id}
                or artifact["provenance_ref"].get("output_key") != request.output_key):
            raise ArtifactExportError("invalid_export_receipt", uncertain=True)
        receipt = validate_export_receipt(request, {"output_key": request.output_key,
            "artifact": {key: artifact[key] for key in ("artifact_id", "size", "sha256")}},
            artifact_id=record["artifact_id"], size=record["size"], sha256=record["sha256"])
        self.journal.ready(request.command_id, receipt, now=self.clock())
        return receipt

    def _reserve(self, request: ExportRequest, output: OutputFile, record: dict[str, Any], deadline: float) -> None:
        reservation = self._send("POST", "", {"command_id": request.command_id,
            "output_key": request.output_key, "name": output.name, "size": record["size"], "sha256": record["sha256"]},
            request=request, deadline=deadline, status=201).get("transfer")
        transfer_id, artifact_id, expiry = self._reservation(reservation)
        if not self.clock() < expiry <= request.expires_at:
            raise ArtifactExportError("export_response_invalid", uncertain=True)
        self.journal.reserve(request.command_id, transfer_id=transfer_id, artifact_id=artifact_id,
            expires_at=expiry, now=self.clock())
        record.update(state="reserved", transfer_id=transfer_id, artifact_id=artifact_id, expires_at=expiry)

    def _run(self, command: Mapping[str, Any], request: ExportRequest, output: OutputFile,
             prior: dict[str, Any] | None) -> ExportReceipt:
        remaining = min(request.expires_at, (prior["started_at"] if prior else self.clock()) + 7200) - self.clock()
        deadline = self.monotonic() + max(0., remaining)
        with _output_handle(output) as (source, identity):
            digest = self._hash(source, identity, request, deadline)
            self._verify_path(source, output, identity)
            record = self.journal.begin(request, command={key: command.get(key) for key in ENVELOPE_FIELDS},
                context_digest=self._context(request, output), source_identity=identity,
                name=output.name, size=int(identity["size"]), sha256=digest, now=self.clock())
            if record["state"] != "committing":
                self.journal.attempt(request.command_id, now=self.clock())
            if record["state"] == "prepared":
                self._reserve(request, output, record, deadline)
            self._record(record, request)
            return self._upload(source, output, identity, request, record, deadline)

    def export(self, command: Mapping[str, Any]) -> ExportReceipt:
        if not self._supported:
            raise ArtifactExportError("capability_unavailable")
        request = ExportRequest.parse(command, node_id=self.node_id, public_key=self.public_key, now=self.clock())
        output = self.outputs.get((request.workspace_id, request.output_key))
        if output is None:
            raise ArtifactExportError("output_alias_unregistered")
        try:
            with self._slot(request.command_id):
                prior = self.journal.get(request.command_id)
                if prior is not None and prior["command_digest"] != request.command_digest:
                    raise ArtifactExportError("export_command_conflict", uncertain=True)
                if prior is not None and prior["state"] == "ready":
                    return self._ready(prior)
                if prior is not None and prior["state"] == "reconciliation":
                    raise ArtifactExportError("export_reconciliation_required", uncertain=True)
                if prior is not None:
                    self._record(prior, request)
                return self._run(command, request, output, prior)
        except (ArtifactReceiveError, OSError) as exc:
            if self.journal.get(request.command_id) is not None:
                self.journal.reconcile(request.command_id, "unsafe_output_path", now=self.clock())
            raise ArtifactExportError("unsafe_output_path", uncertain=True) from exc
        except ArtifactExportError as exc:
            record = self.journal.get(request.command_id)
            permanent = {"export_source_changed", "export_reconciliation_required", "export_journal_corrupt",
                "export_expired", "export_cancelled", "export_redirect_forbidden", "invalid_export_receipt"}
            if record is not None and (not exc.uncertain or exc.code in permanent
                    or (record["attempts"] >= 3 and record["state"] != "committing")):
                self.journal.reconcile(request.command_id, exc.code, now=self.clock())
            raise

    def _ready(self, record: Mapping[str, Any]) -> ExportReceipt:
        try:
            command, receipt = json.loads(record["command"]), json.loads(record["receipt"])
            if type(command) is not dict:
                raise ValueError("export command is not an object")
            request = ExportRequest.parse(command, node_id=self.node_id, public_key=self.public_key, now=self.clock(), allow_expired=True)
            output = self.outputs.get((request.workspace_id, request.output_key))
            if (output is None or request.command_digest != record["command_digest"]
                    or request.command_id != record["command_id"] or self._context(request, output) != record["context_digest"]):
                raise ArtifactExportError("export_journal_corrupt", uncertain=True)
            return validate_export_receipt(request, receipt, artifact_id=record["artifact_id"], size=record["size"], sha256=record["sha256"])
        except (ValueError, TypeError, ArtifactExportError) as exc:
            self.journal.reconcile(record["command_id"], "export_journal_corrupt", now=self.clock())
            raise ArtifactExportError("export_reconciliation_required", uncertain=True) from exc

    def pending_receipts(self, *, limit: int = 20) -> tuple[tuple[str, ExportReceipt], ...]:
        return tuple((record["command_id"], self._ready(record)) for record in self.journal.records(ready=True, limit=limit))

    def recover(self, *, limit: int = 20) -> tuple[tuple[str, str], ...]:
        errors = []
        for record in self.journal.records(ready=False, limit=limit):
            try:
                try:
                    command = json.loads(record["command"])
                except (ValueError, TypeError) as exc:
                    raise ArtifactExportError("export_journal_corrupt", uncertain=True) from exc
                if type(command) is not dict or command.get("command_id") != record["command_id"]:
                    raise ArtifactExportError("export_journal_corrupt", uncertain=True)
                self.export(command)
            except ArtifactExportError as exc:
                errors.append((record["command_id"], exc.code))
                if exc.code in {"invalid_signature", "export_expired", "invalid_export_command", "output_alias_unregistered", "export_journal_corrupt"}:
                    self.journal.reconcile(record["command_id"], exc.code, now=self.clock())
        return tuple(errors)

    def acknowledge(self, command_id: str, receipt: ExportReceipt) -> None:
        self.journal.acknowledge(command_id, receipt, now=self.clock())

    def deferred(self, command_id: str) -> bool:
        record = self.journal.get(command_id)
        return record is not None and record["state"] in {"prepared", "reserved", "committing"}


__all__ = ["ArtifactExporter", "ArtifactExportError", "OutputFile"]
