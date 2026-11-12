"""Explicit actual Windows producer → original Hub → Linux receiver pilot."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import shlex
import socket
import subprocess
import sys
import time
from typing import Any
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from test_artifact_export import export_node
from test_artifact_transfer import TransferFixture, transfer, transfer_http
from test_workflow_dispatch import setup
from test_workflow_repository import OWNER, WORKSPACE, registered
from test_workflow_two_nodes import server
from hub.domain.artifact_receive import RegisteredNodeInputs
from hub.domain.workflow_contract import resolve_contract
from hub.domain.workflow_contract_types import ArtifactReference, AuthorizedInputs, CatalogRef, InputBinding
from hub.infrastructure.command_repository import CommandRepository


def _remote(platform: str, operation: str, config: dict[str, Any], evidence: Path,
            *, port: str | None = None) -> dict[str, Any]:
    host = os.environ["FLEET_" + platform.upper() + "_TEST_HOST"]
    checkout = os.environ["FLEET_" + platform.upper() + "_TEST_ROOT"]
    stem = platform + "-" + operation
    if platform == "windows":
        python = os.environ["FLEET_WINDOWS_TEST_PYTHON"]
        assert PureWindowsPath(checkout).is_absolute() and PureWindowsPath(python).is_absolute()
        remote_root = str(PureWindowsPath(checkout).parent)
        script = "$ProgressPreference='SilentlyContinue'; $env:PYTHONIOENCODING='utf-8'; $env:PYTHONUTF8='1'; Set-Location '" + checkout.replace("'", "''") + "'; & '" + python.replace("'", "''") + "' tests/fixtures/workflow/two_host_producer.py; exit $LASTEXITCODE"
        command = "powershell -NoProfile -NonInteractive -EncodedCommand " + base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    else:
        assert platform == "linux"
        python = os.environ["FLEET_LINUX_TEST_PYTHON"]
        tool_path = os.environ["FLEET_LINUX_TEST_PATH"]
        assert Path(checkout).is_absolute() and Path(python).is_absolute()
        remote_root = str(Path(checkout).parent)
        command = "cd " + shlex.quote(checkout) + " && env LC_ALL=C PYTHONUTF8=1 PYTHONIOENCODING=utf-8 PATH=" + shlex.quote(tool_path)
        command += " setpriv --bounding-set=-dac_override,-dac_read_search --no-new-privs " + shlex.quote(python) + " tests/fixtures/workflow/two_host_receiver.py"
    arguments = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12", "-o", "ExitOnForwardFailure=yes"]
    if port is not None:
        arguments.extend(["-R", port + ":127.0.0.1:" + port])
    arguments.extend([host, command])
    request = config | {"operation": operation, "root": remote_root}
    raw = json.dumps(request, separators=(",", ":")).encode("utf-8")
    assert len(raw) <= 65536
    started = time.monotonic()
    result = subprocess.run(arguments, input=raw, capture_output=True, timeout=120)
    (evidence / (stem + ".stdout")).write_bytes(result.stdout)
    (evidence / (stem + ".stderr")).write_bytes(result.stderr)
    record = {"command": arguments, "operation": operation, "host": host, "exit_code": result.returncode,
        "elapsed_seconds": time.monotonic() - started, "stdin_keys": sorted(request), "stdin_bytes": len(raw)}
    (evidence / (stem + "-command.json")).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    assert result.returncode == 0, result.stdout.decode("utf-8", errors="replace") + result.stderr.decode("utf-8", errors="replace")
    assert len(result.stdout) <= 65536
    report = json.loads(result.stdout)
    assert report["operation"] == operation and report["hostname"].lower() != socket.gethostname().lower()
    assert "credential" not in json.dumps(report).lower()
    (evidence / (stem + ".json")).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def _receiver_input(export_node: Any, transfer: TransferFixture, reference: dict[str, Any]) -> tuple[Any, Any, Any, dict[str, Any]]:
    repository = transfer.workflow.repository
    definition = registered(source=reference)
    repository.create_or_get(OWNER, WORKSPACE, "workflow-receiver", client_token="two-host-receiver",
        definition=definition, now=transfer.workflow.clock())
    claim = next(item for item in repository.claim_due(OWNER, worker_id="input-preparer", now=transfer.workflow.clock(),
        limit=10, lease_seconds=60) if item.workflow_id == "workflow-receiver")
    resolved = resolve_contract(definition, AuthorizedInputs(OWNER, WORKSPACE, "develop", CatalogRef("cli", 1),
        (InputBinding("source", OWNER, WORKSPACE, ArtifactReference(**reference)),)))
    inputs = export_node.app.extensions["fleet"]["services"]["artifact_inputs"]
    inputs.targets[(OWNER, WORKSPACE, "node-b")] = RegisteredNodeInputs(OWNER, WORKSPACE, "node-b", (("source", "source", ".tar"),))
    assert inputs.prepare(claim, expected_revision=1, resolved=resolved, generation=1, target_key="node-b") is False
    assert repository.current_steps(OWNER, "workflow-receiver")[0].state == "waiting_node"
    with repository.transaction(write=False) as connection:
        row = connection.execute("SELECT command_id,grant_id FROM platform_commands WHERE target_node='node-b' AND action='artifact.receive' LIMIT 2").fetchall()
    assert len(row) == 1
    command = CommandRepository(transfer.repository.db_path).get(row[0][0])
    assert command["grant_id"] == row[0][1]
    return inputs, claim, resolved, command


def test_actual_two_host_source_export_receive_and_independent_fixed_validation(
        export_node: Any, transfer: TransferFixture) -> None:
    evidence = Path(os.environ["FLEET_TWO_HOST_TEST_EVIDENCE"])
    evidence.mkdir(parents=True, exist_ok=False)
    case = "handoff-" + os.urandom(8).hex()
    prepared = _remote("windows", "prepare", {"case": case}, evidence)
    assert 0 < prepared["size"] <= 1024 * 1024
    assert prepared["included_paths"] == ["src.txt", "test_payload.py"] and prepared["omitted_paths"] == [".env"]
    platform = export_node.app.extensions["fleet"]["platform_repository"]
    platform.upsert_node(OWNER, {"node_id": "node-b", "label": "Actual Linux receiver"})
    credential = platform.provision_node_credential(OWNER, "node-b", secret="b" * 40)["credential"]
    with transfer.workflow.repository.transaction() as connection:
        connection.execute("UPDATE platform_commands SET status='queued',lease_owner=NULL,lease_until=NULL WHERE command_id='export-a'")
    step_before = transfer.workflow.repository.current_steps(OWNER, "workflow-a")[0].state
    with server(export_node.app) as url:
        port = url.rsplit(":", 1)[1]
        exported = _remote("windows", "export", {"case": case, "node_id": "node-a",
            "credential": export_node.headers["X-Platform-Node-Credential"], "workspace": WORKSPACE,
            "public_key": transfer.public_key.hex(), "hub_url": url, "now": transfer.workflow.clock()}, evidence, port=port)
        assert exported["hostname"] == prepared["hostname"] and exported["puts"] == exported["receipt_posts"] == 1
        command_repository = CommandRepository(transfer.repository.db_path)
        command = command_repository.get("export-a")
        reference = command["result"]["artifact"]
        assert command["status"] == "succeeded" and command["result"] == exported["receipt"]
        assert (reference["sha256"], reference["size"]) == (prepared["sha256"], prepared["size"])
        saved = transfer.repository.find_by_command("export-a", "report")
        assert (saved["transfer_id"], saved["artifact_id"]) == (exported["transfer_id"], reference["artifact_id"])
        with transfer.workflow.repository.transaction(write=False) as connection:
            assert connection.execute("SELECT count(*) FROM artifact_transfers").fetchone()[0] == 1
            assert connection.execute("SELECT count(*) FROM artifact_transfer_parts").fetchone()[0] == 1
            binding = connection.execute("SELECT artifact_id,sha256,size FROM workflow_artifact_refs WHERE output_key='output:report' LIMIT 2").fetchall()
        assert len(binding) == 1 and tuple(binding[0]) == (reference["artifact_id"], reference["sha256"], reference["size"])
        assert transfer.workflow.repository.current_steps(OWNER, "workflow-a")[0].state == step_before
        inputs, claim, resolved, receive_command = _receiver_input(export_node, transfer, reference)
        denied = export_node.client.get("/api/platform/v1/node/artifact-inputs/" + receive_command["grant_id"] + "/content",
            headers=export_node.headers | {"X-Platform-Command-ID": receive_command["command_id"]})
        assert denied.status_code == 404
        received = _remote("linux", "receive", {"case": case, "node_id": "node-b", "credential": credential,
            "public_key": transfer.public_key.hex(), "hub_url": url, "now": transfer.workflow.clock(),
            "command_id": receive_command["command_id"], "sha256": reference["sha256"], "size": reference["size"]}, evidence, port=port)
        assert received["hostname"].lower() != prepared["hostname"].lower()
        assert received["downloads"] == 1 and received["receipt_posts"] == 2 and received["validation_started"] is False
        assert received["receipt"] == {"grant_id": receive_command["grant_id"], "input_alias": "source", **reference}
        assert command_repository.get(receive_command["command_id"])["status"] == "succeeded"
        assert transfer.workflow.repository.current_steps(OWNER, "workflow-receiver")[0].state == "waiting_node"
        assert inputs.prepare(claim, expected_revision=transfer.workflow.repository.get(OWNER, "workflow-receiver").revision,
            resolved=resolved, generation=1, target_key="node-b") is False
        assert transfer.workflow.repository.current_steps(OWNER, "workflow-receiver")[0].state == "ready"
    source_revision = {key: prepared[key] for key in ("repository_id", "base_commit", "head_commit", "tree_id")}
    validation = _remote("linux", "validate", {"case": case, "owner": OWNER, "workspace": WORKSPACE,
        "reference": reference, "source_revision": source_revision, "validation_authorized": True}, evidence)
    assert validation["hostname"] == received["hostname"]
    assert (validation["sha256"], validation["size"]) == (reference["sha256"], reference["size"])
    report = validation["report"]
    assert report["passed"] is True and report["exit_code"] == 0 and report["timed_out"] is False
    assert report["source_revision"] == source_revision and report["subject_digest"] == reference["sha256"]
    assert report["executor_id"] == "linux-handoff-validator" and report["environment"]["launcher_sha256"] is not None
    junit = validation["junit"].encode("utf-8")
    assert hashlib.sha256(junit).hexdigest() == validation["junit_sha256"] == report["junit_sha256"]
    tree = ET.fromstring(junit)
    cases = list(tree.iter("testcase"))
    assert {case.get("name") for case in cases} == {"test_fixed_committed_bytes", "test_private_input_boundary"}
    assert not list(tree.iter("failure")) and not list(tree.iter("error")) and not list(tree.iter("skipped"))
    (evidence / "validation-junit.xml").write_bytes(junit)
    summary = {"controller_hostname": socket.gethostname(), "producer_hostname": prepared["hostname"],
        "receiver_hostname": received["hostname"], "case": case, "source_revision": source_revision,
        "reference": reference, "transfer_id": exported["transfer_id"], "grant_id": receive_command["grant_id"],
        "export_command_id": "export-a", "receive_command_id": receive_command["command_id"],
        "puts": exported["puts"], "downloads": received["downloads"], "receipt_posts": received["receipt_posts"],
        "independent_fixed_cases": len(cases), "receiver_step": "ready", "full_workflow_completed": False}
    (evidence / "handoff-summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
