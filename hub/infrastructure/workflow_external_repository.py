"""External I/O facts inside the original Workflow database and transactions."""
from __future__ import annotations

import sqlite3
from typing import Any

from hub.domain.external_agent import ExternalTaskRef
from hub.domain.external_outputs import parse_external_output_proof
from hub.domain.external_request import ExternalAgentRequest, build_external_request, parse_external_request
from hub.domain.workflow_contract import parse_resolved
from hub.domain.workflow_contract_types import ArtifactReference, ResolvedContract
from hub.domain.workflow_errors import WorkflowValidationError
from hub.domain.workflow_execution import DispatchRecord, ExecutionBinding, ExecutionObservation
from hub.domain.workflow_external import ExternalDispatchPin, RegisteredExternalTarget, parse_external_target, parse_external_task, task_snapshot
from hub.domain.workflow_identity import canonical_json, dispatch_token
from hub.domain.workflow_json import decode_snapshot
from hub.domain.workflow_summary import parse_summary_inputs
from .workflow_summary_repository import WorkflowSummaryRepository, validate_summary_input_closure
from .workflow_repository import WorkflowRepository, WorkflowRepositoryError, _time
from .workflow_schema import inspect_schema
from .workflow_external_schema import REQUEST_COLUMN, STATEMENTS, TABLE_COLUMNS, inspect_external_schema

_ROW = (
    "SELECT e.rowid AS external_rowid,e.token,e.owner_id,e.workspace_id,e.workflow_id,e.step_key,e.generation,"
    "e.target_snapshot,e.target_digest,e.send_started_at,e.task_snapshot,e.created_at,e.updated_at,"
    "{request} AS request_snapshot,{version} AS external_schema_version,"
    "(SELECT minimum_reader_version FROM workflow_schema_meta WHERE singleton=1) AS workflow_reader,"
    "d.owner_id AS dispatch_owner,d.workspace_id AS dispatch_workspace,d.workflow_id AS dispatch_workflow,"
    "d.step_key AS dispatch_step,d.generation AS dispatch_generation,d.payload_hash,d.target_key,d.stage,"
    "d.binding_kind,d.binding_id,d.binding_run_id,d.observation_snapshot,s.kind,s.is_current,s.state,s.active_deadline,"
    "s.resolved_snapshot,s.resolved_digest,s.subject_digest,r.lease_owner,r.lease_expires_at,r.cancel_requested,r.pause_requested,"
    "m.admission_mode,w.enabled AS workspace_enabled,"
    "EXISTS(SELECT 1 FROM workflow_reconciliation_requests q WHERE q.owner_id=e.owner_id AND q.workflow_id=e.workflow_id "
    "AND q.step_key=e.step_key AND q.generation=e.generation AND q.state='pending') AS reconciliation_pending "
    "FROM workflow_external_bindings e JOIN workflow_dispatches d ON d.token=e.token "
    "JOIN workflow_steps s ON (s.owner_id,s.workspace_id,s.workflow_id,s.step_key,s.generation)="
    "(d.owner_id,d.workspace_id,d.workflow_id,d.step_key,d.generation) "
    "JOIN workflow_runs r ON (r.owner_id,r.workspace_id,r.workflow_id)=(d.owner_id,d.workspace_id,d.workflow_id) "
    "JOIN workflow_schema_meta m ON m.singleton=1 "
    "LEFT JOIN workspaces w ON w.owner_id=e.owner_id AND w.workspace_id=e.workspace_id "
)


def _query(connection: sqlite3.Connection) -> str:
    inspect_schema(connection)
    version = inspect_external_schema(connection)
    if version is None:
        raise WorkflowRepositoryError("external_schema_unavailable")
    return _ROW.format(request="e.request_snapshot" if version >= 2 else "NULL", version=version)


class ExternalWorkflowRepository:
    def __init__(self, workflows: WorkflowRepository) -> None:
        self.workflows = workflows

    def init(self) -> None:
        with self.workflows.transaction() as connection:
            if inspect_schema(connection) is None:
                raise WorkflowRepositoryError("workflow_schema_unavailable")
            version = inspect_external_schema(connection)
            if version in (3, 4):
                return
            if version == 1:
                connection.execute("ALTER TABLE workflow_external_bindings ADD COLUMN " + REQUEST_COLUMN)
            if version in (1, 2):
                connection.execute("UPDATE workflow_external_schema_meta SET schema_version=3,minimum_reader_version=3 WHERE singleton=1")
            if version is None:
                for statement in STATEMENTS:
                    connection.execute(statement)
            connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=max(minimum_reader_version,6) WHERE singleton=1")

    @staticmethod
    def pin(connection: sqlite3.Connection, token: str, target: RegisteredExternalTarget, *, now: float) -> None:
        inspect_schema(connection)
        if inspect_external_schema(connection) not in (3, 4):
            raise WorkflowRepositoryError("external_schema_unavailable")
        row = connection.execute(
            "SELECT d.owner_id,d.workspace_id,d.target_key,s.kind,s.resolved_snapshot FROM workflow_dispatches d "
            "JOIN workflow_steps s USING(owner_id,workspace_id,workflow_id,step_key,generation) WHERE d.token=?", (token,),
        ).fetchone()
        if type(target) is not RegisteredExternalTarget or row is None or (row[0], row[1], row[2], row[3]) != (
            target.owner_id, target.workspace_id, target.target_key, "external_agent",
        ):
            raise WorkflowRepositoryError("external_registration_conflict")
        try:
            resolved = parse_resolved(bytes(row[4]))
            target.validate_resolved(resolved)
        except WorkflowValidationError as exc:
            raise WorkflowRepositoryError(exc.code) from exc
        if any(named.reference.kind == "summary" for named in resolved.contract.context_refs):
            connection.execute("UPDATE workflow_external_schema_meta SET schema_version=4,minimum_reader_version=4 WHERE singleton=1")
            connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=max(minimum_reader_version,11) WHERE singleton=1")
        connection.execute(
            "INSERT INTO workflow_external_bindings(token,owner_id,workspace_id,workflow_id,step_key,generation,"
            "target_snapshot,target_digest,created_at,updated_at) SELECT token,owner_id,workspace_id,workflow_id,step_key,generation,?,?,?,? "
            "FROM workflow_dispatches WHERE token=?", (target.snapshot, target.digest, now, now, token),
        )
        if resolved.contract.output_specs:
            connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=max(minimum_reader_version,8) WHERE singleton=1")
            inspect_schema(connection)

    @staticmethod
    def _decode(row: sqlite3.Row) -> ExternalDispatchPin:
        try:
            target = parse_external_target(bytes(row["target_snapshot"]))
            resolved = parse_resolved(bytes(row["resolved_snapshot"]))
            if resolved.contract.schema_version == 2 and row["workflow_reader"] < 6:
                raise WorkflowValidationError("invalid_contract_reader")
            if resolved.contract.output_specs and row["workflow_reader"] < 8:
                raise WorkflowValidationError("invalid_external_output_reader")
            if any(named.reference.kind == "summary" for named in resolved.contract.context_refs) and (
                    row["workflow_reader"] < 11 or row["external_schema_version"] < 4):
                raise WorkflowValidationError("invalid_summary_reader")
            target.validate_resolved(resolved)
            if resolved.snapshot != bytes(row["resolved_snapshot"]):
                raise WorkflowValidationError("noncanonical_resolved_snapshot")
            scope = (row["owner_id"], row["workspace_id"], row["workflow_id"], row["step_key"], row["generation"])
            dispatch_scope = (row["dispatch_owner"], row["dispatch_workspace"], row["dispatch_workflow"], row["dispatch_step"], row["dispatch_generation"])
            if scope != dispatch_scope or (target.owner_id, target.workspace_id, target.target_key, target.digest, resolved.step_key,
                    resolved.digest, resolved.digest, row["kind"], dispatch_token(scope[0], scope[2], scope[3], scope[4])) != (
                    scope[0], scope[1], row["target_key"], row["target_digest"], scope[3], row["payload_hash"],
                    row["resolved_digest"], "external_agent", row["token"]):
                raise WorkflowValidationError("external_dispatch_conflict")
            created, updated = _time(row["created_at"]), _time(row["updated_at"])
            started = None if row["send_started_at"] is None else _time(row["send_started_at"])
            if updated < created or (started is not None and not created <= started <= updated):
                raise WorkflowValidationError("invalid_external_timestamp")
            task = None if row["task_snapshot"] is None else parse_external_task(bytes(row["task_snapshot"]), target, row["token"])
            request = None if row["request_snapshot"] is None else parse_external_request(bytes(row["request_snapshot"]), resolved)
            has_inputs = bool(resolved.contract.input_refs)
            if ((has_inputs and row["external_schema_version"] < 2)
                    or (resolved.contract.context_refs and row["external_schema_version"] < 3)
                    or (request is not None and started is None)):
                raise WorkflowValidationError("invalid_external_request")
            if started is not None and request is None:
                request = build_external_request(resolved)
            if (task is not None and started is None) or (row["binding_kind"] is not None and (
                    row["binding_kind"] != "external" or row["binding_id"] != "wf-external-" + row["token"] or row["binding_run_id"] is not None or task is None)):
                raise WorkflowValidationError("invalid_external_binding")
            observed = {} if row["observation_snapshot"] is None else decode_snapshot(bytes(row["observation_snapshot"]), max_bytes=16384)
            accepted = observed.get("evidence_source") in {"accepted", "agent_reported", "confirmed"}
            observation = None
            if "external_outputs" in observed:
                if (not resolved.contract.output_specs or task is None or row["binding_kind"] != "external"
                        or row["stage"] != "bound" or row["state"] != "succeeded"
                        or set(observed) != {"state", "reason_code", "task_id", "attempt_id", "evidence_source", "external_outputs"}):
                    raise WorkflowValidationError("invalid_external_output_proof")
                proof = parse_external_output_proof(canonical_json(observed["external_outputs"]), resolved, task)
                if row["subject_digest"] != proof.subject_digest(resolved.digest):
                    raise WorkflowValidationError("external_output_subject_mismatch")
                binding = ExecutionBinding(target.owner_id, target.workspace_id, target.target_key, row["token"],
                    row["payload_hash"], "external", row["binding_id"])
                observation = ExecutionObservation(binding, observed["state"], observed["reason_code"], observed["task_id"],
                    observed["attempt_id"], observed["evidence_source"], proof)
                if canonical_json(observed) != bytes(row["observation_snapshot"]):
                    raise WorkflowValidationError("invalid_external_output_proof")
            elif resolved.contract.output_specs and (row["state"] == "succeeded" or row["subject_digest"] is not None):
                raise WorkflowValidationError("external_output_proof_missing")
            return ExternalDispatchPin(target, resolved, started, task, accepted, request, observation)
        except (WorkflowValidationError, TypeError, ValueError) as exc:
            raise WorkflowRepositoryError("external_snapshot_invalid") from exc

    @classmethod
    def _read(cls, connection: sqlite3.Connection, token: str, target: RegisteredExternalTarget,
              payload_hash: str, resolved: ResolvedContract | None = None) -> tuple[sqlite3.Row, ExternalDispatchPin]:
        row = connection.execute(_query(connection) + "WHERE e.token=?", (token,)).fetchone()
        if row is None:
            raise WorkflowRepositoryError("external_dispatch_unpinned")
        pin = cls._decode(row)
        recovered = pin.observation is not None and resolved is None and (
            pin.target.owner_id, pin.target.workspace_id, pin.target.target_key) == (target.owner_id, target.workspace_id, target.target_key)
        if ((pin.target != target and not recovered) or pin.resolved.digest != payload_hash
                or (resolved is not None and pin.resolved != resolved)):
            raise WorkflowRepositoryError("dispatch_conflict")
        cls._validate_output_refs(connection, (row,), (pin,))
        cls._validate_summary_refs(connection, (pin,))
        return row, pin

    @staticmethod
    def _validate_summary_refs(connection: sqlite3.Connection, pins: tuple[ExternalDispatchPin, ...]) -> None:
        records = []
        for pin in pins:
            if pin.request is not None and any(named.reference.kind == "summary" for named in pin.resolved.contract.context_refs):
                prompt = decode_snapshot(pin.request.prompt.encode("utf-8"))
                records.append((pin.resolved, parse_summary_inputs(prompt.get("summary_inputs", []))))
        validate_summary_input_closure(connection, records)

    def lookup(self, token: str, payload_hash: str, resolved: ResolvedContract, target: RegisteredExternalTarget) -> ExternalDispatchPin:
        with self.workflows.transaction(write=False) as connection:
            return self._read(connection, token, target, payload_hash, resolved)[1]

    def read_binding(self, binding: ExecutionBinding, target: RegisteredExternalTarget) -> ExternalDispatchPin:
        if binding != ExecutionBinding(target.owner_id, target.workspace_id, target.target_key, binding.token,
                binding.payload_hash, "external", "wf-external-" + binding.token):
            raise WorkflowRepositoryError("dispatch_conflict")
        with self.workflows.transaction(write=False) as connection:
            return self._read(connection, binding.token, target, binding.payload_hash)[1]

    @staticmethod
    def _output_authority(connection: sqlite3.Connection, row: sqlite3.Row, pin: ExternalDispatchPin, now: float) -> None:
        if not pin.resolved.contract.output_specs or not pin.target.capabilities.output_export_supported or pin.task is None:
            raise WorkflowRepositoryError("external_output_unavailable")
        if (row["admission_mode"] == "frozen" or row["cancel_requested"] or row["workspace_enabled"] != 1
                or row["is_current"] != 1 or row["binding_kind"] != "external"
                or row["lease_owner"] is None or row["lease_expires_at"] is None or _time(row["lease_expires_at"]) <= now):
            raise WorkflowRepositoryError("external_output_forbidden")
        if row["active_deadline"] is None or _time(row["active_deadline"]) <= now:
            raise WorkflowRepositoryError("active_deadline_exceeded")
        unknown = row["state"] == "unknown" or row["stage"] == "unknown"
        if unknown and not row["reconciliation_pending"]:
            raise WorkflowRepositoryError("reconciliation_required")
        if row["stage"] not in ("bound", "unknown") or row["state"] not in ("running", "waiting_external", "paused", "unknown"):
            raise WorkflowRepositoryError("external_output_forbidden")
        if now < _time(row["updated_at"]):
            raise WorkflowRepositoryError("invalid_external_timestamp")
        reservation = connection.execute(
            "SELECT c.state,c.target_key,b.settlement FROM capability_reservations c JOIN workflow_budget_reservations b "
            "USING(owner_id,workspace_id,workflow_id,step_key,generation) WHERE c.owner_id=? AND c.workflow_id=? "
            "AND c.step_key=? AND c.generation=?", (row["owner_id"], row["workflow_id"], row["step_key"], row["generation"]),
        ).fetchone()
        if (reservation is None or reservation[0] not in ("held", "isolated") or reservation[1] != row["target_key"]
                or reservation[2] not in ("reserved", "unknown")):
            raise WorkflowRepositoryError("external_reservation_conflict")

    def authorize_outputs(self, binding: ExecutionBinding, target: RegisteredExternalTarget, *, now: float) -> None:
        now = _time(now)
        expected = ExecutionBinding(target.owner_id, target.workspace_id, target.target_key, binding.token,
                                    binding.payload_hash, "external", "wf-external-" + binding.token)
        if binding != expected:
            raise WorkflowRepositoryError("dispatch_conflict")
        with self.workflows.transaction(write=False) as connection:
            row, pin = self._read(connection, binding.token, target, binding.payload_hash)
            if pin.observation is not None:
                return
            self._output_authority(connection, row, pin, now)

    @classmethod
    def commit_outputs(cls, connection: sqlite3.Connection, record: DispatchRecord,
                       observation: ExecutionObservation, *, now: float, reconciled: bool) -> None:
        """Only called inside original fenced observation/state/event transaction."""
        row = connection.execute(_query(connection) + "WHERE e.token=?", (record.token,)).fetchone()
        if row is None:
            raise WorkflowRepositoryError("external_dispatch_unpinned")
        pin = cls._decode(row)
        cls._validate_output_refs(connection, (row,), (pin,))
        cls._validate_summary_refs(connection, (pin,))
        proof = observation.external_outputs
        if pin.resolved != record.resolved or pin.task is None or observation.binding != ExecutionBinding(
                pin.target.owner_id, pin.target.workspace_id, pin.target.target_key, record.token,
                record.payload_hash, "external", "wf-external-" + record.token):
            raise WorkflowRepositoryError("external_dispatch_conflict")
        if pin.observation is not None:
            if observation != pin.observation:
                raise WorkflowRepositoryError("external_output_conflict")
            return
        if proof is None:
            if pin.resolved.contract.output_specs and observation.state == "succeeded":
                raise WorkflowRepositoryError("external_output_proof_missing")
            return
        try:
            proof = parse_external_output_proof(proof.snapshot, pin.resolved, pin.task)
        except WorkflowValidationError as exc:
            raise WorkflowRepositoryError(exc.code) from exc
        cls._output_authority(connection, row, pin, now)
        if (row["state"] == "unknown" or row["stage"] == "unknown") and not reconciled:
            raise WorkflowRepositoryError("reconciliation_required")
        scope = (row["owner_id"], row["workspace_id"], row["workflow_id"], row["step_key"], row["generation"])
        connection.executemany(
            "INSERT INTO workflow_artifact_refs(owner_id,workspace_id,workflow_id,step_key,generation,output_key,artifact_id,sha256,size) "
            "VALUES(?,?,?,?,?,?,?,?,?)", ((*scope, key, ref.artifact_id, ref.sha256, ref.size) for key, ref in proof.artifact_refs()),
        )
        connection.execute("UPDATE workflow_steps SET subject_digest=? WHERE owner_id=? AND workspace_id=? AND workflow_id=? "
                           "AND step_key=? AND generation=?", (proof.subject_digest(pin.resolved.digest), *scope))

    @staticmethod
    def _validate_output_refs(connection: sqlite3.Connection, rows: tuple[sqlite3.Row, ...],
                              pins: tuple[ExternalDispatchPin, ...]) -> None:
        if not rows:
            return
        expected: dict[tuple[int, str], tuple[Any, ...]] = {}
        for row, pin in zip(rows, pins):
            scope = (row["owner_id"], row["workspace_id"], row["workflow_id"], row["step_key"], row["generation"])
            references = tuple(("input:" + item.ref_key, item.reference) for item in pin.resolved.contract.input_refs)
            if pin.observation is not None:
                references += pin.observation.external_outputs.artifact_refs()
            for key, ref in references:
                if type(ref) is not ArtifactReference:
                    raise WorkflowRepositoryError("external_output_reference_invalid")
                expected[(row["external_rowid"], key)] = (*scope, ref.artifact_id, ref.sha256, ref.size)
        placeholders = ",".join("?" for _ in rows)
        refs = connection.execute(
            "SELECT e.rowid AS external_rowid,a.owner_id,a.workspace_id,a.workflow_id,a.step_key,a.generation,"
            "a.output_key,a.artifact_id,a.sha256,a.size FROM workflow_external_bindings e JOIN workflow_artifact_refs a "
            "ON (a.owner_id,a.workflow_id,a.step_key,a.generation)=(e.owner_id,e.workflow_id,e.step_key,e.generation) "
            f"WHERE e.rowid IN ({placeholders}) LIMIT ?", (*[row["external_rowid"] for row in rows], len(rows) * 48 + 1),
        ).fetchall()
        actual = {(ref["external_rowid"], ref["output_key"]): tuple(ref[key] for key in
            ("owner_id", "workspace_id", "workflow_id", "step_key", "generation", "artifact_id", "sha256", "size")) for ref in refs}
        if len(refs) != len(expected) or actual != expected or any(type(ref["size"]) is not int for ref in refs):
            raise WorkflowRepositoryError("external_output_reference_invalid")

    def prepare_send(self, token: str, payload_hash: str, resolved: ResolvedContract,
                     target: RegisteredExternalTarget, *, now: float, request: ExternalAgentRequest | None = None) -> bool:
        now = _time(now)
        try:
            request = build_external_request(resolved) if request is None else parse_external_request(request.snapshot, resolved)
        except (WorkflowValidationError, AttributeError) as exc:
            raise WorkflowRepositoryError("external_request_invalid") from exc
        with self.workflows.transaction() as connection:
            if inspect_external_schema(connection) not in (3, 4):
                raise WorkflowRepositoryError("external_schema_upgrade_required")
            row, pin = self._read(connection, token, target, payload_hash, resolved)
            if now < _time(row["updated_at"]):
                raise WorkflowRepositoryError("invalid_external_timestamp")
            mode = connection.execute("SELECT admission_mode FROM workflow_schema_meta WHERE singleton=1").fetchone()[0]
            if mode != "open" or row["cancel_requested"] or row["pause_requested"]:
                raise WorkflowRepositoryError("admission_closed")
            if row["stage"] != "sending" or row["is_current"] != 1 or row["state"] != "dispatching" or row["binding_kind"] is not None:
                raise WorkflowRepositoryError("dispatch_conflict")
            if row["lease_owner"] is None or row["lease_expires_at"] is None or _time(row["lease_expires_at"]) <= now:
                raise WorkflowRepositoryError("coordinator_lease_lost")
            if row["active_deadline"] is None or _time(row["active_deadline"]) <= now:
                raise WorkflowRepositoryError("active_deadline_exceeded")
            if connection.execute("SELECT 1 FROM workspaces WHERE owner_id=? AND workspace_id=? AND enabled=1",
                    (target.owner_id, target.workspace_id)).fetchone() is None:
                raise WorkflowRepositoryError("reference_forbidden")
            reservation = connection.execute(
                "SELECT c.state,c.target_key,b.settlement FROM capability_reservations c JOIN workflow_budget_reservations b "
                "USING(owner_id,workspace_id,workflow_id,step_key,generation) WHERE c.owner_id=? AND c.workflow_id=? AND c.step_key=? AND c.generation=?",
                (target.owner_id, row["workflow_id"], row["step_key"], row["generation"]),
            ).fetchone()
            if reservation is None or tuple(reservation) != ("held", target.target_key, "reserved"):
                raise WorkflowRepositoryError("external_reservation_conflict")
            summary_refs = tuple(named.reference for named in resolved.contract.context_refs if named.reference.kind == "summary")
            if summary_refs:
                try:
                    current = WorkflowSummaryRepository.read_connection(connection, resolved.owner_id, resolved.workspace_id, summary_refs)
                    prompt = decode_snapshot(request.prompt.encode("utf-8"))
                    saved = tuple(item for _key, item in parse_summary_inputs(prompt.get("summary_inputs", [])))
                    if current != saved:
                        raise WorkflowValidationError("summary_selection_stale")
                except WorkflowValidationError as exc:
                    raise WorkflowRepositoryError(exc.code) from exc
            if pin.request is not None and pin.request != request:
                raise WorkflowRepositoryError("external_request_conflict")
            if pin.send_started_at is not None:
                return False
            connection.execute("UPDATE workflow_external_bindings SET send_started_at=?,updated_at=?,request_snapshot=? WHERE token=? AND send_started_at IS NULL",
                (now, now, request.snapshot, token))
            return True

    def bind_task(self, token: str, payload_hash: str, resolved: ResolvedContract,
                  target: RegisteredExternalTarget, task: ExternalTaskRef, *, now: float) -> ExternalTaskRef:
        raw = task_snapshot(task, target, token)
        now = _time(now)
        with self.workflows.transaction() as connection:
            _row, pin = self._read(connection, token, target, payload_hash, resolved)
            if pin.send_started_at is None:
                raise WorkflowRepositoryError("external_send_intent_missing")
            if pin.task is not None:
                if pin.task != task:
                    raise WorkflowRepositoryError("external_task_conflict")
                return pin.task
            if now < pin.send_started_at:
                raise WorkflowRepositoryError("invalid_external_timestamp")
            connection.execute("UPDATE workflow_external_bindings SET task_snapshot=?,updated_at=? WHERE token=? AND task_snapshot IS NULL", (raw, now, token))
            return task

    @classmethod
    def validate_snapshot(cls, connection: sqlite3.Connection) -> None:
        if not inspect_external_schema(connection):
            return
        previous = connection.row_factory
        connection.row_factory = sqlite3.Row
        try:
            for table in TABLE_COLUMNS:
                if connection.execute(f"PRAGMA foreign_key_check({table})").fetchone() is not None:
                    raise WorkflowRepositoryError("external_reference_invalid")
            cursor: int | None = None
            query = _query(connection)
            while True:
                predicate = "" if cursor is None else "WHERE e.rowid>? "
                rows = connection.execute(query + predicate + "ORDER BY e.rowid LIMIT 128",
                    () if cursor is None else (cursor,)).fetchall()
                if not rows:
                    return
                pins = tuple(cls._decode(row) for row in rows)
                cls._validate_output_refs(connection, tuple(rows), pins)
                cls._validate_summary_refs(connection, pins)
                cursor = rows[-1]["external_rowid"]
        finally:
            connection.row_factory = previous
