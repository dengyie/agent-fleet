"""Immutable workflow persistence values, independent of SQLite and HTTP."""
from __future__ import annotations

from dataclasses import dataclass

from .workflow_evidence import ValidationEvidence
from fleet_core.workflow.dispatch import DispatchIntent as DispatchIntent  # Compatibility export for existing consumers.


@dataclass(frozen=True, slots=True)
class WorkflowRecord:
    workflow_id: str
    owner_id: str
    workspace_id: str
    definition_snapshot: bytes
    definition_digest: str
    execution_state: str
    validation_state: str
    acceptance_state: str
    revision: int
    cancel_requested: bool
    pause_requested: bool
    next_poll_at: float
    created_at: float
    updated_at: float
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowStepRecord:
    step_key: str
    generation: int
    kind: str
    state: str
    resolved_snapshot: bytes | None
    resolved_digest: str | None
    target_key: str | None
    validation_state: str = "not_requested"
    subject_digest: str | None = None
    reason_code: str | None = None
    dispatch_stage: str | None = None
    output_keys: tuple[str, ...] = ()
    evidence: tuple[ValidationEvidence, ...] = ()
    active_deadline: float | None = None
    wait_deadline: float | None = None
    contract_snapshot: bytes | None = None


@dataclass(frozen=True, slots=True)
class WorkflowSnapshot:
    workflow: WorkflowRecord
    steps: tuple[WorkflowStepRecord, ...]
    admission_mode: str


@dataclass(frozen=True, slots=True)
class CoordinatorClaim:
    owner_id: str
    workspace_id: str
    workflow_id: str
    worker_id: str
    epoch: int
    expires_at: float


@dataclass(frozen=True, slots=True)
class WorkflowEvent:
    sequence: int
    kind: str
    payload: bytes
    created_at: float


@dataclass(frozen=True, slots=True)
class WorkflowEventPage:
    events: tuple[WorkflowEvent, ...]
    next_cursor: int
