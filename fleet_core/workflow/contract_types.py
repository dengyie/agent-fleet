"""Immutable values for registered workflow snapshots and scoped resolution."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class CatalogRef:
    id: str
    revision: int


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    artifact_id: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class CodeRevisionReference:
    repository_id: str
    base_commit: str
    head_commit: str
    tree_id: str
    source_bundle: ArtifactReference


@dataclass(frozen=True, slots=True)
class PriorOutputReference:
    step_key: str
    output_key: str


@dataclass(frozen=True, slots=True)
class ContextReference:
    kind: Literal["memory", "summary"]
    ref_id: str
    revision: int
    sha256: str | None = None


ConcreteReference = ArtifactReference | CodeRevisionReference | ContextReference


@dataclass(frozen=True, slots=True)
class NamedReference:
    ref_key: str
    reference: ConcreteReference | PriorOutputReference


@dataclass(frozen=True, slots=True)
class OutputSpec:
    output_key: str
    kind: str
    required: bool
    max_bytes: int | None
    result_schema: bytes | None


@dataclass(frozen=True, slots=True)
class CapabilityRequirement:
    id: str
    revisions: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class WorkflowBudget:
    max_tokens: int
    max_model_requests: int
    max_active_seconds: int
    max_wait_seconds: int
    max_generations: int


@dataclass(frozen=True, slots=True)
class WorkContract:
    objective: str
    input_refs: tuple[NamedReference, ...]
    output_specs: tuple[OutputSpec, ...]
    capability_requirements: tuple[CapabilityRequirement, ...]
    policy_ref: CatalogRef
    validator_refs: tuple[CatalogRef, ...]
    budget: WorkflowBudget
    retry_class: str
    context_refs: tuple[NamedReference, ...]
    snapshot: bytes
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class WorkflowStepDefinition:
    step_key: str
    kind: str
    requires: tuple[str, ...]
    contract: WorkContract


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    template_id: str
    revision: int
    steps: tuple[WorkflowStepDefinition, ...]
    acceptance_required: bool
    snapshot: bytes


@dataclass(frozen=True, slots=True)
class InputBinding:
    ref_key: str
    owner_id: str
    workspace_id: str
    reference: ConcreteReference
    source: PriorOutputReference | None = None


@dataclass(frozen=True, slots=True)
class AuthorizedInputs:
    owner_id: str
    workspace_id: str
    step_key: str
    adapter_ref: CatalogRef
    bindings: tuple[InputBinding, ...]


@dataclass(frozen=True, slots=True)
class ResolvedContract:
    owner_id: str
    workspace_id: str
    step_key: str
    contract: WorkContract
    snapshot: bytes
    digest: str
