"""Shared artifact failure, legacy size bound and source-store I/O port."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

ARTIFACT_MAX_BYTES = 10 * 1024 * 1024


class ArtifactError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

    def __str__(self):
        return self.code


class SourceArtifactStore(Protocol):
    def put_file(self, owner_id: str, workspace_id: str, source: Path, *,
                 name: str, content_type: str) -> dict[str, Any]: ...

    def read(self, owner_id: str, workspace_id: str, artifact_id: str) -> bytes: ...
