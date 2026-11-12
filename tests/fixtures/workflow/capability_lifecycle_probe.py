"""Explicit automatic lifecycle on selected local or authorized native Nodes."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
from typing import Any, Callable

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fixtures.workflow.capability_hub_probe import _PLATFORMS, _worker
from hub.domain.capability_execution import CAPABILITY_ARGUMENT, CapabilityExecutionContext
from fleet_core.platform_command import verify_command
from hub.domain.workflow_control import WorkflowControlRequest
from hub.domain.workflow_identity import canonical_digest
from hub.infrastructure.workflow_control_repository import WorkflowControlRepository
from hub.infrastructure.workflow_repository import WorkflowRepositoryError
from test_capability_workflow_lifecycle import DIGEST_REF, FixedProvider, Lifecycle, _admit_and_create_run, _setup
from test_capability_router import OWNER, WORKSPACE
from test_workflow_two_nodes import server


class ChildNode:
    def __init__(self, setup: Lifecycle, control: dict[str, Any], root: Path, evidence: Path, target: str) -> None:
        self.setup, self.control, self.root, self.evidence = setup, control, root, evidence
        self.target = target
        self.delivery = setup.app.extensions["fleet"]["services"]["platform_delivery"]
        self.reports: dict[str, dict[str, Any]] = {}
        self.polls = 0

    def operate(self, operation: str, **values: Any) -> dict[str, Any]:
        assert operation not in self.reports
        report = _worker(self.target, operation, self.control | {"now": self.setup.clock[0]} | values,
            self.root, self.evidence, Path(__file__).resolve().parents[3])
        assert type(report["pid"]) is int and report["pid"] > 0
        if self.target == "local":
            assert report["pid"] != os.getpid() and report["platform"] == platform.system().lower()
        else:
            assert report["platform"] == self.target
        self.reports[operation] = report
        return report

    def wait_for_receipt(self, command_id: str, *, timeout_s: float = 30.0,
                         should_cancel: Callable[[], bool] | None = None) -> dict[str, Any]:
        self.polls += 1
        assert self.polls == 1 and timeout_s > 0
        assert should_cancel is None or not should_cancel()
        self.operate("write", command_id=command_id, interrupt=self.control["interrupt"])
        row = self.delivery.repository.get(command_id)
        assert row is not None and row["status"] == ("unknown" if self.control["interrupt"] else "succeeded")
        return row


def _postcheck(setup: Lifecycle, child: ChildNode, original: dict[str, Any], outcome: str) -> str:
    before = setup.lifecycle_snapshot()
    assert before[0] == "unknown" and before[5:9] == ("unknown", None, "unknown", None)
    assert before[11:] == ("held", "unknown")
    current = setup.workflows.get(OWNER, "workflow-a")
    with pytest.raises(WorkflowRepositoryError) as error:
        WorkflowControlRepository(setup.workflows).control(WorkflowControlRequest(
            OWNER, "workflow-a", "retry", current.revision, "blocked-retry", "develop", 1), now=setup.clock[0])
    assert error.value.code == "unresolved_execution"
    client = setup.app.test_client()
    route = "/api/platform/v1/commands/" + original["command_id"] + "/postcheck"
    first = client.post(route)
    assert first.status_code == 202, first.get_json()
    check_id = first.json["postcheck"]["check_command_id"]
    check = child.delivery.repository.get(check_id)
    assert check is not None and check["retry_class"] == "read_only"
    context = CapabilityExecutionContext.from_command(check)
    bound = CapabilityExecutionContext.from_command(original)
    assert context.capability_id == DIGEST_REF.id and context.binding == bound.binding
    assert verify_command(check, setup.public_key) and set(check["arguments"]) == {"path", CAPABILITY_ARGUMENT}
    assert client.post(route).json["postcheck"]["check_command_id"] == check_id
    observed = child.operate("postcheck", original=original, check=check, outcome=outcome)
    assert observed["original"] == child.reports["write"]["original"]
    assert (observed["postcheck_writes"], observed["digest_calls"], observed["restart_io"]) == (0, 1, 0)
    expected = "remains_unknown" if outcome == "read_failed" else outcome
    response = client.get(route)
    assert response.status_code == 200 and response.json["postcheck"]["state"] == expected
    assert client.get(route).json == client.post(route).json == response.json
    assert client.get(route, headers={"Cf-Access-Authenticated-User-Email": "other@example.test"}).status_code == 404
    for public in (first, response):
        wire = public.get_data(as_text=True)
        assert all(secret not in wire for secret in (CAPABILITY_ARGUMENT, "expected_sha256", child.control["credential"], "fixed output"))
    assert setup.lifecycle_snapshot() == before
    assert child.delivery.repository.get(original["command_id"]) == original
    with setup.workflows.transaction(write=False) as connection:
        audits = connection.execute(
            "SELECT outcome FROM platform_command_reconciliations WHERE command_id=? LIMIT 2",
            (original["command_id"],)).fetchall()
    assert [row[0] for row in audits] == ["confirmed_succeeded" if outcome == "matched" else "remains_unknown"]
    assert setup.count("platform_command_postchecks") == 1
    return expected


@pytest.mark.parametrize("target", _PLATFORMS)
@pytest.mark.parametrize("outcome", ("success", "matched", "mismatch", "read_failed"))
def test_automatic_lifecycle_with_independent_node_process(tmp_path: Path, outcome: str, target: str) -> None:
    evidence_root = Path(os.environ["FLEET_CAPABILITY_TEST_EVIDENCE"])
    assert evidence_root.is_absolute() and evidence_root.is_dir()
    evidence = evidence_root / ("lifecycle-" + target + "-" + outcome)
    evidence.mkdir(mode=0o700)
    root = tmp_path / "node"
    root.mkdir(mode=0o700)
    setup = _setup(tmp_path)
    interrupt = outcome != "success"
    provider = FixedProvider(interrupt=interrupt)
    services = setup.app.extensions["fleet"]["services"]
    worker = services["platform_worker"]
    worker.provider_factory = lambda profile: provider
    with server(setup.app) as url:
        control = {"case": "capability-" + os.urandom(8).hex(), "hub_url": url,
            "credential": setup.credential, "public_key": setup.public_key.hex(), "interrupt": interrupt,
            "workspace_id": WORKSPACE}
        child = ChildNode(setup, control, root, evidence, target)
        prepared = child.operate("prepare")
        coordinator = services["workflow_coordinator"]
        assert len(coordinator.registrations) == 1 and setup.count("workflow_dispatches") == 0
        key, registration = next(iter(coordinator.registrations.items()))
        offer = prepared["offer"]
        coordinator.registrations[key] = replace(registration, selection=replace(
            registration.selection, operating_systems=(offer["os"],), architectures=(offer["architecture"],),
            software=tuple(sorted(offer["software"].items())), memory_bytes=0, disk_bytes=200))
        worker.remote_waiter = child
        _admit_and_create_run(setup)
        result = worker.run_once(OWNER)
        assert result is not None and result["state"] == ("unknown" if interrupt else "succeeded")
        assert child.polls == 1 and provider.calls == (1 if interrupt else 2)
        run_id = setup.run_id()
        original = child.delivery.repository.get(run_id + ":step:1")
        assert original is not None and verify_command(original, setup.public_key)
        context = CapabilityExecutionContext.from_command(original)
        assert context.binding.offer_digest == prepared["offer_digest"]
        assert setup.lifecycle_snapshot()[11] == "held"
        with setup.workflows.transaction(write=False) as connection:
            release = connection.execute(
                "SELECT release_evidence_digest FROM capability_reservations "
                "WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=?",
                (OWNER, "workflow-a", "develop", 1)).fetchone()
        assert release is not None and release[0] is None
        setup.tick()
        written = child.reports["write"]
        assert written["write_calls"] == 1 and written["sha256"] == hashlib.sha256(b"fixed output").hexdigest()
        state = setup.lifecycle_snapshot()
        if interrupt:
            terminal = _postcheck(setup, child, original, outcome)
        else:
            assert state[5:11] == ("succeeded", None, "succeeded", "outputs_unavailable", "not_requested", "pending")
            assert state[11:] == ("released", "settled")
            terminal = "succeeded"
        replay = child.operate("replay", original=original)
        assert replay["original"] == written["original"] and replay["restart_io"] == 0
        for _ in range(3):
            setup.tick()
        assert setup.lifecycle_snapshot() == state
        assert child.delivery.repository.get(original["command_id"]) == original
        for table in ("runs", "workflow_dispatches", "capability_step_bindings", "capability_reservations"):
            assert setup.count(table) == 1
        assert setup.count("platform_commands") == (2 if interrupt else 1)
        assert setup.count("workflow_artifact_refs") == setup.count("workflow_evidence") == 0
        with setup.workflows.transaction(write=False) as connection:
            release = connection.execute(
                "SELECT c.release_evidence_digest,d.observation_snapshot,d.token,d.binding_id FROM capability_reservations c "
                "JOIN workflow_dispatches d USING(owner_id,workspace_id,workflow_id,step_key,generation) "
                "WHERE c.owner_id=? AND c.workflow_id=? AND c.step_key=? AND c.generation=?",
                (OWNER, "workflow-a", "develop", 1)).fetchone()
        assert release is not None
        expected_release = canonical_digest({"token": release[2], "binding_id": release[3], **json.loads(release[1])})
        assert release[0] == (None if interrupt else expected_release)
    summary = {"target": target, "case": control["case"], "outcome": outcome, "postcheck_state": terminal, "hub_pid": os.getpid(),
        "node_pids": {operation: report["pid"] for operation, report in child.reports.items()},
        "hostname": prepared["hostname"], "platform": prepared["platform"], "python": prepared["python"],
        "run_id": run_id, "command_id": original["command_id"], "write_calls": 1,
        "reservation": state[11], "budget": state[12], "full_workflow_completed": False}
    (evidence / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
