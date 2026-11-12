"""Compose original Node input pinning and capability admission in one database."""
from __future__ import annotations

import hashlib
import sqlite3

from hub.application.artifact_input_service import ArtifactInputService
from hub.domain.artifact_receive import artifact_inputs
from hub.domain.capability import MAX_CANDIDATES, CapabilityError, CapabilityStoreError, SelectionRequirements, integer, timestamp
from hub.domain.capability_routing import RegisteredCapabilityRoute
from hub.domain.workflow_contract import parse_definition
from hub.domain.workflow_contract_types import CatalogRef, ResolvedContract
from hub.domain.workflow_json import decode_snapshot
from hub.domain.workflow_records import CoordinatorClaim
from .artifact_input_repository import ArtifactInputRepository
from .capability_repository import CapabilityRepository
from .workflow_run_executor import RegisteredRunTarget


class CapabilityWorkflowAdmission:
    def __init__(self, capabilities: CapabilityRepository, *, targets: tuple[RegisteredRunTarget, ...],
                 inputs: ArtifactInputService | None = None) -> None:
        if type(targets) is not tuple or any(type(target) is not RegisteredRunTarget for target in targets):
            raise CapabilityError("invalid_route_target")
        self.capabilities, self.inputs = capabilities, inputs
        groups: dict[tuple[str, str, CatalogRef, CatalogRef, bool], dict[str, RegisteredRunTarget]] = {}
        for target in targets:
            config = decode_snapshot(target.config_snapshot)
            if (target.legacy_task is not None or (target.test_recipe is not None and not target.remote_test) or target.review_adapter is not None
                    or config.get("execution_node_id") != target.target_key):
                continue
            key = (target.owner_id, target.workspace_id, target.adapter_ref, target.policy_ref, target.remote_test)
            nodes = groups.setdefault(key, {})
            if target.target_key in nodes:
                raise CapabilityError("duplicate_route_target")
            nodes[target.target_key] = target
        self.targets = {key: tuple(sorted(nodes)) for key, nodes in groups.items()}
        if any(len(nodes) > MAX_CANDIDATES for nodes in self.targets.values()):
            raise CapabilityError("candidate_limit")
        if inputs is not None and (not isinstance(inputs.repository, ArtifactInputRepository)
                or inputs.repository.workflows.db_path != capabilities.workflows.db_path):
            raise CapabilityError("invalid_route_input_store")

    def _eligible(self, registration: RegisteredCapabilityRoute, resolved: ResolvedContract) -> tuple[str, ...]:
        remote_test = resolved.step_key == "test" and registration.selection.template == CatalogRef("coding-delivery", 1)
        if remote_test:
            from hub.application.workflow_templates import validate_coding_test_contract

            validate_coding_test_contract(resolved)
            if registration.selection.operating_systems != ("linux",):
                raise CapabilityStoreError("unsupported_capability_target")
        nodes = self.targets.get((registration.owner_id, registration.workspace_id, registration.adapter_ref, registration.policy_ref, remote_test), ())
        if not nodes:
            raise CapabilityStoreError("executor_unavailable")
        required = {key for key, _ in artifact_inputs(resolved)}
        if not required:
            return nodes
        if self.inputs is None:
            raise CapabilityStoreError("input_alias_unregistered")
        permitted = set(registration.selection.input_aliases)
        selected: list[str] = []
        for node in nodes:
            target = self.inputs.targets.get((registration.owner_id, registration.workspace_id, node))
            aliases = {key: alias for key, alias, _ in target.aliases} if target is not None else {}
            if required.issubset(aliases) and all(aliases[key] in permitted for key in required):
                selected.append(node)
        if not selected:
            raise CapabilityStoreError("input_alias_unregistered")
        return tuple(selected)

    def advance(self, claim: CoordinatorClaim, *, expected_revision: int, generation: int,
                resolved: ResolvedContract, registration: RegisteredCapabilityRoute, now: float) -> None:
        now = timestamp(now)
        integer(expected_revision, minimum=1)
        integer(generation, minimum=1)
        if (type(claim) is not CoordinatorClaim or type(resolved) is not ResolvedContract
                or type(registration) is not RegisteredCapabilityRoute):
            raise CapabilityStoreError("registration_conflict")
        if (claim.owner_id, claim.workspace_id) != (
                registration.owner_id, registration.workspace_id):
            raise CapabilityStoreError("registration_conflict")
        nodes = self._eligible(registration, resolved)
        has_inputs = bool(artifact_inputs(resolved))
        if has_inputs:
            self.inputs.validate_inputs(resolved)
        with self.capabilities._transaction() as connection:
            workflow = self.capabilities.workflows._fenced(connection, claim, now, expected_revision)
            self.capabilities.workflows._creation_open(connection, workflow)
            definition = parse_definition(bytes(workflow["definition_snapshot"]))
            if hashlib.sha256(definition.snapshot).hexdigest() != registration.definition_digest or registration.definition_digest != workflow["definition_digest"]:
                raise CapabilityStoreError("registration_conflict")
            requirements = registration.requirements(definition, resolved)
            step = connection.execute("SELECT kind,state,resolved_snapshot,resolved_digest,target_key,contract_snapshot FROM workflow_steps "
                "WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=? AND is_current=1 LIMIT 1",
                (claim.owner_id, claim.workflow_id, registration.step_key, generation)).fetchone()
            if step is None or step["state"] not in {"pending", "ready", "waiting_node", "waiting_approval", "paused"}:
                raise CapabilityStoreError("step_not_ready")
            if step["kind"] != "native_run" and not (step["kind"] == "validation" and resolved.step_key == "test"
                    and requirements.template == CatalogRef("coding-delivery", 1)):
                raise CapabilityStoreError("unsupported_capability_target")
            if step["resolved_snapshot"] is None:
                self._freeze(connection, claim, workflow, step, resolved, requirements, nodes, expected_revision, generation, now)
                return
            if (bytes(step["resolved_snapshot"]), step["resolved_digest"]) != (resolved.snapshot, resolved.digest):
                raise CapabilityStoreError("registration_conflict")
            if has_inputs:
                selection = self.capabilities._select(connection, requirements, node_id=step["target_key"], eligible_nodes=nodes, now=now)
                if selection.binding is None:
                    self.capabilities.workflows.defer_admission(claim, expected_revision=expected_revision,
                        reason_code="capability_unavailable", step_key=resolved.step_key, now=now, connection=connection)
                    return
                if not self._prepare(connection, claim, resolved, step["target_key"], expected_revision, generation, now):
                    return
            self.capabilities.admit(claim, step_key=resolved.step_key, generation=generation, expected_revision=expected_revision,
                requirements=requirements, reserve_tokens=registration.reserve_tokens, reserve_requests=registration.reserve_requests,
                now=now, connection=connection, eligible_nodes=nodes)

    def _freeze(self, connection: sqlite3.Connection, claim: CoordinatorClaim, workflow: sqlite3.Row, step: sqlite3.Row,
                resolved: ResolvedContract, requirements: SelectionRequirements, nodes: tuple[str, ...], expected_revision: int,
                generation: int, now: float) -> None:
        self.capabilities.workflows._validate_resolved(workflow, claim, resolved.step_key, resolved,
            contract_snapshot=bytes(step["contract_snapshot"]))
        if not artifact_inputs(resolved):
            self.capabilities.workflows.freeze_inputs(claim, step_key=resolved.step_key, generation=generation,
                resolved=resolved, expected_revision=expected_revision, now=now, connection=connection)
            return
        selection = self.capabilities._select(connection, requirements, now=now, eligible_nodes=nodes)
        if selection.binding is None:
            self.capabilities.workflows.defer_admission(claim, expected_revision=expected_revision,
                reason_code="capability_unavailable", step_key=resolved.step_key, now=now, connection=connection)
            return
        self._prepare(connection, claim, resolved, selection.binding.node_id, expected_revision, generation, now)

    def _prepare(self, connection: sqlite3.Connection, claim: CoordinatorClaim, resolved: ResolvedContract,
                 node: str | None, expected_revision: int, generation: int, now: float) -> bool:
        if self.inputs is None or node is None:
            raise CapabilityStoreError("input_alias_unregistered")
        target = self.inputs.targets.get((claim.owner_id, claim.workspace_id, node))
        if target is None:
            raise CapabilityStoreError("input_alias_unregistered")
        return self.inputs.repository.prepare(claim, expected_revision=expected_revision, resolved=resolved,
            generation=generation, target=target, now=now, connection=connection)
