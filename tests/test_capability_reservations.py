"""Real SQLite admissions: capacity, identity, restart and failure atomicity."""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from hub.domain.capability import CapabilityStoreError, NodePolicy
from hub.domain.workflow_contract import parse_definition, resolve_contract
from hub.domain.workflow_contract_types import AuthorizedInputs, CatalogRef
from hub.infrastructure.capability_repository import CapabilityRepository
from hub.infrastructure.platform_db import PlatformRepository
from hub.infrastructure.workflow_repository import WorkflowRepositoryError
from hub.infrastructure.platform_backup import BackupError, PlatformBackupService
from hub.infrastructure.browser_capture_gc import BrowserCaptureGcError, BrowserCaptureGcRepository
from test_capability_router import BOOT, CAP, OWNER, WORKSPACE, definition, offer_payload, requirements
from test_workflow_repository import prepare_database, registered


def setup_catalog(tmp_path: Path, *, slots=1, count=1):
    workflows = prepare_database(tmp_path / "platform.db")
    platform = PlatformRepository(workflows.db_path)
    platform.upsert_node(OWNER, {"node_id": "node-a", "capabilities": {"legacy": True}})
    credential = platform.provision_node_credential(OWNER, "node-a", secret="n" * 32)
    fingerprint = platform.authenticate_node_credential("node-a", "n" * 32)["credential_fingerprint"]
    repository = CapabilityRepository(workflows, boot_id=BOOT)
    repository.init()
    repository.register_definition(definition(), now=100)
    policy = NodePolicy(OWNER, "node-a", (WORKSPACE,), (CatalogRef("fixture", 1),), (CAP,), slot_limit=slots,
        input_aliases=("source",), residency_aliases=("private",), allowed_risks=("workspace_read",))
    repository.register_node(policy, now=100)
    raw = json.dumps(offer_payload(slots=slots)).encode()
    repository.record_offer(OWNER, "node-a", raw, credential_fingerprint=fingerprint, now=100)
    source = json.loads(registered().snapshot)
    source["steps"][0]["contract"]["capability_requirements"] = [{"id": CAP.id, "revisions": [1]}]
    workflow_definition = parse_definition(json.dumps(source).encode())
    resolved = resolve_contract(workflow_definition, AuthorizedInputs(OWNER, WORKSPACE, "develop", CatalogRef("cli", 1), ()))
    prepared = []
    for index in range(count):
        name = f"workflow-{index:03}"
        workflows.create_or_get(OWNER, WORKSPACE, name, client_token=name, definition=workflow_definition, now=100)
    claims = workflows.claim_due(OWNER, worker_id="coordinator", now=100, limit=count, lease_seconds=60)
    for claim in claims:
        row = workflows.freeze_inputs(claim, step_key="develop", generation=1, resolved=resolved, expected_revision=1, now=101)
        prepared.append((claim, row.revision))
    req = requirements(input_digest=resolved.digest)
    return repository, platform, policy, fingerprint, credential, prepared, req


def admit(repository, prepared, req, *, now=102):
    claim, revision = prepared
    return repository.admit(claim, step_key="develop", generation=1, expected_revision=revision,
        requirements=req, reserve_tokens=100, reserve_requests=2, now=now)


def test_admission_and_duplicate_claim_share_original_intent_budget_slot_and_binding(tmp_path):
    repository, platform, policy, fingerprint, credential, prepared, req = setup_catalog(tmp_path)
    first = admit(repository, prepared[0], req)
    assert first.intent.target_key == "node-a"
    assert first.selection.binding.input_digest == req.input_digest
    assert admit(repository, prepared[0], req) == first
    with repository.workflows.transaction(write=False) as connection:
        for table in ("workflow_dispatches", "workflow_budget_reservations", "capability_reservations", "capability_step_bindings"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1
    platform.upsert_node(OWNER, {"node_id": "node-a", "enabled": False})
    assert admit(repository, prepared[0], req) == first
    with pytest.raises(CapabilityStoreError, match="binding_conflict"):
        admit(repository, prepared[0], replace(req, disk_bytes=201))


def test_failure_while_saving_binding_rolls_back_every_admission_write(tmp_path):
    repository, _, _, _, _, prepared, req = setup_catalog(tmp_path)
    with repository.workflows.transaction() as connection:
        connection.execute("CREATE TRIGGER reject_binding BEFORE INSERT ON capability_step_bindings BEGIN SELECT RAISE(ABORT,'test binding failure'); END")
    before = repository.workflows.events(OWNER, prepared[0][0].workflow_id).events
    with pytest.raises(CapabilityStoreError) as failure:
        admit(repository, prepared[0], req)
    assert failure.value.__cause__ is not None
    with repository.workflows.transaction(write=False) as connection:
        for table in ("workflow_dispatches", "workflow_budget_reservations", "capability_reservations", "capability_step_bindings"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert repository.workflows.events(OWNER, prepared[0][0].workflow_id).events == before
    assert repository.workflows.current_steps(OWNER, prepared[0][0].workflow_id)[0].state == "ready"


def test_second_admission_waits_and_unknown_or_isolated_does_not_release_slot(tmp_path):
    repository, _, _, _, _, prepared, req = setup_catalog(tmp_path, count=2)
    admit(repository, prepared[0], req)
    with repository.workflows.transaction() as connection:
        connection.execute("UPDATE capability_reservations SET state='isolated'")
        connection.execute("UPDATE workflow_steps SET state='unknown' WHERE workflow_id=?", (prepared[0][0].workflow_id,))
    second = admit(repository, prepared[1], req)
    assert second.intent is None
    assert second.selection.exclusions[0].reason_code == "capacity_exhausted"
    assert repository.workflows.current_steps(OWNER, prepared[1][0].workflow_id)[0].state == "waiting_node"


def _contend(repository, prepared, req, gate, output):
    try:
        assert gate.wait(10)
        result = admit(repository, prepared, req)
        output.put("admitted" if result.intent else "waiting_node")
    except BaseException as exc:
        output.put(type(exc).__name__ + ":" + str(exc))


def test_two_real_processes_cannot_oversell_last_node_slot(tmp_path):
    repository, _, _, _, _, prepared, req = setup_catalog(tmp_path, count=2)
    context = multiprocessing.get_context("spawn")
    gate, output = context.Event(), context.Queue()
    children = [context.Process(target=_contend, args=(repository, item, req, gate, output)) for item in prepared]
    for child in children:
        child.start()
    gate.set()
    try:
        assert sorted(output.get(timeout=20) for child in children) == ["admitted", "waiting_node"]
        for child in children:
            child.join(10)
            assert child.exitcode == 0
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
            child.join(10)


def test_offer_restart_requires_a_new_authenticated_report_even_at_same_wall_time(tmp_path):
    repository, _, _, fingerprint, _, _, req = setup_catalog(tmp_path)
    restarted = CapabilityRepository(repository.workflows, boot_id="new-boot")
    restarted.init()
    assert restarted.select(req, now=100).exclusions[0].reason_code == "offer_stale"
    restarted.record_offer(OWNER, "node-a", json.dumps(offer_payload(slots=1)).encode(), credential_fingerprint=fingerprint, now=100)
    assert restarted.select(req, now=100).binding is not None
    assert repository.select(req, now=100).binding is not None


def test_catalog_and_offer_revisions_are_immutable_but_resource_measurements_can_refresh(tmp_path):
    repository, _, _, fingerprint, _, _, req = setup_catalog(tmp_path)
    repository.register_definition(definition(), now=101)
    with pytest.raises(CapabilityStoreError, match="definition_conflict"):
        repository.register_definition(definition(risk="write"), now=101)
    changed = offer_payload(slots=1, software={"python": "3.13.7"})
    with pytest.raises(CapabilityStoreError, match="offer_revision_conflict"):
        repository.record_offer(OWNER, "node-a", json.dumps(changed).encode(), credential_fingerprint=fingerprint, now=101)
    repository.record_offer(OWNER, "node-a", json.dumps(offer_payload(slots=1, memory_bytes=900)).encode(), credential_fingerprint=fingerprint, now=101)
    changed["offer_revision"] = 2
    repository.record_offer(OWNER, "node-a", json.dumps(changed).encode(), credential_fingerprint=fingerprint, now=102)
    assert repository.select(req, now=102).exclusions[0].reason_code == "software_mismatch"
    with pytest.raises(CapabilityStoreError, match="offer_revision_conflict"):
        repository.record_offer(OWNER, "node-a", json.dumps(offer_payload()).encode(), credential_fingerprint=fingerprint, now=103)


@pytest.mark.parametrize("change", ["disable", "rotate"])
def test_report_transaction_rechecks_original_authenticated_credential(tmp_path, change):
    repository, platform, _, fingerprint, _, _, _ = setup_catalog(tmp_path)
    if change == "disable":
        with repository.workflows.transaction() as connection:
            connection.execute("UPDATE node_credentials SET enabled=0 WHERE owner_id=? AND node_id=?", (OWNER, "node-a"))
    else:
        platform.provision_node_credential(OWNER, "node-a", secret="r" * 32)
    with pytest.raises(CapabilityStoreError, match="credential_revoked"):
        repository.record_offer(OWNER, "node-a", json.dumps(offer_payload()).encode(), credential_fingerprint=fingerprint, now=101)


def test_requirements_must_match_original_frozen_contract_and_current_claim(tmp_path):
    repository, _, _, _, _, prepared, req = setup_catalog(tmp_path)
    for changed in (replace(req, input_digest="b" * 64), replace(req, template=CatalogRef("other", 1)),
                    replace(req, adapter_id="other"), replace(req, max_active_seconds=59)):
        with pytest.raises(CapabilityStoreError, match="requirements_conflict"):
            admit(repository, prepared[0], changed)
    with pytest.raises(CapabilityStoreError) as failure:
        admit(repository, prepared[0], req, now=161)
    assert isinstance(failure.value.__cause__, WorkflowRepositoryError)


@pytest.mark.parametrize("encrypted", [False, True])
def test_backup_preserves_bindings_frozen_state_and_requires_new_reports(tmp_path, encrypted):
    repository, _, _, _, _, prepared, req = setup_catalog(tmp_path)
    admitted = admit(repository, prepared[0], req)
    root = tmp_path / "artifacts"
    root.mkdir()
    service = PlatformBackupService()
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    if encrypted:
        service.create_encrypted_backup(repository.workflows.db_path, root, backup, b"k" * 32, "key-a")
        result = service.restore_encrypted_backup(backup, restored, b"k" * 32)
    else:
        service.create_backup(repository.workflows.db_path, root, backup)
        result = service.restore_backup(backup, restored)
    with sqlite3.connect(restored / "platform.db") as connection:
        assert connection.execute("SELECT minimum_reader_version,admission_mode FROM workflow_schema_meta").fetchone() == (2, "frozen")
        assert connection.execute("SELECT snapshot FROM capability_step_bindings").fetchone()[0] == admitted.selection.binding.snapshot
        assert connection.execute("SELECT state FROM capability_reservations").fetchone()[0] == "held"
    with repository.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT admission_mode FROM workflow_schema_meta").fetchone()[0] == "open"


@pytest.mark.parametrize("table", ["capability_definitions", "capability_node_policies", "capability_node_offers", "capability_step_bindings"])
def test_backup_validates_capability_snapshot_content_even_with_recomputed_digest(tmp_path, table):
    repository, _, _, _, _, prepared, req = setup_catalog(tmp_path)
    admit(repository, prepared[0], req)
    with repository.workflows.transaction() as connection:
        row = connection.execute(f"SELECT snapshot FROM {table}").fetchone()
        value = json.loads(bytes(row[0]))
        if table == "capability_definitions":
            value["capability_id"] = "different"
        elif table == "capability_node_policies":
            value["owner_id"] = "other"
        elif table == "capability_node_offers":
            value["slots"] = 1024
        else:
            value["node_id"] = "different"
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        connection.execute(f"UPDATE {table} SET snapshot=?", (raw,))
        if table != "capability_node_offers":
            connection.execute(f"UPDATE {table} SET digest=?", (hashlib.sha256(raw).hexdigest(),))
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    with pytest.raises(BackupError):
        PlatformBackupService().create_backup(repository.workflows.db_path, artifacts, tmp_path / "backup")
    assert not (tmp_path / "backup").exists()


def test_unknown_capability_schema_is_rejected_by_gate_off_backup_and_gc(tmp_path):
    repository, _, _, _, _, _, req = setup_catalog(tmp_path)
    with repository.workflows.transaction() as connection:
        connection.execute("UPDATE capability_schema_meta SET schema_version=99")
    with pytest.raises(CapabilityStoreError, match="unsupported_capability_schema"):
        CapabilityRepository(repository.workflows, boot_id="new-boot").init()
    root = tmp_path / "artifacts"
    root.mkdir()
    with pytest.raises(BackupError, match="unsupported_capability_schema"):
        PlatformBackupService().create_backup(repository.workflows.db_path, root, tmp_path / "backup")


def test_unknown_legacy_resource_usage_cannot_be_treated_as_zero_bytes(tmp_path):
    repository, _, _, _, _, prepared, req = setup_catalog(tmp_path, slots=2, count=2)
    claim, revision = prepared[0]
    repository.workflows.admit_step(claim, step_key="develop", generation=1, expected_revision=revision,
        target_key="node-a", capacity=2, reserve_tokens=1, reserve_requests=1, now=102)
    result = admit(repository, prepared[1], req)
    assert result.intent is None
    assert result.selection.exclusions[0].reason_code == "resource_evidence_missing"


def test_duplicate_binding_lookup_does_not_accept_an_unrelated_revision(tmp_path):
    repository, _, _, _, _, prepared, req = setup_catalog(tmp_path)
    admit(repository, prepared[0], req)
    claim, revision = prepared[0]
    with pytest.raises(CapabilityStoreError, match="revision_conflict"):
        admit(repository, (claim, 9999), req)


@pytest.mark.parametrize(("field", "limit", "reason"), [("memory_bytes", 150, "memory_insufficient"), ("disk_bytes", 300, "disk_insufficient")])
def test_resource_reservations_are_aggregated_before_admitting_another_step(tmp_path, field, limit, reason):
    repository, _, _, fingerprint, _, prepared, req = setup_catalog(tmp_path, slots=2, count=2)
    repository.record_offer(OWNER, "node-a", json.dumps(offer_payload(slots=2, **{field: limit})).encode(),
        credential_fingerprint=fingerprint, now=101)
    assert admit(repository, prepared[0], req).intent is not None
    second = admit(repository, prepared[1], req)
    assert second.intent is None and second.selection.exclusions[0].reason_code == reason


@pytest.mark.parametrize("mode", ["drain", "frozen", "pause", "cancel"])
def test_original_workflow_controls_fence_new_capability_admission(tmp_path, mode):
    repository, _, _, _, _, prepared, req = setup_catalog(tmp_path)
    with repository.workflows.transaction() as connection:
        if mode in {"drain", "frozen"}:
            connection.execute("UPDATE workflow_schema_meta SET admission_mode=?", (mode,))
        else:
            column = "pause_requested" if mode == "pause" else "cancel_requested"
            connection.execute(f"UPDATE workflow_runs SET {column}=1")
    with pytest.raises(CapabilityStoreError):
        admit(repository, prepared[0], req)
    with repository.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM workflow_dispatches").fetchone()[0] == 0


def test_expired_coordinator_lease_does_not_free_unknown_execution_capacity(tmp_path):
    repository, _, _, _, _, prepared, req = setup_catalog(tmp_path, count=2)
    assert admit(repository, prepared[0], req).intent is not None
    with repository.workflows.transaction() as connection:
        connection.execute("UPDATE workflow_steps SET state='unknown' WHERE workflow_id=?", (prepared[0][0].workflow_id,))
        connection.execute("UPDATE workflow_runs SET execution_state='unknown' WHERE workflow_id=?", (prepared[0][0].workflow_id,))
    claims = repository.workflows.claim_due(OWNER, worker_id="new-coordinator", now=161, limit=2, lease_seconds=60)
    second = next(claim for claim in claims if claim.workflow_id == prepared[1][0].workflow_id)
    outcome = admit(repository, (second, prepared[1][1]), req, now=161)
    assert outcome.intent is None and outcome.selection.exclusions[0].reason_code == "capacity_exhausted"


@pytest.mark.parametrize("surface", ["resolved_digest", "definition_digest"])
def test_admission_rejects_corrupt_stored_snapshot_identity(tmp_path, surface):
    repository, _, _, _, _, prepared, req = setup_catalog(tmp_path)
    with repository.workflows.transaction() as connection:
        table = "workflow_steps" if surface == "resolved_digest" else "workflow_runs"
        connection.execute(f"UPDATE {table} SET {surface}=?", ("b" * 64,))
    with pytest.raises(CapabilityStoreError, match="capability_snapshot_corrupt"):
        admit(repository, prepared[0], req)


def test_backup_rejects_binding_without_original_budget_reservation(tmp_path):
    repository, _, _, _, _, prepared, req = setup_catalog(tmp_path)
    admit(repository, prepared[0], req)
    with repository.workflows.transaction() as connection:
        connection.execute("UPDATE workflow_budget_reservations SET reserved_tokens=reserved_tokens+1")
        connection.execute("ALTER TABLE workflow_budget_reservations RENAME TO broken_budget_reservations")
        connection.execute("CREATE TABLE workflow_budget_reservations AS SELECT * FROM broken_budget_reservations WHERE 0")
    root = tmp_path / "artifacts"
    root.mkdir()
    with pytest.raises(BackupError):
        PlatformBackupService().create_backup(repository.workflows.db_path, root, tmp_path / "backup")
