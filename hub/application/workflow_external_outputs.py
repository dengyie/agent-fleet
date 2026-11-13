"""Verify already-published external outputs through the existing ArtifactStore."""
from __future__ import annotations

import hashlib
import io
from typing import Any, BinaryIO, NoReturn, Protocol

from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.exceptions import NoSuchResource

from hub.domain.external_agent import ExternalTaskRef
from hub.domain.external_outputs import (
    MAX_RESULT_BYTES, ExternalOutputSet, VerifiedExternalOutputs, validate_external_outputs,
)
from hub.domain.workflow_contract import parse_input_references
from hub.domain.workflow_contract_types import ArtifactReference, CodeRevisionReference, NamedReference, OutputSpec, ResolvedContract
from hub.domain.workflow_errors import WorkflowValidationError
from hub.domain.workflow_identity import validate_reference
from hub.domain.workflow_json import decode_snapshot
from hub.platform.workflow_source import MAX_BYTES as MAX_SOURCE_BYTES, verify_source_stream


class ExternalOutputArtifacts(Protocol):
    def get(self, owner_id: str, workspace_id: str, artifact_id: str) -> dict[str, Any] | None: ...

    def open_verified(self, owner_id: str, workspace_id: str, artifact_id: str) -> BinaryIO: ...


class WorkflowExternalOutputsError(RuntimeError):
    def __init__(self, code: str, *, cause: BaseException | None = None) -> None:
        self.code = code
        super().__init__(code)
        if cause is not None:
            self.__cause__ = cause


def _reject_resource(uri: str) -> NoReturn:
    raise NoSuchResource(ref=uri)


def _validate_schema(spec: OutputSpec, value: dict[str, Any]) -> None:
    if spec.result_schema is None:
        raise WorkflowExternalOutputsError("invalid_output_schema")
    schema = decode_snapshot(spec.result_schema, max_bytes=MAX_RESULT_BYTES)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, registry=Registry(retrieve=_reject_resource))
    error = next(validator.iter_errors(value), None)
    if error is not None:
        raise WorkflowExternalOutputsError("external_output_schema_mismatch", cause=error) from error


class _VerifiedReader:
    """Hash each byte consumed by the archive parser and its remaining tail."""
    def __init__(self, handle: BinaryIO, reference: ArtifactReference) -> None:
        self.handle = handle
        self.reference = reference
        self.digest = hashlib.sha256()
        self.size = 0
        self.eof = False

    def seek(self, offset: int, whence: int = 0) -> int:
        if offset != 0 or whence != 0 or self.size != 0:
            raise WorkflowExternalOutputsError("artifact_corrupt")
        return self.handle.seek(0)

    def read(self, size: int = 65536) -> bytes:
        if type(size) is not int or size <= 0:
            raise WorkflowExternalOutputsError("artifact_corrupt")
        if self.eof:
            return b""
        limit = min(size, 65536, self.reference.size + 1 - self.size)
        chunk = self.handle.read(limit)
        if type(chunk) is not bytes or len(chunk) > limit:
            raise WorkflowExternalOutputsError("artifact_corrupt")
        self.size += len(chunk)
        if self.size > self.reference.size:
            raise WorkflowExternalOutputsError("artifact_hash_mismatch")
        self.digest.update(chunk)
        self.eof = not chunk
        return chunk

    def finish(self) -> None:
        while self.read():
            continue
        if self.size != self.reference.size or self.digest.hexdigest() != self.reference.sha256:
            raise WorkflowExternalOutputsError("artifact_hash_mismatch")


class WorkflowExternalOutputs:
    def __init__(self, artifacts: ExternalOutputArtifacts) -> None:
        self.artifacts = artifacts

    def _open(self, resolved: ResolvedContract, reference: ArtifactReference) -> BinaryIO:
        manifest = self.artifacts.get(resolved.owner_id, resolved.workspace_id, reference.artifact_id)
        expected = (reference.artifact_id, resolved.owner_id, resolved.workspace_id, reference.sha256, reference.size)
        if (type(manifest) is not dict or type(manifest.get("size")) is not int
                or manifest.get("ready", True) is not True
                or tuple(manifest.get(key) for key in ("artifact_id", "owner_id", "workspace_id", "sha256", "size")) != expected):
            raise WorkflowExternalOutputsError("reference_forbidden")
        return self.artifacts.open_verified(resolved.owner_id, resolved.workspace_id, reference.artifact_id)

    def _read(self, resolved: ResolvedContract, reference: ArtifactReference, *, capture: bool) -> bytes | None:
        with self._open(resolved, reference) as handle:
            reader = _VerifiedReader(handle, reference)
            raw = bytearray(reference.size) if capture else None
            offset = 0
            while chunk := reader.read():
                if raw is not None:
                    raw[offset:offset + len(chunk)] = chunk
                offset += len(chunk)
            reader.finish()
            return bytes(raw) if raw is not None else None

    @staticmethod
    def _source(raw: bytes, spec: OutputSpec) -> CodeRevisionReference:
        value = decode_snapshot(raw, max_bytes=MAX_RESULT_BYTES)
        if (set(value) != {"schema_version", "source_revision", "executor_id"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1):
            raise WorkflowExternalOutputsError("invalid_code_revision_output")
        validate_reference(value["executor_id"])
        parsed = parse_input_references([value["source_revision"]])[0].reference
        if type(parsed) is not CodeRevisionReference or not 1 <= parsed.source_bundle.size <= MAX_SOURCE_BYTES:
            raise WorkflowExternalOutputsError("invalid_code_revision_output")
        _validate_schema(spec, value["source_revision"])
        return parsed

    def _bundle(self, resolved: ResolvedContract, source: CodeRevisionReference) -> None:
        with self._open(resolved, source.source_bundle) as handle:
            reader = _VerifiedReader(handle, source.source_bundle)
            verify_source_stream(reader, expected=source)
            reader.finish()

    def verify(self, resolved: ResolvedContract, task: ExternalTaskRef, exported: ExternalOutputSet) -> VerifiedExternalOutputs:
        try:
            validate_external_outputs(exported, resolved, task)
            specs = {item.output_key: item for item in resolved.contract.output_specs}
            cache: dict[str, bytes | None] = {}
            sources: list[NamedReference] = []
            for output in exported.outputs:
                spec, reference = specs[output.output_key], output.artifact
                if spec.kind == "artifact":
                    continue
                if reference.artifact_id not in cache:
                    cache[reference.artifact_id] = self._read(resolved, reference, capture=True)
                raw = cache[reference.artifact_id]
                if raw is None:
                    raise WorkflowExternalOutputsError("invalid_external_output")
                if spec.kind == "structured":
                    _validate_schema(spec, decode_snapshot(raw, max_bytes=MAX_RESULT_BYTES))
                    continue
                sources.append(NamedReference(output.output_key, self._source(raw, spec)))
            proof = VerifiedExternalOutputs(exported, tuple(sources))
            proof.snapshot  # Bound the complete private proof before reading bundles.
            bundles: dict[str, CodeRevisionReference] = {}
            for named in proof.source_revisions:
                source = named.reference
                prior = bundles.setdefault(source.source_bundle.artifact_id, source)
                if prior != source:
                    raise WorkflowExternalOutputsError("external_output_conflict")
            for source in bundles.values():
                identity = source.source_bundle.artifact_id
                if identity in cache:
                    with io.BytesIO(cache[identity]) as handle:
                        verify_source_stream(handle, expected=source)
                    continue
                self._bundle(resolved, source)
                cache[identity] = None
            for output in exported.outputs:
                identity = output.artifact.artifact_id
                if identity not in cache:
                    cache[identity] = self._read(resolved, output.artifact, capture=False)
            return proof
        except WorkflowExternalOutputsError:
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, WorkflowValidationError) else "external_output_invalid"
            raise WorkflowExternalOutputsError(code, cause=exc) from exc
