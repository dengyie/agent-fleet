"""Trusted route constraints; concrete work identities come from the contract."""
from __future__ import annotations

from dataclasses import dataclass, replace

from .capability import CapabilityError, SelectionRequirements, decode, digest, identifier, integer
from .workflow_contract import parse_resolved
from .workflow_contract_types import CatalogRef, ResolvedContract, WorkflowDefinition


@dataclass(frozen=True, slots=True)
class RegisteredCapabilityRoute:
    definition_digest: str
    step_key: str
    adapter_ref: CatalogRef
    policy_ref: CatalogRef
    selection: SelectionRequirements
    reserve_tokens: int
    reserve_requests: int

    def __post_init__(self) -> None:
        digest(self.definition_digest)
        identifier(self.step_key)
        if type(self.selection) is not SelectionRequirements:
            raise CapabilityError("invalid_route_registration")
        for reference in (self.adapter_ref, self.policy_ref):
            if type(reference) is not CatalogRef:
                raise CapabilityError("invalid_route_registration")
            identifier(reference.id)
            integer(reference.revision, minimum=1)
        if self.adapter_ref.id != self.selection.adapter_id:
            raise CapabilityError("invalid_route_registration")
        integer(self.reserve_tokens)
        integer(self.reserve_requests)

    @property
    def owner_id(self) -> str:
        return self.selection.owner_id

    @property
    def workspace_id(self) -> str:
        return self.selection.workspace_id

    def requirements(self, definition: WorkflowDefinition, resolved: ResolvedContract) -> SelectionRequirements:
        if type(definition) is not WorkflowDefinition or type(resolved) is not ResolvedContract:
            raise CapabilityError("registration_conflict")
        if parse_resolved(resolved.snapshot) != resolved:
            raise CapabilityError("registration_conflict")
        adapter = decode(resolved.snapshot)["adapter_ref"]
        if (self.selection.template != CatalogRef(definition.template_id, definition.revision)
                or (resolved.owner_id, resolved.workspace_id, resolved.step_key, resolved.contract.policy_ref) !=
                    (self.owner_id, self.workspace_id, self.step_key, self.policy_ref)
                or adapter != {"id": self.adapter_ref.id, "revision": self.adapter_ref.revision}
                or resolved.contract.capability_requirements != self.selection.capabilities):
            raise CapabilityError("registration_conflict")
        return replace(self.selection, input_digest=resolved.digest,
            max_active_seconds=resolved.contract.budget.max_active_seconds)
