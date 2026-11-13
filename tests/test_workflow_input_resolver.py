"""Actual current source output resolution; no static predecessor stand-ins."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from hub.application.workflow_coordinator import RegisteredWorkflowPlan
from hub.domain.workflow_contract_types import CatalogRef
from hub.infrastructure.workflow_input_resolver import WorkflowArtifactInputResolver
from hub.infrastructure.workflow_repository import WorkflowRepositoryError
from hub.platform.artifacts import ArtifactStore
from test_workflow_coding_delivery import DeliveryFixture
from test_workflow_coordinator import make_setup
from test_workflow_repository import OWNER, WORKSPACE


def test_resolver_freezes_actual_current_revision_and_bundle(tmp_path: Path) -> None:
    fixture = DeliveryFixture(tmp_path)
    target = fixture.start("develop")
    source = asyncio.run(fixture.service.record_source(target, fixture.exported, executor_id="developer-1"))
    fixture.finish(target)
    snapshot = fixture.repository.snapshots((fixture.claim,), now=102)[0]
    resolver = WorkflowArtifactInputResolver(fixture.repository, fixture.store)
    resolved = asyncio.run(resolver.resolve(snapshot, "review", CatalogRef("reviewer", 1)))
    assert resolved.contract.input_refs[0].reference == fixture.exported.revision
    fixture.repository.freeze_inputs(fixture.claim, step_key="review", generation=1, resolved=resolved,
                                    expected_revision=snapshot.workflow.revision, now=102)
    assert source.subject_digest == fixture.repository.current_steps(OWNER, "workflow-a")[0].subject_digest
    assert fixture.repository.current_steps(OWNER, "workflow-a")[1].resolved_snapshot == resolved.snapshot


def test_resolver_rejects_snapshot_revision_changed_during_file_resolution(tmp_path: Path) -> None:
    fixture = DeliveryFixture(tmp_path)
    target = fixture.start("develop")
    snapshot = fixture.repository.snapshots((fixture.claim,), now=102)[0]
    asyncio.run(fixture.service.record_source(target, fixture.exported, executor_id="developer-1"))
    with pytest.raises(WorkflowRepositoryError, match="revision_conflict"):
        asyncio.run(WorkflowArtifactInputResolver(fixture.repository, fixture.store).resolve(snapshot, "review", CatalogRef("reviewer", 1)))


def test_resolver_never_substitutes_a_foreign_source_artifact(tmp_path: Path) -> None:
    fixture = DeliveryFixture(tmp_path)
    target = fixture.start("develop")
    asyncio.run(fixture.service.record_source(target, fixture.exported, executor_id="developer-1"))
    fixture.finish(target)
    foreign = tmp_path / "foreign.json"
    foreign.write_text("{}", encoding="utf-8")
    manifest = fixture.store.put_file("other@example.test", WORKSPACE, foreign, name="foreign.json", content_type="application/json")
    with fixture.repository.transaction() as connection:
        connection.execute("UPDATE workflow_artifact_refs SET artifact_id=?,sha256=?,size=? WHERE output_key='output:source'",
                           (manifest["artifact_id"], manifest["sha256"], manifest["size"]))
    snapshot = fixture.repository.snapshots((fixture.claim,), now=102)[0]
    with pytest.raises(WorkflowRepositoryError, match="reference_forbidden"):
        asyncio.run(WorkflowArtifactInputResolver(fixture.repository, fixture.store).resolve(snapshot, "review", CatalogRef("reviewer", 1)))


def test_dynamic_plan_uses_real_resolution_and_existing_executor(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    previous = setup.registrations[0]
    registration = RegisteredWorkflowPlan(OWNER, WORKSPACE, previous.definition_digest, "develop", CatalogRef("cli", 1),
        CatalogRef("safe", 1), "node-a", 1, 20, 0)
    resolver = WorkflowArtifactInputResolver(setup.repository, ArtifactStore(tmp_path / "artifacts"))
    # This template has no file inputs; no artifact I/O is needed.
    coordinator = setup.coordinator(registrations=(registration,), input_resolver=resolver)
    for _ in range(3):
        setup.tick(coordinator)
    assert setup.executor.created == 1
    assert setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1).binding.run_id == setup.run_id()
