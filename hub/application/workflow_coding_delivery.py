"""Source export and validator publication through scoped I/O ports."""
from __future__ import annotations

import hashlib
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, ParamSpec, Protocol, TypeVar

from hub.domain.task import DomainError
from hub.domain.workflow_contract import parse_input_references, reference_values
from hub.domain.workflow_contract_types import ArtifactReference, CodeRevisionReference, NamedReference
from hub.domain.workflow_delivery import (
    DeliveryContext, DeliveryEvidence, DeliveryOutput, DeliveryReceipt, DeliveryRecord, DeliveryTarget, DeliveryTaskLease, source_subject,
)
from hub.domain.workflow_errors import WorkflowPersistenceError, WorkflowValidationError
from hub.domain.workflow_identity import canonical_digest, canonical_json, validate_reference
from hub.domain.workflow_json import decode_snapshot
from hub.platform.async_io import run_owned_io
from hub.platform.artifacts import ArtifactError
from hub.platform.workflow_source import (SourceExportError, SourceExportResult, SourceExporter,
                                            extract_source_bundle, verify_source_archive)
from hub.platform.workflow_validators import (
    ReviewIdentity, TestValidation, WorkflowValidatorError, test_report_bytes, validate_review_report,
)

P = ParamSpec("P")
T = TypeVar("T")


class DeliveryStore(Protocol):
    def context(self, target: DeliveryTarget, *, now: float | None = None) -> DeliveryContext: ...
    def receipt(self, target: DeliveryTarget, operation: str, request_digest: str) -> DeliveryReceipt | None: ...
    def commit(self, target: DeliveryTarget, operation: str, request_digest: str, record: DeliveryRecord, *, now: float) -> DeliveryReceipt: ...
    def refresh_source(self, target: DeliveryTarget, *, now: float) -> DeliveryTarget: ...
    def target_for_task(self, origin: dict[str, Any], lease: DeliveryTaskLease, *, now: float, current: bool = True) -> tuple[DeliveryTarget, str]: ...
    def reserve_source(self, target: DeliveryTarget, artifact_id: str, sha256: str, size: int, *, now: float) -> tuple[DeliveryTarget, str]: ...
    def recover_source(self, target: DeliveryTarget, request_digest: str, *, now: float) -> DeliveryReceipt | None: ...


class DeliveryArtifacts(Protocol):
    def get(self, owner_id: str, workspace_id: str, artifact_id: str) -> dict[str, Any] | None: ...
    def read(self, owner_id: str, workspace_id: str, artifact_id: str) -> bytes: ...
    def put_file(self, owner_id: str, workspace_id: str, source: Path, *, name: str, content_type: str,
                 artifact_id: str | None = None) -> dict[str, Any]: ...


class WorkflowCodingDeliveryError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


async def _io(operation: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    try:
        return await run_owned_io(operation, *args, **kwargs)
    except (WorkflowPersistenceError, WorkflowValidationError, DomainError, ArtifactError, SourceExportError, WorkflowValidatorError, OSError) as exc:
        raise WorkflowCodingDeliveryError(getattr(exc, "code", "delivery_io_failed")) from exc


def _request(target: DeliveryTarget, payload: object, *, include_revision: bool = True) -> str:
    binding = target.binding
    return canonical_digest({
        "workflow_id": target.workflow_id, "step_key": target.step_key, "generation": target.generation,
        **({"revision": target.expected_revision} if include_revision else {}),
        "binding": {"owner_id": binding.owner_id, "workspace_id": binding.workspace_id, "target_key": binding.target_key,
                    "token": binding.token, "payload_hash": binding.payload_hash, "kind": binding.kind,
                    "execution_id": binding.execution_id, "run_id": binding.run_id},
        "payload": payload,
        "task_attempt": None if target.task_lease is None else {
            "task_id": target.task_lease.task_id, "attempt_id": target.task_lease.attempt_id, "machine": target.task_lease.machine},
    })


def _source_payload(revision: CodeRevisionReference, executor_id: str) -> dict[str, Any]:
    return {"schema_version": 1, "source_revision": reference_values((NamedReference("source", revision),))[0],
            "executor_id": executor_id}


class WorkflowCodingDeliveryService:
    def __init__(self, repository: DeliveryStore, artifacts: DeliveryArtifacts, *, clock: Callable[[], float], task_repository=None) -> None:
        self.repository = repository
        self.artifacts = artifacts
        self.clock = clock
        self.task_repository = task_repository

    async def task_source_policy(self, machine: str, attempt_id: str, nonce: str) -> dict[str, Any]:
        if self.task_repository is None:
            raise WorkflowCodingDeliveryError("source_unavailable")
        origin = await _io(self.task_repository.source_origin, machine, attempt_id, nonce)
        if origin is None or not origin.get("repository_id"):
            raise WorkflowCodingDeliveryError("source_unavailable")
        lease = DeliveryTaskLease(origin["task_id"], attempt_id, nonce, machine, origin["owner_id"], origin["run_id"])
        target, repository_id = await _io(self.repository.target_for_task, origin, lease, now=self.clock())
        context = await _io(self.repository.context, target, now=self.clock())
        if context.active_deadline is None:
            raise WorkflowCodingDeliveryError("source_active_deadline_exceeded")
        return {"source_capture": True, "source_repository_id": repository_id,
                "source_deadline_utc": datetime.fromtimestamp(context.active_deadline, timezone.utc).isoformat()}

    async def record_task_source(self, machine: str, attempt_id: str, nonce: str, raw: bytes) -> DeliveryReceipt:
        if self.task_repository is None:
            raise WorkflowCodingDeliveryError("source_unavailable")
        origin = await _io(self.task_repository.source_origin, machine, attempt_id, nonce)
        if origin is None or not origin.get("repository_id"):
            raise WorkflowCodingDeliveryError("source_unavailable")
        lease = DeliveryTaskLease(origin["task_id"], attempt_id, nonce, machine, origin["owner_id"], origin["run_id"])
        try:
            target, repository_id = await _io(self.repository.target_for_task, origin, lease, now=self.clock(), current=False)
            verified = await _io(verify_source_archive, raw, repository_id=repository_id)
            sha256 = hashlib.sha256(raw).hexdigest()
            artifact_id = "source-attempt-" + attempt_id
            target, artifact_id = await _io(self.repository.reserve_source, target, artifact_id, sha256, len(raw), now=self.clock())
            artifact = ArtifactReference(artifact_id, sha256, len(raw))
            revision = CodeRevisionReference(repository_id, verified.manifest["base_commit"], verified.manifest["head_commit"],
                                             verified.manifest["tree_id"], artifact)
            prior = await _io(self.repository.recover_source, target,
                _request(target, _source_payload(revision, "cli-runner"), include_revision=False), now=self.clock())
            if prior is not None:
                return prior
            with tempfile.TemporaryDirectory(prefix="workflow-source-upload-") as temporary:
                source = Path(temporary) / "source.tar"
                source.write_bytes(raw)
                manifest = await _io(self.artifacts.put_file, target.binding.owner_id, target.binding.workspace_id,
                                      source, name=f"source-{attempt_id[:12]}.tar", content_type="application/x-tar",
                                      artifact_id=artifact_id)
            artifact = ArtifactReference(manifest["artifact_id"], manifest["sha256"], manifest["size"])
            if (artifact.artifact_id, artifact.sha256, artifact.size) != (artifact_id, sha256, len(raw)):
                raise WorkflowCodingDeliveryError("artifact_hash_mismatch")
            exported = SourceExportResult(revision, manifest, verified.included_paths, verified.omitted_paths)
            receipt = await self.record_source(target, exported, executor_id="cli-runner")
            return receipt
        except WorkflowCodingDeliveryError:
            raise
        except (WorkflowPersistenceError, WorkflowValidationError, ArtifactError, SourceExportError, OSError) as exc:
            raise WorkflowCodingDeliveryError(getattr(exc, "code", "source_upload_failed")) from exc

    async def export_source(self, repository_root: Path, owner: str, workspace: str, *, repository_id: str,
                            base_commit: str, head_commit: str, tree_id: str) -> SourceExportResult:
        try:
            return await SourceExporter(repository_root, self.artifacts).export(owner, workspace, repository_id=repository_id,
                base_commit=base_commit, head_commit=head_commit, tree_id=tree_id)
        except SourceExportError as exc:
            raise WorkflowCodingDeliveryError(exc.code) from exc

    def _read(self, target: DeliveryTarget, reference: ArtifactReference) -> bytes:
        manifest = self.artifacts.get(target.binding.owner_id, target.binding.workspace_id, reference.artifact_id)
        expected = (reference.artifact_id, target.binding.owner_id, target.binding.workspace_id, reference.sha256, reference.size)
        if manifest is None or tuple(manifest.get(key) for key in ("artifact_id", "owner_id", "workspace_id", "sha256", "size")) != expected:
            raise WorkflowCodingDeliveryError("reference_forbidden")
        raw = self.artifacts.read(target.binding.owner_id, target.binding.workspace_id, reference.artifact_id)
        if len(raw) != reference.size or hashlib.sha256(raw).hexdigest() != reference.sha256:
            raise WorkflowCodingDeliveryError("artifact_hash_mismatch")
        return raw

    def _publish(self, target: DeliveryTarget, raw: bytes, name: str, content_type: str = "application/json") -> ArtifactReference:
        if type(raw) is not bytes or len(raw) > 65536:
            raise WorkflowCodingDeliveryError("report_too_large")
        with tempfile.TemporaryDirectory(prefix="workflow-report-") as temporary:
            source = Path(temporary) / "report"
            source.write_bytes(raw)
            manifest = self.artifacts.put_file(target.binding.owner_id, target.binding.workspace_id, source, name=name, content_type=content_type)
        reference = ArtifactReference(manifest["artifact_id"], manifest["sha256"], manifest["size"])
        self._read(target, reference)
        return reference

    def _verify_source(self, target: DeliveryTarget, reference: CodeRevisionReference) -> None:
        self._read(target, reference.source_bundle)
        with tempfile.TemporaryDirectory(prefix="workflow-verify-") as temporary:
            extract_source_bundle(self.artifacts, target.binding.owner_id, target.binding.workspace_id,
                                  reference.source_bundle.artifact_id, Path(temporary) / "source", expected_revision=reference)

    async def record_source(self, target: DeliveryTarget, exported: SourceExportResult, *, executor_id: str) -> DeliveryReceipt:
        try:
            validate_reference(executor_id)
            if target.step_key != "develop" or type(exported) is not SourceExportResult:
                raise WorkflowValidationError("invalid_source_delivery")
            payload = _source_payload(exported.revision, executor_id)
            request_digest = _request(target, payload, include_revision=target.task_lease is None)
            for attempt in range(3):
                prior = (await _io(self.repository.recover_source, target, request_digest, now=self.clock())
                         if target.task_lease is not None else await _io(self.repository.receipt, target, "source", request_digest))
                if prior is not None:
                    return prior
                try:
                    context = await _io(self.repository.context, target, now=self.clock())
                    await _io(self._verify_source, target, exported.revision)
                    metadata = await _io(self._publish, target, canonical_json(payload), "source-revision.json")
                    outputs = (DeliveryOutput("source", "code_revision", metadata), DeliveryOutput("source_bundle", "artifact", exported.revision.source_bundle))
                    subject = source_subject(exported.revision, outputs, decode_snapshot(context.semantic_inputs))
                    return await _io(self.repository.commit, target, "source", request_digest, DeliveryRecord(subject, outputs), now=self.clock())
                except WorkflowCodingDeliveryError as exc:
                    if target.task_lease is None or exc.code != "revision_conflict" or attempt == 2:
                        raise
                    target = await _io(self.repository.refresh_source, target, now=self.clock())
            raise WorkflowCodingDeliveryError("revision_conflict")
        except WorkflowValidationError as exc:
            raise WorkflowCodingDeliveryError(exc.code) from exc

    def _source(self, target: DeliveryTarget, context: DeliveryContext) -> tuple[CodeRevisionReference, str]:
        outputs = {item.output_key: item for item in context.source_outputs}
        if set(outputs) != {"source", "source_bundle"} or context.source_subject is None:
            raise WorkflowCodingDeliveryError("source_unavailable")
        payload = decode_snapshot(self._read(target, outputs["source"].artifact))
        if set(payload) != {"schema_version", "source_revision", "executor_id"} or type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
            raise WorkflowCodingDeliveryError("invalid_source_delivery")
        references = parse_input_references([payload["source_revision"]])
        revision = references[0].reference
        if type(revision) is not CodeRevisionReference or revision.source_bundle != outputs["source_bundle"].artifact:
            raise WorkflowCodingDeliveryError("source_revision_mismatch")
        validate_reference(payload["executor_id"])
        if source_subject(revision, context.source_outputs, decode_snapshot(context.semantic_inputs)) != context.source_subject:
            raise WorkflowCodingDeliveryError("subject_conflict")
        resolved = {item.ref_key: item.reference for item in context.resolved.contract.input_refs}
        if resolved.get("source") != revision:
            raise WorkflowCodingDeliveryError("source_revision_mismatch")
        return revision, payload["executor_id"]

    async def source_context(self, target: DeliveryTarget) -> tuple[DeliveryContext, CodeRevisionReference, str]:
        context = await _io(self.repository.context, target, now=self.clock())
        revision, developer = await _io(self._source, target, context)
        for reference in context.prerequisite_reports:
            await _io(self._read, target, reference)
        return context, revision, developer

    async def record_review(self, target: DeliveryTarget, raw_report: bytes, *, executor: ReviewIdentity) -> DeliveryReceipt:
        try:
            if target.step_key != "review" or type(raw_report) is not bytes or len(raw_report) > 65536 or type(executor) is not ReviewIdentity:
                raise WorkflowValidationError("invalid_review_delivery")
            request_digest = _request(target, {"report_hash": hashlib.sha256(raw_report).hexdigest(), "executor": executor.payload()})
            prior = await _io(self.repository.receipt, target, "review", request_digest)
            if prior is not None:
                return prior
            context, revision, developer = await self.source_context(target)
            result = validate_review_report(raw_report, source_revision=revision, subject_digest=context.source_subject,
                                            executor=executor, developer_executor_id=developer)
            report = await _io(self._publish, target, result.raw_report, "review-report.json")
            evidence = DeliveryEvidence("review-report@1", result.verdict, "review:" + canonical_digest(executor.payload()), report, result.content_digest)
            record = DeliveryRecord(result.subject_digest, (DeliveryOutput("review_report", "artifact", report),), evidence, context.source_generation)
            return await _io(self.repository.commit, target, "review", request_digest, record, now=self.clock())
        except (WorkflowValidationError, WorkflowValidatorError) as exc:
            raise WorkflowCodingDeliveryError(exc.code) from exc

    async def record_test(self, target: DeliveryTarget, result: TestValidation) -> DeliveryReceipt:
        try:
            if target.step_key != "test" or type(result) is not TestValidation or (result.recipe_id, result.recipe_revision) != ("coding-delivery-tests", 1):
                raise WorkflowValidatorError("invalid_test_recipe")
            request_digest = _request(target, {"report_hash": result.report_hash, "executor_id": result.executor_id})
            prior = await _io(self.repository.receipt, target, "test", request_digest)
            if prior is not None:
                return prior
            context, revision, _ = await self.source_context(target)
            raw = test_report_bytes(result, revision)
            if result.subject_digest != context.source_subject or result.junit is None:
                raise WorkflowValidatorError("test_subject_mismatch")
            report = await _io(self._publish, target, raw, "test-report.json")
            junit = await _io(self._publish, target, result.junit, "junit.xml", "application/xml")
            evidence = DeliveryEvidence(f"test-report@{result.recipe_revision}", "passed" if result.passed else "failed",
                                        "test:" + result.executor_id, report, canonical_digest(decode_snapshot(raw)))
            outputs = (DeliveryOutput("test_report", "artifact", report), DeliveryOutput("junit", "artifact", junit))
            record = DeliveryRecord(result.subject_digest, outputs, evidence, context.source_generation)
            return await _io(self.repository.commit, target, "test", request_digest, record, now=self.clock())
        except (WorkflowValidationError, WorkflowValidatorError) as exc:
            raise WorkflowCodingDeliveryError(exc.code) from exc


__all__ = ["WorkflowCodingDeliveryError", "WorkflowCodingDeliveryService", "DeliveryArtifacts", "DeliveryStore"]
