"""Muse journal references stay bound to the original Workflow dispatch token."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from hub.application.workflow_coordinator import WorkflowCoordinator
from hub.application.workflow_dispatch import WorkflowDispatchError
from tools.browser_bridge.muse import MuseBridge
from connectors.external_agents.base import ExternalAgentError, ExternalTaskRef
from connectors.external_agents.muse import MuseExternalAgentAdapter
from test_external_agent_workflow import held, make_external
from test_muse_browser_bridge import Transport, message
from test_workflow_control import control
from test_workflow_repository import OWNER, WORKSPACE


class ReceiptBridge:
    def __init__(self, receipt: Mapping[str, Any]) -> None:
        self.receipt = receipt
        self.calls: list[tuple[str, ...]] = []

    def submit(self, task_id: str, prompt: str, *, scope: str) -> Mapping[str, Any]:
        self.calls.append(("submit", task_id, prompt, scope))
        return self.receipt

    def read(self, task_id: str, *, scope: str) -> Mapping[str, Any]:
        self.calls.append(("read", task_id, scope))
        return {"task_id": task_id, "state": "completed"}


@pytest.mark.parametrize("state", ["sent", "running", "waiting_input", "completed", "failed", "dispatching"])
def test_submit_rejects_foreign_journal_identity_as_uncertain(state: str) -> None:
    bridge = ReceiptBridge({"task_id": "foreign-task", "state": state})
    adapter = MuseExternalAgentAdapter(bridge, workspace_id="workspace")
    with pytest.raises(ExternalAgentError) as error:
        adapter.submit("dispatch-1", {"prompt": "once"})
    assert error.value.code == "external_task_mismatch" and error.value.uncertain
    assert bridge.calls == [("submit", "dispatch-1", "once", "workspace")]


@pytest.mark.parametrize("receipt,code", [
    ({"state": "sent"}, "dispatch_identity_missing"),
    ({"task_id": None, "state": "sent"}, "dispatch_identity_missing"),
    ({"task_id": 42, "state": "sent"}, "dispatch_identity_missing"),
    ({"task_id": "dispatch-1", "state": "unknown"}, "dispatch_unknown"),
    ({"task_id": "foreign-task", "state": "unknown"}, "dispatch_unknown"),
    ({"state": "unknown"}, "dispatch_unknown"),
])
def test_missing_identity_and_unknown_keep_existing_error_semantics(receipt: Mapping[str, Any], code: str) -> None:
    bridge = ReceiptBridge(receipt)
    adapter = MuseExternalAgentAdapter(bridge, workspace_id="workspace")
    with pytest.raises(ExternalAgentError) as error:
        adapter.submit("dispatch-1", {"prompt": "once"})
    assert error.value.code == code and error.value.uncertain
    assert bridge.calls == [("submit", "dispatch-1", "once", "workspace")]


@pytest.mark.parametrize("operation", ["inspect", "request_cancel", "export_outputs"])
@pytest.mark.parametrize("task", [
    ExternalTaskRef("muse", "dispatch-1", "foreign-task"),
    ExternalTaskRef("muse", "dispatch-1"),
    ExternalTaskRef("muse", "dispatch-1", "dispatch-1", "unsupported-session"),
    ExternalTaskRef("other", "dispatch-1", "dispatch-1"),
])
def test_task_operations_reject_foreign_or_incomplete_refs_before_io(operation: str, task: ExternalTaskRef) -> None:
    bridge = ReceiptBridge({"task_id": "dispatch-1", "state": "sent"})
    adapter = MuseExternalAgentAdapter(bridge, workspace_id="workspace")
    arguments = (task, ()) if operation == "export_outputs" else (task,)
    with pytest.raises(ExternalAgentError) as error:
        getattr(adapter, operation)(*arguments)
    assert error.value.code == "external_task_mismatch" and not error.value.uncertain
    assert bridge.calls == []


def test_original_journal_identity_survives_adapter_restart(tmp_path: Path) -> None:
    transport = Transport()
    bridge = MuseBridge(tmp_path / "muse.db", transport=transport)
    adapter = MuseExternalAgentAdapter(bridge, workspace_id=WORKSPACE)
    task = adapter.submit("dispatch-1", {"prompt": "once"})
    assert task == ExternalTaskRef("muse", "dispatch-1", "dispatch-1")
    journal = bridge.lookup("dispatch-1", "once", scope=WORKSPACE)
    transport.messages.append(message("done", "assistant", "done\n" + journal["completion_marker"]))
    restarted = MuseExternalAgentAdapter(MuseBridge(bridge.path, transport=transport), workspace_id=WORKSPACE)
    assert restarted.lookup("dispatch-1", {"prompt": "once"}) == task
    assert restarted.inspect(task).state == "succeeded"
    assert restarted.request_cancel(task).state == "unsupported"
    with pytest.raises(ExternalAgentError, match="output_export_unsupported"):
        restarted.export_outputs(task, ())
    assert len(transport.sends) == 1


def test_foreign_submit_receipt_preserves_unknown_until_explicit_original_task_reconciliation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    setup = make_external(tmp_path)
    original_submit = setup.adapter.bridge.submit

    def foreign_receipt(*args: Any, **kwargs: Any) -> Mapping[str, Any]:
        receipt = original_submit(*args, **kwargs)
        return {**receipt, "task_id": "foreign-task"}

    monkeypatch.setattr(setup.adapter.bridge, "submit", foreign_receipt)
    with pytest.raises(WorkflowDispatchError, match="dispatch_unknown") as error:
        setup.dispatch()
    assert error.value.__cause__.code == "external_submit_unknown"
    cause = error.value.__cause__.__cause__
    assert isinstance(cause, ExternalAgentError)
    assert cause.code == "external_task_mismatch" and cause.uncertain
    record = setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1)
    assert record.stage == "unknown" and record.binding is None
    assert setup.repository.get(OWNER, "workflow-a").execution_state == "unknown"
    assert held(setup) == "held" and len(setup.transport.sends) == 1
    with setup.repository.transaction(write=False) as connection:
        pin = connection.execute("SELECT send_started_at,task_snapshot FROM workflow_external_bindings").fetchone()
        assert pin[0] is not None and pin[1] is None
    with pytest.raises(WorkflowDispatchError, match="reconciliation_required"):
        setup.dispatch()

    setup.clock.now = 130
    bridge = MuseBridge(setup.adapter.bridge.path, transport=setup.transport)
    setup.adapter = MuseExternalAgentAdapter(bridge, workspace_id=WORKSPACE,
        account_ref="account-a", transport_ref="fixture-v1")
    journal = bridge.lookup(record.token, setup.resolved.snapshot.decode(), scope=WORKSPACE)
    assert journal["task_id"] == record.token
    assert setup.adapter.lookup(record.token, {"prompt": setup.resolved.snapshot.decode()}) == ExternalTaskRef("muse", record.token, record.token)
    assert setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1).binding is None

    coordinator = WorkflowCoordinator(setup.repository, worker_id="reconciler", clock=setup.clock,
        executors=((OWNER, WORKSPACE, setup.target.target_key, setup.executor(enabled=False)),))
    control(setup, "reconcile", step="develop")
    first = asyncio.run(coordinator.tick(setup.clock()))
    assert first.claimed == 1 and first.failures == ()
    assert setup.repository.get(OWNER, "workflow-a").execution_state == "unknown"
    assert setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1).binding is None
    assert held(setup) == "held" and len(setup.transport.sends) == 1

    setup.transport.messages.append(message("done", "assistant", "done\n" + journal["completion_marker"]))
    control(setup, "reconcile", step="develop")
    second = asyncio.run(coordinator.tick(setup.clock()))
    assert second.claimed == 1 and second.failures == ()
    final = setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1)
    assert final.token == record.token and final.binding is not None and final.step_state == "succeeded"
    assert held(setup) == "released" and len(setup.transport.sends) == 1
    assert setup.store.read_binding(final.binding, setup.target).task == ExternalTaskRef("muse", record.token, record.token)
    workflow = setup.repository.get(OWNER, "workflow-a")
    assert workflow.acceptance_state == "pending" and workflow.validation_state == "not_requested"
