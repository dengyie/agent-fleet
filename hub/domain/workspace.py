"""Workspace domain contracts and bounded path normalization."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath


class WorkspaceError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

    def __str__(self):
        return self.code


@dataclass(frozen=True)
class Workspace:
    workspace_id: str
    owner_id: str
    backend: str
    root_path: str
    quota_bytes: int = 1024 * 1024 * 1024
    enabled: bool = True


def normalize_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise WorkspaceError("invalid_path")
    path = value.replace("\\", "/")
    if path.startswith("/") or len(path) >= 2 and path[1] == ":":
        raise WorkspaceError("path_escape")
    parts = PurePosixPath(path).parts
    if any(part in ("", ".", "..") for part in parts):
        raise WorkspaceError("path_escape")
    normalized = "/".join(parts)
    if len(normalized.encode("utf-8")) > 4096:
        raise WorkspaceError("path_too_large")
    return normalized


__all__ = ["Workspace", "WorkspaceError", "normalize_relative_path"]
