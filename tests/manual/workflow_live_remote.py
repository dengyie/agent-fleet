"""Opt-in real Linux Node extension of the original commercial CLI pilot."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tests/fixtures/workflow"), str(ROOT / "tests"), str(ROOT)]

from remote_test_probe import NativeNode  # noqa: E402
from remote_test_node_canary import pilot_definition  # noqa: E402
from fleet_core.platform_command import verify_command  # noqa: E402
from hub.composition.workflows import wire_workflows  # noqa: E402
from hub.domain.artifact_receive import RegisteredNodeInputs  # noqa: E402
from hub.domain.capability import NodePolicy, SelectionRequirements  # noqa: E402
from hub.domain.capability_execution import CapabilityExecutionContext, RegisteredCapabilityAction  # noqa: E402
from hub.domain.capability_routing import RegisteredCapabilityRoute  # noqa: E402
from hub.domain.remote_workflow_test import REMOTE_TEST_ACTION  # noqa: E402
from hub.domain.workflow_contract_types import CatalogRef  # noqa: E402
from hub.infrastructure.workflow_run_executor import RegisteredRunTarget, RunStepExecutor  # noqa: E402
from hub.application.workflow_coordinator import RegisteredWorkflowPlan  # noqa: E402


class RemotePilot:
    def __init__(self, evidence: Path, config: Path) -> None:
        settings = json.loads(config.read_text(encoding="utf-8"))
        assert set(settings) == {"host", "checkout", "python", "path", "root"}
        assert settings["host"] == "mesh-huawei-sg", "only the existing authorized acceptance host is supported"
        for key in ("checkout", "python", "root"):
            assert Path(settings[key]).is_absolute() and ".." not in Path(settings[key]).parts
        for key, name in (("host", "HOST"), ("checkout", "CHECKOUT"), ("python", "PYTHON"), ("path", "PATH")):
            os.environ["FLEET_REMOTE_TEST_" + name] = settings[key]
        self.evidence = evidence / "remote-node"
        self.evidence.mkdir(mode=0o700)
        self.node = NativeNode(self.evidence, Path(settings["root"]))
        self.prepared = False
        self.received = False
        self.executed = False

    def register(self, app, owner, definition, targets, adapter):
        fleet = app.extensions["fleet"]
        self.owner, self.workspace = owner, targets[0].workspace_id
        self.repository = fleet["repositories"]["workflows"]
        self.platform = fleet["platform_repository"]
        remote = replace(targets[2], target_key="node-a", config_snapshot=b'{"execution_node_id":"node-a"}', remote_test=True)
        assert isinstance(remote, RegisteredRunTarget)
        self.targets = (*targets[:2], remote)
        cap = pilot_definition()
        reference = CatalogRef(cap.capability_id, cap.revision)
        self.platform.upsert_node(owner, {"node_id": "node-a"})
        self.node.control["credential"] = self.platform.provision_node_credential(owner, "node-a")["credential"]
        policy = NodePolicy(owner, "node-a", (self.workspace,), (CatalogRef(definition.template_id, definition.revision),),
            (reference,), slot_limit=1, input_aliases=("source",), residency_aliases=("private",), allowed_risks=("workspace_read",))
        selection = SelectionRequirements(owner_id=owner, workspace_id=self.workspace, template=CatalogRef("coding-delivery", 1),
            owner_role="operator", adapter_id="fixed-tests", capabilities=definition.steps[2].contract.capability_requirements,
            input_digest="a" * 64, operating_systems=("linux",), architectures=(), software=(),
            input_aliases=("source",), residency_aliases=("private",), memory_bytes=0, disk_bytes=0)
        plan_digest = hashlib.sha256(definition.snapshot).hexdigest()
        plans = tuple(RegisteredWorkflowPlan(owner, self.workspace, plan_digest, key, target.adapter_ref,
            target.policy_ref, target.target_key, 1, 0, 1) for key, target in zip(("develop", "review"), targets[:2]))
        route = RegisteredCapabilityRoute(plan_digest, "test", remote.adapter_ref, remote.policy_ref, selection, 0, 0)
        task_repository = fleet["services"]["workflow_coding_delivery"].task_repository
        wire_workflows(app, fleet["config"], {
            "workflows": self.repository, "task_repository": task_repository,
            "workflow_templates": fleet["services"]["workflows"].catalog,
            "workflow_steps": (*plans, route),
            "workflow_executors": tuple((owner, self.workspace, target.target_key,
                RunStepExecutor(self.repository, target, clock=time.time)) for target in self.targets),
            "workflow_node_inputs": (RegisteredNodeInputs(owner, self.workspace, "node-a", (("source", "source", ".tar"),)),),
            "workflow_capability_definitions": (cap,), "workflow_capability_nodes": (policy,),
            "workflow_capability_actions": (RegisteredCapabilityAction("fixed-tests", reference, REMOTE_TEST_ACTION),),
            "workflow_review_adapters": (adapter,),
        })
        self.services = fleet["services"]
        self.coordinator = self.services["workflow_coordinator"]
        assert self.coordinator.dispatch_enabled, "manual remote pilot must explicitly enable dispatch"
        self.commands = self.services["platform_delivery"].repository
        worker = self.services["platform_worker"]
        assert worker.remote_execution_enabled and worker.workflow_remote_test_executor is self.services["workflow_remote_test_executor"]
        worker.provider_factory = lambda _: (_ for _ in ()).throw(RuntimeError("unregistered_model_forbidden"))
        # The existing receipt waiter invokes the actual Node only after enqueue.
        self.services["platform_delivery"].sleep = self.deliver
        return self.coordinator, worker, adapter.spec.payload()

    def connect(self, origin: str, public_key: bytes) -> None:
        self.public_key = public_key
        self.node.control.update(hub_url=origin, public_key=public_key.hex(), now=time.time())
        self.facts = self.node.operate("facts")

    def rows(self, table: str) -> list[dict]:
        assert table in {"workflow_steps", "runs", "platform_commands", "capability_reservations", "capability_step_bindings"}
        with self.repository.transaction(write=False) as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM " + table).fetchall()]

    def advance(self) -> None:
        steps = self.rows("workflow_steps")
        if not self.prepared and any(row["step_key"] == "review" and row["state"] == "succeeded" for row in steps):
            self.node.control["now"] = time.time()
            self.offer = self.node.operate("prepare")
            for key, route in tuple(self.coordinator.registrations.items()):
                if isinstance(route, RegisteredCapabilityRoute):
                    self.coordinator.registrations[key] = replace(route, selection=replace(route.selection,
                        architectures=(self.offer["offer"]["architecture"],),
                        software=tuple(sorted(self.offer["offer"]["software"].items()))))
            self.prepared = True
        receiving = [row for row in self.rows("platform_commands") if row["action"] == "artifact.receive"]
        if receiving and not self.received:
            assert self.prepared and len(receiving) == 1
            self.node.control["now"] = time.time()
            self.receive = self.node.operate("receive")
            self.received = True

    def deliver(self, _: float) -> None:
        if self.executed:
            time.sleep(0.02)
            return
        commands = [row for row in self.rows("platform_commands") if row["action"] == REMOTE_TEST_ACTION]
        assert len(commands) == 1 and self.received
        self.node.control["now"] = time.time()
        self.execution = self.node.operate("execute", command_id=commands[0]["command_id"])
        self.executed = True

    def verify(self, detail: dict, report: dict) -> dict:
        assert self.prepared and self.received and self.executed
        commands = [self.commands.get(row["command_id"]) for row in self.rows("platform_commands")]
        assert len(commands) == 2 and all(row["status"] == "succeeded" and verify_command(row, self.public_key) for row in commands)
        command = next(row for row in commands if row["action"] == REMOTE_TEST_ACTION)
        assert CapabilityExecutionContext.from_command(command).binding.offer_digest == self.offer["offer_digest"]
        bundle = next(item for step in detail["steps"] for item in step["outputs"] if item["output_key"] == "source_bundle")
        assert bundle["sha256"] == self.receive["source_sha256"] == command["arguments"]["receive_receipt"]["sha256"]
        assert self.execution["executions"] == self.execution["preflights"] == 1
        assert self.execution["journal"]["receipt_pending"] is False
        assert report["passed"] is True
        test_steps = [row for row in self.rows("workflow_steps") if row["step_key"] == "test"]
        assert len(test_steps) == 1 and test_steps[0]["target_key"] == "node-a" and test_steps[0]["state"] == "succeeded"
        reservations = self.rows("capability_reservations")
        assert len(reservations) == 3 and {row["step_key"] for row in reservations} == {"develop", "review", "test"}
        assert all(row["state"] == "released" for row in reservations)
        assert len(self.rows("capability_step_bindings")) == 1
        runs = self.rows("runs")
        assert len(runs) == 3 and all(row["state"] == "succeeded" for row in runs)
        replay = self.node.operate("replay", command=command)
        assert replay["executions"] == replay["preflights"] == replay["downloads"] == 0
        assert replay["journal"] == self.execution["journal"]
        assert self.execution["host_secret_preserved"] and replay["host_secret_preserved"]
        result = {"status": "passed", "hostname": self.facts["hostname"], "node_root": str(self.node.root),
            "platform": self.facts["platform"], "python": self.facts["python"],
            "signed_commands": [{"command_id": row["command_id"], "action": row["action"], "status": row["status"]} for row in commands],
            "source_sha256": bundle["sha256"], "test_executions": 1, "replay_executions": 0,
            "reservation": "released", "same_workflow_develop_review_remote_test": True,
            "human_acceptance": "not_performed", "production_release_ready": False}
        (self.evidence / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        return result
