"""Additive workflow schema; callers execute every statement in one transaction."""
from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 1
READER_VERSION = 16
BASE_READER_VERSION = 1
TABLES = frozenset({
    "workflow_schema_meta", "workflow_runs", "workflow_steps", "workflow_dispatches",
    "workflow_events", "workflow_evidence", "workflow_artifact_refs",
    "workflow_budget_reservations", "capability_reservations",
})
CONTROL_TABLES = frozenset({"workflow_operations", "workflow_stop_requests", "workflow_reconciliation_requests", "workflow_budget_checks", "workflow_source_uploads"})
ACTIVE_STATES = "'queued','running','waiting_node','waiting_external','waiting_approval','paused','cancelling'"
SCHEDULABLE = f"(execution_state IN ({ACTIVE_STATES}) OR (execution_state='unknown' AND reason_code='reconciliation_requested'))"
AVAILABLE_AT = "max(next_poll_at,coalesce(lease_expires_at,0))"
COORDINATOR_STATEMENTS = (
    """CREATE TABLE workflow_coordinator_owners (
        owner_id TEXT PRIMARY KEY, ready_at REAL, last_served_at REAL NOT NULL DEFAULT 0,
        service_sequence INTEGER NOT NULL DEFAULT 0 CHECK(service_sequence>=0)
    )""",
    "CREATE INDEX IF NOT EXISTS workflow_coordinator_ready ON workflow_coordinator_owners(ready_at,service_sequence,owner_id)",
    "CREATE INDEX IF NOT EXISTS workflow_coordinator_sequence ON workflow_coordinator_owners(service_sequence DESC)",
    f"CREATE INDEX IF NOT EXISTS workflow_coordinator_due ON workflow_runs(owner_id,{AVAILABLE_AT},workflow_id) WHERE execution_state IN ({ACTIVE_STATES})",
)
CONTROL_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS workflow_dispatch_run ON workflow_dispatches(owner_id,binding_run_id)",
    """CREATE TABLE IF NOT EXISTS workflow_source_uploads (
        owner_id TEXT NOT NULL, workspace_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
        step_key TEXT NOT NULL, generation INTEGER NOT NULL, run_id TEXT NOT NULL, task_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL, machine TEXT NOT NULL, artifact_id TEXT NOT NULL UNIQUE,
        sha256 TEXT NOT NULL, size INTEGER NOT NULL CHECK(size BETWEEN 1 AND 10485760),
        expected_revision INTEGER NOT NULL, created_at REAL NOT NULL,
        PRIMARY KEY(owner_id,run_id,attempt_id),
        FOREIGN KEY(owner_id,workspace_id,workflow_id,step_key,generation)
            REFERENCES workflow_steps(owner_id,workspace_id,workflow_id,step_key,generation)
    )""",
    f"CREATE INDEX IF NOT EXISTS workflow_control_due ON workflow_runs(owner_id,{AVAILABLE_AT},workflow_id) WHERE {SCHEDULABLE}",
    """CREATE TABLE IF NOT EXISTS workflow_operations (
        owner_id TEXT NOT NULL, workspace_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
        client_token TEXT NOT NULL, request_digest TEXT NOT NULL, receipt BLOB NOT NULL CHECK(length(receipt)<=16384),
        created_at REAL NOT NULL, PRIMARY KEY(owner_id,workflow_id,client_token),
        FOREIGN KEY(owner_id,workspace_id,workflow_id) REFERENCES workflow_runs(owner_id,workspace_id,workflow_id)
    )""",
    """CREATE TABLE IF NOT EXISTS workflow_stop_requests (
        owner_id TEXT NOT NULL, workspace_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
        step_key TEXT NOT NULL, generation INTEGER NOT NULL, reason_code TEXT NOT NULL,
        requested_at REAL NOT NULL, deadline REAL NOT NULL CHECK(deadline>=requested_at),
        PRIMARY KEY(owner_id,workflow_id,step_key,generation),
        FOREIGN KEY(owner_id,workspace_id,workflow_id,step_key,generation)
            REFERENCES workflow_steps(owner_id,workspace_id,workflow_id,step_key,generation)
    )""",
    """CREATE TABLE IF NOT EXISTS workflow_reconciliation_requests (
        owner_id TEXT NOT NULL, workspace_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
        step_key TEXT NOT NULL, generation INTEGER NOT NULL, client_token TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('pending','inspected')),
        mode TEXT NOT NULL CHECK(mode IN ('inspect','cancel')),
        requested_at REAL NOT NULL, inspected_at REAL,
        PRIMARY KEY(owner_id,workflow_id,client_token),
        FOREIGN KEY(owner_id,workspace_id,workflow_id,step_key,generation)
            REFERENCES workflow_steps(owner_id,workspace_id,workflow_id,step_key,generation)
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS workflow_reconciliation_pending ON workflow_reconciliation_requests(owner_id,workflow_id) WHERE state='pending'",
    """CREATE TABLE IF NOT EXISTS workflow_budget_checks (
        owner_id TEXT NOT NULL, workflow_id TEXT NOT NULL, step_key TEXT NOT NULL, generation INTEGER NOT NULL,
        active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), service_sequence INTEGER NOT NULL DEFAULT 0 CHECK(service_sequence>=0),
        PRIMARY KEY(owner_id,workflow_id,step_key,generation),
        FOREIGN KEY(owner_id,workflow_id,step_key,generation) REFERENCES workflow_budget_reservations(owner_id,workflow_id,step_key,generation)
    )""",
    "CREATE INDEX IF NOT EXISTS workflow_budget_check_due ON workflow_budget_checks(service_sequence,owner_id,workflow_id,step_key,generation) WHERE active=1",
    "CREATE INDEX IF NOT EXISTS workflow_budget_check_sequence ON workflow_budget_checks(service_sequence DESC)",
)
SCHEMA_STATEMENTS = (
    "CREATE UNIQUE INDEX IF NOT EXISTS workflow_conversation_scope ON conversations(owner_id,conversation_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS workflow_run_scope ON runs(owner_id,run_id)",
    """CREATE TABLE workflow_schema_meta (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        schema_version INTEGER NOT NULL CHECK(schema_version>0),
        minimum_reader_version INTEGER NOT NULL CHECK(minimum_reader_version>0),
        admission_mode TEXT NOT NULL CHECK(admission_mode IN ('open','drain','frozen'))
    )""",
    """CREATE TABLE workflow_runs (
        workflow_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
        conversation_id TEXT, client_token TEXT NOT NULL, request_digest TEXT NOT NULL,
        definition_snapshot BLOB NOT NULL CHECK(length(definition_snapshot)<=65536),
        definition_digest TEXT NOT NULL CHECK(length(definition_digest)=64),
        execution_state TEXT NOT NULL DEFAULT 'queued' CHECK(execution_state IN ('queued','running','waiting_node','waiting_external','waiting_approval','paused','cancelling','succeeded','failed','unknown','cancelled')),
        validation_state TEXT NOT NULL DEFAULT 'not_requested' CHECK(validation_state IN ('not_requested','pending','passed','failed','inconclusive')),
        acceptance_state TEXT NOT NULL DEFAULT 'pending' CHECK(acceptance_state IN ('not_requested','pending','accepted','rejected')),
        acceptance_required INTEGER NOT NULL CHECK(acceptance_required IN (0,1)),
        revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0), event_sequence INTEGER NOT NULL DEFAULT 0,
        coordinator_epoch INTEGER NOT NULL DEFAULT 0, lease_owner TEXT, lease_expires_at REAL,
        cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
        pause_requested INTEGER NOT NULL DEFAULT 0 CHECK(pause_requested IN (0,1)),
        reason_code TEXT, delivery_digest TEXT, next_poll_at REAL NOT NULL,
        created_at REAL NOT NULL, updated_at REAL NOT NULL,
        UNIQUE(owner_id,client_token), UNIQUE(owner_id,workspace_id,workflow_id),
        FOREIGN KEY(owner_id,workspace_id) REFERENCES workspaces(owner_id,workspace_id),
        FOREIGN KEY(owner_id,conversation_id) REFERENCES conversations(owner_id,conversation_id)
    )""",
    """CREATE TABLE workflow_steps (
        owner_id TEXT NOT NULL, workspace_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
        step_key TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation>0),
        position INTEGER NOT NULL CHECK(position BETWEEN 0 AND 7), kind TEXT NOT NULL,
        is_current INTEGER NOT NULL DEFAULT 1 CHECK(is_current IN (0,1)),
        state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','ready','dispatching','running','waiting_node','waiting_external','waiting_approval','paused','cancelling','succeeded','failed','unknown','cancelled')),
        validation_state TEXT NOT NULL DEFAULT 'not_requested' CHECK(validation_state IN ('not_requested','pending','passed','failed','inconclusive')),
        contract_snapshot BLOB NOT NULL CHECK(length(contract_snapshot)<=16384),
        resolved_snapshot BLOB CHECK(length(resolved_snapshot)<=65536), resolved_digest TEXT,
        subject_digest TEXT, target_key TEXT, coordinator_epoch INTEGER NOT NULL DEFAULT 0,
        active_deadline REAL, wait_deadline REAL, reason_code TEXT,
        created_at REAL NOT NULL, updated_at REAL NOT NULL,
        PRIMARY KEY(owner_id,workflow_id,step_key,generation),
        UNIQUE(owner_id,workspace_id,workflow_id,step_key,generation),
        FOREIGN KEY(owner_id,workspace_id,workflow_id) REFERENCES workflow_runs(owner_id,workspace_id,workflow_id)
    )""",
    """CREATE TABLE workflow_dispatches (
        owner_id TEXT NOT NULL, workspace_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
        step_key TEXT NOT NULL, generation INTEGER NOT NULL, token TEXT NOT NULL UNIQUE,
        payload_hash TEXT NOT NULL CHECK(length(payload_hash)=64),
        stage TEXT NOT NULL DEFAULT 'intent' CHECK(stage IN ('intent','sending','bound','unknown')),
        target_key TEXT NOT NULL, binding_kind TEXT, binding_id TEXT, binding_run_id TEXT,
        observation_snapshot BLOB CHECK(length(observation_snapshot)<=16384),
        created_at REAL NOT NULL, updated_at REAL NOT NULL,
        PRIMARY KEY(owner_id,workflow_id,step_key,generation),
        FOREIGN KEY(owner_id,binding_run_id) REFERENCES runs(owner_id,run_id),
        FOREIGN KEY(owner_id,workspace_id,workflow_id,step_key,generation)
            REFERENCES workflow_steps(owner_id,workspace_id,workflow_id,step_key,generation)
    )""",
    """CREATE TABLE workflow_events (
        owner_id TEXT NOT NULL, workspace_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
        sequence INTEGER NOT NULL CHECK(sequence>0), kind TEXT NOT NULL,
        payload BLOB NOT NULL CHECK(length(payload)<=16384), created_at REAL NOT NULL,
        PRIMARY KEY(workflow_id,sequence),
        FOREIGN KEY(owner_id,workspace_id,workflow_id) REFERENCES workflow_runs(owner_id,workspace_id,workflow_id)
    )""",
    """CREATE TABLE workflow_evidence (
        owner_id TEXT NOT NULL, workspace_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
        step_key TEXT NOT NULL, generation INTEGER NOT NULL, evidence_key TEXT NOT NULL,
        validator_ref TEXT NOT NULL, subject_digest TEXT NOT NULL CHECK(length(subject_digest)=64),
        source TEXT NOT NULL, result TEXT NOT NULL CHECK(result IN ('passed','failed','inconclusive')),
        report_artifact_id TEXT NOT NULL, report_hash TEXT NOT NULL CHECK(length(report_hash)=64),
        report_size INTEGER NOT NULL CHECK(report_size>=0), content_digest TEXT NOT NULL,
        is_current INTEGER NOT NULL DEFAULT 1 CHECK(is_current IN (0,1)), created_at REAL NOT NULL,
        PRIMARY KEY(owner_id,workflow_id,evidence_key),
        FOREIGN KEY(owner_id,workspace_id,workflow_id,step_key,generation)
            REFERENCES workflow_steps(owner_id,workspace_id,workflow_id,step_key,generation)
    )""",
    """CREATE TABLE workflow_artifact_refs (
        owner_id TEXT NOT NULL, workspace_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
        step_key TEXT NOT NULL, generation INTEGER NOT NULL, output_key TEXT NOT NULL,
        artifact_id TEXT NOT NULL, sha256 TEXT NOT NULL CHECK(length(sha256)=64), size INTEGER NOT NULL CHECK(size>=0),
        PRIMARY KEY(owner_id,workflow_id,step_key,generation,output_key),
        FOREIGN KEY(owner_id,workspace_id,workflow_id,step_key,generation)
            REFERENCES workflow_steps(owner_id,workspace_id,workflow_id,step_key,generation)
    )""",
    """CREATE TABLE workflow_budget_reservations (
        owner_id TEXT NOT NULL, workspace_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
        step_key TEXT NOT NULL, generation INTEGER NOT NULL,
        reserved_tokens INTEGER NOT NULL CHECK(reserved_tokens>=0), reserved_requests INTEGER NOT NULL CHECK(reserved_requests>=0),
        settlement TEXT NOT NULL DEFAULT 'reserved' CHECK(settlement IN ('reserved','unknown','settled')),
        charged_tokens INTEGER CHECK(charged_tokens>=0), charged_requests INTEGER CHECK(charged_requests>=0),
        child_request_id TEXT UNIQUE, created_at REAL NOT NULL, updated_at REAL NOT NULL,
        PRIMARY KEY(owner_id,workflow_id,step_key,generation),
        FOREIGN KEY(owner_id,workspace_id,workflow_id,step_key,generation)
            REFERENCES workflow_steps(owner_id,workspace_id,workflow_id,step_key,generation)
    )""",
    """CREATE TABLE capability_reservations (
        owner_id TEXT NOT NULL, workspace_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
        step_key TEXT NOT NULL, generation INTEGER NOT NULL, target_key TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'held' CHECK(state IN ('held','released','isolated')),
        release_evidence_digest TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
        PRIMARY KEY(owner_id,workflow_id,step_key,generation),
        FOREIGN KEY(owner_id,workspace_id,workflow_id,step_key,generation)
            REFERENCES workflow_steps(owner_id,workspace_id,workflow_id,step_key,generation)
    )""",
    "CREATE INDEX workflow_due ON workflow_runs(owner_id,execution_state,next_poll_at,lease_expires_at)",
    "CREATE INDEX workflow_owner_page ON workflow_runs(owner_id,workflow_id DESC)",
    "CREATE INDEX workflow_conversation ON workflow_runs(owner_id,conversation_id)",
    "CREATE UNIQUE INDEX workflow_current_step ON workflow_steps(owner_id,workflow_id,step_key) WHERE is_current=1",
    "CREATE INDEX workflow_current_order ON workflow_steps(owner_id,workflow_id,is_current,position)",
    "CREATE INDEX workflow_dispatch_run ON workflow_dispatches(owner_id,binding_run_id)",
    "CREATE INDEX workflow_current_evidence ON workflow_evidence(owner_id,workflow_id,step_key,generation,is_current)",
    "CREATE INDEX capability_held ON capability_reservations(target_key,state)",
)


class WorkflowSchemaError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def inspect_schema(connection: sqlite3.Connection) -> dict[str, int | str] | None:
    """Recognize existing tables even when all runtime gates are disabled."""
    from hub.domain.capability import CapabilityStoreError
    from .capability_schema import inspect_capability_schema
    from .workflow_external_schema import inspect_external_schema
    from .integration_identity_schema import inspect_integration_schema
    from .workflow_summary_schema import inspect_summary_schema
    summaries = inspect_summary_schema(connection)
    integrations = inspect_integration_schema(connection)
    external = inspect_external_schema(connection)
    try:
        capabilities = inspect_capability_schema(connection)
    except CapabilityStoreError as exc:
        raise WorkflowSchemaError(exc.code) from exc
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not (TABLES | CONTROL_TABLES).intersection(tables):
        if summaries:
            raise WorkflowSchemaError("incomplete_workflow_schema")
        return None
    if not TABLES.issubset(tables):
        raise WorkflowSchemaError("incomplete_workflow_schema")
    # Source uploads are an additive F3 table. Explicit init adds it to older
    # complete control schemas; a missing original control table is corruption.
    required_controls = CONTROL_TABLES - {"workflow_source_uploads"}
    if CONTROL_TABLES.intersection(tables) and not required_controls.issubset(tables):
        raise WorkflowSchemaError("incomplete_workflow_schema")
    row = connection.execute(
        "SELECT schema_version,minimum_reader_version,admission_mode FROM workflow_schema_meta WHERE singleton=1"
    ).fetchone()
    if row is None or type(row[0]) is not int or type(row[1]) is not int or row[0] < 1 or row[1] < 1:
        raise WorkflowSchemaError("invalid_workflow_schema")
    if (row[0] != SCHEMA_VERSION or row[1] > READER_VERSION or (row[1] == 2 and not capabilities)
            or (row[1] == 3 and not external) or (row[1] == 4 and not integrations)
            or (row[1] == 5 and external != 2)):
        raise WorkflowSchemaError("unsupported_workflow_schema")
    if row[2] not in ("open", "drain", "frozen"):
        raise WorkflowSchemaError("invalid_admission_mode")
    return {"schema_version": row[0], "minimum_reader_version": row[1], "admission_mode": row[2]}
