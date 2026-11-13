"""Fixed validators and atomic delivery publication against Git and SQLite.

DirectoryBackend is used only by recipe contract tests; it is not sandbox
acceptance evidence. Test execution bindings below represent adapter receipts.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from hub.application.workflow_coding_delivery import WorkflowCodingDeliveryError, WorkflowCodingDeliveryService
from hub.application.workflow_templates import coding_delivery_definition
from hub.domain.workflow_contract import resolve_contract
from hub.domain.workflow_contract_types import ArtifactReference, AuthorizedInputs, CatalogRef, CodeRevisionReference, InputBinding, PriorOutputReference
from hub.domain.workflow_delivery import DeliveryOutput, DeliveryRecord, DeliveryTarget
from hub.domain.workflow_execution import ExecutionBinding, ExecutionObservation
from hub.infrastructure.workflow_delivery_repository import WorkflowDeliveryRepository
from hub.infrastructure.workflow_repository import WorkflowRepositoryError
from hub.platform.artifacts import ArtifactStore
from fleet_core.backends.directory import DirectoryBackend
from hub.platform.workflow_validators import (
    CODING_DELIVERY_TEST_RECIPE, FixedTestRunner, ReviewIdentity, WorkflowValidatorError,
    revision_payload, test_report_bytes, validate_review_report,
)
from hub.platform.workflow_validators import TestRecipe as Recipe
from test_workflow_repository import OWNER, WORKSPACE, prepare_database
from test_workflow_source import git, repo_with_commit

REVIEWER = ReviewIdentity("reviewer-1", "service")


def source() -> CodeRevisionReference:
    return CodeRevisionReference("repo-a", "1" * 40, "2" * 40, "3" * 40, ArtifactReference("bundle", "a" * 64, 10))


def review(reference: CodeRevisionReference | None = None, *, subject: str = "a" * 64, verdict: str = "passed", executor: ReviewIdentity = REVIEWER) -> bytes:
    return json.dumps({"schema_version": 1, "source_revision": revision_payload(reference or source()), "subject_digest": subject,
                       "verdict": verdict, "findings": [], "executor": executor.payload()}, separators=(",", ":")).encode()


def validate(raw: bytes):
    return validate_review_report(raw, source_revision=source(), subject_digest="a" * 64,
                                  executor=REVIEWER, developer_executor_id="developer-1")


def test_review_validator_binds_source_subject_and_trusted_executor() -> None:
    result = validate(review())
    assert result.verdict == "passed" and result.executor == REVIEWER
    assert result.report_hash == hashlib.sha256(review()).hexdigest()


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(subject_digest="b" * 64),
    lambda value: value.update(source_revision={**revision_payload(source()), "head_commit": "4" * 40}),
    lambda value: value.update(executor={"id": "forged-reviewer", "kind": "service", "model_id": None}),
    lambda value: value.update(schema_version=True),
    lambda value: value.update(findings=[{"code": "x", "severity": "invalid", "message": "x"}]),
])
def test_review_validator_rejects_stale_or_forged_evidence(mutation) -> None:
    value = json.loads(review())
    mutation(value)
    with pytest.raises(WorkflowValidatorError):
        validate(json.dumps(value, separators=(",", ":")).encode())


def test_developer_cannot_review_own_delivery() -> None:
    with pytest.raises(WorkflowValidatorError, match="independent_reviewer_required"):
        validate_review_report(review(), source_revision=source(), subject_digest="a" * 64,
                               executor=REVIEWER, developer_executor_id=REVIEWER.executor_id)


def run_tests(path: Path, reference: CodeRevisionReference | None = None, subject: str = "a" * 64):
    return asyncio.run(FixedTestRunner(backend_factory=DirectoryBackend).run(reference or source(), subject, path,
                       recipe_id=CODING_DELIVERY_TEST_RECIPE.recipe_id, executor_id="test-runner-1"))


def test_fixed_recipe_executes_actual_tests_and_keeps_junit(tmp_path: Path) -> None:
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    result = run_tests(tmp_path)
    assert result.passed and result.exit_code == 0 and not result.timed_out
    assert b'test_ok' in result.junit
    assert test_report_bytes(result, source()) == result.raw_report


def test_missing_sandbox_never_falls_back_to_host_execution(tmp_path: Path) -> None:
    with pytest.raises(WorkflowValidatorError, match="sandbox_unavailable"):
        asyncio.run(FixedTestRunner().run(source(), "a" * 64, tmp_path,
                    recipe_id=CODING_DELIVERY_TEST_RECIPE.recipe_id, executor_id="test-runner-1"))
    assert not (tmp_path / ".fleet-workflow-results.xml").exists()


def test_unregistered_recipe_is_rejected_before_execution(tmp_path: Path) -> None:
    with pytest.raises(WorkflowValidatorError, match="recipe_not_registered"):
        asyncio.run(FixedTestRunner().run(source(), "a" * 64, tmp_path, recipe_id="shell-command", executor_id="test-runner-1"))


def test_fixed_recipe_child_does_not_inherit_hub_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLEET_TEST_SECRET", "must-not-reach-test-process")
    (tmp_path / "test_env.py").write_text("import os\ndef test_env():\n    assert 'FLEET_TEST_SECRET' not in os.environ\n", encoding="utf-8")
    assert run_tests(tmp_path).passed


def test_cancelled_test_waits_for_bounded_backend_and_reaps_process(tmp_path: Path) -> None:
    (tmp_path / "hang.py").write_text("from pathlib import Path\nimport os, time\nPath('started').write_text(str(os.getpid()))\ntime.sleep(30)\n", encoding="utf-8")
    recipe = Recipe("bounded-test", 1, (sys.executable, "hang.py"), 1)
    runner = FixedTestRunner({recipe.recipe_id: recipe}, backend_factory=DirectoryBackend)
    async def scenario() -> None:
        task = asyncio.create_task(runner.run(source(), "a" * 64, tmp_path, recipe_id=recipe.recipe_id, executor_id="test-runner-1"))
        for _ in range(100):
            if (tmp_path / "started").exists():
                break
            await asyncio.sleep(.01)
        assert (tmp_path / "started").exists()
        task.cancel()
        asyncio.get_running_loop().call_later(.05, task.cancel)
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ProcessLookupError):
            os.kill(int((tmp_path / "started").read_text()), 0)
    asyncio.run(scenario())


def test_junit_symlink_and_entity_documents_are_rejected(tmp_path: Path) -> None:
    from hub.platform.workflow_validators import _read_junit

    report = tmp_path / ".fleet-workflow-results.xml"
    report.write_bytes(b'<!DOCTYPE x [<!ENTITY x "expanded">]><testsuite><testcase name="&x;"/></testsuite>')
    with pytest.raises(WorkflowValidatorError, match="invalid_junit"):
        _read_junit(tmp_path)
    report.write_bytes('<!DOCTYPE x><testsuite><testcase/></testsuite>'.encode('utf-16'))
    with pytest.raises(WorkflowValidatorError, match="invalid_junit"):
        _read_junit(tmp_path)
    linkdir = tmp_path / "links"
    linkdir.mkdir()
    (linkdir / ".fleet-workflow-results.xml").symlink_to(report)
    with pytest.raises(WorkflowValidatorError, match="invalid_junit"):
        _read_junit(linkdir)


@pytest.mark.parametrize("content", ["def test_fail():\n    assert False\n", "import pytest\n@pytest.mark.skip\ndef test_skip():\n    assert True\n"])
def test_failure_and_all_skips_cannot_become_passed(tmp_path: Path, content: str) -> None:
    (tmp_path / "test_case.py").write_text(content, encoding="utf-8")
    assert not run_tests(tmp_path).passed


@pytest.mark.parametrize("field,value", [("recipe_revision", 2), ("executor_id", "forged"), ("exit_code", 12), ("junit", b"<testsuite/>")])
def test_test_report_rejects_tampered_receipt_fields(tmp_path: Path, field: str, value: object) -> None:
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    result = run_tests(tmp_path)
    with pytest.raises(WorkflowValidatorError):
        test_report_bytes(replace(result, **{field: value}), source())


@pytest.mark.parametrize("field,value", [("passed", 1), ("timed_out", 0), ("exit_code", False),
                                        ("output_truncated", 0), ("recipe", {"id": "coding-delivery-tests", "revision": True})])
def test_test_report_rejects_coerced_scalar_types(tmp_path: Path, field: str, value: object) -> None:
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    result = run_tests(tmp_path)
    payload = json.loads(result.raw_report)
    payload[field] = value
    raw = json.dumps(payload, separators=(",", ":")).encode()
    with pytest.raises(WorkflowValidatorError):
        test_report_bytes(replace(result, raw_report=raw, report_hash=hashlib.sha256(raw).hexdigest()), source())


class DeliveryFixture:
    def __init__(self, path: Path) -> None:
        self.repository = prepare_database(path / "platform.db")
        self.definition = coding_delivery_definition()
        self.repository.create_or_get(OWNER, WORKSPACE, "workflow-a", client_token="create", definition=self.definition, now=100)
        self.store = ArtifactStore(path / "artifacts", clock=lambda: 100)
        self.delivery = WorkflowDeliveryRepository(self.repository)
        self.service = WorkflowCodingDeliveryService(self.delivery, self.store, clock=lambda: 101)
        self.claim = self.repository.claim_due(OWNER, worker_id="test-adapter", now=100)[0]
        repo, base, head, tree = repo_with_commit(path)
        (repo / "test_delivery.py").write_text("def test_delivery():\n    assert 6 * 7 == 42\n", encoding="utf-8")
        git(repo, "add", "test_delivery.py")
        git(repo, "commit", "-qm", "delivery test")
        head, tree = git(repo, "rev-parse", "HEAD"), git(repo, "rev-parse", "HEAD^{tree}")
        self.exported = asyncio.run(self.service.export_source(repo, OWNER, WORKSPACE, repository_id="repo-a", base_commit=base, head_commit=head, tree_id=tree))

    def start(self, key: str) -> DeliveryTarget:
        step = {item.step_key: item for item in self.definition.steps}[key]
        bindings = tuple(InputBinding(item.ref_key, OWNER, WORKSPACE, self.exported.revision,
                                      item.reference if type(item.reference) is PriorOutputReference else None) for item in step.contract.input_refs)
        resolved = resolve_contract(self.definition, AuthorizedInputs(OWNER, WORKSPACE, key, CatalogRef("test-adapter", 1), bindings))
        revision = self.repository.get(OWNER, "workflow-a").revision
        self.repository.freeze_inputs(self.claim, step_key=key, generation=1, resolved=resolved, expected_revision=revision, now=101)
        revision = self.repository.get(OWNER, "workflow-a").revision
        self.repository.admit_step(self.claim, step_key=key, generation=1, expected_revision=revision, target_key="node-a", capacity=1,
                                   reserve_tokens=0, reserve_requests=0, now=101)
        revision = self.repository.get(OWNER, "workflow-a").revision
        record = self.repository.begin_dispatch(self.claim, step_key=key, generation=1, expected_revision=revision, now=101)
        binding = ExecutionBinding(OWNER, WORKSPACE, "node-a", record.token, record.payload_hash, "external", "execution-" + key)
        record = self.repository.bind_dispatch(self.claim, record, binding, now=101)
        return DeliveryTarget("workflow-a", key, 1, record.revision, binding)

    def finish(self, target: DeliveryTarget) -> None:
        record = self.repository.get_dispatch(OWNER, "workflow-a", target.step_key, target.generation)
        self.repository.apply_observation(self.claim, record, ExecutionObservation(target.binding, "succeeded"), now=102)


def test_coding_template_requires_revision_bundle_reports_and_junit() -> None:
    definition = coding_delivery_definition()
    assert [item.kind for item in definition.steps] == ["coding_task", "coding_task", "validation"]
    assert {item.output_key for item in definition.steps[0].contract.output_specs if item.required} == {"source", "source_bundle"}
    assert {item.output_key for item in definition.steps[2].contract.output_specs if item.required} == {"test_report", "junit"}


def test_source_publication_is_atomic_fenced_and_idempotent(tmp_path: Path) -> None:
    fixture = DeliveryFixture(tmp_path)
    target = fixture.start("develop")
    receipt = asyncio.run(fixture.service.record_source(target, fixture.exported, executor_id="developer-1"))
    assert receipt.revision == target.expected_revision + 1
    assert {item.output_key for item in receipt.outputs} == {"source", "source_bundle"}
    before = set(fixture.store.root.iterdir())
    assert asyncio.run(fixture.service.record_source(target, fixture.exported, executor_id="developer-1")) == receipt
    assert set(fixture.store.root.iterdir()) == before
    with pytest.raises(WorkflowCodingDeliveryError, match="idempotency_conflict"):
        asyncio.run(fixture.service.record_source(target, fixture.exported, executor_id="other-developer"))
    assert fixture.repository.get(OWNER, "workflow-a").revision == receipt.revision


@pytest.mark.parametrize("changed", ["generation", "execution_id", "kind", "run_id", "target_key"])
def test_delivery_receipt_cannot_be_recovered_with_another_execution_scope(tmp_path: Path, changed: str) -> None:
    fixture = DeliveryFixture(tmp_path)
    target = fixture.start("develop")
    receipt = asyncio.run(fixture.service.record_source(target, fixture.exported, executor_id="developer-1"))
    alternate = replace(target, generation=2) if changed == "generation" else replace(target, binding=replace(
        target.binding, **{changed: {"execution_id": "another-execution", "kind": "task", "run_id": "another-run", "target_key": "node-b"}[changed]}))
    before = set(fixture.store.root.iterdir())
    with pytest.raises(WorkflowCodingDeliveryError, match="idempotency_conflict"):
        asyncio.run(fixture.service.record_source(alternate, fixture.exported, executor_id="developer-1"))
    assert set(fixture.store.root.iterdir()) == before
    assert asyncio.run(fixture.service.record_source(target, fixture.exported, executor_id="developer-1")) == receipt


def test_review_evidence_and_output_share_source_subject_and_transaction(tmp_path: Path) -> None:
    fixture = DeliveryFixture(tmp_path)
    target = fixture.start("develop")
    source_receipt = asyncio.run(fixture.service.record_source(target, fixture.exported, executor_id="developer-1"))
    fixture.finish(target)
    review_target = fixture.start("review")
    raw = review(fixture.exported.revision, subject=source_receipt.subject_digest)
    receipt = asyncio.run(fixture.service.record_review(review_target, raw, executor=REVIEWER))
    assert receipt.subject_digest == source_receipt.subject_digest
    assert asyncio.run(fixture.service.record_review(review_target, raw, executor=REVIEWER)) == receipt
    with fixture.repository.transaction(write=False) as connection:
        evidence = connection.execute("SELECT subject_digest,report_artifact_id,report_hash FROM workflow_evidence").fetchone()
        assert tuple(evidence) == (source_receipt.subject_digest, receipt.outputs[0].artifact.artifact_id, hashlib.sha256(raw).hexdigest())
        assert connection.execute("SELECT count(*) FROM workflow_artifact_refs WHERE output_key='output:review_report'").fetchone()[0] == 1


def test_failed_review_blocks_test_admission(tmp_path: Path) -> None:
    fixture = DeliveryFixture(tmp_path)
    target = fixture.start("develop")
    source_receipt = asyncio.run(fixture.service.record_source(target, fixture.exported, executor_id="developer-1"))
    fixture.finish(target)
    review_target = fixture.start("review")
    asyncio.run(fixture.service.record_review(review_target, review(fixture.exported.revision, subject=source_receipt.subject_digest, verdict="failed"), executor=REVIEWER))
    fixture.finish(review_target)
    with pytest.raises(WorkflowRepositoryError, match="predecessor_not_ready"):
        fixture.start("test")


@pytest.mark.parametrize("change", ["binding", "revision", "generation", "cancel"])
def test_invalid_or_late_deliveries_cannot_publish_outputs(tmp_path: Path, change: str) -> None:
    fixture = DeliveryFixture(tmp_path)
    target = fixture.start("develop")
    if change == "binding":
        target = replace(target, binding=replace(target.binding, execution_id="forged"))
    elif change == "revision":
        target = replace(target, expected_revision=1)
    elif change == "generation":
        target = replace(target, generation=2)
    else:
        with fixture.repository.transaction() as connection:
            connection.execute("UPDATE workflow_runs SET cancel_requested=1 WHERE workflow_id='workflow-a'")
    with pytest.raises(WorkflowCodingDeliveryError):
        asyncio.run(fixture.service.record_source(target, fixture.exported, executor_id="developer-1"))
    with fixture.repository.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM workflow_artifact_refs WHERE output_key LIKE 'output:%'").fetchone()[0] == 0


def test_evidence_failure_rolls_back_outputs_and_revision(tmp_path: Path) -> None:
    fixture = DeliveryFixture(tmp_path)
    target = fixture.start("develop")
    from hub.domain.workflow_delivery import DeliveryEvidence
    artifact = fixture.exported.revision.source_bundle
    record = DeliveryRecord("a" * 64, (DeliveryOutput("source_bundle", "artifact", artifact),),
                            DeliveryEvidence("undeclared@1", "passed", "test-adapter", artifact, "b" * 64))
    with pytest.raises(WorkflowRepositoryError, match="validator_not_declared"):
        fixture.delivery.commit(target, "source", "a" * 64, record, now=101)
    assert fixture.repository.get(OWNER, "workflow-a").revision == target.expected_revision
    with fixture.repository.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM workflow_artifact_refs WHERE output_key LIKE 'output:%'").fetchone()[0] == 0


def test_actual_git_and_test_reports_require_explicit_current_http_acceptance(tmp_path: Path) -> None:
    from hub.bootstrap import create_app
    from hub.config import FleetConfig
    from hub.platform.workflow_source import extract_source_bundle

    fixture = DeliveryFixture(tmp_path)
    target = fixture.start("develop")
    source_receipt = asyncio.run(fixture.service.record_source(target, fixture.exported, executor_id="developer-1"))
    fixture.finish(target)
    review_target = fixture.start("review")
    asyncio.run(fixture.service.record_review(review_target, review(fixture.exported.revision, subject=source_receipt.subject_digest), executor=REVIEWER))
    fixture.finish(review_target)
    test_target = fixture.start("test")
    checkout = tmp_path / "test-checkout"
    extract_source_bundle(fixture.store, OWNER, WORKSPACE, fixture.exported.revision.source_bundle.artifact_id, checkout,
                          expected_revision=fixture.exported.revision)
    result = run_tests(checkout, fixture.exported.revision, source_receipt.subject_digest)
    receipt = asyncio.run(fixture.service.record_test(test_target, result))
    assert {item.output_key for item in receipt.outputs} == {"test_report", "junit"}
    assert asyncio.run(fixture.service.record_test(test_target, result)) == receipt
    fixture.finish(test_target)
    app = create_app(FleetConfig.from_root(tmp_path, platform_enabled=True, platform_workflows_enabled=True,
        platform_db=fixture.repository.db_path, platform_artifact_root=fixture.store.root, dev_operator=OWNER, serve_frontend=False))
    client = app.test_client()
    detail = client.get("/api/platform/v1/workflows/workflow-a").get_json()["workflow"]
    assert (detail["execution_state"], detail["validation_state"], detail["acceptance_state"], detail["delivery_state"]) == ("succeeded", "passed", "pending", "awaiting_acceptance")
    assert {step["subject_digest"] for step in detail["steps"]} == {source_receipt.subject_digest}
    for output in receipt.outputs:
        response = client.get(f"/api/platform/v1/artifacts/{output.artifact.artifact_id}/content?workspace_id={WORKSPACE}")
        assert response.status_code == 200
        assert hashlib.sha256(response.data).hexdigest() == output.artifact.sha256
        foreign = client.get(f"/api/platform/v1/artifacts/{output.artifact.artifact_id}/content?workspace_id={WORKSPACE}",
                             headers={"Cf-Access-Authenticated-User-Email": "other@example.test"})
        assert foreign.status_code == 404
    accepted = client.post("/api/platform/v1/workflows/workflow-a/acceptance", headers={"Origin": "http://localhost"},
        json={"decision": "accepted", "delivery_digest": detail["delivery_digest"], "expected_revision": detail["revision"], "client_token": "accept"})
    assert accepted.status_code == 202, accepted.get_json()
    assert client.get("/api/platform/v1/workflows/workflow-a").get_json()["workflow"]["delivery_state"] == "delivered"
