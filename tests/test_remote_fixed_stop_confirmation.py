"""An unconfirmed child stop remains unknown across the Node receipt boundary."""
from pathlib import Path

import pytest

from fleet_core.workflow.validators import WorkflowValidatorError
from test_remote_workflow_test_node import NodeFixture


def test_remote_fixed_test_stop_unconfirmed_does_not_report_terminal_failure(tmp_path: Path, monkeypatch):
    fixture = NodeFixture(tmp_path)

    async def interrupted(*args, **kwargs):
        raise WorkflowValidatorError("test_stop_unconfirmed")

    monkeypatch.setattr(fixture.executor.runner, "run", interrupted)
    command = fixture.command()
    first = fixture.client().handle(command)
    assert first["status"] == "unknown"
    assert first["result"]["error_code"] == "test_stop_unconfirmed"
    monkeypatch.setattr(fixture.executor.runner, "run", lambda *args, **kwargs: pytest.fail("cannot replay an uncertain test"))
    assert fixture.client().handle(command) == first
