"""Immutable summary generation binding over the original Run queue."""
from __future__ import annotations

import secrets
import sqlite3
from typing import Any

from hub.domain.workflow_errors import WorkflowValidationError
from hub.domain.workflow_identity import canonical_digest, canonical_json, validate_reference
from hub.domain.workflow_json import decode_snapshot
from hub.domain.workflow_summary import MAX_REVISION, WorkflowSummaryGenerationProvenance, WorkflowSummaryVersion
from hub.domain.workflow_summary_generation import (
    GENERATION_INSTRUCTION, MAX_GENERATION_INPUT_BYTES, SUMMARY_GENERATION_MARKER,
    SUMMARY_TOOL_POLICY, generation_request,
)
from .workflow_repository import WorkflowRepository, WorkflowRepositoryError, _time
from .workflow_schema import inspect_schema
from .workflow_summary_schema import GENERATION_STATEMENTS, STATEMENTS, inspect_summary_schema

_COLUMNS = "owner_id,workspace_id,run_id,client_token,CASE WHEN typeof(request_snapshot)='blob' AND length(request_snapshot)<=8192 THEN request_snapshot END AS request_snapshot,request_digest,source_sha256,provider_digest,config_sha256,created_at_ms"
_STATES = frozenset({"queued", "running", "waiting_node", "cancelling", "succeeded", "failed", "unknown", "cancelled"})


def _rows(connection: sqlite3.Connection, query: str, parameters: tuple = ()) -> list[dict[str, Any]]:
    cursor = connection.execute(query, parameters)
    names = tuple(column[0] for column in cursor.description)
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _one(connection: sqlite3.Connection, query: str, parameters: tuple = ()) -> dict[str, Any] | None:
    rows = _rows(connection, query, parameters)
    return rows[0] if rows else None


class WorkflowSummaryGenerationRepository:
    def __init__(self, workflows: WorkflowRepository) -> None:
        self.workflows = workflows

    @staticmethod
    def _sources(connection: sqlite3.Connection, owner: str, workspace: str, ids: list[str], *, current: bool) -> list[dict[str, Any]]:
        rows = _rows(connection,
            "SELECT r.run_id,r.owner_id,r.state,r.cancel_requested,c.conversation_id,c.owner_id AS conversation_owner,"
            "c.workspace_id,c.archived_at,m.owner_id AS message_owner,m.message_id,m.role,"
            "CASE WHEN length(CAST(r.config_snapshot AS BLOB))<=65536 THEN r.config_snapshot END AS config,"
            "CASE WHEN length(CAST(m.content AS BLOB))<=49152 THEN m.content END AS input,"
            "CASE WHEN length(CAST(r.result_text AS BLOB))<=49152 THEN r.result_text END AS output "
            "FROM runs r JOIN conversations c ON c.conversation_id=r.conversation_id "
            "JOIN messages m ON m.message_id=r.trigger_message_id WHERE r.owner_id=? AND r.run_id IN ("
            + ",".join("?" for _ in ids) + ")", (owner, *ids))
        by_id = {row["run_id"]: row for row in rows}
        result = []
        conversation = None
        for run_id in ids:
            row = by_id.get(run_id)
            if (row is None or (row["owner_id"], row["conversation_owner"], row["message_owner"], row["workspace_id"], row["role"])
                    != (owner, owner, owner, workspace, "user") or row["config"] is None):
                raise WorkflowRepositoryError("summary_source_unavailable")
            config = decode_snapshot(row["config"].encode(), max_bytes=65536)
            if config.get("workspace_id") != workspace:
                raise WorkflowRepositoryError("summary_source_unavailable")
            if conversation is None: conversation = row["conversation_id"]
            if conversation != row["conversation_id"]:
                raise WorkflowRepositoryError("summary_source_unavailable")
            if current and (row["state"] != "succeeded" or row["cancel_requested"] or row["archived_at"] is not None):
                raise WorkflowRepositoryError("summary_source_unavailable")
            if current and (type(row["input"]) is not str or type(row["output"]) is not str or not row["output"].strip()):
                raise WorkflowValidationError("summary_input_too_large")
            result.append({"run_id": run_id, "conversation_id": row["conversation_id"], "message_id": row["message_id"],
                "input": row["input"], "output": row["output"]})
        return result

    @staticmethod
    def _load(connection: sqlite3.Connection, row: sqlite3.Row) -> tuple[dict[str, Any], dict[str, Any], WorkflowSummaryGenerationProvenance]:
        try:
            snapshot = decode_snapshot(row["request_snapshot"], max_bytes=8192)
            if (set(snapshot) != {"schema_version", "request"} or type(snapshot["schema_version"]) is not int
                    or snapshot["schema_version"] != 1):
                raise WorkflowValidationError("invalid_summary_generation")
            request = generation_request(row["owner_id"], snapshot["request"])
            if (snapshot != {"schema_version": 1, "request": request} or canonical_json(snapshot) != row["request_snapshot"]
                    or (request["workspace_id"], request["client_token"], canonical_digest(request))
                    != (row["workspace_id"], row["client_token"], row["request_digest"])
                    or type(row["created_at_ms"]) is not int or not 0 <= row["created_at_ms"] <= MAX_REVISION):
                raise WorkflowValidationError("invalid_summary_generation")
            run = _one(connection,
                "SELECT r.owner_id,r.run_id,r.conversation_id,r.state,r.cancel_requested,r.lease_id,r.lease_owner,r.lease_expires_at,"
                "CASE WHEN length(CAST(r.config_snapshot AS BLOB))<=65536 THEN r.config_snapshot END AS config_snapshot,"
                "CASE WHEN length(CAST(r.result_text AS BLOB))<=8192 THEN r.result_text END AS result_text,"
                "c.owner_id AS conversation_owner,c.workspace_id,m.owner_id AS message_owner,m.role,m.message_id,"
                "CASE WHEN length(CAST(m.content AS BLOB))<=49152 THEN m.content END AS content "
                "FROM runs r JOIN conversations c ON c.conversation_id=r.conversation_id "
                "JOIN messages m ON m.message_id=r.trigger_message_id WHERE r.owner_id=? AND r.run_id=?",
                (row["owner_id"], row["run_id"]))
            if (run is None or run["state"] not in _STATES
                    or (run["conversation_owner"], run["message_owner"], run["workspace_id"], run["role"])
                    != (row["owner_id"], row["owner_id"], row["workspace_id"], "user")
                    or type(run["config_snapshot"]) is not str or type(run["content"]) is not str):
                raise WorkflowValidationError("invalid_summary_generation")
            config = decode_snapshot(run["config_snapshot"].encode(), max_bytes=65536)
            body = decode_snapshot(run["content"].encode(), max_bytes=MAX_GENERATION_INPUT_BYTES)
            if (canonical_json(config).decode() != run["config_snapshot"] or canonical_digest(config) != row["config_sha256"]
                    or config.get("owner_id") != row["owner_id"] or config.get("workspace_id") != row["workspace_id"]
                    or config.get("execution_node_id") is not None or config.get("tool_policy") != SUMMARY_TOOL_POLICY
                    or config.get("model_profile_id") != request["model_profile_id"]
                    or type(config.get("provider_snapshot")) is not dict or canonical_digest(config.get("provider_snapshot")) != row["provider_digest"]
                    or config.get(SUMMARY_GENERATION_MARKER) != {"revision": 1, "request_digest": row["request_digest"], "source_sha256": row["source_sha256"]}
                    or type(config[SUMMARY_GENERATION_MARKER]["revision"]) is not int
                    or set(body) != {"schema_version", "instruction", "sources"} or type(body["schema_version"]) is not int or body["schema_version"] != 1
                    or body["instruction"] != GENERATION_INSTRUCTION or canonical_json(body).decode() != run["content"]
                    or canonical_digest(body) != row["source_sha256"]):
                raise WorkflowValidationError("invalid_summary_generation")
            sources = WorkflowSummaryGenerationRepository._sources(connection, row["owner_id"], row["workspace_id"], request["source_run_ids"], current=False)
            if type(body["sources"]) is not list or len(body["sources"]) != len(sources):
                raise WorkflowValidationError("invalid_summary_generation")
            for frozen, source in zip(body["sources"], sources):
                if (type(frozen) is not dict or set(frozen) != set(source)
                        or any(frozen[key] != source[key] for key in ("run_id", "conversation_id", "message_id"))
                        or type(frozen["input"]) is not str or type(frozen["output"]) is not str or not frozen["output"].strip()):
                    raise WorkflowValidationError("invalid_summary_generation")
            links = connection.execute("SELECT position,source_run_id FROM workflow_summary_generation_sources WHERE owner_id=? AND generation_run_id=? ORDER BY position LIMIT 17",
                (row["owner_id"], row["run_id"])).fetchall()
            if [tuple(link) for link in links] != list(enumerate(request["source_run_ids"])):
                raise WorkflowValidationError("invalid_summary_generation")
            provenance = WorkflowSummaryGenerationProvenance(row["run_id"], tuple(request["source_run_ids"]),
                row["source_sha256"], request["model_profile_id"], row["provider_digest"])
            return request, dict(run), provenance
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise WorkflowRepositoryError("summary_generation_invalid") from exc

    @staticmethod
    def binding(connection: sqlite3.Connection, owner: str, run_id: str):
        validate_reference(run_id)
        if not inspect_summary_schema(connection) or connection.execute("SELECT 1 FROM sqlite_master WHERE name='workflow_summary_generations'").fetchone() is None:
            raise WorkflowRepositoryError("summary_generation_not_found")
        row = _one(connection, f"SELECT {_COLUMNS} FROM workflow_summary_generations WHERE owner_id=? AND run_id=?", (owner, run_id))
        if row is None: raise WorkflowRepositoryError("summary_generation_not_found")
        return row, WorkflowSummaryGenerationRepository._load(connection, row)

    @staticmethod
    def _value(connection, row, loaded) -> dict[str, Any]:
        from .workflow_summary_repository import _decode, _VERSION_COLUMNS

        request, run, provenance = loaded
        candidate = run["result_text"] if run["state"] == "succeeded" else None
        error = "summary_output_too_large" if run["state"] == "succeeded" and candidate is None else None
        if candidate is not None and (type(candidate) is not str or not candidate.strip()):
            candidate, error = None, "summary_output_empty"
        if candidate is not None:
            try:
                WorkflowSummaryVersion(row["owner_id"], row["workspace_id"], request["summary_id"],
                    request["expected_epoch"] + 1, request["title"], candidate, row["owner_id"], row["created_at_ms"],
                    source_kind="model", generation=provenance)
            except WorkflowValidationError as exc:
                if exc.code != "context_too_large": raise
                candidate, error = None, "summary_output_too_large"
        prior = connection.execute(f"SELECT {_VERSION_COLUMNS} FROM workflow_summary_versions WHERE owner_id=? AND client_token=?",
            (row["owner_id"], "summary-generated-" + canonical_digest({"run_id": row["run_id"]}))).fetchone()
        published_revision = None
        if prior is not None:
            version = _decode(prior, connection)
            if version.generation != provenance: raise WorkflowRepositoryError("summary_generation_invalid")
            published_revision = version.revision
        return {"run_id": row["run_id"], "workspace_id": row["workspace_id"], "summary_id": request["summary_id"],
            "title": request["title"], "expected_epoch": request["expected_epoch"], "state": run["state"],
            "model_profile_id": request["model_profile_id"], "source_run_ids": request["source_run_ids"],
            "source_sha256": row["source_sha256"], "created_at_ms": row["created_at_ms"], "candidate": candidate,
            "error_code": error, "published_revision": published_revision}

    def create(self, owner: str, values: dict[str, Any], *, now: float) -> dict[str, Any]:
        from .workflow_summary_repository import WorkflowSummaryRepository

        request = generation_request(owner, values)
        digest = canonical_digest(request)
        created_ms = int(_time(now) * 1000)
        if not 0 <= created_ms <= MAX_REVISION: raise WorkflowValidationError("invalid_summary_generation")
        with self.workflows.transaction() as connection:
            inspect_schema(connection)
            present = inspect_summary_schema(connection)
            if present and connection.execute("SELECT 1 FROM sqlite_master WHERE name='workflow_summary_generations'").fetchone():
                prior = _one(connection, f"SELECT {_COLUMNS} FROM workflow_summary_generations WHERE owner_id=? AND client_token=?", (owner, request["client_token"]))
                if prior is not None:
                    if prior["request_digest"] != digest: raise WorkflowRepositoryError("idempotency_conflict")
                    return self._value(connection, prior, self._load(connection, prior))
            workspace = request["workspace_id"]
            WorkflowSummaryRepository._authority(connection, owner, workspace, write=True)
            head = WorkflowSummaryRepository._head(connection, owner, workspace, request["summary_id"]) if present else None
            if (head["epoch"] if head else 0) != request["expected_epoch"]: raise WorkflowRepositoryError("revision_conflict")
            if head and not head["enabled"]: raise WorkflowRepositoryError("summary_disabled")
            profile = _one(connection, "SELECT * FROM model_profiles WHERE owner_id=? AND profile_id=? AND enabled=1", (owner, request["model_profile_id"]))
            if profile is None: raise WorkflowRepositoryError("reference_forbidden")
            provider = {"provider": profile["provider"], "model": profile["model"], "secret_ref": profile["secret_ref"],
                "provider_config": decode_snapshot(profile["provider_config"].encode(), max_bytes=65536),
                "capabilities": decode_snapshot(profile["capabilities"].encode(), max_bytes=65536)}
            if provider["provider"] == "openai_compatible":
                provider["provider_config"] = {**provider["provider_config"], "max_retries": 0}
            body = {"schema_version": 1, "instruction": GENERATION_INSTRUCTION,
                "sources": self._sources(connection, owner, workspace, request["source_run_ids"], current=True)}
            raw_body = canonical_json(body)
            if len(raw_body) > MAX_GENERATION_INPUT_BYTES: raise WorkflowValidationError("summary_input_too_large")
            source_digest = canonical_digest(body)
            config = {"owner_id": owner, "workspace_id": workspace, "model_profile_id": request["model_profile_id"],
                "execution_node_id": None, "provider_snapshot": provider, "tool_policy": SUMMARY_TOOL_POLICY,
                SUMMARY_GENERATION_MARKER: {"revision": 1, "request_digest": digest, "source_sha256": source_digest}}
            raw_config = canonical_json(config)
            if len(raw_config) > 65536: raise WorkflowValidationError("summary_input_too_large")
            if not present:
                for statement in STATEMENTS: connection.execute(statement)
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='workflow_summary_generations'").fetchone() is None:
                for statement in GENERATION_STATEMENTS: connection.execute(statement)
            connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=max(minimum_reader_version,16) WHERE singleton=1")
            suffix = secrets.token_hex(12)
            run_id, conversation, message = "summary-run-" + suffix, "summary-conv-" + suffix, "summary-msg-" + suffix
            connection.execute("INSERT INTO conversations(conversation_id,owner_id,title,workspace_id,overrides) VALUES(?,?,?,?,'{}')", (conversation, owner, request["title"], workspace))
            connection.execute("INSERT INTO messages(message_id,conversation_id,owner_id,role,content_ref,content,client_token,created_at) VALUES(?,?,?,'user','inline',?,?,?)",
                (message, conversation, owner, raw_body.decode(), request["client_token"], str(now)))
            connection.execute("INSERT INTO runs(run_id,conversation_id,owner_id,trigger_message_id,state,config_snapshot,created_at,updated_at) VALUES(?,?,?,?,'queued',?,?,?)",
                (run_id, conversation, owner, message, raw_config.decode(), now, now))
            connection.execute("INSERT INTO workflow_summary_generations VALUES(?,?,?,?,?,?,?,?,?,?)", (owner, workspace, run_id, request["client_token"],
                canonical_json({"schema_version": 1, "request": request}), digest, source_digest, canonical_digest(provider), canonical_digest(config), created_ms))
            connection.executemany("INSERT INTO workflow_summary_generation_sources VALUES(?,?,?,?)", [(owner, run_id, position, source) for position, source in enumerate(request["source_run_ids"])])
            row, loaded = self.binding(connection, owner, run_id)
            return self._value(connection, row, loaded)

    def get(self, owner: str, run_id: str) -> dict[str, Any]:
        from .workflow_summary_repository import _scope

        with self.workflows.transaction(write=False) as connection:
            row, loaded = self.binding(connection, owner, run_id)
            _scope(owner, row["workspace_id"])
            return self._value(connection, row, loaded)

    def find(self, owner: str, client_token: str) -> dict[str, Any]:
        validate_reference(client_token)
        with self.workflows.transaction(write=False) as connection:
            if not inspect_summary_schema(connection) or connection.execute("SELECT 1 FROM sqlite_master WHERE name='workflow_summary_generations'").fetchone() is None:
                raise WorkflowRepositoryError("summary_generation_not_found")
            row = _one(connection, f"SELECT {_COLUMNS} FROM workflow_summary_generations WHERE owner_id=? AND client_token=?", (owner, client_token))
            if row is None: raise WorkflowRepositoryError("summary_generation_not_found")
            return self._value(connection, row, self._load(connection, row))

    def prepare(self, claim: dict[str, Any], *, now: float) -> dict[str, Any]:
        from .workflow_summary_repository import WorkflowSummaryRepository

        with self.workflows.transaction(write=False) as connection:
            row, (request, run, _) = self.binding(connection, claim["owner_id"], claim["run_id"])
            WorkflowSummaryRepository._authority(connection, row["owner_id"], row["workspace_id"], write=True)
            if (run["state"] != "running" or run["cancel_requested"]
                    or (run["lease_id"], run["lease_owner"]) != (claim.get("lease_id"), claim.get("lease_owner"))
                    or run["lease_expires_at"] is None or run["lease_expires_at"] <= _time(now)
                    or decode_snapshot(run["config_snapshot"].encode()) != claim.get("config_snapshot")
                    or run["message_id"] != claim.get("trigger_message_id")):
                raise WorkflowRepositoryError("summary_generation_unavailable")
            self._sources(connection, row["owner_id"], row["workspace_id"], request["source_run_ids"], current=True)
            if connection.execute("SELECT 1 FROM model_profiles WHERE owner_id=? AND profile_id=? AND enabled=1", (row["owner_id"], request["model_profile_id"])).fetchone() is None:
                raise WorkflowRepositoryError("reference_forbidden")
            return {"message_id": run["message_id"], "role": "user", "content": run["content"]}

    @staticmethod
    def verify_version(connection: sqlite3.Connection, item: WorkflowSummaryVersion) -> None:
        _, (request, run, provenance) = WorkflowSummaryGenerationRepository.binding(connection, item.owner_id, item.generation.run_id)
        if (run["state"] != "succeeded" or run["result_text"] != item.content or item.generation != provenance
                or (item.workspace_id, item.summary_id, item.title) != (request["workspace_id"], request["summary_id"], request["title"])):
            raise WorkflowRepositoryError("summary_generation_invalid")

    @staticmethod
    def validate_snapshot(connection: sqlite3.Connection) -> None:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='workflow_summary_generations'").fetchone() is None: return
        inspect_summary_schema(connection)
        for table in ("workflow_summary_generations", "workflow_summary_generation_sources"):
            if connection.execute(f"PRAGMA foreign_key_check({table})").fetchone() is not None:
                raise WorkflowRepositoryError("summary_generation_invalid")
        cursor = None
        while True:
            rows = _rows(connection, f"SELECT rowid AS generation_rowid,{_COLUMNS} FROM workflow_summary_generations "
                + ("WHERE rowid>? " if cursor is not None else "") + "ORDER BY rowid LIMIT 128", () if cursor is None else (cursor,))
            for row in rows: WorkflowSummaryGenerationRepository._load(connection, row)
            if len(rows) < 128: return
            cursor = rows[-1]["generation_rowid"]
