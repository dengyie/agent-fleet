"""Native code-package inputs use an explicit trusted target and a read-only source view."""
from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hub.application.workflow_dispatch import WorkflowDispatchError
from hub.domain.workflow_identity import canonical_json
from hub.domain.workflow_errors import WorkflowValidationError
from hub.domain.workflow_contract import parse_definition, resolve_contract
from hub.domain.workflow_contract_types import AuthorizedInputs, CatalogRef, InputBinding
from hub.infrastructure.workflow_run_executor import RunStepExecutor
from hub.platform.workflow_source import SourceExporter
from fleet_core.workflow.review_protocol import ReviewAdapterSpec, ReviewIdentity
from test_workflow_native_text_inputs import make_text
from test_workflow_repository import OWNER, registered


def test_native_code_input_support_is_explicit_and_private(tmp_path: Path) -> None:
    setup = make_text(tmp_path, inline=False)

    assert setup.target.inline_code_inputs is False
    enabled = replace(setup.target, inline_code_inputs=True)
    assert enabled.inline_code_inputs is True

    for invalid in (None, 0, 1, "true"):
        with pytest.raises(ValueError):
            replace(setup.target, inline_code_inputs=invalid)


@pytest.mark.parametrize("change", ["remote", "legacy_memory", "cli", "review", "test", "capability"])
def test_native_code_input_rejects_unsupported_target_before_artifact_io(
        tmp_path: Path, change: str) -> None:
    from hub.application.workflow_text_inputs import WorkflowTextInputs

    setup = make_text(tmp_path, inline=False)
    from hub.domain.workflow_json import decode_snapshot
    config = decode_snapshot(setup.target.config_snapshot)
    fields: dict[str, object] = {"inline_code_inputs": True}
    if change == "remote":
        config["execution_node_id"] = "remote-node"
    elif change == "legacy_memory":
        config["memory_context"] = {"enabled": True}
    elif change == "cli":
        from hub.infrastructure.workflow_run_executor import LegacyTaskTarget
        fields["legacy_task"] = LegacyTaskTarget("node-a", "codex", "project-a")
    elif change == "review":
        fields["review_adapter"] = ReviewAdapterSpec(setup.target.adapter_ref,
            ReviewIdentity("review-service", "service"), "a" * 64)
    elif change == "test":
        from hub.domain.workflow_contract_types import CatalogRef
        fields["test_recipe"] = CatalogRef("coding-delivery-tests", 1)
    else:
        config["workflow_capability"] = {"binding_digest": "a" * 64}

    with pytest.raises((ValueError, WorkflowDispatchError)):
        target = replace(setup.target, config_snapshot=canonical_json(config), **fields)
        executor = RunStepExecutor(setup.workflows, target, clock=setup.clock,
            text_inputs=WorkflowTextInputs(setup.artifacts))
        asyncio.run(executor.create_or_lookup("a" * 64, setup.resolved.digest, setup.resolved))

    with setup.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
        assert connection.execute("SELECT child_request_id FROM workflow_budget_reservations").fetchone()[0] is None


def test_code_input_tool_names_are_not_offered_without_trusted_target() -> None:
    from hub.platform.tool_broker import ToolBroker

    names = {item["name"] for item in ToolBroker.tool_definitions()}
    assert "workflow.input.list" not in names
    assert "workflow.input.read" not in names


def test_native_code_input_requires_opt_in_before_artifact_io(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hub.application.workflow_code_inputs import WorkflowCodeInputs

    setup, resolved, _revision = _resolved_code_source(tmp_path)
    monkeypatch.setattr(setup.artifacts, "get", lambda *_args: pytest.fail("disabled input reached artifact storage"))
    executor = RunStepExecutor(setup.workflows, setup.target, clock=setup.clock,
        code_inputs=WorkflowCodeInputs(setup.artifacts))
    with pytest.raises(WorkflowDispatchError, match="artifact_input_unsupported"):
        asyncio.run(executor.create_or_lookup("a" * 64, resolved.digest, resolved))
    with setup.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


@pytest.mark.parametrize("consumer", ["review", "fixed_test", "remote_test"])
def test_specialized_source_consumers_keep_original_contract_without_native_inline_mode(tmp_path: Path, consumer: str) -> None:
    from hub.domain.workflow_contract import parse_resolved

    if consumer == "review":
        from test_workflow_review_execution import ReviewFixture
        fixture = ReviewFixture(tmp_path)
    elif consumer == "fixed_test":
        from test_workflow_test_execution import ExecutionFixture
        fixture = ExecutionFixture(tmp_path)
    else:
        from test_remote_workflow_test_execution import RemoteExecutionFixture
        fixture = RemoteExecutionFixture(tmp_path)
    assert fixture.target.inline_code_inputs is False
    observation = fixture.dispatch()
    run = fixture.platform.get_run(OWNER, observation.binding.run_id)
    config = run["config_snapshot"]
    assert not {"workflow_code_mode", "workflow_text_mode", "workflow_memory"}.intersection(config)
    with fixture.repository.transaction(write=False) as connection:
        resolved = parse_resolved(bytes(connection.execute(
            "SELECT s.resolved_snapshot FROM workflow_dispatches d JOIN workflow_steps s "
            "USING(owner_id,workflow_id,step_key,generation) WHERE d.token=?", (observation.binding.token,)).fetchone()[0]))
    assert asyncio.run(fixture.executor.lookup(observation.binding.token,
        resolved.digest, resolved)) == observation.binding


def test_wire_code_tool_without_private_view_is_not_allowed() -> None:
    from hub.platform.resource_lease import ResourceLeaseManager
    from hub.platform.tool_broker import ToolBroker

    leases = ResourceLeaseManager(clock=lambda: 10)
    lease = leases.acquire("workspace-a", OWNER)
    broker = ToolBroker(None, leases, resource_id="workspace-a", allowed_tools={"workflow.input.read"})
    receipt = broker.execute(command_id="missing-view", tool="workflow.input.read",
        arguments={"ref_key": "source", "path": "main.py"}, owner_id=OWNER, epoch=lease["epoch"])
    assert (receipt.state, receipt.error_code) == ("failed", "tool_not_allowed")


def _resolved_code_source(tmp_path: Path, *, source_text: str = "🥭完整源码\n", mixed: bool = False):
    setup = make_text(tmp_path, inline=False)
    repository = tmp_path / "source-repository"
    repository.mkdir()
    import subprocess

    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=repository, check=True,
            capture_output=True, text=True).stdout.strip()

    git("init", "-q")
    git("config", "user.name", "Workflow Test")
    git("config", "user.email", "workflow@example.test")
    source = repository / "src" / "main.py"
    source.parent.mkdir()
    source.write_text(source_text, encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "source")
    head, tree = git("rev-parse", "HEAD"), git("rev-parse", "HEAD^{tree}")
    exported = asyncio.run(SourceExporter(repository, setup.artifacts).export(
        OWNER, setup.resolved.workspace_id, repository_id="source-repo", base_commit=head,
        head_commit=head, tree_id=tree))

    value = json.loads(registered().snapshot)
    value["steps"][0]["kind"] = "native_run"
    value["steps"][0]["contract"]["input_refs"] = [{"ref_key": "source", "kind": "code_revision",
        "repository_id": exported.revision.repository_id, "base_commit": exported.revision.base_commit,
        "head_commit": exported.revision.head_commit, "tree_id": exported.revision.tree_id,
        "source_bundle": {"artifact_id": exported.revision.source_bundle.artifact_id,
            "sha256": exported.revision.source_bundle.sha256, "size": exported.revision.source_bundle.size}}]
    bindings = [InputBinding("source", OWNER, setup.resolved.workspace_id, exported.revision)]
    if mixed:
        text_reference = setup.resolved.contract.input_refs[0].reference
        value["steps"][0]["contract"]["input_refs"].append({"ref_key": "text", "kind": "artifact",
            "artifact_id": text_reference.artifact_id, "sha256": text_reference.sha256, "size": text_reference.size})
        bindings.append(InputBinding("text", OWNER, setup.resolved.workspace_id, text_reference))
    definition = parse_definition(canonical_json(value))
    resolved = resolve_contract(definition, AuthorizedInputs(OWNER, setup.resolved.workspace_id,
        "develop", CatalogRef("native", 1), tuple(bindings)))
    return setup, resolved, exported.revision


def test_private_code_view_lists_and_reads_verified_source_with_cleanup(tmp_path: Path) -> None:
    from hub.application.workflow_code_inputs import WorkflowCodeInputs
    from fleet_core.workflow.code_inputs import code_revision_inputs
    from hub.domain.workflow_native_memory import build_native_memory, parse_native_memory

    setup, resolved, revision = _resolved_code_source(tmp_path)
    memory = build_native_memory(resolved, (), codes=code_revision_inputs(resolved))
    assert json.loads(memory.snapshot)["schema_version"] == 4
    assert revision.source_bundle.sha256 in memory.snapshot.decode("utf-8")
    assert "完整源码" not in memory.snapshot.decode("utf-8")
    assert parse_native_memory(json.loads(memory.snapshot), resolved) == memory
    roots: list[Path] = []

    class TemporaryRoot:
        def __init__(self) -> None:
            self.name = tmp_path / "private-source-view"
            roots.append(self.name)

        def cleanup(self) -> None:
            import shutil
            shutil.rmtree(self.name, ignore_errors=True)

    views = WorkflowCodeInputs(setup.artifacts, root_factory=TemporaryRoot)
    with views.open_view(resolved, code_revision_inputs(resolved), lambda: False) as view:
        page = view.list("source")
        assert page == {"entries": [{"path": "src", "kind": "directory"}], "next_offset": None}
        assert view.list("source", "src")["entries"] == [{"path": "src/main.py", "kind": "file"}]
        value = view.read("source", "src/main.py")
        assert value["text"] == "🥭完整源码\n"
        assert value["next_offset"] is None
        assert revision.head_commit not in str(value)
        first = view.read("source", "src/main.py", max_bytes=4)
        assert (first["text"], first["next_offset"]) == ("🥭", 4)
        second = view.read("source", "src/main.py", offset=4, max_bytes=4)
        assert second["text"] == "完"
        with pytest.raises(WorkflowValidationError, match="invalid_code_input_offset"):
            view.read("source", "src/main.py", offset=1)
        private_root = roots[-1]
        assert private_root.exists()
    assert not private_root.exists()
    with pytest.raises(RuntimeError, match="consumer failed"):
        with views.open_view(resolved, code_revision_inputs(resolved), lambda: False):
            private_root = roots[-1]
            raise RuntimeError("consumer failed")
    assert not private_root.exists()


def test_private_code_view_rejects_wrong_scope_and_cleans_up_on_cancel(tmp_path: Path) -> None:
    from hub.application.workflow_code_inputs import WorkflowCodeInputs
    from fleet_core.workflow.code_inputs import code_revision_inputs

    setup, resolved, _revision = _resolved_code_source(tmp_path)
    references = code_revision_inputs(resolved)
    wrong_owner = replace(resolved, owner_id="other-owner")
    with pytest.raises(WorkflowValidationError, match="code_input_forbidden"):
        with WorkflowCodeInputs(setup.artifacts).open_view(wrong_owner, references, lambda: False):
            raise AssertionError("wrong owner opened the package")
    with pytest.raises(WorkflowValidationError, match="source_cancelled"):
        with WorkflowCodeInputs(setup.artifacts).open_view(resolved, references, lambda: True):
            raise AssertionError("cancelled open returned a source view")


@pytest.mark.parametrize("field", ["owner_id", "workspace_id", "artifact_id", "sha256", "size", "content_type"])
def test_code_input_manifest_must_match_every_frozen_field_before_archive_io(tmp_path: Path, field: str) -> None:
    from hub.application.workflow_code_inputs import WorkflowCodeInputs
    from fleet_core.workflow.code_inputs import code_revision_inputs

    setup, resolved, _revision = _resolved_code_source(tmp_path)

    class AlteredManifest:
        def get(self, owner_id, workspace_id, artifact_id):
            value = setup.artifacts.get(owner_id, workspace_id, artifact_id)
            value = dict(value)
            value[field] = "altered" if type(value[field]) is str else value[field] + 1
            return value

        def open_verified(self, *_args):
            raise AssertionError("invalid manifest must be rejected before opening the archive")

    with pytest.raises(WorkflowValidationError, match="code_input_forbidden"):
        with WorkflowCodeInputs(AlteredManifest()).open_view(
                resolved, code_revision_inputs(resolved), lambda: False):
            raise AssertionError("altered manifest was accepted")


def test_code_input_read_rejects_invalid_utf8_and_unsafe_paths(tmp_path: Path) -> None:
    from hub.application.workflow_code_inputs import WorkflowCodeView

    root = tmp_path / "source"
    root.mkdir()
    (root / "invalid.py").write_bytes(b"valid\xffsource")
    view = WorkflowCodeView({"source": root})
    with pytest.raises(WorkflowValidationError, match="code_input_invalid_utf8"):
        view.read("source", "invalid.py")
    with pytest.raises(WorkflowValidationError, match="invalid_code_input_path"):
        view.read("source", "../outside.py")


def test_code_input_limits_bound_reference_count_and_distinct_bundle_bytes(tmp_path: Path) -> None:
    from fleet_core.workflow.code_inputs import MAX_CODE_BYTES, code_revision_inputs
    from fleet_core.workflow.contract_types import NamedReference

    _setup, resolved, revision = _resolved_code_source(tmp_path)
    excessive_count = replace(resolved, contract=replace(resolved.contract, input_refs=tuple(
        NamedReference(f"source-{index}", revision) for index in range(17))))
    with pytest.raises(WorkflowValidationError, match="too_many_code_inputs"):
        code_revision_inputs(excessive_count)

    first = replace(revision, source_bundle=replace(revision.source_bundle, size=MAX_CODE_BYTES // 2 + 1))
    second = replace(revision, head_commit="a" * len(revision.head_commit),
        tree_id="b" * len(revision.tree_id), source_bundle=replace(revision.source_bundle,
            artifact_id="second-source-bundle", size=MAX_CODE_BYTES // 2 + 1))
    excessive_bytes = replace(resolved, contract=replace(resolved.contract, input_refs=(
        NamedReference("source-a", first), NamedReference("source-b", second))))
    with pytest.raises(WorkflowValidationError, match="code_inputs_too_large"):
        code_revision_inputs(excessive_bytes)


@pytest.mark.parametrize("cached_manifest", [False, True])
def test_repeated_source_revision_shares_budget_and_one_extraction(
        tmp_path: Path, cached_manifest: bool) -> None:
    from hub.application.workflow_code_inputs import WorkflowCodeInputs
    from fleet_core.workflow.code_inputs import CodeRevisionInput, MAX_CODE_BYTES

    setup, resolved, revision = _resolved_code_source(tmp_path, source_text="x" * 700000)
    references = tuple(CodeRevisionInput(f"source-{index}", revision) for index in range(16))
    manifest = setup.artifacts.get(OWNER, resolved.workspace_id, revision.source_bundle.artifact_id)
    assert revision.source_bundle.size < MAX_CODE_BYTES < revision.source_bundle.size * len(references)
    opens: list[object] = []

    class RepeatedArtifacts:
        def get(self, *_args):
            return manifest if cached_manifest else dict(manifest)

        def open_verified(self, *args):
            handle = setup.artifacts.open_verified(*args)
            opens.append(handle)
            return handle

    with WorkflowCodeInputs(RepeatedArtifacts()).open_view_for_scope(
            OWNER, resolved.workspace_id, references, lambda: False) as view:
        for reference in references:
            assert view.read(reference.ref_key, "src/main.py", max_bytes=4)["text"] == "xxxx"
        assert len(set(view._roots.values())) == 1
    assert len(opens) == 1 and opens[0].closed


def test_private_code_view_rejects_tampered_archive_tail(tmp_path: Path) -> None:
    from io import BytesIO
    from hub.application.workflow_code_inputs import WorkflowCodeInputs
    from fleet_core.workflow.code_inputs import code_revision_inputs

    setup, resolved, revision = _resolved_code_source(tmp_path)

    class CorruptedArchive:
        def get(self, owner_id, workspace_id, artifact_id):
            return setup.artifacts.get(owner_id, workspace_id, artifact_id)

        def open_verified(self, owner_id, workspace_id, artifact_id):
            return BytesIO(setup.artifacts.read(owner_id, workspace_id, artifact_id) + b"tampered")

    with pytest.raises(WorkflowValidationError, match="unsupported_source_entry"):
        with WorkflowCodeInputs(CorruptedArchive()).open_view(
                resolved, code_revision_inputs(resolved), lambda: False):
            raise AssertionError(f"tampered bundle {revision.source_bundle.artifact_id} was exposed")


def test_code_input_aggregate_extracted_file_limit_cleans_private_root(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hub.application import workflow_code_inputs as code_input_module
    from hub.application.workflow_code_inputs import WorkflowCodeInputs
    from fleet_core.workflow.code_inputs import MAX_CODE_FILES, code_revision_inputs
    from fleet_core.workflow.contract_types import NamedReference

    setup, resolved, revision = _resolved_code_source(tmp_path)
    archive = tmp_path / "duplicate-source.tar"
    archive.write_bytes(setup.artifacts.read(OWNER, resolved.workspace_id, revision.source_bundle.artifact_id))
    second_manifest = setup.artifacts.put_file(OWNER, resolved.workspace_id, archive,
        name="duplicate-source.tar", content_type="application/x-tar")
    second = replace(revision, head_commit="a" * len(revision.head_commit),
        tree_id="b" * len(revision.tree_id), source_bundle=type(revision.source_bundle)(
            second_manifest["artifact_id"], second_manifest["sha256"], second_manifest["size"]))
    multi_resolved = replace(resolved, contract=replace(resolved.contract, input_refs=(
        NamedReference("source-a", revision), NamedReference("source-b", second))))
    references = code_revision_inputs(multi_resolved)
    opened: list[str] = []
    roots: list[Path] = []

    class TemporaryRoot:
        def __init__(self) -> None:
            self.name = tmp_path / "bounded-code-view"
            roots.append(self.name)

        def cleanup(self) -> None:
            import shutil
            shutil.rmtree(self.name, ignore_errors=True)

    def many_files(_handle, destination, *, expected, should_cancel):
        opened.append(expected.head_commit)
        Path(destination).mkdir(parents=True, exist_ok=True)
        return tuple(f"file-{index}" for index in range(MAX_CODE_FILES // 2 + 1))

    monkeypatch.setattr(code_input_module, "extract_source_stream", many_files)
    with pytest.raises(WorkflowValidationError, match="code_inputs_too_large"):
        with WorkflowCodeInputs(setup.artifacts, root_factory=TemporaryRoot).open_view(
                multi_resolved, references, lambda: False):
            raise AssertionError("aggregate file limit was not enforced")
    assert len(opened) == 2 and not roots[-1].exists()


def _dispatch_code_source(tmp_path: Path, *, mixed: bool = False):
    from hub.application.workflow_code_inputs import WorkflowCodeInputs
    from hub.application.workflow_dispatch import WorkflowDispatchService
    from hub.application.workflow_text_inputs import WorkflowTextInputs

    setup, resolved, revision = _resolved_code_source(tmp_path, mixed=mixed)
    code_target = replace(setup.target, inline_code_inputs=True, inline_text_inputs=mixed)
    workflow_id = "code-workflow"
    base_definition = json.loads(registered().snapshot)
    definition = parse_definition(json.dumps({
        **base_definition,
        "steps": [{**base_definition["steps"][0],
            "kind": "native_run", "contract": json.loads(resolved.contract.snapshot)}],
    }, ensure_ascii=False).encode("utf-8"))
    setup.workflows.create_or_get(OWNER, setup.resolved.workspace_id, workflow_id,
        client_token="native-code", definition=definition, now=104)
    claims = setup.workflows.claim_due(OWNER, worker_id="coordinator-code", now=104, limit=8, lease_seconds=60)
    claim = next(item for item in claims if item.workflow_id == workflow_id)
    ready = setup.workflows.freeze_inputs(claim, step_key="develop", generation=1,
        resolved=resolved, expected_revision=1, now=105)
    setup.workflows.admit_step(claim, step_key="develop", generation=1, expected_revision=ready.revision,
        target_key=code_target.target_key, capacity=2, reserve_tokens=100, reserve_requests=2, now=106)
    code_inputs = WorkflowCodeInputs(setup.artifacts)
    executor = RunStepExecutor(setup.workflows, code_target, clock=setup.clock, code_inputs=code_inputs,
        text_inputs=WorkflowTextInputs(setup.artifacts) if mixed else None)
    binding = asyncio.run(WorkflowDispatchService(setup.workflows, executor, clock=setup.clock).dispatch(
        claim, step_key="develop", generation=1, expected_revision=setup.workflows.get(OWNER, workflow_id).revision)).binding
    return setup, resolved, revision, code_target, code_inputs, binding


def test_native_mixed_text_and_code_inputs_reach_original_provider(tmp_path: Path) -> None:
    from hub.application.workflow_text_inputs import WorkflowTextInputs
    from hub.infrastructure.workflow_native_memory_repository import WorkflowNativeMemoryRepository
    from test_workflow_native_text_inputs import TEXT

    setup, resolved, _revision, target, code_inputs, binding = _dispatch_code_source(tmp_path, mixed=True)
    captured: list[Any] = []
    worker = setup.worker(captured)
    worker.workflow_memory_guard = WorkflowNativeMemoryRepository(setup.workflows,
        code_inputs=code_inputs, text_inputs=WorkflowTextInputs(setup.artifacts), clock=setup.clock)
    worker.workflow_code_inputs = code_inputs
    outcome = worker.run_once(OWNER)
    assert outcome["run_id"] == binding.run_id and outcome["state"] == "succeeded"
    assert len(captured) == 1
    message = json.loads(captured[0][0]["content"].split("\n", 1)[1])
    assert message["text_inputs"][0]["text"] == TEXT and "🥭完整源码" not in captured[0][0]["content"]
    config = setup.platform.get_run(OWNER, binding.run_id)["config_snapshot"]
    assert config["workflow_memory"]["schema_version"] == 4
    assert config["workflow_memory"]["text_inputs"][0]["text"] == TEXT
    assert config["workflow_memory"]["code_inputs"][0]["ref_key"] == "source"
    retry = RunStepExecutor(setup.workflows, target, clock=setup.clock)
    assert asyncio.run(retry.create_or_lookup(binding.token, resolved.digest, resolved)) == binding


@pytest.mark.parametrize("damage", ["revision", "hash", "mode", "missing_mode", "missing_snapshot", "reader", "reference"])
def test_code_input_last_history_page_rejects_corruption_before_execution(tmp_path: Path, damage: str) -> None:
    from hub.composition.workflows import assert_workflow_visibility
    from hub.infrastructure.platform_backup import BackupError, PlatformBackupService
    from hub.infrastructure.workflow_native_memory_repository import (
        WorkflowNativeMemoryRepository, validate_native_memory_runs,
    )
    from hub.infrastructure.workflow_repository import WorkflowRepositoryError
    from test_workflow_repository import legacy_budget_links

    setup, _resolved, _revision, _target, code_inputs, binding = _dispatch_code_source(tmp_path)
    with setup.workflows.transaction() as connection:
        connection.execute("UPDATE workflow_dispatches SET rowid=9223372036854775807 WHERE binding_run_id=?",
            (binding.run_id,))
    legacy_budget_links(setup.workflows, tuple(range(-128, 0)))
    with setup.workflows.transaction() as connection:
        validate_native_memory_runs(connection)
        if damage == "reader":
            connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=11")
        elif damage == "reference":
            connection.execute("DELETE FROM workflow_artifact_refs WHERE workflow_id='code-workflow'")
        else:
            config = setup.platform.get_run(OWNER, binding.run_id)["config_snapshot"]
            if damage == "revision":
                config["workflow_memory"]["code_inputs"][0]["head_commit"] = "0" * 40
            elif damage == "hash":
                config["workflow_memory"]["code_inputs"][0]["sha256"] = "0" * 64
            elif damage == "mode":
                config["workflow_code_mode"] = {"revision": True}
            elif damage == "missing_mode":
                config.pop("workflow_code_mode")
            elif damage == "missing_snapshot":
                config.pop("workflow_memory")
            connection.execute("UPDATE runs SET config_snapshot=? WHERE run_id=?",
                (canonical_json(config).decode("utf-8"), binding.run_id))
    with setup.workflows.transaction(write=False) as connection:
        with pytest.raises(WorkflowValidationError):
            validate_native_memory_runs(connection)
        unlinked_before = tuple(tuple(row) for row in connection.execute(
            "SELECT owner_id,workflow_id,step_key,generation FROM workflow_budget_reservations "
            "WHERE child_request_id IS NULL ORDER BY workflow_id LIMIT 130"))
        assert len(unlinked_before) == 129  # 128 legacy rows plus the original unused text fixture.
    with pytest.raises(WorkflowRepositoryError):
        setup.workflows.init()
    with setup.workflows.transaction(write=False) as connection:
        assert tuple(tuple(row) for row in connection.execute(
            "SELECT owner_id,workflow_id,step_key,generation FROM workflow_budget_reservations "
            "WHERE child_request_id IS NULL ORDER BY workflow_id LIMIT 130")) == unlinked_before
    with pytest.raises(WorkflowRepositoryError):
        assert_workflow_visibility(setup.workflows.db_path, enabled=False)
    with pytest.raises(BackupError):
        PlatformBackupService().create_backup(setup.workflows.db_path, setup.artifacts.root, tmp_path / "corrupt-code-backup")
    captured: list[Any] = []
    worker = setup.worker(captured)
    worker.workflow_memory_guard = WorkflowNativeMemoryRepository(setup.workflows,
        code_inputs=code_inputs, clock=setup.clock)
    worker.workflow_code_inputs = code_inputs
    with setup.platform._connect() as connection:
        connection.execute("UPDATE runs SET created_at=0 WHERE run_id=?", (binding.run_id,))
    result = worker.run_once(OWNER)
    assert result["run_id"] == binding.run_id and result["state"] == "failed"
    assert captured == []


@pytest.mark.parametrize("phase", ["provider_pending", "tool_pending"])
def test_code_input_uncertain_checkpoint_does_not_replay_provider_or_tools(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    from hub.infrastructure.workflow_native_memory_repository import WorkflowNativeMemoryRepository
    from hub.platform.providers.base import ModelResponse

    setup, _resolved, _revision, _target, code_inputs, binding = _dispatch_code_source(tmp_path)
    calls: list[str] = []

    class SimulatedExit(BaseException):
        pass

    class Provider:
        def complete(self, messages, tools, **kwargs):
            calls.append("provider")
            return ModelResponse(kind="tool_call", tool="workflow.input.read",
                arguments={"ref_key": "source", "path": "src/main.py"})

    def make_worker():
        worker = setup.worker([])
        worker.provider_factory = lambda _profile: Provider()
        worker.workflow_memory_guard = WorkflowNativeMemoryRepository(setup.workflows,
            code_inputs=code_inputs, clock=setup.clock)
        worker.workflow_code_inputs = code_inputs
        return worker

    original_save = setup.platform.save_runtime_checkpoint

    def interrupt(*args, **kwargs):
        result = original_save(*args, **kwargs)
        if args[2]["phase"] == phase:
            raise SimulatedExit()
        return result

    monkeypatch.setattr(setup.platform, "save_runtime_checkpoint", interrupt)
    with pytest.raises(SimulatedExit):
        make_worker().run_once(OWNER)
    monkeypatch.setattr(setup.platform, "save_runtime_checkpoint", original_save)
    with setup.platform._connect() as connection:
        connection.execute("UPDATE runs SET lease_expires_at=0 WHERE run_id=?", (binding.run_id,))

    def forbidden_provider(_profile):
        raise AssertionError("uncertain Run must not reopen its provider")

    restarted = make_worker()
    restarted.provider_factory = forbidden_provider
    result = restarted.run_once(OWNER)
    assert result["run_id"] == binding.run_id and result["state"] == "unknown"
    assert len(calls) == (0 if phase == "provider_pending" else 1)


@pytest.mark.parametrize("restart_phase", [None, "ready", "response", "receipt"])
def test_native_provider_reads_code_only_after_readonly_tool_call(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restart_phase: str | None) -> None:
    from hub.application.workflow_code_inputs import WorkflowCodeView
    from hub.domain.workflow_native_memory import NATIVE_MEMORY_MARKER
    from hub.infrastructure.workflow_native_memory_repository import WorkflowNativeMemoryRepository
    from hub.platform.providers.base import ModelResponse
    from hub.platform.tool_broker import ToolBroker

    setup, resolved, _revision, code_target, code_inputs, binding = _dispatch_code_source(tmp_path)
    traces: list[tuple[object, ...]] = []
    original_read = WorkflowCodeView.read

    def observed_read(view, ref_key, path, **kwargs):
        traces.append((ref_key, path, tuple(view._roots), tuple(view._roots.get(ref_key, Path("/missing")).rglob("*"))))
        return original_read(view, ref_key, path, **kwargs)

    monkeypatch.setattr(WorkflowCodeView, "read", observed_read)
    view_opens: list[bool] = []
    original_open_view = code_inputs.open_view_for_scope

    def counted_open_view(*args, **kwargs):
        view_opens.append(True)
        return original_open_view(*args, **kwargs)

    code_inputs.open_view_for_scope = counted_open_view
    run = setup.platform.get_run(OWNER, binding.run_id)
    config = run["config_snapshot"]
    assert config["workflow_code_mode"] == {"revision": 1}
    assert config[NATIVE_MEMORY_MARKER]["schema_version"] == 4
    assert "🥭完整源码" not in json.dumps(config, ensure_ascii=False)

    class NoArtifactIO:
        def authorize(self, *_args, **_kwargs):
            raise AssertionError("committed Run lookup must not reauthorize source artifacts")

    with setup.workflows.transaction(write=False) as connection:
        token = connection.execute("SELECT token FROM workflow_dispatches WHERE binding_run_id=?",
            (binding.run_id,)).fetchone()[0]
    retry = RunStepExecutor(setup.workflows, code_target, clock=setup.clock, code_inputs=NoArtifactIO())
    retried = asyncio.run(retry.create_or_lookup(token, resolved.digest, resolved))
    assert retried == binding

    class Provider:
        def __init__(self) -> None:
            self.calls = provider_calls

        def complete(self, messages, tools, *, request_observer=None):
            self.calls.append((copy.deepcopy(messages), copy.deepcopy(tools)))
            if len(self.calls) == 1:
                return ModelResponse(kind="tool_call", tool="workflow.input.list",
                    arguments={"ref_key": "source"})
            if len(self.calls) == 2:
                return ModelResponse(kind="tool_call", tool="workflow.input.read",
                    arguments={"ref_key": "source", "path": "src/main.py"})
            return ModelResponse(kind="final", text="source inspected")

    provider_calls: list[Any] = []
    captured: list[Any] = []
    worker = setup.worker(captured)
    provider = Provider()
    worker.provider_factory = lambda _profile: provider
    worker.workflow_memory_guard = WorkflowNativeMemoryRepository(setup.workflows,
        code_inputs=code_inputs, clock=setup.clock)
    worker.workflow_code_inputs = code_inputs
    if restart_phase is not None:
        class SimulatedProcessExit(BaseException):
            pass

        original_save = setup.platform.save_runtime_checkpoint

        def crash_after_read(*args, **kwargs):
            result = original_save(*args, **kwargs)
            checkpoint = args[2]
            if checkpoint["phase"] == restart_phase and checkpoint["step"] == (3 if restart_phase == "ready" else 2):
                raise SimulatedProcessExit()
            return result

        monkeypatch.setattr(setup.platform, "save_runtime_checkpoint", crash_after_read)
        with pytest.raises(SimulatedProcessExit):
            worker.run_once(OWNER)
        monkeypatch.setattr(setup.platform, "save_runtime_checkpoint", original_save)
        with setup.platform._connect() as connection:
            connection.execute("UPDATE runs SET lease_expires_at=0 WHERE run_id=?", (binding.run_id,))
        worker = setup.worker(captured)
        worker.provider_factory = lambda _profile: Provider()
        worker.workflow_memory_guard = WorkflowNativeMemoryRepository(setup.workflows,
            code_inputs=code_inputs, clock=setup.clock)
        worker.workflow_code_inputs = code_inputs
    outcome = worker.run_once(OWNER)
    events = setup.platform.list_run_events(OWNER, binding.run_id)["events"]
    assert outcome["state"] == "succeeded", (outcome.get("state"), outcome.get("result_text"),
        [item["payload"].get("error_code") for item in events if item["kind"] == "tool_result"], traces)
    assert len(provider_calls) == 3
    assert len(view_opens) == (2 if restart_phase is not None else 1)
    assert len(traces) == 1
    first_messages, first_tools = provider_calls[0]
    assert "🥭完整源码" not in json.dumps(first_messages, ensure_ascii=False)
    assert {item["name"] for item in first_tools} >= {"workflow.input.list", "workflow.input.read"}
    assert "src" in json.dumps(provider_calls[1][0], ensure_ascii=False)
    assert "🥭完整源码" in json.dumps(provider_calls[2][0], ensure_ascii=False)
    assert "workflow.input.read" not in {item["name"] for item in ToolBroker.tool_definitions()}

    from hub.composition.workflows import assert_workflow_visibility
    from hub.infrastructure.platform_backup import BackupError, PlatformBackupService
    from hub.infrastructure.workflow_repository import WorkflowRepository, WorkflowRepositoryError

    backup_service = PlatformBackupService()
    backup_root, restored_root = tmp_path / "native-code-backup", tmp_path / "native-code-restored"
    summary = backup_service.create_backup(setup.workflows.db_path, setup.artifacts.root, backup_root)
    assert summary["workflow"] == {"schema_version": 1, "minimum_reader_version": 12}
    backup_service.restore_backup(backup_root, restored_root)
    restored = WorkflowRepository(restored_root / "platform.db")
    restored.init()
    assert asyncio.run(RunStepExecutor(restored, code_target, clock=setup.clock).lookup(
        binding.token, resolved.digest, resolved)) == binding
    from hub.infrastructure.platform_db import PlatformRepository
    assert PlatformRepository(restored.db_path).get_run(OWNER, binding.run_id)["config_snapshot"] == config
    with restored.transaction(write=False) as connection:
        assert tuple(connection.execute("SELECT admission_mode,minimum_reader_version FROM workflow_schema_meta").fetchone()) == ("frozen", 12)
        assert connection.execute("SELECT count(*) FROM workflow_artifact_refs WHERE artifact_id=?",
            (_revision.source_bundle.artifact_id,)).fetchone()[0] == 1
    with pytest.raises(WorkflowRepositoryError, match="workflow_domain_in_use"):
        assert_workflow_visibility(restored.db_path, enabled=False)
    with restored.transaction() as connection:
        connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=11")
    with pytest.raises(WorkflowRepositoryError):
        restored.init()
    with pytest.raises(WorkflowRepositoryError):
        assert_workflow_visibility(restored.db_path, enabled=False)
    with pytest.raises(BackupError):
        backup_service.create_backup(restored.db_path, restored_root / "artifacts", tmp_path / "old-code-reader-backup")

    encrypted_backup, encrypted_root = tmp_path / "encrypted-code-backup", tmp_path / "encrypted-code-restored"
    encrypted_summary = backup_service.create_encrypted_backup(setup.workflows.db_path, setup.artifacts.root,
        encrypted_backup, b"k" * 32, "native-code-key")
    assert encrypted_summary["source"]["workflow"] == {"schema_version": 1, "minimum_reader_version": 12}
    backup_service.restore_encrypted_backup(encrypted_backup, encrypted_root, b"k" * 32)
    encrypted_repository = WorkflowRepository(encrypted_root / "platform.db")
    encrypted_repository.init()
    assert asyncio.run(RunStepExecutor(encrypted_repository, code_target, clock=setup.clock).lookup(
        binding.token, resolved.digest, resolved)) == binding
    assert PlatformRepository(encrypted_repository.db_path).get_run(OWNER, binding.run_id)["config_snapshot"] == config
    with encrypted_repository.transaction(write=False) as connection:
        assert tuple(connection.execute("SELECT admission_mode,minimum_reader_version FROM workflow_schema_meta").fetchone()) == ("frozen", 12)
        assert connection.execute("SELECT count(*) FROM workflow_artifact_refs WHERE artifact_id=?",
            (_revision.source_bundle.artifact_id,)).fetchone()[0] == 1
