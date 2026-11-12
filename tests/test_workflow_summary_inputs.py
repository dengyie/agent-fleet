"""Actual original Native/external consumption of explicit versioned summaries."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from typing import Any

import pytest

from hub.domain.workflow_contract import parse_definition, resolve_contract
from hub.domain.workflow_contract_types import AuthorizedInputs, CatalogRef, InputBinding
from hub.domain.workflow_identity import canonical_json
from hub.infrastructure.workflow_repository import WorkflowRepository
from hub.infrastructure.workflow_summary_repository import WorkflowSummaryRepository
from test_workflow_dispatch import Clock, SimulatedCrash
from test_workflow_repository import OWNER, WORKSPACE, prepare_database, registered
from test_workflow_summary import CONTENT, publish, reference


def definition_for(item: Any, *, kind: str = "native_run", count: int = 1) -> Any:
    raw = json.loads(registered().snapshot)
    raw["steps"][0]["kind"] = kind
    raw["steps"][0]["contract"].update(output_specs=[], context_refs=[{"ref_key": f"summary-{index}", "kind": "summary",
        "ref_id": item.summary_id, "revision": item.revision, "sha256": item.sha256} for index in range(count)])
    return parse_definition(canonical_json(raw))


def frozen(repository: Any, item: Any, *, kind: str, adapter: str, target: str, external_target: Any = None) -> tuple[Any, Any]:
    definition = definition_for(item, kind=kind)
    repository.create_or_get(OWNER, WORKSPACE, "workflow-a", client_token="summary-workflow", definition=definition, now=100)
    claim = repository.claim_due(OWNER, worker_id="summary-coordinator", now=100, limit=1,
        lease_seconds=20 if kind == "external_agent" else 60)[0]
    resolved = resolve_contract(definition, AuthorizedInputs(OWNER, WORKSPACE, "develop", CatalogRef(adapter, 1),
        (InputBinding("summary-0", OWNER, WORKSPACE, reference(item)),)))
    ready = repository.freeze_inputs(claim, step_key="develop", generation=1, resolved=resolved, expected_revision=1, now=101)
    repository.admit_step(claim, step_key="develop", generation=1, expected_revision=ready.revision,
        target_key=target, capacity=1, reserve_tokens=100, reserve_requests=2, now=102, external_target=external_target)
    return claim, resolved


def native_setup(tmp_path: Path) -> tuple[Any, Any, Any]:
    from hub.infrastructure.platform_db import PlatformRepository
    from hub.infrastructure.workflow_run_executor import RegisteredRunTarget
    from test_workflow_native_memory import NativeSetup

    repository = prepare_database(tmp_path / "platform.db")
    store = WorkflowSummaryRepository(repository)
    item = publish(store)
    platform = PlatformRepository(repository.db_path)
    platform.upsert_workspace(OWNER, {"workspace_id": WORKSPACE, "root_path": str(tmp_path / "workspace")})
    platform.upsert_model(OWNER, {"profile_id": "local", "provider": "deterministic", "model": "local"})
    platform.create_conversation(OWNER, "native-chat", title="Native", workspace_id=WORKSPACE)
    claim, resolved = frozen(repository, item, kind="native_run", adapter="native", target="native-a")
    target = RegisteredRunTarget(OWNER, WORKSPACE, "native-a", CatalogRef("native", 1), CatalogRef("safe", 1),
        "native-chat", canonical_json({"model_profile_id": "local", "provider_snapshot": {
            "provider": "deterministic", "model": "local", "provider_config": {}, "capabilities": {}}}))
    return NativeSetup(repository, platform, None, target, resolved, Clock(), claim), store, item


def external_setup(tmp_path: Path) -> tuple[Any, Any, Any, Any]:
    from hub.application.workflow_external_execution import ExternalStepExecutor
    from hub.application.workflow_external_inputs import WorkflowExternalInputs
    from hub.infrastructure.workflow_external_repository import ExternalWorkflowRepository
    from hub.platform.artifacts import ArtifactStore
    from test_external_agent_workflow import make_external

    original = make_external(tmp_path, admit=False)
    repository = prepare_database(tmp_path / "summary-platform.db")
    summaries = WorkflowSummaryRepository(repository)
    item = publish(summaries)
    store = ExternalWorkflowRepository(repository)
    store.init()
    claim, resolved = frozen(repository, item, kind="external_agent", adapter="muse", target=original.target.target_key,
        external_target=original.target)
    setup = replace(original, repository=repository, store=store, claim=claim, resolved=resolved)
    artifacts = ArtifactStore(tmp_path / "summary-artifacts")
    inputs = WorkflowExternalInputs(artifacts, summaries=summaries)
    executor = ExternalStepExecutor(store, setup.target, setup.adapter, clock=setup.clock, enabled=True, inputs=inputs)
    return setup, executor, summaries, item


def test_summary_native_domain_roundtrip_preserves_complete_content_and_legacy_schema() -> None:
    from hub.domain.workflow_native_memory import build_native_memory, parse_native_memory
    from hub.domain.workflow_summary import WorkflowSummaryVersion
    from test_workflow_native_memory import resolved_for
    from test_external_agent_memory_inputs import memory_item

    item = WorkflowSummaryVersion(OWNER, WORKSPACE, "summary-a", 1, "人工摘要", CONTENT, OWNER, 100000)
    definition = definition_for(item)
    resolved = resolve_contract(definition, AuthorizedInputs(OWNER, WORKSPACE, "develop", CatalogRef("native", 1),
        (InputBinding("summary-0", OWNER, WORKSPACE, reference(item)),)))
    snapshot = build_native_memory(resolved, (), summaries=(("summary-0", item),))
    assert json.loads(snapshot.snapshot)["schema_version"] == 3
    assert json.loads(snapshot.snapshot)["summary_inputs"][0]["item"]["content"] == CONTENT
    assert parse_native_memory(json.loads(snapshot.snapshot), resolved) == snapshot
    old = build_native_memory(resolved_for(), (memory_item(),))
    assert json.loads(old.snapshot)["schema_version"] == 1 and "summary_inputs" not in json.loads(old.snapshot)


def test_native_summary_worker_consumes_original_version_after_new_head(tmp_path: Path) -> None:
    setup, summaries, first = native_setup(tmp_path)
    binding = setup.dispatch().binding
    publish(summaries, token="second", epoch=1, content="new summary", now=104)
    captured: list[Any] = []
    assert setup.worker(captured).run_once(OWNER)["state"] == "succeeded"
    saved = setup.platform.get_run(OWNER, binding.run_id)["config_snapshot"]["workflow_memory"]
    assert saved["schema_version"] == 3 and saved["summary_inputs"][0]["item"]["content"] == CONTENT
    assert json.loads(captured[0][0]["content"].split("\n", 1)[1]) == saved
    assert saved["summary_inputs"][0]["item"]["revision"] == first.revision
    with setup.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT minimum_reader_version FROM workflow_schema_meta").fetchone()[0] == 11


def test_native_summary_disabled_after_dispatch_blocks_provider_but_keeps_original_lookup(tmp_path: Path) -> None:
    setup, summaries, _ = native_setup(tmp_path)
    binding = setup.dispatch().binding
    summaries.set_enabled(OWNER, WORKSPACE, "summary-a", expected_epoch=1, enabled=False)
    assert asyncio.run(setup.executor().lookup(binding.token, setup.resolved.digest, setup.resolved)) == binding
    captured: list[Any] = []
    assert setup.worker(captured).run_once(OWNER)["state"] == "failed" and captured == []


def test_native_summary_crash_before_binding_recovers_original_run_and_snapshot(tmp_path: Path) -> None:
    from hub.infrastructure.workflow_run_executor import RunStepExecutor

    setup, summaries, _ = native_setup(tmp_path)
    class Crash(RunStepExecutor):
        async def create_or_lookup(self, token: str, payload_hash: str, resolved: Any) -> Any:
            await super().create_or_lookup(token, payload_hash, resolved)
            raise SimulatedCrash()
    with pytest.raises(SimulatedCrash): setup.dispatch(Crash(setup.workflows, setup.target, clock=setup.clock))
    publish(summaries, token="second", epoch=1, content="new summary", now=104)
    with setup.workflows.transaction(write=False) as connection:
        token = connection.execute("SELECT token FROM workflow_dispatches").fetchone()[0]
    binding = asyncio.run(setup.executor().lookup(token, setup.resolved.digest, setup.resolved))
    assert binding is not None
    assert setup.platform.get_run(OWNER, binding.run_id)["config_snapshot"]["workflow_memory"]["summary_inputs"][0]["item"]["content"] == CONTENT
    with setup.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 1


@pytest.mark.parametrize("change", ["content", "missing_version", "reader", "scope"])
def test_native_summary_corruption_blocks_original_recovery_and_provider(tmp_path: Path, change: str) -> None:
    from hub.application.workflow_dispatch import WorkflowDispatchError
    from hub.infrastructure.workflow_repository import WorkflowRepositoryError

    setup, _, _ = native_setup(tmp_path)
    binding = setup.dispatch().binding
    with setup.workflows.transaction() as connection:
        if change in ("content", "scope"):
            config = setup.platform.get_run(OWNER, binding.run_id)["config_snapshot"]
            config["workflow_memory"]["summary_inputs"][0]["item"]["content" if change == "content" else "workspace_id"] = "changed"
            connection.execute("UPDATE runs SET config_snapshot=?", (canonical_json(config).decode(),))
        if change == "missing_version": connection.execute("DELETE FROM workflow_summary_versions")
        if change == "reader": connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=10")
    with pytest.raises((WorkflowDispatchError, WorkflowRepositoryError)):
        asyncio.run(setup.executor().lookup(binding.token, setup.resolved.digest, setup.resolved))
    captured: list[Any] = []
    assert setup.worker(captured).run_once(OWNER)["state"] == "failed" and captured == []


def test_external_summary_adapter_receives_complete_original_prompt_and_recovery(tmp_path: Path) -> None:
    setup, executor, summaries, item = external_setup(tmp_path)
    observed = setup.dispatch(executor)
    assert observed.binding is not None and len(setup.transport.sends) == 1
    pin = setup.store.lookup(observed.binding.token, setup.resolved.digest, setup.resolved, setup.target)
    prompt = json.loads(pin.request.prompt)
    assert pin.request.prompt in setup.transport.sends[0]["prompt"]
    assert prompt["schema_version"] == 3 and prompt["summary_inputs"][0]["item"]["content"] == CONTENT
    assert prompt["summary_inputs"][0]["sha256"] == item.sha256
    publish(summaries, token="second", epoch=1, content="second", now=104)
    assert asyncio.run(executor.lookup(observed.binding.token, setup.resolved.digest, setup.resolved)) == observed.binding
    assert len(setup.transport.sends) == 1
    assert setup.store.lookup(observed.binding.token, setup.resolved.digest, setup.resolved, setup.target).request == pin.request


def test_external_summary_revocation_before_first_send_has_zero_sends(tmp_path: Path) -> None:
    from hub.application.workflow_dispatch import WorkflowDispatchError

    setup, executor, summaries, _ = external_setup(tmp_path)
    summaries.set_enabled(OWNER, WORKSPACE, "summary-a", expected_epoch=1, enabled=False)
    with pytest.raises(WorkflowDispatchError): setup.dispatch(executor)
    assert setup.transport.sends == []


def test_external_summary_lost_binding_recovers_original_journal_after_disable(tmp_path: Path, monkeypatch: Any) -> None:
    from test_external_agent_workflow import held

    setup, executor, summaries, _ = external_setup(tmp_path)
    original = setup.store.bind_task
    def crash(*args: Any, **kwargs: Any) -> Any:
        raise SimulatedCrash()
    monkeypatch.setattr(setup.store, "bind_task", crash)
    with pytest.raises(SimulatedCrash): setup.dispatch(executor)
    summaries.set_enabled(OWNER, WORKSPACE, "summary-a", expected_epoch=1, enabled=False)
    monkeypatch.setattr(setup.store, "bind_task", original)
    setup.recover()
    observed = setup.dispatch(executor)
    assert observed.binding is not None and held(setup) == "held" and len(setup.transport.sends) == 1
    prompt = setup.store.lookup(observed.binding.token, setup.resolved.digest, setup.resolved, setup.target).request.prompt
    assert json.loads(prompt)["summary_inputs"][0]["item"]["content"] == CONTENT


@pytest.mark.parametrize("change", ["content", "hash", "revision", "key", "scope", "missing", "extra", "schema", "order"])
def test_summary_private_payloads_reject_tampering(tmp_path: Path, change: str) -> None:
    from hub.domain.external_request import build_external_request, parse_external_request
    from hub.domain.workflow_native_memory import build_native_memory, parse_native_memory

    setup, _, item = native_setup(tmp_path)
    native = build_native_memory(setup.resolved, (), summaries=(("summary-0", item),))
    external = build_external_request(setup.resolved, summaries=(("summary-0", item),))
    for source, is_native in [(json.loads(native.snapshot), True), (json.loads(external.prompt), False)]:
        entry = source["summary_inputs"][0]
        if change == "content": entry["item"]["content"] = "changed"
        if change == "hash": entry["sha256"] = "a" * 64
        if change == "revision": entry["revision"] = True
        if change == "key": entry["ref_key"] = "other"
        if change == "scope": entry["item"]["workspace_id"] = "other"
        if change == "missing": source["summary_inputs"] = []
        if change == "extra": entry["item"]["verified"] = True
        if change == "schema": source["schema_version"] = 2
        if change == "order": source["summary_inputs"].append(entry.copy())
        with pytest.raises(ValueError):
            if is_native:
                parse_native_memory(source, setup.resolved)
            else:
                parse_external_request(canonical_json({"schema_version": 1, "prompt": canonical_json(source).decode()}), setup.resolved)


def test_summary_external_request_rollback_has_zero_sends_and_no_partial_intent(tmp_path: Path, monkeypatch: Any) -> None:
    from contextlib import contextmanager
    import sqlite3
    from hub.application.workflow_dispatch import WorkflowDispatchError

    setup, executor, _, _ = external_setup(tmp_path)
    original = setup.repository.transaction
    @contextmanager
    def rejecting(**kwargs: Any) -> Any:
        with original(**kwargs) as connection:
            connection.set_authorizer(lambda action, table, column, *rest: sqlite3.SQLITE_DENY
                if (action, table, column) == (sqlite3.SQLITE_UPDATE, "workflow_external_bindings", "request_snapshot") else sqlite3.SQLITE_OK)
            yield connection
    monkeypatch.setattr(setup.repository, "transaction", rejecting)
    with pytest.raises(WorkflowDispatchError): setup.dispatch(executor)
    assert setup.transport.sends == []
    with original(write=False) as connection:
        assert tuple(connection.execute("SELECT send_started_at,request_snapshot FROM workflow_external_bindings").fetchone()) == (None, None)


def test_summary_bootstrap_coordinator_worker_and_private_public_dtos(tmp_path: Path) -> None:
    import hashlib
    from hub.application.workflow_coordinator import RegisteredWorkflowPlan
    from hub.bootstrap import create_app
    from hub.config import FleetConfig
    from hub.platform.providers.base import ModelResponse

    setup, _, item = native_setup(tmp_path)
    assert setup.workflows.release_claim(setup.claim, now=102, next_poll_at=102)
    definition = definition_for(item)
    plan = RegisteredWorkflowPlan(OWNER, WORKSPACE, hashlib.sha256(definition.snapshot).hexdigest(), "develop", CatalogRef("native", 1),
        CatalogRef("safe", 1), "native-a", 1, 100, 2)
    config = FleetConfig.from_root(tmp_path, platform_enabled=True, platform_worker_enabled=True, platform_workflows_enabled=True,
        platform_workflow_dispatch_enabled=True, platform_db=setup.workflows.db_path, dev_operator=OWNER, serve_frontend=False)
    app = create_app(config, repositories={"workflow_clock": setup.clock, "workflow_steps": (plan,),
        "workflow_executors": ((OWNER, WORKSPACE, "native-a", setup.executor()),)})
    services = app.extensions["fleet"]["services"]
    for _ in range(4):
        assert not asyncio.run(services["workflow_coordinator"].tick(setup.clock())).failures
        setup.clock.now += 1
    binding = setup.workflows.get_dispatch(OWNER, "workflow-a", "develop", 1).binding
    assert binding is not None
    captured = []
    class Provider:
        def complete(self, messages: Any, tools: Any, *, request_observer: Any = None) -> ModelResponse:
            captured.append(messages)
            return ModelResponse(kind="final", text="done")
    worker = services["platform_worker"]
    worker.clock = worker.run_events.clock = setup.clock
    worker.provider_factory = lambda profile: Provider()
    assert worker.run_once(OWNER)["state"] == "succeeded"
    assert json.loads(captured[0][0]["content"].split("\n", 1)[1])["summary_inputs"][0]["item"]["content"] == CONTENT
    client = app.test_client()
    for path in (f"runs/{binding.run_id}", f"runs/{binding.run_id}/events", "conversations/native-chat", "workflows/workflow-a"):
        response = client.get("/api/platform/v1/" + path)
        assert response.status_code == 200
        assert "summary_inputs" not in response.get_data(as_text=True) and "显式摘要" not in response.get_data(as_text=True)


def test_external_summary_revocation_commits_before_request_intent(tmp_path: Path) -> None:
    from hub.infrastructure.workflow_repository import WorkflowRepositoryError
    from hub.application.workflow_dispatch import WorkflowDispatchService

    setup, executor, summaries, _ = external_setup(tmp_path)
    request = executor.inputs.collect(setup.resolved)
    summaries.set_enabled(OWNER, WORKSPACE, "summary-a", expected_epoch=1, enabled=False)
    # Move to the original sending state through the actual dispatcher before
    # calling the repository boundary with the previously collected request.
    class Capture:
        capabilities = executor.capabilities
        async def lookup(self, *args: Any) -> Any: return None
        async def create_or_lookup(self, *args: Any) -> Any: raise SimulatedCrash()
    with pytest.raises(SimulatedCrash):
        asyncio.run(WorkflowDispatchService(setup.repository, Capture(), clock=setup.clock).dispatch(
            setup.claim, step_key="develop", generation=1, expected_revision=setup.repository.get(OWNER, "workflow-a").revision))
    record = setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1)
    with pytest.raises(WorkflowRepositoryError, match="summary_selection_stale"):
        setup.store.prepare_send(record.token, setup.resolved.digest, setup.resolved, setup.target, now=setup.clock(), request=request)
    with setup.repository.transaction(write=False) as connection:
        assert tuple(connection.execute("SELECT send_started_at,request_snapshot FROM workflow_external_bindings").fetchone()) == (None, None)
    assert setup.transport.sends == []


@pytest.mark.parametrize("kind", ["native_run", "external_agent"])
def test_mixed_summary_memory_text_full_values_and_shared_budget(tmp_path: Path, kind: str) -> None:
    import hashlib
    from hub.domain.external_request import ExternalMemoryInput, build_external_request, parse_external_request
    from hub.domain.workflow_native_memory import build_native_memory, parse_native_memory
    from hub.domain.workflow_text_inputs import WorkflowTextInput
    from test_external_agent_memory_inputs import memory_item

    item = publish(WorkflowSummaryRepository(prepare_database(tmp_path / "platform.db")))
    raw = json.loads(definition_for(item, kind=kind).snapshot)
    raw["steps"][0]["contract"].update(schema_version=2, input_refs=[{"ref_key": "text", "kind": "artifact",
        "artifact_id": "text-a", "sha256": hashlib.sha256(b"full text").hexdigest(), "size": 9}])
    raw["steps"][0]["contract"]["context_refs"].insert(0, {"ref_key": "memory", "kind": "memory", "ref_id": "memory-a", "revision": 0})
    definition = parse_definition(canonical_json(raw))
    contract = definition.steps[0].contract
    resolved = resolve_contract(definition, AuthorizedInputs(OWNER, WORKSPACE, "develop", CatalogRef("native", 1),
        tuple(InputBinding(named.ref_key, OWNER, WORKSPACE, named.reference) for named in contract.input_refs + contract.context_refs)))
    text = WorkflowTextInput("text", contract.input_refs[0].reference, "text/plain", "full text")
    memories, summaries = (memory_item(),), (("summary-0", item),)
    if kind == "native_run":
        built = build_native_memory(resolved, memories, texts=(text,), summaries=summaries)
        assert parse_native_memory(json.loads(built.snapshot), resolved) == built
        value = json.loads(built.snapshot)
        memory_values = value["inputs"]
    else:
        built = build_external_request(resolved, (text,), memories=(ExternalMemoryInput("memory", memories[0]),), summaries=summaries)
        assert parse_external_request(built.snapshot, resolved) == built
        value = json.loads(built.prompt)
        memory_values = value["memory_inputs"]
    assert memory_values[0]["item"]["content"] == memories[0].content
    assert value["summary_inputs"][0]["item"]["content"] == CONTENT
    assert value["text_inputs"][0]["text"] == "full text"
    big_memory = memory_item(content="m" * 4200)
    big_summary = replace(item, content="s" * 4200)
    raw["steps"][0]["contract"]["context_refs"][1]["sha256"] = big_summary.sha256
    definition = parse_definition(canonical_json(raw))
    contract = definition.steps[0].contract
    resolved = resolve_contract(definition, AuthorizedInputs(OWNER, WORKSPACE, "develop", CatalogRef("native", 1),
        tuple(InputBinding(named.ref_key, OWNER, WORKSPACE, named.reference) for named in contract.input_refs + contract.context_refs)))
    with pytest.raises(ValueError, match="context_too_large"):
        if kind == "native_run":
            build_native_memory(resolved, (big_memory,), texts=(text,), summaries=(("summary-0", big_summary),))
        else:
            build_external_request(resolved, (text,), memories=(ExternalMemoryInput("memory", big_memory),), summaries=(("summary-0", big_summary),))


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("encrypted", [False, True])
def test_summary_execution_backups_preserve_exact_original_and_frozen(tmp_path: Path, native: bool, encrypted: bool) -> None:
    from hub.infrastructure.platform_backup import PlatformBackupService
    from hub.infrastructure.platform_db import PlatformRepository

    if native:
        setup, summaries, _ = native_setup(tmp_path)
        binding = setup.dispatch().binding
        repository = setup.workflows
    else:
        setup, executor, summaries, _ = external_setup(tmp_path)
        binding = setup.dispatch(executor).binding
        repository = setup.repository
    summaries.set_enabled(OWNER, WORKSPACE, "summary-a", expected_epoch=1, enabled=False)
    artifacts, backup, restored = tmp_path / "empty-artifacts", tmp_path / "backup", tmp_path / "restored"
    artifacts.mkdir()
    service = PlatformBackupService()
    if encrypted:
        service.create_encrypted_backup(repository.db_path, artifacts, backup, b"k"*32, "test-key")
        service.restore_encrypted_backup(backup, restored, b"k"*32)
    else:
        service.create_backup(repository.db_path, artifacts, backup)
        service.restore_backup(backup, restored)
    recovered = WorkflowRepository(restored / "platform.db")
    recovered.init()
    with recovered.transaction(write=False) as connection:
        assert tuple(connection.execute("SELECT admission_mode,minimum_reader_version FROM workflow_schema_meta").fetchone()) == ("frozen", 11)
    if native:
        assert PlatformRepository(recovered.db_path).get_run(OWNER, binding.run_id)["config_snapshot"]["workflow_memory"]["summary_inputs"][0]["item"]["content"] == CONTENT
    else:
        from hub.infrastructure.workflow_external_repository import ExternalWorkflowRepository
        pin = ExternalWorkflowRepository(recovered).lookup(binding.token, setup.resolved.digest, setup.resolved, setup.target)
        assert json.loads(pin.request.prompt)["summary_inputs"][0]["item"]["content"] == CONTENT


def test_summary_coordinator_resolver_authorizes_actual_version_and_freezes_reference(tmp_path: Path) -> None:
    from hub.infrastructure.workflow_input_resolver import WorkflowArtifactInputResolver
    from hub.platform.artifacts import ArtifactStore

    setup, summaries, _ = native_setup(tmp_path)
    snapshot = setup.workflows.snapshots((setup.claim,), now=103)[0]
    resolver = WorkflowArtifactInputResolver(setup.workflows, ArtifactStore(tmp_path / "artifacts"), summaries=summaries)
    resolved = asyncio.run(resolver.resolve(snapshot, "develop", CatalogRef("native", 1)))
    assert resolved == setup.resolved and CONTENT.encode() not in resolved.snapshot


@pytest.mark.parametrize("change", ["body", "missing_version", "low_reader", "external_metadata"])
def test_external_summary_history_rejects_corrupt_saved_or_missing_authority(tmp_path: Path, change: str) -> None:
    from hub.infrastructure.platform_backup import BackupError, PlatformBackupService
    from hub.infrastructure.workflow_repository import WorkflowRepositoryError

    setup, executor, _, _ = external_setup(tmp_path)
    observed = setup.dispatch(executor)
    with setup.repository.transaction() as connection:
        if change == "body":
            raw = json.loads(connection.execute("SELECT request_snapshot FROM workflow_external_bindings").fetchone()[0])
            prompt = json.loads(raw["prompt"])
            prompt["summary_inputs"][0]["item"]["content"] = "changed"
            raw["prompt"] = canonical_json(prompt).decode()
            connection.execute("UPDATE workflow_external_bindings SET request_snapshot=?", (canonical_json(raw),))
        if change == "missing_version": connection.execute("DELETE FROM workflow_summary_versions")
        if change == "low_reader": connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=10")
        if change == "external_metadata": connection.execute("UPDATE workflow_external_schema_meta SET schema_version=3,minimum_reader_version=3")
    with pytest.raises(WorkflowRepositoryError):
        setup.store.lookup(observed.binding.token, setup.resolved.digest, setup.resolved, setup.target)
    with pytest.raises(WorkflowRepositoryError): setup.repository.init()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    with pytest.raises(BackupError): PlatformBackupService().create_backup(setup.repository.db_path, artifacts, tmp_path / "bad")


def test_summary_native_history_rejects_corrupt_final_dispatch_page(tmp_path: Path) -> None:
    from hub.infrastructure.workflow_native_memory_repository import validate_native_memory_runs
    from hub.infrastructure.workflow_repository import WorkflowRepositoryError
    from test_workflow_repository import legacy_budget_links

    setup, _, _ = native_setup(tmp_path)
    binding = setup.dispatch().binding
    with setup.workflows.transaction() as connection:
        connection.execute("UPDATE workflow_dispatches SET rowid=1000")
    legacy_budget_links(setup.workflows, tuple(range(-128, 0)))
    with setup.workflows.transaction(write=False) as connection:
        statements = []
        connection.set_trace_callback(statements.append)
        validate_native_memory_runs(connection)
        pages = [sql for sql in statements if sql.startswith("SELECT d.rowid AS dispatch_rowid,")]
        assert len(pages) == 2
    config = setup.platform.get_run(OWNER, binding.run_id)["config_snapshot"]
    config["workflow_memory"]["summary_inputs"][0]["item"]["content"] = "changed"
    with setup.workflows.transaction() as connection:
        connection.execute("UPDATE runs SET config_snapshot=? WHERE run_id=?", (canonical_json(config).decode(), binding.run_id))
    with pytest.raises((ValueError, WorkflowRepositoryError)): setup.workflows.init()


def test_summary_inputs_count_duplicate_logical_refs_in_shared_budget(tmp_path: Path) -> None:
    from hub.domain.workflow_native_memory import build_native_memory
    from hub.domain.external_request import build_external_request

    repository = prepare_database(tmp_path / "platform.db")
    item = publish(WorkflowSummaryRepository(repository), content="x" * 4200)
    definition = definition_for(item, count=2)
    resolved = resolve_contract(definition, AuthorizedInputs(OWNER, WORKSPACE, "develop", CatalogRef("native", 1),
        tuple(InputBinding(f"summary-{index}", OWNER, WORKSPACE, reference(item)) for index in range(2))))
    with pytest.raises(ValueError, match="context_too_large"):
        build_native_memory(resolved, (), summaries=(("summary-0", item), ("summary-1", item)))
    with pytest.raises(ValueError, match="context_too_large"):
        build_external_request(resolved, summaries=(("summary-0", item), ("summary-1", item)))
