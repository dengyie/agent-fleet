"""An unconfirmed process stop cannot become a retryable terminal result."""
from pathlib import Path

import pytest

from hub.platform.workflow_validators import WorkflowValidatorError
from test_workflow_repository import OWNER
from test_workflow_review_execution import ReviewFixture
from test_workflow_test_execution import ExecutionFixture


@pytest.mark.parametrize("kind", ["test", "review"])
def test_unconfirmed_stop_persists_unknown_and_does_not_reexecute(tmp_path: Path, monkeypatch, kind: str) -> None:
    fixture = ExecutionFixture(tmp_path) if kind == "test" else ReviewFixture(tmp_path)
    fixture.dispatch()
    worker = fixture.worker()
    service = worker.workflow_test_executor if kind == "test" else worker.workflow_review_executor
    runner = service.runner if kind == "test" else fixture.adapter
    attempts = []

    async def unconfirmed(*args, **kwargs):
        attempts.append(1)
        raise WorkflowValidatorError(kind + "_stop_unconfirmed")

    monkeypatch.setattr(runner, "run", unconfirmed)
    result = worker.run_once(OWNER)
    assert result["state"] == "unknown"
    assert result["result_text"] == kind + "_stop_unconfirmed"
    assert fixture.platform.get_run(OWNER, result["run_id"])["state"] == "unknown"
    assert worker.run_once(OWNER) is None
    assert attempts == [1]
    assert not (fixture.test_evidence() if kind == "test" else fixture.evidence())
