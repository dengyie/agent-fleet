"""Opt-in Linux fixed-test capability acceptance over the original HTTP boundary."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import time
from typing import Any
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from remote_test_node_canary import pilot_definition
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.domain.artifact_receive import RegisteredNodeInputs
from hub.domain.capability import NodePolicy
from hub.domain.capability_execution import CapabilityExecutionContext, RegisteredCapabilityAction
from hub.domain.capability_routing import RegisteredCapabilityRoute
from fleet_core.control import generate_ed25519_keypair
from fleet_core.platform_command import verify_command
from hub.domain.remote_workflow_test import REMOTE_TEST_ACTION, RemoteTestResult
from hub.domain.workflow_contract_types import CatalogRef
from hub.infrastructure.capability_repository import CapabilityRepository
from hub.infrastructure.workflow_run_executor import RegisteredRunTarget, RunStepExecutor
from test_capability_router import requirements
from test_workflow_repository import OWNER, WORKSPACE
from test_workflow_test_execution import ExecutionFixture
from test_workflow_two_nodes import server


class NativeNode:
    def __init__(self, evidence: Path, root: Path) -> None:
        self.evidence, self.root = evidence, root
        self.checkout = Path(__file__).resolve().parents[3]
        self.reports: dict[str, dict[str, Any]] = {}
        self.control: dict[str, Any] = {"credential": "node-a:" + "n" * 32, "root": str(root)}

    def operate(self, operation: str, **values: Any) -> dict[str, Any]:
        assert operation not in self.reports
        config = self.control | values | {"operation": operation}
        canary = "tests/fixtures/workflow/remote_test_node_canary.py"
        remote = os.environ.get("FLEET_REMOTE_TEST_HOST")
        if remote:
            checkout = os.environ["FLEET_REMOTE_TEST_CHECKOUT"]
            command = "cd " + shlex.quote(checkout) + " && env LC_ALL=C PYTHONUTF8=1 PATH="
            command += shlex.quote(os.environ["FLEET_REMOTE_TEST_PATH"])
            command += " setpriv --bounding-set=-dac_override,-dac_read_search --no-new-privs "
            command += shlex.join([os.environ["FLEET_REMOTE_TEST_PYTHON"], canary])
            forwarding = []
            if operation != "facts":
                port = config["hub_url"].rsplit(":", 1)[1]
                forwarding = ["-o", "ExitOnForwardFailure=yes", "-R", port + ":127.0.0.1:" + port]
            arguments = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12", *forwarding, remote, command]
        else:
            assert sys.platform == "linux", "select an authorized Linux host or run this fixture on Linux"
            arguments = [sys.executable, canary]
        raw = json.dumps(config, allow_nan=False).encode("utf-8")
        assert len(raw) <= 65536
        started = time.monotonic()
        result = subprocess.run(arguments, cwd=self.checkout, input=raw, capture_output=True, timeout=120)
        (self.evidence / (operation + ".stdout")).write_bytes(result.stdout)
        (self.evidence / (operation + ".stderr")).write_bytes(result.stderr)
        (self.evidence / (operation + "-process.json")).write_text(json.dumps({"argv": arguments,
            "exit_code": result.returncode, "elapsed_seconds": time.monotonic() - started,
            "stdin_bytes": len(raw), "stdin_keys": sorted(config)}, indent=2) + "\n", encoding="utf-8")
        assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
        assert len(result.stdout) <= 65536 and config["credential"].encode() not in result.stdout
        report = json.loads(result.stdout)
        assert report["operation"] == operation and report["platform"] == "linux"
        assert type(report["pid"]) is int and report["pid"] > 0
        if remote:
            assert report["hostname"].lower() != socket.gethostname().lower()
        else:
            assert report["pid"] != os.getpid()
        self.reports[operation] = report
        return report


def _source(root: Path, namespaces: dict[str, int], *, passed: bool) -> str:
    return (
        "import errno,os,socket\nfrom pathlib import Path\nimport pytest\n"
        "def test_environment():\n"
        "    assert 'FLEET_TEST_SECRET' not in os.environ\n"
        "    assert 'X-Platform-Node-Credential' not in os.environ\n"
        "    status=dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)\n"
        "    assert all(int(status[key],16)==0 for key in ('CapEff','CapPrm','CapBnd'))\n"
        "    assert status['NoNewPrivs'].strip()=='1' and status['Seccomp'].strip()=='2'\n"
        "    namespaces=" + repr(namespaces) + "\n"
        "    assert all(os.stat('/proc/self/ns/'+name).st_ino!=inode for name,inode in namespaces.items())\n"
        "    (Path(os.environ['HOME'])/'allowed').write_text('scratch')\n"
        "    (Path(os.environ['TMPDIR'])/'allowed').write_text('scratch')\n"
        "@pytest.mark.parametrize('operation',['read','write','source','socket'])\n"
        "def test_boundary(operation):\n"
        "    operations={'read':lambda:Path(" + repr(str(root / "host-secret")) + " ).read_bytes(),\n"
        "        'write':lambda:Path(" + repr(str(root / "host-write")) + " ).write_text('denied'),\n"
        "        'source':lambda:Path(__file__).with_name('denied').write_text('denied'),\n"
        "        'socket':lambda:socket.socket()}\n"
        "    with pytest.raises(OSError) as denied:\n"
        "        operations[operation]()\n"
        "    assert denied.value.errno in {errno.ENOENT,errno.EPERM,errno.EACCES,errno.EROFS}\n"
        "def test_delivery():\n"
        "    assert " + repr(passed) + "\n"
    )


class NativeHub(ExecutionFixture):
    def __init__(self, root: Path, *, content: str) -> None:
        super().__init__(root, content=content)
        self.private, self.public = generate_ed25519_keypair()
        definition = pilot_definition()
        cap = CatalogRef(definition.capability_id, definition.revision)
        self.capabilities = CapabilityRepository(self.repository, boot_id="native-remote-test")
        self.capabilities.init()
        self.capabilities.register_definition(definition, now=self.now)
        self.platform.upsert_node(OWNER, {"node_id": "node-a"})
        self.credential = self.platform.provision_node_credential(OWNER, "node-a", secret="n" * 32)["credential"]
        self.capabilities.register_node(NodePolicy(OWNER, "node-a", (WORKSPACE,), (CatalogRef("coding-delivery", 1),),
            (cap,), slot_limit=1, input_aliases=("source",), residency_aliases=("private",),
            allowed_risks=("workspace_read",)), now=self.now)
        self.target = RegisteredRunTarget(OWNER, WORKSPACE, "node-a", CatalogRef("fixed-tests", 1),
            CatalogRef("coding-delivery", 1), "test-execution", b'{"execution_node_id":"node-a"}',
            test_recipe=CatalogRef("coding-delivery-tests", 1), remote_test=True)
        self.executor = RunStepExecutor(self.repository, self.target, clock=self.clock)
        self.route = RegisteredCapabilityRoute(hashlib.sha256(self.definition.snapshot).hexdigest(), "test",
            self.target.adapter_ref, self.target.policy_ref, requirements(template=CatalogRef("coding-delivery", 1),
                adapter_id="fixed-tests", capabilities=self.definition.steps[2].contract.capability_requirements,
                software=(), architectures=(), memory_bytes=0), 0, 0)
        self.app = create_app(FleetConfig.from_root(root, platform_enabled=True, platform_workflows_enabled=True,
            platform_workflow_dispatch_enabled=True, platform_worker_enabled=True, platform_capability_routing_enabled=True,
            platform_remote_execution_enabled=True, platform_artifact_transfer_enabled=True,
            platform_command_signing_raw=self.private, platform_db=self.repository.db_path,
            platform_artifact_root=self.store.root, dev_operator=OWNER, serve_frontend=False), repositories={
                "workflows": self.repository, "capabilities": self.capabilities, "workflow_clock": self.clock,
                "workflow_steps": (self.route,), "workflow_executors": ((OWNER, WORKSPACE, "node-a", self.executor),),
                "workflow_node_inputs": (RegisteredNodeInputs(OWNER, WORKSPACE, "node-a", (("source", "source", ".tar"),)),),
                "workflow_capability_actions": (RegisteredCapabilityAction("fixed-tests", cap, REMOTE_TEST_ACTION),)})
        self.services = self.app.extensions["fleet"]["services"]
        self.commands = self.services["platform_delivery"].repository
        self.commands.clock = self.services["platform_delivery"].clock = self.clock
        self.coordinator = self.services["workflow_coordinator"]
        self.worker_service = self.services["platform_worker"]
        self.worker_service.clock = self.clock
        self.worker_service.run_events.clock = self.clock
        self.worker_service.provider_factory = lambda profile: pytest.fail("remote fixed test must not call the model")
        assert self.repository.release_claim(self.claim, now=self.now, next_poll_at=self.now)

    def tick(self) -> None:
        result = asyncio.run(self.coordinator.tick(self.now))
        self.now += 1
        assert not result.failures, [(failure.code, repr(failure.__cause__)) for failure in result.failures]

    def count(self, table: str) -> int:
        assert table in {"runs", "platform_commands", "capability_step_bindings", "capability_reservations"}
        scope = " WHERE step_key='test'" if table.startswith("capability_") else ""
        with self.repository.transaction(write=False) as connection:
            return connection.execute("SELECT count(*) FROM " + table + scope).fetchone()[0]

    def reservation(self) -> str:
        with self.repository.transaction(write=False) as connection:
            return connection.execute("SELECT state FROM capability_reservations WHERE step_key='test'").fetchone()[0]


@pytest.mark.parametrize("outcome", ("passed", "failed", "lost", "unknown"))
def test_native_remote_fixed_test_original_workflow(tmp_path: Path, outcome: str) -> None:
    evidence_root = Path(os.environ["FLEET_REMOTE_TEST_EVIDENCE"])
    assert evidence_root.is_absolute() and evidence_root.is_dir()
    case = "remote-test-" + outcome + "-" + os.urandom(8).hex()
    evidence = evidence_root / case
    evidence.mkdir(mode=0o700)
    remote_root = os.environ.get("FLEET_REMOTE_TEST_ROOT")
    node_root = Path(remote_root) / case if remote_root else tmp_path / "native-node"
    child = NativeNode(evidence, node_root)
    facts = child.operate("facts")
    hub_root = evidence / "hub"
    hub_root.mkdir(mode=0o700)
    hub = NativeHub(hub_root, content=_source(node_root, facts["namespaces"], passed=outcome != "failed"))
    assert child.control["credential"] == hub.credential
    with server(hub.app) as url:
        child.control.update(hub_url=url, public_key=hub.public.hex(), now=hub.now)
        prepared = child.operate("prepare")
        key, route = next(iter(hub.coordinator.registrations.items()))
        hub.coordinator.registrations[key] = replace(route, selection=replace(route.selection,
            architectures=(prepared["offer"]["architecture"],), software=tuple(sorted(prepared["offer"]["software"].items()))))
        hub.tick()
        for _ in range(3):
            hub.tick()
        step = hub.repository.current_steps(OWNER, "workflow-a")[2]
        assert (step.state, step.reason_code) == ("waiting_node", "inputs_receiving")
        assert hub.count("runs") == hub.count("capability_step_bindings") == hub.count("capability_reservations") == 0
        assert hub.count("platform_commands") == 1
        child.control["now"] = hub.now
        received = child.operate("receive")
        assert received["source_sha256"] == hub.exported.revision.source_bundle.sha256
        for _ in range(8):
            hub.tick()
            if hub.count("runs"):
                break
        assert hub.count("runs") == hub.count("capability_step_bindings") == hub.count("capability_reservations") == 1
        assert hub.reservation() == "held"
        record = hub.repository.get_dispatch(OWNER, "workflow-a", "test", 1)
        command_id = "workflow-test-" + record.token
        executions: list[dict[str, Any]] = []
        recoveries: list[dict[str, Any]] = []

        def deliver(_: float) -> None:
            assert not executions
            child.control["now"] = hub.now
            operation = outcome if outcome in {"lost", "unknown"} else "execute"
            executed = child.operate(operation, command_id=command_id)
            executions.append(executed)
            if outcome == "lost":
                command = hub.commands.get(command_id)
                assert command["status"] == "leased" and executed["dropped_receipts"] == 1
                assert executed["journal"]["receipt_pending"] is True
                recovered = child.operate("recover", command=command)
                assert recovered["journal"] == {**executed["journal"], "receipt_pending": False}
                assert recovered["executions"] == recovered["preflights"] == 0
                recoveries.append(recovered)

        hub.services["platform_delivery"].sleep = deliver
        run = hub.worker_service.run_once(OWNER)
        expected = "unknown" if outcome == "unknown" else "succeeded"
        assert run["state"] == expected, run
        assert len(executions) == 1 and executions[0]["executions"] == executions[0]["preflights"] == 1
        command = hub.commands.get(command_id)
        assert command["status"] == expected and command["retry_class"] == "manual_only"
        assert verify_command(command, hub.public)
        assert CapabilityExecutionContext.from_command(command).binding.offer_digest == prepared["offer_digest"]
        assert command["arguments"]["receive_receipt"]["sha256"] == received["source_sha256"]
        replay = child.operate("replay", command=command)
        acknowledged = recoveries[0] if outcome == "lost" else executions[0]
        assert acknowledged["journal"]["receipt_pending"] is False
        assert replay["journal"] == acknowledged["journal"] and replay["executions"] == replay["preflights"] == 0
        assert replay["host_secret_preserved"] is True
        assert hub.worker_service.run_once(OWNER) is None
        for _ in range(3):
            hub.tick()
        assert hub.count("platform_commands") == 2 and hub.count("runs") == 1
        outputs = hub.outputs()
        if outcome == "unknown":
            assert outputs == {} and hub.test_evidence() == [] and hub.reservation() in {"held", "isolated"}
        else:
            assert set(outputs) == {"test_report", "junit"} and hub.reservation() == "released"
            assert hub.test_evidence()[0]["result"] == ("failed" if outcome == "failed" else "passed")
            wire = RemoteTestResult.parse(command["result"]["result"])
            assert hub.store.read(OWNER, WORKSPACE, outputs["test_report"]["artifact_id"]) == wire.raw_report
            assert hub.store.read(OWNER, WORKSPACE, outputs["junit"]["artifact_id"]) == wire.junit
            cases = {case.get("name"): case for case in ET.fromstring(wire.junit).iter("testcase")}
            assert len(cases) == 6 and all(case.find("skipped") is None and case.find("error") is None for case in cases.values())
            assert {name for name, case in cases.items() if case.find("failure") is not None} == ({"test_delivery"} if outcome == "failed" else set())
            for output_key, manifest in outputs.items():
                (evidence / (output_key + ".bytes")).write_bytes(hub.store.read(OWNER, WORKSPACE, manifest["artifact_id"]))
    summary = {"outcome": outcome, "run_state": run["state"], "command_id": command_id,
        "source_sha256": received["source_sha256"], "hostname": facts["hostname"], "node_root": str(node_root),
        "node_processes": {operation: report["pid"] for operation, report in child.reports.items()},
        "hub_pid": os.getpid(), "reservation": hub.reservation(), "fixed_test_executions": 1,
        "full_workflow_completed": False, "production_sandbox_certified": False}
    (evidence / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
