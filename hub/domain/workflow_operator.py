"""Validated operator requests; identity is supplied by the transport boundary."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .workflow_control import exact_counter
from .workflow_contract import reference_values
from .workflow_contract_types import NamedReference
from .workflow_errors import WorkflowValidationError
from .workflow_identity import validate_reference


@dataclass(frozen=True, slots=True)
class WorkflowCreateRequest:
    owner_id: str
    workspace_id: str
    template_id: str
    template_revision: int
    input_refs: tuple[NamedReference, ...]
    client_token: str
    expected_policy_revision: int

    def __post_init__(self) -> None:
        for value in (self.owner_id, self.workspace_id, self.template_id, self.client_token):
            validate_reference(value)
        exact_counter(self.template_revision)
        exact_counter(self.expected_policy_revision)
        reference_values(self.input_refs)


@dataclass(frozen=True, slots=True)
class WorkflowAcceptanceRequest:
    owner_id: str
    workflow_id: str
    decision: str
    delivery_digest: str
    expected_revision: int
    client_token: str

    def __post_init__(self) -> None:
        for value in (self.owner_id, self.workflow_id, self.client_token):
            validate_reference(value)
        exact_counter(self.expected_revision)
        if type(self.decision) is not str or self.decision not in ("accepted", "rejected"):
            raise WorkflowValidationError("invalid_acceptance_decision")
        if type(self.delivery_digest) is not str or re.fullmatch(r"[a-f0-9]{64}", self.delivery_digest) is None:
            raise WorkflowValidationError("invalid_delivery_digest")
