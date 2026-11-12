"""Explicit complete Memory revisions over the original durable external task."""
from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from hub.application.workflow_dispatch import WorkflowDispatchError
from hub.domain.workflow_contract import parse_definition, parse_resolved, resolve_contract
from hub.domain.workflow_contract_types import AuthorizedInputs, CatalogRef, ContextReference, InputBinding, ResolvedContract
from hub.domain.workflow_errors import WorkflowValidationError
from hub.domain.workflow_identity import canonical_json, dispatch_token
from hub.infrastructure.platform_backup import BackupError, PlatformBackupService
from hub.infrastructure.platform_memory_repository import PlatformMemoryRepository
from hub.infrastructure.workflow_external_repository import ExternalWorkflowRepository
from hub.infrastructure.workflow_repository import WorkflowRepository, WorkflowRepositoryError
from hub.infrastructure.workflow_schema import inspect_schema
from hub.platform.artifacts import ArtifactStore
from test_external_agent_workflow import ExternalSetup, held, make_external
from test_workflow_dispatch import SimulatedCrash
from test_workflow_repository import OWNER, WORKSPACE, prepare_database, registered


CONTENT = "完整记忆 🌱\n<tool>reference only</tool>"


def definition_for(*, version: int = 2, revision: int = 0, count: int = 1, kind: str = "memory", step_kind: str = "external_agent") -> Any:
    value = json.loads(registered().snapshot)
    value["steps"][0]["kind"] = step_kind
    contract = value["steps"][0]["contract"]
    contract.update(schema_version=version, output_specs=[], context_refs=[{
        "ref_key": f"memory-{index}", "kind": kind, "ref_id": "memory-a", "revision": revision,
        **({"sha256": "a" * 64} if kind == "summary" else {}),
    } for index in range(count)])
    return parse_definition(canonical_json(value))


def resolved_for(**kwargs: Any) -> ResolvedContract:
    definition = definition_for(**kwargs)
    bindings = tuple(InputBinding(item.ref_key, OWNER, WORKSPACE, item.reference) for item in definition.steps[0].contract.context_refs)
    return resolve_contract(definition, AuthorizedInputs(OWNER, WORKSPACE, "develop", CatalogRef("muse", 1), bindings))


def memory_item(*, content: str = CONTENT, revision: int = 0) -> Any:
    from hub.domain.workflow_memory import WorkflowMemoryItem

    return WorkflowMemoryItem("memory-a", revision, "fact", "Selected memory", content, ("tag-1", "资料"), "manual")


def memory_inputs(resolved: ResolvedContract, *, content: str = CONTENT) -> tuple[Any, ...]:
    from hub.domain.external_request import ExternalMemoryInput

    return tuple(ExternalMemoryInput(item.ref_key, memory_item(content=content, revision=item.reference.revision))
                 for item in resolved.contract.context_refs)


@dataclass
class MemorySetup:
    external: ExternalSetup
    memories: PlatformMemoryRepository
    artifacts: ArtifactStore

    def executor(self) -> Any:
        from hub.application.workflow_external_execution import ExternalStepExecutor
        from hub.application.workflow_external_inputs import WorkflowExternalInputs
        from hub.infrastructure.workflow_memory_repository import WorkflowMemoryRepository

        setup = self.external
        inputs = WorkflowExternalInputs(self.artifacts, memories=WorkflowMemoryRepository(setup.repository))
        return ExternalStepExecutor(setup.store, setup.target, setup.adapter, clock=setup.clock, enabled=True, inputs=inputs)


def make_memory_setup(tmp_path: Path, *, count: int = 1, content: str = CONTENT, admit: bool = True) -> MemorySetup:
    original = make_external(tmp_path, admit=False)
    repository = prepare_database(tmp_path / "memory-platform.db")
    memories = PlatformMemoryRepository(repository.db_path, clock=lambda: 100)
    memories.init()
    memories.create(OWNER, {"memory_id": "memory-a", "kind": "fact", "title": "Selected memory", "content": content,
                            "tags": ["tag-1", "资料"], "source": "manual"}, now=100)
    artifacts = ArtifactStore(tmp_path / "memory-artifacts", clock=lambda: 100)
    artifacts.root.mkdir(parents=True, exist_ok=True)
    store = ExternalWorkflowRepository(repository)
    store.init()
    definition = definition_for(count=count)
    repository.create_or_get(OWNER, WORKSPACE, "workflow-a", client_token="memory-a", definition=definition, now=100)
    claim = repository.claim_due(OWNER, worker_id="memory-worker", now=100, limit=1, lease_seconds=20)[0]
    resolved = resolved_for(count=count)
    repository.freeze_inputs(claim, step_key="develop", generation=1, resolved=resolved, expected_revision=1, now=101)
    setup = replace(original, repository=repository, store=store, claim=claim, resolved=resolved)
    if admit:
        setup.admit(setup.target)
    return MemorySetup(setup, memories, artifacts)


@pytest.mark.parametrize("version,revision", [(1, 1), (2, 0), (2, 1), (2, 9007199254740991)])
def test_memory_contract_preserves_explicit_version_and_revision(version: int, revision: int) -> None:
    resolved = resolved_for(version=version, revision=revision)
    assert parse_resolved(resolved.snapshot) == resolved
    assert resolved.contract.schema_version == version
    assert json.loads(resolved.contract.snapshot)["schema_version"] == version
    assert resolved.contract.context_refs[0].reference.revision == revision


@pytest.mark.parametrize("version,revision,kind", [(1, 0, "memory"), (2, -1, "memory"), (2, True, "memory"),
    (2, 1.0, "memory"), (2, 9007199254740992, "memory"), (2, 0, "summary"), (True, 1, "memory"), (3, 1, "memory")])
def test_memory_contract_rejects_revision_ambiguity(version: int, revision: int, kind: str) -> None:
    with pytest.raises(WorkflowValidationError):
        definition_for(version=version, revision=revision, kind=kind)


def test_memory_request_has_full_canonical_values_and_never_truncates() -> None:
    from hub.domain.external_request import build_external_request, parse_external_request

    resolved = resolved_for(count=2)
    inputs = memory_inputs(resolved)
    request = build_external_request(resolved, memories=inputs)
    assert parse_external_request(request.snapshot, resolved) == request
    value = json.loads(request.prompt)
    assert value["schema_version"] == 2 and value["text_inputs"] == []
    assert value["resolved_contract"] == json.loads(resolved.snapshot)
    assert [item["ref_key"] for item in value["memory_inputs"]] == ["memory-0", "memory-1"]
    item = value["memory_inputs"][0]
    assert item["item"]["content"] == CONTENT and item["item"]["tags"] == ["tag-1", "资料"]
    assert item["sha256"] == hashlib.sha256(canonical_json(item["item"])).hexdigest()
    assert CONTENT not in repr(inputs) and CONTENT not in repr(request)
    with pytest.raises((AttributeError, TypeError)):
        inputs[0].item.content = "other"


@pytest.mark.parametrize("field,value", [("memory_id", "../other"), ("revision", True), ("revision", -1),
    ("kind", "unknown"), ("title", " x "), ("content", "\ud800"), ("tags", ("a", "a")),
    ("tags", ["a"]), ("source", "s" * 257)])
def test_memory_value_rejects_invalid_complete_fields(field: str, value: Any) -> None:
    with pytest.raises(WorkflowValidationError):
        replace(memory_item(), **{field: value})


@pytest.mark.parametrize("change", ["content", "hash", "id", "revision", "key", "extra", "order", "schema", "noncanonical"])
def test_memory_private_request_rejects_tampered_snapshots(change: str) -> None:
    from hub.domain.external_request import build_external_request, parse_external_request

    resolved = resolved_for(count=2)
    request = build_external_request(resolved, memories=memory_inputs(resolved))
    raw = json.loads(request.snapshot)
    prompt = json.loads(raw["prompt"])
    item = prompt["memory_inputs"][0]
    if change == "content":
        item["item"]["content"] = "changed"
    elif change == "hash":
        item["sha256"] = "a" * 64
    elif change == "id":
        item["memory_id"] = "other"
    elif change == "revision":
        item["revision"] = True
    elif change == "key":
        item["ref_key"] = "other"
    elif change == "extra":
        item["item"]["extra"] = "not admitted"
    elif change == "order":
        prompt["memory_inputs"].reverse()
    elif change == "schema":
        prompt["schema_version"] = True
    raw["prompt"] = json.dumps(prompt) if change == "noncanonical" else canonical_json(prompt).decode()
    with pytest.raises(WorkflowValidationError):
        parse_external_request(canonical_json(raw), resolved)


@pytest.mark.parametrize("content,count", [("x" * 8193, 1), ("x" * 4000, 3), ("\x00" * 1500, 1), ("🌱" * 2020, 1)],
                         ids=["item-bytes", "logical-sum", "escaped-prompt", "utf8-bytes"])
def test_memory_request_rejects_complete_shared_and_prompt_budgets(content: str, count: int) -> None:
    from hub.domain.external_request import build_external_request

    resolved = resolved_for(count=count)
    with pytest.raises(WorkflowValidationError, match="context_too_large"):
        build_external_request(resolved, memories=memory_inputs(resolved, content=content))


def test_memory_and_text_share_one_logical_byte_budget() -> None:
    from hub.domain.external_request import ExternalTextInput, build_external_request
    from hub.domain.workflow_contract_types import ArtifactReference

    raw = json.loads(definition_for().snapshot)
    text = "x" * 4000
    reference = ArtifactReference("text-a", hashlib.sha256(text.encode()).hexdigest(), len(text))
    raw["steps"][0]["contract"]["input_refs"] = [{"ref_key": "text", "kind": "artifact", "artifact_id": reference.artifact_id,
        "sha256": reference.sha256, "size": reference.size}]
    definition = parse_definition(canonical_json(raw))
    bindings = (InputBinding("text", OWNER, WORKSPACE, reference), InputBinding("memory-0", OWNER, WORKSPACE, ContextReference("memory", "memory-a", 0)))
    resolved = resolve_contract(definition, AuthorizedInputs(OWNER, WORKSPACE, "develop", CatalogRef("muse", 1), bindings))
    request = build_external_request(resolved, (ExternalTextInput("text", reference, "text/plain", text),), memories=memory_inputs(resolved))
    assert json.loads(request.prompt)["text_inputs"][0]["text"] == text
    with pytest.raises(WorkflowValidationError, match="context_too_large"):
        build_external_request(resolved, (ExternalTextInput("text", reference, "text/plain", text),),
                               memories=memory_inputs(resolved, content="m" * 4200))


@pytest.mark.parametrize("version", [1, 2])
def test_summary_reference_without_complete_version_is_rejected(version: int) -> None:
    from hub.domain.external_request import build_external_request

    with pytest.raises(WorkflowValidationError, match="summary_input_mismatch"):
        build_external_request(resolved_for(version=version, revision=1, kind="summary"))


def test_memory_batch_is_owner_scoped_one_query_and_preserves_order(tmp_path: Path, monkeypatch: Any) -> None:
    from hub.infrastructure.workflow_memory_repository import WorkflowMemoryRepository

    setup = make_memory_setup(tmp_path, count=3)
    repository = setup.external.repository
    statements: list[str] = []
    original = repository.transaction

    @contextmanager
    def traced(**kwargs: Any) -> Any:
        with original(**kwargs) as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(repository, "transaction", traced)
    references = tuple(named.reference for named in setup.external.resolved.contract.context_refs)
    values = WorkflowMemoryRepository(repository).read_selected(OWNER, references)
    assert len(values) == 3 and values == (memory_item(),) * 3
    selects = [value for value in statements if "FROM platform_memory_items" in value]
    assert len(selects) == 1 and "LIMIT" in selects[0] and "SELECT *" not in selects[0]
    with pytest.raises(WorkflowValidationError, match="memory_selection_stale"):
        WorkflowMemoryRepository(repository).read_selected("other@example.test", references)


@pytest.mark.parametrize("sql", ["UPDATE platform_memory_items SET enabled=0", "UPDATE platform_memory_items SET enabled=2",
    "UPDATE platform_memory_items SET enabled=zeroblob(9000)", "UPDATE platform_memory_items SET revision=zeroblob(9000)",
    "UPDATE platform_memory_items SET revision=1", "UPDATE platform_memory_items SET revision=-1",
    "UPDATE platform_memory_items SET tags='[\"a\",\"a\"]'", "UPDATE platform_memory_items SET tags='{}'",
    "UPDATE platform_memory_items SET title=' bad '", "UPDATE platform_memory_items SET content=zeroblob(9000)",
    "UPDATE platform_memory_items SET content=printf('%09000d',1)", "UPDATE platform_memory_items SET kind='unknown'",
    "UPDATE platform_memory_items SET source=printf('%09000d',1)"])
def test_memory_persisted_bad_values_never_authorize_send(tmp_path: Path, sql: str) -> None:
    setup = make_memory_setup(tmp_path)
    with setup.external.repository.transaction() as connection:
        connection.execute(sql)
    with pytest.raises(WorkflowDispatchError) as raised:
        setup.external.dispatch(setup.executor())
    assert raised.value.__cause__ is not None
    assert setup.external.transport.sends == [] and held(setup.external) == "held"
    with setup.external.repository.transaction(write=False) as connection:
        assert connection.execute("SELECT send_started_at FROM workflow_external_bindings").fetchone()[0] is None


def test_memory_first_send_and_bound_restart_use_only_original_prompt(tmp_path: Path, monkeypatch: Any) -> None:
    setup = make_memory_setup(tmp_path)
    original = setup.external.dispatch(setup.executor())
    pin = setup.external.store.read_binding(original.binding, setup.external.target)
    assert json.loads(pin.request.prompt)["memory_inputs"][0]["item"]["content"] == CONTENT
    assert json.loads(pin.request.prompt)["memory_inputs"][0]["revision"] == 0
    assert pin.request.prompt in setup.external.transport.sends[0]["prompt"]
    setup.memories.update(OWNER, "memory-a", {"content": "new version"}, expected_revision=0, now=110)
    setup.external.recover()
    from hub.infrastructure.workflow_memory_repository import WorkflowMemoryRepository

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("bound recovery must not read current Memory")

    monkeypatch.setattr(WorkflowMemoryRepository, "read_selected", forbidden)
    assert setup.external.dispatch(setup.executor()).binding == original.binding
    assert len(setup.external.transport.sends) == 1


def test_memory_send_loss_looks_up_original_even_after_disable(tmp_path: Path, monkeypatch: Any) -> None:
    setup = make_memory_setup(tmp_path)
    original = setup.external.store.bind_task

    def crash(*args: Any, **kwargs: Any) -> Any:
        raise SimulatedCrash()

    monkeypatch.setattr(setup.external.store, "bind_task", crash)
    with pytest.raises(SimulatedCrash):
        setup.external.dispatch(setup.executor())
    setup.memories.update(OWNER, "memory-a", {"enabled": False}, expected_revision=0, now=110)
    monkeypatch.setattr(setup.external.store, "bind_task", original)
    setup.external.recover()
    observed = setup.external.dispatch(setup.executor())
    assert observed.binding is not None and len(setup.external.transport.sends) == 1
    assert held(setup.external) == "held"


@pytest.mark.parametrize("change", ["edit", "disable", "remove", "same_revision"])
def test_memory_revocation_blocks_allowed_resend_but_preserves_saved_lookup(tmp_path: Path, change: str) -> None:
    setup = make_memory_setup(tmp_path)
    from hub.application.workflow_external_inputs import WorkflowExternalInputs
    from hub.infrastructure.workflow_memory_repository import WorkflowMemoryRepository

    external = setup.external
    inputs = WorkflowExternalInputs(setup.artifacts, memories=WorkflowMemoryRepository(external.repository))
    request = inputs.collect(external.resolved)
    intent = external.repository.get_dispatch(OWNER, "workflow-a", "develop", 1)
    external.repository.begin_dispatch(external.claim, step_key="develop", generation=1,
        expected_revision=external.repository.get(OWNER, "workflow-a").revision, now=103)
    external.store.prepare_send(intent.token, external.resolved.digest, external.resolved, external.target, now=104, request=request)
    with external.repository.transaction() as connection:
        if change == "remove":
            connection.execute("UPDATE platform_memory_items SET memory_id='removed-id'")
        elif change == "same_revision":
            connection.execute("UPDATE platform_memory_items SET content='changed'")
        elif change == "disable":
            connection.execute("UPDATE platform_memory_items SET enabled=0")
        else:
            connection.execute("UPDATE platform_memory_items SET revision=1,content='changed'")
    executor = setup.executor()
    assert asyncio.run(executor.lookup(intent.token, external.resolved.digest, external.resolved)) is None
    with pytest.raises(WorkflowDispatchError):
        asyncio.run(executor.create_or_lookup(intent.token, external.resolved.digest, external.resolved))
    assert external.transport.sends == [] and held(external) == "held"
    assert external.store.lookup(intent.token, external.resolved.digest, external.resolved, external.target).request == request


def test_memory_reader_is_fixed_in_creation_transaction_and_rollback_is_atomic(tmp_path: Path) -> None:
    repository = prepare_database(tmp_path / "only-workflow.db")
    with pytest.raises(WorkflowRepositoryError, match="reference_forbidden"):
        repository.create_or_get(OWNER, WORKSPACE, "bad", client_token="bad", definition=definition_for(), now=100, conversation_id="missing")
    with repository.transaction(write=False) as connection:
        assert inspect_schema(connection)["minimum_reader_version"] == 1
    repository.create_or_get(OWNER, WORKSPACE, "workflow-a", client_token="memory", definition=definition_for(), now=100)
    with repository.transaction(write=False) as connection:
        assert inspect_schema(connection)["minimum_reader_version"] == 6
    repository.init()


def test_memory_resolver_authorizes_only_actual_external_consumer(tmp_path: Path) -> None:
    from hub.infrastructure.workflow_input_resolver import WorkflowArtifactInputResolver
    from hub.infrastructure.workflow_memory_repository import WorkflowMemoryRepository

    setup = make_memory_setup(tmp_path, admit=False)
    external = setup.external
    snapshot = external.repository.snapshots((external.claim,), now=102)[0]
    resolver = WorkflowArtifactInputResolver(external.repository, setup.artifacts, memories=WorkflowMemoryRepository(external.repository))
    assert asyncio.run(resolver.resolve(snapshot, "develop", CatalogRef("muse", 1))) == external.resolved
    setup.memories.update(OWNER, "memory-a", {"enabled": False}, expected_revision=0, now=103)
    with pytest.raises(WorkflowRepositoryError):
        asyncio.run(resolver.resolve(snapshot, "develop", CatalogRef("muse", 1)))


def test_run_executor_rejects_opaque_context_before_creating_run(tmp_path: Path) -> None:
    from hub.infrastructure.workflow_run_executor import RegisteredRunTarget, RunStepExecutor

    repository = prepare_database(tmp_path / "native.db")
    resolved = resolved_for()
    target = RegisteredRunTarget(OWNER, WORKSPACE, "native-a", CatalogRef("muse", 1), CatalogRef("safe", 1), "conversation", b"{}")
    executor = RunStepExecutor(repository, target, clock=lambda: 104)
    token = dispatch_token(OWNER, "workflow-a", "develop", 1)
    with pytest.raises(WorkflowDispatchError, match="context_resolution_required"):
        asyncio.run(executor.create_or_lookup(token, resolved.digest, resolved))
    with repository.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


@pytest.mark.parametrize("encrypted", [False, True])
def test_memory_backup_restores_original_private_request_after_edit(tmp_path: Path, encrypted: bool) -> None:
    setup = make_memory_setup(tmp_path)
    setup.external.dispatch(setup.executor())
    setup.memories.update(OWNER, "memory-a", {"content": "current changed text"}, expected_revision=0, now=110)
    service = PlatformBackupService()
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    if encrypted:
        summary = service.create_encrypted_backup(setup.external.repository.db_path, setup.artifacts.root, backup, b"m" * 32, "memory-key")["source"]
        service.restore_encrypted_backup(backup, restored, b"m" * 32)
    else:
        summary = service.create_backup(setup.external.repository.db_path, setup.artifacts.root, backup)
        service.restore_backup(backup, restored)
    assert summary["workflow"] == {"schema_version": 1, "minimum_reader_version": 6}
    repository = WorkflowRepository(restored / "platform.db")
    with repository.transaction(write=False) as connection:
        assert inspect_schema(connection)["admission_mode"] == "frozen"
        raw = bytes(connection.execute("SELECT request_snapshot FROM workflow_external_bindings").fetchone()[0])
    from hub.domain.external_request import parse_external_request

    assert json.loads(parse_external_request(raw, setup.external.resolved).prompt)["memory_inputs"][0]["item"]["content"] == CONTENT


@pytest.mark.parametrize("legacy", [1, 2])
def test_legacy_external_upgrade_preserves_exact_original_task(tmp_path: Path, legacy: int) -> None:
    setup = make_external(tmp_path)
    original = setup.dispatch()
    if legacy == 1:
        from test_external_agent_text_inputs import legacy_schema

        legacy_schema(setup)
    else:
        with setup.repository.transaction() as connection:
            connection.execute("UPDATE workflow_external_schema_meta SET schema_version=2,minimum_reader_version=2")
            connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=5")
    with setup.repository.transaction(write=False) as connection:
        assert inspect_schema(connection)["minimum_reader_version"] == (3 if legacy == 1 else 5)
    setup.store.init()
    with setup.repository.transaction(write=False) as connection:
        assert tuple(connection.execute("SELECT schema_version,minimum_reader_version FROM workflow_external_schema_meta").fetchone()) == (3, 3)
        assert inspect_schema(connection)["minimum_reader_version"] == 6
    assert setup.dispatch().binding == original.binding and len(setup.transport.sends) == 1


@pytest.mark.parametrize("change", ["missing", "legacy", "future", "low_reader", "hash"])
def test_memory_history_and_gate_off_backup_fail_closed(tmp_path: Path, change: str) -> None:
    setup = make_memory_setup(tmp_path)
    setup.external.dispatch(setup.executor())
    with setup.external.repository.transaction() as connection:
        if change == "missing":
            connection.execute("UPDATE workflow_external_bindings SET request_snapshot=NULL")
        elif change == "legacy":
            connection.execute("UPDATE workflow_external_schema_meta SET schema_version=2,minimum_reader_version=2")
        elif change == "future":
            from hub.infrastructure.workflow_schema import READER_VERSION

            connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=?", (READER_VERSION + 1,))
        elif change == "low_reader":
            connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=5")
        else:
            raw = json.loads(bytes(connection.execute("SELECT request_snapshot FROM workflow_external_bindings").fetchone()[0]))
            prompt = json.loads(raw["prompt"])
            prompt["memory_inputs"][0]["sha256"] = "a" * 64
            raw["prompt"] = canonical_json(prompt).decode()
            connection.execute("UPDATE workflow_external_bindings SET request_snapshot=?", (canonical_json(raw),))
    with pytest.raises(BackupError):
        PlatformBackupService().create_backup(setup.external.repository.db_path, setup.artifacts.root, tmp_path / "bad-backup")
    assert len(setup.external.transport.sends) == 1


def test_conflicting_memory_revisions_and_duplicate_values_fail_closed(tmp_path: Path) -> None:
    from hub.domain.external_request import ExternalMemoryInput, build_external_request
    from hub.infrastructure.workflow_memory_repository import WorkflowMemoryRepository

    setup = make_memory_setup(tmp_path)
    with pytest.raises(WorkflowValidationError, match="memory_revision_conflict"):
        WorkflowMemoryRepository(setup.external.repository).read_selected(OWNER,
            (ContextReference("memory", "memory-a", 0), ContextReference("memory", "memory-a", 1)))
    resolved = resolved_for(count=2)
    values = (ExternalMemoryInput("memory-0", memory_item()), ExternalMemoryInput("memory-1", memory_item(content="other")))
    with pytest.raises(WorkflowValidationError, match="external_input_mismatch"):
        build_external_request(resolved, memories=values)


def test_schema1_memory_revision1_sends_complete_content(tmp_path: Path) -> None:
    from hub.application.workflow_external_inputs import WorkflowExternalInputs
    from hub.infrastructure.workflow_memory_repository import WorkflowMemoryRepository

    setup = make_memory_setup(tmp_path)
    setup.memories.update(OWNER, "memory-a", {"content": CONTENT}, expected_revision=0, now=101)
    resolved = resolved_for(version=1, revision=1)
    request = WorkflowExternalInputs(setup.artifacts, memories=WorkflowMemoryRepository(setup.external.repository)).collect(resolved)
    assert json.loads(request.prompt)["memory_inputs"][0]["item"]["content"] == CONTENT
    assert json.loads(request.prompt)["resolved_contract"]["contract"]["schema_version"] == 1


def test_schema2_without_context_keeps_original_prompt_and_has_reader_fence(tmp_path: Path) -> None:
    from hub.domain.external_request import build_external_request

    resolved = resolved_for(count=0)
    assert build_external_request(resolved).prompt == resolved.snapshot.decode()
    repository = prepare_database(tmp_path / "schema2.db")
    definition = definition_for(count=0)
    repository.create_or_get(OWNER, WORKSPACE, "workflow-a", client_token="schema2", definition=definition, now=100)
    with repository.transaction() as connection:
        connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=1")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    with pytest.raises(BackupError, match="invalid_contract_reader"):
        PlatformBackupService().create_backup(repository.db_path, artifacts, tmp_path / "bad-backup")
    with pytest.raises(WorkflowRepositoryError, match="invalid_contract_reader"):
        repository.create_or_get(OWNER, WORKSPACE, "workflow-a", client_token="schema2", definition=definition, now=101)


def test_reader_marker_and_workflow_creation_roll_back_together(tmp_path: Path) -> None:
    repository = prepare_database(tmp_path / "atomic.db")
    with pytest.raises(RuntimeError, match="outer rollback"):
        with repository.transaction() as connection:
            repository.create_or_get(OWNER, WORKSPACE, "workflow-a", client_token="schema2", definition=definition_for(), now=100, connection=connection)
            assert inspect_schema(connection)["minimum_reader_version"] == 6
            raise RuntimeError("outer rollback")
    with repository.transaction(write=False) as connection:
        assert inspect_schema(connection)["minimum_reader_version"] == 1
        assert connection.execute("SELECT count(*) FROM workflow_runs").fetchone()[0] == 0


def test_memory_request_and_send_marker_roll_back_together(tmp_path: Path, monkeypatch: Any) -> None:
    setup = make_memory_setup(tmp_path)
    repository = setup.external.repository
    transaction = repository.transaction

    @contextmanager
    def rejected(**kwargs: Any) -> Any:
        with transaction(**kwargs) as connection:
            connection.set_authorizer(lambda action, table, column, *rest: sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_UPDATE and table == "workflow_external_bindings" and column == "request_snapshot"
                else sqlite3.SQLITE_OK)
            yield connection

    monkeypatch.setattr(repository, "transaction", rejected)
    with pytest.raises(WorkflowDispatchError):
        setup.external.dispatch(setup.executor())
    with transaction(write=False) as connection:
        assert tuple(connection.execute("SELECT send_started_at,request_snapshot FROM workflow_external_bindings").fetchone()) == (None, None)
    assert setup.external.transport.sends == []


@pytest.mark.parametrize("legacy", [1, 2])
def test_external_metadata_migration_failure_keeps_all_original_facts(tmp_path: Path, monkeypatch: Any, legacy: int) -> None:
    setup = make_external(tmp_path)
    observed = setup.dispatch()
    if legacy == 1:
        from test_external_agent_text_inputs import legacy_schema

        legacy_schema(setup)
    else:
        with setup.repository.transaction() as connection:
            connection.execute("UPDATE workflow_external_schema_meta SET schema_version=2,minimum_reader_version=2")
            connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=5")
    transaction = setup.repository.transaction

    @contextmanager
    def rejected(**kwargs: Any) -> Any:
        with transaction(**kwargs) as connection:
            connection.set_authorizer(lambda action, table, *rest: sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_UPDATE and table == "workflow_schema_meta" else sqlite3.SQLITE_OK)
            yield connection

    monkeypatch.setattr(setup.repository, "transaction", rejected)
    with pytest.raises(WorkflowRepositoryError):
        setup.store.init()
    with transaction(write=False) as connection:
        assert tuple(connection.execute("SELECT schema_version,minimum_reader_version FROM workflow_external_schema_meta").fetchone()) == (legacy, legacy)
        assert inspect_schema(connection)["minimum_reader_version"] == (3 if legacy == 1 else 5)
        assert ("request_snapshot" in {row[1] for row in connection.execute("PRAGMA table_info(workflow_external_bindings)")}) == (legacy == 2)
    assert setup.store.read_binding(observed.binding, setup.target).task is not None


def _disable_memory_in_process(path: str) -> None:
    store = PlatformMemoryRepository(Path(path))
    store.update(OWNER, "memory-a", {"enabled": False}, expected_revision=0, now=110)


def test_other_process_memory_revocation_is_visible_without_cache(tmp_path: Path) -> None:
    from hub.application.workflow_external_inputs import WorkflowExternalInputs
    from hub.infrastructure.workflow_memory_repository import WorkflowMemoryRepository

    setup = make_memory_setup(tmp_path)
    inputs = WorkflowExternalInputs(setup.artifacts, memories=WorkflowMemoryRepository(setup.external.repository))
    request = inputs.collect(setup.external.resolved)
    process = multiprocessing.get_context("spawn").Process(target=_disable_memory_in_process, args=(str(setup.external.repository.db_path),))
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 0
    with pytest.raises(WorkflowDispatchError, match="memory_selection_stale"):
        inputs.authorize(setup.external.resolved, request)
    assert setup.external.transport.sends == []


def test_bootstrap_coordinator_consumes_memory_and_dto_never_leaks_text(tmp_path: Path) -> None:
    from hub.application.workflow_coordinator import RegisteredWorkflowPlan
    from hub.bootstrap import create_app
    from hub.config import FleetConfig

    setup = make_memory_setup(tmp_path, admit=False)
    external = setup.external
    external.repository.release_claim(external.claim, now=104, next_poll_at=104)
    definition = external.repository.get(OWNER, "workflow-a")
    plan = RegisteredWorkflowPlan(OWNER, WORKSPACE, definition.definition_digest, "develop", CatalogRef("muse", 1),
        CatalogRef("safe", 1), external.target.target_key, 1, 100, 2, external_target=external.target)
    config = FleetConfig.from_root(tmp_path, platform_enabled=True, platform_workflows_enabled=True,
        platform_workflow_dispatch_enabled=True, platform_external_agents_enabled=True, platform_db=external.repository.db_path,
        platform_artifact_root=setup.artifacts.root, dev_operator=OWNER, serve_frontend=False)
    app = create_app(config, repositories={"workflow_clock": external.clock, "workflow_steps": (plan,),
        "workflow_external_agents": ((external.target, external.adapter),)})
    coordinator = app.extensions["fleet"]["services"]["workflow_coordinator"]
    for _ in range(4):
        result = asyncio.run(coordinator.tick(external.clock()))
        assert not result.failures
        external.clock.now += 1
    assert external.repository.get(OWNER, "workflow-a").execution_state == "waiting_external"
    assert len(external.transport.sends) == 1
    response = app.test_client().get("/api/platform/v1/workflows/workflow-a")
    assert response.status_code == 200
    assert "memory_inputs" not in response.get_data(as_text=True) and "完整记忆" not in response.get_data(as_text=True)
    events = external.repository.events(OWNER, "workflow-a").events
    assert all(b"memory_inputs" not in event.payload and "完整记忆".encode() not in event.payload for event in events)


def test_native_context_resolver_requires_real_current_memory(tmp_path: Path) -> None:
    from hub.infrastructure.workflow_input_resolver import WorkflowArtifactInputResolver
    from hub.infrastructure.workflow_memory_repository import WorkflowMemoryRepository

    repository = prepare_database(tmp_path / "native-resolver.db")
    memories = PlatformMemoryRepository(repository.db_path)
    memories.init()
    repository.create_or_get(OWNER, WORKSPACE, "workflow-a", client_token="native", definition=definition_for(step_kind="native_run"), now=100)
    claim = repository.claim_due(OWNER, worker_id="native", now=100, limit=1, lease_seconds=20)[0]
    snapshot = repository.snapshots((claim,), now=101)[0]
    resolver = WorkflowArtifactInputResolver(repository, ArtifactStore(tmp_path / "artifacts"), memories=WorkflowMemoryRepository(repository))
    with pytest.raises(WorkflowRepositoryError, match="reference_forbidden"):
        asyncio.run(resolver.resolve(snapshot, "develop", CatalogRef("muse", 1)))
    item = memory_item()
    memories.create(OWNER, {"memory_id": item.memory_id, "kind": item.kind, "title": item.title, "content": item.content,
        "tags": list(item.tags), "source": item.source}, now=100)
    resolved = asyncio.run(resolver.resolve(snapshot, "develop", CatalogRef("muse", 1)))
    assert resolved == resolved_for(step_kind="native_run")
    assert CONTENT.encode() not in resolved.snapshot


def test_historical_memory_requests_are_validated_past_first_batch(tmp_path: Path) -> None:
    from hub.application.workflow_external_inputs import WorkflowExternalInputs
    from hub.infrastructure.workflow_memory_repository import WorkflowMemoryRepository

    setup = make_memory_setup(tmp_path)
    external = setup.external
    # Existing text history test establishes rowid keyset mechanics. This tests
    # Memory decoding in the last batch without any current Memory authority.
    request = WorkflowExternalInputs(setup.artifacts, memories=WorkflowMemoryRepository(external.repository)).collect(external.resolved)
    for index in range(1, 130):
        key = f"history-{index:03d}"
        external.repository.create_or_get(OWNER, WORKSPACE, key, client_token=key, definition=definition_for(), now=100)
        claim = external.repository.claim_due(OWNER, worker_id="history", now=100, limit=1, lease_seconds=20)[0]
        external.repository.freeze_inputs(claim, step_key="develop", generation=1, resolved=external.resolved, expected_revision=1, now=101)
        external.repository.admit_step(claim, step_key="develop", generation=1, expected_revision=2, target_key=external.target.target_key,
            capacity=1024, reserve_tokens=100, reserve_requests=2, now=102, external_target=external.target)
    with external.repository.transaction() as connection:
        connection.execute("UPDATE workflow_external_bindings SET send_started_at=104,updated_at=104,request_snapshot=?", (request.snapshot,))
        connection.execute("UPDATE platform_memory_items SET enabled=0")
    with external.repository.transaction(write=False) as connection:
        ExternalWorkflowRepository.validate_snapshot(connection)
    with external.repository.transaction() as connection:
        connection.execute("UPDATE workflow_external_bindings SET request_snapshot=? WHERE rowid=(SELECT max(rowid) FROM workflow_external_bindings)", (b"{}",))
    with pytest.raises(BackupError, match="external_snapshot_invalid"):
        PlatformBackupService().create_backup(external.repository.db_path, setup.artifacts.root, tmp_path / "history-backup")


@pytest.mark.parametrize("rowid", [-7, 0])
def test_memory_history_validation_includes_nonpositive_rowids(tmp_path: Path, rowid: int) -> None:
    setup = make_memory_setup(tmp_path)
    setup.external.dispatch(setup.executor())
    with setup.external.repository.transaction() as connection:
        connection.execute("UPDATE workflow_external_bindings SET rowid=?,request_snapshot=?", (rowid, b"{}"))
    with pytest.raises(BackupError, match="external_snapshot_invalid"):
        PlatformBackupService().create_backup(setup.external.repository.db_path, setup.artifacts.root, tmp_path / "corrupt-history")


def test_schema2_legacy_request_cannot_be_read_under_old_compatibility_marker(tmp_path: Path) -> None:
    setup = make_external(tmp_path, admit=False)
    value = json.loads(setup.resolved.snapshot)
    value["contract"]["schema_version"] = 2
    resolved = parse_resolved(canonical_json(value))
    # Build a coherent schema-2 no-context dispatch, then simulate an invalid
    # metadata downgrade. Legacy readers cannot understand this contract.
    with setup.repository.transaction() as connection:
        definition = json.loads(bytes(connection.execute("SELECT definition_snapshot FROM workflow_runs").fetchone()[0]))
        definition["steps"][0]["contract"]["schema_version"] = 2
        snapshot = parse_definition(canonical_json(definition)).snapshot
        connection.execute("UPDATE workflow_runs SET definition_snapshot=?,definition_digest=?", (snapshot, hashlib.sha256(snapshot).hexdigest()))
        connection.execute("UPDATE workflow_steps SET contract_snapshot=?,resolved_snapshot=?,resolved_digest=?",
            (resolved.contract.snapshot, resolved.snapshot, resolved.digest))
    setup.resolved = resolved
    setup.admit(setup.target)
    with setup.repository.transaction() as connection:
        connection.execute("UPDATE workflow_external_schema_meta SET schema_version=2,minimum_reader_version=2")
        connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=5")
    intent = setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1)
    with pytest.raises(WorkflowRepositoryError, match="external_snapshot_invalid"):
        setup.store.lookup(intent.token, resolved.digest, resolved, setup.target)
