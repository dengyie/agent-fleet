"""Real Run leases and pytest processes; DirectoryBackend is not a sandbox."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hub.application.run_event_service import RunEventService
from hub.application.run_worker_service import LocalRunWorkerService
from hub.application.workflow_dispatch import WorkflowDispatchService
from hub.domain.workflow_contract import resolve_contract
from hub.domain.workflow_contract_types import AuthorizedInputs, CatalogRef, InputBinding
from hub.domain.workflow_delivery import DeliveryRunLease
from hub.domain.workflow_errors import WorkflowValidationError
from hub.infrastructure.platform_db import PlatformRepository
from hub.infrastructure.workflow_repository import WorkflowRepositoryError
from hub.infrastructure.workflow_run_executor import RegisteredRunTarget, RunStepExecutor
from fleet_core.backends.directory import DirectoryBackend
from hub.platform.workflow_validators import FixedTestRunner
from test_workflow_coding_delivery import DeliveryFixture, REVIEWER, review
from test_workflow_repository import OWNER, WORKSPACE
from test_workflow_source import git


class ExecutionFixture(DeliveryFixture):
    def __init__(self, path: Path, *, content: str | None = None, review_verdict: str = "passed") -> None:
        super().__init__(path)
        self.now = 110.0
        self.service.clock = self.clock
        self.platform = PlatformRepository(self.repository.db_path)
        self.platform.upsert_workspace(OWNER, {"workspace_id": WORKSPACE, "root_path": str(path / "user-workspace")})
        self.platform.create_conversation(OWNER, "test-execution", title="Fixed tests", workspace_id=WORKSPACE)
        if content is not None:
            repo = path / "repo"
            (repo / "test_delivery.py").write_text(content, encoding="utf-8")
            git(repo, "add", "test_delivery.py")
            git(repo, "commit", "-qm", "test case")
            self.exported = asyncio.run(self.service.export_source(repo, OWNER, WORKSPACE, repository_id="repo-a",
                base_commit=self.exported.revision.base_commit, head_commit=git(repo, "rev-parse", "HEAD"), tree_id=git(repo, "rev-parse", "HEAD^{tree}")))
        develop = self.start("develop")
        self.source_receipt = asyncio.run(self.service.record_source(develop, self.exported, executor_id="developer-1"))
        self.finish(develop)
        reviewer = self.start("review")
        asyncio.run(self.service.record_review(reviewer, review(self.exported.revision,
            subject=self.source_receipt.subject_digest, verdict=review_verdict), executor=REVIEWER))
        self.finish(reviewer)
        self.target = RegisteredRunTarget(OWNER, WORKSPACE, "fixed-test-node", CatalogRef("fixed-tests", 1),
            CatalogRef("coding-delivery", 1), "test-execution", b"{}", test_recipe=CatalogRef("coding-delivery-tests", 1))
        self.executor = RunStepExecutor(self.repository, self.target, clock=self.clock)

    def clock(self) -> float:
        return self.now

    def ready(self) -> None:
        definition_step = self.definition.steps[2]
        resolved = resolve_contract(self.definition, AuthorizedInputs(OWNER, WORKSPACE, "test", self.target.adapter_ref,
            (InputBinding("source", OWNER, WORKSPACE, self.exported.revision, definition_step.contract.input_refs[0].reference),)))
        revision = self.repository.get(OWNER, "workflow-a").revision
        self.repository.freeze_inputs(self.claim, step_key="test", generation=1, resolved=resolved, expected_revision=revision, now=self.clock())
        revision = self.repository.get(OWNER, "workflow-a").revision
        self.repository.admit_step(self.claim, step_key="test", generation=1, expected_revision=revision,
            target_key=self.target.target_key, capacity=1, reserve_tokens=0, reserve_requests=0, now=self.clock())

    def dispatch(self) -> Any:
        self.ready()
        return asyncio.run(WorkflowDispatchService(self.repository, self.executor, clock=self.clock).dispatch(
            self.claim, step_key="test", generation=1, expected_revision=self.repository.get(OWNER, "workflow-a").revision))

    def worker(self, *, runner: FixedTestRunner | None = None, enabled: bool = True) -> LocalRunWorkerService:
        from hub.application.workflow_test_execution import WorkflowTestExecutionService

        service = WorkflowTestExecutionService(self.delivery, self.service, clock=self.clock,
            runner=runner or FixedTestRunner(backend_factory=DirectoryBackend)) if enabled else None
        return LocalRunWorkerService(self.platform, RunEventService(self.platform, clock=self.clock), worker_id="test-worker",
            clock=self.clock, artifact_store=self.store, workflow_test_executor=service,
            provider_factory=lambda profile: pytest.fail("fixed tests must never call a model"))

    def lease(self, claim: dict[str, Any]) -> DeliveryRunLease:
        return DeliveryRunLease(OWNER, claim["run_id"], claim["lease_id"], claim["lease_owner"], claim["attempt"])

    def outputs(self) -> dict[str, dict[str, Any]]:
        with self.repository.transaction(write=False) as connection:
            rows = connection.execute("SELECT output_key,artifact_id FROM workflow_artifact_refs "
                "WHERE workflow_id='workflow-a' AND step_key='test' AND output_key LIKE 'output:%' LIMIT 3").fetchall()
        return {row["output_key"][7:]: self.store.get(OWNER, WORKSPACE, row["artifact_id"]) for row in rows}

    def test_evidence(self) -> list[Any]:
        with self.repository.transaction(write=False) as connection:
            return connection.execute("SELECT result,subject_digest,source FROM workflow_evidence "
                "WHERE workflow_id='workflow-a' AND step_key='test' LIMIT 3").fetchall()


@pytest.mark.parametrize("passed", [True, False])
def test_fixed_test_run_uses_real_pytest_and_publishes_scoped_evidence(tmp_path: Path, passed: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLEET_TEST_SECRET", "must-never-reach-child")
    content = "import os\ndef test_delivery():\n    assert 'FLEET_TEST_SECRET' not in os.environ\n    assert " + str(passed) + "\n"
    fixture = ExecutionFixture(tmp_path, content=content)
    observation = fixture.dispatch()
    result = fixture.worker().run_once(OWNER)
    assert result["state"] == "succeeded"
    assert fixture.worker().run_once(OWNER) is None
    assert asyncio.run(fixture.executor.inspect(observation.binding)).state == "succeeded"
    outputs = fixture.outputs()
    assert set(outputs) == {"test_report", "junit"}
    report = json.loads(fixture.store.read(OWNER, WORKSPACE, outputs["test_report"]["artifact_id"]))
    assert report["passed"] is passed and report["exit_code"] == (0 if passed else 1)
    assert report["subject_digest"] == fixture.source_receipt.subject_digest
    assert report["recipe"] == {"id": "coding-delivery-tests", "revision": 1}
    assert report["environment"]["scope"] == "adapter_host"
    assert "DirectoryBackend" in report["environment"]["backend"]
    assert report["environment"]["launcher_sha256"] is None
    assert fixture.test_evidence()[0]["result"] == ("passed" if passed else "failed")
    assert fixture.test_evidence()[0]["subject_digest"] == fixture.source_receipt.subject_digest
    assert not (tmp_path / "user-workspace" / ".fleet-workflow-results.xml").exists()
    events = fixture.platform.list_run_events(OWNER, observation.binding.run_id)["events"]
    assert [item["kind"] for item in events] == ["run_started", "workflow_test_finished"]


def test_test_execution_requires_configured_sandbox_and_never_falls_back(tmp_path: Path) -> None:
    fixture = ExecutionFixture(tmp_path)
    fixture.dispatch()
    result = fixture.worker(runner=FixedTestRunner()).run_once(OWNER)
    assert result["state"] == "failed" and result["result_text"] == "sandbox_unavailable"
    assert fixture.outputs() == {} and fixture.test_evidence() == []


def test_missing_workflow_test_adapter_does_not_fall_through_to_native_model(tmp_path: Path) -> None:
    fixture = ExecutionFixture(tmp_path)
    fixture.dispatch()
    result = fixture.worker(enabled=False).run_once(OWNER)
    assert result["state"] == "failed" and result["result_text"] == "workflow_test_unavailable"
    assert fixture.outputs() == {}


def test_run_created_before_workflow_binding_is_safely_requeued(tmp_path: Path) -> None:
    fixture = ExecutionFixture(tmp_path)
    fixture.ready()
    record = fixture.repository.begin_dispatch(fixture.claim, step_key="test", generation=1,
        expected_revision=fixture.repository.get(OWNER, "workflow-a").revision, now=fixture.clock())
    binding = asyncio.run(fixture.executor.create_or_lookup(record.token, record.payload_hash, record.resolved))
    result = fixture.worker().run_once(OWNER)
    assert result["state"] == "queued" and result["attempt"] == 0
    assert fixture.platform.list_run_events(OWNER, binding.run_id)["events"] == []
    fixture.repository.bind_dispatch(fixture.claim, record, binding, now=fixture.clock())
    assert fixture.worker().run_once(OWNER)["state"] == "succeeded"


@pytest.mark.parametrize("field,value", [("owner_id", "other@example.test"), ("lease_id", "wrong"), ("worker_id", "other-worker"), ("attempt", 2)])
def test_delivery_run_lookup_rejects_another_lease_identity(tmp_path: Path, field: str, value: object) -> None:
    fixture = ExecutionFixture(tmp_path)
    observation = fixture.dispatch()
    claim = fixture.platform.claim_run(OWNER, worker_id="test-worker", now=fixture.clock())
    with pytest.raises(WorkflowRepositoryError, match="run_lease_mismatch"):
        fixture.delivery.target_for_run(replace(fixture.lease(claim), **{field: value}), operation="test", now=fixture.clock())
    target = fixture.delivery.target_for_run(fixture.lease(claim), operation="test", now=fixture.clock())
    assert target.binding == observation.binding and target.run_lease == fixture.lease(claim)


def test_unbound_native_run_cannot_invoke_fixed_recipe(tmp_path: Path) -> None:
    fixture = ExecutionFixture(tmp_path)
    fixture.dispatch()
    claim = fixture.platform.claim_run(OWNER, worker_id="test-worker", now=fixture.clock())
    with fixture.repository.transaction() as connection:
        connection.execute("UPDATE workflow_dispatches SET binding_run_id=NULL WHERE step_key='test'")
    with pytest.raises(WorkflowRepositoryError, match="binding_conflict"):
        fixture.delivery.target_for_run(fixture.lease(claim), operation="test", now=fixture.clock())


@pytest.mark.parametrize("mutation", ["lease", "cancel", "generation", "frozen", "workspace"])
def test_publication_rechecks_live_execution_inside_transaction(tmp_path: Path, mutation: str, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = ExecutionFixture(tmp_path)
    fixture.dispatch()
    original = fixture.service._publish

    def publish(*args: Any, **kwargs: Any) -> Any:
        artifact = original(*args, **kwargs)
        if args[2] == "junit.xml":
            with fixture.repository.transaction() as connection:
                statements = {
                    "lease": "UPDATE runs SET lease_owner='another-worker' WHERE run_id LIKE 'wf-run-%'",
                    "cancel": "UPDATE runs SET cancel_requested=1 WHERE run_id LIKE 'wf-run-%'",
                    "generation": "UPDATE workflow_steps SET is_current=0 WHERE step_key='test'",
                    "frozen": "UPDATE workflow_schema_meta SET admission_mode='frozen'",
                    "workspace": "UPDATE workspaces SET enabled=0 WHERE owner_id='owner@example.test'",
                }
                connection.execute(statements[mutation])
        return artifact

    monkeypatch.setattr(fixture.service, "_publish", publish)
    result = fixture.worker().run_once(OWNER)
    assert result["state"] in {"failed", "unknown", "cancelled", "lease_lost"}
    assert fixture.outputs() == {} and fixture.test_evidence() == []


def test_revision_conflict_retries_publication_without_rerunning_pytest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = ExecutionFixture(tmp_path)
    fixture.dispatch()
    calls: list[str] = []
    original = fixture.delivery.commit

    def conflict_once(*args: Any, **kwargs: Any) -> Any:
        calls.append(args[1])
        if len(calls) == 1:
            with fixture.repository.transaction() as connection:
                connection.execute("UPDATE workflow_runs SET revision=revision+1 WHERE workflow_id='workflow-a'")
        return original(*args, **kwargs)

    monkeypatch.setattr(fixture.delivery, "commit", conflict_once)
    assert fixture.worker().run_once(OWNER)["state"] == "succeeded"
    assert calls == ["test", "test"] and len(fixture.test_evidence()) == 1


def test_lost_delivery_response_recovers_receipt_without_duplicate_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = ExecutionFixture(tmp_path)
    fixture.dispatch()
    original = fixture.service.record_test
    calls: list[tuple[str, ...]] = []

    async def lost_once(*args: Any, **kwargs: Any) -> Any:
        from hub.application.workflow_coding_delivery import WorkflowCodingDeliveryError

        receipt = await original(*args, **kwargs)
        calls.append(tuple(sorted(item.name for item in fixture.store.root.iterdir())))
        if len(calls) == 1:
            raise WorkflowCodingDeliveryError("delivery_io_failed") from OSError("response lost")
        return receipt

    monkeypatch.setattr(fixture.service, "record_test", lost_once)
    assert fixture.worker().run_once(OWNER)["state"] == "succeeded"
    assert len(calls) == 2 and calls[0] == calls[1]
    assert len(fixture.test_evidence()) == 1


@pytest.mark.parametrize("mutation", ["run_cancel", "workflow_cancel", "workspace", "frozen"])
@pytest.mark.parametrize("lost_response", [False, True])
def test_committed_test_result_survives_late_revocation(tmp_path: Path, mutation: str, lost_response: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    from hub.application.workflow_coding_delivery import WorkflowCodingDeliveryError

    fixture = ExecutionFixture(tmp_path)
    observation = fixture.dispatch()
    original = fixture.service.record_test
    artifacts: list[tuple[str, ...]] = []

    async def completed(*args: Any, **kwargs: Any) -> Any:
        receipt = await original(*args, **kwargs)
        artifacts.append(tuple(sorted(item.name for item in fixture.store.root.iterdir())))
        if len(artifacts) != 1:
            return receipt
        if mutation == "run_cancel":
            fixture.platform.cancel_run(OWNER, observation.binding.run_id, now=fixture.clock())
        else:
            with fixture.repository.transaction() as connection:
                connection.execute({
                    "workflow_cancel": "UPDATE workflow_runs SET cancel_requested=1 WHERE workflow_id='workflow-a'",
                    "workspace": "UPDATE workspaces SET enabled=0 WHERE owner_id='owner@example.test'",
                    "frozen": "UPDATE workflow_schema_meta SET admission_mode='frozen'",
                }[mutation])
        if lost_response:
            raise WorkflowCodingDeliveryError("delivery_io_failed") from OSError("response lost after commit")
        return receipt

    monkeypatch.setattr(fixture.service, "record_test", completed)
    result = fixture.worker().run_once(OWNER)
    assert result["state"] == "succeeded" and result["result_text"] == "fixed_test_completed"
    assert len(artifacts) == (2 if lost_response else 1) and len(set(artifacts)) == 1
    assert len(fixture.test_evidence()) == 1 and fixture.test_evidence()[0]["result"] == "passed"


@pytest.mark.parametrize("mutation", ["unpublished", "token", "payload", "binding", "recipe"])
def test_cancellation_requires_exact_persisted_completion_to_keep_success(tmp_path: Path, mutation: str) -> None:
    fixture = ExecutionFixture(tmp_path)
    observation = fixture.dispatch()
    claim = fixture.platform.claim_run(OWNER, worker_id="test-worker", now=fixture.clock())
    if mutation != "unpublished":
        worker = fixture.worker()
        result = asyncio.run(worker.workflow_test_executor.execute(fixture.lease(claim), should_cancel=lambda: False,
            on_started=lambda: worker._event(claim, "run_started", {"run_id": claim["run_id"]})))
        assert result.state == "succeeded" and result.receipt is not None
        with fixture.repository.transaction() as connection:
            config = fixture.platform.get_run(OWNER, observation.binding.run_id)["config_snapshot"]
            if mutation == "binding":
                connection.execute("UPDATE workflow_dispatches SET binding_id='another-run' WHERE step_key='test'")
            else:
                if mutation == "recipe":
                    config["workflow_test"]["revision"] = True
                else:
                    config["workflow_dispatch"]["token" if mutation == "token" else "payload_hash"] = "0" * 64
                connection.execute("UPDATE runs SET config_snapshot=? WHERE run_id=?", (json.dumps(config), observation.binding.run_id))
    fixture.platform.cancel_run(OWNER, observation.binding.run_id, now=fixture.clock())
    result = fixture.platform.finish_run(OWNER, observation.binding.run_id, lease_id=claim["lease_id"], worker_id="test-worker",
        state="succeeded", now=fixture.clock())
    assert result["state"] == "cancelled"


def test_completion_transaction_failure_preserves_cause_and_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sqlite3
    from hub.application.task_service import ApplicationError
    from hub.infrastructure.platform_db import PlatformRepositoryError

    fixture = ExecutionFixture(tmp_path)
    observation = fixture.dispatch()
    claim = fixture.platform.claim_run(OWNER, worker_id="test-worker", now=fixture.clock())
    fixture.platform.cancel_run(OWNER, observation.binding.run_id, now=fixture.clock())
    failure = sqlite3.OperationalError("receipt store unavailable")

    def unavailable(*args: Any) -> bool:
        raise failure

    monkeypatch.setattr(fixture.platform, "_workflow_test_completed", unavailable)
    with pytest.raises(ApplicationError) as error:
        RunEventService(fixture.platform, clock=fixture.clock).state(OWNER, observation.binding.run_id, "succeeded",
            lease_id=claim["lease_id"], worker_id="test-worker")
    assert isinstance(error.value.__cause__, PlatformRepositoryError)
    assert error.value.__cause__.__cause__ is failure
    with fixture.repository.transaction(write=False) as connection:
        run = connection.execute("SELECT state,lease_id FROM runs WHERE owner_id=? AND run_id=?",
            (OWNER, observation.binding.run_id)).fetchone()
    assert run["state"] == "cancelling" and run["lease_id"] == claim["lease_id"]


def test_started_test_run_is_unknown_after_crash_and_never_replayed(tmp_path: Path) -> None:
    fixture = ExecutionFixture(tmp_path)
    observation = fixture.dispatch()
    executions: list[str] = []

    class Crash(BaseException):
        """A worker disappears after its actual process has completed."""

    class InterruptedRunner(FixedTestRunner):
        async def run(self, *args: Any, **kwargs: Any) -> Any:
            result = await super().run(*args, **kwargs)
            executions.append(result.report_hash)
            raise Crash()

    with pytest.raises(Crash):
        fixture.worker(runner=InterruptedRunner(backend_factory=DirectoryBackend)).run_once(OWNER)
    fixture.now = 200
    recovered = fixture.worker().run_once(OWNER)
    assert recovered["run_id"] == observation.binding.run_id and recovered["state"] == "unknown"
    assert len(executions) == 1 and fixture.outputs() == {}
    assert fixture.worker().run_once(OWNER) is None


@pytest.mark.parametrize("reason", ["cancel", "deadline", "workflow_cancel", "workspace", "frozen", "generation", "binding", "resolved", "step_state", "lease"])
def test_cancellation_reaps_actual_test_process_before_worker_returns(tmp_path: Path, reason: str) -> None:
    fixture = ExecutionFixture(tmp_path, content="import os, time\nfrom pathlib import Path\ndef test_wait():\n    Path('started').write_text(str(os.getpid()))\n    time.sleep(30)\n")
    observation = fixture.dispatch()
    if reason == "deadline":
        with fixture.repository.transaction() as connection:
            connection.execute("UPDATE workflow_steps SET active_deadline=? WHERE step_key='test'", (fixture.clock() + 1,))
    ready = threading.Event()
    checkout: list[Path] = []

    def backend(path: Path) -> DirectoryBackend:
        checkout.append(path)
        ready.set()
        return DirectoryBackend(path)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(fixture.worker(runner=FixedTestRunner(backend_factory=backend)).run_once, OWNER)
        assert ready.wait(5)
        marker = checkout[0] / "started"
        for _ in range(100):
            if marker.exists():
                break
            threading.Event().wait(.02)
        assert marker.exists()
        pid = int(marker.read_text())
        if reason == "cancel":
            fixture.platform.cancel_run(OWNER, observation.binding.run_id, now=fixture.clock())
        elif reason == "deadline":
            fixture.now += 1
        else:
            with fixture.repository.transaction() as connection:
                connection.execute({
                    "workflow_cancel": "UPDATE workflow_runs SET cancel_requested=1 WHERE workflow_id='workflow-a'",
                    "workspace": "UPDATE workspaces SET enabled=0 WHERE owner_id='owner@example.test'",
                    "frozen": "UPDATE workflow_schema_meta SET admission_mode='frozen'",
                    "generation": "UPDATE workflow_steps SET is_current=0 WHERE step_key='test'",
                    "binding": "UPDATE workflow_dispatches SET binding_id='another-run' WHERE step_key='test'",
                    "resolved": "UPDATE workflow_steps SET resolved_digest='different' WHERE step_key='test'",
                    "step_state": "UPDATE workflow_steps SET state='paused' WHERE step_key='test'",
                    "lease": "UPDATE runs SET lease_owner='another-worker' WHERE run_id LIKE 'wf-run-%'",
                }[reason])
        result = future.result(timeout=5)
        expected_state = "lease_lost" if reason == "lease" else "failed"
        if reason in ("cancel", "workflow_cancel"):
            expected_state = "cancelled"
        assert result["state"] == expected_state
        expected_error = {"deadline": "test_active_deadline_exceeded", "workspace": "reference_forbidden",
            "frozen": "admission_frozen", "generation": "generation_conflict", "binding": "binding_conflict",
            "resolved": "binding_conflict", "step_state": "delivery_execution_unavailable"}.get(reason)
        if expected_error is not None:
            assert result["result_text"] == expected_error
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert fixture.outputs() == {} and fixture.test_evidence() == []
    assert not any(thread.name.startswith("run-lease-wf-run-") for thread in threading.enumerate())


def test_workspace_revocation_during_unpack_prevents_run_started(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import hub.application.workflow_test_execution as execution

    fixture = ExecutionFixture(tmp_path)
    observation = fixture.dispatch()
    original = execution.extract_source_bundle

    def revoked_after_extract(*args: Any, **kwargs: Any) -> Any:
        revision = original(*args, **kwargs)
        with fixture.repository.transaction() as connection:
            connection.execute("UPDATE workspaces SET enabled=0 WHERE owner_id=? AND workspace_id=?", (OWNER, WORKSPACE))
        return revision

    monkeypatch.setattr(execution, "extract_source_bundle", revoked_after_extract)
    result = fixture.worker().run_once(OWNER)
    assert result["state"] == "failed" and result["result_text"] == "reference_forbidden"
    assert "run_started" not in {item["kind"] for item in fixture.platform.list_run_events(OWNER, observation.binding.run_id)["events"]}
    assert fixture.outputs() == {} and fixture.test_evidence() == []


def test_revision_update_during_process_does_not_revoke_same_binding(tmp_path: Path) -> None:
    fixture = ExecutionFixture(tmp_path)
    fixture.dispatch()
    processes: list[Path] = []

    def backend(path: Path) -> DirectoryBackend:
        processes.append(path)
        with fixture.repository.transaction() as connection:
            connection.execute("UPDATE workflow_runs SET revision=revision+1 WHERE workflow_id='workflow-a'")
        return DirectoryBackend(path)

    assert fixture.worker(runner=FixedTestRunner(backend_factory=backend)).run_once(OWNER)["state"] == "succeeded"
    assert len(processes) == 1 and len(fixture.test_evidence()) == 1


def test_runtime_authorization_read_failure_preserves_cause_and_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hub.application.workflow_test_execution import WorkflowTestExecutionError, WorkflowTestExecutionService

    fixture = ExecutionFixture(tmp_path)
    fixture.dispatch()
    claim = fixture.platform.claim_run(OWNER, worker_id="test-worker", now=fixture.clock())
    failure = WorkflowRepositoryError("workflow_store")
    entered = False

    def unavailable(*args: Any, **kwargs: Any) -> bool:
        raise failure

    def on_started() -> None:
        nonlocal entered
        entered = True

    monkeypatch.setattr(fixture.delivery, "check_run", unavailable)
    service = WorkflowTestExecutionService(fixture.delivery, fixture.service, clock=fixture.clock)
    with pytest.raises(WorkflowTestExecutionError) as error:
        asyncio.run(service.execute(fixture.lease(claim), should_cancel=lambda: False, on_started=on_started))
    assert error.value.code == "workflow_store" and error.value.uncertain is True
    assert error.value.__cause__ is failure and not entered
    assert fixture.outputs() == {} and fixture.test_evidence() == []


def test_stale_review_evidence_blocks_test_execution_before_process_start(tmp_path: Path) -> None:
    fixture = ExecutionFixture(tmp_path)
    fixture.dispatch()
    with fixture.repository.transaction() as connection:
        connection.execute("UPDATE workflow_evidence SET subject_digest=? WHERE step_key='review'", ("b" * 64,))
    assert fixture.worker().run_once(OWNER)["state"] == "failed"
    assert fixture.outputs() == {} and fixture.test_evidence() == []


@pytest.mark.parametrize("revision", [True, 1.0])
def test_fixed_recipe_revision_requires_exact_integer(tmp_path: Path, revision: object) -> None:
    fixture = ExecutionFixture(tmp_path)
    with pytest.raises(WorkflowValidationError, match="invalid_test_recipe"):
        replace(fixture.target, test_recipe=CatalogRef("coding-delivery-tests", revision))


def test_persisted_marker_cannot_coerce_recipe_revision(tmp_path: Path) -> None:
    fixture = ExecutionFixture(tmp_path)
    observation = fixture.dispatch()
    run = fixture.platform.get_run(OWNER, observation.binding.run_id)
    config = run["config_snapshot"]
    config["workflow_test"]["revision"] = True
    with fixture.repository.transaction() as connection:
        connection.execute("UPDATE runs SET config_snapshot=? WHERE run_id=?", (json.dumps(config), observation.binding.run_id))
    assert fixture.worker().run_once(OWNER)["state"] == "failed"
    assert fixture.outputs() == {}


def test_expired_step_deadline_prevents_starting_test_process(tmp_path: Path) -> None:
    fixture = ExecutionFixture(tmp_path)
    observation = fixture.dispatch()
    with fixture.repository.transaction() as connection:
        connection.execute("UPDATE workflow_steps SET active_deadline=? WHERE step_key='test'", (fixture.clock(),))
    assert fixture.worker().run_once(OWNER)["state"] == "failed"
    assert "run_started" not in {item["kind"] for item in fixture.platform.list_run_events(OWNER, observation.binding.run_id)["events"]}
    assert fixture.outputs() == {}


def test_corrupt_review_report_blocks_execution_even_when_verdict_is_passed(tmp_path: Path) -> None:
    fixture = ExecutionFixture(tmp_path)
    observation = fixture.dispatch()
    with fixture.repository.transaction(write=False) as connection:
        artifact = connection.execute("SELECT report_artifact_id FROM workflow_evidence WHERE step_key='review'").fetchone()[0]
    (fixture.store.root / artifact / "content").write_bytes(b"corrupted report")
    assert fixture.worker().run_once(OWNER)["state"] == "failed"
    assert "run_started" not in {item["kind"] for item in fixture.platform.list_run_events(OWNER, observation.binding.run_id)["events"]}
    assert fixture.outputs() == {}


def test_publication_conflicts_are_bounded_without_reexecuting_test_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = ExecutionFixture(tmp_path)
    fixture.dispatch()
    attempts: list[str] = []
    processes: list[Path] = []

    def conflict(*args: Any, **kwargs: Any) -> Any:
        attempts.append(args[1])
        raise WorkflowRepositoryError("revision_conflict")

    def backend(path: Path) -> DirectoryBackend:
        processes.append(path)
        return DirectoryBackend(path)

    monkeypatch.setattr(fixture.delivery, "commit", conflict)
    result = fixture.worker(runner=FixedTestRunner(backend_factory=backend)).run_once(OWNER)
    assert result["state"] == "unknown" and result["result_text"] == "test_publication_unconfirmed"
    assert attempts == ["test"] * 3 and len(processes) == 1
    assert fixture.outputs() == {} and fixture.test_evidence() == []


@pytest.mark.parametrize("closed_pipes", [True, False])
def test_owned_process_cancels_while_silent_or_after_closing_pipes(tmp_path: Path, closed_pipes: bool) -> None:
    from fleet_core.process.bounded_process import ProcessCancelled, run_bounded

    marker = tmp_path / "pid"
    script = tmp_path / "child.py"
    script.write_text("from pathlib import Path\nimport os, time\nPath('pid').write_text(str(os.getpid()))\n" +
        ("os.close(1)\nos.close(2)\n" if closed_pipes else "") + "time.sleep(30)\n", encoding="utf-8")
    with pytest.raises(ProcessCancelled):
        run_bounded((sys.executable, str(script)), cwd=tmp_path, timeout_s=10, stdout_limit=1, should_cancel=marker.exists)
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


def test_callback_lease_failure_reaps_process_and_preserves_original_error(tmp_path: Path) -> None:
    from fleet_core.process.bounded_process import run_bounded

    marker = tmp_path / "pid"
    script = tmp_path / "child.py"
    script.write_text("from pathlib import Path\nimport os, time\nPath('pid').write_text(str(os.getpid()))\ntime.sleep(30)\n", encoding="utf-8")
    failure = RuntimeError("lost lease")

    def cancelled() -> bool:
        if marker.exists():
            raise failure
        return False

    with pytest.raises(RuntimeError) as caught:
        run_bounded((sys.executable, str(script)), cwd=tmp_path, timeout_s=10, stdout_limit=1, should_cancel=cancelled)
    assert caught.value is failure
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


def test_run_worker_workflow_wiring_remains_explicit_and_sandboxed(tmp_path: Path) -> None:
    from hub.bootstrap import create_app
    from hub.config import FleetConfig

    app = create_app(FleetConfig.from_root(tmp_path, platform_enabled=True, platform_worker_enabled=True,
        platform_workflows_enabled=True, dev_operator=OWNER, serve_frontend=False))
    services = app.extensions["fleet"]["services"]
    worker = services["platform_worker"]
    assert worker.workflow_test_executor is services["workflow_test_executor"]
    backend = worker.workflow_test_executor.runner.backend_factory(tmp_path / "isolated")
    assert backend.launcher == ()
    assert backend.execute("test", [sys.executable, "-m", "pytest"]).error_code == "sandbox_unavailable"
    assert not any(thread.name.startswith("run-lease-") for thread in threading.enumerate())
