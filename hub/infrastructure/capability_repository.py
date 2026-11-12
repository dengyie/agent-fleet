"""Pinned directory, authenticated observations and atomic Workflow admission."""
from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from hub.application.capability_router import select_target
from hub.domain.capability import (
    MAX_CANDIDATES, MAX_DEFINITIONS, MAX_REVISION, AuthorizedNode, CapabilityAdmission, CapabilityDefinition,
    CapabilityError, CapabilityStoreError, NodeOffer, NodePolicy, SelectionRequirements, SelectionResult,
    TargetBinding, decode, freeze, identifier, integer, parse_offer, timestamp,
)
from hub.domain.workflow_contract import parse_definition, parse_resolved
from hub.domain.workflow_contract_types import CatalogRef
from hub.domain.workflow_errors import WorkflowPersistenceError, WorkflowValidationError
from hub.domain.workflow_records import CoordinatorClaim, DispatchIntent
from .capability_schema import STATEMENTS, TABLE_COLUMNS, inspect_capability_schema
from .platform_db import node_credential_is_current
from .workflow_repository import WorkflowRepository

MAX_BINDINGS = 10000


class CapabilityRepository:
    def __init__(self, workflows: WorkflowRepository, *, boot_id: str) -> None:
        self.workflows = workflows
        self.boot_id = identifier(boot_id)
        self._startup_sequence: int | None = None

    @contextmanager
    def _transaction(self, *, write: bool = True, connection: sqlite3.Connection | None = None) -> Iterator[sqlite3.Connection]:
        try:
            scope = self.workflows.transaction(write=write) if connection is None else self.workflows._connection(connection)
            with scope as connection:
                if self._startup_sequence is None or not inspect_capability_schema(connection):
                    raise CapabilityStoreError("capability_uninitialized")
                yield connection
        except (WorkflowPersistenceError, CapabilityError, WorkflowValidationError) as exc:
            if isinstance(exc, CapabilityStoreError):
                raise
            raise CapabilityStoreError(exc.code) from exc

    def init(self) -> None:
        try:
            with self.workflows.transaction() as connection:
                if not inspect_capability_schema(connection):
                    self._create_schema(connection)
                inspect_capability_schema(connection)
                sequence = connection.execute("SELECT report_sequence FROM capability_schema_meta WHERE singleton=1").fetchone()[0]
            if self._startup_sequence is None:
                self._startup_sequence = sequence
        except WorkflowPersistenceError as exc:
            if isinstance(exc, CapabilityStoreError):
                raise
            raise CapabilityStoreError(exc.code) from exc

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        for statement in STATEMENTS:
            connection.execute(statement)
        connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=max(minimum_reader_version,2) WHERE singleton=1")

    @staticmethod
    def _limit(connection: sqlite3.Connection, table: str, maximum: int) -> None:
        if connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] >= maximum:
            raise CapabilityStoreError("capability_catalog_limit")

    def register_definition(self, definition: CapabilityDefinition, *, now: float) -> None:
        if type(definition) is not CapabilityDefinition:
            raise CapabilityError("invalid_definition")
        now = timestamp(now)
        snapshot = definition.snapshot
        with self._transaction() as connection:
            previous = connection.execute("SELECT snapshot,digest FROM capability_definitions WHERE capability_id=? AND revision=?",
                (definition.capability_id, definition.revision)).fetchone()
            if previous is not None:
                self._check_digest(bytes(previous["snapshot"]), previous["digest"])
                if bytes(previous["snapshot"]) != snapshot:
                    raise CapabilityStoreError("definition_conflict")
                return
            self._limit(connection, "capability_definitions", MAX_DEFINITIONS)
            connection.execute("INSERT INTO capability_definitions VALUES(?,?,?,?,?)",
                (definition.capability_id, definition.revision, snapshot, definition.digest, now))

    def register_node(self, policy: NodePolicy, *, now: float) -> None:
        if type(policy) is not NodePolicy:
            raise CapabilityError("invalid_node_policy")
        now = timestamp(now)
        snapshot = policy.snapshot
        with self._transaction() as connection:
            previous = connection.execute("SELECT 1 FROM capability_node_policies WHERE owner_id=? AND node_id=?",
                (policy.owner_id, policy.node_id)).fetchone()
            if previous is None:
                self._limit(connection, "capability_node_policies", MAX_DEFINITIONS)
                owner_count = connection.execute("SELECT count(*) FROM capability_node_policies WHERE owner_id=?", (policy.owner_id,)).fetchone()[0]
                if owner_count >= MAX_CANDIDATES:
                    raise CapabilityStoreError("candidate_limit")
            if connection.execute("SELECT 1 FROM nodes WHERE owner_id=? AND node_id=?", (policy.owner_id, policy.node_id)).fetchone() is None:
                raise CapabilityStoreError("node_unregistered")
            connection.execute("INSERT INTO capability_node_policies VALUES(?,?,?,?,?) ON CONFLICT(owner_id,node_id) DO UPDATE SET "
                "snapshot=excluded.snapshot,digest=excluded.digest,updated_at=excluded.updated_at",
                (policy.owner_id, policy.node_id, snapshot, hashlib.sha256(snapshot).hexdigest(), now))

    def record_offer(self, owner: str, node: str, raw: bytes, *, credential_fingerprint: str, now: float) -> NodeOffer:
        identifier(owner)
        identifier(node)
        offer = parse_offer(raw, received_at=now, boot_id=self.boot_id)
        with self._transaction() as connection:
            if not node_credential_is_current(connection, owner=owner, node=node, fingerprint=credential_fingerprint):
                raise CapabilityStoreError("credential_revoked")
            if connection.execute("SELECT 1 FROM capability_node_policies WHERE owner_id=? AND node_id=?", (owner, node)).fetchone() is None:
                raise CapabilityStoreError("node_unregistered")
            previous = connection.execute("SELECT offer_revision,config_digest,received_at FROM capability_node_offers WHERE owner_id=? AND node_id=?", (owner, node)).fetchone()
            if previous is not None and (offer.offer_revision < previous["offer_revision"] or (
                    offer.offer_revision == previous["offer_revision"] and offer.config_digest != previous["config_digest"])):
                raise CapabilityStoreError("offer_revision_conflict")
            if previous is not None and offer.received_at < previous["received_at"]:
                raise CapabilityStoreError("report_clock_regressed")
            sequence = connection.execute("SELECT report_sequence FROM capability_schema_meta WHERE singleton=1").fetchone()[0] + 1
            integer(sequence, maximum=MAX_REVISION)
            connection.execute("UPDATE capability_schema_meta SET report_sequence=? WHERE singleton=1", (sequence,))
            connection.execute("INSERT INTO capability_node_offers VALUES(?,?,?,?,?,?,?) ON CONFLICT(owner_id,node_id) DO UPDATE SET "
                "offer_revision=excluded.offer_revision,snapshot=excluded.snapshot,config_digest=excluded.config_digest,"
                "received_at=excluded.received_at,report_sequence=excluded.report_sequence",
                (owner, node, offer.offer_revision, freeze(offer.payload()), offer.config_digest, offer.received_at, sequence))
        return offer

    @staticmethod
    def _check_digest(snapshot: bytes, expected: str) -> None:
        if hashlib.sha256(snapshot).hexdigest() != expected:
            raise CapabilityStoreError("capability_snapshot_corrupt")

    def _definitions(self, connection: sqlite3.Connection, requirements: SelectionRequirements) -> tuple[CapabilityDefinition, ...]:
        pairs = tuple((ref.id, revision) for ref in requirements.capabilities for revision in ref.revisions)
        placeholders = ",".join("(?,?)" for pair in pairs)
        rows = connection.execute("SELECT capability_id,revision,snapshot,digest FROM capability_definitions WHERE "
            f"(capability_id,revision) IN ({placeholders}) LIMIT 257", tuple(value for pair in pairs for value in pair)).fetchall()
        definitions: list[CapabilityDefinition] = []
        for row in rows:
            raw = bytes(row["snapshot"])
            self._check_digest(raw, row["digest"])
            item = CapabilityDefinition.from_snapshot(raw)
            if (item.capability_id, item.revision) != (row["capability_id"], row["revision"]) or item.snapshot != raw:
                raise CapabilityStoreError("capability_snapshot_corrupt")
            definitions.append(item)
        return tuple(definitions)

    def _select(self, connection: sqlite3.Connection, requirements: SelectionRequirements, *, now: float,
                node_id: str | None = None, exclude: tuple[str, str, str, int] | None = None,
                eligible_nodes: tuple[str, ...] | None = None) -> SelectionResult:
        eligible_sql = ""
        eligible_args: tuple[str, ...] = ()
        if eligible_nodes is not None:
            if type(eligible_nodes) is not tuple or len(eligible_nodes) > MAX_CANDIDATES or len(set(eligible_nodes)) != len(eligible_nodes):
                raise CapabilityStoreError("candidate_limit")
            for node in eligible_nodes:
                identifier(node)
            eligible_sql = "AND p.node_id IN (" + (",".join("?" for _ in eligible_nodes) or "NULL") + ") "
            eligible_args = eligible_nodes
        rows = connection.execute(
            "SELECT p.owner_id,p.node_id,p.snapshot AS policy,p.digest AS policy_digest,n.enabled,c.enabled AS credential_enabled,"
            "coalesce((SELECT enabled FROM workspaces WHERE owner_id=? AND workspace_id=?),0) AS workspace_enabled,"
            "o.offer_revision,o.snapshot AS offer,o.config_digest,o.received_at,o.report_sequence FROM capability_node_policies p "
            "LEFT JOIN nodes n USING(owner_id,node_id) LEFT JOIN node_credentials c USING(owner_id,node_id) "
            "LEFT JOIN capability_node_offers o USING(owner_id,node_id) WHERE p.owner_id=? AND (? IS NULL OR p.node_id=?) "
            + eligible_sql + "ORDER BY p.node_id LIMIT 257",
            (requirements.owner_id, requirements.workspace_id, requirements.owner_id, node_id, node_id) + eligible_args).fetchall()
        if len(rows) > MAX_CANDIDATES:
            raise CapabilityStoreError("candidate_limit")
        nodes = tuple(row["node_id"] for row in rows)
        placeholders = ",".join("?" for node in nodes) or "NULL"
        # The existing target_key is a global physical-resource key. Include
        # legacy and isolated reservations rather than inventing spare slots.
        held = {row["target_key"]: row for row in connection.execute(
            "SELECT r.target_key,count(*) AS slots,coalesce(sum(b.memory_bytes),0) AS memory_bytes,"
            "coalesce(sum(b.disk_bytes),0) AS disk_bytes,count(b.node_id) AS known FROM capability_reservations r LEFT JOIN capability_step_bindings b "
            "USING(owner_id,workspace_id,workflow_id,step_key,generation) WHERE r.state IN ('held','isolated') "
            f"AND r.target_key IN ({placeholders}) AND NOT (r.owner_id=? AND r.workflow_id=? AND r.step_key=? AND r.generation=?) "
            "GROUP BY r.target_key", nodes + (exclude or ("", "", "", 0)))}
        candidates: list[AuthorizedNode] = []
        for row in rows:
            policy_raw = bytes(row["policy"])
            self._check_digest(policy_raw, row["policy_digest"])
            policy = NodePolicy.from_snapshot(policy_raw)
            if policy.snapshot != policy_raw or (policy.owner_id, policy.node_id) != (row["owner_id"], row["node_id"]):
                raise CapabilityStoreError("capability_snapshot_corrupt")
            offer = None
            if row["offer"] is not None:
                observed_boot = self.boot_id if row["report_sequence"] > self._startup_sequence else "before:" + self.boot_id[:120]
                offer = parse_offer(bytes(row["offer"]), received_at=row["received_at"], boot_id=observed_boot)
                if offer.offer_revision != row["offer_revision"] or offer.config_digest != row["config_digest"]:
                    raise CapabilityStoreError("capability_snapshot_corrupt")
            usage = held.get(policy.node_id)
            candidates.append(AuthorizedNode(policy, offer, bool(row["enabled"]), bool(row["credential_enabled"]), bool(row["workspace_enabled"]),
                usage["slots"] if usage else 0, usage["memory_bytes"] if usage else 0, usage["disk_bytes"] if usage else 0,
                usage["known"] == usage["slots"] if usage else True))
        return select_target(requirements, tuple(candidates), self._definitions(connection, requirements), now=now, boot_id=self.boot_id)

    def select(self, requirements: SelectionRequirements, *, now: float) -> SelectionResult:
        if type(requirements) is not SelectionRequirements:
            raise CapabilityError("invalid_requirements")
        now = timestamp(now)
        with self._transaction(write=False) as connection:
            return self._select(connection, requirements, now=now)

    def admit(self, claim: CoordinatorClaim, *, step_key: str, generation: int, expected_revision: int,
              requirements: SelectionRequirements, reserve_tokens: int, reserve_requests: int, now: float,
              connection: sqlite3.Connection | None = None, eligible_nodes: tuple[str, ...] | None = None) -> CapabilityAdmission:
        identifier(step_key)
        integer(generation, minimum=1)
        integer(expected_revision, minimum=1)
        integer(reserve_tokens)
        integer(reserve_requests)
        now = timestamp(now)
        if type(requirements) is not SelectionRequirements or type(claim) is not CoordinatorClaim:
            raise CapabilityError("invalid_requirements")
        with self._transaction(connection=connection) as connection:
            identity = (claim.owner_id, claim.workflow_id, step_key, generation)
            previous = connection.execute("SELECT snapshot,digest,requirements_snapshot,admission_revision FROM capability_step_bindings "
                "WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=?", identity).fetchone()
            if previous is not None:
                return self._replay(connection, claim, identity, previous, requirements, now=now, expected_revision=expected_revision)
            workflow = self.workflows._fenced(connection, claim, now, expected_revision)
            self.workflows._creation_open(connection, workflow)
            step = connection.execute("SELECT state,resolved_snapshot,resolved_digest,target_key FROM workflow_steps "
                "WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=? AND is_current=1", identity).fetchone()
            if step is None or step["state"] not in {"ready", "waiting_node"} or step["resolved_snapshot"] is None:
                raise CapabilityStoreError("step_not_ready")
            self._validate_requirements(workflow, step, claim, requirements)
            selection = self._select(connection, requirements, now=now, node_id=step["target_key"], eligible_nodes=eligible_nodes)
            if selection.binding is None:
                self.workflows.defer_admission(claim, expected_revision=expected_revision, step_key=step_key,
                    reason_code="capability_unavailable", now=now, connection=connection)
                return CapabilityAdmission(selection, None)
            binding = selection.binding
            policy_row = connection.execute("SELECT snapshot FROM capability_node_policies WHERE owner_id=? AND node_id=?",
                (claim.owner_id, binding.node_id)).fetchone()
            policy = NodePolicy.from_snapshot(bytes(policy_row[0]))
            intent = self.workflows.admit_step(claim, step_key=step_key, generation=generation, expected_revision=expected_revision,
                target_key=binding.node_id, capacity=policy.slot_limit, reserve_tokens=reserve_tokens, reserve_requests=reserve_requests,
                now=now, connection=connection)
            self._limit(connection, "capability_step_bindings", MAX_BINDINGS)
            connection.execute("INSERT INTO capability_step_bindings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (claim.owner_id, claim.workspace_id, claim.workflow_id, step_key, generation, binding.node_id,
                 binding.snapshot, hashlib.sha256(binding.snapshot).hexdigest(), requirements.snapshot, binding.memory_bytes, binding.disk_bytes, now, expected_revision))
            self.workflows._event(connection, claim.owner_id, claim.workflow_id, "capability.bound",
                {"step_key": step_key, "generation": generation, "node_id": binding.node_id, "offer_revision": binding.offer_revision,
                 "binding_digest": hashlib.sha256(binding.snapshot).hexdigest()}, now)
            return CapabilityAdmission(selection, intent)

    @staticmethod
    def _validate_requirements(workflow: sqlite3.Row, step: sqlite3.Row, claim: CoordinatorClaim, requirements: SelectionRequirements) -> None:
        resolved = parse_resolved(bytes(step["resolved_snapshot"]))
        definition = parse_definition(bytes(workflow["definition_snapshot"]))
        if resolved.digest != step["resolved_digest"] or hashlib.sha256(definition.snapshot).hexdigest() != workflow["definition_digest"]:
            raise CapabilityStoreError("capability_snapshot_corrupt")
        adapter = decode(resolved.snapshot)["adapter_ref"]
        if (requirements.owner_id, requirements.workspace_id, requirements.template, requirements.input_digest, requirements.adapter_id,
                requirements.capabilities, requirements.max_active_seconds) != (
                claim.owner_id, claim.workspace_id, CatalogRef(definition.template_id, definition.revision), resolved.digest, adapter["id"],
                resolved.contract.capability_requirements, resolved.contract.budget.max_active_seconds):
            raise CapabilityStoreError("requirements_conflict")

    def _replay(self, connection: sqlite3.Connection, claim: CoordinatorClaim, identity: tuple[str, str, str, int],
                row: sqlite3.Row, requirements: SelectionRequirements, *, now: float, expected_revision: int) -> CapabilityAdmission:
        revision = connection.execute("SELECT revision FROM workflow_runs WHERE owner_id=? AND workflow_id=?", identity[:2]).fetchone()
        if revision is None:
            raise CapabilityStoreError("binding_conflict")
        if expected_revision not in (revision[0], row["admission_revision"]):
            raise CapabilityStoreError("revision_conflict")
        self.workflows._fenced(connection, claim, now, revision[0])
        raw = bytes(row["snapshot"])
        self._check_digest(raw, row["digest"])
        binding = TargetBinding.from_snapshot(raw)
        if bytes(row["requirements_snapshot"]) != requirements.snapshot or binding.requirements_digest != hashlib.sha256(requirements.snapshot).hexdigest():
            raise CapabilityStoreError("binding_conflict")
        dispatch = connection.execute("SELECT token,payload_hash,target_key FROM workflow_dispatches "
            "WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=?", identity).fetchone()
        step = connection.execute("SELECT resolved_snapshot,is_current FROM workflow_steps WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=?", identity).fetchone()
        if dispatch is None or step is None or not step["is_current"] or (dispatch["target_key"], dispatch["payload_hash"]) != (binding.node_id, binding.input_digest):
            raise CapabilityStoreError("binding_conflict")
        intent = DispatchIntent(dispatch["token"], dispatch["payload_hash"], dispatch["target_key"], bytes(step["resolved_snapshot"]))
        return CapabilityAdmission(SelectionResult(binding), intent)

    @classmethod
    def validate_snapshot(cls, connection: sqlite3.Connection) -> None:
        """Bounded backup validation; capability tables contain no artifact IDs."""
        if not inspect_capability_schema(connection):
            return
        for table in TABLE_COLUMNS:
            if connection.execute(f"PRAGMA foreign_key_check({table})").fetchone() is not None:
                raise CapabilityStoreError("capability_snapshot_corrupt")
        previous_factory = connection.row_factory
        connection.row_factory = sqlite3.Row
        try:
            cls._validate_records(connection)
        finally:
            connection.row_factory = previous_factory

    @classmethod
    def _validate_records(cls, connection: sqlite3.Connection) -> None:
        definitions: dict[tuple[str, int], str] = {}
        for count, row in enumerate(connection.execute("SELECT capability_id,revision,snapshot,digest FROM capability_definitions LIMIT ?", (MAX_DEFINITIONS + 1,)), 1):
            if count > MAX_DEFINITIONS:
                raise CapabilityStoreError("capability_scan_limit")
            raw = bytes(row["snapshot"])
            cls._check_digest(raw, row["digest"])
            item = CapabilityDefinition.from_snapshot(raw)
            if item.snapshot != raw or (item.capability_id, item.revision) != (row["capability_id"], row["revision"]):
                raise CapabilityStoreError("capability_snapshot_corrupt")
            definitions[(item.capability_id, item.revision)] = row["digest"]
        for count, row in enumerate(connection.execute("SELECT owner_id,node_id,snapshot,digest FROM capability_node_policies LIMIT ?", (MAX_DEFINITIONS + 1,)), 1):
            if count > MAX_DEFINITIONS:
                raise CapabilityStoreError("capability_scan_limit")
            raw = bytes(row["snapshot"])
            cls._check_digest(raw, row["digest"])
            policy = NodePolicy.from_snapshot(raw)
            if policy.snapshot != raw or (policy.owner_id, policy.node_id) != (row["owner_id"], row["node_id"]):
                raise CapabilityStoreError("capability_snapshot_corrupt")
        cursor = connection.execute("SELECT offer_revision,snapshot,config_digest,received_at,report_sequence FROM capability_node_offers LIMIT ?", (MAX_DEFINITIONS + 1,))
        sequence = connection.execute("SELECT report_sequence FROM capability_schema_meta WHERE singleton=1").fetchone()[0]
        for count, row in enumerate(cursor, 1):
            if count > MAX_DEFINITIONS:
                raise CapabilityStoreError("capability_scan_limit")
            offer = parse_offer(bytes(row["snapshot"]), received_at=row["received_at"], boot_id="backup")
            if offer.offer_revision != row["offer_revision"] or offer.config_digest != row["config_digest"] or not 0 < row["report_sequence"] <= sequence:
                raise CapabilityStoreError("capability_snapshot_corrupt")
        cursor = connection.execute("SELECT b.owner_id,b.workspace_id,b.workflow_id,b.step_key,b.generation,b.node_id,b.snapshot,b.digest,"
            "b.requirements_snapshot,b.memory_bytes,b.disk_bytes,r.target_key,d.payload_hash,s.resolved_digest,br.reserved_tokens AS budget_tokens "
            "FROM capability_step_bindings b JOIN capability_reservations r USING(owner_id,workspace_id,workflow_id,step_key,generation) "
            "LEFT JOIN workflow_dispatches d USING(owner_id,workspace_id,workflow_id,step_key,generation) "
            "LEFT JOIN workflow_budget_reservations br USING(owner_id,workspace_id,workflow_id,step_key,generation) "
            "JOIN workflow_steps s USING(owner_id,workspace_id,workflow_id,step_key,generation) LIMIT ?", (MAX_BINDINGS + 1,))
        for count, row in enumerate(cursor, 1):
            if count > MAX_BINDINGS:
                raise CapabilityStoreError("capability_scan_limit")
            raw = bytes(row["snapshot"])
            cls._check_digest(raw, row["digest"])
            binding = TargetBinding.from_snapshot(raw)
            if row["budget_tokens"] is None:
                raise CapabilityStoreError("capability_snapshot_corrupt")
            requested = bytes(row["requirements_snapshot"])
            requirement = decode(requested)
            if binding.snapshot != raw or binding.requirements_digest != hashlib.sha256(requested).hexdigest():
                raise CapabilityStoreError("capability_snapshot_corrupt")
            if (binding.node_id, binding.node_id, binding.input_digest, binding.input_digest, binding.memory_bytes, binding.disk_bytes) != (
                    row["node_id"], row["target_key"], row["payload_hash"], row["resolved_digest"], row["memory_bytes"], row["disk_bytes"]):
                raise CapabilityStoreError("capability_snapshot_corrupt")
            if (requirement.get("owner_id"), requirement.get("workspace_id"), requirement.get("input_digest")) != (
                    row["owner_id"], row["workspace_id"], binding.input_digest):
                raise CapabilityStoreError("capability_snapshot_corrupt")
            if any(definitions.get((ref.id, ref.revision)) != ref.definition_digest for ref in binding.capabilities):
                raise CapabilityStoreError("capability_snapshot_corrupt")
