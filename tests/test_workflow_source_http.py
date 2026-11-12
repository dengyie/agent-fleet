"""Authenticated raw-source requests through the real Flask application."""
from __future__ import annotations

import hashlib
import io
import time
import sys
import tarfile
import asyncio
from urllib.parse import urlsplit
from pathlib import Path
from typing import Any

import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig
from test_workflow_task_recovery import TaskDelivery, task_delivery
from test_workflow_repository import OWNER


@pytest.fixture
def source_http(tmp_path: Path, task_delivery: TaskDelivery) -> tuple[Any, TaskDelivery]:
    fixture = task_delivery
    config = FleetConfig.from_root(tmp_path, platform_enabled=True, platform_workflows_enabled=True,
        platform_db=fixture.repository.db_path, task_db=fixture.tasks.db_path, platform_artifact_root=fixture.store.root,
        runner_credentials={"mac-local": "runner-secret", "other": "other-secret"}, dev_operator=OWNER, serve_frontend=False)
    app = create_app(config, repositories={"task": fixture.tasks, "workflow_clock": fixture.clock})
    return app, fixture


def headers(fixture: TaskDelivery) -> dict[str, str]:
    return {"X-Runner-Credential": "mac-local:runner-secret", "X-Task-Nonce": fixture.lease["nonce"],
            "X-Content-SHA256": hashlib.sha256(fixture.archive).hexdigest(), "Content-Type": "application/x-tar"}


def post(app: Any, fixture: TaskDelivery, **options: Any) -> Any:
    return app.test_client().post(f"/api/commands/{fixture.lease['attempt_id']}/source", **options)


def test_real_source_route_and_result_enforce_current_receipt(source_http: tuple[Any, TaskDelivery], monkeypatch: pytest.MonkeyPatch) -> None:
    app, fixture = source_http
    complete = fixture.tasks.complete_task
    monkeypatch.setattr(fixture.tasks, "complete_task", lambda **kwargs: complete(**kwargs, now=fixture.clock()))
    result_url = f"/api/commands/{fixture.lease['attempt_id']}/result"
    result_options = {"headers": {"X-Runner-Credential": "mac-local:runner-secret"},
                      "json": {"nonce": fixture.lease["nonce"], "exit_code": 0, "duration_s": 1}}
    assert app.test_client().post(result_url, **result_options).status_code == 409
    response = post(app, fixture, data=fixture.archive, headers=headers(fixture))
    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload == {"ok": True, "artifact_id": "source-attempt-" + fixture.lease["attempt_id"],
                       "sha256": hashlib.sha256(fixture.archive).hexdigest(), "size": len(fixture.archive)}
    assert post(app, fixture, data=fixture.archive, headers=headers(fixture)).get_json() == payload
    result = app.test_client().post(result_url, **result_options)
    assert result.status_code == 200 and result.get_json()["state"] == "succeeded"


@pytest.mark.parametrize("credential", [{}, {"Cf-Access-Authenticated-User-Email": OWNER},
    {"X-Agent-Fleet-Token": "ingest"}, {"X-Platform-Node-Credential": "node"},
    {"X-Runner-Credential": "mac-local:wrong"}])
def test_source_route_rejects_other_authentication_domains(source_http: tuple[Any, TaskDelivery], credential: dict[str, str]) -> None:
    app, fixture = source_http
    request_headers = headers(fixture)
    request_headers.pop("X-Runner-Credential")
    request_headers.update(credential)
    response = post(app, fixture, data=fixture.archive, headers=request_headers)
    assert response.status_code == 403
    assert fixture.tasks.source_receipt("mac-local", fixture.lease["attempt_id"], fixture.lease["nonce"]) is None


@pytest.mark.parametrize("changed", [{"X-Task-Nonce": "wrong"}, {"X-Runner-Credential": "other:other-secret"}])
def test_source_route_cannot_use_another_machine_or_nonce(source_http: tuple[Any, TaskDelivery], changed: dict[str, str]) -> None:
    app, fixture = source_http
    response = post(app, fixture, data=fixture.archive, headers={**headers(fixture), **changed})
    assert response.status_code == 400 and response.get_json()["error"] == "source_unavailable"


@pytest.mark.parametrize("changed", [{"X-Task-Nonce": ""}, {"X-Content-SHA256": ""},
    {"X-Content-SHA256": "z" * 64}, {"Content-Type": "application/json"}, {"Content-Encoding": "gzip"}])
def test_source_route_validates_headers_before_reading(source_http: tuple[Any, TaskDelivery], changed: dict[str, str]) -> None:
    app, fixture = source_http
    response = post(app, fixture, data=fixture.archive, headers={**headers(fixture), **changed})
    assert response.status_code == 400 and response.get_json()["error"] == "invalid_source_upload"


def test_source_route_rejects_hash_mismatch(source_http: tuple[Any, TaskDelivery]) -> None:
    app, fixture = source_http
    response = post(app, fixture, data=fixture.archive, headers={**headers(fixture), "X-Content-SHA256": "0" * 64})
    assert response.status_code == 400 and response.get_json()["error"] == "source_hash_mismatch"


@pytest.mark.parametrize("length", [None, "0", str(10 * 1024 * 1024 + 1), "-1"])
def test_source_route_rejects_missing_or_invalid_content_length(source_http: tuple[Any, TaskDelivery], length: str | None) -> None:
    app, fixture = source_http
    response = post(app, fixture, headers=headers(fixture), environ_overrides={"CONTENT_LENGTH": length or ""})
    assert response.status_code == 400


def test_source_route_rejects_truncated_body_as_a_client_error(source_http: tuple[Any, TaskDelivery]) -> None:
    app, fixture = source_http
    response = post(app, fixture, headers=headers(fixture), environ_overrides={
        "CONTENT_LENGTH": str(len(fixture.archive) + 1), "wsgi.input": io.BytesIO(fixture.archive)})
    assert response.status_code == 400 and response.get_json()["error"] == "invalid_content_length"


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("lose_result_response", [False, True])
def test_real_runner_http_capture_review_test_and_acceptance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                          managed: bool, lose_result_response: bool) -> None:
    from tools.runner_config import RunnerConfig
    from test_runner import load_agent_runner
    from test_workflow_task_recovery import build_task_delivery

    fixture = build_task_delivery(tmp_path, epoch=time.time())
    app = create_app(FleetConfig.from_root(tmp_path / "http", platform_enabled=True, platform_workflows_enabled=True,
        platform_db=fixture.repository.db_path, task_db=fixture.tasks.db_path, platform_artifact_root=fixture.store.root,
        runner_credentials={"mac-local": "runner-secret"}, dev_operator=OWNER, serve_frontend=False,
        session_repositories_enabled=True), repositories={"task": fixture.tasks, "workflow_clock": fixture.clock})
    complete, heartbeat = fixture.tasks.complete_task, fixture.tasks.heartbeat
    monkeypatch.setattr(fixture.tasks, "complete_task", lambda **kwargs: complete(**kwargs, now=fixture.clock()))
    monkeypatch.setattr(fixture.tasks, "heartbeat", lambda **kwargs: heartbeat(**kwargs, now=fixture.clock()))

    class HttpTransport:
        def __init__(self) -> None:
            self.source_count = 0
            self.result_count = 0

        def post_bytes(self, url: str, raw: bytes, headers: dict[str, str], **options: Any) -> Any:
            self.source_count += 1
            response = app.test_client().post(urlsplit(url).path, data=raw, headers=headers)
            return response.status_code, response.get_json()

        def post_json(self, url: str, body: Any, headers: dict[str, str], **options: Any) -> Any:
            response = app.test_client().post(urlsplit(url).path, json=body, headers=headers)
            if url.endswith("/result"):
                self.result_count += 1
                assert response.status_code == 200, response.get_json()
                if lose_result_response and self.result_count == 1:
                    return 0, {"error": "fixture_response_lost_after_commit"}
            return response.status_code, response.get_json()

    runner = load_agent_runner()
    transport = HttpTransport()
    cfg = RunnerConfig(hub="https://fixture.invalid", machine="mac-local", credential="runner-secret", runner_id="runner",
        projects={"agent-fleet": tmp_path / "repo"}, cache_dir=tmp_path / "runner-cache", transport=transport,
        agents={"codex": {"command": [sys.executable, "-c",
            "from pathlib import Path; Path('generated.txt').write_text('actual CLI output'); "
            "Path('test_cli_delivery.py').write_text(\"from pathlib import Path\\ndef test_output():\\n    assert Path('generated.txt').read_text() == 'actual CLI output'\\n\"); "
            "print('completed')"], "timeout_s": 30}})
    policy = asyncio.run(fixture.service.task_source_policy("mac-local", fixture.lease["attempt_id"], fixture.lease["nonce"]))
    task = {**fixture.lease, **policy}
    if managed:
        supervisor = runner._bf_supervisor().Supervisor(manifest_dir=tmp_path / "supervisor", machine_id=cfg.machine)
        outcome = runner.run_task_managed(cfg, task, supervisor)
    else:
        outcome = runner.run_task(cfg, task)
    assert outcome == {"exit_code": 0, "source_pending": lose_result_response}
    assert fixture.tasks.get_task(task["task_id"])["state"] == "succeeded"
    source = fixture.tasks.source_receipt("mac-local", task["attempt_id"], task["nonce"])
    with tarfile.open(fileobj=io.BytesIO(fixture.store.read(OWNER, "workspace-a", source["artifact_id"]))) as archive:
        assert archive.extractfile("generated.txt").read() == b"actual CLI output"
        assert ".env" not in archive.getnames()
    monkeypatch.setattr(runner.worktree, "create_worktree", lambda *args: pytest.fail("no CLI replay after a committed result"))
    runner.flush_pending(cfg)
    assert transport.source_count == 1 and transport.result_count == (2 if lose_result_response else 1)
    assert not (cfg.cache_dir / "pending" / (task["attempt_id"] + ".json")).exists()
    fixture.clock.now += 31
    fixture.bridge.process_once(OWNER)
    assert asyncio.run(fixture.executor.inspect(fixture.binding)).state == "succeeded"
    _run_validation_chain_and_accept(app, fixture, tmp_path)


def _run_validation_chain_and_accept(app: Any, fixture: TaskDelivery, root: Path) -> None:
    from hub.application.run_event_service import RunEventService
    from hub.application.run_worker_service import LocalRunWorkerService
    from hub.application.workflow_coordinator import RegisteredWorkflowPlan, WorkflowCoordinator
    from hub.application.workflow_review_execution import WorkflowReviewExecutionService
    from hub.application.workflow_test_execution import WorkflowTestExecutionService
    from hub.domain.workflow_contract_types import CatalogRef
    from hub.domain.workflow_review import ReviewIdentity
    from hub.infrastructure.workflow_input_resolver import WorkflowArtifactInputResolver
    from hub.infrastructure.workflow_run_executor import RegisteredRunTarget, RunStepExecutor
    from fleet_core.backends.directory import DirectoryBackend
    from hub.platform.workflow_review import CommandReviewAdapter
    from hub.platform.workflow_validators import FixedTestRunner
    from test_workflow_repository import WORKSPACE

    review_script = root / "review_command.py"
    review_script.write_text("import json,os\nfrom pathlib import Path\n"
        "request=json.loads(Path(os.environ['FLEET_REVIEW_REQUEST']).read_bytes())\n"
        "source=Path(os.environ['FLEET_REVIEW_SOURCE'])\n"
        "assert (source/'generated.txt').read_text()=='actual CLI output'\n"
        "assert (source/'test_cli_delivery.py').is_file()\n"
        "print(json.dumps({**request,'verdict':'passed','findings':[]}))\n", encoding="utf-8")
    launcher = root / "fixture_review_launcher.py"
    launcher.write_text("import os,sys\nos.execv(sys.argv[1],sys.argv[1:])\n", encoding="utf-8")
    adapter = CommandReviewAdapter(CatalogRef("command-review", 1), ReviewIdentity("fixture-review-service", "service"),
        (sys.executable, str(review_script)), launcher=(sys.executable, str(launcher)))
    for conversation in ("review-chat", "test-chat"):
        fixture.platform.create_conversation(OWNER, conversation, title="Validation", workspace_id=WORKSPACE)
    targets = (
        RegisteredRunTarget(OWNER, WORKSPACE, "review-node", adapter.spec.adapter_ref, CatalogRef("coding-delivery", 1),
            "review-chat", b"{}", review_adapter=adapter.spec),
        RegisteredRunTarget(OWNER, WORKSPACE, "test-node", CatalogRef("fixed-tests", 1), CatalogRef("coding-delivery", 1),
            "test-chat", b"{}", test_recipe=CatalogRef("coding-delivery-tests", 1)),
    )
    digest = fixture.repository.get(OWNER, "workflow-a").definition_digest
    plans = tuple(RegisteredWorkflowPlan(OWNER, WORKSPACE, digest, key, target.adapter_ref, target.policy_ref,
        target.target_key, 1, 0, 0) for key, target in zip(("review", "test"), targets))
    executors = ((OWNER, WORKSPACE, "node-a", fixture.executor), *( (OWNER, WORKSPACE, target.target_key,
        RunStepExecutor(fixture.repository, target, clock=fixture.clock)) for target in targets))
    coordinator = WorkflowCoordinator(fixture.repository, worker_id="chain-coordinator", clock=fixture.clock,
        registrations=plans, executors=executors, dispatch_enabled=True,
        input_resolver=WorkflowArtifactInputResolver(fixture.repository, fixture.store))
    worker = LocalRunWorkerService(fixture.platform, RunEventService(fixture.platform, clock=fixture.clock),
        worker_id="chain-worker", clock=fixture.clock, artifact_store=fixture.store,
        workflow_review_executor=WorkflowReviewExecutionService(fixture.delivery, fixture.service, clock=fixture.clock, adapters=(adapter,)),
        workflow_test_executor=WorkflowTestExecutionService(fixture.delivery, fixture.service, clock=fixture.clock,
            runner=FixedTestRunner(backend_factory=DirectoryBackend)),
        provider_factory=lambda profile: pytest.fail("fixture chain must not call a live model"))
    fixture.clock.now += 31
    executed: list[dict[str, Any]] = []
    client = app.test_client()
    for _ in range(16):
        fixture.clock.now += 2
        summary = asyncio.run(coordinator.tick(fixture.clock()))
        assert not summary.failures, [str(item) for item in summary.failures]
        result = worker.run_once(OWNER)
        if result is not None:
            executed.append(result)
        detail = client.get("/api/platform/v1/workflows/workflow-a").get_json()["workflow"]
        if detail["delivery_state"] == "awaiting_acceptance":
            break
    assert len(executed) == 2 and all(item["state"] == "succeeded" for item in executed)
    assert detail["delivery_state"] == "awaiting_acceptance" and detail["validation_state"] == "passed"
    assert len({step["subject_digest"] for step in detail["steps"]}) == 1
    accepted = client.post("/api/platform/v1/workflows/workflow-a/acceptance", headers={"Origin": "http://localhost"},
        json={"decision": "accepted", "delivery_digest": detail["delivery_digest"], "expected_revision": detail["revision"], "client_token": "accept-chain"})
    assert accepted.status_code == 202, accepted.get_json()
    assert client.get("/api/platform/v1/workflows/workflow-a").get_json()["workflow"]["delivery_state"] == "delivered"
