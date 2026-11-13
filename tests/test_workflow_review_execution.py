"""Real command/SQLite review protocol; the fixture launcher is not a sandbox."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hub.application.run_event_service import RunEventService
from hub.application.run_worker_service import LocalRunWorkerService
from hub.application.workflow_dispatch import WorkflowDispatchService
from hub.application.workflow_review_execution import WorkflowReviewExecutionService
from hub.domain.workflow_contract import resolve_contract
from hub.domain.workflow_contract_types import AuthorizedInputs, CatalogRef, InputBinding
from hub.domain.workflow_delivery import DeliveryRunLease
from hub.domain.workflow_errors import WorkflowValidationError
from hub.domain.workflow_review import ReviewIdentity
from hub.infrastructure.platform_db import PlatformRepository
from hub.infrastructure.workflow_repository import WorkflowRepositoryError
from hub.infrastructure.workflow_run_executor import RegisteredRunTarget, RunStepExecutor
from hub.platform.workflow_review import CommandReviewAdapter
from test_workflow_coding_delivery import DeliveryFixture
from test_workflow_repository import OWNER, WORKSPACE


class ReviewFixture(DeliveryFixture):
    def __init__(self, path: Path, *, verdict: str = "passed", program: str | None = None) -> None:
        super().__init__(path)
        self.now = 110.0
        self.service.clock = self.clock
        self.platform = PlatformRepository(self.repository.db_path)
        self.platform.upsert_workspace(OWNER, {"workspace_id": WORKSPACE, "root_path": str(path / "user-workspace")})
        self.platform.create_conversation(OWNER, "review-execution", title="Independent review", workspace_id=WORKSPACE)
        develop = self.start("develop")
        self.source_receipt = asyncio.run(self.service.record_source(develop, self.exported, executor_id="developer-1"))
        self.finish(develop)
        script = path / "reviewer.py"
        script.write_text(program or (
            "import json, os\nfrom pathlib import Path\n"
            "request = json.loads(Path(os.environ['FLEET_REVIEW_REQUEST']).read_bytes())\n"
            "source = Path(os.environ['FLEET_REVIEW_SOURCE'])\n"
            "assert (source / 'test_delivery.py').is_file()\n"
            "assert 'FLEET_TEST_SECRET' not in os.environ\n"
            "assert not (Path.home() / '.fleet-credentials').exists()\n"
            "assert not (source / '.git').exists()\n"
            "print(json.dumps({**request, 'verdict': " + repr(verdict) + ", 'findings': []}))\n"
        ), encoding="utf-8")
        launcher = path / "fixture_launcher.py"
        launcher.write_text("import os, sys\nos.execv(sys.argv[1], sys.argv[1:])\n", encoding="utf-8")
        self.adapter = CommandReviewAdapter(CatalogRef("command-review", 1), ReviewIdentity("reviewer-1", "model", "fixture-model"),
            (sys.executable, str(script)), launcher=(sys.executable, str(launcher)), timeout_seconds=3)
        self.target = RegisteredRunTarget(OWNER, WORKSPACE, "review-node", self.adapter.spec.adapter_ref,
            CatalogRef("coding-delivery", 1), "review-execution", b"{}", review_adapter=self.adapter.spec)
        self.executor = RunStepExecutor(self.repository, self.target, clock=self.clock)

    def clock(self) -> float:
        return self.now

    def ready(self) -> None:
        definition_step = self.definition.steps[1]
        resolved = resolve_contract(self.definition, AuthorizedInputs(OWNER, WORKSPACE, "review", self.target.adapter_ref,
            (InputBinding("source", OWNER, WORKSPACE, self.exported.revision, definition_step.contract.input_refs[0].reference),)))
        revision = self.repository.get(OWNER, "workflow-a").revision
        self.repository.freeze_inputs(self.claim, step_key="review", generation=1, resolved=resolved, expected_revision=revision, now=self.clock())
        revision = self.repository.get(OWNER, "workflow-a").revision
        self.repository.admit_step(self.claim, step_key="review", generation=1, expected_revision=revision,
            target_key=self.target.target_key, capacity=1, reserve_tokens=0, reserve_requests=0, now=self.clock())

    def dispatch(self) -> Any:
        self.ready()
        return asyncio.run(WorkflowDispatchService(self.repository, self.executor, clock=self.clock).dispatch(
            self.claim, step_key="review", generation=1, expected_revision=self.repository.get(OWNER, "workflow-a").revision))

    def worker(self, *, adapters: tuple[Any, ...] | None = None, enabled: bool = True) -> LocalRunWorkerService:
        service = WorkflowReviewExecutionService(self.delivery, self.service, clock=self.clock,
            adapters=(self.adapter,) if adapters is None else adapters) if enabled else None
        return LocalRunWorkerService(self.platform, RunEventService(self.platform, clock=self.clock), worker_id="review-worker",
            clock=self.clock, artifact_store=self.store, workflow_review_executor=service,
            provider_factory=lambda profile: pytest.fail("command review must not call the native model"))

    def evidence(self) -> list[Any]:
        with self.repository.transaction(write=False) as connection:
            return connection.execute("SELECT result,subject_digest,source,report_artifact_id FROM workflow_evidence "
                                      "WHERE workflow_id='workflow-a' AND step_key='review' LIMIT 3").fetchall()


@pytest.mark.parametrize("verdict", ["passed", "failed", "inconclusive"])
def test_review_run_executes_real_command_and_publishes_independent_evidence(tmp_path: Path, verdict: str,
                                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLEET_TEST_SECRET", "must-not-reach-reviewer")
    fixture = ReviewFixture(tmp_path, verdict=verdict)
    observation = fixture.dispatch()
    result = fixture.worker().run_once(OWNER)
    assert result["state"] == "succeeded"
    assert fixture.worker().run_once(OWNER) is None
    assert asyncio.run(fixture.executor.inspect(observation.binding)).state == "succeeded"
    evidence = fixture.evidence()
    assert len(evidence) == 1 and evidence[0]["result"] == verdict
    assert evidence[0]["subject_digest"] == fixture.source_receipt.subject_digest
    report = json.loads(fixture.store.read(OWNER, WORKSPACE, evidence[0]["report_artifact_id"]))
    assert report["executor"] == fixture.adapter.spec.executor.payload()
    assert report["source_revision"]["tree_id"] == fixture.exported.revision.tree_id
    events = fixture.platform.list_run_events(OWNER, observation.binding.run_id)["events"]
    assert [item["kind"] for item in events] == ["run_started", "workflow_review_finished"]
    if verdict != "passed":
        record = fixture.repository.get_dispatch(OWNER, "workflow-a", "review", 1)
        from hub.domain.workflow_execution import ExecutionObservation

        fixture.repository.apply_observation(fixture.claim, record, ExecutionObservation(observation.binding, "succeeded"), now=fixture.clock())
        fixture.now += 1
        assert fixture.repository.get(OWNER, "workflow-a").execution_state == "waiting_approval"
        with pytest.raises(WorkflowRepositoryError, match="step_not_ready"):
            snapshot = fixture.repository.get(OWNER, "workflow-a")
            fixture.repository.admit_step(fixture.claim, step_key="test", generation=1, expected_revision=snapshot.revision,
                target_key="test-node", capacity=1, reserve_tokens=0, reserve_requests=0, now=fixture.clock())


@pytest.mark.parametrize("failure", ["missing_service", "missing_registration", "changed_registration", "missing_launcher"])
def test_review_requires_exact_registration_and_configured_launcher(tmp_path: Path, failure: str) -> None:
    fixture = ReviewFixture(tmp_path)
    adapters: tuple[Any, ...] | None = None
    if failure == "missing_registration":
        adapters = ()
    elif failure in ("changed_registration", "missing_launcher"):
        adapter = CommandReviewAdapter(fixture.adapter.spec.adapter_ref, fixture.adapter.spec.executor, fixture.adapter.argv,
            launcher=fixture.adapter.launcher if failure == "changed_registration" else (), timeout_seconds=2)
        if failure == "missing_launcher":
            fixture.target = replace(fixture.target, review_adapter=adapter.spec)
            fixture.executor = RunStepExecutor(fixture.repository, fixture.target, clock=fixture.clock)
        adapters = (adapter,)
    fixture.dispatch()
    result = fixture.worker(adapters=adapters, enabled=failure != "missing_service").run_once(OWNER)
    assert result["state"] == "failed"
    assert result["result_text"] == {"missing_service": "workflow_review_unavailable", "missing_registration": "review_adapter_unavailable",
        "changed_registration": "review_adapter_unavailable", "missing_launcher": "sandbox_unavailable"}[failure]
    assert fixture.evidence() == []


@pytest.mark.parametrize("output,code", [
    ("print('not-json')", "invalid_json"),
    ("print('x' * 65537)", "review_report_too_large"),
    ("raise SystemExit(7)", "review_process_failed"),
    ("import time; time.sleep(10)", "review_timeout"),
    ("import json,os; v=json.load(open(os.environ['FLEET_REVIEW_REQUEST'])); v.update(verdict='passed',findings=[],executor={'id':'forged','kind':'service','model_id':None}); print(json.dumps(v))", "executor_identity_mismatch"),
])
def test_review_invalid_process_output_cannot_publish_evidence(tmp_path: Path, output: str, code: str) -> None:
    fixture = ReviewFixture(tmp_path, program=output + "\n")
    fixture.dispatch()
    result = fixture.worker().run_once(OWNER)
    assert result["state"] == "failed" and result["result_text"] == code
    assert fixture.evidence() == []


@pytest.mark.parametrize("conflict", [False, True])
def test_review_publication_retries_same_report_without_process_replay(tmp_path: Path, conflict: bool,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = ReviewFixture(tmp_path)
    fixture.dispatch()
    runs: list[str] = []
    run = fixture.adapter.run
    async def once(*args: Any, **kwargs: Any) -> Any:
        runs.append("run")
        return await run(*args, **kwargs)
    monkeypatch.setattr(fixture.adapter, "run", once)
    original = fixture.service.record_review
    publications: list[tuple[str, ...]] = []
    async def publish(*args: Any, **kwargs: Any) -> Any:
        from hub.application.workflow_coding_delivery import WorkflowCodingDeliveryError

        if conflict and not publications:
            with fixture.repository.transaction() as connection:
                connection.execute("UPDATE workflow_runs SET revision=revision+1 WHERE workflow_id='workflow-a'")
            publications.append(())
        receipt = await original(*args, **kwargs)
        publications.append(tuple(sorted(item.name for item in fixture.store.root.iterdir())))
        if not conflict and len(publications) == 1:
            raise WorkflowCodingDeliveryError("delivery_io_failed") from OSError("response lost")
        return receipt
    monkeypatch.setattr(fixture.service, "record_review", publish)
    assert fixture.worker().run_once(OWNER)["state"] == "succeeded"
    assert runs == ["run"] and len(fixture.evidence()) == 1
    assert len(publications) == 2
    if not conflict:
        assert publications[0] == publications[1]


@pytest.mark.parametrize("mutation", ["contents", "extra_file", "symlink"])
def test_review_cannot_publish_a_verdict_for_a_modified_checkout(tmp_path: Path, mutation: str) -> None:
    change = {
        "contents": "(source / 'test_delivery.py').write_text('changed')",
        "extra_file": "(source / 'extra.py').write_text('changed')",
        "symlink": "(source / 'test_delivery.py').rename(source / 'saved.py'); (source / 'test_delivery.py').symlink_to(source / 'saved.py')",
    }[mutation]
    fixture = ReviewFixture(tmp_path, program="import os,json\nfrom pathlib import Path\n"
        "request=json.loads(Path(os.environ['FLEET_REVIEW_REQUEST']).read_bytes())\n"
        "source=Path(os.environ['FLEET_REVIEW_SOURCE'])\n" + change + "\n"
        "print(json.dumps({**request,'verdict':'passed','findings':[]}))\n")
    fixture.dispatch()
    result = fixture.worker().run_once(OWNER)
    assert result["state"] == "failed" and result["result_text"] == "review_source_changed"
    assert fixture.evidence() == []


@pytest.mark.parametrize("lost_response", [False, True])
def test_committed_review_wins_late_cancel_and_lost_response(tmp_path: Path, lost_response: bool,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = ReviewFixture(tmp_path)
    observation = fixture.dispatch()
    original = fixture.service.record_review
    calls: list[str] = []
    async def publish(*args: Any, **kwargs: Any) -> Any:
        from hub.application.workflow_coding_delivery import WorkflowCodingDeliveryError

        receipt = await original(*args, **kwargs)
        calls.append("publish")
        fixture.platform.cancel_run(OWNER, observation.binding.run_id, now=fixture.clock())
        if lost_response and len(calls) == 1:
            raise WorkflowCodingDeliveryError("delivery_io_failed") from OSError("response lost")
        return receipt
    monkeypatch.setattr(fixture.service, "record_review", publish)
    assert fixture.worker().run_once(OWNER)["state"] == "succeeded"
    assert len(fixture.evidence()) == 1


@pytest.mark.parametrize("mutation", ["cancel", "lease", "workflow", "frozen", "workspace", "deadline", "binding"])
def test_review_reaps_owned_process_before_revocation_returns(tmp_path: Path, mutation: str) -> None:
    started = tmp_path / "started"
    fixture = ReviewFixture(tmp_path, program="import os,time\nfrom pathlib import Path\nPath(" + repr(str(started)) +
        ").write_text(str(os.getpid()))\ntime.sleep(30)\n")
    observation = fixture.dispatch()
    def revoke() -> None:
        limit = time.monotonic() + 5
        while not started.exists() and time.monotonic() < limit:
            time.sleep(.01)
        if not started.exists():
            return
        if mutation == "cancel":
            fixture.platform.cancel_run(OWNER, observation.binding.run_id, now=fixture.clock())
        else:
            with fixture.repository.transaction() as connection:
                connection.execute({
                    "lease": "UPDATE runs SET lease_id='changed' WHERE state='running'",
                    "workflow": "UPDATE workflow_runs SET cancel_requested=1 WHERE workflow_id='workflow-a'",
                    "frozen": "UPDATE workflow_schema_meta SET admission_mode='frozen'",
                    "workspace": "UPDATE workspaces SET enabled=0",
                    "deadline": "UPDATE workflow_steps SET active_deadline=109 WHERE step_key='review'",
                    "binding": "UPDATE workflow_dispatches SET target_key='changed' WHERE step_key='review'",
                }[mutation])
    thread = threading.Thread(target=revoke)
    thread.start()
    try:
        result = fixture.worker().run_once(OWNER)
    finally:
        thread.join(timeout=6)
    assert started.exists() and not thread.is_alive()
    assert result["state"] in ("cancelled", "failed", "lease_lost")
    assert fixture.evidence() == []
    with pytest.raises(ProcessLookupError):
        os.kill(int(started.read_text()), 0)


def test_review_async_cancellation_joins_process_before_removing_source(tmp_path: Path) -> None:
    started = tmp_path / "started"
    fixture = ReviewFixture(tmp_path, program="import os,time\nfrom pathlib import Path\nPath(" + repr(str(started)) +
        ").write_text(str(os.getpid()))\ntime.sleep(30)\n")
    async def scenario() -> None:
        task = asyncio.create_task(fixture.adapter.run(tmp_path / "repo", fixture.exported.revision, fixture.source_receipt.subject_digest,
            developer_executor_id="developer-1", should_cancel=lambda: False))
        async def wait_started() -> None:
            while not started.exists():
                await asyncio.sleep(.01)
        await asyncio.wait_for(wait_started(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ProcessLookupError):
            os.kill(int(started.read_text()), 0)
    asyncio.run(scenario())


def test_review_after_run_started_crash_is_unknown_without_replaying_command(tmp_path: Path) -> None:
    fixture = ReviewFixture(tmp_path)
    observation = fixture.dispatch()
    claim = fixture.platform.claim_run(OWNER, worker_id="lost-worker", now=fixture.clock(), lease_s=5)
    fixture.platform.append_run_event(OWNER, claim["run_id"], "run_started", {}, now=fixture.clock(),
        lease_id=claim["lease_id"], worker_id="lost-worker")
    fixture.now += 6
    result = fixture.worker().run_once(OWNER)
    assert result["state"] == "unknown" and fixture.evidence() == []
    assert fixture.worker().run_once(OWNER) is None
    assert asyncio.run(fixture.executor.inspect(observation.binding)).state == "unknown"


@pytest.mark.parametrize("field,value", [("revision", True), ("configuration_digest", "bad"), ("executor", {})])
def test_review_registration_rejects_coerced_or_incomplete_identity(tmp_path: Path, field: str, value: Any) -> None:
    from hub.domain.workflow_review import ReviewAdapterSpec

    fixture = ReviewFixture(tmp_path)
    raw = {**fixture.adapter.spec.payload(), field: value}
    with pytest.raises(WorkflowValidationError):
        ReviewAdapterSpec.parse(raw)


def test_review_target_disallows_request_marker_and_overlapping_execution(tmp_path: Path) -> None:
    fixture = ReviewFixture(tmp_path)
    with pytest.raises(WorkflowValidationError):
        replace(fixture.target, config_snapshot=json.dumps({"workflow_review": fixture.adapter.spec.payload()}).encode())
    with pytest.raises(WorkflowValidationError):
        replace(fixture.target, test_recipe=CatalogRef("coding-delivery-tests", 1))


def test_review_run_created_before_binding_is_requeued_without_starting_command(tmp_path: Path) -> None:
    fixture = ReviewFixture(tmp_path)
    fixture.ready()
    record = fixture.repository.begin_dispatch(fixture.claim, step_key="review", generation=1,
        expected_revision=fixture.repository.get(OWNER, "workflow-a").revision, now=fixture.clock())
    binding = asyncio.run(fixture.executor.create_or_lookup(record.token, record.payload_hash, record.resolved))
    result = fixture.worker().run_once(OWNER)
    assert result["state"] == "queued" and result["attempt"] == 0
    assert fixture.platform.list_run_events(OWNER, binding.run_id)["events"] == []
    fixture.repository.bind_dispatch(fixture.claim, record, binding, now=fixture.clock())
    assert fixture.worker().run_once(OWNER)["state"] == "succeeded"


@pytest.mark.parametrize("field,value", [("owner_id", "other@example.test"), ("lease_id", "wrong"), ("worker_id", "other-worker"), ("attempt", 2)])
def test_review_lookup_rejects_another_lease_identity(tmp_path: Path, field: str, value: Any) -> None:
    fixture = ReviewFixture(tmp_path)
    fixture.dispatch()
    claim = fixture.platform.claim_run(OWNER, worker_id="review-worker", now=fixture.clock())
    lease = DeliveryRunLease(OWNER, claim["run_id"], claim["lease_id"], "review-worker", claim["attempt"])
    with pytest.raises(WorkflowRepositoryError, match="run_lease_mismatch"):
        fixture.delivery.target_for_review(replace(lease, **{field: value}), now=fixture.clock())


@pytest.mark.parametrize("mutation", ["lease", "cancel", "generation", "frozen", "workspace"])
def test_review_final_publication_transaction_rechecks_execution(tmp_path: Path, mutation: str,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = ReviewFixture(tmp_path)
    fixture.dispatch()
    original = fixture.service._publish
    def publish(*args: Any, **kwargs: Any) -> Any:
        artifact = original(*args, **kwargs)
        with fixture.repository.transaction() as connection:
            connection.execute({
                "lease": "UPDATE runs SET lease_owner='another-worker' WHERE state='running'",
                "cancel": "UPDATE runs SET cancel_requested=1 WHERE state='running'",
                "generation": "UPDATE workflow_steps SET is_current=0 WHERE step_key='review'",
                "frozen": "UPDATE workflow_schema_meta SET admission_mode='frozen'",
                "workspace": "UPDATE workspaces SET enabled=0",
            }[mutation])
        return artifact
    monkeypatch.setattr(fixture.service, "_publish", publish)
    assert fixture.worker().run_once(OWNER)["state"] in ("failed", "cancelled", "lease_lost")
    assert fixture.evidence() == []


def test_review_registration_cannot_make_the_developer_its_own_reviewer(tmp_path: Path) -> None:
    fixture = ReviewFixture(tmp_path)
    adapter = CommandReviewAdapter(fixture.adapter.spec.adapter_ref, ReviewIdentity("developer-1", "service"),
        fixture.adapter.argv, launcher=fixture.adapter.launcher)
    fixture.target = replace(fixture.target, review_adapter=adapter.spec)
    fixture.executor = RunStepExecutor(fixture.repository, fixture.target, clock=fixture.clock)
    observation = fixture.dispatch()
    result = fixture.worker(adapters=(adapter,)).run_once(OWNER)
    assert result["state"] == "failed" and result["result_text"] == "independent_reviewer_required"
    assert fixture.evidence() == []
    events = fixture.platform.list_run_events(OWNER, observation.binding.run_id)["events"]
    assert not any(event["kind"] == "run_started" for event in events)


def test_review_bootstrap_stays_default_off_with_no_implicit_adapters_or_threads(tmp_path: Path) -> None:
    from hub.bootstrap import create_app
    from hub.config import FleetConfig

    app = create_app(FleetConfig.from_root(tmp_path, platform_enabled=True, platform_worker_enabled=True,
        platform_workflows_enabled=True, dev_operator=OWNER, serve_frontend=False))
    services = app.extensions["fleet"]["services"]
    assert services["platform_worker"].workflow_review_executor is services["workflow_review_executor"]
    assert services["workflow_review_executor"].adapters == {}
    assert not any(thread.name.startswith("run-lease-") for thread in threading.enumerate())


def test_review_with_real_sandbox_denies_source_host_and_network_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    secret = tmp_path / "host-secret"
    secret.write_text("private host data", encoding="utf-8")
    monkeypatch.setenv("FLEET_TEST_SECRET", "synthetic-review-credential")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    program = ("import errno,json,os,socket,sys\nfrom pathlib import Path\n"
        "request=Path(os.environ['FLEET_REVIEW_REQUEST'])\n"
        "value=json.loads(request.read_bytes())\n"
        "source=Path(os.environ['FLEET_REVIEW_SOURCE'])\n"
        "assert 'FLEET_TEST_SECRET' not in os.environ\n"
        "assert (source/'test_delivery.py').is_file()\n"
        "operations=[lambda: (source/'forbidden').write_text('x'), lambda: request.write_text('x'), "
        "lambda: Path(" + repr(str(secret)) + ").read_bytes(), "
        "lambda: Path(" + repr(str(tmp_path / "host-write")) + ").write_text('x'), "
        "lambda: socket.create_connection(('127.0.0.1'," + str(listener.getsockname()[1]) + "),timeout=1)]\n"
        "for index,operation in enumerate(operations):\n"
        "    try:\n        operation()\n"
        "    except OSError as denied:\n"
        "        allowed={errno.EPERM,errno.EACCES}\n"
        "        if sys.platform=='linux' and index<4:\n"
        "            allowed.update({errno.EROFS,errno.ENOENT})\n"
        "        assert denied.errno in allowed\n"
        "        continue\n"
        "    raise AssertionError('sandbox boundary was open')\n"
        "(Path.home()/'allowed-scratch').write_text('ok')\n"
        "print(json.dumps({**value,'verdict':'passed','findings':[]}))\n")
    fixture = ReviewFixture(tmp_path, program=program)
    script = str(tmp_path / "reviewer.py")
    runtime = Path(sys.base_prefix).resolve()
    runtime_parents = "".join("(literal " + json.dumps(str(path)) + ")" for path in runtime.parents)
    profile = ('(version 1)(deny default)(allow process*)(allow sysctl-read)(allow mach-lookup)'
        '(allow file-read-metadata)(allow file-read-data '
        '(subpath ' + json.dumps(str(runtime)) + ') ' + runtime_parents +
        '(subpath "/System/Library")(subpath "/usr/lib")(literal "/dev/null") '
        '(literal ' + json.dumps(script) + ') (subpath "{source}")(subpath "{control}")) '
        '(allow file-write* (subpath "{scratch}"))')
    if sys.platform == "darwin":
        launcher = ("/usr/bin/sandbox-exec", "-p", profile)
    elif sys.platform == "linux":
        from workflow_sandbox_runtime import linux_review_launcher
        launcher = linux_review_launcher(Path(script))
    else:
        raise RuntimeError("workflow sandbox acceptance requires Linux or macOS")
    adapter = CommandReviewAdapter(fixture.adapter.spec.adapter_ref, fixture.adapter.spec.executor, (str(Path(sys.executable).resolve()), "-I", script),
        launcher=launcher)
    fixture.target = replace(fixture.target, review_adapter=adapter.spec)
    fixture.executor = RunStepExecutor(fixture.repository, fixture.target, clock=fixture.clock)
    fixture.dispatch()
    with listener:
        result = fixture.worker(adapters=(adapter,)).run_once(OWNER)
    assert result["state"] == "succeeded", result.get("result_text")
    assert fixture.evidence()[0]["result"] == "passed"
    assert secret.read_text() == "private host data" and not (tmp_path / "host-write").exists()
