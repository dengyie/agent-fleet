"""Scoped, bounded operator queries and versioned acceptance transactions."""
from __future__ import annotations

import base64
import binascii
import sqlite3
from typing import cast, overload

from hub.application.workflow_operator import (
    WorkflowAcceptanceDTO, WorkflowBudgetDTO, WorkflowDetailDTO, WorkflowEvidenceDTO, WorkflowEventsDTO,
    WorkflowOutputDTO, WorkflowPageDTO, WorkflowStepDTO, WorkflowSummaryDTO, utc_timestamp,
)
from hub.domain.workflow_contract import parse_definition
from hub.domain.workflow_evidence import delivery_state
from hub.domain.workflow_identity import canonical_digest, canonical_json
from hub.domain.workflow_json import decode_snapshot
from hub.domain.workflow_operator import WorkflowAcceptanceRequest, WorkflowCreateRequest
from hub.domain.workflow_contract_types import WorkflowDefinition
from hub.domain.integration_identity import IntegrationIdentity, IntegrationIdentityError
from hub.domain.workflow_projection import project_workflow
from .workflow_repository import WorkflowRepository, WorkflowRepositoryError, _id, _integer, _time
from .workflow_inputs import WorkflowArtifactInputAuthorizer
from .integration_identity_repository import IntegrationIdentityRepository

_SUMMARY = ("workflow_id,workspace_id,json_extract(definition_snapshot,'$.template_id') AS template_id,"
            "json_extract(definition_snapshot,'$.revision') AS template_revision,execution_state,validation_state,acceptance_state,"
            "revision,reason_code,created_at,updated_at")
_EVENT_FIELDS = frozenset({"step_key", "generation", "reason_code", "execution_state", "validation_state", "state", "decision",
    "definition_digest", "resolved_digest", "subject_digest", "delivery_digest", "evidence_key", "output_keys", "generations",
    "unknown_execution", "result", "settlement", "action", "reason", "evidence_source"})


@overload
def _count(value: int) -> int | str: ...


@overload
def _count(value: None) -> None: ...


def _count(value: int | None) -> int | str | None:
    return str(value) if value is not None and value > 9007199254740991 else value


def _event_payload(raw: bytes) -> dict[str, object]:
    value = decode_snapshot(raw)
    public = {key: item for key, item in value.items() if key in _EVENT_FIELDS}
    for source, target in (("tokens", "charged_tokens"), ("requests", "charged_requests")):
        if source in value:
            public[target] = _count(value[source])
    return public


def _summary(row: sqlite3.Row) -> WorkflowSummaryDTO:
    return {"workflow_id": row["workflow_id"], "workspace_id": row["workspace_id"], "template_id": row["template_id"],
            "template_revision": row["template_revision"], "execution_state": row["execution_state"],
            "validation_state": row["validation_state"], "acceptance_state": row["acceptance_state"],
            "revision": row["revision"], "reason_code": row["reason_code"], "created_at": utc_timestamp(row["created_at"]),
            "updated_at": utc_timestamp(row["updated_at"])}


def _encode_cursor(owner: str, before: str) -> str:
    return base64.urlsafe_b64encode(canonical_json({"v": 1, "owner": canonical_digest(owner), "before": before})).decode().rstrip("=")


def _decode_cursor(owner: str, cursor: str | None) -> str | None:
    if cursor is None:
        return None
    if type(cursor) is not str or not 1 <= len(cursor) <= 512:
        raise WorkflowRepositoryError("invalid_cursor")
    try:
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        value = decode_snapshot(raw)
        before = _id(value["before"])
        if value != {"v": 1, "owner": canonical_digest(owner), "before": before} or _encode_cursor(owner, before) != cursor:
            raise WorkflowRepositoryError("invalid_cursor")
        return before
    except (ValueError, KeyError, binascii.Error) as exc:
        raise WorkflowRepositoryError("invalid_cursor") from exc


class WorkflowOperatorRepository:
    def __init__(self, repository: WorkflowRepository, input_authorizer: WorkflowArtifactInputAuthorizer) -> None:
        self.repository = repository
        self.input_authorizer = input_authorizer

    @staticmethod
    def _authorize_integration(connection: sqlite3.Connection, integration: IntegrationIdentity | None,
                               request: WorkflowCreateRequest, definition: WorkflowDefinition) -> None:
        if integration is None:
            return
        identity = IntegrationIdentityRepository.require_active(connection, integration, scope="workflow.create_from_template")
        identity.authorize_template(request.template_id, request.workspace_id)
        if (request.owner_id != identity.owner_id or not request.client_token.startswith(identity.token_namespace)
                or (definition.template_id, definition.revision) != (request.template_id, request.template_revision)
                or definition.steps[0].contract.policy_ref.revision != request.expected_policy_revision):
            raise IntegrationIdentityError("scope_forbidden")
        if (sum(step.contract.budget.max_tokens for step in definition.steps) > identity.max_budget_tokens
                or sum(step.contract.budget.max_model_requests for step in definition.steps) > identity.max_budget_requests):
            raise IntegrationIdentityError("budget_forbidden")

    def create(self, request: WorkflowCreateRequest, definition: WorkflowDefinition, *, workflow_id: str, now: float,
               integration: IntegrationIdentity | None = None) -> WorkflowSummaryDTO:
        # A lost response remains queryable after input revocation. This read
        # performs the repository's same-payload check without new admission.
        with self.repository.transaction(write=False) as connection:
            self._authorize_integration(connection, integration, request, definition)
            previous = connection.execute("SELECT workflow_id FROM workflow_runs WHERE owner_id=? AND client_token=?",
                                          (request.owner_id, request.client_token)).fetchone()
            if previous is not None:
                workflow = self.repository.create_or_get(request.owner_id, request.workspace_id, workflow_id,
                    client_token=request.client_token, definition=definition, now=now, connection=connection)
                return _summary(connection.execute(f"SELECT {_SUMMARY} FROM workflow_runs WHERE owner_id=? AND workflow_id=?",
                                                    (request.owner_id, workflow.workflow_id)).fetchone())
        for step in definition.steps:
            self.input_authorizer.authorize_references(request.owner_id, request.workspace_id, step.contract.input_refs)
        with self.repository.transaction() as connection:
            self._authorize_integration(connection, integration, request, definition)
            mode = connection.execute("SELECT admission_mode FROM workflow_schema_meta WHERE singleton=1").fetchone()[0]
            if mode != "open":
                previous = connection.execute("SELECT 1 FROM workflow_runs WHERE owner_id=? AND client_token=?", (request.owner_id, request.client_token)).fetchone()
                if previous is None:
                    raise WorkflowRepositoryError("admission_closed")
            workflow = self.repository.create_or_get(request.owner_id, request.workspace_id, workflow_id, client_token=request.client_token,
                                                     definition=definition, now=now, connection=connection)
            return _summary(connection.execute(f"SELECT {_SUMMARY} FROM workflow_runs WHERE owner_id=? AND workflow_id=?",
                                                (request.owner_id, workflow.workflow_id)).fetchone())

    def list(self, owner: str, *, limit: int = 50, cursor: str | None = None) -> WorkflowPageDTO:
        _id(owner)
        _integer(limit, maximum=100)
        before = _decode_cursor(owner, cursor)
        clause = " AND workflow_id<?" if before is not None else ""
        parameters = (owner, before, limit) if before is not None else (owner, limit)
        with self.repository.transaction(write=False) as connection:
            rows = connection.execute(f"SELECT {_SUMMARY} FROM workflow_runs WHERE owner_id=?{clause} ORDER BY workflow_id DESC LIMIT ?", parameters).fetchall()
            return {"workflows": [_summary(row) for row in rows], "next_cursor": _encode_cursor(owner, rows[-1]["workflow_id"]) if len(rows) == limit else None}

    def _detail(self, connection: sqlite3.Connection, owner: str, workflow: str) -> WorkflowDetailDTO:
        row = connection.execute(f"SELECT {_SUMMARY},delivery_digest FROM workflow_runs WHERE owner_id=? AND workflow_id=?", (owner, workflow)).fetchone()
        if row is None:
            raise WorkflowRepositoryError("workflow_not_found")
        snapshot = self.repository._snapshots(connection, (workflow,))[0]
        definition = parse_definition(snapshot.workflow.definition_snapshot)
        projection = project_workflow(definition, snapshot)
        outputs = connection.execute(
            "SELECT a.step_key,a.generation,a.output_key,a.artifact_id,a.sha256,a.size FROM workflow_artifact_refs a "
            "JOIN workflow_steps s USING(owner_id,workspace_id,workflow_id,step_key,generation) "
            "WHERE a.owner_id=? AND a.workflow_id=? AND s.is_current=1 AND a.output_key LIKE 'output:%' ORDER BY s.position,a.output_key LIMIT 129", (owner, workflow),
        ).fetchall()
        if len(outputs) > 128:
            raise WorkflowRepositoryError("invalid_output_cardinality")
        evidence_rows = connection.execute(
            "SELECT e.step_key,e.generation,e.evidence_key,e.validator_ref,e.subject_digest,e.source,e.result,e.report_artifact_id,"
            "e.report_hash,e.report_size,e.content_digest FROM workflow_evidence e JOIN workflow_steps s USING(owner_id,workspace_id,workflow_id,step_key,generation) "
            "WHERE e.owner_id=? AND e.workflow_id=? AND s.is_current=1 AND e.is_current=1 ORDER BY s.position,e.evidence_key LIMIT 257",
            (owner, workflow),
        ).fetchall()
        if len(evidence_rows) > 256:
            raise WorkflowRepositoryError("evidence_limit")
        evidence_by_step: dict[tuple[str, int], list[WorkflowEvidenceDTO]] = {}
        for item in evidence_rows:
            evidence_by_step.setdefault((item["step_key"], item["generation"]), []).append({
                "evidence_key": item["evidence_key"], "validator_ref": item["validator_ref"],
                "subject_digest": item["subject_digest"], "source": item["source"], "result": item["result"],
                "report": {"artifact_id": item["report_artifact_id"], "sha256": item["report_hash"], "size": item["report_size"],
                            "content_digest": item["content_digest"]},
            })
        by_step: dict[str, list[WorkflowOutputDTO]] = {step.step_key: [] for step in snapshot.steps}
        for artifact in outputs:
            by_step[artifact["step_key"]].append({"output_key": artifact["output_key"][7:], "artifact_id": artifact["artifact_id"],
                                                 "sha256": artifact["sha256"], "size": artifact["size"]})
        steps: list[WorkflowStepDTO] = [{"step_key": step.step_key, "generation": step.generation, "kind": step.kind, "execution_state": step.state,
                  "validation_state": verdict, "subject_digest": step.subject_digest, "reason_code": step.reason_code,
                  "outputs": by_step[step.step_key], "evidence": evidence_by_step.get((step.step_key, step.generation), [])}
                 for step, verdict in zip(snapshot.steps, projection.step_validations)]
        digest = canonical_digest({"definition": snapshot.workflow.definition_digest, "steps": [
            {"step_key": step.step_key, "generation": step.generation, "resolved_digest": step.resolved_digest,
             "subject_digest": step.subject_digest, "outputs": canonical_digest(by_step[step.step_key]),
             "evidence": canonical_digest(sorted([[item["evidence_key"], item["validator_ref"], item["subject_digest"], item["source"], item["result"],
                                                     item["report"]["artifact_id"], item["report"]["sha256"], item["report"]["size"], item["report"]["content_digest"]]
                                                    for item in evidence_by_step.get((step.step_key, step.generation), [])]))}
            for step in snapshot.steps]})
        accepted = snapshot.workflow.acceptance_state
        if accepted in ("accepted", "rejected") and row["delivery_digest"] != digest:
            accepted = "pending" if definition.acceptance_required else "not_requested"
        delivery = delivery_state(projection.execution_state, projection.validation_state, accepted,
                                  validation_required=any(step.contract.validator_refs for step in definition.steps),
                                  acceptance_required=definition.acceptance_required, required_outputs_ready=projection.required_outputs_ready)
        budget_rows = connection.execute(
            "SELECT step_key,sum(reserved_tokens),sum(reserved_requests),sum(charged_tokens),sum(charged_requests),"
            "sum(CASE WHEN settlement!='settled' THEN 1 ELSE 0 END) FROM workflow_budget_reservations WHERE owner_id=? AND workflow_id=? GROUP BY step_key LIMIT 8",
            (owner, workflow),
        ).fetchall()
        budgets: list[WorkflowBudgetDTO] = [{"step_key": item[0], "reserved_tokens": _count(item[1]), "reserved_requests": _count(item[2]), "charged_tokens": _count(item[3]),
                    "charged_requests": _count(item[4]), "settlement": "unknown" if item[5] else "settled", "cost": None, "cost_state": "unknown"} for item in budget_rows]
        summary = _summary(row)
        summary.update(execution_state=projection.execution_state, validation_state=projection.validation_state,
                       acceptance_state=accepted, reason_code=projection.reason_code)
        return {**summary, "delivery_state": delivery, "delivery_digest": digest, "steps": steps, "budget": budgets,
                "cancel_requested": snapshot.workflow.cancel_requested, "pause_requested": snapshot.workflow.pause_requested,
                "admission_mode": snapshot.admission_mode}

    def detail(self, owner: str, workflow: str) -> WorkflowDetailDTO:
        _id(owner)
        _id(workflow)
        with self.repository.transaction(write=False) as connection:
            return self._detail(connection, owner, workflow)

    def integration_detail(self, identity: IntegrationIdentity, workflow: str, *, scope: str) -> WorkflowDetailDTO:
        owner = identity.owner_id
        _id(owner)
        _id(workflow)
        with self.repository.transaction(write=False) as connection:
            identity = IntegrationIdentityRepository.require_active(connection, identity, scope=scope)
            row = connection.execute(
                "SELECT client_token,workspace_id,json_extract(definition_snapshot,'$.template_id') AS template_id "
                "FROM workflow_runs WHERE owner_id=? AND workflow_id=?", (owner, workflow),
            ).fetchone()
            if (row is None or not row["client_token"].startswith(identity.token_namespace)
                    or row["workspace_id"] not in identity.workspace_ids or row["template_id"] not in identity.template_ids):
                raise WorkflowRepositoryError("workflow_forbidden")
            return self._detail(connection, owner, workflow)

    def events(self, owner: str, workflow: str, *, after: int = 0, limit: int = 100) -> WorkflowEventsDTO:
        page = self.repository.events(owner, workflow, after=after, limit=limit)
        return {"events": [{"sequence": event.sequence, "kind": event.kind, "payload": _event_payload(event.payload),
                            "created_at": utc_timestamp(event.created_at)} for event in page.events], "next_cursor": page.next_cursor}

    def acceptance(self, request: WorkflowAcceptanceRequest, *, now: float) -> WorkflowAcceptanceDTO:
        now = _time(now)
        digest = canonical_digest({"action": "acceptance", "revision": request.expected_revision,
                                   "decision": request.decision, "delivery_digest": request.delivery_digest})
        with self.repository.transaction() as connection:
            detail = self._detail(connection, request.owner_id, request.workflow_id)
            prior = connection.execute("SELECT request_digest,receipt FROM workflow_operations WHERE owner_id=? AND workflow_id=? AND client_token=?",
                                       (request.owner_id, request.workflow_id, request.client_token)).fetchone()
            if prior is not None:
                if prior[0] != digest:
                    raise WorkflowRepositoryError("idempotency_conflict")
                return cast(WorkflowAcceptanceDTO, decode_snapshot(bytes(prior[1])))
            if detail["revision"] != request.expected_revision or detail["delivery_digest"] != request.delivery_digest:
                raise WorkflowRepositoryError("revision_conflict")
            if detail["admission_mode"] == "frozen":
                raise WorkflowRepositoryError("admission_frozen")
            if detail["delivery_state"] not in ("awaiting_acceptance", "delivered", "rejected"):
                raise WorkflowRepositoryError("delivery_not_ready")
            connection.execute("UPDATE workflow_runs SET acceptance_state=?,delivery_digest=?,revision=revision+1,updated_at=? WHERE owner_id=? AND workflow_id=?",
                               (request.decision, request.delivery_digest, now, request.owner_id, request.workflow_id))
            receipt: WorkflowAcceptanceDTO = {"action": "acceptance", "revision": detail["revision"] + 1, "acceptance_state": request.decision,
                       "delivery_digest": request.delivery_digest}
            connection.execute("INSERT INTO workflow_operations VALUES(?,?,?,?,?,?,?)", (request.owner_id, detail["workspace_id"], request.workflow_id,
                               request.client_token, digest, canonical_json(receipt), now))
            self.repository._event(connection, request.owner_id, request.workflow_id, "workflow.acceptance_recorded",
                                   {"decision": request.decision, "delivery_digest": request.delivery_digest}, now)
            return receipt
