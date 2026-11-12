"""Short SQLite transactions for fenced artifact and validator publication."""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace

from hub.application.workflow_templates import coding_delivery_definition, validate_coding_test_contract, validate_coding_validation_contract
from hub.domain.workflow_contract import parse_contract, parse_definition, parse_resolved
from hub.domain.workflow_contract_types import ArtifactReference, CodeRevisionReference, WorkContract
from hub.domain.workflow_delivery import (
    DeliveryContext, DeliveryOutput, DeliveryReceipt, DeliveryRecord, DeliveryRunLease, DeliveryTaskLease, DeliveryTarget, delivery_operation_token,
)
from hub.domain.workflow_execution import ExecutionBinding
from hub.domain.workflow_projection import project_workflow
from hub.domain.workflow_identity import canonical_digest, canonical_json
from hub.domain.workflow_json import decode_snapshot
from hub.domain.workflow_review import ReviewAdapterSpec
from hub.domain.remote_workflow_test import (REMOTE_TEST_MARKER, RemoteTestError, RemoteTestRequest, validate_remote_test_config)
from .workflow_repository import WorkflowRepository, WorkflowRepositoryError, _time


def _scope(target: DeliveryTarget) -> tuple[str, str, str, str, int]:
    return (target.binding.owner_id, target.binding.workspace_id, target.workflow_id, target.step_key, target.generation)


def _receipt_value(receipt: DeliveryReceipt) -> dict[str, object]:
    return {"revision": receipt.revision, "subject_digest": receipt.subject_digest, "evidence_key": receipt.evidence_key,
            "outputs": [{"output_key": item.output_key, "kind": item.kind, "artifact_id": item.artifact.artifact_id,
                         "sha256": item.artifact.sha256, "size": item.artifact.size} for item in receipt.outputs]}


def _receipt(raw: bytes) -> DeliveryReceipt:
    value = decode_snapshot(raw)
    return DeliveryReceipt(value["revision"], value["subject_digest"], tuple(
        DeliveryOutput(item["output_key"], item["kind"], ArtifactReference(item["artifact_id"], item["sha256"], item["size"]))
        for item in value["outputs"]), value["evidence_key"])


class WorkflowDeliveryRepository:
    def __init__(self, repository: WorkflowRepository, task_repository=None) -> None:
        self.repository = repository
        self.task_repository = task_repository

    def target_for_task(self, origin: dict, lease: DeliveryTaskLease, *, now: float, current: bool = True) -> tuple[DeliveryTarget, str]:
        from hub.application.legacy_task_bridge import legacy_client_token

        with self.repository.transaction(write=False) as connection:
            rows = connection.execute("SELECT d.*,w.revision,r.config_snapshot,l.request FROM workflow_dispatches d "
                "JOIN workflow_runs w USING(owner_id,workspace_id,workflow_id) "
                "JOIN runs r ON r.owner_id=d.owner_id AND r.run_id=d.binding_run_id "
                "JOIN legacy_task_links l ON l.owner_id=r.owner_id AND l.run_id=r.run_id "
                "WHERE d.owner_id=? AND d.binding_run_id=? AND l.task_id=? LIMIT 2", (lease.owner_id, lease.run_id, lease.task_id)).fetchall()
            if len(rows) != 1:
                raise WorkflowRepositoryError("binding_conflict")
            row = rows[0]
            marker = decode_snapshot(row["config_snapshot"].encode()).get("workflow_source")
            dispatch = decode_snapshot(row["config_snapshot"].encode()).get("workflow_dispatch")
            if type(marker) is not dict or set(marker) != {"repository_id", "revision"} or type(marker["revision"]) is not int or (
                    marker["revision"] != 1 or marker["repository_id"] != origin["repository_id"]):
                raise WorkflowRepositoryError("binding_conflict")
            request = decode_snapshot(row["request"].encode())
            if origin["requested_by"] != lease.owner_id or origin["client_token"] != legacy_client_token(lease.owner_id, lease.run_id) or any(
                    request.get(key) != origin[key] for key in ("machine", "project", "agent_type", "instruction")):
                raise WorkflowRepositoryError("binding_conflict")
            if type(dispatch) is not dict or (dispatch.get("token"), dispatch.get("payload_hash")) != (row["token"], row["payload_hash"]):
                raise WorkflowRepositoryError("binding_conflict")
            target = DeliveryTarget(row["workflow_id"], row["step_key"], row["generation"], row["revision"],
                ExecutionBinding(lease.owner_id, row["workspace_id"], row["target_key"], row["token"], row["payload_hash"],
                                 "run", lease.run_id, lease.run_id), task_lease=lease)
            if target.step_key != "develop":
                raise WorkflowRepositoryError("binding_conflict")
            self._check_binding(row, target)
            if current:
                self._current(connection, target, now=now)
            return target, marker["repository_id"]

    def reserve_source(self, target: DeliveryTarget, artifact_id: str, sha256: str, size: int, *, now: float) -> tuple[DeliveryTarget, str]:
        lease = target.task_lease
        if lease is None:
            raise WorkflowRepositoryError("invalid_delivery_lease")
        with self.repository.transaction() as connection:
            prior = connection.execute("SELECT * FROM workflow_source_uploads WHERE owner_id=? AND run_id=? AND attempt_id=?",
                                      (lease.owner_id, lease.run_id, lease.attempt_id)).fetchone()
            if prior is not None:
                if (prior["task_id"], prior["machine"], prior["sha256"], prior["size"], prior["workflow_id"], prior["generation"]) != (
                        lease.task_id, lease.machine, sha256, size, target.workflow_id, target.generation):
                    raise WorkflowRepositoryError("source_upload_conflict")
                return replace(target, expected_revision=prior["expected_revision"]), prior["artifact_id"]
            self._current(connection, target, now=now)
            connection.execute("INSERT INTO workflow_source_uploads VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*_scope(target), lease.run_id, lease.task_id, lease.attempt_id, lease.machine, artifact_id, sha256, size, target.expected_revision, now))
            return target, artifact_id

    def refresh_source(self, target: DeliveryTarget, *, now: float) -> DeliveryTarget:
        lease = target.task_lease
        if lease is None:
            raise WorkflowRepositoryError("invalid_delivery_lease")
        with self.repository.transaction() as connection:
            previous = connection.execute("SELECT 1 FROM workflow_operations WHERE owner_id=? AND workflow_id=? AND client_token=?",
                (lease.owner_id, target.workflow_id, delivery_operation_token(target, "source"))).fetchone()
            if previous is not None:
                return target
            revision = connection.execute("SELECT revision FROM workflow_runs WHERE owner_id=? AND workflow_id=?",
                                         (lease.owner_id, target.workflow_id)).fetchone()
            if revision is None:
                raise WorkflowRepositoryError("binding_conflict")
            refreshed = replace(target, expected_revision=revision[0])
            self._current(connection, refreshed, now=now)
            connection.execute("UPDATE workflow_source_uploads SET expected_revision=? WHERE owner_id=? AND run_id=? AND attempt_id=?",
                               (revision[0], lease.owner_id, lease.run_id, lease.attempt_id))
            return refreshed

    def source_completed(self, target: DeliveryTarget) -> bool:
        with self.repository.transaction(write=False) as connection:
            return connection.execute("SELECT 1 FROM workflow_operations WHERE owner_id=? AND workflow_id=? AND client_token=?",
                (target.binding.owner_id, target.workflow_id, delivery_operation_token(target, "source"))).fetchone() is not None

    @staticmethod
    def _previous(connection: sqlite3.Connection, target: DeliveryTarget, operation: str, request_digest: str) -> DeliveryReceipt | None:
        prior = connection.execute("SELECT request_digest,receipt FROM workflow_operations WHERE owner_id=? AND workflow_id=? AND client_token=?",
            (target.binding.owner_id, target.workflow_id, delivery_operation_token(target, operation))).fetchone()
        if prior is None:
            return None
        if prior["request_digest"] != request_digest:
            raise WorkflowRepositoryError("idempotency_conflict")
        return _receipt(bytes(prior["receipt"]))

    def receipt(self, target: DeliveryTarget, operation: str, request_digest: str) -> DeliveryReceipt | None:
        with self.repository.transaction(write=False) as connection:
            return self._previous(connection, target, operation, request_digest)

    def recover_source(self, target: DeliveryTarget, request_digest: str, *, now: float) -> DeliveryReceipt | None:
        """Repair only the Task half of an already committed source delivery.

        No new artifact, Workflow write, lease renewal or state transition is
        allowed here. A late cancellation/expiry does not erase that receipt.
        """
        prior = self.receipt(target, "source", request_digest)
        if prior is None or target.task_lease is None:
            return prior
        if self.task_repository is None:
            raise WorkflowRepositoryError("invalid_delivery_lease")
        lease = target.task_lease
        with self.task_repository.source_fence(lease.machine, lease.attempt_id, lease.nonce, now=now, recovered=True) as fence:
            if (fence["task_id"], fence["origin_owner_id"], fence["origin_run_id"]) != (lease.task_id, lease.owner_id, lease.run_id):
                raise WorkflowRepositoryError("binding_conflict")
            self._task_source_receipt(target, prior, fence["_connection"], now=now, recovered=True)
        return prior

    def _task_source_receipt(self, target: DeliveryTarget, receipt: DeliveryReceipt, connection: sqlite3.Connection,
                             *, now: float, recovered: bool = False) -> None:
        output = next((item for item in receipt.outputs if item.output_key == "source_bundle"), None)
        if output is None:
            raise WorkflowRepositoryError("invalid_source_receipt")
        lease, artifact = target.task_lease, output.artifact
        self.task_repository.record_source_receipt(task_id=lease.task_id, attempt_id=lease.attempt_id,
            artifact_id=artifact.artifact_id, sha256=artifact.sha256, size=artifact.size, now=now,
            connection=connection, recovered=recovered)

    @staticmethod
    def _run_lease(connection: sqlite3.Connection, lease: DeliveryRunLease, now: float, *, allow_cancellation: bool = False) -> sqlite3.Row:
        row = connection.execute("SELECT r.owner_id,r.conversation_id,r.trigger_message_id,r.state,r.config_snapshot,r.cancel_requested,r.lease_id,r.lease_owner,r.attempt,"
            "r.lease_expires_at,w.enabled FROM runs r JOIN conversations c USING(owner_id,conversation_id) "
            "JOIN workspaces w ON w.owner_id=c.owner_id AND w.workspace_id=c.workspace_id WHERE r.owner_id=? AND r.run_id=?",
            (lease.owner_id, lease.run_id)).fetchone()
        if row is None or (row["lease_id"], row["lease_owner"], row["attempt"]) != (lease.lease_id, lease.worker_id, lease.attempt) or (
                row["lease_expires_at"] is None or row["lease_expires_at"] <= now):
            raise WorkflowRepositoryError("run_lease_mismatch")
        if row["state"] not in (("running", "cancelling") if allow_cancellation else ("running",)) or (
                row["cancel_requested"] and not allow_cancellation):
            raise WorkflowRepositoryError("delivery_execution_unavailable")
        if row["enabled"] != 1:
            raise WorkflowRepositoryError("reference_forbidden")
        return row

    def target_for_run(self, lease: DeliveryRunLease, *, operation: str, now: float, remote_test: bool = False) -> DeliveryTarget:
        if type(lease) is not DeliveryRunLease or operation not in ("test", "review"):
            raise WorkflowRepositoryError("invalid_delivery_lease")
        now = _time(now)
        with self.repository.transaction(write=False) as connection:
            run = self._run_lease(connection, lease, now)
            rows = connection.execute("SELECT d.*,w.revision FROM workflow_dispatches d JOIN workflow_runs w "
                "USING(owner_id,workspace_id,workflow_id) WHERE d.owner_id=? AND d.binding_run_id=? LIMIT 2",
                (lease.owner_id, lease.run_id)).fetchall()
            if len(rows) != 1:
                raise WorkflowRepositoryError("binding_conflict")
            row = rows[0]
            config = decode_snapshot(run["config_snapshot"].encode())
            marker = config.get("workflow_dispatch")
            if operation == "test":
                recipe = config.get("workflow_test")
                if type(recipe) is not dict or type(recipe.get("revision")) is not int or recipe != {"recipe_id": "coding-delivery-tests", "revision": 1}:
                    raise WorkflowRepositoryError("binding_conflict")
            else:
                ReviewAdapterSpec.parse(config.get("workflow_review"))
            remote = operation == "test" and REMOTE_TEST_MARKER in config
            if remote != remote_test:
                raise WorkflowRepositoryError("binding_conflict")
            if remote:
                from .capability_dispatch import CapabilityDispatchRepository, pinned_dispatch

                pinned = pinned_dispatch(connection, row["token"])
                if pinned is None:
                    raise WorkflowRepositoryError("binding_conflict")
                try:
                    validate_remote_test_config(config, node_id=row["target_key"], binding_digest=hashlib.sha256(pinned.binding.snapshot).hexdigest())
                    CapabilityDispatchRepository._run_configuration(connection, run, config, pinned)
                except (RemoteTestError, ValueError) as exc:
                    raise WorkflowRepositoryError("binding_conflict") from exc
            if (not remote and set(config) != {"owner_id", "workspace_id", "workflow_dispatch", "workflow_" + operation}) or (
                    config["owner_id"], config["workspace_id"]) != (lease.owner_id, row["workspace_id"]):
                raise WorkflowRepositoryError("binding_conflict")
            if type(marker) is not dict or set(marker) != {"token", "payload_hash", "config_digest"} or (
                    marker.get("token"), marker.get("payload_hash")) != (row["token"], row["payload_hash"]):
                raise WorkflowRepositoryError("binding_conflict")
            if row["stage"] == "sending" and row["binding_kind"] is None and row["binding_id"] is None:
                raise WorkflowRepositoryError("binding_not_ready")
            binding = ExecutionBinding(lease.owner_id, row["workspace_id"], row["target_key"], row["token"], row["payload_hash"],
                                       "run", lease.run_id, lease.run_id)
            target = DeliveryTarget(row["workflow_id"], row["step_key"], row["generation"], row["revision"], binding, lease)
            if target.step_key != operation:
                raise WorkflowRepositoryError("binding_conflict")
            self._current(connection, target, now=now)
            prior = connection.execute("SELECT receipt FROM workflow_operations WHERE owner_id=? AND workflow_id=? AND client_token=?",
                (lease.owner_id, target.workflow_id, delivery_operation_token(target, operation))).fetchone()
            return target if prior is None else replace(target, expected_revision=_receipt(bytes(prior["receipt"])).revision - 1)

    def target_for_remote_test(self, lease: DeliveryRunLease, *, now: float) -> DeliveryTarget:
        return self.target_for_run(lease, operation="test", now=now, remote_test=True)

    def remote_test_request(self, target: DeliveryTarget, *, source_revision: CodeRevisionReference, subject_digest: str,
                            now: float) -> RemoteTestRequest:
        from hub.domain.artifact_receive import receive_grant_id
        from .artifact_input_repository import node_input_receipts_ready
        from .capability_dispatch import pinned_dispatch

        with self.repository.transaction(write=False) as connection:
            self._current(connection, target, now=_time(now))
            pinned = pinned_dispatch(connection, target.binding.token)
            if pinned is None or pinned.resolved.contract.input_refs[0].reference != source_revision:
                raise WorkflowRepositoryError("binding_conflict")
            if not node_input_receipts_ready(connection, owner=target.binding.owner_id, workflow=target.workflow_id,
                    generation=target.generation, resolved=pinned.resolved, target=target.binding.target_key,
                    public_key=self.repository.artifact_input_public_key):
                raise WorkflowRepositoryError("input_receipt_required")
            grant = receive_grant_id(target.binding.owner_id, target.workflow_id, "test", target.generation,
                target.binding.target_key, "source")
            row = connection.execute("SELECT result FROM platform_commands WHERE command_id=?", ("receive-" + grant,)).fetchone()
            return RemoteTestRequest(target.workflow_id, target.generation, target.binding.token, target.binding.payload_hash,
                hashlib.sha256(pinned.binding.snapshot).hexdigest(), source_revision, subject_digest, decode_snapshot(row[0].encode()))

    def target_for_review(self, lease: DeliveryRunLease, *, now: float) -> tuple[DeliveryTarget, ReviewAdapterSpec]:
        target = self.target_for_run(lease, operation="review", now=now)
        with self.repository.transaction(write=False) as connection:
            run = self._run_lease(connection, lease, _time(now))
            spec = ReviewAdapterSpec.parse(decode_snapshot(run["config_snapshot"].encode()).get("workflow_review"))
        return target, spec

    @staticmethod
    def _check_binding(row: sqlite3.Row | None, target: DeliveryTarget) -> sqlite3.Row:
        if row is None:
            raise WorkflowRepositoryError("generation_conflict")
        binding = target.binding
        if (row["stage"], row["token"], row["payload_hash"], row["target_key"], row["binding_kind"], row["binding_id"], row["binding_run_id"]) != (
                "bound", binding.token, binding.payload_hash, binding.target_key, binding.kind, binding.execution_id, binding.run_id):
            raise WorkflowRepositoryError("binding_conflict")
        return row

    def check_run(self, target: DeliveryTarget, *, now: float) -> bool:
        """Return cancellation only after checking current execution ownership."""
        if type(target) is not DeliveryTarget or target.run_lease is None or target.step_key not in ("test", "review"):
            raise WorkflowRepositoryError("invalid_delivery_lease")
        now = _time(now)
        with self.repository.transaction(write=False) as connection:
            run = self._run_lease(connection, target.run_lease, now, allow_cancellation=True)
            row = connection.execute("SELECT s.state,s.active_deadline,s.resolved_digest,w.cancel_requested,"
                "d.stage,d.token,d.payload_hash,d.target_key,d.binding_kind,d.binding_id,d.binding_run_id "
                "FROM workflow_steps s JOIN workflow_runs w USING(owner_id,workspace_id,workflow_id) "
                "LEFT JOIN workflow_dispatches d USING(owner_id,workspace_id,workflow_id,step_key,generation) "
                "WHERE s.owner_id=? AND s.workspace_id=? AND s.workflow_id=? AND s.step_key=? AND s.generation=? AND s.is_current=1",
                _scope(target)).fetchone()
            row = self._check_binding(row, target)
            if run["cancel_requested"] or run["state"] == "cancelling" or row["cancel_requested"]:
                return True
            if row["state"] not in ("running", "waiting_external"):
                raise WorkflowRepositoryError("delivery_execution_unavailable")
            if row["resolved_digest"] != target.binding.payload_hash:
                raise WorkflowRepositoryError("binding_conflict")
            if connection.execute("SELECT admission_mode FROM workflow_schema_meta WHERE singleton=1").fetchone()[0] == "frozen":
                raise WorkflowRepositoryError("admission_frozen")
            if row["active_deadline"] is None or now >= row["active_deadline"]:
                raise WorkflowRepositoryError(target.step_key + "_active_deadline_exceeded")
            return False

    def _test_predecessors(self, connection: sqlite3.Connection, target: DeliveryTarget) -> None:
        snapshot = self.repository._snapshots(connection, (target.workflow_id,))[0]
        definition = parse_definition(snapshot.workflow.definition_snapshot)
        if (definition.template_id, definition.revision) != ("coding-delivery", 1) or tuple(
                item.step_key for item in snapshot.steps) != ("develop", "review", "test"):
            raise WorkflowRepositoryError("invalid_test_contract")
        source, review, test = snapshot.steps
        if source.state != "succeeded" or review.state != "succeeded" or source.subject_digest is None or review.subject_digest != source.subject_digest:
            raise WorkflowRepositoryError("test_predecessor_unavailable")
        projection = project_workflow(definition, snapshot)
        if projection.step_validations[:2] != ("not_requested", "passed") or not {"source", "source_bundle"} <= set(source.output_keys) or "review_report" not in review.output_keys:
            raise WorkflowRepositoryError("test_predecessor_unavailable")
        if test.resolved_snapshot is None:
            raise WorkflowRepositoryError("input_resolution_required")
        validate_coding_test_contract(parse_resolved(test.resolved_snapshot))

    def _review_predecessors(self, connection: sqlite3.Connection, target: DeliveryTarget) -> None:
        snapshot = self.repository._snapshots(connection, (target.workflow_id,))[0]
        definition = parse_definition(snapshot.workflow.definition_snapshot)
        if (definition.template_id, definition.revision) != ("coding-delivery", 1) or tuple(
                item.step_key for item in snapshot.steps) != ("develop", "review", "test"):
            raise WorkflowRepositoryError("invalid_review_contract")
        source, review, _ = snapshot.steps
        if source.state != "succeeded" or source.subject_digest is None or not {"source", "source_bundle"} <= set(source.output_keys):
            raise WorkflowRepositoryError("review_predecessor_unavailable")
        if review.resolved_snapshot is None:
            raise WorkflowRepositoryError("input_resolution_required")
        validate_coding_validation_contract(parse_resolved(review.resolved_snapshot), "review")

    def _current(self, connection: sqlite3.Connection, target: DeliveryTarget, *, now: float | None = None) -> sqlite3.Row:
        row = connection.execute(
            "SELECT s.state,s.subject_digest,s.contract_snapshot,s.resolved_snapshot,s.resolved_digest,w.revision,w.cancel_requested,"
            "w.acceptance_required,s.active_deadline,d.stage,d.token,d.payload_hash,d.target_key,d.binding_kind,d.binding_id,d.binding_run_id "
            "FROM workflow_steps s JOIN workflow_runs w USING(owner_id,workspace_id,workflow_id) "
            "LEFT JOIN workflow_dispatches d USING(owner_id,workspace_id,workflow_id,step_key,generation) "
            "WHERE s.owner_id=? AND s.workspace_id=? AND s.workflow_id=? AND s.step_key=? AND s.generation=? AND s.is_current=1", _scope(target)).fetchone()
        row = self._check_binding(row, target)
        if row["revision"] != target.expected_revision:
            raise WorkflowRepositoryError("revision_conflict")
        if row["cancel_requested"] or row["state"] not in ("running", "waiting_external", "succeeded"):
            raise WorkflowRepositoryError("delivery_execution_unavailable")
        if connection.execute("SELECT admission_mode FROM workflow_schema_meta WHERE singleton=1").fetchone()[0] == "frozen":
            raise WorkflowRepositoryError("admission_frozen")
        if row["resolved_snapshot"] is None:
            raise WorkflowRepositoryError("input_resolution_required")
        if target.binding.kind == "run" and target.step_key in ("test", "review") and target.run_lease is None:
            raise WorkflowRepositoryError("invalid_delivery_lease")
        if target.run_lease is not None:
            checked_at = _time(now)
            self._run_lease(connection, target.run_lease, checked_at)
            if row["active_deadline"] is None or checked_at >= row["active_deadline"]:
                raise WorkflowRepositoryError(target.step_key + "_active_deadline_exceeded")
            if target.step_key == "test":
                self._test_predecessors(connection, target)
            elif target.step_key == "review":
                self._review_predecessors(connection, target)
            else:
                raise WorkflowRepositoryError("invalid_delivery_lease")
        if target.task_lease is not None:
            lease = target.task_lease
            run = connection.execute("SELECT r.state,r.cancel_requested,w.enabled FROM runs r JOIN conversations c USING(owner_id,conversation_id) "
                "JOIN workspaces w ON w.owner_id=c.owner_id AND w.workspace_id=c.workspace_id WHERE r.owner_id=? AND r.run_id=?",
                (lease.owner_id, lease.run_id)).fetchone()
            if run is None or run["state"] not in ("waiting_task", "running", "succeeded") or run["cancel_requested"] or run["enabled"] != 1:
                raise WorkflowRepositoryError("delivery_execution_unavailable")
            if row["active_deadline"] is None or _time(now) >= row["active_deadline"]:
                raise WorkflowRepositoryError("source_active_deadline_exceeded")
            if parse_contract(bytes(row["contract_snapshot"])) != coding_delivery_definition().steps[0].contract:
                raise WorkflowRepositoryError("invalid_source_contract")
        return row

    @staticmethod
    def _source(connection: sqlite3.Connection, target: DeliveryTarget) -> tuple[int | None, str | None, tuple[DeliveryOutput, ...], bytes]:
        source = connection.execute("SELECT generation,subject_digest,contract_snapshot FROM workflow_steps "
            "WHERE owner_id=? AND workspace_id=? AND workflow_id=? AND position=0 AND is_current=1", _scope(target)[:3]).fetchone()
        if source is None:
            raise WorkflowRepositoryError("source_unavailable")
        contract = parse_contract(bytes(source["contract_snapshot"]))
        value = decode_snapshot(contract.snapshot)
        semantics = canonical_json({"input_refs": value["input_refs"], "context_refs": value["context_refs"]})
        rows = connection.execute("SELECT output_key,artifact_id,sha256,size FROM workflow_artifact_refs "
            "WHERE owner_id=? AND workspace_id=? AND workflow_id=? AND step_key='develop' AND generation=? "
            "AND output_key IN ('output:source','output:source_bundle') ORDER BY output_key LIMIT 2",
            (*_scope(target)[:3], source["generation"])).fetchall()
        outputs = tuple(DeliveryOutput(item["output_key"][7:], "code_revision" if item["output_key"] == "output:source" else "artifact",
            ArtifactReference(item["artifact_id"], item["sha256"], item["size"])) for item in rows)
        return source["generation"], source["subject_digest"], outputs, semantics

    def context(self, target: DeliveryTarget, *, now: float | None = None) -> DeliveryContext:
        with self.repository.transaction(write=False) as connection:
            row = self._current(connection, target, now=now)
            generation, subject, outputs, semantics = self._source(connection, target)
            resolved = parse_resolved(bytes(row["resolved_snapshot"]))
            if resolved.digest != row["resolved_digest"]:
                raise WorkflowRepositoryError("invalid_resolved_digest")
            reports: tuple[ArtifactReference, ...] = ()
            if target.run_lease is not None and target.step_key == "test":
                evidence = connection.execute("SELECT e.report_artifact_id,e.report_hash,e.report_size FROM workflow_evidence e "
                    "JOIN workflow_steps s USING(owner_id,workspace_id,workflow_id,step_key,generation) "
                    "WHERE e.owner_id=? AND e.workflow_id=? AND e.step_key='review' AND e.is_current=1 AND s.is_current=1 "
                    "AND e.validator_ref='review-report@1' AND e.subject_digest=? AND e.result='passed' LIMIT 2",
                    (target.binding.owner_id, target.workflow_id, subject)).fetchall()
                if len(evidence) != 1:
                    raise WorkflowRepositoryError("test_predecessor_unavailable")
                reports = tuple(ArtifactReference(item["report_artifact_id"], item["report_hash"], item["report_size"]) for item in evidence)
            return DeliveryContext(parse_contract(bytes(row["contract_snapshot"])), resolved, generation, subject, outputs, semantics, row["active_deadline"], reports)

    def commit(self, target: DeliveryTarget, operation: str, request_digest: str, record: DeliveryRecord, *, now: float) -> DeliveryReceipt:
        lease = target.task_lease
        if lease is None:
            return self._commit(target, operation, request_digest, record, now=now)
        if self.task_repository is None:
            raise WorkflowRepositoryError("invalid_delivery_lease")
        with self.task_repository.source_fence(lease.machine, lease.attempt_id, lease.nonce, now=now) as task_fence:
            if (task_fence["task_id"], task_fence["origin_owner_id"], task_fence["origin_run_id"]) != (lease.task_id, lease.owner_id, lease.run_id):
                raise WorkflowRepositoryError("binding_conflict")
            receipt = self._commit(target, operation, request_digest, record, now=now)
            if operation == "source":
                self._task_source_receipt(target, receipt, task_fence["_connection"], now=now)
            return receipt

    def _commit(self, target: DeliveryTarget, operation: str, request_digest: str, record: DeliveryRecord, *, now: float) -> DeliveryReceipt:
        now = _time(now)
        with self.repository.transaction() as connection:
            prior = self._previous(connection, target, operation, request_digest)
            if prior is not None:
                return prior
            row = self._current(connection, target, now=now)
            if operation == "review" and target.run_lease is not None:
                run = self._run_lease(connection, target.run_lease, now)
                spec = ReviewAdapterSpec.parse(decode_snapshot(run["config_snapshot"].encode()).get("workflow_review"))
                if record.evidence is None or record.evidence.source != "review:" + canonical_digest(spec.executor.payload()):
                    raise WorkflowRepositoryError("executor_identity_mismatch")
            if row["subject_digest"] not in (None, record.subject_digest):
                raise WorkflowRepositoryError("subject_conflict")
            if record.source_generation is not None:
                source_generation, source_subject, _, _ = self._source(connection, target)
                if (source_generation, source_subject) != (record.source_generation, record.subject_digest):
                    raise WorkflowRepositoryError("subject_conflict")
            contract = parse_contract(bytes(row["contract_snapshot"]))
            specs = {item.output_key: item for item in contract.output_specs}
            for output in record.outputs:
                spec = specs.get(output.output_key)
                if spec is None or spec.kind != output.kind or spec.max_bytes is not None and output.artifact.size > spec.max_bytes:
                    raise WorkflowRepositoryError("output_not_declared")
                artifact = output.artifact
                previous = connection.execute("SELECT artifact_id,sha256,size FROM workflow_artifact_refs WHERE owner_id=? AND workspace_id=? "
                    "AND workflow_id=? AND step_key=? AND generation=? AND output_key=?", (*_scope(target), "output:" + output.output_key)).fetchone()
                if previous is not None and tuple(previous) != (artifact.artifact_id, artifact.sha256, artifact.size):
                    raise WorkflowRepositoryError("output_conflict")
                if previous is None:
                    connection.execute("INSERT INTO workflow_artifact_refs VALUES(?,?,?,?,?,?,?,?,?)",
                        (*_scope(target), "output:" + output.output_key, artifact.artifact_id, artifact.sha256, artifact.size))
            evidence_key = self._evidence(connection, target, record, contract, now)
            connection.execute("UPDATE workflow_steps SET subject_digest=?,updated_at=? WHERE owner_id=? AND workspace_id=? AND workflow_id=? AND step_key=? AND generation=?",
                (record.subject_digest, now, *_scope(target)))
            connection.execute("UPDATE workflow_runs SET revision=revision+1,next_poll_at=?,updated_at=?,delivery_digest=NULL,"
                "acceptance_state=CASE WHEN acceptance_required=1 THEN 'pending' ELSE 'not_requested' END WHERE owner_id=? AND workflow_id=?",
                (now, now, target.binding.owner_id, target.workflow_id))
            receipt = DeliveryReceipt(target.expected_revision + 1, record.subject_digest, record.outputs, evidence_key)
            connection.execute("INSERT INTO workflow_operations VALUES(?,?,?,?,?,?,?)", (*_scope(target)[:3],
                delivery_operation_token(target, operation), request_digest, canonical_json(_receipt_value(receipt)), now))
            self.repository._event(connection, target.binding.owner_id, target.workflow_id, "step.delivery_recorded",
                {"step_key": target.step_key, "generation": target.generation, "subject_digest": record.subject_digest,
                 "output_keys": [item.output_key for item in record.outputs], "evidence_key": evidence_key}, now)
            self.repository._refresh_owner_queue(connection, target.binding.owner_id)
            return receipt

    @staticmethod
    def _evidence(connection: sqlite3.Connection, target: DeliveryTarget, record: DeliveryRecord, contract: WorkContract, now: float) -> str | None:
        evidence = record.evidence
        if evidence is None:
            return None
        if evidence.validator_ref not in {f"{item.id}@{item.revision}" for item in contract.validator_refs}:
            raise WorkflowRepositoryError("validator_not_declared")
        key = "ev:" + canonical_digest({"step": target.step_key, "generation": target.generation, "validator": evidence.validator_ref})
        if connection.execute("SELECT 1 FROM workflow_evidence WHERE owner_id=? AND workflow_id=? AND evidence_key=?",
                (target.binding.owner_id, target.workflow_id, key)).fetchone() is not None:
            raise WorkflowRepositoryError("evidence_conflict")
        connection.execute("INSERT INTO workflow_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)", (*_scope(target), key,
            evidence.validator_ref, record.subject_digest, evidence.source, evidence.result, evidence.report.artifact_id,
            evidence.report.sha256, evidence.report.size, evidence.content_digest, now))
        return key
