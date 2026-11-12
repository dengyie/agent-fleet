"""Content-addressed, owner-scoped workspace artifact manifests."""
from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import os
import secrets
import shutil
import stat
import tempfile
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any, BinaryIO, Iterable

from fleet_core.artifact_contract import ARTIFACT_MAX_BYTES, ArtifactError
from fleet_core.atomic_directory import rename_directory_no_replace
from platform_schema import validate_id, validate_owner_id
from .artifact_lock import artifact_snapshot_barrier


class ArtifactStore:
    """Persist immutable files and public manifests outside the workspace.

    The store never exposes its absolute storage path. Reads and deletes are
    owner/workspace scoped, and manifests contain only bounded metadata plus a
    stable opaque artifact id.
    """

    MAX_BYTES = ARTIFACT_MAX_BYTES
    MAX_PREVIEW_BYTES = 64 * 1024
    MAX_NAME = 256
    STAGING_MAX_AGE_SECONDS = 60 * 60
    STAGING_SCAN_LIMIT = 128
    PREVIEW_CONTENT_TYPES = frozenset({
        "text/plain", "text/markdown", "text/csv", "application/json",
    })
    _CONTENT_TYPES_BY_SUFFIX = {
        ".md": "text/markdown",
        ".markdown": "text/markdown",
        ".txt": "text/plain",
        ".csv": "text/csv",
        ".json": "application/json",
    }

    def __init__(self, root: Path, *, clock=time.time):
        self.root = Path(root).expanduser().resolve()
        self.clock = clock

    @classmethod
    def _staging_name(cls, name: str) -> bool:
        """Recognize only this store's interrupted TemporaryDirectory names."""
        if not isinstance(name, str) or not name.startswith("."):
            return False
        parts = name.split(".")
        if len(parts) != 3 or len(parts[1]) != 24 or not parts[2]:
            return False
        allowed = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
        return all(part and all(char in allowed for char in part) for part in parts[1:])

    def cleanup_staging(self, *, now: float | None = None,
                        max_age_s: int = STAGING_MAX_AGE_SECONDS,
                        limit: int = STAGING_SCAN_LIMIT) -> int:
        """Remove bounded, stale interrupted artifact staging directories.

        Only directories created by ``put_file``/``put_bytes`` are eligible.
        The snapshot barrier is shared with publication and backup so a cleanup
        pass cannot race a writer. Symlink entries are never followed.
        """
        if type(max_age_s) is not int or not 1 <= max_age_s <= 7 * 24 * 60 * 60:
            raise ArtifactError("invalid_staging_age")
        if type(limit) is not int or not 1 <= limit <= self.STAGING_SCAN_LIMIT:
            raise ArtifactError("invalid_staging_limit")
        try:
            timestamp = float(self.clock() if now is None else now)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ArtifactError("invalid_staging_time") from exc
        if not math.isfinite(timestamp):
            raise ArtifactError("invalid_staging_time")
        if not self.root.exists():
            return 0
        cutoff = timestamp - max_age_s
        removed = 0
        try:
            with artifact_snapshot_barrier(self.root):
                entries = []
                for entry in self.root.iterdir():
                    if not self._staging_name(entry.name):
                        continue
                    try:
                        info = entry.lstat()
                    except OSError as exc:
                        raise ArtifactError("staging_cleanup_failed") from exc
                    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                        continue
                    if info.st_mtime > cutoff:
                        continue
                    entries.append((info.st_mtime, entry.name, entry))
                entries.sort(key=lambda item: (item[0], item[1]))
                for _mtime, _name, entry in entries[:limit]:
                    try:
                        info = entry.lstat()
                        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                            continue
                        shutil.rmtree(entry)
                    except OSError as exc:
                        raise ArtifactError("staging_cleanup_failed") from exc
                    removed += 1
        except ArtifactError:
            raise
        except OSError as exc:
            raise ArtifactError("staging_cleanup_failed") from exc
        return removed

    @classmethod
    def _content_type_for_name(cls, name: str) -> str:
        suffix = Path(name).suffix.lower()
        return (
            cls._CONTENT_TYPES_BY_SUFFIX.get(suffix)
            or mimetypes.guess_type(name)[0]
            or "application/octet-stream"
        )

    def _paths(self, artifact_id: str) -> tuple[Path, Path]:
        if not isinstance(artifact_id, str) or not artifact_id or len(artifact_id) > 128:
            raise ArtifactError("invalid_artifact_id")
        if any(ch in artifact_id for ch in "/\\"):
            raise ArtifactError("invalid_artifact_id")
        directory = self.root / artifact_id
        return directory / "content", directory / "manifest.json"

    @staticmethod
    def _public(manifest: dict[str, Any]) -> dict[str, Any]:
        try:
            result = {key: manifest[key] for key in (
                "artifact_id", "owner_id", "workspace_id", "name", "size",
                "sha256", "content_type", "created_at",
            )}
            if manifest.get("schema_version") == 2:
                result.update(schema_version=2, provenance_ref=manifest["provenance_ref"])
            return result
        except (KeyError, TypeError):
            raise ArtifactError("artifact_corrupt") from None


    def put_file(self, owner_id: str, workspace_id: str, source: Path, *,
                 name: str | None = None, content_type: str | None = None,
                 artifact_id: str | None = None) -> dict[str, Any]:
        try:
            owner_id = validate_owner_id(owner_id)
            workspace_id = validate_id(workspace_id, "workspace_id")
        except ValueError:
            raise ArtifactError("invalid_scope") from None
        source = Path(source).expanduser().resolve()
        if not source.is_file():
            raise ArtifactError("not_found")
        try:
            size = source.stat().st_size
        except OSError:
            raise ArtifactError("read_failed") from None
        if size > self.MAX_BYTES:
            raise ArtifactError("artifact_too_large")
        if name is not None and not isinstance(name, str):
            raise ArtifactError("invalid_artifact_name")
        display_name = name or source.name
        if not display_name or len(display_name) > self.MAX_NAME or display_name in {".", ".."}:
            raise ArtifactError("invalid_artifact_name")
        if "/" in display_name or "\\" in display_name or any(ord(char) < 0x20 or ord(char) == 0x7f for char in display_name):
            raise ArtifactError("invalid_artifact_name")
        try:
            raw = source.read_bytes()
        except OSError:
            raise ArtifactError("read_failed") from None
        if len(raw) > self.MAX_BYTES:
            raise ArtifactError("artifact_too_large")
        if content_type is not None and (not isinstance(content_type, str) or not content_type or len(content_type) > 120 or any(ord(char) < 0x20 or ord(char) == 0x7f for char in content_type)):
            raise ArtifactError("invalid_content_type")
        if artifact_id is None:
            artifact_id = secrets.token_urlsafe(18)
        elif not isinstance(artifact_id, str) or not artifact_id or len(artifact_id) > 128 or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for char in artifact_id):
            raise ArtifactError("invalid_artifact_id")
        content_path, manifest_path = self._paths(artifact_id)
        manifest = {
            "artifact_id": artifact_id,
            "owner_id": owner_id,
            "workspace_id": workspace_id,
            "name": display_name,
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "content_type": content_type or self._content_type_for_name(display_name),
            "created_at": float(self.clock()),
        }
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            with artifact_snapshot_barrier(self.root):
                if manifest_path.exists() or content_path.exists():
                    prior = self.get(owner_id, workspace_id, artifact_id)
                    if prior is None or (prior["sha256"], prior["size"], prior["name"], prior["content_type"]) != (
                            manifest["sha256"], manifest["size"], manifest["name"], manifest["content_type"]):
                        raise ArtifactError("artifact_id_conflict")
                    self.read(owner_id, workspace_id, artifact_id)
                    return prior
                with tempfile.TemporaryDirectory(prefix=f".{artifact_id}.", dir=self.root) as temp_name:
                    temp_dir = Path(temp_name)
                    (temp_dir / "content").write_bytes(raw)
                    (temp_dir / "manifest.json").write_text(
                        json.dumps(manifest, ensure_ascii=True, sort_keys=True), encoding="utf-8"
                    )
                    try:
                        rename_directory_no_replace(temp_dir, content_path.parent)
                    except FileExistsError as exc:
                        raise ArtifactError("artifact_id_conflict") from exc
        except ArtifactError:
            raise
        except (OSError, ValueError) as exc:
            raise ArtifactError("write_failed") from exc
        return self._public(manifest)

    def put_stream(self, owner_id: str, workspace_id: str, source: Iterable[bytes], *,
                   name: str, content_type: str, artifact_id: str,
                   expected_size: int, expected_sha256: str,
                   provenance_ref: dict[str, Any], staging_directory: Path,
                   barrier_held: bool = False) -> dict[str, Any]:
        """Copy bounded chunks once, verify, then publish an immutable hidden V2 file.

        The transfer owns its durable staging directory. Neither normal errors
        nor a process crash deletes it; recovery verifies the same directory
        and publishes the same artifact id.
        """
        try:
            owner_id = validate_owner_id(owner_id)
            workspace_id = validate_id(workspace_id, "workspace_id")
        except ValueError as exc:
            raise ArtifactError("invalid_scope") from exc
        if type(expected_size) is not int or not 0 <= expected_size <= 256 * 1024 * 1024:
            raise ArtifactError("invalid_size")
        if (not isinstance(expected_sha256, str) or len(expected_sha256) != 64
                or any(char not in "0123456789abcdef" for char in expected_sha256)):
            raise ArtifactError("invalid_hash")
        if (not isinstance(artifact_id, str) or not artifact_id or len(artifact_id) > 128
                or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for char in artifact_id)):
            raise ArtifactError("invalid_artifact_id")
        if (not isinstance(name, str) or not name or len(name) > self.MAX_NAME or name in {".", ".."}
                or any(char in "/\\" or ord(char) < 0x20 or ord(char) == 0x7f for char in name)):
            raise ArtifactError("invalid_artifact_name")
        if (not isinstance(content_type, str) or not content_type or len(content_type) > 120
                or any(ord(char) < 0x20 or ord(char) == 0x7f for char in content_type)):
            raise ArtifactError("invalid_content_type")
        content_path, manifest_path = self._paths(artifact_id)
        stage = Path(staging_directory)
        manifest = {
            "artifact_id": artifact_id, "owner_id": owner_id, "workspace_id": workspace_id,
            "name": name, "size": expected_size, "sha256": expected_sha256, "content_type": content_type,
            "created_at": float(self.clock()), "ready": False, "schema_version": 2, "provenance_ref": provenance_ref,
        }
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with (nullcontext() if barrier_held else artifact_snapshot_barrier(self.root)):
                if content_path.parent.exists():
                    prior = self.verify_transfer_artifact(owner_id, workspace_id, artifact_id,
                        expected_size=expected_size, expected_sha256=expected_sha256)
                    if (prior["name"], prior["content_type"], prior["provenance_ref"]) != (name, content_type, provenance_ref):
                        raise ArtifactError("artifact_id_conflict")
                    return prior
                if stage.exists() or stage.is_symlink():
                    self._verify_staged_transfer(stage, manifest)
                else:
                    self._write_transfer(stage, source, manifest)
                rename_directory_no_replace(stage, content_path.parent)
                self._sync_directory(self.root)
                self._sync_directory(stage.parent)
        except ArtifactError:
            raise
        except (OSError, ValueError) as exc:
            raise ArtifactError("write_failed") from exc
        return self._public(manifest)

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        if os.name == "nt":
            raise ArtifactError("capability_unavailable")
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _write_transfer(self, stage: Path, source: Iterable[bytes], manifest: dict[str, Any]) -> None:
        stage.mkdir(mode=0o700)
        digest = hashlib.sha256()
        size = 0
        with (stage / "content").open("xb") as output:
            for chunk in source:
                if not isinstance(chunk, bytes) or len(chunk) > 1024 * 1024:
                    raise ArtifactError("invalid_chunk")
                size += len(chunk)
                if size > manifest["size"]:
                    raise ArtifactError("artifact_size_mismatch")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if size != manifest["size"] or digest.hexdigest() != manifest["sha256"]:
            raise ArtifactError("artifact_hash_mismatch")
        with (stage / "manifest.json").open("x", encoding="utf-8") as output:
            json.dump(manifest, output, ensure_ascii=True, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        self._sync_directory(stage)
        self._sync_directory(stage.parent)

    def _verify_staged_transfer(self, stage: Path, manifest: dict[str, Any]) -> None:
        if stage.is_symlink() or not stage.is_dir():
            raise ArtifactError("artifact_corrupt")
        if {item.name for item in stage.iterdir()} != {"content", "manifest.json"}:
            raise ArtifactError("artifact_corrupt")
        raw = self._read_transfer_manifest(stage)
        for key in manifest.keys() - {"created_at", "ready"}:
            if raw.get(key) != manifest[key]:
                raise ArtifactError("artifact_id_conflict")
        self._verify_content(stage / "content", manifest["size"], manifest["sha256"]).close()

    @staticmethod
    def _read_transfer_manifest(directory: Path) -> dict[str, Any]:
        if directory.is_symlink() or not directory.is_dir():
            raise ArtifactError("artifact_corrupt")
        path = directory / "manifest.json"
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
            raise ArtifactError("artifact_corrupt")
        raw = json.loads(path.read_text(encoding="utf-8"))
        if type(raw) is not dict or raw.get("schema_version") != 2 or type(raw.get("ready")) is not bool:
            raise ArtifactError("artifact_corrupt")
        if type(raw.get("provenance_ref")) is not dict:
            raise ArtifactError("artifact_corrupt")
        return raw

    @staticmethod
    def _verify_content(path: Path, expected_size: int, expected_sha256: str) -> BinaryIO:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        handle = os.fdopen(fd, "rb")
        try:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != expected_size:
                raise ArtifactError("artifact_corrupt")
            digest = hashlib.sha256()
            size = 0
            while chunk := handle.read(64 * 1024):
                size += len(chunk)
                if size > expected_size:
                    raise ArtifactError("artifact_corrupt")
                digest.update(chunk)
            if size != expected_size or digest.hexdigest() != expected_sha256:
                raise ArtifactError("artifact_corrupt")
            handle.seek(0)
            return handle
        except BaseException:
            handle.close()
            raise

    def mark_ready(self, owner_id: str, workspace_id: str, artifact_id: str,
                   *, visible: bool = True, barrier_held: bool = False) -> dict[str, Any]:
        """Atomically persist visibility; hidden files never appear in public reads."""
        content_path, manifest_path = self._paths(artifact_id)
        try:
            with (nullcontext() if barrier_held else artifact_snapshot_barrier(self.root)):
                raw = self._read_transfer_manifest(content_path.parent)
                if (raw.get("owner_id"), raw.get("workspace_id")) != (owner_id, workspace_id):
                    raise ArtifactError("not_found")
                if raw["ready"] == visible:
                    return self._public(raw)
                raw["ready"] = visible
                temporary = manifest_path.with_name(".visibility")
                fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    json.dump(raw, output, ensure_ascii=True, sort_keys=True)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, manifest_path)
                self._sync_directory(manifest_path.parent)
                return self._public(raw)
        except ArtifactError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise ArtifactError("write_failed") from exc

    def verify_transfer_artifact(self, owner_id: str, workspace_id: str, artifact_id: str,
                                 *, expected_size: int, expected_sha256: str) -> dict[str, Any]:
        content_path, _manifest_path = self._paths(artifact_id)
        try:
            raw = self._read_transfer_manifest(content_path.parent)
            if (raw.get("owner_id"), raw.get("workspace_id"), raw.get("artifact_id"), raw.get("size"), raw.get("sha256")) != (
                    owner_id, workspace_id, artifact_id, expected_size, expected_sha256):
                raise ArtifactError("artifact_corrupt")
            self._verify_content(content_path, expected_size, expected_sha256).close()
            return self._public(raw)
        except ArtifactError:
            raise
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise ArtifactError("artifact_corrupt") from exc

    def open_verified(self, owner_id: str, workspace_id: str, artifact_id: str) -> BinaryIO:
        """Return the same verified descriptor, rewound, owned by the caller."""
        content_path, _manifest_path = self._paths(artifact_id)
        manifest = self.get(owner_id, workspace_id, artifact_id)
        if manifest is None:
            raise ArtifactError("not_found")
        if content_path.parent.is_symlink() or (content_path.parent / "manifest.json").is_symlink():
            raise ArtifactError("artifact_corrupt")
        try:
            return self._verify_content(content_path, manifest["size"], manifest["sha256"])
        except OSError as exc:
            raise ArtifactError("artifact_corrupt") from exc

    def put_bytes(self, owner_id: str, workspace_id: str, raw: bytes, *,
                  name: str = "screenshot.png", content_type: str = "image/png") -> dict[str, Any]:
        """Persist bounded bytes supplied by a trusted Node upload adapter."""
        try:
            owner_id = validate_owner_id(owner_id)
            workspace_id = validate_id(workspace_id, "workspace_id")
        except ValueError:
            raise ArtifactError("invalid_scope") from None
        if not isinstance(raw, bytes):
            raise ArtifactError("invalid_bytes")
        if len(raw) > self.MAX_BYTES:
            raise ArtifactError("artifact_too_large")
        if not isinstance(name, str) or not name or len(name) > self.MAX_NAME:
            raise ArtifactError("invalid_artifact_name")
        if name in {".", ".."} or "/" in name or "\\" in name or any(
                ord(char) < 0x20 or ord(char) == 0x7f for char in name):
            raise ArtifactError("invalid_artifact_name")
        if (not isinstance(content_type, str) or content_type != "image/png"
                or not raw.startswith(b"\x89PNG\r\n\x1a\n")):
            raise ArtifactError("invalid_content_type")
        artifact_id = secrets.token_urlsafe(18)
        content_path, _manifest_path = self._paths(artifact_id)
        manifest = {
            "artifact_id": artifact_id, "owner_id": owner_id,
            "workspace_id": workspace_id, "name": name, "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "content_type": content_type, "created_at": float(self.clock()),
        }
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            with artifact_snapshot_barrier(self.root):
                with tempfile.TemporaryDirectory(prefix=f".{artifact_id}.", dir=self.root) as temp_name:
                    temp_dir = Path(temp_name)
                    (temp_dir / "content").write_bytes(raw)
                    (temp_dir / "manifest.json").write_text(
                        json.dumps(manifest, ensure_ascii=True, sort_keys=True), encoding="utf-8"
                    )
                    temp_dir.rename(content_path.parent)
        except (OSError, ValueError):
            raise ArtifactError("write_failed") from None
        return self._public(manifest)

    def delete(self, owner_id: str, workspace_id: str, artifact_id: str) -> bool:
        """Delete one immutable artifact only after owner/scope verification."""
        try:
            owner_id = validate_owner_id(owner_id)
            workspace_id = validate_id(workspace_id, "workspace_id")
        except ValueError:
            raise ArtifactError("invalid_scope") from None
        content_path, manifest_path = self._paths(artifact_id)
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return False
        if not isinstance(manifest, dict):
            raise ArtifactError("artifact_corrupt")
        if manifest.get("owner_id") != owner_id or manifest.get("workspace_id") != workspace_id:
            return False
        try:
            with artifact_snapshot_barrier(self.root):
                content_path.parent.mkdir(parents=True, exist_ok=True)
                content_path.unlink(missing_ok=True)
                manifest_path.unlink(missing_ok=True)
                content_path.parent.rmdir()
        except OSError:
            raise ArtifactError("delete_failed") from None
        return True

    def get(self, owner_id: str, workspace_id: str, artifact_id: str) -> dict[str, Any] | None:
        try:
            owner_id = validate_owner_id(owner_id)
            workspace_id = validate_id(workspace_id, "workspace_id")
        except ValueError:
            raise ArtifactError("invalid_scope") from None
        content_path, manifest_path = self._paths(artifact_id)
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(manifest, dict):
            raise ArtifactError("artifact_corrupt")
        if manifest.get("owner_id") != owner_id or manifest.get("workspace_id") != workspace_id:
            return None
        if manifest.get("ready", True) is not True:
            return None
        if "schema_version" in manifest and (type(manifest["schema_version"]) is not int or manifest["schema_version"] != 2):
            raise ArtifactError("artifact_corrupt")
        return self._public(manifest)


    def read(self, owner_id: str, workspace_id: str, artifact_id: str) -> bytes:
        content_path, manifest_path = self._paths(artifact_id)
        manifest = self.get(owner_id, workspace_id, artifact_id)
        if manifest is None:
            raise ArtifactError("not_found")
        try:
            raw = content_path.read_bytes()
        except OSError:
            raise ArtifactError("read_failed") from None
        if len(raw) != manifest["size"] or hashlib.sha256(raw).hexdigest() != manifest["sha256"]:
            raise ArtifactError("artifact_corrupt")
        return raw

    def read_preview(self, owner_id: str, workspace_id: str, artifact_id: str) -> dict[str, Any]:
        """Return a bounded, integrity-checked text projection.

        The complete artifact is verified before truncation so a preview can
        never hide a corrupted tail.  UTF-8 replacement is deliberate: a
        multi-byte character crossing the byte boundary must not make the
        preview endpoint fail or return executable markup.
        """
        manifest = self.get(owner_id, workspace_id, artifact_id)
        if manifest is None:
            raise ArtifactError("not_found")
        content_type = manifest.get("content_type")
        if content_type not in self.PREVIEW_CONTENT_TYPES:
            raise ArtifactError("preview_unsupported")
        with self.open_verified(owner_id, workspace_id, artifact_id) as handle:
            bounded = handle.read(self.MAX_PREVIEW_BYTES)
        return {
            "text": bounded.decode("utf-8", errors="replace"),
            "bytes": len(bounded),
            "truncated": manifest["size"] > self.MAX_PREVIEW_BYTES,
            "content_type": content_type,
        }


    def list(self, owner_id: str, workspace_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        try:
            owner_id = validate_owner_id(owner_id)
            workspace_id = validate_id(workspace_id, "workspace_id")
        except ValueError:
            raise ArtifactError("invalid_scope") from None
        if not self.root.exists():
            return []
        out = []
        bounded_limit = max(1, min(int(limit), 1000))
        for manifest_path in sorted(self.root.glob("*/manifest.json")):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if manifest.get("ready", True) is not True:
                continue
            if manifest.get("owner_id") == owner_id and manifest.get("workspace_id") == workspace_id:
                try:
                    out.append(self._public(manifest))
                except ArtifactError:
                    continue
                if len(out) >= bounded_limit:
                    break
        return out



__all__ = ["ArtifactError", "ArtifactStore"]
