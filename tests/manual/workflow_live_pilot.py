"""Opt-in real CLI/model acceptance; never collected by pytest or run in CI.

Run with the project's test dependencies and the installed, authenticated Codex:
    python3 tests/manual/workflow_live_pilot.py --evidence-root /private/new/path --run-live

This creates a disposable Hub/account/repository, not a production deployment.
It stops before human acceptance. Evidence includes private provider settings;
share only the sanitized result and explicitly inspected downloaded artifacts.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT)]

from test_workflow_e2e import CookieClient, hub_server  # noqa: E402
from test_workflow_sandbox import isolated_test_backend  # noqa: E402
from test_runner import load_agent_runner  # noqa: E402
from hub.application.run_event_service import RunEventService  # noqa: E402
from hub.application.run_worker_service import LocalRunWorkerService  # noqa: E402
from hub.application.workflow_coordinator import RegisteredWorkflowPlan, WorkflowCoordinator  # noqa: E402
from hub.application.workflow_review_execution import WorkflowReviewExecutionService  # noqa: E402
from hub.application.workflow_templates import RegisteredWorkflowTemplate, WorkflowTemplateCatalog, coding_delivery_definition  # noqa: E402
from hub.application.workflow_test_execution import WorkflowTestExecutionService  # noqa: E402
from hub.bootstrap import create_app  # noqa: E402
from hub.config import FleetConfig  # noqa: E402
from hub.domain.workflow_contract_types import CatalogRef  # noqa: E402
from hub.domain.workflow_review import ReviewIdentity  # noqa: E402
from hub.infrastructure.workflow_delivery_repository import WorkflowDeliveryRepository  # noqa: E402
from hub.infrastructure.workflow_input_resolver import WorkflowArtifactInputResolver  # noqa: E402
from hub.infrastructure.workflow_run_executor import LegacyTaskTarget, RegisteredRunTarget, RunStepExecutor  # noqa: E402
from hub.platform.workflow_review import CommandReviewAdapter  # noqa: E402
from hub.platform.workflow_validators import FixedTestRunner  # noqa: E402
from fleet_core.control import generate_ed25519_keypair  # noqa: E402
from tools.runner_config import RunnerConfig  # noqa: E402
from tools.transport import Transport  # noqa: E402

WORKSPACE = "real-cli-pilot"
BRIDGE = Path(__file__).with_name("workflow_live_codex.py").resolve()
TASK = """# Disposable real Workflow pilot

Create `ports.py` exporting `parse_port(value: str) -> int` using only the Python
standard library. Non-string inputs raise TypeError (including bool/int/bytes).
Strip leading/trailing whitespace using str.strip(). The remaining nonempty
input must consist only of ASCII digits 0..9; signs, Unicode digits, interior
whitespace, decimal points and exponent notation raise ValueError. Leading
zeros are allowed. The integer must be between 1 and 65535 inclusive, otherwise
raise ValueError. Do not change this file or test_ports.py; create only ports.py.
Do not install packages, use network, use git or delegate. Independent review
and fixed tests will run after the developer exits.
"""
TESTS = '''import pytest
from ports import parse_port

@pytest.mark.parametrize("text,expected", [("1",1),("65535",65535),("443",443),(" 80 ",80),("\\t0080\\n",80),("00001",1),("00065535",65535),("\\u200380\\u2003",80)])
def test_valid(text,expected):
    assert parse_port(text) == expected
    assert type(parse_port(text)) is int

@pytest.mark.parametrize("text", ["0","0000","65536","99999999999999999999999","-1","+1","1.0","1e3",""," ","１２","١٢","8 0","80\\x00","0x50","8\\n0"])
def test_invalid_text(text):
    with pytest.raises(ValueError):
        parse_port(text)

@pytest.mark.parametrize("value", [True,False,80,None,b"80",[],{}])
def test_invalid_type(value):
    with pytest.raises(TypeError):
        parse_port(value)
'''


def save(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as output:
        json.dump(value, output, indent=2, ensure_ascii=False)
        output.write("\n")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def source_snapshot() -> dict[str, str]:
    paths = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT, capture_output=True, check=True).stdout.split(b"\0")
    return {name: digest(ROOT / name) for raw in paths if raw
            for name in (os.fsdecode(raw),) if (ROOT / name).is_file()}


def toml(value: object) -> str:
    if isinstance(value, dict):
        return "{ " + ", ".join(json.dumps(key) + " = " + toml(item) for key, item in value.items()) + " }"
    return json.dumps(value, ensure_ascii=True)


def prepare_cli(root: Path) -> tuple[Path, dict]:
    try:
        import tomllib
    except ModuleNotFoundError:
        from pip._vendor import tomli as tomllib
    settings = tomllib.loads((Path.home() / ".codex/config.toml").read_text(encoding="utf-8"))
    provider = settings["model_provider"]
    provider_config = settings["model_providers"][provider]
    # This manual pilot deliberately supports only the configured bearer provider;
    # it must not silently switch accounts/providers or copy unrelated settings.
    if not provider_config.get("experimental_bearer_token") or provider_config.get("requires_openai_auth") is not False:
        raise RuntimeError("pilot_requires_existing_explicit_bearer_provider")
    executable = shutil.which("codex")
    if not executable:
        raise RuntimeError("installed_codex_missing")
    codex = Path(executable).resolve(strict=True)
    native_paths = tuple((codex.parent.parent / "node_modules/@openai").glob("codex-darwin-*/vendor/*/bin/codex"))
    if len(native_paths) != 1:
        raise RuntimeError("pilot_requires_unambiguous_installed_native_cli")
    native = native_paths[0].resolve(strict=True)
    identity = {"codex": str(codex), "cli_sha256": digest(codex), "model": settings["model"],
                "provider": provider, "reasoning": settings.get("model_reasoning_effort"),
                "native_binary": str(native), "native_sha256": digest(native),
                "provider_snapshot_sha256": hashlib.sha256(json.dumps(provider_config, sort_keys=True).encode()).hexdigest(),
                "version": subprocess.run([str(codex), "--version"], check=True, capture_output=True, text=True).stdout.strip()}
    private = root / "private"
    private.mkdir(mode=0o700)
    minimal = {key: settings[key] for key in ("model", "model_provider", "model_reasoning_effort") if key in settings}
    content = "\n".join(key + " = " + toml(value) for key, value in minimal.items()) + "\n"
    content += "[model_providers." + json.dumps(provider) + "]\n"
    content += "\n".join(key + " = " + toml(value) for key, value in provider_config.items()) + "\n"
    content += "[features]\nmulti_agent = false\nmemories = false\n"
    identity["runtime_config_sha256"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    paths = {}
    for stage in ("develop", "review"):
        runtime = private / stage
        runtime.mkdir(mode=0o700)
        for child in ("home", "scratch", "codex"):
            (runtime / child).mkdir(mode=0o700)
        config_path = runtime / "codex/config.toml"
        config_path.write_text(content, encoding="utf-8")
        config_path.chmod(0o600)
        paths[stage + "_runtime"] = str(runtime)
    config_path = private / "config.json"
    save(config_path, {**identity, **paths})
    return config_path, identity


def register(app, owner: str, repo: Path, config_path: Path, identity: dict, *, remote=None):
    fleet = app.extensions["fleet"]
    platform = fleet["platform_repository"]
    repository = fleet["repositories"]["workflows"]
    definition = coding_delivery_definition()
    fleet["services"]["workflows"].catalog = WorkflowTemplateCatalog((RegisteredWorkflowTemplate(owner, WORKSPACE, definition),))
    platform.upsert_workspace(owner, {"workspace_id": WORKSPACE, "root_path": str(repo)})
    for name in ("develop-chat", "review-chat", "test-chat"):
        platform.create_conversation(owner, name, title="Real CLI pilot", workspace_id=WORKSPACE)
    adapter = CommandReviewAdapter(CatalogRef("real-codex-review", 1),
        ReviewIdentity("independent-real-codex-reviewer", "model", identity["model"]),
        (sys.executable, str(BRIDGE), "review", str(config_path), digest(config_path)),
        launcher=(sys.executable, str(BRIDGE), "launch-review", str(config_path), digest(config_path), "{source}", "{control}", "{scratch}"),
        timeout_seconds=300)
    policy = CatalogRef("coding-delivery", 1)
    config_snapshot = json.dumps({"pilot_cli": identity, "pilot_bridge_sha256": digest(BRIDGE)},
                                 sort_keys=True, separators=(",", ":")).encode("utf-8")
    targets = (
        RegisteredRunTarget(owner, WORKSPACE, "cli-node", CatalogRef("real-codex-developer", 1), policy, "develop-chat", config_snapshot,
            LegacyTaskTarget("pilot-local", "codex", "pilot-repo"), source_repository_id="pilot-repo"),
        RegisteredRunTarget(owner, WORKSPACE, "review-node", adapter.spec.adapter_ref, policy, "review-chat", b"{}", review_adapter=adapter.spec),
        RegisteredRunTarget(owner, WORKSPACE, "test-node", CatalogRef("fixed-tests", 1), policy, "test-chat", b"{}",
            test_recipe=CatalogRef("coding-delivery-tests", 1)),
    )
    if remote is not None:
        return remote.register(app, owner, definition, targets, adapter)
    plan_digest = hashlib.sha256(definition.snapshot).hexdigest()
    plans = tuple(RegisteredWorkflowPlan(owner, WORKSPACE, plan_digest, key, target.adapter_ref, policy,
        target.target_key, 1, 0, 1) for key, target in zip(("develop", "review", "test"), targets))
    store = fleet["services"]["platform_artifacts"]
    repository.configure_hub_input_targets(tuple(item for target in targets if (item := target.hub_input_target()) is not None))
    delivery = fleet["services"]["workflow_coding_delivery"]
    delivery_repository = WorkflowDeliveryRepository(repository, delivery.task_repository)
    coordinator = WorkflowCoordinator(repository, worker_id="real-pilot-coordinator", clock=time.time, registrations=plans,
        executors=tuple((owner, WORKSPACE, target.target_key, RunStepExecutor(repository, target, clock=time.time)) for target in targets),
        dispatch_enabled=True, input_resolver=WorkflowArtifactInputResolver(repository, store))
    coordinator.artifact_input_service = fleet["services"].get("artifact_inputs")
    worker = LocalRunWorkerService(platform, RunEventService(platform), worker_id="real-pilot-worker", artifact_store=store,
        provider_factory=lambda profile: (_ for _ in ()).throw(RuntimeError("unregistered_provider_forbidden")),
        workflow_review_executor=WorkflowReviewExecutionService(delivery_repository, delivery, clock=time.time, adapters=(adapter,)),
        workflow_test_executor=WorkflowTestExecutionService(delivery_repository, delivery, clock=time.time,
            runner=FixedTestRunner(backend_factory=isolated_test_backend)))
    return coordinator, worker, adapter.spec.payload()


def run(root: Path, *, remote=None) -> dict:
    config_path, identity = prepare_cli(root)
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Disposable Workflow Pilot")
    git(repo, "config", "user.email", "workflow-pilot@example.test")
    for filename, text in (("TASK.md", TASK), ("test_ports.py", TESTS)):
        (repo / filename).write_text(text, encoding="utf-8")
    git(repo, "add", "TASK.md", "test_ports.py")
    git(repo, "commit", "-qm", "independent pilot requirements and acceptance cases")
    base = git(repo, "rev-parse", "HEAD")
    seed = {filename: digest(repo / filename) for filename in ("TASK.md", "test_ports.py")}
    save(root / "seed.json", {"base_commit": base, "sha256": seed})
    signing_key, public_key = generate_ed25519_keypair()
    credential, password = secrets.token_urlsafe(32), secrets.token_urlsafe(32) + "1!aA"
    encryption_key = secrets.token_bytes(32)
    save(root / "private/hub-access.json", {"email": "workflow-pilot@example.test", "password": password,
        "runner_credential": credential, "signing_key_b64": base64.b64encode(signing_key).decode(),
        "transcript_key_b64": base64.b64encode(encryption_key).decode()})
    hub_root = root / "hub"
    hub_root.mkdir()
    app = create_app(FleetConfig.from_root(hub_root, platform_enabled=True, platform_workflows_enabled=True,
        platform_workflow_dispatch_enabled=remote is not None,
        platform_worker_enabled=remote is not None,
        platform_capability_routing_enabled=remote is not None, platform_remote_execution_enabled=remote is not None,
        platform_artifact_transfer_enabled=remote is not None,
        serve_frontend=False, platform_command_signing_raw=signing_key,
        session_repositories_enabled=True, session_encryption_raw=encryption_key,
        runner_credentials={"pilot-local": credential}, project_whitelist={"pilot-local": ["pilot-repo"]},
        accounts={"enabled": True, "origin": "http://127.0.0.1", "registration": "invite"}),
        repositories={"observation": SimpleNamespace(read_current=lambda machine: {"reachable": machine == "pilot-local"})})
    accounts = app.extensions["accounts"]
    accounts.bootstrap_admin("workflow-pilot@example.test", password)
    owner = accounts.users()[0]["id"]
    fleet = app.extensions["fleet"]
    coordinator, worker, adapter = register(app, owner, repo, config_path, identity, remote=remote)
    save(root / "registration.json", {"workspace": WORKSPACE, "owner": owner,
        "cli": identity, "review": adapter, "base_commit": base, "template": {"id": "coding-delivery", "revision": 1},
        "bridge_sha256": digest(BRIDGE), "harness_sha256": digest(Path(__file__))})
    runner = load_agent_runner()
    bridge = fleet["services"]["legacy_task_bridge"]
    with hub_server(app) as origin:
        if remote is not None:
            remote.connect(origin, public_key)
        cfg = RunnerConfig(hub=origin, machine="pilot-local", credential=credential, runner_id="real-pilot-runner",
            projects={"pilot-repo": repo}, cache_dir=root / "runner-cache", heartbeat_interval_s=5,
            transport=Transport(mode="direct", attempts=1), agents={"codex": {
                "command": [sys.executable, str(BRIDGE), "develop", str(config_path), digest(config_path)], "timeout_s": 300}})
        client = CookieClient(origin)
        assert client.request("/api/accounts/login", {"email": "workflow-pilot@example.test", "password": password})[0] == 200
        create = {"template_id": "coding-delivery", "revision": 1, "workspace_id": WORKSPACE,
                  "input_refs": [], "client_token": "real-cli-pilot", "expected_policy_revision": 1}
        status, created = client.request("/api/platform/v1/workflows", create)
        assert status == 201, created
        workflow_id = created["workflow"]["workflow_id"]
        save(root / "created.json", created)
        assert client.request("/api/platform/v1/workflows", create)[1]["workflow"]["workflow_id"] == workflow_id
        executed = []
        deadline = time.monotonic() + 720
        iterations = 0
        try:
            while time.monotonic() < deadline:
                iterations += 1
                if remote is not None:
                    remote.advance()
                summary = asyncio.run(coordinator.tick(time.time()))
                assert not summary.failures, summary.failures
                bridge.process_once(owner)
                if runner.poll_once(cfg):
                    print("original Runner invocation finished", flush=True)
                    bridge.process_once(owner)
                result = worker.run_once(owner)
                if result is not None:
                    executed.append(result)
                    print("original worker result", result["state"], flush=True)
                    save(root / ("worker-result-" + str(len(executed)) + ".json"), result)
                status, body = client.request("/api/platform/v1/workflows/" + workflow_id)
                assert status == 200, body
                detail = body["workflow"]
                (root / "workflow-latest.json").write_text(json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8")
                if detail["delivery_state"] == "awaiting_acceptance" or detail["reason_code"] == "blocked_by_validation":
                    break
                if detail["execution_state"] in ("failed", "cancelled", "unknown"):
                    break
                if any(item.get("state") != "succeeded" for item in executed):
                    break
                time.sleep(1)
            else:
                raise TimeoutError("original_workflow_not_terminal_within_budget")
            downloads = root / "downloads"
            downloads.mkdir()
            manifests = []
            test_report = None
            junit_count = 0
            for step in detail["steps"]:
                for output in step["outputs"]:
                    raw = client.download(f"/api/platform/v1/artifacts/{output['artifact_id']}/content?workspace_id={WORKSPACE}")
                    assert len(raw) == output["size"] and hashlib.sha256(raw).hexdigest() == output["sha256"]
                    filename = step["step_key"] + "-" + output["output_key"]
                    (downloads / filename).write_bytes(raw)
                    manifests.append({"file": filename, **output})
                    if step["step_key"] == "test" and output["output_key"] == "test_report":
                        test_report = json.loads(raw)
                    if step["step_key"] == "test" and output["output_key"] == "junit":
                        junit = ET.fromstring(raw)
                        junit_count = len(list(junit.iter("testcase")))
                        assert junit_count == 31
                        assert not any(node.tag in ("failure", "error", "skipped") for node in junit.iter())
            save(root / "artifact-manifests.json", manifests)
            assert detail["delivery_state"] == "awaiting_acceptance", detail
            assert len(executed) == 2 and all(item["state"] == "succeeded" for item in executed)
            assert len({step["subject_digest"] for step in detail["steps"]}) == 1
            assert test_report and test_report["passed"] is True and test_report["environment"]["launcher_sha256"]
            # Validate the exact exported source, not the developer's working tree.
            import tarfile
            source_path = downloads / "develop-source_bundle"
            with tarfile.open(source_path) as archive:
                exported = {member.name: archive.extractfile(member).read() for member in archive.getmembers() if member.isfile()}
            assert set(exported) == {".fleet-source.json", "TASK.md", "test_ports.py", "ports.py"}, sorted(exported)
            metadata = json.loads(exported[".fleet-source.json"])
            assert {entry["path"] for entry in metadata["entries"]} == {"TASK.md", "test_ports.py", "ports.py"}
            assert metadata["base_commit"] == base and metadata["repository_id"] == "pilot-repo"
            assert all(hashlib.sha256(exported[name]).hexdigest() == expected for name, expected in seed.items())
            assert all(digest(repo / name) == expected for name, expected in seed.items())
            # Fresh HTTP session reads the same durable workflow after logout/login.
            assert client.request("/api/accounts/logout", {})[0] == 200
            assert client.request("/api/platform/v1/workflows/" + workflow_id)[0] == 401
            restored = CookieClient(origin)
            assert restored.request("/api/accounts/login", {"email": "workflow-pilot@example.test", "password": password})[0] == 200
            restored_detail = restored.request("/api/platform/v1/workflows/" + workflow_id)[1]["workflow"]
            assert restored_detail == detail
            assert restored.request("/api/accounts/logout", {})[0] == 200
            assert not runner.poll_once(cfg)
            remote_result = remote.verify(detail, test_report) if remote is not None else None
            return {"status": "automation_passed_awaiting_human", "workflow_id": workflow_id,
                "delivery_state": detail["delivery_state"], "human_acceptance": "not_performed",
                "delivery_digest": detail["delivery_digest"], "revision": detail["revision"],
                "subject_digest": detail["steps"][0]["subject_digest"], "junit_cases": junit_count,
                "cli": identity, "same_model_separate_sessions": True, "iterations": iterations,
                "artifact_count": len(manifests), "production_release_ready": False,
                **({"remote_test": remote_result} if remote_result is not None else {})}
        finally:
            client.request("/api/accounts/logout", {})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--run-live", action="store_true", help="explicitly authorize the two real model calls")
    parser.add_argument("--remote-test-config", type=Path, help="explicit authorized Linux SSH configuration; existing runtimes only")
    args = parser.parse_args()
    if not args.run_live:
        parser.error("--run-live is required; no model call was made")
    if sys.platform != "darwin":
        parser.error("this manual CLI pilot currently requires the installed macOS OS launcher")
    root = args.evidence_root.expanduser().resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.umask(0o077)
    started = time.time()
    snapshot = source_snapshot()
    save(root / "source-snapshot.json", {"head": git(ROOT, "rev-parse", "HEAD"),
         "branch": git(ROOT, "branch", "--show-current"), "files": snapshot})
    try:
        remote = None
        if args.remote_test_config is not None:
            from workflow_live_remote import RemotePilot

            remote = RemotePilot(root, args.remote_test_config)
        result = run(root, remote=remote)
    except BaseException as error:
        # Preserve all original state. The detailed exception is local evidence.
        save(root / "result.json", {"status": "failed", "error_type": type(error).__name__,
            "error": str(error)[:16000], "elapsed_seconds": time.time() - started,
            "human_acceptance": "not_performed", "production_release_ready": False})
        raise
    save(root / "result.json", {**result, "elapsed_seconds": time.time() - started})
    current = source_snapshot()
    save(root / "source-verification.json", {"files": len(snapshot),
        "changed": [name for name in snapshot.keys() | current.keys() if snapshot.get(name) != current.get(name)]})
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
