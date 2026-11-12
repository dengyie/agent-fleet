"""Bounded, command-fenced transfers with durable filesystem reconciliation."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import time
from contextlib import contextmanager
from itertools import islice
from pathlib import Path
from typing import Any, Callable, Iterator

from hub.domain.artifact_transfer import ArtifactTransferError, SHA256_RE, TransferLimits, TransferManifest, validate_part, validate_part_metadata, validate_reservation
from hub.infrastructure.artifact_transfer_repository import ArtifactTransferRepository, MAX_TRANSFERS
from hub.platform.artifacts import ArtifactError, ArtifactStore
from hub.platform.artifact_lock import artifact_snapshot_barrier


class ArtifactTransferServiceError(ArtifactTransferError):
    """Transfer failure with a stable public code and retained exception cause."""


class ArtifactTransferService:
    def __init__(self, repository: ArtifactTransferRepository, artifacts: ArtifactStore, *,
                 limits: TransferLimits = TransferLimits(), clock: Callable[[], float] = time.time,
                 identity: Callable[[], str] = lambda: secrets.token_urlsafe(24)) -> None:
        self.repository = repository
        self.artifacts = artifacts
        self.limits = limits
        self.clock = clock
        self.identity = identity
        self.staging_root = artifacts.root.parent / ("." + artifacts.root.name + ".transfers")

    @staticmethod
    def _children(directory: Path, limit: int) -> list[Path]:
        try:
            children = list(islice(directory.iterdir(), limit + 1))
        except OSError as exc:
            raise ArtifactTransferServiceError("staging_unavailable") from exc
        if len(children) > limit:
            raise ArtifactTransferServiceError("transfer_scan_limit")
        return children

    def _directory(self, transfer_id: str) -> Path:
        if (not isinstance(transfer_id, str) or not 1 <= len(transfer_id) <= 128
                or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for ch in transfer_id)):
            raise ArtifactTransferServiceError("transfer_not_found")
        directory = self.staging_root / transfer_id
        if self.staging_root.is_symlink() or directory.is_symlink():
            raise ArtifactTransferServiceError("unsafe_staging_path")
        return directory

    @contextmanager
    def _locked(self, transfer_id: str) -> Iterator[Path]:
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
            raise ArtifactTransferServiceError("capability_unavailable")
        import fcntl
        directory = self._directory(transfer_id)
        descriptor = None
        try:
            self.staging_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.mkdir(mode=0o700, exist_ok=True)
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ArtifactTransferServiceError("transfer_busy") from exc
            yield directory
        except ArtifactTransferError:
            raise
        except ArtifactError as exc:
            raise ArtifactTransferServiceError(exc.code) from exc
        except OSError as exc:
            raise ArtifactTransferServiceError("staging_unavailable") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _load(self, transfer_id: str, *, owner_id: str, node_id: str, worker_id: str,
              command_id: str | None = None) -> tuple[TransferManifest, tuple[tuple[int, int, str], ...], dict[str, Any]]:
        current = self.repository.get(transfer_id, owner_id=owner_id, node_id=node_id)
        row = self.repository.reservation(transfer_id) if current is not None else None
        if (current is None or row is None or row["command_worker_id"] != worker_id
                or (command_id is not None and row["command_id"] != command_id)):
            raise ArtifactTransferServiceError("transfer_not_found")
        return current[0], current[1], row

    def _read_part(self, path: Path) -> bytes:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > self.limits.chunk_size:
                raise ArtifactTransferServiceError("transfer_corrupt")
            payload = handle.read(self.limits.chunk_size + 1)
        if len(payload) > self.limits.chunk_size:
            raise ArtifactTransferServiceError("transfer_corrupt")
        return payload

    def _account(self, manifest: TransferManifest, directory: Path) -> list[Path]:
        children = self._children(directory, 257)
        amount = 0
        publication = 0
        for path in children:
            if path.name == ".publication":
                if path.is_symlink() or not path.is_dir():
                    raise ArtifactTransferServiceError("unsafe_staging_path")
                for item in self._children(path, 2):
                    if item.name not in {"manifest.json", "content"} or item.is_symlink() or not item.is_file():
                        raise ArtifactTransferServiceError("unsafe_staging_path")
                    if item.name == "content":
                        publication += item.stat().st_size
                continue
            if path.is_symlink() or not path.is_file():
                raise ArtifactTransferServiceError("unsafe_staging_path")
            amount += path.stat().st_size
        final = self.artifacts.root / manifest.artifact_id / "content"
        if manifest.state != "ready" and final.exists():
            if final.is_symlink() or not final.is_file():
                raise ArtifactTransferServiceError("unsafe_staging_path")
            publication += final.stat().st_size
        self.repository.reconcile_staging_bytes(manifest.transfer_id, amount,
            publication_bytes=publication, now=float(self.clock()))
        return children

    def _reconcile_parts(self, manifest: TransferManifest, parts: tuple[tuple[int, int, str], ...],
                         directory: Path, *, worker_id: str, adopt: bool = False) -> None:
        children = self._account(manifest, directory)
        by_offset = {str(offset): (offset, length, digest) for offset, length, digest in parts}
        present = set()
        for path in children:
            if path.name == ".publication":
                continue
            if not path.name.isdecimal() or str(int(path.name)) != path.name:
                raise ArtifactTransferServiceError("transfer_corrupt")
            payload = self._read_part(path)
            digest = hashlib.sha256(payload).hexdigest()
            validate_part(offset=int(path.name), length=len(payload), payload=payload, declared_hash=digest,
                total_size=manifest.size, chunk_size=manifest.chunk_size, max_parts=self.limits.max_parts, existing_parts=len(parts))
            prior = by_offset.get(path.name)
            if prior is not None and prior != (int(path.name), len(payload), digest):
                raise ArtifactTransferServiceError("part_hash_mismatch")
            if prior is None and adopt and manifest.state in {"reserved", "uploading"}:
                self.repository.record_part(manifest, worker_id=worker_id, offset=int(path.name), length=len(payload),
                    sha256=digest, now=float(self.clock()), limits=self.limits)
            if prior is None and not adopt:
                raise ArtifactTransferServiceError("transfer_reconciliation_required")
            present.add(path.name)
        if set(by_offset) - present:
            raise ArtifactTransferServiceError("transfer_corrupt")

    def _legacy_ready_bytes(self, owner_id: str, workspace_id: str) -> int:
        known = {manifest.artifact_id for manifest in self.repository.manifests()}
        if not self.artifacts.root.exists():
            return 0
        total = 0
        for directory in self._children(self.artifacts.root, MAX_TRANSFERS):
            if directory.name in known:
                continue
            path = directory / "manifest.json"
            if directory.is_symlink() or not directory.is_dir() or path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
                raise ArtifactTransferServiceError("transfer_reconciliation_required")
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ArtifactTransferServiceError("artifact_corrupt") from exc
            if type(raw) is not dict or "schema_version" in raw or raw.get("ready", True) is not True:
                raise ArtifactTransferServiceError("transfer_reconciliation_required")
            size = raw.get("size")
            if type(size) is not int or size < 0 or size > self.artifacts.MAX_BYTES:
                raise ArtifactTransferServiceError("artifact_corrupt")
            if (raw.get("owner_id"), raw.get("workspace_id")) == (owner_id, workspace_id):
                total += size
        return total

    def reserve(self, *, command_id: str, owner_id: str, node_id: str, worker_id: str,
                output_key: str, name: str, size: int, sha256: str,
                content_type: str = "application/octet-stream") -> TransferManifest:
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
            raise ArtifactTransferServiceError("capability_unavailable")
        validate_reservation(size=size, sha256=sha256, name=name, limits=self.limits)
        if (not isinstance(content_type, str) or not content_type or len(content_type) > 120
                or any(ord(char) < 0x20 or ord(char) == 0x7f for char in content_type)):
            raise ArtifactTransferServiceError("invalid_content_type")
        existing = self.repository.find_by_command(command_id, output_key)
        if existing:
            prior = self.repository.reservation(existing["transfer_id"])
            if prior["state"] == "ready":
                return self.repository.reserve(command_id=command_id, owner_id=owner_id, node_id=node_id,
                    worker_id=worker_id, output_key=output_key, name=name, size=size, sha256=sha256,
                    transfer_id=existing["transfer_id"], artifact_id=existing["artifact_id"], content_type=content_type,
                    limits=self.limits, now=float(self.clock()))
        scope = self.repository.command_scope(command_id=command_id, owner_id=owner_id, node_id=node_id,
            worker_id=worker_id, output_key=output_key, now=float(self.clock()))
        self._reconcile_usage()
        with artifact_snapshot_barrier(self.artifacts.root):
            legacy = self._legacy_ready_bytes(owner_id, scope.workspace_id)
            manifest = self.repository.reserve(command_id=command_id, owner_id=owner_id, node_id=node_id,
                worker_id=worker_id, output_key=output_key, name=name, size=size, sha256=sha256,
                transfer_id=existing["transfer_id"] if existing else self.identity(),
                artifact_id=existing["artifact_id"] if existing else "transfer-" + self.identity(), content_type=content_type,
                limits=self.limits, now=float(self.clock()), legacy_ready_bytes=legacy)
        try:
            with self._locked(manifest.transfer_id):
                self.artifacts._sync_directory(self.staging_root)
                return manifest
        except ArtifactTransferError as exc:
            if exc.code != "transfer_busy":
                self.repository.state(manifest.transfer_id, "failed", code=exc.code, now=float(self.clock()))
            raise

    def status(self, transfer_id: str, *, owner_id: str, node_id: str, worker_id: str,
               command_id: str | None = None, credential_fingerprint: str | None = None) -> dict[str, Any]:
        self._load(transfer_id, owner_id=owner_id, node_id=node_id, worker_id=worker_id, command_id=command_id)
        with self._locked(transfer_id) as directory:
            manifest, parts, row = self._load(transfer_id, owner_id=owner_id, node_id=node_id, worker_id=worker_id, command_id=command_id)
            self._recover_locked(manifest, parts, row, directory, credential_fingerprint=credential_fingerprint)
            manifest, parts, row = self._load(transfer_id, owner_id=owner_id, node_id=node_id, worker_id=worker_id, command_id=command_id)
            return {"transfer_id": transfer_id, "artifact_id": manifest.artifact_id, "state": manifest.state,
                "size": manifest.size, "sha256": manifest.sha256, "chunk_size": manifest.chunk_size,
                "expires_at": manifest.expires_at, "failure_code": row["failure_code"],
                "parts": [{"offset": offset, "length": length, "sha256": digest} for offset, length, digest in parts]}

    def put_part(self, transfer_id: str, *, owner_id: str, node_id: str, worker_id: str, offset: int,
                 payload: bytes, declared_hash: str, content_length: int | None,
                 command_id: str | None = None) -> dict[str, Any]:
        if type(payload) is not bytes or type(content_length) is not int or content_length != len(payload):
            raise ArtifactTransferServiceError("invalid_content_length")
        if len(payload) > self.limits.chunk_size:
            raise ArtifactTransferServiceError("part_too_large")
        self._load(transfer_id, owner_id=owner_id, node_id=node_id, worker_id=worker_id, command_id=command_id)
        with self._locked(transfer_id) as directory:
            manifest, parts, row = self._load(transfer_id, owner_id=owner_id, node_id=node_id, worker_id=worker_id, command_id=command_id)
            if manifest.state not in {"reserved", "uploading"}:
                raise ArtifactTransferServiceError("transfer_state_conflict")
            self._check_writable(manifest, row, worker_id)
            digest = validate_part(offset=offset, length=len(payload), payload=payload, declared_hash=declared_hash,
                total_size=manifest.size, chunk_size=manifest.chunk_size, max_parts=self.limits.max_parts, existing_parts=len(parts))
            path = directory / str(offset)
            try:
                if path.exists() or path.is_symlink():
                    if self._read_part(path) != payload:
                        raise ArtifactTransferServiceError("part_conflict")
                else:
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, "wb") as output:
                        output.write(payload)
                        output.flush()
                        os.fsync(output.fileno())
                    self.artifacts._sync_directory(directory)
                self._account(manifest, directory)
                self.repository.record_part(manifest, worker_id=worker_id, offset=offset, length=len(payload),
                    sha256=digest, now=float(self.clock()), limits=self.limits)
            except OSError as exc:
                self._account(manifest, directory)
                self.repository.state(transfer_id, "failed", code="staging_write_failed", now=float(self.clock()))
                raise ArtifactTransferServiceError("staging_write_failed") from exc
            return {"offset": offset, "length": len(payload), "sha256": digest}

    def prepare_part(self, transfer_id: str, *, owner_id: str, node_id: str, worker_id: str,
                     command_id: str, offset: int, length: int, declared_hash: str) -> tuple[float, str]:
        manifest, _parts, row = self._load(transfer_id, owner_id=owner_id, node_id=node_id, worker_id=worker_id, command_id=command_id)
        if manifest.state not in {"reserved", "uploading"}:
            raise ArtifactTransferServiceError("transfer_state_conflict")
        authorized_until = self._check_writable(manifest, row, worker_id)
        validate_part_metadata(offset=offset, length=length, total_size=manifest.size,
            chunk_size=manifest.chunk_size, max_parts=self.limits.max_parts)
        if not SHA256_RE.fullmatch(declared_hash):
            raise ArtifactTransferServiceError("invalid_hash")
        expiry = min(row["expires_at"], row["absolute_expires_at"])
        return min(expiry, authorized_until), "reservation_expired" if expiry <= authorized_until else "artifact_command_unavailable"

    def _check_writable(self, manifest: TransferManifest, row: dict[str, Any], worker_id: str) -> float:
        if manifest.state not in {"reserved", "uploading", "verifying", "publishing"}:
            raise ArtifactTransferServiceError("transfer_state_conflict")
        now = float(self.clock())
        if row["expires_at"] <= now or row["absolute_expires_at"] <= now:
            self.repository.state(manifest.transfer_id, "failed", code="reservation_expired", now=now)
            raise ArtifactTransferServiceError("reservation_expired")
        return self.repository.authorize(manifest, worker_id=worker_id, now=now)

    def commit(self, transfer_id: str, *, owner_id: str, node_id: str, worker_id: str,
               command_id: str, size: int, sha256: str, credential_fingerprint: str | None = None) -> dict[str, Any]:
        self._load(transfer_id, owner_id=owner_id, node_id=node_id, worker_id=worker_id, command_id=command_id)
        with self._locked(transfer_id) as directory:
            manifest, parts, row = self._load(transfer_id, owner_id=owner_id, node_id=node_id, worker_id=worker_id, command_id=command_id)
            if type(size) is not int or (size, sha256) != (manifest.size, manifest.sha256):
                raise ArtifactTransferServiceError("transfer_conflict")
            if manifest.state in {"ready", "verifying", "publishing", "unknown"}:
                self._recover_locked(manifest, parts, row, directory, credential_fingerprint=credential_fingerprint)
                manifest, parts, row = self._load(transfer_id, owner_id=owner_id, node_id=node_id, worker_id=worker_id, command_id=command_id)
                if manifest.state == "ready":
                    return self.artifacts.get(owner_id, manifest.scope.workspace_id, manifest.artifact_id)
            self._check_writable(manifest, row, worker_id)
            self._reconcile_parts(manifest, parts, directory, worker_id=worker_id)
            self._validate_layout(manifest, parts)
            return self._publish(manifest, parts, row, directory, credential_fingerprint=credential_fingerprint)

    @staticmethod
    def _validate_layout(manifest: TransferManifest, parts: tuple[tuple[int, int, str], ...]) -> None:
        cursor = 0
        for offset, length, _digest in parts:
            if offset != cursor or length != min(manifest.chunk_size, manifest.size - offset) or length <= 0:
                raise ArtifactTransferServiceError("missing_or_overlapping_part")
            cursor += length
        if cursor != manifest.size:
            raise ArtifactTransferServiceError("missing_or_overlapping_part")

    def _chunks(self, directory: Path, parts: tuple[tuple[int, int, str], ...]) -> Iterator[bytes]:
        for offset, length, digest in parts:
            payload = self._read_part(directory / str(offset))
            if len(payload) != length or hashlib.sha256(payload).hexdigest() != digest:
                raise ArtifactTransferServiceError("part_hash_mismatch")
            yield payload

    def _publish(self, manifest: TransferManifest, parts: tuple[tuple[int, int, str], ...],
                 row: dict[str, Any], directory: Path, *, credential_fingerprint: str | None = None) -> dict[str, Any]:
        try:
            with artifact_snapshot_barrier(self.artifacts.root):
                self.repository.begin_verify(manifest, now=float(self.clock()), limits=self.limits,
                    legacy_ready_bytes=self._legacy_ready_bytes(manifest.scope.owner_id, manifest.scope.workspace_id))
                self.repository.publishing(manifest.transfer_id, now=float(self.clock()))
                provenance = self._provenance(manifest, row)
                self.artifacts.put_stream(manifest.scope.owner_id, manifest.scope.workspace_id, self._chunks(directory, parts),
                    name=manifest.name, content_type=row["content_type"], artifact_id=manifest.artifact_id,
                    expected_size=manifest.size, expected_sha256=manifest.sha256, provenance_ref=provenance,
                    staging_directory=directory / ".publication", barrier_held=True)
                self.repository.publish_ready(manifest, now=float(self.clock()), credential_fingerprint=credential_fingerprint)
                return self.artifacts.mark_ready(manifest.scope.owner_id, manifest.scope.workspace_id,
                    manifest.artifact_id, barrier_held=True)
        except (ArtifactTransferError, ArtifactError, OSError) as exc:
            current = self.repository.reservation(manifest.transfer_id)
            self._account(self.repository._manifest(current), directory)
            if current["state"] in {"verifying", "publishing", "unknown"}:
                self.repository.state(manifest.transfer_id, "unknown", code=getattr(exc, "code", "publication_failed"), now=float(self.clock()))
            if isinstance(exc, ArtifactTransferError):
                raise
            raise ArtifactTransferServiceError(getattr(exc, "code", "publication_failed")) from exc

    @staticmethod
    def _provenance(manifest: TransferManifest, row: dict[str, Any]) -> dict[str, Any]:
        return {"workflow_id": manifest.scope.workflow_id, "step_key": manifest.scope.step_key,
            "generation": manifest.scope.generation, "output_key": manifest.scope.output_key,
            "producer_binding": {"command_id": manifest.scope.command_id}, "input_subject_digest": row["input_digest"]}

    def _reconcile_usage(self) -> None:
        manifests = self._scan()
        for manifest in manifests:
            with self._locked(manifest.transfer_id) as directory:
                manifest = self.repository.get(manifest.transfer_id, owner_id=manifest.scope.owner_id, node_id=manifest.scope.node_id)[0]
                self._account(manifest, directory)
                if manifest.state in {"reserved", "uploading"} and manifest.expires_at <= float(self.clock()):
                    self.repository.state(manifest.transfer_id, "failed", code="reservation_expired", now=float(self.clock()))

    def _scan(self, *, limit: int = MAX_TRANSFERS) -> tuple[TransferManifest, ...]:
        with artifact_snapshot_barrier(self.artifacts.root):
            manifests = self.repository.manifests(limit=limit)
            known = {manifest.transfer_id for manifest in manifests}
            if self.staging_root.is_symlink():
                raise ArtifactTransferServiceError("unsafe_staging_path")
            if self.staging_root.exists() and any(path.name not in known for path in self._children(self.staging_root, limit)):
                raise ArtifactTransferServiceError("transfer_reconciliation_required")
            return manifests

    def recover(self, *, limit: int = MAX_TRANSFERS) -> int:
        manifests = self._scan(limit=limit)
        recovered = 0
        for manifest in manifests:
            with self._locked(manifest.transfer_id) as directory:
                current = self.repository.get(manifest.transfer_id, owner_id=manifest.scope.owner_id, node_id=manifest.scope.node_id)
                row = self.repository.reservation(manifest.transfer_id)
                recovered += self._recover_locked(current[0], current[1], row, directory)
        return recovered

    def _recover_locked(self, manifest: TransferManifest, parts: tuple[tuple[int, int, str], ...],
                        row: dict[str, Any], directory: Path, *, credential_fingerprint: str | None = None) -> bool:
        self._account(manifest, directory)
        if manifest.state in {"failed", "quarantined"}:
            return False
        try:
            if manifest.state in {"reserved", "uploading"}:
                if min(row["expires_at"], row["absolute_expires_at"]) <= float(self.clock()):
                    self.repository.state(manifest.transfer_id, "failed", code="reservation_expired", now=float(self.clock()))
                    return False
                self._reconcile_parts(manifest, parts, directory, worker_id=row["command_worker_id"], adopt=True)
                return False
            with artifact_snapshot_barrier(self.artifacts.root):
                if (self.artifacts.root / manifest.artifact_id).exists():
                    verified = self.artifacts.verify_transfer_artifact(manifest.scope.owner_id, manifest.scope.workspace_id,
                        manifest.artifact_id, expected_size=manifest.size, expected_sha256=manifest.sha256)
                    if (verified.get("provenance_ref"), verified["name"], verified["content_type"]) != (self._provenance(manifest, row), row["name"], row["content_type"]):
                        raise ArtifactTransferServiceError("artifact_corrupt")
                    if manifest.state != "ready":
                        self.repository.authorize(manifest, worker_id=row["command_worker_id"], now=float(self.clock()))
                        self.repository.publishing(manifest.transfer_id, now=float(self.clock()))
                    self.repository.publish_ready(manifest, now=float(self.clock()), credential_fingerprint=credential_fingerprint)
                    self.artifacts.mark_ready(manifest.scope.owner_id, manifest.scope.workspace_id, manifest.artifact_id, barrier_held=True)
                    return True
            if manifest.state == "ready":
                raise ArtifactTransferServiceError("artifact_missing")
            self.repository.authorize(manifest, worker_id=row["command_worker_id"], now=float(self.clock()))
            self._validate_layout(manifest, parts)
            self._publish(manifest, parts, row, directory, credential_fingerprint=credential_fingerprint)
            return True
        except (ArtifactTransferError, ArtifactError, OSError) as exc:
            self.repository.state(manifest.transfer_id, "quarantined", code=getattr(exc, "code", "recovery_failed"), now=float(self.clock()))
            manifest_path = self.artifacts.root / manifest.artifact_id / "manifest.json"
            if manifest_path.exists():
                self.artifacts.mark_ready(manifest.scope.owner_id, manifest.scope.workspace_id, manifest.artifact_id, visible=False)
            return False

    def gc_candidates(self) -> tuple[dict[str, Any], ...]:
        """Read-only inventory; no transfer cleanup or unlink operation exists."""
        inventory = self.repository.cleanup_inventory()
        references = self.repository.referenced_ids(tuple(item["artifact_id"] for item in inventory))
        return tuple(item | {"referenced": item["artifact_id"] in references, "dry_run": True}
            for item in inventory)
