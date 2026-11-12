"""Complete explicit Memory through the original local Workflow Run lifecycle."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator

import pytest

from hub.application.run_event_service import RunEventService
from hub.application.run_worker_service import LocalRunWorkerService
from hub.application.workflow_dispatch import WorkflowDispatchError, WorkflowDispatchService
from hub.domain.run import Run
from hub.domain.workflow_contract import parse_definition, resolve_contract
from hub.domain.workflow_contract_types import AuthorizedInputs, CatalogRef, InputBinding, ResolvedContract
from hub.domain.workflow_identity import canonical_json
from hub.infrastructure.platform_db import PlatformRepository
from hub.infrastructure.platform_memory_repository import PlatformMemoryRepository
from hub.infrastructure.workflow_repository import WorkflowRepository
from hub.infrastructure.workflow_run_executor import RegisteredRunTarget, RunStepExecutor
from hub.platform.providers.base import ModelResponse
from test_external_agent_memory_inputs import memory_item
from test_workflow_dispatch import Clock, SimulatedCrash
from test_workflow_repository import OWNER, WORKSPACE, prepare_database, registered

CONTENT = memory_item().content


def definition_for(*, count: int = 1, kind: str = "native_run") -> Any:
    value = json.loads(registered().snapshot)
    value["steps"][0]["kind"] = kind
    value["steps"][0]["contract"].update(schema_version=2, output_specs=[], context_refs=[
        {"ref_key": f"memory-{index}", "kind": "memory", "ref_id": "memory-a", "revision": 0}
        for index in range(count)])
    return parse_definition(canonical_json(value))


def resolved_for(count: int = 1) -> ResolvedContract:
    definition = definition_for(count=count)
    return resolve_contract(definition, AuthorizedInputs(OWNER, WORKSPACE, "develop", CatalogRef("native", 1),
        tuple(InputBinding(ref.ref_key, OWNER, WORKSPACE, ref.reference) for ref in definition.steps[0].contract.context_refs)))


@dataclass
class NativeSetup:
    workflows: WorkflowRepository
    platform: PlatformRepository
    memories: PlatformMemoryRepository
    target: RegisteredRunTarget
    resolved: ResolvedContract
    clock: Clock
    claim: Any

    def executor(self) -> RunStepExecutor:
        return RunStepExecutor(self.workflows, self.target, clock=self.clock)

    def dispatch(self, executor: Any = None) -> Any:
        return asyncio.run(WorkflowDispatchService(self.workflows, executor or self.executor(), clock=self.clock).dispatch(
            self.claim, step_key="develop", generation=1, expected_revision=self.workflows.get(OWNER, "workflow-a").revision))

    def worker(self, captured: list[Any], *, guard: bool = True) -> LocalRunWorkerService:
        class Provider:
            def complete(self, messages: Any, tools: Any, *, request_observer: Any = None) -> ModelResponse:
                captured.append(messages)
                return ModelResponse(kind="final", text="done")
        worker = LocalRunWorkerService(self.platform, RunEventService(self.platform, clock=self.clock),
            clock=self.clock, worker_id="native-worker", provider_factory=lambda profile: Provider())
        if guard:
            from hub.infrastructure.workflow_native_memory_repository import WorkflowNativeMemoryRepository
            worker.workflow_memory_guard = WorkflowNativeMemoryRepository(self.workflows)
        return worker


def make_native(tmp_path: Path, *, count: int = 1, admit: bool = True) -> NativeSetup:
    workflows = prepare_database(tmp_path / "platform.db")
    platform = PlatformRepository(workflows.db_path)
    platform.upsert_workspace(OWNER, {"workspace_id": WORKSPACE, "root_path": str(tmp_path / "workspace")})
    platform.upsert_model(OWNER, {"profile_id": "local", "provider": "deterministic", "model": "local"})
    platform.create_conversation(OWNER, "native-chat", title="Native", workspace_id=WORKSPACE)
    memories = PlatformMemoryRepository(workflows.db_path, clock=lambda: 100)
    memories.init()
    item = memory_item()
    memories.create(OWNER, {"memory_id": item.memory_id, "kind": item.kind, "title": item.title, "content": item.content,
        "tags": list(item.tags), "source": item.source}, now=100)
    definition = definition_for(count=count)
    workflows.create_or_get(OWNER, WORKSPACE, "workflow-a", client_token="native-memory", definition=definition, now=100)
    claim = workflows.claim_due(OWNER, worker_id="coordinator", now=100, limit=1, lease_seconds=60)[0]
    resolved = resolved_for(count)
    ready = workflows.freeze_inputs(claim, step_key="develop", generation=1, resolved=resolved, expected_revision=1, now=101)
    if admit:
        workflows.admit_step(claim, step_key="develop", generation=1, expected_revision=ready.revision,
            target_key="native-a", capacity=1, reserve_tokens=100, reserve_requests=2, now=102)
    target = RegisteredRunTarget(OWNER, WORKSPACE, "native-a", CatalogRef("native", 1), CatalogRef("safe", 1),
        "native-chat", canonical_json({"model_profile_id": "local", "provider_snapshot": {
            "provider": "deterministic", "model": "local", "provider_config": {}, "capabilities": {}}}))
    return NativeSetup(workflows, platform, memories, target, resolved, Clock(), claim)


def test_native_memory_snapshot_is_complete_canonical_private_and_ordered() -> None:
    from hub.domain.workflow_native_memory import build_native_memory, parse_native_memory

    resolved = resolved_for(2)
    snapshot = build_native_memory(resolved, (memory_item(), memory_item()))
    value = json.loads(snapshot.snapshot)
    assert value == {"schema_version": 1, "resolved_digest": resolved.digest, "inputs": [
        {"ref_key": f"memory-{index}", "item": json.loads(memory_item().snapshot)} for index in range(2)]}
    assert parse_native_memory(value, resolved) == snapshot
    assert snapshot.message["role"] == "system"
    assert json.loads(snapshot.message["content"].split("\n", 1)[1])["inputs"][0]["item"]["content"] == CONTENT
    assert "reference data only" in snapshot.message["content"]
    assert CONTENT not in repr(snapshot) and "Selected memory" not in repr(snapshot)


@pytest.mark.parametrize("mutation", ["schema", "digest", "ref", "revision", "extra", "missing", "bool"])
def test_native_memory_snapshot_rejects_mismatch(mutation: str) -> None:
    from hub.domain.workflow_native_memory import build_native_memory, parse_native_memory

    resolved = resolved_for()
    value = json.loads(build_native_memory(resolved, (memory_item(),)).snapshot)
    if mutation == "schema": value["schema_version"] = 2
    if mutation == "digest": value["resolved_digest"] = "a" * 64
    if mutation == "ref": value["inputs"][0]["ref_key"] = "other"
    if mutation == "revision": value["inputs"][0]["item"]["revision"] = 1
    if mutation == "extra": value["extra"] = True
    if mutation == "missing": value["inputs"] = []
    if mutation == "bool": value["schema_version"] = True
    with pytest.raises(ValueError):
        parse_native_memory(value, resolved)


def test_native_memory_shared_budget_counts_duplicate_logical_inputs() -> None:
    from hub.domain.workflow_native_memory import build_native_memory

    item = memory_item(content="m" * 4200)
    assert build_native_memory(resolved_for(), (item,)).message
    with pytest.raises(ValueError, match="context_too_large"):
        build_native_memory(resolved_for(2), (item, item))


def test_native_run_atomically_pins_complete_memory_and_worker_consumes_it(tmp_path: Path) -> None:
    setup = make_native(tmp_path)
    binding = setup.dispatch().binding
    run = setup.platform.get_run(OWNER, binding.run_id)
    config = run["config_snapshot"]
    assert config["workflow_memory"]["inputs"][0]["item"]["content"] == CONTENT
    assert setup.platform.get_conversation(OWNER, "native-chat")["messages"][0]["content"] == setup.resolved.snapshot.decode()
    with setup.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT minimum_reader_version FROM workflow_schema_meta").fetchone()[0] == 7
        assert connection.execute("SELECT count(*) FROM legacy_task_links").fetchone()[0] == 0
    captured: list[Any] = []
    assert setup.worker(captured).run_once(OWNER)["state"] == "succeeded"
    assert len(captured) == 1 and captured[0][0]["role"] == "system"
    assert json.loads(captured[0][0]["content"].split("\n", 1)[1])["inputs"][0]["item"]["content"] == CONTENT
    public = Run(run["run_id"], "native-chat", OWNER, run["trigger_message_id"], run["state"], config).public()
    assert "完整记忆" not in json.dumps(public, ensure_ascii=False)
    assert "workflow_memory" not in public["config"]
    with setup.workflows.transaction(write=False) as connection:
        assert "完整记忆" not in "".join(row[0] for row in connection.execute("SELECT payload FROM run_events"))


@pytest.mark.parametrize("action", ["edit", "disable", "delete", "tamper"])
def test_queued_native_memory_revocation_blocks_provider_but_original_lookup_survives(tmp_path: Path, action: str) -> None:
    setup = make_native(tmp_path)
    binding = setup.dispatch().binding
    if action == "edit": setup.memories.update(OWNER, "memory-a", {"content": "changed"}, expected_revision=0, now=105)
    if action == "disable": setup.memories.update(OWNER, "memory-a", {"enabled": False}, expected_revision=0, now=105)
    if action == "delete":
        with setup.workflows.transaction() as connection: connection.execute("DELETE FROM platform_memory_items WHERE owner_id=?", (OWNER,))
    if action == "tamper":
        with setup.workflows.transaction() as connection: connection.execute("UPDATE platform_memory_items SET content='same revision changed'")
    assert asyncio.run(setup.executor().lookup(binding.token, setup.resolved.digest, setup.resolved)) == binding
    captured: list[Any] = []
    result = setup.worker(captured).run_once(OWNER)
    assert result["state"] == "failed" and captured == []
    assert CONTENT not in result.get("result_text", "")
    assert asyncio.run(setup.executor().lookup(binding.token, setup.resolved.digest, setup.resolved)) == binding


@pytest.mark.parametrize("table", ["runs", "workflow_schema_meta"])
def test_native_memory_creation_rolls_back_snapshot_links_and_reader(tmp_path: Path, table: str, monkeypatch: Any) -> None:
    setup = make_native(tmp_path)
    original = setup.workflows.transaction
    @contextmanager
    def failing(**kwargs: Any) -> Iterator[sqlite3.Connection]:
        with original(**kwargs) as connection:
            connection.set_authorizer(lambda action, name, *rest: sqlite3.SQLITE_DENY
                if (action, name) == (sqlite3.SQLITE_INSERT if table == "runs" else sqlite3.SQLITE_UPDATE, table) else sqlite3.SQLITE_OK)
            yield connection
    monkeypatch.setattr(setup.workflows, "transaction", failing)
    with pytest.raises(WorkflowDispatchError): setup.dispatch()
    monkeypatch.setattr(setup.workflows, "transaction", original)
    with original(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
        assert connection.execute("SELECT minimum_reader_version FROM workflow_schema_meta").fetchone()[0] == 6
        assert connection.execute("SELECT child_request_id FROM workflow_budget_reservations").fetchone()[0] is None


def test_native_memory_missing_worker_guard_fails_before_provider(tmp_path: Path) -> None:
    setup = make_native(tmp_path)
    setup.dispatch()
    captured: list[Any] = []
    assert setup.worker(captured, guard=False).run_once(OWNER)["state"] == "failed"
    assert captured == []


def test_native_memory_original_run_survives_crash_before_binding_and_memory_edit(tmp_path: Path) -> None:
    setup = make_native(tmp_path)
    class Crash(RunStepExecutor):
        async def create_or_lookup(self, token: str, payload_hash: str, resolved: ResolvedContract) -> Any:
            await super().create_or_lookup(token, payload_hash, resolved)
            raise SimulatedCrash()
    with pytest.raises(SimulatedCrash):
        setup.dispatch(Crash(setup.workflows, setup.target, clock=setup.clock))
    setup.memories.update(OWNER, "memory-a", {"content": "after crash"}, expected_revision=0, now=105)
    with setup.workflows.transaction(write=False) as connection:
        token = connection.execute("SELECT token FROM workflow_dispatches").fetchone()[0]
    found = asyncio.run(setup.executor().lookup(token, setup.resolved.digest, setup.resolved))
    assert found is not None
    assert setup.platform.get_run(OWNER, found.run_id)["config_snapshot"]["workflow_memory"]["inputs"][0]["item"]["content"] == CONTENT
    captured: list[Any] = []
    assert setup.worker(captured).run_once(OWNER)["state"] == "queued"
    assert captured == []


def test_native_memory_rejects_target_marker_injection(tmp_path: Path) -> None:
    setup = make_native(tmp_path)
    with pytest.raises(ValueError, match="invalid_run_configuration"):
        replace(setup.target, config_snapshot=canonical_json({"workflow_memory": {}}))


@pytest.mark.parametrize("change", ["missing", "digest", "content", "reader", "message", "noncanonical", "both_markers"])
def test_native_memory_corruption_blocks_recovery_and_provider(tmp_path: Path, change: str) -> None:
    from hub.infrastructure.workflow_repository import WorkflowRepositoryError

    setup = make_native(tmp_path)
    binding = setup.dispatch().binding
    with setup.workflows.transaction() as connection:
        config = json.loads(connection.execute("SELECT config_snapshot FROM runs").fetchone()[0])
        if change == "missing": config.pop("workflow_memory")
        if change == "digest": config["workflow_memory"]["resolved_digest"] = "a" * 64
        if change == "content": config["workflow_memory"]["inputs"][0]["item"]["content"] = "changed"
        if change == "both_markers":
            config.pop("workflow_memory")
            config.pop("workflow_dispatch")
        if change == "reader": connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=6")
        if change == "message": connection.execute("UPDATE messages SET content='changed'")
        encoded = json.dumps(config) if change == "noncanonical" else canonical_json(config).decode()
        connection.execute("UPDATE runs SET config_snapshot=?", (encoded,))
    with pytest.raises(WorkflowDispatchError):
        asyncio.run(setup.executor().lookup(binding.token, setup.resolved.digest, setup.resolved))
    with pytest.raises(WorkflowRepositoryError): setup.workflows.init()
    captured: list[Any] = []
    assert setup.worker(captured).run_once(OWNER)["state"] == "failed"
    assert captured == []


@pytest.mark.parametrize("change", ["owner", "lease", "attempt", "config", "markers", "expired", "pause", "workspace", "generation"])
def test_native_memory_guard_rechecks_current_authority(tmp_path: Path, change: str) -> None:
    from hub.domain.workflow_native_memory import WorkflowNativeMemoryError
    from hub.infrastructure.workflow_native_memory_repository import WorkflowNativeMemoryRepository

    setup = make_native(tmp_path)
    setup.dispatch()
    claim = setup.platform.claim_run(OWNER, worker_id="native-worker", now=104, lease_s=60)
    if change == "owner": claim["owner_id"] = "other@example.test"
    if change == "lease": claim["lease_id"] = "forged"
    if change == "attempt": claim["attempt"] += 1
    if change == "config": claim["config_snapshot"]["model_profile_id"] = "other"
    if change == "markers":
        claim["config_snapshot"].pop("workflow_dispatch")
        claim["config_snapshot"].pop("workflow_memory")
    with setup.workflows.transaction() as connection:
        if change == "expired": connection.execute("UPDATE runs SET lease_expires_at=104")
        if change == "pause": connection.execute("UPDATE workflow_runs SET pause_requested=1")
        if change == "workspace": connection.execute("UPDATE workspaces SET enabled=0")
        if change == "generation": connection.execute("UPDATE workflow_steps SET is_current=0")
    with pytest.raises(WorkflowNativeMemoryError):
        WorkflowNativeMemoryRepository(setup.workflows).prepare(claim, now=105)


@pytest.mark.parametrize("encrypted", [False, True])
def test_native_memory_frozen_backup_preserves_original_after_deletion(tmp_path: Path, encrypted: bool) -> None:
    from hub.infrastructure.platform_backup import PlatformBackupService

    setup = make_native(tmp_path)
    binding = setup.dispatch().binding
    setup.memories.delete(OWNER, "memory-a", expected_revision=0)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    service = PlatformBackupService()
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    if encrypted:
        service.create_encrypted_backup(setup.workflows.db_path, artifacts, backup, b"k" * 32, "test-key")
        service.restore_encrypted_backup(backup, restored, b"k" * 32)
    else:
        service.create_backup(setup.workflows.db_path, artifacts, backup)
        service.restore_backup(backup, restored)
    repository = WorkflowRepository(restored / "platform.db")
    repository.init()
    executor = RunStepExecutor(repository, setup.target, clock=setup.clock)
    assert asyncio.run(executor.lookup(binding.token, setup.resolved.digest, setup.resolved)) == binding
    platform = PlatformRepository(repository.db_path)
    assert platform.get_run(OWNER, binding.run_id)["config_snapshot"]["workflow_memory"]["inputs"][0]["item"]["content"] == CONTENT
    with repository.transaction(write=False) as connection:
        assert connection.execute("SELECT admission_mode,minimum_reader_version FROM workflow_schema_meta").fetchone()[:] == ("frozen", 7)
        assert connection.execute("SELECT state FROM capability_reservations").fetchone()[0] == "held"
    setup.workflows, setup.platform = repository, platform
    captured: list[Any] = []
    assert setup.worker(captured).run_once(OWNER)["state"] == "failed"
    assert captured == []


@pytest.mark.parametrize("rowid", [-9223372036854775808, 0, 9223372036854775807])
def test_native_memory_history_includes_signed_rowids_and_corrupt_last_page(tmp_path: Path, rowid: int) -> None:
    from hub.infrastructure.workflow_native_memory_repository import validate_native_memory_runs
    from hub.domain.workflow_errors import WorkflowValidationError

    setup = make_native(tmp_path)
    setup.dispatch()
    with setup.workflows.transaction() as connection:
        connection.execute("UPDATE workflow_dispatches SET rowid=?", (rowid,))
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        validate_native_memory_runs(connection)
        pages = [sql for sql in statements if sql.startswith("SELECT d.rowid AS dispatch_rowid,")]
        assert len(pages) == 1 and "WHERE" not in pages[0] and pages[0].endswith("ORDER BY d.rowid LIMIT 128")
        assert not any("WHERE d.token=" in sql for sql in statements)
        connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=6")
        with pytest.raises(WorkflowValidationError, match="invalid_native_memory_reader"):
            validate_native_memory_runs(connection)


def test_native_memory_http_bootstrap_coordinator_and_worker(tmp_path: Path) -> None:
    from hub.application.workflow_coordinator import RegisteredWorkflowPlan
    from hub.bootstrap import create_app
    from hub.config import FleetConfig

    setup = make_native(tmp_path, admit=False)
    assert setup.workflows.release_claim(setup.claim, now=102, next_poll_at=102)
    definition = definition_for()
    plan = RegisteredWorkflowPlan(OWNER, WORKSPACE, hashlib.sha256(definition.snapshot).hexdigest(), "develop", CatalogRef("native", 1),
        CatalogRef("safe", 1), "native-a", 1, 100, 2)
    config = FleetConfig.from_root(tmp_path, platform_enabled=True, platform_worker_enabled=True, platform_workflows_enabled=True,
        platform_workflow_dispatch_enabled=True, platform_db=setup.workflows.db_path, dev_operator=OWNER, serve_frontend=False)
    app = create_app(config, repositories={"workflow_clock": setup.clock, "workflow_steps": (plan,),
        "workflow_executors": ((OWNER, WORKSPACE, "native-a", setup.executor()),)})
    services = app.extensions["fleet"]["services"]
    coordinator = services["workflow_coordinator"]
    for _ in range(4):
        assert not asyncio.run(coordinator.tick(setup.clock())).failures
        setup.clock.now += 1
    binding = setup.workflows.get_dispatch(OWNER, "workflow-a", "develop", 1).binding
    assert binding is not None
    worker = services["platform_worker"]
    assert worker.workflow_memory_guard is not None
    worker.clock = setup.clock
    worker.run_events.clock = setup.clock
    captured: list[Any] = []
    class Provider:
        def complete(self, messages: Any, tools: Any, *, request_observer: Any = None) -> ModelResponse:
            captured.append(messages)
            return ModelResponse(kind="final", text="done")
    worker.provider_factory = lambda profile: Provider()
    assert worker.run_once(OWNER)["state"] == "succeeded"
    assert len(captured) == 1 and json.loads(captured[0][0]["content"].split("\n", 1)[1])["inputs"][0]["item"]["content"] == CONTENT
    client = app.test_client()
    for path in (f"runs/{binding.run_id}", f"runs/{binding.run_id}/events", "conversations/native-chat", "workflows/workflow-a"):
        response = client.get("/api/platform/v1/" + path)
        assert response.status_code == 200
        raw = response.get_data(as_text=True)
        assert "workflow_memory" not in raw and "Selected memory" not in raw and "完整记忆" not in raw


def test_native_memory_history_validates_last_page_without_point_reads(tmp_path: Path) -> None:
    from hub.infrastructure.platform_backup import BackupError, PlatformBackupService
    from hub.infrastructure.workflow_native_memory_repository import validate_native_memory_runs
    from hub.infrastructure.workflow_repository import WorkflowRepositoryError
    from test_workflow_repository import legacy_budget_links

    setup = make_native(tmp_path)
    binding = setup.dispatch().binding
    with setup.workflows.transaction() as connection:
        connection.execute("UPDATE workflow_dispatches SET rowid=1000")
    legacy_budget_links(setup.workflows, tuple(range(-128, 0)))
    with setup.workflows.transaction(write=False) as connection:
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        validate_native_memory_runs(connection)
    pages = [sql for sql in statements if sql.startswith("SELECT d.rowid AS dispatch_rowid,")]
    assert len(pages) == 2 and all(sql.endswith("ORDER BY d.rowid LIMIT 128") for sql in pages)
    assert "WHERE" not in pages[0] and "WHERE d.rowid>-1" in pages[1]
    assert not any("WHERE d.token=" in sql for sql in statements)
    with setup.workflows.transaction() as connection:
        config = setup.platform.get_run(OWNER, binding.run_id)["config_snapshot"]
        config["workflow_memory"]["inputs"][0]["item"]["content"] = "corrupt final page"
        connection.execute("UPDATE runs SET config_snapshot=? WHERE run_id=?", (canonical_json(config).decode(), binding.run_id))
        connection.execute("UPDATE workflow_budget_reservations SET child_request_id=NULL WHERE workflow_id!='workflow-a'")
    with pytest.raises(WorkflowRepositoryError): setup.workflows.init()
    with setup.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM workflow_budget_reservations WHERE child_request_id IS NULL").fetchone()[0] == 128
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    with pytest.raises(BackupError):
        PlatformBackupService().create_backup(setup.workflows.db_path, artifacts, tmp_path / "bad-backup")


@pytest.mark.parametrize("change", ["remote", "cli", "old_memory"])
def test_native_memory_rejects_unsupported_targets_without_memory_read(tmp_path: Path, change: str) -> None:
    from hub.infrastructure.workflow_run_executor import LegacyTaskTarget

    setup = make_native(tmp_path)
    raw = json.loads(setup.target.config_snapshot)
    if change == "remote": raw["execution_node_id"] = "remote-node"
    if change == "old_memory": raw["memory_context"] = {"enabled": True}
    target = replace(setup.target, config_snapshot=canonical_json(raw),
        legacy_task=LegacyTaskTarget("node-a", "codex", "project") if change == "cli" else None)
    with setup.workflows.transaction() as connection:
        connection.execute("DROP TABLE platform_memory_items")
    with pytest.raises(WorkflowDispatchError, match="context_resolution_required"):
        asyncio.run(RunStepExecutor(setup.workflows, target, clock=setup.clock).create_or_lookup("a" * 64, setup.resolved.digest, setup.resolved))
    with setup.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def test_native_memory_original_binding_recovers_after_worker_restart(tmp_path: Path) -> None:
    setup = make_native(tmp_path)
    class Crash(RunStepExecutor):
        async def create_or_lookup(self, token: str, payload_hash: str, resolved: ResolvedContract) -> Any:
            await super().create_or_lookup(token, payload_hash, resolved)
            raise SimulatedCrash()
    with pytest.raises(SimulatedCrash): setup.dispatch(Crash(setup.workflows, setup.target, clock=setup.clock))
    captured: list[Any] = []
    assert setup.worker(captured).run_once(OWNER)["state"] == "queued"
    setup.workflows = WorkflowRepository(setup.workflows.db_path)
    setup.workflows.init()
    result = setup.dispatch()
    assert result.binding is not None
    assert setup.worker(captured).run_once(OWNER)["state"] == "succeeded"
    assert len(captured) == 1
    with setup.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM messages WHERE role='user'").fetchone()[0] == 1


def test_native_memory_marker_cannot_be_injected_through_turn_override(tmp_path: Path) -> None:
    from hub.bootstrap import create_app
    from hub.config import FleetConfig

    setup = make_native(tmp_path, admit=False)
    app = create_app(FleetConfig.from_root(tmp_path, platform_enabled=True,
        platform_workflows_enabled=True, platform_db=setup.workflows.db_path, dev_operator=OWNER, serve_frontend=False))
    response = app.test_client().post("/api/platform/v1/conversations/native-chat/turns", json={
        "text": "reference", "client_token": "injected", "overrides": {"workflow_memory": {"private": "fake"}}})
    assert response.status_code == 400
    with setup.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


@pytest.mark.parametrize("invalid", ["owner", "kind", "tags", "enabled"])
def test_native_memory_creation_fails_for_invalid_current_values(tmp_path: Path, invalid: str) -> None:
    setup = make_native(tmp_path)
    with setup.workflows.transaction() as connection:
        if invalid == "owner": connection.execute("UPDATE platform_memory_items SET owner_id='other@example.test'")
        if invalid == "kind": connection.execute("UPDATE platform_memory_items SET kind='corrupt'")
        if invalid == "tags": connection.execute("UPDATE platform_memory_items SET tags='{}'")
        if invalid == "enabled": connection.execute("UPDATE platform_memory_items SET enabled=0")
    with pytest.raises(WorkflowDispatchError): setup.dispatch()
    with setup.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert connection.execute("SELECT minimum_reader_version FROM workflow_schema_meta").fetchone()[0] == 6


def test_native_memory_config_envelope_budget_rolls_back_before_run_creation(tmp_path: Path) -> None:
    setup = make_native(tmp_path)
    value = json.loads(setup.target.config_snapshot)
    value["provider_snapshot"]["provider_config"]["padding"] = "p" * 65000
    target = replace(setup.target, config_snapshot=canonical_json(value))
    with pytest.raises(WorkflowDispatchError, match="context_too_large"):
        setup.dispatch(RunStepExecutor(setup.workflows, target, clock=setup.clock))
    with setup.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert connection.execute("SELECT minimum_reader_version FROM workflow_schema_meta").fetchone()[0] == 6
