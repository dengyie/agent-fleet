"""Real receive descriptors, archives and journals; fixture launcher is not confinement evidence."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sys
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest

from hub.domain.artifact_receive import ArtifactReceiveError
from hub.domain.capability import BoundCapability, TargetBinding
from hub.domain.capability_execution import CapabilityExecutionContext
from fleet_core.control import generate_ed25519_keypair
from fleet_core.platform_command import PlatformCommand
from hub.domain.remote_workflow_test import REMOTE_TEST_ACTION, RemoteTestError, RemoteTestRequest, remote_test_command_id
from tools.platform.artifact_transfer import ArtifactReceiver, InputDirectory
from tools.platform.journal import NodeJournal
from tools.platform.node_client import NodeClient
from tools.platform.node_executor import NODE_TOOLS, NodeToolExecutor
from tools.platform.workflow_remote_test import NodeRemoteTestExecutor, decode_test_result
from tools.platform.workflow_source import SourceExportError, SourceExporter, extract_source_stream
from test_artifact_receive import ContentTransport
from test_workflow_source import git, repo_with_commit


def fixture_test_launcher() -> tuple[str, ...]:
    # Sandbox uses a private HOME, so make this test runner's dependencies
    # explicit even when pytest was installed in the operator's user site.
    dependencies = Path(pytest.__file__).resolve().parents[1]
    return ("/usr/bin/env", "PYTHONPATH=" + str(dependencies))


class FixtureExecutor(NodeRemoteTestExecutor):
    @property
    def available(self) -> bool:
        return bool(self.launcher and self.receiver.available)


class NodeFixture:
    def __init__(self, path: Path) -> None:
        self.private, self.public = generate_ed25519_keypair()
        repo, base, _, _ = repo_with_commit(path)
        (repo / "test_delivery.py").write_text("def test_delivery():\n    assert 6 * 7 == 42\n", encoding="utf-8")
        git(repo, "add", "test_delivery.py")
        git(repo, "commit", "-qm", "fixed test")
        from hub.platform.artifacts import ArtifactStore

        store = ArtifactStore(path / "artifacts")
        self.revision = asyncio.run(SourceExporter(repo, store).export("owner", "workspace", repository_id="repo",
            base_commit=base, head_commit=git(repo, "rev-parse", "HEAD"), tree_id=git(repo, "rev-parse", "HEAD^{tree}"))).revision
        self.raw = store.read("owner", "workspace", self.revision.source_bundle.artifact_id)
        self.root = path / "inputs"
        self.root.mkdir(mode=0o700)
        self.receiver = ArtifactReceiver(node_id="node-a", credential="node-a:secret", worker_id="worker", hub_url="https://hub.test",
            public_key=self.public, inputs={"source": InputDirectory(self.root)}, journal_path=path / "receives.db",
            transport=ContentTransport(self.raw), clock=lambda: 100)
        artifact = self.revision.source_bundle
        receive = PlatformCommand.create(command_id="receive-" + "c" * 64, target_node="node-a", resource_id="workspace",
            action="artifact.receive", retry_class="idempotent_with_key", grant_id="c" * 64, expires_at=500,
            arguments={"workflow_id": "workflow-a", "step_key": "test", "generation": 1, "ref_key": "source", "input_digest": "f" * 64,
                "artifact_id": artifact.artifact_id, "sha256": artifact.sha256, "size": artifact.size, "input_alias": "source", "suffix": ".tar"})
        self.receipt = self.receiver.receive(receive.signed(self.private).as_dict())
        self.binding = TargetBinding("node-a", 1, "a" * 64, "b" * 64, (BoundCapability("coding.test", 1, "d" * 64),),
            "f" * 64, "e" * 64, 0, 0)
        self.request = RemoteTestRequest("workflow-a", 1, "e" * 64, "f" * 64,
            hashlib.sha256(self.binding.snapshot).hexdigest(), self.revision, "d" * 64, self.receipt)
        self.executor = FixtureExecutor(self.receiver, launcher=fixture_test_launcher(), clock=lambda: 100, should_cancel=lambda: False)
        self.journal = NodeJournal(path / "commands.db")
        self.journal.init()

    def command(self, request: RemoteTestRequest | None = None) -> dict:
        request = request or self.request
        command = PlatformCommand.create(command_id=remote_test_command_id(request.dispatch_token), target_node="node-a",
            resource_id="workspace", action=REMOTE_TEST_ACTION, retry_class="manual_only", run_id="wf-run-" + request.dispatch_token,
            expires_at=500, arguments=request.payload())
        return CapabilityExecutionContext("coding.test", "fixed-tests", self.binding).bind(command).signed(self.private).as_dict()

    def client(self, executor=None) -> NodeClient:
        tool = NodeToolExecutor(None, workflow_test_executor=executor or self.executor, capability_prepare=lambda command: dict(command))
        return NodeClient(self.journal, executor=tool, clock=lambda: 100, node_id="node-a", public_key=self.public)


def test_node_executes_fixed_recipe_and_restart_returns_original_receipt(tmp_path: Path) -> None:
    fixture = NodeFixture(tmp_path)
    first = fixture.client().handle(fixture.command())
    assert first["status"] == "succeeded"
    result = decode_test_result(first["result"]["result"], revision=fixture.revision, subject_digest="d" * 64, node_id="node-a")
    assert result.passed and b"test_delivery" in result.junit
    assert json.loads(result.raw_report)["environment"]["recipe_argv_sha256"]
    fixture.executor.runner.run = lambda *args, **kwargs: pytest.fail("must not replay fixed tests")
    assert fixture.client().handle(fixture.command()) == first
    assert "workflow.test" not in NODE_TOOLS


def test_interrupted_command_is_unknown_and_never_dispatched(tmp_path: Path) -> None:
    fixture = NodeFixture(tmp_path)
    command = fixture.command()
    fixture.journal.begin(command["command_id"], now=99)
    fixture.executor.runner.run = lambda *args, **kwargs: pytest.fail("interrupted test must never replay")
    assert fixture.client().handle(command)["status"] == "unknown"
    assert fixture.client().handle(command)["status"] == "unknown"


@pytest.mark.parametrize("mutation", ["bytes", "generation", "digest", "workspace", "receipt", "inode", "journal", "signature"])
def test_original_receive_scope_and_bytes_are_required(tmp_path: Path, mutation: str) -> None:
    fixture = NodeFixture(tmp_path)
    command = fixture.command()
    if mutation == "bytes":
        (fixture.root / ("c" * 64 + ".tar")).write_bytes(b"corrupt")
    elif mutation == "generation":
        command = fixture.command(replace(fixture.request, generation=2))
    elif mutation == "digest":
        command = fixture.command(replace(fixture.request, input_digest="0" * 64))
    elif mutation == "workspace":
        command["resource_id"] = "other"
    elif mutation == "receipt":
        command = fixture.command(replace(fixture.request, receive_receipt=fixture.receipt | {"grant_id": "b" * 64}))
    elif mutation == "inode":
        original = fixture.root / ("c" * 64 + ".tar")
        original.rename(fixture.root / "preserved.tar")
        original.write_bytes(fixture.raw)
    elif mutation == "journal":
        with fixture.receiver.journal.transaction() as connection:
            connection.execute("UPDATE artifact_receives SET state='receiving'")
    else:
        command["signature"] = None
    fixture.executor.runner.run = lambda *args, **kwargs: pytest.fail("invalid source must not execute")
    assert fixture.client().handle(command)["status"] in {"failed", "rejected"}


def test_missing_launcher_and_unbound_action_fail_closed(tmp_path: Path) -> None:
    fixture = NodeFixture(tmp_path)
    unavailable = FixtureExecutor(fixture.receiver, launcher=(), clock=lambda: 100, should_cancel=lambda: False)
    assert fixture.client(unavailable).handle(fixture.command())["result"]["error_code"] == "capability_unavailable"
    command = fixture.command()
    command["arguments"].pop("_fleet_capability")
    assert NodeToolExecutor(None, workflow_test_executor=fixture.executor)(command)["error_code"] == "capability_unavailable"
    production = NodeRemoteTestExecutor(fixture.receiver, launcher=("/usr/bin/env",), clock=lambda: 100, should_cancel=lambda: False)
    if sys.platform != "linux":
        assert not production.available


def test_extraction_validates_revision_and_rejects_cancelled_checkout(tmp_path: Path) -> None:
    fixture = NodeFixture(tmp_path)
    with pytest.raises(Exception, match="source_revision_mismatch"):
        extract_source_stream(io.BytesIO(fixture.raw), tmp_path / "wrong", expected=replace(fixture.revision, head_commit="0" * 40),
            should_cancel=lambda: False)
    with pytest.raises(Exception, match="source_cancelled"):
        extract_source_stream(io.BytesIO(fixture.raw), tmp_path / "cancelled", expected=fixture.revision, should_cancel=lambda: True)
    assert not (tmp_path / "wrong").exists() and not (tmp_path / "cancelled").exists()


@pytest.mark.parametrize("mutation", ["path", "symlink", "duplicate", "bytes", "trailing"])
def test_stream_extraction_rejects_hostile_archive_without_publishing(tmp_path: Path, mutation: str) -> None:
    fixture = NodeFixture(tmp_path)
    raw = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(fixture.raw), mode="r:") as source, tarfile.open(fileobj=raw, mode="w") as output:
        for index, member in enumerate(source):
            contents = source.extractfile(member).read()
            if index == 1 and mutation == "path":
                member.name = "../escape.py"
            if index == 1 and mutation == "symlink":
                member.type, member.linkname, member.size = tarfile.SYMTYPE, "/tmp/escape.py", 0
            if index == 1 and mutation == "bytes":
                contents = b"x" * len(contents)
            output.addfile(member, io.BytesIO(contents) if member.isfile() else None)
            if index == 1 and mutation == "duplicate":
                output.addfile(member, io.BytesIO(contents))
    changed = raw.getvalue() + (b"nonzero trailing bytes" if mutation == "trailing" else b"")
    destination = tmp_path / "hostile"
    with pytest.raises(SourceExportError):
        extract_source_stream(io.BytesIO(changed), destination, expected=fixture.revision, should_cancel=lambda: False)
    assert not destination.exists() and not (tmp_path / "escape.py").exists()


def test_real_node_capability_preflight_strips_context_only_after_validation(tmp_path: Path) -> None:
    from hub.domain.workflow_contract_types import CatalogRef
    from tools.platform.node_capabilities import LocalCapability, NodeCapabilityConfig, NodeCapabilityFacts, NodeCapabilityService
    from test_capability_router import definition

    fixture = NodeFixture(tmp_path)
    registered = definition(capability_id="coding.test", adapter_id="fixed-tests", retry_class="manual_only")
    config = NodeCapabilityConfig(1, (LocalCapability(registered, (REMOTE_TEST_ACTION,)),), (CatalogRef("coding.test", 1),))
    service = NodeCapabilityService(config, node_id="node-a", observer=lambda: NodeCapabilityFacts(
        "linux", "x86_64", (), 1000, 2000), available_actions=lambda: frozenset({REMOTE_TEST_ACTION}))
    offer = service.offer(now=100)
    fixture.binding = replace(fixture.binding, offer_digest=offer.config_digest,
        capabilities=(BoundCapability("coding.test", 1, registered.digest),))
    fixture.request = replace(fixture.request, binding_digest=hashlib.sha256(fixture.binding.snapshot).hexdigest())
    tool = NodeToolExecutor(None, workflow_test_executor=fixture.executor, capability_prepare=lambda command: service.prepare(command, now=100))
    client = NodeClient(fixture.journal, executor=tool, clock=lambda: 100, node_id="node-a", public_key=fixture.public)
    assert client.handle(fixture.command())["status"] == "succeeded"
    assert "_fleet_capability" in fixture.command()["arguments"]


def test_runtime_requires_explicit_gate_and_omits_unavailable_offer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hub.domain.workflow_contract_types import CatalogRef
    from tools.platform.node_capabilities import LocalCapability, NodeCapabilityConfig, NodeCapabilityFacts
    from tools.platform.node_runtime import NodeRuntime, NodeRuntimeConfig
    from test_capability_router import definition

    fixture = NodeFixture(tmp_path)
    registered = definition(capability_id="coding.test", adapter_id="fixed-tests", retry_class="manual_only")
    config = NodeRuntimeConfig(workspace_id="workspace", node_id="node-a", credential="node-a:secret", workspace_root=tmp_path / "runtime-workspace",
        journal_path=tmp_path / "runtime.db", hub_url="https://hub.test", public_key=fixture.public,
        capabilities=frozenset({"artifact.receive", REMOTE_TEST_ACTION}), artifact_inputs=(("source", InputDirectory(fixture.root)),),
        sandbox_launcher=("/usr/bin/env",), capability_config=NodeCapabilityConfig(1,
            (LocalCapability(registered, (REMOTE_TEST_ACTION,)),), (CatalogRef("coding.test", 1),)))
    node = NodeRuntime(config, clock=lambda: 100, capability_observer=lambda: NodeCapabilityFacts("linux", "x86_64", (), 1000, 2000))
    try:
        assert REMOTE_TEST_ACTION not in node.manifest()["capabilities"]
        assert node.capability_service.offer(now=100).capabilities == ()
    finally:
        node.close_all()
    for change in ({"sandbox_launcher": ()}, {"capability_config": None}, {"capabilities": frozenset({REMOTE_TEST_ACTION})}):
        with pytest.raises(ValueError):
            replace(config, workflow_test_enabled=True, **change)
    monkeypatch.setattr(NodeRemoteTestExecutor, "available", property(lambda self: False))
    node = NodeRuntime(replace(config, workflow_test_enabled=True), clock=lambda: 100,
        capability_observer=lambda: NodeCapabilityFacts("linux", "x86_64", (), 1000, 2000))
    try:
        assert REMOTE_TEST_ACTION not in node.manifest()["capabilities"]
        assert node.capability_service.offer(now=100).capabilities == ()
    finally:
        node.close_all()
