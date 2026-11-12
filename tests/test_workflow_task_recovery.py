"""Real Task/Workflow SQLite boundaries, including the two-store crash window."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from concurrent.futures import ThreadPoolExecutor

import pytest

from hub.application.legacy_task_bridge import LegacyTaskBridge
from hub.application.task_service import ApplicationError, TaskHostPolicy, TaskService
from hub.application.workflow_coding_delivery import WorkflowCodingDeliveryService
from hub.application.workflow_dispatch import WorkflowDispatchService
from hub.application.workflow_templates import coding_delivery_definition
from hub.domain.task import DomainError
from hub.domain.workflow_contract import resolve_contract
from hub.domain.workflow_contract_types import AuthorizedInputs, CatalogRef
from hub.domain.workflow_execution import ExecutionBinding
from hub.infrastructure.platform_db import PlatformRepository
from hub.infrastructure.task_repository import SqliteTaskRepository
from hub.infrastructure.workflow_delivery_repository import WorkflowDeliveryRepository
from hub.infrastructure.workflow_repository import WorkflowRepository
from hub.infrastructure.workflow_run_executor import LegacyTaskTarget, RegisteredRunTarget, RunStepExecutor
from hub.platform.artifacts import ArtifactStore
from hub.platform.workflow_source import SourceExporter
from test_workflow_repository import OWNER, WORKSPACE, prepare_database
from test_workflow_source import repo_with_commit


class Clock:
    def __init__(self, now: float = 104.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@dataclass
class TaskDelivery:
    repository: WorkflowRepository
    platform: PlatformRepository
    tasks: SqliteTaskRepository
    task_service: TaskService
    bridge: LegacyTaskBridge
    executor: RunStepExecutor
    binding: ExecutionBinding
    delivery: WorkflowDeliveryRepository
    service: WorkflowCodingDeliveryService
    store: ArtifactStore
    lease: dict[str, Any]
    archive: bytes
    clock: Clock

    def upload(self) -> Any:
        return asyncio.run(self.service.record_task_source("mac-local", self.lease["attempt_id"], self.lease["nonce"], self.archive))


def build_task_delivery(tmp_path: Path, *, source_capture: bool = True, epoch: float = 100.0) -> TaskDelivery:
    repository = prepare_database(tmp_path / "platform.db")
    platform = PlatformRepository(repository.db_path)
    platform.create_conversation(OWNER, "execution-chat", title="Step", workspace_id=WORKSPACE)
    definition = coding_delivery_definition()
    repository.create_or_get(OWNER, WORKSPACE, "workflow-a", client_token="create", definition=definition, now=epoch)
    claim = repository.claim_due(OWNER, worker_id="worker", now=epoch, lease_seconds=60)[0]
    resolved = resolve_contract(definition, AuthorizedInputs(OWNER, WORKSPACE, "develop", CatalogRef("cli", 1), ()))
    ready = repository.freeze_inputs(claim, step_key="develop", generation=1, resolved=resolved, expected_revision=1, now=epoch + 1)
    repository.admit_step(claim, step_key="develop", generation=1, expected_revision=ready.revision,
                          target_key="node-a", capacity=1, reserve_tokens=100, reserve_requests=2, now=epoch + 2)
    clock = Clock(epoch + 4)
    target = RegisteredRunTarget(OWNER, WORKSPACE, "node-a", CatalogRef("cli", 1), CatalogRef("coding-delivery", 1),
        "execution-chat", b"{}", LegacyTaskTarget("mac-local", "codex", "agent-fleet"),
        source_repository_id="repo-a" if source_capture else None)
    executor = RunStepExecutor(repository, target, clock=clock)
    observation = asyncio.run(WorkflowDispatchService(repository, executor, clock=clock).dispatch(
        claim, step_key="develop", generation=1, expected_revision=repository.get(OWNER, "workflow-a").revision))
    tasks = SqliteTaskRepository(tmp_path / "tasks.db")
    tasks.init()
    task_service = TaskService(tasks, SimpleNamespace(read_current=lambda machine: {"reachable": True}),
        SimpleNamespace(emit=lambda event, **kwargs: None), TaskHostPolicy(project_whitelist={"mac-local": ["agent-fleet"]}))
    bridge = LegacyTaskBridge(platform, task_service, tasks, clock=clock)
    bridge.process_once(OWNER)
    lease = tasks.lease_task(machine="mac-local", runner_id="fixture-runner", now=clock())
    store = ArtifactStore(tmp_path / "artifacts", clock=clock)
    delivery = WorkflowDeliveryRepository(repository, tasks)
    service = WorkflowCodingDeliveryService(delivery, store, clock=clock, task_repository=tasks)
    repo, base, head, tree = repo_with_commit(tmp_path)
    archive = tmp_path / "source.tar"
    asyncio.run(SourceExporter(repo).export_archive(repository_id="repo-a", base_commit=base,
                head_commit=head, tree_id=tree, destination=archive))
    return TaskDelivery(repository, platform, tasks, task_service, bridge, executor, observation.binding,
                        delivery, service, store, lease, archive.read_bytes(), clock)


@pytest.fixture
def task_delivery(tmp_path: Path) -> TaskDelivery:
    return build_task_delivery(tmp_path)


def test_workflow_without_source_capture_also_keeps_attempt_after_expiry(tmp_path: Path) -> None:
    fixture = build_task_delivery(tmp_path, source_capture=False)
    assert fixture.lease["source_capture"] is False
    origin = fixture.tasks.source_origin("mac-local", fixture.lease["attempt_id"], fixture.lease["nonce"])
    assert origin["run_id"] == fixture.binding.run_id and origin["repository_id"] is None
    fixture.clock.now += 301
    assert fixture.tasks.expire_leases(now=fixture.clock()) == []
    assert fixture.tasks.get_task(fixture.lease["task_id"])["state"] == "expired"
    assert fixture.tasks.lease_task(machine="mac-local", runner_id="another", now=fixture.clock()) is None


def test_workflow_lease_expiration_cannot_automatically_reexecute_cli(task_delivery: TaskDelivery) -> None:
    fixture = task_delivery
    fixture.clock.now += 301
    assert fixture.tasks.expire_leases(now=fixture.clock()) == []
    task = fixture.tasks.get_task(fixture.lease["task_id"])
    assert task["state"] == "expired" and task["attempt_id"] == fixture.lease["attempt_id"]
    assert fixture.tasks.lease_task(machine="mac-local", runner_id="another-runner", now=fixture.clock()) is None
    fixture.bridge.process_once(OWNER)
    observed = asyncio.run(fixture.executor.inspect(fixture.binding))
    assert observed.state == "unknown" and observed.reason_code == "legacy_stop_unconfirmed"
    with fixture.repository.transaction(write=False) as connection:
        assert connection.execute("SELECT state FROM capability_reservations").fetchone()[0] == "held"


@pytest.mark.parametrize("state", ["succeeded", "failed", "cancelled", "expired"])
def test_task_retry_cannot_bypass_workflow_generation(task_delivery: TaskDelivery, state: str) -> None:
    fixture = task_delivery
    with fixture.tasks._connect() as connection:
        connection.execute("UPDATE tasks SET state=? WHERE task_id=?", (state, fixture.lease["task_id"]))
    with pytest.raises(DomainError) as domain_error:
        fixture.tasks.retry_task(fixture.lease["task_id"], OWNER, now=fixture.clock())
    assert domain_error.value.code == "workflow_control_required"
    assert fixture.tasks.get_task(fixture.lease["task_id"])["state"] == state
    with pytest.raises(ApplicationError) as error:
        fixture.task_service.retry(fixture.lease["task_id"], OWNER)
    assert error.value.code == "workflow_control_required" and error.value.status == 409


@pytest.mark.parametrize("operation,state", [("pause_task", "running"), ("continue_task", "paused")])
def test_task_pause_resume_cannot_restart_workflow_attempt(task_delivery: TaskDelivery, operation: str, state: str) -> None:
    fixture = task_delivery
    with fixture.tasks._connect() as connection:
        connection.execute("UPDATE tasks SET state=? WHERE task_id=?", (state, fixture.lease["task_id"]))
    with pytest.raises(DomainError) as domain_error:
        getattr(fixture.tasks, operation)(fixture.lease["task_id"], OWNER, now=fixture.clock())
    assert domain_error.value.code == "workflow_control_required"
    assert fixture.tasks.get_task(fixture.lease["task_id"])["attempt_id"] == fixture.lease["attempt_id"]


def test_source_receipt_precedes_success_result_and_is_idempotent(task_delivery: TaskDelivery) -> None:
    fixture = task_delivery
    result = dict(attempt_id=fixture.lease["attempt_id"], nonce=fixture.lease["nonce"], exit_code=0,
                  log_summary="", diff_stat="", duration_s=1, now=fixture.clock())
    assert fixture.tasks.complete_task(**result) is None
    receipt = fixture.upload()
    assert fixture.upload() == receipt
    assert fixture.tasks.source_receipt("mac-local", fixture.lease["attempt_id"], fixture.lease["nonce"]) is not None
    assert fixture.tasks.complete_task(**result)["state"] == "succeeded"


def test_task_source_policy_checks_current_dispatch_and_returns_original_deadline(task_delivery: TaskDelivery) -> None:
    fixture = task_delivery
    policy = asyncio.run(fixture.service.task_source_policy("mac-local", fixture.lease["attempt_id"], fixture.lease["nonce"]))
    step = fixture.repository.current_steps(OWNER, "workflow-a")[0]
    assert policy == {"source_capture": True, "source_repository_id": "repo-a",
                      "source_deadline_utc": datetime.fromtimestamp(step.active_deadline, timezone.utc).isoformat()}
    with fixture.repository.transaction() as connection:
        connection.execute("UPDATE workflow_runs SET cancel_requested=1")
    with pytest.raises(Exception) as rejected:
        asyncio.run(fixture.service.task_source_policy("mac-local", fixture.lease["attempt_id"], fixture.lease["nonce"]))
    assert rejected.value.code == "delivery_execution_unavailable"


def test_runner_source_response_uses_output_key_not_receipt_order(task_delivery: TaskDelivery, monkeypatch: pytest.MonkeyPatch) -> None:
    from hub.application.runner_service import RunnerService

    fixture = task_delivery
    receipt = fixture.upload()
    source = next(item.artifact for item in receipt.outputs if item.output_key == "source_bundle")
    async def reordered(*args: Any) -> Any:
        return replace(receipt, outputs=tuple(reversed(receipt.outputs)))
    monkeypatch.setattr(fixture.service, "record_task_source", reordered)
    runner = RunnerService(fixture.tasks, SimpleNamespace(emit=lambda *args, **kwargs: None))
    runner.workflow_delivery = fixture.service
    assert runner.source("mac-local", fixture.lease["attempt_id"], fixture.lease["nonce"], fixture.archive) == {
        "ok": True, "artifact_id": source.artifact_id, "sha256": source.sha256, "size": source.size}


def test_source_store_failure_is_retryable_and_keeps_original_cause(task_delivery: TaskDelivery, monkeypatch: pytest.MonkeyPatch) -> None:
    from hub.application.runner_service import RunnerService
    from hub.application.workflow_coding_delivery import WorkflowCodingDeliveryError

    fixture = task_delivery
    root = OSError("fixture disk unavailable")
    async def unavailable(*args: Any) -> Any:
        raise WorkflowCodingDeliveryError("delivery_io_failed") from root
    monkeypatch.setattr(fixture.service, "record_task_source", unavailable)
    runner = RunnerService(fixture.tasks, SimpleNamespace(emit=lambda *args, **kwargs: None))
    runner.workflow_delivery = fixture.service
    with pytest.raises(ApplicationError) as rejected:
        runner.source("mac-local", fixture.lease["attempt_id"], fixture.lease["nonce"], fixture.archive)
    assert rejected.value.status == 503 and rejected.value.__cause__.__cause__ is root


def test_concurrent_identical_source_uploads_return_one_delivery(task_delivery: TaskDelivery) -> None:
    fixture = task_delivery
    with ThreadPoolExecutor(max_workers=2) as workers:
        first, second = tuple(workers.map(lambda _: fixture.upload(), range(2)))
    assert first == second
    with fixture.repository.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM workflow_source_uploads").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM workflow_operations").fetchone()[0] == 1


def test_source_publication_cannot_replace_a_competing_directory(task_delivery: TaskDelivery, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = task_delivery
    target = fixture.store.root / ("source-attempt-" + fixture.lease["attempt_id"])
    original_exists = Path.exists
    inode = None
    def race(path: Path) -> bool:
        nonlocal inode
        if path == target / "content" and inode is None:
            target.mkdir()
            inode = target.stat().st_ino
            return False
        return original_exists(path)
    monkeypatch.setattr(Path, "exists", race)
    with pytest.raises(Exception) as rejected:
        fixture.upload()
    assert rejected.value.code == "artifact_id_conflict"
    assert target.stat().st_ino == inode and tuple(target.iterdir()) == ()


def test_same_attempt_cannot_upload_a_different_source_packet(task_delivery: TaskDelivery) -> None:
    fixture = task_delivery
    receipt = fixture.upload()
    with pytest.raises(Exception) as rejected:
        asyncio.run(fixture.service.record_task_source("mac-local", fixture.lease["attempt_id"], fixture.lease["nonce"],
                                                     fixture.archive + bytes(10240)))
    assert rejected.value.code == "source_upload_conflict"
    assert fixture.upload() == receipt


@pytest.mark.parametrize("revoked", ["task", "workflow", "deadline"])
def test_source_cannot_publish_when_revoked_before_final_commit(task_delivery: TaskDelivery, monkeypatch: pytest.MonkeyPatch,
                                                              revoked: str) -> None:
    fixture = task_delivery
    original = fixture.delivery.commit
    def revoke(*args: Any, **kwargs: Any) -> Any:
        if revoked == "task":
            fixture.tasks.cancel_task(fixture.lease["task_id"], OWNER, now=fixture.clock())
        elif revoked == "workflow":
            with fixture.repository.transaction() as connection:
                connection.execute("UPDATE workflow_runs SET cancel_requested=1")
        else:
            fixture.clock.now += 3601
            kwargs["now"] = fixture.clock()
        return original(*args, **kwargs)
    monkeypatch.setattr(fixture.delivery, "commit", revoke)
    with pytest.raises(Exception) as rejected:
        fixture.upload()
    assert rejected.value.code in {"lease_mismatch", "delivery_execution_unavailable", "source_active_deadline_exceeded"}
    assert fixture.tasks.source_receipt("mac-local", fixture.lease["attempt_id"], fixture.lease["nonce"]) is None
    with fixture.repository.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM workflow_operations").fetchone()[0] == 0


@pytest.mark.parametrize("cancel_after_commit", [False, True])
def test_source_retry_repairs_task_receipt_after_workflow_commit(task_delivery: TaskDelivery, monkeypatch: pytest.MonkeyPatch,
                                                               cancel_after_commit: bool) -> None:
    fixture = task_delivery

    class ProcessStopped(BaseException):
        """A process exits after the Workflow transaction, before Task commit."""

    original = fixture.tasks.record_source_receipt
    def crash(**kwargs: Any) -> None:
        raise ProcessStopped()

    monkeypatch.setattr(fixture.tasks, "record_source_receipt", crash)
    with pytest.raises(ProcessStopped):
        fixture.upload()
    assert fixture.tasks.source_receipt("mac-local", fixture.lease["attempt_id"], fixture.lease["nonce"]) is None
    before = fixture.repository.get(OWNER, "workflow-a")
    artifacts = set(fixture.store.root.iterdir())
    if cancel_after_commit:
        fixture.tasks.cancel_task(fixture.lease["task_id"], OWNER, now=fixture.clock())
    monkeypatch.setattr(fixture.tasks, "record_source_receipt", original)
    recovered = fixture.upload()
    assert fixture.tasks.source_receipt("mac-local", fixture.lease["attempt_id"], fixture.lease["nonce"]) is not None
    assert fixture.upload() == recovered
    assert fixture.repository.get(OWNER, "workflow-a") == before
    assert set(fixture.store.root.iterdir()) == artifacts
    result = fixture.tasks.complete_task(attempt_id=fixture.lease["attempt_id"], nonce=fixture.lease["nonce"], exit_code=0,
        log_summary="", diff_stat="", duration_s=1, now=fixture.clock())
    assert result is None if cancel_after_commit else result["state"] == "succeeded"
    with fixture.repository.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM workflow_operations").fetchone()[0] == 1
