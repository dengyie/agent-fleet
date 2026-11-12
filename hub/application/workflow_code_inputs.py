"""Verified, temporary read-only views of Native Workflow source packages."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path, PurePosixPath
import tempfile
from typing import Any, BinaryIO, Callable, Iterator, Protocol

from fleet_core.workflow.code_inputs import (
    MAX_CODE_FILES, MAX_CODE_LIST_PAGE, MAX_CODE_READ_BYTES, CodeRevisionInput,
    code_revision_inputs,
)
from fleet_core.workflow.source import SourceExportError, extract_source_stream
from hub.domain.workflow_contract_types import ResolvedContract
from hub.domain.workflow_errors import WorkflowValidationError


class WorkflowCodeArtifacts(Protocol):
    def get(self, owner_id: str, workspace_id: str, artifact_id: str) -> dict[str, Any] | None: ...

    def open_verified(self, owner_id: str, workspace_id: str, artifact_id: str) -> BinaryIO: ...


def _relative_path(value: object, *, allow_root: bool = False) -> tuple[str, ...]:
    if type(value) is not str or (not value and not allow_root) or len(value.encode("utf-8")) > 512:
        raise WorkflowValidationError("invalid_code_input_path")
    if not value:
        return ()
    if "\\" in value or ":" in value or "\x00" in value:
        raise WorkflowValidationError("invalid_code_input_path")
    path = PurePosixPath(value)
    parts = tuple(value.split("/"))
    if path.is_absolute() or any(part in ("", ".", "..") for part in parts):
        raise WorkflowValidationError("invalid_code_input_path")
    return parts


class WorkflowCodeView:
    def __init__(self, roots: dict[str, Path]) -> None:
        self._roots = {key: Path(root).resolve(strict=True) for key, root in roots.items()}

    def _file(self, ref_key: object, path: object) -> Path:
        if type(ref_key) is not str or ref_key not in self._roots:
            raise WorkflowValidationError("code_input_not_found")
        parts = _relative_path(path)
        target = self._roots[ref_key].joinpath(*parts)
        try:
            resolved = target.resolve(strict=True)
            resolved.relative_to(self._roots[ref_key])
        except (OSError, ValueError) as exc:
            raise WorkflowValidationError("code_input_not_found") from exc
        if resolved.is_symlink() or not resolved.is_file():
            raise WorkflowValidationError("code_input_not_found")
        return resolved

    def list(self, ref_key: str, path: str = "", *, offset: int = 0,
             limit: int = MAX_CODE_LIST_PAGE) -> dict[str, Any]:
        if type(offset) is not int or not 0 <= offset <= MAX_CODE_FILES:
            raise WorkflowValidationError("invalid_code_input_offset")
        if type(limit) is not int or not 1 <= limit <= MAX_CODE_LIST_PAGE:
            raise WorkflowValidationError("invalid_code_input_limit")
        if type(ref_key) is not str or ref_key not in self._roots:
            raise WorkflowValidationError("code_input_not_found")
        parts = _relative_path(path, allow_root=True)
        directory = self._roots[ref_key].joinpath(*parts)
        try:
            resolved = directory.resolve(strict=True)
            resolved.relative_to(self._roots[ref_key])
        except (OSError, ValueError) as exc:
            raise WorkflowValidationError("code_input_not_found") from exc
        if resolved.is_symlink() or not resolved.is_dir():
            raise WorkflowValidationError("code_input_not_found")
        entries: list[dict[str, str]] = []
        for item in resolved.iterdir():
            if item.is_symlink():
                raise WorkflowValidationError("code_input_unavailable")
            relative = item.relative_to(self._roots[ref_key]).as_posix()
            entries.append({"path": relative, "kind": "directory" if item.is_dir() else "file"})
        entries.sort(key=lambda item: item["path"])
        page = entries[offset:offset + limit]
        next_offset = offset + len(page)
        return {"entries": page, "next_offset": next_offset if next_offset < len(entries) else None}

    def read(self, ref_key: str, path: str, *, offset: int = 0,
             max_bytes: int = MAX_CODE_READ_BYTES) -> dict[str, Any]:
        if type(offset) is not int or offset < 0:
            raise WorkflowValidationError("invalid_code_input_offset")
        if type(max_bytes) is not int or not 4 <= max_bytes <= MAX_CODE_READ_BYTES:
            raise WorkflowValidationError("invalid_code_input_limit")
        target = self._file(ref_key, path)
        try:
            size = target.stat().st_size
            if offset > size:
                raise WorkflowValidationError("invalid_code_input_offset")
            with target.open("rb") as handle:
                handle.seek(offset)
                raw = handle.read(max_bytes + 4)
            if offset and raw and raw[0] & 0xC0 == 0x80:
                raise WorkflowValidationError("invalid_code_input_offset")
            if len(raw) > max_bytes:
                raw = raw[:max_bytes]
                while raw:
                    try:
                        text = raw.decode("utf-8")
                        break
                    except UnicodeDecodeError as exc:
                        if exc.reason != "unexpected end of data":
                            raise WorkflowValidationError("code_input_invalid_utf8") from exc
                        raw = raw[:-1]
                else:
                    text = ""
            else:
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise WorkflowValidationError("code_input_invalid_utf8") from exc
            next_offset = offset + len(raw)
            return {"text": text, "offset": offset, "next_offset": next_offset if next_offset < size else None,
                    "size": size}
        except WorkflowValidationError:
            raise
        except OSError as exc:
            raise WorkflowValidationError("code_input_unavailable") from exc


class WorkflowCodeInputs:
    def __init__(self, artifacts: WorkflowCodeArtifacts, *, root_factory: Callable[[], Any] | None = None) -> None:
        self.artifacts = artifacts
        self.root_factory = root_factory or (lambda: tempfile.TemporaryDirectory(prefix="workflow-code-input-"))

    def _metadata(self, resolved: ResolvedContract, item: CodeRevisionInput) -> dict[str, Any]:
        bundle = item.reference.source_bundle
        metadata = self.artifacts.get(resolved.owner_id, resolved.workspace_id, bundle.artifact_id)
        if (type(metadata) is not dict or
                (metadata.get("owner_id"), metadata.get("workspace_id"), metadata.get("artifact_id"),
                 metadata.get("sha256"), metadata.get("size"), metadata.get("content_type")) !=
                (resolved.owner_id, resolved.workspace_id, bundle.artifact_id, bundle.sha256, bundle.size, "application/x-tar")):
            raise WorkflowValidationError("code_input_forbidden")
        return metadata

    def authorize(self, resolved: ResolvedContract, references: tuple[CodeRevisionInput, ...]) -> None:
        if type(resolved) is not ResolvedContract or references != code_revision_inputs(resolved):
            raise WorkflowValidationError("code_input_mismatch")
        bundles: dict[str, tuple[str, int]] = {}
        total = 0
        for item in references:
            bundle = item.reference.source_bundle
            identity = (bundle.sha256, bundle.size)
            previous = bundles.setdefault(bundle.artifact_id, identity)
            if previous != identity:
                raise WorkflowValidationError("code_input_mismatch")
            self._metadata(resolved, item)
        total = sum(size for _sha, size in bundles.values())
        if total > 10 * 1024 * 1024:
            raise WorkflowValidationError("code_inputs_too_large")

    @contextmanager
    def open_view(self, resolved: ResolvedContract, references: tuple[CodeRevisionInput, ...],
                  should_cancel: Callable[[], bool]) -> Iterator[WorkflowCodeView]:
        if type(resolved) is not ResolvedContract or not callable(should_cancel):
            raise WorkflowValidationError("invalid_code_input")
        expected = code_revision_inputs(resolved)
        if type(references) is not tuple or references != expected or not references:
            raise WorkflowValidationError("code_input_mismatch")
        with self.open_view_for_scope(resolved.owner_id, resolved.workspace_id, references, should_cancel) as view:
            yield view

    @contextmanager
    def open_view_for_scope(self, owner_id: str, workspace_id: str, references: tuple[CodeRevisionInput, ...],
                            should_cancel: Callable[[], bool]) -> Iterator[WorkflowCodeView]:
        if type(references) is not tuple or not references or not callable(should_cancel):
            raise WorkflowValidationError("invalid_code_input")
        metadata: dict[str, dict[str, Any]] = {}
        for item in references:
            metadata_item = self.artifacts.get(owner_id, workspace_id, item.reference.source_bundle.artifact_id)
            bundle = item.reference.source_bundle
            if (type(metadata_item) is not dict or
                    (metadata_item.get("owner_id"), metadata_item.get("workspace_id"), metadata_item.get("artifact_id"),
                     metadata_item.get("sha256"), metadata_item.get("size"), metadata_item.get("content_type")) !=
                    (owner_id, workspace_id, bundle.artifact_id, bundle.sha256, bundle.size, "application/x-tar")):
                raise WorkflowValidationError("code_input_forbidden")
            current = metadata_item
            prior = metadata.setdefault(item.reference.source_bundle.artifact_id, current)
            if prior != current:
                raise WorkflowValidationError("code_input_mismatch")
        total_bytes = sum(item["size"] for item in metadata.values())
        if total_bytes > 10 * 1024 * 1024:
            raise WorkflowValidationError("code_inputs_too_large")

        temporary = self.root_factory()
        root = Path(temporary.name if hasattr(temporary, "name") else temporary)
        roots: dict[str, Path] = {}
        extracted: dict[object, tuple[Path, tuple[str, ...]]] = {}
        try:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            file_count = 0
            for item in references:
                revision_key = item.reference
                prior = extracted.get(revision_key)
                if prior is None:
                    bundle = item.reference.source_bundle
                    destination = root / hashlib.sha256(repr(revision_key).encode("utf-8")).hexdigest()
                    try:
                        with self.artifacts.open_verified(owner_id, workspace_id, bundle.artifact_id) as handle:
                            paths = extract_source_stream(handle, destination, expected=item.reference,
                                                          should_cancel=should_cancel)
                    except SourceExportError as exc:
                        raise WorkflowValidationError(exc.code) from exc
                    file_count += len(paths)
                    if file_count > MAX_CODE_FILES:
                        raise WorkflowValidationError("code_inputs_too_large")
                    prior = (destination, paths)
                    extracted[revision_key] = prior
                roots[item.ref_key] = prior[0]
            if should_cancel():
                raise WorkflowValidationError("source_cancelled")
            yield WorkflowCodeView(roots)
        finally:
            cleanup = getattr(temporary, "cleanup", None)
            if callable(cleanup):
                cleanup()
            elif isinstance(temporary, (str, os.PathLike)):
                import shutil
                shutil.rmtree(temporary, ignore_errors=True)


__all__ = ["WorkflowCodeInputs", "WorkflowCodeView"]
