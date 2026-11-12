"""Coordinate registered input grants and stream only the original scoped bytes."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
import time
from typing import Any, BinaryIO, ContextManager, Iterator, Protocol

from hub.domain.artifact_receive import ArtifactReceiveError, ReceiveRequest, RegisteredNodeInputs, artifact_inputs
from hub.domain.workflow_contract_types import ResolvedContract
from hub.domain.workflow_records import CoordinatorClaim
from hub.domain.workflow_errors import WorkflowPersistenceError
from hub.platform.artifacts import ArtifactError


class InputGrantStore(Protocol):
    def download_slot(self, owner: str, node: str) -> ContextManager[None]: ...
    def prepare(self, claim: CoordinatorClaim, *, expected_revision: int, resolved: ResolvedContract,
                generation: int, target: RegisteredNodeInputs, now: float) -> bool: ...
    def authorize(self, grant_id: str, *, command_id: str, owner: str, node: str, worker: str,
                  credential_fingerprint: str, now: float) -> tuple[ReceiveRequest, str]: ...


class InputArtifactStore(Protocol):
    def get(self, owner_id: str, workspace_id: str, artifact_id: str) -> dict[str, Any] | None: ...
    def open_verified(self, owner_id: str, workspace_id: str, artifact_id: str) -> BinaryIO: ...


class ArtifactInputService:
    def __init__(self, repository: InputGrantStore, artifacts: InputArtifactStore, *,
                 targets: tuple[RegisteredNodeInputs, ...] = (), clock: Callable[[], float] = time.time) -> None:
        self.repository, self.artifacts, self.clock = repository, artifacts, clock
        self.targets = {(target.owner_id, target.workspace_id, target.node_id): target for target in targets}
        if len(self.targets) != len(targets):
            raise ArtifactReceiveError("duplicate_input_target")

    def prepare(self, claim: CoordinatorClaim, *, expected_revision: int, resolved: ResolvedContract,
                generation: int, target_key: str) -> bool | None:
        if not artifact_inputs(resolved):
            return None
        target = self.targets.get((claim.owner_id, claim.workspace_id, target_key))
        if target is None:
            raise WorkflowPersistenceError("input_alias_unregistered")
        self.validate_inputs(resolved)
        return self.repository.prepare(claim, expected_revision=expected_revision, resolved=resolved,
            generation=generation, target=target, now=self.clock())

    def validate_inputs(self, resolved: ResolvedContract) -> None:
        try:
            for _key, artifact in artifact_inputs(resolved):
                metadata = self.artifacts.get(resolved.owner_id, resolved.workspace_id, artifact.artifact_id)
                if metadata is None or tuple(metadata[key] for key in ("sha256", "size")) != (artifact.sha256, artifact.size):
                    raise ArtifactReceiveError("receive_not_found")
        except ArtifactError as exc:
            raise WorkflowPersistenceError("input_receipt_required") from exc

    @contextmanager
    def content(self, grant_id: str, *, command_id: str, owner: str, node: str, worker: str,
                credential_fingerprint: str) -> Iterator[tuple[ReceiveRequest, Iterator[bytes]]]:
        def authorize() -> tuple[ReceiveRequest, str]:
            return self.repository.authorize(grant_id, command_id=command_id, owner=owner, node=node,
                worker=worker, credential_fingerprint=credential_fingerprint, now=self.clock())
        request, workspace = authorize()
        try:
            metadata = self.artifacts.get(owner, workspace, request.artifact_id)
            if metadata is None or (metadata["sha256"], metadata["size"]) != (request.sha256, request.size):
                raise ArtifactReceiveError("receive_not_found")
            with self.repository.download_slot(owner, node), self.artifacts.open_verified(owner, workspace, request.artifact_id) as source:
                authorize()
                def chunks() -> Iterator[bytes]:
                    remaining = request.size
                    while remaining:
                        authorize()
                        chunk = source.read(min(65536, remaining))
                        if not chunk:
                            raise ArtifactReceiveError("artifact_hash_mismatch")
                        authorize()
                        remaining -= len(chunk)
                        yield chunk
                    authorize()
                yield request, chunks()
        except ArtifactError as exc:
            raise ArtifactReceiveError("receive_not_found") from exc
