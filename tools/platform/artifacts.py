"""Content-addressed, owner-scoped workspace artifact manifests."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any

from platform_schema import validate_id, validate_owner_id
from .artifact_lock import artifact_snapshot_barrier


class ArtifactError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

    def __str__(self):
        return self.code


class ArtifactStore:
    """Persist immutable files and public manifests outside the workspace.

    The store never exposes its absolute storage path. Reads and deletes are
    owner/workspace scoped, and manifests contain only bounded metadata plus a
    stable opaque artifact id.
    """

    MAX_BYTES = 10 * 1024 * 1024
    MAX_PREVIEW_BYTES = 64 * 1024
    MAX_NAME = 256
    PREVIEW_CONTENT_TYPES = frozenset({
        "text/plain", "text/markdown", "text/csv", "application/json",
    })

    def __init__(self, root: Path, *, clock=time.time):
        self.root = Path(root).expanduser().resolve()
        self.clock = clock

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
            return {key: manifest[key] for key in (
                "artifact_id", "owner_id", "workspace_id", "name", "size",
                "sha256", "content_type", "created_at",
            )}
        except (KeyError, TypeError):
            raise ArtifactError("artifact_corrupt") from None

    def put_file(self, owner_id: str, workspace_id: str, source: Path, *,
                 name: str | None = None, content_type: str | None = None) -> dict[str, Any]:
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
        artifact_id = secrets.token_urlsafe(18)
        content_path, manifest_path = self._paths(artifact_id)
        manifest = {
            "artifact_id": artifact_id,
            "owner_id": owner_id,
            "workspace_id": workspace_id,
            "name": display_name,
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "content_type": content_type or mimetypes.guess_type(display_name)[0] or "application/octet-stream",
            "created_at": float(self.clock()),
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
        raw = self.read(owner_id, workspace_id, artifact_id)
        bounded = raw[: self.MAX_PREVIEW_BYTES]
        return {
            "text": bounded.decode("utf-8", errors="replace"),
            "bytes": len(bounded),
            "truncated": len(raw) > self.MAX_PREVIEW_BYTES,
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
            if manifest.get("owner_id") == owner_id and manifest.get("workspace_id") == workspace_id:
                try:
                    out.append(self._public(manifest))
                except ArtifactError:
                    continue
                if len(out) >= bounded_limit:
                    break
        return out


__all__ = ["ArtifactError", "ArtifactStore"]
