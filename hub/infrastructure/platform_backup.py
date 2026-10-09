"""Offline, integrity-checked backup and restore for the platform data pair.

The platform database and content-addressed artifact directory are one
recoverable unit.  This module deliberately has no Flask, scheduler, network,
or secret-broker dependency.  A backup is built in a sibling temporary
directory and is published only after every file has been copied and checked.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from tools.platform.artifact_lock import artifact_snapshot_barrier

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


BACKUP_SCHEMA_VERSION = 1
MAX_ARTIFACTS = 10_000
MAX_TOTAL_ARTIFACT_BYTES = 10 * 1024 * 1024 * 1024
BACKUP_FILENAME = "backup.json"
DATABASE_FILENAME = "platform.db"
ARTIFACTS_DIRNAME = "artifacts"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_ENVELOPE_BYTES = 4 * 1024 * 1024
ENCRYPTED_BACKUP_SCHEMA_VERSION = 1
ENCRYPTED_MANIFEST_FILENAME = "encrypted-backup.json"
ENCRYPTED_PAYLOAD_DIRNAME = "payload"
_ENCRYPTED_MANIFEST_BYTES = 4 * 1024 * 1024
_GCM_NONCE_BYTES = 12
_GCM_TAG_BYTES = 16
_ENCRYPTION_CHUNK_BYTES = 1024 * 1024
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")
_ENCRYPTED_MAGIC = b"AFB1"
_MAX_ENCRYPTED_FILES = 2 + (MAX_ARTIFACTS * 2)
_MAX_RETENTION_KEEP = 10_000
_MAX_RETENTION_AGE_S = 10 * 365 * 24 * 60 * 60
_MAX_RETENTION_BYTES = 100 * 1024 * 1024 * 1024


class BackupError(RuntimeError):
    """Stable, secret-free backup/restore failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True)
class BackupRetentionPolicy:
    """Bounded retention inputs for encrypted backup directories."""

    keep_last: int
    max_age_s: float | None
    max_total_bytes: int | None

    def __post_init__(self) -> None:
        if isinstance(self.keep_last, bool) or not isinstance(self.keep_last, int):
            _raise("invalid_retention_policy")
        if self.keep_last < 0 or self.keep_last > _MAX_RETENTION_KEEP:
            _raise("invalid_retention_policy")
        if self.max_age_s is not None:
            try:
                age = float(self.max_age_s)
            except (TypeError, ValueError):
                _raise("invalid_retention_policy")
            if not math.isfinite(age) or age < 0 or age > _MAX_RETENTION_AGE_S:
                _raise("invalid_retention_policy")
        if self.max_total_bytes is not None:
            if (isinstance(self.max_total_bytes, bool)
                    or not isinstance(self.max_total_bytes, int)
                    or self.max_total_bytes < 0
                    or self.max_total_bytes > _MAX_RETENTION_BYTES):
                _raise("invalid_retention_policy")


def _raise(code: str):
    raise BackupError(code)


def _regular_file(path: Path) -> Path:
    """Require a non-symlink regular file without exposing its path."""
    try:
        if path.is_symlink() or not path.is_file():
            _raise("unsafe_path")
    except OSError:
        _raise("unsafe_path")
    return path


def _directory(path: Path, *, missing_code: str = "unsafe_path") -> Path:
    try:
        if path.is_symlink():
            _raise("unsafe_path")
        if not path.exists() or not path.is_dir():
            _raise(missing_code)
    except OSError:
        _raise("unsafe_path")
    return path


def _sha256(path: Path) -> tuple[int, str]:
    _regular_file(path)
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
    except OSError:
        _raise("read_failed")
    return size, digest.hexdigest()


def _safe_artifact_id(value: Any, *, code: str = "invalid_artifact_path") -> str:
    if not isinstance(value, str) or not _SAFE_ID_RE.fullmatch(value):
        _raise(code)
    return value


def _bounded_text(value: Any, *, limit: int, code: str) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        _raise(code)
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        _raise(code)
    return value


def _manifest_bytes(path: Path) -> tuple[dict[str, Any], int, str]:
    _regular_file(path)
    try:
        raw = path.read_bytes()
    except OSError:
        _raise("read_failed")
    if len(raw) > _MAX_MANIFEST_BYTES:
        _raise("manifest_too_large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, TypeError, ValueError):
        _raise("invalid_manifest")
    if not isinstance(value, dict):
        _raise("invalid_manifest")
    return value, len(raw), hashlib.sha256(raw).hexdigest()


def _validate_manifest(value: Mapping[str, Any], artifact_id: str) -> tuple[int, str]:
    if value.get("artifact_id") != artifact_id:
        _raise("invalid_manifest")
    for key in ("owner_id", "workspace_id", "name", "content_type"):
        _bounded_text(value.get(key), limit=512 if key == "name" else 256, code="invalid_manifest")
    name = value["name"]
    if "/" in name or "\\" in name or name in {".", ".."}:
        _raise("invalid_manifest")
    size = value.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        _raise("invalid_manifest")
    if size > MAX_TOTAL_ARTIFACT_BYTES:
        _raise("artifact_too_large")
    digest = value.get("sha256")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        _raise("invalid_manifest")
    created_at = value.get("created_at")
    try:
        created_number = float(created_at)
    except (TypeError, ValueError):
        _raise("invalid_manifest")
    if not math.isfinite(created_number) or created_number < 0:
        _raise("invalid_manifest")
    return size, digest


def _artifact_entry(artifact_root: Path, artifact_dir: Path) -> tuple[dict[str, Any], Path, Path]:
    if artifact_dir.is_symlink() or not artifact_dir.is_dir():
        _raise("unsafe_path")
    artifact_id = _safe_artifact_id(artifact_dir.name)
    try:
        children = {item.name for item in artifact_dir.iterdir()}
    except OSError:
        _raise("read_failed")
    if children != {"manifest.json", "content"}:
        _raise("invalid_artifact_path")
    manifest_path = artifact_dir / "manifest.json"
    content_path = artifact_dir / "content"
    manifest, _manifest_size, manifest_digest = _manifest_bytes(manifest_path)
    expected_size, expected_digest = _validate_manifest(manifest, artifact_id)
    actual_size, actual_digest = _sha256(content_path)
    if actual_size != expected_size:
        _raise("artifact_size_mismatch")
    if actual_digest != expected_digest:
        _raise("artifact_hash_mismatch")
    return {
        "artifact_id": artifact_id,
        "path": f"{ARTIFACTS_DIRNAME}/{artifact_id}",
        "size": actual_size,
        "sha256": actual_digest,
        "manifest_sha256": manifest_digest,
    }, manifest_path, content_path


def _read_artifact_inventory(artifact_root: Path) -> list[tuple[dict[str, Any], Path, Path]]:
    _directory(artifact_root, missing_code="artifact_root_missing")
    try:
        children = sorted(artifact_root.iterdir(), key=lambda item: item.name)
    except OSError:
        _raise("read_failed")
    if len(children) > MAX_ARTIFACTS:
        _raise("artifact_count_exceeded")
    inventory: list[tuple[dict[str, Any], Path, Path]] = []
    total = 0
    for child in children:
        entry, manifest_path, content_path = _artifact_entry(artifact_root, child)
        total += int(entry["size"])
        if total > MAX_TOTAL_ARTIFACT_BYTES:
            _raise("artifact_bytes_exceeded")
        inventory.append((entry, manifest_path, content_path))
    return inventory


def _database_integrity(path: Path) -> tuple[int | None, int, str]:
    size, digest = _sha256(path)
    connection = None
    try:
        connection = sqlite3.connect(str(path), timeout=10)
        row = connection.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            _raise("database_integrity_failed")
        schema_version = None
        try:
            version_row = connection.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if version_row is not None:
                value = version_row[0]
                if isinstance(value, bool):
                    _raise("invalid_database_schema")
                schema_version = int(value)
                if schema_version < 0:
                    _raise("invalid_database_schema")
        except sqlite3.OperationalError:
            # A database without the platform meta table is still a valid
            # SQLite file, but it is represented as an unknown platform schema.
            schema_version = None
        return schema_version, size, digest
    except BackupError:
        raise
    except (sqlite3.Error, OSError, ValueError, TypeError):
        _raise("database_integrity_failed")
    finally:
        if connection is not None:
            connection.close()


def _json_envelope(path: Path) -> dict[str, Any]:
    _regular_file(path)
    try:
        raw = path.read_bytes()
    except OSError:
        _raise("read_failed")
    if len(raw) > _MAX_ENVELOPE_BYTES:
        _raise("backup_metadata_too_large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, TypeError, ValueError):
        _raise("invalid_backup_metadata")
    if not isinstance(value, dict):
        _raise("invalid_backup_metadata")
    return value


def _validate_envelope_shape(envelope: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    version = envelope.get("schema_version")
    if type(version) is not int or version != BACKUP_SCHEMA_VERSION:
        _raise("unsupported_backup_schema")
    database = envelope.get("database")
    artifacts = envelope.get("artifacts")
    if not isinstance(database, Mapping) or not isinstance(artifacts, list):
        _raise("invalid_backup_metadata")
    if len(artifacts) > MAX_ARTIFACTS:
        _raise("artifact_count_exceeded")
    filename = database.get("filename")
    if filename != DATABASE_FILENAME:
        _raise("invalid_backup_metadata")
    db_size = database.get("size")
    db_digest = database.get("sha256")
    if isinstance(db_size, bool) or not isinstance(db_size, int) or db_size < 0:
        _raise("invalid_backup_metadata")
    if not isinstance(db_digest, str) or not _SHA256_RE.fullmatch(db_digest):
        _raise("invalid_backup_metadata")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    total = 0
    for item in artifacts:
        if not isinstance(item, Mapping):
            _raise("invalid_backup_metadata")
        artifact_id = _safe_artifact_id(item.get("artifact_id"))
        if artifact_id in seen:
            _raise("invalid_backup_metadata")
        seen.add(artifact_id)
        relative_path = item.get("path")
        if relative_path != f"{ARTIFACTS_DIRNAME}/{artifact_id}":
            _raise("invalid_artifact_path")
        size = item.get("size")
        digest = item.get("sha256")
        manifest_digest = item.get("manifest_sha256")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            _raise("invalid_backup_metadata")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            _raise("invalid_backup_metadata")
        if not isinstance(manifest_digest, str) or not _SHA256_RE.fullmatch(manifest_digest):
            _raise("invalid_backup_metadata")
        total += size
        if total > MAX_TOTAL_ARTIFACT_BYTES:
            _raise("artifact_bytes_exceeded")
        normalized.append({
            "artifact_id": artifact_id,
            "path": relative_path,
            "size": size,
            "sha256": digest,
            "manifest_sha256": manifest_digest,
        })
    return {"filename": filename, "size": db_size, "sha256": db_digest}, normalized


def _validate_backup_files(backup_root: Path) -> dict[str, Any]:
    _directory(backup_root, missing_code="backup_missing")
    envelope = _json_envelope(backup_root / BACKUP_FILENAME)
    database_meta, artifact_meta = _validate_envelope_shape(envelope)
    database_path = _regular_file(backup_root / DATABASE_FILENAME)
    schema_version, db_size, db_digest = _database_integrity(database_path)
    if db_size != database_meta["size"] or db_digest != database_meta["sha256"]:
        _raise("database_hash_mismatch")
    artifacts_root = _directory(backup_root / ARTIFACTS_DIRNAME, missing_code="artifact_root_missing")
    try:
        actual_dirs = sorted(item.name for item in artifacts_root.iterdir())
    except OSError:
        _raise("read_failed")
    expected_ids = sorted(item["artifact_id"] for item in artifact_meta)
    if actual_dirs != expected_ids:
        _raise("invalid_artifact_path")
    checked_artifacts: list[dict[str, Any]] = []
    by_id = {item["artifact_id"]: item for item in artifact_meta}
    for artifact_id in expected_ids:
        entry, _manifest_path, _content_path = _artifact_entry(
            artifacts_root, artifacts_root / artifact_id
        )
        expected = by_id[artifact_id]
        if entry != expected:
            if entry["manifest_sha256"] != expected["manifest_sha256"]:
                _raise("manifest_hash_mismatch")
            if entry["size"] != expected["size"]:
                _raise("artifact_size_mismatch")
            _raise("artifact_hash_mismatch")
        checked_artifacts.append(entry)
    return {
        "schema_version": BACKUP_SCHEMA_VERSION,
        "platform_schema_version": schema_version,
        "database": database_meta,
        "artifact_count": len(checked_artifacts),
        "artifact_bytes": sum(item["size"] for item in checked_artifacts),
        "artifacts": checked_artifacts,
    }


def _publish_new_directory(stage: Path, target: Path) -> None:
    """Publish a staged directory after an explicit no-overwrite check."""
    if os.path.lexists(target):
        _raise("destination_exists")
    try:
        _rename_directory_no_replace(stage, target)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            _raise("destination_exists")
        _raise("publish_failed")


def _rename_directory_no_replace(stage: Path, target: Path) -> None:
    """Use the platform's atomic directory rename that refuses replacement."""
    if os.name == "nt":
        os.rename(stage, target)
        return

    source = os.fsencode(stage)
    destination = os.fsencode(target)
    libc = ctypes.CDLL(None, use_errno=True)

    if sys.platform == "darwin":
        rename = libc.renamex_np
        rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        result = rename(source, destination, 0x00000004)  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        rename = getattr(libc, "renameat2", None)
        if rename is None:
            raise OSError(errno.ENOTSUP, "no-replace rename unavailable")
        rename.argtypes = (
            ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
            ctypes.c_uint,
        )
        result = rename(-100, source, -100, destination, 0x00000001)
    else:
        raise OSError(errno.ENOTSUP, "no-replace rename unavailable")

    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), os.fspath(target))


def _encryption_key(value: Any) -> bytes:
    if not isinstance(value, (bytes, bytearray)) or len(value) != 32:
        _raise("invalid_encryption_key")
    return bytes(value)


def _encryption_key_id(value: Any) -> str:
    if not isinstance(value, str) or not _KEY_ID_RE.fullmatch(value):
        _raise("invalid_key_id")
    return value


def _encrypted_json(path: Path) -> dict[str, Any]:
    _regular_file(path)
    try:
        raw = path.read_bytes()
    except OSError:
        _raise("encrypted_manifest_read_failed")
    if len(raw) > _ENCRYPTED_MANIFEST_BYTES:
        _raise("encrypted_manifest_too_large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, TypeError, ValueError):
        _raise("invalid_encrypted_manifest")
    if not isinstance(value, dict):
        _raise("invalid_encrypted_manifest")
    return value


def _source_summary(value: Any) -> dict[str, Any]:
    """Validate the public projection of the inner backup contract."""
    if not isinstance(value, Mapping):
        _raise("invalid_encrypted_manifest")
    envelope = {
        "schema_version": value.get("schema_version"),
        "database": value.get("database"),
        "artifacts": value.get("artifacts"),
    }
    database, artifacts = _validate_envelope_shape(envelope)
    artifact_count = value.get("artifact_count")
    artifact_bytes = value.get("artifact_bytes")
    if (isinstance(artifact_count, bool) or not isinstance(artifact_count, int)
            or artifact_count != len(artifacts)):
        _raise("invalid_encrypted_manifest")
    if (isinstance(artifact_bytes, bool) or not isinstance(artifact_bytes, int)
            or artifact_bytes < 0
            or artifact_bytes != sum(item["size"] for item in artifacts)):
        _raise("invalid_encrypted_manifest")
    platform_schema = value.get("platform_schema_version")
    if platform_schema is not None:
        if (isinstance(platform_schema, bool) or not isinstance(platform_schema, int)
                or platform_schema < 0):
            _raise("invalid_encrypted_manifest")
    return {
        "schema_version": BACKUP_SCHEMA_VERSION,
        "platform_schema_version": platform_schema,
        "database": database,
        "artifact_count": artifact_count,
        "artifact_bytes": artifact_bytes,
        "artifacts": artifacts,
    }


def _expected_plain_paths(source: Mapping[str, Any]) -> set[str]:
    paths = {DATABASE_FILENAME, BACKUP_FILENAME}
    for item in source["artifacts"]:
        artifact_id = item["artifact_id"]
        paths.add(f"{ARTIFACTS_DIRNAME}/{artifact_id}/manifest.json")
        paths.add(f"{ARTIFACTS_DIRNAME}/{artifact_id}/content")
    return paths


def _validate_plain_path(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        _raise("invalid_encrypted_manifest")
    if value.startswith(("/", "\\")) or "\\" in value:
        _raise("invalid_encrypted_manifest")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        _raise("invalid_encrypted_manifest")
    return value


def _encrypted_structure(backup_root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    root = _directory(backup_root, missing_code="encrypted_backup_missing")
    # The encrypted root is a deliberately tiny format boundary.  Reject
    # unknown files, directories, and symlinked marker entries before reading
    # the manifest so a backup cannot smuggle unrelated material beside the
    # ciphertext tree.
    try:
        root_children = list(root.iterdir())
    except OSError:
        _raise("encrypted_manifest_read_failed")
    expected_root_names = {ENCRYPTED_MANIFEST_FILENAME, ENCRYPTED_PAYLOAD_DIRNAME}
    actual_root_names = {child.name for child in root_children}
    if actual_root_names != expected_root_names:
        for child in root_children:
            if child.name in expected_root_names and child.is_symlink():
                _raise("unsafe_path")
        _raise("invalid_encrypted_manifest")
    for child in root_children:
        if child.is_symlink():
            _raise("unsafe_path")
    manifest = _encrypted_json(root / ENCRYPTED_MANIFEST_FILENAME)
    if type(manifest.get("schema_version")) is not int:
        _raise("unsupported_encrypted_schema")
    if manifest.get("schema_version") != ENCRYPTED_BACKUP_SCHEMA_VERSION:
        _raise("unsupported_encrypted_schema")
    if manifest.get("format") != "aes-256-gcm-stream-v1":
        _raise("unsupported_encrypted_schema")
    key_id = _encryption_key_id(manifest.get("key_id"))
    created_at = manifest.get("created_at")
    try:
        created_number = float(created_at)
    except (TypeError, ValueError):
        _raise("invalid_encrypted_manifest")
    if not math.isfinite(created_number) or created_number < 0:
        _raise("invalid_encrypted_manifest")
    source = _source_summary(manifest.get("source"))
    files = manifest.get("files")
    if not isinstance(files, list) or not files or len(files) > _MAX_ENCRYPTED_FILES:
        _raise("invalid_encrypted_manifest")
    expected_paths = _expected_plain_paths(source)
    normalized_files: list[dict[str, Any]] = []
    seen: set[str] = set()
    plaintext_total = 0
    expected_payloads: set[str] = set()
    for item in files:
        if not isinstance(item, Mapping):
            _raise("invalid_encrypted_manifest")
        plain_path = _validate_plain_path(item.get("path"))
        if plain_path not in expected_paths or plain_path in seen:
            _raise("invalid_encrypted_manifest")
        seen.add(plain_path)
        payload_path = item.get("payload")
        expected_payload = f"{ENCRYPTED_PAYLOAD_DIRNAME}/{plain_path}.enc"
        if payload_path != expected_payload:
            _raise("invalid_encrypted_manifest")
        size = item.get("size")
        payload_size = item.get("payload_size")
        digest = item.get("sha256")
        payload_digest = item.get("payload_sha256")
        if (isinstance(size, bool) or not isinstance(size, int) or size < 0
                or size > MAX_TOTAL_ARTIFACT_BYTES):
            _raise("invalid_encrypted_manifest")
        if (isinstance(payload_size, bool) or not isinstance(payload_size, int)
                or payload_size != size + len(_ENCRYPTED_MAGIC) + _GCM_NONCE_BYTES + _GCM_TAG_BYTES):
            _raise("invalid_encrypted_manifest")
        if (not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest)
                or not isinstance(payload_digest, str) or not _SHA256_RE.fullmatch(payload_digest)):
            _raise("invalid_encrypted_manifest")
        plaintext_total += size
        if plaintext_total > MAX_TOTAL_ARTIFACT_BYTES + _MAX_MANIFEST_BYTES:
            _raise("artifact_bytes_exceeded")
        expected_payloads.add(payload_path)
        normalized_files.append({
            "path": plain_path,
            "payload": payload_path,
            "size": size,
            "payload_size": payload_size,
            "sha256": digest,
            "payload_sha256": payload_digest,
        })
    if seen != expected_paths:
        _raise("invalid_encrypted_manifest")
    payload_root = _directory(root / ENCRYPTED_PAYLOAD_DIRNAME, missing_code="invalid_encrypted_manifest")
    actual_payloads: set[str] = set()
    actual_dirs: set[str] = set()
    try:
        for current, dirs, filenames in os.walk(payload_root, topdown=True, followlinks=False):
            current_path = Path(current)
            for directory in list(dirs):
                path = current_path / directory
                if path.is_symlink() or not path.is_dir():
                    _raise("unsafe_path")
                actual_dirs.add(path.relative_to(root).as_posix())
            for filename in filenames:
                path = current_path / filename
                if path.is_symlink() or not path.is_file():
                    _raise("unsafe_path")
                actual_payloads.add(path.relative_to(root).as_posix())
    except OSError:
        _raise("encrypted_manifest_read_failed")
    expected_dirs: set[str] = set()
    for payload in expected_payloads:
        parent = payload.rsplit("/", 1)[0]
        while parent and parent != ENCRYPTED_PAYLOAD_DIRNAME:
            expected_dirs.add(parent)
            parent = parent.rsplit("/", 1)[0] if "/" in parent else ENCRYPTED_PAYLOAD_DIRNAME
    if actual_payloads != expected_payloads or actual_dirs != expected_dirs:
        _raise("invalid_encrypted_manifest")
    normalized = {
        "encrypted": True,
        "schema_version": ENCRYPTED_BACKUP_SCHEMA_VERSION,
        "key_id": key_id,
        "created_at": created_number,
        "source": source,
        "artifact_count": source["artifact_count"],
        "artifact_bytes": source["artifact_bytes"],
        "ciphertext_bytes": sum(item["payload_size"] for item in normalized_files),
    }
    return normalized, normalized_files


def _hash_payloads(backup_root: Path, files: list[dict[str, Any]]) -> None:
    root = Path(backup_root)
    for item in files:
        path = root / item["payload"]
        size, digest = _sha256(path)
        if size != item["payload_size"]:
            _raise("ciphertext_size_mismatch")
        if digest != item["payload_sha256"]:
            _raise("ciphertext_hash_mismatch")


def _encrypt_file(source: Path, destination: Path, key: bytes) -> dict[str, Any]:
    source_size, source_digest = _sha256(source)
    nonce = secrets.token_bytes(_GCM_NONCE_BYTES)
    partial = destination.with_name(f".{destination.name}.{secrets.token_hex(8)}.partial")
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as input_file, partial.open("wb") as output_file:
            output_file.write(_ENCRYPTED_MAGIC + nonce)
            while True:
                chunk = input_file.read(_ENCRYPTION_CHUNK_BYTES)
                if not chunk:
                    break
                output_file.write(encryptor.update(chunk))
            output_file.write(encryptor.finalize())
            output_file.write(encryptor.tag)
        payload_size, payload_digest = _sha256(partial)
        os.replace(partial, destination)
    except (OSError, ValueError):
        try:
            partial.unlink()
        except OSError:
            pass
        _raise("encryption_failed")
    return {
        "size": source_size,
        "sha256": source_digest,
        "payload_size": payload_size,
        "payload_sha256": payload_digest,
    }


def _decrypt_file(source: Path, destination: Path, key: bytes) -> None:
    partial = destination.with_name(f".{destination.name}.{secrets.token_hex(8)}.partial")
    try:
        _regular_file(source)
        size = source.stat().st_size
        minimum = len(_ENCRYPTED_MAGIC) + _GCM_NONCE_BYTES + _GCM_TAG_BYTES
        if size < minimum:
            _raise("invalid_encrypted_payload")
        with source.open("rb") as input_file:
            header = input_file.read(len(_ENCRYPTED_MAGIC) + _GCM_NONCE_BYTES)
            if len(header) != len(_ENCRYPTED_MAGIC) + _GCM_NONCE_BYTES or not header.startswith(_ENCRYPTED_MAGIC):
                _raise("invalid_encrypted_payload")
            nonce = header[len(_ENCRYPTED_MAGIC):]
            input_file.seek(-_GCM_TAG_BYTES, os.SEEK_END)
            tag = input_file.read(_GCM_TAG_BYTES)
            input_file.seek(len(header), os.SEEK_SET)
            remaining = size - len(header) - _GCM_TAG_BYTES
            decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
            destination.parent.mkdir(parents=True, exist_ok=True)
            with partial.open("wb") as output_file:
                while remaining:
                    chunk = input_file.read(min(_ENCRYPTION_CHUNK_BYTES, remaining))
                    if not chunk or len(chunk) > remaining:
                        _raise("invalid_encrypted_payload")
                    remaining -= len(chunk)
                    output_file.write(decryptor.update(chunk))
                output_file.write(decryptor.finalize())
        os.replace(partial, destination)
    except InvalidTag:
        try:
            partial.unlink()
        except OSError:
            pass
        _raise("encryption_invalid")
    except BackupError:
        try:
            partial.unlink()
        except OSError:
            pass
        raise
    except (OSError, ValueError):
        try:
            partial.unlink()
        except OSError:
            pass
        _raise("decryption_failed")


def _decrypt_to_plain(backup_root: Path, files: list[dict[str, Any]], key: bytes, plain_root: Path) -> None:
    # The unencrypted backup contract always contains an artifacts directory,
    # including when the source catalog is empty. Directory entries are not
    # encrypted as files, so recreate this required empty root before
    # validating the decrypted pair.
    (plain_root / ARTIFACTS_DIRNAME).mkdir(parents=True, exist_ok=True)
    for item in files:
        destination = plain_root / item["path"]
        if destination.exists() or destination.is_symlink():
            _raise("unsafe_path")
        _decrypt_file(Path(backup_root) / item["payload"], destination, key)
        size, digest = _sha256(destination)
        if size != item["size"]:
            _raise("plaintext_size_mismatch")
        if digest != item["sha256"]:
            _raise("plaintext_hash_mismatch")


class PlatformBackupService:
    """Create, inspect, and restore an offline platform backup pair."""

    def create_backup(self, db_path: Path, artifact_root: Path, target_root: Path) -> dict[str, Any]:
        source_db = Path(db_path).expanduser()
        source_artifacts = Path(artifact_root).expanduser()
        target = Path(target_root).expanduser()
        _regular_file(source_db)
        if os.path.lexists(target):
            _raise("destination_exists")
        _directory(source_artifacts, missing_code="artifact_root_missing")
        target.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=str(target.parent)))
        try:
            # Freeze publication/GC while taking the database snapshot and
            # its artifact closure. Taking inventory first can omit content
            # referenced by a subsequently committed Run.
            with artifact_snapshot_barrier(source_artifacts):
                staged_db = stage / DATABASE_FILENAME
                source = destination = None
                try:
                    source = sqlite3.connect(str(source_db), timeout=10)
                    destination = sqlite3.connect(str(staged_db), timeout=10)
                    source.backup(destination)
                    destination.commit()
                except (sqlite3.Error, OSError):
                    _raise("database_backup_failed")
                finally:
                    if destination is not None:
                        destination.close()
                    if source is not None:
                        source.close()
                inventory = _read_artifact_inventory(source_artifacts)
                artifacts_stage = stage / ARTIFACTS_DIRNAME
                artifacts_stage.mkdir()
                for entry, manifest_path, content_path in inventory:
                    destination_dir = artifacts_stage / entry["artifact_id"]
                    destination_dir.mkdir()
                    shutil.copyfile(manifest_path, destination_dir / "manifest.json")
                    shutil.copyfile(content_path, destination_dir / "content")
            schema_version, db_size, db_digest = _database_integrity(staged_db)
            envelope = {
                "schema_version": BACKUP_SCHEMA_VERSION,
                "platform_schema_version": schema_version,
                "database": {
                    "filename": DATABASE_FILENAME,
                    "size": db_size,
                    "sha256": db_digest,
                },
                "artifacts": [entry for entry, _manifest, _content in inventory],
            }
            (stage / BACKUP_FILENAME).write_text(
                json.dumps(envelope, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            checked = _validate_backup_files(stage)
            _publish_new_directory(stage, target)
            return checked
        except BackupError:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        except (OSError, ValueError, TypeError):
            shutil.rmtree(stage, ignore_errors=True)
            _raise("backup_failed")

    def inspect_backup(self, backup_root: Path) -> dict[str, Any]:
        return _validate_backup_files(Path(backup_root).expanduser())

    def restore_backup(self, backup_root: Path, target_root: Path) -> dict[str, Any]:
        source = Path(backup_root).expanduser()
        target = Path(target_root).expanduser()
        checked = _validate_backup_files(source)
        if os.path.lexists(target):
            _raise("destination_exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=str(target.parent)))
        try:
            shutil.copyfile(source / DATABASE_FILENAME, stage / DATABASE_FILENAME)
            shutil.copyfile(source / BACKUP_FILENAME, stage / BACKUP_FILENAME)
            # Preserve symlink nodes during staging; the second full
            # validation then rejects them without ever traversing outside the
            # backup root.
            shutil.copytree(source / ARTIFACTS_DIRNAME, stage / ARTIFACTS_DIRNAME, symlinks=True)
            staged_checked = _validate_backup_files(stage)
            if staged_checked != checked:
                _raise("restore_validation_failed")
            _publish_new_directory(stage, target)
            return staged_checked
        except BackupError:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        except (OSError, ValueError, TypeError):
            shutil.rmtree(stage, ignore_errors=True)
            _raise("restore_failed")

    def create_encrypted_backup(
        self,
        db_path: Path,
        artifact_root: Path,
        target_root: Path,
        key: bytes,
        key_id: str,
    ) -> dict[str, Any]:
        """Create an authenticated encrypted backup without publishing plaintext."""
        encryption_key = _encryption_key(key)
        normalized_key_id = _encryption_key_id(key_id)
        target = Path(target_root).expanduser()
        if os.path.lexists(target):
            _raise("destination_exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        plain_parent = Path(tempfile.mkdtemp(prefix=f".{target.name}.plain.", dir=str(target.parent)))
        plain_root = plain_parent / "source"
        stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=str(target.parent)))
        try:
            source = self.create_backup(db_path, artifact_root, plain_root)
            files = [
                {"path": DATABASE_FILENAME},
                {"path": BACKUP_FILENAME},
            ]
            for artifact in source["artifacts"]:
                artifact_id = artifact["artifact_id"]
                files.extend([
                    {"path": f"{ARTIFACTS_DIRNAME}/{artifact_id}/manifest.json"},
                    {"path": f"{ARTIFACTS_DIRNAME}/{artifact_id}/content"},
                ])
            for item in files:
                source_path = plain_root / item["path"]
                payload_path = stage / ENCRYPTED_PAYLOAD_DIRNAME / f"{item['path']}.enc"
                details = _encrypt_file(source_path, payload_path, encryption_key)
                item.update({
                    "payload": f"{ENCRYPTED_PAYLOAD_DIRNAME}/{item['path']}.enc",
                    **details,
                })
            manifest = {
                "schema_version": ENCRYPTED_BACKUP_SCHEMA_VERSION,
                "format": "aes-256-gcm-stream-v1",
                "key_id": normalized_key_id,
                "created_at": time.time(),
                "source": source,
                "files": files,
            }
            (stage / ENCRYPTED_MANIFEST_FILENAME).write_text(
                json.dumps(manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            checked, checked_files = _encrypted_structure(stage)
            _hash_payloads(stage, checked_files)
            _publish_new_directory(stage, target)
            return checked | {"verified": True}
        except BackupError:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        except (OSError, ValueError, TypeError):
            shutil.rmtree(stage, ignore_errors=True)
            _raise("encrypted_backup_failed")
        finally:
            shutil.rmtree(plain_parent, ignore_errors=True)

    def inspect_encrypted_backup(
        self,
        backup_root: Path,
        *,
        key: bytes | None = None,
        expected_key_id: str | None = None,
    ) -> dict[str, Any]:
        """Inspect public encrypted metadata, optionally verifying plaintext."""
        metadata, files = _encrypted_structure(Path(backup_root).expanduser())
        _hash_payloads(Path(backup_root).expanduser(), files)
        if expected_key_id is not None and metadata["key_id"] != _encryption_key_id(expected_key_id):
            _raise("key_id_mismatch")
        if key is None:
            return metadata | {"verified": False}
        encryption_key = _encryption_key(key)
        plain_root = Path(tempfile.mkdtemp(prefix=".encrypted-inspect."))
        try:
            _decrypt_to_plain(Path(backup_root).expanduser(), files, encryption_key, plain_root)
            checked = _validate_backup_files(plain_root)
            if checked != metadata["source"]:
                _raise("source_contract_mismatch")
            return metadata | {"verified": True}
        except InvalidTag:
            _raise("encryption_invalid")
        finally:
            shutil.rmtree(plain_root, ignore_errors=True)

    def restore_encrypted_backup(
        self,
        backup_root: Path,
        target_root: Path,
        key: bytes,
        *,
        expected_key_id: str | None = None,
    ) -> dict[str, Any]:
        """Decrypt and atomically publish a validated backup pair."""
        source = Path(backup_root).expanduser()
        target = Path(target_root).expanduser()
        metadata, files = _encrypted_structure(source)
        _hash_payloads(source, files)
        if expected_key_id is not None and metadata["key_id"] != _encryption_key_id(expected_key_id):
            _raise("key_id_mismatch")
        encryption_key = _encryption_key(key)
        if os.path.lexists(target):
            _raise("destination_exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        plain_root = Path(tempfile.mkdtemp(prefix=f".{target.name}.plain.", dir=str(target.parent)))
        stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=str(target.parent)))
        try:
            _decrypt_to_plain(source, files, encryption_key, plain_root)
            checked = _validate_backup_files(plain_root)
            if checked != metadata["source"]:
                _raise("source_contract_mismatch")
            shutil.copytree(plain_root, stage, dirs_exist_ok=True)
            staged_checked = _validate_backup_files(stage)
            if staged_checked != checked:
                _raise("restore_validation_failed")
            _publish_new_directory(stage, target)
            return staged_checked
        except InvalidTag:
            _raise("encryption_invalid")
        except BackupError:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        except (OSError, ValueError, TypeError):
            shutil.rmtree(stage, ignore_errors=True)
            _raise("encrypted_restore_failed")
        finally:
            shutil.rmtree(plain_root, ignore_errors=True)

    @staticmethod
    def _retention_now(value: Any) -> float:
        try:
            now = time.time() if value is None else float(value)
        except (TypeError, ValueError):
            _raise("invalid_retention_now")
        if not math.isfinite(now) or now < 0:
            _raise("invalid_retention_now")
        return now

    def plan_retention(
        self,
        root: Path,
        policy: BackupRetentionPolicy,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Return a deterministic deletion plan; never mutates the root."""
        if not isinstance(policy, BackupRetentionPolicy):
            _raise("invalid_retention_policy")
        backup_root = _directory(Path(root).expanduser(), missing_code="retention_root_missing")
        current = self._retention_now(now)
        complete: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        try:
            children = sorted(backup_root.iterdir(), key=lambda item: item.name)
        except OSError:
            _raise("retention_read_failed")
        for child in children:
            if child.is_symlink():
                skipped.append({"name": child.name, "reason": "unsafe_path"})
                continue
            if not child.is_dir():
                skipped.append({"name": child.name, "reason": "not_directory"})
                continue
            try:
                metadata, files = _encrypted_structure(child)
                _hash_payloads(child, files)
            except BackupError as exc:
                skipped.append({"name": child.name, "reason": exc.code})
                continue
            complete.append({
                "name": child.name,
                "created_at": metadata["created_at"],
                "bytes": sum(item["payload_size"] for item in files),
            })
        complete.sort(key=lambda item: (-item["created_at"], item["name"]))
        mandatory_names = {item["name"] for item in complete[:policy.keep_last]}
        retain_names = set(mandatory_names)
        cutoff = None if policy.max_age_s is None else current - float(policy.max_age_s)
        delete_names: set[str] = set()
        for item in complete[policy.keep_last:]:
            if cutoff is not None and item["created_at"] >= cutoff:
                retain_names.add(item["name"])
            else:
                delete_names.add(item["name"])
        total = sum(item["bytes"] for item in complete if item["name"] in retain_names)
        if policy.max_total_bytes is not None and total > policy.max_total_bytes:
            for item in sorted(
                (candidate for candidate in complete
                 if candidate["name"] in retain_names
                 and candidate["name"] not in mandatory_names),
                key=lambda candidate: (candidate["created_at"], candidate["name"]),
            ):
                if total <= policy.max_total_bytes:
                    break
                retain_names.remove(item["name"])
                delete_names.add(item["name"])
                total -= item["bytes"]
        delete = [item for item in complete if item["name"] in delete_names]
        retain = [item for item in complete if item["name"] not in delete_names]
        return {
            "apply": False,
            "root_name": backup_root.name,
            "now": current,
            "delete": [item["name"] for item in sorted(delete, key=lambda item: (item["created_at"], item["name"]))],
            "retain": [item["name"] for item in sorted(retain, key=lambda item: (-item["created_at"], item["name"]))],
            "skipped": skipped,
            "delete_bytes": sum(item["bytes"] for item in delete),
            "retain_bytes": sum(item["bytes"] for item in retain),
        }

    def prune_retention(
        self,
        root: Path,
        policy: BackupRetentionPolicy,
        *,
        now: float | None = None,
        apply: bool = False,
    ) -> dict[str, Any]:
        plan = self.plan_retention(root, policy, now=now)
        if not apply:
            return plan
        backup_root = _directory(Path(root).expanduser(), missing_code="retention_root_missing")
        deleted: list[str] = []
        for name in plan["delete"]:
            candidate = backup_root / name
            if candidate.is_symlink() or not candidate.is_dir():
                _raise("retention_race")
            # Revalidate the marker and contents immediately before deletion.
            metadata, files = _encrypted_structure(candidate)
            _hash_payloads(candidate, files)
            del metadata
            del files
            shutil.rmtree(candidate)
            deleted.append(name)
        return plan | {"apply": True, "deleted": deleted}


__all__ = [
    "ARTIFACTS_DIRNAME", "BACKUP_FILENAME", "BACKUP_SCHEMA_VERSION",
    "BackupError", "MAX_ARTIFACTS", "MAX_TOTAL_ARTIFACT_BYTES",
    "BackupRetentionPolicy", "PlatformBackupService",
]
