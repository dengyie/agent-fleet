"""Persistent Node CLI configuration uses the original capability/runtime chain."""
from __future__ import annotations

import base64
import hashlib
import json
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fleet_core.capability import CapabilityDefinition
from fleet_core.capability import BoundCapability, TargetBinding
from fleet_core.capability_execution import CapabilityExecutionContext
from fleet_core.control import generate_ed25519_keypair
from fleet_core.remote_workflow_test import REMOTE_TEST_ACTION
from fleet_core.remote_workflow_test import RemoteTestRequest, remote_test_command_id
from fleet_core.locking import LockBusy
from fleet_core.platform_command import PlatformCommand
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.infrastructure.platform_db import PlatformRepository
from tools.plugins.node import load_runtime, main
from test_artifact_receive import ContentTransport
from test_remote_workflow_test_node import NodeFixture
from test_workflow_e2e import hub_server
from test_workflow_repository import OWNER, WORKSPACE
from test_workflow_startup import registration, seed


def node_configuration(root: Path) -> tuple[Path, dict]:
    _, public = generate_ed25519_keypair()
    credential = root / "credential"
    credential.write_text("node-a:local-secret", encoding="utf-8")
    public_file = root / "public-key"
    public_file.write_text(base64.b64encode(public).decode("ascii"), encoding="utf-8")
    inputs = root / "inputs"
    inputs.mkdir(mode=0o700)
    capability = CapabilityDefinition("coding.test", 1, "fixed-tests", "in-v1", "out-v1", b'{"type":"object"}',
        b'{"type":"object"}', "workspace_read", "manual_only", ("operator",), 3600, 65536)
    value = {"node_id": "node-a", "hub_url": "https://hub.example", "credential_file": str(credential),
        "public_key_file": str(public_file), "state_directory": str(root / "state"), "plugins": {},
        "workflow_test": {"enabled": True, "offer_revision": 1, "capability": capability.payload(),
            "input_directory": str(inputs), "sandbox_launcher": [sys.executable, "-V"]}}
    path = root / "node.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path, value


def test_node_cli_loads_fixed_test_configuration_and_keeps_single_owner(tmp_path: Path) -> None:
    path, value = node_configuration(tmp_path)
    runtime, interval = load_runtime(path)
    try:
        assert interval == 2
        assert runtime.config.workflow_test_enabled
        assert runtime.config.capabilities == frozenset({"artifact.receive", REMOTE_TEST_ACTION})
        assert runtime.workflow_test_executor is not None
        assert runtime.config.capability_config.registrations[0].definition.payload() == value["workflow_test"]["capability"]
        assert runtime.artifact_receiver.inputs["source"].root == tmp_path / "inputs"
        with pytest.raises(LockBusy):
            load_runtime(path)
    finally:
        runtime.close_all()
    restarted, _ = load_runtime(path)
    restarted.close_all()


def test_node_cli_manifest_loads_config_without_contacting_hub(tmp_path: Path, capsys) -> None:
    path, _ = node_configuration(tmp_path)
    assert main(["--config", str(path), "--manifest"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["node_id"] == "node-a"
    assert output["capabilities"]["artifact.receive"] is True
    assert "credential" not in output and "local-secret" not in json.dumps(output)


def test_node_cli_once_reports_to_real_hub_from_persistent_configuration(tmp_path: Path) -> None:
    path, value = node_configuration(tmp_path)
    private, public = generate_ed25519_keypair()
    Path(value["public_key_file"]).write_text(base64.b64encode(public).decode("ascii"), encoding="utf-8")
    manifest = registration(OWNER, WORKSPACE)
    manifest["coding_delivery"][0]["test"]["remote"] = {"node_id": "node-a",
        "capability": value["workflow_test"]["capability"], "architecture": platform.machine(),
        "software": {"python": platform.python_version()}, "residency_alias": "private"}
    manifest_path = tmp_path / "hub-registration.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    config = FleetConfig.from_root(tmp_path / "hub", dev_operator=OWNER, platform_enabled=True,
        platform_workflows_enabled=True, platform_workflow_registration_file=manifest_path,
        platform_capability_routing_enabled=True, platform_artifact_transfer_enabled=True,
        platform_remote_execution_enabled=True, platform_command_signing_raw=private, serve_frontend=False)
    repository = PlatformRepository(config.platform_db)
    seed(repository, tmp_path)
    repository.upsert_node(OWNER, {"node_id": "node-a"})
    credential = repository.provision_node_credential(OWNER, "node-a")["credential"]
    Path(value["credential_file"]).write_text(credential, encoding="utf-8")
    app = create_app(config)
    requests = []

    @app.after_request
    def record(response):
        from flask import request
        requests.append((request.path, response.status_code))
        return response

    with hub_server(app) as origin:
        value["hub_url"] = origin
        path.write_text(json.dumps(value), encoding="utf-8")
        completed = subprocess.run([sys.executable, "-m", "tools.plugins.node", "--config", str(path), "--once"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20)
        assert completed.returncode == 0, completed.stderr + completed.stdout
        assert json.loads(completed.stdout) == {"ok": True, "commands": 0, "receipts": 0}
        assert ("/api/platform/v1/nodes/capability-offers", 200) in requests
        assert ("/api/platform/v1/nodes/heartbeat", 200) in requests
        assert ("/api/platform/v1/nodes/poll", 200) in requests
        assert all("plugins" not in url for url, _ in requests)
        assert credential not in completed.stdout + completed.stderr
        restarted, _ = load_runtime(path)
        try:
            assert restarted.report_capabilities().offer_revision == 1
        finally:
            restarted.close_all()


def test_configured_node_signed_receive_recovers_without_downloading_again(tmp_path: Path) -> None:
    path, value = node_configuration(tmp_path)
    private, public = generate_ed25519_keypair()
    Path(value["public_key_file"]).write_text(base64.b64encode(public).decode("ascii"), encoding="utf-8")

    class Wire(ContentTransport):
        def request_bounded_json(self, *_args, **_kwargs):
            raise AssertionError("direct receive replay must not report or poll")

    wire = Wire(b"fixed source bytes")
    command = PlatformCommand.create(command_id="receive-" + "a" * 64, target_node="node-a", resource_id="workspace",
        grant_id="a" * 64, action="artifact.receive", retry_class="idempotent_with_key", expires_at=time.time() + 300,
        arguments={"artifact_id": "artifact-a", "sha256": hashlib.sha256(wire.data).hexdigest(), "size": len(wire.data),
            "input_alias": "source", "suffix": ".tar", "workflow_id": "wf-a", "step_key": "test",
            "generation": 1, "ref_key": "source", "input_digest": "b" * 64}).signed(private).as_dict()
    runtime, _ = load_runtime(path, transport=wire)
    try:
        receipt = runtime.client.handle(command)
        assert receipt["status"] == "succeeded", receipt
    finally:
        runtime.close_all()
    restarted, _ = load_runtime(path, transport=wire)
    try:
        assert restarted.client.handle(command) == receipt
        assert len(wire.requests) == 1
        assert (tmp_path / "inputs" / ("a" * 64 + ".tar")).read_bytes() == wire.data
        assert len(restarted.artifact_receiver.pending_receipts()) == 1
    finally:
        restarted.close_all()


@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT])
def test_node_cli_signal_reaches_inflight_runtime_before_poll_returns(tmp_path: Path, monkeypatch, stop_signal) -> None:
    from tools.plugins import node

    path, _ = node_configuration(tmp_path)
    runtime, interval = load_runtime(path)
    monkeypatch.setattr(node, "load_runtime", lambda *_args: (runtime, interval))
    monkeypatch.setattr(node, "announce", lambda _runtime: False)

    def poll_once(**_kwargs):
        signal.raise_signal(stop_signal)
        assert runtime.client.should_cancel(), "SIGTERM must reach active Node command control before poll returns"
        return {"ok": True, "commands": 0, "receipts": 0}

    monkeypatch.setattr(runtime, "poll_once", poll_once)
    assert node.main(["--config", str(path)]) == 0


@pytest.mark.skipif(sys.platform != "linux", reason="production fixed-test executor requires Linux")
def test_configured_linux_node_runs_original_fixed_recipe_and_replays_saved_result(tmp_path: Path) -> None:
    from tools.platform.workflow_remote_test import decode_test_result

    source_root = tmp_path / "source-fixture"
    source_root.mkdir()
    fixture = NodeFixture(source_root)
    node_root = tmp_path / "configured-node"
    node_root.mkdir()
    path, value = node_configuration(node_root)
    from workflow_sandbox_runtime import linux_test_backend

    value["workflow_test"]["sandbox_launcher"] = list(linux_test_backend(node_root).launcher)
    path.write_text(json.dumps(value), encoding="utf-8")
    Path(value["public_key_file"]).write_text(base64.b64encode(fixture.public).decode("ascii"), encoding="utf-8")

    class Wire(ContentTransport):
        def request_bounded_json(self, *_args, **_kwargs):
            raise AssertionError("direct command must not contact a Hub")

    wire = Wire(fixture.raw)
    runtime, _ = load_runtime(path, transport=wire)
    try:
        receive = PlatformCommand.create(command_id="receive-" + "a" * 64, target_node="node-a", resource_id="workspace",
            grant_id="a" * 64, action="artifact.receive", retry_class="idempotent_with_key", expires_at=time.time() + 300,
            arguments={"artifact_id": fixture.revision.source_bundle.artifact_id,
                "sha256": fixture.revision.source_bundle.sha256, "size": len(fixture.raw),
                "input_alias": "source", "suffix": ".tar", "workflow_id": "wf-a", "step_key": "test",
                "generation": 1, "ref_key": "source", "input_digest": "b" * 64}).signed(fixture.private).as_dict()
        received = runtime.client.handle(receive)
        assert received["status"] == "succeeded", received
        offer = runtime.capability_service.offer(now=time.time())
        assert {ref.id for ref in offer.capabilities} == {"coding.test"}
        definition = runtime.config.capability_config.registrations[0].definition
        binding = TargetBinding("node-a", 1, offer.config_digest, "c" * 64,
            (BoundCapability("coding.test", 1, definition.digest),), "b" * 64, "d" * 64, 0, 0)
        request = RemoteTestRequest("wf-a", 1, "e" * 64, "b" * 64,
            hashlib.sha256(binding.snapshot).hexdigest(), fixture.revision, "f" * 64, received["result"])
        command = PlatformCommand.create(command_id=remote_test_command_id(request.dispatch_token), target_node="node-a",
            resource_id="workspace", action=REMOTE_TEST_ACTION, retry_class="manual_only",
            run_id="wf-run-" + request.dispatch_token, expires_at=time.time() + 300, arguments=request.payload())
        signed = CapabilityExecutionContext("coding.test", "fixed-tests", binding).bind(command).signed(fixture.private).as_dict()
        first = runtime.client.handle(signed)
        assert first["status"] == "succeeded", first
        decoded = decode_test_result(first["result"]["result"], revision=fixture.revision, subject_digest="f" * 64, node_id="node-a")
        assert decoded.passed and b"test_delivery" in decoded.junit
    finally:
        runtime.close_all()
    restarted, _ = load_runtime(path, transport=wire)
    try:
        def no_execution(*_args, **_kwargs):
            pytest.fail("saved fixed tests must not execute or preflight again after restart")

        restarted.workflow_test_executor.runner.run = no_execution
        restarted.executor.capability_prepare = no_execution
        assert restarted.client.handle(signed) == first
        assert len(wire.requests) == 1
        assert restarted.journal.get(signed["command_id"])["state"] == "succeeded"
    finally:
        restarted.close_all()


@pytest.mark.parametrize("mutation", ["off", "relative_input", "relative_launcher", "wrong_adapter", "unknown_field", "bad_hash"])
def test_node_cli_rejects_incomplete_fixed_test_registration_before_state_creation(tmp_path: Path, mutation: str) -> None:
    path, value = node_configuration(tmp_path)
    section = value["workflow_test"]
    if mutation == "off":
        section["enabled"] = False
    elif mutation == "relative_input":
        section["input_directory"] = "inputs"
    elif mutation == "relative_launcher":
        section["sandbox_launcher"] = ["python3"]
    elif mutation == "wrong_adapter":
        section["capability"]["adapter_id"] = "other"
    elif mutation == "unknown_field":
        section["command"] = ["arbitrary"]
    else:
        section["capability"]["input_schema_hash"] = "0" * 64
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        load_runtime(path)
    assert not (tmp_path / "state").exists()
