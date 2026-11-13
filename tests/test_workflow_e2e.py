"""Cookie/HTTP/Runner/provider workflow chain; all external peers are local fixtures."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from dataclasses import replace
from contextlib import contextmanager
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Queue
from threading import Thread
from types import SimpleNamespace
from typing import Any, Callable, Iterator
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

import pytest
from flask import Flask
from werkzeug.serving import make_server

from hub.application.run_event_service import RunEventService
from hub.application.run_worker_service import LocalRunWorkerService
from hub.application.workflow_coordinator import RegisteredWorkflowPlan, WorkflowCoordinator
from hub.application.workflow_review_execution import WorkflowReviewExecutionService
from hub.application.workflow_templates import RegisteredWorkflowTemplate, WorkflowTemplateCatalog, coding_delivery_definition
from hub.application.workflow_test_execution import WorkflowTestExecutionService
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.domain.workflow_contract_types import CatalogRef
from fleet_core.control import generate_ed25519_keypair
from hub.domain.workflow_review import ReviewIdentity
from hub.infrastructure.workflow_delivery_repository import WorkflowDeliveryRepository
from hub.infrastructure.workflow_input_resolver import WorkflowArtifactInputResolver
from hub.infrastructure.workflow_run_executor import LegacyTaskTarget, RegisteredRunTarget, RunStepExecutor
from fleet_core.backends.directory import DirectoryBackend
from fleet_core.backends.sandbox import SandboxBackend
from hub.platform.workflow_review import CommandReviewAdapter
from hub.platform.workflow_validators import FixedTestRunner
from tools.runner_config import RunnerConfig
from tools.transport import Transport
from test_runner import load_agent_runner
from test_workflow_coordinator import Clock
from test_workflow_source import repo_with_commit
from test_workflow_sandbox import isolated_test_backend
from test_workflow_startup import registration, seed

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/workflow"
WORKSPACE = "approved-repository"
PASSWORD = "Workflow-local-peer-123!"


class CookieClient:
    def __init__(self, origin: str) -> None:
        self.origin = origin
        self.cookies = CookieJar()
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(self.cookies))

    def request(self, path: str, payload: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        request = Request(self.origin + path, data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers={"Content-Type": "application/json", "Origin": self.origin})
        try:
            response = self.opener.open(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read(1024 * 1024 + 1)
            assert len(raw) <= 1024 * 1024
            return response.status, json.loads(raw)

    def download(self, path: str) -> bytes:
        with self.opener.open(self.origin + path, timeout=10) as response:
            assert response.status == 200 and response.headers["Content-Disposition"].startswith("attachment;")
            raw = response.read(10 * 1024 * 1024 + 1)
        assert len(raw) <= 10 * 1024 * 1024
        return raw


@contextmanager
def hub_server(app: Flask) -> Iterator[str]:
    server = make_server("127.0.0.1", 0, app, threaded=True)
    origin = f"http://127.0.0.1:{server.server_port}"
    app.config["ACCOUNT_ORIGIN"] = origin
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield origin
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()


@contextmanager
def strict_model_peer(verdict: str) -> Iterator[tuple[str, Queue[dict[str, Any]], Queue[str]]]:
    calls: Queue[dict[str, Any]] = Queue()
    errors: Queue[str] = Queue()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_arguments: Any) -> None:
            return

        def do_POST(self) -> None:
            self.connection.settimeout(5)
            try:
                assert self.path == "/v1/chat/completions"
                assert self.headers.get("Authorization") == "Bearer fixture-review-key"
                length = int(self.headers["Content-Length"])
                assert 1 <= length <= 65536
                payload = json.loads(self.rfile.read(length))
                assert set(payload) == {"model", "messages", "tools", "stream"}
                assert payload["model"] == "fixture-review-model" and payload["tools"] == [] and payload["stream"] is False
                messages = payload["messages"]
                assert len(messages) == 2 and [message["role"] for message in messages] == ["system", "user"]
                assert messages[0]["content"] == "Review this fixed source and return the registered JSON report."
                actual = json.loads(messages[1]["content"])
                assert set(actual) == {"request", "source_sha256", "source_text", "test_present"}
                assert actual["source_text"] == "HTTP workflow output\n" and actual["test_present"] is True
                assert actual["source_sha256"] == hashlib.sha256(b"HTTP workflow output\n").hexdigest()
                request = actual["request"]
                assert set(request) == {"schema_version", "source_revision", "subject_digest", "executor"}
                assert request["schema_version"] == 1
                assert request["executor"] == {"id": "fixture-model-reviewer", "kind": "model", "model_id": "fixture-review-model"}
                calls.put(actual)
                body = json.dumps({"choices": [{"message": {"role": "assistant", "content": json.dumps(
                    {**request, "verdict": verdict, "findings": []})}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 7}}).encode("utf-8")
                status = 200
            except (AssertionError, KeyError, TypeError, ValueError, OSError) as error:
                errors.put(type(error).__name__ + ": " + str(error))
                status, body = 400, b'{"error":{"code":"fixture_protocol_rejected"}}'
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/chat/completions", calls, errors
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()


def _register_chain(app: Flask, owner: str, endpoint: str, clock: Clock,
    test_backend: Callable[[Path], DirectoryBackend],
) -> tuple[WorkflowCoordinator, LocalRunWorkerService]:
    fleet = app.extensions["fleet"]
    platform = fleet["platform_repository"]
    repository = fleet["repositories"]["workflows"]
    definition = coding_delivery_definition()
    fleet["services"]["workflows"].catalog = WorkflowTemplateCatalog((RegisteredWorkflowTemplate(owner, WORKSPACE, definition),))
    platform.upsert_workspace(owner, {"workspace_id": WORKSPACE, "root_path": str(app.config["WORKFLOW_FIXTURE_ROOT"])})
    for conversation in ("develop-chat", "review-chat", "test-chat"):
        platform.create_conversation(owner, conversation, title="HTTP workflow", workspace_id=WORKSPACE)
    adapter = CommandReviewAdapter(CatalogRef("fixture-review", 1), ReviewIdentity("fixture-model-reviewer", "model", "fixture-review-model"),
        (sys.executable, str(FIXTURES / "model_reviewer.py"), str(ROOT), endpoint),
        launcher=(sys.executable, "-c", "import os,sys;os.execv(sys.argv[1],sys.argv[1:])"))
    policy = CatalogRef("coding-delivery", 1)
    targets = (
        RegisteredRunTarget(owner, WORKSPACE, "cli-node", CatalogRef("fixture-cli", 1), policy, "develop-chat", b"{}",
            LegacyTaskTarget("mac-local", "codex", "agent-fleet"), source_repository_id="repo-a"),
        RegisteredRunTarget(owner, WORKSPACE, "review-node", adapter.spec.adapter_ref, policy, "review-chat", b"{}", review_adapter=adapter.spec),
        RegisteredRunTarget(owner, WORKSPACE, "test-node", CatalogRef("fixed-tests", 1), policy, "test-chat", b"{}",
            test_recipe=CatalogRef("coding-delivery-tests", 1)),
    )
    digest = hashlib.sha256(definition.snapshot).hexdigest()
    plans = tuple(RegisteredWorkflowPlan(owner, WORKSPACE, digest, key, target.adapter_ref, policy,
        target.target_key, 1, 0, 1) for key, target in zip(("develop", "review", "test"), targets))
    store = fleet["services"]["platform_artifacts"]
    repository.configure_hub_input_targets(tuple(item for target in targets if (item := target.hub_input_target()) is not None))
    delivery = fleet["services"]["workflow_coding_delivery"]
    delivery_repository = WorkflowDeliveryRepository(repository, delivery.task_repository)
    coordinator = WorkflowCoordinator(repository, worker_id="http-coordinator", clock=clock, registrations=plans,
        executors=tuple((owner, WORKSPACE, target.target_key, RunStepExecutor(repository, target, clock=clock)) for target in targets),
        dispatch_enabled=True, input_resolver=WorkflowArtifactInputResolver(repository, store))
    coordinator.artifact_input_service = fleet["services"].get("artifact_inputs")
    worker = LocalRunWorkerService(platform, RunEventService(platform, clock=clock), worker_id="http-worker", clock=clock,
        artifact_store=store, provider_factory=lambda profile: pytest.fail("workflow Runs must use their registered adapters"),
        workflow_review_executor=WorkflowReviewExecutionService(delivery_repository, delivery, clock=clock, adapters=(adapter,)),
        workflow_test_executor=WorkflowTestExecutionService(delivery_repository, delivery, clock=clock,
            runner=FixedTestRunner(backend_factory=test_backend)))
    return coordinator, worker


@contextmanager
def _registered_chain_server(app, config, owner, repo, endpoint, clock, test_backend, startup_registration):
    if startup_registration:
        seed(app.extensions["fleet"]["platform_repository"], repo, owner=owner, workspace=WORKSPACE)
        manifest = registration(owner, WORKSPACE)
        manifest["coding_delivery"][0]["review"].update(
            executor_id="fixture-model-reviewer", kind="model", model_id="fixture-review-model",
            argv=[sys.executable, str(FIXTURES / "model_reviewer.py"), str(ROOT), endpoint],
            launcher=[sys.executable, "-c", "import os,sys;os.execv(sys.argv[1],sys.argv[1:])"])
        path = config.root / "coding.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        # The original sandbox backend accepts a fixed argv. This fixture builds
        # the existing test profile using the sandbox's execution directory.
        launcher = config.root / "test-launcher.py"
        launcher.write_text(
            f"import os,sys\nfrom pathlib import Path\nsys.path[:0]={str(ROOT / 'tests')!r}, {str(ROOT)!r}, {str(Path(pytest.__file__).resolve().parent.parent)!r}\n"
            "from test_workflow_sandbox import isolated_test_backend\n"
            "argv=isolated_test_backend(Path.cwd()).launcher\nos.execv(argv[0], [*argv, *sys.argv[1:]])\n", encoding="utf-8")
        app = create_app(replace(config, platform_workflow_registration_file=path,
            platform_workflow_dispatch_enabled=True, platform_worker_enabled=True,
            platform_worker_scheduler_enabled=True, platform_sandbox_launcher=(sys.executable, str(launcher))),
            repositories={"workflow_clock": clock,
                "observation": SimpleNamespace(read_current=lambda machine: {"reachable": machine == "mac-local"})})
        services = app.extensions["fleet"]["services"]
        coordinator, worker = services["workflow_coordinator"], services["platform_worker"]
        worker.clock = worker.run_events.clock = clock
    else:
        coordinator, worker = _register_chain(app, owner, endpoint, clock, test_backend)
    with hub_server(app) as origin:
        yield app, coordinator, worker, origin


@pytest.mark.parametrize("verdict", ["passed", "failed"])
def test_cookie_http_runner_model_review_and_human_acceptance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verdict: str) -> None:
    _exercise_cookie_http_chain(tmp_path, monkeypatch, verdict, DirectoryBackend)


def test_cookie_http_chain_runs_fixed_tests_in_real_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _exercise_cookie_http_chain(tmp_path, monkeypatch, "passed", isolated_test_backend)


def test_cookie_http_chain_from_startup_registration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _exercise_cookie_http_chain(tmp_path, monkeypatch, "passed", isolated_test_backend, startup_registration=True)


@pytest.mark.parametrize("verdict", ["passed", "failed"])
def test_transfer_gate_preserves_hub_review_test_and_acceptance(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verdict: str) -> None:
    _exercise_cookie_http_chain(tmp_path, monkeypatch, verdict, DirectoryBackend, transfer_enabled=True)


def _exercise_cookie_http_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verdict: str,
    test_backend: Callable[[Path], DirectoryBackend], *, transfer_enabled: bool = False, startup_registration: bool = False,
) -> None:
    monkeypatch.setenv("FLEET_TEST_SECRET", "synthetic-hub-value")
    clock = Clock(time.time())
    signing_key, _ = generate_ed25519_keypair()
    config = FleetConfig.from_root(tmp_path, platform_enabled=True, platform_workflows_enabled=True, serve_frontend=False,
        platform_artifact_transfer_enabled=transfer_enabled, platform_command_signing_raw=signing_key,
        runner_credentials={"mac-local": "fixture-runner-secret"}, project_whitelist={"mac-local": ["agent-fleet"]},
        accounts={"enabled": True, "origin": "http://127.0.0.1", "registration": "invite"})
    app = create_app(config, repositories={"workflow_clock": clock,
        "observation": SimpleNamespace(read_current=lambda machine: {"reachable": machine == "mac-local"})})
    accounts = app.extensions["accounts"]
    accounts.bootstrap_admin("workflow@example.test", PASSWORD)
    owner = accounts.users()[0]["id"]
    repo, _, _, _ = repo_with_commit(tmp_path)
    app.config["WORKFLOW_FIXTURE_ROOT"] = repo
    trace = tmp_path / "cli-invocation.json"
    with strict_model_peer(verdict) as (endpoint, calls, errors), _registered_chain_server(
            app, config, owner, repo, endpoint, clock, test_backend, startup_registration) as (app, coordinator, worker, origin):
        fleet = app.extensions["fleet"]
        bridge = fleet["services"]["legacy_task_bridge"]
        bridge.clock = clock
        client = CookieClient(origin)
        assert client.request("/api/platform/v1/workflows")[0] == 401
        assert client.request("/api/accounts/login", {"email": "workflow@example.test", "password": PASSWORD})[0] == 200
        assert list(client.cookies), "operator identity must come from an actual HTTP session cookie"
        template_status, template_body = client.request("/api/platform/v1/workflow-templates")
        assert template_status == 200 and len(template_body["templates"]) == 1
        create = {"template_id": "coding-delivery", "revision": 1, "workspace_id": WORKSPACE,
            "input_refs": [], "client_token": "http-create", "expected_policy_revision": 1}
        status, created = client.request("/api/platform/v1/workflows", create)
        assert status == 201, created
        workflow_id = created["workflow"]["workflow_id"]
        assert client.request("/api/platform/v1/workflows", create)[1]["workflow"]["workflow_id"] == workflow_id
        runner = load_agent_runner()
        developer_args = ["legacy-coding-runner"] if startup_registration else []
        cfg = RunnerConfig(hub=origin, machine="mac-local", credential="fixture-runner-secret", runner_id="http-fixture-runner",
            projects={"agent-fleet": repo}, cache_dir=tmp_path / "runner-cache", transport=Transport(mode="direct", attempts=1),
            agents={"codex": {"command": [sys.executable, str(FIXTURES / "cli_developer.py"), str(trace), *developer_args], "timeout_s": 20}})
        executed: list[dict[str, Any]] = []
        for _ in range(32):
            clock.now += 2
            summary = asyncio.run(coordinator.tick(clock()))
            assert not summary.failures, summary.failures
            bridge.process_once(owner)
            if runner.poll_once(cfg):
                clock.now += 31
                bridge.process_once(owner)
            result = worker.run_once(owner)
            if result is not None:
                executed.append(result)
                assert result["state"] == "succeeded", result["result_text"]
            status, data = client.request("/api/platform/v1/workflows/" + workflow_id)
            assert status == 200, data
            detail = data["workflow"]
            if detail["delivery_state"] == "awaiting_acceptance" or detail["reason_code"] == "blocked_by_validation":
                break
        assert detail["delivery_state"] == "awaiting_acceptance" or detail["reason_code"] == "blocked_by_validation", detail
        if transfer_enabled:
            with fleet["repositories"]["workflows"].transaction(write=False) as connection:
                assert connection.execute("SELECT count(*) FROM platform_commands WHERE action='artifact.receive'").fetchone()[0] == 0
        assert errors.empty(), list(errors.queue)
        assert calls.qsize() == 1, detail
        reviewed = calls.get_nowait()
        assert reviewed["request"]["subject_digest"] == detail["steps"][0]["subject_digest"]
        assert len(executed) == (2 if verdict == "passed" else 1)
        invocation = json.loads(trace.read_bytes())
        assert invocation["owner_id"] == owner and invocation["workspace_id"] == WORKSPACE
        assert invocation["generated_sha256"] == reviewed["source_sha256"]
        for step in detail["steps"]:
            for output in step["outputs"]:
                raw = client.download(f"/api/platform/v1/artifacts/{output['artifact_id']}/content?workspace_id={WORKSPACE}")
                assert len(raw) == output["size"] and hashlib.sha256(raw).hexdigest() == output["sha256"]
                if step["step_key"] == "test" and output["output_key"] == "test_report":
                    environment = json.loads(raw)["environment"]
                    backend_type = SandboxBackend if test_backend is isolated_test_backend else DirectoryBackend
                    assert environment["backend"] == backend_type.__module__ + "." + backend_type.__qualname__
                    assert (environment["launcher_sha256"] is not None) is (test_backend is isolated_test_backend)
        accept = {"decision": "accepted", "delivery_digest": detail["delivery_digest"],
            "expected_revision": detail["revision"], "client_token": "http-accept"}
        status, accepted = client.request(f"/api/platform/v1/workflows/{workflow_id}/acceptance", accept)
        if verdict == "passed":
            assert status == 202, accepted
            assert len({step["subject_digest"] for step in detail["steps"]}) == 1
            assert client.request(f"/api/platform/v1/workflows/{workflow_id}")[1]["workflow"]["delivery_state"] == "delivered"
        else:
            assert status == 409 and accepted["error"] == "delivery_not_ready"
            assert detail["validation_state"] == "failed" and detail["steps"][2]["execution_state"] == "pending"
        assert client.request("/api/accounts/logout", {})[0] == 200
        assert client.request("/api/platform/v1/workflows/" + workflow_id)[0] == 401
        assert calls.empty() and not runner.poll_once(cfg)
