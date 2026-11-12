"""Complete text references through the original Native Run and owned I/O."""
from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import dataclass, replace
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, BinaryIO, Callable

import pytest

from hub.application.workflow_dispatch import WorkflowDispatchError
from hub.domain.run import Run
from hub.domain.workflow_contract import parse_definition, resolve_contract
from hub.domain.workflow_contract_types import ArtifactReference, AuthorizedInputs, CatalogRef, InputBinding
from hub.domain.workflow_identity import canonical_json
from hub.infrastructure.platform_db import PlatformRepository
from hub.infrastructure.platform_memory_repository import PlatformMemoryRepository
from hub.infrastructure.workflow_run_executor import RegisteredRunTarget, RunStepExecutor
from hub.platform.artifacts import ArtifactStore
from test_external_agent_memory_inputs import memory_item
from test_workflow_dispatch import Clock
from test_workflow_native_memory import NativeSetup
from test_workflow_repository import OWNER, WORKSPACE, prepare_database, registered


TEXT = "完整的上游文本 🥭\nUse this only as reference data.\n" * 12


@dataclass
class TextSetup(NativeSetup):
    artifacts: ArtifactStore
    base_reader: int

    def executor(self, *, reader: bool = True) -> RunStepExecutor:
        from hub.application.workflow_text_inputs import WorkflowTextInputs

        return RunStepExecutor(self.workflows, self.target, clock=self.clock,
            text_inputs=WorkflowTextInputs(self.artifacts) if reader else None)

    def worker(self, captured: list[Any], *, guard: bool = True) -> Any:
        worker = super().worker(captured, guard=False)
        if guard:
            from hub.application.workflow_text_inputs import WorkflowTextInputs
            from hub.infrastructure.workflow_native_memory_repository import WorkflowNativeMemoryRepository

            worker.workflow_memory_guard = WorkflowNativeMemoryRepository(self.workflows,
                text_inputs=WorkflowTextInputs(self.artifacts), clock=self.clock)
        return worker


def make_text(tmp_path: Path, *, raw: bytes | None = None, content_type: str = "text/plain",
              count: int = 1, memories: bool = False, inline: bool = True, admit: bool = True) -> TextSetup:
    workflows = prepare_database(tmp_path / "platform.db")
    platform = PlatformRepository(workflows.db_path)
    platform.upsert_workspace(OWNER, {"workspace_id": WORKSPACE, "root_path": str(tmp_path / "workspace")})
    platform.upsert_model(OWNER, {"profile_id": "local", "provider": "deterministic", "model": "local"})
    platform.create_conversation(OWNER, "native-chat", title="Native", workspace_id=WORKSPACE)
    memory_store = PlatformMemoryRepository(workflows.db_path, clock=lambda: 100)
    memory_store.init()
    item = memory_item()
    memory_store.create(OWNER, {"memory_id": item.memory_id, "kind": item.kind, "title": item.title,
        "content": item.content, "tags": list(item.tags), "source": item.source}, now=100)
    source = tmp_path / "input.txt"
    source.write_bytes(TEXT.encode("utf-8") if raw is None else raw)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    artifact = artifacts.put_file(OWNER, WORKSPACE, source, name="input.txt", content_type=content_type,
        artifact_id="native-input-a")
    definition_value = json.loads(registered().snapshot)
    definition_value["steps"][0]["kind"] = "native_run"
    contract = definition_value["steps"][0]["contract"]
    contract.update(schema_version=2 if memories else 1, output_specs=[], input_refs=[
        {"ref_key": f"input-{index}", "kind": "artifact", **{key: artifact[key] for key in ("artifact_id", "sha256", "size")}}
        for index in range(count)], context_refs=[
            {"ref_key": "memory", "kind": "memory", "ref_id": item.memory_id, "revision": item.revision}] if memories else [])
    definition = parse_definition(canonical_json(definition_value))
    resolved = resolve_contract(definition, AuthorizedInputs(OWNER, WORKSPACE, "develop", CatalogRef("native", 1),
        tuple(InputBinding(named.ref_key, OWNER, WORKSPACE, named.reference)
              for named in definition.steps[0].contract.input_refs + definition.steps[0].contract.context_refs)))
    workflows.create_or_get(OWNER, WORKSPACE, "workflow-a", client_token="native-text", definition=definition, now=100)
    claim = workflows.claim_due(OWNER, worker_id="coordinator", now=100, limit=1, lease_seconds=60)[0]
    ready = workflows.freeze_inputs(claim, step_key="develop", generation=1, resolved=resolved, expected_revision=1, now=101)
    if admit:
        workflows.admit_step(claim, step_key="develop", generation=1, expected_revision=ready.revision,
            target_key="native-a", capacity=1, reserve_tokens=100, reserve_requests=2, now=102)
    target = RegisteredRunTarget(OWNER, WORKSPACE, "native-a", CatalogRef("native", 1), CatalogRef("safe", 1),
        "native-chat", canonical_json({"model_profile_id": "local", "provider_snapshot": {
            "provider": "deterministic", "model": "local", "provider_config": {}, "capabilities": {}}}))
    if inline:
        target = replace(target, inline_text_inputs=True)
    with workflows.transaction(write=False) as connection:
        base_reader = connection.execute("SELECT minimum_reader_version FROM workflow_schema_meta").fetchone()[0]
    return TextSetup(workflows, platform, memory_store, target, resolved, Clock(), claim, artifacts, base_reader)


class ObservedArtifacts:
    def __init__(self, store: ArtifactStore, *, metadata: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
                 body: bytes | None = None, fragment: int | None = None) -> None:
        self.store = store
        self.metadata = metadata
        self.body = body
        self.fragment = fragment
        self.opens = 0
        self.handles: list[BinaryIO] = []
        self.read_sizes: list[int] = []

    def get(self, owner: str, workspace: str, artifact: str) -> dict[str, Any] | None:
        value = self.store.get(owner, workspace, artifact)
        return self.metadata(value) if self.metadata is not None and value is not None else value

    def open_verified(self, owner: str, workspace: str, artifact: str) -> BinaryIO:
        self.opens += 1
        handle = io.BytesIO(self.body) if self.body is not None else self.store.open_verified(owner, workspace, artifact)
        self.handles.append(handle)
        original_read = handle.read

        def read(size: int = -1) -> bytes:
            self.read_sizes.append(size)
            return original_read(min(size, self.fragment) if self.fragment is not None and size >= 0 else size)

        handle.read = read
        return handle


def assert_no_run(setup: TextSetup) -> None:
    with setup.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM legacy_task_links").fetchone()[0] == 0
        assert connection.execute("SELECT child_request_id FROM workflow_budget_reservations").fetchone()[0] is None
        assert connection.execute("SELECT minimum_reader_version FROM workflow_schema_meta").fetchone()[0] == setup.base_reader


def test_native_text_target_support_is_explicit_strict_and_private(tmp_path: Path) -> None:
    setup = make_text(tmp_path, inline=False)
    assert setup.target.inline_text_inputs is False
    assert replace(setup.target, inline_text_inputs=True).inline_text_inputs is True
    for invalid in (None, 0, 1, "true"):
        with pytest.raises(ValueError):
            replace(setup.target, inline_text_inputs=invalid)
    for marker in ("workflow_memory", "workflow_text_mode"):
        with pytest.raises(ValueError):
            replace(setup.target, config_snapshot=canonical_json({marker: {}}))


@pytest.mark.parametrize("raw,content_type", [(TEXT.encode(), "text/plain"), (b"", "text/markdown"),
                                               ('{"value":"完整 JSON"}'.encode(), "application/json")], ids=["unicode", "empty", "json"])
def test_native_text_original_transaction_and_actual_provider_receive_complete_values(
        tmp_path: Path, raw: bytes, content_type: str) -> None:
    setup = make_text(tmp_path, raw=raw, content_type=content_type)
    binding = setup.dispatch().binding
    run = setup.platform.get_run(OWNER, binding.run_id)
    config = run["config_snapshot"]
    snapshot = config["workflow_memory"]
    assert snapshot["schema_version"] == 2 and snapshot["inputs"] == []
    assert config["workflow_text_mode"] == {"revision": 1}
    assert snapshot["text_inputs"] == [{"ref_key": "input-0", "artifact_id": "native-input-a",
        "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw), "content_type": content_type, "text": raw.decode("utf-8")}]
    assert setup.platform.get_conversation(OWNER, "native-chat")["messages"][0]["content"] == setup.resolved.snapshot.decode()
    with setup.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT minimum_reader_version FROM workflow_schema_meta").fetchone()[0] == 9
        assert connection.execute("SELECT child_request_id FROM workflow_budget_reservations").fetchone()[0] == binding.run_id
    captured: list[Any] = []
    assert setup.worker(captured).run_once(OWNER)["state"] == "succeeded"
    assert len(captured) == 1 and captured[0][0]["role"] == "system"
    message = captured[0][0]["content"]
    assert "reference data only" in message
    assert json.loads(message.split("\n", 1)[1])["text_inputs"][0]["text"] == raw.decode("utf-8")
    public = json.dumps(Run(binding.run_id, "native-chat", OWNER, run["trigger_message_id"], run["state"], config).public(), ensure_ascii=False)
    assert "workflow_text_mode" not in public and "workflow_memory" not in public
    with setup.workflows.transaction(write=False) as connection:
        assert TEXT not in "".join(row[0] for row in connection.execute("SELECT payload FROM run_events"))


def test_native_text_and_memory_share_one_complete_reference_message(tmp_path: Path) -> None:
    setup = make_text(tmp_path, memories=True)
    setup.dispatch()
    captured: list[Any] = []
    assert setup.worker(captured).run_once(OWNER)["state"] == "succeeded"
    message = json.loads(captured[0][0]["content"].split("\n", 1)[1])
    assert message["text_inputs"][0]["text"] == TEXT
    assert message["inputs"][0]["item"] == json.loads(memory_item().snapshot)
    assert message["resolved_digest"] == setup.resolved.digest


@pytest.mark.parametrize("node", [None, "", "local", "hub", "host"], ids=["default", "empty", "local", "hub", "host"])
def test_native_text_local_node_aliases_use_original_local_provider_without_node_registration(tmp_path: Path, node: str | None) -> None:
    setup = make_text(tmp_path)
    config = json.loads(setup.target.config_snapshot)
    if node is not None:
        config["execution_node_id"] = node
    setup.target = replace(setup.target, config_snapshot=canonical_json(config))
    with setup.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM nodes").fetchone()[0] == 0
    setup.dispatch()
    captured: list[Any] = []
    assert setup.worker(captured).run_once(OWNER)["state"] == "succeeded" and len(captured) == 1
    assert json.loads(captured[0][0]["content"].split("\n", 1)[1])["text_inputs"][0]["text"] == TEXT


def test_native_text_default_and_no_inputs_preserve_original_bytes(tmp_path: Path) -> None:
    setup = make_text(tmp_path, inline=False)
    binding = setup.dispatch(RunStepExecutor(setup.workflows, setup.target, clock=setup.clock)).binding
    run = setup.platform.get_run(OWNER, binding.run_id)
    assert "workflow_memory" not in run["config_snapshot"] and "workflow_text_mode" not in run["config_snapshot"]
    assert setup.platform.get_conversation(OWNER, "native-chat")["messages"][0]["content"] == setup.resolved.snapshot.decode()
    with setup.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT minimum_reader_version FROM workflow_schema_meta").fetchone()[0] == 1
    from test_workflow_native_memory import make_native

    other = tmp_path / "no-inputs"
    other.mkdir()
    native = make_native(other, count=0)
    first = native.executor()._request(native.resolved)
    enabled = RunStepExecutor(native.workflows, replace(native.target, inline_text_inputs=True), clock=native.clock)
    assert enabled._request(native.resolved) == first


@pytest.mark.parametrize("change", ["missing_provider", "remote", "legacy_memory", "cli", "capability"])
def test_native_text_rejects_unsupported_target_before_artifact_io(tmp_path: Path, change: str) -> None:
    from hub.infrastructure.workflow_run_executor import LegacyTaskTarget

    setup = make_text(tmp_path, inline=False)
    config = json.loads(setup.target.config_snapshot)
    fields: dict[str, Any] = {"inline_text_inputs": True}
    if change == "missing_provider": config.pop("provider_snapshot")
    if change == "remote": config["execution_node_id"] = "remote-node"
    if change == "legacy_memory": config["memory_context"] = {"enabled": True}
    if change == "cli": fields["legacy_task"] = LegacyTaskTarget("node-a", "codex", "project-a")
    if change == "capability": config["workflow_capability"] = {"binding_digest": "a" * 64}
    with pytest.raises((ValueError, WorkflowDispatchError)):
        target = replace(setup.target, config_snapshot=canonical_json(config), **fields)
        asyncio.run(RunStepExecutor(setup.workflows, target, clock=setup.clock).create_or_lookup(
            "a" * 64, setup.resolved.digest, setup.resolved))
    assert_no_run(setup)


def test_native_text_required_reader_cannot_silently_fall_back(tmp_path: Path) -> None:
    setup = make_text(tmp_path)
    with pytest.raises(WorkflowDispatchError) as error:
        setup.dispatch(setup.executor(reader=False))
    assert error.value.uncertain is False
    assert_no_run(setup)


def test_native_text_code_revision_input_is_rejected_before_descriptor_or_run(tmp_path: Path) -> None:
    from hub.application.workflow_dispatch import WorkflowDispatchService
    from hub.application.workflow_text_inputs import WorkflowTextInputs

    setup = make_text(tmp_path, admit=False)
    value = json.loads(setup.workflows.get(OWNER, "workflow-a").definition_snapshot)
    reference = setup.resolved.contract.input_refs[0].reference
    value["steps"][0]["contract"]["input_refs"] = [{"ref_key": "source", "kind": "code_revision",
        "repository_id": "repository-a", "base_commit": "a" * 40, "head_commit": "b" * 40, "tree_id": "c" * 40,
        "source_bundle": {"artifact_id": reference.artifact_id, "sha256": reference.sha256, "size": reference.size}}]
    definition = parse_definition(canonical_json(value))
    resolved = resolve_contract(definition, AuthorizedInputs(OWNER, WORKSPACE, "develop", CatalogRef("native", 1),
        tuple(InputBinding(named.ref_key, OWNER, WORKSPACE, named.reference) for named in definition.steps[0].contract.input_refs)))
    setup.workflows.create_or_get(OWNER, WORKSPACE, "code-workflow", client_token="code-workflow", definition=definition, now=104)
    claim = setup.workflows.claim_due(OWNER, worker_id="code-worker", now=104, limit=1, lease_seconds=60)[0]
    assert claim.workflow_id == "code-workflow"
    ready = setup.workflows.freeze_inputs(claim, step_key="develop", generation=1, resolved=resolved, expected_revision=1, now=104)
    setup.workflows.admit_step(claim, step_key="develop", generation=1, expected_revision=ready.revision,
        target_key="native-a", capacity=1, reserve_tokens=100, reserve_requests=2, now=104)
    store = ObservedArtifacts(setup.artifacts)
    executor = RunStepExecutor(setup.workflows, setup.target, clock=setup.clock, text_inputs=WorkflowTextInputs(store))
    with pytest.raises(WorkflowDispatchError, match="artifact_input_unsupported"):
        asyncio.run(WorkflowDispatchService(setup.workflows, executor, clock=setup.clock).dispatch(
            claim, step_key="develop", generation=1, expected_revision=3))
    assert store.opens == 0
    assert_no_run(setup)


@pytest.mark.parametrize("raw,count,memories", [(b"x" * 4200, 2, False), (b"x" * 8193, 1, False),
                                               (b"x" * 8192, 1, True), (b"\x00" * 6000, 1, False)], ids=["duplicate", "single", "mixed", "snapshot"])
def test_native_text_full_logical_and_snapshot_limits_rollback_all_creation(
        tmp_path: Path, raw: bytes, count: int, memories: bool) -> None:
    setup = make_text(tmp_path, raw=raw, count=count, memories=memories)
    with pytest.raises(WorkflowDispatchError, match="context_too_large") as error:
        setup.dispatch()
    assert error.value.uncertain is False
    assert_no_run(setup)


@pytest.mark.parametrize("change", ["owner", "workspace", "id", "hash", "size", "bool", "mime", "hidden"])
def test_native_text_manifest_authority_precedes_descriptor_read(tmp_path: Path, change: str) -> None:
    from hub.application.workflow_text_inputs import WorkflowTextInputs

    setup = make_text(tmp_path)

    def mutate(value: dict[str, Any]) -> dict[str, Any] | None:
        fields = {"owner": ("owner_id", "other@example.test"), "workspace": ("workspace_id", "other"),
                  "id": ("artifact_id", "other"), "hash": ("sha256", "a" * 64), "size": ("size", 1),
                  "bool": ("size", True), "mime": ("content_type", "application/octet-stream")}
        if change == "hidden": return None
        key, item = fields[change]
        return {**value, key: item}

    store = ObservedArtifacts(setup.artifacts, metadata=mutate)
    executor = setup.executor()
    executor.text_inputs = WorkflowTextInputs(store)
    with pytest.raises(WorkflowDispatchError) as error:
        setup.dispatch(executor)
    assert error.value.uncertain is False and store.opens == 0
    assert_no_run(setup)


@pytest.mark.parametrize("body", [TEXT.encode()[:-1], TEXT.encode() + b"x", b"x" * len(TEXT.encode())], ids=["short", "long", "hash"])
def test_native_text_complete_bytes_are_verified_and_owned_descriptor_closes(tmp_path: Path, body: bytes) -> None:
    from hub.application.workflow_text_inputs import WorkflowTextInputs

    setup = make_text(tmp_path)
    store = ObservedArtifacts(setup.artifacts, body=body)
    executor = setup.executor()
    executor.text_inputs = WorkflowTextInputs(store)
    with pytest.raises(WorkflowDispatchError): setup.dispatch(executor)
    assert store.opens == 1 and all(handle.closed for handle in store.handles)
    assert_no_run(setup)


def test_native_text_deduplicates_fragmented_complete_reads_and_preserves_logical_order(tmp_path: Path) -> None:
    from hub.application.workflow_text_inputs import WorkflowTextInputs

    setup = make_text(tmp_path, count=2)
    store = ObservedArtifacts(setup.artifacts, fragment=7)
    executor = setup.executor()
    executor.text_inputs = WorkflowTextInputs(store)
    binding = setup.dispatch(executor).binding
    inputs = setup.platform.get_run(OWNER, binding.run_id)["config_snapshot"]["workflow_memory"]["text_inputs"]
    assert [item["ref_key"] for item in inputs] == ["input-0", "input-1"]
    assert all(item["text"] == TEXT for item in inputs) and store.opens == 1
    assert all(handle.closed for handle in store.handles)
    assert all(0 < size <= 8193 for size in store.read_sizes)


def test_native_text_invalid_utf8_preserves_original_exception_without_partial_run(tmp_path: Path) -> None:
    setup = make_text(tmp_path, raw=b"\xff")
    with pytest.raises(WorkflowDispatchError) as error: setup.dispatch()
    current: BaseException | None = error.value
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, UnicodeDecodeError): break
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    assert isinstance(current, UnicodeDecodeError)
    assert_no_run(setup)


@pytest.mark.parametrize("change", ["body", "mode", "missing", "reader", "config", "scope"])
def test_native_text_saved_snapshot_corruption_is_not_consumed(tmp_path: Path, change: str) -> None:
    setup = make_text(tmp_path)
    binding = setup.dispatch().binding
    config = setup.platform.get_run(OWNER, binding.run_id)["config_snapshot"]
    if change == "body": config["workflow_memory"]["text_inputs"][0]["text"] = "changed"
    if change == "mode": config["workflow_text_mode"]["revision"] = True
    if change == "missing": config.pop("workflow_memory")
    if change == "config": config["model_profile_id"] = "other"
    if change == "scope": config["owner_id"] = "other@example.test"
    with setup.workflows.transaction() as connection:
        if change == "reader": connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=8")
        else: connection.execute("UPDATE runs SET config_snapshot=? WHERE run_id=?", (canonical_json(config).decode(), binding.run_id))
    captured: list[Any] = []
    assert setup.worker(captured).run_once(OWNER)["state"] == "failed" and captured == []


@pytest.mark.parametrize("change", ["workspace", "cancel", "frozen", "generation", "deadline", "memory"])
def test_native_text_current_execution_authority_blocks_provider(tmp_path: Path, change: str) -> None:
    setup = make_text(tmp_path, memories=True)
    setup.dispatch()
    with setup.workflows.transaction() as connection:
        statements = {"workspace": "UPDATE workspaces SET enabled=0", "cancel": "UPDATE workflow_runs SET cancel_requested=1",
            "frozen": "UPDATE workflow_schema_meta SET admission_mode='frozen'", "generation": "UPDATE workflow_steps SET is_current=0",
            "deadline": "UPDATE workflow_steps SET active_deadline=100", "memory": "UPDATE platform_memory_items SET content='changed'"}
        connection.execute(statements[change])
    captured: list[Any] = []
    result = setup.worker(captured).run_once(OWNER)
    assert result["state"] == "failed" and captured == []
    assert TEXT not in json.dumps(result, ensure_ascii=False)


def test_native_text_guard_is_mandatory_and_original_lookup_does_not_read_files(tmp_path: Path) -> None:
    from hub.application.workflow_text_inputs import WorkflowTextInputs

    setup = make_text(tmp_path)
    binding = setup.dispatch().binding
    original = setup.platform.get_run(OWNER, binding.run_id)["config_snapshot"]
    store = ObservedArtifacts(setup.artifacts, metadata=lambda value: None)
    executor = setup.executor(reader=False)
    assert asyncio.run(executor.lookup(binding.token, setup.resolved.digest, setup.resolved)) == binding
    executor.text_inputs = WorkflowTextInputs(store)
    assert asyncio.run(executor.create_or_lookup(binding.token, setup.resolved.digest, setup.resolved)) == binding
    assert store.opens == 0 and setup.platform.get_run(OWNER, binding.run_id)["config_snapshot"] == original
    captured: list[Any] = []
    assert setup.worker(captured, guard=False).run_once(OWNER)["state"] == "failed" and captured == []


@pytest.mark.parametrize("missing", ["reader", "clock"])
def test_native_text_guard_requires_current_metadata_reader_and_injected_clock(tmp_path: Path, missing: str) -> None:
    from hub.application.workflow_text_inputs import WorkflowTextInputs
    from hub.infrastructure.workflow_native_memory_repository import WorkflowNativeMemoryRepository

    setup = make_text(tmp_path)
    setup.dispatch()
    captured: list[Any] = []
    worker = setup.worker(captured)
    worker.workflow_memory_guard = WorkflowNativeMemoryRepository(setup.workflows,
        text_inputs=None if missing == "reader" else WorkflowTextInputs(setup.artifacts),
        clock=None if missing == "clock" else setup.clock)
    result = worker.run_once(OWNER)
    assert result["state"] == "failed" and result["result_text"] == "native_text_guard_unavailable" and captured == []


def test_native_text_metadata_error_preserves_original_cause_and_bounded_code(tmp_path: Path) -> None:
    from hub.application.workflow_text_inputs import WorkflowTextInputs
    from hub.domain.workflow_native_memory import WorkflowNativeMemoryError
    from hub.infrastructure.workflow_native_memory_repository import WorkflowNativeMemoryRepository

    setup = make_text(tmp_path)
    setup.dispatch()
    claim = setup.platform.claim_run(OWNER, worker_id="native-worker", now=setup.clock(), lease_s=60)
    cause = OSError("private artifact metadata failure")

    def unavailable(_metadata: dict[str, Any]) -> None:
        raise cause

    guard = WorkflowNativeMemoryRepository(setup.workflows,
        text_inputs=WorkflowTextInputs(ObservedArtifacts(setup.artifacts, metadata=unavailable)), clock=setup.clock)
    with pytest.raises(WorkflowNativeMemoryError) as error:
        guard.prepare(claim, now=setup.clock())
    assert error.value.code == "artifact_input_unavailable"
    assert error.value.__cause__.__cause__ is cause
    assert "private artifact metadata failure" not in str(error.value)


@pytest.mark.parametrize("change", ["memory", "deadline", "cancel"])
def test_native_text_final_authority_read_follows_metadata_io(tmp_path: Path, change: str) -> None:
    from hub.application.workflow_text_inputs import WorkflowTextInputs
    from hub.infrastructure.workflow_native_memory_repository import WorkflowNativeMemoryRepository

    setup = make_text(tmp_path, memories=True)
    setup.dispatch()
    initial_time = setup.clock()
    if change == "deadline":
        with setup.workflows.transaction() as connection:
            connection.execute("UPDATE workflow_steps SET active_deadline=?", (initial_time + 1,))
    fired = False

    def metadata(value: dict[str, Any]) -> dict[str, Any]:
        nonlocal fired
        if fired: return value
        fired = True
        if change == "deadline": setup.clock.now = initial_time + 2
        else:
            with setup.workflows.transaction() as connection:
                connection.execute("UPDATE platform_memory_items SET content='changed'" if change == "memory"
                                   else "UPDATE workflow_runs SET cancel_requested=1")
        return value

    worker = setup.worker([])
    store = ObservedArtifacts(setup.artifacts, metadata=metadata)
    worker.workflow_memory_guard = WorkflowNativeMemoryRepository(setup.workflows, text_inputs=WorkflowTextInputs(store), clock=setup.clock)
    called: list[str] = []
    worker.provider_factory = lambda profile: called.append("provider")
    assert worker.run_once(OWNER)["state"] == "failed" and fired and called == []


def test_native_text_creation_rechecks_current_authority_after_complete_read(tmp_path: Path) -> None:
    from hub.application.workflow_text_inputs import WorkflowTextInputs

    setup = make_text(tmp_path)
    store = ObservedArtifacts(setup.artifacts)
    original_open = store.open_verified

    def opening(owner: str, workspace: str, artifact: str) -> BinaryIO:
        handle = original_open(owner, workspace, artifact)
        with setup.workflows.transaction() as connection:
            connection.execute("UPDATE workspaces SET enabled=0 WHERE owner_id=?", (OWNER,))
        return handle

    store.open_verified = opening
    executor = setup.executor()
    executor.text_inputs = WorkflowTextInputs(store)
    with pytest.raises(WorkflowDispatchError): setup.dispatch(executor)
    assert all(handle.closed for handle in store.handles)
    assert_no_run(setup)


def test_native_text_original_transaction_rolls_back_snapshot_reader_message_and_budget(tmp_path: Path) -> None:
    setup = make_text(tmp_path)
    with setup.workflows.transaction() as connection:
        connection.execute("CREATE TRIGGER reject_text_run BEFORE INSERT ON runs BEGIN SELECT RAISE(ABORT,'reject text'); END")
    with pytest.raises(WorkflowDispatchError): setup.dispatch()
    assert_no_run(setup)


def test_native_text_owned_cancellation_waits_for_reader_and_does_not_commit_cancelled_work(tmp_path: Path) -> None:
    from hub.application.workflow_text_inputs import WorkflowTextInputs

    setup = make_text(tmp_path)
    opened = threading.Event()
    release = threading.Event()
    handle = io.BytesIO(TEXT.encode())
    store = ObservedArtifacts(setup.artifacts)

    def opening(owner: str, workspace: str, artifact: str) -> BinaryIO:
        opened.set()
        assert release.wait(10)
        return handle

    store.open_verified = opening
    executor = setup.executor()
    executor.text_inputs = WorkflowTextInputs(store)
    dispatch = setup.workflows.get_dispatch(OWNER, "workflow-a", "develop", 1)
    with setup.workflows.transaction() as connection:
        connection.execute("UPDATE workflow_dispatches SET stage='sending'")

    async def run() -> None:
        task = asyncio.create_task(executor.create_or_lookup(dispatch.token, setup.resolved.digest, setup.resolved))
        try:
            assert await asyncio.to_thread(opened.wait, 5)
            with setup.workflows.transaction() as connection:
                connection.execute("UPDATE workflow_runs SET cancel_requested=1")
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError): await task

    asyncio.run(run())
    assert handle.closed and opened.is_set()
    assert_no_run(setup)


def test_native_text_snapshot_is_canonical_immutable_private_and_cached(tmp_path: Path) -> None:
    from hub.domain.workflow_native_memory import build_native_memory, parse_native_memory
    from hub.domain.workflow_text_inputs import WorkflowTextInput

    setup = make_text(tmp_path)
    reference = setup.resolved.contract.input_refs[0].reference
    assert type(reference) is ArtifactReference
    snapshot = build_native_memory(setup.resolved, (), texts=(WorkflowTextInput("input-0", reference, "text/plain", TEXT),))
    assert snapshot.snapshot is snapshot.snapshot
    assert parse_native_memory(json.loads(snapshot.snapshot), setup.resolved) == snapshot
    assert TEXT not in repr(snapshot) and TEXT not in repr(snapshot.texts[0])
    with pytest.raises(AttributeError): snapshot.texts[0].text = "changed"


@pytest.mark.parametrize("change", ["schema", "extra", "missing", "key", "hash", "size", "bool", "mime", "digest", "text"])
def test_native_text_snapshot_strict_fields_identity_and_full_bytes(tmp_path: Path, change: str) -> None:
    from hub.domain.workflow_native_memory import build_native_memory, parse_native_memory
    from hub.domain.workflow_text_inputs import WorkflowTextInput

    setup = make_text(tmp_path)
    reference = setup.resolved.contract.input_refs[0].reference
    value = json.loads(build_native_memory(setup.resolved, (), texts=(WorkflowTextInput("input-0", reference, "text/plain", TEXT),)).snapshot)
    item = value["text_inputs"][0]
    if change == "schema": value["schema_version"] = True
    if change == "extra": item["extra"] = "forbidden"
    if change == "missing": value["text_inputs"] = []
    if change == "key": item["ref_key"] = "other"
    if change == "hash": item["sha256"] = "a" * 64
    if change == "size": item["size"] += 1
    if change == "bool": item["size"] = True
    if change == "mime": item["content_type"] = "application/octet-stream"
    if change == "digest": value["resolved_digest"] = "a" * 64
    if change == "text": item["text"] += "tail"
    with pytest.raises(ValueError): parse_native_memory(value, setup.resolved)


@pytest.mark.parametrize("boundary", ["history", "worker", "lookup"])
@pytest.mark.parametrize("damage", ["missing", "hash", "size", "workspace", "key", "extra"])
def test_native_text_original_input_reference_closure_is_mandatory(tmp_path: Path, boundary: str, damage: str) -> None:
    from hub.infrastructure.workflow_native_memory_repository import validate_native_memory_runs

    setup = make_text(tmp_path)
    binding = setup.dispatch().binding
    with closing(sqlite3.connect(setup.workflows.db_path)) as connection:
        if damage == "extra":
            connection.execute("INSERT INTO workflow_artifact_refs SELECT owner_id,workspace_id,workflow_id,step_key,generation,"
                               "'input:undeclared',artifact_id,sha256,size FROM workflow_artifact_refs")
        else:
            statements = {"missing": "DELETE FROM workflow_artifact_refs WHERE workflow_id='workflow-a'",
                "hash": "UPDATE workflow_artifact_refs SET sha256='" + "f" * 64 + "'",
                "size": "UPDATE workflow_artifact_refs SET size=size+1",
                "workspace": "UPDATE workflow_artifact_refs SET workspace_id='other'",
                "key": "UPDATE workflow_artifact_refs SET output_key='input:other'"}
            connection.execute(statements[damage])
        connection.commit()
    if boundary == "history":
        with setup.workflows.transaction(write=False) as connection:
            with pytest.raises(ValueError): validate_native_memory_runs(connection)
        return
    if boundary == "lookup":
        with pytest.raises(WorkflowDispatchError, match="native_memory_reference_mismatch"):
            asyncio.run(setup.executor().lookup(binding.token, setup.resolved.digest, setup.resolved))
        return
    captured: list[Any] = []
    assert setup.worker(captured).run_once(OWNER)["state"] == "failed" and captured == []


@pytest.mark.parametrize("memories", [False, True])
def test_native_text_bound_original_run_is_required_for_history(tmp_path: Path, memories: bool) -> None:
    from hub.infrastructure.workflow_native_memory_repository import validate_native_memory_runs

    setup = make_text(tmp_path, memories=memories)
    setup.dispatch()
    with closing(sqlite3.connect(setup.workflows.db_path)) as connection:
        connection.execute("UPDATE runs SET run_id='lost-native-run'")
        connection.commit()
    with setup.workflows.transaction(write=False) as connection:
        with pytest.raises(ValueError, match="native_memory_execution_missing"):
            validate_native_memory_runs(connection)


def test_native_text_complete_payload_also_respects_final_run_configuration_limit(tmp_path: Path) -> None:
    setup = make_text(tmp_path, raw=b"x" * 5000)
    config = json.loads(setup.target.config_snapshot)
    config["provider_snapshot"]["provider_config"] = {"fixed": "x" * 61500}
    setup.target = replace(setup.target, config_snapshot=canonical_json(config))
    with pytest.raises(WorkflowDispatchError, match="context_too_large"): setup.dispatch()
    assert_no_run(setup)


@pytest.mark.parametrize("inputs", ["artifact", "memory", "mixed"])
def test_native_complete_inputs_initial_request_bound_is_deterministic(tmp_path: Path, inputs: str) -> None:
    from hub.application.workflow_text_inputs import WorkflowTextInputs
    from hub.domain.workflow_errors import WorkflowValidationError

    setup = make_text(tmp_path, raw=b"x", count=0 if inputs == "memory" else 1,
        memories=inputs != "artifact", inline=inputs != "memory")
    config = json.loads(setup.target.config_snapshot)
    config["provider_snapshot"]["provider_config"] = {"fixed": "x" * 65300}
    setup.target = replace(setup.target, config_snapshot=canonical_json(config))
    observed = ObservedArtifacts(setup.artifacts)
    executor = setup.executor()
    executor.text_inputs = WorkflowTextInputs(observed)
    with pytest.raises(WorkflowDispatchError) as error:
        setup.dispatch(executor)
    assert error.value.code == "context_too_large"
    assert error.value.uncertain is False
    assert isinstance(error.value.__cause__, WorkflowValidationError)
    assert error.value.__cause__.code == "json_too_large"
    assert observed.opens == 0
    assert_no_run(setup)
    with setup.workflows.transaction(write=False) as connection:
        row = connection.execute("SELECT state,generation FROM workflow_steps WHERE step_key=?", ("develop",)).fetchone()
        assert tuple(row) == ("failed", 1)


def test_native_text_configuration_drift_during_read_cannot_rebind_original_intent(tmp_path: Path) -> None:
    from hub.application.workflow_text_inputs import WorkflowTextInputs

    setup = make_text(tmp_path)
    store = ObservedArtifacts(setup.artifacts)
    executor = setup.executor()
    original_open = store.open_verified

    def opening(owner: str, workspace: str, artifact: str) -> BinaryIO:
        handle = original_open(owner, workspace, artifact)
        config = json.loads(executor.target.config_snapshot)
        config["provider_snapshot"]["provider_config"] = {"different": True}
        executor.target = replace(executor.target, config_snapshot=canonical_json(config))
        return handle

    store.open_verified = opening
    executor.text_inputs = WorkflowTextInputs(store)
    with pytest.raises(WorkflowDispatchError, match="dispatch_conflict"): setup.dispatch(executor)
    assert_no_run(setup)


def test_native_text_target_is_captured_before_request_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hub.infrastructure import workflow_run_executor as runtime

    setup = make_text(tmp_path)
    executor = setup.executor()
    original = executor.target
    config = json.loads(original.config_snapshot)
    config["provider_snapshot"]["provider_config"] = {"changed_during_request": True}
    replacement = replace(original, config_snapshot=canonical_json(config))
    decode = runtime.decode_snapshot
    reads = 0
    changed = False

    def drifting(raw: bytes, **kwargs: Any) -> dict[str, Any]:
        nonlocal reads, changed
        value = decode(raw, **kwargs)
        if raw == original.config_snapshot:
            reads += 1
            if reads == 2:
                executor.target = replacement
                changed = True
        return value

    monkeypatch.setattr(runtime, "decode_snapshot", drifting)
    with pytest.raises(WorkflowDispatchError, match="dispatch_conflict"):
        setup.dispatch(executor)
    assert changed
    assert_no_run(setup)


@pytest.mark.parametrize("transfer_enabled", [False, True])
def test_native_text_default_bootstrap_coordinator_worker_and_http_hide_private_values(
        tmp_path: Path, transfer_enabled: bool) -> None:
    from hub.application.workflow_coordinator import RegisteredWorkflowPlan
    from hub.bootstrap import create_app
    from hub.config import FleetConfig
    from hub.platform.providers.base import ModelResponse
    from fleet_core.control import generate_ed25519_keypair

    setup = make_text(tmp_path, admit=False)
    assert setup.workflows.release_claim(setup.claim, now=102, next_poll_at=102)
    value = json.loads(registered().snapshot)
    value["steps"][0].update(kind="native_run", contract=json.loads(setup.resolved.contract.snapshot))
    definition = parse_definition(canonical_json(value))
    plan = RegisteredWorkflowPlan(OWNER, WORKSPACE, hashlib.sha256(definition.snapshot).hexdigest(), "develop", CatalogRef("native", 1),
        CatalogRef("safe", 1), "native-a", 1, 100, 2)
    executor = RunStepExecutor(setup.workflows, setup.target, clock=setup.clock)
    signing_key, _ = generate_ed25519_keypair()
    config = FleetConfig.from_root(tmp_path, platform_enabled=True, platform_worker_enabled=True, platform_workflows_enabled=True,
        platform_artifact_transfer_enabled=transfer_enabled, platform_command_signing_raw=signing_key,
        platform_workflow_dispatch_enabled=True, platform_db=setup.workflows.db_path, platform_artifact_root=setup.artifacts.root,
        dev_operator=OWNER, serve_frontend=False)
    app = create_app(config, repositories={"workflow_clock": setup.clock, "workflow_steps": (plan,),
        "workflow_executors": ((OWNER, WORKSPACE, "native-a", executor),)})
    services = app.extensions["fleet"]["services"]
    assert executor.text_inputs is not None
    for _ in range(4):
        assert not asyncio.run(services["workflow_coordinator"].tick(setup.clock())).failures
        setup.clock.now += 1
    binding = setup.workflows.get_dispatch(OWNER, "workflow-a", "develop", 1).binding
    assert binding is not None
    captured: list[Any] = []

    class Provider:
        def complete(self, messages: Any, tools: Any, *, request_observer: Any = None) -> ModelResponse:
            captured.append(messages)
            return ModelResponse(kind="final", text="done")

    worker = services["platform_worker"]
    worker.clock = setup.clock
    worker.run_events.clock = setup.clock
    worker.provider_factory = lambda profile: Provider()
    assert worker.run_once(OWNER)["state"] == "succeeded"
    assert json.loads(captured[0][0]["content"].split("\n", 1)[1])["text_inputs"][0]["text"] == TEXT
    if transfer_enabled:
        with setup.workflows.transaction(write=False) as connection:
            assert connection.execute("SELECT count(*) FROM platform_commands WHERE action='artifact.receive'").fetchone()[0] == 0
    client = app.test_client()
    for path in (f"runs/{binding.run_id}", f"runs/{binding.run_id}/events", "workflows/workflow-a", "conversations/native-chat"):
        response = client.get("/api/platform/v1/" + path)
        assert response.status_code == 200
        public = json.dumps(response.get_json(), ensure_ascii=False)
        assert "workflow_memory" not in public and "workflow_text_mode" not in public and "完整的上游文本" not in public
