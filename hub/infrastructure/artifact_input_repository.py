"""Input grants reuse signed Commands, their leases and durable terminal receipts."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import json
import sqlite3
from typing import Any, Iterator
from hub.platform.artifact_lock import ArtifactSlotError, artifact_io_slot

from hub.domain.artifact_receive import (
    ArtifactReceiveError, RECEIVE_ACTION, RegisteredNodeInputs, ReceiveRequest,
    artifact_inputs, receive_grant_id, validate_receipt,
)
from fleet_core.platform_command import PlatformCommand, args_hash
from hub.domain.workflow_contract import parse_resolved
from hub.domain.workflow_contract_types import ResolvedContract
from hub.domain.workflow_records import CoordinatorClaim
from .command_repository import CommandRepository
from .platform_db import node_credential_is_current
from .workflow_repository import WorkflowRepository
from hub.domain.workflow_errors import WorkflowPersistenceError
from hub.domain.artifact_transfer import TransferLimits
from .artifact_capacity import ArtifactCapacityError, capacity_directory, check_capacity, node_download_key

_COMMAND_COLUMNS = ("command_id,owner_id,target_node,action,resource_id,arguments,retry_class,expires_at,"
                    "args_hash,run_id,grant_id,signature,status,lease_owner,lease_until,result")


def node_input_receipts_ready(connection: sqlite3.Connection, *, owner: str, workflow: str,
                              generation: int, resolved: ResolvedContract, target: str,
                              public_key: bytes | None = None) -> bool:
    """One bounded batch and O(n) validation; a Hub ArtifactRef is no Node receipt."""
    inputs = artifact_inputs(resolved)
    if not inputs:
        return True
    node = connection.execute("SELECT n.enabled AND ws.enabled FROM nodes n JOIN workspaces ws ON ws.owner_id=n.owner_id WHERE n.owner_id=? AND n.node_id=? AND ws.workspace_id=? LIMIT 1", (owner, target, resolved.workspace_id)).fetchone()
    if node is None:
        return False
    if not node[0]:
        return False
    if public_key is None:
        return False
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='platform_commands'").fetchone()
    if exists is None:
        return False
    grants = tuple(receive_grant_id(owner, workflow, resolved.step_key, generation, target, ref_key) for ref_key, _artifact in inputs)
    placeholders = ",".join("?" for _grant in grants)
    rows = connection.execute(f"SELECT {_COMMAND_COLUMNS} FROM platform_commands WHERE command_id IN ({placeholders}) LIMIT ?",
                              tuple("receive-" + grant for grant in grants) + (len(grants),)).fetchall()
    commands = {row["command_id"]: row for row in rows}
    for (ref_key, artifact), grant_id in zip(inputs, grants):
        row = commands.get("receive-" + grant_id)
        if row is None or row["status"] != "succeeded" or row["grant_id"] != grant_id:
            return False
        try:
            arguments, result = json.loads(row["arguments"]), json.loads(row["result"])
            if type(arguments) is not dict or type(result) is not dict:
                raise ArtifactReceiveError("receive_grant_corrupt")
            request = ReceiveRequest.parse(dict(row) | {"arguments": arguments}, node_id=target,
                public_key=public_key, now=0, max_bytes=256 * 1024 * 1024, allow_expired=True)
            validate_receipt(request, result)
            expected = {"grant_id": grant_id, "artifact_id": artifact.artifact_id, "sha256": artifact.sha256,
                "size": artifact.size, "input_alias": arguments["input_alias"]}
            actual = tuple(arguments[key] for key in ("artifact_id", "sha256", "size", "workflow_id", "step_key", "generation", "ref_key", "input_digest"))
        except (TypeError, ValueError, KeyError) as exc:
            raise ArtifactReceiveError("receive_grant_corrupt") from exc
        if (actual != (artifact.artifact_id, artifact.sha256, artifact.size, workflow, resolved.step_key, generation, ref_key, resolved.digest)
                or args_hash(arguments) != row["args_hash"] or result != expected or type(result.get("size")) is not int
                or (row["target_node"], row["resource_id"], row["owner_id"], row["action"], row["retry_class"]) !=
                    (target, resolved.workspace_id, owner, RECEIVE_ACTION, "idempotent_with_key")):
            raise ArtifactReceiveError("receive_grant_corrupt")
    return True


class ArtifactInputRepository:
    def __init__(self, workflows: WorkflowRepository, commands: CommandRepository, *, signing_key: bytes,
                 public_key: bytes, limits: TransferLimits = TransferLimits()) -> None:
        self.workflows, self.commands = workflows, commands
        self.signing_key, self.public_key = signing_key, public_key
        self.limits = limits
        self.workflows.artifact_input_public_key = public_key

    @contextmanager
    def download_slot(self, owner: str, node: str) -> Iterator[None]:
        try:
            with capacity_directory(self.workflows.db_path) as descriptor, ExitStack() as slots:
                with self.workflows.transaction(write=True) as connection:
                    check_capacity(connection, descriptor, owner=owner, node=node, limits=self.limits)
                    slots.enter_context(artifact_io_slot(descriptor, "artifact-download-global", 4))
                    slots.enter_context(artifact_io_slot(descriptor, node_download_key(owner, node), 2))
                yield
        except (ArtifactCapacityError, ArtifactSlotError) as exc:
            raise ArtifactReceiveError(exc.code) from exc

    def prepare(self, claim: CoordinatorClaim, *, expected_revision: int, resolved: ResolvedContract,
                generation: int, target: RegisteredNodeInputs, now: float,
                connection: sqlite3.Connection | None = None) -> bool:
        with self.workflows._connection(connection) as connection:
            workflow = self.workflows._fenced(connection, claim, now, expected_revision)
            self.workflows._creation_open(connection, workflow)
            node = connection.execute("SELECT n.enabled,ws.enabled FROM nodes n JOIN workspaces ws ON ws.owner_id=n.owner_id WHERE n.owner_id=? AND n.node_id=? AND ws.workspace_id=? LIMIT 1",
                (claim.owner_id, target.node_id, claim.workspace_id)).fetchone()
            if node is None or tuple(node) != (1, 1):
                raise ArtifactReceiveError("receive_node_unavailable")
            step = connection.execute("SELECT resolved_snapshot,resolved_digest,target_key,wait_deadline,state FROM workflow_steps WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=? AND is_current=1 LIMIT 1",
                (claim.owner_id, claim.workflow_id, resolved.step_key, generation)).fetchone()
            if step is None or step["state"] not in {"pending", "waiting_node", "ready", "waiting_approval", "paused"}:
                raise ArtifactReceiveError("receive_scope_unavailable")
            was_unresolved = step["resolved_snapshot"] is None
            if was_unresolved:
                self.workflows.freeze_inputs(claim, step_key=resolved.step_key, generation=generation, resolved=resolved,
                    expected_revision=expected_revision, now=now, connection=connection, input_target=target.node_id, inputs_pending=True)
            elif (bytes(step["resolved_snapshot"]), step["resolved_digest"], step["target_key"]) != (resolved.snapshot, resolved.digest, target.node_id):
                raise ArtifactReceiveError("receive_conflict")
            expiry = min(now + 1800, step["wait_deadline"] or now + resolved.contract.budget.max_wait_seconds)
            if expiry <= now:
                raise ArtifactReceiveError("receive_expired")
            aliases = {key: (alias, suffix) for key, alias, suffix in target.aliases}
            for ref_key, artifact in artifact_inputs(resolved):
                if ref_key not in aliases:
                    raise WorkflowPersistenceError("input_alias_unregistered")
                alias, suffix = aliases[ref_key]
                grant = receive_grant_id(claim.owner_id, claim.workflow_id, resolved.step_key, generation, target.node_id, ref_key)
                command = PlatformCommand.create(command_id="receive-" + grant, grant_id=grant,
                    owner_id=claim.owner_id, target_node=target.node_id, action=RECEIVE_ACTION,
                    resource_id=claim.workspace_id, retry_class="idempotent_with_key", expires_at=expiry,
                    arguments={"workflow_id": claim.workflow_id, "step_key": resolved.step_key, "generation": generation,
                        "ref_key": ref_key, "input_digest": resolved.digest, "artifact_id": artifact.artifact_id,
                        "sha256": artifact.sha256, "size": artifact.size, "input_alias": alias, "suffix": suffix}).signed(self.signing_key)
                self.commands.enqueue(command, idempotency_key=command.command_id, require_signature=True, connection=connection)
            ready = node_input_receipts_ready(connection, owner=claim.owner_id, workflow=claim.workflow_id,
                generation=generation, resolved=resolved, target=target.node_id, public_key=self.public_key)
            if not ready or was_unresolved:
                return False
            if step["state"] != "ready":
                connection.execute("UPDATE workflow_steps SET state='ready',reason_code=NULL,updated_at=? WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=?",
                    (now, claim.owner_id, claim.workflow_id, resolved.step_key, generation))
                connection.execute("UPDATE workflow_runs SET revision=revision+1,execution_state='running',reason_code=NULL,updated_at=? WHERE owner_id=? AND workflow_id=?", (now, claim.owner_id, claim.workflow_id))
                self.workflows._event(connection, claim.owner_id, claim.workflow_id, "step.inputs_received", {"step_key": resolved.step_key, "generation": generation, "target_key": target.node_id}, now)
                return False
            return True

    def _authorize(self, connection: sqlite3.Connection, command: dict[str, Any], *, worker: str | None, now: float) -> ReceiveRequest:
        request = ReceiveRequest.parse(command, node_id=command["target_node"], public_key=self.public_key, now=now, max_bytes=256 * 1024 * 1024)
        if (command["status"] not in {"leased", "accepted", "running"} or command["lease_owner"] != worker
                or command["lease_until"] is None or command["lease_until"] <= now):
            raise ArtifactReceiveError("receive_lease_unavailable")
        arguments = command["arguments"]
        row = connection.execute("SELECT s.resolved_snapshot,s.resolved_digest,s.target_key,s.state,s.wait_deadline,w.cancel_requested,m.admission_mode "
            "FROM workflow_steps s JOIN workflow_runs w USING(owner_id,workspace_id,workflow_id) "
            "JOIN workflow_schema_meta m ON m.singleton=1 "
            "JOIN workspaces ws ON ws.owner_id=s.owner_id AND ws.workspace_id=s.workspace_id AND ws.enabled=1 "
            "JOIN nodes n ON n.owner_id=s.owner_id AND n.node_id=s.target_key AND n.enabled=1 "
            "WHERE s.owner_id=? AND s.workspace_id=? AND s.workflow_id=? AND s.step_key=? AND s.generation=? AND s.is_current=1 LIMIT 1",
            (command["owner_id"], command["resource_id"], arguments["workflow_id"], arguments["step_key"], arguments["generation"])).fetchone()
        if (row is None or row["cancel_requested"] or row["admission_mode"] == "frozen" or row["state"] != "waiting_node"
                or row["target_key"] != command["target_node"] or row["resolved_digest"] != arguments["input_digest"]
                or row["wait_deadline"] is None or row["wait_deadline"] <= now):
            raise ArtifactReceiveError("receive_scope_unavailable")
        resolved = parse_resolved(bytes(row["resolved_snapshot"]))
        reference = dict(artifact_inputs(resolved)).get(arguments["ref_key"])
        if resolved.digest != arguments["input_digest"] or reference is None or (reference.artifact_id, reference.sha256, reference.size) != (request.artifact_id, request.sha256, request.size):
            raise ArtifactReceiveError("receive_scope_unavailable")
        expected = receive_grant_id(command["owner_id"], arguments["workflow_id"], arguments["step_key"], arguments["generation"], command["target_node"], arguments["ref_key"])
        if request.grant_id != expected:
            raise ArtifactReceiveError("receive_scope_unavailable")
        return request

    def authorize(self, grant_id: str, *, command_id: str, owner: str, node: str, worker: str,
                  credential_fingerprint: str, now: float) -> tuple[ReceiveRequest, str]:
        if command_id != "receive-" + grant_id:
            raise ArtifactReceiveError("receive_not_found")
        with self.workflows.transaction(write=False) as connection:
            row = connection.execute(f"SELECT {_COMMAND_COLUMNS} FROM platform_commands WHERE command_id=? AND owner_id=? AND target_node=? LIMIT 1", (command_id, owner, node)).fetchone()
            if row is None or row["grant_id"] != grant_id or row["action"] != RECEIVE_ACTION:
                raise ArtifactReceiveError("receive_not_found")
            if not node_credential_is_current(connection, owner=owner, node=node, fingerprint=credential_fingerprint):
                raise ArtifactReceiveError("receive_credential_revoked")
            command = self.commands._decode(row)
            return self._authorize(connection, command, worker=worker, now=now), command["resource_id"]

    def receipt_guard(self, connection: sqlite3.Connection, command: dict[str, Any], status: str,
                      result: dict[str, Any], worker: str | None, now: float,
                      credential_fingerprint: str | None) -> None:
        if not node_credential_is_current(connection, owner=command["owner_id"], node=command["target_node"],
                                          fingerprint=credential_fingerprint):
            raise ArtifactReceiveError("receive_credential_revoked")
        if command["status"] in {"succeeded", "failed", "unknown", "expired"}:
            if (status, result) != (command["status"], command["result"]):
                raise ArtifactReceiveError("invalid_receive_receipt")
            return
        request = self._authorize(connection, command, worker=worker, now=now)
        if status == "succeeded":
            validate_receipt(request, result)
