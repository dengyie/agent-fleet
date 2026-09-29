"""Non-Git directory backend with path and output boundaries."""
from __future__ import annotations

import os
import hashlib
import subprocess
from pathlib import Path

from hub.domain.workspace import WorkspaceError, normalize_relative_path
from .base import ToolReceipt

MAX_READ_BYTES = 64 * 1024
MAX_DIGEST_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
ALLOWED_EXECUTABLES = frozenset({
    "cat", "find", "git", "ls", "pwd", "python",
    "python3", "pytest", "rg", "sed",
})


class DirectoryBackend:
    def __init__(self, root: Path, *, quota_bytes: int = 1024 * 1024 * 1024):
        self.root = Path(root).expanduser().resolve()
        self.quota_bytes = int(quota_bytes)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, relative_path: str) -> Path:
        normalized = normalize_relative_path(relative_path) if relative_path else ""
        if not normalized:
            return self.root
        candidate = (self.root / normalized).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            raise WorkspaceError("path_escape") from None
        # Resolve catches symlink escapes for existing paths.  For a new file,
        # validate the resolved parent too.
        parent = candidate.parent.resolve()
        try:
            parent.relative_to(self.root)
        except ValueError:
            raise WorkspaceError("path_escape") from None
        return candidate

    def _receipt(self, command_id, state, result=None, error_code=None, truncated=False):
        return ToolReceipt(command_id, state, result or {}, error_code, truncated)

    def list(self, relative_path: str = ""):
        try:
            path = self._path(relative_path)
            if not path.exists() or not path.is_dir():
                return self._receipt("", "failed", error_code="not_found")
            entries = []
            for item in sorted(path.iterdir(), key=lambda p: p.name)[:1000]:
                entries.append({"name": item.name, "kind": "directory" if item.is_dir() else "file"})
            return self._receipt("", "succeeded", {"path": relative_path, "entries": entries})
        except WorkspaceError as exc:
            return self._receipt("", "failed", error_code=exc.code)

    def read(self, relative_path: str):
        try:
            path = self._path(relative_path)
            if not path.is_file():
                return self._receipt("", "failed", error_code="not_found")
            raw = path.read_bytes()
            truncated = len(raw) > MAX_READ_BYTES
            return self._receipt("", "succeeded", {"path": relative_path, "content": raw[:MAX_READ_BYTES].decode("utf-8", errors="replace")}, truncated=truncated)
        except WorkspaceError as exc:
            return self._receipt("", "failed", error_code=exc.code)
        except OSError:
            return self._receipt("", "failed", error_code="read_failed")

    def digest(self, relative_path: str):
        try:
            path = self._path(relative_path)
            if not path.is_file():
                return self._receipt("", "failed", error_code="not_found")
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_DIGEST_BYTES:
                        return self._receipt("", "failed", error_code="digest_too_large")
                    digest.update(chunk)
            return self._receipt("", "succeeded", {
                "path": relative_path, "sha256": digest.hexdigest(), "size": size,
            })
        except WorkspaceError as exc:
            return self._receipt("", "failed", error_code=exc.code)
        except OSError:
            return self._receipt("", "failed", error_code="digest_failed")

    def write(self, relative_path: str, content: bytes):
        try:
            if not isinstance(content, (bytes, bytearray)):
                return self._receipt("", "failed", error_code="invalid_content")
            path = self._path(relative_path)
            current_size = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
            if current_size + len(content) > self.quota_bytes:
                return self._receipt("", "failed", error_code="disk_quota")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(bytes(content))
            return self._receipt("", "succeeded", {"path": relative_path, "size": len(content)})
        except WorkspaceError as exc:
            return self._receipt("", "failed", error_code=exc.code)
        except OSError:
            return self._receipt("", "failed", error_code="write_failed")

    def execute(self, command_id: str, argv: list[str], *, timeout_s: float = 30.0):
        if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) or len(arg) > 4096 for arg in argv):
            return self._receipt(command_id, "failed", error_code="invalid_command")
        executable = Path(argv[0]).name
        if executable not in ALLOWED_EXECUTABLES or any(arg in {"-c", "--command"} for arg in argv[1:]):
            return self._receipt(command_id, "failed", error_code="command_not_allowed")
        try:
            proc = subprocess.run(argv, cwd=self.root, capture_output=True, timeout=max(0.1, min(float(timeout_s), 300.0)), check=False, env={"PATH": os.environ.get("PATH", "")})
            raw = proc.stdout + proc.stderr
            truncated = len(raw) > MAX_OUTPUT_BYTES
            output = raw[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
            return self._receipt(command_id, "succeeded" if proc.returncode == 0 else "failed", {"returncode": proc.returncode, "output": output}, error_code=None if proc.returncode == 0 else "process_failed", truncated=truncated)
        except subprocess.TimeoutExpired:
            return self._receipt(command_id, "failed", error_code="timeout")
        except OSError:
            return self._receipt(command_id, "failed", error_code="exec_failed")


__all__ = ["ALLOWED_EXECUTABLES", "DirectoryBackend", "MAX_DIGEST_BYTES", "MAX_OUTPUT_BYTES", "MAX_READ_BYTES"]
