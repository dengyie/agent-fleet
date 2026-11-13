"""Complete scoped text inputs and the original external task across restart."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from hub.application.workflow_dispatch import WorkflowDispatchError
from hub.domain.workflow_contract import parse_definition, parse_resolved, resolve_contract
from hub.domain.workflow_contract_types import ArtifactReference, AuthorizedInputs, CatalogRef, InputBinding, ResolvedContract
from hub.domain.workflow_errors import WorkflowValidationError
from hub.domain.workflow_identity import canonical_json, dispatch_token
from hub.infrastructure.platform_backup import BackupError, PlatformBackupService
from hub.composition.workflows import assert_workflow_visibility
from hub.infrastructure.workflow_external_repository import ExternalWorkflowRepository
from hub.infrastructure.workflow_repository import WorkflowRepository, WorkflowRepositoryError
from hub.infrastructure.workflow_schema import READER_VERSION, WorkflowSchemaError, inspect_schema
from hub.platform.artifacts import ArtifactStore
from test_external_agent_workflow import ExternalSetup, held, make_external
from test_workflow_dispatch import SimulatedCrash
from test_workflow_repository import OWNER, WORKSPACE, registered


def resolved_for(raw: bytes = b"selected text", *, count: int = 1) -> ResolvedContract:
    reference = ArtifactReference("text-source", hashlib.sha256(raw).hexdigest(), len(raw))
    value = json.loads(registered().snapshot)
    contract = value["steps"][0]["contract"]
    value["steps"][0]["kind"] = "external_agent"
    contract["output_specs"] = []
    contract["input_refs"] = [{"ref_key": f"source-{i}", "kind": "artifact", "artifact_id": reference.artifact_id,
                               "sha256": reference.sha256, "size": reference.size} for i in range(count)]
    definition = parse_definition(canonical_json(value))
    bindings = tuple(InputBinding(f"source-{i}", OWNER, WORKSPACE, reference) for i in range(count))
    return resolve_contract(definition, AuthorizedInputs(OWNER, WORKSPACE, "develop", CatalogRef("muse", 1), bindings))


def text_value(resolved: ResolvedContract, text: str = "selected text", *, media: str = "text/plain") -> Any:
    from hub.domain.external_request import ExternalTextInput

    return tuple(ExternalTextInput(item.ref_key, item.reference, media, text) for item in resolved.contract.input_refs)


class ObservedHandle(io.BytesIO):
    def __init__(self, raw: bytes) -> None:
        super().__init__(raw)
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        assert 0 <= size <= 8193
        return super().read(size)


class ObservedArtifacts:
    def __init__(self, resolved: ResolvedContract, raw: bytes = b"selected text") -> None:
        reference = resolved.contract.input_refs[0].reference
        self.manifest: dict[str, Any] | None = {
            "owner_id": OWNER, "workspace_id": WORKSPACE, "artifact_id": reference.artifact_id,
            "sha256": reference.sha256, "size": reference.size, "content_type": "text/plain",
        }
        self.raw = raw
        self.handles: list[ObservedHandle] = []
        self.gets = 0

    def get(self, owner_id: str, workspace_id: str, artifact_id: str) -> dict[str, Any] | None:
        assert (owner_id, workspace_id, artifact_id) == (OWNER, WORKSPACE, "text-source")
        self.gets += 1
        return self.manifest

    def open_verified(self, owner_id: str, workspace_id: str, artifact_id: str) -> BinaryIO:
        assert (owner_id, workspace_id, artifact_id) == (OWNER, WORKSPACE, "text-source")
        handle = ObservedHandle(self.raw)
        self.handles.append(handle)
        return handle


def make_text_setup(tmp_path: Path, *, raw: bytes = "完整资料 🌱\n<script>data</script>".encode(), media: str = "text/markdown",
                    admit: bool = True, freeze: bool = True) -> tuple[ExternalSetup, ArtifactStore]:
    setup = make_external(tmp_path, admit=False)
    # A second workflow uses real scoped artifact authority and the original target.
    artifacts = ArtifactStore(tmp_path / "artifacts", clock=lambda: 100)
    source = tmp_path / "selected.txt"
    source.write_bytes(raw)
    manifest = artifacts.put_file(OWNER, WORKSPACE, source, content_type=media, artifact_id="selected-text")
    value = json.loads(registered().snapshot)
    value["steps"][0]["kind"] = "external_agent"
    value["steps"][0]["contract"]["output_specs"] = []
    value["steps"][0]["contract"]["input_refs"] = [{"ref_key": "source", "kind": "artifact",
        **{key: manifest[key] for key in ("artifact_id", "sha256", "size")}}]
    definition = parse_definition(canonical_json(value))
    # Use a fresh database to keep the standard workflow identity in fixture helpers.
    from test_workflow_repository import prepare_database

    repository = prepare_database(tmp_path / "text-platform.db")
    store = ExternalWorkflowRepository(repository)
    store.init()
    repository.create_or_get(OWNER, WORKSPACE, "workflow-a", client_token="text-a", definition=definition, now=100)
    claim = repository.claim_due(OWNER, worker_id="text-worker", now=100, limit=1, lease_seconds=20)[0]
    reference = ArtifactReference(manifest["artifact_id"], manifest["sha256"], manifest["size"])
    resolved = resolve_contract(definition, AuthorizedInputs(OWNER, WORKSPACE, "develop", CatalogRef("muse", 1),
        (InputBinding("source", OWNER, WORKSPACE, reference),)))
    result = replace(setup, repository=repository, store=store, claim=claim, resolved=resolved)
    if freeze:
        repository.freeze_inputs(claim, step_key="develop", generation=1, resolved=resolved, expected_revision=1, now=101)
    if admit:
        result.admit(result.target)
    return result, artifacts


def text_executor(setup: ExternalSetup, artifacts: Any, *, enabled: bool = True) -> Any:
    from hub.application.workflow_external_execution import ExternalStepExecutor
    from hub.application.workflow_external_inputs import WorkflowExternalInputs

    return ExternalStepExecutor(setup.store, setup.target, setup.adapter, clock=setup.clock,
        enabled=enabled, inputs=WorkflowExternalInputs(artifacts))


@pytest.mark.parametrize("text,media", [("", "text/plain"), ("完整\n\ufeff🌱", "text/markdown"), ('{"a":1}', "application/json")])
def test_text_request_roundtrip_keeps_complete_utf8(text: str, media: str) -> None:
    from hub.domain.external_request import build_external_request, parse_external_request

    resolved = resolved_for(text.encode())
    request = build_external_request(resolved, text_value(resolved, text, media=media))
    assert parse_external_request(request.snapshot, resolved) == request
    assert "prompt=" not in repr(request) and "text=" not in repr(text_value(resolved, text, media=media)[0])
    payload = json.loads(request.prompt)
    assert payload["resolved_contract"] == json.loads(resolved.snapshot)
    assert payload["text_inputs"][0]["text"].encode() == text.encode()
    assert payload["text_inputs"][0]["sha256"] == hashlib.sha256(text.encode()).hexdigest()
    with pytest.raises((AttributeError, TypeError)):
        request.prompt = "different"


def test_no_input_request_preserves_original_prompt_exactly(tmp_path: Path) -> None:
    from hub.domain.external_request import build_external_request, parse_external_request

    setup = make_external(tmp_path)
    request = build_external_request(setup.resolved)
    assert request.prompt == setup.resolved.snapshot.decode()
    assert parse_external_request(request.snapshot, setup.resolved) == request


@pytest.mark.parametrize("change", ["text", "size", "sha256", "artifact_id", "ref_key", "media", "extra", "order", "schema", "duplicate", "noncanonical"])
def test_private_request_rejects_mismatched_or_noncanonical_content(change: str) -> None:
    from hub.domain.external_request import build_external_request, parse_external_request

    resolved = resolved_for(count=2)
    request = build_external_request(resolved, text_value(resolved))
    value = json.loads(request.snapshot)
    payload = json.loads(value["prompt"])
    item = payload["text_inputs"][0]
    if change in {"text", "artifact_id", "ref_key"}:
        item[change] = "different"
    elif change == "size":
        item["size"] = True
    elif change == "sha256":
        item["sha256"] = "a" * 64
    elif change == "media":
        item["content_type"] = "image/png"
    elif change == "extra":
        item["extra"] = "forbidden"
    elif change == "order":
        payload["text_inputs"].reverse()
    elif change == "schema":
        payload["schema_version"] = True
    elif change == "duplicate":
        payload["text_inputs"].append(item)
    value["prompt"] = canonical_json(payload).decode()
    raw = json.dumps(value).encode() if change == "noncanonical" else canonical_json(value)
    with pytest.raises(WorkflowValidationError):
        parse_external_request(raw, resolved)


@pytest.mark.parametrize("raw,count", [(b"x" * 8193, 1), (b"x" * 4097, 2), (b"x" * 7600, 1), (b"\x00" * 1500, 1)],
                         ids=["single-bytes", "aggregate-bytes", "prompt-overhead", "json-escaping"])
def test_text_request_never_truncates_byte_or_prompt_overflow(raw: bytes, count: int) -> None:
    from hub.domain.external_request import build_external_request

    resolved = resolved_for(raw, count=count)
    with pytest.raises(WorkflowValidationError, match="context_too_large"):
        build_external_request(resolved, text_value(resolved, raw.decode()))


def test_collector_deduplicates_bounded_reads_and_current_authorization() -> None:
    from hub.application.workflow_external_inputs import WorkflowExternalInputs

    resolved = resolved_for(count=2)
    artifacts = ObservedArtifacts(resolved)
    service = WorkflowExternalInputs(artifacts)
    request = service.collect(resolved)
    assert len(artifacts.handles) == 1 and artifacts.gets == 1
    # The complete shared reader probes EOF after all declared bytes.
    assert artifacts.handles[0].closed and artifacts.handles[0].read_sizes == [14, 1]
    service.authorize(resolved, request)
    assert len(artifacts.handles) == 1 and artifacts.gets == 2
    artifacts.manifest = None
    with pytest.raises(WorkflowDispatchError, match="external_input"):
        service.authorize(resolved, request)


@pytest.mark.parametrize("field,value", [("owner_id", "foreign"), ("workspace_id", "foreign"), ("artifact_id", "foreign"),
    ("sha256", "f" * 64), ("size", 0), ("size", True), ("content_type", "text/html"), ("content_type", "text/plain;charset=utf-8")])
def test_collector_rejects_metadata_before_opening(field: str, value: Any) -> None:
    from hub.application.workflow_external_inputs import WorkflowExternalInputs

    resolved = resolved_for()
    artifacts = ObservedArtifacts(resolved)
    artifacts.manifest[field] = value
    with pytest.raises(WorkflowDispatchError, match="external_input"):
        WorkflowExternalInputs(artifacts).collect(resolved)
    assert artifacts.handles == []


@pytest.mark.parametrize("raw", [b"corrupt bytes", b"selected text!", b"selected tex", b"\xff" * 13])
def test_collector_rechecks_actual_bytes_and_always_closes(raw: bytes) -> None:
    from hub.application.workflow_external_inputs import WorkflowExternalInputs

    resolved = resolved_for()
    artifacts = ObservedArtifacts(resolved, raw)
    with pytest.raises(WorkflowDispatchError) as error:
        WorkflowExternalInputs(artifacts).collect(resolved)
    assert error.value.__cause__ is not None
    assert artifacts.handles[0].closed


def test_collector_keeps_original_io_error_chain() -> None:
    from hub.application.workflow_external_inputs import WorkflowExternalInputs

    class Broken(ObservedArtifacts):
        def open_verified(self, *args: Any) -> BinaryIO:
            raise OSError("private storage path")

    with pytest.raises(WorkflowDispatchError) as error:
        WorkflowExternalInputs(Broken(resolved_for())).collect(resolved_for())
    assert isinstance(error.value.__cause__, OSError)
    assert "private storage path" not in str(error.value)


@pytest.mark.parametrize("change", ["reference", "media"])
def test_duplicate_artifact_cannot_have_conflicting_definitions(change: str) -> None:
    from hub.domain.external_request import build_external_request

    resolved = resolved_for(count=2)
    inputs = text_value(resolved)
    if change == "media":
        inputs = (inputs[0], replace(inputs[1], content_type="text/markdown"))
    else:
        raw = json.loads(resolved.snapshot)
        raw["contract"]["input_refs"][1]["sha256"] = hashlib.sha256(b"different txt").hexdigest()
        resolved = parse_resolved(canonical_json(raw))
        inputs = (inputs[0], replace(inputs[1], reference=resolved.contract.input_refs[1].reference, text="different txt"))
    with pytest.raises(WorkflowValidationError, match="external_input_mismatch"):
        build_external_request(resolved, inputs)


def test_collector_rejects_hash_matching_invalid_utf8() -> None:
    from hub.application.workflow_external_inputs import WorkflowExternalInputs

    resolved = resolved_for(b"\xff")
    artifacts = ObservedArtifacts(resolved, b"\xff")
    with pytest.raises(WorkflowDispatchError) as error:
        WorkflowExternalInputs(artifacts).collect(resolved)
    assert isinstance(error.value.__cause__, UnicodeDecodeError)
    assert artifacts.handles[0].closed


def test_text_dispatch_pins_request_before_io_and_restart_reads_original_task(tmp_path: Path) -> None:
    setup, artifacts = make_text_setup(tmp_path)
    original_submit = setup.adapter.submit

    def checked_submit(token: str, payload: dict[str, Any]) -> Any:
        pin = ExternalWorkflowRepository(WorkflowRepository(setup.repository.db_path)).lookup(token, setup.resolved.digest, setup.resolved, setup.target)
        assert pin.send_started_at == 104 and pin.request.prompt == payload["prompt"]
        assert json.loads(payload["prompt"])["text_inputs"][0]["text"].startswith("完整资料")
        return original_submit(token, payload)

    setup.adapter.submit = checked_submit
    first = setup.dispatch(text_executor(setup, artifacts))
    assert first.state == "waiting_external" and held(setup) == "held"
    setup.recover()
    # The original binding is sufficient even if local inputs become unavailable.
    second = setup.dispatch(text_executor(setup, None, enabled=False))
    assert second.binding == first.binding and len(setup.transport.sends) == 1
    public = b"".join(event.payload for event in setup.repository.events(OWNER, "workflow-a").events)
    assert "完整资料".encode() not in public


def test_private_request_content_is_absent_from_operator_http(tmp_path: Path) -> None:
    from hub.bootstrap import create_app
    from hub.config import FleetConfig

    setup, artifacts = make_text_setup(tmp_path)
    setup.apply(setup.dispatch(text_executor(setup, artifacts)))
    config = FleetConfig.from_root(tmp_path, platform_enabled=True, platform_workflows_enabled=True,
        platform_db=setup.repository.db_path, platform_artifact_root=artifacts.root, dev_operator=OWNER, serve_frontend=False)
    client = create_app(config).test_client()
    response = client.get("/api/platform/v1/workflows/workflow-a")
    assert response.status_code == 200
    assert "完整资料" not in response.get_data(as_text=True)
    assert "request_snapshot" not in response.get_data(as_text=True)


def test_lost_receipt_lookup_uses_saved_prompt_without_rereading_files(tmp_path: Path, monkeypatch: Any) -> None:
    setup, artifacts = make_text_setup(tmp_path)
    original = setup.store.bind_task
    monkeypatch.setattr(setup.store, "bind_task", lambda *a, **k: (_ for _ in ()).throw(SimulatedCrash()))
    with pytest.raises(SimulatedCrash):
        setup.dispatch(text_executor(setup, artifacts))
    monkeypatch.setattr(setup.store, "bind_task", original)
    setup.recover()
    observed = setup.dispatch(text_executor(setup, None))
    assert observed.state == "waiting_external" and len(setup.transport.sends) == 1
    assert held(setup) == "held"


def test_missing_input_before_intent_is_definite_failure_without_send(tmp_path: Path) -> None:
    setup, artifacts = make_text_setup(tmp_path)
    artifacts.get = lambda *args: None
    with pytest.raises(WorkflowDispatchError):
        setup.dispatch(text_executor(setup, artifacts))
    with setup.repository.transaction(write=False) as connection:
        row = connection.execute("SELECT send_started_at,request_snapshot FROM workflow_external_bindings").fetchone()
        assert tuple(row) == (None, None)
    assert not setup.transport.sends


def test_request_write_failure_rolls_back_send_intent(tmp_path: Path) -> None:
    setup, artifacts = make_text_setup(tmp_path)
    with setup.repository.transaction() as connection:
        connection.execute("CREATE TRIGGER request_fail BEFORE UPDATE OF request_snapshot ON workflow_external_bindings BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(WorkflowDispatchError):
        setup.dispatch(text_executor(setup, artifacts))
    with setup.repository.transaction(write=False) as connection:
        assert tuple(connection.execute("SELECT send_started_at,request_snapshot FROM workflow_external_bindings").fetchone()) == (None, None)
    assert not setup.transport.sends


@pytest.mark.parametrize("started", [False, True], ids=["before-first-intent", "before-original-intent"])
def test_backdated_send_time_never_corrupts_or_reuses_original_intent(tmp_path: Path, started: bool) -> None:
    setup, artifacts = make_text_setup(tmp_path)
    token = dispatch_token(OWNER, "workflow-a", "develop", 1)
    setup.repository.begin_dispatch(setup.claim, step_key="develop", generation=1,
        expected_revision=setup.repository.get(OWNER, "workflow-a").revision, now=104)
    request = text_executor(setup, artifacts).inputs.collect(setup.resolved)
    if started:
        setup.store.prepare_send(token, setup.resolved.digest, setup.resolved, setup.target, request=request, now=104)
    before = setup.store.lookup(token, setup.resolved.digest, setup.resolved, setup.target)
    with pytest.raises(WorkflowRepositoryError, match="invalid_external_timestamp"):
        setup.store.prepare_send(token, setup.resolved.digest, setup.resolved, setup.target,
            request=request, now=103 if started else 101)
    assert setup.store.lookup(token, setup.resolved.digest, setup.resolved, setup.target) == before
    assert not setup.transport.sends and held(setup) == "held"


def test_saved_request_cannot_be_replaced_and_revocation_blocks_resend(tmp_path: Path) -> None:
    from hub.domain.external_request import build_external_request

    setup, artifacts = make_text_setup(tmp_path)
    token = dispatch_token(OWNER, "workflow-a", "develop", 1)
    setup.repository.begin_dispatch(setup.claim, step_key="develop", generation=1,
        expected_revision=setup.repository.get(OWNER, "workflow-a").revision, now=104)
    executor = text_executor(setup, artifacts)
    request = executor.inputs.collect(setup.resolved)
    assert setup.store.prepare_send(token, setup.resolved.digest, setup.resolved, setup.target, request=request, now=104)
    # A different valid MIME envelope is still not the original fixed request.
    values = text_value(setup.resolved, json.loads(request.prompt)["text_inputs"][0]["text"], media="text/plain")
    replacement = build_external_request(setup.resolved, values)
    with pytest.raises(WorkflowRepositoryError, match="external_request_conflict"):
        ExternalWorkflowRepository(WorkflowRepository(setup.repository.db_path)).prepare_send(
            token, setup.resolved.digest, setup.resolved, setup.target, request=replacement, now=104)
    # Metadata loss is not permission to replay a previously started external operation.
    setup.adapter.lookup = lambda *args: None
    artifacts.get = lambda *args: None
    with pytest.raises(WorkflowDispatchError):
        asyncio.run(executor.create_or_lookup(token, setup.resolved.digest, setup.resolved))
    assert not setup.transport.sends and held(setup) == "held"
    with pytest.raises(WorkflowValidationError):
        build_external_request(setup.resolved, ())


@pytest.mark.parametrize("encrypted", [False, True])
def test_text_request_survives_backup_restore_frozen(tmp_path: Path, encrypted: bool) -> None:
    setup, artifacts = make_text_setup(tmp_path)
    observed = setup.dispatch(text_executor(setup, artifacts))
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    service = PlatformBackupService()
    if encrypted:
        service.create_encrypted_backup(setup.repository.db_path, artifacts.root, backup, b"t" * 32, "text-key")
        service.restore_encrypted_backup(backup, restored, b"t" * 32)
    else:
        service.create_backup(setup.repository.db_path, artifacts.root, backup)
        service.restore_backup(backup, restored)
    repository = WorkflowRepository(restored / "platform.db")
    repository.init()
    pin = ExternalWorkflowRepository(repository).read_binding(observed.binding, setup.target)
    assert json.loads(pin.request.prompt)["text_inputs"][0]["text"].startswith("完整资料")
    with repository.transaction(write=False) as connection:
        assert inspect_schema(connection)["admission_mode"] == "frozen"
        assert inspect_schema(connection)["minimum_reader_version"] == 6
        ExternalWorkflowRepository.validate_snapshot(connection)


@pytest.mark.parametrize("change", ["missing", "wrong_text", "noncanonical", "no_send"])
def test_private_request_corruption_blocks_io_and_backup(tmp_path: Path, change: str) -> None:
    setup, artifacts = make_text_setup(tmp_path)
    setup.dispatch(text_executor(setup, artifacts))
    with setup.repository.transaction() as connection:
        raw = bytes(connection.execute("SELECT request_snapshot FROM workflow_external_bindings").fetchone()[0])
        if change == "missing":
            connection.execute("UPDATE workflow_external_bindings SET request_snapshot=NULL")
        elif change == "no_send":
            connection.execute("UPDATE workflow_external_bindings SET send_started_at=NULL")
        else:
            value = json.loads(raw)
            if change == "wrong_text":
                value["prompt"] = value["prompt"].replace("完整资料", "伪造资料")
            data = json.dumps(value).encode() if change == "noncanonical" else canonical_json(value)
            connection.execute("UPDATE workflow_external_bindings SET request_snapshot=?", (data,))
    with pytest.raises(WorkflowDispatchError):
        setup.dispatch(text_executor(setup, artifacts))
    with pytest.raises(BackupError, match="external_snapshot_invalid"):
        PlatformBackupService().create_backup(setup.repository.db_path, artifacts.root, tmp_path / "corrupt-backup")
    assert len(setup.transport.sends) == 1


def legacy_schema(setup: ExternalSetup) -> None:
    with sqlite3.connect(setup.repository.db_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(workflow_external_bindings)")}
        if "request_snapshot" in columns:
            connection.execute("ALTER TABLE workflow_external_bindings DROP COLUMN request_snapshot")
        connection.execute("UPDATE workflow_external_schema_meta SET schema_version=1,minimum_reader_version=1")
        connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=3")


def test_legacy_schema_is_readable_gate_off_and_migrates_without_changing_facts(tmp_path: Path) -> None:
    setup = make_external(tmp_path)
    observed = setup.dispatch()
    legacy_schema(setup)
    repository = WorkflowRepository(setup.repository.db_path)
    repository.init()
    store = ExternalWorkflowRepository(repository)
    before = store.read_binding(observed.binding, setup.target)
    assert before.request.prompt == setup.resolved.snapshot.decode()
    with pytest.raises(WorkflowRepositoryError, match="workflow_domain_in_use"):
        assert_workflow_visibility(repository.db_path, enabled=False)
    store.init()
    after = store.read_binding(observed.binding, setup.target)
    assert before == after and len(setup.transport.sends) == 1
    with repository.transaction(write=False) as connection:
        assert tuple(connection.execute("SELECT schema_version,minimum_reader_version FROM workflow_external_schema_meta").fetchone()) == (3, 3)
        assert inspect_schema(connection)["minimum_reader_version"] == 6


def test_schema_upgrade_failure_preserves_legacy_reader_and_task(tmp_path: Path, monkeypatch: Any) -> None:
    setup = make_external(tmp_path)
    observed = setup.dispatch()
    legacy_schema(setup)
    transaction = setup.repository.transaction

    @contextmanager
    def rejected(*args: Any, **kwargs: Any):
        with transaction(*args, **kwargs) as connection:
            connection.set_authorizer(lambda action, table, *rest: sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_UPDATE and table == "workflow_external_schema_meta" else sqlite3.SQLITE_OK)
            yield connection

    monkeypatch.setattr(setup.repository, "transaction", rejected)
    with pytest.raises(WorkflowRepositoryError):
        setup.store.init()
    with sqlite3.connect(setup.repository.db_path) as connection:
        assert "request_snapshot" not in {row[1] for row in connection.execute("PRAGMA table_info(workflow_external_bindings)")}
        assert tuple(connection.execute("SELECT schema_version,minimum_reader_version FROM workflow_external_schema_meta").fetchone()) == (1, 1)
        assert connection.execute("SELECT minimum_reader_version FROM workflow_schema_meta").fetchone()[0] == 3
    pin = ExternalWorkflowRepository(WorkflowRepository(setup.repository.db_path)).read_binding(observed.binding, setup.target)
    assert pin.task.dispatch_token == observed.binding.token


@pytest.mark.parametrize("sql,code", [
    ("UPDATE workflow_external_schema_meta SET minimum_reader_version=4", "unsupported_external_schema"),
    ("UPDATE workflow_schema_meta SET minimum_reader_version=4", "invalid_external_reader"),
    (f"UPDATE workflow_schema_meta SET minimum_reader_version={READER_VERSION + 1}", "unsupported_workflow_schema"),
])
def test_text_schema_reader_fence_is_independent_of_runtime_gates(tmp_path: Path, sql: str, code: str) -> None:
    setup, artifacts = make_text_setup(tmp_path)
    with sqlite3.connect(setup.repository.db_path) as connection:
        connection.execute(sql)
    with pytest.raises(WorkflowRepositoryError, match=code) as error:
        WorkflowRepository(setup.repository.db_path).init()
    assert isinstance(error.value.__cause__, WorkflowSchemaError)
    with pytest.raises(BackupError, match=code):
        PlatformBackupService().create_backup(setup.repository.db_path, artifacts.root, tmp_path / "future-backup")


def test_text_input_gate_off_does_not_collect_or_send(tmp_path: Path) -> None:
    from hub.application.workflow_dispatch import WorkflowDispatchDeferred

    setup, artifacts = make_text_setup(tmp_path)
    artifacts.open_verified = lambda *args: (_ for _ in ()).throw(AssertionError("gate-off read"))
    with pytest.raises(WorkflowDispatchDeferred):
        setup.dispatch(text_executor(setup, artifacts, enabled=False))
    assert not setup.transport.sends


@pytest.mark.parametrize("change", ["cancel", "lease", "workspace", "reservation"])
def test_original_authority_is_rechecked_after_input_collection(tmp_path: Path, change: str) -> None:
    setup, artifacts = make_text_setup(tmp_path)
    executor = text_executor(setup, artifacts)
    collect = executor.inputs.collect

    def collect_and_revoke(resolved: ResolvedContract) -> Any:
        request = collect(resolved)
        with setup.repository.transaction() as connection:
            if change == "cancel":
                connection.execute("UPDATE workflow_runs SET cancel_requested=1")
            elif change == "lease":
                connection.execute("UPDATE workflow_runs SET lease_expires_at=103")
            elif change == "workspace":
                connection.execute("UPDATE workspaces SET enabled=0")
            else:
                connection.execute("UPDATE capability_reservations SET state='isolated'")
        return request

    executor.inputs.collect = collect_and_revoke
    with pytest.raises(WorkflowDispatchError):
        setup.dispatch(executor)
    with setup.repository.transaction(write=False) as connection:
        assert tuple(connection.execute("SELECT send_started_at,request_snapshot FROM workflow_external_bindings").fetchone()) == (None, None)
    assert not setup.transport.sends


def test_historical_request_scan_checks_last_bounded_batch(tmp_path: Path) -> None:
    setup, artifacts = make_text_setup(tmp_path)
    definition = parse_definition(setup.repository.get(OWNER, "workflow-a").definition_snapshot)
    for index in range(130):
        workflow = f"history-{index}"
        setup.repository.create_or_get(OWNER, WORKSPACE, workflow, client_token=workflow, definition=definition, now=100)
        claim = setup.repository.claim_due(OWNER, worker_id="history", now=100, limit=1, lease_seconds=20)[0]
        ready = setup.repository.freeze_inputs(claim, step_key="develop", generation=1, resolved=setup.resolved, expected_revision=1, now=101)
        setup.repository.admit_step(claim, step_key="develop", generation=1, expected_revision=ready.revision,
            target_key=setup.target.target_key, capacity=1024, reserve_tokens=100, reserve_requests=2, now=102, external_target=setup.target)
    statements: list[str] = []
    with setup.repository.transaction(write=False) as connection:
        connection.set_trace_callback(statements.append)
        setup.store.validate_snapshot(connection)
    queries = [statement for statement in statements if "ORDER BY e.rowid LIMIT 128" in statement]
    assert len(queries) == 3 and all("ORDER BY e.rowid LIMIT 128" in query for query in queries)
    with setup.repository.transaction() as connection:
        connection.execute("UPDATE workflow_external_bindings SET send_started_at=104,updated_at=104,request_snapshot=? WHERE rowid=(SELECT max(rowid) FROM workflow_external_bindings)", (b"{}",))
    with pytest.raises(BackupError, match="external_snapshot_invalid"):
        PlatformBackupService().create_backup(setup.repository.db_path, artifacts.root, tmp_path / "history-backup")


def test_external_admission_keeps_hub_text_separate_from_node_receipts(tmp_path: Path) -> None:
    setup, artifacts = make_text_setup(tmp_path, admit=False)
    setup.repository.artifact_input_public_key = b"k" * 32
    setup.admit(setup.target)
    observed = setup.dispatch(text_executor(setup, artifacts))
    assert observed.state == "waiting_external" and len(setup.transport.sends) == 1


def test_external_freeze_rejects_node_input_target(tmp_path: Path) -> None:
    setup, artifacts = make_text_setup(tmp_path, admit=False, freeze=False)
    with pytest.raises(WorkflowRepositoryError, match="invalid_input_target"):
        setup.repository.freeze_inputs(setup.claim, step_key="develop", generation=1, resolved=setup.resolved,
            expected_revision=1, now=101, input_target=setup.target.target_key, inputs_pending=True)
    assert setup.repository.current_steps(OWNER, "workflow-a")[0].resolved_snapshot is None


def test_bootstrap_coordinator_resolves_text_without_node_alias(tmp_path: Path) -> None:
    from hub.application.workflow_coordinator import RegisteredWorkflowPlan
    from hub.bootstrap import create_app
    from hub.config import FleetConfig

    setup, artifacts = make_text_setup(tmp_path, admit=False, freeze=False)
    setup.repository.release_claim(setup.claim, now=104, next_poll_at=104)
    definition = setup.repository.get(OWNER, "workflow-a")
    plan = RegisteredWorkflowPlan(OWNER, WORKSPACE, definition.definition_digest, "develop", CatalogRef("muse", 1),
        CatalogRef("safe", 1), setup.target.target_key, 1, 100, 2, external_target=setup.target)
    config = FleetConfig.from_root(tmp_path, platform_enabled=True, platform_workflows_enabled=True,
        platform_workflow_dispatch_enabled=True, platform_external_agents_enabled=True,
        platform_db=setup.repository.db_path, platform_artifact_root=artifacts.root, dev_operator=OWNER, serve_frontend=False)
    app = create_app(config, repositories={"workflow_clock": setup.clock, "workflow_steps": (plan,),
        "workflow_external_agents": ((setup.target, setup.adapter),)})
    coordinator = app.extensions["fleet"]["services"]["workflow_coordinator"]

    class NodeOnlyInputs:
        def prepare(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("external step attempted Node receive")

    coordinator.artifact_input_service = NodeOnlyInputs()
    coordinator.repository.artifact_input_public_key = b"k" * 32
    for _ in range(4):
        result = asyncio.run(coordinator.tick(setup.clock()))
        assert not result.failures
        setup.clock.now += 1
    assert setup.repository.get(OWNER, "workflow-a").execution_state == "waiting_external"
    assert len(setup.transport.sends) == 1
