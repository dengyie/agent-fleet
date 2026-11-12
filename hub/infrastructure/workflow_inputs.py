"""Authorize revised input references using the existing scoped artifact store."""
from __future__ import annotations

import asyncio
from typing import Any, Protocol

from hub.domain.workflow_control import AuthorizedInputRevision, WorkflowControlRequest
from hub.domain.workflow_contract_types import ArtifactReference, CodeRevisionReference, NamedReference, PriorOutputReference
from hub.domain.workflow_errors import WorkflowPersistenceError
from .workflow_repository import WorkflowRepository, WorkflowRepositoryError


class ArtifactMetadataStore(Protocol):
    def get(self, owner_id: str, workspace_id: str, artifact_id: str) -> dict[str, Any] | None: ...


class WorkflowArtifactInputAuthorizer:
    def __init__(self, repository: WorkflowRepository, artifacts: ArtifactMetadataStore) -> None:
        self.repository = repository
        self.artifacts = artifacts

    async def authorize(self, request: WorkflowControlRequest) -> AuthorizedInputRevision:
        try:
            return await asyncio.to_thread(self._authorize, request)
        except WorkflowPersistenceError:
            raise
        except Exception as exc:
            raise WorkflowRepositoryError("input_authorization_unavailable") from exc

    def _authorize(self, request: WorkflowControlRequest) -> AuthorizedInputRevision:
        workflow = self.repository.get(request.owner_id, request.workflow_id)
        if workflow is None:
            raise WorkflowRepositoryError("workflow_not_found")
        if request.action != "invalidate" or request.input_refs is None:
            raise WorkflowRepositoryError("invalid_input_change")
        self.authorize_references(workflow.owner_id, workflow.workspace_id, request.input_refs)
        return AuthorizedInputRevision(workflow.owner_id, workflow.workspace_id, request.input_refs)

    def authorize_references(self, owner: str, workspace: str, references: tuple[NamedReference, ...]) -> None:
        seen: dict[str, tuple[str, int]] = {}
        for named in references:
            reference = named.reference
            if type(reference) is PriorOutputReference:
                continue
            artifact = reference.source_bundle if type(reference) is CodeRevisionReference else reference
            if type(artifact) is not ArtifactReference:
                raise WorkflowRepositoryError("reference_forbidden")
            expected = (artifact.sha256, artifact.size)
            if artifact.artifact_id in seen:
                if seen[artifact.artifact_id] != expected:
                    raise WorkflowRepositoryError("reference_forbidden")
                continue
            manifest = self.artifacts.get(owner, workspace, artifact.artifact_id)
            if manifest is None or (manifest.get("artifact_id"), manifest.get("owner_id"), manifest.get("workspace_id"),
                                    manifest.get("sha256"), manifest.get("size")) != (
                artifact.artifact_id, owner, workspace, *expected,
            ):
                raise WorkflowRepositoryError("reference_forbidden")
            seen[artifact.artifact_id] = expected
