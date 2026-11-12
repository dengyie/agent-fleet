"""Real publication staging must be reopenable and cleaned after every outcome."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from hub.application.workflow_coding_delivery import WorkflowCodingDeliveryError
from hub.platform.artifacts import ArtifactError
from test_workflow_coding_delivery import DeliveryFixture
from test_workflow_repository import OWNER, WORKSPACE
from test_workflow_task_recovery import build_task_delivery


@pytest.mark.parametrize("publication", ["report", "source_upload"])
@pytest.mark.parametrize("store_fails", [False, True])
def test_original_publication_reopens_closed_staging_and_cleans_after_store_result(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, publication: str, store_fails: bool) -> None:
    fixture = DeliveryFixture(tmp_path) if publication == "report" else build_task_delivery(tmp_path)
    target = fixture.start("develop") if publication == "report" else None
    raw = b'{"result":"complete"}' if publication == "report" else fixture.archive
    original_put = fixture.store.put_file
    observed: list[tuple[Path, str]] = []

    def publish(owner, workspace, source, **kwargs):
        path = Path(source)
        staged = path.read_bytes()
        if publication == "report" or kwargs.get("content_type") == "application/x-tar":
            assert staged == raw
        if store_fails:
            observed.append((path, ""))
            raise ArtifactError("read_failed")
        result = original_put(owner, workspace, path, **kwargs)
        observed.append((path, result["artifact_id"]))
        assert result["size"] == len(staged) and result["sha256"] == hashlib.sha256(staged).hexdigest()
        return result

    monkeypatch.setattr(fixture.store, "put_file", publish)

    def execute():
        if publication == "report":
            return fixture.service._publish(target, raw, "report.json")
        return fixture.upload()

    if store_fails:
        with pytest.raises((ArtifactError, WorkflowCodingDeliveryError), match="read_failed"):
            execute()
    else:
        result = execute()
        artifact_id = result.artifact_id if publication == "report" else "source-attempt-" + fixture.lease["attempt_id"]
        assert fixture.store.read(OWNER, WORKSPACE, artifact_id) == raw
        if publication == "source_upload":
            assert fixture.upload() == result
            assert len(observed) == 2  # Source bundle and original source-revision report.

    assert observed
    for path, _artifact in observed:
        assert not path.exists()


def test_report_failed_publication_does_not_commit_source_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = DeliveryFixture(tmp_path)
    target = fixture.start("develop")
    before = set(fixture.store.root.iterdir())

    def unavailable(*_args, **_kwargs):
        raise ArtifactError("read_failed")

    with monkeypatch.context() as patch:
        patch.setattr(fixture.store, "put_file", unavailable)
        with pytest.raises(WorkflowCodingDeliveryError, match="read_failed"):
            asyncio.run(fixture.service.record_source(target, fixture.exported, executor_id="developer-1"))
    assert set(fixture.store.root.iterdir()) == before
    receipt = asyncio.run(fixture.service.record_source(target, fixture.exported, executor_id="developer-1"))
    assert asyncio.run(fixture.service.record_source(target, fixture.exported, executor_id="developer-1")) == receipt
