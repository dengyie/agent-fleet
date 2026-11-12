"""Short, fenced SQLite transactions for workflow admission; no execution I/O."""
from __future__ import annotations

import hashlib
import math
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator

from hub.domain.workflow_contract import parse_contract, parse_definition, parse_resolved, resolve_contract
from hub.domain.workflow_contract_types import (
    ArtifactReference, AuthorizedInputs, CatalogRef, CodeRevisionReference,
    InputBinding, NamedReference, PriorOutputReference, ResolvedContract, WorkflowDefinition,
)
from hub.domain.workflow_errors import WorkflowPersistenceError, WorkflowValidationError
from hub.domain.workflow_execution import DispatchRecord, ExecutionBinding
from hub.domain.workflow_execution import ExecutionObservation, HubInputTarget
from hub.domain.workflow_external import RegisteredExternalTarget
from hub.domain.workflow_control import WorkflowReconciliationRequest, WorkflowStopRequest
from hub.domain.workflow_evidence import ValidationEvidence
from hub.domain.workflow import transition_step
from hub.domain.workflow_projection import TERMINAL_STATES, project_workflow
from hub.domain.workflow_identity import canonical_digest, canonical_json, dispatch_token, validate_reference
from hub.domain.workflow_json import decode_snapshot
from hub.domain.workflow_records import (
    CoordinatorClaim, DispatchIntent, WorkflowEvent, WorkflowEventPage, WorkflowRecord, WorkflowSnapshot, WorkflowStepRecord,
)
from . import workflow_schema

_RUN_COLUMNS = (
    "workflow_id,owner_id,workspace_id,definition_snapshot,definition_digest,execution_state,"
    "validation_state,acceptance_state,revision,cancel_requested,pause_requested,next_poll_at,created_at,updated_at,reason_code"
)
_STEP_COLUMNS = "step_key,generation,kind,state,resolved_snapshot,resolved_digest,target_key,validation_state,subject_digest,reason_code,active_deadline,wait_deadline,contract_snapshot"
_ACTIVE_STATES = workflow_schema.ACTIVE_STATES
_MAX_INTEGER = 9007199254740991


class WorkflowRepositoryError(WorkflowPersistenceError):
    """SQLite implementation of the workflow persistence failure contract."""


def _integer(value: int, *, minimum: int = 1, maximum: int = _MAX_INTEGER) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise WorkflowRepositoryError("invalid_integer")
    return value


def _time(value: float, *, positive: bool = False) -> float:
    if type(value) not in (int, float) or not 0 <= value <= 253402300799 or not math.isfinite(value):
        raise WorkflowRepositoryError("invalid_time")
    if positive and value <= 0:
        raise WorkflowRepositoryError("invalid_time")
    return float(value)


def _id(value: str) -> str:
    try:
        return validate_reference(value)
    except WorkflowValidationError as exc:
        raise WorkflowRepositoryError("invalid_identifier") from exc


def _run(row: sqlite3.Row) -> WorkflowRecord:
    return WorkflowRecord(
        row["workflow_id"], row["owner_id"], row["workspace_id"], bytes(row["definition_snapshot"]),
        row["definition_digest"], row["execution_state"], row["validation_state"], row["acceptance_state"],
        row["revision"], bool(row["cancel_requested"]), bool(row["pause_requested"]),
        row["next_poll_at"], row["created_at"], row["updated_at"], row["reason_code"],
    )


def _step(row: sqlite3.Row) -> WorkflowStepRecord:
    return WorkflowStepRecord(
        row["step_key"], row["generation"], row["kind"], row["state"],
        bytes(row["resolved_snapshot"]) if row["resolved_snapshot"] is not None else None,
        row["resolved_digest"], row["target_key"],
        row["validation_state"], row["subject_digest"], row["reason_code"],
        active_deadline=row["active_deadline"], wait_deadline=row["wait_deadline"],
        contract_snapshot=bytes(row["contract_snapshot"]),
    )


class WorkflowRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path).resolve()
        self.artifact_input_public_key: bytes | None = None
        self._hub_input_targets: dict[tuple[str, str, str], HubInputTarget] = {}

    def configure_hub_input_targets(self, targets: tuple[HubInputTarget, ...]) -> None:
        if type(targets) is not tuple or len(targets) > 256 or any(type(item) is not HubInputTarget for item in targets):
            raise WorkflowRepositoryError("invalid_hub_input_target")
        indexed = {(item.owner_id, item.workspace_id, item.target_key): item for item in targets}
        if len(indexed) != len(targets):
            raise WorkflowRepositoryError("duplicate_hub_input_target")
        self._hub_input_targets = indexed

    def requires_node_inputs(self, resolved: ResolvedContract, *, kind: str, target_key: str) -> bool:
        target = self._hub_input_targets.get((resolved.owner_id, resolved.workspace_id, target_key))
        if target is None:
            return True
        adapter = decode_snapshot(resolved.snapshot)["adapter_ref"]
        return (target.kind, target.policy_ref, {"id": target.adapter_ref.id, "revision": target.adapter_ref.revision}) != (
            kind, resolved.contract.policy_ref, adapter)

    @contextmanager
    def transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        """Own one connection and transaction; close rolls back on every exception."""
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.db_path.as_uri() + "?mode=rw", uri=True, timeout=10, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except sqlite3.Error as exc:
            raise WorkflowRepositoryError("workflow_store") from exc
        except workflow_schema.WorkflowSchemaError as exc:
            raise WorkflowRepositoryError(exc.code) from exc
        finally:
            if connection is not None:
                connection.close()

    @contextmanager
    def _connection(self, connection: sqlite3.Connection | None = None) -> Iterator[sqlite3.Connection]:
        if connection is None:
            with self.transaction() as owned:
                yield owned
            return
        path = connection.execute("PRAGMA database_list").fetchone()[2]
        if not connection.in_transaction or Path(path).resolve() != self.db_path:
            raise WorkflowRepositoryError("invalid_transaction")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise WorkflowRepositoryError("invalid_transaction")
        connection.execute("SAVEPOINT workflow_operation")
        try:
            yield connection
        except BaseException as exc:
            connection.execute("ROLLBACK TO workflow_operation")
            connection.execute("RELEASE workflow_operation")
            if isinstance(exc, sqlite3.Error):
                raise WorkflowRepositoryError("workflow_store") from exc
            raise
        connection.execute("RELEASE workflow_operation")

    def init(self) -> None:
        with self.transaction() as connection:
            row = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None or row[0] not in ("3", "4"):
                raise WorkflowRepositoryError("unsupported_platform_schema")
            from hub.domain.mcp_catalog import MCPCatalogError
            from .mcp_catalog_schema import inspect_catalog_schema

            try:
                inspect_catalog_schema(connection)
                existing = workflow_schema.inspect_schema(connection)
                if existing is None:
                    for statement in workflow_schema.SCHEMA_STATEMENTS:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO workflow_schema_meta VALUES(1,?,?, 'open')",
                        (workflow_schema.SCHEMA_VERSION, workflow_schema.BASE_READER_VERSION),
                    )
                self._init_coordinator_queue(connection)
                for statement in workflow_schema.CONTROL_STATEMENTS:
                    connection.execute(statement)
                from .workflow_summary_repository import WorkflowSummaryRepository

                WorkflowSummaryRepository.validate_snapshot(connection)
                self._init_budget_links(connection)
                from .workflow_external_repository import ExternalWorkflowRepository

                ExternalWorkflowRepository.validate_snapshot(connection)
            except (workflow_schema.WorkflowSchemaError, MCPCatalogError, WorkflowValidationError) as exc:
                raise WorkflowRepositoryError(exc.code) from exc

    @staticmethod
    def _init_budget_links(connection: sqlite3.Connection) -> None:
        from .workflow_native_memory_repository import native_memory_history

        for rows in native_memory_history(connection):
            updates: list[tuple[Any, ...]] = []
            identities: list[tuple[Any, ...]] = []
            for row in rows:
                if row["binding_run_id"] is None:
                    continue
                identity = (row["owner_id"], row["workflow_id"], row["step_key"], row["generation"])
                if row["budget_workflow"] is None or row["child_request_id"] not in (None, row["binding_run_id"]):
                    raise WorkflowRepositoryError("usage_attribution_conflict")
                updates.append((row["binding_run_id"], *identity))
                identities.append(identity)
            connection.executemany("UPDATE workflow_budget_reservations SET child_request_id=? WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=?", updates)
            connection.executemany("INSERT OR IGNORE INTO workflow_budget_checks(owner_id,workflow_id,step_key,generation) VALUES(?,?,?,?)", identities)

    def _init_coordinator_queue(self, connection: sqlite3.Connection) -> None:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_coordinator_owners'").fetchone():
            return
        for statement in workflow_schema.COORDINATOR_STATEMENTS:
            connection.execute(statement)
        # An older schema-v1 backup has execution facts but no optional queue.
        cursor = ""
        while True:
            rows = connection.execute(
                "SELECT owner_id FROM workflow_runs WHERE owner_id>? GROUP BY owner_id ORDER BY owner_id LIMIT 128", (cursor,),
            ).fetchall()
            for row in rows:
                self._refresh_owner_queue(connection, row[0])
            if len(rows) < 128:
                return
            cursor = rows[-1][0]

    @staticmethod
    def _refresh_owner_queue(connection: sqlite3.Connection, owner: str) -> None:
        row = connection.execute(
            f"SELECT {workflow_schema.AVAILABLE_AT} FROM workflow_runs WHERE owner_id=? "
            f"AND {workflow_schema.SCHEDULABLE} ORDER BY {workflow_schema.AVAILABLE_AT},workflow_id LIMIT 1", (owner,),
        ).fetchone()
        due = row[0] if row is not None else None
        connection.execute(
            "INSERT INTO workflow_coordinator_owners(owner_id,ready_at) VALUES(?,?) "
            "ON CONFLICT(owner_id) DO UPDATE SET ready_at=CASE WHEN excluded.ready_at IS NULL THEN NULL "
            "ELSE max(excluded.ready_at,last_served_at) END "
            "WHERE ready_at IS NOT CASE WHEN excluded.ready_at IS NULL THEN NULL "
            "ELSE max(excluded.ready_at,last_served_at) END", (owner, due),
        )

    @staticmethod
    def _record_owner_claim(connection: sqlite3.Connection, owner: str, now: float) -> None:
        row = connection.execute("SELECT service_sequence FROM workflow_coordinator_owners ORDER BY service_sequence DESC LIMIT 1").fetchone()
        sequence = _integer((row[0] if row is not None else 0) + 1)
        connection.execute(
            "UPDATE workflow_coordinator_owners SET last_served_at=max(last_served_at,?),service_sequence=? WHERE owner_id=?",
            (now, sequence, owner),
        )

    def _event(
        self, connection: sqlite3.Connection, owner: str, workflow: str,
        kind: str, payload: dict[str, object], now: float,
    ) -> None:
        raw = canonical_json(payload)
        if len(raw) > 16384:
            raise WorkflowRepositoryError("event_too_large")
        connection.execute(
            "UPDATE workflow_runs SET event_sequence=event_sequence+1 WHERE owner_id=? AND workflow_id=?",
            (owner, workflow),
        )
        connection.execute(
            "INSERT INTO workflow_events(owner_id,workspace_id,workflow_id,sequence,kind,payload,created_at) "
            "SELECT owner_id,workspace_id,workflow_id,event_sequence,?,?,? FROM workflow_runs WHERE owner_id=? AND workflow_id=?",
            (kind, raw, now, owner, workflow),
        )

    @staticmethod
    def _definition(value: WorkflowDefinition) -> WorkflowDefinition:
        try:
            if type(value) is not WorkflowDefinition or parse_definition(value.snapshot) != value:
                raise WorkflowRepositoryError("invalid_definition_snapshot")
        except WorkflowValidationError as exc:
            raise WorkflowRepositoryError("invalid_definition_snapshot") from exc
        return value

    def create_or_get(
        self, owner: str, workspace: str, workflow_id: str, *, client_token: str,
        definition: WorkflowDefinition, now: float, conversation_id: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> WorkflowRecord:
        owner, workspace, workflow_id, client_token = (_id(item) for item in (owner, workspace, workflow_id, client_token))
        now = _time(now)
        if conversation_id is not None:
            _id(conversation_id)
        definition = self._definition(definition)
        digest = hashlib.sha256(definition.snapshot).hexdigest()
        request_digest = canonical_digest({"workspace_id": workspace, "conversation_id": conversation_id, "definition_digest": digest})
        with self._connection(connection) as conn:
            metadata = workflow_schema.inspect_schema(conn)
            requires_reader_6 = any(step.contract.schema_version == 2 for step in definition.steps)
            previous = conn.execute(
                f"SELECT {_RUN_COLUMNS},request_digest FROM workflow_runs WHERE owner_id=? AND client_token=?",
                (owner, client_token),
            ).fetchone()
            if previous is not None:
                if previous["request_digest"] != request_digest:
                    raise WorkflowRepositoryError("idempotency_conflict")
                if requires_reader_6 and metadata["minimum_reader_version"] < 6:
                    raise WorkflowRepositoryError("invalid_contract_reader")
                return _run(previous)
            available = conn.execute(
                "SELECT 1 FROM workspaces WHERE owner_id=? AND workspace_id=? AND enabled=1", (owner, workspace),
            ).fetchone()
            if available is None:
                raise WorkflowRepositoryError("reference_forbidden")
            if conversation_id is not None and conn.execute(
                "SELECT 1 FROM conversations WHERE owner_id=? AND conversation_id=? AND workspace_id=?",
                (owner, conversation_id, workspace),
            ).fetchone() is None:
                raise WorkflowRepositoryError("reference_forbidden")
            if requires_reader_6:
                conn.execute("UPDATE workflow_schema_meta SET minimum_reader_version=max(minimum_reader_version,6) WHERE singleton=1")
            conn.execute(
                "INSERT INTO workflow_runs(workflow_id,owner_id,workspace_id,conversation_id,client_token,request_digest,"
                "definition_snapshot,definition_digest,acceptance_required,acceptance_state,next_poll_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (workflow_id, owner, workspace, conversation_id, client_token, request_digest, definition.snapshot, digest,
                 int(definition.acceptance_required), "pending" if definition.acceptance_required else "not_requested", now, now, now),
            )
            for position, step in enumerate(definition.steps):
                conn.execute(
                    "INSERT INTO workflow_steps(owner_id,workspace_id,workflow_id,step_key,generation,position,kind,"
                    "contract_snapshot,validation_state,created_at,updated_at) VALUES(?,?,?,?,1,?,?,?,?,?,?)",
                    (owner, workspace, workflow_id, step.step_key, position, step.kind, step.contract.snapshot,
                     "pending" if step.contract.validator_refs else "not_requested", now, now),
                )
                self._input_artifacts(conn, owner, workspace, workflow_id, step.step_key, 1, step.contract.input_refs)
            self._event(conn, owner, workflow_id, "workflow.created", {"definition_digest": digest}, now)
            self._refresh_owner_queue(conn, owner)
            return _run(conn.execute(f"SELECT {_RUN_COLUMNS} FROM workflow_runs WHERE owner_id=? AND workflow_id=?", (owner, workflow_id)).fetchone())

    @staticmethod
    def _input_artifacts(
        connection: sqlite3.Connection, owner: str, workspace: str, workflow: str,
        step: str, generation: int, references: tuple[NamedReference, ...],
    ) -> None:
        for named in references:
            reference = named.reference
            if type(reference) is CodeRevisionReference:
                reference = reference.source_bundle
            if type(reference) is not ArtifactReference:
                continue
            previous = connection.execute(
                "SELECT artifact_id,sha256,size FROM workflow_artifact_refs WHERE owner_id=? AND workflow_id=? "
                "AND step_key=? AND generation=? AND output_key=?",
                (owner, workflow, step, generation, "input:" + named.ref_key),
            ).fetchone()
            content = (reference.artifact_id, reference.sha256, reference.size)
            if previous is not None:
                if tuple(previous) != content:
                    raise WorkflowRepositoryError("artifact_reference_conflict")
                continue
            connection.execute(
                "INSERT INTO workflow_artifact_refs VALUES(?,?,?,?,?,?,?,?,?)",
                (owner, workspace, workflow, step, generation, "input:" + named.ref_key, *content),
            )

    def get(self, owner: str, workflow_id: str) -> WorkflowRecord | None:
        owner, workflow_id = _id(owner), _id(workflow_id)
        with self.transaction(write=False) as connection:
            row = connection.execute(f"SELECT {_RUN_COLUMNS} FROM workflow_runs WHERE owner_id=? AND workflow_id=?", (owner, workflow_id)).fetchone()
            return _run(row) if row is not None else None

    def list(self, owner: str, *, limit: int = 50, before: str | None = None) -> tuple[WorkflowRecord, ...]:
        _id(owner)
        _integer(limit, maximum=100)
        if before is not None:
            _id(before)
        cursor_filter = " AND workflow_id<?" if before is not None else ""
        parameters = (owner, before, limit) if before is not None else (owner, limit)
        with self.transaction(write=False) as connection:
            rows = connection.execute(
                f"SELECT {_RUN_COLUMNS} FROM workflow_runs WHERE owner_id=?{cursor_filter} ORDER BY workflow_id DESC LIMIT ?",
                parameters,
            ).fetchall()
            return tuple(_run(row) for row in rows)

    def current_steps(self, owner: str, workflow_id: str) -> tuple[WorkflowStepRecord, ...]:
        owner, workflow_id = _id(owner), _id(workflow_id)
        with self.transaction(write=False) as connection:
            rows = connection.execute(
                f"SELECT {_STEP_COLUMNS} FROM workflow_steps WHERE owner_id=? AND workflow_id=? AND is_current=1 ORDER BY position LIMIT 8",
                (owner, workflow_id),
            ).fetchall()
            return tuple(_step(row) for row in rows)

    def events(self, owner: str, workflow_id: str, *, after: int = 0, limit: int = 100) -> WorkflowEventPage:
        owner, workflow_id = _id(owner), _id(workflow_id)
        _integer(after, minimum=0)
        _integer(limit, maximum=100)
        with self.transaction(write=False) as connection:
            if connection.execute("SELECT 1 FROM workflow_runs WHERE owner_id=? AND workflow_id=?", (owner, workflow_id)).fetchone() is None:
                raise WorkflowRepositoryError("workflow_not_found")
            rows = connection.execute(
                "SELECT sequence,kind,payload,created_at FROM workflow_events WHERE owner_id=? AND workflow_id=? "
                "AND sequence>? ORDER BY sequence LIMIT ?", (owner, workflow_id, after, limit),
            ).fetchall()
            events = tuple(WorkflowEvent(row[0], row[1], bytes(row[2]), row[3]) for row in rows)
            return WorkflowEventPage(events, events[-1].sequence if events else after)

    def claim_due(
        self, owner: str, *, worker_id: str, now: float, limit: int = 32, lease_seconds: float = 30,
    ) -> tuple[CoordinatorClaim, ...]:
        owner, worker_id = _id(owner), _id(worker_id)
        now, lease_seconds = _time(now), _time(lease_seconds, positive=True)
        _integer(limit, maximum=32)
        expires = _time(now + lease_seconds)
        with self.transaction() as connection:
            meta = workflow_schema.inspect_schema(connection)
            if meta is None or meta["admission_mode"] == "frozen":
                return ()
            rows = connection.execute(
                "SELECT workflow_id,workspace_id,coordinator_epoch FROM workflow_runs WHERE owner_id=? "
                f"AND {workflow_schema.SCHEDULABLE} AND next_poll_at<=? "
                "AND (lease_expires_at IS NULL OR lease_expires_at<=?) ORDER BY next_poll_at,workflow_id LIMIT ?",
                (owner, now, now, limit),
            ).fetchall()
            claims: list[CoordinatorClaim] = []
            for row in rows:
                epoch = _integer(row["coordinator_epoch"] + 1)
                connection.execute(
                    "UPDATE workflow_runs SET coordinator_epoch=?,lease_owner=?,lease_expires_at=? WHERE owner_id=? AND workflow_id=?",
                    (epoch, worker_id, expires, owner, row["workflow_id"]),
                )
                claims.append(CoordinatorClaim(owner, row["workspace_id"], row["workflow_id"], worker_id, epoch, expires))
            if claims:
                self._record_owner_claim(connection, owner, now)
                self._refresh_owner_queue(connection, owner)
            return tuple(claims)

    def claim_fair_due(
        self, *, worker_id: str, now: float, limit: int = 32, lease_seconds: float = 60,
    ) -> tuple[CoordinatorClaim, ...]:
        _id(worker_id)
        now, lease_seconds = _time(now), _time(lease_seconds, positive=True)
        _integer(limit, maximum=32)
        expires = _time(now + lease_seconds)
        with self.transaction() as connection:
            meta = workflow_schema.inspect_schema(connection)
            if meta is None or meta["admission_mode"] == "frozen":
                return ()
            claims: list[CoordinatorClaim] = []
            for _ in range(limit):
                owner = connection.execute(
                    "SELECT owner_id FROM workflow_coordinator_owners WHERE ready_at<=? "
                    "ORDER BY ready_at,service_sequence,owner_id LIMIT 1", (now,),
                ).fetchone()
                if owner is None:
                    break
                row = connection.execute(
                    "SELECT workflow_id,workspace_id,coordinator_epoch FROM workflow_runs WHERE owner_id=? "
                    f"AND {workflow_schema.SCHEDULABLE} AND {workflow_schema.AVAILABLE_AT}<=? "
                    f"ORDER BY {workflow_schema.AVAILABLE_AT},workflow_id LIMIT 1", (owner[0], now),
                ).fetchone()
                if row is None:
                    self._refresh_owner_queue(connection, owner[0])
                    continue
                epoch = _integer(row["coordinator_epoch"] + 1)
                connection.execute(
                    "UPDATE workflow_runs SET coordinator_epoch=?,lease_owner=?,lease_expires_at=? WHERE owner_id=? AND workflow_id=?",
                    (epoch, worker_id, expires, owner[0], row["workflow_id"]),
                )
                self._record_owner_claim(connection, owner[0], now)
                self._refresh_owner_queue(connection, owner[0])
                claims.append(CoordinatorClaim(owner[0], row["workspace_id"], row["workflow_id"], worker_id, epoch, expires))
            return tuple(claims)

    @staticmethod
    def _fenced(connection: sqlite3.Connection, claim: CoordinatorClaim, now: float, expected_revision: int) -> sqlite3.Row:
        if type(claim) is not CoordinatorClaim:
            raise WorkflowRepositoryError("invalid_claim")
        _integer(expected_revision)
        mode = connection.execute("SELECT admission_mode FROM workflow_schema_meta WHERE singleton=1").fetchone()
        if mode is None or mode[0] == "frozen":
            raise WorkflowRepositoryError("admission_frozen")
        row = connection.execute(
            f"SELECT {_RUN_COLUMNS},definition_digest FROM workflow_runs WHERE owner_id=? AND workspace_id=? AND workflow_id=? "
            "AND lease_owner=? AND coordinator_epoch=? AND lease_expires_at>?",
            (claim.owner_id, claim.workspace_id, claim.workflow_id, claim.worker_id, claim.epoch, now),
        ).fetchone()
        if row is None:
            raise WorkflowRepositoryError("lease_mismatch")
        if row["revision"] != expected_revision:
            raise WorkflowRepositoryError("revision_conflict")
        return row

    def freeze_inputs(
        self, claim: CoordinatorClaim, *, step_key: str, generation: int,
        resolved: ResolvedContract, expected_revision: int, now: float,
        connection: sqlite3.Connection | None = None,
        input_target: str | None = None, inputs_pending: bool = False,
    ) -> WorkflowRecord:
        _id(step_key)
        _integer(generation)
        now = _time(now)
        with self._connection(connection) as conn:
            workflow = self._fenced(conn, claim, now, expected_revision)
            self._creation_open(conn, workflow)
            step = conn.execute(
                "SELECT state,kind,position,contract_snapshot FROM workflow_steps WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=? AND is_current=1",
                (claim.owner_id, claim.workflow_id, step_key, generation),
            ).fetchone()
            if step is None or step["state"] not in ("pending", "waiting_node", "waiting_approval", "paused"):
                raise WorkflowRepositoryError("step_not_pending")
            if step["kind"] == "external_agent" and (input_target is not None or inputs_pending):
                raise WorkflowRepositoryError("invalid_input_target")
            if conn.execute(
                "SELECT 1 FROM workflow_dispatches WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=?",
                (claim.owner_id, claim.workflow_id, step_key, generation),
            ).fetchone() is not None:
                raise WorkflowRepositoryError("step_already_dispatched")
            blockers = conn.execute(
                "SELECT 1 FROM workflow_steps WHERE owner_id=? AND workflow_id=? AND is_current=1 AND position<? "
                "AND (state!='succeeded' OR validation_state NOT IN ('passed','not_requested')) LIMIT 1",
                (claim.owner_id, claim.workflow_id, step["position"]),
            ).fetchone()
            if blockers is not None:
                raise WorkflowRepositoryError("predecessor_not_ready")
            self._validate_resolved(workflow, claim, step_key, resolved, contract_snapshot=bytes(step["contract_snapshot"]))
            if type(inputs_pending) is not bool or (inputs_pending and (not input_target or not isinstance(input_target, str))):
                raise WorkflowRepositoryError("invalid_input_target")
            initial_state = "waiting_node" if inputs_pending else "ready"
            reason = "inputs_receiving" if inputs_pending else None
            if input_target is not None and not inputs_pending:
                from .artifact_input_repository import node_input_receipts_ready
                if not node_input_receipts_ready(conn, owner=claim.owner_id, workflow=claim.workflow_id,
                        generation=generation, resolved=resolved, target=input_target, public_key=self.artifact_input_public_key):
                    raise WorkflowRepositoryError("input_receipt_required")
            conn.execute(
                "UPDATE workflow_steps SET state=?,reason_code=?,target_key=coalesce(?,target_key),resolved_snapshot=?,resolved_digest=?,coordinator_epoch=?,updated_at=?,wait_deadline=coalesce(wait_deadline,?) "
                "WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=?",
                (initial_state, reason, input_target, resolved.snapshot, resolved.digest, claim.epoch, now, _time(now + resolved.contract.budget.max_wait_seconds),
                 claim.owner_id, claim.workflow_id, step_key, generation),
            )
            self._input_artifacts(conn, claim.owner_id, claim.workspace_id, claim.workflow_id, step_key, generation, resolved.contract.input_refs)
            conn.execute("UPDATE workflow_runs SET revision=revision+1,updated_at=? WHERE owner_id=? AND workflow_id=?", (now, claim.owner_id, claim.workflow_id))
            self._event(conn, claim.owner_id, claim.workflow_id,
                "step.inputs_receiving" if inputs_pending else "step.ready",
                {"step_key": step_key, "generation": generation, "resolved_digest": resolved.digest,
                 **({"target_key": input_target} if inputs_pending else {})}, now)
            return _run(conn.execute(f"SELECT {_RUN_COLUMNS} FROM workflow_runs WHERE owner_id=? AND workflow_id=?", (claim.owner_id, claim.workflow_id)).fetchone())

    @staticmethod
    def _validate_resolved(workflow: sqlite3.Row, claim: CoordinatorClaim, step_key: str, resolved: ResolvedContract, *, contract_snapshot: bytes) -> None:
        if type(resolved) is not ResolvedContract or (resolved.owner_id, resolved.workspace_id, resolved.step_key) != (claim.owner_id, claim.workspace_id, step_key):
            raise WorkflowRepositoryError("resolved_scope_conflict")
        definition = parse_definition(bytes(workflow["definition_snapshot"]))
        contract = parse_contract(contract_snapshot)
        sources = {item.ref_key: item.reference for item in contract.input_refs if type(item.reference) is PriorOutputReference}
        payload = decode_snapshot(resolved.snapshot)
        adapter = payload.get("adapter_ref")
        if type(adapter) is not dict or set(adapter) != {"id", "revision"}:
            raise WorkflowRepositoryError("invalid_resolved_snapshot")
        bindings = tuple(
            InputBinding(item.ref_key, claim.owner_id, claim.workspace_id, item.reference, sources.get(item.ref_key))
            for item in resolved.contract.input_refs + resolved.contract.context_refs
        )
        expected = resolve_contract(definition, AuthorizedInputs(claim.owner_id, claim.workspace_id, step_key, CatalogRef(adapter["id"], adapter["revision"]), bindings),
                                    contract_override=contract)
        if expected != resolved:
            raise WorkflowRepositoryError("invalid_resolved_snapshot")

    def admit_step(
        self, claim: CoordinatorClaim, *, step_key: str, generation: int, expected_revision: int,
        target_key: str, capacity: int, reserve_tokens: int, reserve_requests: int, now: float,
        connection: sqlite3.Connection | None = None,
        external_target: RegisteredExternalTarget | None = None,
    ) -> DispatchIntent:
        _id(step_key)
        _id(target_key)
        _integer(generation)
        _integer(capacity, maximum=1024)
        _integer(reserve_tokens, minimum=0)
        _integer(reserve_requests, minimum=0)
        now = _time(now)
        with self._connection(connection) as conn:
            workflow = self._fenced(conn, claim, now, expected_revision)
            mode = conn.execute("SELECT admission_mode FROM workflow_schema_meta WHERE singleton=1").fetchone()[0]
            if mode != "open" or workflow["cancel_requested"] or workflow["pause_requested"]:
                raise WorkflowRepositoryError("admission_closed")
            step = conn.execute(
                "SELECT state,kind,resolved_snapshot,resolved_digest,contract_snapshot,target_key FROM workflow_steps WHERE owner_id=? AND workflow_id=? "
                "AND step_key=? AND generation=? AND is_current=1",
                (claim.owner_id, claim.workflow_id, step_key, generation),
            ).fetchone()
            if step is None or step["state"] not in ("ready", "waiting_node") or step["resolved_snapshot"] is None:
                raise WorkflowRepositoryError("step_not_ready")
            if (step["kind"] == "external_agent") != (external_target is not None):
                raise WorkflowRepositoryError("external_registration_required")
            from .artifact_input_repository import node_input_receipts_ready
            from hub.domain.workflow_contract import parse_resolved
            if step["target_key"] is not None and step["target_key"] != target_key:
                raise WorkflowRepositoryError("input_target_conflict")
            resolved = parse_resolved(bytes(step["resolved_snapshot"]))
            requires_receipt = step["kind"] != "external_agent" and (step["target_key"] is not None or (
                self.artifact_input_public_key is not None and self.requires_node_inputs(resolved, kind=step["kind"], target_key=target_key)))
            if requires_receipt and not node_input_receipts_ready(conn, owner=claim.owner_id, workflow=claim.workflow_id,
                    generation=generation, resolved=resolved, target=target_key,
                    public_key=self.artifact_input_public_key):
                raise WorkflowRepositoryError("input_receipt_required")
            contract = decode_snapshot(bytes(step["contract_snapshot"]))
            budget = contract["budget"]
            if generation > budget["max_generations"]:
                raise WorkflowRepositoryError("budget_exhausted")
            previous = conn.execute(
                "SELECT coalesce(sum(CASE WHEN settlement='settled' THEN charged_tokens ELSE max(reserved_tokens,coalesce(charged_tokens,0)) END),0),"
                "coalesce(sum(CASE WHEN settlement='settled' THEN charged_requests ELSE max(reserved_requests,coalesce(charged_requests,0)) END),0) "
                "FROM workflow_budget_reservations WHERE owner_id=? AND workflow_id=? AND step_key=?",
                (claim.owner_id, claim.workflow_id, step_key),
            ).fetchone()
            if previous[0] + reserve_tokens > budget["max_tokens"] or previous[1] + reserve_requests > budget["max_model_requests"]:
                raise WorkflowRepositoryError("budget_exhausted")
            held = conn.execute("SELECT count(*) FROM capability_reservations WHERE target_key=? AND state IN ('held','isolated')", (target_key,)).fetchone()[0]
            if held >= capacity:
                raise WorkflowRepositoryError("capacity_exhausted")
            token = dispatch_token(claim.owner_id, claim.workflow_id, step_key, generation)
            params = (claim.owner_id, claim.workspace_id, claim.workflow_id, step_key, generation)
            conn.execute(
                "INSERT INTO workflow_dispatches(owner_id,workspace_id,workflow_id,step_key,generation,token,payload_hash,target_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (*params, token, step["resolved_digest"], target_key, now, now),
            )
            if external_target is not None:
                from .workflow_external_repository import ExternalWorkflowRepository

                ExternalWorkflowRepository.pin(conn, token, external_target, now=now)
            conn.execute(
                "INSERT INTO workflow_budget_reservations(owner_id,workspace_id,workflow_id,step_key,generation,reserved_tokens,reserved_requests,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (*params, reserve_tokens, reserve_requests, now, now),
            )
            conn.execute(
                "INSERT INTO capability_reservations(owner_id,workspace_id,workflow_id,step_key,generation,target_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (*params, target_key, now, now),
            )
            conn.execute(
                "UPDATE workflow_steps SET state='dispatching',target_key=?,coordinator_epoch=?,active_deadline=?,wait_deadline=?,updated_at=? "
                "WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=?",
                (target_key, claim.epoch, _time(now + budget["max_active_seconds"]), _time(now + budget["max_wait_seconds"]), now,
                 claim.owner_id, claim.workflow_id, step_key, generation),
            )
            conn.execute("UPDATE workflow_runs SET execution_state='running',revision=revision+1,updated_at=? WHERE owner_id=? AND workflow_id=?", (now, claim.owner_id, claim.workflow_id))
            self._event(conn, claim.owner_id, claim.workflow_id, "step.admitted", {"step_key": step_key, "generation": generation, "dispatch_token": token}, now)
            return DispatchIntent(token, step["resolved_digest"], target_key, bytes(step["resolved_snapshot"]))

    def release_claim(self, claim: CoordinatorClaim, *, now: float, next_poll_at: float) -> bool:
        return self.release_claims((claim,), now=now, next_poll_at=next_poll_at)[0]

    def release_claims(self, claims: tuple[CoordinatorClaim, ...], *, now: float, next_poll_at: float) -> tuple[bool, ...]:
        """Release one bounded tick atomically; stale claims cannot change new leases."""
        if type(claims) is not tuple or len(claims) > 32 or any(type(claim) is not CoordinatorClaim for claim in claims):
            raise WorkflowRepositoryError("invalid_claims")
        now, next_poll_at = _time(now), _time(next_poll_at)
        if next_poll_at < now:
            raise WorkflowRepositoryError("invalid_poll_time")
        if not claims:
            return ()
        with self.transaction() as connection:
            released = []
            owners = set()
            for claim in claims:
                changed = connection.execute(
                    "UPDATE workflow_runs SET lease_owner=NULL,lease_expires_at=NULL,next_poll_at=? WHERE owner_id=? AND workspace_id=? AND workflow_id=? "
                    "AND lease_owner=? AND coordinator_epoch=? AND lease_expires_at>?",
                    (next_poll_at, claim.owner_id, claim.workspace_id, claim.workflow_id, claim.worker_id, claim.epoch, now),
                )
                released.append(changed.rowcount == 1)
                if changed.rowcount:
                    owners.add(claim.owner_id)
            for owner in sorted(owners):
                self._refresh_owner_queue(connection, owner)
            return tuple(released)

    @staticmethod
    def _dispatch_record(
        connection: sqlite3.Connection, owner: str, workflow_id: str, step_key: str, generation: int,
        *, recovered: bool = False,
    ) -> DispatchRecord:
        row = connection.execute(
            "SELECT d.*,s.resolved_snapshot,s.resolved_digest,s.state AS step_state,r.revision "
            "FROM workflow_dispatches d JOIN workflow_steps s USING(owner_id,workspace_id,workflow_id,step_key,generation) "
            "JOIN workflow_runs r USING(owner_id,workspace_id,workflow_id) "
            "WHERE d.owner_id=? AND d.workflow_id=? AND d.step_key=? AND d.generation=?",
            (owner, workflow_id, step_key, generation),
        ).fetchone()
        if row is None:
            raise WorkflowRepositoryError("dispatch_not_found")
        try:
            resolved = parse_resolved(bytes(row["resolved_snapshot"]))
            if (resolved.owner_id, resolved.workspace_id, resolved.step_key, resolved.digest, resolved.snapshot) != (
                owner, row["workspace_id"], step_key, row["payload_hash"], bytes(row["resolved_snapshot"]),
            ) or row["resolved_digest"] != resolved.digest or row["token"] != dispatch_token(owner, workflow_id, step_key, generation):
                raise WorkflowRepositoryError("invalid_dispatch_snapshot")
            binding = None
            if row["binding_kind"] is not None or row["binding_id"] is not None:
                binding = ExecutionBinding(owner, row["workspace_id"], row["target_key"], row["token"], row["payload_hash"],
                                           row["binding_kind"], row["binding_id"], row["binding_run_id"])
            if row["stage"] == "bound" and binding is None:
                raise WorkflowRepositoryError("invalid_dispatch_binding")
        except (WorkflowValidationError, TypeError) as exc:
            raise WorkflowRepositoryError("invalid_dispatch_snapshot") from exc
        return DispatchRecord(workflow_id, step_key, generation, row["stage"], row["token"], row["payload_hash"],
                              row["target_key"], resolved, binding, row["revision"], row["step_state"], recovered)

    def get_dispatch(self, owner: str, workflow_id: str, step_key: str, generation: int) -> DispatchRecord:
        _id(owner)
        _id(workflow_id)
        _id(step_key)
        _integer(generation)
        with self.transaction(write=False) as connection:
            return self._dispatch_record(connection, owner, workflow_id, step_key, generation)

    @staticmethod
    def _creation_open(connection: sqlite3.Connection, workflow: sqlite3.Row) -> None:
        mode = connection.execute("SELECT admission_mode FROM workflow_schema_meta WHERE singleton=1").fetchone()
        if mode is None or mode[0] != "open" or workflow["cancel_requested"] or workflow["pause_requested"]:
            raise WorkflowRepositoryError("admission_closed")

    @staticmethod
    def _current_dispatch(connection: sqlite3.Connection, claim: CoordinatorClaim, record: DispatchRecord) -> None:
        if type(record) is not DispatchRecord or record.workflow_id != claim.workflow_id or (
            record.resolved.owner_id, record.resolved.workspace_id,
        ) != (claim.owner_id, claim.workspace_id):
            raise WorkflowRepositoryError("dispatch_conflict")
        current = connection.execute(
            "SELECT s.is_current,s.resolved_snapshot,s.resolved_digest,d.token,d.payload_hash,d.target_key FROM workflow_steps s "
            "JOIN workflow_dispatches d USING(owner_id,workspace_id,workflow_id,step_key,generation) "
            "WHERE s.owner_id=? AND s.workspace_id=? AND s.workflow_id=? AND s.step_key=? AND s.generation=?",
            (claim.owner_id, claim.workspace_id, claim.workflow_id, record.step_key, record.generation),
        ).fetchone()
        if current is None or current[0] != 1:
            raise WorkflowRepositoryError("generation_conflict")
        if (current["token"], current["payload_hash"], current["target_key"], bytes(current["resolved_snapshot"]), current["resolved_digest"]) != (
            record.token, record.payload_hash, record.target_key, record.resolved.snapshot, record.resolved.digest,
        ):
            raise WorkflowRepositoryError("dispatch_conflict")

    def begin_dispatch(
        self, claim: CoordinatorClaim, *, step_key: str, generation: int, expected_revision: int, now: float,
    ) -> DispatchRecord:
        _id(step_key)
        _integer(generation)
        now = _time(now)
        # Existing executions only need inspection. WAL readers must not wait
        # for an unrelated writer just to recover an already-bound dispatch.
        with self.transaction(write=False) as connection:
            self._fenced(connection, claim, now, expected_revision)
            record = self._dispatch_record(connection, claim.owner_id, claim.workflow_id, step_key, generation)
            self._current_dispatch(connection, claim, record)
            if record.stage == "unknown" or record.step_state == "unknown":
                raise WorkflowRepositoryError("reconciliation_required")
            if record.binding is not None:
                return record
        # A fresh write transaction must recheck every fence; never promote the
        # read snapshot or dispatch from a record changed after that read.
        with self.transaction() as connection:
            workflow = self._fenced(connection, claim, now, expected_revision)
            record = self._dispatch_record(connection, claim.owner_id, claim.workflow_id, step_key, generation)
            self._current_dispatch(connection, claim, record)
            if record.stage == "unknown" or record.step_state == "unknown":
                raise WorkflowRepositoryError("reconciliation_required")
            if record.binding is not None:
                return record
            if record.step_state != "dispatching":
                raise WorkflowRepositoryError("dispatch_not_pending")
            if record.stage == "sending":
                return self._dispatch_record(connection, claim.owner_id, claim.workflow_id, step_key, generation, recovered=True)
            self._creation_open(connection, workflow)
            connection.execute(
                "UPDATE workflow_dispatches SET stage='sending',updated_at=? WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=?",
                (now, claim.owner_id, claim.workflow_id, step_key, generation),
            )
            connection.execute("UPDATE workflow_runs SET revision=revision+1,updated_at=? WHERE owner_id=? AND workflow_id=?",
                               (now, claim.owner_id, claim.workflow_id))
            self._event(connection, claim.owner_id, claim.workflow_id, "step.dispatch_started",
                        {"step_key": step_key, "generation": generation}, now)
            return self._dispatch_record(connection, claim.owner_id, claim.workflow_id, step_key, generation)

    def authorize_dispatch(self, claim: CoordinatorClaim, record: DispatchRecord, *, now: float) -> None:
        with self.transaction(write=False) as connection:
            workflow = self._fenced(connection, claim, _time(now), record.revision)
            self._current_dispatch(connection, claim, record)
            self._creation_open(connection, workflow)

    def bind_dispatch(
        self, claim: CoordinatorClaim, record: DispatchRecord, binding: ExecutionBinding, *, now: float,
    ) -> DispatchRecord:
        if type(binding) is not ExecutionBinding or (
            binding.owner_id, binding.workspace_id, binding.target_key, binding.token, binding.payload_hash,
        ) != (claim.owner_id, claim.workspace_id, record.target_key, record.token, record.payload_hash):
            raise WorkflowRepositoryError("dispatch_conflict")
        now = _time(now)
        with self.transaction() as connection:
            self._fenced(connection, claim, now, record.revision)
            self._current_dispatch(connection, claim, record)
            current = self._dispatch_record(connection, claim.owner_id, claim.workflow_id, record.step_key, record.generation)
            if current.binding is not None:
                if current.binding != binding:
                    raise WorkflowRepositoryError("dispatch_conflict")
                return current
            if current.stage != "sending" or current.step_state not in ("dispatching", "cancelling"):
                raise WorkflowRepositoryError("dispatch_not_pending")
            if binding.run_id is not None and connection.execute(
                "SELECT 1 FROM runs r JOIN conversations c USING(conversation_id) "
                "WHERE r.owner_id=? AND c.owner_id=? AND c.workspace_id=? AND r.run_id=?",
                (claim.owner_id, claim.owner_id, claim.workspace_id, binding.run_id),
            ).fetchone() is None:
                raise WorkflowRepositoryError("reference_forbidden")
            row = connection.execute("SELECT binding_run_id FROM workflow_dispatches WHERE token=?", (record.token,)).fetchone()
            if row[0] is not None and row[0] != binding.run_id:
                raise WorkflowRepositoryError("dispatch_conflict")
            connection.execute(
                "UPDATE workflow_dispatches SET stage='bound',binding_kind=?,binding_id=?,binding_run_id=?,updated_at=? WHERE token=?",
                (binding.kind, binding.execution_id, binding.run_id, now, record.token),
            )
            if binding.run_id is not None:
                connection.execute(
                    "UPDATE workflow_budget_reservations SET child_request_id=? WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=?",
                    (binding.run_id, claim.owner_id, claim.workflow_id, record.step_key, record.generation),
                )
                connection.execute("INSERT OR IGNORE INTO workflow_budget_checks(owner_id,workflow_id,step_key,generation) VALUES(?,?,?,?)",
                                   (claim.owner_id, claim.workflow_id, record.step_key, record.generation))
            connection.execute(
                "UPDATE workflow_steps SET state=CASE WHEN state='cancelling' THEN state ELSE 'running' END,coordinator_epoch=?,updated_at=? "
                "WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=?",
                (claim.epoch, now, claim.owner_id, claim.workflow_id, record.step_key, record.generation),
            )
            connection.execute("UPDATE workflow_runs SET revision=revision+1,updated_at=? WHERE owner_id=? AND workflow_id=?",
                               (now, claim.owner_id, claim.workflow_id))
            self._event(connection, claim.owner_id, claim.workflow_id, "step.bound", {
                "step_key": record.step_key, "generation": record.generation,
                "binding_kind": binding.kind, "binding_id": binding.execution_id,
            }, now)
            return self._dispatch_record(connection, claim.owner_id, claim.workflow_id, record.step_key, record.generation)

    def fail_dispatch(
        self, claim: CoordinatorClaim, record: DispatchRecord, *, reason_code: str, uncertain: bool, now: float,
    ) -> None:
        _id(reason_code)
        if type(uncertain) is not bool:
            raise WorkflowRepositoryError("invalid_boolean")
        now = _time(now)
        state = "unknown" if uncertain else "failed"
        with self.transaction() as connection:
            self._fenced(connection, claim, now, record.revision)
            self._current_dispatch(connection, claim, record)
            current = self._dispatch_record(connection, claim.owner_id, claim.workflow_id, record.step_key, record.generation)
            if current.step_state == "unknown" or current.stage == "unknown":
                raise WorkflowRepositoryError("reconciliation_required")
            if current.step_state in TERMINAL_STATES:
                raise WorkflowRepositoryError("terminal_workflow")
            observation = canonical_json({"state": state, "reason_code": reason_code, "not_started": not uncertain})
            connection.execute(
                "UPDATE workflow_dispatches SET stage=CASE WHEN ? THEN 'unknown' ELSE stage END,observation_snapshot=?,updated_at=? WHERE token=?",
                (int(uncertain), observation, now, record.token),
            )
            connection.execute(
                "UPDATE workflow_steps SET state=?,reason_code=?,updated_at=? WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=?",
                (state, reason_code, now, claim.owner_id, claim.workflow_id, record.step_key, record.generation),
            )
            connection.execute("UPDATE workflow_runs SET execution_state=?,reason_code=?,revision=revision+1,updated_at=? WHERE owner_id=? AND workflow_id=?",
                               (state, reason_code, now, claim.owner_id, claim.workflow_id))
            self._event(connection, claim.owner_id, claim.workflow_id, "step.dispatch_unknown" if uncertain else "step.dispatch_rejected",
                        {"step_key": record.step_key, "generation": record.generation, "reason_code": reason_code}, now)
            self._refresh_owner_queue(connection, claim.owner_id)

    @staticmethod
    def _snapshots(connection: sqlite3.Connection, workflow_ids: tuple[str, ...]) -> tuple[WorkflowSnapshot, ...]:
        if not workflow_ids:
            return ()
        _integer(len(workflow_ids), maximum=32)
        placeholders = ",".join("?" for _ in workflow_ids)
        runs = connection.execute(
            f"SELECT {_RUN_COLUMNS} FROM workflow_runs WHERE workflow_id IN ({placeholders}) LIMIT 32", workflow_ids,
        ).fetchall()
        columns = ",".join("s." + column for column in _STEP_COLUMNS.split(","))
        steps = connection.execute(
            f"SELECT s.owner_id,s.workflow_id,{columns},d.stage AS dispatch_stage FROM workflow_steps s "
            "LEFT JOIN workflow_dispatches d USING(owner_id,workspace_id,workflow_id,step_key,generation) "
            f"WHERE s.workflow_id IN ({placeholders}) AND s.is_current=1 ORDER BY s.workflow_id,s.position LIMIT ?",
            (*workflow_ids, len(workflow_ids) * 8 + 1),
        ).fetchall()
        evidence = connection.execute(
            "SELECT e.workflow_id,e.step_key,e.generation,e.evidence_key,e.validator_ref,e.subject_digest,e.result "
            "FROM workflow_evidence e JOIN workflow_steps s USING(owner_id,workspace_id,workflow_id,step_key,generation) "
            f"WHERE e.workflow_id IN ({placeholders}) AND s.is_current=1 AND e.is_current=1 LIMIT ?",
            (*workflow_ids, len(workflow_ids) * 256 + 1),
        ).fetchall()
        outputs = connection.execute(
            "SELECT a.workflow_id,a.step_key,a.generation,a.output_key FROM workflow_artifact_refs a "
            "JOIN workflow_steps s USING(owner_id,workspace_id,workflow_id,step_key,generation) "
            f"WHERE a.workflow_id IN ({placeholders}) AND s.is_current=1 AND a.output_key LIKE 'output:%' LIMIT ?",
            (*workflow_ids, len(workflow_ids) * 128 + 1),
        ).fetchall()
        by_evidence: dict[tuple[str, str, int], list[ValidationEvidence]] = {}
        for row in evidence:
            key = (row["workflow_id"], row["step_key"], row["generation"])
            by_evidence.setdefault(key, []).append(ValidationEvidence(row["evidence_key"], row["validator_ref"], row["subject_digest"], row["result"]))
            if len(by_evidence[key]) > 32:
                raise WorkflowRepositoryError("evidence_limit")
        by_output: dict[tuple[str, str, int], list[str]] = {}
        for row in outputs:
            key = (row["workflow_id"], row["step_key"], row["generation"])
            by_output.setdefault(key, []).append(row["output_key"][7:])
            if len(by_output[key]) > 16:
                raise WorkflowRepositoryError("output_limit")
        by_workflow: dict[str, list[WorkflowStepRecord]] = {row["workflow_id"]: [] for row in runs}
        for row in steps:
            key = (row["workflow_id"], row["step_key"], row["generation"])
            by_workflow[row["workflow_id"]].append(replace(
                _step(row), dispatch_stage=row["dispatch_stage"], evidence=tuple(by_evidence.get(key, ())),
                output_keys=tuple(by_output.get(key, ())),
            ))
            if len(by_workflow[row["workflow_id"]]) > 8:
                raise WorkflowRepositoryError("step_limit")
        mode = connection.execute("SELECT admission_mode FROM workflow_schema_meta WHERE singleton=1").fetchone()[0]
        return tuple(WorkflowSnapshot(_run(row), tuple(by_workflow[row["workflow_id"]]), mode) for row in runs)

    def snapshots(self, claims: tuple[CoordinatorClaim, ...], *, now: float) -> tuple[WorkflowSnapshot, ...]:
        if type(claims) is not tuple or len(claims) > 32 or any(type(claim) is not CoordinatorClaim for claim in claims):
            raise WorkflowRepositoryError("invalid_claim")
        if not claims:
            return ()
        now = _time(now)
        by_id = {claim.workflow_id: claim for claim in claims}
        if len(by_id) != len(claims):
            raise WorkflowRepositoryError("invalid_claim")
        placeholders = ",".join("?" for _ in claims)
        with self.transaction(write=False) as connection:
            rows = connection.execute(
                "SELECT owner_id,workspace_id,workflow_id,lease_owner,coordinator_epoch,lease_expires_at FROM workflow_runs "
                f"WHERE workflow_id IN ({placeholders}) LIMIT 32", tuple(by_id),
            ).fetchall()
            for row in rows:
                claim = by_id[row["workflow_id"]]
                if (row["owner_id"], row["workspace_id"], row["lease_owner"], row["coordinator_epoch"]) != (
                    claim.owner_id, claim.workspace_id, claim.worker_id, claim.epoch,
                ) or row["lease_expires_at"] is None or row["lease_expires_at"] <= now:
                    raise WorkflowRepositoryError("lease_mismatch")
            if len(rows) != len(claims):
                raise WorkflowRepositoryError("lease_mismatch")
            try:
                return self._snapshots(connection, tuple(by_id))
            except WorkflowValidationError as exc:
                raise WorkflowRepositoryError("invalid_workflow_snapshot") from exc

    def _project(self, connection: sqlite3.Connection, claim: CoordinatorClaim, now: float, *, touch: bool = False) -> WorkflowRecord:
        snapshot = self._snapshots(connection, (claim.workflow_id,))[0]
        try:
            projection = project_workflow(parse_definition(snapshot.workflow.definition_snapshot), snapshot)
        except WorkflowValidationError as exc:
            raise WorkflowRepositoryError("invalid_workflow_snapshot") from exc
        workflow = snapshot.workflow
        if workflow.execution_state in TERMINAL_STATES and projection.execution_state != workflow.execution_state:
            raise WorkflowRepositoryError("terminal_workflow")
        changed = (workflow.execution_state, workflow.validation_state, workflow.reason_code) != (
            projection.execution_state, projection.validation_state, projection.reason_code,
        )
        for step, verdict in zip(snapshot.steps, projection.step_validations):
            if step.validation_state == verdict:
                continue
            connection.execute(
                "UPDATE workflow_steps SET validation_state=? WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=?",
                (verdict, claim.owner_id, claim.workflow_id, step.step_key, step.generation),
            )
            changed = True
        if changed or touch:
            connection.execute(
                "UPDATE workflow_runs SET execution_state=?,validation_state=?,reason_code=?,revision=revision+1,updated_at=? "
                "WHERE owner_id=? AND workflow_id=?",
                (projection.execution_state, projection.validation_state, projection.reason_code, now, claim.owner_id, claim.workflow_id),
            )
        if changed:
            self._event(connection, claim.owner_id, claim.workflow_id, "workflow.projected", {
                "execution_state": projection.execution_state, "validation_state": projection.validation_state,
                "reason_code": projection.reason_code,
            }, now)
        self._refresh_owner_queue(connection, claim.owner_id)
        return _run(connection.execute(f"SELECT {_RUN_COLUMNS} FROM workflow_runs WHERE owner_id=? AND workflow_id=?",
                                       (claim.owner_id, claim.workflow_id)).fetchone())

    def refresh_projection(self, claim: CoordinatorClaim, *, expected_revision: int, now: float) -> WorkflowRecord:
        now = _time(now)
        with self.transaction() as connection:
            self._fenced(connection, claim, now, expected_revision)
            return self._project(connection, claim, now)

    def apply_observation(
        self, claim: CoordinatorClaim, record: DispatchRecord, observation: ExecutionObservation, *, now: float,
        reconciled: bool = False,
    ) -> WorkflowRecord:
        if type(reconciled) is not bool:
            raise WorkflowRepositoryError("invalid_boolean")
        if type(observation) is not ExecutionObservation or (not reconciled and observation.binding != record.binding):
            raise WorkflowRepositoryError("dispatch_conflict")
        now = _time(now)
        with self.transaction() as connection:
            self._fenced(connection, claim, now, record.revision)
            self._current_dispatch(connection, claim, record)
            current = self._dispatch_record(connection, claim.owner_id, claim.workflow_id, record.step_key, record.generation)
            binding = observation.binding
            if (binding.owner_id, binding.workspace_id, binding.token, binding.payload_hash, binding.target_key) != (
                claim.owner_id, claim.workspace_id, current.token, current.payload_hash, current.target_key,
            ) or (current.binding is not None and current.binding != binding):
                raise WorkflowRepositoryError("dispatch_conflict")
            if current.stage not in (("bound", "unknown") if reconciled else ("bound",)):
                raise WorkflowRepositoryError("dispatch_conflict")
            if reconciled:
                self._reconciliation(connection, claim, record.step_key, record.generation)
                if binding.run_id is not None and connection.execute(
                    "SELECT 1 FROM runs r JOIN conversations c USING(owner_id,conversation_id) "
                    "WHERE r.owner_id=? AND c.workspace_id=? AND r.run_id=?",
                    (claim.owner_id, claim.workspace_id, binding.run_id),
                ).fetchone() is None:
                    raise WorkflowRepositoryError("reference_forbidden")
            try:
                origin = current.step_state
                if reconciled:
                    transition_step(origin, observation.state, reconciled=True)
                elif observation.state == "cancelled" and origin != "cancelled":
                    # Only this authoritative bound observation confirms stopping.
                    origin = transition_step(origin, "cancelling")
                if not reconciled:
                    transition_step(origin, observation.state)
            except WorkflowValidationError as exc:
                raise WorkflowRepositoryError("invalid_observation_transition") from exc
            payload = {"state": observation.state, "reason_code": observation.reason_code,
                       "task_id": observation.task_id, "attempt_id": observation.attempt_id}
            if observation.evidence_source is not None:
                payload["evidence_source"] = observation.evidence_source
            private_payload = payload
            step_kind = connection.execute(
                "SELECT kind FROM workflow_steps WHERE owner_id=? AND workspace_id=? AND workflow_id=? AND step_key=? AND generation=?",
                (claim.owner_id, claim.workspace_id, claim.workflow_id, record.step_key, record.generation),
            ).fetchone()
            if step_kind is not None and step_kind["kind"] == "external_agent":
                from .workflow_external_repository import ExternalWorkflowRepository

                ExternalWorkflowRepository.commit_outputs(connection, current, observation, now=now, reconciled=reconciled)
            elif observation.external_outputs is not None:
                raise WorkflowRepositoryError("external_output_forbidden")
            if observation.external_outputs is not None:
                private_payload = {**payload, "external_outputs": observation.external_outputs.payload()}
            raw = canonical_json(private_payload)
            if len(raw) > 16384:
                raise WorkflowRepositoryError("external_output_too_large")
            previous = connection.execute("SELECT observation_snapshot FROM workflow_dispatches WHERE token=?", (record.token,)).fetchone()[0]
            changed = previous != raw or current.step_state != observation.state
            if changed:
                if reconciled:
                    connection.execute(
                        "UPDATE workflow_dispatches SET stage='bound',binding_kind=?,binding_id=?,binding_run_id=? WHERE token=?",
                        (binding.kind, binding.execution_id, binding.run_id, record.token),
                    )
                    if binding.run_id is not None:
                        connection.execute("UPDATE workflow_budget_reservations SET child_request_id=? WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=?",
                                           (binding.run_id, claim.owner_id, claim.workflow_id, record.step_key, record.generation))
                        connection.execute("INSERT OR IGNORE INTO workflow_budget_checks(owner_id,workflow_id,step_key,generation) VALUES(?,?,?,?)",
                                           (claim.owner_id, claim.workflow_id, record.step_key, record.generation))
                connection.execute(
                    "UPDATE workflow_dispatches SET stage=CASE WHEN ?='unknown' THEN 'unknown' ELSE stage END,"
                    "observation_snapshot=?,updated_at=? WHERE token=?",
                    (observation.state, raw, now, record.token),
                )
                connection.execute(
                    "UPDATE workflow_steps SET state=?,reason_code=?,coordinator_epoch=?,updated_at=? "
                    "WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=?",
                    (observation.state, observation.reason_code, claim.epoch, now, claim.owner_id, claim.workflow_id, record.step_key, record.generation),
                )
                self._event(connection, claim.owner_id, claim.workflow_id, "step.observed",
                            {"step_key": record.step_key, "generation": record.generation, **payload}, now)
            if observation.state in TERMINAL_STATES:
                connection.execute(
                    "UPDATE workflow_budget_reservations SET settlement='unknown',updated_at=? WHERE owner_id=? AND workflow_id=? "
                    "AND step_key=? AND generation=? AND settlement='reserved'",
                    (now, claim.owner_id, claim.workflow_id, record.step_key, record.generation),
                )
            if observation.state in ("succeeded", "failed", "cancelled"):
                connection.execute(
                    "UPDATE capability_reservations SET state='released',release_evidence_digest=?,updated_at=? "
                    "WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=? AND state='held'",
                    (canonical_digest({"token": record.token, "binding_id": observation.binding.execution_id, **payload}),
                     now, claim.owner_id, claim.workflow_id, record.step_key, record.generation),
                )
                from .workflow_budget import WorkflowBudgetRepository

                WorkflowBudgetRepository(self)._project(connection, claim.owner_id, claim.workflow_id, record.step_key, record.generation, now)
            if reconciled:
                self._finish_reconciliation(connection, claim, now, "terminal_evidence")
                connection.execute("UPDATE workflow_runs SET execution_state='running' WHERE owner_id=? AND workflow_id=?",
                                   (claim.owner_id, claim.workflow_id))
            return self._project(connection, claim, now, touch=changed)

    @staticmethod
    def _reconciliation(connection: sqlite3.Connection, claim: CoordinatorClaim, step: str, generation: int) -> None:
        if connection.execute(
            "SELECT 1 FROM workflow_reconciliation_requests WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=? AND state='pending'",
            (claim.owner_id, claim.workflow_id, step, generation),
        ).fetchone() is None:
            raise WorkflowRepositoryError("reconciliation_not_requested")

    def reconciliation_request(self, claim: CoordinatorClaim, *, now: float, expected_revision: int) -> WorkflowReconciliationRequest:
        with self.transaction(write=False) as connection:
            self._fenced(connection, claim, _time(now), expected_revision)
            row = connection.execute(
                "SELECT q.step_key,q.generation,q.mode,s.deadline FROM workflow_reconciliation_requests q "
                "LEFT JOIN workflow_stop_requests s USING(owner_id,workspace_id,workflow_id,step_key,generation) "
                "WHERE q.owner_id=? AND q.workflow_id=? AND q.state='pending'",
                (claim.owner_id, claim.workflow_id),
            ).fetchone()
            if row is None:
                raise WorkflowRepositoryError("reconciliation_not_requested")
            return WorkflowReconciliationRequest(row[0], row[1], row[2], row[3] if row[2] == "cancel" else None)

    def _finish_reconciliation(self, connection: sqlite3.Connection, claim: CoordinatorClaim, now: float, reason: str) -> None:
        connection.execute(
            "UPDATE workflow_reconciliation_requests SET state='inspected',inspected_at=? WHERE owner_id=? AND workflow_id=? AND state='pending'",
            (now, claim.owner_id, claim.workflow_id),
        )
        self._event(connection, claim.owner_id, claim.workflow_id, "step.reconciled", {"reason_code": reason}, now)

    def reconciliation_inconclusive(self, claim: CoordinatorClaim, record: DispatchRecord, *, now: float, reason_code: str) -> None:
        _id(reason_code)
        now = _time(now)
        with self.transaction() as connection:
            self._fenced(connection, claim, now, record.revision)
            self._current_dispatch(connection, claim, record)
            self._reconciliation(connection, claim, record.step_key, record.generation)
            self._finish_reconciliation(connection, claim, now, reason_code)
            connection.execute("UPDATE workflow_runs SET reason_code=?,revision=revision+1,updated_at=? WHERE owner_id=? AND workflow_id=?",
                               (reason_code, now, claim.owner_id, claim.workflow_id))
            self._refresh_owner_queue(connection, claim.owner_id)

    def stop_requests(self, claim: CoordinatorClaim, *, now: float, expected_revision: int) -> tuple[WorkflowStopRequest, ...]:
        from .workflow_control_repository import WorkflowControlRepository

        return WorkflowControlRepository(self).stop_requests(claim, now=now, expected_revision=expected_revision)

    def ensure_stop(
        self, claim: CoordinatorClaim, *, step_key: str, generation: int, expected_revision: int,
        now: float, cancel_seconds: float = 30,
    ) -> WorkflowStopRequest | None:
        now = _time(now)
        cancel_seconds = _time(cancel_seconds, positive=True)
        if cancel_seconds > 300:
            raise WorkflowRepositoryError("invalid_cancel_timeout")
        with self.transaction() as connection:
            workflow = self._fenced(connection, claim, now, expected_revision)
            step = connection.execute(
                "SELECT state,active_deadline,wait_deadline,created_at,contract_snapshot FROM workflow_steps "
                "WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=? AND is_current=1",
                (claim.owner_id, claim.workflow_id, step_key, generation),
            ).fetchone()
            if step is None:
                raise WorkflowRepositoryError("generation_conflict")
            existing = connection.execute(
                "SELECT step_key,generation,reason_code,requested_at,deadline FROM workflow_stop_requests "
                "WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=?",
                (claim.owner_id, claim.workflow_id, step_key, generation),
            ).fetchone()
            if existing is not None:
                return WorkflowStopRequest(*existing)
            if step["state"] in TERMINAL_STATES:
                return None
            waiting = step["state"] in ("waiting_node", "waiting_external", "waiting_approval", "paused")
            deadline = step["wait_deadline"] if waiting else step["active_deadline"]
            if deadline is None or deadline > now:
                return None
            reason = "wait_deadline_exceeded" if waiting else "active_deadline_exceeded"
            deadline = _time(now + _time(cancel_seconds, positive=True))
            connection.execute("INSERT INTO workflow_stop_requests VALUES(?,?,?,?,?,?,?,?)",
                               (claim.owner_id, claim.workspace_id, claim.workflow_id, step_key, generation, reason, now, deadline))
            connection.execute(
                "UPDATE workflow_steps SET state='cancelling',reason_code=?,updated_at=? WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=?",
                (reason, now, claim.owner_id, claim.workflow_id, step_key, generation),
            )
            connection.execute("UPDATE workflow_runs SET revision=revision+1,updated_at=? WHERE owner_id=? AND workflow_id=?",
                               (now, claim.owner_id, claim.workflow_id))
            self._event(connection, claim.owner_id, claim.workflow_id, "step.stop_requested", {"step_key": step_key, "generation": generation, "reason_code": reason}, now)
            return WorkflowStopRequest(step_key, generation, reason, now, deadline)

    def apply_stop_observation(
        self, claim: CoordinatorClaim, record: DispatchRecord, observation: ExecutionObservation | None, *, now: float, deadline: float,
    ) -> WorkflowRecord:
        now, deadline = _time(now), _time(deadline)
        if observation is not None and (type(observation) is not ExecutionObservation or observation.binding != record.binding):
            raise WorkflowRepositoryError("dispatch_conflict")
        if observation is not None and observation.state in ("succeeded", "failed", "cancelled"):
            return self.apply_observation(claim, record, observation, now=now)
        if now < deadline:
            return self.refresh_projection(claim, expected_revision=record.revision, now=now)
        with self.transaction() as connection:
            self._fenced(connection, claim, now, record.revision)
            self._current_dispatch(connection, claim, record)
            stop = connection.execute(
                "SELECT deadline FROM workflow_stop_requests WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=?",
                (claim.owner_id, claim.workflow_id, record.step_key, record.generation),
            ).fetchone()
            if stop is None or stop[0] != deadline:
                raise WorkflowRepositoryError("stop_deadline_conflict")
            connection.execute("UPDATE workflow_dispatches SET stage='unknown',updated_at=? WHERE token=?", (now, record.token))
            connection.execute(
                "UPDATE workflow_steps SET state='unknown',reason_code='stop_unconfirmed',updated_at=? WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=?",
                (now, claim.owner_id, claim.workflow_id, record.step_key, record.generation),
            )
            connection.execute(
                "UPDATE workflow_budget_reservations SET settlement='unknown',updated_at=? WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=? AND settlement='reserved'",
                (now, claim.owner_id, claim.workflow_id, record.step_key, record.generation),
            )
            self._event(connection, claim.owner_id, claim.workflow_id, "step.stop_unconfirmed", {"step_key": record.step_key, "generation": record.generation}, now)
            return self._project(connection, claim, now, touch=True)

    def expire_waiting(self, claim: CoordinatorClaim, *, expected_revision: int, now: float) -> bool:
        now = _time(now)
        with self.transaction() as connection:
            workflow = self._fenced(connection, claim, now, expected_revision)
            rows = connection.execute(
                "SELECT s.step_key,s.generation,s.state,s.wait_deadline,s.contract_snapshot,d.stage FROM workflow_steps s "
                "LEFT JOIN workflow_dispatches d USING(owner_id,workspace_id,workflow_id,step_key,generation) "
                "WHERE s.owner_id=? AND s.workflow_id=? AND s.is_current=1 ORDER BY s.position LIMIT 8", (claim.owner_id, claim.workflow_id),
            ).fetchall()
            step = next((item for item in rows if item["state"] not in TERMINAL_STATES), None)
            if step is None or step["stage"] not in (None, "intent") or step["wait_deadline"] is None or now < step["wait_deadline"]:
                return False
            connection.execute(
                "UPDATE workflow_steps SET state='failed',reason_code='wait_deadline_exceeded',updated_at=? WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=?",
                (now, claim.owner_id, claim.workflow_id, step["step_key"], step["generation"]),
            )
            from .workflow_control_repository import WorkflowControlRepository

            WorkflowControlRepository._release_not_started(connection, claim.owner_id, claim.workflow_id, step["step_key"], step["generation"], now)
            self._event(connection, claim.owner_id, claim.workflow_id, "step.wait_expired",
                        {"step_key": step["step_key"], "generation": step["generation"]}, now)
            self._project(connection, claim, now, touch=True)
            return True

    @staticmethod
    def _start_wait(connection: sqlite3.Connection, owner: str, workflow: str, now: float) -> None:
        step = connection.execute(
            "SELECT step_key,generation,contract_snapshot FROM workflow_steps WHERE owner_id=? AND workflow_id=? AND is_current=1 "
            "AND state NOT IN ('succeeded','failed','unknown','cancelled') ORDER BY position LIMIT 1", (owner, workflow),
        ).fetchone()
        if step is None:
            return
        seconds = parse_contract(bytes(step["contract_snapshot"])).budget.max_wait_seconds
        connection.execute(
            "UPDATE workflow_steps SET wait_deadline=coalesce(wait_deadline,?) WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=?",
            (_time(now + seconds), owner, workflow, step["step_key"], step["generation"]),
        )

    def defer_admission(
        self, claim: CoordinatorClaim, *, expected_revision: int, reason_code: str, now: float, step_key: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> WorkflowRecord:
        _id(reason_code)
        now = _time(now)
        waiting_node_reasons = {"capacity_exhausted", "input_receipt_required", "input_alias_unregistered", "receive_node_unavailable", "capability_unavailable"}
        state = "waiting_node" if reason_code in waiting_node_reasons else "waiting_approval"
        with self._connection(connection) as connection:
            workflow = self._fenced(connection, claim, now, expected_revision)
            if workflow["execution_state"] in TERMINAL_STATES:
                raise WorkflowRepositoryError("terminal_workflow")
            changed = False
            self._start_wait(connection, claim.owner_id, claim.workflow_id, now)
            if step_key is not None:
                _id(step_key)
                step = connection.execute(
                    "SELECT state,reason_code FROM workflow_steps WHERE owner_id=? AND workflow_id=? AND step_key=? AND is_current=1",
                    (claim.owner_id, claim.workflow_id, step_key),
                ).fetchone()
                allowed = ("pending", "ready", "waiting_node") if state == "waiting_node" else ("ready", "waiting_node")
                if step is None or step["state"] not in allowed:
                    raise WorkflowRepositoryError("step_not_ready")
                changed = (step["state"], step["reason_code"]) != (state, reason_code)
                connection.execute(
                    "UPDATE workflow_steps SET state=?,reason_code=?,updated_at=? WHERE owner_id=? AND workflow_id=? AND step_key=? AND is_current=1",
                    (state, reason_code, now, claim.owner_id, claim.workflow_id, step_key),
                )
            if changed or (workflow["execution_state"], workflow["reason_code"]) != (state, reason_code):
                connection.execute(
                    "UPDATE workflow_runs SET execution_state=?,reason_code=?,revision=revision+1,updated_at=? WHERE owner_id=? AND workflow_id=?",
                    (state, reason_code, now, claim.owner_id, claim.workflow_id),
                )
                self._event(connection, claim.owner_id, claim.workflow_id, "workflow.deferred", {"reason_code": reason_code, "step_key": step_key}, now)
            return _run(connection.execute(f"SELECT {_RUN_COLUMNS} FROM workflow_runs WHERE owner_id=? AND workflow_id=?",
                                           (claim.owner_id, claim.workflow_id)).fetchone())
