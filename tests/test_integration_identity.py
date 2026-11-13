"""Deterministic identity, real shared authority, and restore revocation fences."""
from __future__ import annotations

import hashlib
import asyncio
import json
import multiprocessing
import sqlite3
from dataclasses import FrozenInstanceError, replace
from itertools import count
from pathlib import Path
from typing import Any
from threading import Event

import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.domain.integration_identity import IntegrationIdentity, IntegrationIdentityError, parse_integration_identity
from hub.domain.workflow_identity import canonical_json
from hub.domain.workflow_operator import WorkflowCreateRequest
from hub.domain.workflow_contract import parse_definition
from hub.infrastructure.integration_identity_repository import IntegrationIdentityRepository
from hub.infrastructure.platform_backup import BackupError, PlatformBackupService
from hub.composition.workflows import assert_workflow_visibility
from hub.infrastructure.workflow_repository import WorkflowRepository, WorkflowRepositoryError
from hub.infrastructure.workflow_schema import inspect_schema
from test_fleet_mcp_boundary import _app, mcp
from test_workflow_http import body
from test_workflow_repository import OWNER, WORKSPACE, prepare_database

TOKEN = "identity-test-" + "a" * 32


def identity(name: str = "integration", token: str = TOKEN, **changes: Any) -> IntegrationIdentity:
    value = IntegrationIdentity.register(name, OWNER, token, template_ids={"fixture"}, workspace_ids={WORKSPACE})
    return replace(value, **changes)


def store_at(tmp_path: Path) -> IntegrationIdentityRepository:
    result = IntegrationIdentityRepository(prepare_database(tmp_path / "platform.db"))
    result.init()
    return result


def test_identity_is_immutable_canonical_private_and_namespace_stable() -> None:
    value = identity()
    assert parse_integration_identity(value.snapshot) == value
    assert value.definition_digest == hashlib.sha256(value.snapshot).hexdigest()
    assert value.token_digest not in repr(value) and TOKEN not in repr(value)
    assert TOKEN.encode() not in value.snapshot
    assert replace(value, revoked=True).snapshot == value.snapshot
    assert replace(value, max_artifacts=1).token_namespace == value.token_namespace
    assert identity("other").token_namespace != value.token_namespace
    with pytest.raises(FrozenInstanceError):
        value.revoked = True
    assert value.authenticate(TOKEN)
    assert not replace(value, revoked=True).authenticate(TOKEN)


@pytest.mark.parametrize("changes", [
    {"owner_id": ""}, {"owner_id": "\ud800"}, {"integration_id": "x" * 129}, {"integration_id": "a\0b"},
    {"token_digest": "A" * 64}, {"token_digest": "z" * 64}, {"token_digest": "1" * 63}, {"token_digest": 1},
    {"template_ids": frozenset()}, {"template_ids": frozenset({True})}, {"template_ids": {"fixture"}},
    {"workspace_ids": frozenset({""})}, {"workspace_ids": frozenset({str(n) for n in range(129)})},
    {"scopes": frozenset({"workflow.acceptance"})}, {"scopes": []},
    {"max_artifacts": True}, {"max_artifacts": 1.0}, {"max_artifacts": 101},
    {"max_budget_tokens": -1}, {"max_budget_tokens": 2_000_001}, {"max_budget_tokens": False},
    {"max_budget_requests": 1001}, {"max_budget_requests": 1.0}, {"revoked": 1},
])
def test_direct_identity_construction_rejects_invalid_authority(changes: dict[str, Any]) -> None:
    with pytest.raises(IntegrationIdentityError):
        identity(**changes)


@pytest.mark.parametrize("token", [None, True, "x" * 31, "x" * 513, " " * 32, "é" * 32, "\n" + "x" * 32])
def test_bearer_validation_is_bounded_and_never_coerces(token: Any) -> None:
    with pytest.raises(IntegrationIdentityError, match="invalid_integration_token"):
        IntegrationIdentity.register("integration", OWNER, token, template_ids={"fixture"}, workspace_ids={WORKSPACE})
    assert not identity().authenticate(token)


def test_allowlist_input_consumption_is_bounded_and_empty_scopes_stay_empty() -> None:
    consumed = []

    def endless():
        for index in count():
            consumed.append(index)
            yield str(index)

    with pytest.raises(IntegrationIdentityError, match="invalid_integration_scope"):
        IntegrationIdentity.register("integration", OWNER, TOKEN, template_ids=endless(), workspace_ids={WORKSPACE})
    assert len(consumed) == 129
    value = IntegrationIdentity.register("integration", OWNER, TOKEN, template_ids={"fixture"}, workspace_ids={WORKSPACE}, scopes=())
    assert value.scopes == frozenset()
    for values in ("fixture", {"fixture": True}, ["fixture", "fixture"], None):
        with pytest.raises(IntegrationIdentityError):
            IntegrationIdentity.register("integration", OWNER, TOKEN, template_ids=values, workspace_ids={WORKSPACE})


@pytest.mark.parametrize("change", ["whitespace", "extra", "duplicate", "revoked", "unsorted"])
def test_identity_snapshot_rejects_noncanonical_or_extra_authority(change: str) -> None:
    value = identity(template_ids=frozenset({"a", "b"}))
    raw = json.loads(value.snapshot)
    if change == "whitespace":
        encoded = json.dumps(raw, indent=2).encode()
    elif change == "duplicate":
        encoded = value.snapshot[:-1] + b',"owner_id":"other"}'
    else:
        if change == "unsorted":
            raw["template_ids"].reverse()
        else:
            raw[change] = True
        encoded = canonical_json(raw)
    with pytest.raises(IntegrationIdentityError, match="integration_snapshot_invalid"):
        parse_integration_identity(encoded)


def test_registration_is_durable_immutable_unique_and_revocation_cannot_be_undone(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    value = identity()
    assert store.register(value, now=100) == value
    assert store.authenticate(TOKEN) == value
    for changed in (replace(value, max_artifacts=1), replace(value, token_digest="b" * 64)):
        with pytest.raises(IntegrationIdentityError, match="integration_registration_conflict"):
            store.register(changed, now=101)
    for changed in (replace(value, integration_id="foreign"), replace(value, owner_id="other")):
        with pytest.raises(IntegrationIdentityError, match="integration_token_conflict"):
            store.register(changed, now=101)
    with pytest.raises(IntegrationIdentityError, match="integration_not_found"):
        store.revoke("other", value.integration_id, expected_digest=value.definition_digest, now=101)
    with pytest.raises(IntegrationIdentityError, match="integration_revision_conflict"):
        store.revoke(OWNER, value.integration_id, expected_digest="a" * 64, now=101)
    assert store.authenticate(TOKEN) == value
    assert store.revoke(OWNER, value.integration_id, expected_digest=value.definition_digest, now=102).revoked
    store.revoke(OWNER, value.integration_id, expected_digest=value.definition_digest, now=103)
    reloaded = IntegrationIdentityRepository(WorkflowRepository(store.workflows.db_path))
    reloaded.init()
    assert reloaded.authenticate(TOKEN) is None
    assert reloaded.register(value, now=104).revoked
    with store.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT revoked_at FROM workflow_integration_identities").fetchone()[0] == 102
        IntegrationIdentityRepository.validate_snapshot(connection)


def test_failed_registration_rolls_back_and_preserves_sqlite_cause(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    with store.workflows.transaction() as connection:
        connection.execute("CREATE TRIGGER reject_identity BEFORE INSERT ON workflow_integration_identities BEGIN SELECT RAISE(ABORT,'fixture'); END")
    with pytest.raises(WorkflowRepositoryError, match="workflow_store") as error:
        store.register(identity(), now=100)
    assert isinstance(error.value.__cause__, sqlite3.Error)
    with store.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM workflow_integration_identities").fetchone()[0] == 0


@pytest.mark.parametrize("scope", ["workflow.get", "workflow.list_artifacts", "workflow.create_from_template"])
def test_revocation_after_http_authentication_blocks_detail_and_prior_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str) -> None:
    app, client, setup, token, value = _app(tmp_path)
    created = mcp(client, token, "tools/call", {"name": "workflow.create_from_template", "arguments": body()})
    workflow = created.get_json()["result"]["structuredContent"]["workflow_id"]
    store = app.extensions["fleet"]["repositories"]["integration_identities"]
    original = store.authenticate

    def authenticate_then_revoke(bearer):
        principal = original(bearer)
        store.revoke(value.owner_id, value.integration_id, expected_digest=value.definition_digest, now=101)
        return principal

    monkeypatch.setattr(store, "authenticate", authenticate_then_revoke)
    arguments = body() if scope == "workflow.create_from_template" else {"workflow_id": workflow}
    reply = mcp(client, token, "tools/call", {"name": scope, "arguments": arguments})
    assert reply.status_code == 401 and reply.get_json()["error"]["message"] == "integration_revoked"
    assert len(setup.repository.list(value.owner_id)) == 2


def test_stale_or_forged_principal_does_not_borrow_persistent_authority(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    value = store.register(identity(), now=100)
    for changed in (replace(value, max_budget_tokens=2_000_000), replace(value, template_ids=frozenset({"other"})),
                    replace(value, token_digest="c" * 64), replace(value, revoked=True)):
        with store.workflows.transaction(write=False) as connection:
            with pytest.raises(IntegrationIdentityError, match="integration_revoked"):
                store.require_active(connection, changed, scope="workflow.get")


def _process_revoke(path: str, expected: str, ready: Any, proceed: Any, output: Any) -> None:
    store = IntegrationIdentityRepository(WorkflowRepository(Path(path)))
    store.init()
    active = store.authenticate(TOKEN)
    ready.set()
    if not proceed.wait(8):
        raise RuntimeError("fixture_timeout")
    revoked = store.revoke(OWNER, "integration", expected_digest=expected, now=101)
    output.put((active is not None, revoked.revoked, store.authenticate(TOKEN) is None))


def test_real_process_restart_and_revocation_reject_cached_principal(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    value = store.register(identity(), now=100)
    context = multiprocessing.get_context("spawn")
    ready, proceed, output = context.Event(), context.Event(), context.Queue()
    process = context.Process(target=_process_revoke, args=(str(store.workflows.db_path), value.definition_digest, ready, proceed, output))
    process.start()
    try:
        assert ready.wait(8)
        assert store.authenticate(TOKEN) == value
        proceed.set()
        assert output.get(timeout=8) == (True, True, True)
        process.join(8)
        assert process.exitcode == 0
        assert store.authenticate(TOKEN) is None
        with store.workflows.transaction(write=False) as connection:
            with pytest.raises(IntegrationIdentityError, match="integration_revoked"):
                store.require_active(connection, value, scope="workflow.get")
    finally:
        if process.is_alive():
            process.terminate()
            process.join(3)
        output.close()
        output.join_thread()


def test_bootstrap_gate_off_is_passive_and_restart_needs_no_injected_identity(tmp_path: Path) -> None:
    workflows = prepare_database(tmp_path / "platform.db")
    config = FleetConfig.from_root(tmp_path, platform_enabled=True, platform_db=workflows.db_path,
        platform_workflows_enabled=True, serve_frontend=False)
    app = create_app(config, repositories={"integration_identities": (identity(),)})
    assert not isinstance(app.extensions["fleet"]["repositories"].get("integration_identities"), IntegrationIdentityRepository)
    with workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name GLOB 'workflow_integration_*'").fetchall() == []
        assert inspect_schema(connection)["minimum_reader_version"] == 1
    enabled = replace(config, platform_fleet_mcp_enabled=True)
    first = create_app(enabled, repositories={"integration_identities": (identity(),)})
    assert mcp(first.test_client(), TOKEN, "tools/list", {}).status_code == 200
    restarted = create_app(enabled)
    assert mcp(restarted.test_client(), TOKEN, "tools/list", {}).status_code == 200
    store = restarted.extensions["fleet"]["repositories"]["integration_identities"]
    store.revoke(OWNER, "integration", expected_digest=identity().definition_digest, now=253402300799)
    repeated = create_app(enabled, repositories={"integration_identities": (identity(),)})
    assert mcp(repeated.test_client(), TOKEN, "tools/list", {}).status_code == 401


@pytest.mark.parametrize("encrypted", [False, True])
def test_backup_restore_disables_credentials_without_mutating_source(tmp_path: Path, encrypted: bool) -> None:
    store = store_at(tmp_path)
    value = store.register(identity(), now=100)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    service = PlatformBackupService()
    if encrypted:
        summary = service.create_encrypted_backup(store.workflows.db_path, artifacts, backup, b"i" * 32, "identity-key")["source"]
        service.restore_encrypted_backup(backup, restored, b"i" * 32)
    else:
        summary = service.create_backup(store.workflows.db_path, artifacts, backup)
        service.restore_backup(backup, restored)
    assert summary["workflow"] == {"schema_version": 1, "minimum_reader_version": 4}
    assert store.authenticate(TOKEN) == value
    recovered = IntegrationIdentityRepository(WorkflowRepository(restored / "platform.db"))
    recovered.workflows.init()
    recovered.init()
    assert recovered.authenticate(TOKEN) is None
    assert recovered.register(value, now=101).revoked
    with recovered.workflows.transaction(write=False) as connection:
        assert inspect_schema(connection)["admission_mode"] == "frozen"
        assert tuple(connection.execute("SELECT revoked_at,restore_disabled FROM workflow_integration_identities").fetchone()) == (None, 1)
    store.revoke(OWNER, "integration", expected_digest=value.definition_digest, now=102)
    assert recovered.authenticate(TOKEN) is None


@pytest.mark.parametrize("sql,code", [
    ("UPDATE workflow_integration_schema_meta SET schema_version=99", "unsupported_integration_schema"),
    ("UPDATE workflow_integration_schema_meta SET minimum_reader_version=2", "unsupported_integration_schema"),
    ("UPDATE workflow_schema_meta SET minimum_reader_version=3", "invalid_integration_reader"),
    ("UPDATE workflow_schema_meta SET minimum_reader_version=99", "unsupported_workflow_schema"),
    ("ALTER TABLE workflow_integration_identities RENAME TO identity_history", "incomplete_integration_schema"),
])
def test_gate_off_readers_and_backup_reject_incompatible_identity_metadata(tmp_path: Path, sql: str, code: str) -> None:
    store = store_at(tmp_path)
    with store.workflows.transaction() as connection:
        connection.execute(sql)
    with pytest.raises(WorkflowRepositoryError, match=code):
        store.workflows.init()
    with pytest.raises(WorkflowRepositoryError, match="workflow_visibility_unavailable"):
        assert_workflow_visibility(store.workflows.db_path, enabled=False)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    with pytest.raises(BackupError, match=code):
        PlatformBackupService().create_backup(store.workflows.db_path, artifacts, tmp_path / "backup")


@pytest.mark.parametrize("change", ["digest", "owner", "noncanonical", "text_snapshot", "revoked_history"])
def test_corrupt_identity_snapshots_reject_authentication_and_backup(tmp_path: Path, change: str) -> None:
    store = store_at(tmp_path)
    value = store.register(identity(), now=100)
    with store.workflows.transaction() as connection:
        if change == "digest":
            connection.execute("UPDATE workflow_integration_identities SET definition_digest=?", ("a" * 64,))
        elif change == "owner":
            connection.execute("UPDATE workflow_integration_identities SET owner_id='other'")
        else:
            snapshot = json.dumps(json.loads(value.snapshot)).encode() if change == "noncanonical" else value.snapshot.decode()
            if change == "revoked_history":
                snapshot = b"{}"
                connection.execute("UPDATE workflow_integration_identities SET revoked_at=101")
            connection.execute("UPDATE workflow_integration_identities SET definition_snapshot=?", (snapshot,))
    with pytest.raises(IntegrationIdentityError, match="integration_snapshot_invalid"):
        store.authenticate(TOKEN)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    with pytest.raises(BackupError, match="integration_snapshot_invalid"):
        PlatformBackupService().create_backup(store.workflows.db_path, artifacts, tmp_path / "backup")


def test_historical_identity_scan_is_keyset_bounded_and_checks_final_negative_rowid(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    with store.workflows.transaction() as connection:
        for index in range(260):
            value = identity(str(index), f"identity-{index:04d}-" + "x" * 32)
            connection.execute("INSERT INTO workflow_integration_identities VALUES(?,?,?,?,?,?,?,0)",
                (value.owner_id, value.integration_id, value.token_digest, value.snapshot, value.definition_digest, 100, 101))
        # SQLite permits nonpositive rowids; integrity scans must not skip them.
        connection.execute("UPDATE workflow_integration_identities SET rowid=-2 WHERE integration_id='0'")
    queries: list[str] = []
    with store.workflows.transaction(write=False) as connection:
        connection.set_trace_callback(queries.append)
        store.validate_snapshot(connection)
    scanned = [query for query in queries if "SELECT rowid AS identity_rowid" in query]
    assert len(scanned) == 4 and all("LIMIT 128" in query and "OFFSET" not in query for query in scanned)
    for name in ("259", "0"):
        with store.workflows.transaction() as connection:
            connection.execute("UPDATE workflow_integration_identities SET definition_digest=? WHERE integration_id=?", ("b" * 64, name))
        with store.workflows.transaction(write=False) as connection:
            with pytest.raises(IntegrationIdentityError, match="integration_snapshot_invalid"):
                store.validate_snapshot(connection)
        original = identity(name, f"identity-{int(name):04d}-" + "x" * 32)
        with store.workflows.transaction() as connection:
            connection.execute("UPDATE workflow_integration_identities SET definition_digest=? WHERE integration_id=?", (original.definition_digest, name))


def test_identity_reader_remains_independent_of_capability_and_external_domains(tmp_path: Path) -> None:
    from hub.infrastructure.capability_repository import CapabilityRepository
    from hub.infrastructure.workflow_external_repository import ExternalWorkflowRepository

    store = store_at(tmp_path)
    CapabilityRepository(store.workflows, boot_id="identity-tests").init()
    ExternalWorkflowRepository(store.workflows).init()
    with store.workflows.transaction(write=False) as connection:
        assert inspect_schema(connection)["minimum_reader_version"] == 6
    store.workflows.init()
    store.init()


def test_identity_authentication_uses_unique_digest_index_at_scale(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    value = store.register(identity(), now=100)
    with store.workflows.transaction() as connection:
        for index in range(500):
            item = identity(str(index), f"indexed-{index:04d}-" + "x" * 32)
            connection.execute("INSERT INTO workflow_integration_identities VALUES(?,?,?,?,?,?,NULL,0)",
                (item.owner_id, item.integration_id, item.token_digest, item.snapshot, item.definition_digest, 100))
        query = connection.execute("EXPLAIN QUERY PLAN SELECT owner_id FROM workflow_integration_identities WHERE token_digest=?", (value.token_digest,)).fetchall()
        assert "workflow_integration_token" in " ".join(row[3] for row in query)
    assert store.authenticate(TOKEN) == value


def test_identity_schema_initialization_rolls_back_as_one_transaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import hub.infrastructure.integration_identity_repository as module

    workflows = prepare_database(tmp_path / "platform.db")
    monkeypatch.setattr(module, "STATEMENTS", (*module.STATEMENTS, "invalid fixture SQL"))
    with pytest.raises(WorkflowRepositoryError, match="workflow_store"):
        IntegrationIdentityRepository(workflows).init()
    with workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name GLOB 'workflow_integration_*'").fetchall() == []
        assert inspect_schema(connection)["minimum_reader_version"] == 1


def test_http_cannot_authenticate_from_legacy_in_memory_identity_tuple(tmp_path: Path) -> None:
    app, client, setup, token, value = _app(tmp_path)
    app.extensions["fleet"]["repositories"]["integration_identities"] = (value,)
    response = mcp(client, token, "tools/list", {})
    assert response.status_code == 503
    assert len(setup.repository.list(OWNER)) == 1


def test_integration_transaction_rejects_mismatched_definition_and_request(tmp_path: Path) -> None:
    app, _, setup, _, value = _app(tmp_path)
    request = WorkflowCreateRequest(value.owner_id, WORKSPACE, "fixture", 1, (), value.namespace_token("mismatch"), 1)
    raw = json.loads(setup.definition.snapshot)
    raw["template_id"] = "unapproved-template"
    definition = parse_definition(canonical_json(raw))
    repository = app.extensions["fleet"]["services"]["workflows"].repository
    with pytest.raises(IntegrationIdentityError, match="scope_forbidden"):
        repository.create(request, definition, workflow_id="unapproved-workflow", now=101, integration=value)
    assert len(setup.repository.list(OWNER)) == 1


def test_cancelled_integration_create_joins_original_transaction_and_retry_returns_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _, setup, _, value = _app(tmp_path)
    service = app.extensions["fleet"]["services"]["workflows"]
    entered, release, finished = Event(), Event(), Event()
    original = service.repository.create
    request = WorkflowCreateRequest(value.owner_id, WORKSPACE, "fixture", 1, (), value.namespace_token("cancelled-client"), 1)

    def blocked_create(*args, **kwargs):
        entered.set()
        if not release.wait(3):
            raise RuntimeError("fixture_timeout")
        result = original(*args, **kwargs)
        finished.set()
        return result

    monkeypatch.setattr(service.repository, "create", blocked_create)

    async def exercise():
        task = asyncio.create_task(service.create(request, integration=value))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and not finished.is_set()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert finished.is_set()
        receipt = await service.create(request, integration=value)
        assert receipt["workflow_id"] == "created-workflow"

    asyncio.run(exercise())
    assert len(setup.repository.list(OWNER)) == 2


def _race_register(path: str, index: int, ready: Any, proceed: Any, output: Any) -> None:
    value = identity(token=f"competing-{index}-" + "x" * 32)
    store = IntegrationIdentityRepository(WorkflowRepository(Path(path)))
    ready.set()
    if not proceed.wait(8):
        raise RuntimeError("fixture_timeout")
    store.init()
    try:
        store.register(value, now=100)
    except IntegrationIdentityError as exc:
        output.put(exc.code)
    else:
        output.put("registered")


def test_two_process_registration_conflict_has_one_immutable_winner(tmp_path: Path) -> None:
    workflows = prepare_database(tmp_path / "platform.db")
    context = multiprocessing.get_context("spawn")
    proceed, output = context.Event(), context.Queue()
    readiness = (context.Event(), context.Event())
    processes = tuple(context.Process(target=_race_register, args=(str(workflows.db_path), index, readiness[index], proceed, output))
                      for index in range(2))
    for process in processes:
        process.start()
    try:
        assert all(ready.wait(8) for ready in readiness)
        proceed.set()
        assert sorted(output.get(timeout=8) for _ in processes) == ["integration_registration_conflict", "registered"]
        for process in processes:
            process.join(8)
            assert process.exitcode == 0
        store = IntegrationIdentityRepository(workflows)
        store.init()
        assert sum(store.authenticate(f"competing-{index}-" + "x" * 32) is not None for index in range(2)) == 1
        with workflows.transaction(write=False) as connection:
            assert connection.execute("SELECT count(*) FROM workflow_integration_identities").fetchone()[0] == 1
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(3)
        output.close()
        output.join_thread()


def test_partial_token_index_cannot_claim_global_credential_uniqueness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import hub.infrastructure.integration_identity_repository as module

    workflows = prepare_database(tmp_path / "platform.db")
    statements = (*module.STATEMENTS[:-1], module.STATEMENTS[-1] + " WHERE restore_disabled=0")
    monkeypatch.setattr(module, "STATEMENTS", statements)
    store = IntegrationIdentityRepository(workflows)
    with pytest.raises(WorkflowRepositoryError, match="invalid_integration_schema"):
        store.init()


def test_runtime_authentication_rejects_future_workflow_reader(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    value = store.register(identity(), now=100)
    with store.workflows.transaction() as connection:
        connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=99")
    with pytest.raises(IntegrationIdentityError, match="integration_store_unavailable") as error:
        store.authenticate(TOKEN)
    assert isinstance(error.value.__cause__, WorkflowRepositoryError)
    assert error.value.__cause__.code == "unsupported_workflow_schema"
    with pytest.raises(WorkflowRepositoryError, match="unsupported_workflow_schema"):
        with store.workflows.transaction(write=False) as connection:
            store.require_active(connection, value, scope="workflow.get")


def test_startup_snapshot_scan_does_not_own_the_sqlite_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = store_at(tmp_path)
    store.register(identity(), now=100)
    original = store.validate_snapshot
    checked = []

    def inspect_without_writer_lock(connection):
        connection.execute("SELECT count(*) FROM workflow_integration_identities").fetchone()
        competitor = sqlite3.connect(store.workflows.db_path, timeout=0, isolation_level=None)
        try:
            competitor.execute("BEGIN IMMEDIATE")
            competitor.rollback()
            checked.append(True)
        finally:
            competitor.close()
        original(connection)

    monkeypatch.setattr(store, "validate_snapshot", inspect_without_writer_lock)
    store.init()
    assert checked == [True]
