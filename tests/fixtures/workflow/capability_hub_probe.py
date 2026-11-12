"""Explicit actual-platform capability/postcheck verification over original HTTP."""
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

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from capability_node_canary import pilot_definitions
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.domain.capability import NodePolicy, SelectionRequirements
from hub.domain.capability_execution import CAPABILITY_ARGUMENT, CapabilityExecutionContext, RegisteredCapabilityAction
from fleet_core.control import generate_ed25519_keypair
from fleet_core.platform_command import verify_command
from hub.domain.workflow_contract import parse_definition, resolve_contract
from hub.domain.workflow_contract_types import AuthorizedInputs, CapabilityRequirement, CatalogRef
from hub.infrastructure.capability_dispatch import CapabilityDispatchRepository
from hub.infrastructure.capability_repository import CapabilityRepository
from hub.infrastructure.platform_db import PlatformRepository
from hub.infrastructure.workflow_run_executor import RegisteredRunTarget
from test_capability_dispatch import CAP, DIGEST_CAP, Fixture
from test_workflow_repository import OWNER, WORKSPACE, prepare_database, registered
from test_workflow_two_nodes import server

_PLATFORMS = tuple(os.environ.get("FLEET_CAPABILITY_TEST_PLATFORMS", "local").split(","))
assert _PLATFORMS and len(set(_PLATFORMS)) == len(_PLATFORMS) and set(_PLATFORMS) <= {"local", "windows", "linux"}


def _worker(target: str, operation: str, config: dict[str, Any], local_root: Path,
            evidence: Path, checkout: Path) -> dict[str, Any]:
    request = config | {"operation": operation}
    if target == "local":
        arguments = [sys.executable, str(checkout / "tests/fixtures/workflow/capability_node_canary.py")]
        request["root"] = str(local_root)
    else:
        prefix = "FLEET_" + target.upper() + "_TEST_"
        remote_checkout, host = os.environ[prefix + "ROOT"], os.environ[prefix + "HOST"]
        if target == "windows":
            request["root"] = str(PureWindowsPath(remote_checkout).parent)
            python = os.environ.get(prefix + "PYTHON", "python")
            script = "$ProgressPreference='SilentlyContinue'; $env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'; Set-Location '" + remote_checkout.replace("'", "''") + "'; & '" + python.replace("'", "''") + "' tests/fixtures/workflow/capability_node_canary.py; exit $LASTEXITCODE"
            command = "powershell -NoProfile -NonInteractive -EncodedCommand " + base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        else:
            request["root"] = str(Path(remote_checkout).parent)
            command = "cd " + shlex.quote(remote_checkout) + " && env LC_ALL=C PYTHONUTF8=1 PYTHONIOENCODING=utf-8 PATH="
            command += shlex.quote(os.environ[prefix + "PATH"]) + " setpriv --bounding-set=-dac_override,-dac_read_search --no-new-privs "
            command += shlex.join([os.environ[prefix + "PYTHON"], "tests/fixtures/workflow/capability_node_canary.py"])
        port = config["hub_url"].rsplit(":", 1)[1]
        arguments = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12", "-o", "ExitOnForwardFailure=yes",
            "-R", port + ":127.0.0.1:" + port, host, command]
    raw = json.dumps(request, allow_nan=False).encode("utf-8")
    assert len(raw) <= 65536
    started = time.monotonic()
    result = subprocess.run(arguments, cwd=checkout, input=raw, capture_output=True, timeout=120)
    (evidence / (operation + ".stdout")).write_bytes(result.stdout)
    (evidence / (operation + ".stderr")).write_bytes(result.stderr)
    record = {"command": arguments, "operation": operation, "platform": target, "exit_code": result.returncode,
        "elapsed_seconds": time.monotonic() - started, "stdin_keys": sorted(request), "stdin_bytes": len(raw)}
    (evidence / (operation + "-command.json")).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    assert result.returncode == 0, result.stdout.decode("utf-8", errors="replace") + result.stderr.decode("utf-8", errors="replace")
    assert len(result.stdout) <= 65536 and config["credential"].encode() not in result.stdout
    report = json.loads(result.stdout)
    assert report["operation"] == operation
    if target != "local":
        assert report["platform"] == target and report["hostname"].lower() != socket.gethostname().lower()
    (evidence / (operation + ".json")).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def _hub(root: Path) -> tuple[Any, CapabilityRepository, PlatformRepository, str, bytes, bytes]:
    workflows = prepare_database(root / "platform.db")
    platform = PlatformRepository(workflows.db_path)
    platform.upsert_node(OWNER, {"node_id": "node-a"})
    credential = platform.provision_node_credential(OWNER, "node-a", secret="n" * 32)["credential"]
    platform.create_conversation(OWNER, "step-chat", title="Step", workspace_id=WORKSPACE)
    platform.upsert_model(OWNER, {"profile_id": "fixture", "provider": "deterministic", "model": "fixture"})
    capabilities = CapabilityRepository(workflows, boot_id="actual-platform-pilot")
    capabilities.init()
    for definition in pilot_definitions():
        capabilities.register_definition(definition, now=100)
    capabilities.register_node(NodePolicy(OWNER, "node-a", (WORKSPACE,), (CatalogRef("fixture", 1),), (CAP, DIGEST_CAP),
        slot_limit=1, allowed_risks=("workspace_read", "workspace_write")), now=100)
    actions = (RegisteredCapabilityAction("cli", CAP, "tool.workspace.write"),
        RegisteredCapabilityAction("cli", DIGEST_CAP, "reconcile.workspace.digest"))
    private, public = generate_ed25519_keypair()
    app = create_app(FleetConfig.from_root(root, platform_enabled=True, platform_workflows_enabled=True,
        platform_capability_routing_enabled=True, platform_db=workflows.db_path,
        platform_command_signing_raw=private, dev_operator=OWNER, serve_frontend=False),
        repositories={"workflows": workflows, "capabilities": capabilities, "workflow_clock": lambda: 104.0,
            "workflow_capability_actions": actions})
    services = app.extensions["fleet"]["services"]
    services["platform_delivery"].clock = services["platform_delivery"].repository.clock = services["command_postcheck"].clock = lambda: 104.0
    return app, capabilities, platform, credential, private, public


def _admit(repository: CapabilityRepository, platform: PlatformRepository, credential: str,
           private: bytes, public: bytes, prepared: dict[str, Any]) -> Fixture:
    refs = (CAP, DIGEST_CAP)
    requested = tuple(CapabilityRequirement(ref.id, (1,)) for ref in refs)
    source = json.loads(registered().snapshot)
    source["steps"][0]["kind"] = "native_run"
    source["steps"][0]["contract"]["capability_requirements"] = [{"id": ref.id, "revisions": [1]} for ref in refs]
    definition = parse_definition(json.dumps(source).encode("utf-8"))
    resolved = resolve_contract(definition, AuthorizedInputs(OWNER, WORKSPACE, "develop", CatalogRef("cli", 1), ()))
    workflows = repository.workflows
    workflows.create_or_get(OWNER, WORKSPACE, "workflow-a", client_token="workflow-a", definition=definition, now=100)
    claim = workflows.claim_due(OWNER, worker_id="coordinator", now=100, limit=1, lease_seconds=60)[0]
    frozen = workflows.freeze_inputs(claim, step_key="develop", generation=1, resolved=resolved, expected_revision=1, now=101)
    offer = prepared["offer"]
    requirements = SelectionRequirements(OWNER, WORKSPACE, CatalogRef("fixture", 1), "operator", "cli", requested,
        resolved.digest, operating_systems=(offer["os"],), architectures=(offer["architecture"],),
        software=tuple(sorted(offer["software"].items())), memory_bytes=0, disk_bytes=200)
    admitted = repository.admit(claim, step_key="develop", generation=1, expected_revision=frozen.revision,
        requirements=requirements, reserve_tokens=100, reserve_requests=2, now=104)
    assert admitted.selection.binding.offer_digest == prepared["offer_digest"]
    actions = (RegisteredCapabilityAction("cli", CAP, "tool.workspace.write"),
        RegisteredCapabilityAction("cli", DIGEST_CAP, "reconcile.workspace.digest"))
    guard = CapabilityDispatchRepository(repository, actions=actions)
    config = {"model_profile_id": "fixture", "execution_node_id": "node-a", "provider_snapshot": {
        "provider": "deterministic", "model": "fixture", "provider_config": {}, "capabilities": {}}}
    target = RegisteredRunTarget(OWNER, WORKSPACE, "node-a", CatalogRef("cli", 1), CatalogRef("safe", 1),
        "step-chat", json.dumps(config).encode("utf-8"))
    fingerprint = platform.authenticate_node_credential("node-a", credential.split(":", 1)[1])["credential_fingerprint"]
    return Fixture(repository, platform, guard, admitted, claim, target, private, public, [104.0], fingerprint,
        credential, "tool.workspace.write")


def _snapshot(fixture: Fixture, command_id: str, delivery: Any) -> tuple[dict[str, Any], Any]:
    tables = ("runs", "workflow_runs", "workflow_steps", "workflow_dispatches", "workflow_budget_reservations",
        "capability_step_bindings", "capability_reservations")
    with fixture.repository.workflows.transaction(write=False) as connection:
        state = {table: tuple(tuple(row) for row in connection.execute("SELECT * FROM " + table + " LIMIT 2")) for table in tables}
        assert all(len(rows) == 1 for rows in state.values())
    return state, delivery.repository.get(command_id)


@pytest.mark.parametrize("target", _PLATFORMS)
@pytest.mark.parametrize("reservation", ["held", "isolated"])
@pytest.mark.parametrize("outcome", ["matched", "mismatch", "read_failed"])
def test_actual_platform_capability_postcheck_preserves_unknown_and_original_reservation(
        tmp_path: Path, target: str, reservation: str, outcome: str) -> None:
    evidence = Path(os.environ["FLEET_CAPABILITY_TEST_EVIDENCE"]) / (target + "-" + reservation + "-" + outcome)
    evidence.mkdir(parents=True, exist_ok=False)
    local_root = tmp_path / "node"
    local_root.mkdir(mode=0o700)
    checkout = Path(__file__).resolve().parents[3]
    app, repository, platform, credential, private, public = _hub(tmp_path)
    case = "capability-" + os.urandom(8).hex()
    with server(app) as url:
        control = {"case": case, "hub_url": url, "credential": credential, "public_key": public.hex(),
            "now": 104.0, "workspace_id": WORKSPACE}
        prepared = _worker(target, "prepare", control, local_root, evidence, checkout)
        fixture = _admit(repository, platform, credential, private, public, prepared)
        run_id = fixture.run()
        delivery = app.extensions["fleet"]["services"]["platform_delivery"]
        original = fixture.command(run_id)
        saved = delivery.enqueue(original, idempotency_key=original.command_id)
        written = _worker(target, "write", control | {"command_id": original.command_id}, local_root, evidence, checkout)
        assert written["original"]["state"] == "unknown" and written["write_calls"] == 1
        assert delivery.repository.get(original.command_id)["status"] == "unknown"
        fixture.isolate_write(delivery, original.command_id, reservation=reservation)
        before = _snapshot(fixture, original.command_id, delivery)
        client = app.test_client()
        route = "/api/platform/v1/commands/" + original.command_id + "/postcheck"
        first = client.post(route)
        assert first.status_code == 202
        check_id = first.json["postcheck"]["check_command_id"]
        check = delivery.repository.get(check_id)
        context = CapabilityExecutionContext.from_command(check)
        assert context.capability_id == DIGEST_CAP.id and context.binding == fixture.admission.selection.binding
        assert check["retry_class"] == "read_only" and check["target_node"] == saved["target_node"] == "node-a"
        assert verify_command(check, public) and set(check["arguments"]) == {"path", CAPABILITY_ARGUMENT}
        assert client.post(route).json["postcheck"]["check_command_id"] == check_id
        observed = _worker(target, "postcheck", control | {"original": saved, "check": check, "outcome": outcome},
            local_root, evidence, checkout)
        assert observed["original"] == written["original"]
        assert (observed["postcheck_writes"], observed["digest_calls"], observed["restart_io"]) == (0, 1, 0)
        expected_state = "remains_unknown" if outcome == "read_failed" else outcome
        response = client.get(route)
        assert response.status_code == 200 and response.json["postcheck"]["state"] == expected_state
        for public_response in (first, response, client.post(route), client.get(route)):
            wire = public_response.get_data(as_text=True)
            assert all(secret not in wire for secret in ("content", CAPABILITY_ARGUMENT, "expected_sha256", credential, "fixed output"))
        assert client.get(route, headers={"Cf-Access-Authenticated-User-Email": "other@example.test"}).status_code == 404
        assert _snapshot(fixture, original.command_id, delivery) == before
        with repository.workflows.transaction(write=False) as connection:
            assert connection.execute("SELECT count(*) FROM platform_commands").fetchone()[0] == 2
            assert connection.execute("SELECT count(*) FROM platform_command_postchecks").fetchone()[0] == 1
            audits = connection.execute("SELECT outcome FROM platform_command_reconciliations WHERE command_id=? LIMIT 2",
                (original.command_id,)).fetchall()
        assert [row[0] for row in audits] == ["confirmed_succeeded" if outcome == "matched" else "remains_unknown"]
    summary = {"case": case, "target": target, "hostname": prepared["hostname"], "platform": prepared["platform"],
        "python": prepared["python"], "offer_digest": prepared["offer_digest"], "binding_digest": hashlib.sha256(context.binding.snapshot).hexdigest(),
        "original_command_id": original.command_id, "check_command_id": check_id, "original_state": "unknown",
        "reservation": reservation, "outcome": expected_state, "write_calls": 1, "digest_calls": 1,
        "restart_io": 0, "original_snapshots_unchanged": True, "full_workflow_completed": False}
    (evidence / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
