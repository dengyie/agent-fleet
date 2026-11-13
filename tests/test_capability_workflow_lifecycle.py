"""Automatic capability Workflow execution and deterministic reconciliation."""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.domain.artifact_receive import RegisteredNodeInputs
from hub.domain.capability import NodePolicy, SelectionRequirements
from hub.domain.capability_execution import RegisteredCapabilityAction
from hub.domain.capability_routing import RegisteredCapabilityRoute
from fleet_core.control import generate_ed25519_keypair
from hub.domain.workflow_contract import parse_definition
from hub.domain.workflow_contract_types import ArtifactReference, CapabilityRequirement, CatalogRef
from hub.infrastructure.capability_repository import CapabilityRepository
from hub.infrastructure.platform_db import PlatformRepository
from hub.infrastructure.workflow_repository import WorkflowRepository, WorkflowRepositoryError
from hub.infrastructure.workflow_control_repository import WorkflowControlRepository
from hub.domain.workflow_control import WorkflowControlRequest
from hub.infrastructure.workflow_run_executor import RegisteredRunTarget, RunStepExecutor
from test_capability_router import OWNER, WORKSPACE
from test_workflow_repository import prepare_database, registered
from test_workflow_two_nodes import server
from fixtures.workflow.capability_node_canary import _BoundedTransport, pilot_definitions
from tools.platform.node_capabilities import LocalCapability, NodeCapabilityConfig
from tools.platform.node_runtime import NodeRuntime, NodeRuntimeConfig
from tools.platform.artifact_transfer import InputDirectory
from hub.platform.artifacts import ArtifactStore
from hub.platform.providers.base import ModelResponse


WRITE, DIGEST = pilot_definitions()
WRITE_REF = CatalogRef(WRITE.capability_id, WRITE.revision)
DIGEST_REF = CatalogRef(DIGEST.capability_id, DIGEST.revision)
CAPABILITY_REFS = (WRITE_REF, DIGEST_REF)
CAPABILITY_REQUIREMENTS = tuple(CapabilityRequirement(ref.id, (ref.revision,)) for ref in CAPABILITY_REFS)


class FixedProvider:
    """One fixed write call, followed by a final response when reached."""

    def __init__(self, *, interrupt: bool) -> None:
        self.interrupt = interrupt
        self.calls = 0

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, request_observer=None) -> ModelResponse:
        self.calls += 1
        usage = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
        if self.calls == 1:
            return ModelResponse(kind="tool_call", tool="workspace.write",
                arguments={"path": "report.txt", "content": "fixed output"}, usage=usage)
        if self.interrupt:
            raise AssertionError("unknown write must stop before provider replay")
        return ModelResponse(kind="final", text="fixed final response", usage=usage)


class NodeWaiter:
    def __init__(self, runtime: NodeRuntime, delivery: Any) -> None:
        self.runtime = runtime
        self.delivery = delivery
        self.polls = 0

    def wait_for_receipt(self, command_id: str, *, timeout_s: float = 30.0, should_cancel=None) -> dict[str, Any]:
        self.polls += 1
        result = self.runtime.poll_once(lease_s=30)
        assert result["ok"] is True
        assert result["commands"] == 1
        row = self.delivery.repository.get(command_id)
        assert row is not None
        assert row["status"] in {"succeeded", "unknown", "failed"}
        return row


@dataclass
class Lifecycle:
    app: Any
    workflows: WorkflowRepository
    platform: PlatformRepository
    capabilities: CapabilityRepository
    clock: list[float]
    credential: str
    public_key: bytes
    runtime: NodeRuntime | None = None
    provider: FixedProvider | None = None
    input_artifact: ArtifactReference | None = None

    def tick(self) -> Any:
        result = asyncio.run(self.app.extensions["fleet"]["services"]["workflow_coordinator"].tick(self.clock[0]))
        self.clock[0] += 1
        assert not result.failures, [(error.code, error.__cause__) for error in result.failures]
        return result

    def count(self, table: str) -> int:
        with self.workflows.transaction(write=False) as connection:
            return int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])

    def run_id(self) -> str:
        with self.platform._connect() as connection:
            row = connection.execute("SELECT run_id FROM runs ORDER BY created_at,run_id LIMIT 1").fetchone()
        assert row is not None
        return str(row[0])

    def lifecycle_snapshot(self) -> tuple[Any, ...]:
        with self.workflows.transaction(write=False) as connection:
            row = connection.execute(
                "SELECT d.stage,d.token,d.payload_hash,d.binding_id,d.binding_run_id,s.state,s.reason_code,"
                "r.execution_state,r.reason_code,r.validation_state,r.acceptance_state,"
                "c.state,b.settlement "
                "FROM workflow_dispatches d "
                "JOIN workflow_steps s USING(owner_id,workspace_id,workflow_id,step_key,generation) "
                "JOIN workflow_runs r USING(owner_id,workspace_id,workflow_id) "
                "JOIN capability_reservations c USING(owner_id,workspace_id,workflow_id,step_key,generation) "
                "JOIN workflow_budget_reservations b USING(owner_id,workspace_id,workflow_id,step_key,generation) "
                "WHERE d.owner_id=? AND d.workflow_id=? AND d.step_key='develop' AND d.generation=1",
                (OWNER, "workflow-a"),
            ).fetchone()
        assert row is not None
        return tuple(row)


def _workflow_definition(source: ArtifactReference | None = None) -> Any:
    raw = json.loads(registered().snapshot)
    raw["steps"][0]["kind"] = "native_run"
    if source is not None:
        raw["steps"][0]["contract"]["input_refs"] = [{
            "ref_key": "source", "kind": "artifact", "artifact_id": source.artifact_id,
            "sha256": source.sha256, "size": source.size}]
    raw["steps"][0]["contract"]["capability_requirements"] = [
        {"id": item.id, "revisions": [item.revision]} for item in CAPABILITY_REFS
    ]
    return parse_definition(json.dumps(raw, separators=(",", ":")).encode("utf-8"))


def _runtime(tmp_path: Path, setup: Lifecycle, url: str, *, transport: Any = None) -> NodeRuntime:
    registrations = tuple(LocalCapability(definition, (action,)) for definition, action in (
        (WRITE, "tool.workspace.write"), (DIGEST, "reconcile.workspace.digest")))
    local = NodeCapabilityConfig(1, registrations, CAPABILITY_REFS, slots=1)
    return NodeRuntime(NodeRuntimeConfig(workspace_id=WORKSPACE,
        node_id="node-a", credential=setup.credential,
        workspace_root=tmp_path / "node-workspace", journal_path=tmp_path / "node.db",
        hub_url=url, public_key=setup.public_key, worker_id="lifecycle-node",
        capabilities=frozenset({"workspace.write", "reconcile.workspace.digest"}) |
            (frozenset({"artifact.receive"}) if setup.input_artifact is not None else frozenset()),
        artifact_inputs=(("source", InputDirectory(tmp_path / "node-inputs", frozenset({".txt"}))),)
            if setup.input_artifact is not None else (),
        capability_config=local,
    ), transport=transport or _BoundedTransport(mode="direct", attempts=1), clock=lambda: setup.clock[0])


def _setup(tmp_path: Path, *, input_bytes: bytes | None = None) -> Lifecycle:
    workflows = prepare_database(tmp_path / "platform.db")
    platform_repo = PlatformRepository(workflows.db_path)
    platform_repo.upsert_workspace(OWNER, {
        "workspace_id": WORKSPACE, "name": "lifecycle", "backend": "directory",
        "root_path": str(tmp_path / "hub-workspace"), "enabled": True,
    })
    (tmp_path / "hub-workspace").mkdir(mode=0o700)
    platform_repo.upsert_model(OWNER, {"profile_id": "fixture", "provider": "deterministic", "model": "fixture"})
    platform_repo.upsert_node(OWNER, {"node_id": "node-a", "capabilities": {"workspace.write": True}})
    credential = platform_repo.provision_node_credential(OWNER, "node-a", secret="a" * 32)["credential"]

    source = None
    if input_bytes is not None:
        (tmp_path / "node-inputs").mkdir(mode=0o700)
        input_file = tmp_path / "input.txt"
        input_file.write_bytes(input_bytes)
        manifest = ArtifactStore(tmp_path / "artifacts").put_file(OWNER, WORKSPACE, input_file)
        source = ArtifactReference(manifest["artifact_id"], manifest["sha256"], manifest["size"])
    definition = _workflow_definition(source)
    workflows.create_or_get(OWNER, WORKSPACE, "workflow-a", client_token="workflow-a", definition=definition, now=100)
    platform_repo.create_conversation(OWNER, "chat-node-a", title="node-a", workspace_id=WORKSPACE)
    capabilities = CapabilityRepository(workflows, boot_id="lifecycle-boot")
    capabilities.init()
    for item in (WRITE, DIGEST):
        capabilities.register_definition(item, now=100)
    capabilities.register_node(NodePolicy(
        OWNER, "node-a", (WORKSPACE,), (CatalogRef("fixture", 1),), CAPABILITY_REFS,
        slot_limit=1, input_aliases=("source",) if source is not None else (),
        allowed_risks=("workspace_read", "workspace_write")), now=100)

    clock = [100.0]
    target = RegisteredRunTarget(
        OWNER, WORKSPACE, "node-a", CatalogRef("cli", 1), CatalogRef("safe", 1), "chat-node-a",
        json.dumps({"execution_node_id": "node-a", "model_profile_id": "fixture",
                    "provider_snapshot": {"provider": "deterministic", "model": "fixture",
                                          "provider_config": {}, "capabilities": {}}},
                   separators=(",", ":")).encode("utf-8"))
    executor = RunStepExecutor(workflows, target, clock=lambda: clock[0])
    requirements = SelectionRequirements(
        OWNER, WORKSPACE, CatalogRef("fixture", 1), "operator", "cli", CAPABILITY_REQUIREMENTS,
        "0" * 64, input_aliases=("source",) if source is not None else (),
        max_active_seconds=60, result_limit_bytes=65536)
    route = RegisteredCapabilityRoute(
        hashlib.sha256(definition.snapshot).hexdigest(), "develop", CatalogRef("cli", 1),
        CatalogRef("safe", 1), requirements, 100, 2)
    private, public = generate_ed25519_keypair()
    app = create_app(FleetConfig.from_root(
        tmp_path, platform_enabled=True, platform_workflows_enabled=True,
        platform_workflow_dispatch_enabled=True, platform_capability_routing_enabled=True,
        platform_worker_enabled=True, platform_worker_lease_s=120,
        platform_remote_execution_enabled=True, platform_command_signing_raw=private,
        platform_artifact_transfer_enabled=source is not None,
        platform_require_command_signature=True, platform_db=workflows.db_path,
        platform_artifact_root=tmp_path / "artifacts", dev_operator=OWNER, serve_frontend=False,
    ), repositories={
        "workflows": workflows, "capabilities": capabilities,
        "workflow_clock": lambda: clock[0], "workflow_steps": (route,),
        "workflow_executors": ((OWNER, WORKSPACE, "node-a", executor),),
        "workflow_node_inputs": (RegisteredNodeInputs(OWNER, WORKSPACE, "node-a",
            (("source", "source", ".txt"),)),) if source is not None else (),
        "workflow_capability_actions": (
            RegisteredCapabilityAction("cli", WRITE_REF, "tool.workspace.write"),
            RegisteredCapabilityAction("cli", DIGEST_REF, "reconcile.workspace.digest"),
        ),
    })
    services = app.extensions["fleet"]["services"]
    delivery = services["platform_delivery"]
    delivery.clock = delivery.repository.clock = lambda: clock[0]
    services["command_postcheck"].clock = lambda: clock[0]
    services["run_events"].clock = lambda: clock[0]
    worker = services["platform_worker"]
    worker.clock = lambda: clock[0]
    worker.lease_s = 120.0
    worker.resource_leases.clock = worker.clock
    return Lifecycle(app, workflows, platform_repo, capabilities, clock, credential, public, input_artifact=source)


def _admit_and_create_run(setup: Lifecycle) -> None:
    for _ in range(8):
        setup.tick()
        if setup.count("runs") == 1:
            return
    raise AssertionError("automatic capability admission did not create a Run")


def _execute(setup: Lifecycle, tmp_path: Path, *, interrupt: bool, url: str) -> str:
    runtime = _runtime(tmp_path, setup, url)
    setup.runtime = runtime
    setup.provider = FixedProvider(interrupt=interrupt)
    worker = setup.app.extensions["fleet"]["services"]["platform_worker"]
    worker.provider_factory = lambda profile: setup.provider
    worker.remote_waiter = NodeWaiter(runtime, setup.app.extensions["fleet"]["services"]["platform_delivery"])
    runtime.report_capabilities()
    _admit_and_create_run(setup)
    if interrupt:
        original_write = runtime.backend.write

        def write_then_raise(path: str, content: bytes) -> Any:
            receipt = original_write(path, content)
            assert receipt.state == "succeeded"
            raise OSError("write completed before receipt was lost")

        runtime.backend.write = write_then_raise
    result = worker.run_once(OWNER)
    assert result is not None
    assert result["state"] == ("unknown" if interrupt else "succeeded")
    run_id = setup.run_id()
    setup.tick()
    return run_id


def test_automatic_capability_workflow_success_releases_only_after_terminal_run(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    with server(setup.app) as url:
        try:
            run_id = _execute(setup, tmp_path, interrupt=False, url=url)
            assert setup.provider is not None and setup.provider.calls == 2
            assert (tmp_path / "node-workspace/report.txt").read_bytes() == b"fixed output"
            assert setup.lifecycle_snapshot()[0] == "bound"
            state = setup.lifecycle_snapshot()
            assert state[5:11] == ("succeeded", None, "succeeded", "outputs_unavailable", "not_requested", "pending")
            assert state[11:] == ("released", "settled")
            assert setup.count("workflow_artifact_refs") == 0
            assert setup.count("workflow_evidence") == 0
            command = setup.app.extensions["fleet"]["services"]["platform_delivery"].repository.get(run_id + ":step:1")
            assert command is not None and command["status"] == "succeeded"
        finally:
            if setup.runtime is not None:
                setup.runtime.close_all()


@pytest.mark.parametrize("outcome", ("matched", "mismatch", "read_failed"))
def test_unknown_write_postcheck_is_read_only_and_preserves_held_binding(
    tmp_path: Path, outcome: str,
) -> None:
    setup = _setup(tmp_path)
    with server(setup.app) as url:
        try:
            run_id = _execute(setup, tmp_path, interrupt=True, url=url)
            assert setup.provider is not None and setup.provider.calls == 1
            output = tmp_path / "node-workspace/report.txt"
            original_bytes = output.read_bytes()
            before = setup.lifecycle_snapshot()
            assert before[0] == "unknown" and before[5:9] == ("unknown", None, "unknown", None)
            assert before[11:] == ("held", "unknown")
            current = setup.workflows.get(OWNER, "workflow-a")
            with pytest.raises(WorkflowRepositoryError) as retry_error:
                WorkflowControlRepository(setup.workflows).control(
                    WorkflowControlRequest(OWNER, "workflow-a", "retry", current.revision,
                                            "blocked-retry", "develop", 1), now=setup.clock[0])
            assert getattr(retry_error.value, "code", None) == "unresolved_execution"
            assert setup.lifecycle_snapshot() == before
            if outcome != "matched":
                output.rename(tmp_path / "node-workspace/retained-original.txt")
            if outcome == "mismatch":
                output.write_bytes(b"different fixed fixture bytes")
            command_id = run_id + ":step:1"
            client = setup.app.test_client()
            response = client.post(f"/api/platform/v1/commands/{command_id}/postcheck")
            assert response.status_code == 202, response.get_json()
            postcheck = response.get_json()["postcheck"]
            assert postcheck["state"] == "pending"
            runtime = setup.runtime
            assert runtime is not None
            digest_calls = 0
            runtime.backend.write = lambda path, content: (_ for _ in ()).throw(AssertionError("postcheck must not write"))
            original_digest = runtime.backend.digest
            def counted_digest(path: str) -> Any:
                nonlocal digest_calls
                digest_calls += 1
                return original_digest(path)
            runtime.backend.digest = counted_digest
            poll = runtime.poll_once(lease_s=30)
            assert poll["ok"] is True and poll["commands"] == 1
            checked = client.get(f"/api/platform/v1/commands/{command_id}/postcheck")
            assert checked.status_code == 200, checked.get_json()
            expected = {"matched": "matched", "mismatch": "mismatch", "read_failed": "remains_unknown"}[outcome]
            assert checked.get_json()["postcheck"]["state"] == expected
            assert setup.lifecycle_snapshot() == before
            original = setup.app.extensions["fleet"]["services"]["platform_delivery"].repository.get(command_id)
            check_id = postcheck["check_command_id"]
            check = setup.app.extensions["fleet"]["services"]["platform_delivery"].repository.get(check_id)
            assert original is not None and original["status"] == "unknown"
            assert check is not None and check["status"] == ("failed" if outcome == "read_failed" else "succeeded")
            assert digest_calls == 1
            assert client.get(f"/api/platform/v1/commands/{command_id}/postcheck").get_json() == checked.get_json()
            assert runtime.poll_once()["commands"] == 0
            runtime.close_all()
            restarted = _runtime(tmp_path, setup, url)
            restart_io = 0
            def no_restart_io(*arguments: Any) -> Any:
                nonlocal restart_io
                restart_io += 1
                raise AssertionError("terminal journals must not repeat I/O")
            restarted.backend.write = restarted.backend.digest = no_restart_io
            assert restarted.client.handle(setup.app.extensions["fleet"]["services"]["platform_delivery"].repository.get(command_id))[
                "status"] == "unknown"
            assert restarted.client.handle(setup.app.extensions["fleet"]["services"]["platform_delivery"].repository.get(check_id))[
                "status"] == ("failed" if outcome == "read_failed" else "succeeded")
            assert restart_io == 0
            restarted.close_all()
            assert hashlib.sha256(original_bytes).hexdigest() == hashlib.sha256(b"fixed output").hexdigest()
        finally:
            if setup.runtime is not None:
                setup.runtime.close_all()
