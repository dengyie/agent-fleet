"""Transactional workflow controls; immutable history and durable receipts."""
from __future__ import annotations

import sqlite3

from hub.domain.workflow_contract import parse_contract, parse_definition, rebind_contract, reference_values
from hub.domain.workflow_control import AuthorizedInputRevision, WorkflowControlReceipt, WorkflowControlRequest, WorkflowStopRequest, generation_plan
from hub.domain.workflow_errors import WorkflowValidationError
from hub.domain.workflow_identity import canonical_digest, canonical_json
from hub.domain.workflow_json import decode_snapshot
from hub.domain.workflow_projection import TERMINAL_STATES
from hub.domain.workflow_records import CoordinatorClaim
from .workflow_repository import WorkflowRepository, WorkflowRepositoryError, _RUN_COLUMNS, _time


class WorkflowControlRepository:
    def __init__(self, repository: WorkflowRepository) -> None:
        self.repository = repository

    @staticmethod
    def _receipt(raw: bytes) -> WorkflowControlReceipt:
        value = decode_snapshot(raw)
        return WorkflowControlReceipt(value["action"], value["revision"], value["execution_state"],
                                      tuple((item[0], item[1]) for item in value["generations"]))

    def control(self, request: WorkflowControlRequest, *, now: float, cancel_seconds: float = 30,
                authorized_inputs: AuthorizedInputRevision | None = None) -> WorkflowControlReceipt:
        if type(request) is not WorkflowControlRequest:
            raise WorkflowRepositoryError("invalid_control_request")
        now = _time(now)
        cancel_seconds = _time(cancel_seconds, positive=True)
        if cancel_seconds > 300:
            raise WorkflowRepositoryError("invalid_cancel_timeout")
        digest = canonical_digest({"action": request.action, "revision": request.expected_revision, "step_key": request.step_key,
                                   "generation": request.expected_generation, "reason": request.reason,
                                   "input_refs": reference_values(request.input_refs) if request.input_refs is not None else None})
        with self.repository.transaction() as connection:
            workflow = connection.execute(f"SELECT {_RUN_COLUMNS},acceptance_required FROM workflow_runs WHERE owner_id=? AND workflow_id=?",
                                          (request.owner_id, request.workflow_id)).fetchone()
            if workflow is None:
                raise WorkflowRepositoryError("workflow_not_found")
            previous = connection.execute(
                "SELECT request_digest,receipt FROM workflow_operations WHERE owner_id=? AND workflow_id=? AND client_token=?",
                (request.owner_id, request.workflow_id, request.client_token),
            ).fetchone()
            if previous is not None:
                if previous[0] != digest:
                    raise WorkflowRepositoryError("idempotency_conflict")
                return self._receipt(bytes(previous[1]))
            if workflow["revision"] != request.expected_revision:
                raise WorkflowRepositoryError("revision_conflict")
            if request.input_refs is not None and (type(authorized_inputs) is not AuthorizedInputRevision or
                    (authorized_inputs.owner_id, authorized_inputs.workspace_id, authorized_inputs.input_refs) !=
                    (request.owner_id, workflow["workspace_id"], request.input_refs)):
                raise WorkflowRepositoryError("reference_forbidden")
            mode = connection.execute("SELECT admission_mode FROM workflow_schema_meta WHERE singleton=1").fetchone()[0]
            if mode == "frozen" and request.action not in ("pause", "cancel"):
                raise WorkflowRepositoryError("admission_frozen")
            generations = self._apply(connection, workflow, request, now, cancel_seconds)
            current = connection.execute(f"SELECT {_RUN_COLUMNS} FROM workflow_runs WHERE owner_id=? AND workflow_id=?",
                                         (request.owner_id, request.workflow_id)).fetchone()
            receipt = WorkflowControlReceipt(request.action, current["revision"], current["execution_state"], generations)
            raw = canonical_json({"action": receipt.action, "revision": receipt.revision, "execution_state": receipt.execution_state,
                                  "generations": [list(item) for item in receipt.generations]})
            connection.execute("INSERT INTO workflow_operations VALUES(?,?,?,?,?,?,?)",
                               (request.owner_id, workflow["workspace_id"], request.workflow_id, request.client_token, digest, raw, now))
            self.repository._refresh_owner_queue(connection, request.owner_id)
            return receipt

    def _apply(
        self, connection: sqlite3.Connection, workflow: sqlite3.Row, request: WorkflowControlRequest, now: float, cancel_seconds: float,
    ) -> tuple[tuple[str, int], ...]:
        if request.action in ("retry", "invalidate"):
            return self._regenerate(connection, workflow, request, now)
        if request.action == "reconcile":
            step = connection.execute(
                "SELECT state,generation FROM workflow_steps WHERE owner_id=? AND workflow_id=? AND step_key=? AND is_current=1",
                (request.owner_id, request.workflow_id, request.step_key),
            ).fetchone()
            if step is None or (request.expected_generation is not None and step["generation"] != request.expected_generation):
                raise WorkflowRepositoryError("generation_conflict")
            if step["state"] != "unknown":
                raise WorkflowRepositoryError("reconciliation_not_required")
            if connection.execute("SELECT 1 FROM workflow_reconciliation_requests WHERE owner_id=? AND workflow_id=? AND state='pending'",
                                  (request.owner_id, request.workflow_id)).fetchone():
                raise WorkflowRepositoryError("reconciliation_in_progress")
            connection.execute("INSERT INTO workflow_reconciliation_requests VALUES(?,?,?,?,?,?,'pending','inspect',?,NULL)",
                               (request.owner_id, workflow["workspace_id"], request.workflow_id, request.step_key,
                                step["generation"], request.client_token, now))
            self._wake(connection, request, now, "reconciliation_requested")
            self.repository._event(connection, request.owner_id, request.workflow_id, "step.reconciliation_requested",
                                   {"step_key": request.step_key, "generation": step["generation"]}, now)
            return ()
        if workflow["execution_state"] == "unknown" and request.action == "cancel":
            self._cancel_unknown(connection, workflow, request, now, cancel_seconds)
            return ()
        if workflow["execution_state"] in TERMINAL_STATES:
            if request.action == "cancel":
                return ()
            raise WorkflowRepositoryError("terminal_workflow")
        if request.action == "cancel":
            self._cancel(connection, workflow, now, cancel_seconds)
        else:
            pause = request.action == "pause"
            if bool(workflow["cancel_requested"]):
                raise WorkflowRepositoryError("cancellation_in_progress")
            if bool(workflow["pause_requested"]) == pause:
                return ()
            if pause:
                self.repository._start_wait(connection, request.owner_id, request.workflow_id, now)
            connection.execute(
                "UPDATE workflow_runs SET pause_requested=?,revision=revision+1,next_poll_at=?,updated_at=?,"
                "lease_owner=NULL,lease_expires_at=NULL WHERE owner_id=? AND workflow_id=?",
                (int(pause), now, now, request.owner_id, request.workflow_id),
            )
        self.repository._event(connection, request.owner_id, request.workflow_id, "workflow." + request.action + "_requested", {}, now)
        return ()

    def _cancel_unknown(self, connection: sqlite3.Connection, workflow: sqlite3.Row, request: WorkflowControlRequest,
                        now: float, cancel_seconds: float) -> None:
        pending = connection.execute("SELECT 1 FROM workflow_reconciliation_requests WHERE owner_id=? AND workflow_id=? AND state='pending'",
                                     (request.owner_id, request.workflow_id)).fetchone()
        if pending is not None:
            raise WorkflowRepositoryError("reconciliation_in_progress")
        unknown = connection.execute(
            "SELECT step_key,generation FROM workflow_steps WHERE owner_id=? AND workflow_id=? AND is_current=1 AND state='unknown' LIMIT 8",
            (request.owner_id, request.workflow_id),
        ).fetchall()
        if len(unknown) != 1:
            raise WorkflowRepositoryError("unresolved_execution")
        step, generation = unknown[0]
        self._cancel(connection, workflow, now, cancel_seconds)
        if workflow["cancel_requested"]:
            self._wake(connection, request, now, "reconciliation_requested")
        self._stop_row(connection, workflow, step, generation, "cancel_requested", now, cancel_seconds)
        connection.execute("INSERT INTO workflow_reconciliation_requests VALUES(?,?,?,?,?,?,'pending','cancel',?,NULL)",
                           (request.owner_id, workflow["workspace_id"], request.workflow_id, step, generation, request.client_token, now))
        connection.execute("UPDATE workflow_runs SET execution_state='unknown',reason_code='reconciliation_requested' WHERE owner_id=? AND workflow_id=?",
                           (request.owner_id, request.workflow_id))
        self.repository._event(connection, request.owner_id, request.workflow_id, "workflow.cancel_requested", {"unknown_execution": True}, now)

    @staticmethod
    def _wake(connection: sqlite3.Connection, request: WorkflowControlRequest, now: float, reason: str) -> None:
        connection.execute(
            "UPDATE workflow_runs SET reason_code=?,revision=revision+1,next_poll_at=?,updated_at=?,lease_owner=NULL,lease_expires_at=NULL "
            "WHERE owner_id=? AND workflow_id=?", (reason, now, now, request.owner_id, request.workflow_id),
        )

    def _cancel(self, connection: sqlite3.Connection, workflow: sqlite3.Row, now: float, cancel_seconds: float) -> None:
        if workflow["cancel_requested"]:
            return
        owner, workflow_id = workflow["owner_id"], workflow["workflow_id"]
        steps = connection.execute(
            "SELECT s.step_key,s.generation,s.state,d.stage FROM workflow_steps s "
            "LEFT JOIN workflow_dispatches d USING(owner_id,workspace_id,workflow_id,step_key,generation) "
            "WHERE s.owner_id=? AND s.workflow_id=? AND s.is_current=1 ORDER BY s.position LIMIT 8", (owner, workflow_id),
        ).fetchall()
        for step in steps:
            if step["state"] in TERMINAL_STATES:
                continue
            never_sent = step["stage"] in (None, "intent")
            state = "cancelled" if never_sent else "cancelling"
            connection.execute(
                "UPDATE workflow_steps SET state=?,reason_code='cancel_requested',updated_at=? "
                "WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=?",
                (state, now, owner, workflow_id, step["step_key"], step["generation"]),
            )
            if never_sent:
                self._release_not_started(connection, owner, workflow_id, step["step_key"], step["generation"], now)
                continue
            self._stop_row(connection, workflow, step["step_key"], step["generation"], "cancel_requested", now, cancel_seconds)
        pending = connection.execute(
            "SELECT 1 FROM workflow_steps WHERE owner_id=? AND workflow_id=? AND is_current=1 AND state='cancelling' LIMIT 1", (owner, workflow_id),
        ).fetchone()
        state = "unknown" if any(step["state"] == "unknown" for step in steps) else ("cancelling" if pending else "cancelled")
        connection.execute(
            "UPDATE workflow_runs SET cancel_requested=1,execution_state=?,reason_code='cancel_requested',revision=revision+1,"
            "next_poll_at=?,updated_at=?,lease_owner=NULL,lease_expires_at=NULL WHERE owner_id=? AND workflow_id=?",
            (state, now, now, owner, workflow_id),
        )

    @staticmethod
    def _stop_row(connection: sqlite3.Connection, workflow: sqlite3.Row, step: str, generation: int, reason: str, now: float, seconds: float) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO workflow_stop_requests VALUES(?,?,?,?,?,?,?,?)",
            (workflow["owner_id"], workflow["workspace_id"], workflow["workflow_id"], step, generation, reason, now, _time(now + seconds)),
        )

    @staticmethod
    def _release_not_started(connection: sqlite3.Connection, owner: str, workflow: str, step: str, generation: int, now: float) -> None:
        parameters = (now, owner, workflow, step, generation)
        connection.execute(
            "UPDATE workflow_budget_reservations SET settlement='settled',charged_tokens=0,charged_requests=0,updated_at=? "
            "WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=?", parameters,
        )
        connection.execute(
            "UPDATE capability_reservations SET state='released',release_evidence_digest=?,updated_at=? "
            "WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=? AND state='held'",
            (canonical_digest({"workflow_id": workflow, "step_key": step, "generation": generation, "not_started": True}), *parameters),
        )

    def _regenerate(self, connection: sqlite3.Connection, workflow: sqlite3.Row, request: WorkflowControlRequest, now: float) -> tuple[tuple[str, int], ...]:
        if workflow["execution_state"] in ("unknown", "cancelled") or workflow["cancel_requested"]:
            raise WorkflowRepositoryError("unresolved_execution" if workflow["execution_state"] == "unknown" else "cancelled_workflow")
        rows = connection.execute(
            "SELECT s.step_key,s.generation,s.state,s.contract_snapshot,s.position,s.kind,s.resolved_snapshot,"
            "EXISTS(SELECT 1 FROM capability_reservations c WHERE c.owner_id=s.owner_id AND c.workflow_id=s.workflow_id "
            "AND c.step_key=s.step_key AND c.generation=s.generation AND c.state IN ('held','isolated')) AS held "
            "FROM workflow_steps s WHERE s.owner_id=? AND s.workflow_id=? AND s.is_current=1 ORDER BY s.position LIMIT 8",
            (request.owner_id, request.workflow_id),
        ).fetchall()
        by_key = {row["step_key"]: row for row in rows}
        selected = by_key.get(request.step_key)
        if selected is None or selected["generation"] != request.expected_generation:
            raise WorkflowRepositoryError("generation_conflict")
        try:
            plan = generation_plan(tuple((row["step_key"], row["generation"], row["state"], bool(row["held"]),
                                          decode_snapshot(bytes(row["contract_snapshot"]))["budget"]["max_generations"]) for row in rows),
                                   request.step_key, invalidate=request.action == "invalidate")
        except WorkflowValidationError as exc:
            raise WorkflowRepositoryError(exc.code) from exc
        definition = parse_definition(bytes(workflow["definition_snapshot"]))
        templates = {item.step_key: item for item in definition.steps}
        for key, generation in plan:
            previous = by_key[key]
            current_contract = parse_contract(bytes(previous["contract_snapshot"]))
            if rebind_contract(templates[key].contract, current_contract.input_refs) != current_contract:
                raise WorkflowRepositoryError("invalid_contract_override")
            if key == request.step_key and request.input_refs is not None:
                try:
                    current_contract = rebind_contract(templates[key].contract, request.input_refs)
                except WorkflowValidationError as exc:
                    raise WorkflowRepositoryError(exc.code) from exc
            contract = decode_snapshot(current_contract.snapshot)
            budget = contract["budget"]
            totals = connection.execute(
                "SELECT coalesce(sum(CASE WHEN settlement='settled' THEN charged_tokens ELSE max(reserved_tokens,coalesce(charged_tokens,0)) END),0),"
                "coalesce(sum(CASE WHEN settlement='settled' THEN charged_requests ELSE max(reserved_requests,coalesce(charged_requests,0)) END),0) "
                "FROM workflow_budget_reservations WHERE owner_id=? AND workflow_id=? AND step_key=?", (request.owner_id, request.workflow_id, key),
            ).fetchone()
            if totals[0] >= budget["max_tokens"] > 0 or totals[1] >= budget["max_model_requests"] > 0:
                raise WorkflowRepositoryError("budget_exhausted")
            connection.execute("UPDATE workflow_steps SET is_current=0 WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=?",
                               (request.owner_id, request.workflow_id, key, previous["generation"]))
            connection.execute("UPDATE workflow_evidence SET is_current=0 WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=?",
                               (request.owner_id, request.workflow_id, key, previous["generation"]))
            connection.execute(
                "INSERT INTO workflow_steps(owner_id,workspace_id,workflow_id,step_key,generation,position,kind,contract_snapshot,validation_state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (request.owner_id, workflow["workspace_id"], request.workflow_id, key, generation, previous["position"], previous["kind"],
                 current_contract.snapshot, "pending" if contract["validator_refs"] else "not_requested", now, now),
            )
            self.repository._input_artifacts(connection, request.owner_id, workflow["workspace_id"], request.workflow_id, key, generation, current_contract.input_refs)
        connection.execute(
            "UPDATE workflow_runs SET execution_state='running',validation_state='pending',acceptance_state=?,delivery_digest=NULL,reason_code=NULL,"
            "revision=revision+1,next_poll_at=?,updated_at=?,lease_owner=NULL,lease_expires_at=NULL WHERE owner_id=? AND workflow_id=?",
            ("pending" if workflow["acceptance_required"] else "not_requested", now, now, request.owner_id, request.workflow_id),
        )
        self.repository._event(connection, request.owner_id, request.workflow_id, "workflow.generations_created",
                               {"action": request.action, "reason": request.reason, "generations": [list(item) for item in plan]}, now)
        return plan

    def stop_requests(self, claim: CoordinatorClaim, *, now: float, expected_revision: int) -> tuple[WorkflowStopRequest, ...]:
        with self.repository.transaction(write=False) as connection:
            self.repository._fenced(connection, claim, _time(now), expected_revision)
            rows = connection.execute(
                "SELECT q.step_key,q.generation,q.reason_code,q.requested_at,q.deadline FROM workflow_stop_requests q "
                "JOIN workflow_steps s USING(owner_id,workspace_id,workflow_id,step_key,generation) "
                "WHERE q.owner_id=? AND q.workflow_id=? AND s.is_current=1 AND s.state='cancelling' ORDER BY s.position LIMIT 8",
                (claim.owner_id, claim.workflow_id),
            ).fetchall()
            return tuple(WorkflowStopRequest(*row) for row in rows)
