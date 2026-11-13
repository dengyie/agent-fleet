"""Deterministic, bounded selection over authenticated observations and policy."""
from __future__ import annotations

import hashlib
import heapq

from hub.domain.capability import (
    MAX_CANDIDATES, MAX_DEFINITIONS, AuthorizedNode, BoundCapability, CandidateExclusion,
    CapabilityDefinition, CapabilityError, SelectionRequirements, SelectionResult, TargetBinding,
    identifier, timestamp,
)
from hub.domain.workflow_contract_types import CatalogRef


def _authority(requirements: SelectionRequirements, candidate: AuthorizedNode) -> str | None:
    policy = candidate.policy
    if policy.owner_id != requirements.owner_id:
        return "owner_forbidden"
    if not candidate.enabled or not candidate.credential_enabled:
        return "node_disabled"
    if not candidate.workspace_enabled or requirements.workspace_id not in policy.workspaces:
        return "workspace_forbidden"
    if requirements.template not in policy.templates:
        return "template_forbidden"
    return None


def _implementation(requirements: SelectionRequirements, candidate: AuthorizedNode,
                    definitions: dict[tuple[str, int], CapabilityDefinition]) -> tuple[str | None, tuple[tuple[CapabilityDefinition, ...], ...]]:
    offer = candidate.offer
    if offer is None:
        return "offer_missing", ()
    if (requirements.operating_systems and offer.os not in requirements.operating_systems) or (
            requirements.architectures and offer.architecture not in requirements.architectures):
        return "platform_mismatch", ()
    software = dict(offer.software)
    if any(software.get(name) != version for name, version in requirements.software):
        return "software_mismatch", ()
    offered = {(ref.id, ref.revision) for ref in offer.capabilities}
    offered_ids = {ref.id for ref in offer.capabilities}
    choices: list[tuple[CapabilityDefinition, ...]] = []
    for required in requirements.capabilities:
        if required.id not in offered_ids:
            return "capability_unavailable", ()
        compatible = tuple(definitions[(required.id, revision)] for revision in required.revisions
            if (required.id, revision) in offered and (required.id, revision) in definitions
            and definitions[(required.id, revision)].adapter_id == requirements.adapter_id)
        if not compatible:
            return "revision_mismatch", ()
        choices.append(compatible)
    if not set(requirements.input_aliases).issubset(candidate.policy.input_aliases):
        return "input_unreachable", ()
    if not set(requirements.residency_aliases).issubset(candidate.policy.residency_aliases):
        return "residency_forbidden", ()
    return None, tuple(choices)


def _policy(requirements: SelectionRequirements, candidate: AuthorizedNode,
            choices: tuple[tuple[CapabilityDefinition, ...], ...]) -> tuple[BoundCapability, ...] | None:
    permitted = set(candidate.policy.capabilities) & set(candidate.offer.local_allowed_capabilities)
    roles = requirements.owner_role
    risks = set(candidate.policy.allowed_risks)
    selected: list[BoundCapability] = []
    for versions in choices:
        allowed = (version for version in versions if CatalogRef(version.capability_id, version.revision) in permitted
            and roles in version.allowed_owner_roles and version.risk in risks
            and requirements.max_active_seconds <= version.max_active_seconds
            and requirements.result_limit_bytes <= version.result_limit_bytes)
        chosen = max(allowed, key=lambda definition: definition.revision, default=None)
        if chosen is None:
            return None
        selected.append(BoundCapability(chosen.capability_id, chosen.revision, chosen.digest))
    return tuple(sorted(selected, key=lambda ref: ref.id))


def _evaluate(requirements: SelectionRequirements, candidate: AuthorizedNode, definitions: dict[tuple[str, int], CapabilityDefinition],
              *, now: float, boot_id: str, freshness_seconds: float) -> tuple[str | None, tuple[BoundCapability, ...]]:
    reason = _authority(requirements, candidate)
    if reason:
        return reason, ()
    reason, choices = _implementation(requirements, candidate, definitions)
    if reason:
        return reason, ()
    offer = candidate.offer
    age = now - offer.received_at
    if offer.boot_id != boot_id or not 0 <= age <= freshness_seconds:
        return "offer_stale", ()
    if offer.health != "healthy":
        return "node_unhealthy", ()
    capabilities = _policy(requirements, candidate, choices)
    if capabilities is None:
        return "policy_forbidden", ()
    if candidate.held_slots >= min(offer.slots, candidate.policy.slot_limit):
        return "capacity_exhausted", ()
    if not candidate.resources_known and (requirements.memory_bytes or requirements.disk_bytes):
        return "resource_evidence_missing", ()
    if requirements.memory_bytes + candidate.held_memory_bytes > offer.memory_bytes:
        return "memory_insufficient", ()
    if requirements.disk_bytes + candidate.held_disk_bytes > offer.disk_bytes:
        return "disk_insufficient", ()
    return None, capabilities


def select_target(requirements: SelectionRequirements, authorized_nodes: tuple[AuthorizedNode, ...],
                  definitions: tuple[CapabilityDefinition, ...], *, now: float, boot_id: str,
                  freshness_seconds: float = 120) -> SelectionResult:
    """One candidate pass; O(n + report entries), plus bounded revision matching.

    Definitions and observations are facts, never authority. Only the trusted
    owner/node/workspace/template policy can allow a candidate. The result is
    a proposal: a persistence adapter must recheck and reserve atomically.
    """
    if type(requirements) is not SelectionRequirements:
        raise CapabilityError("invalid_requirements")
    now = timestamp(now)
    identifier(boot_id)
    freshness_seconds = timestamp(freshness_seconds)
    if not 0 < freshness_seconds <= 3600:
        raise CapabilityError("invalid_freshness")
    if type(authorized_nodes) is not tuple or len(authorized_nodes) > MAX_CANDIDATES:
        raise CapabilityError("candidate_limit")
    if type(definitions) is not tuple or len(definitions) > MAX_DEFINITIONS or any(type(item) is not CapabilityDefinition for item in definitions):
        raise CapabilityError("definition_limit")
    by_revision = {(item.capability_id, item.revision): item for item in definitions}
    if len(by_revision) != len(definitions):
        raise CapabilityError("duplicate_definition")
    seen: set[tuple[str, str]] = set()
    exclusions: list[CandidateExclusion] = []
    best: tuple[int, int, str] | None = None
    binding: TargetBinding | None = None
    requirements_digest = hashlib.sha256(requirements.snapshot).hexdigest()
    for candidate in authorized_nodes:
        if type(candidate) is not AuthorizedNode:
            raise CapabilityError("invalid_candidate")
        identity = (candidate.policy.owner_id, candidate.policy.node_id)
        if identity in seen:
            raise CapabilityError("duplicate_candidate")
        seen.add(identity)
        reason, capabilities = _evaluate(requirements, candidate, by_revision, now=now, boot_id=boot_id, freshness_seconds=freshness_seconds)
        if reason:
            exclusions.append(CandidateExclusion(candidate.policy.node_id if identity[0] == requirements.owner_id else None, reason))
            continue
        key = (candidate.policy.priority, candidate.held_slots, candidate.policy.node_id)
        if best is None or key < best:
            best = key
            binding = TargetBinding(candidate.policy.node_id, candidate.offer.offer_revision, candidate.offer.config_digest,
                hashlib.sha256(candidate.policy.snapshot).hexdigest(), capabilities, requirements.input_digest,
                requirements_digest, requirements.memory_bytes, requirements.disk_bytes)
    if binding is not None:
        return SelectionResult(binding)
    bounded = heapq.nsmallest(32, exclusions, key=lambda item: (item.node_id or "", item.reason_code))
    return SelectionResult(None, tuple(bounded), len(exclusions))
