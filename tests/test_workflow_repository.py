"""Real SQLite checks for workflow ownership, atomic admission and fencing."""
from __future__ import annotations

from collections.abc import Iterator
import hashlib
import json
import multiprocessing
import sqlite3
from dataclasses import replace
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from hub.domain.workflow_contract import parse_definition, resolve_contract
from hub.domain.workflow_contract_types import AuthorizedInputs, CatalogRef, WorkflowDefinition
from hub.domain.workflow_records import CoordinatorClaim
from hub.infrastructure.platform_db import PlatformRepository
from hub.infrastructure.platform_backup import BackupError, PlatformBackupService
from hub.infrastructure.workflow_repository import WorkflowRepository, WorkflowRepositoryError
from hub.platform.artifacts import ArtifactStore

OWNER = "owner@example.test"
WORKSPACE = "workspace-a"


def registered(*, source: dict[str, Any] | None = None) -> WorkflowDefinition:
    contract = {
        "schema_version": 1, "objective": "Produce a checked result", "input_refs": [],
        "output_specs": [{"output_key": "report", "kind": "artifact", "required": True, "max_bytes": 100}],
        "capability_requirements": [], "policy_ref": {"id": "safe", "revision": 1},
        "validator_refs": [], "context_refs": [], "retry_class": "manual_only",
        "budget": {"max_tokens": 100, "max_model_requests": 2, "max_active_seconds": 60, "max_wait_seconds": 120},
    }
    if source is not None:
        contract["input_refs"] = [{"ref_key": "source", "kind": "artifact", **source}]
    return parse_definition(json.dumps({
        "schema_version": 1, "template_id": "fixture", "revision": 1, "acceptance_required": True,
        "steps": [{"step_key": "develop", "kind": "coding_task", "requires": [], "contract": contract}],
    }).encode("utf-8"))


def prepare_database(path: Path) -> WorkflowRepository:
    platform = PlatformRepository(path)
    platform.init()
    for owner in (OWNER, "other@example.test"):
        platform.upsert_workspace(owner, {"workspace_id": WORKSPACE, "root_path": "/unused"})
    repository = WorkflowRepository(path)
    repository.init()
    return repository


@pytest.fixture
def repository(tmp_path: Path) -> WorkflowRepository:
    return prepare_database(tmp_path / "platform.db")


def create(repository: WorkflowRepository, workflow_id: str = "workflow-a", *, now: float = 100) -> Any:
    return repository.create_or_get(
        OWNER, WORKSPACE, workflow_id, client_token=workflow_id,
        definition=registered(), now=now,
    )


def test_batch_release_fences_stale_expired_and_other_owner_claims(repository: WorkflowRepository) -> None:
    for index in range(3):
        create(repository, f"workflow-{index}")
    repository.create_or_get("other@example.test", WORKSPACE, "other-workflow", client_token="other-token",
        definition=registered(), now=100)
    first = repository.claim_due(OWNER, worker_id="first", now=100, limit=3, lease_seconds=10)
    other = repository.claim_due("other@example.test", worker_id="other", now=100, limit=1, lease_seconds=60)[0]
    renewed = repository.claim_due(OWNER, worker_id="second", now=111, limit=1, lease_seconds=60)[0]
    assert repository.release_claims((*first, other, renewed), now=111, next_poll_at=120) == (False, False, False, True, True)
    with repository.transaction(write=False) as connection:
        for owner in (OWNER, "other@example.test"):
            row = connection.execute("SELECT ready_at FROM workflow_coordinator_owners WHERE owner_id=?", (owner,)).fetchone()
            expected = 111 if owner == OWNER else 120  # Fair queue never precedes last_served_at.
            assert row[0] == expected
        row = connection.execute("SELECT lease_owner,next_poll_at FROM workflow_runs WHERE workflow_id=?", (renewed.workflow_id,)).fetchone()
        assert tuple(row) == (None, 120)


def test_batch_release_rolls_back_all_leases_when_queue_refresh_fails(repository: WorkflowRepository, monkeypatch) -> None:
    for index in range(2):
        create(repository, f"workflow-{index}")
    claims = repository.claim_due(OWNER, worker_id="worker", now=100, limit=2, lease_seconds=60)

    def unavailable(*_args):
        raise sqlite3.OperationalError("injected queue failure")

    with monkeypatch.context() as patch:
        patch.setattr(repository, "_refresh_owner_queue", unavailable)
        with pytest.raises(WorkflowRepositoryError, match="workflow_store"):
            repository.release_claims(claims, now=101, next_poll_at=102)
    with repository.transaction(write=False) as connection:
        rows = connection.execute("SELECT lease_owner,lease_expires_at FROM workflow_runs").fetchall()
        assert len(rows) == 2 and all(tuple(row) == ("worker", 160) for row in rows)
    assert repository.release_claims(claims, now=101, next_poll_at=102) == (True, True)


@pytest.mark.parametrize("claims", [[], (None,), (None,) * 33])
def test_batch_release_rejects_invalid_collection(repository: WorkflowRepository, claims) -> None:
    with pytest.raises(WorkflowRepositoryError, match="invalid_claims"):
        repository.release_claims(claims, now=100, next_poll_at=101)


def test_additive_schema_is_idempotent_and_preserves_platform_version(repository: WorkflowRepository) -> None:
    repository.init()
    with repository.transaction() as connection:
        assert connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "3"
        meta = connection.execute("SELECT schema_version,minimum_reader_version,admission_mode FROM workflow_schema_meta").fetchone()
        assert tuple(meta) == (1, 1, "open")
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_older_complete_control_schema_is_readable_before_explicit_source_init(repository: WorkflowRepository) -> None:
    from hub.infrastructure.workflow_schema import inspect_schema

    with repository.transaction() as connection:
        connection.execute("DROP TABLE workflow_source_uploads")
        connection.execute("UPDATE workflow_schema_meta SET admission_mode='frozen'")
    with repository.transaction(write=False) as connection:
        assert inspect_schema(connection)["admission_mode"] == "frozen"
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='workflow_source_uploads'").fetchone() is None
    repository.init()
    with repository.transaction(write=False) as connection:
        assert inspect_schema(connection)["admission_mode"] == "frozen"
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='workflow_source_uploads'").fetchone() is not None


def test_create_is_atomic_idempotent_and_scoped(repository: WorkflowRepository) -> None:
    first = create(repository)
    repeated = repository.create_or_get(
        OWNER, WORKSPACE, "unused-new-id", client_token="workflow-a", definition=registered(), now=200,
    )
    assert repeated == first
    assert first.revision == 1
    assert first.definition_digest == hashlib.sha256(first.definition_snapshot).hexdigest()
    assert repository.get("other@example.test", first.workflow_id) is None
    assert repository.current_steps(OWNER, first.workflow_id)[0].state == "pending"
    assert [event.kind for event in repository.events(OWNER, first.workflow_id).events] == ["workflow.created"]
    changed = json.loads(registered().snapshot)
    changed["acceptance_required"] = False
    with pytest.raises(WorkflowRepositoryError, match="idempotency_conflict"):
        repository.create_or_get(OWNER, WORKSPACE, "other-id", client_token="workflow-a",
                                 definition=parse_definition(json.dumps(changed).encode("utf-8")), now=200)


def test_snapshots_cannot_disagree_with_their_parsed_fields(repository: WorkflowRepository) -> None:
    with pytest.raises(WorkflowRepositoryError, match="invalid_definition_snapshot"):
        repository.create_or_get(OWNER, WORKSPACE, "workflow-a", client_token="a",
                                 definition=replace(registered(), revision=2), now=100)
    assert repository.list(OWNER) == ()


def test_scope_foreign_keys_and_outer_transaction_rollback(repository: WorkflowRepository) -> None:
    with pytest.raises(WorkflowRepositoryError, match="reference_forbidden"):
        repository.create_or_get(OWNER, "absent", "workflow-a", client_token="a", definition=registered(), now=100)
    with pytest.raises(RuntimeError, match="interrupt"):
        with repository.transaction() as connection:
            repository.create_or_get(OWNER, WORKSPACE, "workflow-a", client_token="a",
                                     definition=registered(), now=100, connection=connection)
            raise RuntimeError("interrupt")
    assert repository.get(OWNER, "workflow-a") is None
    create(repository)
    with pytest.raises(WorkflowRepositoryError) as error:
        with repository.transaction() as connection:
            connection.execute(
                "INSERT INTO workflow_events(owner_id,workspace_id,workflow_id,sequence,kind,payload,created_at) VALUES(?,?,?,?,?,?,?)",
                ("other@example.test", WORKSPACE, "workflow-a", 2, "bad", b"{}", 100),
            )
    assert isinstance(error.value.__cause__, sqlite3.IntegrityError)


def test_claim_expiry_fences_old_coordinator_without_changing_generation(repository: WorkflowRepository) -> None:
    create(repository)
    first = repository.claim_due(OWNER, worker_id="worker-a", now=100, limit=1, lease_seconds=10)[0]
    assert repository.claim_due(OWNER, worker_id="worker-b", now=109, limit=1, lease_seconds=10) == ()
    second = repository.claim_due(OWNER, worker_id="worker-b", now=110, limit=1, lease_seconds=10)[0]
    assert second.epoch == first.epoch + 1
    resolved = resolve_contract(registered(), AuthorizedInputs(OWNER, WORKSPACE, "develop", CatalogRef("cli", 1), ()))
    with pytest.raises(WorkflowRepositoryError, match="lease_mismatch"):
        repository.freeze_inputs(first, step_key="develop", generation=1, resolved=resolved,
                                 expected_revision=1, now=111)
    ready = repository.freeze_inputs(second, step_key="develop", generation=1, resolved=resolved,
                                    expected_revision=1, now=111)
    assert ready.revision == 2
    assert repository.current_steps(OWNER, "workflow-a")[0].generation == 1
    assert not repository.release_claim(first, now=111, next_poll_at=112)
    assert repository.release_claim(second, now=111, next_poll_at=112)


def test_admission_writes_intent_budget_capacity_and_event_in_one_transaction(repository: WorkflowRepository) -> None:
    create(repository)
    claim = repository.claim_due(OWNER, worker_id="worker", now=100, limit=1, lease_seconds=20)[0]
    resolved = resolve_contract(registered(), AuthorizedInputs(OWNER, WORKSPACE, "develop", CatalogRef("cli", 1), ()))
    ready = repository.freeze_inputs(claim, step_key="develop", generation=1, resolved=resolved,
                                    expected_revision=1, now=101)
    with pytest.raises(RuntimeError, match="interrupt"):
        with repository.transaction() as connection:
            repository.admit_step(claim, step_key="develop", generation=1, expected_revision=ready.revision,
                                  target_key="node-a", capacity=1, reserve_tokens=100, reserve_requests=2,
                                  now=102, connection=connection)
            raise RuntimeError("interrupt")
    assert repository.current_steps(OWNER, "workflow-a")[0].state == "ready"
    with repository.transaction() as connection:
        for table in ("workflow_dispatches", "workflow_budget_reservations", "capability_reservations"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    admitted = repository.admit_step(claim, step_key="develop", generation=1, expected_revision=ready.revision,
                                    target_key="node-a", capacity=1, reserve_tokens=100, reserve_requests=2, now=103)
    assert len(admitted.token) == 64
    assert admitted.payload_hash == resolved.digest
    assert repository.current_steps(OWNER, "workflow-a")[0].state == "dispatching"
    assert repository.get(OWNER, "workflow-a").revision == 3
    events = repository.events(OWNER, "workflow-a", after=1, limit=1)
    assert len(events.events) == 1 and events.next_cursor == 2
    assert repository.events(OWNER, "workflow-a", after=2).events[0].kind == "step.admitted"


def legacy_budget_links(repository: WorkflowRepository, rowids: tuple[int, ...]) -> dict[str, str]:
    """Create real admissions and Runs, then reproduce a pre-repair database."""
    platform = PlatformRepository(repository.db_path)
    platform.create_conversation(OWNER, "budget-link-chat", title="", workspace_id=WORKSPACE)
    definition = registered()
    resolved = resolve_contract(definition, AuthorizedInputs(OWNER, WORKSPACE, "develop", CatalogRef("cli", 1), ()))
    bindings: dict[str, str] = {}
    for index, rowid in enumerate(rowids):
        key = f"budget-link-{index:03d}"
        repository.create_or_get(OWNER, WORKSPACE, key, client_token=key, definition=definition, now=100)
        claim = repository.claim_due(OWNER, worker_id="worker", now=100, limit=1, lease_seconds=20)[0]
        ready = repository.freeze_inputs(claim, step_key="develop", generation=1, resolved=resolved,
                                         expected_revision=1, now=101)
        intent = repository.admit_step(claim, step_key="develop", generation=1, expected_revision=ready.revision,
                                       target_key="node-a", capacity=1024, reserve_tokens=1, reserve_requests=1, now=102)
        run = platform.append_turn(OWNER, "budget-link-chat", f"{key}-message", f"{key}-run",
                                   text="budget link", client_token=key, config_snapshot={}, now=103)["run"]
        with repository.transaction() as connection:
            connection.execute("UPDATE workflow_dispatches SET rowid=?,binding_run_id=? WHERE token=?",
                               (rowid, run["run_id"], intent.token))
        bindings[key] = run["run_id"]
    return bindings


@pytest.mark.parametrize("legacy_rowid", [-9223372036854775808, -1, 0, 9223372036854775807])
def test_init_budget_links_scans_signed_rowid_dispatch(repository: WorkflowRepository, legacy_rowid: int) -> None:
    """Initialization includes zero, negative and SQLite's extreme rowids."""
    bindings = legacy_budget_links(repository, (legacy_rowid,))

    repository.init()

    with repository.transaction(write=False) as connection:
        assert dict(connection.execute("SELECT workflow_id,child_request_id FROM workflow_budget_reservations")) == bindings
        assert connection.execute("SELECT count(*) FROM workflow_budget_checks").fetchone()[0] == 1


@pytest.mark.parametrize("row_count", [0, 128, 129, 257])
def test_init_budget_links_uses_bounded_keyset_pages_and_preserves_check_progress(
    repository: WorkflowRepository, monkeypatch: pytest.MonkeyPatch, row_count: int,
) -> None:
    bindings = legacy_budget_links(repository, tuple(range(-129, row_count - 129)))
    statements: list[str] = []
    original = repository.transaction

    @contextmanager
    def traced(*, write: bool = True) -> Iterator[sqlite3.Connection]:
        with original(write=write) as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(repository, "transaction", traced)
    repository.init()
    pages = [sql for sql in statements if sql.startswith("SELECT d.rowid AS dispatch_rowid,") and "FROM workflow_dispatches d " in sql]
    assert len(pages) == row_count // 128 + 1
    assert all(sql.endswith("ORDER BY d.rowid LIMIT 128") and "OFFSET" not in sql for sql in pages)
    assert "WHERE" not in pages[0]
    with repository.transaction() as connection:
        assert dict(connection.execute("SELECT workflow_id,child_request_id FROM workflow_budget_reservations")) == bindings
        assert connection.execute("SELECT count(*) FROM workflow_budget_checks").fetchone()[0] == row_count
        connection.execute("UPDATE workflow_budget_checks SET active=0,service_sequence=17 WHERE workflow_id='budget-link-000'")
        before = tuple(tuple(row) for row in connection.execute(
            "SELECT workflow_id,active,service_sequence FROM workflow_budget_checks ORDER BY workflow_id"))

    repository.init()

    with repository.transaction(write=False) as connection:
        assert dict(connection.execute("SELECT workflow_id,child_request_id FROM workflow_budget_reservations")) == bindings
        assert tuple(tuple(row) for row in connection.execute(
            "SELECT workflow_id,active,service_sequence FROM workflow_budget_checks ORDER BY workflow_id")) == before


@pytest.mark.parametrize(("last_rowid", "conflict"), [(-1, "missing"), (0, "different"), (128, "missing"), (128, "different")])
def test_init_budget_links_rejects_conflicts_and_rolls_back_earlier_pages(
    repository: WorkflowRepository, last_rowid: int, conflict: str,
) -> None:
    bindings = legacy_budget_links(repository, tuple(range(last_rowid - 128, last_rowid + 1)))
    last_workflow = next(reversed(bindings))
    with repository.transaction() as connection:
        if conflict == "missing":
            connection.execute("DELETE FROM workflow_budget_reservations WHERE workflow_id=?", (last_workflow,))
        else:
            connection.execute("UPDATE workflow_budget_reservations SET child_request_id='previous-attribution' WHERE workflow_id=?",
                               (last_workflow,))
        before = tuple(tuple(row) for row in connection.execute(
            "SELECT workflow_id,child_request_id FROM workflow_budget_reservations ORDER BY workflow_id"))

    with pytest.raises(WorkflowRepositoryError, match="usage_attribution_conflict"):
        repository.init()

    with repository.transaction(write=False) as connection:
        assert tuple(tuple(row) for row in connection.execute(
            "SELECT workflow_id,child_request_id FROM workflow_budget_reservations ORDER BY workflow_id")) == before
        assert connection.execute("SELECT count(*) FROM workflow_budget_checks").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_capacity_budget_and_revision_fail_without_partial_records(repository: WorkflowRepository) -> None:
    create(repository)
    claim = repository.claim_due(OWNER, worker_id="worker", now=100, limit=1, lease_seconds=20)[0]
    resolved = resolve_contract(registered(), AuthorizedInputs(OWNER, WORKSPACE, "develop", CatalogRef("cli", 1), ()))
    ready = repository.freeze_inputs(claim, step_key="develop", generation=1, resolved=resolved,
                                    expected_revision=1, now=101)
    for revision, tokens, requests, code in ((1, 1, 1, "revision_conflict"), (2, 101, 1, "budget_exhausted"), (2, 1, 3, "budget_exhausted")):
        with pytest.raises(WorkflowRepositoryError, match=code):
            repository.admit_step(claim, step_key="develop", generation=1, expected_revision=revision,
                                  target_key="node-a", capacity=1, reserve_tokens=tokens, reserve_requests=requests, now=102)
    repository.admit_step(claim, step_key="develop", generation=1, expected_revision=ready.revision,
                          target_key="node-a", capacity=1, reserve_tokens=1, reserve_requests=1, now=102)
    create(repository, "workflow-b")
    claim_b = repository.claim_due(OWNER, worker_id="other-worker", now=103, limit=1, lease_seconds=20)[0]
    repository.freeze_inputs(claim_b, step_key="develop", generation=1, resolved=resolved, expected_revision=1, now=104)
    with pytest.raises(WorkflowRepositoryError, match="capacity_exhausted"):
        repository.admit_step(claim_b, step_key="develop", generation=1, expected_revision=2,
                              target_key="node-a", capacity=1, reserve_tokens=1, reserve_requests=1, now=105)
    assert repository.current_steps(OWNER, "workflow-b")[0].state == "ready"


@pytest.mark.parametrize("state", ["held", "isolated", "released"])
@pytest.mark.parametrize("owner", [OWNER, "other@example.test"])
def test_isolated_reservation_counts_for_fixed_target_capacity(
    repository: WorkflowRepository, state: str, owner: str,
) -> None:
    create(repository)
    claim = repository.claim_due(OWNER, worker_id="worker-a", now=100, limit=1, lease_seconds=20)[0]
    resolved = resolve_contract(registered(), AuthorizedInputs(OWNER, WORKSPACE, "develop", CatalogRef("cli", 1), ()))
    ready = repository.freeze_inputs(claim, step_key="develop", generation=1, resolved=resolved,
                                    expected_revision=1, now=101)
    repository.admit_step(claim, step_key="develop", generation=1, expected_revision=ready.revision,
                          target_key="node-a", capacity=1, reserve_tokens=1, reserve_requests=1, now=102)
    with repository.transaction() as connection:
        connection.execute("UPDATE capability_reservations SET state=? WHERE owner_id=? AND workflow_id=?", (state, OWNER, "workflow-a"))

    repository.create_or_get(owner, WORKSPACE, "workflow-b", client_token="workflow-b", definition=registered(), now=103)
    claim_b = repository.claim_due(owner, worker_id="worker-b", now=103, limit=1, lease_seconds=20)[0]
    other_resolved = resolve_contract(registered(), AuthorizedInputs(owner, WORKSPACE, "develop", CatalogRef("cli", 1), ()))
    ready_b = repository.freeze_inputs(claim_b, step_key="develop", generation=1, resolved=other_resolved,
                                       expected_revision=1, now=104)
    before_events = repository.events(owner, "workflow-b")
    if state != "released":
        with pytest.raises(WorkflowRepositoryError, match="capacity_exhausted"):
            repository.admit_step(claim_b, step_key="develop", generation=1, expected_revision=ready_b.revision,
                                  target_key="node-a", capacity=1, reserve_tokens=1, reserve_requests=1, now=105)
        assert repository.get(owner, "workflow-b") == ready_b
        assert repository.events(owner, "workflow-b") == before_events
        with repository.transaction(write=False) as connection:
            for table in ("workflow_dispatches", "workflow_budget_reservations", "capability_reservations"):
                assert connection.execute(f"SELECT count(*) FROM {table} WHERE owner_id=? AND workflow_id=?", (owner, "workflow-b")).fetchone()[0] == 0
    target = "node-a" if state == "released" else "node-b"
    repository.admit_step(claim_b, step_key="develop", generation=1, expected_revision=ready_b.revision,
                          target_key=target, capacity=1, reserve_tokens=1, reserve_requests=1, now=106)
    with repository.transaction(write=False) as connection:
        assert connection.execute("SELECT state FROM capability_reservations WHERE owner_id=? AND workflow_id=?", (OWNER, "workflow-a")).fetchone()[0] == state
        assert connection.execute("SELECT target_key FROM capability_reservations WHERE owner_id=? AND workflow_id=?", (owner, "workflow-b")).fetchone()[0] == target


def _race_fixed_capacity(path: str, claim: CoordinatorClaim, barrier: Any, outcomes: Any) -> None:
    repository = WorkflowRepository(Path(path))
    barrier.wait(timeout=20)
    try:
        repository.admit_step(claim, step_key="develop", generation=1, expected_revision=2,
                              target_key="node-a", capacity=2, reserve_tokens=1, reserve_requests=1, now=110)
        outcomes.put("admitted")
    except WorkflowRepositoryError as exc:
        outcomes.put(exc.code)


def test_two_process_fixed_admissions_include_isolated_slot(repository: WorkflowRepository) -> None:
    resolved = resolve_contract(registered(), AuthorizedInputs(OWNER, WORKSPACE, "develop", CatalogRef("cli", 1), ()))
    claims = []
    for workflow in ("workflow-a", "workflow-b", "workflow-c"):
        create(repository, workflow)
        claim = repository.claim_due(OWNER, worker_id=workflow, now=100, limit=1, lease_seconds=30)[0]
        repository.freeze_inputs(claim, step_key="develop", generation=1, resolved=resolved, expected_revision=1, now=101)
        claims.append(claim)
    repository.admit_step(claims[0], step_key="develop", generation=1, expected_revision=2,
                          target_key="node-a", capacity=2, reserve_tokens=1, reserve_requests=1, now=102)
    with repository.transaction() as connection:
        connection.execute("UPDATE capability_reservations SET state='isolated' WHERE owner_id=? AND workflow_id=?", (OWNER, "workflow-a"))
    context = multiprocessing.get_context("spawn")
    barrier, outcomes = context.Barrier(2), context.Queue()
    processes = [context.Process(target=_race_fixed_capacity, args=(str(repository.db_path), claim, barrier, outcomes)) for claim in claims[1:]]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=30)
            assert process.exitcode == 0
        assert sorted(outcomes.get(timeout=2) for _ in processes) == ["admitted", "capacity_exhausted"]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        outcomes.close()
        outcomes.join_thread()
    with repository.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM capability_reservations WHERE state IN ('held','isolated')").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM workflow_dispatches").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM workflow_budget_reservations").fetchone()[0] == 2
        assert connection.execute("SELECT state FROM capability_reservations WHERE owner_id=? AND workflow_id=?", (OWNER, "workflow-a")).fetchone()[0] == "isolated"


@pytest.mark.parametrize("field,value", [("limit", True), ("limit", 0), ("limit", 33), ("lease_seconds", 0), ("lease_seconds", float("nan"))])
def test_claim_parameters_are_exact_and_bounded(repository: WorkflowRepository, field: str, value: object) -> None:
    parameters: dict[str, Any] = {"worker_id": "worker", "now": 100, "limit": 1, "lease_seconds": 10}
    parameters[field] = value
    with pytest.raises(WorkflowRepositoryError):
        repository.claim_due(OWNER, **parameters)


def test_future_schema_is_rejected_without_overwriting_metadata(repository: WorkflowRepository) -> None:
    with repository.transaction() as connection:
        connection.execute("UPDATE workflow_schema_meta SET schema_version=2,minimum_reader_version=2")
    with pytest.raises(WorkflowRepositoryError, match="unsupported_workflow_schema"):
        repository.init()
    with sqlite3.connect(repository.db_path) as connection:
        assert connection.execute("SELECT schema_version FROM workflow_schema_meta").fetchone()[0] == 2


def test_schema_failure_rolls_back_all_additions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import hub.infrastructure.workflow_schema as schema

    path = tmp_path / "platform.db"
    PlatformRepository(path).init()
    monkeypatch.setattr(schema, "SCHEMA_STATEMENTS", (*schema.SCHEMA_STATEMENTS, "SELECT missing_column FROM missing_table"))
    with pytest.raises(WorkflowRepositoryError):
        WorkflowRepository(path).init()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name LIKE 'workflow_%'").fetchall() == []
        assert connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "3"


def _concurrent_init_and_claim(path: str, worker: str, output: Any) -> None:
    try:
        repo = WorkflowRepository(Path(path))
        repo.init()
        claims = repo.claim_due(OWNER, worker_id=worker, now=100, limit=1, lease_seconds=30)
        output.put(("ok", len(claims)))
    except Exception as exc:
        output.put(("error", repr(exc)))


def test_two_os_processes_cannot_claim_one_workflow_twice(repository: WorkflowRepository) -> None:
    create(repository)
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    children = [context.Process(target=_concurrent_init_and_claim, args=(str(repository.db_path), f"worker-{i}", output)) for i in range(2)]
    try:
        for child in children:
            child.start()
        results = [output.get(timeout=15) for _ in children]
        for child in children:
            child.join(15)
            assert child.exitcode == 0
        assert sorted(results) == [("ok", 0), ("ok", 1)]
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(5)
        output.close()
        output.join_thread()


@pytest.mark.parametrize("encrypted", [False, True])
def test_backup_recognizes_workflow_references_and_restore_is_frozen(tmp_path: Path, encrypted: bool) -> None:
    repository = prepare_database(tmp_path / "platform.db")
    source = tmp_path / "source.txt"
    source.write_text("workflow immutable input", encoding="utf-8")
    root = tmp_path / "artifacts"
    artifact = ArtifactStore(root).put_file(OWNER, WORKSPACE, source)
    definition = registered(source={key: artifact[key] for key in ("artifact_id", "sha256", "size")})
    repository.create_or_get(OWNER, WORKSPACE, "workflow-a", client_token="a", definition=definition, now=100)
    service = PlatformBackupService()
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    if encrypted:
        created = service.create_encrypted_backup(repository.db_path, root, backup, b"a" * 32, "key-a")
        summary = created["source"]
        service.restore_encrypted_backup(backup, restored, b"a" * 32)
    else:
        summary = service.create_backup(repository.db_path, root, backup)
        service.restore_backup(backup, restored)
    assert summary["workflow"]["schema_version"] == 1
    assert summary["workflow"]["minimum_reader_version"] == 1
    recovered = WorkflowRepository(restored / "platform.db")
    recovered.init()
    assert recovered.get(OWNER, "workflow-a").definition_digest == hashlib.sha256(definition.snapshot).hexdigest()
    assert recovered.claim_due(OWNER, worker_id="worker", now=200, limit=1, lease_seconds=10) == ()
    with recovered.transaction() as connection:
        assert connection.execute("SELECT admission_mode FROM workflow_schema_meta").fetchone()[0] == "frozen"
    assert ArtifactStore(restored / "artifacts").read(OWNER, WORKSPACE, artifact["artifact_id"]) == b"workflow immutable input"


def test_backup_fails_closed_for_missing_workflow_artifact_and_future_reader(tmp_path: Path) -> None:
    repository = prepare_database(tmp_path / "platform.db")
    root = tmp_path / "artifacts"
    root.mkdir()
    repository.create_or_get(OWNER, WORKSPACE, "workflow-a", client_token="a", now=100,
                             definition=registered(source={"artifact_id": "missing", "sha256": "a" * 64, "size": 12}))
    service = PlatformBackupService()
    with pytest.raises(BackupError, match="workflow_artifact_missing"):
        service.create_backup(repository.db_path, root, tmp_path / "backup")
    with repository.transaction() as connection:
        connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=2")
    with pytest.raises(BackupError, match="unsupported_workflow_schema"):
        service.create_backup(repository.db_path, root, tmp_path / "future-backup")
    assert not (tmp_path / "backup").exists()
    assert not (tmp_path / "future-backup").exists()


@pytest.mark.parametrize("mismatch", ["sha256", "owner_id", "workspace_id"])
def test_backup_verifies_artifact_content_and_scope(tmp_path: Path, mismatch: str) -> None:
    repository = prepare_database(tmp_path / "platform.db")
    source = tmp_path / "source.txt"
    source.write_text("owned input", encoding="utf-8")
    root = tmp_path / "artifacts"
    artifact = ArtifactStore(root).put_file(OWNER, WORKSPACE, source)
    repository.create_or_get(OWNER, WORKSPACE, "workflow-a", client_token="a", now=100,
                             definition=registered(source={key: artifact[key] for key in ("artifact_id", "sha256", "size")}))
    if mismatch == "sha256":
        with repository.transaction() as connection:
            connection.execute("UPDATE workflow_artifact_refs SET sha256=?", ("b" * 64,))
    else:
        manifest = root / artifact["artifact_id"] / "manifest.json"
        value = json.loads(manifest.read_text(encoding="utf-8"))
        value[mismatch] = "other"
        manifest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(BackupError, match="workflow_artifact_conflict"):
        PlatformBackupService().create_backup(repository.db_path, root, tmp_path / "backup")


def test_conversation_cannot_be_deleted_while_referenced_even_without_workflow_gate(repository: WorkflowRepository) -> None:
    platform = PlatformRepository(repository.db_path)
    platform.create_conversation(OWNER, "conversation-a", title="", workspace_id=WORKSPACE)
    repository.create_or_get(OWNER, WORKSPACE, "workflow-a", client_token="a", definition=registered(), now=100,
                             conversation_id="conversation-a")
    platform.set_conversation_archived(OWNER, "conversation-a", archived=True, now=101)
    from hub.infrastructure.platform_db import PlatformRepositoryError
    with pytest.raises(PlatformRepositoryError, match="conversation_workflow_referenced"):
        platform.delete_archived_conversation(OWNER, "conversation-a")
    assert platform.get_conversation(OWNER, "conversation-a") is not None
    from hub.application.conversation_service import ConversationService
    from hub.application.task_service import ApplicationError
    with pytest.raises(ApplicationError) as translated:
        ConversationService(platform, None).delete_archived(OWNER, "conversation-a")
    assert translated.value.code == "conversation_workflow_referenced"
    assert isinstance(translated.value.__cause__, PlatformRepositoryError)


def _admit_in_process(path: str, claim: Any, start: Any, output: Any) -> None:
    try:
        if not start.wait(10):
            raise RuntimeError("start_timeout")
        repository = WorkflowRepository(Path(path))
        repository.admit_step(claim, step_key="develop", generation=1, expected_revision=2,
                               target_key="node-shared", capacity=1, reserve_tokens=100, reserve_requests=2, now=102)
        output.put("admitted")
    except WorkflowRepositoryError as exc:
        output.put(exc.code)


def test_two_processes_cannot_oversell_last_persistent_target_slot(repository: WorkflowRepository) -> None:
    create(repository, "workflow-a")
    create(repository, "workflow-b")
    claims = repository.claim_due(OWNER, worker_id="worker", now=100, limit=2, lease_seconds=30)
    resolved = resolve_contract(registered(), AuthorizedInputs(OWNER, WORKSPACE, "develop", CatalogRef("cli", 1), ()))
    for claim in claims:
        repository.freeze_inputs(claim, step_key="develop", generation=1, resolved=resolved, expected_revision=1, now=101)
    context = multiprocessing.get_context("spawn")
    start, output = context.Event(), context.Queue()
    children = [context.Process(target=_admit_in_process, args=(str(repository.db_path), claim, start, output)) for claim in claims]
    try:
        for child in children:
            child.start()
        start.set()
        assert sorted(output.get(timeout=15) for _ in children) == ["admitted", "capacity_exhausted"]
        for child in children:
            child.join(15)
            assert child.exitcode == 0
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(5)
        output.close()
        output.join_thread()
    with repository.transaction() as connection:
        for table in ("workflow_dispatches", "workflow_budget_reservations", "capability_reservations"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1
    # Reclaiming coordinator leases never frees the execution reservation.
    assert len(repository.claim_due(OWNER, worker_id="replacement", now=200, limit=2, lease_seconds=30)) == 2
    with repository.transaction() as connection:
        assert connection.execute("SELECT count(*) FROM capability_reservations WHERE state='held'").fetchone()[0] == 1


def test_caught_inner_failure_cannot_commit_half_an_admission(repository: WorkflowRepository) -> None:
    create(repository)
    claim = repository.claim_due(OWNER, worker_id="worker", now=100, limit=1, lease_seconds=30)[0]
    resolved = resolve_contract(registered(), AuthorizedInputs(OWNER, WORKSPACE, "develop", CatalogRef("cli", 1), ()))
    repository.freeze_inputs(claim, step_key="develop", generation=1, resolved=resolved, expected_revision=1, now=101)
    with repository.transaction() as connection:
        connection.execute("CREATE TRIGGER fail_admission BEFORE UPDATE OF state ON workflow_steps WHEN NEW.state='dispatching' BEGIN SELECT RAISE(ABORT,'injected_fault'); END")
    with repository.transaction() as connection:
        with pytest.raises(WorkflowRepositoryError) as error:
            repository.admit_step(claim, step_key="develop", generation=1, expected_revision=2,
                                  target_key="node-a", capacity=1, reserve_tokens=1, reserve_requests=1, now=102, connection=connection)
        assert isinstance(error.value.__cause__, sqlite3.IntegrityError)
    with repository.transaction() as connection:
        for table in ("workflow_dispatches", "workflow_budget_reservations", "capability_reservations"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert repository.current_steps(OWNER, "workflow-a")[0].state == "ready"


def test_readers_do_not_take_sqlite_writer_lock(repository: WorkflowRepository) -> None:
    create(repository)
    with repository.transaction() as connection:
        connection.execute("UPDATE workflow_runs SET revision=2 WHERE workflow_id='workflow-a'")
        assert repository.get(OWNER, "workflow-a").revision == 1


def test_fresh_schema_initialization_is_atomic_across_processes(tmp_path: Path) -> None:
    path = tmp_path / "platform.db"
    PlatformRepository(path).init()
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    children = [context.Process(target=_concurrent_init_and_claim, args=(str(path), f"worker-{index}", output)) for index in range(2)]
    try:
        for child in children:
            child.start()
        assert [output.get(timeout=15) for _ in children] == [("ok", 0), ("ok", 0)]
        for child in children:
            child.join(15)
            assert child.exitcode == 0
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(5)
        output.close()
        output.join_thread()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM workflow_schema_meta").fetchone()[0] == 1


def test_capture_gc_preserves_workflow_evidence_without_runtime_gate(repository: WorkflowRepository, tmp_path: Path) -> None:
    from hub.infrastructure.browser_capture_gc import BrowserCaptureGcRepository
    from hub.infrastructure.command_repository import CommandRepository

    create(repository)
    CommandRepository(repository.db_path).init()
    root = tmp_path / "artifacts"
    artifact = ArtifactStore(root, clock=lambda: 0).put_bytes(OWNER, WORKSPACE, b"\x89PNG\r\n\x1a\nbounded-png")
    with repository.transaction() as connection:
        connection.execute("CREATE TABLE browser_artifact_tickets(ticket_id TEXT PRIMARY KEY,state TEXT,content_type TEXT,artifact_id TEXT,owner_id TEXT,workspace_id TEXT,consumed_at REAL)")
        connection.execute("INSERT INTO browser_artifact_tickets VALUES('ticket','consumed','image/png',?,?,?,0)",
                           (artifact["artifact_id"], OWNER, WORKSPACE))
        connection.execute(
            "INSERT INTO workflow_evidence(owner_id,workspace_id,workflow_id,step_key,generation,evidence_key,validator_ref,subject_digest,source,result,report_artifact_id,report_hash,report_size,content_digest,created_at) "
            "VALUES(?,?,?,'develop',1,'evidence','fixed@1',?,'fixed_validator','passed',?,?,?,?,100)",
            (OWNER, WORKSPACE, "workflow-a", "a" * 64, artifact["artifact_id"], artifact["sha256"], artifact["size"], "b" * 64),
        )
    gc = BrowserCaptureGcRepository(repository.db_path, root)
    gc.init()
    gc._mark_candidates(100, 3000000, 128)
    with repository.transaction() as connection:
        assert connection.execute("SELECT count(*) FROM browser_capture_gc").fetchone()[0] == 0
    assert (root / artifact["artifact_id"] / "content").is_file()


def test_capture_gc_rejects_unrecognized_workflow_schema(repository: WorkflowRepository, tmp_path: Path) -> None:
    from hub.infrastructure.browser_capture_gc import BrowserCaptureGcError, BrowserCaptureGcRepository
    from hub.infrastructure.command_repository import CommandRepository

    CommandRepository(repository.db_path).init()
    with repository.transaction() as connection:
        connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=2")
    with repository.transaction(write=False) as connection:
        with pytest.raises(BrowserCaptureGcError, match="capture_gc_reference_surface_unknown"):
            BrowserCaptureGcRepository(repository.db_path, tmp_path / "artifacts")._reference_surfaces(connection)


def test_deep_cursor_page_has_bounded_database_work(repository: WorkflowRepository, monkeypatch: pytest.MonkeyPatch) -> None:
    first = create(repository)
    with repository.transaction() as connection:
        connection.executemany(
            "INSERT INTO workflow_runs(workflow_id,owner_id,workspace_id,client_token,request_digest,definition_snapshot,definition_digest,"
            "acceptance_required,next_poll_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,0,0,0)",
            ((f"wf-{index:05}", OWNER, WORKSPACE, f"page-{index}", "a" * 64, first.definition_snapshot,
              first.definition_digest, 1) for index in range(2048)),
        )
    original = repository.transaction
    ticks = 0

    def progress() -> int:
        nonlocal ticks
        ticks += 1
        return int(ticks > 50)

    @contextmanager
    def bounded_transaction(*, write: bool = True):
        with original(write=write) as connection:
            connection.set_progress_handler(progress, 100)
            yield connection

    monkeypatch.setattr(repository, "transaction", bounded_transaction)
    rows = repository.list(OWNER, before="wf-00010", limit=5)
    assert [row.workflow_id for row in rows] == [f"wf-{index:05}" for index in range(9, 4, -1)]
    assert ticks <= 50
