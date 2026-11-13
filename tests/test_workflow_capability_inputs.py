"""Signed input receipt, automatic admission and original worker composition.

The provider writes a fixed output; it does not claim to consume the input.
These are local HTTP integration tests, not a remote validation protocol.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path
from typing import Any, Iterator

import pytest

from hub.domain.capability_execution import CapabilityExecutionContext
from hub.domain.workflow_contract import parse_definition
from hub.domain.workflow_control import WorkflowControlRequest
from hub.infrastructure.workflow_control_repository import WorkflowControlRepository
from hub.infrastructure.workflow_repository import WorkflowRepositoryError
from fixtures.workflow.capability_node_canary import _BoundedTransport
from test_capability_workflow_lifecycle import FixedProvider, Lifecycle, NodeWaiter, _runtime, _setup
from test_workflow_repository import OWNER, WORKSPACE
from test_workflow_two_nodes import server


INPUT = b"fixed signed input bytes\n" * 17
EXECUTION_TABLES = ("workflow_dispatches", "workflow_budget_reservations",
                    "capability_reservations", "capability_step_bindings", "runs")


class ObservedTransport(_BoundedTransport):
    def __init__(self, *, lose_receipt: bool) -> None:
        super().__init__(mode="direct", attempts=1)
        self.lose_receipt = lose_receipt
        self.downloads = 0
        self.lost = False

    def post_json(self, url: str, body: dict[str, Any], headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        result = super().post_json(url, body, headers)
        if url.endswith("/receipts") and self.lose_receipt and not self.lost:
            self.lost = True
            raise OSError("receive response lost after Hub commit")
        return result

    @contextmanager
    def get_stream(self, *args: Any, **kwargs: Any) -> Iterator[Any]:
        self.downloads += 1
        with super().get_stream(*args, **kwargs) as response:
            yield response


def _counts(setup: Lifecycle) -> tuple[int, ...]:
    return tuple(setup.count(table) for table in EXECUTION_TABLES)


def _commands(setup: Lifecycle, action: str) -> tuple[dict[str, Any], ...]:
    with setup.workflows.transaction(write=False) as connection:
        ids = connection.execute("SELECT command_id FROM platform_commands WHERE action=? ORDER BY command_id LIMIT 4",
                                 (action,)).fetchall()
    delivery = setup.app.extensions["fleet"]["services"]["platform_delivery"]
    rows = tuple(delivery.repository.get(row[0]) for row in ids)
    assert all(row is not None for row in rows)
    return rows


def _wait_for_runs(setup: Lifecycle, count: int) -> None:
    for _ in range(8):
        setup.tick()
        if setup.count("runs") == count:
            return
    raise AssertionError("automatic admission did not create the expected original Run")


def _worker(setup: Lifecycle, node: Any, *, interrupt: bool) -> FixedProvider:
    provider = FixedProvider(interrupt=interrupt)
    worker = setup.app.extensions["fleet"]["services"]["platform_worker"]
    worker.provider_factory = lambda profile: provider
    worker.remote_waiter = NodeWaiter(node, setup.app.extensions["fleet"]["services"]["platform_delivery"])
    return provider


@pytest.mark.parametrize("lose_receipt", (False, True))
def test_signed_receive_precedes_automatic_worker_and_restart_never_replays(
    tmp_path: Path, lose_receipt: bool,
) -> None:
    setup = _setup(tmp_path, input_bytes=INPUT)
    transport = ObservedTransport(lose_receipt=lose_receipt)
    with server(setup.app) as url:
        node = _runtime(tmp_path, setup, url, transport=transport)
        try:
            node.report_capabilities()
            setup.tick()
            step = setup.workflows.current_steps(OWNER, "workflow-a")[0]
            assert (step.state, step.reason_code, step.target_key) == ("waiting_node", "inputs_receiving", "node-a")
            assert _counts(setup) == (0, 0, 0, 0, 0)
            receive, = _commands(setup, "artifact.receive")
            polled = node.poll_once()
            assert polled == {"ok": True, "commands": 1, "receipts": 0 if lose_receipt else 1}
            assert _counts(setup) == (0, 0, 0, 0, 0)
            if lose_receipt:
                assert transport.lost and len(node.artifact_receiver.pending_receipts()) == 1
                node.close_all()
                node = _runtime(tmp_path, setup, url, transport=transport)
                assert node.poll_once() == {"ok": True, "commands": 0, "receipts": 1}
            assert node.artifact_receiver.pending_receipts() == ()
            received, = (tmp_path / "node-inputs").glob("*.txt")
            assert received.read_bytes() == INPUT
            assert received.name == receive["grant_id"] + ".txt"
            assert setup.input_artifact is not None
            assert hashlib.sha256(received.read_bytes()).hexdigest() == setup.input_artifact.sha256
            setup.tick()
            assert setup.workflows.current_steps(OWNER, "workflow-a")[0].state == "ready"
            assert _counts(setup) == (0, 0, 0, 0, 0)
            _wait_for_runs(setup, 1)
            assert _counts(setup) == (1, 1, 1, 1, 1)
            writes = 0
            original_write = node.backend.write

            def counted_write(path: str, content: bytes) -> Any:
                nonlocal writes
                writes += 1
                return original_write(path, content)

            node.backend.write = counted_write
            provider = _worker(setup, node, interrupt=False)
            result = setup.app.extensions["fleet"]["services"]["platform_worker"].run_once(OWNER)
            assert result is not None and result["state"] == "succeeded"
            assert writes == 1 and provider.calls == 2
            assert setup.lifecycle_snapshot()[11] == "held"
            setup.tick()
            state = setup.lifecycle_snapshot()
            assert state[5:11] == ("succeeded", None, "succeeded", "outputs_unavailable", "not_requested", "pending")
            assert state[11:] == ("released", "settled")
            command, = _commands(setup, "tool.workspace.write")
            context = CapabilityExecutionContext.from_command(command)
            assert context.binding.node_id == "node-a" and context.binding.input_digest == step.resolved_digest
            assert setup.count("workflow_evidence") == 0
            node.close_all()
            node = _runtime(tmp_path, setup, url, transport=transport)
            restart_io = 0

            def forbidden_write(*arguments: Any) -> Any:
                nonlocal restart_io
                restart_io += 1
                raise AssertionError("terminal journal must not repeat the write")

            node.backend.write = forbidden_write
            assert node.client.handle(command)["status"] == "succeeded"
            for _ in range(3):
                setup.tick()
                assert node.poll_once()["commands"] == 0
            assert restart_io == 0 and transport.downloads == 1
            assert setup.lifecycle_snapshot() == state and _counts(setup) == (1, 1, 1, 1, 1)
            assert _commands(setup, "artifact.receive")[0]["command_id"] == receive["command_id"]
        finally:
            node.close_all()


@pytest.mark.parametrize("interrupt", (False, True))
def test_two_received_workflows_share_one_slot_and_unknown_keeps_it(tmp_path: Path, interrupt: bool) -> None:
    setup = _setup(tmp_path, input_bytes=INPUT)
    definition = parse_definition(setup.workflows.get(OWNER, "workflow-a").definition_snapshot)
    setup.workflows.create_or_get(OWNER, WORKSPACE, "workflow-b", client_token="workflow-b", definition=definition, now=100)
    with server(setup.app) as url:
        node = _runtime(tmp_path, setup, url)
        try:
            node.report_capabilities()
            setup.tick()
            receives = _commands(setup, "artifact.receive")
            assert len(receives) == 2 and _counts(setup) == (0, 0, 0, 0, 0)
            assert node.poll_once() == {"ok": True, "commands": 2, "receipts": 2}
            assert len(tuple((tmp_path / "node-inputs").glob("*.txt"))) == 2
            _wait_for_runs(setup, 1)
            for _ in range(2):
                setup.tick()
            assert _counts(setup) == (1, 1, 1, 1, 1)
            with setup.workflows.transaction(write=False) as connection:
                first = connection.execute("SELECT workflow_id FROM workflow_dispatches LIMIT 1").fetchone()[0]
            waiting = "workflow-b" if first == "workflow-a" else "workflow-a"
            step = setup.workflows.current_steps(OWNER, waiting)[0]
            assert (step.state, step.reason_code, step.target_key, step.generation) == ("waiting_node", "capability_unavailable", "node-a", 1)
            original_write = node.backend.write
            writes = 0

            def counted_write(path: str, content: bytes) -> Any:
                nonlocal writes
                writes += 1
                receipt = original_write(path, content)
                assert receipt.state == "succeeded"
                if interrupt:
                    raise OSError("fixed write completed before result loss")
                return receipt

            node.backend.write = counted_write
            provider = _worker(setup, node, interrupt=interrupt)
            worker = setup.app.extensions["fleet"]["services"]["platform_worker"]
            result = worker.run_once(OWNER)
            assert result is not None and result["state"] == ("unknown" if interrupt else "succeeded")
            assert writes == 1 and provider.calls == (1 if interrupt else 2)
            for _ in range(6):
                setup.tick()
            if interrupt:
                current = setup.workflows.get(OWNER, first)
                with pytest.raises(WorkflowRepositoryError) as error:
                    WorkflowControlRepository(setup.workflows).control(WorkflowControlRequest(
                        OWNER, first, "retry", current.revision, "blocked-retry", "develop", 1), now=setup.clock[0])
                assert error.value.code == "unresolved_execution"
                assert _counts(setup) == (1, 1, 1, 1, 1)
                assert worker.run_once(OWNER) is None
            else:
                assert _counts(setup) == (2, 2, 2, 2, 2)
                _worker(setup, node, interrupt=False)
                assert worker.run_once(OWNER)["state"] == "succeeded"
                for _ in range(3):
                    setup.tick()
                assert writes == 2
            with setup.workflows.transaction(write=False) as connection:
                reservations = connection.execute("SELECT state,count(*) FROM capability_reservations GROUP BY state").fetchall()
            assert {row[0]: row[1] for row in reservations} == ({"held": 1} if interrupt else {"released": 2})
            assert setup.workflows.current_steps(OWNER, waiting)[0].target_key == step.target_key
            assert tuple(row["command_id"] for row in _commands(setup, "artifact.receive")) == tuple(row["command_id"] for row in receives)
            assert setup.count("workflow_evidence") == 0
        finally:
            node.close_all()
