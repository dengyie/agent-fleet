"""Trusted binding lookup and first-dispatch checks inside existing SQLite writes."""
from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from hub.domain.capability import CapabilityError, CapabilityStoreError, SelectionRequirements, TargetBinding, decode, identifier, timestamp
from hub.domain.capability_execution import CAPABILITY_ARGUMENT, CapabilityExecutionContext, RegisteredCapabilityAction
from fleet_core.platform_command import PlatformCommand, args_hash
from hub.domain.workflow_contract import parse_definition, parse_resolved
from hub.domain.workflow_contract_types import CapabilityRequirement, CatalogRef, ResolvedContract
from hub.domain.workflow_errors import WorkflowValidationError
from hub.domain.workflow_identity import canonical_digest
from hub.domain.remote_workflow_test import (REMOTE_TEST_ACTION, REMOTE_TEST_MARKER, RemoteTestError,
    RemoteTestRequest, remote_test_command_id, validate_remote_test_config)
from hub.domain.workflow_contract_types import CodeRevisionReference
from fleet_core.node_action_contract import NODE_TOOLS, POSTCHECK_ACTION
from hub.platform.remote_tool_broker import remote_retry_class
from .capability_repository import CapabilityRepository
from .capability_schema import inspect_capability_schema

CAPABILITY_RUN_MARKER = "workflow_capability"
CAPABILITY_ACTIONS = frozenset("tool." + tool for tool in NODE_TOOLS) | {POSTCHECK_ACTION, REMOTE_TEST_ACTION}


@dataclass(frozen=True, slots=True)
class PinnedCapabilityDispatch:
    record: sqlite3.Row
    binding: TargetBinding
    requirements: SelectionRequirements
    resolved: ResolvedContract

    @property
    def identity(self) -> tuple[str, str, str, int]:
        return tuple(self.record[key] for key in ("owner_id", "workflow_id", "step_key", "generation"))


def pinned_dispatch(connection: sqlite3.Connection, token: str) -> PinnedCapabilityDispatch | None:
    """Read historical identity without withdrawing already committed execution facts."""
    identifier(token)
    try:
        if not inspect_capability_schema(connection):
            return None
        row = connection.execute(
            "SELECT b.owner_id,b.workspace_id,b.workflow_id,b.step_key,b.generation,b.node_id,b.snapshot,b.digest,"
            "b.requirements_snapshot,b.memory_bytes,b.disk_bytes,d.token,d.payload_hash,d.target_key AS dispatch_target,"
            "d.stage,d.binding_run_id,s.resolved_snapshot,s.resolved_digest,s.is_current,s.kind,s.state AS step_state,"
            "s.target_key AS step_target,s.active_deadline,s.wait_deadline,w.definition_snapshot,w.definition_digest,"
            "w.cancel_requested,w.pause_requested,w.execution_state,br.settlement,br.child_request_id,"
            "r.state AS reservation_state,r.target_key AS reservation_target FROM workflow_dispatches d "
            "JOIN capability_step_bindings b USING(owner_id,workspace_id,workflow_id,step_key,generation) "
            "JOIN workflow_steps s USING(owner_id,workspace_id,workflow_id,step_key,generation) "
            "JOIN workflow_runs w USING(owner_id,workspace_id,workflow_id) "
            "LEFT JOIN workflow_budget_reservations br USING(owner_id,workspace_id,workflow_id,step_key,generation) "
            "LEFT JOIN capability_reservations r USING(owner_id,workspace_id,workflow_id,step_key,generation) "
            "WHERE d.token=?", (token,),
        ).fetchone()
        if row is None:
            return None
        return _parse_dispatch(row)
    except (CapabilityError, WorkflowValidationError, TypeError, ValueError) as exc:
        raise CapabilityStoreError("capability_snapshot_corrupt") from exc


def _parse_dispatch(row: sqlite3.Row) -> PinnedCapabilityDispatch:
    raw = bytes(row["snapshot"])
    CapabilityRepository._check_digest(raw, row["digest"])
    binding = TargetBinding.from_snapshot(raw)
    req_raw = bytes(row["requirements_snapshot"])
    requirements = SelectionRequirements.from_snapshot(req_raw)
    resolved = parse_resolved(bytes(row["resolved_snapshot"]))
    definition = parse_definition(bytes(row["definition_snapshot"]))
    if binding.snapshot != raw or requirements.snapshot != req_raw or (
            hashlib.sha256(req_raw).hexdigest(), hashlib.sha256(definition.snapshot).hexdigest()) != (
            binding.requirements_digest, row["definition_digest"]):
        raise CapabilityStoreError("capability_snapshot_corrupt")
    if (row["node_id"], row["dispatch_target"], row["step_target"], row["reservation_target"], row["payload_hash"],
            row["resolved_digest"], row["memory_bytes"], row["disk_bytes"], requirements.input_digest,
            requirements.memory_bytes, requirements.disk_bytes) != (
            binding.node_id, binding.node_id, binding.node_id, binding.node_id, binding.input_digest, binding.input_digest,
            binding.memory_bytes, binding.disk_bytes, binding.input_digest, binding.memory_bytes, binding.disk_bytes):
        raise CapabilityStoreError("capability_snapshot_corrupt")
    adapter = decode(resolved.snapshot)["adapter_ref"]
    if (resolved.owner_id, resolved.workspace_id, resolved.step_key, resolved.digest, requirements.owner_id,
            requirements.workspace_id, requirements.template, requirements.adapter_id, requirements.max_active_seconds) != (
            row["owner_id"], row["workspace_id"], row["step_key"], binding.input_digest, row["owner_id"], row["workspace_id"],
            CatalogRef(definition.template_id, definition.revision), adapter["id"], resolved.contract.budget.max_active_seconds):
        raise CapabilityStoreError("capability_snapshot_corrupt")
    required = {ref.id: set(ref.revisions) for ref in resolved.contract.capability_requirements}
    if required != {ref.id: set(ref.revisions) for ref in requirements.capabilities} or (
            set(required) != {ref.id for ref in binding.capabilities}) or any(ref.revision not in required[ref.id] for ref in binding.capabilities):
        raise CapabilityStoreError("capability_snapshot_corrupt")
    if row["settlement"] is None or row["reservation_state"] is None or row["binding_run_id"] != row["child_request_id"]:
        raise CapabilityStoreError("capability_snapshot_corrupt")
    return PinnedCapabilityDispatch(row, binding, requirements, resolved)


def _command_run(connection: sqlite3.Connection, command: Mapping[str, Any]) -> tuple[sqlite3.Row, dict[str, Any]] | None:
    if not command.get("run_id"):
        return None
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone() is None:
        return None
    run = connection.execute("SELECT owner_id,conversation_id,trigger_message_id,config_snapshot,state,cancel_requested,lease_expires_at FROM runs WHERE run_id=?",
                             (command["run_id"],)).fetchone()
    if run is None:
        return None
    try:
        config = decode(run["config_snapshot"].encode())
    except (CapabilityError, AttributeError) as exc:
        raise CapabilityStoreError("capability_snapshot_corrupt") from exc
    return run, config


def _run_token(connection: sqlite3.Connection, owner: str, run_id: str) -> str | None:
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_dispatches'").fetchone() is None:
        return None
    rows = connection.execute("SELECT token FROM workflow_dispatches WHERE owner_id=? AND binding_run_id=? LIMIT 2", (owner, run_id)).fetchall()
    if len(rows) > 1:
        raise CapabilityStoreError("capability_snapshot_corrupt")
    return rows[0][0] if rows else None


def _postcheck_origin(connection: sqlite3.Connection, command_id: str) -> dict[str, Any] | None:
    """Follow a unique descriptor even if a caller changes the check's Run."""
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='platform_command_postchecks'").fetchone() is None:
        return None
    row = connection.execute(
        "SELECT c.command_id,c.owner_id,c.target_node,c.action,c.resource_id,c.arguments,c.retry_class,"
        "c.args_hash,c.run_id,c.status,c.signature FROM platform_command_postchecks p "
        "JOIN platform_commands c ON c.command_id=p.command_id WHERE p.check_command_id=?", (command_id,),
    ).fetchone()
    if row is None:
        return None
    original = dict(row)
    original["arguments"] = decode(row["arguments"].encode("utf-8"))
    return original


def reject_disabled_command(connection: sqlite3.Connection, command: Mapping[str, Any]) -> None:
    if command.get("action") == REMOTE_TEST_ACTION:
        raise CapabilityStoreError("capability_unavailable")
    if CAPABILITY_ARGUMENT in command.get("arguments", {}):
        raise CapabilityStoreError("capability_unavailable")
    original = _postcheck_origin(connection, command["command_id"])
    if original is not None and CAPABILITY_ARGUMENT in original["arguments"]:
        raise CapabilityStoreError("capability_unavailable")
    found = _command_run(connection, command)
    if found is None:
        return
    run, config = found
    if CAPABILITY_RUN_MARKER in config:
        raise CapabilityStoreError("capability_unavailable")
    token = _run_token(connection, run["owner_id"], command["run_id"])
    if token is not None and pinned_dispatch(connection, token) is not None:
        raise CapabilityStoreError("capability_unavailable")


class CapabilityDispatchRepository:
    def __init__(self, repository: CapabilityRepository, *, actions: tuple[RegisteredCapabilityAction, ...] = ()) -> None:
        if type(repository) is not CapabilityRepository or type(actions) is not tuple or len(actions) > 2048:
            raise CapabilityError("invalid_capability_dispatch")
        self.repository = repository
        self.actions: dict[tuple[str, str, str, int], RegisteredCapabilityAction] = {}
        for item in actions:
            if type(item) is not RegisteredCapabilityAction or item.action not in CAPABILITY_ACTIONS:
                raise CapabilityError("invalid_capability_action")
            key = (item.adapter_id, item.action, item.capability.id, item.capability.revision)
            if key in self.actions:
                raise CapabilityError("duplicate_capability_action")
            self.actions[key] = item

    def _transaction(self, connection: sqlite3.Connection) -> None:
        database = connection.execute("PRAGMA database_list").fetchone()[2]
        if not connection.in_transaction or Path(database).resolve() != self.repository.workflows.db_path.resolve():
            raise CapabilityStoreError("capability_transaction_required")
        if self.repository._startup_sequence is None:
            raise CapabilityStoreError("capability_uninitialized")

    def validate_dispatch(self, connection: sqlite3.Connection, token: str, *, now: float) -> TargetBinding | None:
        self._transaction(connection)
        now = timestamp(now)
        pinned = pinned_dispatch(connection, token)
        if pinned is None:
            return None
        return self._validate_pinned(connection, pinned, now=now)

    def _validate_pinned(self, connection: sqlite3.Connection, pinned: PinnedCapabilityDispatch, *, now: float) -> TargetBinding:
        self._active_dispatch(connection, pinned, now=now)
        return self._revalidate_binding(connection, pinned, now=now)

    def _revalidate_binding(self, connection: sqlite3.Connection, pinned: PinnedCapabilityDispatch, *, now: float) -> TargetBinding:
        # Charge the original held/isolated reservation once, retaining every
        # other occupied reservation in the physical node's aggregate.
        exact = replace(pinned.requirements, capabilities=tuple(CapabilityRequirement(ref.id, (ref.revision,)) for ref in pinned.binding.capabilities))
        selected = self.repository._select(connection, exact, now=now, node_id=pinned.binding.node_id, exclude=pinned.identity)
        if selected.binding is None or replace(selected.binding, requirements_digest=pinned.binding.requirements_digest) != pinned.binding:
            raise CapabilityStoreError("capability_changed")
        return pinned.binding

    @staticmethod
    def _active_dispatch(connection: sqlite3.Connection, pinned: PinnedCapabilityDispatch, *, now: float) -> None:
        row = pinned.record
        if (row["is_current"] != 1 or row["cancel_requested"] or row["step_state"] in {"succeeded", "failed", "cancelled", "unknown", "cancelling"}
                or row["execution_state"] in {"succeeded", "failed", "cancelled", "unknown", "cancelling"}
                or row["reservation_state"] != "held" or row["settlement"] != "reserved"):
            raise CapabilityStoreError("capability_changed")
        if any(value is not None and timestamp(value) <= now for value in (row["active_deadline"], row["wait_deadline"])):
            raise CapabilityStoreError("capability_changed")
        mode = connection.execute("SELECT admission_mode FROM workflow_schema_meta WHERE singleton=1").fetchone()
        if mode is None or mode[0] != "open" or row["pause_requested"] or row["execution_state"] == "paused":
            raise CapabilityStoreError("capability_dispatch_blocked")

    def _postcheck_context(self, connection: sqlite3.Connection, command: Mapping[str, Any], original: Mapping[str, Any] | None,
                           pinned: PinnedCapabilityDispatch, *, run_state: str, now: float) -> tuple[CapabilityExecutionContext, float | None]:
        if original is None or command.get("action") != POSTCHECK_ACTION or command.get("retry_class") != "read_only":
            raise CapabilityStoreError("capability_unavailable")
        descriptor = connection.execute(
            "SELECT owner_id,kind,check_command_id,path,expected_sha256,state FROM platform_command_postchecks "
            "WHERE check_command_id=? AND owner_id=?", (command.get("command_id"), command.get("owner_id")),
        ).fetchone()
        if descriptor is None or descriptor["kind"] != "workspace_digest" or descriptor["state"] != "pending":
            raise CapabilityStoreError("capability_unavailable")
        arguments, check_arguments = original["arguments"], command.get("arguments")
        if (original["owner_id"] != descriptor["owner_id"] or original["status"] != "unknown"
                or original["action"] != "tool.workspace.write" or not original["signature"]
                or set(arguments) != {"path", "content", CAPABILITY_ARGUMENT}
                or not isinstance(arguments.get("path"), str) or not arguments["path"]
                or not isinstance(arguments.get("content"), str)
                or args_hash(arguments) != original["args_hash"]
                or arguments["path"] != descriptor["path"]
                or hashlib.sha256(arguments["content"].encode("utf-8")).hexdigest() != descriptor["expected_sha256"]):
            raise CapabilityStoreError("capability_changed")
        expected_id = "pc_" + hashlib.sha256(("workspace_digest:" + original["command_id"]).encode("utf-8")).hexdigest()[:48]
        if (descriptor["check_command_id"] != expected_id or not isinstance(check_arguments, Mapping)
                or set(check_arguments) not in ({"path"}, {"path", CAPABILITY_ARGUMENT})
                or check_arguments["path"] != descriptor["path"] or command.get("grant_id") is not None
                or any(command.get(key) != original[key] for key in ("owner_id", "target_node", "resource_id", "run_id"))):
            raise CapabilityStoreError("capability_changed")
        original_context = CapabilityExecutionContext.from_command(original)
        if original_context != self._action_context(connection, original, pinned):
            raise CapabilityStoreError("capability_changed")
        context = self._action_context(connection, command, pinned)
        if context.capability_id == original_context.capability_id:
            raise CapabilityStoreError("capability_unavailable")
        row = pinned.record
        if (run_state != "unknown" or row["is_current"] != 1 or row["stage"] != "unknown"
                or row["step_state"] != "unknown" or row["execution_state"] != "unknown"
                or row["reservation_state"] not in {"held", "isolated"} or row["settlement"] != "unknown"
                or row["cancel_requested"]):
            raise CapabilityStoreError("capability_changed")
        if any(value is not None and timestamp(value) <= now for value in (row["active_deadline"], row["wait_deadline"])):
            raise CapabilityStoreError("capability_changed")
        mode = connection.execute("SELECT admission_mode FROM workflow_schema_meta WHERE singleton=1").fetchone()
        if mode is None or mode[0] != "open" or row["pause_requested"]:
            raise CapabilityStoreError("capability_dispatch_blocked")
        self._revalidate_binding(connection, pinned, now=now)
        return context, row["active_deadline"]

    def _context(self, connection: sqlite3.Connection, command: Mapping[str, Any], *, now: float) -> tuple[CapabilityExecutionContext, float | None] | None:
        origin = _postcheck_origin(connection, command.get("command_id"))
        postcheck = origin is not None or command.get("action") == POSTCHECK_ACTION
        origin_is_capability = bool(origin is not None and CAPABILITY_ARGUMENT in origin["arguments"])
        scope = origin or command
        found = _command_run(connection, scope)
        if found is None:
            if command.get("action") == REMOTE_TEST_ACTION:
                raise CapabilityStoreError("capability_unavailable")
            if origin_is_capability:
                raise CapabilityStoreError("capability_snapshot_corrupt")
            return None
        run, config = found
        marker = config.get("workflow_dispatch")
        token = _run_token(connection, run["owner_id"], scope["run_id"])
        if token is None:
            if command.get("action") == REMOTE_TEST_ACTION or REMOTE_TEST_MARKER in config:
                raise CapabilityStoreError("capability_unavailable")
            if CAPABILITY_RUN_MARKER in config or origin_is_capability:
                raise CapabilityStoreError("capability_snapshot_corrupt")
            return None
        pinned = pinned_dispatch(connection, token)
        if pinned is None:
            if command.get("action") == REMOTE_TEST_ACTION or REMOTE_TEST_MARKER in config:
                raise CapabilityStoreError("capability_unavailable")
            if CAPABILITY_RUN_MARKER in config or origin_is_capability:
                raise CapabilityStoreError("capability_snapshot_corrupt")
            return None
        binding = pinned.binding
        row = pinned.record
        self._run_configuration(connection, run, config, pinned)
        remote = REMOTE_TEST_MARKER in config
        if remote or command.get("action") == REMOTE_TEST_ACTION:
            self._remote_test_context(connection, command, config, pinned)
        if (run["owner_id"], row["owner_id"], row["workspace_id"], row["binding_run_id"], row["child_request_id"],
                marker.get("payload_hash"), config.get("execution_node_id"), config.get(CAPABILITY_RUN_MARKER), row["kind"]) != (
                scope.get("owner_id"), scope.get("owner_id"), scope.get("resource_id"), scope.get("run_id"), scope.get("run_id"),
                binding.input_digest, scope.get("target_node"), {"binding_digest": hashlib.sha256(binding.snapshot).hexdigest()}, "validation" if remote else "native_run"):
            raise CapabilityStoreError("capability_changed")
        if origin is not None and scope is not command and any(command.get(key) != scope.get(key)
                for key in ("owner_id", "target_node", "resource_id", "run_id")):
            raise CapabilityStoreError("capability_changed")
        if binding.node_id != scope.get("target_node") or run["cancel_requested"]:
            raise CapabilityStoreError("capability_changed")
        if postcheck:
            return self._postcheck_context(connection, command, origin, pinned, run_state=run["state"], now=now)
        if run["state"] != "running" or run["lease_expires_at"] is None or timestamp(run["lease_expires_at"]) <= now:
            raise CapabilityStoreError("capability_changed")
        if row["stage"] != "bound":
            raise CapabilityStoreError("capability_dispatch_blocked")
        self._validate_pinned(connection, pinned, now=now)
        return self._action_context(connection, command, pinned), row["active_deadline"]

    def _remote_test_context(self, connection: sqlite3.Connection, command: Mapping[str, Any],
                             config: dict[str, Any], pinned: PinnedCapabilityDispatch) -> None:
        from hub.application.workflow_templates import validate_coding_test_contract
        from .artifact_input_repository import node_input_receipts_ready

        try:
            binding_digest = hashlib.sha256(pinned.binding.snapshot).hexdigest()
            validate_remote_test_config(config, node_id=pinned.binding.node_id, binding_digest=binding_digest)
            validate_coding_test_contract(pinned.resolved)
            arguments = dict(command.get("arguments", {}))
            arguments.pop(CAPABILITY_ARGUMENT, None)
            request = RemoteTestRequest.parse(arguments)
            row = pinned.record
            revision = pinned.resolved.contract.input_refs[0].reference
            if (type(revision) is not CodeRevisionReference or request.source_revision != revision
                    or (request.workflow_id, request.generation, request.dispatch_token, request.input_digest, request.binding_digest) != (
                        row["workflow_id"], row["generation"], row["token"], row["payload_hash"], binding_digest)
                    or (command.get("action"), command.get("command_id"), command.get("retry_class"), command.get("grant_id")) != (
                        REMOTE_TEST_ACTION, remote_test_command_id(row["token"]), "manual_only", None)
                    or pinned.requirements.operating_systems != ("linux",)):
                raise RemoteTestError("invalid_remote_test_command")
            ready = node_input_receipts_ready(connection, owner=row["owner_id"], workflow=row["workflow_id"],
                generation=row["generation"], resolved=pinned.resolved, target=pinned.binding.node_id,
                public_key=self.repository.workflows.artifact_input_public_key)
            received = connection.execute("SELECT result FROM platform_commands WHERE command_id=? AND owner_id=?",
                ("receive-" + request.receive_receipt["grant_id"], row["owner_id"])).fetchone()
            source = connection.execute("SELECT subject_digest FROM workflow_steps WHERE owner_id=? AND workflow_id=? "
                "AND step_key='develop' AND is_current=1 AND state='succeeded'", (row["owner_id"], row["workflow_id"])).fetchone()
            if not ready or received is None or decode(received[0].encode()) != request.receive_receipt or (
                    source is None or source[0] != request.subject_digest):
                raise RemoteTestError("invalid_remote_test_receipt")
        except (RemoteTestError, WorkflowValidationError) as exc:
            raise CapabilityStoreError("capability_changed") from exc

    @staticmethod
    def _run_configuration(connection: sqlite3.Connection, run: sqlite3.Row, config: dict[str, Any], pinned: PinnedCapabilityDispatch) -> None:
        expected_config = dict(config)
        marker = expected_config.pop("workflow_dispatch", None)
        if type(marker) is not dict or set(marker) != {"token", "payload_hash", "config_digest"} or marker["token"] != pinned.record["token"]:
            raise CapabilityStoreError("capability_changed")
        expected = canonical_digest({"target_key": pinned.binding.node_id, "adapter_ref": decode(pinned.resolved.snapshot)["adapter_ref"],
            "conversation_id": run["conversation_id"], "config": expected_config, "legacy_request": None})
        message = connection.execute("SELECT owner_id,conversation_id,content,client_token FROM messages WHERE message_id=?", (run["trigger_message_id"],)).fetchone()
        if expected != marker["config_digest"] or message is None or tuple(message) != (
                run["owner_id"], run["conversation_id"], pinned.resolved.snapshot.decode(), pinned.record["token"]):
            raise CapabilityStoreError("capability_changed")

    def _action_context(self, connection: sqlite3.Connection, command: Mapping[str, Any], pinned: PinnedCapabilityDispatch) -> CapabilityExecutionContext:
        adapter = pinned.requirements.adapter_id
        matches = tuple(ref for ref in pinned.binding.capabilities if (adapter, command["action"], ref.id, ref.revision) in self.actions)
        if len(matches) != 1:
            raise CapabilityStoreError("capability_unavailable")
        selected = matches[0]
        definition = connection.execute("SELECT snapshot,digest FROM capability_definitions WHERE capability_id=? AND revision=?",
                                        (selected.id, selected.revision)).fetchone()
        retry = "read_only" if command["action"] == POSTCHECK_ACTION else remote_retry_class(command["action"][5:])
        if definition is None or definition["digest"] != selected.definition_digest:
            raise CapabilityStoreError("capability_changed")
        raw = bytes(definition["snapshot"])
        CapabilityRepository._check_digest(raw, selected.definition_digest)
        # The same transaction's selection already validated the complete schema.
        if decode(raw)["retry_class"] != retry or command["retry_class"] != retry:
            raise CapabilityStoreError("capability_changed")
        return CapabilityExecutionContext(selected.id, adapter, pinned.binding)

    def prepare_command(self, connection: sqlite3.Connection, command: PlatformCommand, *, now: float) -> PlatformCommand:
        self._transaction(connection)
        if type(command) is not PlatformCommand or CAPABILITY_ARGUMENT in command.arguments:
            raise CapabilityStoreError("invalid_capability_command")
        try:
            prepared = self._context(connection, command.as_dict(), now=timestamp(now))
        except (CapabilityError, WorkflowValidationError) as exc:
            raise CapabilityStoreError("capability_snapshot_corrupt") from exc
        if prepared is None:
            return command
        context, deadline = prepared
        expires_at = min(command.expires_at, timestamp(deadline)) if deadline is not None else timestamp(command.expires_at)
        try:
            return context.bind(replace(command, expires_at=expires_at))
        except CapabilityError as exc:
            raise CapabilityStoreError(exc.code) from exc

    def guard_command(self, connection: sqlite3.Connection, command: Mapping[str, Any], now: float) -> None:
        self._transaction(connection)
        try:
            prepared = self._context(connection, command, now=timestamp(now))
        except (CapabilityError, WorkflowValidationError) as exc:
            raise CapabilityStoreError("capability_snapshot_corrupt") from exc
        if prepared is None:
            if CAPABILITY_ARGUMENT in command.get("arguments", {}):
                raise CapabilityStoreError("invalid_capability_command")
            return
        context, deadline = prepared
        if deadline is not None and timestamp(command["expires_at"]) > deadline:
            raise CapabilityStoreError("capability_changed")
        try:
            supplied = CapabilityExecutionContext.from_command(command)
            if supplied != context or args_hash(command["arguments"]) != command["args_hash"]:
                raise CapabilityError("invalid_capability_command")
        except CapabilityError as exc:
            raise CapabilityStoreError("invalid_capability_command") from exc
        if not command.get("signature"):
            raise CapabilityStoreError("command_signature_required")
        if command["action"] == "tool.browser.submit":
            self._submit_approval(connection, command)

    @staticmethod
    def _submit_approval(connection: sqlite3.Connection, command: Mapping[str, Any]) -> None:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='browser_submit_approvals' AND type='table'").fetchone() is None:
            raise CapabilityStoreError("approval_required")
        arguments = command["arguments"]
        approval = connection.execute("SELECT owner_id,workspace_id,run_id,node_id,session_id,selector,state,consumed_command_id "
            "FROM browser_submit_approvals WHERE approval_id=?", (arguments.get("approval_id"),)).fetchone()
        if approval is None or tuple(approval) != (command["owner_id"], command["resource_id"], command["run_id"], command["target_node"],
                arguments.get("session_id"), arguments.get("selector"), "consumed", command["command_id"]):
            raise CapabilityStoreError("approval_required")
